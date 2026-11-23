"""SQLite: run history + invoice ledger (used for duplicate checks and PO balances)."""
import json
import sqlite3
from datetime import datetime

import pandas as pd

from config import DB_PATH, DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT, filename TEXT, file_hash TEXT,
    doc_type TEXT, extraction_method TEXT,
    vendor_id TEXT, vendor_name TEXT,
    invoice_no TEXT, invoice_no_norm TEXT, invoice_date TEXT,
    po_number TEXT, subtotal REAL, tax REAL, total REAL,
    decision TEXT, final_status TEXT, summary TEXT, next_action TEXT,
    reasons TEXT, stages TEXT, extracted TEXT, email_draft TEXT,
    reviewer_note TEXT, duration_ms INTEGER
);
"""
NEW_COLS = {"pdf": "BLOB", "source": "TEXT", "tokens_in": "INTEGER", "tokens_out": "INTEGER",
            "cost_inr": "REAL", "reviewed_at": "TEXT"}


def conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute(SCHEMA)
    have = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
    for col, typ in NEW_COLS.items():  # migrate older databases in place
        if col not in have:
            c.execute(f"ALTER TABLE runs ADD COLUMN {col} {typ}")
    return c


def save_run(r: dict) -> int:
    cols = ["created_at", "filename", "file_hash", "doc_type", "extraction_method",
            "vendor_id", "vendor_name", "invoice_no", "invoice_no_norm", "invoice_date",
            "po_number", "subtotal", "tax", "total", "decision", "final_status", "summary",
            "next_action", "reasons", "stages", "extracted", "email_draft", "reviewer_note",
            "duration_ms"] + list(NEW_COLS)
    r = dict(r)
    r.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    for k in ("reasons", "stages", "extracted"):
        r[k] = json.dumps(r.get(k), default=str)
    with conn() as c:
        cur = c.execute(
            f"INSERT INTO runs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [r.get(k) for k in cols])
        return cur.lastrowid


def _row(row):
    d = dict(row)
    for k in ("reasons", "stages", "extracted"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


def get_run(run_id):
    with conn() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _row(row) if row else None


def list_runs():
    with conn() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(runs)") if r[1] != "pdf"]
        return pd.read_sql_query(f"SELECT {','.join(cols)} FROM runs ORDER BY id DESC", c)


def prior_invoices(vendor_id):
    """Invoices from this vendor that are live (not rejected)."""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM runs WHERE vendor_id=? AND final_status NOT IN ('REJECTED', 'SUPERSEDED')",
            (vendor_id,)).fetchall()
    return [dict(r) for r in rows]


def find_by_hash(file_hash):
    with conn() as c:
        row = c.execute("SELECT id, final_status FROM runs WHERE file_hash=? AND final_status NOT IN ('REJECTED', 'SUPERSEDED')",
                        (file_hash,)).fetchone()
    return dict(row) if row else None


def po_consumed(po_number):
    """A4: only APPROVED invoices consume PO balance."""
    with conn() as c:
        v = c.execute("SELECT COALESCE(SUM(subtotal),0) FROM runs WHERE po_number=? AND final_status='APPROVED'",
                      (po_number,)).fetchone()[0]
    return float(v or 0)


def resolve(run_id, status, note):
    with conn() as c:
        c.execute("UPDATE runs SET final_status=?, reviewer_note=?, reviewed_at=? WHERE id=?",
                  (status, note, datetime.now().isoformat(timespec="seconds"), run_id))


def reset():
    with conn() as c:
        c.execute("DELETE FROM runs")
        c.execute("DELETE FROM sqlite_sequence WHERE name='runs'")


def vendors():
    return pd.read_csv(DATA / "vendors.csv", dtype=str)


def purchase_orders():
    df = pd.read_csv(DATA / "purchase_orders.csv", dtype={"po_amount": float})
    df["approved_billed"] = df["po_number"].map(po_consumed)
    df["remaining"] = df["po_amount"] - df["approved_billed"]
    return df


def add_vendor(name, gstin, terms, email):
    df = vendors()
    vid = f"V{len(df) + 1:03d}"
    row = pd.DataFrame([{"vendor_id": vid, "vendor_name": name, "gstin": gstin.upper().strip(),
                         "status": "active", "payment_terms_days": int(terms), "contact_email": email}])
    pd.concat([df, row]).to_csv(DATA / "vendors.csv", index=False)
    return vid


def add_po(po_number, vendor_id, amount, description):
    df = pd.read_csv(DATA / "purchase_orders.csv", dtype=str)
    row = pd.DataFrame([{"po_number": po_number.strip().upper(), "vendor_id": vendor_id, "po_amount": f"{float(amount):.2f}",
                         "status": "open", "po_date": datetime.now().date().isoformat(), "description": description}])
    pd.concat([df, row]).to_csv(DATA / "purchase_orders.csv", index=False)
