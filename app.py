import io
import os
import re
import time
import calendar
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import wraps, lru_cache
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from flask import Flask, jsonify, request, send_file, abort, session, redirect, Response, g
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
CURRENCY = os.environ.get("CURRENCY", "EUR")
LOCALE = os.environ.get("LOCALE", "es-ES")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Andorra"))
INVITE_CODE = os.environ.get("INVITE_CODE", "").strip()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("SECURE_COOKIES")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=90),
)


def today():
    return datetime.now(TZ).date()


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)


# =====================================================================
# Base de datos
# =====================================================================
def init_db():
    with db() as conn:
        conn.execute("SELECT pg_advisory_lock(771001)")
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS categories (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    icon TEXT NOT NULL DEFAULT '💸',
                    color TEXT NOT NULL DEFAULT '#7c5cff',
                    kind TEXT NOT NULL CHECK (kind IN ('expense','income')),
                    budget NUMERIC(12,2),
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    id SERIAL PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('expense','income')),
                    amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
                    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
                    note TEXT DEFAULT '',
                    tx_date DATE NOT NULL DEFAULT CURRENT_DATE,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(tx_date);
                CREATE TABLE IF NOT EXISTS recurring (
                    id SERIAL PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('expense','income')),
                    amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
                    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
                    note TEXT DEFAULT '',
                    day SMALLINT NOT NULL CHECK (day BETWEEN 1 AND 31),
                    start_date DATE NOT NULL DEFAULT CURRENT_DATE,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS recurring_runs (
                    recurring_id INTEGER REFERENCES recurring(id) ON DELETE CASCADE,
                    month DATE NOT NULL,
                    PRIMARY KEY (recurring_id, month)
                );
                ALTER TABLE transactions
                    ADD COLUMN IF NOT EXISTS recurring_id INTEGER REFERENCES recurring(id) ON DELETE SET NULL;
                ALTER TABLE recurring
                    ADD COLUMN IF NOT EXISTS every SMALLINT NOT NULL DEFAULT 1 CHECK (every BETWEEN 1 AND 12);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                -- Multiusuario: cada dato pertenece a un usuario
                ALTER TABLE categories   ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
                ALTER TABLE transactions ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
                ALTER TABLE recurring    ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
                CREATE INDEX IF NOT EXISTS idx_tx_user_date ON transactions(user_id, tx_date);
                CREATE INDEX IF NOT EXISTS idx_cat_user ON categories(user_id);
                CREATE INDEX IF NOT EXISTS idx_rec_user ON recurring(user_id);
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    key TEXT NOT NULL,
                    value TEXT,
                    PRIMARY KEY (user_id, key)
                );

                -- Metas de ahorro
                CREATE TABLE IF NOT EXISTS goals (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    icon TEXT NOT NULL DEFAULT '🎯',
                    color TEXT NOT NULL DEFAULT '#2ecc8f',
                    target NUMERIC(12,2) NOT NULL CHECK (target > 0),
                    deadline DATE,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS goal_moves (
                    id SERIAL PRIMARY KEY,
                    goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    amount NUMERIC(12,2) NOT NULL CHECK (amount <> 0),
                    move_date DATE NOT NULL DEFAULT CURRENT_DATE,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_moves_user ON goal_moves(user_id, move_date);

                -- Administración y seguridad de sesiones
                -- Subcategorías: un nivel, heredan el color de la principal
                ALTER TABLE categories ADD COLUMN IF NOT EXISTS parent_id INTEGER REFERENCES categories(id) ON DELETE SET NULL;
                CREATE INDEX IF NOT EXISTS idx_cat_parent ON categories(parent_id);
                ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_pw BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 1;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ;
                UPDATE users SET is_admin=TRUE
                    WHERE id=(SELECT MIN(id) FROM users) AND NOT EXISTS (SELECT 1 FROM users WHERE is_admin);
            """)
            # Migración única: reparte la paleta nueva para que ninguna categoría repita color
            done = conn.execute("SELECT 1 FROM app_meta WHERE key='palette_v2'").fetchone()
            if not done:
                cats = conn.execute("SELECT id, user_id FROM categories ORDER BY user_id NULLS FIRST, id").fetchall()
                counters = {}
                for c in cats:
                    i = counters.get(c["user_id"], 0)
                    conn.execute("UPDATE categories SET color=%s WHERE id=%s", (PALETTE[i % PALETTE_SIZE], c["id"]))
                    counters[c["user_id"]] = i + 1
                conn.execute("INSERT INTO app_meta(key,value) VALUES ('palette_v2','1') ON CONFLICT DO NOTHING")
            if not os.environ.get("SECRET_KEY"):
                conn.execute(
                    "INSERT INTO app_meta(key,value) VALUES ('secret_key',%s) ON CONFLICT DO NOTHING",
                    (secrets.token_hex(32),),
                )
        finally:
            conn.execute("SELECT pg_advisory_unlock(771001)")


def load_secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    with db() as conn:
        row = conn.execute("SELECT value FROM app_meta WHERE key='secret_key'").fetchone()
    return row["value"]


def claim_orphan_data(conn, uid):
    """Los datos creados antes de haber cuentas pasan al primer usuario."""
    for t in ("categories", "transactions", "recurring"):
        conn.execute(f"UPDATE {t} SET user_id=%s WHERE user_id IS NULL", (uid,))
    old = conn.execute("SELECT value FROM settings WHERE key='monthly_limit'").fetchone()
    if old and old["value"]:
        conn.execute(
            "INSERT INTO user_settings(user_id,key,value) VALUES (%s,'monthly_limit',%s) ON CONFLICT DO NOTHING",
            (uid, old["value"]),
        )


# =====================================================================
# Utilidades
# =====================================================================
def occurrence(y, m, day):
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


def next_month(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def prev_month(y, m):
    return (y - 1, 12) if m == 1 else (y, m - 1)


def add_months(y, m, n):
    k = y * 12 + (m - 1) + n
    return k // 12, k % 12 + 1


def anchor(r):
    """Primer mes en que toca: el de inicio, o el siguiente si ese día ya había pasado."""
    sd = r["start_date"]
    if occurrence(sd.year, sd.month, r["day"]) >= sd:
        return sd.year, sd.month
    return next_month(sd.year, sd.month)


def due_in_month(r, y, m):
    """¿Le toca a este recurrente el mes y/m? (cada `every` meses desde el primero)"""
    ay, am = anchor(r)
    idx = (y * 12 + m) - (ay * 12 + am)
    return idx >= 0 and idx % (r.get("every") or 1) == 0


def num(v):
    return float(v) if isinstance(v, Decimal) else v


def clean(rows):
    return [{k: (v.isoformat() if isinstance(v, date) else num(v)) for k, v in r.items()} for r in rows]


def month_range(ym):
    y, m = map(int, ym.split("-"))
    start = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return start, end


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM app_meta WHERE key=%s", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO app_meta(key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        (key, value),
    )


# ---------- Ajustes por usuario ----------
CURRENCIES = ("EUR", "USD", "GBP", "CHF", "MXN", "ARS", "COP", "CLP", "PEN")
DEFAULT_SETTINGS = {"currency": CURRENCY, "month_start": 1, "carry_over": False, "theme": "auto"}


def user_settings(conn, u):
    rows = conn.execute(
        "SELECT key,value FROM user_settings WHERE user_id=%s AND key IN ('currency','month_start','carry_over','theme')",
        (u,),
    ).fetchall()
    st = dict(DEFAULT_SETTINGS)
    for r in rows:
        if r["key"] == "month_start":
            st["month_start"] = int(r["value"])
        elif r["key"] == "carry_over":
            st["carry_over"] = r["value"] == "1"
        else:
            st[r["key"]] = r["value"]
    return st


# ---------- Periodos (mes que empieza el día que elija el usuario) ----------
def period_range(ym, s=1):
    """Periodo 'ym' = desde el día s de ese mes hasta el día s del mes siguiente (sin incluir)."""
    y, m = map(int, ym.split("-"))
    ny, nm = next_month(y, m)
    return date(y, m, s), date(ny, nm, s)


def current_period_key(s=1, t=None):
    t = t or today()
    y, m = (t.year, t.month) if t.day >= s else prev_month(t.year, t.month)
    return f"{y:04d}-{m:02d}"


def shift_key(ym, n):
    y, m = map(int, ym.split("-"))
    y, m = add_months(y, m, n)
    return f"{y:04d}-{m:02d}"


def body():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


# ---------- Validación de entradas ----------
class BadInput(ValueError):
    pass


MAX_AMOUNT = Decimal("9999999999.99")      # cabe en NUMERIC(12,2)
KEY_RE = re.compile(r"^(\d{4})-(\d{2})$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def parse_amount(v, allow_negative=False, msg="Importe inválido"):
    try:
        a = Decimal(str(v).replace(",", ".")).quantize(Decimal("0.01"))
    except Exception:
        raise BadInput(msg)
    if not a.is_finite() or abs(a) > MAX_AMOUNT or a == 0 or (a < 0 and not allow_negative):
        raise BadInput(msg)
    return a


def parse_optional_amount(v, msg="Importe inválido"):
    if v in (None, "", 0, "0"):
        return None
    return parse_amount(v, msg=msg)


def parse_date(v, default=None, msg="Fecha inválida"):
    if v in (None, ""):
        return default
    try:
        d = date.fromisoformat(str(v)[:10])
    except Exception:
        raise BadInput(msg)
    if not 1900 <= d.year <= 2999:
        raise BadInput(msg)
    return d


def parse_key(v, default):
    """Clave de periodo 'YYYY-MM'."""
    if not v:
        return default
    m = KEY_RE.match(str(v))
    if not m or not 1900 <= int(m.group(1)) <= 2999 or not 1 <= int(m.group(2)) <= 12:
        raise BadInput("Mes inválido")
    return v


def parse_color(v, default):
    if v in (None, ""):
        return default
    if not COLOR_RE.match(str(v)):
        raise BadInput("Color inválido")
    return str(v).lower()


def parse_icon(v, default):
    if v in (None, ""):
        return default
    v = str(v).strip()
    if not v or len(v) > 8 or any(ch in v for ch in "<>&\"'`=/\\") or any(ch.isalnum() and ch.isascii() for ch in v):
        raise BadInput("Icono inválido")
    return v


def parse_text(v, maxlen=500):
    return (str(v) if v is not None else "").strip()[:maxlen]


@app.errorhandler(BadInput)
def bad_input(e):
    return jsonify({"error": str(e) or "Datos inválidos"}), 400


@app.errorhandler(psycopg.errors.DataError)
def bad_data(e):
    return jsonify({"error": "Datos inválidos"}), 400


def own_category(conn, uid, cid):
    if not cid:
        return None
    row = conn.execute("SELECT id FROM categories WHERE id=%s AND user_id=%s", (cid, uid)).fetchone()
    return row["id"] if row else None


def materialize(conn, uid):
    """Crea los movimientos de los recurrentes cuyo día ya ha llegado (una sola vez por mes)."""
    t = today()
    rules = conn.execute("SELECT * FROM recurring WHERE active AND user_id=%s", (uid,)).fetchall()
    for r in rules:
        y, m = anchor(r)
        while (y, m) <= (t.year, t.month):
            d = occurrence(y, m, r["day"])
            if due_in_month(r, y, m) and r["start_date"] <= d <= t:
                with conn.transaction():
                    run = conn.execute(
                        "INSERT INTO recurring_runs(recurring_id,month) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING 1",
                        (r["id"], date(y, m, 1)),
                    ).fetchone()
                    if run:
                        conn.execute(
                            """INSERT INTO transactions(user_id,kind,amount,category_id,note,tx_date,recurring_id)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                            (uid, r["kind"], r["amount"], r["category_id"], r["note"], d, r["id"]),
                        )
            y, m = add_months(y, m, r["every"] or 1)


def pending_for_period(conn, uid, start, end):
    """Recurrentes del periodo [start, end) que todavía no se han cobrado/pagado."""
    t = today()
    rows = conn.execute(
        """SELECT r.id,r.kind,r.amount,r.note,r.day,r.every,r.start_date,r.category_id,
                  c.name AS category,c.icon,c.color
           FROM recurring r LEFT JOIN categories c ON c.id=r.category_id
           WHERE r.active AND r.user_id=%s""",
        (uid,),
    ).fetchall()
    runs = {(x["recurring_id"], x["month"]) for x in conn.execute(
        """SELECT x.recurring_id, x.month FROM recurring_runs x JOIN recurring r ON r.id=x.recurring_id
           WHERE r.user_id=%s AND x.month >= %s AND x.month <= %s""",
        (uid, date(start.year, start.month, 1), date(end.year, end.month, 1)),
    ).fetchall()}
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        for r in rows:
            d = occurrence(y, m, r["day"])
            if (start <= d < end and d > t and d >= r["start_date"] and due_in_month(r, y, m)
                    and (r["id"], date(y, m, 1)) not in runs):
                item = dict(r)
                item["due_date"] = d
                out.append(item)
        y, m = next_month(y, m)
    out.sort(key=lambda r: r["due_date"])
    return out


# =====================================================================
# Autenticación
# =====================================================================
def uid():
    return session.get("uid")


def load_user():
    """Comprueba que la sesión sigue siendo válida (usuario existe y no se ha reseteado la contraseña)."""
    if not uid():
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT id,username,is_admin,must_change_pw,session_version FROM users WHERE id=%s", (uid(),)
        ).fetchone()
        if row and row["session_version"] == session.get("sv", 1):
            conn.execute(
                "UPDATE users SET last_seen=now() WHERE id=%s AND (last_seen IS NULL OR last_seen < now() - INTERVAL '5 minutes')",
                (row["id"],),
            )
            return row
    session.clear()
    return None


