import os
import sqlite3
import secrets
import math
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Tuple

import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

APP_NAME = "Pet QR"
DB_PATH = os.getenv("DB_PATH", "/data/scans.db")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

# Discord
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_ENABLED = bool(DISCORD_WEBHOOK_URL)

# Geo
GEO_ENABLED = os.getenv("GEO_ENABLED", "true").lower() in ("1", "true", "yes", "y")

# Admin auth
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_urlsafe(32))

# Public behavior
SHOW_PHONE_PUBLIC = os.getenv("SHOW_PHONE_PUBLIC", "true").lower() in ("1", "true", "yes", "y")

# Alerts tuning
TZ_NAME = os.getenv("TZ", "America/Argentina/Mendoza")
ALERT_DISTANCE_KM = float(os.getenv("ALERT_DISTANCE_KM", "2.0"))  # distancia desde "home"
ALERT_SCAN_BURST_MIN = int(os.getenv("ALERT_SCAN_BURST_MIN", "10"))  # ventana en min
ALERT_SCAN_BURST_COUNT = int(os.getenv("ALERT_SCAN_BURST_COUNT", "4"))  # escaneos para disparar
ALERT_NIGHT_START = int(os.getenv("ALERT_NIGHT_START", "22"))
ALERT_NIGHT_END = int(os.getenv("ALERT_NIGHT_END", "6"))

app = FastAPI(title=APP_NAME)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="petqr_session",
    same_site="lax",
    https_only=os.getenv("COOKIE_HTTPS_ONLY", "true").lower() in ("1", "true", "yes", "y"),
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


# ------------------------ Helpers ------------------------

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))

def get_client_ip(req: Request) -> str:
    # Cloudflare
    cf = req.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    tcip = req.headers.get("true-client-ip")
    if tcip:
        return tcip.strip()

    # Reverse proxies
    xff = req.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    xrip = req.headers.get("x-real-ip")
    if xrip:
        return xrip.strip()

    return req.client.host if req.client else ""

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p = math.pi / 180.0
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat/2)**2
         + math.cos(lat1*p) * math.cos(lat2*p) * math.sin(dlon/2)**2)
    return 2 * R * math.asin(math.sqrt(a))

def local_time_str(ts_utc_iso: str) -> str:
    tz = ZoneInfo(TZ_NAME)
    dt = parse_iso(ts_utc_iso).astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")

def is_night(ts_utc_iso: str) -> bool:
    tz = ZoneInfo(TZ_NAME)
    h = parse_iso(ts_utc_iso).astimezone(tz).hour
    # night is from start -> 23:59 and 0 -> end-1
    if ALERT_NIGHT_START <= ALERT_NIGHT_END:
        # rare case (e.g. 20->22)
        return ALERT_NIGHT_START <= h < ALERT_NIGHT_END
    return (h >= ALERT_NIGHT_START) or (h < ALERT_NIGHT_END)


# ------------------------ DB ------------------------

def db_init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                photo TEXT,
                breed TEXT,
                sex TEXT,
                age TEXT,
                size TEXT,
                color TEXT,
                chip_id TEXT,
                vaccines TEXT,
                sterilized TEXT,
                allergies TEXT,
                meds TEXT,
                temperament TEXT,
                distinctive TEXT,
                notes TEXT,
                reward TEXT,
                status TEXT NOT NULL DEFAULT 'lost', -- lost|home
                home_lat REAL,
                home_lon REAL,
                last_seen_utc TEXT,
                last_lat REAL,
                last_lon REAL,
                last_accuracy REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pet_id TEXT NOT NULL,
                label TEXT,
                name TEXT NOT NULL,
                phone TEXT,
                whatsapp TEXT,
                priority INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(pet_id) REFERENCES pets(id) ON DELETE CASCADE
            )
        """)
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scans_pet ON scans(pet_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scans_ts ON scans(ts_utc)")
        conn.commit()

    seed_default_pet()

def seed_default_pet():
    """
    Si está vacío, crea un pet por defecto (rocky) y un contacto principal usando env vars.
    """
    default_id = "rocky"
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM pets")
        n = cur.fetchone()[0]
        if n > 0:
            return

        owner_name = os.getenv("OWNER_NAME", "Tincho")
        owner_phone = os.getenv("OWNER_PHONE", "+54 9 261 000 0000")
        owner_wa = os.getenv("OWNER_WHATSAPP", "+5492610000000")

        conn.execute("""
            INSERT INTO pets (id, name, photo, breed, sex, age, size, color, chip_id, vaccines, sterilized,
                              allergies, meds, temperament, distinctive, notes, reward, status, home_lat, home_lon)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            default_id,
            "Rocky",
            "/static/pet.jpg",
            "Mestizo",
            "Macho",
            "3 años",
            "Mediano",
            "Marrón con pecho blanco",
            "—",
            "Antirrábica al día ✅",
            "Sí",
            "Ninguna conocida",
            "Ninguna",
            "Súper bueno, algo miedoso con ruidos fuertes",
            "Manchita blanca en el pecho / collar rojo",
            "No lo corras. Ofrecele agua y hablale tranqui 🙂",
            os.getenv("REWARD", "—"),
            "lost",
            float(os.getenv("HOME_LAT", "0") or 0),
            float(os.getenv("HOME_LON", "0") or 0),
        ))

        conn.execute("""
            INSERT INTO contacts (pet_id, label, name, phone, whatsapp, priority)
            VALUES (?,?,?,?,?,?)
        """, (default_id, "Dueño", owner_name, owner_phone, owner_wa, 1))

        conn.commit()

