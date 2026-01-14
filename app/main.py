import os
import sqlite3
import secrets
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

APP_NAME = "Pet QR"
DB_PATH = os.getenv("DB_PATH", "/data/scans.db")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

# Telegram
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
TG_ENABLED = bool(TG_BOT_TOKEN and TG_CHAT_ID)

# Geo
GEO_ENABLED = os.getenv("GEO_ENABLED", "true").lower() in ("1", "true", "yes", "y")

# Admin auth (login)
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")  # ¡cambiar en producción!
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_urlsafe(32))

# Si querés varios perros, agregalos acá (después lo hacemos DB/JSON si pinta)
PETS: Dict[str, Dict[str, Any]] = {
    "rocky": {
        "name": "Rocky",
        "photo": "/static/pet.jpg",
        "breed": "Mestizo",
        "notes": "Es buenazo, pero se asusta si lo corrés. Ofrecele agua y hablale tranqui 🙂",
        "owner_name": os.getenv("OWNER_NAME", "Tincho"),
        "phone": os.getenv("OWNER_PHONE", "+54 9 261 000 0000"),
        "whatsapp": os.getenv("OWNER_WHATSAPP", "+5492610000000"),
        "extra": "No muerde. Responde a 'Rocky'.",
    }
}

app = FastAPI(title=APP_NAME)

# Sessions (cookie)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="petqr_session",
    same_site="lax",
    https_only=os.getenv("COOKIE_HTTPS_ONLY", "false").lower() in ("1", "true", "yes", "y"),
)

# Static + Templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


# ------------------------ DB helpers ------------------------

def db_init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pet_id TEXT NOT NULL,
                ts_utc TEXT NOT NULL,
                ip TEXT,
                user_agent TEXT,
                referrer TEXT,
                lat REAL,
                lon REAL,
                accuracy REAL
            )
        """)
        # Índices básicos para dashboard
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scans_pet ON scans(pet_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scans_ts ON scans(ts_utc)")
        conn.commit()


def get_client_ip(req: Request) -> str:
    xff = req.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    xrip = req.headers.get("x-real-ip")
    if xrip:
        return xrip.strip()
    return req.client.host if req.client else ""


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_insert_scan(
    pet_id: str,
    ts_utc: str,
    ip: str,
    ua: str,
    ref: str = "",
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    acc: Optional[float] = None,
):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO scans (pet_id, ts_utc, ip, user_agent, referrer, lat, lon, accuracy)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (pet_id, ts_utc, ip, ua, ref, lat, lon, acc),
        )
        conn.commit()


def db_query_scans(
    pet_id: Optional[str] = None,
    date_from: Optional[str] = None,  # YYYY-MM-DD
    date_to: Optional[str] = None,    # YYYY-MM-DD
    page: int = 1,
    page_size: int = 25,
) -> Tuple[int, List[dict]]:
    """
    Devuelve (total_count, rows)
    Filtrado por pet_id y rango de fechas (inclusive).
    """
    page = max(1, page)
    page_size = min(max(5, page_size), 200)
    offset = (page - 1) * page_size

    where = []
    params: List[Any] = []

    if pet_id and pet_id != "all":
        where.append("pet_id = ?")
        params.append(pet_id)

    # date_from / date_to interpretadas como UTC boundaries
    if date_from:
        where.append("substr(ts_utc, 1, 10) >= ?")
        params.append(date_from)
    if date_to:
        where.append("substr(ts_utc, 1, 10) <= ?")
        params.append(date_to)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        total = conn.execute(
            f"SELECT COUNT(*) as c FROM scans {where_sql}",
            params
        ).fetchone()["c"]

        rows = conn.execute(
            f"""
            SELECT id, pet_id, ts_utc, ip, user_agent, referrer, lat, lon, accuracy
            FROM scans
            {where_sql}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            params + [page_size, offset]
        ).fetchall()

    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "pet_id": r["pet_id"],
            "ts_utc": r["ts_utc"],
            "ip": r["ip"],
            "user_agent": r["user_agent"],
            "referrer": r["referrer"],
            "lat": r["lat"],
            "lon": r["lon"],
            "accuracy": r["accuracy"],
        })
    return total, result


def db_stats() -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) as c FROM scans").fetchone()["c"]
        last = conn.execute("SELECT ts_utc, pet_id, ip FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        per_pet = conn.execute(
            "SELECT pet_id, COUNT(*) as c FROM scans GROUP BY pet_id ORDER BY c DESC"
        ).fetchall()

    return {
        "total": total,
        "last": dict(last) if last else None,
        "per_pet": [{"pet_id": x["pet_id"], "count": x["c"]} for x in per_pet],
    }


