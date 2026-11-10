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

  /* Logout button always shows text on mobile (no icon) */
  .btn-logout .btn-label { display: inline !important; }
  .btn-logout { padding: 8px 14px; font-size: 14px; }

  /* Compact logout on mobile */
  .btn-logout { padding: 8px 12px; }

/* Settings dropdown */
.settings-wrapper { position: relative; }
.btn-settings {
  font-size: 18px;
  padding: 8px 10px;
}
.settings-dropdown {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 8px 30px rgba(0,0,0,.12);
  padding: 6px;
  min-width: 220px;
  z-index: 50;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.settings-dropdown.hidden { display: none !important; }
.settings-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border: none;
  background: transparent;
  border-radius: 8px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  color: var(--text);
  text-align: left;
  transition: background .15s;
  white-space: nowrap;
}
.settings-item:hover {
  background: var(--bg);
}
.settings-item:active {
  transform: scale(.98);
}
.settings-item .notif-badge { margin-left: auto; }

/* Mobile — settings dropdown wider */
@media (max-width: 640px) {
  .settings-dropdown {
    min-width: 200px;
    right: auto;
    left: 50%;
    transform: translateX(-50%);
  }
}

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

/* Sipariş silme butonu */
.btn-del-order {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--muted);
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 13px;
  cursor: pointer;
  transition: all .15s;
  flex-shrink: 0;
}
.btn-del-order:hover {
  background: var(--danger);
  color: #fff;
  border-color: var(--danger);
}

