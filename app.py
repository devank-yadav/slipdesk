import os
import io
import zipfile
import sqlite3
import secrets
import hashlib
from urllib.parse import urlencode
from datetime import datetime, date, timedelta

from flask import Flask, render_template, request, send_file, redirect, url_for, session, jsonify
from functools import lru_cache

stringWidth = None
try:
    import libsql_experimental as libsql
except Exception:
    libsql = None

GOOGLE_MAPS_API_KEY = os.environ.get('GOOGLE_MAPS_API_KEY', '')

# ---------- PDF DESIGN CONSTANTS ----------
COLOR_DARK   = (0.13, 0.13, 0.15)
COLOR_TEXT   = (0.05, 0.05, 0.07)
COLOR_MUTED  = (0.42, 0.42, 0.46)
COLOR_LINE   = (0.55, 0.55, 0.58)
COLOR_HEADER = (0.10, 0.10, 0.12)


@lru_cache(maxsize=1)
def _pdf_libs():
    # Lazy-load heavy PDF deps to reduce cold-start time.
    from reportlab.pdfgen import canvas as _canvas
    from reportlab.lib.pagesizes import A4 as _A4, landscape as _landscape
    from reportlab.pdfbase.pdfmetrics import stringWidth as _stringWidth
    from pypdf import PdfWriter as _PdfWriter, PdfReader as _PdfReader
    return {
        'canvas': _canvas,
        'A4': _A4,
        'landscape': _landscape,
        'stringWidth': _stringWidth,
        'PdfWriter': _PdfWriter,
        'PdfReader': _PdfReader,
    }


def _string_width(text, font_name, font_size):
    global stringWidth
    if stringWidth is None:
        stringWidth = _pdf_libs()['stringWidth']
    return stringWidth(text, font_name, font_size)


def _wrap_text(text, font_name, font_size, max_width):
    if not text:
        return []
    words = text.split()
    lines, current = [], ''
    for w in words:
        test = (current + ' ' + w).strip()
        if _string_width(test, font_name, font_size) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def _format_date(d):
    try:
        return datetime.strptime(d, '%Y-%m-%d').strftime('%d / %m / %Y')
    except Exception:
        return d or ''


def _route_positions(n_lines):
    """Return dynamic y-coordinates for route lines, driver label, and signature."""
    route_y = 248
    n = max(n_lines, 1)
    line_h = 22 if n <= 5 else 16
    line_ys = [route_y - i * line_h for i in range(n)]
    last_y = line_ys[-1]
    driver_y = max(last_y - 36, 95)
    return {'line_ys': line_ys, 'line_h': line_h, 'driver_y': driver_y}


def _draw_tracked_right(c, x_right, y, text, font_name, font_size, tracking):
    """Right-aligned text with extra letter-spacing (tracking)."""
    total_w = sum(_string_width(ch, font_name, font_size) for ch in text) + tracking * (len(text) - 1)
    x = x_right - total_w
    for ch in text:
        c.drawString(x, y, ch)
        x += _string_width(ch, font_name, font_size) + tracking


def _draw_tracked_center(c, x_center, y, text, font_name, font_size, tracking):
    total_w = sum(_string_width(ch, font_name, font_size) for ch in text) + tracking * (len(text) - 1)
    x = x_center - total_w / 2
    for ch in text:
        c.drawString(x, y, ch)
        x += _string_width(ch, font_name, font_size) + tracking


def draw_slip_template(c, width, height, n_route_lines=1):
    """Draw the static chrome of the duty slip (header, labels, lines)."""

    # ---- HEADER: contact info (top-left) ----
    c.setFont('Helvetica', 9)
    c.setFillColorRGB(*COLOR_MUTED)
    c.drawString(40, 568, 'Contact No:')
    c.drawString(40, 553, 'Address:')
    c.setFillColorRGB(*COLOR_DARK)
    c.drawString(108, 568, '9718305627, 9718305628')
    c.drawString(108, 553, 'B213, Saraswati Enclave, Gurgaon')

    # ---- HEADER: brand (top-right, two lines, wide tracking) ----
    c.setFillColorRGB(*COLOR_HEADER)
    c.setFont('Helvetica-Bold', 28)
    _draw_tracked_right(c, width - 40, 568, 'OSPREY',  'Helvetica-Bold', 28, 6)
    _draw_tracked_right(c, width - 40, 538, 'TRAVELS', 'Helvetica-Bold', 28, 6)

    # ---- Divider ----
    c.setStrokeColorRGB(*COLOR_DARK)
    c.setLineWidth(1.2)
    c.line(40, 525, width - 40, 525)

    # ---- DUTY SLIP NO + DATE block (under header, left side) ----
    c.setFillColorRGB(*COLOR_DARK)
    c.setFont('Helvetica-Bold', 10.5)
    c.drawString(40, 498, 'DUTY SLIP NO:')
    c.drawString(40, 472, 'DATE:')

    c.setStrokeColorRGB(*COLOR_LINE)
    c.setLineWidth(0.5)
    c.setDash(1, 2)
    c.line(135, 494, 320, 494)
    c.line(135, 468, 320, 468)
    c.setDash()

    # ---- TWO-COLUMN BODY ROWS ----
    rows = [
        ('COMPANY NAME:',    'CUSTOMER NAME:'),
        ('TYPE OF VEHICLE:', 'VEHICLE NO:'),
        ('STARTING KM:',     'STARTING TIME:'),
        ('CLOSING KM:',      'CLOSING TIME:'),
        ('TOTAL KM:',        'TOTAL TIME:'),
        ('PROJECT CODE:',    'MAIL APPROVAL DATE:'),
    ]
    base_y, row_h = 432, 28
    label_x_L, line_start_L, line_end_L = 40, 175, 410
    label_x_R, line_start_R, line_end_R = 445, 580, 802

    for i, (left_label, right_label) in enumerate(rows):
        y = base_y - i * row_h
        c.setFont('Helvetica-Bold', 10.5)
        c.setFillColorRGB(*COLOR_DARK)
        c.drawString(label_x_L, y, left_label)
        c.drawString(label_x_R, y, right_label)

        c.setStrokeColorRGB(*COLOR_LINE)
        c.setLineWidth(0.5)
        c.setDash(1, 2)
        c.line(line_start_L, y - 4, line_end_L, y - 4)
        c.line(line_start_R, y - 4, line_end_R, y - 4)
        c.setDash()

    # ---- ROUTE COVERED (dynamic multi-line, full width) ----
    route_y = base_y - 6 * row_h - 16  # = 248
    c.setFont('Helvetica-Bold', 10.5)
    c.setFillColorRGB(*COLOR_DARK)
    c.drawString(40, route_y, 'ROUTE COVERED:')

    pos = _route_positions(n_route_lines)
    c.setStrokeColorRGB(*COLOR_LINE)
    c.setLineWidth(0.5)
    c.setDash(1, 2)
    for ly in pos['line_ys']:
        c.line(155, ly - 4, line_end_R, ly - 4)
    c.setDash()

    # ---- DRIVER NAME (left, below route — dynamic position) ----
    driver_y = pos['driver_y']
    c.setFont('Helvetica-Bold', 10.5)
    c.setFillColorRGB(*COLOR_DARK)
    c.drawString(40, driver_y, 'DRIVER NAME:')
    c.setStrokeColorRGB(*COLOR_LINE)
    c.setLineWidth(0.5)
    c.setDash(1, 2)
    c.line(135, driver_y - 4, line_end_L, driver_y - 4)
    c.setDash()

    # ---- USER SIGNATURE (bottom right, always fixed) ----
    c.setStrokeColorRGB(*COLOR_LINE)
    c.setLineWidth(0.7)
    c.setDash(1, 2)
    c.line(575, 78, 802, 78)
    c.setDash()
    c.setFont('Helvetica-Bold', 9)
    c.setFillColorRGB(*COLOR_MUTED)
    _draw_tracked_center(c, 688, 62, 'USER SIGNATURE', 'Helvetica-Bold', 9, 1.2)


