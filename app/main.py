import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, Response

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("stremio-usenet")

BASE_URL = os.environ["ADDON_BASE_URL"].rstrip("/")
PROWLARR_URL = os.environ["PROWLARR_URL"].rstrip("/")
PROWLARR_KEY = os.environ["PROWLARR_API_KEY"]
SAB_URL = os.environ["SAB_URL"].rstrip("/")
SAB_KEY = os.environ["SAB_API_KEY"]
SAB_CATEGORY = os.getenv("SAB_CATEGORY", "stremio")
SAB_COMPLETE_ROOT = Path(os.getenv("SAB_COMPLETE_ROOT", "/downloads/complete/stremio")).resolve()
CINEMETA_URL = os.getenv("CINEMETA_URL", "https://v3-cinemeta.strem.io").rstrip("/")
DB_PATH = os.getenv("DATABASE_PATH", "/state/sessions.sqlite3")
TTL_SECONDS = int(os.getenv("SESSION_TTL_HOURS", "12")) * 3600
WATCHED_THRESHOLD = float(os.getenv("WATCHED_THRESHOLD", "0.90"))
MAX_WAIT_SECONDS = int(os.getenv("MAX_WAIT_HOURS", "6")) * 3600

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".webm"}
app = FastAPI(title="Stremio SABnzbd disposable streaming add-on", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)

def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            video_id TEXT NOT NULL,
            media_type TEXT NOT NULL,
            title TEXT NOT NULL,
            job_name TEXT NOT NULL,
            nzo_id TEXT,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            completed_at REAL,
            last_event_at REAL,
            expires_at REAL,
            watched INTEGER NOT NULL DEFAULT 0,
            file_path TEXT,
            error TEXT
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_video ON sessions(video_id, created_at)")
        conn.commit()

@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(cleanup_loop())

@app.get("/health")
async def health():
    return {"ok": True, "service": "stremio-usenet"}

@app.get("/manifest.json")
async def manifest():
    return {
        "id": "local.stremio.sabnzbd.disposable",
        "version": "0.1.0",
        "name": "Local Usenet (SABnzbd)",
        "description": "On-demand, temporary playback through your own SABnzbd server.",
        "resources": [
            {"name": "stream", "types": ["movie", "series"], "idPrefixes": ["tt"]},
            {"name": "player", "types": ["movie", "series"], "idPrefixes": ["tt"]}
        ],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "behaviorHints": {"configurable": False, "configurationRequired": False}
    }

async def get_cinemeta(media_type: str, video_id: str):
    imdb_id = video_id.split(":")[0]
    url = f"{CINEMETA_URL}/meta/{media_type}/{imdb_id}.json"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url)
        r.raise_for_status()
        payload = r.json()
    meta = payload.get("meta") or {}
    if not meta:
        raise HTTPException(404, "Cinemeta metadata not found for this item")
    title = meta.get("name") or imdb_id
    year = meta.get("year") or meta.get("releaseInfo")
    if media_type == "series":
        match = re.search(r":(\d+):(\d+)", video_id)
        if not match:
            raise HTTPException(400, "Series stream ID did not include season and episode")
        season, episode = int(match.group(1)), int(match.group(2))
        ep = None
        for item in meta.get("videos") or []:
            if item.get("id") == video_id:
                ep = item
                break
        if ep is None:
            # Some Stremio clients send a video ID variant; match season/episode fields.
            for item in meta.get("videos") or []:
                if int(item.get("season", -1)) == season and int(item.get("episode", -1)) == episode:
                    ep = item
                    break
        episode_title = (ep or {}).get("title")
        query = f"{title} S{season:02d}E{episode:02d}"
        if episode_title:
            query += f" {episode_title}"
    else:
        query = f"{title} {year}" if year else title
    return title, query

async def prowlarr_search(query: str, media_type: str):
    categories = "2000" if media_type == "movie" else "5000"
    headers = {"X-Api-Key": PROWLARR_KEY}
    params = {"query": query, "type": "search", "categories": categories, "limit": 30}
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        r = await client.get(f"{PROWLARR_URL}/api/v1/search", params=params, headers=headers)
        r.raise_for_status()
        data = r.json()
    results = []
    for item in data if isinstance(data, list) else []:
        if str(item.get("protocol", "")).lower() not in ("usenet", "usenet"):
            continue
        if not item.get("downloadUrl"):
            continue
        results.append(item)
    # Prowlarr's result ordering is retained; do not invent a quality score.
    return results

async def sab_add_nzb(download_url: str, job_name: str):
    headers = {"X-Api-Key": PROWLARR_KEY}
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        nzb = await client.get(download_url, headers=headers)
        nzb.raise_for_status()
        if not nzb.content:
            raise RuntimeError("Prowlarr returned an empty NZB")
        params = {
            "mode": "addfile",
            "output": "json",
            "apikey": SAB_KEY,
            "cat": SAB_CATEGORY,
            "nzbname": job_name,
            "pp": "3"
        }
        files = {"name": (f"{job_name}.nzb", nzb.content, "application/x-nzb")}
        added = await client.post(f"{SAB_URL}/api", params=params, files=files)
        added.raise_for_status()
        payload = added.json()
    nzo_ids = (payload.get("value") or {}).get("nzo_ids") or (payload.get("nzo_ids") or [])
    if not nzo_ids:
        raise RuntimeError(f"SABnzbd did not return an NZB job ID: {payload}")
    return nzo_ids[0]

