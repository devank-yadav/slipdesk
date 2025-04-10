import os
import io
import zipfile
import sqlite3
from datetime import datetime

from flask import Flask, render_template, request, send_file, redirect, url_for, session, jsonify
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from PIL import Image

app = Flask(__name__)
app.secret_key = 'mysecretkey'

DATABASE = 'invoices.db'
INVOICE_DIR = 'generated_invoices'
os.makedirs(INVOICE_DIR, exist_ok=True)

def init_db():
    with sqlite3.connect(DATABASE) as conn:
        # Create base invoices table if not exist
        conn.execute('''
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT,
                date TEXT,
                file_path TEXT,
                admin_username TEXT
            )
        ''')
        # Check columns, add missing
        columns = [col[1] for col in conn.execute("PRAGMA table_info(invoices)").fetchall()]
        if 'created_at' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN created_at TEXT")
        if 'driver_name' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN driver_name TEXT")
        if 'company_name' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN company_name TEXT")
        if 'duty_slip_no' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN duty_slip_no TEXT")
        if 'vehicle_type' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN vehicle_type TEXT")
        if 'vehicle_no' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN vehicle_no TEXT")
        if 'starting_km' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN starting_km TEXT")
        if 'closing_km' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN closing_km TEXT")
        if 'total_km' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN total_km TEXT")
        if 'starting_time' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN starting_time TEXT")
        if 'closing_time' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN closing_time TEXT")
        if 'total_time' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN total_time TEXT")
        # Separate DN and Remarks
        if 'dn' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN dn TEXT")
        if 'remarks' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN remarks TEXT")
        if 'route_covered' not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN route_covered TEXT")

        # Users table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                password TEXT
            )
        ''')
        cur = conn.execute("SELECT * FROM users WHERE username = ?", ("admin",))
        if not cur.fetchone():
            conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", ("admin", "admin"))

        # Drivers table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS drivers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        ''')

init_db()

def delete_invoice_file(invoice_id, admin_username):
    """Remove invoice record + PDF file if exists."""
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute("""
            SELECT file_path FROM invoices
            WHERE id = ? AND admin_username = ?
        """, (invoice_id, admin_username))
        row = cursor.fetchone()
        if row:
            file_path = row[0]
            conn.execute("""
                DELETE FROM invoices
                WHERE id = ? AND admin_username = ?
            """, (invoice_id, admin_username))
            if os.path.exists(file_path):
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
    with sqlite3.connect(DATABASE) as conn:
        cur = conn.execute("""
            SELECT * FROM users
            WHERE username = ? AND password = ?
        """, (username, password))
        user = cur.fetchone()
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

    where_clause = "WHERE admin_username = ?"
    params = [admin_username]

    if customer_name:
        where_clause += " AND customer_name LIKE ?"
        params.append(f"%{customer_name}%")
    if driver_filter:
        where_clause += " AND driver_name = ?"
        params.append(driver_filter)
    if month_filter:
        where_clause += " AND strftime('%Y-%m', created_at) = ?"
        params.append(month_filter)
    if date_from:
        where_clause += " AND date >= ?"
        params.append(date_from)
    if date_to:
        where_clause += " AND date <= ?"
        params.append(date_to)

    query = f"""
        SELECT
          id, customer_name, date, file_path,
          created_at, driver_name
        FROM invoices
        {where_clause}
        ORDER BY id DESC
    """

    with sqlite3.connect(DATABASE) as conn:
        cur = conn.execute(query, tuple(params))
        invoices = cur.fetchall()

        # distinct drivers
        cur_drivers = conn.execute("""
            SELECT DISTINCT driver_name
            FROM invoices
            WHERE admin_username = ?
              AND driver_name IS NOT NULL
            ORDER BY driver_name ASC
        """, (admin_username,))
        driver_names = [row[0] for row in cur_drivers if row[0]]

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
import zipfile

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
        cursor = conn.execute(f"SELECT id, file_path FROM invoices WHERE id IN ({placeholders})", selected_ids)
        selected = cursor.fetchall()

        if action == 'delete':
            for inv_id, path in selected:
                conn.execute("DELETE FROM invoices WHERE id = ?", (inv_id,))
                if os.path.exists(path):
                    os.remove(path)
            return redirect(url_for('admin_portal'))

        elif action == 'download':
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w') as zipf:
                for inv_id, path in selected:
                    if os.path.exists(path):
                        zipf.write(path, arcname=os.path.basename(path))
            zip_buffer.seek(0)
            return send_file(zip_buffer,
                             as_attachment=True,
                             download_name='invoices.zip',
                             mimetype='application/zip')
    return redirect(url_for('admin_portal'))

