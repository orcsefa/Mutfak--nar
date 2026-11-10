"""
Mutfak Stok Uygulaması — Flask + SQLite
Aile üyelerinin aynı verileri gördüğü, yöneticilerin stok rakamlarını değiştirdiği
bir stok takip uygulaması.
"""
import os
import json
import sqlite3
import urllib.request
from datetime import datetime
from functools import wraps

from flask import Flask, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("KITCHEN_DB", os.path.join(BASE_DIR, "kitchen.db"))

app = Flask(__name__, static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY", "mutfak-stok-gecici-anahtar-degistir")

# ---------------------------------------------------------------- DB

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    is_buyer INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'adet',
    quantity REAL NOT NULL DEFAULT 0,
    category TEXT DEFAULT '',
    expiry TEXT,
    threshold REAL DEFAULT 0,
    full_qty REAL DEFAULT 0,
    threshold_pct REAL DEFAULT 0,
    price REAL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    user_name TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    quantity REAL DEFAULT 0,
    ordered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    message TEXT NOT NULL,
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    read INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read INTEGER DEFAULT 0
);
"""


def init_db():
    d = sqlite3.connect(DB_PATH)
    d.executescript(SCHEMA)
    # migration: existing databases get the new columns
    cols = [r[1] for r in d.execute("PRAGMA table_info(items)").fetchall()]
    for col, ddl in (("image", "ALTER TABLE items ADD COLUMN image TEXT"),
                     ("full_qty", "ALTER TABLE items ADD COLUMN full_qty REAL DEFAULT 0"),
                     ("threshold_pct", "ALTER TABLE items ADD COLUMN threshold_pct REAL DEFAULT 0"),
                     ("price", "ALTER TABLE items ADD COLUMN price REAL DEFAULT 0")):
        if col not in cols:
            d.execute(ddl)
    # users table migration
    ucols = [r[1] for r in d.execute("PRAGMA table_info(users)").fetchall()]
    if "is_buyer" not in ucols:
        d.execute("ALTER TABLE users ADD COLUMN is_buyer INTEGER DEFAULT 0")
        if col not in cols:
            d.execute(ddl)
    row = d.execute("SELECT COUNT(*) FROM users").fetchone()
    if row[0] == 0:
        d.execute(
            "INSERT INTO users (username, name, password_hash, role, is_buyer, created_at) VALUES (?,?,?,?,?,?)",
            ("admin", "Yönetici", generate_password_hash("admin123"), "admin", 0,
             datetime.now().isoformat(timespec="seconds")),
        )
    d.commit()
    # Auto-restore from GitHub Gist (if configured)
    try:
        token = os.environ.get("GITHUB_TOKEN", "")
        gist_id = os.environ.get("BACKUP_GIST_ID", "")
        if token and gist_id:
            data = sync_from_gist(token, gist_id)
            if data:
                import_backup(d, data)
                print("[Backup] Veriler GitHub Gist'ten yüklendi")
    except Exception as e:
        print(f"[Backup] Auto-restore hatası: {e}")
    d.close()


# ---------------------------------------------------------------- Backup & Restore

BACKUP_TABLES = ["users", "items", "movements", "orders", "errors", "notifications"]

def export_backup(db):
    """Export all data as JSON string."""
    data = {}
    for table in BACKUP_TABLES:
        try:
            rows = db.execute(f"SELECT * FROM {table}").fetchall()
            data[table] = [dict(r) for r in rows]
        except Exception:
            data[table] = []
    return json.dumps(data, ensure_ascii=False, default=str)

def import_backup(db, data_str):
    """Import data from JSON string. Replaces all data."""
    data = json.loads(data_str)
    # Disable FK checks during import to avoid constraint errors
    db.execute("PRAGMA foreign_keys = OFF")
    # Delete in FK-safe order (children first)
    for table in reversed(BACKUP_TABLES):
        if table in data:
            db.execute(f"DELETE FROM {table}")
            for row in data[table]:
                cols = list(row.keys())
                placeholders = ",".join(["?"] * len(cols))
                sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
                db.execute(sql, [row[c] for c in cols])
    db.execute("PRAGMA foreign_keys = ON")
    db.commit()


def sync_to_gist(token, gist_id, data_str):
    """Upload backup to GitHub Gist via API."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/gists/{gist_id}",
            data=json.dumps({"files": {"backup.json": {"content": data_str}}}).encode("utf-8"),
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False

