from flask import Flask, render_template, request, send_file
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from datetime import datetime
import sqlite3
import os
from PIL import Image

app = Flask(__name__)
DATABASE = 'invoices.db'
INVOICE_DIR = 'generated_invoices'
os.makedirs(INVOICE_DIR, exist_ok=True)

# Setup DB
def init_db():
    with sqlite3.connect(DATABASE) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS invoices (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            customer_name TEXT,
                            date TEXT,
                            file_path TEXT
                        )''')
init_db()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/generate', methods=['POST'])
def generate():
    name = request.form['customer_name']
    date = request.form['date']
    desc = request.form['description']
    quantity = int(request.form['quantity'])
    price = float(request.form['price'])
    total = quantity * price

    filename = f"{name.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
    filepath = os.path.join(INVOICE_DIR, filename)

    # Load invoice image template
    template_path = "invoice_template.png"  # Put this file in the same directory
    width, height = A4
    c = canvas.Canvas(filepath, pagesize=A4)
    c.drawImage(ImageReader(template_path), 0, 0, width=width, height=height)

    # Draw text on top of image
    c.setFont("Helvetica", 12)
    c.drawString(100, 700, f"Customer: {name}")
    c.drawString(100, 680, f"Date: {date}")
    c.drawString(100, 640, f"Item: {desc}")
    c.drawString(100, 620, f"Quantity: {quantity}")
    c.drawString(100, 600, f"Price per item: ₹{price:.2f}")
    c.drawString(100, 580, f"Total: ₹{total:.2f}")
    c.save()

    # Save in DB
    with sqlite3.connect(DATABASE) as conn:
        conn.execute("INSERT INTO invoices (customer_name, date, file_path) VALUES (?, ?, ?)", (name, date, filepath))

    return send_file(filepath, as_attachment=True)

@app.route('/invoices')
def show_invoices():
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.execute("SELECT customer_name, date, file_path FROM invoices ORDER BY id DESC")
        rows = cursor.fetchall()
    html = "<h2>Generated Invoices</h2><ul>"
    for row in rows:
        html += f'<li>{row[0]} - {row[1]} - <a href="/{row[2]}">Download</a></li>'
    html += "</ul>"
    return html

@app.route(f"/{INVOICE_DIR}/<filename>")
def download_invoice(filename):
    return send_file(os.path.join(INVOICE_DIR, filename), as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)