def fill_slip_data(c, data):
    """Place dynamic values into the template at the correct coordinates."""
    c.setFillColorRGB(*COLOR_TEXT)
    c.setFont('Helvetica', 11)

    # Duty slip no + Date
    c.drawString(140, 498, data.get('duty_slip_no', '') or '')
    c.drawString(140, 472, _format_date(data.get('date', '')))

    # Two-column rows
    base_y, row_h = 432, 28
    val_x_L, val_x_R = 180, 585

    left_values = [
        data.get('company_name', ''),
        data.get('vehicle_type', ''),
        data.get('starting_km', ''),
        data.get('closing_km', ''),
        data.get('total_km', ''),
        data.get('project_code', ''),
    ]
    right_values = [
        data.get('customer_name', ''),
        data.get('vehicle_no', ''),
        data.get('starting_time', ''),
        data.get('closing_time', ''),
        data.get('total_time', ''),
        _format_date(data.get('mail_approval_date', '')),
    ]
    for i, (lv, rv) in enumerate(zip(left_values, right_values)):
        y = base_y - i * row_h
        c.drawString(val_x_L, y, str(lv or ''))
        c.drawString(val_x_R, y, str(rv or ''))

    # Route covered: all wrapped lines, positions computed dynamically
    route_lines = _wrap_text(data.get('route_covered', '') or '', 'Helvetica', 11, 632)
    pos = _route_positions(len(route_lines))
    for i, line in enumerate(route_lines):
        c.drawString(160, pos['line_ys'][i], line)

    # Driver name at dynamic position
    c.drawString(140, pos['driver_y'], data.get('driver_name', '') or '')


def _build_pdf(data: dict) -> io.BytesIO:
    route_lines = _wrap_text(data.get('route_covered', '') or '', 'Helvetica', 11, 632)
    n = max(len(route_lines), 1)
    pdf = _pdf_libs()
    buf = io.BytesIO()
    c = pdf['canvas'].Canvas(buf, pagesize=pdf['landscape'](pdf['A4']))
    w, h = pdf['landscape'](pdf['A4'])
    draw_slip_template(c, w, h, n_route_lines=n)
    fill_slip_data(c, data)
    c.save()
    buf.seek(0)
    return buf


app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)


def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode('utf-8')).hexdigest()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_DATABASE = os.path.join(BASE_DIR, 'invoices.db')
TURSO_DATABASE_URL = os.getenv('TURSO_DATABASE_URL') or os.getenv('STORAGE_URL')
TURSO_AUTH_TOKEN = os.getenv('TURSO_AUTH_TOKEN') or os.getenv('STORAGE_AUTH_TOKEN')
USE_TURSO = bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN and libsql)

_sqlite_connect = sqlite3.connect
_turso_shared_conn = None


class _ConnectionProxy:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            try:
                self._conn.commit()
            except Exception:
                pass
        if not USE_TURSO:
            self._conn.close()
        return False

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _db_connect(database, *args, **kwargs):
    if USE_TURSO:
        global _turso_shared_conn
        if _turso_shared_conn is None:
            _turso_shared_conn = libsql.connect(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
        # Do not close shared connection on exit; just commit when needed.
        return _ConnectionProxy(_turso_shared_conn)
    return _sqlite_connect(database, *args, **kwargs)


# Keep existing sqlite3.connect(...) call sites working with Turso.
sqlite3.connect = _db_connect

if USE_TURSO:
    DATABASE = TURSO_DATABASE_URL
elif os.getenv('VERCEL'):
    DATABASE = '/tmp/invoices.db'
else:
    DATABASE = SOURCE_DATABASE


def init_db():
    with sqlite3.connect(DATABASE) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT,
                date TEXT,
                file_path TEXT,
                admin_username TEXT
            )
        ''')
        columns = [col[1] for col in conn.execute("PRAGMA table_info(invoices)").fetchall()]
        required_columns = [
            ('created_at', 'TEXT'),
            ('driver_name', 'TEXT'),
            ('company_name', 'TEXT'),
            ('duty_slip_no', 'TEXT'),
            ('vehicle_type', 'TEXT'),
            ('vehicle_no', 'TEXT'),
            ('starting_km', 'TEXT'),
            ('closing_km', 'TEXT'),
            ('total_km', 'TEXT'),
            ('starting_time', 'TEXT'),
            ('closing_time', 'TEXT'),
            ('total_time', 'TEXT'),
            ('dn', 'TEXT'),
            ('remarks', 'TEXT'),
            ('route_covered', 'TEXT'),
            ('bill_status', 'TEXT'),
            ('payment_date', 'TEXT'),
            ('project_code', 'TEXT'),
            ('mail_approval_date', 'TEXT'),
        ]
        for col_name, col_type in required_columns:
            if col_name not in columns:
                conn.execute(f"ALTER TABLE invoices ADD COLUMN {col_name} {col_type}")
        # Backfill status for existing rows
        conn.execute("UPDATE invoices SET bill_status = 'Bill Generated' WHERE bill_status IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_created_at ON invoices(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_bill_status ON invoices(bill_status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_driver_name ON invoices(driver_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_vehicle_type ON invoices(vehicle_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_customer_name ON invoices(customer_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_duty_slip_no ON invoices(duty_slip_no)")

        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                password TEXT
            )
        ''')
        existing = conn.execute("SELECT id, password FROM users WHERE username = ?", ("admin",)).fetchone()
        if not existing:
            conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", ("admin", _hash_pw("admin")))
        elif existing[1] and len(existing[1]) != 64:
            # Migrate plaintext password to sha256 hash
            conn.execute("UPDATE users SET password = ? WHERE id = ?", (_hash_pw(existing[1]), existing[0]))

        conn.execute('''
            CREATE TABLE IF NOT EXISTS drivers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS vehicles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                vehicle_no TEXT
            )
        ''')
        # Migrate vehicles table — add vehicle_no if missing
        veh_cols = [col[1] for col in conn.execute("PRAGMA table_info(vehicles)").fetchall()]
        if 'vehicle_no' not in veh_cols:
            conn.execute("ALTER TABLE vehicles ADD COLUMN vehicle_no TEXT")

        conn.execute('''
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                company TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS slip_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_username TEXT NOT NULL,
                template_name TEXT NOT NULL,
                customer_name TEXT, company_name TEXT,
                vehicle_type TEXT, vehicle_no TEXT,
                route_covered TEXT, dn TEXT, remarks TEXT,
                driver_name TEXT, starting_km TEXT, total_km TEXT,
                created_at TEXT,
                use_count INTEGER DEFAULT 0,
                last_used TEXT
            )
        ''')
        # Migrate existing slip_templates — add new columns if missing
        tpl_cols = [col[1] for col in conn.execute("PRAGMA table_info(slip_templates)").fetchall()]
        if 'use_count' not in tpl_cols:
            conn.execute("ALTER TABLE slip_templates ADD COLUMN use_count INTEGER DEFAULT 0")
        if 'last_used' not in tpl_cols:
            conn.execute("ALTER TABLE slip_templates ADD COLUMN last_used TEXT")
        if 'project_code' not in tpl_cols:
            conn.execute("ALTER TABLE slip_templates ADD COLUMN project_code TEXT")
        if 'mail_approval_date' not in tpl_cols:
            conn.execute("ALTER TABLE slip_templates ADD COLUMN mail_approval_date TEXT")
        if 'starting_time' not in tpl_cols:
            conn.execute("ALTER TABLE slip_templates ADD COLUMN starting_time TEXT")
        if 'closing_time' not in tpl_cols:
            conn.execute("ALTER TABLE slip_templates ADD COLUMN closing_time TEXT")
        if 'route_stops_json' not in tpl_cols:
            conn.execute("ALTER TABLE slip_templates ADD COLUMN route_stops_json TEXT")