def sync_from_gist(token, gist_id):
    """Download backup from GitHub Gist via API."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/gists/{gist_id}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            gist = json.loads(resp.read().decode("utf-8"))
            for file in gist.get("files", []):
                if file["filename"] == "backup.json":
                    return file["content"]
    except Exception:
        pass
    return None

def auto_sync_backup(db):
    """Auto-sync backup to GitHub Gist (if configured). Non-blocking."""
    token = os.environ.get("GITHUB_TOKEN", "")
    gist_id = os.environ.get("BACKUP_GIST_ID", "")
    if not token or not gist_id:
        return False
    try:
        data = export_backup(db)
        return sync_to_gist(token, gist_id, data)
    except Exception:
        return False

def auto_restore_backup(db):
    """Auto-restore backup from GitHub Gist (if configured). Called on startup."""
    token = os.environ.get("GITHUB_TOKEN", "")
    gist_id = os.environ.get("BACKUP_GIST_ID", "")
    if not token or not gist_id:
        return False
    try:
        data = sync_from_gist(token, gist_id)
        if data:
            import_backup(db, data)
            return True
    except Exception:
        pass
    return False


def log_movement(item_id, user, amount, note=""):
    db = get_db()
    db.execute(
        "INSERT INTO movements (item_id, user_id, user_name, amount, note, created_at) VALUES (?,?,?,?,?,?)",
        (item_id, user["id"], user["name"], amount, note,
         datetime.now().isoformat(timespec="seconds")),
    )


def fmt_num(n):
    return float(round(float(n), 2))


def effective_threshold(item):
    """Active alarm level for an item: the highest of amount / percent
    thresholds. Returns (value, description) or (0, '')."""
    amount_th = float(item["threshold"] or 0)
    full_qty = float(item["full_qty"] or 0)
    pct = float(item["threshold_pct"] or 0)
    cands = []
    if amount_th > 0:
        cands.append((amount_th, f"eşik: {fmt_num(amount_th)}"))
    if full_qty > 0 and pct > 0:
        cands.append((full_qty * pct / 100, f"%{fmt_num(pct)} eşik: {fmt_num(full_qty)}"))
    if not cands:
        return 0, ""
    return max(cands, key=lambda x: x[0])


def notify_low_stock(item_id, new_qty, old_qty, force=False):
    """When an item crosses below its active threshold, notify every user once."""
    db = get_db()
    item = db.execute(
        "SELECT name, unit, threshold, full_qty, threshold_pct FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        return
    th, desc = effective_threshold(item)
    if new_qty <= 0 or new_qty > th:
        return
    if old_qty is not None and not force and old_qty <= th:
        return  # already below — no repeated spam
    msg = (f"{item['name']} stok miktarı {fmt_num(new_qty)} "
           f"{item['unit']}'a düştü ({desc})")
    now = datetime.now().isoformat(timespec="seconds")
    for u in db.execute("SELECT id FROM users").fetchall():
        db.execute(
            "INSERT INTO notifications (user_id, item_id, message, created_at, read) "
            "VALUES (?,?,?,?,0)",
            (u["id"], item_id, msg, now),
        )


def user_json(row):
    return {"id": row["id"], "username": row["username"],
            "name": row["name"], "role": row["role"],
            "is_buyer": bool(row["is_buyer"])}


HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mutfak Takip</title>
<style>
:root {
  --primary: #0e9f6e;
  --primary-dark: #0b7f57;
  --bg: #f4f6f5;
  --card: #ffffff;
  --text: #1f2933;
  --muted: #7b8794;
  --border: #e4e7eb;
  --danger: #e02424;
  --warn: #b7791f;
  --radius: 12px;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
}

.hidden { display: none !important; }

/* ---------------- Auth ---------------- */
.auth-screen {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: linear-gradient(160deg, #e8f5ef 0%, var(--bg) 55%);
}

.auth-card {
  width: 100%;
  max-width: 400px;
  background: var(--card);
  border-radius: var(--radius);
  box-shadow: 0 10px 30px rgba(16, 42, 32, .12);
  padding: 32px 28px;
}

.auth-logo { font-size: 44px; text-align: center; }
.auth-card h1 { text-align: center; font-size: 16px; margin-top: 4px; }
.subtitle { text-align: center; color: var(--muted); font-size: 14px; margin: 4px 0 20px; }

/* ---------- Modern butonlar ---------- */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 10px 18px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: #fff;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  color: var(--text);
  transition: all .18s ease;
  box-shadow: 0 1px 3px rgba(16, 42, 32, .06);
  white-space: nowrap;
  user-select: none;
}
.btn:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(16, 42, 32, .1);
  border-color: #d0d5dd;
}
.btn:active { transform: translateY(0); }

.btn-primary {
  background: linear-gradient(135deg, var(--primary), var(--primary-dark));
  border-color: transparent;
  color: #fff;
  box-shadow: 0 2px 8px rgba(14, 159, 110, .3);
}
.btn-primary:hover {
  box-shadow: 0 4px 16px rgba(14, 159, 110, .4);
}

.btn-block { width: 100%; margin-top: 6px; }

.btn-ghost {
  background: transparent;
  border-color: transparent;
  box-shadow: none;
  padding: 8px 12px;
}
.btn-ghost:hover {
  background: var(--bg);
  border-color: transparent;
  box-shadow: none;
  transform: none;
}

.btn-danger {
  background: #fff;
  border-color: #fcc;
  color: var(--danger);
}
.btn-danger:hover {
  background: var(--danger);
  color: #fff;
  border-color: var(--danger);
}

.btn-sm {
  padding: 6px 12px;
  font-size: 13px;
  border-radius: 8px;
}

.btn-outline {
  background: transparent;
  border-color: var(--primary);
  color: var(--primary-dark);
}
.btn-outline:hover {
  background: #e0f4ec;
  border-color: var(--primary);
  box-shadow: 0 2px 8px rgba(14, 159, 110, .12);
}

.btn-info {
  background: var(--bg);
  border-color: var(--border);
  color: var(--muted);
}
.btn-info:hover {
  background: #e0f4ec;
  border-color: var(--primary);
  color: var(--primary-dark);
  box-shadow: 0 2px 8px rgba(14, 159, 110, .12);
}

/* Icon butonlar — geri, 🔔, ⚠️ vb. */
.btn-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 8px 12px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  color: var(--text);
  transition: all .18s ease;
  white-space: nowrap;
  user-select: none;
}
.btn-icon:hover {
  background: var(--bg);
  border-color: var(--border);
}

/* Password button — subtle */
.btn-password {
  color: var(--muted);
}
.btn-password:hover {
  color: var(--text);
  background: var(--bg);
  border-color: var(--border);
}

/* Logout button — modern red box */
.btn-logout {
  background: linear-gradient(135deg, #e02424, #c01a1a);
  color: #fff;
  border-color: transparent;
  box-shadow: 0 2px 6px rgba(224, 36, 36, .25);
}
.btn-logout:hover {
  background: linear-gradient(135deg, #c01a1a, #a01515);
  box-shadow: 0 4px 12px rgba(224, 36, 36, .4);
  color: #fff;
}
.logout-icon {
  width: 18px;
  height: 18px;
  display: inline-block;
  vertical-align: middle;
  flex-shrink: 0;
}
.logout-icon svg {
  width: 18px;
  height: 18px;
  display: block;
}

/* Geri butonu — modern mavi kutu */
.btn-back {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  border: none;
  border-radius: 10px;
  background: linear-gradient(135deg, #2a5a8a, #1a4070);
  color: #fff;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: all .18s ease;
  box-shadow: 0 2px 6px rgba(42, 90, 138, .25);
}
.btn-back:hover {
  transform: translateX(-2px);
  box-shadow: 0 4px 12px rgba(42, 90, 138, .4);
  background: linear-gradient(135deg, #1a4070, #103050);
}

/* Auth tabs — segmented style */
.auth-tabs {
  display: flex;
  gap: 4px;
  padding: 4px;
  background: var(--bg);
  border-radius: 12px;
  margin-bottom: 18px;
}
.auth-tab {
  flex: 1;
  padding: 9px;
  border: none;
  background: transparent;
  border-radius: 8px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  color: var(--muted);
  transition: all .18s ease;
}
.auth-tab:hover { color: var(--text); }
.auth-tab.active {
  background: #fff;
  color: var(--primary-dark);
  box-shadow: 0 2px 8px rgba(16, 42, 32, .08);
  font-weight: 600;
}

label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 12px; }
input, select {
  width: 100%;
  margin-top: 4px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 15px;
  color: var(--text);
  background: #fff;
  transition: border-color .15s, box-shadow .15s;
}
input:focus, select:focus {
  outline: none;
  border-color: var(--primary);
  box-shadow: 0 0 0 3px rgba(14, 159, 110, .12);
}

.hint { font-size: 12px; color: var(--muted); margin-top: 10px; line-height: 1.5; }

/* ---------------- App ---------------- */
.app-screen { max-width: 1100px; margin: 0 auto; padding: 16px 20px 60px; }

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--card);
  border-radius: var(--radius);
  padding: 12px 18px;
  box-shadow: 0 2px 8px rgba(16, 42, 32, .06);
  margin-bottom: 16px;
}
.brand { font-size: 20px; font-weight: 700; display: flex; align-items: center; }
.brand-icon { width: 34px; height: 34px; margin-right: 6px; border-radius: 8px; object-fit: contain; }
.user-area { display: flex; align-items: center; gap: 12px; }
.user-info { display: flex; align-items: center; gap: 8px; font-size: 14px; }
.role-badge {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 999px;
  font-weight: 600;
}
.role-badge.admin { background: #e0f4ec; color: var(--primary-dark); }
.role-badge.viewer { background: #f0f1f3; color: var(--muted); }

.toolbar {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.toolbar .input { width: auto; margin-top: 0; min-width: 140px; flex: 1; }

.stats {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.stat {
  background: var(--card);
  border-radius: var(--radius);
  padding: 12px 20px;
  min-width: 130px;
  box-shadow: 0 2px 8px rgba(16, 42, 32, .06);
}
.stat b { display: block; font-size: 24px; }
.stat span { font-size: 13px; color: var(--muted); }
.stat.warn b { color: var(--warn); }

.items-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
  gap: 14px;
}
.item-card {
  background: var(--card);
  border-radius: var(--radius);
  padding: 16px;
  box-shadow: 0 2px 8px rgba(16, 42, 32, .06);
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-left: 5px solid transparent;
}
.item-card.low { border-left-color: var(--warn); }
.item-card.expired { border-left-color: var(--danger); }
.item-card.zero { border-left-color: var(--danger); }

.item-top { display: flex; justify-content: space-between; align-items: flex-start; }
.item-name { font-size: 17px; font-weight: 700; word-break: break-word; }
.item-cat { font-size: 12px; color: var(--muted); margin-top: 2px; }

.item-qty { font-size: 26px; font-weight: 700; color: var(--primary-dark); }
.item-qty small { font-size: 13px; color: var(--muted); font-weight: 400; margin-left: 4px; }

.badge {
  font-size: 11px;
  font-weight: 600;
  padding: 3px 8px;
  border-radius: 999px;
}
.badge.ok { background: #e0f4ec; color: var(--primary-dark); }
.badge.warn { background: #fdf3e0; color: var(--warn); }
.badge.expired { background: #fde8e8; color: var(--danger); }

.item-actions { display: flex; gap: 8px; margin-top: 6px; }
.item-actions .btn { flex: 1; }

.empty { text-align: center; color: var(--muted); padding: 60px 0; }

/* ---------------- Modal ---------------- */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 30, .45);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  z-index: 100;
}
.modal {
  background: #fff;
  border-radius: var(--radius);
  width: 100%;
  max-width: 460px;
  max-height: 85vh;
  overflow-y: auto;
  padding: 24px;
  box-shadow: 0 20px 50px rgba(0, 0, 0, .25);
}
.modal h2 { font-size: 19px; margin-bottom: 16px; }
.modal-close {
  float: right;
  border: none;
  background: none;
  font-size: 22px;
  cursor: pointer;
  color: var(--muted);
}

/* Kategori öneri çipleri — modal içinde */
.cat-field {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.cat-field-head {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--muted);
}
.cat-field-head input {
  font-weight: 400;
  color: var(--text);
}
.cat-suggest {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 10px;
  border: 1px dashed var(--border);
  border-radius: 8px;
  background: var(--bg);
}
.cat-suggest-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .03em;
}
.cat-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.cat-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 5px 10px;
  border: none;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all .15s ease;
  white-space: nowrap;
}
.cat-chip:hover {
  transform: translateY(-1px);
  box-shadow: 0 3px 8px rgba(0, 0, 0, .12);
  filter: brightness(1.1);
}
.cat-chip:active {
  transform: translateY(0);
}

/* ---------------- Yetkiler ekranı ---------------- */
.users-list { display: flex; flex-direction: column; gap: 12px; }
.user-card {
  background: var(--card);
  border-radius: var(--radius);
  padding: 16px 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  box-shadow: 0 2px 8px rgba(16, 42, 32, .06);
  border: 1px solid var(--border);
  transition: box-shadow .15s;
}
.user-card:hover { box-shadow: 0 4px 16px rgba(16, 42, 32, .1); }
.user-card-left { display: flex; align-items: center; gap: 12px; }
.user-avatar {
  width: 42px;
  height: 42px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--primary), var(--primary-dark));
  color: #fff;
  font-size: 18px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.user-card-name { font-size: 15px; font-weight: 600; }
.tag-me { font-size: 11px; color: var(--primary-dark); font-weight: 500; margin-left: 4px; }
.user-card-username { font-size: 12px; color: var(--muted); margin-top: 2px; }
.user-card-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }

/* ---------------- Responsive — mobil & tablet ---------------- */
@media (max-width: 640px) {
  .app-screen, #users-screen, #orders-screen { padding: 12px 14px 50px; }

  .topbar {
    padding: 10px 14px;
    flex-wrap: nowrap;
    gap: 8px;
  }
  .brand { font-size: 16px; min-width: 0; }
  .brand img.brand-icon { width: 28px; height: 28px; }
  .brand span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .user-area {
    gap: 4px;
    flex-wrap: nowrap;
    margin-left: auto;
  }

  /* Hide button labels on mobile — icon only */
  .btn-label { display: none; }
  .btn-icon { padding: 8px 10px; font-size: 15px; }

  /* Compact logout on mobile */
  .btn-logout { padding: 8px 12px; }

  .toolbar { gap: 8px; flex-wrap: wrap; }
  .toolbar .input { min-width: 0; flex: 1 1 140px; }
  .toolbar .btn { padding: 9px 12px; font-size: 13px; flex: 1 1 auto; }

  .stats { gap: 8px; }
  .stat { flex: 1; min-width: 0; padding: 10px 14px; }
  .stat b { font-size: 20px; }
  .stat span { font-size: 12px; }

  .items-grid { display: flex; flex-direction: column; gap: 10px; }

  .modal { max-width: 100%; padding: 20px; border-radius: 14px; }
  .modal h2 { font-size: 18px; }

  .auth-card { padding: 28px 20px; }

  .user-card { flex-direction: column; align-items: stretch; gap: 12px; }
  .user-card-left { gap: 10px; }
  .user-card-actions { justify-content: flex-start; flex-wrap: wrap; }
  .user-avatar { width: 38px; height: 38px; font-size: 16px; }

  .item-card-v2 { padding: 12px 12px; }
  .item-row-qty { font-size: 20px; }
  .item-name { font-size: 16px; }

  .quick-move { flex-wrap: wrap; }
  .quick-move .btn { flex: 1 1 40%; }
}

/* Tablet */
@media (min-width: 641px) and (max-width: 900px) {
  .items-grid { grid-template-columns: repeat(2, 1fr); }
  .stat { flex: 1; }
}

/* ---------------- Profesyonel butonlar & form ---------------- */

/* Ürün form — profesyonel */
#item-form label {
  margin-bottom: 14px;
  font-size: 13px;
  font-weight: 500;
  color: #4a5568;
}
#item-form label input,
#item-form label select {
  margin-top: 5px;
  padding: 10px 12px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  font-size: 15px;
  background: #fff;
  transition: border-color .15s, box-shadow .15s;
}
#item-form label input:focus,
#item-form label select:focus {
  outline: none;
  border-color: var(--primary);
  box-shadow: 0 0 0 3px rgba(14, 159, 110, .12);
}
#item-form button[type="submit"] {
  padding: 12px;
  font-size: 15px;
  font-weight: 600;
  margin-top: 6px;
}

/* Eski kart buton stilleri — artık op. 4 kullanılıyor ama korunur */
.item-actions .btn-sm {
  font-weight: 500;
  border-radius: 8px;
}
.item-actions .btn-plus { color: var(--primary-dark); border-color: var(--primary); background: #e0f4ec; }
.item-actions .btn-plus:hover { background: var(--primary); color: #fff; }
.item-actions .btn-minus { color: var(--danger); border-color: var(--danger); background: #fde8e8; }
.item-actions .btn-minus:hover { background: var(--danger); color: #fff; }
.item-actions .btn-edit { color: #3b6ea5; border-color: #3b6ea5; background: #e8f0f7; }
.item-actions .btn-edit:hover { background: #3b6ea5; color: #fff; }
.item-actions .btn-del { color: var(--danger); border-color: var(--danger); background: #fff; }
.item-actions .btn-del:hover { background: var(--danger); color: #fff; }

/* Modal — daha profesyonel */
.modal {
  border: 1px solid var(--border);
  border-radius: 16px;
  max-width: 500px;
  padding: 28px;
}
.modal h2 {
  font-size: 20px;
  font-weight: 700;
  color: #1f2933;
  margin-bottom: 18px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
}

/* Move form — profesyonel */
#move-form label {
  font-weight: 500;
  color: #4a5568;
  font-size: 13px;
}
#move-form label input {
  border-color: #d1d5db;
}
#move-form label input:focus {
  outline: none;
  border-color: var(--primary);
  box-shadow: 0 0 0 3px rgba(14, 159, 110, .12);
}

/* Auth card — profesyonel */
.auth-card {
  border: 1px solid var(--border);
  padding: 36px 32px;
}
.auth-card h1 {
  font-weight: 700;
  letter-spacing: -0.5px;
}

/* Topbar — daha profesyonel */
.topbar {
  border: 1px solid var(--border);
}
.brand {
  letter-spacing: -0.3px;
}

/* Stats — profesyonel */
.stat {
  border: 1px solid var(--border);
}

/* Item card — profesyonel */
.item-card {
  border: 1px solid var(--border);
}
.item-card:hover { box-shadow: 0 4px 16px rgba(16, 42, 32, .1); }

/* ---------------- Movements / users (eski modal için) ---------------- */
.move-list { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
.move-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
}
.move-row .m-item { font-weight: 600; font-size: 14px; }
.move-row .m-meta { font-size: 12px; color: var(--muted); margin-top: 2px; }
.amount-pos { color: var(--primary-dark); font-weight: 700; }
.amount-neg { color: var(--danger); font-weight: 700; }

.user-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border-bottom: 1px solid var(--border);
  padding: 10px 0;
}
.user-row .u-name { font-weight: 600; }
.user-row .u-username { font-size: 12px; color: var(--muted); }

.quick-move { display: flex; gap: 8px; margin-bottom: 10px; }
.quick-move .btn { flex: 1; }

/* ---------------- Toast ---------------- */
.toast {
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%) translateY(20px);
  background: #1f2933;
  color: #fff;
  padding: 12px 22px;
  border-radius: 999px;
  font-size: 14px;
  opacity: 0;
  pointer-events: none;
  transition: all .25s;
  z-index: 200;
}
.toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
.toast.error { background: var(--danger); }
.toast.success { background: var(--primary); }

/* ---------------- Bildirimler ---------------- */
.notif-btn, .err-btn { font-size: 16px; line-height: 1; }
.err-detail {
  background: var(--bg);
  border-radius: 8px;
  padding: 10px;
  margin: 8px 0;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  font-family: ui-monospace, "Courier New", monospace;
}
.notif-badge {
  display: inline-block;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  margin-left: 5px;
  background: var(--danger);
  color: #fff;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  text-align: center;
  line-height: 18px;
}
.notif-row {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  border-bottom: 1px solid var(--border);
  padding-bottom: 10px;
}
.notif-row.read { opacity: 0.55; }
.notif-icon { flex: 0 0 44px; text-align: center; }
.notif-icon img, .notif-icon .item-emoji { width: 44px; height: 44px; object-fit: cover; border-radius: 8px; }
.notif-body { flex: 1; min-width: 0; }
.notif-msg { font-size: 14px; word-break: break-word; }
.notif-time { font-size: 12px; color: var(--muted); margin-top: 2px; }

/* ---------------- Malzeme kartları — yatay satır (op. 4) ---------------- */
.items-grid {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.item-card-row {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 14px;
  display: flex;
  align-items: center;
  gap: 12px;
  box-shadow: 0 2px 6px rgba(16, 42, 32, .05);
  transition: box-shadow .15s;
}
.item-card-row:hover { box-shadow: 0 4px 14px rgba(16, 42, 32, .1); }
.item-card-row.low { border-left: 4px solid var(--warn); }
.item-card-row.zero { border-left: 4px solid var(--danger); }

.item-row-icon {
  width: 44px;
  height: 44px;
  border-radius: 8px;
  background: var(--bg);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.item-row-icon img,
.item-row-icon .item-emoji {
  width: 36px;
  height: 36px;
  object-fit: cover;
  border-radius: 6px;
}

.item-row-info {
  flex: 1;
  min-width: 0;
}
.item-row-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.item-row-info .item-name {
  font-size: 15px;
  font-weight: 700;
  color: var(--text);
}
.item-row-info .item-cat {
  font-size: 12px;
  color: var(--muted);
  margin-top: 1px;
}
.item-row-qty {
  font-size: 20px;
  font-weight: 700;
  color: var(--primary-dark);
  margin-top: 2px;
}
.item-row-qty small {
  font-size: 12px;
  color: var(--muted);
  font-weight: 400;
  margin-left: 3px;
}
.item-progress {
  height: 5px;
  background: var(--border);
  border-radius: 3px;
  margin-top: 6px;
  overflow: hidden;
}
.item-progress-fill {
  height: 100%;
  border-radius: 3px;
  transition: width .3s;
}

.item-row-actions {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex-shrink: 0;
}
.item-row-actions .btn-sm {
  padding: 5px 10px;
  font-size: 13px;
  border-radius: 6px;
  min-width: 36px;
}

/* Eski kart stilleri (op. 1) — artık kullanılmıyor ama korunuyor */
.item-card { display: none; }

/* ---------------- Malzeme kartları — V2 (yatay satır + alt butonlar) ---------------- */
.items-grid {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.item-card-v2 {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 14px;
  box-shadow: 0 2px 6px rgba(16, 42, 32, .05);
  transition: box-shadow .15s;
}
.item-card-v2:hover { box-shadow: 0 4px 14px rgba(16, 42, 32, .1); }
.item-card-v2.low { border-left: 4px solid var(--warn); }
.item-card-v2.zero { border-left: 4px solid var(--danger); }

.item-v2-top {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.item-v2-head {
  flex: 1;
  min-width: 0;
}
.item-v2-title-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.item-v2-title-row .item-name {
  font-size: 15px;
  font-weight: 700;
  color: var(--text);
  word-break: break-word;
}
.btn-info-inline {
  background: transparent;
  border: none;
  cursor: pointer;
  font-size: 15px;
  padding: 2px 4px;
  border-radius: 6px;
  transition: background .15s;
  flex-shrink: 0;
}
.btn-info-inline:hover { background: #e0f4ec; }
.item-v2-head .item-cat {
  font-size: 12px;
  color: var(--muted);
  margin-top: 2px;
}

.item-v2-body {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 10px;
  margin-top: 8px;
}
.item-v2-body > div:first-child {
  flex: 1;
  min-width: 0;
}
.item-v2-qtybtns {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex-shrink: 0;
}
.item-v2-qtybtns .btn-sm {
  padding: 6px 12px;
  font-size: 15px;
  border-radius: 8px;
  min-width: 42px;
}

.item-v2-admin {
  display: flex;
  gap: 8px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}
.item-v2-admin .btn-sm {
  flex: 1;
}

.btn-order {
  background: linear-gradient(135deg, var(--primary), var(--primary-dark));
  border: none;
  color: #fff;
  font-weight: 600;
  box-shadow: 0 2px 6px rgba(14, 159, 110, .25);
}
.btn-order:hover {
  box-shadow: 0 4px 12px rgba(14, 159, 110, .4);
}

/* ---------------- Kategori badge ---------------- */
.cat-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 999px;
  margin-top: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}
.cat-badge-default {
  background: var(--bg);
  color: var(--muted);
}
.item-v2-head .cat-badge {
  margin-top: 4px;
}

/* ---------------- Sipariş kartları ---------------- */
.order-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  box-shadow: 0 2px 6px rgba(16, 42, 32, .05);
  transition: box-shadow .15s;
}
.order-card:hover { box-shadow: 0 4px 14px rgba(16, 42, 32, .1); }
.order-card-left { display: flex; align-items: center; gap: 12px; flex: 1; min-width: 0; }
.order-icon { width: 40px; height: 40px; border-radius: 8px; background: var(--bg); display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.order-icon img, .order-icon .item-emoji { width: 32px; height: 32px; object-fit: cover; border-radius: 6px; }
.order-name { font-size: 14px; font-weight: 700; color: var(--text); }
.order-meta { font-size: 12px; color: var(--muted); margin-top: 2px; }
.order-time { font-size: 11px; color: var(--muted); margin-top: 2px; }
.order-card-right { flex-shrink: 0; }
.order-price { font-size: 16px; font-weight: 700; color: var(--primary-dark); }

/* ---------------- Alıcı rolü badge ---------------- */
.buyer-badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 10px;
  border-radius: 999px;
  background: #f0e8ff;
  color: #7c3aed;
  margin-top: 4px;
}

/* ---------------- Malzeme görseli ---------------- */
.item-media { font-size: 40px; line-height: 1.1; }
.item-emoji { display: inline-block; }
.item-img {
  width: 56px;
  height: 56px;
  object-fit: cover;
  border-radius: 10px;
  display: block;
  background: var(--bg);
}
.img-preview {
  margin: 8px 0 12px;
  min-height: 56px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg);
  border-radius: 10px;
  padding: 8px;
}
.img-preview img {
  max-width: 100%;
  max-height: 80px;
  border-radius: 8px;
  object-fit: cover;
}
.img-preview .item-emoji { font-size: 40px; }

</style>
</head>
<body>

<div id="toast" class="toast"></div>

<!-- ================= GİRİŞ / KAYIT ================= -->
<div id="auth-screen" class="auth-screen">
  <div class="auth-card">
    <div class="auth-logo"><img src="data:image/png;base64,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" alt="Çınar Logo"></div>
    <h1>Mutfak Takip</h1>

    <div class="auth-tabs">
      <button class="auth-tab active" data-tab="login">Giriş Yap</button>
      <button class="auth-tab" data-tab="register">Kayıt Ol</button>
    </div>

    <form id="login-form">
      <label>Kullanıcı Adı <input type="text" id="login-username" autocomplete="username" required></label>
      <label>Şifre <input type="password" id="login-password" autocomplete="current-password" required></label>
      <button type="submit" class="btn btn-primary btn-block">Giriş Yap</button>
    </form>

    <form id="register-form" class="hidden">
      <label>Ad Soyad <input type="text" id="reg-name" required></label>
      <label>Kullanıcı Adı <input type="text" id="reg-username" required></label>
      <label>Şifre <input type="password" id="reg-password" required minlength="4"></label>
      <button type="submit" class="btn btn-primary btn-block">Kayıt Ol</button>
      <p class="hint">Kayıt olan üyeler malzeme ekleyemez; bir yönetici
        <em>"Yönetici Yap"</em> opsiyonuyla düzenleme yetkisi verebilir.</p>
    </form>
  </div>
</div>

<!-- ================= UYGULAMA ================= -->
<div id="app-screen" class="app-screen hidden">
  <header class="topbar">
    <div class="brand"><img src="data:image/png;base64,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" alt="" class="brand-icon"> Çınar Mutfak</div>
    <div class="user-area">
      <span id="user-role" class="role-badge"></span>
      <button id="btn-notifications" class="btn-icon notif-btn" title="Bildirimler">🔔<span id="notif-badge" class="notif-badge hidden">0</span></button>
      <button id="btn-errors" class="btn-icon err-btn hidden" title="Hata raporları">⚠️<span id="err-badge" class="notif-badge hidden">0</span></button>
      <button id="btn-password" class="btn-icon btn-password" title="Şifre Değiştir">🔑<span class="btn-label"> Şifre</span></button>
      <button id="btn-logout" class="btn-icon btn-logout" title="Çıkış Yap"><img src="data:image/png;base64,AAAAAAAAAAAAAAAA////AwAAAAD///8B////Av///wH///8x////6P///+j///8x////Af///wL///8BAAAAAP///wMAAAAAAAAAAAAAAAAAAAAAAAAAAP///wIAAAAA////BwAAAAD///8EAAAAAP///4n//////////////4kAAAAA////BAAAAAD///8HAAAAAP///wIAAAAAAAAAAAAAAAD///8DAAAAAP///2r////f////cwAAAAAAAAAA////jf////7////+////jQAAAAAAAAAA////dP///9////9rAAAAAP///wMAAAAA////AwAAAAD///9w//////////////+tAAAAAAAAAAD///+M//////////////+MAAAAAAAAAAD///+t//////////////9wAAAAAP///wMAAAAA////Ov//////////////wf///xT///8EAAAAAP///4z//////////////4wAAAAA////BP///xP////B//////////////86AAAAAP///wP////G/////////9v///8QAAAAAP///wYAAAAA////jP//////////////jAAAAAD///8GAAAAAP///xD////b/////////8f///8D////SP//////////////SwAAAAD///8E////BAAAAAD///+M//////////////+MAAAAAP///wT///8EAAAAAP///0v//////////////0n///+f/////////9D///8B////AgAAAAD///8EAAAAAP///4v//////////////4sAAAAA////BAAAAAD///8C////Af///9D/////////n////9P/////////jQAAAAD///8EAAAAAP///wQAAAAA////jf////z////8////jQAAAAD///8EAAAAAP///wQAAAAA////jP/////////T////6v////////9qAAAAAP///wQAAAAA////BAAAAAD///+E//////////////+EAAAAAP///wQAAAAA////BAAAAAD///9p/////////+r////r/////////2gAAAAA////BAAAAAD///8B////Af///x3///+4////uf///x3///8B////AQAAAAD///8EAAAAAP///2f/////////6////9j/////////h////wH///8EAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP///wT///8B////h//////////Y////qf////7////HAAAAAP///wIAAAAAAAAAAAAAAAD///8B////Av///wL///8BAAAAAAAAAAAAAAAA////AgAAAAD////H/////v///6n///9V//////////3///87AAAAAP///wQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP///wQAAAAA////O/////3/////////Vf///wj////U/////v///8z///8F////Af///wQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD///8E////Af///wT////L/////v///9T///8IAAAAAP///0z//////////////6r///8FAAAAAP///wL///8E////BP///wT///8E////AgAAAAD///8F////qv//////////////TAAAAAD///8DAAAAAP///4v//////////////8T///85AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA////Of///8P//////////////4sAAAAA////A////wH///8DAAAAAP///4z///////////////7///+7////e////1n///9Z////e////7v////+//////////////+NAAAAAP///wP///8BAAAAAAAAAAD///8CAAAAAP///1X////U///////////////////////////////////////////////U////VQAAAAD///8CAAAAAAAAAAAAAAAAAAAAAAAAAAD///8DAAAAAP///wv///9i////s////+L////4////+P///+L///+z////Yv///wsAAAAA////AwAAAAAAAAAAAAAAAA==" alt="" class="logout-icon"><span class="btn-label"> Çıkış</span></button>
    </div>
  </header>

  <div class="toolbar">
    <input type="search" id="search" class="input" placeholder="Malzeme ara...">
    <select id="category-filter" class="input"></select>
    <button id="btn-history" class="btn">Hareketler</button>
    <button id="btn-orders" class="btn">📦 Siparişler</button>
    <button id="btn-users" class="btn hidden">Kullanıcılar</button>
    <button id="btn-new-item" class="btn btn-primary hidden">＋ Malzeme Ekle</button>
    <button id="btn-backup" class="btn btn-ghost hidden" title="Verilerimi yedekle">💾 Yedek</button>
    <button id="btn-restore" class="btn btn-ghost hidden" title="Yedeklerden geri yükle">↺ Yükle</button>
    <input type="file" id="restore-file" accept=".json" style="display:none">
  </div>

  <div id="stats" class="stats"></div>
  <div id="items-grid" class="items-grid"></div>
  <div id="empty" class="empty hidden">Henüz malzeme eklenmedi.</div>
</div>

<!-- ================= YETKİLER EKRANI ================= -->
<div id="users-screen" class="app-screen hidden">
  <header class="topbar">
    <div class="brand">
      <button id="btn-back-users" class="btn-back">← Geri</button>
      <span style="margin-left:10px">Çınar Mutfak · Kullanıcılar</span>
    </div>
    <div class="user-area">
      <span class="role-badge admin">Yönetici</span>
    </div>
  </header>

  <div class="toolbar">
    <h2 style="font-size:18px;flex:1">Kayıtlı Kullanıcılar</h2>
  </div>

  <div id="users-list" class="users-list"></div>
</div>

<!-- ================= SİPARİŞLER EKRANI ================= -->
<div id="orders-screen" class="app-screen hidden">
  <header class="topbar">
    <div class="brand">
      <button id="btn-back-orders" class="btn-back">← Geri</button>
      <span style="margin-left:10px">Çınar Mutfak · Siparişler</span>
    </div>
    <div class="user-area">
      <span class="role-badge ${state.user?.role || "viewer"}">${state.user?.role === "admin" ? "Yönetici" : "Görüntüleyici"}</span>
    </div>
  </header>

  <div class="toolbar">
    <h2 style="font-size:18px;flex:1">📦 Sipariş Geçmişi</h2>
    <button id="btn-new-order" class="btn btn-primary">＋ Yeni Sipariş</button>
  </div>

  <div id="orders-list" class="users-list"></div>
</div>

<!-- ================= MODAL ================= -->
<div id="modal-overlay" class="modal-overlay hidden">
  <div class="modal" id="modal"></div>
</div>

<script>
/* Mutfak Stok Uygulaması — frontend */
const $ = (s) => document.querySelector(s);
const state = { user: null, items: [], search: "", category: "" };

const UNITS = ["adet", "kg", "lt", "tane", "paket", "ekmek", "bardak", "kase", "pazar", "sap", "dilim"];

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmt(n) {
  return parseFloat(Number(n).toFixed(2)).toString();
}

let toastTimer = null;
function toast(msg, kind = "info") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 2600);
}

async function api(url, opts = {}) {
  const r = await fetch(url, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = {};
  try { data = await r.json(); } catch (e) { /* boş */ }
  if (!r.ok) {
    if (!opts.silent) toast(data.error || "Bir hata oluştu", "error");
    // report server errors (5xx or unexpected 4xx) to the error log
    if (r.status >= 500 || (r.status >= 400 && !opts.silent && !opts.noReport)) {
      reportError(`HTTP ${r.status} ${url}`, JSON.stringify(data).slice(0, 2000));
    }
    throw new Error(data.error || r.status);
  }
  return data;
}

/* ---------- Ekranlar ---------- */
function showAuth() {
  $("#auth-screen").classList.remove("hidden");
  $("#app-screen").classList.add("hidden");
}
function showApp() {
  $("#auth-screen").classList.add("hidden");
  $("#app-screen").classList.remove("hidden");
}

async function boot() {
  try {
    const me = await api("/api/me", { silent: true });
    state.user = me.user;
    enterApp();
  } catch (e) {
    showAuth();
  }
}

async function enterApp() {
  showApp();
  loadNotifications();
  const roleEl = $("#user-role");
  roleEl.textContent = state.user.role === "admin" ? "Yönetici" : "Görüntüleyici";
  roleEl.className = "role-badge " + state.user.role;
  const isAdmin = state.user.role === "admin";
  $("#btn-new-item").classList.toggle("hidden", !isAdmin);
  $("#btn-users").classList.toggle("hidden", !isAdmin);
  $("#btn-errors").classList.toggle("hidden", !isAdmin);
  $("#btn-backup").classList.toggle("hidden", !isAdmin);
  $("#btn-restore").classList.toggle("hidden", !isAdmin);
  if (isAdmin) loadErrors();
  await loadItems();
}

/* ---------- Malzemeler ---------- */
async function loadItems() {
  const { items } = await api("/api/items");
  state.items = items;
  renderItems();
  loadNotifications();
}

function filteredItems() {
  const q = state.search.trim().toLowerCase();
  return state.items.filter((it) => {
    const matchQ = !q || it.name.toLowerCase().includes(q);
    const matchCat = !state.category || it.category === state.category;
    return matchQ && matchCat;
  });
}

function effectiveThreshold(it) {
  const cands = [];
  const amountTh = Number(it.threshold || 0);
  const fullQty = Number(it.full_qty || 0);
  const pct = Number(it.threshold_pct || 0);
  if (amountTh > 0) cands.push(amountTh);
  if (fullQty > 0 && pct > 0) cands.push((fullQty * pct) / 100);
  return cands.length ? Math.max(...cands) : 0;
}

function isLow(it) {
  const qty = Number(it.quantity || 0);
  if (qty <= 0) return false;
  const eff = effectiveThreshold(it);
  return eff > 0 && qty <= eff;
}

function renderStats() {
  const low = state.items.filter(isLow).length;
  const empty = state.items.filter((it) => it.quantity <= 0).length;
  $("#stats").innerHTML = `
    <div class="stat"><b>${state.items.length}</b><span>Malzeme</span></div>
    <div class="stat ${low ? "warn" : ""}"><b>${low + empty}</b><span>Azalmış / Tükendi</span></div>
  `;
}

function expiryBadge(expiry) {
  if (!expiry) return "";
  const today = new Date();
  const d = new Date(expiry + "T00:00:00");
  const days = Math.round((d - today) / 86400000);
  if (days < 0) return `<span class="badge expired">Sünmüş (${-days} gün)</span>`;
  if (days <= 7) return `<span class="badge warn">${days} gün kaldı</span>`;
  return `<span class="badge ok">${days} gün kaldı</span>`;
}

function itemIcon(item) {
  if (item.image && item.image.startsWith("data:")) {
    return `<img class="item-img" src="${item.image}" alt="">`;
  }
  const emoji = item.image && item.image.trim() ? item.image.trim() : "📦";
  return `<span class="item-emoji">${esc(emoji)}</span>`;
}

const CATEGORY_COLORS = {
  "süt ürünleri": { bg: "#e0f4ec", fg: "#0b7f57", icon: "🥛" },
  "sebze": { bg: "#e8f5e8", fg: "#2d7a2d", icon: "🥬" },
  "meyve": { bg: "#fde8e8", fg: "#c0392b", icon: "🍎" },
  "et": { bg: "#fde8e8", fg: "#c0392b", icon: "🍖" },
  "unlu": { bg: "#fdf3e0", fg: "#b7791f", icon: "🍞" },
  "İçecek": { bg: "#e8f0f7", fg: "#2a5a8a", icon: "🥤" },
  "Baharat": { bg: "#f0e8ff", fg: "#7c3aed", icon: "🌶️" },
  "Diğer": { bg: "#f0f1f3", fg: "#7b8794", icon: "📦" },
};

function catColor(cat) {
  if (!cat) return null;
  const key = cat.toLowerCase().trim();
  if (CATEGORY_COLORS[key]) return CATEGORY_COLORS[key];
  // hash-based color for unknown categories
  let hash = 0;
  for (let i = 0; i < key.length; i++) hash = ((hash << 5) - hash + key.charCodeAt(i)) | 0;
  const palette = [
    { bg: "#e0f4ec", fg: "#0b7f57", icon: "📦" },
    { bg: "#fdf3e0", fg: "#b7791f", icon: "📦" },
    { bg: "#e8f0f7", fg: "#2a5a8a", icon: "📦" },
    { bg: "#fde8e8", fg: "#c0392b", icon: "📦" },
    { bg: "#f0e8ff", fg: "#7c3aed", icon: "📦" },
    { bg: "#e8f5e8", fg: "#2d7a2d", icon: "📦" },
  ];
  return palette[Math.abs(hash) % palette.length];
}

function renderItems() {
  renderStats();

  // kategori filter seçenekleri
  const cats = [...new Set(state.items.map((it) => it.category).filter(Boolean))].sort();
  const sel = $("#category-filter");
  const cur = state.category;
  sel.innerHTML = `<option value="">Tüm kategoriler</option>` +
    cats.map((c) => `<option ${c === cur ? "selected" : ""}>${esc(c)}</option>`).join("");

  const list = filteredItems();
  $("#empty").classList.toggle("hidden", list.length > 0);
  $("#items-grid").innerHTML = list.map((it) => {
    const low = isLow(it);
    const zero = it.quantity <= 0;
    const eff = effectiveThreshold(it);
    const pct = eff > 0 ? Math.min(100, Math.round((Number(it.quantity) / eff) * 100)) : 100;
    const pctColor = zero ? "var(--danger)" : low ? "var(--warn)" : "var(--primary)";
    const exp = expiryBadge(it.expiry);
    const isBuyer = state.user?.is_buyer;
    const catInfo = catColor(it.category);
    const catBadge = catInfo
      ? `<span class="cat-badge" style="background:${catInfo.bg};color:${catInfo.fg};">${catInfo.icon || ""} ${esc(it.category)}</span>`
      : (it.category ? `<span class="cat-badge cat-badge-default">${esc(it.category)}</span>` : "");
    return `
      <div class="item-card-v2 ${zero ? "zero" : low ? "low" : ""}">
        <div class="item-v2-top">
          <div class="item-row-icon">${itemIcon(it)}</div>
          <div class="item-v2-head">
            <div class="item-v2-title-row">
              <span class="item-name">${esc(it.name)}</span>
              <button class="btn-icon btn-info-inline" data-id="${it.id}" title="Hareketler">ℹ️</button>
            </div>
            ${catBadge}
          </div>
          ${exp}
        </div>
        <div class="item-v2-body">
          <div>
            <div class="item-row-qty">${fmt(it.quantity)}<small>${esc(it.unit)}</small></div>
            <div class="item-progress"><div class="item-progress-fill" style="width:${pct}%;background:${pctColor};"></div></div>
            <button class="btn btn-sm btn-order" data-id="${it.id}" style="margin-top:8px;">📦 Sipariş Ver</button>
          </div>
          <div class="item-v2-qtybtns">
            <button class="btn btn-sm btn-plus" data-id="${it.id}" title="Ekle">＋</button>
            <button class="btn btn-sm btn-minus" data-id="${it.id}" title="Kullan">－</button>
          </div>
        </div>
        ${state.user.role === "admin" ? `
        <div class="item-v2-admin">
          <button class="btn btn-sm btn-edit" data-id="${it.id}">✎ Düzenle</button>
          <button class="btn btn-sm btn-danger btn-del" data-id="${it.id}">🗑 Sil</button>
        </div>` : ""}
      </div>`;
  }).join("");
}

/* ---------- Hareketler ---------- */
async function openMovements() {
  const { movements } = await api("/api/movements");
  const rows = movements.map((m) => `
    <div class="move-row">
      <div>
        <div class="m-item">${esc(m.item_name)}</div>
        <div class="m-meta">${esc(m.user_name)} · ${esc(m.created_at)}${m.note ? " · " + esc(m.note) : ""}</div>
      </div>
      <div class="${m.amount >= 0 ? "amount-pos" : "amount-neg"}">
        ${m.amount >= 0 ? "+" : ""}${fmt(m.amount)} ${esc(m.item_unit)}
      </div>
    </div>`).join("");
  openModal(`<button class="modal-close" data-close>×</button><h2>Stok Hareketleri</h2>
    <div class="move-list">${rows || "Henüz hareket yok."}</div>`);
}

/* ---------- Hata yakalama & raporlama ---------- */
let lastError = null;
let errorTimer = null;

function reportError(message, detail) {
  try {
    if (errorTimer) return;  // don't flood
    errorTimer = setTimeout(() => { errorTimer = null; }, 2000);
    fetch("/api/errors", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: String(message).slice(0, 500), detail: String(detail || "").slice(0, 2000) }),
    }).catch(() => {});
  } catch (e) { /* ignore */ }
}

window.addEventListener("error", (e) => {
  reportError(e.message, `${e.filename}:${e.lineno}:${e.colno}\n${e.error ? e.error.stack || "" : ""}`);
});

window.addEventListener("unhandledrejection", (e) => {
  const r = e.reason;
  reportError(
    r && r.message ? r.message : String(r),
    r && r.stack ? r.stack : ""
  );
});

/* ---------- Bildirimler ---------- */
async function loadNotifications() {
  try {
    const data = await api("/api/notifications", { silent: true });
    const badge = $("#notif-badge");
    if (data.unread > 0) {
      badge.textContent = data.unread > 9 ? "9+" : String(data.unread);
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
    state.notifications = data.notifications;
  } catch (e) { /* oturum kapanmış olabilir */ }
}

function openNotifications() {
  const rows = (state.notifications || []).map((n) => `
    <div class="notif-row ${n.read ? "read" : ""}">
      <div class="notif-icon">${itemIcon({ image: n.item_image, name: n.item_name })}</div>
      <div class="notif-body">
        <div class="notif-msg">${esc(n.message)}</div>
        <div class="notif-time">${esc(n.created_at)}</div>
      </div>
    </div>`).join("");
  openModal(`<button class="modal-close" data-close>×</button><h2>Bildirimler</h2>
    <div class="move-list">${rows || "Henüz bildirim yok."}</div>
    <button id="btn-read-all" class="btn btn-primary btn-block" style="margin-top:14px">Tümü okundu</button>`);
  $("#btn-read-all").addEventListener("click", async () => {
    await api("/api/notifications/read", { method: "POST" });
    toast("Tüm bildirimler okundu", "success");
    loadNotifications();
    openNotifications();
  });
}

$("#btn-notifications").addEventListener("click", openNotifications);

/* ---------- Hata raporları (yönetici) ---------- */
async function loadErrors() {
  if (state.user?.role !== "admin") return;
  try {
    const data = await api("/api/errors", { silent: true });
    const badge = $("#err-badge");
    if (data.unread > 0) {
      badge.textContent = data.unread > 9 ? "9+" : String(data.unread);
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
    state.errors = data.errors;
  } catch (e) { /* ignore */ }
}

function openErrors() {
  const rows = (state.errors || []).map((e) => {
    const detail = e.detail ? `<pre class="err-detail">${esc(e.detail)}</pre>` : "";
    const copyText = `[Hata ${e.created_at}] Kullanıcı: ${e.user_name}\nMesaj: ${e.message}\n${e.detail ? "Detay:\\n" + e.detail : ""}`;
    return `
    <div class="notif-row ${e.read ? "read" : ""}">
      <div class="notif-body">
        <div class="notif-msg">${esc(e.message)}</div>
        <div class="notif-time">${esc(e.user_name)} · ${esc(e.created_at)}</div>
        ${detail}
        <button class="btn btn-sm" data-copy="${encodeURIComponent(copyText)}">Hata metnini kopyala</button>
      </div>
    </div>`;
  }).join("");
  openModal(`<button class="modal-close" data-close>×</button><h2>Hata Raporları</h2>
    <div class="move-list">${rows || "Henüz hata raporu yok."}</div>
    <button id="btn-errors-read" class="btn btn-primary btn-block" style="margin-top:14px">Tümü okundu</button>`);
  $("#btn-errors-read").addEventListener("click", async () => {
    await api("/api/errors/read", { method: "POST" });
    toast("Tüm hatalar okundu", "success");
    loadErrors();
    openErrors();
  });
}

$("#btn-errors").addEventListener("click", openErrors);

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const text = decodeURIComponent(btn.dataset.copy);
  navigator.clipboard.writeText(text).then(() => {
    toast("Hata metni kopyalandı — bana gönderebilirsin", "success");
  }).catch(() => {
    // fallback: show in a prompt
    const t = prompt("Kopyala:", text);
  });
});

/* ---------- Bildirimler (ayrı) ---------- */

/* ---------- Siparişler ---------- */
async function openOrdersScreen() {
  $("#app-screen").classList.add("hidden");
  $("#orders-screen").classList.remove("hidden");
  await loadOrdersToScreen();
}

async function loadOrdersToScreen() {
  const { orders } = await api("/api/orders");
  state.orders = orders;
  const rows = orders.map((o) => `
    <div class="order-card">
      <div class="order-card-left">
        <div class="order-icon">${itemIcon({ image: o.item_image, name: o.item_name })}</div>
        <div>
          <div class="order-name">${esc(o.item_name)}</div>
          <div class="order-meta">${fmt(o.quantity)} ${esc(o.item_unit)} · ${esc(o.user_name)}</div>
          <div class="order-time">${esc(o.ordered_at)}</div>
        </div>
      </div>
      <div class="order-card-right">
        ${o.item_price > 0 ? `<div class="order-price">₺${fmt(o.quantity * o.item_price)}</div>` : ""}
      </div>
    </div>`).join("");
  $("#orders-list").innerHTML = rows || `<div class="empty">Henüz sipariş yok.</div>`;
}

$("#btn-orders").addEventListener("click", openOrdersScreen);
$("#btn-back-orders").addEventListener("click", () => {
  $("#orders-screen").classList.add("hidden");
  $("#app-screen").classList.remove("hidden");
});
$("#btn-new-order").addEventListener("click", openNewOrderModal);

/* ---------- Yeni sipariş — ürün seçici modal ---------- */
async function openNewOrderModal() {
  const { items } = await api("/api/items");
  const options = items.map((it) => `
    <option value="${it.id}">${esc(it.name)} — ${fmt(it.quantity)} ${esc(it.unit)} · ${it.price > 0 ? "₺" + fmt(it.price) : "—"}</option>
  `).join("");
  openModal(`
    <button class="modal-close" data-close>×</button>
    <h2>📦 Yeni Sipariş</h2>
    <form id="order-form">
      <label>Malzeme <select name="item_id" required>${options}</select></label>
      <label>Sipariş miktarı <input name="quantity" type="number" step="0.01" min="0.01" value="1" required></label>
      <button type="submit" class="btn btn-primary btn-block">📦 Sipariş Kaydet</button>
    </form>
  `);
  $("#order-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/orders", {
        method: "POST",
        body: { item_id: Number(f.get("item_id")), quantity: f.get("quantity") },
      });
      toast("Sipariş kaydedildi — Alıcılar bilgilendirildi", "success");
      closeModal();
      loadOrdersToScreen();
    } catch (err) { /* toast hata zaten gösterildi */ }
  });
}

/* ---------- Sipariş verme modal ---------- */
function openOrderModal(item) {
  openModal(`
    <button class="modal-close" data-close>×</button>
    <h2>Sipariş Ver — ${esc(item.name)}</h2>
    <p style="color:var(--muted);font-size:13px;margin-bottom:12px">Mevcut miktar: <b>${fmt(item.quantity)} ${esc(item.unit)}</b></p>
    <form id="order-form">
      <label>Sipariş miktarı <input name="quantity" type="number" step="0.01" min="0.01" value="1" required></label>
      ${item.price > 0 ? `<p style="font-size:13px;color:var(--muted);margin-bottom:12px">Fiyat: <b>₺${fmt(item.price)}/${esc(item.unit)}</b></p>` : ""}
      <button type="submit" class="btn btn-primary btn-block">📦 Sipariş Kaydet</button>
    </form>`);

  $("#order-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/orders", {
        method: "POST",
        body: { item_id: item.id, quantity: f.get("quantity") },
      });
      toast("Sipariş kaydedildi — Alıcılar bilgilendirildi", "success");
      closeModal();
    } catch (err) { /* toast hata zaten gösterildi */ }
  });
}

/* ---------- Kullanıcılar (yönetici — özel ekran) ---------- */
async function showUsersScreen() {
  $("#app-screen").classList.add("hidden");
  $("#users-screen").classList.remove("hidden");
  await loadUsersToScreen();
}

async function loadUsersToScreen() {
  const { users } = await api("/api/users");
  state.users = users;
  const rows = users.map((u) => `
    <div class="user-card">
      <div class="user-card-left">
        <div class="user-avatar">${esc(u.name.charAt(0).toUpperCase())}</div>
        <div>
          <div class="user-card-name">${esc(u.name)} ${u.id === state.user.id ? "<span class='tag-me'>(sen)</span>" : ""}</div>
          <div class="user-card-username">@${esc(u.username)}</div>
          ${u.is_buyer ? `<span class="buyer-badge">🛒 Alıcı</span>` : ""}
        </div>
      </div>
      <div class="user-card-actions">
        <span class="role-badge ${u.role}">${u.role === "admin" ? "Yönetici" : "Görüntüleyici"}</span>
        ${u.role === "admin"
          ? `<button class="btn btn-sm btn-outline" data-role="viewer" data-uid="${u.id}">Yöneticiliği geri al</button>`
          : `<button class="btn btn-sm btn-primary" data-role="admin" data-uid="${u.id}">Yönetici Yap</button>`}
        <button class="btn btn-sm ${u.is_buyer ? "btn-primary" : "btn-outline"}" data-buyer="${u.id}" data-buyer-val="${u.is_buyer ? 0 : 1}">
          ${u.is_buyer ? "🛒 Alıcı (aktif)" : "🛒 Alıcı Yap"}
        </button>
        <button class="btn btn-sm" data-passwd="${u.id}" data-name="${esc(u.name)}">Şifre Değiştir</button>
        ${u.id !== state.user.id ? `<button class="btn btn-sm btn-danger" data-del-user="${u.id}">Sil</button>` : ""}
      </div>
    </div>`).join("");
  $("#users-list").innerHTML = rows;
}

$("#btn-back-users").addEventListener("click", () => {
  $("#users-screen").classList.add("hidden");
  $("#app-screen").classList.remove("hidden");
});

$("#btn-users").addEventListener("click", showUsersScreen);

/* modal-based users (kept for compat but not used) */
async function openUsers() {
  await showUsersScreen();
}

async function changeRole(uid, role) {
  await api(`/api/users/${uid}/role`, { method: "POST", body: { role } });
  toast("Rol güncellendi", "success");
  openUsers();
}

async function deleteUser(uid) {
  if (!confirm("Bu kullanıcıyı silmek istiyor musunuz?")) return;
  await api(`/api/users/${uid}`, { method: "DELETE" });
  toast("Kullanıcı silindi", "success");
  openUsers();
}

/* ---------- Şifre değiştirme ---------- */
function openPasswordModal(uid, name) {
  const self = uid === state.user.id;
  openModal(`
    <button class="modal-close" data-close>×</button>
    <h2>${self ? "Şifre Değiştir" : "Şifre Sıfırla — ${esc(name)}"}</h2>
    <form id="passwd-form">
      ${self ? `<label>Mevcut şifre <input name="old" type="password" required></label>` : ""}
      <label>Yeni şifre (en az 4 karakter) <input name="new" type="password" required minlength="4"></label>
      <label>Yeni şifreyi tekrar <input name="new2" type="password" required minlength="4"></label>
      <button type="submit" class="btn btn-primary btn-block">Kaydet</button>
    </form>`);

  $("#passwd-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    if (f.get("new") !== f.get("new2")) {
      toast("Yeni şifreler eşleşmiyor", "error");
      return;
    }
    try {
      if (self) {
        await api("/api/password", { method: "POST", body: { old: f.get("old"), new: f.get("new") } });
      } else {
        await api(`/api/users/${uid}/password`, { method: "POST", body: { new: f.get("new") } });
      }
      toast("Şifre güncellendi", "success");
      closeModal();
    } catch (err) { /* toast hata zaten gösterildi */ }
  });
}

$("#btn-password").addEventListener("click", () => openPasswordModal(state.user.id, state.user.name));

/* ---------- Görsel yükleme ---------- */
function fileToResizedDataURL(file, maxSize = 400, quality = 0.8) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        const scale = Math.min(1, maxSize / Math.max(img.width, img.height));
        const canvas = document.createElement("canvas");
        canvas.width = Math.round(img.width * scale);
        canvas.height = Math.round(img.height * scale);
        canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
        resolve(canvas.toDataURL("image/jpeg", quality));
      };
      img.onerror = () => reject(new Error("görsel yüklenemedi"));
      img.src = reader.result;
    };
    reader.onerror = () => reject(new Error("görsel yüklenemedi"));
    reader.readAsDataURL(file);
  });
}

/* ---------- Malzeme form ---------- */
function openItemForm(item) {
  const editing = !!item;
  const opts = UNITS.map((u) => `<option ${item && item.unit === u ? "selected" : ""}>${u}</option>`).join("");
  const emojiVal = item?.image && !item.image.startsWith("data:") ? item.image : "";

  // mevcut kategoriler (farklı, sıfırlanmamış)
  const existingCats = [...new Set(state.items.map((it) => (it.category || "").trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b, "tr"));
  const catDatalist = `<datalist id="cat-datalist">${existingCats.map((c) => `<option value="${esc(c)}">`).join("")}</datalist>`;
  const catChips = existingCats.length
    ? `<div class="cat-suggest" title="Mevcut kategoriler — tıklayarak seç">
         <span class="cat-suggest-label">Mevcut kategoriler</span>
         <div class="cat-chips">
           ${existingCats.map((c) => {
             const ci = catColor(c);
             return `<button type="button" class="cat-chip" data-cat="${esc(c)}" style="background:${ci.bg};color:${ci.fg}">${ci.icon} ${esc(c)}</button>`;
           }).join("")}
         </div>
       </div>`
    : "";
  openModal(`
    <button class="modal-close" data-close>×</button>
    <h2>${editing ? "Malzeme Düzenle" : "Malzeme Ekle"}</h2>
    <form id="item-form">
      <label>Adı <input name="name" required value="${esc(item?.name || "")}"></label>
      <label>Birim
        <select name="unit">${opts}
          ${item && !UNITS.includes(item.unit) ? `<option selected>${esc(item.unit)}</option>` : ""}
        </select>
      </label>
      <label>Miktar <input name="quantity" type="number" step="0.01" value="${item?.quantity ?? 0}"></label>
      <label class="cat-field">
        <span class="cat-field-head">Kategori
          <input name="category" list="cat-datalist" value="${esc(item?.category || "")}" placeholder="ör. Süt ürünleri" autocomplete="off">
        </span>
        ${catChips}
      </label>
      <label>Son Kullanma Tarihi <input name="expiry" type="date" value="${esc(item?.expiry || "")}"></label>
      <label>Düşük stok eşiği (miktar) <input name="threshold" type="number" step="0.01" value="${item?.threshold ?? 0}"></label>
      <label>Tam stok miktarı <input name="full_qty" type="number" step="0.01" value="${item?.full_qty ?? item?.quantity ?? 0}"></label>
      <label>Eşik oranı (% tam stoka göre) <input name="threshold_pct" type="number" step="1" min="0" max="100" value="${item?.threshold_pct ?? 0}"></label>
      <label>Birim fiyat (₺) <input name="price" type="number" step="0.01" min="0" value="${item?.price ?? 0}"></label>
      <label>İkon (emoji, isteğe bağlı) <input name="emoji" value="${esc(emojiVal)}" placeholder="ör. 🧀, 🥛, 🥦"></label>
      <label>Küçük görsel (isteğe bağlı) <input name="image" type="file" accept="image/*"></label>
      <div id="img-preview" class="img-preview"></div>
      <button type="submit" class="btn btn-primary btn-block">Kaydet</button>
    </form>${catDatalist}`);

  const fileInput = $("#item-form").querySelector('[name="image"]');
  const emojiInput = $("#item-form").querySelector('[name="emoji"]');
  const preview = $("#img-preview");

  // kategori çipleri — tıklanınca inputa yaz
  document.querySelectorAll(".cat-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const inp = document.querySelector('#item-form [name="category"]');
      if (inp) { inp.value = btn.dataset.cat; inp.focus(); }
    });
  });

  function refreshPreview() {
    if (fileInput.files[0]) {
      preview.innerHTML = `<img src="${URL.createObjectURL(fileInput.files[0])}" alt="">`;
    } else if (emojiInput.value.trim()) {
      preview.innerHTML = `<span class="item-emoji">${esc(emojiInput.value.trim())}</span>`;
    } else if (item?.image && item.image.startsWith("data:")) {
      preview.innerHTML = `<img src="${item.image}" alt="">`;
    } else {
      preview.innerHTML = "";
    }
  }
  fileInput.addEventListener("change", refreshPreview);
  emojiInput.addEventListener("input", refreshPreview);
  refreshPreview();

  $("#item-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    let image = item?.image || "";
    try {
      if (fileInput.files[0]) {
        image = await fileToResizedDataURL(fileInput.files[0]);
      } else if (String(f.get("emoji")).trim()) {
        image = String(f.get("emoji")).trim();
      }
    } catch (err) {
      toast("Görsel yüklenemedi — daha küçük bir görsel deneyin", "error");
      return;
    }
    const body = {
      name: f.get("name"), unit: f.get("unit"), quantity: f.get("quantity"),
      category: f.get("category"), expiry: f.get("expiry"), threshold: f.get("threshold"),
      full_qty: f.get("full_qty"), threshold_pct: f.get("threshold_pct"),
      price: f.get("price"),
      image,
    };
    if (editing) {
      await api(`/api/items/${item.id}`, { method: "PUT", body });
      toast("Malzeme güncellendi", "success");
    } else {
      await api("/api/items", { method: "POST", body });
      toast("Malzeme eklendi", "success");
    }
    closeModal();
    loadItems();
  });
}

/* ---------- Ürün hareket geçmişi (fiyatlı) ---------- */
async function openItemHistory(item) {
  const { movements } = await api("/api/movements?limit=200");
  const itemMoves = movements.filter((m) => m.item_id === item.id);
  const price = Number(item.price || 0);
  const rows = itemMoves.map((m) => {
    const val = price > 0 ? Math.abs(m.amount) * price : 0;
    const valText = price > 0 ? ` · ₺${val.toFixed(2)}` : "";
    return `
    <div class="move-row">
      <div>
        <div class="m-item">${m.amount >= 0 ? "➕" : "➖"} ${fmt(Math.abs(m.amount))} ${esc(m.item_unit)}${valText}</div>
        <div class="m-meta">${esc(m.user_name)} · ${esc(m.created_at)}${m.note ? " · " + esc(m.note) : ""}</div>
      </div>
      <div class="${m.amount >= 0 ? "amount-pos" : "amount-neg"}">
        ${m.amount >= 0 ? "+" : ""}${fmt(m.amount)} ${esc(m.item_unit)}
      </div>
    </div>`;
  }).join("");
  openModal(`<button class="modal-close" data-close>×</button>
    <h2>${esc(item.name)} — Hareketler</h2>
    <p style="color:var(--muted);font-size:13px;margin-bottom:12px">
      Mevcut miktar: <b>${fmt(item.quantity)} ${esc(item.unit)}</b>
      ${price > 0 ? ` · Birim fiyat: <b>₺${fmt(price)}</b>` : ""}
    </p>
    <div class="move-list">${rows || "Henüz hareket yok."}</div>`);
}

async function deleteItem(id) {
  if (!confirm("Bu malzemeyi silmek istiyor musunuz?")) return;
  await api(`/api/items/${id}`, { method: "DELETE" });
  toast("Malzeme silindi", "success");
  loadItems();
}

/* ---------- Stok hareketi ---------- */
function openMoveForm(item) {
  openModal(`
    <button class="modal-close" data-close>×</button>
    <h2>${esc(item.name)} — Stok Güncelle</h2>
    <p style="color:var(--muted);font-size:13px;margin-bottom:12px">Mevcut miktar:
      <b>${fmt(item.quantity)} ${esc(item.unit)}</b></p>
    <div class="quick-move">
      <button class="btn" data-quick="1">+1</button>
      <button class="btn" data-quick="5">+5</button>
      <button class="btn" data-quick="-1">−1</button>
      <button class="btn" data-quick="-0.5">−0.5</button>
    </div>
    <form id="move-form">
      <label>Miktar (olumlu = ekle, olumsuz = kullan)
        <input name="amount" type="number" step="0.01" required placeholder="ör. 2 ya da -1"></label>
      <label>Not (isteğe bağlı) <input name="note" placeholder="ör. Kahvaltıda kullanıldı"></label>
      <button type="submit" class="btn btn-primary btn-block">Kaydet</button>
    </form>`);

  document.querySelectorAll("[data-quick]").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelector('[name="amount"]').value = b.dataset.quick;
    });
  });

  $("#move-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    await api(`/api/items/${item.id}/move`, {
      method: "POST",
      body: { amount: f.get("amount"), note: f.get("note") },
    });
    toast("Stok güncellendi", "success");
    closeModal();
    loadItems();
  });
}

/* ---------- Modal yardımcı ---------- */
function openModal(html) {
  $("#modal").innerHTML = html;
  $("#modal-overlay").classList.remove("hidden");
}
function closeModal() {
  $("#modal-overlay").classList.add("hidden");
  $("#modal").innerHTML = "";
}
$("#modal-overlay").addEventListener("click", (e) => {
  if (e.target.id === "modal-overlay" || e.target.hasAttribute("data-close")) closeModal();
});

/* ---------- Wire up ---------- */
document.querySelectorAll(".auth-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".auth-tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    const isLogin = tab.dataset.tab === "login";
    $("#login-form").classList.toggle("hidden", !isLogin);
    $("#register-form").classList.toggle("hidden", isLogin);
  });
});

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const user = await api("/api/login", {
      method: "POST",
      body: { username: $("#login-username").value, password: $("#login-password").value },
    });
    state.user = user;
    toast("Hoş geldiniz!", "success");
    enterApp();
  } catch (err) { /* toast hata zaten gösterildi */ }
});

$("#register-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/register", {
      method: "POST",
      body: {
        name: $("#reg-name").value,
        username: $("#reg-username").value,
        password: $("#reg-password").value,
      },
    });
    toast("Kayıt başarılı — giriş yapabilirsiniz", "success");
    document.querySelector('[data-tab="login"]').click();
  } catch (err) { /* toast hata zaten gösterildi */ }
});

$("#btn-logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  state.user = null;
  showAuth();
});

$("#search").addEventListener("input", (e) => { state.search = e.target.value; renderItems(); });
$("#category-filter").addEventListener("change", (e) => { state.category = e.target.value; renderItems(); });

$("#items-grid").addEventListener("click", (e) => {
  const id = Number(e.target.dataset.id);
  if (e.target.classList.contains("btn-plus") || e.target.classList.contains("btn-minus")) {
    const item = state.items.find((it) => it.id === id);
    if (item) openMoveForm(item);
  } else if (e.target.classList.contains("btn-edit")) {
    const item = state.items.find((it) => it.id === id);
    if (item) openItemForm(item);
  } else if (e.target.classList.contains("btn-del")) {
    deleteItem(id);
  } else if (e.target.classList.contains("btn-info-inline")) {
    const item = state.items.find((it) => it.id === id);
    if (item) openItemHistory(item);
  } else if (e.target.classList.contains("btn-order")) {
    const item = state.items.find((it) => it.id === id);
    if (item) openOrderModal(item);
  }
});

$("#btn-new-item").addEventListener("click", () => openItemForm(null));
$("#btn-history").addEventListener("click", openMovements);

// User action buttons now live in the users screen (not modal)
$("#users-list").addEventListener("click", (e) => {
  if (e.target.dataset.role) changeRole(Number(e.target.dataset.uid), e.target.dataset.role);
  if (e.target.dataset.delUser) deleteUser(Number(e.target.dataset.delUser));
  if (e.target.dataset.passwd) openPasswordModal(Number(e.target.dataset.passwd), e.target.dataset.name);
  if (e.target.dataset.buyer) toggleBuyer(Number(e.target.dataset.buyer), Number(e.target.dataset.buyerVal));
});

async function toggleBuyer(uid, val) {
  await api(`/api/users/${uid}/buyer`, { method: "POST", body: { is_buyer: val } });
  toast(val ? "Alıcı rolü verildi" : "Alıcı rolü alındı", "success");
  loadUsersToScreen();
}

$("#modal").addEventListener("click", (e) => {
  if (e.target.dataset.role) changeRole(Number(e.target.dataset.uid), e.target.dataset.role);
  if (e.target.dataset.delUser) deleteUser(Number(e.target.dataset.delUser));
  if (e.target.dataset.passwd) openPasswordModal(Number(e.target.dataset.passwd), e.target.dataset.name);
});

// ---------- Backup & Restore ----------

$("#btn-backup").addEventListener("click", async () => {
  try {
    const res = await api("/api/backup");
    const data = res.backup;
    const blob = new Blob([data], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `mutfak-stok-backup-${new Date().toISOString().slice(0,10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast("Veriler yedeklendik — dosya indirildi", "success");
  } catch (e) {
    toast("Backup sırasında hata oluştu", "error");
  }
});

$("#btn-restore").addEventListener("click", () => {
  document.getElementById("restore-file").click();
});

$("#restore-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  if (!confirm("Tüm mevcut verilerin yedek verilerle değiştireceğine emin misiniz?")) {
    e.target.value = "";
    return;
  }
  try {
    const text = await file.text();
    const res = await api("/api/restore", { method: "POST", body: { backup: text } });
    toast(res.message || "Veriler restore edildi", "success");
    e.target.value = "";
    // Refresh everything
    await loadItems();
    state.user = (await api("/api/me", { silent: true })).user;
    enterApp();
  } catch (err) {
    toast("Restore sırasında hata oluştu", "error");
    e.target.value = "";
  }
});

boot();

</script>
</body>
</html>
"""