def insert_session(video_id, media_type, title, job_name):
    token = uuid.uuid4().hex
    now = time.time()
    with db() as conn:
        # One outstanding session per Stremio video ID avoids ambiguous player events.
        old = conn.execute(
            "SELECT token, nzo_id FROM sessions WHERE video_id=? AND status NOT IN ('deleted','failed') ORDER BY created_at DESC LIMIT 1",
            (video_id,)
        ).fetchone()
        if old:
            raise HTTPException(409, "A download session for this title already exists. Wait for it to finish or let its 12-hour expiry run.")
        conn.execute("""
            INSERT INTO sessions(token,video_id,media_type,title,job_name,status,created_at)
            VALUES(?,?,?,?,?,?,?)
        """, (token, video_id, media_type, title, job_name, "submitting", now))
        conn.commit()
    return token

def update_session(token, **fields):
    allowed = {"nzo_id", "status", "completed_at", "last_event_at", "expires_at", "watched", "file_path", "error"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sql = ", ".join(f"{k}=?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE sessions SET {sql} WHERE token=?", (*fields.values(), token))
        conn.commit()

def get_session(token):
    with db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None

async def get_sab_history(nzo_id: str):
    params = {"mode": "history", "output": "json", "apikey": SAB_KEY, "limit": 100, "nzo_ids": nzo_id}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{SAB_URL}/api", params=params)
        r.raise_for_status()
        payload = r.json()
    slots = ((payload.get("history") or {}).get("slots") or [])
    for slot in slots:
        if slot.get("nzo_id") == nzo_id:
            return slot
    return None

def safe_video_file(folder: Path):
    folder = folder.resolve()
    root = SAB_COMPLETE_ROOT.resolve()
    if root not in folder.parents and folder != root:
        return None
    if not folder.exists():
        return None
    candidates = []
    for p in folder.rglob("*"):
        try:
            rp = p.resolve()
            if root not in rp.parents or not p.is_file():
                continue
            if p.suffix.lower() in VIDEO_EXTS:
                candidates.append(p)
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)

def locate_completed_file(session, history):
    # SAB's storage field is the authoritative completed-job location when available.
    storage = (history.get("storage") or history.get("path")) if history else None
    candidates = []
    if storage:
        p = Path(storage)
        if not p.is_absolute():
            p = SAB_COMPLETE_ROOT / p
        candidates.append(p)
    candidates.append(SAB_COMPLETE_ROOT / session["job_name"])
    for folder in candidates:
        found = safe_video_file(folder)
        if found:
            return found
    return None

async def wait_for_video(token):
    deadline = time.time() + MAX_WAIT_SECONDS
    while time.time() < deadline:
        session = get_session(token)
        if not session:
            raise HTTPException(404, "Unknown playback session")
        if session["status"] == "failed":
            raise HTTPException(502, session.get("error") or "SABnzbd download failed")
        nzo_id = session.get("nzo_id")
        if nzo_id:
            try:
                history = await get_sab_history(nzo_id)
                if history:
                    status = str(history.get("status", "")).lower()
                    if status in ("failed", "cannot be completed"):
                        update_session(token, status="failed", error=f"SABnzbd status: {status}")
                        raise HTTPException(502, f"SABnzbd status: {status}")
                    if status == "completed":
                        path = locate_completed_file(session, history)
                        if path:
                            update_session(token, status="ready", completed_at=time.time(),
                                           expires_at=time.time() + TTL_SECONDS, file_path=str(path))
                            return path
            except HTTPException:
                raise
            except Exception as exc:
                log.warning("SAB history poll failed for %s: %s", token, exc)
        await asyncio.sleep(3)
    update_session(token, status="failed", error="Timed out waiting for SABnzbd to finish and unpack the download")
    raise HTTPException(504, "Timed out waiting for the download to complete")