init_db()


def delete_invoice_file(invoice_id):
    with sqlite3.connect(DATABASE) as conn:
        conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))


def get_next_duty_slip_no():
    with sqlite3.connect(DATABASE) as conn:
        rows = conn.execute(
            "SELECT duty_slip_no FROM invoices WHERE duty_slip_no IS NOT NULL AND duty_slip_no != '' ORDER BY id DESC LIMIT 200",
        ).fetchall()
    max_num = 0
    for (s,) in rows:
        digits = ''.join(c for c in s if c.isdigit())
        if digits:
            n = int(digits)
            if n > max_num:
                max_num = n
    return str(max_num + 1) if max_num > 0 else ''


@app.route('/')
def home():
    if 'admin' in session:
        return redirect(url_for('admin_portal'))
    return redirect(url_for('login_page'))


@app.route('/login')
def login_page():
    return render_template('admin_login.html')


@app.route('/admin_login', methods=['POST'])
def admin_login():
    username = request.form['username']
    password = request.form['password']
    with sqlite3.connect(DATABASE) as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE username = ? AND password = ?",
            (username, _hash_pw(password))
        ).fetchone()
    if user:
        session['admin'] = username
        return redirect(url_for('admin_portal'))
    return render_template('admin_login.html', error="Invalid credentials")


@app.route('/logout')
def logout():
    session.pop('admin', None)
    return redirect(url_for('home'))


# --------------------------
# ADMIN PORTAL
# --------------------------
@app.route('/admin_portal')
def admin_portal():
    if 'admin' not in session:
        return redirect(url_for('home'))

    search_q       = request.args.get('q', '').strip()
    customer_name  = request.args.get('customer_name', '').strip()
    driver_filter  = request.args.get('driver', '').strip()
    vehicle_filter = request.args.get('vehicle', '').strip()
    month_filter   = request.args.get('month', '')
    date_from      = request.args.get('date_from', '')
    date_to        = request.args.get('date_to', '')
    status_filter  = request.args.get('status', '')
    date_quick     = request.args.get('date_quick', '')    # today | week | month | last_month | last7
    status_quick   = request.args.get('status_quick', '')  # pending_bill | pending_pay | paid

    page = max(int(request.args.get('page', 1) or 1), 1)
    page_size = int(request.args.get('page_size', 50) or 50)
    page_size = min(max(page_size, 20), 200)
    offset = (page - 1) * page_size
    base_args = request.args.to_dict(flat=True)
    base_args.pop('page', None)
    base_args.pop('page_size', None)
    base_query = urlencode({k: v for k, v in base_args.items() if v not in (None, '')})

    where_clause = "WHERE 1=1"
    params = []

    # Date quick filter
    today = date.today()
    if date_quick == 'today':
        where_clause += " AND date = ?"
        params.append(today.strftime('%Y-%m-%d'))
    elif date_quick == 'week':
        monday = today - timedelta(days=today.weekday())
        where_clause += " AND date >= ?"
        params.append(monday.strftime('%Y-%m-%d'))
    elif date_quick == 'month':
        where_clause += " AND strftime('%Y-%m', date) = ?"
        params.append(today.strftime('%Y-%m'))
    elif date_quick == 'last_month':
        first_this = today.replace(day=1)
        last_prev  = first_this - timedelta(days=1)
        where_clause += " AND strftime('%Y-%m', date) = ?"
        params.append(last_prev.strftime('%Y-%m'))
    elif date_quick == 'last7':
        seven_ago = today - timedelta(days=7)
        where_clause += " AND date >= ?"
        params.append(seven_ago.strftime('%Y-%m-%d'))

    # Status quick filter (independent — can combine with date_quick)
    if status_quick == 'pending_bill':
        where_clause += " AND COALESCE(bill_status,'Bill Generated') = 'Bill Generated'"
    elif status_quick == 'pending_pay':
        where_clause += " AND bill_status = 'Bill Submitted'"
    elif status_quick == 'paid':
        where_clause += " AND bill_status = 'Payment Received'"

    if search_q:
        where_clause += " AND (customer_name LIKE ? OR company_name LIKE ? OR vehicle_no LIKE ? OR vehicle_type LIKE ? OR driver_name LIKE ? OR duty_slip_no LIKE ?)"
        like = f"%{search_q}%"
        params.extend([like, like, like, like, like, like])
    if customer_name:
        where_clause += " AND customer_name LIKE ?"
        params.append(f"%{customer_name}%")
    if driver_filter:
        where_clause += " AND driver_name = ?"
        params.append(driver_filter)
    if vehicle_filter:
        where_clause += " AND vehicle_type = ?"
        params.append(vehicle_filter)
    if month_filter:
        where_clause += " AND strftime('%Y-%m', created_at) = ?"
        params.append(month_filter)
    if date_from:
        where_clause += " AND date >= ?"
        params.append(date_from)
    if date_to:
        where_clause += " AND date <= ?"
        params.append(date_to)
    if status_filter:
        where_clause += " AND bill_status = ?"
        params.append(status_filter)

    query = f"""
        SELECT id, customer_name, company_name, date, duty_slip_no,
               file_path, created_at, driver_name,
               COALESCE(bill_status, 'Bill Generated') as bill_status,
               payment_date,
               vehicle_type, vehicle_no, starting_km, closing_km, total_km,
               starting_time, closing_time, total_time, dn, remarks, route_covered,
               project_code, mail_approval_date
        FROM invoices {where_clause}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """

    with sqlite3.connect(DATABASE) as conn:
        count_row = conn.execute(f"SELECT COUNT(*) FROM invoices {where_clause}", tuple(params)).fetchone()
        total_results = count_row[0] if count_row else 0
        invoices = conn.execute(query, tuple(params + [page_size, offset])).fetchall()
        driver_names = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT driver_name FROM invoices WHERE driver_name IS NOT NULL AND driver_name != '' ORDER BY driver_name ASC"
            ).fetchall() if row[0]
        ]
        vehicle_names = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT vehicle_type FROM invoices WHERE vehicle_type IS NOT NULL AND vehicle_type != '' ORDER BY vehicle_type ASC"
            ).fetchall() if row[0]
        ]
        # Dashboard hero stats — compute in one round-trip
        stats = conn.execute("""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN strftime('%Y-%m', date) = ? THEN 1 ELSE 0 END) AS this_month,
              SUM(CASE WHEN COALESCE(bill_status,'Bill Generated') = 'Bill Generated' THEN 1 ELSE 0 END) AS pending_bill,
              SUM(CASE WHEN bill_status = 'Bill Submitted' THEN 1 ELSE 0 END) AS pending_pay,
              SUM(CASE WHEN bill_status = 'Payment Received' AND strftime('%Y-%m', payment_date) = ? THEN 1 ELSE 0 END) AS paid_month
            FROM invoices
        """, (today.strftime('%Y-%m'), today.strftime('%Y-%m'))).fetchone()

        stats_total = stats[0] if stats else 0
        stats_this_month = stats[1] if stats else 0
        stats_pending_bill = stats[2] if stats else 0
        stats_pending_pay = stats[3] if stats else 0
        stats_paid_month = stats[4] if stats else 0

    return render_template('admin_portal.html',
                           invoices=invoices,
                           driver_names=driver_names,
                           vehicle_names=vehicle_names,
                           search_q=search_q,
                           customer_name=customer_name,
                           driver_filter=driver_filter,
                           vehicle_filter=vehicle_filter,
                           month_filter=month_filter,
                           date_from=date_from,
                           date_to=date_to,
                           status_filter=status_filter,
                           date_quick=date_quick,
                           status_quick=status_quick,
                           stats_total=stats_total,
                           stats_this_month=stats_this_month,
                           stats_pending_bill=stats_pending_bill,
                           stats_pending_pay=stats_pending_pay,
                           stats_paid_month=stats_paid_month,
                           today_str=today.strftime('%A, %d %B %Y'),
                           page=page,
                           page_size=page_size,
                           total_results=total_results,
                           base_query=base_query)


