
from __future__ import annotations

import csv
import html
import io
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

APP_TITLE = "Warehouse Fixer"
BASE_DIR = os.path.dirname(__file__)
DB_FILE = os.environ.get("WAREHOUSE_DB_FILE", os.path.join(BASE_DIR, "warehouse.db"))

app = FastAPI(title=APP_TITLE)


# -------------------------
# Database helpers
# -------------------------
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS inbound_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            datetime TEXT NOT NULL,
            product TEXT NOT NULL,
            qty INTEGER NOT NULL,
            location TEXT NOT NULL,
            user TEXT NOT NULL,
            notes TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS removed_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            removed_at TEXT NOT NULL,
            removed_by TEXT NOT NULL,
            original_product TEXT NOT NULL,
            original_location TEXT NOT NULL,
            removed_qty INTEGER NOT NULL,
            reason TEXT
        )
        """
    )

    def ensure_col(table: str, col: str, ddl: str) -> None:
        cur.execute(f"PRAGMA table_info({table})")
        existing = [r["name"] for r in cur.fetchall()]
        if col not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    ensure_col("inbound_log", "notes", "notes TEXT")
    ensure_col("removed_log", "reason", "reason TEXT")

    conn.commit()
    conn.close()


init_db()


# -------------------------
# Helpers
# -------------------------
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def norm(value: Optional[str]) -> str:
    return (value or "").strip()


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def current_qty_for(conn: sqlite3.Connection, product: str, location: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(qty), 0) AS q
        FROM inbound_log
        WHERE UPPER(TRIM(product)) = UPPER(TRIM(?))
          AND UPPER(TRIM(location)) = UPPER(TRIM(?))
        """,
        (product, location),
    ).fetchone()
    return int(row["q"] if row and row["q"] is not None else 0)


def insert_movement(
    conn: sqlite3.Connection,
    product: str,
    location: str,
    qty_delta: int,
    user: str,
    dt_iso: Optional[str] = None,
    notes: str = "",
) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO inbound_log (datetime, product, qty, location, user, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (dt_iso or now_iso(), product, int(qty_delta), location, user, notes),
    )
    return int(cur.lastrowid)


def log_removed(
    conn: sqlite3.Connection,
    product: str,
    location: str,
    removed_qty: int,
    removed_by: str,
    reason: str = "",
) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO removed_log (removed_at, removed_by, original_product, original_location, removed_qty, reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (now_iso(), removed_by, product, location, int(removed_qty), reason),
    )
    return int(cur.lastrowid)


def csv_response(rows: List[Dict[str, Any]], fieldnames: List[str], filename: str) -> StreamingResponse:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -------------------------
# API endpoints
# -------------------------
@app.get("/", response_class=JSONResponse)
def root() -> Dict[str, Any]:
    return {"status": "ok", "message": f"{APP_TITLE} API is running"}


@app.get("/health", response_class=JSONResponse)
def health() -> Dict[str, bool]:
    return {"ok": True}


@app.get("/inventory", response_class=JSONResponse)
def inventory_api() -> Dict[str, List[Dict[str, Any]]]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT TRIM(product) AS product, TRIM(location) AS location, COALESCE(SUM(qty), 0) AS qty
        FROM inbound_log
        GROUP BY TRIM(product), TRIM(location)
        HAVING COALESCE(SUM(qty), 0) != 0
        ORDER BY TRIM(product), TRIM(location)
        """
    ).fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows]}


@app.get("/lookup/products", response_class=JSONResponse)
def lookup_products(q: str = "") -> Dict[str, List[Dict[str, Any]]]:
    q = norm(q)
    if not q:
        return {"items": []}

    like = f"%{q}%"
    prefix = f"{q}%"
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT
            TRIM(product) AS product,
            COALESCE(SUM(qty), 0) AS total_qty,
            COUNT(DISTINCT TRIM(location)) AS pallet_count
        FROM inbound_log
        WHERE TRIM(product) LIKE TRIM(?)
        GROUP BY TRIM(product)
        HAVING COALESCE(SUM(qty), 0) != 0
        ORDER BY
            CASE
                WHEN UPPER(TRIM(product)) = UPPER(TRIM(?)) THEN 0
                WHEN UPPER(TRIM(product)) LIKE UPPER(TRIM(?)) THEN 1
                ELSE 2
            END,
            TRIM(product)
        LIMIT 50
        """,
        (like, q, prefix),
    ).fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows]}


@app.get("/lookup/pallets/{product}", response_class=JSONResponse)
def lookup_pallets(product: str) -> Dict[str, List[Dict[str, Any]]]:
    product = norm(product)
    if not product:
        return {"items": []}

    conn = get_conn()
    rows = conn.execute(
        """
        SELECT
            TRIM(location) AS location,
            COALESCE(SUM(qty), 0) AS qty
        FROM inbound_log
        WHERE UPPER(TRIM(product)) = UPPER(TRIM(?))
        GROUP BY TRIM(location)
        HAVING COALESCE(SUM(qty), 0) != 0
        ORDER BY TRIM(location)
        """,
        (product,),
    ).fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows]}


@app.get("/export/inventory.csv")
def export_inventory_csv() -> StreamingResponse:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT TRIM(product) AS product, TRIM(location) AS location, COALESCE(SUM(qty), 0) AS qty
        FROM inbound_log
        GROUP BY TRIM(product), TRIM(location)
        ORDER BY TRIM(product), TRIM(location)
        """
    ).fetchall()
    conn.close()
    return csv_response([dict(r) for r in rows], ["product", "location", "qty"], "inventory.csv")


@app.get("/export/inbound.csv")
def export_inbound_csv() -> StreamingResponse:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, datetime, product, qty, location, user, COALESCE(notes,'') AS notes
        FROM inbound_log
        ORDER BY id ASC
        """
    ).fetchall()
    conn.close()
    return csv_response([dict(r) for r in rows], ["id", "datetime", "product", "qty", "location", "user", "notes"], "inbound.csv")