/* Sipariş toplam bar */
.orders-total {
  position: sticky;
  bottom: 0;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 18px;
  margin-top: 14px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  box-shadow: 0 -2px 12px rgba(0,0,0,.06);
  font-size: 15px;
}
.orders-total .total-label { font-weight: 500; color: var(--muted); }
.orders-total .total-amount { font-size: 22px; font-weight: 700; color: var(--primary-dark); }
.orders-total .total-count { font-size: 13px; color: var(--muted); margin-left: 8px; }

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
    <div class="brand"><img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACIAAAAiCAYAAAA6RwvCAAAIpElEQVR4nL2Xa4xdVRXHf2vvc869M9O309YW+pq2UDqdR7kk8+6VxJgJFUswU4pR8IHhgwkhwW+Kk4YYSUwUNdEPBCVGk5pRULClJBB6nTJtaQfamXZSCxba0k4f0JnO6957Hnv54c7Yh32Jyj/Zycnae6/1P3s99l7wH6Cjo8N2dHRYgEwm4wO0NtY+3dpY+/SlskvX/T8gUx/ZbNYDaGmsfSjbtFbXNa3Vlsbahy6du3LPTSu/DgzgmpvXLjROfwg8v3P3/lxb25r1EvovJ4FTg2JCK6Fx9+7adWBra2N9Fvi6M/K9np53Tk3p+J8R8dCTqODUdatSbxMzveqxY/r68CKW/NYINhlFZL81pk1RYuSWmyVibkBCstmLa1ysoxLEaoxpiya86Wu++b4uXnteTq0I5PC9ngZFpltj2iLrVGM3OrVvUsd1f9q73iSguRxxJ5g3KycYPVbuVa4ZleX3no7HBlN2UXZYhkd8grzjozt9OWgCzVdKsrg78SoHYm9sBnSC2ZwjvoGdi0Q6Ojrs2bNnBeDcuXOmuro6OXn88HpJxNu8r++Fu2PjA6gTKldP2NnLJySaALGKGoEinK23go+VXAJAOsHfDK7lrtr78YgWLrp926FDh+zcuXMdwLx587Srqyu5jMiUYAoDAwO0NNY9GJTZB9qaaj63Znl+z95TaQMQ5yEuGLzAIUXQWMCCyYNORYJgzq2Zf7qpULPO97w/RmGypaur62XgMjtXnoi0NtY9LiIWLzkn4s4bIyNJqM1xiCh2W19f2eMoYyKkxACiSBoG/1BJ5bBwcoOiY6ACIoAyVrn/9Nc8tc8QqailpfFz9Vmcm2ETM8c45qpqsnP3gWcA9TrBbAYH+nAqCOryYx6alDTFEcxcNqbhSFCRFOyziMaqCnIx8MYH05SPK5fEoqgqapjphzzrApioUJ15isV47DBYnAGp8CgWiweAn3aC8XZks4ZcziGyLwyT6qXtg3FQ4fzCsCcT51JSdc8ZSYpG9z2zHJzx5IrYN76iV4S8ChgnXuJUD90X4HxkUU+s+dmi0XTU5omW7Ys9RPYB7MhmjTfpnsRYd8QVAq+yepRbmsdsNA7GQjgGfgVy12NH2fPjFSSxXJ6IOjn+dR5g4tLEwQdSMrzKQkE59HBKSBDKYGZfrCt2O6/oc2TyKD2Ty+UKgIoQagIjx8olGhcK5y3jZzxsAMULwoKGETKPHcX4Csk1ioIADpwHBzcGDFVbzLiCD/7HDjuiSB6mDargFLGEgOZyuYLX0lD7hDGmwzlXF4WOibMpawMlmrC8uXkVqzpOUnXPxxSGhIUNw8yqGicucs3yJBG82+4TzhLMqOKmC7d0RyzJxfR+O0XRE8qH1LoogUR+1NZUv8k512UETorRBlfw0re2feRWbRykMCRUfDakqv0Mb/+yin9srSQ9S4kmhPScCLlOPVaBcIZgipRI/C3ijhcjjrd4FGeXyH3weZ9T9dYFoaQRGgROmp17+rbE6taZwE0MHZ5pht4r02C6khSEVRvPUPfIB7zzq2W8+9Jc0rMUF974epIY3DRh0RsRd/w5YuD+gA/v9pEQXLlQ8WGinzmGSXwmEpJ1O/f0bTGZTMbv6envVptsiMa9od6frdDBt2aoV64UhoWVG85R/+j7HHh2KUdenEdqpqLu2mREQSuExa9H3P5yxMGOgFOtHmZU0TTMGYi1dkukfl6HQk029PT0d2cyGd/09vZG2Ww2vXNn/2sahN/1AjFv/WS5G3qvHK+sRGbFFz/izu8cpe/XSzjcNZ9ghqJXZouWwkbLhaWvRqx8JaJ/U8DpxhIJl4JpJxw1vw+d8a0pWn1i9+7+17LZbLq3tzfyMpmMn8vlCuuaa9pQ+2RxXFxV+xkzbUERFwpileIFYVn7x4hVen++nCQy3PHAICpcTF8BVwZVWyOWvRHR/2DA2YyHjGmpgEVQqDScaPbMkl2J88rkB83NNe/lcrnuTCbje729vdG65povqXpbkrwtW7XpuN624ay4eDIztPS7SQFWrD+PX57Q+4vl4KDmkUHEaIlQCqpeiFm0K+adb6Q4X29hrOQOFNRArHB0fSDhtIjbXkuWmpR9tbm5ZlNPT+9L0tJU932DPIWo1nzrfb21acTEBeHCB2n6n1+COkGk5AN1gleWMHK8nMKQz+yVY8TjloLzSNIw80NHYYYwPk/wiiXjU25TC39f7zO+0KCBMKc/dqv/EolBJEGf9EQJFGJjxIvGPOeS0tUeTI+ZX38Bd0UlVQdz14wgBpwznNs7gwthwHCNcKbWIg5MxL9VX7UQVUiJXKL4E6pixGhCrBAIQHNzTZt19ndJPli8oPW0Vn/llPgVMTZ1rWIBLgF/Jux/ajF7Rudz9FELo4Dl2m+xUPHysPzVUBf3IcVAj8cm+WpPT3+3yWazXk9Pf7dJwgZ/RmHbiTfm0/ebW51NTb07rjKKkISQ5EvuEi0ZIZoc4VVGQSGAlX8N3aK3HRPlbls+CRt6evq7s9ms5+Vyubi9vT21ffv20+vWVT8XlOsXvLIkHj2Z8uK8lArD1eAEW66EBWP9UCk74xLJlyrr1SAKmoYkRaxp48WaPLd378DpSdtFAejsxAwMdMjgiSO706ngrkRjNLnRu7qExJUeXJ65ifUCiUBgLIViuG/BotsaV6/u0s2bccJkkmaz2XRcGH7SGClzrvTeu55qFTGq6gS+DKDwJxExonrdtqGk2OCc5r30rKcmb/+bbsSuidbG2q2tjbVb/1s9lzG5ol28LsrKymw+n0/iwtB2AC89u31KdrM6crncDduMG2KqyW5pqHulpaHulUtlnzYEoLWhrr+1oa7/Utknwc2lxhXoLO3TpqY1q0SoFqG6qWnNKkA7P6HOT7RpRzZrAKyajcZaMdaKh+m4dO5TIZLL5ZLOToyqbFBVVBXn5L7OTkwul7vpYL0U/wTrGjWAkZoLswAAAABJRU5ErkJggg==" alt="" class="brand-icon"> Çınar Mutfak</div>
    <div class="user-area">
      <span id="user-role" class="role-badge"></span>
      <button id="btn-notifications" class="btn-icon notif-btn" title="Bildirimler">🔔<span id="notif-badge" class="notif-badge hidden">0</span></button>
      <div class="settings-wrapper">
        <button id="btn-settings" class="btn-icon btn-settings" title="Ayarlar">⚙️</button>
        <div id="settings-dropdown" class="settings-dropdown hidden">
          <button class="settings-item" data-action="users">👥 Kullanıcılar</button>
          <button class="settings-item" data-action="password">🔑 Şifre Değiştir</button>
          <button class="settings-item" data-action="backup">💾 Yedek Al</button>
          <button class="settings-item" data-action="restore">↺ Yedek Yükle</button>
          <button class="settings-item" data-action="errors">⚠️ Hata Raporları<span id="err-badge-dd" class="notif-badge hidden">0</span></button>
        </div>
      </div>
      <button id="btn-logout" class="btn-icon btn-logout" title="Çıkış Yap"><span class="btn-label">Çıkış Yap</span></button>
    </div>
  </header>

  <div class="toolbar">
    <input type="search" id="search" class="input" placeholder="Malzeme ara...">
    <select id="category-filter" class="input"></select>
    <button id="btn-history" class="btn">Hareketler</button>
    <button id="btn-orders" class="btn">📦 Siparişler</button>
    <button id="btn-new-item" class="btn btn-primary hidden">＋ Malzeme Ekle</button>
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
      <span class="role-badge" id="orders-role-badge"></span>
    </div>
  </header>

  <div class="toolbar">
    <h2 style="font-size:18px;flex:1">📦 Sipariş Geçmişi</h2>
    <button id="btn-new-order" class="btn btn-primary">＋ Yeni Sipariş</button>
  </div>

  <div id="orders-list" class="users-list"></div>
  <div id="orders-total" class="orders-total hidden"></div>
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
  // Settings dropdown visibility (password/backup/restore/errors/users for admin)
  $("#btn-settings").classList.toggle("hidden", !isAdmin);
  // Toggle error badge in dropdown
  const errBadge = $("#err-badge-dd");
  if (errBadge) {
    if (state.errorCount > 0) {
      errBadge.textContent = state.errorCount;
      errBadge.classList.remove("hidden");
    } else {
      errBadge.classList.add("hidden");
    }
  }
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
  const badge = $("#orders-role-badge");
  if (state.user) {
    badge.textContent = state.user.role === "admin" ? "Yönetici" : "Görüntüleyici";
    badge.className = "role-badge " + state.user.role;
  }
  await loadOrdersToScreen();
}