# --------------------------
# GENERATE INVOICE
# --------------------------
@app.route('/generator')
def generator_form():
    if 'admin' not in session:
        return redirect(url_for('home'))

    with sqlite3.connect(DATABASE) as conn:
        c = conn.execute("SELECT name FROM drivers ORDER BY name ASC")
        driver_list = [row[0] for row in c.fetchall()]

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
    filepath = os.path.join(INVOICE_DIR, filename)

    # Landscape orientation
    c = canvas.Canvas(filepath, pagesize=landscape(A4))
    width, height = landscape(A4)  # ~842 x 595

    # Optional background
    template_path = "invoice_template.png"
    if os.path.exists(template_path):
        c.drawImage(ImageReader(template_path), 0, 0, width=width, height=height)

    c.setFont("Helvetica", 12)
    topY = height - 40
    line_spacing = 20

    # Row 1
    c.drawString(40, topY, f"CUSTOMER NAME: {customer_name}")
    c.drawString(300, topY, f"COMPANY NAME: {company_name}")

    # Row 2
    c.drawString(40, topY - line_spacing, f"DATE: {date_value}")
    c.drawString(300, topY - line_spacing, f"DUTY SLIP NO: {duty_slip_no}")

    # Row 3
    c.drawString(40, topY - 2*line_spacing, f"TYPE OF VEHICLE: {vehicle_type}")
    c.drawString(300, topY - 2*line_spacing, f"VEHICLE NO: {vehicle_no}")

    # Row 4
    c.drawString(40, topY - 3*line_spacing, f"STARTING KM: {starting_km}")
    c.drawString(300, topY - 3*line_spacing, f"CLOSING KM: {closing_km}")
    c.drawString(540, topY - 3*line_spacing, f"TOTAL KM: {total_km}")

    # Row 5
    c.drawString(40, topY - 4*line_spacing, f"STARTING TIME: {starting_time}")
    c.drawString(300, topY - 4*line_spacing, f"CLOSING TIME: {closing_time}")
    c.drawString(540, topY - 4*line_spacing, f"TOTAL TIME: {total_time}")

    # Row 6 (DN, Remarks)
    c.drawString(40, topY - 5*line_spacing, f"DN: {dn}")
    c.drawString(300, topY - 5*line_spacing, f"Remarks: {remarks}")

    # Row 7
    c.drawString(40, topY - 6*line_spacing, f"ROUTE COVERED: {route_covered}")

    # Row 8
    c.drawString(40, topY - 7*line_spacing, f"DRIVER NAME: {driver_name}")

    c.save()

    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    with sqlite3.connect(DATABASE) as conn:
        conn.execute("""
            INSERT INTO invoices(
                customer_name, company_name, date,
                duty_slip_no, vehicle_type, vehicle_no,
                starting_km, closing_km, total_km,
                starting_time, closing_time, total_time,
                dn, remarks, route_covered, driver_name,
                admin_username, created_at, file_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            customer_name, company_name, date_value,
            duty_slip_no, vehicle_type, vehicle_no,
            starting_km, closing_km, total_km,
            starting_time, closing_time, total_time,
            dn, remarks, route_covered, driver_name,
            admin_username, created_at, filepath
        ))

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
    with sqlite3.connect(DATABASE) as conn:
        cur = conn.execute("""
            SELECT DISTINCT customer_name
            FROM invoices
            WHERE admin_username = ?
              AND customer_name LIKE ?
            ORDER BY customer_name ASC
            LIMIT 10
        """, (admin_username, f"%{query}%"))
        results = [row[0] for row in cur.fetchall()]
    return jsonify(results)

# SETTINGS
@app.route('/settings', methods=['GET','POST'])
def settings_page():
    if 'admin' not in session:
        return redirect(url_for('home'))

    message = ""
    if request.method == 'POST':
        new_driver = request.form.get('new_driver', '').strip()
        if new_driver:
            with sqlite3.connect(DATABASE) as conn:
                try:
                    conn.execute("INSERT INTO drivers (name) VALUES (?)", (new_driver,))
                    message = f"Driver '{new_driver}' added successfully!"
                except sqlite3.IntegrityError:
                    message = f"Driver '{new_driver}' already exists!"

    with sqlite3.connect(DATABASE) as conn:
        # total invoices
        c1 = conn.execute("SELECT COUNT(*) FROM invoices")
        total_invoices = c1.fetchone()[0]

        # invoices this month
        c2 = conn.execute("""
            SELECT COUNT(*)
            FROM invoices
            WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m','now')
        """)
        invoices_this_month = c2.fetchone()[0]

        # total drivers
        c3 = conn.execute("SELECT COUNT(*) FROM drivers")
        total_drivers = c3.fetchone()[0]

    return render_template('settings.html',
                           message=message,
                           total_invoices=total_invoices,
                           invoices_this_month=invoices_this_month,
                           total_drivers=total_drivers)

@app.route(f"/{INVOICE_DIR}/<filename>")
def download_invoice(filename):
    return send_file(os.path.join(INVOICE_DIR, filename), as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)
