import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/ramakant.sqlite")
if not DATABASE_URL.startswith("sqlite:///"):
    raise RuntimeError("This starter uses SQLite. Set DATABASE_URL to sqlite:///your-database-file.")
database_name = unquote(DATABASE_URL[len("sqlite:///"):])
DB_PATH = Path(database_name)
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./data/uploads"))
if not UPLOAD_DIR.is_absolute():
    UPLOAD_DIR = BASE_DIR / UPLOAD_DIR
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

APP_ENV = os.getenv("APP_ENV", "development")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "secretary")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ramakant")
PASSWORD_SALT = os.getenv("ADMIN_PASSWORD_SALT", "local-ramakant-demo-salt")
if APP_ENV == "production" and ADMIN_PASSWORD == "ramakant":
    raise RuntimeError("Set a new private ADMIN_PASSWORD before production deployment.")
PASSWORD_HASH = hashlib.scrypt(ADMIN_PASSWORD.encode(), salt=PASSWORD_SALT.encode(), n=2**14, r=8, p=1)
MAX_UPLOAD = 9 * 1024 * 1024
SESSION_SECONDS = 8 * 60 * 60
sessions: dict[str, datetime] = {}

app = FastAPI(title="Ramakant CHSL API", version="2.0.0")
origins = [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["GET", "POST", "PUT", "PATCH"], allow_headers=["Content-Type"])


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database() -> None:
    with connect_db() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS content (id INTEGER PRIMARY KEY CHECK(id=1), json TEXT NOT NULL, updated_at TEXT NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS complaints (
            id TEXT PRIMARY KEY, room TEXT NOT NULL, concern TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Open', created_at TEXT NOT NULL
        )""")
        if db.execute("SELECT id FROM content WHERE id=1").fetchone() is None:
            seed = json.loads((BASE_DIR / "app" / "default-content.json").read_text(encoding="utf-8"))
            db.execute("INSERT INTO content(id,json,updated_at) VALUES(1,?,?)", (json.dumps(seed, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))


initialize_database()


class LoginBody(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=300)


class ComplaintBody(BaseModel):
    room: str = Field(min_length=1, max_length=80)
    concern: str = Field(min_length=5, max_length=2000)


class StatusBody(BaseModel):
    status: str


class ComplaintImport(BaseModel):
    complaints: list[dict[str, Any]] = Field(max_length=500)


def read_content() -> dict[str, Any]:
    with connect_db() as db:
        row = db.execute("SELECT json FROM content WHERE id=1").fetchone()
    return json.loads(row["json"])


def current_session(ramakant_session: str | None = Cookie(default=None)) -> str | None:
    if not ramakant_session:
        return None
    expiry = sessions.get(ramakant_session)
    if expiry is None:
        return None
    if expiry <= datetime.now(timezone.utc):
        sessions.pop(ramakant_session, None)
        return None
    return ramakant_session


def require_admin(token: str | None = Depends(current_session)) -> str:
    if not token:
        raise HTTPException(status_code=401, detail="Please sign in again.")
    return token


def clean(value: Any, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def valid_content(data: Any) -> bool:
    if not isinstance(data, dict) or not isinstance(data.get("site"), dict):
        return False
    if not isinstance(data.get("notices"), list) or not isinstance(data.get("events"), list):
        return False
    committees = data.get("committees")
    if not isinstance(committees, dict) or not isinstance(committees.get("managing", {}).get("members"), list):
        return False
    redevelopment = committees.get("redevelopment", {})
    if not isinstance(redevelopment.get("members"), list) or not isinstance(redevelopment.get("documents"), list):
        return False
    if any(not isinstance(event, dict) or not isinstance(event.get("photos", []), list) or len(event.get("photos", [])) > 10 for event in data["events"]):
        return False
    try:
        return len(json.dumps(data, ensure_ascii=False)) < 2_000_000
    except (TypeError, ValueError):
        return False


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/content")
def get_content() -> dict[str, Any]:
    return read_content()


@app.get("/api/session")
def get_session(token: str | None = Depends(current_session)) -> dict[str, bool]:
    return {"authenticated": bool(token)}


@app.post("/api/login")
def login(body: LoginBody, response: Response) -> dict[str, Any]:
    incoming = hashlib.scrypt(body.password.encode(), salt=PASSWORD_SALT.encode(), n=2**14, r=8, p=1)
    if not hmac.compare_digest(body.username, ADMIN_USERNAME) or not hmac.compare_digest(incoming, PASSWORD_HASH):
        raise HTTPException(status_code=401, detail="Incorrect username or password.")
    token = secrets.token_urlsafe(32)
    sessions[token] = datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS)
    response.set_cookie("ramakant_session", token, max_age=SESSION_SECONDS, httponly=True, secure=APP_ENV == "production", samesite="lax", path="/")
    return {"authenticated": True, "username": ADMIN_USERNAME}


@app.post("/api/logout")
def logout(response: Response, token: str | None = Depends(current_session)) -> dict[str, bool]:
    if token:
        sessions.pop(token, None)
    response.delete_cookie("ramakant_session", httponly=True, secure=APP_ENV == "production", samesite="lax", path="/")
    return {"ok": True}


@app.put("/api/content")
def put_content(data: dict[str, Any], _: str = Depends(require_admin)) -> dict[str, Any]:
    if not valid_content(data):
        raise HTTPException(status_code=400, detail="Content is incomplete or an event has more than 10 photos.")
    updated = datetime.now(timezone.utc).isoformat()
    with connect_db() as db:
        db.execute("UPDATE content SET json=?, updated_at=? WHERE id=1", (json.dumps(data, ensure_ascii=False), updated))
    return {"ok": True, "updatedAt": updated}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), _: str = Depends(require_admin)) -> dict[str, str]:
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf"}
    extension = allowed.get(file.content_type or "")
    if not extension:
        raise HTTPException(status_code=415, detail="Upload a JPG, PNG, WEBP, or PDF file.")
    data = await file.read(MAX_UPLOAD + 1)
    if not data or len(data) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="Each file must be smaller than 9 MB.")
    name = f"{uuid4().hex}{extension}"
    (UPLOAD_DIR / name).write_bytes(data)
    return {"url": f"/uploads/{name}", "name": clean(file.filename, 150)}


@app.post("/api/complaints", status_code=201)
def create_complaint(body: ComplaintBody) -> dict[str, str]:
    complaint_id = f"RC-{secrets.token_hex(3).upper()}"
    created = datetime.now(timezone.utc).isoformat()
    with connect_db() as db:
        db.execute("INSERT INTO complaints(id,room,concern,status,created_at) VALUES(?,?,?,?,?)", (complaint_id, clean(body.room, 80), clean(body.concern, 2000), "Open", created))
    return {"id": complaint_id, "status": "Open"}


@app.get("/api/complaints")
def list_complaints(_: str = Depends(require_admin)) -> list[dict[str, Any]]:
    with connect_db() as db:
        rows = db.execute("SELECT id,room,concern,status,created_at AS createdAt FROM complaints ORDER BY created_at DESC LIMIT 500").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/complaints/import")
def import_complaints(body: ComplaintImport, _: str = Depends(require_admin)) -> dict[str, Any]:
    imported = 0
    with connect_db() as db:
        for item in body.complaints:
            complaint_id = clean(item.get("id"), 32)
            room = clean(item.get("room"), 80)
            concern = clean(item.get("concern"), 2000)
            if len(complaint_id) != 9 or not complaint_id.startswith("RC-") or not room or len(concern) < 5:
                continue
            created = str(item.get("createdAt") or datetime.now(timezone.utc).isoformat())[:64]
            status = "Resolved" if item.get("status") == "Resolved" else "Open"
            result = db.execute("INSERT OR IGNORE INTO complaints(id,room,concern,status,created_at) VALUES(?,?,?,?,?)", (complaint_id, room, concern, status, created))
            imported += result.rowcount
    return {"ok": True, "imported": imported}


@app.patch("/api/complaints/{complaint_id}")
def update_complaint(complaint_id: str, body: StatusBody, _: str = Depends(require_admin)) -> dict[str, bool]:
    status = "Resolved" if body.status == "Resolved" else "Open"
    with connect_db() as db:
        result = db.execute("UPDATE complaints SET status=? WHERE id=?", (status, complaint_id))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Complaint not found.")
    return {"ok": True}


app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend_routes(path: str) -> FileResponse:
        if path.startswith("api/") or path == "health":
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(STATIC_DIR / "index.html")
