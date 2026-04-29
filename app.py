import os
import io
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Mapping, Any
import zipfile

from flask import Flask, render_template, request, send_file, redirect, url_for, session, jsonify, abort, flash
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from sqlalchemy import create_engine, text, MetaData, Table, Column, Integer, String, Text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

# Optional security middleware — degrade gracefully if not installed (local dev)
try:
    from flask_wtf.csrf import CSRFProtect, generate_csrf
    _HAS_CSRF = True
except ImportError:
    _HAS_CSRF = False

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    _HAS_LIMITER = True
except ImportError:
    _HAS_LIMITER = False

try:
    from flask_compress import Compress
    _HAS_COMPRESS = True
except ImportError:
    _HAS_COMPRESS = False

app = Flask(__name__)

# Gzip responses
if _HAS_COMPRESS:
    Compress(app)

# Secret key from env. In production (Vercel) this MUST be set; otherwise sessions reset on every cold start.
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    if os.environ.get("VERCEL") or os.environ.get("PRODUCTION"):
        raise RuntimeError("SECRET_KEY environment variable is required in production.")
    _secret = secrets.token_hex(32)
app.secret_key = _secret

# Hardened session cookies
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL") or os.environ.get("PRODUCTION")),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

# CSRF protection
if _HAS_CSRF:
    csrf = CSRFProtect(app)
    @app.context_processor
    def _inject_csrf():
        return {"csrf_token": generate_csrf}
else:
    @app.context_processor
    def _inject_csrf():
        return {"csrf_token": lambda: ""}

# Rate limiter — applied selectively to login below
if _HAS_LIMITER:
    limiter = Limiter(get_remote_address, app=app, default_limits=[])
else:
    limiter = None

BASE_DIR = Path(__file__).resolve().parent
TMP_ROOT = Path(os.environ.get("TMPDIR", "/tmp")) / "billgenerator"


def ensure_writable_dir(preferred: Path, fallback: Path) -> Path:
    """
    Return a directory path that can be written to. Tries the preferred path first
    and falls back to an alternate path (usually /tmp on serverless platforms).
    """
    for path in (preferred, fallback):
        try:
            path.mkdir(parents=True, exist_ok=True)
            test_file = path / ".write_test"
            with open(test_file, "w") as handle:
                handle.write("ok")
            test_file.unlink(missing_ok=True)
            return path
        except OSError:
            continue
    raise RuntimeError("Unable to create a writable directory for generated invoices.")


def ensure_writable_file(preferred: Path, fallback: Path) -> Path:
    """
    Return a file path that can be opened for writing. Falls back to an alternate
    location if the preferred path is read-only.
    """
    for path in (preferred, fallback):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a"):
                pass
            return path
        except OSError:
            continue
    raise RuntimeError("Unable to create a writable SQLite database file.")


INVOICE_DIR_PATH = ensure_writable_dir(
    BASE_DIR / "generated_invoices",
    TMP_ROOT / "generated_invoices"
)
INVOICE_DIR = str(INVOICE_DIR_PATH)

DATABASE_URL = os.environ.get("DATABASE_URL")
DATABASE_PATH = None
if not DATABASE_URL:
    DATABASE_PATH = ensure_writable_file(
        BASE_DIR / "invoices.db",
        TMP_ROOT / "invoices.db"
    )
    DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine: Engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args=connect_args,
)

metadata = MetaData()

invoices_table = Table(
    "invoices",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("customer_name", Text),
    Column("company_name", Text),
    Column("date", String(32)),
    Column("duty_slip_no", String(64)),
    Column("vehicle_type", String(64)),
    Column("vehicle_no", String(64)),
    Column("starting_km", String(32)),
    Column("closing_km", String(32)),
    Column("total_km", String(32)),
    Column("starting_time", String(32)),
    Column("closing_time", String(32)),
    Column("total_time", String(32)),
    Column("dn", Text),
    Column("remarks", Text),
    Column("route_covered", Text),
    Column("driver_name", String(128)),
    Column("admin_username", String(128), nullable=False),
    Column("created_at", String(32)),
    Column("file_path", Text),
)

users_table = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("username", String(150), unique=True, nullable=False),
    Column("password", String(150), nullable=False),
)

drivers_table = Table(
    "drivers",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(150), unique=True, nullable=False),
)

slip_templates_table = Table(
    "slip_templates",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("admin_username", String(128), nullable=False),
    Column("template_name", String(200), nullable=False),
    Column("customer_name", Text),
    Column("company_name", Text),
    Column("vehicle_type", String(64)),
    Column("vehicle_no", String(64)),
    Column("route_covered", Text),
    Column("dn", Text),
    Column("remarks", Text),
    Column("driver_name", String(128)),
    Column("starting_km", String(32)),
    Column("total_km", String(32)),
    Column("created_at", String(32)),
)