# Con contraseña temporal solo se puede consultar la cuenta, cambiar la clave o salir
PW_CHANGE_ALLOWED = {"me", "change_password", "logout"}


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        g.user = load_user()
        if not g.user:
            return jsonify({"error": "no autenticado"}), 401
        if g.user["must_change_pw"] and request.endpoint not in PW_CHANGE_ALLOWED:
            return jsonify({"error": "Cambia tu contraseña temporal para continuar"}), 403
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        g.user = load_user()
        if not g.user:
            return jsonify({"error": "no autenticado"}), 401
        if g.user["must_change_pw"]:
            return jsonify({"error": "Cambia tu contraseña temporal para continuar"}), 403
        if not g.user["is_admin"]:
            return jsonify({"error": "Solo para administradores"}), 403
        return f(*a, **kw)
    return wrapper


def start_session(row):
    session.clear()
    session.permanent = True
    session["uid"] = row["id"]
    session["sv"] = row["session_version"]


@app.before_request
def csrf_guard():
    # Las llamadas que modifican datos deben venir de la propia app (cabecera personalizada)
    if request.path.startswith("/api/") and request.method in ("POST", "PUT", "DELETE"):
        if request.headers.get("X-Requested-With") != "fetch":
            return jsonify({"error": "petición no válida"}), 400