async def cleanup_sab_job(session):
    nzo_id = session.get("nzo_id")
    if not nzo_id:
        return False
    params = {
        "mode": "history",
        "name": "delete",
        "value": nzo_id,
        "del_files": "1",
        "output": "json",
        "apikey": SAB_KEY
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{SAB_URL}/api", params=params)
        r.raise_for_status()
        payload = r.json()
    ok = (payload.get("status") is True) or (payload.get("status") == "True")
    return bool(ok)

async def cleanup_loop():
    while True:
        try:
            now = time.time()
            with db() as conn:
                rows = conn.execute("""
                    SELECT * FROM sessions
                    WHERE status NOT IN ('deleted','failed')
                      AND expires_at IS NOT NULL AND expires_at <= ?
                """, (now,)).fetchall()
            for row in rows:
                session = dict(row)
                try:
                    ok = await cleanup_sab_job(session)
                    if ok:
                        update_session(session["token"], status="deleted", file_path=None)
                        log.info("Cleaned up expired session %s", session["token"])
                    else:
                        log.error("SABnzbd did not confirm cleanup for session %s", session["token"])
                except Exception:
                    log.exception("Cleanup failed for session %s", session["token"])
        except Exception:
            log.exception("Cleanup loop error")
        await asyncio.sleep(60)

@app.get("/stream/{media_type}/{video_id}.json")
async def stream(media_type: str, video_id: str):
    if media_type not in ("movie", "series"):
        return {"streams": []}
    title, query = await get_cinemeta(media_type, video_id)
    results = await prowlarr_search(query, media_type)
    if not results:
        return {"streams": []}

    # Important Stremio protocol trade-off:
    # Stremio requests /stream while opening the detail page, before the user
    # presses Play. We therefore queue ONE top Prowlarr result here. This avoids
    # launching several downloads and avoids a long-running HTTP request from
    # the player's media URL while SABnzbd downloads.
    selected = results[0]
    token = uuid.uuid4().hex
    job_name = f"stremio-{token[:12]}"
    # Reserve the video ID before submitting, preventing duplicate sessions.
    token = insert_session(video_id, media_type, title, job_name)
    try:
        nzo_id = await sab_add_nzb(selected["downloadUrl"], job_name)
        update_session(token, nzo_id=nzo_id, status="queued",
                       expires_at=time.time() + MAX_WAIT_SECONDS + TTL_SECONDS)
    except Exception as exc:
        update_session(token, status="failed", error=str(exc))
        log.exception("Could not submit NZB to SABnzbd")
        return {"streams": []}

    release = selected.get("title") or title
    return {
        "streams": [{
            "name": "SABnzbd · temporary",
            "title": f"{release}\nDownload queued; play when ready",
            "url": f"{BASE_URL}/play/{token}",
            "behaviorHints": {"notWebReady": False}
        }]
    }

@app.api_route("/play/{token}", methods=["GET", "HEAD"])
async def play(token: str, request: Request):
    session = get_session(token)
    if not session:
        raise HTTPException(404, "Unknown or expired playback session")
    if session["status"] == "deleted":
        raise HTTPException(410, "This temporary download has been cleaned up")
    path = Path(session["file_path"]) if session.get("file_path") else await wait_for_video(token)
    if not path.exists() or path.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(404, "Playable video file not found")
    size = path.stat().st_size
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    range_header = request.headers.get("range")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": mime,
        "Content-Disposition": f'inline; filename="{path.name.replace(chr(34), "")}"',
        "Cache-Control": "no-store"
    }
    if not range_header:
        headers["Content-Length"] = str(size)
        if request.method == "HEAD":
            return Response(status_code=200, headers=headers)
        def iter_file():
            with path.open("rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        return StreamingResponse(iter_file(), status_code=200, headers=headers, media_type=mime)

    match = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
    if not match:
        raise HTTPException(416, "Invalid byte range")
    start_s, end_s = match.groups()
    if start_s == "":
        length = int(end_s or 0)
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    if start >= size or end < start:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    end = min(end, size - 1)
    headers["Content-Length"] = str(end - start + 1)
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD":
        return Response(status_code=206, headers=headers)
    def iter_range():
        with path.open("rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
    return StreamingResponse(iter_range(), status_code=206, headers=headers, media_type=mime)

@app.get("/player/{media_type}/{video_id}/{extra_args:path}")
async def player_event(media_type: str, video_id: str, extra_args: str):
    # Stremio sends player events to add-ons declaring the "player" resource.
    raw = extra_args[:-5] if extra_args.endswith(".json") else extra_args
    args = parse_qs(raw, keep_blank_values=True)
    action = (args.get("action") or [""])[0]
    current_ms = int((args.get("currentTime") or ["0"])[0] or 0)
    duration_ms = int((args.get("duration") or ["0"])[0] or 0)
    now = time.time()
    with db() as conn:
        row = conn.execute("""
            SELECT * FROM sessions
            WHERE video_id=? AND status NOT IN ('deleted','failed')
            ORDER BY created_at DESC LIMIT 1
        """, (video_id,)).fetchone()
    if not row:
        return {"ok": True}
    session = dict(row)
    log.info("Stremio player event: type=%s video_id=%s action=%s current_ms=%s duration_ms=%s",
             media_type, video_id, action, current_ms, duration_ms)
    if action == "start":
        update_session(session["token"], status="playing", last_event_at=now)
    elif action == "pause":
        update_session(session["token"], status="ready", last_event_at=now, expires_at=now + TTL_SECONDS)
    elif action == "stop":
        watched = duration_ms > 0 and (current_ms / duration_ms) >= WATCHED_THRESHOLD
        if watched:
            update_session(session["token"], watched=1, status="cleanup_pending",
                           last_event_at=now, expires_at=now)
        else:
            update_session(session["token"], status="ready", last_event_at=now,
                           expires_at=now + TTL_SECONDS)
    return {"ok": True}

@app.get("/")
async def root():
    return {
        "service": "Stremio SABnzbd disposable add-on",
        "manifest": f"{BASE_URL}/manifest.json",
        "note": "Local-network service. Do not expose this port to the public internet."
    }
