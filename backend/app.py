"""
WaybackRevive — Deleted YouTube Video Recovery Backend
FastAPI + yt-dlp + Internet Archive CDX API

Deploy FREE on:
  - Railway.app  (recommended — $5/month free credit, no sleep)
  - Render.com   (free tier, sleeps after 15 min inactivity, 30s cold start)
  - Fly.io       (free tier, 3 shared VMs)
"""

import os
import re
import uuid
import json
import time
import shutil
import threading
import tempfile
import logging
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from typing import Optional

import requests
import yt_dlp
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# ════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
log = logging.getLogger("wayback")

# ════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/wayback_downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_AGE_SECONDS  = 3600        # Delete downloaded files after 1 hour
FREE_DAILY_LIMIT      = 3           # Free downloads per IP per day
FREE_MAX_QUALITY      = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
PRO_MAX_QUALITY       = "bestvideo+bestaudio/best"
MAX_FILESIZE_FREE     = "500M"      # Guard against enormous files on free tier
JOB_TTL_SECONDS       = 7200       # Keep job records for 2 hours

# CORS — update this to your frontend domain in production
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

# ════════════════════════════════════════════
# IN-MEMORY STORES (no Redis needed)
# ════════════════════════════════════════════
jobs: dict[str, dict] = {}          # job_id → job dict
ip_downloads: dict[str, list] = defaultdict(list)  # ip → [timestamp, ...]
store_lock = threading.Lock()

