from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import random
import secrets
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
import requests
from PIL import Image, ImageOps
from markupsafe import Markup
from flask import Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for

from anilist import best_cover, best_title, cover_images, creator_credits, explore_popular, fetch as anilist_fetch, recommendations as anilist_recommendations, safe_text, search as anilist_search, staff as anilist_staff
from manga import CHAPTER_PARSER_VERSION, download_series, filter_chapters, get_all_chapters, sanitize_filename, search_manga
from accounts import AccountStore, Profile
from offline_client import build_bundle as build_offline_bundle
from metadata_utils import normalize_metadata_block

VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"
try:
    APP_VERSION = VERSION_FILE.read_text(encoding="utf-8").strip() or "2.3.1"
except OSError:
    APP_VERSION = "2.3.1"

BASE_DIR = Path(os.environ.get("STACK_DIR", "/stack")).resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))).resolve()
LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", str(DATA_DIR / "library"))).resolve()
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", str(DATA_DIR / "downloads"))).resolve()
STATE_DIR = Path(os.environ.get("STATE_DIR", str(DATA_DIR / "state"))).resolve()
CACHE_DIR = STATE_DIR / "pdf-cache"
CHAPTER_CACHE_DIR = STATE_DIR / "chapter-cache"
KINDLE_CACHE_DIR = STATE_DIR / "kindle-cache"
KINDLE_COVER_DIR = KINDLE_CACHE_DIR / "covers"
KINDLE_COVER_WIDTH = max(300, min(1600, int(os.environ.get("KINDLE_COVER_WIDTH", "600"))))
KINDLE_COVER_HEIGHT = max(400, min(2400, int(os.environ.get("KINDLE_COVER_HEIGHT", "800"))))
KINDLE_COVER_QUALITY = max(70, min(95, int(os.environ.get("KINDLE_COVER_QUALITY", "90"))))
KINDLE_COVER_FIT = os.environ.get("KINDLE_COVER_FIT", "contain").strip().lower()
if KINDLE_COVER_FIT not in {"contain", "cover"}:
    KINDLE_COVER_FIT = "contain"
KINDLE_COVER_PROCESSOR_VERSION = 3
JOBS_FILE = STATE_DIR / "jobs.json"
EVENTS_FILE = STATE_DIR / "events.json"
ACCOUNTS_DB = STATE_DIR / "accounts-v2.sqlite3"
EXPLORE_CACHE_FILE = STATE_DIR / "explore-cache.json"
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
KINDLE_PROFILE_USERNAME = os.environ.get("KINDLE_PROFILE_USERNAME", "").strip()

for directory in (LIBRARY_DIR, DOWNLOAD_DIR, STATE_DIR, CACHE_DIR, CHAPTER_CACHE_DIR, KINDLE_CACHE_DIR, KINDLE_COVER_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-mangadl-secret")
app.config.update(
    SESSION_COOKIE_NAME="mangabridge_profile",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"},
)
ACCOUNTS = AccountStore(ACCOUNTS_DB)


def current_profile() -> Optional[Profile]:
    profile_id = session.get("profile_id")
    if not profile_id:
        return None
    try:
        return ACCOUNTS.get_profile(int(profile_id))
    except (TypeError, ValueError):
        return None


def require_profile() -> Profile:
    profile = current_profile()
    if not profile:
        abort(401)
    return profile


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return str(token)


def csrf_field() -> Markup:
    return Markup(f'<input type="hidden" name="_csrf" value="{csrf_token()}">')


def kindle_profile() -> Optional[Profile]:
    requested_username = request.headers.get("X-MangaBridge-Profile", "").strip()
    if requested_username:
        return ACCOUNTS.get_profile_by_username(requested_username)
    if KINDLE_PROFILE_USERNAME:
        selected = ACCOUNTS.get_profile_by_username(KINDLE_PROFILE_USERNAME)
        if selected:
            return selected
    return ACCOUNTS.first_profile()


@app.before_request
def optional_basic_auth() -> Optional[Response]:
    """Keep the Kindle API compatible while the web UI uses profile sessions."""
    is_kindle_api = request.path.startswith("/api/kindle/")
    if is_kindle_api:
        if not KINDLE_API_TOKEN and not AUTH_USERNAME:
            return None
        supplied_token = request.headers.get("X-MangaDL-Token", "")
        if KINDLE_API_TOKEN and supplied_token and hmac.compare_digest(supplied_token, KINDLE_API_TOKEN):
            return None
        if AUTH_USERNAME:
            auth = request.authorization
            username_ok = bool(auth and hmac.compare_digest(auth.username or "", AUTH_USERNAME))
            password_ok = bool(auth and hmac.compare_digest(auth.password or "", AUTH_PASSWORD))
            if username_ok and password_ok:
                return None
        return jsonify({"error": "A valid Kindle API token is required"}), 401

    if request.method == "POST":
        expected = session.get("_csrf_token")
        supplied = request.form.get("_csrf", "") or request.headers.get("X-CSRF-Token", "")
        if not expected or not supplied or not hmac.compare_digest(str(expected), str(supplied)):
            return Response("Invalid or missing CSRF token", 400)

    if request.endpoint == "static" or request.path in {"/health", "/setup", "/login"}:
        return None
    if ACCOUNTS.count_profiles() == 0:
        return redirect(url_for("setup"))
    if not current_profile():
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
    g.profile = current_profile()
    return None


@app.before_request
def validate_kindle_profile_header() -> Optional[Response]:
    if not request.path.startswith("/api/kindle/"):
        return None
    requested_username = request.headers.get("X-MangaBridge-Profile", "").strip()
    if requested_username and not ACCOUNTS.get_profile_by_username(requested_username):
        return jsonify({"error": f"Unknown MangaBridge profile: {requested_username}"}), 404
    return None


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
SERIES_LOCKS: Dict[str, threading.Lock] = {}
SERIES_LOCKS_GUARD = threading.Lock()

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


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def render_desc(metadata: Dict[str, Any]) -> str:
    value = metadata.get("description") if isinstance(metadata, dict) else ""
    return safe_text(value if isinstance(value, str) else "")


def load_metadata(series_dir: Path) -> Dict[str, Any]:
    value = read_json(series_dir / "metadata.json", {})
    return value if isinstance(value, dict) else {}


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
    cached_value = read_json(cache_path, {})
    cached = cached_value if isinstance(cached_value, dict) else {}
    fetched_at = parse_iso(cached.get("fetched_at"))
    cached_rows = cached.get("chapters") if isinstance(cached.get("chapters"), list) else []

    def valid_cached_rows() -> List[Tuple[float, str]]:
        rows: List[Tuple[float, str]] = []
        for row in cached_rows:
            if not isinstance(row, list) or len(row) != 2:
                continue
            try:
                number = float(row[0])
            except (TypeError, ValueError, OverflowError):
                continue
            url = row[1]
            if not isinstance(url, str) or not url:
                continue
            rows.append((number, url))
        return rows

    fallback_rows = valid_cached_rows()
    cache_is_current = cached.get("parser_version") == CHAPTER_PARSER_VERSION
    if (
        not force
        and cache_is_current
        and fallback_rows
        and fetched_at
        and datetime.now(timezone.utc) - fetched_at < timedelta(seconds=max_age_seconds)
    ):
        return fallback_rows
    try:
        rows = get_all_chapters(series_id)
        atomic_write_json(
            cache_path,
            {
                "fetched_at": now_iso(),
                "parser_version": CHAPTER_PARSER_VERSION,
                "chapters": [[number, url] for number, url in rows],
            },
        )
        return rows
    except Exception as exc:
        app.logger.warning("Chapter refresh failed for %s: %s", series_id, exc)
        return fallback_rows


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


def series_display(series_dir: Path, force_remote: bool = False, user_id: Optional[int] = None) -> Dict[str, Any]:
    metadata = load_metadata(series_dir)
    source = normalize_source(metadata)
    series_id = str(source.get("series_id") or "")
    title = str(metadata.get("title") or source.get("title") or series_dir.name)
    remote = remote_chapters(series_id, force=force_remote) if series_id else []
    local = local_chapter_map(series_dir)
    remote_map = {number: url for number, url in remote}
    all_numbers = sorted(set(remote_map) | set(local), reverse=True)
    watch = metadata.get("watch") if isinstance(metadata.get("watch"), dict) else default_watch()
    metadata_block = normalize_metadata_block(metadata.get("metadata"))
    if user_id is not None:
        reading = ACCOUNTS.progress_for_series(user_id, relpath(series_dir))
        library_settings = {"pinned": ACCOUNTS.is_pinned(user_id, relpath(series_dir))}
    else:
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

    try:
        reading_last_page = max(0, int(reading.get("last_page") or 0))
    except (TypeError, ValueError):
        reading_last_page = 0
    try:
        reading_last_total_pages = max(0, int(reading.get("last_total_pages") or 0))
    except (TypeError, ValueError):
        reading_last_total_pages = 0

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
        "cover": (metadata.get("coverImage") if isinstance(metadata.get("coverImage"), str) else "") or metadata_block.get("coverImage"),
        "downloaded_count": len(local),
        "downloaded_numbers": downloaded_numbers,
        "downloaded_size": sum(item["size"] for item in local.values()),
        "read_count": len(read_numbers & set(downloaded_numbers)),
        "unread_count": len(unread_downloaded),
        "continue_chapter": continue_chapter,
        "last_chapter": last_chapter,
        "last_page": reading_last_page,
        "last_total_pages": reading_last_total_pages,
        "last_read_at": reading.get("last_read_at"),
        "last_read_epoch": safe_int(reading.get("last_read_epoch"), 0),
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
                    "meta_cover": best_cover(match),
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


def series_download_lock(series_path: str) -> threading.Lock:
    with SERIES_LOCKS_GUARD:
        return SERIES_LOCKS.setdefault(series_path, threading.Lock())


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
    completion_url: Optional[str] = None,
) -> None:
    series_path = relpath(series_dir)
    lock = series_download_lock(series_path)
    lock.acquire()
    try:
        update_job(job_id, status="running", message="Checking shared library")
        mark_series_active(series_path, True)
        local_numbers = set(local_chapter_map(series_dir))
        if chapter_numbers is None:
            source_id = str(series.get("series_id") or "")
            selected = filter_chapters(remote_chapters(source_id, force=True), chapter_range)
            requested_numbers = [number for number, _ in selected]
        else:
            requested_numbers = sorted(set(float(number) for number in chapter_numbers))
        missing_numbers = [number for number in requested_numbers if number not in local_numbers]

        if not missing_numbers:
            default_url = f"/series/{quote(series_path, safe='/')}"
            update_job(
                job_id,
                status="done",
                message="Already present in the shared library",
                result={"chapters": []},
                reader_url=completion_url or default_url,
            )
            return

        def progress(payload: Dict[str, Any]) -> None:
            update_job(
                job_id,
                message=str(payload.get("message") or "Downloading"),
                progress_current=int(payload.get("image_index") or 0),
                progress_total=int(payload.get("image_total") or 0),
            )

        manifest = download_series(
            series,
            chapter_range_from_numbers(missing_numbers),
            "pdf",
            DOWNLOAD_DIR,
            progress_cb=progress,
            target_dir=series_dir,
            chapter_numbers=missing_numbers,
        )
        cleanup_legacy_images(series_dir)
        update_series_download_state(series_dir, manifest)
        with JOBS_LOCK:
            chapter_num = JOBS[job_id].chapter_num
        encoded_series = quote(series_path, safe="/")
        reader_url = completion_url or (
            f"/series/{encoded_series}/chapter/{chapter_key(chapter_num)}/read"
            if chapter_num is not None
            else f"/series/{encoded_series}"
        )
        update_job(job_id, status="done", message="Ready", result=manifest, reader_url=reader_url)
        append_event("download", f"Downloaded {series.get('title') or series_dir.name}", chapter_range_from_numbers(missing_numbers), series_path)
    except Exception as exc:
        update_job(job_id, status="error", message="Download failed", error=str(exc))
        append_event("error", f"Download failed: {series.get('title') or series_dir.name}", str(exc), series_path)
    finally:
        mark_series_active(series_path, False)
        lock.release()


