# app.py
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.proxy_headers import ProxyHeadersMiddleware
from starlette.templating import Jinja2Templates

# -----------------------------
# Config
# -----------------------------
DB_PATH = os.getenv("DB_PATH", "/data/scans.db")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please")
DEFAULT_PET_ID = os.getenv("DEFAULT_PET_ID", "frida").strip().lower()

# -----------------------------
# App
# -----------------------------
app = FastAPI()

# Para que request.client.host sea el cliente real detrás de reverse proxy (Coolify)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Sesiones para admin
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=BASE_URL.startswith("https://"),
)

# Static + templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# -----------------------------
# DB helpers
# -----------------------------
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS pets (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            photo TEXT DEFAULT '/static/pet.jpg',
            status TEXT DEFAULT 'lost', -- lost|home
            breed TEXT DEFAULT '',
            sex TEXT DEFAULT '',
            age TEXT DEFAULT '',
            size TEXT DEFAULT '',
            color TEXT DEFAULT '',
            chip TEXT DEFAULT '',
            vaccinated TEXT DEFAULT '',
            neutered TEXT DEFAULT '',
            allergies TEXT DEFAULT '',
            medication TEXT DEFAULT '',
            temperament TEXT DEFAULT '',
            special_marks TEXT DEFAULT '',
            reward TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            home_lat REAL,
            home_lon REAL,
            created_utc TEXT DEFAULT ''
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pet_id TEXT NOT NULL,
            label TEXT DEFAULT 'Contacto',
            name TEXT NOT NULL,
            phone TEXT DEFAULT '',
            whatsapp TEXT DEFAULT '',
            priority INTEGER DEFAULT 1,
            FOREIGN KEY(pet_id) REFERENCES pets(id) ON DELETE CASCADE
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pet_id TEXT NOT NULL,
            ts_utc TEXT NOT NULL,
            ip TEXT DEFAULT '',
            ua TEXT DEFAULT '',
            ref TEXT DEFAULT '',
            note TEXT DEFAULT '',
            FOREIGN KEY(pet_id) REFERENCES pets(id) ON DELETE CASCADE
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pet_id TEXT NOT NULL,
            ts_utc TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            accuracy REAL,
            ip TEXT DEFAULT '',
            ua TEXT DEFAULT '',
            FOREIGN KEY(pet_id) REFERENCES pets(id) ON DELETE CASCADE
        )
        """)

        # Seed: si no hay perros, crear DEFAULT (Frida)
        row = conn.execute("SELECT id FROM pets LIMIT 1").fetchone()
        if row is None:
            conn.execute("""
                INSERT INTO pets (id, name, photo, status, created_utc)
                VALUES (?, ?, ?, ?, ?)
            """, (DEFAULT_PET_ID, "Frida", "/static/pet.jpg", "lost", now_utc_iso()))

            conn.execute("""
                INSERT INTO contacts (pet_id, label, name, phone, whatsapp, priority)
                VALUES (?, 'Dueño', 'Tincho', '', '', 1)
            """, (DEFAULT_PET_ID,))
        conn.commit()


init_db()


# -----------------------------
# Auth helpers
# -----------------------------
def is_authed(request: Request) -> bool:
    return bool(request.session.get("admin_ok"))


def require_auth(request: Request) -> Optional[RedirectResponse]:
    if not is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


# -----------------------------
# Request helpers
# -----------------------------
def get_real_ip(request: Request) -> str:
    # ProxyHeadersMiddleware ya setea request.client.host correctamente
    ip = ""
    if request.client:
        ip = request.client.host or ""

    # Fallback: headers típicos (por si algo raro)
    xff = request.headers.get("x-forwarded-for")
    if xff:
        ip = xff.split(",")[0].strip()
    return ip


def get_ref(request: Request) -> str:
    return request.headers.get("referer", "")


def get_ua(request: Request) -> str:
    return request.headers.get("user-agent", "")


# -----------------------------
# Core: Pet page
# -----------------------------
@app.get("/", response_class=RedirectResponse)
def root():
    # default -> frida
    return RedirectResponse(url=f"/p/{DEFAULT_PET_ID}", status_code=302)


# ✅ Compat: si alguien entra a /p/rocky que vaya a Frida
@app.get("/p/rocky", response_class=RedirectResponse)
def redirect_rocky():
    return RedirectResponse(url=f"/p/{DEFAULT_PET_ID}", status_code=301)


@app.get("/p/{pet_id}", response_class=HTMLResponse)
def pet_page(request: Request, pet_id: str):
    pet_id = (pet_id or "").strip()

    # ✅ Normalizamos mayúsculas: /p/Frida -> /p/frida
    if pet_id != pet_id.lower():
        return RedirectResponse(url=f"/p/{pet_id.lower()}", status_code=301)

    pet_id = pet_id.lower()

    with get_conn() as conn:
        pet = conn.execute("SELECT * FROM pets WHERE id=?", (pet_id,)).fetchone()
        if pet is None:
            # ✅ Nunca "null": devolvemos 404 lindo + sugerencia
            return templates.TemplateResponse(
                "pet.html",
                {
                    "request": request,
                    "pet": None,
                    "contacts": [],
                    "base_url": BASE_URL,
                    "error": f"No existe el perro '{pet_id}'.",
                },
                status_code=404,
            )

        # Log scan
        conn.execute("""
            INSERT INTO scans (pet_id, ts_utc, ip, ua, ref, note)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (pet_id, now_utc_iso(), get_real_ip(request), get_ua(request), get_ref(request), "page_view"))
        conn.commit()

        contacts = conn.execute("""
            SELECT * FROM contacts WHERE pet_id=? ORDER BY priority ASC, id ASC
        """, (pet_id,)).fetchall()

        # last location
        last_loc = conn.execute("""
            SELECT * FROM locations WHERE pet_id=? ORDER BY id DESC LIMIT 1
        """, (pet_id,)).fetchone()

    return templates.TemplateResponse(
        "pet.html",
        {
            "request": request,
            "pet": dict(pet),
            "contacts": [dict(c) for c in contacts],
            "last_loc": dict(last_loc) if last_loc else None,
            "base_url": BASE_URL,
            "error": None,
        },
    )