def db_get_pet(pet_id: str) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM pets WHERE id=?", (pet_id,)).fetchone()
        return dict(row) if row else None

def db_list_pets() -> List[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM pets ORDER BY name").fetchall()
        return [dict(r) for r in rows]

def db_contacts(pet_id: str) -> List[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM contacts WHERE pet_id=? ORDER BY priority ASC, id ASC
        """, (pet_id,)).fetchall()
        return [dict(r) for r in rows]

def db_insert_scan(pet_id: str, ts_utc: str, ip: str, ua: str, ref: str = "",
                   lat: Optional[float]=None, lon: Optional[float]=None, acc: Optional[float]=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO scans (pet_id, ts_utc, ip, user_agent, referrer, lat, lon, accuracy)
            VALUES (?,?,?,?,?,?,?,?)
        """, (pet_id, ts_utc, ip, ua, ref, lat, lon, acc))
        conn.commit()

def db_update_last_seen(pet_id: str, ts_utc: str, lat: Optional[float], lon: Optional[float], acc: Optional[float]):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE pets
            SET last_seen_utc=?, last_lat=?, last_lon=?, last_accuracy=?
            WHERE id=?
        """, (ts_utc, lat, lon, acc, pet_id))
        conn.commit()

def db_scan_burst_count(pet_id: str, minutes: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT COUNT(*) FROM scans
            WHERE pet_id=? AND ts_utc >= ?
        """, (pet_id, cutoff)).fetchone()
        return int(row[0] or 0)

def db_last_locations(pet_id: str, limit: int = 25) -> List[dict]:
    limit = max(1, min(200, int(limit)))
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT ts_utc, lat, lon, accuracy
            FROM scans
            WHERE pet_id=? AND lat IS NOT NULL AND lon IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
        """, (pet_id, limit)).fetchall()
        return [dict(r) for r in rows]

def db_stats() -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) as c FROM scans").fetchone()["c"]
        last = conn.execute("SELECT ts_utc, pet_id, ip FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        per_pet = conn.execute("SELECT pet_id, COUNT(*) as c FROM scans GROUP BY pet_id ORDER BY c DESC").fetchall()
    return {
        "total": total,
        "last": dict(last) if last else None,
        "per_pet": [{"pet_id": x["pet_id"], "count": x["c"]} for x in per_pet],
    }

def db_update_pet(pet_id: str, data: dict):
    keys = [
        "name","photo","breed","sex","age","size","color","chip_id","vaccines","sterilized",
        "allergies","meds","temperament","distinctive","notes","reward","status","home_lat","home_lon"
    ]
    fields = []
    params = []
    for k in keys:
        if k in data:
            fields.append(f"{k}=?")
            params.append(data[k])
    if not fields:
        return
    params.append(pet_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE pets SET {', '.join(fields)} WHERE id=?", params)
        conn.commit()

def db_add_contact(pet_id: str, label: str, name: str, phone: str, whatsapp: str, priority: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO contacts (pet_id, label, name, phone, whatsapp, priority)
            VALUES (?,?,?,?,?,?)
        """, (pet_id, label, name, phone, whatsapp, int(priority)))
        conn.commit()