# ---------------------------------------------------------------- Auth

def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def login_required(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        user = current_user()
        if user is None:
            return jsonify(error="Giriş yapmalısınız"), 401
        return f(*args, **kwargs)
    return wrap


def admin_required(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        user = current_user()
        if user is None:
            return jsonify(error="Giriş yapmalısınız"), 401
        if user["role"] != "admin":
            return jsonify(error="Bu işlem için yönetici yetkisi gerekiyor"), 403
        return f(*args, **kwargs)
    return wrap


# ---------------------------------------------------------------- API

@app.get("/api/me")
@login_required
def api_me():
    return jsonify(user=user_json(current_user()))


@app.post("/api/login")
def api_login():
    d = request.get_json(force=True)
    row = get_db().execute(
        "SELECT * FROM users WHERE username = ?",
        ((d.get("username") or "").strip(),)
    ).fetchone()
    if not row or not check_password_hash(row["password_hash"], d.get("password") or ""):
        return jsonify(error="Kullanıcı adı veya şifre hatalı"), 401
    session["uid"] = row["id"]
    return jsonify(user=user_json(row))


@app.post("/api/register")
def api_register():
    d = request.get_json(force=True)
    username = (d.get("username") or "").strip()
    name = (d.get("name") or "").strip()
    password = d.get("password") or ""
    if not username or not name or len(password) < 4:
        return jsonify(error="Tüm alanlar zorunlu (şifre en az 4 karakter)"), 400
    db = get_db()
    try:
        db.execute(
        "INSERT INTO users (username, name, password_hash, role, is_buyer, created_at) VALUES (?,?,?,?,?,?)",
        (username, name, generate_password_hash(password), "viewer", 0,
             datetime.now().isoformat(timespec="seconds")),
        )
        db.commit()
        auto_sync_backup(get_db())
    except sqlite3.IntegrityError:
        return jsonify(error="Bu kullanıcı adı zaten mevcut"), 409
    return jsonify(message="Kaydınız oluşturuldu. Giriş yapabilirsiniz.")


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/password")
@login_required
def api_change_password():
    d = request.get_json(force=True)
    user = current_user()
    if not check_password_hash(user["password_hash"], d.get("old") or ""):
        return jsonify(error="Mevcut şifre hatalı"), 400
    if len(d.get("new") or "") < 4:
        return jsonify(error="Yeni şifre en az 4 karakter olmalıdır"), 400
    db = get_db()
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
               (generate_password_hash(d["new"]), user["id"]))
    db.commit()
    auto_sync_backup(get_db())
    return jsonify(message="Şifre değiştirildi")


@app.post("/api/users/<int:uid>/password")
@admin_required
def api_reset_password(uid):
    d = request.get_json(force=True)
    if len(d.get("new") or "") < 4:
        return jsonify(error="Şifre en az 4 karakter olmalıdır"), 400
    db = get_db()
    if db.execute("SELECT id FROM users WHERE id = ?", (uid,)).fetchone() is None:
        return jsonify(error="Kullanıcı bulunamadı"), 404
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
               (generate_password_hash(d["new"]), uid))
    db.commit()
    auto_sync_backup(get_db())
    return jsonify(message="Şifre sıfırlandı")


