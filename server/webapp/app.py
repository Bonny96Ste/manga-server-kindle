from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote
from typing import Any, Dict, List, Optional, Sequence, Tuple

import fitz
from PIL import Image
from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, send_file, url_for

from anilist import best_title, fetch as anilist_fetch, safe_text, search as anilist_search
from manga import download_series, filter_chapters, get_all_chapters, sanitize_filename, search_manga

BASE_DIR = Path(os.environ.get("STACK_DIR", "/stack")).resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))).resolve()
LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", str(DATA_DIR / "library"))).resolve()
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", str(DATA_DIR / "downloads"))).resolve()
STATE_DIR = Path(os.environ.get("STATE_DIR", str(DATA_DIR / "state"))).resolve()
CACHE_DIR = STATE_DIR / "pdf-cache"
CHAPTER_CACHE_DIR = STATE_DIR / "chapter-cache"
KINDLE_CACHE_DIR = STATE_DIR / "kindle-cache"
JOBS_FILE = STATE_DIR / "jobs.json"
EVENTS_FILE = STATE_DIR / "events.json"
WATCH_SCAN_SECONDS = max(300, int(os.environ.get("WATCH_SCAN_SECONDS", "300")))
PDF_RENDER_SCALE = max(0.8, min(3.0, float(os.environ.get("PDF_RENDER_SCALE", "1.6"))))
KINDLE_PDF_MODE = os.environ.get("KINDLE_PDF_MODE", "original").strip().lower()
if KINDLE_PDF_MODE not in {"original", "balanced"}:
    KINDLE_PDF_MODE = "original"
KINDLE_RENDER_WIDTH = max(600, min(2400, int(os.environ.get("KINDLE_RENDER_WIDTH", "1200"))))
KINDLE_RENDER_MAX_HEIGHT = max(800, min(3200, int(os.environ.get("KINDLE_RENDER_MAX_HEIGHT", "1600"))))
KINDLE_JPEG_QUALITY = max(60, min(95, int(os.environ.get("KINDLE_JPEG_QUALITY", "86"))))
KINDLE_CACHE_MAX_BYTES = max(256, int(os.environ.get("KINDLE_CACHE_MAX_MB", "2048"))) * 1024 * 1024
KINDLE_API_TOKEN = os.environ.get("KINDLE_API_TOKEN", "").strip()
HIDDEN_MARKER = ".mangadl-hidden"
AUTH_USERNAME = os.environ.get("APP_USERNAME", "").strip()
AUTH_PASSWORD = os.environ.get("APP_PASSWORD", "")

for directory in (LIBRARY_DIR, DOWNLOAD_DIR, STATE_DIR, CACHE_DIR, CHAPTER_CACHE_DIR, KINDLE_CACHE_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-mangadl-secret")


@app.before_request
def optional_basic_auth() -> Optional[Response]:
    is_kindle_api = request.path.startswith("/api/kindle/")
    if is_kindle_api and KINDLE_API_TOKEN:
        supplied_token = request.headers.get("X-MangaDL-Token", "")
        if supplied_token and hmac.compare_digest(supplied_token, KINDLE_API_TOKEN):
            return None
        if not AUTH_USERNAME:
            return jsonify({"error": "A valid Kindle API token is required"}), 401

    if not AUTH_USERNAME:
        return None
    auth = request.authorization
    username_ok = bool(auth and hmac.compare_digest(auth.username or "", AUTH_USERNAME))
    password_ok = bool(auth and hmac.compare_digest(auth.password or "", AUTH_PASSWORD))
    if username_ok and password_ok:
        return None
    if is_kindle_api:
        return jsonify({"error": "Authentication required"}), 401
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="Manga Library"'},
    )


@dataclass
class Job:
    id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    output: str
    series_path: str = ""
    chapter_num: Optional[float] = None
    message: str = ""
    progress_current: int = 0
    progress_total: int = 0
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    reader_url: Optional[str] = None


JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
ACTIVE_SERIES: set[str] = set()
ACTIVE_SERIES_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
WATCHER_LOCK = threading.Lock()
WATCHER_STARTED = False
EVENTS_LOCK = threading.Lock()