def db_delete_contact(contact_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM contacts WHERE id=?", (int(contact_id),))
        conn.commit()


# ------------------------ Discord ------------------------

async def discord_notify(content: str):
    if not DISCORD_ENABLED:
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(DISCORD_WEBHOOK_URL, json={"content": content})


async def maybe_send_alerts(pet: dict, pet_id: str, ts_utc: str, ip: str,
                            lat: Optional[float], lon: Optional[float], acc: Optional[float]):
    """
    Alertas inteligentes SOLO si pet.status == 'lost'
    """
    if pet.get("status") != "lost":
        return

    alerts = []

    # Night alert
    if is_night(ts_utc):
        alerts.append("🌙 *Horario nocturno*")

    # Burst scans
    burst = db_scan_burst_count(pet_id, ALERT_SCAN_BURST_MIN)
    if burst >= ALERT_SCAN_BURST_COUNT:
        alerts.append(f"🔥 *Muchos escaneos* ({burst} en {ALERT_SCAN_BURST_MIN} min)")

    # Distance from home (needs home coords + current coords)
    home_lat = pet.get("home_lat")
    home_lon = pet.get("home_lon")
    if lat is not None and lon is not None and home_lat and home_lon and float(home_lat) != 0 and float(home_lon) != 0:
        dist = haversine_km(float(home_lat), float(home_lon), float(lat), float(lon))
        if dist >= ALERT_DISTANCE_KM:
            alerts.append(f"🧭 *Lejos de casa*: ~{dist:.2f} km")

    # Send message if any alerts triggered
    if alerts:
        gmaps = f"https://maps.google.com/?q={lat},{lon}" if lat is not None and lon is not None else ""
        msg = (
            f"🚨 **ALERTA inteligente**\n"
            f"• Perro: **{pet.get('name')}** (`{pet_id}`)\n"
            f"• Hora local: `{local_time_str(ts_utc)}`\n"
            f"• IP: `{ip}`\n"
            + "\n".join([f"• {a}" for a in alerts]) +
            (f"\n• Maps: {gmaps}" if gmaps else "") +
            f"\n• Perfil: {BASE_URL}/p/{pet_id}"
        )
        await discord_notify(msg)


# ------------------------ Auth ------------------------

def is_authed(request: Request) -> bool:
    return bool(request.session.get("auth") is True)

def require_auth(request: Request):
    if not is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


# ------------------------ Startup ------------------------

@app.on_event("startup")
def startup():
    db_init()


# ------------------------ Public ------------------------

@app.get("/{pet_id}")
def redirect_pet_case_insensitive(pet_id: str):
    return RedirectResponse(url=f"/{pet_id.lower()}", status_code=301)

@app.get("/health", response_class=JSONResponse)
def health():
    return {"ok": True, "name": APP_NAME}

@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse("<h1>Pet QR</h1><p>Usá /p/&lt;pet_id&gt; (ej: /p/rocky)</p>", status_code=200)

@app.get("/p/{pet_id}", response_class=HTMLResponse)
async def pet_page(pet_id: str, request: Request):
    pet = db_get_pet(pet_id)
    if not pet:
        return HTMLResponse("<h1>Perro no encontrado</h1>", status_code=404)
    if pet_id == "Frida":
        return RedirectResponse(url=f"/p/{pet_id.lower()}", status_code=301)
    contacts = db_contacts(pet_id)

    ts = now_utc_iso()
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")
    ref = request.headers.get("referer", "")

    db_insert_scan(pet_id, ts, ip, ua, ref)
    db_update_last_seen(pet_id, ts, None, None, None)

    # Notificación normal (no alerta)
    await discord_notify(
        f"🐶 **QR escaneado**\n"
        f"• Perro: **{pet.get('name')}** (`{pet_id}`)\n"
        f"• Hora local: `{local_time_str(ts)}`\n"
        f"• IP: `{ip}`\n"
        f"• Estado: `{pet.get('status')}`\n"
        f"• Perfil: {BASE_URL}/p/{pet_id}"
    )

    return templates.TemplateResponse(
        "pet.html",
        {
            "request": request,
            "pet_id": pet_id,
            "pet": pet,
            "contacts": contacts,
            "geo_enabled": GEO_ENABLED,
            "show_phone_public": SHOW_PHONE_PUBLIC,
            "base_url": BASE_URL
        },
    )

@app.get("/api/locations/{pet_id}", response_class=JSONResponse)
def api_locations(pet_id: str, limit: int = 25):
    pet = db_get_pet(pet_id)
    if not pet:
        return JSONResponse({"ok": False, "error": "pet_not_found"}, status_code=404)

    pts = db_last_locations(pet_id, limit=limit)
    return {"ok": True, "points": pts, "pet": {"id": pet_id, "name": pet.get("name"), "status": pet.get("status")}}

@app.post("/api/scan/{pet_id}", response_class=JSONResponse)
async def scan_geo(pet_id: str, request: Request):
    pet = db_get_pet(pet_id)
    if not pet:
        return JSONResponse({"ok": False, "error": "pet_not_found"}, status_code=404)

    data = await request.json()
    lat = data.get("lat")
    lon = data.get("lon")
    acc = data.get("accuracy")

    ts = now_utc_iso()
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "")

    db_insert_scan(pet_id, ts, ip, ua, lat=lat, lon=lon, acc=acc)
    db_update_last_seen(pet_id, ts, lat, lon, acc)

    gmaps = f"https://maps.google.com/?q={lat},{lon}" if lat is not None and lon is not None else ""
    await discord_notify(
        f"📍 **Ubicación compartida**\n"
        f"• Perro: **{pet.get('name')}** (`{pet_id}`)\n"
        f"• Hora local: `{local_time_str(ts)}`\n"
        f"• IP: `{ip}`\n"
        f"• Precisión: `{acc} m`\n"
        f"• Maps: {gmaps}"
    )

    # Alertas inteligentes
    await maybe_send_alerts(pet, pet_id, ts, ip, lat, lon, acc)

    return {"ok": True}