def ensure_invoice_columns():
    """Backfill missing invoice columns when running against legacy SQLite files."""
    additions = [
        ("created_at", "TEXT"),
        ("driver_name", "TEXT"),
        ("company_name", "TEXT"),
        ("duty_slip_no", "TEXT"),
        ("vehicle_type", "TEXT"),
        ("vehicle_no", "TEXT"),
        ("starting_km", "TEXT"),
        ("closing_km", "TEXT"),
        ("total_km", "TEXT"),
        ("starting_time", "TEXT"),
        ("closing_time", "TEXT"),
        ("total_time", "TEXT"),
        ("dn", "TEXT"),
        ("remarks", "TEXT"),
        ("route_covered", "TEXT"),
    ]

    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "sqlite":
            existing = {row[1] for row in conn.execute(text("PRAGMA table_info(invoices)"))}
            for column, ddl in additions:
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE invoices ADD COLUMN {column} {ddl}"))
        else:
            for column, ddl in additions:
                conn.execute(text(f"ALTER TABLE invoices ADD COLUMN IF NOT EXISTS {column} {ddl}"))


def ensure_indexes():
    """Create performance indexes used by the admin portal listing/filtering."""
    statements = [
        'CREATE INDEX IF NOT EXISTS idx_invoices_admin_date   ON invoices(admin_username, "date" DESC)',
        'CREATE INDEX IF NOT EXISTS idx_invoices_admin_cust   ON invoices(admin_username, customer_name)',
        'CREATE INDEX IF NOT EXISTS idx_invoices_admin_driver ON invoices(admin_username, driver_name)',
        'CREATE INDEX IF NOT EXISTS idx_invoices_created_at   ON invoices(created_at)',
    ]
    with engine.begin() as conn:
        for stmt in statements:
            try:
                conn.execute(text(stmt))
            except Exception:
                # Some old SQLAlchemy/SQLite versions choke on quoted reserved word in index DDL — non-fatal
                pass


def init_db():
    metadata.create_all(engine)
    ensure_invoice_columns()
    ensure_indexes()

    initial_admin_password = os.environ.get("ADMIN_INITIAL_PASSWORD", "admin")
    hashed = generate_password_hash(initial_admin_password, method="pbkdf2:sha256")
    with engine.begin() as conn:
        user_exists = conn.execute(
            text("SELECT 1 FROM users WHERE username = :username"),
            {"username": "admin"},
        ).first()
        if not user_exists:
            conn.execute(
                text("INSERT INTO users (username, password) VALUES (:username, :password)"),
                {"username": "admin", "password": hashed},
            )

init_db()


def execute_query(statement: str, params: Optional[Mapping[str, Any]] = None):
    with engine.begin() as conn:
        conn.execute(text(statement), params or {})


def fetch_one(statement: str, params: Optional[Mapping[str, Any]] = None):
    with engine.connect() as conn:
        return conn.execute(text(statement), params or {}).first()


def fetch_all(statement: str, params: Optional[Mapping[str, Any]] = None):
    with engine.connect() as conn:
        return conn.execute(text(statement), params or {}).fetchall()


def wrap_text_for_pdf(value: str, max_width: float, font_name: str = "Helvetica", font_size: int = 12, max_lines: Optional[int] = None):
    if not value:
        return []

    words = str(value).split()
    lines = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        ellipsis = "..."
        while pdfmetrics.stringWidth(last + ellipsis, font_name, font_size) > max_width and " " in last:
            last = " ".join(last.split(" ")[:-1])
        if pdfmetrics.stringWidth(last + ellipsis, font_name, font_size) > max_width:
            while last and pdfmetrics.stringWidth(last + ellipsis, font_name, font_size) > max_width:
                last = last[:-1]
        lines[-1] = last.rstrip() + ellipsis if last else ellipsis

    return lines


def _invoice_payload_from_row(invoice_row):
    if not invoice_row:
        return None
    return {
        "customer_name": invoice_row.customer_name or "",
        "company_name": invoice_row.company_name or "",
        "date": invoice_row.date or "",
        "duty_slip_no": invoice_row.duty_slip_no or "",
        "vehicle_type": invoice_row.vehicle_type or "",
        "vehicle_no": invoice_row.vehicle_no or "",
        "starting_km": invoice_row.starting_km or "",
        "closing_km": invoice_row.closing_km or "",
        "total_km": invoice_row.total_km or "",
        "starting_time": invoice_row.starting_time or "",
        "closing_time": invoice_row.closing_time or "",
        "total_time": invoice_row.total_time or "",
        "dn": invoice_row.dn or "",
        "remarks": invoice_row.remarks or "",
        "route_covered": invoice_row.route_covered or "",
        "driver_name": invoice_row.driver_name or "",
    }