async function loadOrdersToScreen() {
  const { orders } = await api("/api/orders");
  state.orders = orders;
  const isAdmin = state.user?.role === "admin";
  const rows = orders.map((o) => {
    const price = o.item_price > 0 ? o.quantity * o.item_price : 0;
    return `
    <div class="order-card">
      <div class="order-card-left">
        <div class="order-icon">${itemIcon({ image: o.item_image, name: o.item_name })}</div>
        <div>
          <div class="order-name">${esc(o.item_name)} <span style="font-size:12px;color:var(--muted);font-weight:400">— ${esc(o.user_name)}</span></div>
          <div class="order-meta">${fmt(o.quantity)} ${esc(o.unit)}</div>
          <div class="order-time">${esc(o.ordered_at)}</div>
        </div>
      </div>
      <div class="order-card-right">
        ${price > 0 ? `<div class="order-price">₺${fmt(price)}</div>` : ""}
        ${isAdmin ? `<button class="btn-del-order" data-del-order="${o.id}" title="Sipariş sil">🗑</button>` : ""}
      </div>
    </div>`;
  }).join("");
  $("#orders-list").innerHTML = rows || `<div class="empty">Henüz sipariş yok.</div>`;

  const total = orders.reduce((sum, o) => sum + (o.item_price > 0 ? o.quantity * o.item_price : 0), 0);
  const totalEl = $("#orders-total");
  if (orders.length > 0) {
    totalEl.classList.remove("hidden");
    totalEl.innerHTML = `
      <div>
        <span class="total-label">Toplam Maliyet</span>
        <span class="total-count">${orders.length} sipariş</span>
      </div>
      <div class="total-amount">₺${fmt(total)}</div>
    `;
  } else {
    totalEl.classList.add("hidden");
  }
}