# ════════════════════════════════════════════
# APP
# ════════════════════════════════════════════
app = FastAPI(title="WaybackRevive API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ════════════════════════════════════════════
# REQUEST MODELS
# ════════════════════════════════════════════
class CheckRequest(BaseModel):
    url: str
    video_id: Optional[str] = None
    url_type: Optional[str] = None   # "youtube" | "wayback"


class DownloadRequest(BaseModel):
    url: str
    format: str = "mp4_720"         # mp4_720 | mp4_1080 | mp3


# ════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════
def extract_video_id(url: str) -> Optional[str]:
    """Extract YouTube video ID from any supported URL format."""
    patterns = [
        r"[?&]v=([a-zA-Z0-9_-]{11})",
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
        r"shorts/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/watch\?.*v=([a-zA-Z0-9_-]{11})",
        r"archive\.org.*youtube\.com/watch%3Fv%3D([a-zA-Z0-9_-]{11})",
        r"archive\.org.*youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def get_ip(request: Request) -> str:
    """Get real client IP, respecting proxy headers."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_rate_limit(ip: str) -> tuple[bool, int]:
    """Returns (allowed, remaining_today)."""
    now = time.time()
    day_start = now - 86400
    with store_lock:
        timestamps = [t for t in ip_downloads[ip] if t > day_start]
        ip_downloads[ip] = timestamps
        used = len(timestamps)
        allowed = used < FREE_DAILY_LIMIT
        remaining = max(0, FREE_DAILY_LIMIT - used)
        return allowed, remaining


def record_download(ip: str):
    with store_lock:
        ip_downloads[ip].append(time.time())


def format_timestamp(ts: str) -> str:
    """Format Wayback Machine timestamp (YYYYMMDDHHmmss) to readable string."""
    try:
        dt = datetime.strptime(ts[:14], "%Y%m%d%H%M%S")
        dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%B %d, %Y")
    except Exception:
        return ts


def ts_age(ts: str) -> str:
    """Return human-readable age of a Wayback timestamp."""
    try:
        dt = datetime.strptime(ts[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days = (now - dt).days
        if days < 30:
            return f"{days} days ago"
        elif days < 365:
            return f"{days // 30} months ago"
        else:
            return f"{days // 365} years ago"
    except Exception:
        return ""


def get_wayback_available(video_id: str) -> Optional[dict]:
    """
    Fast Wayback availability check — returns the single closest snapshot.
    Usually responds in 2-3 seconds, much faster than CDX.
    """
    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        resp = requests.get(
            "https://archive.org/wayback/available",
            params={"url": yt_url},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        closest = data.get("archived_snapshots", {}).get("closest", {})
        if closest.get("available") and closest.get("url"):
            ts = closest.get("timestamp", "")
            return {
                "timestamp": ts,
                "url": closest["url"],
                "date_formatted": format_timestamp(ts),
                "age": ts_age(ts),
            }
    except Exception as e:
        log.warning(f"Wayback availability API failed for {video_id}: {e}")
    return None


def search_wayback(video_id: str, limit: int = 8) -> list[dict]:
    """
    Query the CDX API for all snapshots of a YouTube video.
    Returns a list of {timestamp, url, date_formatted, age} dicts.
    Retries once with a longer timeout before giving up.
    """
    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    cdx_url = (
        "https://web.archive.org/cdx/search/cdx"
        f"?url={requests.utils.quote(yt_url)}"
        "&output=json"
        "&fl=timestamp,original,statuscode"
        "&filter=statuscode:200"
        f"&limit={limit}"
        "&collapse=timestamp:8"   # one per day
    )
    for attempt, timeout in enumerate([8, 14]):
        try:
            resp = requests.get(cdx_url, timeout=timeout)
            resp.raise_for_status()
            rows = resp.json()
            if not rows or len(rows) <= 1:
                return []
            results = []
            for row in rows[1:]:
                ts = row[0]
                wayback_url = f"https://web.archive.org/web/{ts}/https://www.youtube.com/watch?v={video_id}"
                results.append({
                    "timestamp": ts,
                    "url": wayback_url,
                    "date_formatted": format_timestamp(ts),
                    "age": ts_age(ts),
                })
            results.sort(key=lambda x: x["timestamp"], reverse=True)
            return results
        except requests.exceptions.Timeout:
            if attempt == 0:
                log.warning(f"CDX timeout (8s) for {video_id}, retrying with 14s…")
                continue
            log.warning(f"CDX search timed out for {video_id} after both attempts")
        except Exception as e:
            log.warning(f"CDX search failed for {video_id}: {e}")
            break
    return []


def search_archives_robust(video_id: str) -> list[dict]:
    """
    Run CDX search and the availability API in parallel.
    CDX gives multiple snapshots; availability API is a fast fallback.
    Returns merged, deduped list sorted newest-first.
    """
    with ThreadPoolExecutor(max_workers=2) as executor:
        cdx_future = executor.submit(search_wayback, video_id, 8)
        avail_future = executor.submit(get_wayback_available, video_id)

        archives: list[dict] = []
        avail_entry: Optional[dict] = None

        try:
            archives = cdx_future.result(timeout=25) or []
        except Exception:
            archives = []

        try:
            avail_entry = avail_future.result(timeout=10)
        except Exception:
            avail_entry = None

    if archives:
        # Availability API may give a newer/different snapshot — add if unique
        if avail_entry:
            existing_days = {a["timestamp"][:8] for a in archives}
            if avail_entry["timestamp"][:8] not in existing_days:
                archives.insert(0, avail_entry)
        return archives

    if avail_entry:
        log.info(f"CDX empty/failed for {video_id}, used availability API fallback")
        return [avail_entry]

    return []


def get_video_info_ytdlp(url: str) -> Optional[dict]:
    """
    Use yt-dlp in simulation mode to extract video metadata.
    Returns a dict with title, thumbnail, duration, uploader, etc.
    Returns None on failure.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "socket_timeout": 20,
        "extractor_args": {
            "youtube": {"player_skip": ["webpage"]},
        },
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None
            return {
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader") or info.get("channel"),
                "upload_date": info.get("upload_date"),
                "view_count": info.get("view_count"),
                "video_id": info.get("id"),
            }
    except Exception as e:
        log.info(f"yt-dlp info extraction failed: {e}")
        return None


def cleanup_old_files():
    """Delete downloaded files older than MAX_FILE_AGE_SECONDS."""
    now = time.time()
    for path in DOWNLOAD_DIR.glob("*"):
        try:
            if path.is_file() and (now - path.stat().st_mtime) > MAX_FILE_AGE_SECONDS:
                path.unlink()
                log.info(f"Cleaned up old file: {path.name}")
            elif path.is_dir() and (now - path.stat().st_mtime) > MAX_FILE_AGE_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
                log.info(f"Cleaned up old dir: {path.name}")
        except Exception:
            pass

    # Also prune old job records
    with store_lock:
        cutoff = now - JOB_TTL_SECONDS
        expired = [jid for jid, j in jobs.items() if j.get("created_at", 0) < cutoff]
        for jid in expired:
            del jobs[jid]


# ════════════════════════════════════════════
# BACKGROUND DOWNLOAD WORKER
# ════════════════════════════════════════════
def do_download(job_id: str, url: str, fmt: str):
    """Run in a background thread. Downloads video using yt-dlp."""
    job = jobs[job_id]
    job["status"] = "downloading"
    job["progress_pct"] = 0
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(job_dir / "%(title)s.%(ext)s")

    # Format selection
    if fmt == "mp4_720":
        format_str = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best"
        merge_fmt = "mp4"
    elif fmt == "mp4_1080":
        format_str = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best"
        merge_fmt = "mp4"
    elif fmt == "mp3":
        format_str = "bestaudio/best"
        merge_fmt = None
    else:
        format_str = FREE_MAX_QUALITY
        merge_fmt = "mp4"

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total * 100) if total else 0
            speed = d.get("_speed_str", "")
            eta = d.get("_eta_str", "")
            job.update({
                "progress_pct": round(min(pct, 95), 1),
                "speed": speed,
                "eta": eta,
                "status_msg": f"Downloading… {speed}",
            })
        elif d["status"] == "finished":
            job.update({"status": "processing", "progress_pct": 95, "status_msg": "Merging streams with ffmpeg…"})

    ydl_opts = {
        "format": format_str,
        "outtmpl": output_template,
        "merge_output_format": merge_fmt,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "max_filesize": MAX_FILESIZE_FREE if fmt == "mp4_720" else None,
        "postprocessors": (
            [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
            if fmt == "mp3" else []
        ),
        "extractor_args": {
            "youtubewebarchive": {"check_all": ["thumbnails"]},
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Find the output file
        files = list(job_dir.glob("*"))
        files = [f for f in files if f.is_file() and not f.name.endswith(".part")]
        if not files:
            job.update({"status": "error", "error": "Download finished but no output file was found. The archive snapshot may be incomplete."})
            return

        output_file = max(files, key=lambda f: f.stat().st_size)
        job.update({
            "status": "done",
            "progress_pct": 100,
            "file_path": str(output_file),
            "filename": output_file.name,
            "status_msg": "Done!",
        })
        log.info(f"Job {job_id}: done — {output_file.name} ({output_file.stat().st_size // 1024}KB)")

    except yt_dlp.utils.DownloadError as e:
        err_msg = str(e)
        if "No video formats found" in err_msg:
            err_msg = "No video formats available in this archive snapshot. Try a different snapshot or date."
        elif "Private video" in err_msg:
            err_msg = "This video was private when archived and cannot be recovered."
        elif "This video is not available" in err_msg:
            err_msg = "Video not available in this archive snapshot. Try an older or newer capture."
        elif "Requested format is not available" in err_msg:
            err_msg = "The requested quality is not available. Try 720p instead."
        job.update({"status": "error", "error": err_msg})
        log.warning(f"Job {job_id}: yt-dlp error — {err_msg}")
    except Exception as e:
        job.update({"status": "error", "error": f"Unexpected error: {str(e)}"})
        log.error(f"Job {job_id}: unexpected error — {e}", exc_info=True)

    # Cleanup
    cleanup_old_files()


# ════════════════════════════════════════════
# ROUTES
# ════════════════════════════════════════════
@app.get("/")
def root():
    return {"service": "WaybackRevive API", "status": "online", "version": "1.0.0"}

@app.get("/api/health")
def health():
    return {"service": "WaybackRevive API", "status": "online", "version": "1.0.0"}


@app.post("/api/check")
async def check_video(req: CheckRequest, request: Request):
    """
    Check if a video is available on YouTube or in the Internet Archive.
    Returns: status, video_info, archives list.
    """
    url = req.url.strip()
    video_id = req.video_id or extract_video_id(url)
    if not video_id and "archive.org" not in url:
        raise HTTPException(400, detail="Could not extract a YouTube video ID from the provided URL.")

    result = {
        "status": "unknown",
        "video_info": None,
        "archives": [],
        "message": "",
    }

    # ── If it's a direct Wayback URL, try to extract info directly ──
    if "archive.org" in url and video_id:
        log.info(f"Direct wayback URL — video_id={video_id}")
        # Search CDX + availability API in parallel for more snapshots
        archives = search_archives_robust(video_id)
        info = get_video_info_ytdlp(url)
        if not info and archives:
            info = get_video_info_ytdlp(archives[0]["url"])

        if info:
            info["status"] = "archived"
        result.update({
            "status": "archived" if info or archives else "not_found",
            "video_info": info,
            "archives": archives or [{"timestamp": "", "url": url, "date_formatted": "Provided URL", "age": ""}],
            "message": f"Found {len(archives)} archive snapshots." if archives else "Using the provided Wayback URL.",
        })
        return result

    # ── Try YouTube first (check if video is still live) ──
    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    log.info(f"Checking YouTube availability: {video_id}")
    info = get_video_info_ytdlp(yt_url)

    if info:
        # Video is live on YouTube
        info["status"] = "live"
        result.update({
            "status": "live",
            "video_info": info,
            "archives": [{"timestamp": "live", "url": yt_url, "date_formatted": "Live on YouTube", "age": "Now"}],
            "message": "Video is currently available on YouTube.",
        })
        return result

    # ── Video not live — search Internet Archive ──
    log.info(f"Video not on YouTube — searching Wayback Machine for {video_id}")
    archives = search_archives_robust(video_id)

    if not archives:
        result.update({
            "status": "not_found",
            "video_info": {"title": f"Video ID: {video_id}", "status": "deleted"},
            "archives": [],
            "message": (
                "This video was not found on YouTube and has no captures in the Internet Archive. "
                "It may have been deleted before the archive crawled it, or it may have been a private/unlisted video."
            ),
        })
        return result

    # Get info from the most recent archive snapshot
    info = get_video_info_ytdlp(archives[0]["url"])
    if info:
        info["status"] = "archived"
        info["archive_count"] = len(archives)
    else:
        info = {
            "title": f"Archived Video (ID: {video_id})",
            "status": "deleted",
            "archive_count": len(archives),
            "video_id": video_id,
        }

    result.update({
        "status": "archived",
        "video_info": info,
        "archives": archives,
        "message": f"Found {len(archives)} archive snapshot{'s' if len(archives) != 1 else ''} for this deleted video.",
    })
    return result


@app.post("/api/download")
async def start_download(req: DownloadRequest, request: Request):
    """
    Queue a download job. Returns job_id immediately.
    Frontend polls /api/status/{job_id} for progress.
    """
    ip = get_ip(request)
    url = req.url.strip()
    fmt = req.format

    # Pro formats require auth — for now, reject locked formats on free tier
    # In production, check a valid API key / session token here
    pro_formats = ("mp4_1080", "mp3")
    api_key = request.headers.get("x-api-key", "")
    is_pro = bool(api_key and api_key == os.environ.get("PRO_API_KEY", ""))

    if fmt in pro_formats and not is_pro:
        raise HTTPException(403, detail="This format requires a Pro subscription. Upgrade at waybackrevive.com/pricing")

    # Rate limit check
    allowed, remaining = check_rate_limit(ip)
    if not allowed and not is_pro:
        raise HTTPException(429, detail=f"Free daily limit reached (3 downloads/day). Upgrade to Pro for unlimited downloads.")

    # Validate URL
    if not (
        "youtube.com" in url
        or "youtu.be" in url
        or "archive.org" in url
    ):
        raise HTTPException(400, detail="URL must be a YouTube or Wayback Machine URL.")

    # Create job
    job_id = str(uuid.uuid4())
    with store_lock:
        jobs[job_id] = {
            "id": job_id,
            "url": url,
            "format": fmt,
            "status": "queued",
            "progress_pct": 0,
            "speed": "",
            "eta": "",
            "status_msg": "Queued…",
            "file_path": None,
            "filename": None,
            "error": None,
            "created_at": time.time(),
            "ip": ip,
        }

    # Record download count for rate limiting
    if not is_pro:
        record_download(ip)

    # Kick off background thread
    t = threading.Thread(target=do_download, args=(job_id, url, fmt), daemon=True)
    t.start()

    log.info(f"Job {job_id} queued — {fmt} — {url[:80]}")
    return {"job_id": job_id, "remaining_today": max(0, remaining - 1)}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    """Poll download progress for a job."""
    if job_id not in jobs:
        raise HTTPException(404, detail="Job not found or expired.")
    j = jobs[job_id]
    return {
        "job_id": job_id,
        "status": j["status"],
        "progress_pct": j["progress_pct"],
        "speed": j.get("speed", ""),
        "eta": j.get("eta", ""),
        "status_msg": j.get("status_msg", ""),
        "error": j.get("error"),
        "filename": j.get("filename"),
    }


@app.get("/api/file/{job_id}")
async def get_file(job_id: str, request: Request):
    """Stream the downloaded file to the client."""
    if job_id not in jobs:
        raise HTTPException(404, detail="Job not found or expired.")

    j = jobs[job_id]
    if j["status"] != "done":
        raise HTTPException(409, detail="Download is not yet complete.")

    file_path = Path(j["file_path"])
    if not file_path.exists():
        raise HTTPException(410, detail="File has expired and been deleted. Please download again.")

    filename = j["filename"] or file_path.name
    # Guess MIME type
    ext = file_path.suffix.lower()
    mime_map = {
        ".mp4": "video/mp4", ".mkv": "video/x-matroska",
        ".webm": "video/webm", ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4", ".ogg": "audio/ogg",
    }
    media_type = mime_map.get(ext, "application/octet-stream")

    # Stream file in chunks
    def iter_file(path, chunk=65536):
        with open(path, "rb") as f:
            while True:
                data = f.read(chunk)
                if not data:
                    break
                yield data

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(file_path.stat().st_size),
        "Cache-Control": "no-cache",
    }
    return StreamingResponse(iter_file(file_path), media_type=media_type, headers=headers)


# ════════════════════════════════════════════
# STARTUP CLEANUP
# ════════════════════════════════════════════
@app.on_event("startup")
async def startup_event():
    log.info("WaybackRevive API starting up…")
    cleanup_old_files()
    # Schedule periodic cleanup every 30 minutes
    def periodic_cleanup():
        while True:
            time.sleep(1800)
            cleanup_old_files()
    threading.Thread(target=periodic_cleanup, daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