@app.get("/api/items")
@login_required
def api_items():
    rows = get_db().execute("SELECT * FROM items ORDER BY category, name").fetchall()
    return jsonify(items=[dict(r) for r in rows])


@app.post("/api/items")
@admin_required
def api_add_item():
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(error="Malzeme adı boş olamaz"), 400
    unit = (d.get("unit") or "adet").strip() or "adet"
    quantity = float(d.get("quantity") or 0)
    threshold = float(d.get("threshold") or 0)
    full_qty = float(d.get("full_qty") or 0) or quantity  # default: current amount
    pct = float(d.get("threshold_pct") or 0)
    price = float(d.get("price") or 0)
    category = (d.get("category") or "").strip()
    expiry = (d.get("expiry") or "").strip() or None
    image = (d.get("image") or "").strip()
    db = get_db()
    cur = db.execute(
        "INSERT INTO items (name, unit, quantity, category, expiry, threshold, full_qty, threshold_pct, price, image, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (name, unit, quantity, category, expiry, threshold, full_qty, pct, price, image,
         datetime.now().isoformat(timespec="seconds")),
    )
    item_id = cur.lastrowid
    if quantity != 0:
        log_movement(item_id, current_user(), quantity, "İlk stok")
    notify_low_stock(item_id, quantity, None)
    db.commit()
    auto_sync_backup(get_db())
    return jsonify(message="Malzeme eklendi")


