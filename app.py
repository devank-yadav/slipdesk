import os
import io
from datetime import datetime
from pathlib import Path
from typing import Optional, Mapping, Any
import zipfile

from flask import Flask, render_template, request, send_file, redirect, url_for, session, jsonify
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from sqlalchemy import create_engine, text, MetaData, Table, Column, Integer, String, Text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

app = Flask(__name__)
app.secret_key = 'mysecretkey'

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


def init_db():
    metadata.create_all(engine)
    ensure_invoice_columns()

    with engine.begin() as conn:
        user_exists = conn.execute(
            text("SELECT 1 FROM users WHERE username = :username"),
            {"username": "admin"},
        ).first()
        if not user_exists:
            conn.execute(
                text("INSERT INTO users (username, password) VALUES (:username, :password)"),
                {"username": "admin", "password": "admin"},
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

@app.route('/admin_login', methods=['POST'])
def admin_login():
    username = request.form['username']
    password = request.form['password']
    user = fetch_one(
        """
        SELECT id FROM users
        WHERE username = :username AND password = :password
        """,
        {"username": username, "password": password},
    )
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
        conditions.append("SUBSTR(created_at, 1, 7) = :month_filter")
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

    param_map = {f"id_{idx}": value for idx, value in enumerate(id_values)}
    placeholder_list = ", ".join(f":id_{idx}" for idx in range(len(id_values)))
    select_stmt = text(f"SELECT id, file_path FROM invoices WHERE id IN ({placeholder_list})")

    with engine.begin() as conn:
        selected = conn.execute(select_stmt, param_map).fetchall()
        if not selected:
            return redirect(url_for('admin_portal'))

        if action == 'delete':
            conn.execute(
                text(f"DELETE FROM invoices WHERE id IN ({placeholder_list})"),
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
                if resolved_path and os.path.exists(resolved_path):
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
@app.route('/generator')
def generator_form():
    if 'admin' not in session:
        return redirect(url_for('home'))

    driver_rows = fetch_all("SELECT name FROM drivers ORDER BY name ASC")
    driver_list = [row[0] for row in driver_rows]

    return render_template('generator.html', driver_list=driver_list)

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
    if request.method == 'POST':
        new_driver = request.form.get('new_driver', '').strip()
        if new_driver:
            try:
                execute_query("INSERT INTO drivers (name) VALUES (:name)", {"name": new_driver})
                message = f"Driver '{new_driver}' added successfully!"
            except IntegrityError:
                message = f"Driver '{new_driver}' already exists!"

    total_invoices_row = fetch_one("SELECT COUNT(*) FROM invoices")
    total_invoices = total_invoices_row[0] if total_invoices_row else 0

    current_month = datetime.now().strftime('%Y-%m')
    invoices_month_row = fetch_one(
        """
        SELECT COUNT(*)
        FROM invoices
        WHERE created_at IS NOT NULL
          AND SUBSTR(created_at, 1, 7) = :current_month
        """,
        {"current_month": current_month},
    )
    invoices_this_month = invoices_month_row[0] if invoices_month_row else 0

    total_drivers_row = fetch_one("SELECT COUNT(*) FROM drivers")
    total_drivers = total_drivers_row[0] if total_drivers_row else 0

    return render_template('settings.html',
                           message=message,
                           total_invoices=total_invoices,
                           invoices_this_month=invoices_this_month,
                           total_drivers=total_drivers)

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
    return send_file(resolved_path, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)