@app.post("/api/report/{pet_id}", response_class=JSONResponse)
async def report_sighting(pet_id: str, request: Request):
    pet = db_get_pet(pet_id)
    if not pet:
        return JSONResponse({"ok": False, "error": "pet_not_found"}, status_code=404)

    payload = await request.json()
    message = (payload.get("message") or "").strip()
    contact = (payload.get("contact") or "").strip()
    lat = payload.get("lat")
    lon = payload.get("lon")

    if len(message) < 3:
        return JSONResponse({"ok": False, "error": "message_too_short"}, status_code=400)

    ip = get_client_ip(request)
    ts = now_utc_iso()

    gmaps = ""
    if lat is not None and lon is not None:
        gmaps = f"https://maps.google.com/?q={lat},{lon}"

    await discord_notify(
        f"🧾 **Reporte desde la ficha**\n"
        f"• Perro: **{pet.get('name')}** (`{pet_id}`)\n"
        f"• Hora local: `{local_time_str(ts)}`\n"
        f"• IP: `{ip}`\n"
        f"• Contacto: `{contact or '—'}`\n"
        f"• Mensaje: {message}\n"
        + (f"• Maps: {gmaps}\n" if gmaps else "")
        + f"• Perfil: {BASE_URL}/p/{pet_id}"
    )

    # si mandaron ubicación, actualizamos "última vez visto"
    if lat is not None and lon is not None:
        db_update_last_seen(pet_id, ts, lat, lon, None)
        await maybe_send_alerts(pet, pet_id, ts, ip, lat, lon, None)

    return {"ok": True}

@app.get("/v/{pet_id}.vcf")
def vcard(pet_id: str):
    pet = db_get_pet(pet_id)
    if not pet:
        return Response("Not found", status_code=404)

    contacts = db_contacts(pet_id)
    main = contacts[0] if contacts else {"name": "Contacto", "phone": ""}

    vcf = "\n".join([
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"N:{main.get('name','Contacto')};;;;",
        f"FN:{main.get('name','Contacto')} (Mascota: {pet.get('name','')})",
        f"TEL;TYPE=CELL:{main.get('phone','')}",
        f"NOTE:Si encontraste a {pet.get('name','mi perro')}, perfil: {BASE_URL}/p/{pet_id}",
        "END:VCARD",
        ""
    ])
    headers = {
        "Content-Type": "text/vcard; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{pet_id}.vcf"',
    }
    return Response(vcf, headers=headers)


# ------------------------ Admin ------------------------

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_get(request: Request):
    if is_authed(request):
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/admin/login", response_class=HTMLResponse)
def admin_login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASSWORD:
        request.session["auth"] = True
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Usuario o contraseña incorrectos."})

@app.get("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, pet: Optional[str] = None):
    redir = require_auth(request)
    if redir:
        return redir

    stats = db_stats()
    pets = db_list_pets()
    selected = pet or (pets[0]["id"] if pets else "rocky")
    sel_pet = db_get_pet(selected) if selected else None
    contacts = db_contacts(selected) if selected else []
    last_points = db_last_locations(selected, limit=25) if selected else []

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": stats,
            "pets": pets,
            "selected_pet": selected,
            "pet": sel_pet,
            "contacts": contacts,
            "points": last_points,
            "base_url": BASE_URL,
            "tz": TZ_NAME,
            "show_phone_public": SHOW_PHONE_PUBLIC,
        },
    )

