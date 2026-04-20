from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import aiofiles
import uuid
import json
import os
import random
import string
import mimetypes
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── App Setup ────────────────────────────────────────────────────────────────
app       = FastAPI(title="FileShare")
templates = Jinja2Templates(directory="templates")
# app.mount("/static", StaticFiles(directory="static"), name="static")



# ── Storage Setup ─────────────────────────────────────────────────────────────
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
DB_FILE    = Path("files_db.json")
MAX_SIZE   = 1000 * 1024 * 1024   # 1000 MB

# ── ✅ Configurable Limits ─────────────────────────────────────────────────────
MAX_FILES         = 5          # Maximum number of files allowed at one time
FILE_EXPIRY_MINS  = 5         # Files are auto-deleted after this many minutes

# ── DB Helpers (flat JSON file) ───────────────────────────────────────────────
def load_db() -> dict:
    if DB_FILE.exists():
        with open(DB_FILE) as f:
            return json.load(f)
    return {}

def save_db(data: dict) -> None:
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ── Helper ────────────────────────────────────────────────────────────────────
def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

def make_short_code(length: int = 6) -> str:
    """Generate a unique 6-char alphanumeric short code (e.g. aB3xK9)."""
    chars = string.ascii_letters + string.digits
    db    = load_db()
    used  = {v["short_code"] for v in db.values() if "short_code" in v}
    while True:
        code = "".join(random.choices(chars, k=length))
        if code not in used:
            return code

# ── Auto-Expiry Cleanup Job ───────────────────────────────────────────────────
def purge_expired_files():
    """Called by the scheduler every minute — removes files older than FILE_EXPIRY_MINS."""
    db      = load_db()
    cutoff  = datetime.now() - timedelta(minutes=FILE_EXPIRY_MINS)
    expired = [
        token for token, info in db.items()
        if datetime.fromisoformat(info["uploaded_at"]) < cutoff
    ]
    if not expired:
        return
    for token in expired:
        file_path = UPLOAD_DIR / db[token]["stored_name"]
        if file_path.exists():
            os.remove(file_path)
        del db[token]
    save_db(db)
    print(f"[FileShare] Purged {len(expired)} expired file(s).")

# ── Scheduler Lifecycle ───────────────────────────────────────────────────────
scheduler = AsyncIOScheduler()

@app.on_event("startup")
async def start_scheduler():
    scheduler.add_job(purge_expired_files, "interval", minutes=1, id="expiry_job")
    scheduler.start()
    print(f"[FileShare] Auto-expiry enabled — files deleted after {FILE_EXPIRY_MINS} min.")

@app.on_event("shutdown")
async def stop_scheduler():
    scheduler.shutdown()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Accept a file, save to disk, store metadata, return share token."""

    # ── Enforce file-count cap ──────────────────────────────────────────────
    db = load_db()
    if len(db) >= MAX_FILES:
        raise HTTPException(
            400,
            f"Storage full — maximum of {MAX_FILES} files allowed at one time. "
            "Delete an existing file before uploading a new one."
        )

    token       = str(uuid.uuid4())
    ext         = Path(file.filename).suffix.lower()
    stored_name = f"{token}{ext}"
    file_path   = UPLOAD_DIR / stored_name

    size = 0
    async with aiofiles.open(file_path, "wb") as out:
        while chunk := await file.read(1024 * 1024):   # read 1 MB at a time
            size += len(chunk)
            if size > MAX_SIZE:
                await out.close()
                os.remove(file_path)
                raise HTTPException(400, "File exceeds the 1 GB limit.")
            await out.write(chunk)

    mime = file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream"

    short_code = make_short_code()

    db[token] = {
        "original_name": file.filename,
        "stored_name":   stored_name,
        "short_code":    short_code,
        "size":          size,
        "size_human":    human_size(size),
        "mime_type":     mime,
        "uploaded_at":   datetime.now().isoformat(timespec="seconds"),
        "downloads":     0,
    }
    save_db(db)

    return {
        "token":      token,
        "short_code": short_code,
        "filename":   file.filename,
        "size":       size,
        "size_human": human_size(size),
    }


@app.get("/files")
async def list_files():
    """Return all stored file metadata, including time-until-expiry."""
    db  = load_db()
    now = datetime.now()
    result = []
    for k, v in db.items():
        uploaded = datetime.fromisoformat(v["uploaded_at"])
        expires  = uploaded + timedelta(minutes=FILE_EXPIRY_MINS)
        secs_left = max(0, int((expires - now).total_seconds()))
        result.append({"token": k, **v, "expires_in_secs": secs_left})
    return result


@app.get("/download/{token}")
async def download_file(token: str):
    """Stream a file to the client and increment download counter."""
    db = load_db()
    if token not in db:
        raise HTTPException(404, "File not found.")

    info      = db[token]
    file_path = UPLOAD_DIR / info["stored_name"]

    if not file_path.exists():
        raise HTTPException(404, "File missing from storage.")

    db[token]["downloads"] += 1
    save_db(db)

    return FileResponse(
        path      = file_path,
        filename  = info["original_name"],
        media_type= info["mime_type"],
    )


@app.delete("/delete/{token}")
async def delete_file(token: str):
    """Remove a file from disk and the metadata store."""
    db = load_db()
    if token not in db:
        raise HTTPException(404, "File not found.")

    file_path = UPLOAD_DIR / db[token]["stored_name"]
    if file_path.exists():
        os.remove(file_path)

    del db[token]
    save_db(db)
    return {"message": "File deleted successfully."}


@app.get("/info/{token}")
async def file_info(token: str):
    """Return metadata for a single file (used for share-link previews)."""
    db = load_db()
    if token not in db:
        raise HTTPException(404, "File not found.")
    return {"token": token, **db[token]}


@app.get("/f/{code}")
async def short_link(code: str):
    """Short-link download — resolves a 6-char code to the file."""
    db = load_db()
    match = next(
        ((token, info) for token, info in db.items() if info.get("short_code") == code),
        None,
    )
    if not match:
        raise HTTPException(404, f"No file found for code '{code}'.")

    token, info = match
    file_path   = UPLOAD_DIR / info["stored_name"]

    if not file_path.exists():
        raise HTTPException(404, "File missing from storage.")

    db[token]["downloads"] += 1
    save_db(db)

    return FileResponse(
        path       = file_path,
        filename   = info["original_name"],
        media_type = info["mime_type"],
    )