$("#btn-orders").addEventListener("click", openOrdersScreen);
$("#btn-back-orders").addEventListener("click", () => {
  $("#orders-screen").classList.add("hidden");
  $("#app-screen").classList.remove("hidden");
});
$("#btn-new-order").addEventListener("click", openNewOrderModal);

// Delete order from orders list
$("#orders-list").addEventListener("click", async (e) => {
  const delBtn = e.target.closest("[data-del-order]");
  if (!delBtn) return;
  const oid = Number(delBtn.dataset.delOrder);
  if (!confirm("Bu siparişi silmek istiyor musunuz?")) return;
  try {
    await api(`/api/orders/${oid}`, { method: "DELETE" });
    toast("Sipariş silindi", "success");
    loadOrdersToScreen();
  } catch (err) { /* toast hata zaten gösterildi */ }
});

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

// ---------- Settings dropdown ----------
$("#btn-settings").addEventListener("click", (e) => {
  e.stopPropagation();
  $("#settings-dropdown").classList.toggle("hidden");
});

// Close dropdown when clicking outside
document.addEventListener("click", () => {
  $("#settings-dropdown").classList.add("hidden");
});

// Settings dropdown item handlers
$("#settings-dropdown").addEventListener("click", (e) => {
  const item = e.target.closest(".settings-item");
  if (!item) return;
  const action = item.dataset.action;
  $("#settings-dropdown").classList.add("hidden");
  if (action === "users") {
    openUsersScreen();
  } else if (action === "password") {
    openPasswordModal(state.user.id, state.user.name);
  } else if (action === "backup") {
    doBackup();
  } else if (action === "restore") {
    $("#restore-file").click();
  } else if (action === "errors") {
    openErrors();
  }
});

// ---------- Backup ----------
async function doBackup() {
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
  } catch (err) {
    toast("Backup sırasında hata oluştu", "error");
  }
}

// ---------- Restore ----------
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
    await loadItems();
    state.user = (await api("/api/me", { silent: true })).user;
    enterApp();
  } catch (err) {
    toast("Restore sırasında hata oluştu", "error");
    e.target.value = "";
  }
});

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


@app.delete("/api/orders/<int:oid>")
@admin_required
def api_delete_order(oid):
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    if order is None:
        return jsonify(error="Sipariş bulunamadı"), 404
    db.execute("DELETE FROM orders WHERE id = ?", (oid,))
    db.commit()
    auto_sync_backup(get_db())
    return jsonify(message="Sipariş silindi")


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