@app.route('/update_status/<int:invoice_id>', methods=['POST'])
def update_status(invoice_id):
    if 'admin' not in session:
        return jsonify({'ok': False}), 401
    new_status = request.form.get('status', 'Bill Generated')
    payment_date = request.form.get('payment_date', '') or None
    if new_status != 'Payment Received':
        payment_date = None
    with sqlite3.connect(DATABASE) as conn:
        conn.execute(
            "UPDATE invoices SET bill_status = ?, payment_date = ? WHERE id = ?",
            (new_status, payment_date, invoice_id)
        )
    return jsonify({'ok': True})


@app.route('/bulk_update_status', methods=['POST'])
def bulk_update_status():
    if 'admin' not in session:
        return jsonify({'ok': False}), 401
    selected_ids = request.form.getlist('selected_invoices')
    status = request.form.get('status', 'Bill Generated')
    payment_date = request.form.get('payment_date', '') or None
    if status != 'Payment Received':
        payment_date = None
    if not selected_ids:
        return jsonify({'ok': False, 'error': 'No invoices selected'})
    with sqlite3.connect(DATABASE) as conn:
        placeholders = ','.join('?' * len(selected_ids))
        conn.execute(
            f"UPDATE invoices SET bill_status = ?, payment_date = ? WHERE id IN ({placeholders})",
            [status, payment_date] + selected_ids
        )
    return jsonify({'ok': True, 'updated': len(selected_ids), 'status': status, 'payment_date': payment_date or ''})


@app.route('/delete_invoice/<int:invoice_id>', methods=['POST'])
def delete_invoice(invoice_id):
    if 'admin' not in session:
        return redirect(url_for('home'))
    delete_invoice_file(invoice_id)
    return redirect(url_for('admin_portal'))