def build_invoice_pdf(invoice_data: Mapping[str, str], output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(output_path), pagesize=landscape(A4))
    width, height = landscape(A4)

    template_path = BASE_DIR / "invoice_template.png"
    if template_path.exists():
        c.drawImage(ImageReader(str(template_path)), 0, 0, width=width, height=height)

    c.setFont("Helvetica", 12)
    left_x = 150
    right_x = 530
    coords = {
        "duty_slip_no": (left_x, 493),
        "date": (left_x, 466),
        "company_name": (left_x, 397),
        "vehicle_type": (left_x, 354),
        "starting_km": (left_x, 309),
        "closing_km": (left_x, 266),
        "total_km": (left_x, 221),
        "remarks": (left_x, 186),
        "customer_name": (right_x, 398),
        "vehicle_no": (right_x, 354),
        "starting_time": (right_x, 310),
        "closing_time": (right_x, 264),
        "total_time": (right_x, 220),
        "dn": (right_x, 181),
        "driver_name": (right_x, 130),
    }
    route_line_y = [143, 106, 66, 30]
    left_max_width = 220
    right_max_width = 220
    top_max_width = 220

    def draw_single_line(field_name, coord, max_width):
        line_parts = wrap_text_for_pdf(invoice_data.get(field_name, ""), max_width, max_lines=1)
        if line_parts:
            c.drawString(coord[0], coord[1], line_parts[0])

    draw_single_line("duty_slip_no", coords["duty_slip_no"], top_max_width)
    draw_single_line("date", coords["date"], top_max_width)
    draw_single_line("company_name", coords["company_name"], left_max_width)
    draw_single_line("vehicle_type", coords["vehicle_type"], left_max_width)
    draw_single_line("starting_km", coords["starting_km"], left_max_width)
    draw_single_line("closing_km", coords["closing_km"], left_max_width)
    draw_single_line("total_km", coords["total_km"], left_max_width)
    draw_single_line("customer_name", coords["customer_name"], right_max_width)
    draw_single_line("vehicle_no", coords["vehicle_no"], right_max_width)
    draw_single_line("starting_time", coords["starting_time"], right_max_width)
    draw_single_line("closing_time", coords["closing_time"], right_max_width)
    draw_single_line("total_time", coords["total_time"], right_max_width)
    draw_single_line("dn", coords["dn"], right_max_width)
    draw_single_line("driver_name", coords["driver_name"], right_max_width)
    draw_single_line("remarks", coords["remarks"], left_max_width)

    route_lines = wrap_text_for_pdf(invoice_data.get("route_covered", ""), left_max_width, max_lines=len(route_line_y))
    for text_line, y in zip(route_lines, route_line_y):
        c.drawString(left_x, y, text_line)

    c.save()


def _is_safe_invoice_path(path_str: str) -> bool:
    """Reject paths that escape the invoices directory (path traversal protection)."""
    if not path_str:
        return False
    try:
        resolved = Path(path_str).resolve()
        invoice_root = INVOICE_DIR_PATH.resolve()
        return str(resolved).startswith(str(invoice_root))
    except Exception:
        return False


def ensure_invoice_pdf_path(invoice_row):
    file_path = (invoice_row.file_path or "").strip()
    if file_path and os.path.exists(file_path):
        return file_path

    safe_customer = (invoice_row.customer_name or "invoice").replace(" ", "_")
    regenerated_name = f"{safe_customer}_{invoice_row.id}_regenerated.pdf"
    regenerated_path = INVOICE_DIR_PATH / regenerated_name
    invoice_data = _invoice_payload_from_row(invoice_row)
    build_invoice_pdf(invoice_data, regenerated_path)

    execute_query(
        """
        UPDATE invoices
        SET file_path = :file_path
        WHERE id = :invoice_id
        """,
        {"file_path": str(regenerated_path), "invoice_id": invoice_row.id},
    )
    return str(regenerated_path)


