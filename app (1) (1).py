"""
Mutfak Stok Uygulaması — Flask + SQLite
Aile üyelerinin aynı verileri gördüğü, yöneticilerin stok rakamlarını değiştirdiği
bir stok takip uygulaması.
"""
import os
import sqlite3
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
    # migration: existing databases get the image column
    cols = [r[1] for r in d.execute("PRAGMA table_info(items)").fetchall()]
    if "image" not in cols:
        d.execute("ALTER TABLE items ADD COLUMN image TEXT")
    row = d.execute("SELECT COUNT(*) FROM users").fetchone()
    if row[0] == 0:
        d.execute(
            "INSERT INTO users (username, name, password_hash, role, created_at) VALUES (?,?,?,?,?)",
            ("admin", "Yönetici", generate_password_hash("admin123"), "admin",
             datetime.now().isoformat(timespec="seconds")),
        )
    d.commit()
    d.close()


def log_movement(item_id, user, amount, note=""):
    db = get_db()
    db.execute(
        "INSERT INTO movements (item_id, user_id, user_name, amount, note, created_at) VALUES (?,?,?,?,?,?)",
        (item_id, user["id"], user["name"], amount, note,
         datetime.now().isoformat(timespec="seconds")),
    )


def fmt_num(n):
    return float(round(float(n), 2))


def notify_low_stock(item_id, new_qty, old_qty):
    """When an item crosses below its threshold, notify every user once."""
    db = get_db()
    item = db.execute(
        "SELECT name, unit, threshold FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if item is None:
        return
    threshold = float(item["threshold"] or 0)
    if new_qty <= 0 or new_qty > threshold:
        return
    if old_qty is not None and old_qty <= threshold:
        return  # already below — no repeated spam
    msg = (f"{item['name']} stok miktarı {fmt_num(new_qty)} "
           f"{item['unit']}'a düştü (eşik: {fmt_num(threshold)})")
    now = datetime.now().isoformat(timespec="seconds")
    for u in db.execute("SELECT id FROM users").fetchall():
        db.execute(
            "INSERT INTO notifications (user_id, item_id, message, created_at, read) "
            "VALUES (?,?,?,?,0)",
            (u["id"], item_id, msg, now),
        )


def user_json(row):
    return {"id": row["id"], "username": row["username"],
            "name": row["name"], "role": row["role"]}


HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mutfak Stok Uygulaması</title>
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
.auth-card h1 { text-align: center; font-size: 24px; margin-top: 4px; }
.subtitle { text-align: center; color: var(--muted); font-size: 14px; margin: 4px 0 20px; }

.auth-tabs { display: flex; gap: 8px; margin-bottom: 18px; }
.auth-tab {
  flex: 1;
  padding: 10px;
  border: 1px solid var(--border);
  background: var(--bg);
  border-radius: 8px;
  cursor: pointer;
  font-size: 14px;
  color: var(--muted);
}
.auth-tab.active { background: var(--primary); color: #fff; border-color: var(--primary); }

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
}
input:focus, select:focus { outline: 2px solid var(--primary); border-color: transparent; }

.btn {
  padding: 10px 16px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fff;
  cursor: pointer;
  font-size: 14px;
  color: var(--text);
  transition: filter .15s;
}
.btn:hover { filter: brightness(.95); }
.btn-primary { background: var(--primary); border-color: var(--primary); color: #fff; }
.btn-primary:hover { background: var(--primary-dark); }
.btn-block { width: 100%; margin-top: 6px; }
.btn-ghost { background: transparent; }
.btn-danger { background: #fff; border-color: var(--danger); color: var(--danger); }
.btn-sm { padding: 6px 10px; font-size: 13px; }

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
.brand { font-size: 20px; font-weight: 700; }
.brand-icon { margin-right: 6px; }
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

/* ---------------- Movements / users ---------------- */
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
.notif-btn { font-size: 16px; line-height: 1; }
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
    <div class="auth-logo">🧺</div>
    <h1>Mutfak Stok</h1>
    <p class="subtitle">Aile stok takip uygulaması</p>

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
    <div class="brand"><span class="brand-icon">🧺</span> Mutfak Stok</div>
    <div class="user-area">
      <div class="user-info">
        <span id="user-name"></span>
        <span id="user-role" class="role-badge"></span>
      </div>
      <button id="btn-notifications" class="btn btn-ghost notif-btn" title="Bildirimler">🔔<span id="notif-badge" class="notif-badge hidden">0</span></button>
      <button id="btn-password" class="btn btn-ghost">🔑 Şifre</button>
      <button id="btn-logout" class="btn btn-ghost">Çıkış Yap</button>
    </div>
  </header>

  <div class="toolbar">
    <input type="search" id="search" class="input" placeholder="Malzeme ara...">
    <select id="category-filter" class="input"></select>
    <button id="btn-history" class="btn">Hareketler</button>
    <button id="btn-users" class="btn hidden">Kullanıcılar</button>
    <button id="btn-new-item" class="btn btn-primary hidden">+ Malzeme Ekle</button>
  </div>

  <div id="stats" class="stats"></div>
  <div id="items-grid" class="items-grid"></div>
  <div id="empty" class="empty hidden">Henüz malzeme eklenmedi.</div>
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
  $("#user-name").textContent = state.user.name;
  const roleEl = $("#user-role");
  roleEl.textContent = state.user.role === "admin" ? "Yönetici" : "Görüntüleyici";
  roleEl.className = "role-badge " + state.user.role;
  const isAdmin = state.user.role === "admin";
  $("#btn-new-item").classList.toggle("hidden", !isAdmin);
  $("#btn-users").classList.toggle("hidden", !isAdmin);
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

function renderStats() {
  const low = state.items.filter((it) => it.quantity <= it.threshold && it.quantity > 0).length;
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
    const low = it.quantity <= it.threshold && it.quantity > 0;
    const zero = it.quantity <= 0;
    const cls = zero ? "zero" : low ? "low" : "";
    const exp = expiryBadge(it.expiry);
    return `
      <div class="item-card ${cls}">
        <div class="item-top">
          <div>
            <div class="item-media">${itemIcon(it)}</div>
            <div class="item-name">${esc(it.name)}</div>
            ${it.category ? `<div class="item-cat">${esc(it.category)}</div>` : ""}
          </div>
          ${exp}
        </div>
        <div class="item-qty">${fmt(it.quantity)}<small>${esc(it.unit)}</small></div>
        <div class="item-actions">
          <button class="btn btn-sm btn-plus" data-id="${it.id}">+</button>
          <button class="btn btn-sm btn-minus" data-id="${it.id}">−</button>
          ${state.user.role === "admin" ? `
            <button class="btn btn-sm btn-edit" data-id="${it.id}">Düzenle</button>
            <button class="btn btn-sm btn-danger btn-del" data-id="${it.id}">Sil</button>` : ""}
        </div>
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

/* ---------- Kullanıcılar (yönetici) ---------- */
async function openUsers() {
  const { users } = await api("/api/users");
  const rows = users.map((u) => `
    <div class="user-row">
      <div>
        <div class="u-name">${esc(u.name)} ${u.id === state.user.id ? "<small>(sizin)</small>" : ""}</div>
        <div class="u-username">@${esc(u.username)}</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <span class="role-badge ${u.role}">${u.role === "admin" ? "Yönetici" : "Görüntüleyici"}</span>
        ${u.role === "admin"
          ? `<button class="btn btn-sm btn-danger" data-role="viewer" data-uid="${u.id}">Yönetici değil</button>`
          : `<button class="btn btn-sm" data-role="admin" data-uid="${u.id}">Yönetici Yap</button>`}
        <button class="btn btn-sm" data-passwd="${u.id}" data-name="${esc(u.name)}">Şifre Değiştir</button>
        ${u.id !== state.user.id ? `<button class="btn btn-sm btn-danger" data-del-user="${u.id}">Sil</button>` : ""}
      </div>
    </div>`).join("");
  openModal(`<button class="modal-close" data-close>×</button><h2>Kullanıcılar</h2>
    <div class="move-list">${rows}</div>`);
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
      <label>Kategori <input name="category" value="${esc(item?.category || "")}" placeholder="ör. Süt ürünleri"></label>
      <label>Son Kullanma Tarihi <input name="expiry" type="date" value="${esc(item?.expiry || "")}"></label>
      <label>Düşük stok eşiği <input name="threshold" type="number" step="0.01" value="${item?.threshold ?? 0}"></label>
      <label>İkon (emoji, isteğe bağlı) <input name="emoji" value="${esc(emojiVal)}" placeholder="ör. 🧀, 🥛, 🥦"></label>
      <label>Küçük görsel (isteğe bağlı) <input name="image" type="file" accept="image/*"></label>
      <div id="img-preview" class="img-preview"></div>
      <button type="submit" class="btn btn-primary btn-block">Kaydet</button>
    </form>`);

  const fileInput = $("#item-form").querySelector('[name="image"]');
  const emojiInput = $("#item-form").querySelector('[name="emoji"]');
  const preview = $("#img-preview");

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
  }
});