def queue_download(
    series_dir: Path,
    series: Dict[str, str],
    chapter_range: str,
    chapter_numbers: Optional[Sequence[float]] = None,
    title_prefix: str = "Download",
    chapter_num: Optional[float] = None,
    completion_url: Optional[str] = None,
) -> Job:
    job = new_job(
        f"{title_prefix}: {series.get('title') or series_dir.name}",
        str(series_dir),
        relpath(series_dir),
        chapter_num,
    )
    thread = threading.Thread(
        target=run_download_job,
        args=(job.id, series_dir, series, chapter_range, chapter_numbers, completion_url),
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
        store_anilist_metadata(series_dir, data)


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


def client_chapter_numbers(series_dir: Path, chapter_range: str, force_remote: bool = False) -> List[float]:
    local_numbers = set(local_chapter_map(series_dir))
    metadata = load_metadata(series_dir)
    source = normalize_source(metadata)
    remote_rows: List[Tuple[float, str]] = []
    series_id = str(source.get("series_id") or "")
    if series_id:
        remote_rows = remote_chapters(series_id, force=force_remote)
    available = sorted(set(number for number, _ in remote_rows) | local_numbers)
    selected = filter_chapters([(number, "") for number in available], chapter_range)
    return [number for number, _ in selected]


def offline_bundle_for_series(series_dir: Path, numbers: Sequence[float]):
    local = local_chapter_map(series_dir)
    selected = [number for number in numbers if number in local]
    if not selected:
        abort(404)
    source = normalize_source(load_metadata(series_dir))
    title = series_from_metadata(series_dir)["title"]
    chapters = [
        {
            "number": number,
            "path": local[number]["path"],
            "size": local[number]["size"],
        }
        for number in selected
    ]
    return build_offline_bundle(
        title=title,
        series_id=str(source.get("series_id") or relpath(series_dir)),
        created_at=now_iso(),
        chapters=chapters,
    )


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


# ---------------------------------------------------------------------------
# Profiles, shared libraries, and web UI
# ---------------------------------------------------------------------------

def profile_series_paths(profile_id: int) -> List[str]:
    return [path for path in ACCOUNTS.list_user_series(profile_id) if (LIBRARY_DIR / path).is_dir()]


def migrate_legacy_progress(profile_id: int) -> None:
    for series_path in profile_series_paths(profile_id):
        series_dir = resolve_under(LIBRARY_DIR, series_path)
        metadata = load_metadata(series_dir)
        reading = metadata.get("reading") if isinstance(metadata.get("reading"), dict) else {}
        for raw in reading.get("read_chapters") or []:
            try:
                ACCOUNTS.update_progress(profile_id, series_path, float(raw), "complete")
            except (TypeError, ValueError):
                continue
        try:
            last = float(reading.get("last_chapter")) if reading.get("last_chapter") is not None else None
        except (TypeError, ValueError):
            last = None
        if last is not None:
            ACCOUNTS.update_progress(profile_id, series_path, last, "open")


def resolve_visible_series(series_path: str, *, own_required: bool = False) -> Path:
    profile = require_profile()
    series_dir = resolve_under(LIBRARY_DIR, series_path)
    if not series_dir.is_dir() or (series_dir / HIDDEN_MARKER).exists():
        abort(404)
    allowed = ACCOUNTS.has_series(profile.id, series_path) if own_required else ACCOUNTS.accessible_series(profile.id, series_path)
    if not allowed:
        abort(404)
    return series_dir


def media_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    tags = [
        tag.get("name")
        for tag in (data.get("tags") or [])
        if isinstance(tag, dict) and not tag.get("isMediaSpoiler") and (tag.get("rank") or 0) >= 50
    ]
    return {
        "id": data.get("id"),
        "title": best_title(data),
        "description": safe_text(data.get("description") or ""),
        "cover": best_cover(data),
        "banner": data.get("bannerImage") or "",
        "status": data.get("status"),
        "format": data.get("format"),
        "chapters": data.get("chapters"),
        "volumes": data.get("volumes"),
        "score": data.get("averageScore"),
        "popularity": data.get("popularity"),
        "trending": data.get("trending"),
        "genres": data.get("genres") or [],
        "tags": tags[:6],
        "site_url": data.get("siteUrl"),
    }


def store_anilist_metadata(series_dir: Path, data: Dict[str, Any]) -> None:
    metadata = load_metadata(series_dir)
    source = normalize_source(metadata)
    source["anilist_id"] = data.get("id")
    metadata["source"] = source
    metadata["metadata"] = {
        "source": "AniList",
        "source_id": data.get("id"),
        "title": best_title(data),
        "description": data.get("description") or "",
        "status": data.get("status"),
        "format": data.get("format"),
        "chapters": data.get("chapters"),
        "volumes": data.get("volumes"),
        "averageScore": data.get("averageScore"),
        "popularity": data.get("popularity"),
        "trending": data.get("trending"),
        "siteUrl": data.get("siteUrl"),
        "bannerImage": data.get("bannerImage"),
        "genres": data.get("genres") or [],
        "tags": [tag.get("name") for tag in (data.get("tags") or []) if isinstance(tag, dict) and not tag.get("isMediaSpoiler")][:8],
        "creators": creator_credits(data),
        "coverImages": cover_images(data),
        "coverImage": best_cover(data),
        "synced_at": now_iso(),
    }
    metadata["coverImages"] = metadata["metadata"]["coverImages"]
    metadata["coverImage"] = metadata["metadata"]["coverImage"]
    save_metadata(series_dir, metadata)


def page_background(profile_id: Optional[int]) -> str:
    if profile_id is None:
        return ""
    candidates: List[str] = []
    for series_path in profile_series_paths(profile_id):
        try:
            metadata = load_metadata(resolve_under(LIBRARY_DIR, series_path))
            block = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
            image = block.get("bannerImage") or metadata.get("bannerImage") or block.get("coverImage") or metadata.get("coverImage")
            if image:
                candidates.append(str(image))
        except Exception:
            continue
    return random.choice(candidates) if candidates else ""


def search_results_for_profile(profile: Profile, query: str) -> List[Dict[str, Any]]:
    results = enrich_search_results(search_manga(query)[:10]) if query else []
    for item in results:
        path = item.get("existing_path")
        item["exists_globally"] = bool(path)
        item["already_added"] = bool(path and ACCOUNTS.has_series(profile.id, str(path)))
    return results


def explore_sections(profile: Profile, force: bool = False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    cache = read_json(EXPLORE_CACHE_FILE, {})
    fetched = parse_iso(cache.get("fetched_at"))
    fresh = fetched and datetime.now(timezone.utc) - fetched < timedelta(hours=6)
    popular_raw: List[Dict[str, Any]] = cache.get("popular") if isinstance(cache.get("popular"), list) else []
    if force or not fresh or not popular_raw:
        try:
            popular_raw = explore_popular(18)
        except Exception as exc:
            app.logger.warning("AniList popular query failed: %s", exc)
        cache["popular"] = popular_raw
        cache["fetched_at"] = now_iso()

    source_records: List[Tuple[str, int]] = []
    for series_path in profile_series_paths(profile.id):
        metadata = load_metadata(resolve_under(LIBRARY_DIR, series_path))
        source = normalize_source(metadata)
        try:
            media_id = int(source.get("anilist_id"))
        except (TypeError, ValueError):
            continue
        progress = ACCOUNTS.progress_for_series(profile.id, series_path)
        source_records.append((str(progress.get("last_read_at") or ""), media_id))
    source_records.sort(reverse=True)
    source_ids = [media_id for _, media_id in source_records]
    selected_source_ids = source_ids[:4]
    signature = ",".join(str(value) for value in selected_source_ids)
    rec_cache = cache.get("recommendations") if isinstance(cache.get("recommendations"), dict) else {}
    recommendation_raw = rec_cache.get(signature) if isinstance(rec_cache.get(signature), list) else []
    if source_ids and (force or not fresh or not recommendation_raw):
        combined: Dict[int, Dict[str, Any]] = {}
        for media_id in selected_source_ids:
            try:
                for item in anilist_recommendations(media_id):
                    if item.get("id"):
                        combined[int(item["id"])] = item
            except Exception as exc:
                app.logger.warning("AniList recommendation query failed for %s: %s", media_id, exc)
        recommendation_raw = sorted(combined.values(), key=lambda item: item.get("recommendationRating") or 0, reverse=True)[:18]
        rec_cache[signature] = recommendation_raw
        cache["recommendations"] = rec_cache
        atomic_write_json(EXPLORE_CACHE_FILE, cache)

    owned_anilist = set(source_ids)
    popular = [media_payload(item) for item in popular_raw if item.get("id") not in owned_anilist]
    related = [media_payload(item) for item in recommendation_raw if item.get("id") not in owned_anilist]
    return popular[:12], related[:12]


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if ACCOUNTS.count_profiles() > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        try:
            profile = ACCOUNTS.create_profile(
                request.form.get("username", ""),
                request.form.get("display_name", ""),
                request.form.get("password", ""),
                admin=True,
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("setup.html")
        ACCOUNTS.assign_existing_library(profile.id, [relpath(path) for path in list_series_dirs()])
        migrate_legacy_progress(profile.id)
        session.clear()
        session["profile_id"] = profile.id
        append_event("profile", f"Created profile {profile.display_name}", "Existing library assigned to the first profile")
        return redirect(url_for("library_home"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if ACCOUNTS.count_profiles() == 0:
        return redirect(url_for("setup"))
    profiles = ACCOUNTS.list_profiles()
    selected_id = request.form.get("profile_id") or request.args.get("profile")
    if request.method == "POST":
        try:
            profile_id = int(selected_id or 0)
        except ValueError:
            profile_id = 0
        profile = ACCOUNTS.get_profile(profile_id)
        if not profile or not ACCOUNTS.verify_password(profile_id, request.form.get("password", "")):
            flash("That profile or password was not recognised.", "error")
        else:
            session.clear()
            session["profile_id"] = profile.id
            destination = request.form.get("next", "")
            if not destination.startswith("/") or destination.startswith("//"):
                destination = url_for("library_home")
            return redirect(destination)
    return render_template("login.html", profiles=profiles, selected_id=selected_id, next=request.args.get("next", ""))


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/profiles")
def profiles_view():
    profile = require_profile()
    return render_template(
        "profiles.html",
        profiles=ACCOUNTS.list_profiles(),
        current=profile,
        spaces=ACCOUNTS.list_spaces_for_user(profile.id),
    )


@app.post("/profiles/create")
def profile_create():
    require_profile()
    try:
        created = ACCOUNTS.create_profile(
            request.form.get("username", ""),
            request.form.get("display_name", ""),
            request.form.get("password", ""),
        )
    except (ValueError, Exception) as exc:
        message = "That username is already in use." if "UNIQUE" in str(exc).upper() else str(exc)
        flash(message, "error")
    else:
        append_event("profile", f"Created profile {created.display_name}")
        flash(f"Created profile {created.display_name}.", "success")
    return redirect(url_for("profiles_view"))


@app.post("/profiles/update")
def profile_update():
    profile = require_profile()
    mode = request.form.get("password_mode", "keep")
    password: Optional[str]
    if mode == "keep":
        password = None
    elif mode == "remove":
        password = ""
    else:
        password = request.form.get("password", "")
        if not password:
            flash("Enter a new password or choose Keep current password.", "error")
            return redirect(url_for("profiles_view"))
    try:
        ACCOUNTS.update_profile(profile.id, request.form.get("display_name", ""), password)
        flash("Profile updated.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("profiles_view"))


@app.post("/shared/create")
def shared_create():
    profile = require_profile()
    try:
        space_id = ACCOUNTS.create_space(profile.id, request.form.get("name", ""))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("profiles_view"))
    flash("Shared library created. Invite profiles from its settings.", "success")
    return redirect(url_for("shared_view", space_id=space_id))


@app.route("/shared/<int:space_id>")
def shared_view(space_id: int):
    profile = require_profile()
    if not ACCOUNTS.is_space_member(space_id, profile.id, accepted=True):
        abort(404)
    space = ACCOUNTS.get_space(space_id)
    if not space:
        abort(404)
    q = request.args.get("q", "").strip().lower()
    cards: List[Dict[str, Any]] = []
    for row in ACCOUNTS.shared_series(space_id, profile.id):
        try:
            series_dir = resolve_under(LIBRARY_DIR, row["series_path"])
            card = series_display(series_dir, user_id=profile.id)
        except Exception:
            continue
        card["owners"] = row["owner_names"]
        card["owned_by_me"] = profile.id in row["owner_ids"]
        if not q or q in card["title"].lower():
            cards.append(card)
    cards.sort(key=lambda item: (not item["pinned"], item["title"].lower()))
    hidden_cards = []
    for hidden_path in ACCOUNTS.hidden_shared_series(space_id, profile.id):
        try:
            hidden_cards.append({"path": hidden_path, "title": series_from_metadata(resolve_under(LIBRARY_DIR, hidden_path))["title"]})
        except Exception:
            continue
    space_members = ACCOUNTS.space_members(space_id)
    active_members = [member for member in space_members if member.get("status") in {"accepted", "pending"}]
    active_member_ids = {member["id"] for member in active_members}
    return render_template(
        "library.html",
        library=cards,
        q=request.args.get("q", ""),
        sort="title",
        results=[],
        stats={
            "series": len(cards),
            "pdfs": sum(item["downloaded_count"] for item in cards),
            "bytes": sum(item["downloaded_size"] for item in cards),
            "unread": sum(item["unread_count"] for item in cards),
            "watched": sum(1 for item in cards if item["watch"]["enabled"]),
        },
        events=[],
        mode="shared",
        space=space,
        members=active_members,
        member_ids=list(active_member_ids),
        invitable_profiles=[candidate for candidate in ACCOUNTS.list_profiles() if candidate.id not in active_member_ids],
        hidden_cards=hidden_cards,
    )


@app.post("/shared/<int:space_id>/invite")
def shared_invite(space_id: int):
    profile = require_profile()
    space = ACCOUNTS.get_space(space_id)
    if not space or int(space["owner_id"]) != profile.id:
        abort(403)
    try:
        target_id = int(request.form.get("user_id", "0"))
    except ValueError:
        abort(400)
    ACCOUNTS.invite_to_space(space_id, target_id, profile.id)
    flash("Invitation sent. The profile must accept it before libraries are shared.", "success")
    return redirect(url_for("shared_view", space_id=space_id))


@app.post("/shared/<int:space_id>/respond")
def shared_respond(space_id: int):
    profile = require_profile()
    ACCOUNTS.respond_invite(space_id, profile.id, request.form.get("action") == "accept")
    return redirect(url_for("profiles_view"))


@app.post("/shared/<int:space_id>/hide/<path:series_path>")
def shared_hide(space_id: int, series_path: str):
    profile = require_profile()
    if not ACCOUNTS.is_space_member(space_id, profile.id, accepted=True):
        abort(403)
    ACCOUNTS.hide_shared_series(space_id, profile.id, series_path, request.form.get("hidden", "1") == "1")
    flash("Series hidden from this shared view. Your own library is unchanged.", "success")
    return redirect(url_for("shared_view", space_id=space_id))


@app.post("/shared/<int:space_id>/leave")
def shared_leave(space_id: int):
    profile = require_profile()
    ACCOUNTS.leave_space(space_id, profile.id)
    flash("Shared library membership updated.", "success")
    return redirect(url_for("profiles_view"))


@app.route("/")
@app.route("/library")
def library_home():
    profile = require_profile()
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "activity").strip().lower()
    library: List[Dict[str, Any]] = []
    for series_path in profile_series_paths(profile.id):
        try:
            card = series_display(resolve_under(LIBRARY_DIR, series_path), user_id=profile.id)
            card["owners"] = []
            card["owned_by_me"] = True
            library.append(card)
        except Exception:
            continue
    visible_library = [item for item in library if not q or q.lower() in item["title"].lower()]
    if sort == "title":
        visible_library.sort(key=lambda item: (not item["pinned"], item["title"].lower()))
    elif sort == "unread":
        visible_library.sort(key=lambda item: (not item["pinned"], -item["unread_count"], item["title"].lower()))
    elif sort == "latest":
        visible_library.sort(key=lambda item: (not item["pinned"], -(item["remote_latest"] or -1), item["title"].lower()))
    else:
        visible_library.sort(key=lambda item: (item["pinned"], item["last_read_at"] or ""), reverse=True)
    results = search_results_for_profile(profile, q)
    return render_template(
        "library.html",
        library=visible_library,
        q=q,
        sort=sort,
        results=results,
        stats={
            "series": len(library),
            "pdfs": sum(item["downloaded_count"] for item in library),
            "bytes": sum(item["downloaded_size"] for item in library),
            "unread": sum(item["unread_count"] for item in library),
            "watched": sum(1 for item in library if item["watch"]["enabled"]),
        },
        events=list_events(6),
        mode="personal",
        space=None,
        members=[],
    )


@app.route("/search")
def search_page():
    return redirect(url_for("library_home", q=request.args.get("q", "").strip()))


@app.route("/explore")
def explore_view():
    profile = require_profile()
    popular, related = explore_sections(profile, force=request.args.get("refresh") == "1")
    return render_template("explore.html", popular=popular, related=related)


@app.post("/explore/add/<int:anilist_id>")
def explore_add(anilist_id: int):
    profile = require_profile()
    try:
        data = anilist_fetch(anilist_id)
    except Exception as exc:
        flash(f"AniList lookup failed: {exc}", "error")
        return redirect(url_for("explore_view"))
    title = best_title(data)
    source_matches = search_manga(title)[:8]
    if not source_matches:
        alternate = (data.get("title") or {}).get("romaji")
        if alternate and alternate != title:
            source_matches = search_manga(alternate)[:8]
    if not source_matches:
        flash(f"Could not find a downloadable source for {title}.", "error")
        return redirect(url_for("explore_view"))
    match = max(source_matches, key=lambda item: SequenceMatcher(None, title.lower(), item.get("title", "").lower()).ratio())
    series_dir = ensure_series_dir(match["title"], match["series_id"])
    metadata = load_metadata(series_dir)
    source = normalize_source(metadata)
    source.update({
        "title": match["title"], "series_id": match["series_id"], "slug": match.get("slug", ""),
        "url": match.get("url", ""), "provider": "WeebCentral", "added_at": source.get("added_at") or now_iso(),
        "anilist_id": anilist_id,
    })
    metadata["source"] = source
    metadata["title"] = match["title"]
    save_metadata(series_dir, metadata)
    store_anilist_metadata(series_dir, data)
    ACCOUNTS.add_series(profile.id, relpath(series_dir))
    append_event("library", f"{profile.display_name} added {title}", "Added from Explore", relpath(series_dir))
    return redirect(url_for("series_view", series_path=relpath(series_dir)))


@app.post("/library/add")
def library_add():
    profile = require_profile()
    title = request.form.get("title", "").strip()
    series_id = request.form.get("series_id", "").strip()
    if not title or not series_id:
        abort(400)
    series_dir = ensure_series_dir(title, series_id)
    metadata = load_metadata(series_dir)
    source = normalize_source(metadata)
    source.update({
        "title": title, "series_id": series_id, "slug": request.form.get("slug", "").strip(),
        "url": request.form.get("url", "").strip(), "provider": "WeebCentral", "added_at": source.get("added_at") or now_iso(),
    })
    metadata["source"] = source
    metadata["title"] = title
    metadata["watch"] = metadata.get("watch") if isinstance(metadata.get("watch"), dict) else default_watch()
    save_metadata(series_dir, metadata)
    anilist_id = request.form.get("anilist_id", "").strip()
    if anilist_id:
        try:
            store_anilist_metadata(series_dir, anilist_fetch(int(anilist_id)))
        except Exception:
            pass
    ACCOUNTS.add_series(profile.id, relpath(series_dir))
    append_event("library", f"{profile.display_name} added {title}", "Files are shared globally", relpath(series_dir))
    flash(f"Added {title} to {profile.display_name}'s library.", "success")
    return redirect(url_for("series_view", series_path=relpath(series_dir)))


@app.post("/series/<path:series_path>/adopt")
def series_adopt(series_path: str):
    profile = require_profile()
    series_dir = resolve_visible_series(series_path)
    ACCOUNTS.add_series(profile.id, series_path)
    flash(f"Added {series_from_metadata(series_dir)['title']} to your personal library.", "success")
    return redirect(url_for("series_view", series_path=series_path))


@app.route("/series/<path:series_path>")
def series_view(series_path: str):
    profile = require_profile()
    series_dir = resolve_visible_series(series_path)
    series = series_display(series_dir, user_id=profile.id)
    metadata_block = series.get("metadata_block") if isinstance(series.get("metadata_block"), dict) else {}
    source = series.get("source") if isinstance(series.get("source"), dict) else {}
    raw_metadata = series.get("metadata") if isinstance(series.get("metadata"), dict) else {}
    raw_metadata_block = raw_metadata.get("metadata") if isinstance(raw_metadata.get("metadata"), dict) else {}
    anilist_id = source.get("anilist_id") or metadata_block.get("source_id")
    creator_metadata_missing = "creators" not in raw_metadata_block or not isinstance(raw_metadata_block.get("creators"), list)
    if anilist_id and creator_metadata_missing:
        try:
            store_anilist_metadata(series_dir, anilist_fetch(int(anilist_id)))
            series = series_display(series_dir, user_id=profile.id)
        except Exception as exc:
            app.logger.info("Could not backfill creator metadata for %s: %s", series_path, exc)
    owners = ACCOUNTS.series_owners(series_path)
    impact = ACCOUNTS.progress_users_for_series(series_path)
    shared_space = None
    try:
        space_id = int(request.args.get("shared", "0"))
    except ValueError:
        space_id = 0
    if space_id and ACCOUNTS.is_space_member(space_id, profile.id, accepted=True):
        shared_space = ACCOUNTS.get_space(space_id)
    return render_template(
        "series.html",
        series=series,
        owners=owners,
        impact=impact,
        owned_by_me=ACCOUNTS.has_series(profile.id, series_path),
        shared_space=shared_space,
    )


@app.post("/series/<path:series_path>/refresh")
def series_refresh(series_path: str):
    series_dir = resolve_visible_series(series_path)
    refresh_series_metadata(series_dir)
    source = normalize_source(load_metadata(series_dir))
    if source.get("series_id"):
        remote_chapters(str(source["series_id"]), force=True)
    flash("Series information and chapter list refreshed.", "success")
    return redirect(url_for("series_view", series_path=series_path))


@app.post("/series/<path:series_path>/remove")
def series_remove(series_path: str):
    profile = require_profile()
    series_dir = resolve_visible_series(series_path, own_required=True)
    if series_is_active(series_path):
        flash("This series is currently downloading.", "error")
        return redirect(url_for("series_view", series_path=series_path))
    action = request.form.get("action", "remove")
    title = series_from_metadata(series_dir)["title"]
    other_owners = [owner for owner in ACCOUNTS.series_owners(series_path) if owner.id != profile.id]
    if action == "delete_files":
        if request.form.get("confirm_text") != "DELETE":
            flash("Type DELETE to confirm global file deletion.", "error")
            return redirect(url_for("series_view", series_path=series_path))
        shutil.rmtree(series_dir)
        ACCOUNTS.remove_series_everywhere(series_path)
        append_event("library", f"{profile.display_name} deleted {title}", f"Removed files for {len(other_owners) + 1} profiles")
        flash(f"Deleted {title} and its files for every profile.", "success")
    else:
        ACCOUNTS.remove_series(profile.id, series_path)
        append_event("library", f"{profile.display_name} removed {title}", "Shared files retained")
        flash(f"Removed {title} from your library. Shared files were retained.", "success")
    return redirect(url_for("library_home"))


@app.post("/series/<path:series_path>/pin")
def series_pin(series_path: str):
    profile = require_profile()
    resolve_visible_series(series_path, own_required=True)
    ACCOUNTS.toggle_pin(profile.id, series_path)
    return redirect(request.referrer or url_for("series_view", series_path=series_path))


@app.post("/series/<path:series_path>/progress")
def series_progress(series_path: str):
    profile = require_profile()
    resolve_visible_series(series_path)
    payload = request.get_json(silent=True) or request.form
    try:
        number = float(payload.get("chapter_num"))
        last_page = int(payload.get("last_page") or 0)
    except (TypeError, ValueError):
        abort(400)
    action = str(payload.get("action") or "open")
    is_read = ACCOUNTS.update_progress(profile.id, series_path, number, action, last_page)
    return jsonify({"ok": True, "read": is_read, "chapter_num": number})


@app.post("/series/<path:series_path>/download-missing")
def series_download_missing(series_path: str):
    series_dir = resolve_visible_series(series_path)
    source = normalize_source(load_metadata(series_dir))
    remote = remote_chapters(str(source.get("series_id") or ""), force=True)
    local = set(local_chapter_map(series_dir))
    missing = [number for number, _ in remote if number not in local]
    if not missing:
        flash("There are no missing chapters to download.", "success")
        return redirect(url_for("series_view", series_path=series_path))
    job = queue_download(series_dir, series_from_metadata(series_dir), chapter_range_from_numbers(missing), chapter_numbers=missing, title_prefix="Download missing")
    return redirect(url_for("job_view", job_id=job.id))


@app.route("/author/<int:staff_id>")
def author_view(staff_id: int):
    profile = require_profile()
    try:
        data = anilist_staff(staff_id)
    except Exception as exc:
        flash(f"Author information could not be loaded: {exc}", "error")
        return redirect(request.referrer or url_for("library_home"))
    if not data:
        abort(404)

    name_block = data.get("name") if isinstance(data.get("name"), dict) else {}
    image_block = data.get("image") if isinstance(data.get("image"), dict) else {}
    accessible_by_anilist: Dict[int, str] = {}
    for series_dir in list_series_dirs():
        series_path = relpath(series_dir)
        if not ACCOUNTS.accessible_series(profile.id, series_path):
            continue
        metadata = load_metadata(series_dir)
        source = normalize_source(metadata)
        metadata_block = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
        try:
            media_id = int(source.get("anilist_id") or metadata_block.get("source_id"))
        except (TypeError, ValueError):
            continue
        accessible_by_anilist[media_id] = series_path

    works: List[Dict[str, Any]] = []
    for media in data.get("works") or []:
        if not isinstance(media, dict):
            continue
        item = media_payload(media)
        try:
            media_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        item["library_path"] = accessible_by_anilist.get(media_id)
        works.append(item)

    author = {
        "id": staff_id,
        "name": name_block.get("full") or name_block.get("userPreferred") or f"Author {staff_id}",
        "native_name": name_block.get("native") or "",
        "image": image_block.get("large") or image_block.get("medium") or "",
        "description": safe_text(data.get("description") or ""),
        "site_url": data.get("siteUrl") or "",
        "occupations": data.get("primaryOccupations") or [],
        "gender": data.get("gender") or "",
        "age": data.get("age"),
        "home_town": data.get("homeTown") or "",
        "years_active": data.get("yearsActive") or [],
    }
    return render_template("author.html", author=author, works=works)


@app.route("/activity")
def activity_view():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda job: job.created_at, reverse=True)
    return render_template("activity.html", jobs=jobs, events=list_events(100))


@app.route("/library/export.json")
def library_export():
    profile = require_profile()
    payload = {
        "exported_at": now_iso(),
        "profile": profile.username,
        "series": [series_display(resolve_under(LIBRARY_DIR, path), user_id=profile.id) for path in profile_series_paths(profile.id)],
    }
    response = jsonify(payload)
    response.headers["Content-Disposition"] = f'attachment; filename="mangabridge-{profile.username}-library.json"'
    return response


@app.route("/health")
def health():
    return jsonify({"status": "ok", "version": APP_VERSION, "series": len(list_series_dirs()), "profiles": ACCOUNTS.count_profiles(), "time": now_iso()})


@app.post("/series/<path:series_path>/watch")
def series_watch(series_path: str):
    series_dir = resolve_visible_series(series_path)
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
    flash("Monitoring settings updated for the shared physical series.", "success")
    return redirect(url_for("series_view", series_path=series_path))


@app.post("/series/<path:series_path>/download")
def series_download(series_path: str):
    series_dir = resolve_visible_series(series_path)
    chapter_range = request.form.get("chapter_range", "all").strip() or "all"
    job = queue_download(series_dir, series_from_metadata(series_dir), chapter_range, title_prefix="Download")
    return redirect(url_for("job_view", job_id=job.id))


@app.post("/series/<path:series_path>/chapter/<chapter_id>/download")
def chapter_download(series_path: str, chapter_id: str):
    series_dir = resolve_visible_series(series_path)
    number = parse_chapter_key(chapter_id)
    job = queue_download(series_dir, series_from_metadata(series_dir), f"{number:g}", chapter_numbers=[number], title_prefix="Download chapter", chapter_num=number)
    payload = {
        "job_id": job.id,
        "status_url": url_for("job_api", job_id=job.id),
        "job_url": url_for("job_view", job_id=job.id),
        "reader_url": url_for("chapter_read", series_path=series_path, chapter_id=chapter_key(number)),
    }
    if request.headers.get("X-Requested-With") == "fetch" or request.accept_mimetypes.best == "application/json":
        return jsonify(payload), 202
    return redirect(payload["job_url"])


@app.post("/series/<path:series_path>/chapter/<chapter_id>/client-download")
def chapter_client_download(series_path: str, chapter_id: str):
    series_dir = resolve_visible_series(series_path)
    number = parse_chapter_key(chapter_id)
    completion_url = url_for("chapter_client_file", series_path=series_path, chapter_id=chapter_key(number))
    if number in local_chapter_map(series_dir):
        return redirect(completion_url)
    job = queue_download(
        series_dir,
        series_from_metadata(series_dir),
        f"{number:g}",
        chapter_numbers=[number],
        title_prefix="Prepare client file",
        chapter_num=number,
        completion_url=completion_url,
    )
    return redirect(url_for("job_view", job_id=job.id))


@app.get("/series/<path:series_path>/chapter/<chapter_id>/client-file")
def chapter_client_file(series_path: str, chapter_id: str):
    series_dir = resolve_visible_series(series_path)
    number = parse_chapter_key(chapter_id)
    local = local_chapter_map(series_dir).get(number)
    if not local:
        abort(404)
    title = series_from_metadata(series_dir)["title"]
    filename = sanitize_filename(f"{title} - Chapter {number:g}") + ".pdf"
    return send_file(
        local["path"],
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
        conditional=True,
    )


@app.post("/series/<path:series_path>/client-range")
def series_client_range(series_path: str):
    series_dir = resolve_visible_series(series_path)
    chapter_range = request.form.get("chapter_range", "all").strip() or "all"
    numbers = client_chapter_numbers(series_dir, chapter_range, force_remote=True)
    if not numbers:
        flash("No chapters matched that range.", "error")
        return redirect(url_for("series_view", series_path=series_path))
    exact_range = chapter_range_from_numbers(numbers)
    completion_url = url_for("series_client_bundle", series_path=series_path, chapters=chapter_range)
    local_numbers = set(local_chapter_map(series_dir))
    missing = [number for number in numbers if number not in local_numbers]
    if not missing:
        return redirect(completion_url)
    job = queue_download(
        series_dir,
        series_from_metadata(series_dir),
        exact_range,
        chapter_numbers=numbers,
        title_prefix="Prepare offline library",
        completion_url=completion_url,
    )
    return redirect(url_for("job_view", job_id=job.id))


@app.get("/series/<path:series_path>/client-library.zip")
def series_client_bundle(series_path: str):
    series_dir = resolve_visible_series(series_path)
    chapter_range = request.args.get("chapters", "all").strip() or "all"
    local_numbers = sorted(local_chapter_map(series_dir))
    numbers = [
        number
        for number, _ in filter_chapters([(number, "") for number in local_numbers], chapter_range)
    ]
    archive, filename = offline_bundle_for_series(series_dir, numbers)
    return send_file(
        archive,
        mimetype="application/zip",
        as_attachment=True,
        download_name=filename,
        conditional=False,
    )


@app.post("/series/<path:series_path>/chapter/<chapter_id>/delete")
def chapter_delete(series_path: str, chapter_id: str):
    profile = require_profile()
    series_dir = resolve_visible_series(series_path, own_required=True)
    if series_is_active(series_path):
        flash("This series is currently downloading. Delete the chapter after the job finishes.", "error")
        return redirect(url_for("series_view", series_path=series_path))
    number = parse_chapter_key(chapter_id)
    if request.form.get("confirm") != "yes":
        abort(400)
    removed = 0
    for path in list(series_dir.rglob("*.pdf")):
        if chapter_num_from_name(path.name) == number:
            path.unlink(missing_ok=True)
            removed += 1
    append_event("storage", f"{profile.display_name} deleted chapter {number:g}", f"{series_from_metadata(series_dir)['title']} · affects every profile", series_path)
    flash(f"Deleted chapter {number:g}. The shared file was removed for all profiles.", "success")
    return redirect(url_for("series_view", series_path=series_path))


@app.route("/series/<path:series_path>/chapter/<chapter_id>/read")
def chapter_read(series_path: str, chapter_id: str):
    profile = require_profile()
    series_dir = resolve_visible_series(series_path)
    number = parse_chapter_key(chapter_id)
    series = series_display(series_dir, user_id=profile.id)
    navigation = reader_context(series, number)
    local = local_chapter_map(series_dir).get(number)
    if not local:
        return render_template("reader.html", series=series, chapter_num=number, mode="missing", page_count=0, current_downloaded=False, resume_page=0, **navigation)
    try:
        page_count = chapter_pdf_info(local["path"])["page_count"]
    except Exception as exc:
        return render_template("reader.html", series=series, chapter_num=number, mode="error", reader_error=str(exc), page_count=0, current_downloaded=True, resume_page=0, **navigation)
    ACCOUNTS.update_progress(profile.id, series_path, number, "open")
    resume_page = series.get("last_page", 0) if series.get("last_chapter") == number else 0
    return render_template("reader.html", series=series, chapter_num=number, mode="pdf", page_count=page_count, current_downloaded=True, resume_page=resume_page, **navigation)


@app.route("/series/<path:series_path>/chapter/<chapter_id>/page/<int:page_index>")
def chapter_page(series_path: str, chapter_id: str, page_index: int):
    series_dir = resolve_visible_series(series_path)
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
        if target.is_file() and target.suffix.lower() == ".pdf":
            number = chapter_num_from_name(target.name)
            if number is not None:
                for series_dir in list_series_dirs():
                    if series_dir in target.parents and ACCOUNTS.accessible_series(require_profile().id, relpath(series_dir)):
                        return redirect(url_for("chapter_read", series_path=relpath(series_dir), chapter_id=chapter_key(number)))
    return redirect(url_for("library_home"))


@app.route("/file/<path:item_path>")
def serve_file(item_path: str):
    target = resolve_under(LIBRARY_DIR, item_path)
    if not target.is_file() or target.suffix.lower() != ".pdf":
        abort(404)
    allowed = any(target == directory or directory in target.parents for directory in [resolve_under(LIBRARY_DIR, path) for path in profile_series_paths(require_profile().id)])
    if not allowed:
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
    profile = require_profile()
    return jsonify([series_display(resolve_under(LIBRARY_DIR, path), user_id=profile.id) for path in profile_series_paths(profile.id)])


@app.context_processor
def inject_v2_context() -> Dict[str, Any]:
    profile = current_profile()
    spaces = ACCOUNTS.list_spaces_for_user(profile.id) if profile else []
    return {
        "current_profile": profile,
        "shared_spaces": spaces,
        "page_background": page_background(profile.id if profile else None),
        "all_profiles": ACCOUNTS.list_profiles() if profile else [],
    }


# ---------------------------------------------------------------------------
# Kindle / KOReader API
# ---------------------------------------------------------------------------

def kindle_series_dirs() -> List[Path]:
    profile = kindle_profile()
    if not profile:
        return list_series_dirs()
    directories: List[Path] = []
    for series_path in profile_series_paths(profile.id):
        try:
            directory = resolve_under(LIBRARY_DIR, series_path)
        except ValueError:
            continue
        if directory.is_dir():
            directories.append(directory)
    return directories


def find_series_by_id(series_id: str, include_archived: bool = False) -> Optional[Path]:
    candidates = all_series_dirs() if include_archived else kindle_series_dirs()
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




def metadata_cover_images(metadata: Dict[str, Any]) -> Dict[str, str]:
    """Collect all known cover sizes from old and new metadata layouts."""
    result: Dict[str, str] = {}
    metadata_block = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
    for block in (metadata, metadata_block):
        raw_images = block.get("coverImages")
        if isinstance(raw_images, dict):
            for key in ("extraLarge", "large", "medium"):
                value = raw_images.get(key)
                if value:
                    result.setdefault(key, str(value))

        raw_cover = block.get("coverImage")
        if isinstance(raw_cover, dict):
            for key in ("extraLarge", "large", "medium"):
                value = raw_cover.get(key)
                if value:
                    result.setdefault(key, str(value))
        elif raw_cover:
            value = str(raw_cover)
            inferred_key = "extraLarge" if "/extraLarge/" in value else "large"
            result.setdefault(inferred_key, value)
    return result


def metadata_cover_url(metadata: Dict[str, Any]) -> str:
    """Return the highest-resolution cover URL currently stored."""
    images = metadata_cover_images(metadata)
    return images.get("extraLarge") or images.get("large") or images.get("medium") or ""


def refresh_anilist_cover_metadata(series_dir: Path, force: bool = False) -> Dict[str, str]:
    """Upgrade legacy metadata to AniList's largest available cover image.

    Older MangaBridge releases requested only ``coverImage.large``.  Existing
    series are upgraded lazily when their Kindle cover is requested, so users
    do not need to remove and re-add them.
    """
    metadata = load_metadata(series_dir)
    current = metadata_cover_images(metadata)
    if current.get("extraLarge") and not force:
        return current

    source = normalize_source(metadata)
    metadata_block = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
    media_id = source.get("anilist_id") or metadata_block.get("source_id")
    title = str(metadata.get("title") or source.get("title") or series_dir.name)

    try:
        if media_id:
            data = anilist_fetch(int(media_id))
        else:
            data = best_anilist_match(title, anilist_search(title, per_page=6)) or {}
    except Exception:
        return current

    images = cover_images(data)
    if not images:
        return current

    preferred = best_cover(data)
    updated_block = dict(metadata_block)
    updated_block.update(
        {
            "source": "AniList",
            "source_id": data.get("id"),
            "coverImages": images,
            "coverImage": preferred,
            "synced_at": now_iso(),
        }
    )
    metadata["metadata"] = updated_block
    metadata["coverImages"] = images
    metadata["coverImage"] = preferred
    if data.get("id"):
        source["anilist_id"] = data.get("id")
        metadata["source"] = source
    save_metadata(series_dir, metadata)
    return images


def kindle_cover_revision(metadata: Dict[str, Any]) -> str:
    payload = "|".join(
        (
            str(KINDLE_COVER_PROCESSOR_VERSION),
            KINDLE_COVER_FIT,
            f"{KINDLE_COVER_WIDTH}x{KINDLE_COVER_HEIGHT}",
            metadata_cover_url(metadata),
        )
    )
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]


def kindle_cover_url(series_id: str, revision: str = "") -> str:
    encoded_series_id = quote(str(series_id), safe="")
    base = f"/api/kindle/v1/series/{encoded_series_id}/cover"
    return f"{base}?v={quote(revision, safe='')}" if revision else base


def kindle_cover_cache_path(series_dir: Path) -> Path:
    source = normalize_source(load_metadata(series_dir))
    series_id = str(source.get("series_id") or series_dir.name)
    digest = hashlib.sha256(series_id.encode("utf-8", "replace")).hexdigest()[:24]
    return KINDLE_COVER_DIR / f"{digest}.jpg"


def kindle_cover_cache_metadata_path(series_dir: Path) -> Path:
    return kindle_cover_cache_path(series_dir).with_suffix(".json")


def prepare_kindle_cover(series_dir: Path, force: bool = False) -> Path:
    """Fetch a high-resolution AniList cover and create a Kindle-ready JPEG.

    The source preference is AniList ``extraLarge``.  Unlike Pillow's
    ``thumbnail()``, the resize operation below is allowed to enlarge the
    source to the Kindle canvas, so a small image is never left floating in a
    600x800 white field.  ``contain`` preserves the complete artwork; ``cover``
    fills the screen with a small amount of edge cropping when aspect ratios
    differ.
    """
    metadata = load_metadata(series_dir)
    images = metadata_cover_images(metadata)
    if force or not images.get("extraLarge"):
        images = refresh_anilist_cover_metadata(series_dir, force=force)
        metadata = load_metadata(series_dir)

    remote_url = images.get("extraLarge") or images.get("large") or images.get("medium")
    if not remote_url:
        raise FileNotFoundError("No metadata cover is available for this series")

    output_path = kindle_cover_cache_path(series_dir)
    cache_metadata_path = kindle_cover_cache_metadata_path(series_dir)
    revision = kindle_cover_revision(metadata)
    cache_metadata = read_json(cache_metadata_path, {})
    cache_valid = (
        output_path.exists()
        and output_path.stat().st_size > 0
        and cache_metadata.get("revision") == revision
        and cache_metadata.get("source_url") == remote_url
        and cache_metadata.get("processor_version") == KINDLE_COVER_PROCESSOR_VERSION
    )
    if cache_valid and not force:
        return output_path

    response = requests.get(
        remote_url,
        timeout=(10, 30),
        headers={
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "User-Agent": "MangaBridge-Server/1.0.5",
        },
    )
    response.raise_for_status()
    if len(response.content) > 25 * 1024 * 1024:
        raise RuntimeError("Cover image is unexpectedly large")

    with Image.open(io.BytesIO(response.content)) as source_image:
        source_image = ImageOps.exif_transpose(source_image).convert("L")
        source_width, source_height = source_image.size
        if source_width < 100 or source_height < 100:
            raise RuntimeError(f"Metadata cover is unexpectedly small: {source_width}x{source_height}")

        target_size = (KINDLE_COVER_WIDTH, KINDLE_COVER_HEIGHT)
        if KINDLE_COVER_FIT == "cover":
            canvas = ImageOps.fit(
                source_image,
                target_size,
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
        else:
            scale = min(KINDLE_COVER_WIDTH / source_width, KINDLE_COVER_HEIGHT / source_height)
            draw_width = max(1, round(source_width * scale))
            draw_height = max(1, round(source_height * scale))
            resized = source_image.resize((draw_width, draw_height), Image.Resampling.LANCZOS)
            canvas = Image.new("L", target_size, 255)
            left = (KINDLE_COVER_WIDTH - draw_width) // 2
            top = (KINDLE_COVER_HEIGHT - draw_height) // 2
            canvas.paste(resized, (left, top))

        temporary = output_path.with_suffix(".tmp.jpg")
        canvas.save(
            temporary,
            format="JPEG",
            quality=KINDLE_COVER_QUALITY,
            optimize=True,
            progressive=False,
            dpi=(167, 167),
        )
    temporary.replace(output_path)
    atomic_write_json(
        cache_metadata_path,
        {
            "revision": revision,
            "processor_version": KINDLE_COVER_PROCESSOR_VERSION,
            "source_url": remote_url,
            "source_kind": "extraLarge" if remote_url == images.get("extraLarge") else "fallback",
            "source_width": source_width,
            "source_height": source_height,
            "output_width": KINDLE_COVER_WIDTH,
            "output_height": KINDLE_COVER_HEIGHT,
            "fit": KINDLE_COVER_FIT,
            "created_at": now_iso(),
        },
    )
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
    lock = series_download_lock(series_path)
    lock.acquire()
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
        lock.release()


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
    metadata = series.get("metadata") or {}
    cover_available = bool(metadata_cover_url(metadata))
    revision = kindle_cover_revision(metadata) if cover_available else ""
    has_progress = series.get("last_chapter") is not None
    kindle_last_page = int(series.get("last_page") or 0) + 1 if has_progress else 0
    return {
        "id": series["series_id"],
        "title": series["title"],
        "downloaded_count": series["downloaded_count"],
        "available_count": series["remote_chapters"],
        "latest": series["remote_latest"],
        "unread_count": series["unread_count"],
        "continue_chapter": series["continue_chapter"],
        "last_chapter": series.get("last_chapter"),
        "last_page": kindle_last_page,
        "last_total_pages": series.get("last_total_pages", 0),
        "last_read_at": series.get("last_read_at"),
        "last_read_epoch": series.get("last_read_epoch", 0),
        "watching": bool(series["watch"]["enabled"]),
        "cover_url": kindle_cover_url(series["series_id"], revision) if cover_available else None,
        "cover_revision": revision or None,
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

    profile = kindle_profile()
    reading = ACCOUNTS.progress_for_series(profile.id, relpath(series_dir)) if profile else (metadata.get("reading") if isinstance(metadata.get("reading"), dict) else {})
    read_numbers: set[float] = set()
    for value in reading.get("read_chapters") or []:
        try:
            read_numbers.add(float(value))
        except (TypeError, ValueError):
            continue
    unread = [number for number in sorted(local) if number not in read_numbers]
    unread_available = [number for number in available_numbers if number not in read_numbers]
    last_chapter = None
    try:
        if reading.get("last_chapter") is not None:
            last_chapter = float(reading["last_chapter"])
    except (TypeError, ValueError):
        last_chapter = None
    continue_chapter = None
    if last_chapter in available_numbers and last_chapter not in read_numbers:
        continue_chapter = last_chapter
    elif last_chapter is not None:
        continue_chapter = next((number for number in unread_available if number > last_chapter), None)
    if continue_chapter is None and unread_available:
        continue_chapter = unread_available[0]
    if continue_chapter is None and available_numbers:
        continue_chapter = available_numbers[-1]
    watch = metadata.get("watch") if isinstance(metadata.get("watch"), dict) else default_watch()

    cover_available = bool(metadata_cover_url(metadata))
    revision = kindle_cover_revision(metadata) if cover_available else ""
    return {
        "id": series_id,
        "title": title,
        "downloaded_count": len(local),
        "available_count": len(available_numbers),
        "latest": max(available_numbers) if available_numbers else None,
        "unread_count": len(unread),
        "continue_chapter": continue_chapter,
        "last_chapter": reading.get("last_chapter"),
        "last_page": int(reading.get("last_page") or 0) + 1 if reading.get("last_chapter") is not None else 0,
        "last_total_pages": int(reading.get("last_total_pages") or 0),
        "last_read_at": reading.get("last_read_at"),
        "last_read_epoch": int(reading.get("last_read_epoch") or 0),
        "watching": bool(watch.get("enabled")),
        "cover_url": kindle_cover_url(series_id, revision) if cover_available else None,
        "cover_revision": revision or None,
    }


def kindle_series_payload(series_dir: Path, force_remote: bool = False) -> Dict[str, Any]:
    profile = kindle_profile()
    series_path = relpath(series_dir)
    series = series_display(series_dir, force_remote=force_remote, user_id=profile.id if profile else None)
    progress_entries = ACCOUNTS.progress_entries_for_series(profile.id, series_path) if profile else {}
    payload = kindle_summary_from_display(series)
    all_numbers = sorted(float(row["number"]) for row in series["remote_chapter_rows"])
    read_numbers = {number for number, progress in progress_entries.items() if progress.get("is_read")}
    last_chapter = series.get("last_chapter")
    kindle_continue = None
    if last_chapter in all_numbers and last_chapter not in read_numbers:
        kindle_continue = last_chapter
    elif last_chapter is not None:
        kindle_continue = next((number for number in all_numbers if number > last_chapter and number not in read_numbers), None)
    if kindle_continue is None:
        kindle_continue = next((number for number in all_numbers if number not in read_numbers), None)
    if kindle_continue is None and all_numbers:
        kindle_continue = all_numbers[-1]
    payload["continue_chapter"] = kindle_continue
    payload["chapters"] = []
    for row in series["remote_chapter_rows"]:
        progress = progress_entries.get(float(row["number"]), {})
        payload["chapters"].append({
            "number": row["number"],
            "downloaded_on_server": bool(row["downloaded"]),
            "read_on_server": bool(row["read"]),
            "size": row["size"],
            "last_page": int(progress.get("last_page") or 0) + 1 if progress else 0,
            "total_pages": int(progress.get("total_pages") or 0),
            "progress_updated_at": progress.get("updated_at"),
            "progress_updated_epoch": int(progress.get("updated_epoch") or 0),
        })
    return payload


@app.route("/api/kindle/v1/ping")
def kindle_ping():
    return jsonify(
        {
            "ok": True,
            "api_version": 1,
            "server": "MangaDL Ultimate",
            "bridge_version": APP_VERSION,
            "web_profiles": True,
            "selected_profile": kindle_profile().username if kindle_profile() else None,
            "kindle_profile": {
                "pdf_mode": KINDLE_PDF_MODE,
                "cover_width": KINDLE_COVER_WIDTH,
                "cover_height": KINDLE_COVER_HEIGHT,
                "cover_fit": KINDLE_COVER_FIT,
                "cover_source": "AniList extraLarge",
                "cover_processor_version": KINDLE_COVER_PROCESSOR_VERSION,
                "width": KINDLE_RENDER_WIDTH,
                "max_height": KINDLE_RENDER_MAX_HEIGHT,
                "jpeg_quality": KINDLE_JPEG_QUALITY,
                "cache_limit_mb": KINDLE_CACHE_MAX_BYTES // (1024 * 1024),
            },
        }
    )


@app.route("/api/kindle/v1/library")
def kindle_library():
    rows = [kindle_library_summary(path) for path in kindle_series_dirs()]
    rows.sort(key=lambda item: item["title"].lower())
    return jsonify({"api_version": 1, "series": rows, "generated_at": now_iso()})


@app.route("/api/kindle/v1/series/<series_id>")
def kindle_series(series_id: str):
    series_dir = find_series_by_id(series_id)
    if not series_dir:
        abort(404)
    force = request.args.get("refresh") in {"1", "true", "yes"}
    return jsonify(kindle_series_payload(series_dir, force_remote=force))



@app.route("/api/kindle/v1/series/<series_id>/cover")
def kindle_series_cover(series_id: str):
    series_dir = find_series_by_id(series_id)
    if not series_dir:
        abort(404)
    force = request.args.get("refresh") in {"1", "true", "yes"}
    try:
        cover_path = prepare_kindle_cover(series_dir, force=force)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not fetch metadata cover: {exc}"}), 502
    except (OSError, ValueError, RuntimeError) as exc:
        return jsonify({"error": f"Could not prepare metadata cover: {exc}"}), 500
    title = sanitize_filename(series_from_metadata(series_dir)["title"])
    cache_metadata = read_json(kindle_cover_cache_metadata_path(series_dir), {})
    response = send_file(
        cover_path,
        mimetype="image/jpeg",
        as_attachment=False,
        download_name=f"{title}-cover.jpg",
        conditional=True,
        max_age=604800,
    )
    response.headers["X-MangaBridge-Cover-Revision"] = str(cache_metadata.get("revision") or "")
    response.headers["X-MangaBridge-Cover-Source"] = str(cache_metadata.get("source_kind") or "")
    response.headers["X-MangaBridge-Cover-Source-Size"] = (
        f"{cache_metadata.get('source_width', '')}x{cache_metadata.get('source_height', '')}"
    )
    return response


@app.route("/api/kindle/v1/series/<series_id>/chapter/<chapter_id>/progress", methods=["GET", "POST"])
def kindle_chapter_progress(series_id: str, chapter_id: str):
    profile = kindle_profile()
    if not profile:
        return jsonify({"error": "No MangaBridge profile is configured"}), 409
    series_dir = find_series_by_id(series_id)
    if not series_dir:
        abort(404)
    number = parse_chapter_key(chapter_id)
    series_path = relpath(series_dir)
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        try:
            kindle_page = max(0, int(payload.get("page") or payload.get("last_page") or 0))
            total_pages = max(0, int(payload.get("total_pages") or 0))
        except (TypeError, ValueError):
            return jsonify({"error": "page and total_pages must be integers"}), 400
        stored_page = max(0, kindle_page - 1) if kindle_page > 0 else 0
        complete = bool(payload.get("complete")) or (total_pages > 0 and kindle_page >= total_pages)
        ACCOUNTS.update_progress(
            profile.id,
            series_path,
            number,
            "complete" if complete else "open",
            last_page=stored_page,
            total_pages=total_pages,
        )
    progress = ACCOUNTS.chapter_progress(profile.id, series_path, number)
    progress["last_page"] = int(progress.get("last_page") or 0) + 1 if progress.get("updated_at") else 0
    progress.update({
        "api_version": 1,
        "series_id": series_id,
        "profile": profile.username,
    })
    return jsonify(progress)


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
        "csrf_field": csrf_field,
        "csrf_token": csrf_token,
    }


load_persisted_jobs()
start_watcher_once()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")), debug=False)