CHAPTER_PATTERNS = (
    re.compile(r"(?:^|[\s._-])(?:chapter|chap|ch)[\s._-]*(?P<num>\d+(?:\.\d+)?)", re.IGNORECASE),
    re.compile(r"^(?P<num>\d+(?:\.\d+)?)$", re.IGNORECASE),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fallback = {} if default is None else dict(default)
    if not path.exists():
        return fallback
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else fallback
    except Exception:
        return fallback


def append_event(event_type: str, title: str, detail: str = "", series_path: str = "") -> None:
    event = {
        "id": uuid.uuid4().hex[:12],
        "type": event_type,
        "title": title,
        "detail": detail,
        "series_path": series_path,
        "created_at": now_iso(),
    }
    with EVENTS_LOCK:
        payload = read_json(EVENTS_FILE, {"events": []})
        events = payload.get("events") if isinstance(payload.get("events"), list) else []
        events.insert(0, event)
        atomic_write_json(EVENTS_FILE, {"events": events[:200]})


def list_events(limit: int = 50) -> List[Dict[str, Any]]:
    payload = read_json(EVENTS_FILE, {"events": []})
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    return [event for event in events[:limit] if isinstance(event, dict)]


def persist_jobs_locked() -> None:
    jobs = sorted((asdict(job) for job in JOBS.values()), key=lambda item: item["created_at"], reverse=True)[:100]
    atomic_write_json(JOBS_FILE, {"jobs": jobs})


def load_persisted_jobs() -> None:
    payload = read_json(JOBS_FILE, {"jobs": []})
    rows = payload.get("jobs") if isinstance(payload.get("jobs"), list) else []
    with JOBS_LOCK:
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                job = Job(**{key: row.get(key) for key in Job.__dataclass_fields__})
            except TypeError:
                continue
            if job.status in {"queued", "running"}:
                job.status = "error"
                job.error = "Interrupted by an application restart"
                job.message = "Interrupted"
                job.updated_at = now_iso()
            JOBS[job.id] = job
        persist_jobs_locked()


def relpath(target: Path, base: Path = LIBRARY_DIR) -> str:
    return target.resolve().relative_to(base.resolve()).as_posix()


def resolve_under(base: Path, relative: str) -> Path:
    candidate = (base / relative).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError("Invalid path")
    return candidate


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def render_desc(metadata: Dict[str, Any]) -> str:
    return safe_text(metadata.get("description") or "")


def load_metadata(series_dir: Path) -> Dict[str, Any]:
    return read_json(series_dir / "metadata.json", {})


def save_metadata(series_dir: Path, metadata: Dict[str, Any]) -> None:
    metadata["updated_at"] = now_iso()
    atomic_write_json(series_dir / "metadata.json", metadata)


def normalize_source(metadata: Dict[str, Any]) -> Dict[str, Any]:
    raw = metadata.get("source")
    source = dict(raw) if isinstance(raw, dict) else {}
    if isinstance(raw, str) and raw:
        source.setdefault("provider", raw)
    for key in ("title", "series_id", "slug", "url", "anilist_id"):
        value = metadata.get(key)
        if value not in (None, ""):
            source.setdefault(key, value)
    return source


def all_series_dirs() -> List[Path]:
    if not LIBRARY_DIR.exists():
        return []
    return [
        path
        for path in sorted(LIBRARY_DIR.iterdir(), key=lambda item: item.name.lower())
        if path.is_dir() and not path.name.startswith(".")
    ]


def list_series_dirs() -> List[Path]:
    return [path for path in all_series_dirs() if not (path / HIDDEN_MARKER).exists()]


def chapter_num_from_name(name: str) -> Optional[float]:
    stem = Path(name).stem.strip()
    for pattern in CHAPTER_PATTERNS:
        match = pattern.search(stem)
        if match:
            try:
                return float(match.group("num"))
            except ValueError:
                return None
    return None


def chapter_key(number: float) -> str:
    return f"chapter-{number:g}"


def parse_chapter_key(key: str) -> float:
    number = chapter_num_from_name(key)
    if number is None:
        raise ValueError("Invalid chapter key")
    return number


def pdf_preference(path: Path, number: float) -> Tuple[int, float, int]:
    canonical = path.stem.lower() == chapter_key(number).lower()
    return (1 if canonical else 0, path.stat().st_mtime, path.stat().st_size)


def local_chapter_map(series_dir: Path) -> Dict[float, Dict[str, Any]]:
    chapters: Dict[float, Dict[str, Any]] = {}
    if not series_dir.exists():
        return chapters
    for path in series_dir.rglob("*.pdf"):
        if not path.is_file() or ".mangadl-temp" in path.parts:
            continue
        number = chapter_num_from_name(path.name)
        if number is None:
            continue
        existing = chapters.get(number)
        if existing is None or pdf_preference(path, number) > pdf_preference(existing["path"], number):
            chapters[number] = {
                "kind": "pdf",
                "path": path,
                "downloaded": True,
                "size": path.stat().st_size,
            }
    return chapters


def cleanup_legacy_images(series_dir: Path) -> int:
    pdf_numbers = set(local_chapter_map(series_dir))
    removed = 0
    for child in list(series_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        number = chapter_num_from_name(child.name)
        if number is None or number not in pdf_numbers:
            continue
        has_images = any(
            item.is_file() and item.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            for item in child.iterdir()
        )
        if has_images:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def cleanup_all_legacy_images() -> None:
    for series_dir in all_series_dirs():
        try:
            cleanup_legacy_images(series_dir)
        except Exception:
            continue


def chapter_cache_path(series_id: str) -> Path:
    digest = hashlib.sha256(series_id.encode("utf-8")).hexdigest()[:20]
    return CHAPTER_CACHE_DIR / f"{digest}.json"


def remote_chapters(series_id: str, force: bool = False, max_age_seconds: int = 300) -> List[Tuple[float, str]]:
    if not series_id:
        return []
    cache_path = chapter_cache_path(series_id)
    cached = read_json(cache_path, {})
    fetched_at = parse_iso(cached.get("fetched_at"))
    if not force and fetched_at and datetime.now(timezone.utc) - fetched_at < timedelta(seconds=max_age_seconds):
        rows = cached.get("chapters") or []
        return [(float(row[0]), str(row[1])) for row in rows if isinstance(row, list) and len(row) == 2]
    try:
        rows = get_all_chapters(series_id)
        atomic_write_json(cache_path, {"fetched_at": now_iso(), "chapters": [[number, url] for number, url in rows]})
        return rows
    except Exception:
        rows = cached.get("chapters") or []
        return [(float(row[0]), str(row[1])) for row in rows if isinstance(row, list) and len(row) == 2]


def default_watch() -> Dict[str, Any]:
    return {
        "enabled": False,
        "auto_download": True,
        "output_format": "pdf",
        "mode": "new",
        "baseline_latest": None,
        "interval_minutes": max(5, WATCH_SCAN_SECONDS // 60),
        "last_checked_at": None,
        "last_auto_download_at": None,
    }


def series_from_metadata(series_dir: Path) -> Dict[str, str]:
    metadata = load_metadata(series_dir)
    source = normalize_source(metadata)
    return {
        "title": str(metadata.get("title") or source.get("title") or series_dir.name),
        "series_id": str(source.get("series_id") or ""),
        "slug": str(source.get("slug") or ""),
        "url": str(source.get("url") or ""),
    }


def series_display(series_dir: Path, force_remote: bool = False) -> Dict[str, Any]:
    metadata = load_metadata(series_dir)
    source = normalize_source(metadata)
    series_id = str(source.get("series_id") or "")
    title = str(metadata.get("title") or source.get("title") or series_dir.name)
    remote = remote_chapters(series_id, force=force_remote) if series_id else []
    local = local_chapter_map(series_dir)
    remote_map = {number: url for number, url in remote}
    all_numbers = sorted(set(remote_map) | set(local), reverse=True)
    watch = metadata.get("watch") if isinstance(metadata.get("watch"), dict) else default_watch()
    metadata_block = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
    reading = metadata.get("reading") if isinstance(metadata.get("reading"), dict) else {}
    library_settings = metadata.get("library") if isinstance(metadata.get("library"), dict) else {}

    read_numbers: set[float] = set()
    for value in reading.get("read_chapters") or []:
        try:
            read_numbers.add(float(value))
        except (TypeError, ValueError):
            continue
    downloaded_numbers = sorted(local)
    unread_downloaded = [number for number in downloaded_numbers if number not in read_numbers]
    last_chapter = None
    try:
        if reading.get("last_chapter") is not None:
            last_chapter = float(reading["last_chapter"])
    except (TypeError, ValueError):
        last_chapter = None

    continue_chapter: Optional[float] = None
    if last_chapter in local and last_chapter not in read_numbers:
        continue_chapter = last_chapter
    elif last_chapter is not None:
        continue_chapter = next((number for number in unread_downloaded if number > last_chapter), None)
    if continue_chapter is None and unread_downloaded:
        continue_chapter = unread_downloaded[0]
    if continue_chapter is None and downloaded_numbers:
        continue_chapter = downloaded_numbers[-1]

    interval_raw = watch.get("interval_minutes", WATCH_SCAN_SECONDS // 60)
    try:
        interval_minutes = max(5, int(interval_raw))
    except (TypeError, ValueError):
        interval_minutes = max(5, WATCH_SCAN_SECONDS // 60)

    return {
        "path": relpath(series_dir),
        "name": series_dir.name,
        "title": title,
        "series_id": series_id,
        "source": source,
        "metadata": metadata,
        "metadata_block": metadata_block,
        "cover": metadata.get("coverImage") or metadata_block.get("coverImage"),
        "downloaded_count": len(local),
        "downloaded_numbers": downloaded_numbers,
        "downloaded_size": sum(item["size"] for item in local.values()),
        "read_count": len(read_numbers & set(downloaded_numbers)),
        "unread_count": len(unread_downloaded),
        "continue_chapter": continue_chapter,
        "last_read_at": reading.get("last_read_at"),
        "pinned": bool(library_settings.get("pinned")),
        "remote_chapters": len(remote),
        "remote_latest": max(remote_map) if remote_map else (max(local) if local else None),
        "missing_count": len([number for number in remote_map if number not in local]),
        "remote_chapter_rows": [
            {
                "number": number,
                "key": chapter_key(number),
                "downloaded": number in local,
                "read": number in read_numbers,
                "size": local[number]["size"] if number in local else None,
            }
            for number in all_numbers
        ],
        "watch": {
            "enabled": bool(watch.get("enabled")),
            "auto_download": bool(watch.get("auto_download", True)),
            "output_format": "pdf",
            "mode": watch.get("mode", "new") if watch.get("mode") in {"new", "missing"} else "new",
            "baseline_latest": watch.get("baseline_latest"),
            "interval_minutes": interval_minutes,
            "last_checked_at": watch.get("last_checked_at"),
            "last_auto_download_at": watch.get("last_auto_download_at"),
        },
    }


def ensure_series_dir(title: str, series_id: str) -> Path:
    for existing in all_series_dirs():
        source = normalize_source(load_metadata(existing))
        if str(source.get("series_id") or "") == series_id:
            (existing / HIDDEN_MARKER).unlink(missing_ok=True)
            return existing
    safe_title = sanitize_filename(title or "Untitled")
    series_dir = LIBRARY_DIR / f"{safe_title} [{series_id}]"
    series_dir.mkdir(parents=True, exist_ok=True)
    return series_dir


def best_anilist_match(query: str, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    query_norm = query.lower().strip()
    best: Optional[Dict[str, Any]] = None
    best_score = -1.0
    for item in candidates:
        title = best_title(item)
        score = SequenceMatcher(None, query_norm, title.lower().strip()).ratio()
        if query_norm and query_norm in title.lower():
            score += 0.2
        if score > best_score:
            best, best_score = item, score
    return best


def enrich_search_results(results: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    existing_map = {
        str(normalize_source(load_metadata(path)).get("series_id") or ""): relpath(path)
        for path in list_series_dirs()
    }
    enriched: List[Dict[str, Any]] = []
    for item in results:
        try:
            candidates = anilist_search(item["title"], per_page=4)
        except Exception:
            candidates = []
        match = best_anilist_match(item["title"], candidates)
        payload: Dict[str, Any] = {**item, "already_added": item.get("series_id") in existing_map, "existing_path": existing_map.get(item.get("series_id", ""))}
        if match:
            title_info = match.get("title") or {}
            payload.update(
                {
                    "anilist_id": match.get("id"),
                    "meta_title": best_title(match),
                    "meta_title_romaji": title_info.get("romaji"),
                    "meta_cover": (match.get("coverImage") or {}).get("large"),
                    "meta_description": safe_text(match.get("description") or ""),
                    "meta_status": match.get("status"),
                    "meta_chapters": match.get("chapters"),
                    "meta_volumes": match.get("volumes"),
                    "meta_score": match.get("averageScore"),
                    "meta_genres": match.get("genres") or [],
                    "meta_site_url": match.get("siteUrl"),
                }
            )
        enriched.append(payload)
    return enriched


def new_job(title: str, output: str, series_path: str = "", chapter_num: Optional[float] = None) -> Job:
    job = Job(
        id=uuid.uuid4().hex[:12],
        title=title,
        status="queued",
        created_at=now_iso(),
        updated_at=now_iso(),
        output=output,
        series_path=series_path,
        chapter_num=chapter_num,
    )
    with JOBS_LOCK:
        JOBS[job.id] = job
        persist_jobs_locked()
    return job


def update_job(job_id: str, **changes: Any) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = now_iso()
        persist_jobs_locked()


def job_or_404(job_id: str) -> Job:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    return job


def mark_series_active(series_path: str, active: bool) -> None:
    with ACTIVE_SERIES_LOCK:
        if active:
            ACTIVE_SERIES.add(series_path)
        else:
            ACTIVE_SERIES.discard(series_path)


def series_is_active(series_path: str) -> bool:
    with ACTIVE_SERIES_LOCK:
        return series_path in ACTIVE_SERIES


def update_series_download_state(series_dir: Path, manifest: Dict[str, Any]) -> None:
    metadata = load_metadata(series_dir)
    source = normalize_source(metadata)
    source.update(
        {
            "series_id": manifest.get("series_id") or source.get("series_id"),
            "slug": manifest.get("series_slug") or source.get("slug"),
            "url": manifest.get("series_url") or source.get("url"),
            "title": manifest.get("title") or source.get("title"),
            "provider": "WeebCentral",
        }
    )
    metadata["source"] = source
    metadata["title"] = manifest.get("title") or metadata.get("title") or series_dir.name
    metadata["downloaded_chapters"] = sorted(local_chapter_map(series_dir))
    metadata["last_downloaded_at"] = now_iso()
    watch = metadata.get("watch") if isinstance(metadata.get("watch"), dict) else default_watch()
    watch["output_format"] = "pdf"
    metadata["watch"] = watch
    save_metadata(series_dir, metadata)


def run_download_job(
    job_id: str,
    series_dir: Path,
    series: Dict[str, str],
    chapter_range: str,
    chapter_numbers: Optional[Sequence[float]] = None,
) -> None:
    series_path = relpath(series_dir)
    try:
        update_job(job_id, status="running", message="Preparing download")
        mark_series_active(series_path, True)

        def progress(payload: Dict[str, Any]) -> None:
            update_job(
                job_id,
                message=str(payload.get("message") or "Downloading"),
                progress_current=int(payload.get("image_index") or 0),
                progress_total=int(payload.get("image_total") or 0),
            )

        manifest = download_series(
            series,
            chapter_range,
            "pdf",
            DOWNLOAD_DIR,
            progress_cb=progress,
            target_dir=series_dir,
            chapter_numbers=chapter_numbers,
        )
        cleanup_legacy_images(series_dir)
        update_series_download_state(series_dir, manifest)
        with JOBS_LOCK:
            chapter_num = JOBS[job_id].chapter_num
        encoded_series = quote(series_path, safe="/")
        reader_url = (
            f"/series/{encoded_series}/chapter/{chapter_key(chapter_num)}/read"
            if chapter_num is not None
            else f"/series/{encoded_series}"
        )
        update_job(job_id, status="done", message="Ready", result=manifest, reader_url=reader_url)
        append_event("download", f"Downloaded {series.get('title') or series_dir.name}", f"Range {chapter_range}", series_path)
    except Exception as exc:
        update_job(job_id, status="error", message="Download failed", error=str(exc))
        append_event("error", f"Download failed: {series.get('title') or series_dir.name}", str(exc), series_path)
    finally:
        mark_series_active(series_path, False)


def queue_download(
    series_dir: Path,
    series: Dict[str, str],
    chapter_range: str,
    chapter_numbers: Optional[Sequence[float]] = None,
    title_prefix: str = "Download",
    chapter_num: Optional[float] = None,
) -> Job:
    job = new_job(
        f"{title_prefix}: {series.get('title') or series_dir.name}",
        str(series_dir),
        relpath(series_dir),
        chapter_num,
    )
    thread = threading.Thread(
        target=run_download_job,
        args=(job.id, series_dir, series, chapter_range, chapter_numbers),
        daemon=True,
    )
    thread.start()
    return job


def refresh_series_metadata(series_dir: Path) -> None:
    metadata = load_metadata(series_dir)
    source = normalize_source(metadata)
    query = str(metadata.get("title") or source.get("title") or series_dir.name)
    media_id = source.get("anilist_id")
    try:
        data = anilist_fetch(int(media_id)) if media_id else best_anilist_match(query, anilist_search(query, per_page=6)) or {}
    except Exception:
        data = {}
    if data:
        metadata["metadata"] = {
            "source": "AniList",
            "source_id": data.get("id"),
            "title": best_title(data),
            "description": data.get("description") or "",
            "status": data.get("status"),
            "chapters": data.get("chapters"),
            "volumes": data.get("volumes"),
            "averageScore": data.get("averageScore"),
            "siteUrl": data.get("siteUrl"),
            "genres": data.get("genres") or [],
            "coverImage": (data.get("coverImage") or {}).get("large"),
            "synced_at": now_iso(),
        }
        metadata["coverImage"] = metadata["metadata"].get("coverImage") or metadata.get("coverImage")
        source["anilist_id"] = data.get("id")
        metadata["source"] = source
    save_metadata(series_dir, metadata)


def chapter_pdf_info(pdf_path: Path) -> Dict[str, Any]:
    with fitz.open(pdf_path) as document:
        return {"page_count": document.page_count}


def pdf_cache_directory(pdf_path: Path) -> Path:
    stat = pdf_path.stat()
    token = f"{relpath(pdf_path)}:{stat.st_mtime_ns}:{stat.st_size}:{PDF_RENDER_SCALE}"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return CACHE_DIR / digest


def render_pdf_page(pdf_path: Path, page_index: int) -> Path:
    cache_dir = pdf_cache_directory(pdf_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    output = cache_dir / f"{page_index:04d}.png"
    if output.exists():
        return output
    with fitz.open(pdf_path) as document:
        if page_index < 0 or page_index >= document.page_count:
            raise IndexError("Page out of range")
        page = document.load_page(page_index)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(PDF_RENDER_SCALE, PDF_RENDER_SCALE), alpha=False)
        temporary = output.with_suffix(".png.tmp")
        temporary.write_bytes(pixmap.tobytes("png"))
        temporary.replace(output)
    return output


def reader_context(series: Dict[str, Any], chapter_num: float) -> Dict[str, Any]:
    rows = series.get("remote_chapter_rows") or []
    index = next((idx for idx, row in enumerate(rows) if row["number"] == chapter_num), None)
    previous = rows[index + 1] if index is not None and index + 1 < len(rows) else None
    next_item = rows[index - 1] if index is not None and index > 0 else None
    current = rows[index] if index is not None else {"number": chapter_num, "read": False, "downloaded": False}
    return {
        "chapter_options": rows,
        "current_row": current,
        "current_key": chapter_key(chapter_num),
        "prev_url": url_for("chapter_read", series_path=series["path"], chapter_id=previous["key"]) if previous else None,
        "next_url": url_for("chapter_read", series_path=series["path"], chapter_id=next_item["key"]) if next_item else None,
        "series_url": url_for("series_view", series_path=series["path"]),
        "download_url": url_for("chapter_download", series_path=series["path"], chapter_id=chapter_key(chapter_num)),
        "progress_url": url_for("series_progress", series_path=series["path"]),
    }


def chapter_range_from_numbers(numbers: Sequence[float]) -> str:
    return ",".join(f"{number:g}" for number in sorted(set(numbers))) or "all"


def watcher_tick() -> None:
    now = datetime.now(timezone.utc)
    for series_dir in list_series_dirs():
        metadata = load_metadata(series_dir)
        watch = metadata.get("watch") if isinstance(metadata.get("watch"), dict) else {}
        if not watch.get("enabled"):
            continue
        try:
            interval = max(5, int(watch.get("interval_minutes", WATCH_SCAN_SECONDS // 60)))
        except (TypeError, ValueError):
            interval = max(5, WATCH_SCAN_SECONDS // 60)
        last_checked = parse_iso(watch.get("last_checked_at"))
        if last_checked and now - last_checked < timedelta(minutes=interval):
            continue
        source = normalize_source(metadata)
        series_id = str(source.get("series_id") or "")
        series_path = relpath(series_dir)
        if not series_id or series_is_active(series_path):
            continue

        remote = remote_chapters(series_id, force=True)
        remote_numbers = [number for number, _ in remote]
        local_numbers = set(local_chapter_map(series_dir))
        latest = max(remote_numbers) if remote_numbers else None
        mode = watch.get("mode", "new") if watch.get("mode") in {"new", "missing"} else "new"

        if mode == "new":
            baseline = watch.get("baseline_latest")
            try:
                baseline_number = float(baseline) if baseline is not None else latest
            except (TypeError, ValueError):
                baseline_number = latest
            missing = [number for number in remote_numbers if number not in local_numbers and (baseline_number is None or number > baseline_number)]
            watch["baseline_latest"] = latest
        else:
            missing = [number for number in remote_numbers if number not in local_numbers]

        signature = chapter_range_from_numbers(missing) if missing else ""
        should_log_missing = bool(missing) and signature != watch.get("last_missing_signature")
        watch["last_missing_signature"] = signature
        watch["last_checked_at"] = now_iso()
        watch["output_format"] = "pdf"
        metadata["watch"] = watch
        save_metadata(series_dir, metadata)

        if should_log_missing:
            append_event("chapter", f"New chapters found for {series_from_metadata(series_dir)['title']}", signature, series_path)
        if missing and watch.get("auto_download", True):
            job = queue_download(
                series_dir,
                series_from_metadata(series_dir),
                chapter_range_from_numbers(missing),
                chapter_numbers=missing,
                title_prefix="Auto-download",
            )
            watch["last_auto_download_at"] = now_iso()
            metadata["last_watch_job_id"] = job.id
            metadata["watch"] = watch
            save_metadata(series_dir, metadata)


def watch_loop() -> None:
    cleanup_all_legacy_images()
    while not STOP_EVENT.is_set():
        try:
            watcher_tick()
        except Exception:
            pass
        STOP_EVENT.wait(WATCH_SCAN_SECONDS)


def start_watcher_once() -> None:
    global WATCHER_STARTED
    with WATCHER_LOCK:
        if WATCHER_STARTED:
            return
        WATCHER_STARTED = True
    threading.Thread(target=watch_loop, daemon=True).start()


@app.route("/")
@app.route("/library")
def library_home():
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "activity").strip().lower()
    library = [series_display(path) for path in list_series_dirs()]
    if q:
        term = q.lower()
        visible_library = [item for item in library if term in item["title"].lower() or term in item["path"].lower()]
    else:
        visible_library = library

    if sort == "title":
        visible_library.sort(key=lambda item: (not item["pinned"], item["title"].lower()))
    elif sort == "unread":
        visible_library.sort(key=lambda item: (not item["pinned"], -item["unread_count"], item["title"].lower()))
    elif sort == "latest":
        visible_library.sort(key=lambda item: (not item["pinned"], -(item["remote_latest"] or -1), item["title"].lower()))
    else:
        visible_library.sort(key=lambda item: (item["pinned"], item["last_read_at"] or ""), reverse=True)

    results = enrich_search_results(search_manga(q)[:8]) if q else []
    stats = {
        "series": len(library),
        "pdfs": sum(item["downloaded_count"] for item in library),
        "bytes": sum(item["downloaded_size"] for item in library),
        "unread": sum(item["unread_count"] for item in library),
        "watched": sum(1 for item in library if item["watch"]["enabled"]),
    }
    return render_template(
        "library.html",
        library=visible_library,
        q=q,
        sort=sort,
        results=results,
        stats=stats,
        events=list_events(8),
    )


@app.route("/search")
def search_page():
    return redirect(url_for("library_home", q=request.args.get("q", "").strip()))


@app.post("/library/add")
def library_add():
    title = request.form.get("title", "").strip()
    series_id = request.form.get("series_id", "").strip()
    if not title or not series_id:
        abort(400)
    series_dir = ensure_series_dir(title, series_id)
    metadata = load_metadata(series_dir)
    source = normalize_source(metadata)
    source.update(
        {
            "title": title,
            "series_id": series_id,
            "slug": request.form.get("slug", "").strip(),
            "url": request.form.get("url", "").strip(),
            "provider": "WeebCentral",
            "added_at": source.get("added_at") or now_iso(),
        }
    )
    metadata["source"] = source
    metadata["title"] = title
    metadata["watch"] = metadata.get("watch") if isinstance(metadata.get("watch"), dict) else default_watch()
    anilist_id = request.form.get("anilist_id", "").strip()
    if anilist_id:
        source["anilist_id"] = anilist_id
        metadata["source"] = source
        try:
            data = anilist_fetch(int(anilist_id))
        except Exception:
            data = {}
        if data:
            metadata["metadata"] = {
                "source": "AniList",
                "source_id": data.get("id"),
                "title": best_title(data),
                "description": data.get("description") or "",
                "status": data.get("status"),
                "chapters": data.get("chapters"),
                "volumes": data.get("volumes"),
                "averageScore": data.get("averageScore"),
                "siteUrl": data.get("siteUrl"),
                "genres": data.get("genres") or [],
                "coverImage": (data.get("coverImage") or {}).get("large"),
                "synced_at": now_iso(),
            }
            metadata["coverImage"] = metadata["metadata"].get("coverImage")
    save_metadata(series_dir, metadata)
    append_event("library", f"Added {title}", "Series added to the library", relpath(series_dir))
    flash(f"Added {title} to the library.", "success")
    return redirect(url_for("series_view", series_path=relpath(series_dir)))


@app.route("/series/<path:series_path>")
def series_view(series_path: str):
    series_dir = resolve_under(LIBRARY_DIR, series_path)
    if not series_dir.is_dir() or (series_dir / HIDDEN_MARKER).exists():
        abort(404)
    return render_template("series.html", series=series_display(series_dir))


@app.post("/series/<path:series_path>/refresh")
def series_refresh(series_path: str):
    series_dir = resolve_under(LIBRARY_DIR, series_path)
    if not series_dir.is_dir():
        abort(404)
    refresh_series_metadata(series_dir)
    source = normalize_source(load_metadata(series_dir))
    if source.get("series_id"):
        remote_chapters(str(source["series_id"]), force=True)
    flash("Series information refreshed.", "success")
    return redirect(url_for("series_view", series_path=series_path))


@app.post("/series/<path:series_path>/remove")
def series_remove(series_path: str):
    series_dir = resolve_under(LIBRARY_DIR, series_path)
    if not series_dir.is_dir():
        abort(404)
    if series_is_active(series_path):
        flash("This series is currently downloading. Try again when the job completes.", "error")
        destination = "archive_view" if (series_dir / HIDDEN_MARKER).exists() else "series_view"
        return redirect(url_for(destination, **({"series_path": series_path} if destination == "series_view" else {})))
    action = request.form.get("action", "keep").strip().lower()
    title = series_from_metadata(series_dir)["title"]
    if action == "delete":
        shutil.rmtree(series_dir)
        append_event("library", f"Deleted {title}", "Series and files deleted")
        flash(f"Removed {title} and deleted its files.", "success")
    else:
        (series_dir / HIDDEN_MARKER).write_text(now_iso(), encoding="utf-8")
        append_event("library", f"Archived {title}", "Files retained", series_path)
        flash(f"Removed {title} from the library. Its files were kept in Archive.", "success")
    return redirect(url_for("library_home"))


@app.post("/series/<path:series_path>/pin")
def series_pin(series_path: str):
    series_dir = resolve_under(LIBRARY_DIR, series_path)
    if not series_dir.is_dir():
        abort(404)
    metadata = load_metadata(series_dir)
    library_settings = metadata.get("library") if isinstance(metadata.get("library"), dict) else {}
    library_settings["pinned"] = not bool(library_settings.get("pinned"))
    metadata["library"] = library_settings
    save_metadata(series_dir, metadata)
    return redirect(request.referrer or url_for("series_view", series_path=series_path))


@app.post("/series/<path:series_path>/progress")
def series_progress(series_path: str):
    series_dir = resolve_under(LIBRARY_DIR, series_path)
    if not series_dir.is_dir():
        abort(404)
    payload = request.get_json(silent=True) or request.form
    try:
        number = float(payload.get("chapter_num"))
    except (TypeError, ValueError):
        abort(400)
    action = str(payload.get("action") or "open")
    metadata = load_metadata(series_dir)
    reading = metadata.get("reading") if isinstance(metadata.get("reading"), dict) else {}
    read_numbers: set[float] = set()
    for value in reading.get("read_chapters") or []:
        try:
            read_numbers.add(float(value))
        except (TypeError, ValueError):
            continue
    if action in {"complete", "read"}:
        read_numbers.add(number)
    elif action == "unread":
        read_numbers.discard(number)
    reading["read_chapters"] = sorted(read_numbers)
    reading["last_chapter"] = number
    reading["last_read_at"] = now_iso()
    reading["last_action"] = action
    metadata["reading"] = reading
    save_metadata(series_dir, metadata)
    return jsonify({"ok": True, "read": number in read_numbers, "chapter_num": number})


@app.post("/series/<path:series_path>/cleanup")
def series_cleanup(series_path: str):
    series_dir = resolve_under(LIBRARY_DIR, series_path)
    if not series_dir.is_dir():
        abort(404)
    removed = cleanup_legacy_images(series_dir)
    append_event("storage", f"Cleaned {series_from_metadata(series_dir)['title']}", f"Removed {removed} legacy image folders", series_path)
    flash(f"Storage cleanup complete. Removed {removed} redundant image folders.", "success")
    return redirect(url_for("series_view", series_path=series_path))


@app.post("/series/<path:series_path>/download-missing")
def series_download_missing(series_path: str):
    series_dir = resolve_under(LIBRARY_DIR, series_path)
    if not series_dir.is_dir():
        abort(404)
    source = normalize_source(load_metadata(series_dir))
    remote = remote_chapters(str(source.get("series_id") or ""), force=True)
    local = set(local_chapter_map(series_dir))
    missing = [number for number, _ in remote if number not in local]
    if not missing:
        flash("There are no missing chapters to download.", "success")
        return redirect(url_for("series_view", series_path=series_path))
    job = queue_download(
        series_dir,
        series_from_metadata(series_dir),
        chapter_range_from_numbers(missing),
        chapter_numbers=missing,
        title_prefix="Download missing",
    )
    return redirect(url_for("job_view", job_id=job.id))


@app.route("/archive")
def archive_view():
    archived = [series_display(path) for path in all_series_dirs() if (path / HIDDEN_MARKER).exists()]
    return render_template("archive.html", archived=archived)


@app.post("/archive/<path:series_path>/restore")
def archive_restore(series_path: str):
    series_dir = resolve_under(LIBRARY_DIR, series_path)
    if not series_dir.is_dir():
        abort(404)
    (series_dir / HIDDEN_MARKER).unlink(missing_ok=True)
    title = series_from_metadata(series_dir)["title"]
    append_event("library", f"Restored {title}", "Returned from Archive", series_path)
    flash(f"Restored {title} to the library.", "success")
    return redirect(url_for("series_view", series_path=series_path))


@app.route("/activity")
def activity_view():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda job: job.created_at, reverse=True)
    return render_template("activity.html", jobs=jobs, events=list_events(100))


@app.route("/library/export.json")
def library_export():
    payload = {
        "exported_at": now_iso(),
        "series": [series_display(path) for path in list_series_dirs()],
    }
    response = jsonify(payload)
    response.headers["Content-Disposition"] = 'attachment; filename="mangadl-library.json"'
    return response


@app.route("/health")
def health():
    return jsonify({"status": "ok", "series": len(list_series_dirs()), "time": now_iso()})


@app.post("/series/<path:series_path>/watch")
def series_watch(series_path: str):
    series_dir = resolve_under(LIBRARY_DIR, series_path)
    if not series_dir.is_dir():
        abort(404)
    metadata = load_metadata(series_dir)
    watch = metadata.get("watch") if isinstance(metadata.get("watch"), dict) else default_watch()
    was_enabled = bool(watch.get("enabled"))
    watch["enabled"] = request.form.get("enabled") == "on"
    watch["auto_download"] = request.form.get("auto_download") == "on"
    watch["output_format"] = "pdf"
    watch["mode"] = request.form.get("mode", "new") if request.form.get("mode") in {"new", "missing"} else "new"
    try:
        watch["interval_minutes"] = max(5, int(request.form.get("interval_minutes", "30")))
    except ValueError:
        watch["interval_minutes"] = 30
    if watch["enabled"] and watch["mode"] == "new" and (not was_enabled or watch.get("baseline_latest") is None):
        source = normalize_source(metadata)
        rows = remote_chapters(str(source.get("series_id") or ""), force=True)
        watch["baseline_latest"] = max((number for number, _ in rows), default=None)
    metadata["watch"] = watch
    save_metadata(series_dir, metadata)
    append_event("watch", f"Monitoring updated for {series_from_metadata(series_dir)['title']}", f"Mode: {watch['mode']}", series_path)
    flash("Watch settings updated.", "success")
    return redirect(url_for("series_view", series_path=series_path))


@app.post("/series/<path:series_path>/download")
def series_download(series_path: str):
    series_dir = resolve_under(LIBRARY_DIR, series_path)
    if not series_dir.is_dir():
        abort(404)
    chapter_range = request.form.get("chapter_range", "all").strip() or "all"
    job = queue_download(series_dir, series_from_metadata(series_dir), chapter_range, title_prefix="Download")
    return redirect(url_for("job_view", job_id=job.id))


@app.post("/series/<path:series_path>/chapter/<chapter_id>/download")
def chapter_download(series_path: str, chapter_id: str):
    series_dir = resolve_under(LIBRARY_DIR, series_path)
    if not series_dir.is_dir():
        abort(404)
    number = parse_chapter_key(chapter_id)
    job = queue_download(
        series_dir,
        series_from_metadata(series_dir),
        f"{number:g}",
        chapter_numbers=[number],
        title_prefix="Download chapter",
        chapter_num=number,
    )
    payload = {
        "job_id": job.id,
        "status_url": url_for("job_api", job_id=job.id),
        "job_url": url_for("job_view", job_id=job.id),
        "reader_url": url_for("chapter_read", series_path=series_path, chapter_id=chapter_key(number)),
    }
    if request.headers.get("X-Requested-With") == "fetch" or request.accept_mimetypes.best == "application/json":
        return jsonify(payload), 202
    return redirect(payload["job_url"])


@app.route("/series/<path:series_path>/chapter/<chapter_id>/read")
def chapter_read(series_path: str, chapter_id: str):
    series_dir = resolve_under(LIBRARY_DIR, series_path)
    if not series_dir.is_dir():
        abort(404)
    number = parse_chapter_key(chapter_id)
    series = series_display(series_dir)
    navigation = reader_context(series, number)
    local = local_chapter_map(series_dir).get(number)
    if not local:
        return render_template(
            "reader.html",
            series=series,
            chapter_num=number,
            mode="missing",
            page_count=0,
            current_downloaded=False,
            **navigation,
        )
    try:
        page_count = chapter_pdf_info(local["path"])["page_count"]
    except Exception as exc:
        return render_template(
            "reader.html",
            series=series,
            chapter_num=number,
            mode="error",
            reader_error=str(exc),
            page_count=0,
            current_downloaded=True,
            **navigation,
        )
    return render_template(
        "reader.html",
        series=series,
        chapter_num=number,
        mode="pdf",
        page_count=page_count,
        current_downloaded=True,
        **navigation,
    )


@app.route("/series/<path:series_path>/chapter/<chapter_id>/page/<int:page_index>")
def chapter_page(series_path: str, chapter_id: str, page_index: int):
    series_dir = resolve_under(LIBRARY_DIR, series_path)
    if not series_dir.is_dir():
        abort(404)
    number = parse_chapter_key(chapter_id)
    local = local_chapter_map(series_dir).get(number)
    if not local:
        abort(404)
    try:
        rendered = render_pdf_page(local["path"], page_index)
    except (IndexError, RuntimeError, ValueError):
        abort(404)
    return send_file(rendered, mimetype="image/png", conditional=True, max_age=86400)


@app.route("/viewer")
def viewer():
    requested = request.args.get("path", "").strip()
    if requested:
        target = resolve_under(LIBRARY_DIR, requested)
        if not target.is_file() or target.suffix.lower() != ".pdf":
            abort(404)
        number = chapter_num_from_name(target.name)
        if number is not None:
            for series_dir in list_series_dirs():
                if target == series_dir or series_dir in target.parents:
                    return redirect(url_for("chapter_read", series_path=relpath(series_dir), chapter_id=chapter_key(number)))
    flash("Open a chapter from its series page to use the integrated reader.", "success")
    return redirect(url_for("library_home"))


@app.route("/file/<path:item_path>")
def serve_file(item_path: str):
    target = resolve_under(LIBRARY_DIR, item_path)
    if not target.is_file() or target.suffix.lower() != ".pdf":
        abort(404)
    return send_file(target, conditional=True, as_attachment=True)


@app.route("/api/job/<job_id>")
def job_api(job_id: str):
    return jsonify(asdict(job_or_404(job_id)))


@app.route("/job/<job_id>")
def job_view(job_id: str):
    return render_template("job.html", job=job_or_404(job_id))


@app.route("/api/library")
def api_library():
    return jsonify([series_display(path) for path in list_series_dirs()])



# ---------------------------------------------------------------------------
# Kindle / KOReader API
# ---------------------------------------------------------------------------

def find_series_by_id(series_id: str, include_archived: bool = False) -> Optional[Path]:
    candidates = all_series_dirs() if include_archived else list_series_dirs()
    for series_dir in candidates:
        source = normalize_source(load_metadata(series_dir))
        if str(source.get("series_id") or "") == str(series_id):
            return series_dir
    return None


def kindle_cache_path(pdf_path: Path) -> Path:
    stat = pdf_path.stat()
    fingerprint = (
        f"{pdf_path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}:v2:"
        f"{KINDLE_PDF_MODE}:{KINDLE_RENDER_WIDTH}:{KINDLE_RENDER_MAX_HEIGHT}:{KINDLE_JPEG_QUALITY}"
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return KINDLE_CACHE_DIR / digest[:2] / f"{digest}.pdf"


def prune_kindle_cache(exclude: Optional[Path] = None) -> None:
    files = [path for path in KINDLE_CACHE_DIR.rglob("*.pdf") if path.is_file()]
    total = sum(path.stat().st_size for path in files)
    if total <= KINDLE_CACHE_MAX_BYTES:
        return
    target = int(KINDLE_CACHE_MAX_BYTES * 0.9)
    files.sort(key=lambda path: path.stat().st_mtime)
    for path in files:
        if exclude and path == exclude:
            continue
        try:
            size = path.stat().st_size
            path.unlink()
            total -= size
        except OSError:
            continue
        if total <= target:
            break


def optimize_pdf_for_kindle(pdf_path: Path) -> Path:
    """Create an optional fixed-page, high-quality grayscale Kindle copy.

    The previous converter created PDF pages whose physical dimensions matched
    each raster image.  On older KOReader/Kindle builds, chapters with mixed
    page dimensions could be laid out incorrectly after the first page.  This
    renderer uses a stable 600x800 (or 800x600) page box, fits the whole source
    page without cropping, and embeds a higher-resolution image for zooming.
    """
    output_path = kindle_cache_path(pdf_path)
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".pdf.part")
    temporary.unlink(missing_ok=True)

    with fitz.open(pdf_path) as source, fitz.open() as target:
        if source.page_count < 1:
            raise RuntimeError("The chapter PDF contains no pages")

        for page_index in range(source.page_count):
            page = source.load_page(page_index)
            rect = page.rect
            if rect.width <= 0 or rect.height <= 0:
                raise RuntimeError(f"Chapter PDF page {page_index + 1} has invalid dimensions")

            source_is_landscape = rect.width > rect.height
            page_width, page_height = (800.0, 600.0) if source_is_landscape else (600.0, 800.0)

            scale = min(
                KINDLE_RENDER_WIDTH / rect.width,
                KINDLE_RENDER_MAX_HEIGHT / rect.height,
            )
            scale = max(0.5, min(scale, 4.0))
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                colorspace=fitz.csGRAY,
                alpha=False,
                annots=False,
            )

            image = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
            image_buffer = io.BytesIO()
            image.save(
                image_buffer,
                format="JPEG",
                quality=KINDLE_JPEG_QUALITY,
                optimize=True,
                progressive=False,
                dpi=(150, 150),
            )
            image.close()

            target_page = target.new_page(width=page_width, height=page_height)
            fit_scale = min(page_width / pixmap.width, page_height / pixmap.height)
            draw_width = pixmap.width * fit_scale
            draw_height = pixmap.height * fit_scale
            left = (page_width - draw_width) / 2
            top = (page_height - draw_height) / 2
            image_rect = fitz.Rect(left, top, left + draw_width, top + draw_height)
            target_page.insert_image(
                image_rect,
                stream=image_buffer.getvalue(),
                keep_proportion=True,
                overlay=True,
            )

        target.set_metadata(
            {
                "title": pdf_path.stem,
                "producer": "MangaDL Kindle Bridge",
                "creator": "MangaDL Ultimate",
            }
        )
        target.save(temporary, garbage=4, deflate=True, clean=True)

    temporary.replace(output_path)
    prune_kindle_cache(exclude=output_path)
    return output_path


def kindle_delivery_path(pdf_path: Path) -> Path:
    """Return the file sent to the Kindle.

    Original mode preserves source page boundaries and image resolution.  It is
    the safest default for KOReader and avoids the page-overlap issue reported
    on Kindle 4.  Balanced mode remains available for users who prefer smaller
    grayscale files.
    """
    if KINDLE_PDF_MODE == "balanced":
        return optimize_pdf_for_kindle(pdf_path)
    return pdf_path


def kindle_download_name(series_dir: Path, chapter_num: float) -> str:
    title = sanitize_filename(series_from_metadata(series_dir)["title"])
    return f"{title} - Chapter {chapter_num:g}.pdf"


def kindle_file_result(series_dir: Path, chapter_num: float) -> Dict[str, Any]:
    """Return a Kindle file descriptor without requiring a Flask context.

    This helper is also called by background worker threads.  Building the URL
    manually keeps those workers independent of Flask's request/application
    context and preserves a relative URL for installations behind a proxy.
    """
    series_id = str(normalize_source(load_metadata(series_dir)).get("series_id") or "")
    encoded_series_id = quote(series_id, safe="")
    encoded_chapter_id = quote(chapter_key(chapter_num), safe="")
    return {
        "series_id": series_id,
        "chapter": chapter_num,
        "filename": kindle_download_name(series_dir, chapter_num),
        "download_url": (
            f"/api/kindle/v1/series/{encoded_series_id}/chapter/"
            f"{encoded_chapter_id}/file"
        ),
    }


def active_kindle_job_for_series(
    series_path: str,
    chapter_numbers: Sequence[float],
) -> Optional[Job]:
    """Reuse only an identical single-chapter Kindle request."""
    requested = sorted(set(float(number) for number in chapter_numbers))
    if len(requested) != 1:
        return None
    requested_chapter = requested[0]
    with JOBS_LOCK:
        candidates = [
            job
            for job in JOBS.values()
            if job.series_path == series_path
            and job.status in {"queued", "running"}
            and job.title.startswith("Kindle:")
            and job.chapter_num is not None
            and float(job.chapter_num) == requested_chapter
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda job: job.created_at)


def run_kindle_job(
    job_id: str,
    series_dir: Path,
    chapter_numbers: Sequence[float],
    chapter_range: str,
) -> None:
    series_path = relpath(series_dir)
    series = series_from_metadata(series_dir)
    requested = sorted(set(float(number) for number in chapter_numbers))
    try:
        update_job(job_id, status="running", message="Checking server library")
        mark_series_active(series_path, True)
        local = local_chapter_map(series_dir)
        missing = [number for number in requested if number not in local]

        if missing:
            def progress(payload: Dict[str, Any]) -> None:
                update_job(
                    job_id,
                    message=str(payload.get("message") or "Downloading chapter"),
                    progress_current=int(payload.get("image_index") or 0),
                    progress_total=int(payload.get("image_total") or 0),
                )

            manifest = download_series(
                series,
                chapter_range_from_numbers(missing),
                "pdf",
                DOWNLOAD_DIR,
                progress_cb=progress,
                target_dir=series_dir,
                chapter_numbers=missing,
            )
            cleanup_legacy_images(series_dir)
            update_series_download_state(series_dir, manifest)

        local = local_chapter_map(series_dir)
        files: List[Dict[str, Any]] = []
        for index, number in enumerate(requested, start=1):
            entry = local.get(number)
            if not entry:
                raise RuntimeError(f"Chapter {number:g} is not available after download")
            update_job(
                job_id,
                message=f"Preparing chapter {number:g} for Kindle ({index}/{len(requested)})",
                progress_current=index,
                progress_total=len(requested),
            )
            delivery = kindle_delivery_path(entry["path"])
            result = kindle_file_result(series_dir, number)
            result["size"] = delivery.stat().st_size
            result["pdf_mode"] = KINDLE_PDF_MODE
            files.append(result)

        update_job(
            job_id,
            status="done",
            message="Ready for Kindle",
            result={"files": files, "chapter_range": chapter_range},
            reader_url=f"/series/{quote(series_path, safe='/')}",
        )
        append_event(
            "kindle",
            f"Prepared {series.get('title') or series_dir.name} for Kindle",
            chapter_range,
            series_path,
        )
    except Exception as exc:
        update_job(job_id, status="error", message="Kindle preparation failed", error=str(exc))
        append_event(
            "error",
            f"Kindle preparation failed: {series.get('title') or series_dir.name}",
            str(exc),
            series_path,
        )
    finally:
        mark_series_active(series_path, False)


def queue_kindle_job(
    series_dir: Path,
    chapter_numbers: Sequence[float],
    chapter_range: str,
) -> Job:
    series_path = relpath(series_dir)
    existing = active_kindle_job_for_series(series_path, chapter_numbers)
    if existing:
        return existing
    series = series_from_metadata(series_dir)
    numbers = sorted(set(float(number) for number in chapter_numbers))
    one_chapter = numbers[0] if len(numbers) == 1 else None
    job = new_job(
        f"Kindle: {series.get('title') or series_dir.name}",
        str(series_dir),
        series_path,
        one_chapter,
    )
    threading.Thread(
        target=run_kindle_job,
        args=(job.id, series_dir, numbers, chapter_range),
        daemon=True,
    ).start()
    return job


def kindle_job_payload(job: Job) -> Dict[str, Any]:
    payload = asdict(job)
    payload["status_url"] = url_for("kindle_job", job_id=job.id)
    return payload


def kindle_summary_from_display(series: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": series["series_id"],
        "title": series["title"],
        "downloaded_count": series["downloaded_count"],
        "available_count": series["remote_chapters"],
        "latest": series["remote_latest"],
        "unread_count": series["unread_count"],
        "continue_chapter": series["continue_chapter"],
        "watching": bool(series["watch"]["enabled"]),
    }


def kindle_library_summary(series_dir: Path) -> Dict[str, Any]:
    """Return a small payload without making a fresh upstream request."""
    metadata = load_metadata(series_dir)
    source = normalize_source(metadata)
    series_id = str(source.get("series_id") or "")
    title = str(metadata.get("title") or source.get("title") or series_dir.name)
    local = local_chapter_map(series_dir)
    cached = read_json(chapter_cache_path(series_id), {}) if series_id else {}
    remote_numbers: List[float] = []
    for row in cached.get("chapters") or []:
        if not isinstance(row, list) or len(row) != 2:
            continue
        try:
            remote_numbers.append(float(row[0]))
        except (TypeError, ValueError):
            continue
    available_numbers = sorted(set(remote_numbers) | set(local))

    reading = metadata.get("reading") if isinstance(metadata.get("reading"), dict) else {}
    read_numbers: set[float] = set()
    for value in reading.get("read_chapters") or []:
        try:
            read_numbers.add(float(value))
        except (TypeError, ValueError):
            continue
    unread = [number for number in sorted(local) if number not in read_numbers]
    continue_chapter = unread[0] if unread else (max(local) if local else None)
    watch = metadata.get("watch") if isinstance(metadata.get("watch"), dict) else default_watch()

    return {
        "id": series_id,
        "title": title,
        "downloaded_count": len(local),
        "available_count": len(available_numbers),
        "latest": max(available_numbers) if available_numbers else None,
        "unread_count": len(unread),
        "continue_chapter": continue_chapter,
        "watching": bool(watch.get("enabled")),
    }


def kindle_series_payload(series_dir: Path, force_remote: bool = False) -> Dict[str, Any]:
    series = series_display(series_dir, force_remote=force_remote)
    payload = kindle_summary_from_display(series)
    payload["chapters"] = [
        {
            "number": row["number"],
            "downloaded_on_server": bool(row["downloaded"]),
            "read_on_server": bool(row["read"]),
            "size": row["size"],
        }
        for row in series["remote_chapter_rows"]
    ]
    return payload


@app.route("/api/kindle/v1/ping")
def kindle_ping():
    return jsonify(
        {
            "ok": True,
            "api_version": 1,
            "server": "MangaDL Ultimate",
            "bridge_version": "1.0.2",
            "kindle_profile": {
                "pdf_mode": KINDLE_PDF_MODE,
                "width": KINDLE_RENDER_WIDTH,
                "max_height": KINDLE_RENDER_MAX_HEIGHT,
                "jpeg_quality": KINDLE_JPEG_QUALITY,
                "cache_limit_mb": KINDLE_CACHE_MAX_BYTES // (1024 * 1024),
            },
        }
    )


@app.route("/api/kindle/v1/library")
def kindle_library():
    rows = [kindle_library_summary(path) for path in list_series_dirs()]
    rows.sort(key=lambda item: item["title"].lower())
    return jsonify({"api_version": 1, "series": rows, "generated_at": now_iso()})


@app.route("/api/kindle/v1/series/<series_id>")
def kindle_series(series_id: str):
    series_dir = find_series_by_id(series_id)
    if not series_dir:
        abort(404)
    force = request.args.get("refresh") in {"1", "true", "yes"}
    return jsonify(kindle_series_payload(series_dir, force_remote=force))


@app.post("/api/kindle/v1/series/<series_id>/chapter/<chapter_id>/prepare")
def kindle_prepare_chapter(series_id: str, chapter_id: str):
    series_dir = find_series_by_id(series_id)
    if not series_dir:
        abort(404)
    number = parse_chapter_key(chapter_id)
    local = local_chapter_map(series_dir).get(number)
    if local:
        delivery = kindle_delivery_path(local["path"])
        if delivery.exists() and delivery.stat().st_size > 0:
            payload = kindle_file_result(series_dir, number)
            payload.update({
                "status": "ready",
                "size": delivery.stat().st_size,
                "pdf_mode": KINDLE_PDF_MODE,
            })
            return jsonify(payload)
    job = queue_kindle_job(series_dir, [number], f"{number:g}")
    return jsonify({"status": job.status, "job": kindle_job_payload(job)}), 202


@app.post("/api/kindle/v1/series/<series_id>/bulk")
def kindle_prepare_bulk(series_id: str):
    series_dir = find_series_by_id(series_id)
    if not series_dir:
        abort(404)
    payload = request.get_json(silent=True) or {}
    chapter_range = str(payload.get("chapter_range") or "").strip()
    if not chapter_range:
        return jsonify({"error": "chapter_range is required"}), 400
    source = normalize_source(load_metadata(series_dir))
    remote = remote_chapters(str(source.get("series_id") or ""), force=True)
    selected = filter_chapters(remote, chapter_range)
    numbers = [number for number, _ in selected]
    if not numbers:
        return jsonify({"error": "No chapters matched the requested range"}), 400
    job = queue_kindle_job(series_dir, numbers, chapter_range)
    return jsonify({"status": job.status, "job": kindle_job_payload(job), "chapters": numbers}), 202


@app.route("/api/kindle/v1/jobs/<job_id>")
def kindle_job(job_id: str):
    return jsonify(kindle_job_payload(job_or_404(job_id)))


@app.route("/api/kindle/v1/series/<series_id>/chapter/<chapter_id>/file")
def kindle_chapter_file(series_id: str, chapter_id: str):
    series_dir = find_series_by_id(series_id)
    if not series_dir:
        abort(404)
    number = parse_chapter_key(chapter_id)
    local = local_chapter_map(series_dir).get(number)
    if not local:
        return jsonify({"error": "Chapter is not downloaded on the server"}), 404
    delivery = kindle_delivery_path(local["path"])
    return send_file(
        delivery,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=kindle_download_name(series_dir, number),
        conditional=True,
        max_age=86400,
    )

@app.context_processor
def inject_globals() -> Dict[str, Any]:
    return {
        "format_bytes": format_bytes,
        "render_desc": render_desc,
        "chapter_key": chapter_key,
    }


load_persisted_jobs()
start_watcher_once()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")), debug=False)