$("#btn-new-item").addEventListener("click", () => openItemForm(null));
$("#btn-history").addEventListener("click", openMovements);
$("#btn-users").addEventListener("click", openUsers);

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
            "INSERT INTO users (username, name, password_hash, role, created_at) VALUES (?,?,?,?,?)",
            (username, name, generate_password_hash(password), "viewer",
             datetime.now().isoformat(timespec="seconds")),
        )
        db.commit()
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
    category = (d.get("category") or "").strip()
    expiry = (d.get("expiry") or "").strip() or None
    image = (d.get("image") or "").strip()
    db = get_db()
    cur = db.execute(
        "INSERT INTO items (name, unit, quantity, category, expiry, threshold, image, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (name, unit, quantity, category, expiry, threshold, image,
         datetime.now().isoformat(timespec="seconds")),
    )
    item_id = cur.lastrowid
    if quantity != 0:
        log_movement(item_id, current_user(), quantity, "İlk stok")
    notify_low_stock(item_id, quantity, None)
    db.commit()
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
    db.execute(
        "UPDATE items SET name=?, unit=?, quantity=?, category=?, expiry=?, threshold=?, image=? WHERE id=?",
        (name, (d.get("unit") or row["unit"]).strip() or "adet", new_qty,
         (d.get("category") or "").strip(), (d.get("expiry") or "").strip() or None,
         float(d.get("threshold") if "threshold" in d else row["threshold"]),
         (d.get("image") if "image" in d else row["image"]) or "", item_id),
    )
    if "quantity" in d and new_qty != row["quantity"]:
        log_movement(item_id, current_user(), new_qty - row["quantity"], "Düzenleme")
        notify_low_stock(item_id, new_qty, row["quantity"])
    db.commit()
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
    return jsonify(message="Stok güncellendi")


@app.get("/api/movements")
@login_required
def api_movements():
    limit = min(int(request.args.get("limit", 100)), 500)
    rows = get_db().execute(
        "SELECT m.*, i.name AS item_name, i.unit AS item_unit FROM movements m "
        "JOIN items i ON i.id = m.item_id "
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
    return jsonify(message="Tüm bildirimler okundu")


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
    return jsonify(message="Rol güncellendi")


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
    return jsonify(message="Kullanıcı silindi")


# ---------------------------------------------------------------- Frontend

@app.get("/")
def index():
    return HTML


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