@app.put("/api/items/<int:item_id>")
@admin_required
def api_edit_item(item_id):
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(error="Malzeme adı boş olamaz"), 400
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return jsonify(error="Malzeme bulunamadı"), 404
    new_qty = float(d.get("quantity") if "quantity" in d else row["quantity"])
    threshold_changed = (
        float(d.get("threshold") if "threshold" in d else row["threshold"])
        != float(row["threshold"])
        or float(d.get("threshold_pct") if "threshold_pct" in d else row["threshold_pct"])
        != float(row["threshold_pct"])
        or float(d.get("full_qty") if "full_qty" in d else row["full_qty"])
        != float(row["full_qty"])
    )
    db.execute(
        "UPDATE items SET name=?, unit=?, quantity=?, category=?, expiry=?, threshold=?, full_qty=?, threshold_pct=?, price=?, image=? WHERE id=?",
        (name, (d.get("unit") or row["unit"]).strip() or "adet", new_qty,
         (d.get("category") or "").strip(), (d.get("expiry") or "").strip() or None,
         float(d.get("threshold") if "threshold" in d else row["threshold"]),
         float(d.get("full_qty") if "full_qty" in d else row["full_qty"]),
         float(d.get("threshold_pct") if "threshold_pct" in d else row["threshold_pct"]),
         float(d.get("price") if "price" in d else row["price"]),
         (d.get("image") if "image" in d else row["image"]) or "", item_id),
    )
    if "quantity" in d and new_qty != row["quantity"]:
        log_movement(item_id, current_user(), new_qty - row["quantity"], "Düzenleme")
        notify_low_stock(item_id, new_qty, row["quantity"], force=threshold_changed)
    elif threshold_changed:
        notify_low_stock(item_id, row["quantity"], None, force=True)
    db.commit()
    auto_sync_backup(get_db())
    return jsonify(message="Malzeme güncellendi")