@app.route('/bulk_action', methods=['POST'])
def bulk_action():
    if 'admin' not in session:
        return redirect(url_for('home'))

    selected_ids = request.form.getlist('selected_invoices')
    action = request.form.get('action')
    if not selected_ids:
        return redirect(url_for('admin_portal'))

    with sqlite3.connect(DATABASE) as conn:
        placeholders = ','.join('?' for _ in selected_ids)

        if action == 'delete':
            conn.execute(f"DELETE FROM invoices WHERE id IN ({placeholders})", selected_ids)
            return redirect(url_for('admin_portal'))

        elif action == 'download':
            rows = conn.execute(
                f"""SELECT id, duty_slip_no, customer_name, company_name, date,
                           vehicle_type, vehicle_no, starting_km, closing_km, total_km,
                           starting_time, closing_time, total_time,
                           project_code, mail_approval_date, route_covered, driver_name
                    FROM invoices WHERE id IN ({placeholders})""",
                selected_ids
            ).fetchall()
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w') as zipf:
                for r in rows:
                    data = {
                        'duty_slip_no': r[1], 'customer_name': r[2], 'company_name': r[3],
                        'date': r[4], 'vehicle_type': r[5], 'vehicle_no': r[6],
                        'starting_km': r[7], 'closing_km': r[8], 'total_km': r[9],
                        'starting_time': r[10], 'closing_time': r[11], 'total_time': r[12],
                        'project_code': r[13], 'mail_approval_date': r[14],
                        'route_covered': r[15], 'driver_name': r[16],
                    }
                    pdf_buf = _build_pdf(data)
                    fname = f"slip_{r[1] or r[2].replace(' ', '_')}.pdf"
                    zipf.writestr(fname, pdf_buf.read())
            zip_buffer.seek(0)
            return send_file(zip_buffer, as_attachment=True, download_name='invoices.zip', mimetype='application/zip')

        elif action == 'excel':
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment
            rows = conn.execute(
                f"""SELECT id, duty_slip_no, date, customer_name, company_name,
                           driver_name, vehicle_type, vehicle_no,
                           starting_km, closing_km, total_km,
                           starting_time, closing_time, total_time,
                           route_covered, dn, remarks, project_code,
                           mail_approval_date, bill_status, payment_date,
                           created_at
                    FROM invoices WHERE id IN ({placeholders})
                    ORDER BY date DESC, id DESC""",
                selected_ids,
            ).fetchall()
            wb = Workbook()
            ws = wb.active
            ws.title = "Duty Slips"
            headers = ["ID", "Slip #", "Date", "Customer", "Company",
                       "Driver", "Vehicle Type", "Vehicle No",
                       "Start KM", "Close KM", "Total KM",
                       "Start Time", "Close Time", "Total Time",
                       "Route", "DN", "Remarks", "Project Code",
                       "Mail Approval Date", "Status", "Payment Date",
                       "Created At"]
            ws.append(headers)
            header_font = Font(bold=True, color="FFFFFF")
            header_fill = PatternFill("solid", fgColor="007AFF")
            for cell in ws[1]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center", vertical="center")
            for row in rows:
                ws.append(["" if v is None else v for v in row])
            for col_idx, h in enumerate(headers, 1):
                ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = max(12, len(h) + 2)
            buf = io.BytesIO()
            wb.save(buf)
            buf.seek(0)
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            return send_file(buf, as_attachment=True,
                             download_name=f'duty_slips_{stamp}.xlsx',
                             mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

        elif action == 'print':
            rows = conn.execute(
                f"""SELECT id, duty_slip_no, customer_name, company_name, date,
                           vehicle_type, vehicle_no, starting_km, closing_km, total_km,
                           starting_time, closing_time, total_time,
                           project_code, mail_approval_date, route_covered, driver_name
                    FROM invoices WHERE id IN ({placeholders})""",
                selected_ids
            ).fetchall()
            pdf = _pdf_libs()
            writer = pdf['PdfWriter']()
            for r in rows:
                data = {
                    'duty_slip_no': r[1], 'customer_name': r[2], 'company_name': r[3],
                    'date': r[4], 'vehicle_type': r[5], 'vehicle_no': r[6],
                    'starting_km': r[7], 'closing_km': r[8], 'total_km': r[9],
                    'starting_time': r[10], 'closing_time': r[11], 'total_time': r[12],
                    'project_code': r[13], 'mail_approval_date': r[14],
                    'route_covered': r[15], 'driver_name': r[16],
                }
                reader = pdf['PdfReader'](io.BytesIO(_build_pdf(data).read()))
                for page in reader.pages:
                    writer.add_page(page)
            merged_buffer = io.BytesIO()
            writer.write(merged_buffer)
            merged_buffer.seek(0)
            return send_file(merged_buffer, mimetype='application/pdf', download_name='print_batch.pdf', as_attachment=False)

    return redirect(url_for('admin_portal'))


# --------------------------
# GENERATOR
# --------------------------
@app.route('/generator')
def generator_form():
    if 'admin' not in session:
        return redirect(url_for('home'))
    prefill = {}
    template_id = request.args.get('template_id', type=int)
    with sqlite3.connect(DATABASE) as conn:
        driver_list   = [row[0] for row in conn.execute("SELECT name FROM drivers ORDER BY name ASC").fetchall()]
        vehicle_rows  = conn.execute("SELECT name, COALESCE(vehicle_no,'') FROM vehicles ORDER BY name ASC").fetchall()
        customer_rows = conn.execute("SELECT name, COALESCE(company,'') FROM customers ORDER BY name ASC").fetchall()
        quick_templates = conn.execute(
            "SELECT id, template_name FROM slip_templates WHERE admin_username = ? ORDER BY use_count DESC, created_at DESC",
            (session['admin'],)
        ).fetchall()
        if template_id:
            t = conn.execute(
                """SELECT customer_name, company_name, vehicle_type, vehicle_no,
                          route_covered, dn, remarks, driver_name,
                          starting_km, total_km,
                          COALESCE(project_code,''), COALESCE(mail_approval_date,''),
                          COALESCE(starting_time,''), COALESCE(closing_time,''),
                          COALESCE(route_stops_json,'')
                   FROM slip_templates
                   WHERE id = ? AND admin_username = ?""",
                (template_id, session['admin']),
            ).fetchone()
            if t:
                prefill = {
                    'customer_name': t[0] or '', 'company_name': t[1] or '',
                    'vehicle_type':  t[2] or '', 'vehicle_no':   t[3] or '',
                    'route_covered': t[4] or '', 'dn':           t[5] or '',
                    'remarks':       t[6] or '', 'driver_name':  t[7] or '',
                    'starting_km':   t[8] or '', 'total_km':     t[9] or '',
                    'project_code':  t[10] or '', 'mail_approval_date': t[11] or '',
                    'starting_time': t[12] or '', 'closing_time': t[13] or '',
                    'route_stops_json': t[14] or '',
                }
    vehicle_list  = [r[0] for r in vehicle_rows]
    vehicle_map   = {r[0]: r[1] for r in vehicle_rows}
    customer_list = [r[0] for r in customer_rows]
    customer_map  = {r[0]: r[1] for r in customer_rows}
    next_slip_no = get_next_duty_slip_no()
    today = date.today().strftime('%Y-%m-%d')
    return render_template('generator.html',
                           driver_list=driver_list, vehicle_list=vehicle_list, vehicle_map=vehicle_map,
                           customer_list=customer_list, customer_map=customer_map,
                           quick_templates=quick_templates,
                           prefill=prefill, next_slip_no=next_slip_no, today=today)


@app.route('/clone/<int:invoice_id>')
def clone_invoice(invoice_id):
    if 'admin' not in session:
        return redirect(url_for('home'))
    with sqlite3.connect(DATABASE) as conn:
        row = conn.execute(
            "SELECT customer_name, company_name, vehicle_type, vehicle_no, driver_name, project_code, mail_approval_date, route_covered FROM invoices WHERE id = ?",
            (invoice_id,)
        ).fetchone()
        driver_list   = [r[0] for r in conn.execute("SELECT name FROM drivers ORDER BY name ASC").fetchall()]
        vehicle_rows  = conn.execute("SELECT name, COALESCE(vehicle_no,'') FROM vehicles ORDER BY name ASC").fetchall()
        customer_rows = conn.execute("SELECT name, COALESCE(company,'') FROM customers ORDER BY name ASC").fetchall()
    if not row:
        return redirect(url_for('generator_form'))
    vehicle_list  = [r[0] for r in vehicle_rows]
    vehicle_map   = {r[0]: r[1] for r in vehicle_rows}
    customer_list = [r[0] for r in customer_rows]
    customer_map  = {r[0]: r[1] for r in customer_rows}
    prefill = {
        'customer_name': row[0] or '',
        'company_name': row[1] or '',
        'vehicle_type': row[2] or '',
        'vehicle_no': row[3] or '',
        'driver_name': row[4] or '',
        'project_code': row[5] or '',
        'mail_approval_date': row[6] or '',
        'route_covered': row[7] or '',
    }
    next_slip_no = get_next_duty_slip_no()
    today = date.today().strftime('%Y-%m-%d')
    return render_template('generator.html',
                           driver_list=driver_list, vehicle_list=vehicle_list, vehicle_map=vehicle_map,
                           customer_list=customer_list, customer_map=customer_map,
                           quick_templates=[],
                           prefill=prefill, next_slip_no=next_slip_no, today=today)


@app.route('/generate', methods=['POST'])
def generate_invoice():
    if 'admin' not in session:
        return redirect(url_for('home'))

    admin_username = session['admin']
    customer_name = request.form['customer_name']
    company_name = request.form['company_name']
    date_value = request.form['date']
    duty_slip_no = request.form['duty_slip_no']
    vehicle_type = request.form['vehicle_type']
    vehicle_no = request.form['vehicle_no']
    starting_km = request.form['starting_km']
    closing_km = request.form['closing_km']
    total_km = request.form['total_km']
    starting_time = request.form['starting_time']
    closing_time = request.form['closing_time']
    total_time = request.form['total_time']
    project_code = request.form.get('project_code', '')
    mail_approval_date = request.form.get('mail_approval_date', '')
    route_covered = request.form['route_covered']
    driver_name = request.form['driver_name']

    slip_data = {
        'customer_name': customer_name,
        'company_name': company_name,
        'date': date_value,
        'duty_slip_no': duty_slip_no,
        'vehicle_type': vehicle_type,
        'vehicle_no': vehicle_no,
        'starting_km': starting_km,
        'closing_km': closing_km,
        'total_km': total_km,
        'starting_time': starting_time,
        'closing_time': closing_time,
        'total_time': total_time,
        'project_code': project_code,
        'mail_approval_date': mail_approval_date,
        'route_covered': route_covered,
        'driver_name': driver_name,
    }
    buf = _build_pdf(slip_data)

    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with sqlite3.connect(DATABASE) as conn:
        conn.execute("""
            INSERT INTO invoices(
                customer_name, company_name, date,
                duty_slip_no, vehicle_type, vehicle_no,
                starting_km, closing_km, total_km,
                starting_time, closing_time, total_time,
                project_code, mail_approval_date, route_covered, driver_name,
                admin_username, created_at, bill_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Bill Generated')
        """, (
            customer_name, company_name, date_value,
            duty_slip_no, vehicle_type, vehicle_no,
            starting_km, closing_km, total_km,
            starting_time, closing_time, total_time,
            project_code, mail_approval_date, route_covered, driver_name,
            admin_username, created_at
        ))
        # Auto-create customer if not already in the list
        if customer_name:
            exists = conn.execute(
                "SELECT 1 FROM customers WHERE LOWER(name) = LOWER(?)", (customer_name,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO customers (name, company) VALUES (?, ?)",
                    (customer_name, company_name or '')
                )
        # Auto-create vehicle if not already in the list
        if vehicle_type:
            exists = conn.execute(
                "SELECT 1 FROM vehicles WHERE LOWER(name) = LOWER(?)", (vehicle_type,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO vehicles (name, vehicle_no) VALUES (?, ?)",
                    (vehicle_type, vehicle_no or '')
                )

    filename = f"slip_{duty_slip_no or customer_name.replace(' ', '_')}.pdf"
    return send_file(buf, as_attachment=True, download_name=filename, mimetype='application/pdf')


@app.route('/templates/<int:template_id>/json', methods=['GET'])
def template_json(template_id):
    if 'admin' not in session:
        return jsonify({}), 401
    with sqlite3.connect(DATABASE) as conn:
        t = conn.execute(
            """SELECT template_name, customer_name, company_name, vehicle_type, vehicle_no,
                      route_covered, driver_name, starting_km, total_km,
                      COALESCE(project_code,''), COALESCE(mail_approval_date,''),
                      COALESCE(starting_time,''), COALESCE(closing_time,''),
                      COALESCE(route_stops_json,'')
               FROM slip_templates WHERE id = ? AND admin_username = ?""",
            (template_id, session['admin'])
        ).fetchone()
    if not t:
        return jsonify({}), 404
    return jsonify({
        'template_name': t[0], 'customer_name': t[1], 'company_name': t[2],
        'vehicle_type': t[3], 'vehicle_no': t[4], 'route_covered': t[5],
        'driver_name': t[6], 'starting_km': t[7], 'total_km': t[8],
        'project_code': t[9], 'mail_approval_date': t[10],
        'starting_time': t[11], 'closing_time': t[12], 'route_stops_json': t[13],
    })


@app.route('/invoice/<int:invoice_id>/download')
def download_invoice(invoice_id):
    if 'admin' not in session:
        return redirect(url_for('login_page'))
    with sqlite3.connect(DATABASE) as conn:
        row = conn.execute(
            """SELECT customer_name, company_name, date, duty_slip_no, vehicle_type,
                      vehicle_no, starting_km, closing_km, total_km,
                      starting_time, closing_time, total_time,
                      project_code, mail_approval_date, route_covered, driver_name
               FROM invoices WHERE id = ? AND admin_username = ?""",
            (invoice_id, session['admin'])
        ).fetchone()
    if not row:
        return "Invoice not found", 404
    data = {
        'customer_name': row[0], 'company_name': row[1], 'date': row[2],
        'duty_slip_no': row[3], 'vehicle_type': row[4], 'vehicle_no': row[5],
        'starting_km': row[6], 'closing_km': row[7], 'total_km': row[8],
        'starting_time': row[9], 'closing_time': row[10], 'total_time': row[11],
        'project_code': row[12], 'mail_approval_date': row[13],
        'route_covered': row[14], 'driver_name': row[15],
    }
    buf = _build_pdf(data)
    filename = f"slip_{row[3] or row[0].replace(' ', '_')}.pdf"
    return send_file(buf, as_attachment=True, download_name=filename, mimetype='application/pdf')


@app.route('/config/maps_key', methods=['GET'])
def get_maps_key():
    if 'admin' not in session:
        return jsonify({'key': ''}), 401
    return jsonify({'key': GOOGLE_MAPS_API_KEY})


@app.route('/customer_autocomplete')
def customer_autocomplete():
    if 'admin' not in session:
        return jsonify([])
    query = request.args.get('query', '').strip()
    if not query:
        return jsonify([])
    with sqlite3.connect(DATABASE) as conn:
        results = [row[0] for row in conn.execute("""
            SELECT DISTINCT customer_name FROM invoices
            WHERE customer_name LIKE ?
            ORDER BY customer_name ASC LIMIT 10
        """, (f"%{query}%",)).fetchall()]
    return jsonify(results)


# --------------------------
# SETTINGS
# --------------------------
@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if 'admin' not in session:
        return redirect(url_for('home'))

    message = ''
    message_type = 'success'

    if request.method == 'POST':
        action = request.form.get('action', 'add_driver')
        if action == 'add_driver':
            new_driver = request.form.get('new_driver', '').strip()
            if new_driver:
                with sqlite3.connect(DATABASE) as conn:
                    try:
                        conn.execute("INSERT INTO drivers (name) VALUES (?)", (new_driver,))
                        message = f"Driver '{new_driver}' added successfully."
                    except sqlite3.IntegrityError:
                        message = f"Driver '{new_driver}' already exists."
                        message_type = 'error'
        elif action == 'delete_driver':
            driver_id = request.form.get('driver_id')
            if driver_id:
                with sqlite3.connect(DATABASE) as conn:
                    conn.execute("DELETE FROM drivers WHERE id = ?", (driver_id,))
                message = "Driver removed."
        elif action == 'add_vehicle':
            new_vehicle = request.form.get('new_vehicle', '').strip()
            new_vehicle_no = request.form.get('new_vehicle_no', '').strip()
            if new_vehicle:
                with sqlite3.connect(DATABASE) as conn:
                    try:
                        conn.execute("INSERT INTO vehicles (name, vehicle_no) VALUES (?, ?)", (new_vehicle, new_vehicle_no or None))
                        message = f"Vehicle '{new_vehicle}' added."
                    except sqlite3.IntegrityError:
                        message = f"Vehicle '{new_vehicle}' already exists."
                        message_type = 'error'
        elif action == 'delete_vehicle':
            vehicle_id = request.form.get('vehicle_id')
            if vehicle_id:
                with sqlite3.connect(DATABASE) as conn:
                    conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
                message = "Vehicle removed."
        elif action == 'add_customer':
            new_customer = request.form.get('new_customer', '').strip()
            new_company  = request.form.get('new_company', '').strip()
            if new_customer:
                with sqlite3.connect(DATABASE) as conn:
                    try:
                        conn.execute("INSERT INTO customers (name, company) VALUES (?, ?)", (new_customer, new_company or None))
                        message = f"Customer '{new_customer}' added."
                    except sqlite3.IntegrityError:
                        message = f"Customer '{new_customer}' already exists."
                        message_type = 'error'
        elif action == 'delete_customer':
            customer_id = request.form.get('customer_id')
            if customer_id:
                with sqlite3.connect(DATABASE) as conn:
                    conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
                message = "Customer removed."

    with sqlite3.connect(DATABASE) as conn:
        total_invoices = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        invoices_this_month = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m','now')"
        ).fetchone()[0]
        drivers   = conn.execute("SELECT id, name FROM drivers ORDER BY name ASC").fetchall()
        vehicles  = conn.execute("SELECT id, name, COALESCE(vehicle_no,'') FROM vehicles ORDER BY name ASC").fetchall()
        customers = conn.execute("SELECT id, name, COALESCE(company,'') FROM customers ORDER BY name ASC").fetchall()
        not_submitted = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE COALESCE(bill_status,'Bill Generated') = 'Bill Generated'"
        ).fetchone()[0]
        bill_submitted = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE bill_status = 'Bill Submitted'"
        ).fetchone()[0]
        payment_received = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE bill_status = 'Payment Received'"
        ).fetchone()[0]
        vehicle_km_rows = conn.execute("""
            SELECT vehicle_type,
                   strftime('%Y-%m', date) as month,
                   ROUND(SUM(CAST(NULLIF(total_km,'') AS REAL)), 1) as km
            FROM invoices
            WHERE vehicle_type IS NOT NULL AND vehicle_type != ''
              AND date IS NOT NULL AND date != ''
            GROUP BY vehicle_type, month
            ORDER BY month DESC, vehicle_type ASC
        """).fetchall()
        monthly_report_rows = conn.execute("""
            SELECT strftime('%Y-%m', date) AS month,
                   COUNT(*) AS slips,
                   SUM(CASE WHEN COALESCE(bill_status,'Bill Generated')='Bill Generated' THEN 1 ELSE 0 END) AS not_submitted,
                   SUM(CASE WHEN bill_status='Bill Submitted' THEN 1 ELSE 0 END) AS submitted,
                   SUM(CASE WHEN bill_status='Payment Received' THEN 1 ELSE 0 END) AS paid,
                   ROUND(SUM(CAST(NULLIF(total_km,'') AS REAL)),1) AS km
            FROM invoices
            WHERE date IS NOT NULL AND date != ''
            GROUP BY month
            ORDER BY month DESC
        """).fetchall()

    return render_template('settings.html',
                           message=message,
                           message_type=message_type,
                           total_invoices=total_invoices,
                           invoices_this_month=invoices_this_month,
                           drivers=drivers,
                           vehicles=vehicles,
                           customers=customers,
                           vehicle_km_rows=vehicle_km_rows,
                           monthly_report_rows=monthly_report_rows,
                           not_submitted=not_submitted,
                           bill_submitted=bill_submitted,
                           payment_received=payment_received)