@app.post("/admin/pet/{pet_id}/status")
def admin_toggle_status(request: Request, pet_id: str, status: str = Form(...)):
    redir = require_auth(request)
    if redir:
        return redir
    status = "home" if status == "home" else "lost"
    db_update_pet(pet_id, {"status": status})
    return RedirectResponse(url=f"/admin?pet={pet_id}", status_code=303)

@app.post("/admin/pet/{pet_id}/update")
def admin_update_pet(
    request: Request,
    pet_id: str,
    name: str = Form(...),
    photo: str = Form(""),
    breed: str = Form(""),
    sex: str = Form(""),
    age: str = Form(""),
    size: str = Form(""),
    color: str = Form(""),
    chip_id: str = Form(""),
    vaccines: str = Form(""),
    sterilized: str = Form(""),
    allergies: str = Form(""),
    meds: str = Form(""),
    temperament: str = Form(""),
    distinctive: str = Form(""),
    notes: str = Form(""),
    reward: str = Form(""),
    home_lat: str = Form(""),
    home_lon: str = Form(""),
):
    redir = require_auth(request)
    if redir:
        return redir

    def fnum(x: str) -> Optional[float]:
        x = (x or "").strip()
        if not x:
            return None
        try:
            return float(x)
        except:
            return None

    db_update_pet(pet_id, {
        "name": name.strip(),
        "photo": photo.strip(),
        "breed": breed.strip(),
        "sex": sex.strip(),
        "age": age.strip(),
        "size": size.strip(),
        "color": color.strip(),
        "chip_id": chip_id.strip(),
        "vaccines": vaccines.strip(),
        "sterilized": sterilized.strip(),
        "allergies": allergies.strip(),
        "meds": meds.strip(),
        "temperament": temperament.strip(),
        "distinctive": distinctive.strip(),
        "notes": notes.strip(),
        "reward": reward.strip(),
        "home_lat": fnum(home_lat),
        "home_lon": fnum(home_lon),
    })
    return RedirectResponse(url=f"/admin?pet={pet_id}", status_code=303)

@app.post("/admin/pet/{pet_id}/contact/add")
def admin_add_contact(
    request: Request,
    pet_id: str,
    label: str = Form("Contacto"),
    name: str = Form(...),
    phone: str = Form(""),
    whatsapp: str = Form(""),
    priority: int = Form(1),
):
    redir = require_auth(request)
    if redir:
        return redir
    db_add_contact(pet_id, label.strip(), name.strip(), phone.strip(), whatsapp.strip(), priority)
    return RedirectResponse(url=f"/admin?pet={pet_id}", status_code=303)

@app.post("/admin/contact/{contact_id}/delete")
def admin_delete_contact(request: Request, contact_id: int, pet_id: str = Form(...)):
    redir = require_auth(request)
    if redir:
        return redir
    db_delete_contact(contact_id)
    return RedirectResponse(url=f"/admin?pet={pet_id}", status_code=303)
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

    # Validación básica de ID (evita espacios/raros)
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
    if any(ch not in allowed for ch in pet_id):
        return HTMLResponse("ID inválido. Usá a-z 0-9 - _", status_code=400)

    with sqlite3.connect(DB_PATH) as conn:
        exists = conn.execute("SELECT 1 FROM pets WHERE id=?", (pet_id,)).fetchone()
        if exists:
            return HTMLResponse("Ya existe un perro con ese ID.", status_code=400)

        conn.execute("""
            INSERT INTO pets (id, name, photo, status)
            VALUES (?, ?, ?, 'lost')
        """, (pet_id, name, "/static/pet.jpg"))

        # Copiamos contacto principal del primer perro (si existe) como default
        first = conn.execute("SELECT id FROM pets ORDER BY rowid ASC LIMIT 1").fetchone()
        if first:
            first_id = first[0]
            c = conn.execute("""
                SELECT label, name, phone, whatsapp, priority
                FROM contacts
                WHERE pet_id=?
                ORDER BY priority ASC LIMIT 1
            """, (first_id,)).fetchone()
            if c:
                conn.execute("""
                    INSERT INTO contacts (pet_id, label, name, phone, whatsapp, priority)
                    VALUES (?,?,?,?,?,?)
                """, (pet_id, c[0], c[1], c[2], c[3], c[4]))

        conn.commit()

    return RedirectResponse(url=f"/admin?pet={pet_id}", status_code=303)