@app.delete("/api/items/<int:item_id>")
@admin_required
def api_delete_item(item_id):
    db = get_db()
    row = db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return jsonify(error="Malzeme bulunamadı"), 404
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    auto_sync_backup(get_db())
    return jsonify(message="Malzeme silindi")


@app.post("/api/items/<int:item_id>/move")
@admin_required
def api_move(item_id):
    d = request.get_json(force=True)
    try:
        amount = float(d.get("amount"))
    except (TypeError, ValueError):
        return jsonify(error="Miktar geçerli bir sayı olmalı"), 400
    if amount == 0:
        return jsonify(error="Miktar 0 olamaz"), 400
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return jsonify(error="Malzeme bulunamadı"), 404
    note = (d.get("note") or "").strip()
    old_qty = row["quantity"]
    db.execute("UPDATE items SET quantity = quantity + ? WHERE id = ?", (amount, item_id))
    log_movement(item_id, current_user(), amount, note)
    notify_low_stock(item_id, old_qty + amount, old_qty)
    db.commit()
    auto_sync_backup(get_db())
    return jsonify(message="Stok güncellendi")


@app.get("/api/movements")
@login_required
def api_movements():
    limit = min(int(request.args.get("limit", 100)), 500)
    rows = get_db().execute(
        "SELECT m.*, i.name AS item_name, i.unit AS item_unit, i.price AS item_price "
        "FROM movements m JOIN items i ON i.id = m.item_id "
        "ORDER BY m.created_at DESC, m.id DESC LIMIT ?", (limit,)
    ).fetchall()
    return jsonify(movements=[dict(r) for r in rows])