@app.route('/export/monthly_report')
def export_monthly_report():
    if 'admin' not in session:
        return redirect(url_for('login_page'))
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    # Accept ?months=2026-04,2026-03  OR  ?months=2026-04 (single) OR nothing (last 6)
    months_param = request.args.get('months', '').strip()
    month_list = [m.strip() for m in months_param.split(',') if m.strip()] if months_param else []

    with sqlite3.connect(DATABASE) as conn:
        if month_list:
            ph = ','.join('?' * len(month_list))
            summary_rows = conn.execute(f"""
                SELECT strftime('%Y-%m', date) AS m,
                       COUNT(*) AS slips,
                       SUM(CASE WHEN COALESCE(bill_status,'Bill Generated')='Bill Generated' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN bill_status='Bill Submitted' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN bill_status='Payment Received' THEN 1 ELSE 0 END),
                       ROUND(SUM(CAST(NULLIF(total_km,'') AS REAL)),1)
                FROM invoices WHERE date IS NOT NULL AND date != ''
                  AND strftime('%Y-%m', date) IN ({ph})
                GROUP BY m ORDER BY m DESC
            """, month_list).fetchall()
            km_rows = conn.execute(f"""
                SELECT vehicle_type, strftime('%Y-%m', date),
                       ROUND(SUM(CAST(NULLIF(total_km,'') AS REAL)),1)
                FROM invoices
                WHERE vehicle_type IS NOT NULL AND vehicle_type != ''
                  AND date IS NOT NULL AND date != ''
                  AND strftime('%Y-%m', date) IN ({ph})
                GROUP BY vehicle_type, strftime('%Y-%m', date)
                ORDER BY strftime('%Y-%m', date) DESC, vehicle_type ASC
            """, month_list).fetchall()
        else:
            summary_rows = conn.execute("""
                SELECT strftime('%Y-%m', date) AS m,
                       COUNT(*) AS slips,
                       SUM(CASE WHEN COALESCE(bill_status,'Bill Generated')='Bill Generated' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN bill_status='Bill Submitted' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN bill_status='Payment Received' THEN 1 ELSE 0 END),
                       ROUND(SUM(CAST(NULLIF(total_km,'') AS REAL)),1)
                FROM invoices WHERE date IS NOT NULL AND date != ''
                  AND strftime('%Y-%m', date) >= strftime('%Y-%m', date('now','-5 months'))
                GROUP BY m ORDER BY m DESC
            """).fetchall()
            km_rows = conn.execute("""
                SELECT vehicle_type, strftime('%Y-%m', date),
                       ROUND(SUM(CAST(NULLIF(total_km,'') AS REAL)),1)
                FROM invoices
                WHERE vehicle_type IS NOT NULL AND vehicle_type != ''
                  AND date IS NOT NULL AND date != ''
                  AND strftime('%Y-%m', date) >= strftime('%Y-%m', date('now','-5 months'))
                GROUP BY vehicle_type, strftime('%Y-%m', date)
                ORDER BY strftime('%Y-%m', date) DESC, vehicle_type ASC
            """).fetchall()

    wb = Workbook()
    hdr_fill = PatternFill("solid", fgColor="1967D2")
    hdr_font = Font(color="FFFFFF", bold=True, size=11)
    thin = Side(style='thin', color='CCCCCC')
    cell_border = Border(left=thin, right=thin, bottom=thin)

    def _style_hdr(cell, text):
        cell.value = text
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    def _auto_width(ws):
        for col in ws.columns:
            max_len = max((len(str(c.value or '')) for c in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 30)

    # ── Sheet 1: Slip Summary ──
    ws1 = wb.active
    ws1.title = "Slip Summary"
    ws1.row_dimensions[1].height = 22
    for col, h in enumerate(["Month", "Total Slips", "Not Submitted", "Bill Submitted", "Payment Received", "Total KM"], 1):
        _style_hdr(ws1.cell(row=1, column=col), h)
    for r, row in enumerate(summary_rows, 2):
        for c, val in enumerate(row, 1):
            cell = ws1.cell(row=r, column=c, value=val if val is not None else 0)
            cell.border = cell_border
            if c > 1:
                cell.alignment = Alignment(horizontal="center")
    _auto_width(ws1)

    # ── Sheet 2: Vehicle KM ──
    ws2 = wb.create_sheet("Vehicle KM")
    ws2.row_dimensions[1].height = 22
    for col, h in enumerate(["Vehicle", "Month", "Total KM (km)"], 1):
        _style_hdr(ws2.cell(row=1, column=col), h)
    for r, row in enumerate(km_rows, 2):
        for c, val in enumerate(row, 1):
            cell = ws2.cell(row=r, column=c, value=val if val is not None else 0)
            cell.border = cell_border
            if c > 1:
                cell.alignment = Alignment(horizontal="center")
    _auto_width(ws2)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    if month_list and len(month_list) == 1:
        stamp = month_list[0]
    elif month_list:
        stamp = f"{month_list[-1]}_to_{month_list[0]}"
    else:
        stamp = datetime.now().strftime('%Y-%m')
    return send_file(buf, download_name=f'slip_report_{stamp}.xlsx',
                     as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/templates', methods=['GET'])
def templates_list():
    if 'admin' not in session:
        return redirect(url_for('home'))
    admin_username = session['admin']
    # Pre-fill params from generator "Save as Template" link
    prefill = {k: request.args.get(k, '') for k in (
        'template_name', 'customer_name', 'company_name', 'driver_name',
        'vehicle_type', 'vehicle_no', 'route_covered', 'dn', 'remarks',
        'starting_km', 'total_km')}
    with sqlite3.connect(DATABASE) as conn:
        rows = conn.execute(
            """SELECT id, template_name, customer_name, company_name,
                      driver_name, vehicle_type, vehicle_no, route_covered,
                      created_at, use_count, last_used
               FROM slip_templates
               WHERE admin_username = ?
               ORDER BY use_count DESC, created_at DESC""",
            (admin_username,),
        ).fetchall()
    return render_template('templates_list.html', templates=rows, prefill=prefill)


@app.route('/templates/new')
def templates_new():
    if 'admin' not in session:
        return redirect(url_for('home'))
    with sqlite3.connect(DATABASE) as conn:
        driver_list   = [r[0] for r in conn.execute("SELECT name FROM drivers ORDER BY name ASC").fetchall()]
        vehicle_rows  = conn.execute("SELECT name, COALESCE(vehicle_no,'') FROM vehicles ORDER BY name ASC").fetchall()
        customer_rows = conn.execute("SELECT name, COALESCE(company,'') FROM customers ORDER BY name ASC").fetchall()
    vehicle_list  = [r[0] for r in vehicle_rows]
    vehicle_map   = {r[0]: r[1] for r in vehicle_rows}
    customer_list = [r[0] for r in customer_rows]
    customer_map  = {r[0]: r[1] for r in customer_rows}
    # Pre-fill from generator "Save as Template" click
    prefill = {k: request.args.get(k, '') for k in (
        'customer_name', 'company_name', 'driver_name', 'vehicle_type',
        'vehicle_no', 'route_covered', 'dn', 'remarks', 'starting_km',
        'total_km', 'project_code', 'mail_approval_date')}
    return render_template('template_new.html',
                           driver_list=driver_list,
                           vehicle_list=vehicle_list, vehicle_map=vehicle_map,
                           customer_list=customer_list, customer_map=customer_map,
                           prefill=prefill)


@app.route('/templates/save', methods=['POST'])
def templates_save():
    if 'admin' not in session:
        return redirect(url_for('home'))
    admin_username = session['admin']
    name = (request.form.get('template_name') or '').strip()
    if not name:
        return redirect(url_for('templates_list'))
    fields = {k: (request.form.get(k) or '').strip() for k in (
        'customer_name', 'company_name', 'vehicle_type', 'vehicle_no',
        'route_covered', 'dn', 'remarks', 'driver_name',
        'starting_km', 'total_km', 'project_code', 'mail_approval_date',
        'starting_time', 'closing_time', 'route_stops_json')}
    with sqlite3.connect(DATABASE) as conn:
        conn.execute(
            """INSERT INTO slip_templates
               (admin_username, template_name, customer_name, company_name,
                vehicle_type, vehicle_no, route_covered, dn, remarks,
                driver_name, starting_km, total_km, project_code,
                mail_approval_date, starting_time, closing_time,
                route_stops_json, created_at, use_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (admin_username, name,
             fields['customer_name'], fields['company_name'],
             fields['vehicle_type'], fields['vehicle_no'],
             fields['route_covered'], fields['dn'], fields['remarks'],
             fields['driver_name'], fields['starting_km'], fields['total_km'],
             fields['project_code'], fields['mail_approval_date'],
             fields['starting_time'], fields['closing_time'],
             fields['route_stops_json'],
             datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        )
    return redirect(url_for('templates_list'))


@app.route('/templates/<int:template_id>/delete', methods=['POST'])
def templates_delete(template_id):
    if 'admin' not in session:
        return redirect(url_for('home'))
    with sqlite3.connect(DATABASE) as conn:
        conn.execute(
            "DELETE FROM slip_templates WHERE id = ? AND admin_username = ?",
            (template_id, session['admin']),
        )
    return redirect(url_for('templates_list'))


@app.route('/templates/<int:template_id>/use', methods=['POST'])
def templates_use(template_id):
    if 'admin' not in session:
        return redirect(url_for('home'))
    with sqlite3.connect(DATABASE) as conn:
        conn.execute(
            """UPDATE slip_templates
               SET use_count = COALESCE(use_count, 0) + 1,
                   last_used = ?
               WHERE id = ? AND admin_username = ?""",
            (datetime.now().strftime('%Y-%m-%d %H:%M'), template_id, session['admin']),
        )
    return redirect(url_for('generator_form', template_id=template_id))


@app.errorhandler(404)
def not_found(e):
    return ('''<!doctype html><html><head><title>404 — Osprey Travels</title>
<style>body{font-family:-apple-system,sans-serif;background:#f2f2f7;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0;}
.box{text-align:center;padding:40px;background:#fff;border-radius:16px;box-shadow:0 2px 8px rgba(0,0,0,.08);}
h1{font-size:56px;margin:0;color:#1c1c1e;}p{color:#8e8e93;margin:8px 0 24px;}
a{background:#007aff;color:#fff;padding:10px 22px;border-radius:8px;text-decoration:none;font-weight:600;}
</style></head><body><div class="box"><h1>404</h1><p>This page doesn't exist.</p>
<a href="/">Go Home</a></div></body></html>''', 404)


@app.errorhandler(500)
def server_error(e):
    return ('''<!doctype html><html><head><title>500 — Osprey Travels</title>
<style>body{font-family:-apple-system,sans-serif;background:#f2f2f7;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0;}
.box{text-align:center;padding:40px;background:#fff;border-radius:16px;box-shadow:0 2px 8px rgba(0,0,0,.08);}
h1{font-size:56px;margin:0;color:#ff3b30;}p{color:#8e8e93;margin:8px 0 24px;}
a{background:#007aff;color:#fff;padding:10px 22px;border-radius:8px;text-decoration:none;font-weight:600;}
</style></head><body><div class="box"><h1>500</h1><p>Something went wrong on our end.</p>
<a href="/">Go Home</a></div></body></html>''', 500)


if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG', '0') == '1', port=5050)
