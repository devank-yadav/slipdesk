"""
One-time migration script: copy data from local invoices.db (SQLite) to a remote
Postgres / managed database whose URL is given via the DATABASE_URL env var.

Usage:
    export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
    python scripts/migrate_to_postgres.py
"""
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text, MetaData

ROOT = Path(__file__).resolve().parent.parent
SQLITE_PATH = ROOT / "invoices.db"
SQLITE_URL = f"sqlite:///{SQLITE_PATH}"

dest_url = os.environ.get("DATABASE_URL")
if not dest_url:
    print("ERROR: DATABASE_URL env var is required (target Postgres URL).")
    sys.exit(1)

if not SQLITE_PATH.exists():
    print(f"No source SQLite file at {SQLITE_PATH} — nothing to migrate.")
    sys.exit(0)

print(f"Source : {SQLITE_URL}")
print(f"Target : {dest_url}")

src_engine = create_engine(SQLITE_URL, future=True)
dst_engine = create_engine(dest_url, future=True, pool_pre_ping=True)

# Initialize destination schema using the app's models
sys.path.insert(0, str(ROOT))
os.environ["DATABASE_URL"] = dest_url  # so app.py inits the right engine
import app as _app  # noqa: F401  (this triggers init_db on the destination)

TABLES = ["users", "drivers", "invoices", "slip_templates"]

with src_engine.connect() as src, dst_engine.begin() as dst:
    for tbl in TABLES:
        try:
            rows = src.execute(text(f"SELECT * FROM {tbl}")).mappings().all()
        except Exception as e:
            print(f"  - {tbl}: skipping ({e.__class__.__name__})")
            continue
        if not rows:
            print(f"  - {tbl}: 0 rows")
            continue
        cols = list(rows[0].keys())
        col_list = ", ".join(f'"{c}"' for c in cols)
        param_list = ", ".join(f":{c}" for c in cols)
        insert_sql = text(f'INSERT INTO {tbl} ({col_list}) VALUES ({param_list})')
        n = 0
        for row in rows:
            try:
                dst.execute(insert_sql, dict(row))
                n += 1
            except Exception as e:
                print(f"    skip row id={row.get('id')}: {e.__class__.__name__}: {e}")
        print(f"  + {tbl}: copied {n}/{len(rows)} rows")

print("Migration complete.")