@app.get("/api/notifications")
@login_required
def api_notifications():
    user = current_user()
    db = get_db()
    rows = db.execute(
        "SELECT n.*, i.name AS item_name, i.image AS item_image FROM notifications n "
        "JOIN items i ON i.id = n.item_id "
        "WHERE n.user_id = ? ORDER BY n.created_at DESC, n.id DESC LIMIT 100",
        (user["id"],),
    ).fetchall()
    unread = db.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read = 0",
        (user["id"],),
    ).fetchone()[0]
    return jsonify(notifications=[dict(r) for r in rows], unread=unread)


@app.post("/api/notifications/read")
@login_required
def api_mark_notifications_read():
    user = current_user()
    db = get_db()
    db.execute("UPDATE notifications SET read = 1 WHERE user_id = ? AND read = 0",
               (user["id"],))
    db.commit()
    auto_sync_backup(get_db())
    return jsonify(message="Tüm bildirimler okundu")


# ---------------------------------------------------------------- Errors

@app.post("/api/errors")
@login_required
def api_log_error():
    d = request.get_json(silent=True) or {}
    message = (d.get("message") or "").strip()[:500]
    detail = (d.get("detail") or "").strip()[:2000]
    if not message:
        return jsonify(ok=True)  # silently accept empty
    user = current_user()
    db = get_db()
    # dedupe: skip if identical message from same user in last 30s
    recent = db.execute(
        "SELECT id FROM errors WHERE user_id = ? AND message = ? "
        "AND created_at > datetime('now', '-30 seconds')",
        (user["id"], message),
    ).fetchone()
    if recent:
        return jsonify(ok=True)
    db.execute(
        "INSERT INTO errors (user_id, message, detail, created_at, read) VALUES (?,?,?,?,0)",
        (user["id"], message, detail,
         datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()
    auto_sync_backup(get_db())
    return jsonify(ok=True)


@app.get("/api/errors")
@admin_required
def api_list_errors():
    db = get_db()
    rows = db.execute(
        "SELECT e.*, u.name AS user_name FROM errors e "
        "JOIN users u ON u.id = e.user_id "
        "ORDER BY e.created_at DESC, e.id DESC LIMIT 200"
    ).fetchall()
    unread = db.execute(
        "SELECT COUNT(*) FROM errors WHERE read = 0"
    ).fetchone()[0]
    return jsonify(errors=[dict(r) for r in rows], unread=unread)


@app.post("/api/errors/read")
@admin_required
def api_mark_errors_read():
    db = get_db()
    db.execute("UPDATE errors SET read = 1 WHERE read = 0")
    db.commit()
    auto_sync_backup(get_db())
    return jsonify(message="Tüm hatalar okundu")


@app.get("/api/users")
@admin_required
def api_users():
    rows = get_db().execute("SELECT * FROM users ORDER BY created_at").fetchall()
    return jsonify(users=[user_json(r) for r in rows])


@app.post("/api/users/<int:uid>/role")
@admin_required
def api_set_role(uid):
    d = request.get_json(force=True)
    role = d.get("role")
    if role not in ("admin", "viewer"):
        return jsonify(error="Geçersiz rol"), 400
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if target is None:
        return jsonify(error="Kullanıcı bulunamadı"), 404
    if target["role"] == "admin" and role == "viewer":
        admin_count = db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        if admin_count <= 1:
            return jsonify(error="Son yöneticilerin rolü değiştirilemez"), 400
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    db.commit()
    auto_sync_backup(get_db())
    return jsonify(message="Rol güncellendi")


@app.post("/api/users/<int:uid>/buyer")
@admin_required
def api_set_buyer(uid):
    d = request.get_json(force=True)
    buyer = 1 if d.get("is_buyer") else 0
    db = get_db()
    target = db.execute("SELECT id FROM users WHERE id = ?", (uid,)).fetchone()
    if target is None:
        return jsonify(error="Kullanıcı bulunamadı"), 404
    db.execute("UPDATE users SET is_buyer = ? WHERE id = ?", (buyer, uid))
    db.commit()
    auto_sync_backup(get_db())
    return jsonify(message="Alıcı rolü güncellendi", is_buyer=bool(buyer))


# ---------------------------------------------------------------- Orders

@app.post("/api/orders")
@login_required
def api_create_order():
    d = request.get_json(force=True)
    item_id = d.get("item_id")
    quantity = float(d.get("quantity") or 0)
    user = current_user()
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return jsonify(error="Malzeme bulunamadı"), 404
    db.execute(
        "INSERT INTO orders (user_id, item_id, quantity, ordered_at) VALUES (?,?,?,?)",
        (user["id"], item_id, quantity, datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()
    auto_sync_backup(get_db())
    # notify all buyers about this order
    now = datetime.now().isoformat(timespec="seconds")
    msg = f"{user['name']} {item['name']} ürününden sipariş verdi"
    for u in db.execute("SELECT id FROM users WHERE is_buyer = 1").fetchall():
        db.execute(
            "INSERT INTO notifications (user_id, item_id, message, created_at, read) VALUES (?,?,?,?,0)",
            (u["id"], item_id, msg, now),
        )
    db.commit()
    auto_sync_backup(get_db())
    return jsonify(message="Sipariş kaydedildi")


@app.get("/api/orders")
@login_required
def api_list_orders():
    db = get_db()
    rows = db.execute(
        "SELECT o.*, i.name AS item_name, i.unit AS item_unit, i.price AS item_price, "
        "i.image AS item_image, u.name AS user_name "
        "FROM orders o JOIN items i ON i.id = o.item_id JOIN users u ON u.id = o.user_id "
        "ORDER BY o.ordered_at DESC, o.id DESC LIMIT 200"
    ).fetchall()
    return jsonify(orders=[dict(r) for r in rows])


@app.delete("/api/users/<int:uid>")
@admin_required
def api_delete_user(uid):
    db = get_db()
    me = current_user()
    if uid == me["id"]:
        return jsonify(error="Kendi hesabınızı silemezsiniz"), 400
    target = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if target is None:
        return jsonify(error="Kullanıcı bulunamadı"), 404
    if target["role"] == "admin":
        admin_count = db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        if admin_count <= 1:
            return jsonify(error="Son yönetici silinemez"), 400
    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    db.commit()
    auto_sync_backup(get_db())
    return jsonify(message="Kullanıcı silindi")


# ---------------------------------------------------------------- Backup & Restore API

@app.get("/api/backup")
@admin_required
def api_backup():
    """Export all data as JSON backup."""
    db = get_db()
    data = export_backup(db)
    return jsonify(backup=data)


@app.post("/api/restore")
@admin_required
def api_restore():
    """Restore data from JSON backup."""
    db = get_db()
    data_str = request.json.get("backup", "")
    if not data_str:
        return jsonify(error="Backup verisi boş"), 400
    try:
        import_backup(db, data_str)
    except Exception as e:
        return jsonify(error=f"Restore hatası: {e}"), 400
    return jsonify(message="Veriler restore edildi")


# ---------------------------------------------------------------- Frontend

@app.get("/")
def index():
    return HTML


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