def delete_invoice_file(invoice_id, admin_username):
    """Remove invoice record + PDF file if exists."""
    file_path = None
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT file_path FROM invoices
                WHERE id = :invoice_id AND admin_username = :admin
                """
            ),
            {"invoice_id": invoice_id, "admin": admin_username},
        ).first()
        if row:
            file_path = row[0]
            conn.execute(
                text(
                    """
                    DELETE FROM invoices
                    WHERE id = :invoice_id AND admin_username = :admin
                    """
                ),
                {"invoice_id": invoice_id, "admin": admin_username},
            )
    if file_path and os.path.exists(file_path):
        os.remove(file_path)

@app.route('/')
def home():
    if 'admin' in session:
        return redirect(url_for('admin_portal'))
    return redirect(url_for('login_page'))

@app.route('/login')
def login_page():
    return render_template('admin_login.html')

def _verify_login(username: str, password: str) -> bool:
    """Verify username/password. Auto-migrates legacy plain-text passwords to hashes on success."""
    if not username or not password:
        return False
    row = fetch_one(
        "SELECT id, password FROM users WHERE username = :username",
        {"username": username},
    )
    if not row:
        return False
    stored = row[1] or ""
    # Hashed values produced by werkzeug start with "pbkdf2:" or "scrypt:" or similar
    if stored.startswith(("pbkdf2:", "scrypt:", "argon2:")):
        return check_password_hash(stored, password)
    # Legacy plain-text — accept once, then migrate to hash
    if stored == password:
        new_hash = generate_password_hash(password, method="pbkdf2:sha256")
        execute_query(
            "UPDATE users SET password = :p WHERE username = :u",
            {"p": new_hash, "u": username},
        )
        return True
    return False


@app.route('/admin_login', methods=['POST'])
def admin_login():
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    if _verify_login(username, password):
        session['admin'] = username
        session.permanent = True
        return redirect(url_for('admin_portal'))
    return render_template('admin_login.html', error="Invalid credentials"), 401


# Apply rate limit if Flask-Limiter is available
if limiter is not None:
    admin_login = limiter.limit("5 per minute; 30 per hour")(admin_login)

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

    admin_username = session['admin']
    # gather filters
    customer_name = request.args.get('customer_name', '').strip()
    driver_filter = request.args.get('driver', '').strip()
    month_filter = request.args.get('month', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    conditions = ["admin_username = :admin"]
    params = {"admin": admin_username}

    if customer_name:
        conditions.append("LOWER(customer_name) LIKE :customer_name")
        params["customer_name"] = f"%{customer_name.lower()}%"
    if driver_filter:
        conditions.append("driver_name = :driver_name")
        params["driver_name"] = driver_filter
    if month_filter:
        conditions.append('SUBSTR("date", 1, 7) = :month_filter')
        params["month_filter"] = month_filter
    if date_from:
        conditions.append('"date" >= :date_from')
        params["date_from"] = date_from
    if date_to:
        conditions.append('"date" <= :date_to')
        params["date_to"] = date_to

    where_clause = " AND ".join(conditions)
    invoices = fetch_all(
        f"""
        SELECT id, customer_name, "date", file_path, created_at, driver_name
        FROM invoices
        WHERE {where_clause}
        ORDER BY id DESC
        """,
        params,
    )

    driver_rows = fetch_all(
        """
        SELECT DISTINCT driver_name
        FROM invoices
        WHERE admin_username = :admin
          AND driver_name IS NOT NULL
          AND driver_name <> ''
        ORDER BY driver_name ASC
        """,
        {"admin": admin_username},
    )
    driver_names = [row[0] for row in driver_rows if row[0]]

    return render_template('admin_portal.html',
                           invoices=invoices,
                           driver_names=driver_names,
                           customer_name=customer_name,
                           driver_filter=driver_filter,
                           month_filter=month_filter,
                           date_from=date_from,
                           date_to=date_to)

# Single Delete
@app.route('/delete_invoice/<int:invoice_id>', methods=['POST'])
def delete_invoice(invoice_id):
    if 'admin' not in session:
        return redirect(url_for('home'))
    admin_username = session['admin']
    delete_invoice_file(invoice_id, admin_username)
    return redirect(url_for('admin_portal'))

# Bulk Action: delete / download

@app.route('/bulk_action', methods=['POST'])
def bulk_action():
    if 'admin' not in session:
        return redirect(url_for('home'))

    selected_ids = request.form.getlist('selected_invoices')
    action = request.form.get('action')
    if not selected_ids:
        return redirect(url_for('admin_portal'))

    try:
        id_values = [int(id_val) for id_val in selected_ids]
    except ValueError:
        return redirect(url_for('admin_portal'))

    admin_username = session['admin']
    param_map = {f"id_{idx}": value for idx, value in enumerate(id_values)}
    param_map["admin"] = admin_username
    placeholder_list = ", ".join(f":id_{idx}" for idx in range(len(id_values)))
    select_stmt = text(f"SELECT id, file_path FROM invoices WHERE id IN ({placeholder_list}) AND admin_username = :admin")

    with engine.begin() as conn:
        selected = conn.execute(select_stmt, param_map).fetchall()
        if not selected:
            return redirect(url_for('admin_portal'))

        if action == 'delete':
            conn.execute(
                text(f"DELETE FROM invoices WHERE id IN ({placeholder_list}) AND admin_username = :admin"),
                param_map,
            )

    if action == 'delete':
        for _, path in selected:
            if path and os.path.exists(path):
                os.remove(path)
        return redirect(url_for('admin_portal'))

    if action == 'download':
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w') as zipf:
            for invoice_id, _ in selected:
                invoice_row = fetch_one(
                    """
                    SELECT id, customer_name, company_name, "date", duty_slip_no, vehicle_type,
                           vehicle_no, starting_km, closing_km, total_km, starting_time,
                           closing_time, total_time, dn, remarks, route_covered, driver_name, file_path
                    FROM invoices
                    WHERE id = :invoice_id
                    """,
                    {"invoice_id": invoice_id},
                )
                if not invoice_row:
                    continue
                resolved_path = ensure_invoice_pdf_path(invoice_row)
                if resolved_path and _is_safe_invoice_path(resolved_path) and os.path.exists(resolved_path):
                    zipf.write(resolved_path, arcname=os.path.basename(resolved_path))
        zip_buffer.seek(0)
        return send_file(
            zip_buffer,
            as_attachment=True,
            download_name='invoices.zip',
            mimetype='application/zip'
        )

    return redirect(url_for('admin_portal'))

# --------------------------
# GENERATE INVOICE
# --------------------------
def _next_duty_slip_no(admin_username: str) -> str:
    """Suggest the next duty slip number = max numeric prefix + 1, fallback '1'."""
    try:
        rows = fetch_all(
            "SELECT duty_slip_no FROM invoices WHERE admin_username = :a AND duty_slip_no IS NOT NULL AND duty_slip_no <> ''",
            {"a": admin_username},
        )
        max_n = 0
        for r in rows:
            digits = ''.join(ch for ch in (r[0] or '') if ch.isdigit())
            if digits:
                try:
                    n = int(digits)
                    if n > max_n: max_n = n
                except ValueError:
                    pass
        return str(max_n + 1) if max_n else "1"
    except Exception:
        return ""


@app.route('/generator')
def generator_form():
    if 'admin' not in session:
        return redirect(url_for('home'))

    admin_username = session['admin']

    driver_rows = fetch_all("SELECT name FROM drivers ORDER BY name ASC")
    driver_list = [row[0] for row in driver_rows]

    template_rows = fetch_all(
        "SELECT id, template_name FROM slip_templates WHERE admin_username = :a ORDER BY template_name ASC",
        {"a": admin_username},
    )
    template_list = [{"id": r[0], "template_name": r[1]} for r in template_rows]

    prefill = {}
    template_id = request.args.get('from_template', type=int)
    if template_id:
        t = fetch_one(
            """
            SELECT customer_name, company_name, vehicle_type, vehicle_no, route_covered,
                   dn, remarks, driver_name, starting_km, total_km
            FROM slip_templates WHERE id = :id AND admin_username = :a
            """,
            {"id": template_id, "a": admin_username},
        )
        if t:
            prefill = {
                "customer_name": t[0] or "",
                "company_name": t[1] or "",
                "vehicle_type": t[2] or "",
                "vehicle_no": t[3] or "",
                "route_covered": t[4] or "",
                "dn": t[5] or "",
                "remarks": t[6] or "",
                "driver_name": t[7] or "",
                "starting_km": t[8] or "",
                "total_km": t[9] or "",
            }
    # Default date to today
    prefill.setdefault("date", datetime.now().strftime('%Y-%m-%d'))

    return render_template(
        'generator.html',
        driver_list=driver_list,
        template_list=template_list,
        prefill=prefill,
        next_duty_slip_no=_next_duty_slip_no(admin_username),
    )


# --------------------------
# CONFIG: Google Maps API key (from env, never UI)
# --------------------------
@app.route('/config/maps_key')
def get_maps_key():
    if 'admin' not in session:
        return jsonify({'key': ''}), 401
    return jsonify({'key': os.environ.get('GOOGLE_MAPS_API_KEY', '')})


# --------------------------
# SLIP TEMPLATES
# --------------------------
@app.route('/templates')
def templates_list():
    if 'admin' not in session:
        return redirect(url_for('home'))
    rows = fetch_all(
        """
        SELECT id, template_name, customer_name, route_covered, driver_name, created_at
        FROM slip_templates WHERE admin_username = :a
        ORDER BY template_name ASC
        """,
        {"a": session['admin']},
    )
    templates = [
        {"id": r[0], "template_name": r[1], "customer_name": r[2] or '',
         "route_covered": r[3] or '', "driver_name": r[4] or '', "created_at": r[5] or ''}
        for r in rows
    ]
    return render_template('templates_list.html', templates=templates)


@app.route('/templates/create', methods=['POST'])
def templates_create():
    if 'admin' not in session:
        return jsonify({"ok": False, "error": "not logged in"}), 401
    name = (request.form.get('template_name') or '').strip()
    if not name:
        return jsonify({"ok": False, "error": "template_name required"}), 400
    payload = {
        "a": session['admin'],
        "name": name,
        "customer_name": request.form.get('customer_name') or '',
        "company_name": request.form.get('company_name') or '',
        "vehicle_type": request.form.get('vehicle_type') or '',
        "vehicle_no": request.form.get('vehicle_no') or '',
        "route_covered": request.form.get('route_covered') or '',
        "dn": request.form.get('dn') or '',
        "remarks": request.form.get('remarks') or '',
        "driver_name": request.form.get('driver_name') or '',
        "starting_km": request.form.get('starting_km') or '',
        "total_km": request.form.get('total_km') or '',
        "created_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    try:
        execute_query(
            """
            INSERT INTO slip_templates(
                admin_username, template_name, customer_name, company_name,
                vehicle_type, vehicle_no, route_covered, dn, remarks,
                driver_name, starting_km, total_km, created_at
            ) VALUES (
                :a, :name, :customer_name, :company_name,
                :vehicle_type, :vehicle_no, :route_covered, :dn, :remarks,
                :driver_name, :starting_km, :total_km, :created_at
            )
            """,
            payload,
        )
    except IntegrityError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route('/templates/<int:template_id>/delete', methods=['POST'])
def templates_delete(template_id):
    if 'admin' not in session:
        return redirect(url_for('home'))
    execute_query(
        "DELETE FROM slip_templates WHERE id = :id AND admin_username = :a",
        {"id": template_id, "a": session['admin']},
    )
    return redirect(url_for('templates_list'))


# --------------------------
# EXCEL EXPORT
# --------------------------
def _excel_response_for_invoices(rows, filename: str):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "Duty Slips"

    headers = [
        "ID", "Date", "Duty Slip No", "Customer", "Company",
        "Vehicle Type", "Vehicle No", "Driver",
        "Start KM", "Close KM", "Total KM",
        "Start Time", "Close Time", "Total Time",
        "DN", "Remarks", "Route Covered", "Created At",
    ]
    ws.append(headers)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2563EB")
    for col_idx, _ in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='left', vertical='center')

    for r in rows:
        ws.append([
            r[0], r[1] or '', r[2] or '', r[3] or '', r[4] or '',
            r[5] or '', r[6] or '', r[7] or '',
            r[8] or '', r[9] or '', r[10] or '',
            r[11] or '', r[12] or '', r[13] or '',
            r[14] or '', r[15] or '', r[16] or '', r[17] or '',
        ])

    # Auto-size columns (approximate)
    for col in ws.columns:
        max_len = max((len(str(cell.value)) if cell.value else 0) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(12, max_len + 2), 50)

    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


def _fetch_invoices_for_excel(where_clause: str, params: Mapping[str, Any]):
    return fetch_all(
        f"""
        SELECT id, "date", duty_slip_no, customer_name, company_name,
               vehicle_type, vehicle_no, driver_name,
               starting_km, closing_km, total_km,
               starting_time, closing_time, total_time,
               dn, remarks, route_covered, created_at
        FROM invoices
        WHERE {where_clause}
        ORDER BY "date" DESC, id DESC
        """,
        params,
    )


@app.route('/export/excel', methods=['POST'])
def export_excel():
    """Export selected invoices (from bulk action checkboxes) to Excel."""
    if 'admin' not in session:
        return redirect(url_for('home'))
    selected_ids = request.form.getlist('selected_invoices')
    if not selected_ids:
        return redirect(url_for('admin_portal'))
    try:
        id_values = [int(x) for x in selected_ids]
    except ValueError:
        return redirect(url_for('admin_portal'))

    param_map = {f"id_{i}": v for i, v in enumerate(id_values)}
    param_map["a"] = session['admin']
    placeholders = ", ".join(f":id_{i}" for i in range(len(id_values)))
    where = f"id IN ({placeholders}) AND admin_username = :a"
    rows = _fetch_invoices_for_excel(where, param_map)
    return _excel_response_for_invoices(rows, "duty_slips_selected.xlsx")


@app.route('/export/excel/month/<month>')
def export_excel_month(month):
    """Export all invoices for the given YYYY-MM month for the current admin."""
    if 'admin' not in session:
        return redirect(url_for('home'))
    if not (len(month) == 7 and month[4] == '-'):
        return redirect(url_for('admin_portal'))
    rows = _fetch_invoices_for_excel(
        'admin_username = :a AND SUBSTR("date", 1, 7) = :m',
        {"a": session['admin'], "m": month},
    )
    return _excel_response_for_invoices(rows, f"duty_slips_{month}.xlsx")


# --------------------------
# MONTHLY REPORT
# --------------------------
def _build_monthly_report_data(admin_username: str, month: str):
    headline = fetch_one(
        """
        SELECT COUNT(*),
               COALESCE(SUM(CAST(NULLIF(total_km,'') AS REAL)), 0)
        FROM invoices
        WHERE admin_username = :a AND SUBSTR("date", 1, 7) = :m
        """,
        {"a": admin_username, "m": month},
    )
    total_slips = headline[0] if headline else 0
    total_km = round(float(headline[1] or 0), 1) if headline else 0

    per_driver = fetch_all(
        """
        SELECT COALESCE(NULLIF(driver_name,''), 'Unassigned') AS d,
               COUNT(*) AS slips,
               COALESCE(SUM(CAST(NULLIF(total_km,'') AS REAL)), 0) AS km
        FROM invoices
        WHERE admin_username = :a AND SUBSTR("date", 1, 7) = :m
        GROUP BY d ORDER BY slips DESC
        """,
        {"a": admin_username, "m": month},
    )
    per_customer = fetch_all(
        """
        SELECT COALESCE(NULLIF(customer_name,''), 'Unknown') AS c,
               COUNT(*) AS slips,
               COALESCE(SUM(CAST(NULLIF(total_km,'') AS REAL)), 0) AS km
        FROM invoices
        WHERE admin_username = :a AND SUBSTR("date", 1, 7) = :m
        GROUP BY c ORDER BY slips DESC
        """,
        {"a": admin_username, "m": month},
    )
    per_day = fetch_all(
        """
        SELECT "date", COUNT(*)
        FROM invoices
        WHERE admin_username = :a AND SUBSTR("date", 1, 7) = :m
        GROUP BY "date" ORDER BY "date" ASC
        """,
        {"a": admin_username, "m": month},
    )
    return {
        "month": month,
        "admin": admin_username,
        "generated_at": datetime.now().strftime('%Y-%m-%d %H:%M'),
        "total_slips": total_slips,
        "total_km": total_km,
        "per_driver": [{"driver": r[0], "slips": r[1], "km": round(float(r[2] or 0), 1)} for r in per_driver],
        "per_customer": [{"customer": r[0], "slips": r[1], "km": round(float(r[2] or 0), 1)} for r in per_customer],
        "per_day": [{"date": r[0], "slips": r[1]} for r in per_day],
    }


def _build_monthly_report_pdf(report: dict) -> io.BytesIO:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen.canvas import Canvas

    buf = io.BytesIO()
    c = Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 20 * mm

    def line(text, size=12, bold=False, indent=0):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(20 * mm + indent, y, text)
        y -= (size + 4)

    line(f"Monthly Duty Slip Report — {report['month']}", size=16, bold=True)
    line(f"Account: {report['admin']}    Generated: {report['generated_at']}", size=10)
    y -= 6
    line(f"Total slips: {report['total_slips']}     Total KM: {report['total_km']}", size=12, bold=True)
    y -= 6

    line("Per-Driver Breakdown", size=12, bold=True)
    line(f"{'Driver':<30} {'Slips':>8} {'Total KM':>12}", size=10)
    for d in report['per_driver']:
        line(f"{d['driver'][:30]:<30} {d['slips']:>8} {d['km']:>12}", size=10)
    y -= 6

    line("Per-Customer Breakdown", size=12, bold=True)
    line(f"{'Customer':<35} {'Slips':>8} {'Total KM':>12}", size=10)
    for d in report['per_customer']:
        if y < 25 * mm:
            c.showPage(); y = height - 20 * mm
        line(f"{d['customer'][:35]:<35} {d['slips']:>8} {d['km']:>12}", size=10)
    y -= 6

    line("Daily Activity", size=12, bold=True)
    for d in report['per_day']:
        if y < 25 * mm:
            c.showPage(); y = height - 20 * mm
        line(f"{d['date']}  →  {d['slips']} slip(s)", size=10)

    c.save()
    buf.seek(0)
    return buf


@app.route('/report/month/<month>')
def monthly_report(month):
    if 'admin' not in session:
        return redirect(url_for('home'))
    if not (len(month) == 7 and month[4] == '-'):
        return redirect(url_for('settings_page'))

    report = _build_monthly_report_data(session['admin'], month)

    if request.args.get('format') == 'pdf':
        buf = _build_monthly_report_pdf(report)
        return send_file(buf, as_attachment=True, download_name=f"report_{month}.pdf", mimetype='application/pdf')

    return render_template('monthly_report.html', report=report)

@app.route('/generate', methods=['POST'])
def generate_invoice():
    if 'admin' not in session:
        return redirect(url_for('home'))

    admin_username = session['admin']

    # Gather duty slip fields
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
    dn = request.form['dn']
    remarks = request.form['remarks']
    route_covered = request.form['route_covered']
    driver_name = request.form['driver_name']

    filename = f"{customer_name.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
    filepath = str(INVOICE_DIR_PATH / filename)
    invoice_payload = {
        "customer_name": customer_name,
        "company_name": company_name,
        "date": date_value,
        "duty_slip_no": duty_slip_no,
        "vehicle_type": vehicle_type,
        "vehicle_no": vehicle_no,
        "starting_km": starting_km,
        "closing_km": closing_km,
        "total_km": total_km,
        "starting_time": starting_time,
        "closing_time": closing_time,
        "total_time": total_time,
        "dn": dn,
        "remarks": remarks,
        "route_covered": route_covered,
        "driver_name": driver_name,
    }
    build_invoice_pdf(invoice_payload, Path(filepath))

    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    execute_query(
        """
        INSERT INTO invoices(
            customer_name, company_name, "date",
            duty_slip_no, vehicle_type, vehicle_no,
            starting_km, closing_km, total_km,
            starting_time, closing_time, total_time,
            dn, remarks, route_covered, driver_name,
            admin_username, created_at, file_path
        )
        VALUES (
            :customer_name, :company_name, :date_value,
            :duty_slip_no, :vehicle_type, :vehicle_no,
            :starting_km, :closing_km, :total_km,
            :starting_time, :closing_time, :total_time,
            :dn, :remarks, :route_covered, :driver_name,
            :admin_username, :created_at, :file_path
        )
        """,
        {
            "customer_name": customer_name,
            "company_name": company_name,
            "date_value": date_value,
            "duty_slip_no": duty_slip_no,
            "vehicle_type": vehicle_type,
            "vehicle_no": vehicle_no,
            "starting_km": starting_km,
            "closing_km": closing_km,
            "total_km": total_km,
            "starting_time": starting_time,
            "closing_time": closing_time,
            "total_time": total_time,
            "dn": dn,
            "remarks": remarks,
            "route_covered": route_covered,
            "driver_name": driver_name,
            "admin_username": admin_username,
            "created_at": created_at,
            "file_path": filepath,
        },
    )

    return send_file(filepath, as_attachment=True)

# Autocomplete
@app.route('/customer_autocomplete')
def customer_autocomplete():
    if 'admin' not in session:
        return jsonify([])
    query = request.args.get('query', '').strip()
    if not query:
        return jsonify([])
    admin_username = session['admin']
    rows = fetch_all(
        """
        SELECT DISTINCT customer_name
        FROM invoices
        WHERE admin_username = :admin
          AND customer_name IS NOT NULL
          AND LOWER(customer_name) LIKE :pattern
        ORDER BY customer_name ASC
        LIMIT 10
        """,
        {"admin": admin_username, "pattern": f"%{query.lower()}%"},
    )
    return jsonify([row[0] for row in rows if row[0]])

# SETTINGS
@app.route('/settings', methods=['GET','POST'])
def settings_page():
    if 'admin' not in session:
        return redirect(url_for('home'))

    message = ""
    message_type = "success"
    if request.method == 'POST':
        new_driver = request.form.get('new_driver', '').strip()
        if new_driver:
            try:
                execute_query("INSERT INTO drivers (name) VALUES (:name)", {"name": new_driver})
                message = f"Driver '{new_driver}' added successfully!"
            except IntegrityError:
                message = f"Driver '{new_driver}' already exists!"
                message_type = "error"

    total_invoices_row = fetch_one("SELECT COUNT(*) FROM invoices")
    total_invoices = total_invoices_row[0] if total_invoices_row else 0

    current_month = datetime.now().strftime('%Y-%m')
    invoices_month_row = fetch_one(
        """
        SELECT COUNT(*)
        FROM invoices
        WHERE "date" IS NOT NULL
          AND SUBSTR("date", 1, 7) = :current_month
        """,
        {"current_month": current_month},
    )
    invoices_this_month = invoices_month_row[0] if invoices_month_row else 0

    driver_rows = fetch_all("SELECT id, name FROM drivers ORDER BY name ASC")
    driver_list = [{"id": row[0], "name": row[1]} for row in driver_rows]

    return render_template('settings.html',
                           message=message,
                           message_type=message_type,
                           total_invoices=total_invoices,
                           invoices_this_month=invoices_this_month,
                           total_drivers=len(driver_list),
                           driver_list=driver_list,
                           current_month=current_month)

@app.route('/delete_driver/<int:driver_id>', methods=['POST'])
def delete_driver(driver_id):
    if 'admin' not in session:
        return redirect(url_for('home'))
    execute_query("DELETE FROM drivers WHERE id = :driver_id", {"driver_id": driver_id})
    return redirect(url_for('settings_page'))

@app.route("/invoice/<int:invoice_id>/download")
def download_invoice(invoice_id):
    if 'admin' not in session:
        return redirect(url_for('home'))
    admin_username = session['admin']
    invoice_row = fetch_one(
        """
        SELECT id, customer_name, company_name, "date", duty_slip_no, vehicle_type,
               vehicle_no, starting_km, closing_km, total_km, starting_time,
               closing_time, total_time, dn, remarks, route_covered, driver_name, file_path
        FROM invoices
        WHERE id = :invoice_id AND admin_username = :admin
        """,
        {"invoice_id": invoice_id, "admin": admin_username},
    )
    if not invoice_row:
        return redirect(url_for('admin_portal'))
    resolved_path = ensure_invoice_pdf_path(invoice_row)
    if not _is_safe_invoice_path(resolved_path):
        abort(404)
    return send_file(resolved_path, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)