# -----------------------------
# API: receive GPS
# -----------------------------
@app.post("/api/location/{pet_id}")
def api_location(
    request: Request,
    pet_id: str,
    lat: float = Form(...),
    lon: float = Form(...),
    accuracy: Optional[float] = Form(None),
):
    pet_id = (pet_id or "").strip().lower()

    with get_conn() as conn:
        pet = conn.execute("SELECT 1 FROM pets WHERE id=?", (pet_id,)).fetchone()
        if pet is None:
            return JSONResponse({"ok": False, "error": "pet_not_found"}, status_code=404)

        conn.execute("""
            INSERT INTO locations (pet_id, ts_utc, lat, lon, accuracy, ip, ua)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (pet_id, now_utc_iso(), lat, lon, accuracy, get_real_ip(request), get_ua(request)))
        conn.commit()

    return {"ok": True}


@app.get("/api/locations/{pet_id}")
def api_locations(pet_id: str, limit: int = 25):
    pet_id = (pet_id or "").strip().lower()
    limit = max(1, min(int(limit), 200))

    with get_conn() as conn:
        pet = conn.execute("SELECT 1 FROM pets WHERE id=?", (pet_id,)).fetchone()
        if pet is None:
            return JSONResponse({"ok": False, "error": "pet_not_found"}, status_code=404)

        rows = conn.execute("""
            SELECT lat, lon, accuracy, ts_utc
            FROM locations
            WHERE pet_id=?
            ORDER BY id DESC
            LIMIT ?
        """, (pet_id, limit)).fetchall()

    return {"ok": True, "points": [dict(r) for r in rows]}


# -----------------------------
# Admin auth
# -----------------------------
@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_get(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/admin/login", response_class=HTMLResponse)
def admin_login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if (username or "") == ADMIN_USER and (password or "") == ADMIN_PASS:
        request.session["admin_ok"] = True
        return RedirectResponse(url="/admin", status_code=303)

    return templates.TemplateResponse("login.html", {"request": request, "error": "Credenciales inválidas"})


@app.get("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


# -----------------------------
# Admin dashboard
# -----------------------------
@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, pet: Optional[str] = None):
    redir = require_auth(request)
    if redir:
        return redir

    with get_conn() as conn:
        pets = conn.execute("SELECT id, name, status FROM pets ORDER BY id ASC").fetchall()
        if not pets:
            # seguridad extra, jamás debería pasar por seed
            return templates.TemplateResponse("dashboard.html", {
                "request": request,
                "pets": [],
                "selected_pet": DEFAULT_PET_ID,
                "stats": {},
                "last_seen": None,
                "scans": [],
                "base_url": BASE_URL,
            })

        selected = (pet or DEFAULT_PET_ID).strip().lower()
        if selected not in [p["id"] for p in pets]:
            selected = pets[0]["id"]

        # última vez visto
        last_seen = conn.execute("""
            SELECT ts_utc, ip, ua FROM scans
            WHERE pet_id=? ORDER BY id DESC LIMIT 1
        """, (selected,)).fetchone()

        scans = conn.execute("""
            SELECT ts_utc, ip, ref, note FROM scans
            WHERE pet_id=? ORDER BY id DESC LIMIT 50
        """, (selected,)).fetchall()

        total_scans = conn.execute("SELECT COUNT(*) c FROM scans WHERE pet_id=?", (selected,)).fetchone()["c"]
        total_locs = conn.execute("SELECT COUNT(*) c FROM locations WHERE pet_id=?", (selected,)).fetchone()["c"]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "pets": [dict(p) for p in pets],
            "selected_pet": selected,
            "stats": {"scans": total_scans, "locations": total_locs},
            "last_seen": dict(last_seen) if last_seen else None,
            "scans": [dict(s) for s in scans],
            "base_url": BASE_URL,
        },
    )


# -----------------------------
# Admin: create / delete pets
# -----------------------------
@app.post("/admin/pet/create")
def admin_create_pet(
    request: Request,
    pet_id: str = Form(...),
    name: str = Form(...),
):
    redir = require_auth(request)
    if redir:
        return redir

    pet_id = (pet_id or "").strip().lower()
    name = (name or "").strip()

    if not pet_id or not name:
        return RedirectResponse(url="/admin", status_code=303)

    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
    if any(ch not in allowed for ch in pet_id):
        return HTMLResponse("ID inválido. Usá a-z 0-9 - _", status_code=400)

    with get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM pets WHERE id=?", (pet_id,)).fetchone()
        if exists:
            return HTMLResponse("Ya existe un perro con ese ID.", status_code=400)

        conn.execute("""
            INSERT INTO pets (id, name, photo, status, created_utc)
            VALUES (?, ?, ?, 'lost', ?)
        """, (pet_id, name, "/static/pet.jpg", now_utc_iso()))
        conn.commit()

    return RedirectResponse(url=f"/admin?pet={pet_id}", status_code=303)


@app.post("/admin/pet/delete")
def admin_delete_pet(
    request: Request,
    pet_id: str = Form(...),
):
    redir = require_auth(request)
    if redir:
        return redir

    pet_id = (pet_id or "").strip().lower()
    if pet_id == DEFAULT_PET_ID:
        return HTMLResponse("No podés borrar el perro default.", status_code=400)

    with get_conn() as conn:
        conn.execute("DELETE FROM locations WHERE pet_id=?", (pet_id,))
        conn.execute("DELETE FROM scans WHERE pet_id=?", (pet_id,))
        conn.execute("DELETE FROM contacts WHERE pet_id=?", (pet_id,))
        conn.execute("DELETE FROM pets WHERE id=?", (pet_id,))
        conn.commit()

    return RedirectResponse(url="/admin", status_code=303)


# -----------------------------
# Admin: edit pet
# -----------------------------
@app.get("/admin/pet/edit", response_class=HTMLResponse)
def admin_edit_pet_get(request: Request, pet: str):
    redir = require_auth(request)
    if redir:
        return redir

    pet_id = (pet or "").strip().lower()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pets WHERE id=?", (pet_id,)).fetchone()
        if row is None:
            return HTMLResponse("Pet no existe.", status_code=404)

    return templates.TemplateResponse("edit_pet.html", {"request": request, "pet": dict(row), "base_url": BASE_URL})


@app.post("/admin/pet/edit")
def admin_edit_pet_post(
    request: Request,
    pet_id: str = Form(...),
    name: str = Form(""),
    photo: str = Form("/static/pet.jpg"),
    status: str = Form("lost"),
    breed: str = Form(""),
    sex: str = Form(""),
    age: str = Form(""),
    size: str = Form(""),
    color: str = Form(""),
    chip: str = Form(""),
    vaccinated: str = Form(""),
    neutered: str = Form(""),
    allergies: str = Form(""),
    medication: str = Form(""),
    temperament: str = Form(""),
    special_marks: str = Form(""),
    reward: str = Form(""),
    notes: str = Form(""),
    home_lat: Optional[str] = Form(None),
    home_lon: Optional[str] = Form(None),
):
    redir = require_auth(request)
    if redir:
        return redir

    pid = (pet_id or "").strip().lower()

    def parse_float(s: Optional[str]) -> Optional[float]:
        if s is None:
            return None
        s = str(s).strip()
        if s == "":
            return None
        try:
            return float(s)
        except:
            return None

    hlat = parse_float(home_lat)
    hlon = parse_float(home_lon)

    with get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM pets WHERE id=?", (pid,)).fetchone()
        if not exists:
            return HTMLResponse("Pet no existe.", status_code=404)

        conn.execute("""
            UPDATE pets SET
                name=?, photo=?, status=?, breed=?, sex=?, age=?, size=?, color=?, chip=?,
                vaccinated=?, neutered=?, allergies=?, medication=?, temperament=?,
                special_marks=?, reward=?, notes=?, home_lat=?, home_lon=?
            WHERE id=?
        """, (
            name, photo, status, breed, sex, age, size, color, chip,
            vaccinated, neutered, allergies, medication, temperament,
            special_marks, reward, notes, hlat, hlon, pid
        ))
        conn.commit()

    return RedirectResponse(url=f"/admin?pet={pid}", status_code=303)


# -----------------------------
# Health
# -----------------------------
@app.get("/health")
def health():
    return {"ok": True, "db": DB_PATH, "default_pet": DEFAULT_PET_ID}