@app.get("/export/removed.csv")
def export_removed_csv() -> StreamingResponse:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, removed_at, removed_by, original_product, original_location, removed_qty, COALESCE(reason,'') AS reason
        FROM removed_log
        ORDER BY id ASC
        """
    ).fetchall()
    conn.close()
    return csv_response([dict(r) for r in rows], ["id", "removed_at", "removed_by", "original_product", "original_location", "removed_qty", "reason"], "removed.csv")


@app.post("/quick/in")
def quick_in(payload: Dict[str, Any]) -> Dict[str, Any]:
    product = norm(payload.get("product"))
    location = norm(payload.get("location"))
    user = "SYSTEM"
    notes = ""
    qty = payload.get("qty")

    if not product or not location:
        raise HTTPException(status_code=400, detail="product and location required")

    try:
        qty_i = int(float(qty))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="qty must be a number") from exc

    if qty_i == 0:
        raise HTTPException(status_code=400, detail="qty cannot be 0")

    conn = get_conn()
    try:
        before = current_qty_for(conn, product, location)
        insert_movement(conn, product, location, qty_i, user, None, notes or "IN")
        conn.commit()
        return {"ok": True, "product": product, "location": location, "before": before, "delta": qty_i, "after": before + qty_i}
    finally:
        conn.close()


@app.post("/quick/out")
def quick_out(payload: Dict[str, Any]) -> Dict[str, Any]:
    product = norm(payload.get("product"))
    location = norm(payload.get("location"))
    user = "SYSTEM"
    notes = ""

    if not product or not location:
        raise HTTPException(status_code=400, detail="product and location required")

    conn = get_conn()
    try:
        before = current_qty_for(conn, product, location)
        if before == 0:
            return {"ok": True, "skipped": True, "reason": "no stock at location", "product": product, "location": location, "before": 0, "after": 0}

        insert_movement(conn, product, location, -before, user, None, notes or "OUT")
        log_removed(conn, product, location, before, user, reason=notes or "OUT")
        conn.commit()
        return {"ok": True, "product": product, "location": location, "before": before, "delta": -before, "after": 0}
    finally:
        conn.close()


@app.post("/quick/change")
def quick_change(payload: Dict[str, Any]) -> Dict[str, Any]:
    product = norm(payload.get("product"))
    location = norm(payload.get("location"))
    user = "SYSTEM"
    notes = ""
    qty = payload.get("qty")

    if not product or not location:
        raise HTTPException(status_code=400, detail="product and location required")

    try:
        new_qty = int(float(qty))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="qty must be a number") from exc

    if new_qty < 0:
        raise HTTPException(status_code=400, detail="qty cannot be negative")

    conn = get_conn()
    try:
        before = current_qty_for(conn, product, location)
        delta = new_qty - before
        if delta == 0:
            return {"ok": True, "skipped": True, "reason": "no change", "product": product, "location": location, "before": before, "after": before, "delta": 0}

        insert_movement(conn, product, location, delta, user, None, notes or "CHANGE")
        if delta < 0:
            log_removed(conn, product, location, -delta, user, reason=notes or "CHANGE reduction")
        conn.commit()
        return {"ok": True, "product": product, "location": location, "before": before, "delta": delta, "after": new_qty}
    finally:
        conn.close()


# -------------------------
# Theme/UI
# -------------------------
THEME_CSS = r"""
:root{
  --bg:#0b1220;
  --text:#e9eefc;
  --muted:rgba(233,238,252,.75);
  --shadow: 0 10px 30px rgba(0,0,0,.35);
  --radius:18px;
  --panel: rgba(17,26,46,.86);
  --border: rgba(255,255,255,.08);
}
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0;
  font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
  background: radial-gradient(1200px 600px at 20% 10%, rgba(55,90,255,.18), transparent 60%),
              radial-gradient(900px 500px at 80% 20%, rgba(0,255,170,.10), transparent 60%),
              var(--bg);
  color:var(--text);
}
.container{max-width:1100px;margin:0 auto;padding:18px;}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px;flex-wrap:wrap;}
.brand{display:flex;align-items:center;gap:12px;}
.badge{
  width:44px;height:44px;border-radius:14px;
  background: linear-gradient(135deg, rgba(55,90,255,1), rgba(0,255,170,1));
  box-shadow: var(--shadow);
}
h1{font-size:22px;margin:0}
.sub{color:var(--muted); font-size:13px}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;}
@media (max-width: 900px){.grid{grid-template-columns:repeat(2,minmax(0,1fr));}}
@media (max-width: 620px){.grid{grid-template-columns:1fr;}}
.tile{
  border:0;text-decoration:none;color:white;border-radius:var(--radius);
  padding:18px 16px;min-height:140px;
  display:flex;flex-direction:column;justify-content:space-between;
  box-shadow:var(--shadow);position:relative;overflow:hidden;cursor:pointer;user-select:none;
}
.tile:active{transform: translateY(1px);}
.tile .label{font-weight:900;letter-spacing:.4px;font-size:18px;}
.tile .hint{font-size:13px;opacity:.92;}
.iconWrap{
  width:56px;height:56px;border-radius:16px;background:rgba(255,255,255,.18);
  display:flex;align-items:center;justify-content:center;backdrop-filter: blur(6px);
}
svg{width:30px;height:30px;fill:#fff}
.c-blue{background: linear-gradient(135deg,#2d6bff,#00c6ff);}
.c-green{background: linear-gradient(135deg,#00c853,#00e5ff);}
.c-purple{background: linear-gradient(135deg,#7c4dff,#ff4081);}
.c-orange{background: linear-gradient(135deg,#ff6d00,#ffca28);}
.c-red{background: linear-gradient(135deg,#ff1744,#ff5252);}
.c-teal{background: linear-gradient(135deg,#00bfa5,#1de9b6);}
.panel{
  background: rgba(17,26,46,.75);
  border: 1px solid rgba(255,255,255,.08);
  border-radius: var(--radius);
  padding: 14px;
  box-shadow: var(--shadow);
}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center;}
.btn{
  border:0;border-radius:14px;padding:12px 14px;font-weight:900;cursor:pointer;color:white;
  background: rgba(255,255,255,.14);
}
.btn:active{transform: translateY(1px);}
.small{font-size:12px; opacity:.85}
pre{
  white-space:pre-wrap;background:rgba(0,0,0,.25);padding:12px;border-radius:14px;
  border:1px solid rgba(255,255,255,.08);overflow:auto;
}
input[type=text], input[type=number], select{
  width:100%;
  padding:14px 14px;
  border-radius:14px;
  background:rgba(255,255,255,.08);
  border:1px solid rgba(255,255,255,.12);
  color:var(--text);
  font-size:16px;
  font-weight:700;
}
label{display:block;margin:10px 0 6px 0;font-weight:800;color:var(--muted);font-size:12px;letter-spacing:.4px;}
input[type=file]{
  width:100%;padding:12px;border-radius:14px;background:rgba(255,255,255,.08);
  border:1px solid rgba(255,255,255,.12);color:var(--text);
}
a{color:#b8c7ff}
.table{
  width:100%;
  border-collapse:separate;
  border-spacing:0;
  overflow:hidden;
  border-radius:14px;
  border:1px solid rgba(255,255,255,.10);
}
.table th, .table td{
  padding:10px 10px;
  text-align:left;
  font-size:13px;
  border-bottom:1px solid rgba(255,255,255,.06);
}
.table th{color:var(--muted);font-weight:900;font-size:12px;letter-spacing:.4px;}
.table tr:last-child td{border-bottom:0}

.quick-body{overflow:hidden;}
.quick-container{max-width:none;height:100vh;padding:10px 12px 12px 12px;display:flex;flex-direction:column;}
.quick-topbar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px;flex:0 0 auto;flex-wrap:wrap;}
.quick-brand{display:flex;align-items:center;gap:10px;}
.quick-badge{width:36px;height:36px;border-radius:12px;background: linear-gradient(135deg, rgba(55,90,255,1), rgba(0,255,170,1));box-shadow: var(--shadow);}
.quick-title{font-size:20px;margin:0;}
.quick-sub{color:var(--muted);font-size:12px;}
.quick-nav{display:flex;gap:8px;flex-wrap:wrap;}
.quick-nav .btn{padding:10px 12px;}
.quick-main{flex:1 1 auto;min-height:0;display:flex;align-items:stretch;justify-content:center;}
.quick-workspace{width:100%;min-height:0;}
.quick-panel{height:100%;background: var(--panel);border: 1px solid var(--border);border-radius: 22px;padding: 14px;box-shadow: var(--shadow);display:flex;flex-direction:column;min-height:0;}
.quick-header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px;flex:0 0 auto;}
.quick-header h2{margin:0 0 4px 0;font-size:24px;}
.quick-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 12px;flex:1 1 auto;min-height:0;align-content:start;}
.quick-field.full{grid-column:1 / -1;}
.quick-field label{margin:0 0 5px 0;}
.quick-field input{height:52px;}
.quick-actions{display:flex;gap:12px;margin-top:12px;flex:0 0 auto;}
.quick-help{font-size:12px;color:var(--muted);}
.quickModal{position:fixed;inset:0;background:rgba(0,0,0,.72);display:none;align-items:center;justify-content:center;z-index:99999;padding:18px;}
.quickModal.show{display:flex;}
.quickModalBox{width:min(560px, 100%);background: rgba(17,26,46,.98);border:1px solid rgba(255,255,255,.10);border-radius:22px;box-shadow:var(--shadow);padding:18px;}
.quickModalTitle{font-size:24px;font-weight:900;margin-bottom:10px;}
.quickModalBody{white-space:pre-wrap;word-break:break-word;background:rgba(0,0,0,.25);padding:12px;border-radius:14px;border:1px solid rgba(255,255,255,.08);max-height:44vh;overflow:auto;}
.nav-btn{width:60px;height:60px;display:flex;align-items:center;justify-content:center;padding:0;}

.list-box {
  background: #1f2937;
  border: 1px solid #374151;
  border-radius: 14px;
  max-height: 260px;
  min-height: 72px;
  overflow-y: auto;
  padding: 6px;
}
.list-item {
  padding: 10px 12px;
  border-radius: 10px;
  cursor: pointer;
  color: #e5e7eb;
  font-weight: 700;
}
.list-item:hover { background: #374151; }
.list-item.selected { background: #2563eb; color: #ffffff; }
.list-placeholder { color: #9ca3af; padding: 10px 12px; }
.status-msg{margin-top:6px;color:var(--muted);font-size:12px;min-height:16px;}

.swipe-wrap{margin-top:14px;}
.swipe-track{
  position:relative;
  height:58px;
  border-radius:999px;
  background:rgba(255,255,255,.08);
  border:1px solid rgba(255,255,255,.12);
  overflow:hidden;
  user-select:none;
  touch-action:none;
}
.swipe-fill{
  position:absolute;
  inset:0 auto 0 0;
  width:0%;
  background:linear-gradient(135deg,#2d6bff,#00c6ff);
  transition:width .15s ease;
}
.swipe-label{
  position:absolute;
  inset:0;
  display:flex;
  align-items:center;
  justify-content:center;
  font-weight:900;
  letter-spacing:.3px;
  color:#fff;
  pointer-events:none;
}
.swipe-handle{
  position:absolute;
  top:4px;
  left:4px;
  width:50px;
  height:50px;
  border-radius:999px;
  background:#fff;
  color:#0b1220;
  display:flex;
  align-items:center;
  justify-content:center;
  font-size:24px;
  font-weight:900;
  box-shadow:var(--shadow);
  cursor:pointer;
  touch-action:none;
  transition:left .15s ease;
}
.swipe-track.ready .swipe-label{opacity:.75;}
.swipe-track.success .swipe-fill{width:100% !important;}
.swipe-track.success .swipe-label{content:"Confirmed";}
.swipe-hint{margin-top:8px;font-size:12px;color:var(--muted);text-align:center;}

@media (max-width: 760px){
  .quick-container{padding:8px;}
  .quick-title{font-size:18px;}
  .quick-main{justify-content:stretch;}
  .quick-workspace{width:100%;}
  .quick-panel{padding:12px;}
  .quick-header h2{font-size:21px;}
  .quick-form-grid{grid-template-columns:1fr;gap:8px;}
  .quick-field.full{grid-column:auto;}
  .quick-field input{height:50px;font-size:16px;}
}
"""

ICON_DB = {
    "data": """<svg viewBox="0 0 24 24"><path d="M4 6c0-2.2 3.6-4 8-4s8 1.8 8 4-3.6 4-8 4-8-1.8-8-4Zm0 6c0 2.2 3.6 4 8 4s8-1.8 8-4v-2c-1.8 1.5-5 2.5-8 2.5S5.8 11.5 4 10v2Zm0 6c0 2.2 3.6 4 8 4s8-1.8 8-4v-2c-1.8 1.5-5 2.5-8 2.5S5.8 17.5 4 16v2Z"/></svg>""",
    "import": """<svg viewBox="0 0 24 24"><path d="M12 3v10.2l3.6-3.6L17 11l-5 5-5-5 1.4-1.4L11 13.2V3h1ZM5 19h14v2H5v-2Z"/></svg>""",
    "export": """<svg viewBox="0 0 24 24"><path d="M12 21V10.8l-3.6 3.6L7 13l5-5 5 5-1.4 1.4L13 10.8V21h-1ZM5 3h14v2H5V3Z"/></svg>""",
    "logs": """<svg viewBox="0 0 24 24"><path d="M6 2h9l5 5v15a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Zm8 1.5V8h4.5L14 3.5ZM7 12h10v2H7v-2Zm0 4h10v2H7v-2Zm0-8h6v2H7V8Z"/></svg>""",
    "home": """<svg viewBox="0 0 24 24"><path d="M12 3 2 12h3v9h6v-6h2v6h6v-9h3L12 3Z"/></svg>""",
    "in": """<svg viewBox="0 0 24 24"><path d="M12 3v10.2l3.6-3.6L17 11l-5 5-5-5 1.4-1.4L11 13.2V3h1ZM5 19h14v2H5v-2Z"/></svg>""",
    "out": """<svg viewBox="0 0 24 24"><path d="M12 21V10.8l-3.6 3.6L7 13l5-5 5 5-1.4 1.4L13 10.8V21h-1ZM5 3h14v2H5V3Z"/></svg>""",
    "change": """<svg viewBox="0 0 24 24"><path d="M12 6V3L8 7l4 4V8c2.8 0 5 2.2 5 5 0 .9-.2 1.7-.6 2.4l1.5 1.5c.7-1.1 1.1-2.5 1.1-3.9 0-3.9-3.1-7-7-7Zm-5.9.1C5.4 7.2 5 8.5 5 10c0 3.9 3.1 7 7 7v3l4-4-4-4v3c-2.8 0-5-2.2-5-5 0-.9.2-1.7.6-2.4L6.1 6.1Z"/></svg>""",
    "inv": """<svg viewBox="0 0 24 24"><path d="M3 3h18v6H3V3Zm0 8h18v10H3V11Zm4 2v6h2v-6H7Zm4 0v6h2v-6h-2Zm4 0v6h2v-6h-2Z"/></svg>""",
    "search": """<svg viewBox="0 0 24 24"><path d="M10 2a8 8 0 1 1 0 16 8 8 0 0 1 0-16Zm0 2a6 6 0 1 0 0 12 6 6 0 0 0 0-12Zm11 15-4.35-4.35-1.4 1.4L19.6 20.4 21 19Z"/></svg>""",
}


def page_shell(title: str, body_html: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="theme-color" content="#0b1220" />
  <meta name="mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <title>{esc(APP_TITLE)} - {esc(title)}</title>
  <style>{THEME_CSS}</style>
</head>
<body>
  <div class="container">
    <div class="topbar">
      <div class="brand">
        <div class="badge"></div>
        <div>
          <h1>{esc(APP_TITLE)}</h1>
          <div class="sub">{esc(title)}</div>
        </div>
      </div>
      <div class="row">
 <a class="btn nav-btn" href="/ui">{ICON_DB['home']}</a>
 <a class="btn nav-btn" href="/ui/data">Data</a>
 <a class="btn nav-btn" href="/docs">API</a>
</div>
    </div>
    {body_html}
  </div>
</body>
</html>"""


def quick_page_shell(title: str, body_html: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="theme-color" content="#0b1220" />
  <meta name="mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <title>{esc(APP_TITLE)} - {esc(title)}</title>
  <style>{THEME_CSS}</style>
</head>
<body class="quick-body">
  <div class="quick-container">
    <div class="quick-topbar">
      <div class="quick-brand">
        <div class="quick-badge"></div>
        <div>
          <h1 class="quick-title">{esc(APP_TITLE)}</h1>
          <div class="quick-sub">{esc(title)}</div>
        </div>
      </div>
      <div class="quick-nav">
        <a class="btn nav-btn" href="/ui">{ICON_DB['home']}</a>
        <a class="btn" href="/ui/data">Data</a>
        <a class="btn" href="/ui/api">API</a>
      </div>
    </div>
    <div class="quick-main">
      <div class="quick-workspace">{body_html}</div>
    </div>
  </div>
</body>
</html>"""


def tile(href: str, color_class: str, icon_key: str, label: str, hint: str) -> str:
    return f"""
    <a class="tile {esc(color_class)}" href="{esc(href)}">
      <div class="iconWrap">{ICON_DB[icon_key]}</div>
      <div>
        <div class="label">{esc(label)}</div>
        <div class="hint">{esc(hint)}</div>
      </div>
    </a>
    """


HOME_BODY = f"""
<div class="grid" style="grid-template-columns:repeat(2,minmax(0,1fr));">
  {tile('/ui/in', 'c-green', 'in', 'IN', 'Add stock')}
  {tile('/ui/out', 'c-red', 'out', 'OUT', 'Remove full pallet qty')}
  {tile('/ui/search', 'c-blue', 'search', 'SEARCH', 'Find all pallet locations for a product')}
  {tile('/ui/change', 'c-purple', 'change', 'CHANGE', 'Set new qty')}
</div>
"""


DATA_MENU_BODY = f"""
<div class="grid" style="grid-template-columns:repeat(2,minmax(0,1fr));">
  {tile('/ui/import', 'c-purple', 'import', 'IMPORT', 'Upload stock CSV files')}
  {tile('/ui/export', 'c-green', 'export', 'EXPORT', 'Download CSV files')}
  {tile('/ui/logs', 'c-orange', 'logs', 'LOGS', 'Inbound + Removed history')}
  {tile('/ui/info', 'c-blue', 'data', 'INFO', 'Ownership and usage notice')}
</div>
<div style="height:14px"></div>
<div class="panel" style="height:100%;display:flex;flex-direction:column;min-height:0;">
  <div class="small">
    Data contains import, export, logs and info tools.<br>
    Stock actions remain on the Home screen.
  </div>
</div>
"""


IMPORT_BODY = r"""
<div class="quick-panel">
  <div class="quick-header">
    <div>
      <h2 style="margin:0 0 4px 0;">Import</h2>
      <div class="quick-help">Upload a CSV to load starting stock or stock movements.</div>
    </div>
    <div class="iconWrap" style="width:60px;height:60px;border-radius:18px;flex:0 0 auto;">""" + ICON_DB["import"] + r"""</div>
  </div>

  <div style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:14px;max-width:760px;">
    <div style="font-size:18px;font-weight:900;">Stock Import</div>
    <div class="quick-help">CSV format: Product Code,Location,QTY or product,action,location,qty</div>
    <div style="height:12px"></div>
    <input type="file" id="importFile" accept=".csv" />
    <div style="height:10px"></div>
    <button class="btn" type="button" onclick="runImport()">Run Import</button>
  </div>
</div>

<script>
async function runImport(){
  const fileInput = document.getElementById("importFile");
  const file = fileInput && fileInput.files ? fileInput.files[0] : null;
  if(!file){
    alert("Choose CSV first");
    return;
  }

  const fd = new FormData();
  fd.append("file", file);

  try{
    const res = await fetch("/import/initial", {method:"POST", body:fd});
    const ct = res.headers.get("content-type") || "";

    if(ct.includes("text/csv")){
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      window.open(url);
      alert("Import completed but some rows failed. Error CSV downloaded.");
    }else{
      const data = await res.json();
      if(data.ok){
        alert("Import complete. Rows inserted: " + data.inserted);
      }else{
        alert(data.message || "Import failed.");
      }
    }
  }catch(e){
    alert("Import failed.");
  }
}
</script>
"""


EXPORT_BODY = r"""
<div class="quick-panel">
  <div class="quick-header">
    <div>
      <h2 style="margin:0 0 4px 0;">Export</h2>
      <div class="quick-help">Download current inventory and log CSV files.</div>
    </div>
    <div class="iconWrap" style="width:60px;height:60px;border-radius:18px;flex:0 0 auto;">""" + ICON_DB["export"] + r"""</div>
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;max-width:900px;">
    <button class="btn" type="button" onclick="window.open('/export/inventory.csv')">Export Inventory</button>
    <button class="btn" type="button" onclick="window.open('/export/inbound.csv')">Export Inbound Log</button>
    <button class="btn" type="button" onclick="window.open('/export/removed.csv')">Export Removed Log</button>
    <button class="btn" type="button" onclick="window.open('/template/moves.csv')">Download Template</button>
  </div>
</div>
"""


INFO_BODY = r"""
<div class="quick-panel">
  <div class="quick-header">
    <div>
      <h2 style="margin:0 0 4px 0;">Info</h2>
      <div class="quick-help">Ownership and internal-use notice.</div>
    </div>
    <div class="iconWrap" style="width:60px;height:60px;border-radius:18px;flex:0 0 auto;">""" + ICON_DB["data"] + r"""</div>
  </div>

  <div style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:18px;max-width:820px;">
    <div style="font-size:20px;font-weight:900;margin-bottom:10px;">Warehouse Fixer</div>
    <div class="quick-help" style="font-size:14px;line-height:1.7;color:var(--text);">
      This system is the property of Carl Black.<br><br>
      Warehouse Fixer

This software is the sole property of Carl Black.

Warehouse Fixer is a privately developed system and is not owned by, affiliated with, or part of any employer, client, or associated business unless explicitly agreed in writing.

All intellectual property rights, including but not limited to the system design, workflows, logic, interface, and data structures, are owned exclusively by Carl Black.

Use of this system is permitted for internal operational purposes only under authorization from the owner. No rights, ownership, or claims to the system are transferred through its use.

Unauthorized access, copying, modification, distribution, reverse engineering, or commercial use of this system, in whole or in part, is strictly prohibited.

All rights reserved.
    </div>
  </div>
</div>
"""


def quick_form_page(title: str, icon_key: str, show_qty: bool, qty_label: str, submit_url: str, note_text: str, selection_mode: str = "manual") -> str:
    if selection_mode == "manual":
        product_block = """
    <div class="quick-field">
      <label>Product Code</label>
      <input id="product" type="text" placeholder="Type product code" autocomplete="off" />
    </div>

    <div class="quick-field">
      <label>Location</label>
      <input id="location" type="text" placeholder="e.g. A01-1L" autocomplete="off" />
    </div>
        """
    else:
        product_block = """
    <input id="product" type="hidden" />
    <input id="location" type="hidden" />
    <input id="locationPick" type="hidden" />

    <div class="quick-field full">
      <label>Product Code</label>
      <input id="productSearch" type="text" placeholder="Type product code" autocomplete="off" />
      <div id="searchStatus" class="status-msg"></div>
    </div>

    <div class="quick-field full">
      <label>Select Pallet / Location</label>
      <div id="locationList" class="list-box">
        <div class="list-placeholder">Enter a product code to load pallet locations</div>
      </div>
    </div>

    <div class="quick-field full">
      <label>Selected</label>
      <input id="selectionSummary" type="text" placeholder="No pallet selected yet" readonly />
    </div>

    <div class="quick-field full" id="currentQtyWrap" style="display:none;">
      <label>Current Qty At Selected Pallet</label>
      <input id="currentQty" type="text" placeholder="0" readonly />
    </div>
        """

    qty_block = ""
    if show_qty:
        qty_block = f"""
    <div class="quick-field{' full' if selection_mode == 'pallet_select' else ''}">
      <label>{esc(qty_label)}</label>
      <input id="qty" type="number" inputmode="numeric" placeholder="Enter qty" />
    </div>
        """

    return f"""
<div class="quick-panel">
  <div class="quick-header">
    <div>
      <h2>{esc(title)}</h2>
      <div class="quick-help">{esc(note_text)}</div>
    </div>
    <div class="iconWrap" style="width:60px;height:60px;border-radius:18px;flex:0 0 auto;">{ICON_DB[icon_key]}</div>
  </div>

  <form id="quickForm" class="quick-form-grid" onsubmit="return false;">
    {product_block}
    {qty_block}
  </form>

  <div class="swipe-wrap">
    <div id="swipeTrack" class="swipe-track">
      <div id="swipeFill" class="swipe-fill"></div>
      <div id="swipeLabel" class="swipe-label">Swipe to confirm</div>
      <div id="swipeHandle" class="swipe-handle">›</div>
    </div>
    <div class="swipe-hint">Swipe all the way right to submit this change.</div>
  </div>
</div>

<div id="resultModal" class="quickModal" onclick="modalBackdrop(event)">
  <div class="quickModalBox">
    <div id="resultModalTitle" class="quickModalTitle">Result</div>
    <div id="resultModalBody" class="quickModalBody">Ready.</div>
    <div style="height:12px"></div>
    <div class="row" style="justify-content:flex-end;">
      <button class="btn" type="button" onclick="closeModal()">OK</button>
    </div>
  </div>
</div>

<script>
const QUICK_SUBMIT_URL = {submit_url!r};
const QUICK_REQUIRE_QTY = {str(show_qty).lower()};
const QUICK_SELECTION_MODE = {selection_mode!r};

let modalOnClose = null;
let isSubmitting = false;
let searchTimer = null;
let activeSearchToken = 0;
let selectedLocationValue = "";
let selectedLocationQty = "";

function val(id) {{
  const el = document.getElementById(id);
  return el ? (el.value || "").trim() : "";
}}

function setText(id, value) {{
  const el = document.getElementById(id);
  if (el) el.value = value || "";
}}

function showStatus(message) {{
  const el = document.getElementById("searchStatus");
  if (el) el.textContent = message || "";
}}

function clearLocationList(message) {{
  const list = document.getElementById("locationList");
  if (!list) return;
  list.innerHTML = '<div class="list-placeholder">' + (message || "No locations found") + '</div>';
}}

function updateSelectionSummary() {{
  const summary = document.getElementById("selectionSummary");
  if (!summary) return;

  if (!val("product") || !selectedLocationValue) {{
    summary.value = "No pallet selected yet";
    return;
  }}

  summary.value = selectedLocationQty !== ""
    ? `${{val("product")}} @ ${{selectedLocationValue}} (Current: ${{selectedLocationQty}})`
    : `${{val("product")}} @ ${{selectedLocationValue}}`;
}}

function resetPalletSelection(message) {{
  selectedLocationValue = "";
  selectedLocationQty = "";
  setText("location", "");
  setText("locationPick", "");
  setText("currentQty", "");
  const wrap = document.getElementById("currentQtyWrap");
  if (wrap) wrap.style.display = "none";
  clearLocationList(message || "Enter a product code to load pallet locations");
  updateSelectionSummary();
}}

function renderPallets(product, items) {{
  const list = document.getElementById("locationList");
  if (!list) return;

  list.innerHTML = "";

  if (!items.length) {{
    clearLocationList("No stock found for that product");
    return;
  }}

  items.forEach((item) => {{
    const row = document.createElement("div");
    row.className = "list-item";
    row.textContent = `${{item.location}} (Qty: ${{item.qty}})`;

    row.addEventListener("click", () => {{
      document.querySelectorAll("#locationList .list-item").forEach((el) => el.classList.remove("selected"));
      row.classList.add("selected");
      selectedLocationValue = item.location || "";
      selectedLocationQty = String(item.qty ?? "");
      setText("product", product);
      setText("location", selectedLocationValue);
      setText("locationPick", selectedLocationValue);
      setText("currentQty", selectedLocationQty);

      const wrap = document.getElementById("currentQtyWrap");
      if (wrap) wrap.style.display = "block";

      if (QUICK_REQUIRE_QTY) {{
        const qtyInput = document.getElementById("qty");
        if (qtyInput && !qtyInput.value) qtyInput.value = selectedLocationQty;
      }}

      updateSelectionSummary();

      const qtyInput = document.getElementById("qty");
      if (qtyInput && QUICK_REQUIRE_QTY) qtyInput.focus();
    }});

    list.appendChild(row);
  }});
}}

async function loadPalletsForProduct(product) {{
  const token = ++activeSearchToken;
  resetPalletSelection("Loading pallet locations...");
  showStatus("Searching...");

  try {{
    const res = await fetch("/lookup/pallets/" + encodeURIComponent(product));
    const data = await res.json();

    if (token !== activeSearchToken) return;

    const items = data && data.items ? data.items : [];
    setText("product", product);

    if (!items.length) {{
      showStatus("No stock found for that product");
      clearLocationList("No stock found for that product");
      return;
    }}

    showStatus(`${{items.length}} pallet location${{items.length === 1 ? "" : "s"}} found`);
    renderPallets(product, items);
  }} catch (e) {{
    if (token !== activeSearchToken) return;
    showStatus("Search failed");
    clearLocationList("Search failed");
  }}
}}

async function searchProductsNow() {{
  const q = val("productSearch");
  setText("product", "");
  activeSearchToken++;
  resetPalletSelection("Enter a product code to load pallet locations");

  if (!q) {{
    showStatus("");
    return;
  }}

  try {{
    const res = await fetch("/lookup/products?q=" + encodeURIComponent(q));
    const data = await res.json();
    const items = data && data.items ? data.items : [];

    const exact = items.find((item) => (item.product || "").trim().toUpperCase() === q.toUpperCase());

    if (exact) {{
      await loadPalletsForProduct(exact.product || q);
      return;
    }}

    if (items.length === 1) {{
      await loadPalletsForProduct(items[0].product || q);
      return;
    }}

    if (items.length > 1) {{
      showStatus("Multiple products found, keep typing");
      clearLocationList("Multiple products found, keep typing");
      return;
    }}

    showStatus("No matching products");
    clearLocationList("No matching products");
  }} catch (e) {{
    showStatus("Search failed");
    clearLocationList("Search failed");
  }}
}}

function queueSearch() {{
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(searchProductsNow, 220);
}}

function clearForm() {{
  ["product", "location", "qty", "productSearch", "currentQty", "selectionSummary", "locationPick"].forEach((id) => {{
    const el = document.getElementById(id);
    if (el) el.value = "";
  }});
  showStatus("");
  if (QUICK_SELECTION_MODE === "pallet_select") {{
    resetPalletSelection("Enter a product code to load pallet locations");
  }}
  resetSwipe();
}}

function focusPrimaryField() {{
  const el = document.getElementById(QUICK_SELECTION_MODE === "pallet_select" ? "productSearch" : "product");
  if (el) el.focus();
}}

function formatMessage(value) {{
  if (typeof value === "string") {{
    try {{
      return JSON.stringify(JSON.parse(value), null, 2);
    }} catch (_) {{
      return value;
    }}
  }}
  try {{
    return JSON.stringify(value, null, 2);
  }} catch (_) {{
    return String(value);
  }}
}}

function showModal(title, text, onClose) {{
  document.getElementById("resultModalTitle").textContent = title;
  document.getElementById("resultModalBody").textContent = formatMessage(text);
  document.getElementById("resultModal").classList.add("show");
  modalOnClose = onClose || null;
}}

function closeModal() {{
  document.getElementById("resultModal").classList.remove("show");
  if (typeof modalOnClose === "function") {{
    const fn = modalOnClose;
    modalOnClose = null;
    fn();
  }}
}}

function modalBackdrop(event) {{
  if (event.target && event.target.id === "resultModal") closeModal();
}}

async function submitQuickForm() {{
  if (isSubmitting) return;

  const payload = {{
    product: val("product"),
    location: val("location")
  }};

  if (!payload.product || !payload.location) {{
    showModal("Missing info", QUICK_SELECTION_MODE === "pallet_select" ? "Enter a product code and select a pallet/location first." : "Product and location are required.");
    resetSwipe();
    return;
  }}

  if (QUICK_REQUIRE_QTY) {{
    payload.qty = val("qty");
    if (payload.qty === "") {{
      showModal("Missing qty", "Enter a qty first.");
      resetSwipe();
      return;
    }}
  }}

  try {{
    isSubmitting = true;
    const res = await fetch(QUICK_SUBMIT_URL, {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(payload)
    }});

    const contentType = res.headers.get("content-type") || "";
    let data;

    if (contentType.includes("application/json")) {{
      data = await res.json();
    }} else {{
      const text = await res.text();
      try {{
        data = JSON.parse(text);
      }} catch (_) {{
        data = {{ detail: text }};
      }}
    }}

    if (!res.ok) {{
      throw new Error(data && data.detail ? data.detail : "Request failed");
    }}

    markSwipeSuccess();
showModal(
  "Stock Added",
  `Product: ${{data.product}}
Location: ${{data.location}}
Added: ${{data.delta}}
New Total: ${{data.after}}`,
  () => {{
    clearForm();
    focusPrimaryField();
  }}
);
}} catch (err) {{
  const message = err && err.message ? err.message : "Request failed";
  showModal("Error", message, () => {{
    focusPrimaryField();
  }});
  resetSwipe();
}} finally {{
  isSubmitting = false;
}}
}}

let swipeDragging = false;
let swipeStartX = 0;
let swipeCurrentX = 0;

function getSwipeEls() {{
  return {{
    track: document.getElementById("swipeTrack"),
    fill: document.getElementById("swipeFill"),
    handle: document.getElementById("swipeHandle"),
    label: document.getElementById("swipeLabel")
  }};
}}

function setSwipePosition(px) {{
  const {{ track, fill, handle }} = getSwipeEls();
  if (!track || !fill || !handle) return;
  const max = Math.max(0, track.clientWidth - handle.clientWidth - 8);
  const clamped = Math.max(0, Math.min(px, max));
  handle.style.left = (4 + clamped) + "px";
  const pct = max > 0 ? ((clamped + handle.clientWidth / 2) / track.clientWidth) * 100 : 0;
  fill.style.width = pct + "%";
}}

function resetSwipe() {{
  const {{ track, fill, handle, label }} = getSwipeEls();
  if (!track || !fill || !handle || !label) return;
  track.classList.remove("success");
  label.textContent = "Swipe to confirm";
  handle.style.left = "4px";
  fill.style.width = "0%";
}}

function markSwipeSuccess() {{
  const {{ track, fill, handle, label }} = getSwipeEls();
  if (!track || !fill || !handle || !label) return;
  track.classList.add("success");
  label.textContent = "Confirmed";
  const max = Math.max(0, track.clientWidth - handle.clientWidth - 8);
  handle.style.left = (4 + max) + "px";
  fill.style.width = "100%";
}}

function pointerX(event) {{
  if (event.touches && event.touches.length) return event.touches[0].clientX;
  if (event.changedTouches && event.changedTouches.length) return event.changedTouches[0].clientX;
  return event.clientX;
}}

function startSwipe(event) {{
  if (isSubmitting) return;
  swipeDragging = true;
  swipeStartX = pointerX(event);
  const handle = document.getElementById("swipeHandle");
  swipeCurrentX = parseFloat((handle && handle.style.left || "4").replace("px","")) - 4;
  event.preventDefault();
}}

function moveSwipe(event) {{
  if (!swipeDragging) return;
  const current = pointerX(event);
  const delta = current - swipeStartX;
  setSwipePosition(swipeCurrentX + delta);
  event.preventDefault();
}}

async function endSwipe() {{
  if (!swipeDragging) return;
  swipeDragging = false;
  const {{ track, handle }} = getSwipeEls();
  if (!track || !handle) return;

  const max = Math.max(0, track.clientWidth - handle.clientWidth - 8);
  const current = parseFloat((handle.style.left || "4").replace("px","")) - 4;
  const pct = max > 0 ? current / max : 0;

  if (pct >= 0.82) {{
    await submitQuickForm();
  }} else {{
    resetSwipe();
  }}
}}

window.addEventListener("load", () => {{
  const form = document.getElementById("quickForm");
  const search = document.getElementById("productSearch");
  const handle = document.getElementById("swipeHandle");
  const track = document.getElementById("swipeTrack");

  if (form) {{
    form.addEventListener("keydown", (event) => {{
      if (event.key === "Enter") {{
        event.preventDefault();
      }}
    }});
  }}

  if (search) {{
    search.addEventListener("input", queueSearch);
  }}

  if (handle) {{
    handle.addEventListener("mousedown", startSwipe);
    handle.addEventListener("touchstart", startSwipe, {{ passive: false }});
  }}

  if (track) {{
    track.addEventListener("mousedown", startSwipe);
    track.addEventListener("touchstart", startSwipe, {{ passive: false }});
  }}

  window.addEventListener("mousemove", moveSwipe);
  window.addEventListener("touchmove", moveSwipe, {{ passive: false }});
  window.addEventListener("mouseup", endSwipe);
  window.addEventListener("touchend", endSwipe);

  resetSwipe();
  if (QUICK_SELECTION_MODE === "pallet_select") {{
    resetPalletSelection("Enter a product code to load pallet locations");
    updateSelectionSummary();
  }}
  focusPrimaryField();
}});
</script>
"""


QUICK_IN_BODY = quick_form_page(
    title="IN",
    icon_key="in",
    show_qty=True,
    qty_label="Qty",
    submit_url="/quick/in",
    note_text="Add stock to a product and location.",
    selection_mode="manual",
)

QUICK_OUT_BODY = quick_form_page(
    title="OUT",
    icon_key="out",
    show_qty=False,
    qty_label="",
    submit_url="/quick/out",
    note_text="Enter the product code, pick the pallet, then swipe to remove the full pallet qty.",
    selection_mode="pallet_select",
)

QUICK_CHANGE_BODY = quick_form_page(
    title="CHANGE",
    icon_key="change",
    show_qty=True,
    qty_label="New Qty",
    submit_url="/quick/change",
    note_text="Enter the product code, pick the pallet, set the new qty, then swipe to confirm.",
    selection_mode="pallet_select",
)


SEARCH_BODY = r"""
<div class="quick-panel">
  <div class="quick-header">
    <div>
      <h2 style="margin:0 0 4px 0;">Search</h2>
      <div class="quick-help">Enter a product code to list all pallet locations with stock.</div>
    </div>
    <div class="iconWrap" style="width:60px;height:60px;border-radius:18px;flex:0 0 auto;">""" + ICON_DB["search"] + r"""</div>
  </div>

  <div class="quick-form-grid">
    <div class="quick-field full">
      <label>Product Code</label>
      <input id="searchProduct" type="text" placeholder="Type product code" autocomplete="off" />
      <div id="searchMsg" class="status-msg"></div>
    </div>

    <div class="quick-field full">
      <label>Pallet Locations</label>
      <div id="searchResults" class="list-box">
        <div class="list-placeholder">Enter a product code to search</div>
      </div>
    </div>
  </div>
</div>

<script>
let searchTimer = null;
let searchToken = 0;

function searchClear(msg){
  const box = document.getElementById("searchResults");
  if (box) box.innerHTML = '<div class="list-placeholder">' + msg + '</div>';
}

function setSearchMsg(msg){
  const el = document.getElementById("searchMsg");
  if (el) el.textContent = msg || "";
}

function renderSearchResults(product, items){
  const box = document.getElementById("searchResults");
  if (!box) return;
  box.innerHTML = "";
  if (!items.length){
    searchClear("No stock found for that product");
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "list-item";
    row.textContent = `${product} — ${item.location} (Qty: ${item.qty})`;
    box.appendChild(row);
  });
}

async function runSearchNow(){
  const q = (document.getElementById("searchProduct").value || "").trim();
  searchToken++;
  const token = searchToken;

  if (!q){
    setSearchMsg("");
    searchClear("Enter a product code to search");
    return;
  }

  setSearchMsg("Searching...");
  searchClear("Searching...");

  try{
    const prodRes = await fetch("/lookup/products?q=" + encodeURIComponent(q));
    const prodData = await prodRes.json();
    const items = prodData && prodData.items ? prodData.items : [];
    const exact = items.find((item) => (item.product || "").trim().toUpperCase() === q.toUpperCase());
    const product = exact ? exact.product : (items.length === 1 ? items[0].product : "");

    if (token !== searchToken) return;

    if (!product){
      if (items.length > 1){
        setSearchMsg("Multiple products found, keep typing");
        searchClear("Multiple products found, keep typing");
      } else {
        setSearchMsg("No matching products");
        searchClear("No matching products");
      }
      return;
    }

    const res = await fetch("/lookup/pallets/" + encodeURIComponent(product));
    const data = await res.json();
    if (token !== searchToken) return;

    const pallets = data && data.items ? data.items : [];
    setSearchMsg(`${pallets.length} pallet location${pallets.length === 1 ? "" : "s"} found`);
    renderSearchResults(product, pallets);
  }catch(e){
    if (token !== searchToken) return;
    setSearchMsg("Search failed");
    searchClear("Search failed");
  }
}

function queueSearch(){
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearchNow, 220);
}

window.addEventListener("load", () => {
  const input = document.getElementById("searchProduct");
  if (input){
    input.addEventListener("input", queueSearch);
    input.focus();
  }
});
</script>
"""


def inventory_table_html(items: List[Dict[str, Any]]) -> str:
    rows = "\n".join(
        f"<tr><td>{esc(item['product'])}</td><td>{esc(item['location'])}</td><td>{esc(item['qty'])}</td></tr>"
        for item in items
    )
    return f"""
<table class="table">
  <thead><tr><th>PRODUCT</th><th>LOCATION</th><th>QTY</th></tr></thead>
  <tbody>
    {rows if rows else '<tr><td colspan="3">No stock records.</td></tr>'}
  </tbody>
</table>
"""


LOGS_PAGE_BODY = r"""
<div class="quick-panel">
  <div class="quick-header">
    <div>
      <h2 style="margin:0 0 4px 0;">Logs</h2>
      <div class="quick-help">View the latest inbound and removed history.</div>
    </div>
    <div class="iconWrap" style="width:60px;height:60px;border-radius:18px;flex:0 0 auto;">""" + ICON_DB["logs"] + r"""</div>
  </div>

  <div class="row" style="margin-bottom:12px;">
    <button class="btn" type="button" onclick="loadInbound()">Inbound (latest 50)</button>
    <button class="btn" type="button" onclick="loadRemoved()">Removed (latest 50)</button>
    <button class="btn" type="button" onclick="window.open('/export/inbound.csv')">Export Inbound CSV</button>
    <button class="btn" type="button" onclick="window.open('/export/removed.csv')">Export Removed CSV</button>
  </div>

  <pre id="logOut" style="flex:1 1 auto;min-height:0;margin:0;">Tap a button to load logs.</pre>
</div>

<script>
async function loadInbound(){
  const out = document.getElementById("logOut");
  out.textContent = "Loading inbound log...";
  try{
    const res = await fetch("/ui/logs/inbound");
    out.textContent = await res.text();
  }catch(e){
    out.textContent = "Load failed: " + e;
  }
}
async function loadRemoved(){
  const out = document.getElementById("logOut");
  out.textContent = "Loading removed log...";
  try{
    const res = await fetch("/ui/logs/removed");
    out.textContent = await res.text();
  }catch(e){
    out.textContent = "Load failed: " + e;
  }
}
</script>
"""


# -------------------------
# Initial CSV Import
# Supports:
# 1) Product Code,Location,QTY
# 2) product,action,location,qty
# -------------------------
def run_initial_import(file_bytes: bytes) -> Dict[str, Any]:
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        return {"error": "CSV has no header row"}

    headers = [h.strip() for h in reader.fieldnames if h]
    old_headers = ["Product Code", "Location", "QTY"]
    new_headers = ["product", "action", "location", "qty"]

    errors: List[List[str]] = []
    inserted = 0

    conn = get_conn()
    cur = conn.cursor()

    def current_qty(product: str, location: str) -> int:
        row = cur.execute(
            """
            SELECT COALESCE(SUM(qty), 0)
            FROM inbound_log
            WHERE UPPER(TRIM(product)) = UPPER(TRIM(?))
              AND UPPER(TRIM(location)) = UPPER(TRIM(?))
            """,
            (product, location),
        ).fetchone()
        return int(row[0] or 0)

    if headers == old_headers:
        seen = set()
        for r in reader:
            prod = norm(r.get("Product Code"))
            loc = norm(r.get("Location"))
            qty_raw = norm(r.get("QTY"))
            key = (prod.upper(), loc.upper())

            if not prod or not loc:
                errors.append([prod, "", loc, qty_raw, "Missing product or location"])
                continue
            if key in seen:
                errors.append([prod, "", loc, qty_raw, "Duplicate row in file"])
                continue
            seen.add(key)

            try:
                qty = int(float(qty_raw))
            except Exception:
                errors.append([prod, "", loc, qty_raw, "QTY not numeric"])
                continue

            cur.execute(
                """
                INSERT INTO inbound_log (datetime, product, qty, location, user, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now_iso(), prod, qty, loc, "IMPORT", "INITIAL IMPORT"),
            )
            inserted += 1

        conn.commit()
        conn.close()
        return {"inserted": inserted, "errors": errors}

    if headers == new_headers:
        for r in reader:
            prod = norm(r.get("product"))
            action = norm(r.get("action")).upper()
            loc = norm(r.get("location"))
            qty_raw = norm(r.get("qty"))

            if not prod or not loc:
                errors.append([prod, action, loc, qty_raw, "Missing product or location"])
                continue
            if action not in {"IN", "OUT", "CHANGE"}:
                errors.append([prod, action, loc, qty_raw, "Action must be IN, OUT, or CHANGE"])
                continue

            qty = None
            if qty_raw != "":
                try:
                    qty = int(float(qty_raw))
                except Exception:
                    errors.append([prod, action, loc, qty_raw, "QTY not numeric"])
                    continue

            before = current_qty(prod, loc)

            if action == "IN":
                if qty is None:
                    errors.append([prod, action, loc, qty_raw, "QTY required for IN"])
                    continue
                if qty == 0:
                    errors.append([prod, action, loc, qty_raw, "QTY cannot be 0 for IN"])
                    continue
                cur.execute(
                    """
                    INSERT INTO inbound_log (datetime, product, qty, location, user, notes)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (now_iso(), prod, qty, loc, "IMPORT", "CSV IN"),
                )
                inserted += 1

            elif action == "OUT":
                if before == 0:
                    errors.append([prod, action, loc, qty_raw, "No stock at this location to remove"])
                    continue
                cur.execute(
                    """
                    INSERT INTO inbound_log (datetime, product, qty, location, user, notes)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (now_iso(), prod, -before, loc, "IMPORT", "CSV OUT"),
                )
                inserted += 1

            else:
                if qty is None:
                    errors.append([prod, action, loc, qty_raw, "QTY required for CHANGE"])
                    continue
                if qty < 0:
                    errors.append([prod, action, loc, qty_raw, "QTY cannot be negative for CHANGE"])
                    continue
                delta = qty - before
                cur.execute(
                    """
                    INSERT INTO inbound_log (datetime, product, qty, location, user, notes)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (now_iso(), prod, delta, loc, "IMPORT", "CSV CHANGE"),
                )
                inserted += 1

        conn.commit()
        conn.close()
        return {"inserted": inserted, "errors": errors}

    conn.close()
    return {"error": "CSV headers must be exactly either: Product Code,Location,QTY OR product,action,location,qty"}


@app.post("/import/initial")
async def import_initial(file: UploadFile = File(...)) -> Any:
    raw = await file.read()
    result = run_initial_import(raw)

    if "error" in result:
        return {"ok": False, "message": result["error"]}

    errors = result["errors"]
    inserted = result["inserted"]

    if not errors:
        return {"ok": True, "inserted": inserted, "errors": 0}

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["product", "action", "location", "qty", "error"])
    for err in errors:
        writer.writerow(err)
    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="import_errors.csv"'},
    )


# -------------------------
# UI routes
# -------------------------
@app.get("/ui", response_class=HTMLResponse)
def ui_home() -> str:
    return "<h1>HELLO WORKING</h1>"


@app.get("/ui/search", response_class=HTMLResponse)
def ui_search() -> str:
    return quick_page_shell("Search", SEARCH_BODY)


@app.get("/ui/in", response_class=HTMLResponse)
def ui_quick_in() -> str:
    return quick_page_shell("IN", QUICK_IN_BODY)


@app.get("/ui/out", response_class=HTMLResponse)
def ui_quick_out() -> str:
    return quick_page_shell("OUT", QUICK_OUT_BODY)


@app.get("/ui/change", response_class=HTMLResponse)
def ui_quick_change() -> str:
    return quick_page_shell("CHANGE", QUICK_CHANGE_BODY)


@app.get("/ui/inventory", response_class=HTMLResponse)
def ui_inventory() -> str:
    return quick_page_shell("Data", DATA_MENU_BODY)


@app.get("/ui/data", response_class=HTMLResponse)
def ui_data() -> str:
    return quick_page_shell("Data", DATA_MENU_BODY)


@app.get("/ui/import", response_class=HTMLResponse)
def ui_import() -> str:
    return quick_page_shell("Import", IMPORT_BODY)


@app.get("/ui/export", response_class=HTMLResponse)
def ui_export() -> str:
    return quick_page_shell("Export", EXPORT_BODY)


@app.get("/ui/info", response_class=HTMLResponse)
def ui_info() -> str:
    return quick_page_shell("Info", INFO_BODY)


@app.get("/ui/logs", response_class=HTMLResponse)
def ui_logs() -> str:
    return quick_page_shell("Logs", LOGS_PAGE_BODY)


@app.get("/ui/logs/inbound", response_class=HTMLResponse)
def ui_logs_inbound() -> str:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, datetime, product, qty, location, user, COALESCE(notes,'') AS notes
        FROM inbound_log
        ORDER BY id DESC
        LIMIT 50
        """
    ).fetchall()
    conn.close()

    lines = ["id | datetime | product | qty | location | user | notes"]
    for r in rows:
        lines.append(
            f'{r["id"]} | {r["datetime"]} | {r["product"]} | {r["qty"]} | {r["location"]} | {r["user"]} | {r["notes"]}'
        )
    return "\n".join(lines)


@app.get("/ui/logs/removed", response_class=HTMLResponse)
def ui_logs_removed() -> str:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, removed_at, removed_by, original_product, original_location, removed_qty, COALESCE(reason,'') AS reason
        FROM removed_log
        ORDER BY id DESC
        LIMIT 50
        """
    ).fetchall()
    conn.close()

    lines = ["id | removed_at | removed_by | product | location | removed_qty | reason"]
    for r in rows:
        lines.append(
            f'{r["id"]} | {r["removed_at"]} | {r["removed_by"]} | {r["original_product"]} | {r["original_location"]} | {r["removed_qty"]} | {r["reason"]}'
        )
    return "\n".join(lines)


@app.get("/template/moves.csv")
def download_moves_template() -> StreamingResponse:
    rows = [
        {"product": "2400", "action": "IN", "location": "A11-7L", "qty": "96"},
        {"product": "2400", "action": "OUT", "location": "A11-7L", "qty": ""},
        {"product": "2400", "action": "CHANGE", "location": "A11-7L", "qty": "50"},
    ]
    return csv_response(rows, ["product", "action", "location", "qty"], "warehouse_moves_template.csv")