# ------------------------ Telegram ------------------------

async def telegram_notify(text: str):
    if not TG_ENABLED:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True}
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, json=payload)


# ------------------------ Auth helpers ------------------------

def is_authed(request: Request) -> bool:
    return bool(request.session.get("auth") is True)


def require_auth(request: Request):
    if not is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


# ------------------------ App lifecycle ------------------------

@app.on_event("startup")
def startup():
    db_init()


# ------------------------ Public endpoints ------------------------

@app.get("/health", response_class=JSONResponse)
def health():
    return {"ok": True, "name": APP_NAME}


@app.get("/", response_class=HTMLResponse)
def root():
    # landing simple (evita 404)
    return HTMLResponse(
        "<h1>Pet QR</h1><p>Usá /p/&lt;pet_id&gt; (ej: /p/rocky)</p>",
        status_code=200
    )


@app.get("/p/{pet_id}", response_class=HTMLResponse)
async def pet_page(pet_id: str, request: Request):
    pet = PETS.get(pet_id)
    if not pet:
        return HTMLResponse("<h1>Perro no encontrado</h1>", status_code=404)

    ts = now_utc_iso()
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    ref = request.headers.get("referer", "")

    # Log del escaneo al abrir
    db_insert_scan(pet_id=pet_id, ts_utc=ts, ip=ip, ua=ua, ref=ref)

    # Notificación inicial
    msg = (
        f"🐶 Escanearon el QR de {pet['name']} ({pet_id})\n"
        f"🕒 {ts}\n"
        f"🌐 IP: {ip}\n"
        f"🔗 Perfil: {BASE_URL}/p/{pet_id}"
    )
    await telegram_notify(msg)

    return templates.TemplateResponse(
        "pet.html",
        {"request": request, "pet_id": pet_id, "pet": pet, "geo_enabled": GEO_ENABLED},
    )


@app.post("/api/scan/{pet_id}")
async def scan_geo(pet_id: str, request: Request):
    """
    Se llama desde el navegador si el usuario aceptó compartir ubicación.
    Guarda lat/lon y notifica con link a maps.
    """
    pet = PETS.get(pet_id)
    if not pet:
        return JSONResponse({"ok": False, "error": "pet_not_found"}, status_code=404)

    data = await request.json()
    lat = data.get("lat")
    lon = data.get("lon")
    acc = data.get("accuracy")

    ts = now_utc_iso()
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")

    db_insert_scan(pet_id=pet_id, ts_utc=ts, ip=ip, ua=ua, lat=lat, lon=lon, acc=acc)

    gmaps = f"https://maps.google.com/?q={lat},{lon}" if lat is not None and lon is not None else ""
    msg = (
        f"📍 Ubicación compartida para {pet['name']} ({pet_id})\n"
        f"🕒 {ts}\n"
        f"🎯 Precisión: {acc} m\n"
        f"🗺️ {gmaps}"
    )
    await telegram_notify(msg)

    return {"ok": True}


# ------------------------ Admin Dashboard ------------------------

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_get(request: Request):
    if is_authed(request):
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/admin/login", response_class=HTMLResponse)
def admin_login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if username == ADMIN_USER and password == ADMIN_PASSWORD:
        request.session["auth"] = True
        return RedirectResponse(url="/admin", status_code=303)

    return templates.TemplateResponse("login.html", {"request": request, "error": "Usuario o contraseña incorrectos."})


@app.get("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    pet: Optional[str] = None,
    date_from: Optional[str] = None,  # YYYY-MM-DD
    date_to: Optional[str] = None,    # YYYY-MM-DD
    page: int = 1,
    page_size: int = 25,
):
    redir = require_auth(request)
    if redir:
        return redir

    stats = db_stats()
    total, rows = db_query_scans(
        pet_id=pet,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )

    # Paginado
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))

    pets_list = [{"id": "all", "name": "Todos"}] + [{"id": k, "name": v["name"]} for k, v in PETS.items()]
    selected_pet = pet or "all"

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": stats,
            "rows": rows,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": page_size,
            "pets": pets_list,
            "selected_pet": selected_pet,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "base_url": BASE_URL,
        },
    )


@app.get("/admin/api/scans", response_class=JSONResponse)
def admin_api_scans(
    request: Request,
    pet: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
):
    redir = require_auth(request)
    if redir:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    total, rows = db_query_scans(pet_id=pet, date_from=date_from, date_to=date_to, page=page, page_size=page_size)
    return {"ok": True, "total": total, "rows": rows}