_fails = {}          # clave -> (intentos, bloqueado_hasta, ultimo_intento)
FAIL_TTL = 15 * 60   # se olvidan los intentos tras 15 min sin actividad


def _prune_fails():
    if len(_fails) < 500:
        return
    now = time.time()
    for k in [k for k, (_, until, last) in _fails.items() if until < now and now - last > FAIL_TTL]:
        _fails.pop(k, None)


def throttled(key):
    _, until, _ = _fails.get(key, (0, 0, 0))
    return until > time.time()


def register_fail(key, limit=5, block=60):
    _prune_fails()
    now = time.time()
    n, _, last = _fails.get(key, (0, 0, 0))
    n = 1 if now - last > FAIL_TTL else n + 1
    _fails[key] = (n, now + block if n >= limit else 0, now)


USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")


def invite_settings(conn):
    code = get_meta(conn, "invite_code", INVITE_CODE) or ""
    open_ = get_meta(conn, "registration_open", "1") == "1"
    return code, open_


@app.post("/api/auth/register")
def register():
    d = body()
    username = (d.get("username") or "").strip().lower()
    password = d.get("password") or ""
    rkey = f"reg:{request.remote_addr}"
    if throttled(rkey):
        return jsonify({"error": "Demasiados intentos. Espera unos minutos."}), 429
    register_fail(rkey, limit=10, block=600)   # cuenta cada intento de registro por IP
    with db() as conn:
        first = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
        code, open_ = invite_settings(conn)
        if not first:
            if not open_:
                return jsonify({"error": "El registro está cerrado"}), 403
            if code and (d.get("invite") or "").strip() != code:
                return jsonify({"error": "Código de invitación incorrecto"}), 403
        if not USERNAME_RE.match(username):
            return jsonify({"error": "Usuario: 3-32 caracteres (letras, números, . _ -)"}), 400
        if len(password) < 6:
            return jsonify({"error": "La contraseña debe tener al menos 6 caracteres"}), 400
        try:
            row = conn.execute(
                """INSERT INTO users(username,password_hash,is_admin) VALUES (%s,%s,%s)
                   RETURNING id,session_version""",
                (username, generate_password_hash(password), first),
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            return jsonify({"error": "Ese usuario ya existe"}), 409
        if first:
            claim_orphan_data(conn, row["id"])
    start_session(row)
    return jsonify({"ok": True})


@app.post("/api/auth/login")
def login():
    d = body()
    username = (d.get("username") or "").strip().lower()
    key = f"{request.remote_addr}:{username}"
    if throttled(key):
        return jsonify({"error": "Demasiados intentos. Espera un minuto."}), 429
    with db() as conn:
        row = conn.execute(
            "SELECT id,password_hash,session_version FROM users WHERE username=%s", (username,)
        ).fetchone()
    if not row or not check_password_hash(row["password_hash"], d.get("password") or ""):
        register_fail(key)
        time.sleep(0.4)
        return jsonify({"error": "Usuario o contraseña incorrectos"}), 401
    _fails.pop(key, None)
    start_session(row)
    return jsonify({"ok": True})


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
@login_required
def me():
    with db() as conn:
        st = user_settings(conn, g.user["id"])
    return jsonify({
        "username": g.user["username"],
        "is_admin": g.user["is_admin"],
        "must_change_pw": g.user["must_change_pw"],
        "settings": st,
        "current_period": current_period_key(st["month_start"]),
    })


@app.put("/api/me/password")
@login_required
def change_password():
    d = body()
    if len(d.get("new") or "") < 6:
        return jsonify({"error": "La nueva contraseña debe tener al menos 6 caracteres"}), 400
    with db() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id=%s", (uid(),)).fetchone()
        if not check_password_hash(row["password_hash"], d.get("current") or ""):
            return jsonify({"error": "La contraseña actual no es correcta"}), 403
        # Cierra las demás sesiones abiertas y mantiene esta
        new = conn.execute(
            """UPDATE users SET password_hash=%s, must_change_pw=FALSE, session_version=session_version+1
               WHERE id=%s RETURNING id,session_version""",
            (generate_password_hash(d["new"]), uid()),
        ).fetchone()
    start_session(new)
    return jsonify({"ok": True})


@app.put("/api/me/settings")
@login_required
def save_settings():
    d = body()
    vals = {}
    if "currency" in d:
        if d["currency"] not in CURRENCIES:
            return jsonify({"error": "Moneda no válida"}), 400
        vals["currency"] = d["currency"]
    if "month_start" in d:
        try:
            ms = int(d["month_start"])
            assert 1 <= ms <= 28
        except Exception:
            return jsonify({"error": "El día de inicio debe estar entre 1 y 28"}), 400
        vals["month_start"] = str(ms)
    if "carry_over" in d:
        vals["carry_over"] = "1" if d["carry_over"] else "0"
    if "theme" in d:
        if d["theme"] not in ("auto", "light", "dark"):
            return jsonify({"error": "Tema no válido"}), 400
        vals["theme"] = d["theme"]
    with db() as conn:
        for k, v in vals.items():
            conn.execute(
                """INSERT INTO user_settings(user_id,key,value) VALUES (%s,%s,%s)
                   ON CONFLICT (user_id,key) DO UPDATE SET value=EXCLUDED.value""",
                (g.user["id"], k, v),
            )
        st = user_settings(conn, g.user["id"])
    return jsonify({"settings": st, "current_period": current_period_key(st["month_start"])})


# =====================================================================
# Administración
# =====================================================================
@app.get("/api/admin/users")
@admin_required
def admin_users():
    with db() as conn:
        rows = conn.execute(
            """SELECT u.id,u.username,u.is_admin,u.must_change_pw,u.created_at,u.last_seen,
                      (SELECT COUNT(*) FROM transactions t WHERE t.user_id=u.id) AS tx_count
               FROM users u ORDER BY u.id"""
        ).fetchall()
        code, open_ = invite_settings(conn)
    out = []
    for r in rows:
        r = dict(r)
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        r["last_seen"] = r["last_seen"].isoformat() if r["last_seen"] else None
        r["is_me"] = r["id"] == g.user["id"]
        out.append(r)
    return jsonify({"users": out, "invite_code": code, "registration_open": open_})


def readable_password(n=10):
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


@app.post("/api/admin/users/<int:target>/reset")
@admin_required
def admin_reset_password(target):
    temp = readable_password()
    with db() as conn:
        row = conn.execute(
            """UPDATE users SET password_hash=%s, must_change_pw=TRUE, session_version=session_version+1
               WHERE id=%s RETURNING username""",
            (generate_password_hash(temp), target),
        ).fetchone()
    if not row:
        return jsonify({"error": "Usuario no encontrado"}), 404
    if target == g.user["id"]:
        session.clear()
    return jsonify({"username": row["username"], "temp_password": temp})


@app.put("/api/admin/users/<int:target>/admin")
@admin_required
def admin_toggle(target):
    if target == g.user["id"]:
        return jsonify({"error": "No puedes quitarte el admin a ti mismo"}), 400
    make = bool(body().get("is_admin"))
    with db() as conn:
        row = conn.execute("UPDATE users SET is_admin=%s WHERE id=%s RETURNING id", (make, target)).fetchone()
    if not row:
        return jsonify({"error": "Usuario no encontrado"}), 404
    return jsonify({"ok": True})


@app.delete("/api/admin/users/<int:target>")
@admin_required
def admin_delete_user(target):
    if target == g.user["id"]:
        return jsonify({"error": "No puedes borrar tu propia cuenta"}), 400
    with db() as conn:
        with conn.transaction():
            conn.execute(
                "DELETE FROM recurring_runs WHERE recurring_id IN (SELECT id FROM recurring WHERE user_id=%s)", (target,)
            )
            conn.execute("DELETE FROM transactions WHERE user_id=%s", (target,))
            conn.execute("DELETE FROM recurring WHERE user_id=%s", (target,))
            conn.execute("DELETE FROM categories WHERE user_id=%s", (target,))
            conn.execute("DELETE FROM users WHERE id=%s", (target,))
    return "", 204


@app.put("/api/admin/settings")
@admin_required
def admin_settings():
    d = body()
    with db() as conn:
        if "invite_code" in d:
            code = (d.get("invite_code") or "").strip()
            if code and len(code) < 4:
                return jsonify({"error": "El código debe tener al menos 4 caracteres"}), 400
            set_meta(conn, "invite_code", code)
        if "registration_open" in d:
            set_meta(conn, "registration_open", "1" if d["registration_open"] else "0")
        code, open_ = invite_settings(conn)
    return jsonify({"invite_code": code, "registration_open": open_})


# =====================================================================
# Páginas y app instalable
# =====================================================================
def page(name):
    for p in (f"templates/{name}", name):
        f = os.path.join(BASE_DIR, p)
        if os.path.isfile(f):
            resp = send_file(f)
            resp.headers["Cache-Control"] = "no-cache"
            return resp
    abort(404)


@app.route("/")
def index():
    if not uid():
        return redirect("/login")
    return page("index.html")


@app.route("/login")
def login_page():
    if uid():
        return redirect("/")
    return page("login.html")


@app.route("/manifest.webmanifest")
def manifest():
    resp = jsonify({
        "name": "Mis Gastos",
        "short_name": "Gastos",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0f1117",
        "theme_color": "#0f1117",
        "lang": "es",
        "icons": [
            {"src": "/icon/192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icon/512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/icon/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    })
    resp.headers["Content-Type"] = "application/manifest+json"
    return resp


# Icono de la app dibujado con código (no hace falta subir imágenes)
@lru_cache(maxsize=8)
def draw_icon(size, maskable=False):
    from PIL import Image, ImageDraw
    S = size * 4
    bg = Image.new("RGBA", (S, S))
    bd = ImageDraw.Draw(bg)
    for y in range(S):
        t = y / S
        bd.line([(0, y), (S, y)], fill=(int(141 - 99 * t), int(107 - 79 * t), int(255 - 133 * t), 255))
    mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(mask)
    if maskable:
        md.rectangle([0, 0, S, S], fill=255)
    else:
        md.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(bg, (0, 0), mask)
    d = ImageDraw.Draw(img)
    sc = 0.62 if maskable else 0.78
    r = S * sc / 2 * 0.78
    w = int(S * sc * 0.14)
    box = [S / 2 - r, S / 2 - r, S / 2 + r, S / 2 + r]
    d.arc(box, start=-90, end=150, fill=(255, 255, 255, 255), width=w)
    d.arc(box, start=165, end=255, fill=(46, 204, 143, 255), width=w)
    buf = io.BytesIO()
    img.resize((size, size), Image.LANCZOS).save(buf, "PNG", optimize=True)
    return buf.getvalue()


ICONS = {"192": (192, False), "512": (512, False), "maskable-512": (512, True), "apple": (180, False)}


@app.route("/icon/<name>.png")
def icon(name):
    if name not in ICONS:
        abort(404)
    resp = Response(draw_icon(*ICONS[name]), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


SW_JS = """// Service worker mínimo: permite instalar la app. Los datos siempre se piden al servidor.
const CACHE = 'gastos-v2';
const SHELL = ['/icon/192.png', '/icon/512.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(() => new Response(
      '<meta name="viewport" content="width=device-width,initial-scale=1"><body style="background:#0f1117;color:#eef0f6;font-family:sans-serif;display:grid;place-items:center;height:100vh;margin:0;text-align:center"><div><div style="font-size:48px">📡</div><p>Sin conexión.<br>Vuelve a intentarlo en un momento.</p></div></body>',
      { headers: { 'Content-Type': 'text/html; charset=utf-8' } })));
    return;
  }
  if (SHELL.includes(url.pathname)) {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
  }
});
"""


@app.route("/sw.js")
def service_worker():
    resp = Response(SW_JS, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.get("/api/config")
def config():
    with db() as conn:
        code, open_ = invite_settings(conn)
        first = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
    return jsonify({"currency": CURRENCY, "locale": LOCALE, "palette": PALETTE,
                    "invite_required": bool(code) and not first,
                    "registration_open": open_ or first})


# =====================================================================
# Categorías
# =====================================================================
CAT_COLS = "id,name,icon,color,kind,budget,parent_id"


@app.get("/api/categories")
@login_required
def list_categories():
    with db() as conn:
        rows = conn.execute(
            f"SELECT {CAT_COLS} FROM categories WHERE user_id=%s ORDER BY kind,name", (uid(),)
        ).fetchall()
    return jsonify(clean(rows))


# Paleta de categorías: 30 colores elegidos para diferenciarse al máximo entre sí.
# El orden importa: los primeros N son siempre los N más distintos, y se asignan en ese orden.
PALETTE = [
    "#ff5c7a", "#0024ff", "#18f000", "#008400", "#78ccff", "#cc18ff",
    "#9c240c", "#ffc06c", "#544890", "#18c09c", "#9c9000", "#ffc0e4",
    "#6c54ff", "#9c2478", "#a860c0", "#786000", "#ff60f0", "#f0840c",
    "#0078c0", "#e4009c", "#b46c60", "#c09cff", "#006060", "#e40000",
    "#9000cc", "#00b400", "#489cff", "#90e4b4", "#b4c000", "#3c9084",
]
PALETTE_SIZE = len(PALETTE)  # mientras queden colores libres no se puede repetir


def top_colors(conn, u, except_id=None):
    """Colores usados por las categorías principales del usuario (las subcategorías heredan y no cuentan)."""
    rows = conn.execute(
        "SELECT id, lower(color) AS color FROM categories WHERE user_id=%s AND parent_id IS NULL", (u,)
    ).fetchall()
    return {r["color"] for r in rows if r["id"] != except_id}


def color_taken(conn, u, color, except_id=None):
    """True si otra categoría principal ya usa ese color (y aún quedan colores libres)."""
    used = top_colors(conn, u, except_id)
    return bool(color) and len(used) < PALETTE_SIZE and color.lower() in used


def first_free_color(conn, u, except_id=None):
    used = top_colors(conn, u, except_id)
    return next((c for c in PALETTE if c not in used), PALETTE[0])


def valid_parent(conn, u, parent_id, kind, self_id=None):
    """Devuelve la categoría principal si es válida como padre (del usuario, del mismo tipo y de primer nivel)."""
    if not parent_id or parent_id == self_id:
        return None
    return conn.execute(
        "SELECT id,color FROM categories WHERE id=%s AND user_id=%s AND kind=%s AND parent_id IS NULL",
        (parent_id, u, kind),
    ).fetchone()


@app.post("/api/categories")
@login_required
def create_category():
    d = body()
    name = parse_text(d.get("name"), 60)
    if not name or d.get("kind") not in ("expense", "income"):
        return jsonify({"error": "Nombre y tipo requeridos"}), 400
    icon = parse_icon(d.get("icon"), "💸")
    budget = parse_optional_amount(d.get("budget"), "Límite inválido")
    with db() as conn:
        parent = None
        if d.get("parent_id"):
            parent = valid_parent(conn, uid(), d["parent_id"], d["kind"])
            if not parent:
                return jsonify({"error": "La categoría principal no es válida"}), 400
            color = parent["color"]          # la subcategoría hereda el color
        else:
            color = parse_color(d.get("color"), None) or first_free_color(conn, uid())
            if color_taken(conn, uid(), color):
                return jsonify({"error": "Ese color ya lo usa otra categoría"}), 409
        row = conn.execute(
            f"""INSERT INTO categories(user_id,name,icon,color,kind,budget,parent_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING {CAT_COLS}""",
            (uid(), name, icon, color, d["kind"],
             budget, parent["id"] if parent else None),
        ).fetchone()
    return jsonify(clean([row])[0]), 201


@app.put("/api/categories/<int:cid>")
@login_required
def update_category(cid):
    d = body()
    name = parse_text(d.get("name"), 60) if "name" in d else None
    if "name" in d and not name:
        return jsonify({"error": "El nombre no puede estar vacío"}), 400
    icon = parse_icon(d.get("icon"), None)
    new_color = parse_color(d.get("color"), None)
    budget = parse_optional_amount(d.get("budget"), "Límite inválido")
    with db() as conn:
        cur = conn.execute(
            "SELECT id,color,kind,parent_id FROM categories WHERE id=%s AND user_id=%s", (cid, uid())
        ).fetchone()
        if not cur:
            return jsonify({"error": "no encontrada"}), 404
        parent_id = d["parent_id"] if "parent_id" in d else cur["parent_id"]
        if parent_id:
            has_children = conn.execute("SELECT 1 FROM categories WHERE parent_id=%s LIMIT 1", (cid,)).fetchone()
            if has_children:
                return jsonify({"error": "Esta categoría tiene subcategorías; no puede ir dentro de otra"}), 400
            parent = valid_parent(conn, uid(), parent_id, cur["kind"], cid)
            if not parent:
                return jsonify({"error": "La categoría principal no es válida"}), 400
            color = parent["color"]
        else:
            color = new_color or cur["color"]
            if cur["parent_id"] and not new_color:
                color = first_free_color(conn, uid(), cid)      # deja de ser subcategoría: necesita color propio
            if color.lower() != (cur["color"] or "").lower() or cur["parent_id"]:
                if color_taken(conn, uid(), color, cid):
                    if cur["parent_id"]:
                        color = first_free_color(conn, uid(), cid)
                    else:
                        return jsonify({"error": "Ese color ya lo usa otra categoría"}), 409
        with conn.transaction():
            row = conn.execute(
                f"""UPDATE categories SET name=COALESCE(%s,name), icon=COALESCE(%s,icon),
                   color=%s, budget=%s, parent_id=%s WHERE id=%s AND user_id=%s
                   RETURNING {CAT_COLS}""",
                (name, icon, color, budget, parent_id or None, cid, uid()),
            ).fetchone()
            # las subcategorías siempre llevan el color de su principal
            conn.execute("UPDATE categories SET color=%s WHERE parent_id=%s", (color, cid))
    return jsonify(clean([row])[0])


@app.delete("/api/categories/<int:cid>")
@login_required
def delete_category(cid):
    with db() as conn:
        if conn.execute(
            "SELECT 1 FROM categories c WHERE c.parent_id=%s AND c.user_id=%s LIMIT 1", (cid, uid())
        ).fetchone():
            return jsonify({"error": "Primero mueve o elimina sus subcategorías"}), 400
        conn.execute("DELETE FROM categories WHERE id=%s AND user_id=%s", (cid, uid()))
    return "", 204


@app.put("/api/categories/<int:cid>/budget")
@login_required
def set_category_budget(cid):
    v = parse_optional_amount(body().get("budget"), "Límite inválido")
    with db() as conn:
        row = conn.execute(
            "UPDATE categories SET budget=%s WHERE id=%s AND user_id=%s AND kind='expense' RETURNING id",
            (v, cid, uid()),
        ).fetchone()
    if not row:
        return jsonify({"error": "no encontrada"}), 404
    return jsonify({"ok": True})


# =====================================================================
# Movimientos (+ búsqueda)
# =====================================================================
@app.get("/api/transactions")
@login_required
def list_transactions():
    u = uid()
    q = (request.args.get("q") or "").strip()
    dfrom = parse_date(request.args.get("from"))
    dto = parse_date(request.args.get("to"))
    kind = request.args.get("kind") if request.args.get("kind") in ("expense", "income") else None
    searching = bool(q or dfrom or dto or kind)

    where, params = ["t.user_id=%s"], [u]
    if searching:
        if dfrom:
            where.append("t.tx_date >= %s"); params.append(dfrom)
        if dto:
            where.append("t.tx_date <= %s"); params.append(dto)
        if kind:
            where.append("t.kind = %s"); params.append(kind)
        if q:
            like = f"%{q}%"
            cond = "(t.note ILIKE %s OR c.name ILIKE %s OR p.name ILIKE %s"
            params += [like, like, like]
            try:
                amt = parse_amount(q)
                cond += " OR t.amount = %s"
                params.append(abs(amt))
            except BadInput:
                pass
            where.append(cond + ")")
        limit = "LIMIT 500"
    else:
        with db() as conn:
            ms = user_settings(conn, u)["month_start"]
        ym = parse_key(request.args.get("month"), current_period_key(ms))
        start, end = period_range(ym, ms)
        where += ["t.tx_date >= %s", "t.tx_date < %s", "NOT (t.kind='expense' AND t.recurring_id IS NOT NULL)"]
        params += [start, end]
        limit = ""

    with db() as conn:
        materialize(conn, u)
        rows = conn.execute(
            f"""SELECT t.id,t.kind,t.amount,t.note,t.tx_date,t.category_id,t.recurring_id,
                       c.name AS category, p.name AS parent_category, c.icon, COALESCE(p.color,c.color) AS color
                FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
                LEFT JOIN categories p ON p.id=c.parent_id
                WHERE {' AND '.join(where)}
                ORDER BY t.tx_date DESC, t.id DESC {limit}""",
            params,
        ).fetchall()
    return jsonify(clean(rows))


def tx_payload(d):
    if d.get("kind") not in ("expense", "income"):
        raise BadInput("Tipo inválido")
    amount = parse_amount(d.get("amount"))
    return amount, parse_date(d.get("tx_date"), today()), parse_text(d.get("note"))


@app.post("/api/transactions")
@login_required
def create_transaction():
    d = body()
    amount, tx_date, note = tx_payload(d)
    with db() as conn:
        row = conn.execute(
            """INSERT INTO transactions(user_id,kind,amount,category_id,note,tx_date)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
            (uid(), d["kind"], amount, own_category(conn, uid(), d.get("category_id")),
             note, tx_date),
        ).fetchone()
    return jsonify(row), 201


@app.put("/api/transactions/<int:tid>")
@login_required
def update_transaction(tid):
    d = body()
    amount, tx_date, note = tx_payload(d)
    with db() as conn:
        row = conn.execute(
            """UPDATE transactions SET kind=%s, amount=%s, category_id=%s, note=%s, tx_date=%s
               WHERE id=%s AND user_id=%s RETURNING id""",
            (d["kind"], amount, own_category(conn, uid(), d.get("category_id")),
             note, tx_date, tid, uid()),
        ).fetchone()
    if not row:
        return jsonify({"error": "no encontrada"}), 404
    return jsonify(row)


@app.delete("/api/transactions/<int:tid>")
@login_required
def delete_transaction(tid):
    with db() as conn:
        conn.execute("DELETE FROM transactions WHERE id=%s AND user_id=%s", (tid, uid()))
    return "", 204


# =====================================================================
# Resumen
# =====================================================================
@app.get("/api/summary")
@login_required
def summary():
    u = uid()
    t = today()
    with db() as conn:
        st = user_settings(conn, u)
        ms = st["month_start"]
        ym = parse_key(request.args.get("month"), current_period_key(ms))
        start, end = period_range(ym, ms)
        pkey = shift_key(ym, -1)
        pstart, _ = period_range(pkey, ms)
        # Mes en curso: se compara con los mismos días del mes anterior (no con el mes entero)
        pcut = start
        if start <= t < end:
            pcut = min(start, pstart + (t - start) + timedelta(days=1))
        tstart, _ = period_range(shift_key(ym, -5), ms)
        materialize(conn, u)
        pending = pending_for_period(conn, u, start, end)
        totals = conn.execute(
            """SELECT COALESCE(SUM(amount) FILTER (WHERE kind='income'),0) AS income,
                      COALESCE(SUM(amount) FILTER (WHERE kind='expense'),0) AS expense,
                      COALESCE(SUM(amount) FILTER (WHERE kind='expense' AND recurring_id IS NOT NULL),0) AS rec_expense
               FROM transactions WHERE user_id=%s AND tx_date >= %s AND tx_date < %s""",
            (u, start, end),
        ).fetchone()
        prev = conn.execute(
            """SELECT COALESCE(SUM(amount) FILTER (WHERE kind='income'),0) AS income,
                      COALESCE(SUM(amount) FILTER (WHERE kind='expense'),0) AS expense,
                      COALESCE(SUM(amount) FILTER (WHERE kind='expense' AND recurring_id IS NULL),0) AS var_expense
               FROM transactions WHERE user_id=%s AND tx_date >= %s AND tx_date < %s""",
            (u, pstart, pcut),
        ).fetchone()
        by_cat = conn.execute(
            """SELECT c.id, COALESCE(c.name,'Sin categoría') AS name,
                      COALESCE(c.icon,'❔') AS icon, COALESCE(p.color,c.color,'#8a8fa3') AS color,
                      c.budget, t.kind, SUM(t.amount) AS total,
                      c.parent_id, p.name AS parent_name, p.icon AS parent_icon
               FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
               LEFT JOIN categories p ON p.id=c.parent_id
               WHERE t.user_id=%s AND t.tx_date >= %s AND t.tx_date < %s
                 AND NOT (t.kind='expense' AND t.recurring_id IS NOT NULL)
               GROUP BY c.id,c.name,c.icon,c.color,c.budget,t.kind,c.parent_id,p.name,p.icon,p.color
               ORDER BY total DESC""",
            (u, start, end),
        ).fetchall()
        prev_cat = conn.execute(
            """SELECT category_id, kind, SUM(amount) AS total FROM transactions
               WHERE user_id=%s AND tx_date >= %s AND tx_date < %s
                 AND NOT (kind='expense' AND recurring_id IS NOT NULL)
               GROUP BY category_id, kind""",
            (u, pstart, pcut),
        ).fetchall()
        daily = conn.execute(
            """SELECT tx_date,
                      COALESCE(SUM(amount) FILTER (WHERE kind='income'),0) AS income,
                      COALESCE(SUM(amount) FILTER (WHERE kind='expense' AND recurring_id IS NULL),0) AS expense
               FROM transactions WHERE user_id=%s AND tx_date >= %s AND tx_date < %s
               GROUP BY tx_date ORDER BY tx_date""",
            (u, start, end),
        ).fetchall()
        trend = conn.execute(
            """SELECT to_char(date_trunc('month', tx_date - make_interval(days => %s)),'YYYY-MM') AS month,
                      COALESCE(SUM(amount) FILTER (WHERE kind='income'),0) AS income,
                      COALESCE(SUM(amount) FILTER (WHERE kind='expense'),0) AS expense
               FROM transactions
               WHERE user_id=%s AND tx_date >= %s AND tx_date < %s
               GROUP BY 1 ORDER BY 1""",
            (ms - 1, u, tstart, end),
        ).fetchall()
        budgets = conn.execute(
            """SELECT c.id,c.name,c.icon,c.color,c.budget,c.parent_id,
                      COALESCE((SELECT SUM(t.amount) FROM transactions t
                                JOIN categories x ON x.id=t.category_id
                                WHERE (x.id=c.id OR x.parent_id=c.id)
                                  AND t.tx_date >= %s AND t.tx_date < %s),0) AS spent
               FROM categories c
               WHERE c.user_id=%s AND c.kind='expense' AND c.budget IS NOT NULL
               ORDER BY c.name""",
            (start, end, u),
        ).fetchall()
        lim = conn.execute(
            "SELECT value FROM user_settings WHERE user_id=%s AND key='monthly_limit'", (u,)
        ).fetchone()
        goals_month = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM goal_moves WHERE user_id=%s AND move_date >= %s AND move_date < %s",
            (u, start, end),
        ).fetchone()["s"]
        alltime = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount ELSE -amount END),0) AS net
               FROM transactions WHERE user_id=%s AND tx_date <= %s""",
            (u, t),
        ).fetchone()["net"]
        in_goals = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM goal_moves WHERE user_id=%s", (u,)
        ).fetchone()["s"]
        carry = 0.0
        if st["carry_over"]:
            before = conn.execute(
                """SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount ELSE -amount END),0) AS net
                   FROM transactions WHERE user_id=%s AND tx_date < %s""",
                (u, start),
            ).fetchone()["net"]
            goals_before = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM goal_moves WHERE user_id=%s AND move_date < %s",
                (u, start),
            ).fetchone()["s"]
            carry = num(before) - num(goals_before)

    income, expense = num(totals["income"]), num(totals["expense"])
    pend_in = sum(num(p["amount"]) for p in pending if p["kind"] == "income")
    pend_out = sum(num(p["amount"]) for p in pending if p["kind"] == "expense")
    goals_m = num(goals_month)
    # Límite optimizado: ingresos del periodo - todos los recurrentes de gasto del periodo (pagados + pendientes)
    rec_paid = num(totals["rec_expense"])
    smart_income = income + pend_in
    smart_recurring = rec_paid + pend_out
    smart_limit = smart_income - smart_recurring
    variable_spent = expense - rec_paid
    available = income + pend_in - expense - pend_out - goals_m + carry
    is_current = start <= t < end
    days_left = (end - t).days if is_current else None

    prev_map = {f"{r['category_id']}:{r['kind']}": num(r["total"]) for r in prev_cat}
    by_cat = clean(by_cat)
    for r in by_cat:
        r["prev_total"] = prev_map.get(f"{r['id']}:{r['kind']}", 0)

    return jsonify({
        "month": ym,
        "period_start": start.isoformat(),
        "period_end": (end - timedelta(days=1)).isoformat(),
        "income": income,
        "expense": expense,
        "balance": income - expense,
        "pending_income": pend_in,
        "pending_expense": pend_out,
        "to_goals": goals_m,
        "carry_over": carry,
        "available": available,
        "is_current": is_current,
        "is_past": end <= t,
        "days_left": days_left,
        "per_day": (available / days_left) if days_left and available > 0 else 0,
        "upcoming": clean(pending),
        "monthly_limit": float(lim["value"]) if lim and lim["value"] else None,
        "smart": {
            "income": smart_income,
            "recurring": smart_recurring,
            "limit": smart_limit,
            "spent": variable_spent,
        },
        "prev_month": pkey,
        "prev_partial": pcut < start,
        "prev_income": num(prev["income"]),
        "prev_expense": num(prev["expense"]),
        "prev_variable_expense": num(prev["var_expense"]),
        "savings_free": num(alltime) - num(in_goals),
        "savings_in_goals": num(in_goals),
        "by_category": by_cat,
        "daily": clean(daily),
        "trend": clean(trend),
        "budgets": clean(budgets),
    })


# =====================================================================
# Vista anual
# =====================================================================
@app.get("/api/year")
@login_required
def year_view():
    u = uid()
    try:
        y = int(request.args.get("y") or today().year)
        assert 1900 <= y <= 2999
    except Exception:
        return jsonify({"error": "año inválido"}), 400
    with db() as conn:
        ms = user_settings(conn, u)["month_start"]
        start, _ = period_range(f"{y:04d}-01", ms)
        _, end = period_range(f"{y:04d}-12", ms)
        months = conn.execute(
            """SELECT to_char(date_trunc('month', tx_date - make_interval(days => %s)),'YYYY-MM') AS month,
                      COALESCE(SUM(amount) FILTER (WHERE kind='income'),0) AS income,
                      COALESCE(SUM(amount) FILTER (WHERE kind='expense'),0) AS expense
               FROM transactions WHERE user_id=%s AND tx_date >= %s AND tx_date < %s
               GROUP BY 1 ORDER BY 1""",
            (ms - 1, u, start, end),
        ).fetchall()
        top = conn.execute(
            """SELECT m.id, COALESCE(m.name,'Sin categoría') AS name, COALESCE(m.icon,'❔') AS icon,
                      COALESCE(m.color,'#8a8fa3') AS color, SUM(t.amount) AS total
               FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
               LEFT JOIN categories m ON m.id=COALESCE(c.parent_id,c.id)
               WHERE t.user_id=%s AND t.kind='expense' AND t.tx_date >= %s AND t.tx_date < %s
               GROUP BY m.id,m.name,m.icon,m.color ORDER BY total DESC LIMIT 6""",
            (u, start, end),
        ).fetchall()
    by_month = {r["month"]: r for r in months}
    series = []
    for m in range(1, 13):
        k = f"{y:04d}-{m:02d}"
        r = by_month.get(k)
        series.append({"month": k, "income": num(r["income"]) if r else 0, "expense": num(r["expense"]) if r else 0})
    inc = sum(x["income"] for x in series)
    exp = sum(x["expense"] for x in series)
    active = [x for x in series if x["income"] or x["expense"]]
    return jsonify({
        "year": y,
        "months": series,
        "income": inc,
        "expense": exp,
        "saved": inc - exp,
        "savings_rate": ((inc - exp) / inc * 100) if inc else None,
        "avg_expense": (exp / len(active)) if active else 0,
        "top_categories": clean(top),
    })


# =====================================================================
# Recurrentes
# =====================================================================
def rec_payload(d):
    if d.get("kind") not in ("expense", "income"):
        raise BadInput("Tipo inválido")
    amount = parse_amount(d.get("amount"))
    try:
        day = int(d.get("day"))
        every = int(d.get("every") or 1)
    except Exception:
        raise BadInput("Datos inválidos")
    if not 1 <= day <= 31 or not 1 <= every <= 12:
        raise BadInput("Datos inválidos")
    return amount, day, every, parse_text(d.get("note"))


@app.get("/api/recurring")
@login_required
def list_recurring():
    u = uid()
    t = today()
    with db() as conn:
        materialize(conn, u)
        rows = conn.execute(
            """SELECT r.id,r.kind,r.amount,r.note,r.day,r.every,r.start_date,r.category_id,
                      c.name AS category,c.icon,c.color,
                      EXISTS (SELECT 1 FROM recurring_runs x WHERE x.recurring_id=r.id AND x.month=%s) AS done_this_month
               FROM recurring r LEFT JOIN categories c ON c.id=r.category_id
               WHERE r.active AND r.user_id=%s ORDER BY r.day""",
            (date(t.year, t.month, 1), u),
        ).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        y, m = t.year, t.month
        for _ in range(240):
            d = occurrence(y, m, r["day"])
            is_now = (y, m) == (t.year, t.month)
            if due_in_month(r, y, m) and d >= r["start_date"] and d > t and not (is_now and r["done_this_month"]):
                break
            y, m = next_month(y, m)
        r["next_date"] = d
        out.append(r)
    return jsonify(clean(out))


@app.post("/api/recurring")
@login_required
def create_recurring():
    d = body()
    amount, day, every, note = rec_payload(d)
    start_date = parse_date(d.get("start_date"), today())
    with db() as conn:
        row = conn.execute(
            """INSERT INTO recurring(user_id,kind,amount,category_id,note,day,every,start_date)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (uid(), d["kind"], amount, own_category(conn, uid(), d.get("category_id")), note,
             day, every, start_date),
        ).fetchone()
        materialize(conn, uid())
    return jsonify(row), 201


@app.put("/api/recurring/<int:rid>")
@login_required
def update_recurring(rid):
    d = body()
    amount, day, every, note = rec_payload(d)
    with db() as conn:
        row = conn.execute(
            """UPDATE recurring SET kind=%s, amount=%s, category_id=%s, note=%s, day=%s, every=%s
               WHERE id=%s AND user_id=%s RETURNING id""",
            (d["kind"], amount, own_category(conn, uid(), d.get("category_id")), note,
             day, every, rid, uid()),
        ).fetchone()
        if row:
            materialize(conn, uid())
    if not row:
        return jsonify({"error": "no encontrado"}), 404
    return jsonify(row)


@app.delete("/api/recurring/<int:rid>")
@login_required
def delete_recurring(rid):
    # Se desactiva para conservar el historial de movimientos ya creados
    with db() as conn:
        conn.execute("UPDATE recurring SET active=FALSE WHERE id=%s AND user_id=%s", (rid, uid()))
    return "", 204


# =====================================================================
# Límite mensual
# =====================================================================
@app.put("/api/limit")
@login_required
def set_monthly_limit():
    val = parse_optional_amount(body().get("amount"))
    with db() as conn:
        if val is None:
            conn.execute("DELETE FROM user_settings WHERE user_id=%s AND key='monthly_limit'", (uid(),))
        else:
            conn.execute(
                """INSERT INTO user_settings(user_id,key,value) VALUES (%s,'monthly_limit',%s)
                   ON CONFLICT (user_id,key) DO UPDATE SET value=EXCLUDED.value""",
                (uid(), str(val)),
            )
    return jsonify({"ok": True})


# =====================================================================
# Metas de ahorro
# =====================================================================
def goal_payload(d):
    name = parse_text(d.get("name"), 60)
    if not name:
        raise BadInput("Pon un nombre y un objetivo")
    target = parse_amount(d.get("target"), msg="Pon un nombre y un objetivo")
    return (name, target, parse_icon(d.get("icon"), "🎯"), parse_color(d.get("color"), "#2ecc8f"),
            parse_date(d.get("deadline")))


@app.get("/api/goals")
@login_required
def list_goals():
    with db() as conn:
        rows = conn.execute(
            """SELECT g.id,g.name,g.icon,g.color,g.target,g.deadline,
                      COALESCE(SUM(m.amount),0) AS saved
               FROM goals g LEFT JOIN goal_moves m ON m.goal_id=g.id
               WHERE g.user_id=%s GROUP BY g.id ORDER BY g.created_at""",
            (uid(),),
        ).fetchall()
    return jsonify(clean(rows))


@app.post("/api/goals")
@login_required
def create_goal():
    name, target, icon, color, deadline = goal_payload(body())
    with db() as conn:
        row = conn.execute(
            """INSERT INTO goals(user_id,name,icon,color,target,deadline)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
            (uid(), name, icon, color, target, deadline),
        ).fetchone()
    return jsonify(row), 201


@app.put("/api/goals/<int:gid>")
@login_required
def update_goal(gid):
    name, target, icon, color, deadline = goal_payload(body())
    with db() as conn:
        row = conn.execute(
            """UPDATE goals SET name=%s, icon=%s, color=%s, target=%s, deadline=%s
               WHERE id=%s AND user_id=%s RETURNING id""",
            (name, icon, color, target, deadline, gid, uid()),
        ).fetchone()
    if not row:
        return jsonify({"error": "no encontrada"}), 404
    return jsonify(row)


@app.delete("/api/goals/<int:gid>")
@login_required
def delete_goal(gid):
    with db() as conn:
        conn.execute("DELETE FROM goals WHERE id=%s AND user_id=%s", (gid, uid()))
    return "", 204


@app.post("/api/goals/<int:gid>/move")
@login_required
def goal_move(gid):
    amount = parse_amount(body().get("amount"), allow_negative=True)
    with db() as conn:
        g = conn.execute(
            """SELECT g.id, COALESCE(SUM(m.amount),0) AS saved FROM goals g
               LEFT JOIN goal_moves m ON m.goal_id=g.id
               WHERE g.id=%s AND g.user_id=%s GROUP BY g.id""",
            (gid, uid()),
        ).fetchone()
        if not g:
            return jsonify({"error": "no encontrada"}), 404
        if amount < 0 and -amount > g["saved"]:
            return jsonify({"error": "No puedes retirar más de lo ahorrado"}), 400
        conn.execute(
            "INSERT INTO goal_moves(goal_id,user_id,amount,move_date) VALUES (%s,%s,%s,%s)",
            (gid, uid(), amount, today()),
        )
    return jsonify({"ok": True}), 201


@app.get("/health")
def health():
    return {"ok": True}


if DATABASE_URL:
    init_db()
    app.secret_key = load_secret_key()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
