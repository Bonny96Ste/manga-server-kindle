from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence




def hash_password(password: str) -> str:
    iterations = 260000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password_hash(stored: str, password: str) -> bool:
    try:
        algorithm, raw_iterations, salt, expected = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(raw_iterations)).hex()
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_epoch(value: Any) -> int:
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class Profile:
    id: int
    username: str
    display_name: str
    avatar_seed: str
    is_admin: bool
    has_password: bool
    created_at: str


class AccountStore:
    """SQLite-backed profile and sharing state.

    Manga files remain in the existing global library directory. This database only
    records who has a series in their library, per-user progress, and shared views.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._lock, self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL DEFAULT '',
                    avatar_seed TEXT NOT NULL DEFAULT '',
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_series (
                    user_id INTEGER NOT NULL,
                    series_path TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, series_path),
                    FOREIGN KEY (user_id) REFERENCES profiles(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS reading_progress (
                    user_id INTEGER NOT NULL,
                    series_path TEXT NOT NULL,
                    chapter_num REAL NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0,
                    last_page INTEGER NOT NULL DEFAULT 0,
                    total_pages INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, series_path, chapter_num),
                    FOREIGN KEY (user_id) REFERENCES profiles(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS shared_spaces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    owner_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (owner_id) REFERENCES profiles(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS shared_space_members (
                    space_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','accepted','declined')),
                    invited_by INTEGER NOT NULL,
                    invited_at TEXT NOT NULL,
                    responded_at TEXT,
                    PRIMARY KEY (space_id, user_id),
                    FOREIGN KEY (space_id) REFERENCES shared_spaces(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES profiles(id) ON DELETE CASCADE,
                    FOREIGN KEY (invited_by) REFERENCES profiles(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS shared_hidden_series (
                    space_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    series_path TEXT NOT NULL,
                    hidden_at TEXT NOT NULL,
                    PRIMARY KEY (space_id, user_id, series_path),
                    FOREIGN KEY (space_id) REFERENCES shared_spaces(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES profiles(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_user_series_path ON user_series(series_path);
                CREATE INDEX IF NOT EXISTS idx_progress_series ON reading_progress(series_path);
                CREATE INDEX IF NOT EXISTS idx_shared_members_user ON shared_space_members(user_id, status);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(reading_progress)").fetchall()}
            if "total_pages" not in columns:
                db.execute("ALTER TABLE reading_progress ADD COLUMN total_pages INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _profile(row: sqlite3.Row) -> Profile:
        return Profile(
            id=int(row["id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            avatar_seed=str(row["avatar_seed"] or row["username"]),
            is_admin=bool(row["is_admin"]),
            has_password=bool(row["password_hash"]),
            created_at=str(row["created_at"]),
        )

    def count_profiles(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0])

    def list_profiles(self) -> List[Profile]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM profiles ORDER BY display_name COLLATE NOCASE").fetchall()
        return [self._profile(row) for row in rows]

    def get_profile(self, user_id: int) -> Optional[Profile]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM profiles WHERE id = ?", (user_id,)).fetchone()
        return self._profile(row) if row else None

    def get_profile_by_username(self, username: str) -> Optional[Profile]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM profiles WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        return self._profile(row) if row else None

    def verify_password(self, user_id: int, password: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT password_hash FROM profiles WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return False
        stored = str(row["password_hash"] or "")
        return (not stored and not password) or (bool(stored) and verify_password_hash(stored, password))

    def create_profile(self, username: str, display_name: str, password: str = "", *, admin: bool = False) -> Profile:
        username = username.strip()
        display_name = display_name.strip() or username
        if not username or not all(char.isalnum() or char in "._-" for char in username):
            raise ValueError("Username may contain letters, numbers, dots, underscores and hyphens")
        password_hash = hash_password(password) if password else ""
        with self._lock, self.connect() as db:
            cursor = db.execute(
                "INSERT INTO profiles(username, display_name, password_hash, avatar_seed, is_admin, created_at) VALUES(?,?,?,?,?,?)",
                (username, display_name, password_hash, username, 1 if admin else 0, now_iso()),
            )
            user_id = int(cursor.lastrowid)
        profile = self.get_profile(user_id)
        if not profile:
            raise RuntimeError("Profile creation failed")
        return profile

    def update_profile(self, user_id: int, display_name: str, password: Optional[str] = None) -> None:
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("Display name is required")
        with self._lock, self.connect() as db:
            if password is None:
                db.execute("UPDATE profiles SET display_name = ? WHERE id = ?", (display_name, user_id))
            else:
                password_hash = hash_password(password) if password else ""
                db.execute(
                    "UPDATE profiles SET display_name = ?, password_hash = ? WHERE id = ?",
                    (display_name, password_hash, user_id),
                )

    def assign_existing_library(self, user_id: int, series_paths: Iterable[str]) -> None:
        with self._lock, self.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO user_series(user_id, series_path, added_at) VALUES(?,?,?)",
                [(user_id, path, now_iso()) for path in series_paths],
            )

    def add_series(self, user_id: int, series_path: str) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO user_series(user_id, series_path, added_at) VALUES(?,?,?)",
                (user_id, series_path, now_iso()),
            )

    def remove_series(self, user_id: int, series_path: str) -> None:
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM user_series WHERE user_id = ? AND series_path = ?", (user_id, series_path))
            db.execute("DELETE FROM reading_progress WHERE user_id = ? AND series_path = ?", (user_id, series_path))
            db.execute("DELETE FROM shared_hidden_series WHERE user_id = ? AND series_path = ?", (user_id, series_path))

    def remove_series_everywhere(self, series_path: str) -> None:
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM user_series WHERE series_path = ?", (series_path,))
            db.execute("DELETE FROM reading_progress WHERE series_path = ?", (series_path,))
            db.execute("DELETE FROM shared_hidden_series WHERE series_path = ?", (series_path,))

    def has_series(self, user_id: int, series_path: str) -> bool:
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM user_series WHERE user_id = ? AND series_path = ?", (user_id, series_path)
            ).fetchone() is not None

    def list_user_series(self, user_id: int) -> List[str]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT series_path FROM user_series WHERE user_id = ? ORDER BY added_at DESC", (user_id,)
            ).fetchall()
        return [str(row["series_path"]) for row in rows]

    def series_owners(self, series_path: str, user_ids: Optional[Sequence[int]] = None) -> List[Profile]:
        params: List[Any] = [series_path]
        where = "us.series_path = ?"
        if user_ids:
            placeholders = ",".join("?" for _ in user_ids)
            where += f" AND p.id IN ({placeholders})"
            params.extend(user_ids)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT p.* FROM profiles p JOIN user_series us ON us.user_id = p.id WHERE {where} ORDER BY p.display_name COLLATE NOCASE",
                params,
            ).fetchall()
        return [self._profile(row) for row in rows]

    def series_user_count(self, series_path: str) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM user_series WHERE series_path = ?", (series_path,)).fetchone()[0])

    def toggle_pin(self, user_id: int, series_path: str) -> bool:
        with self._lock, self.connect() as db:
            row = db.execute(
                "SELECT pinned FROM user_series WHERE user_id = ? AND series_path = ?", (user_id, series_path)
            ).fetchone()
            if not row:
                raise ValueError("Series is not in this profile's library")
            value = 0 if row["pinned"] else 1
            db.execute(
                "UPDATE user_series SET pinned = ? WHERE user_id = ? AND series_path = ?",
                (value, user_id, series_path),
            )
            return bool(value)

    def is_pinned(self, user_id: int, series_path: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT pinned FROM user_series WHERE user_id = ? AND series_path = ?", (user_id, series_path)
            ).fetchone()
        return bool(row and row["pinned"])

    def update_progress(
        self,
        user_id: int,
        series_path: str,
        chapter_num: float,
        action: str,
        last_page: int = 0,
        total_pages: int = 0,
    ) -> bool:
        with self._lock, self.connect() as db:
            existing = db.execute(
                "SELECT is_read, last_page, total_pages FROM reading_progress WHERE user_id = ? AND series_path = ? AND chapter_num = ?",
                (user_id, series_path, chapter_num),
            ).fetchone()
            is_read = bool(existing["is_read"]) if existing else False
            page = max(max(0, int(last_page or 0)), int(existing["last_page"]) if existing else 0)
            pages = max(max(0, int(total_pages or 0)), int(existing["total_pages"]) if existing else 0)
            if action in {"complete", "read"}:
                is_read = True
            elif action == "unread":
                is_read = False
            if pages and page >= max(0, pages - 1):
                is_read = True
            db.execute(
                """INSERT INTO reading_progress(user_id, series_path, chapter_num, is_read, last_page, total_pages, updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(user_id, series_path, chapter_num)
                   DO UPDATE SET is_read=excluded.is_read, last_page=excluded.last_page,
                                 total_pages=excluded.total_pages, updated_at=excluded.updated_at""",
                (user_id, series_path, chapter_num, 1 if is_read else 0, page, pages, now_iso()),
            )
        return is_read

    def chapter_progress(self, user_id: int, series_path: str, chapter_num: float) -> Dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT chapter_num, is_read, last_page, total_pages, updated_at FROM reading_progress WHERE user_id = ? AND series_path = ? AND chapter_num = ?",
                (user_id, series_path, chapter_num),
            ).fetchone()
        if not row:
            return {
                "chapter": float(chapter_num),
                "is_read": False,
                "last_page": 0,
                "total_pages": 0,
                "updated_at": None,
                "updated_epoch": 0,
            }
        return {
            "chapter": float(row["chapter_num"]),
            "is_read": bool(row["is_read"]),
            "last_page": int(row["last_page"]),
            "total_pages": int(row["total_pages"]),
            "updated_at": str(row["updated_at"]),
            "updated_epoch": iso_epoch(row["updated_at"]),
        }

    def progress_entries_for_series(self, user_id: int, series_path: str) -> Dict[float, Dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT chapter_num, is_read, last_page, total_pages, updated_at FROM reading_progress WHERE user_id = ? AND series_path = ?",
                (user_id, series_path),
            ).fetchall()
        return {
            float(row["chapter_num"]): {
                "chapter": float(row["chapter_num"]),
                "is_read": bool(row["is_read"]),
                "last_page": int(row["last_page"]),
                "total_pages": int(row["total_pages"]),
                "updated_at": str(row["updated_at"]),
                "updated_epoch": iso_epoch(row["updated_at"]),
            }
            for row in rows
        }

    def progress_for_series(self, user_id: int, series_path: str) -> Dict[str, Any]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT chapter_num, is_read, last_page, total_pages, updated_at FROM reading_progress WHERE user_id = ? AND series_path = ? ORDER BY chapter_num",
                (user_id, series_path),
            ).fetchall()
        read = {float(row["chapter_num"]) for row in rows if row["is_read"]}
        latest_row = max(rows, key=lambda row: str(row["updated_at"]), default=None)
        return {
            "read_chapters": read,
            "last_chapter": float(latest_row["chapter_num"]) if latest_row else None,
            "last_page": int(latest_row["last_page"]) if latest_row else 0,
            "last_total_pages": int(latest_row["total_pages"]) if latest_row else 0,
            "last_read_at": str(latest_row["updated_at"]) if latest_row else None,
            "last_read_epoch": iso_epoch(latest_row["updated_at"]) if latest_row else 0,
        }

    def progress_users_for_series(self, series_path: str) -> List[Dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT p.id, p.username, p.display_name,
                          MAX(rp.updated_at) AS last_read_at,
                          SUM(CASE WHEN rp.is_read = 1 THEN 1 ELSE 0 END) AS read_count,
                          COUNT(rp.chapter_num) AS touched_count
                   FROM profiles p
                   JOIN user_series us ON us.user_id = p.id AND us.series_path = ?
                   LEFT JOIN reading_progress rp ON rp.user_id = p.id AND rp.series_path = us.series_path
                   GROUP BY p.id ORDER BY p.display_name COLLATE NOCASE""",
                (series_path,),
            ).fetchall()
        return [dict(row) for row in rows]

    def chapter_users(self, series_path: str, chapter_num: float) -> List[Dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT p.id, p.username, p.display_name, rp.is_read, rp.last_page, rp.updated_at
                   FROM profiles p
                   JOIN user_series us ON us.user_id = p.id AND us.series_path = ?
                   LEFT JOIN reading_progress rp ON rp.user_id = p.id AND rp.series_path = us.series_path AND rp.chapter_num = ?
                   ORDER BY p.display_name COLLATE NOCASE""",
                (series_path, chapter_num),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_space(self, owner_id: int, name: str) -> int:
        name = name.strip()
        if not name:
            raise ValueError("Shared library name is required")
        with self._lock, self.connect() as db:
            cursor = db.execute(
                "INSERT INTO shared_spaces(name, owner_id, created_at) VALUES(?,?,?)", (name, owner_id, now_iso())
            )
            space_id = int(cursor.lastrowid)
            db.execute(
                "INSERT INTO shared_space_members(space_id, user_id, status, invited_by, invited_at, responded_at) VALUES(?,?,?,?,?,?)",
                (space_id, owner_id, "accepted", owner_id, now_iso(), now_iso()),
            )
        return space_id

    def invite_to_space(self, space_id: int, user_id: int, invited_by: int) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                """INSERT INTO shared_space_members(space_id, user_id, status, invited_by, invited_at, responded_at)
                   VALUES(?,?,?,?,?,NULL)
                   ON CONFLICT(space_id, user_id)
                   DO UPDATE SET status='pending', invited_by=excluded.invited_by, invited_at=excluded.invited_at, responded_at=NULL""",
                (space_id, user_id, "pending", invited_by, now_iso()),
            )

    def respond_invite(self, space_id: int, user_id: int, accept: bool) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                "UPDATE shared_space_members SET status = ?, responded_at = ? WHERE space_id = ? AND user_id = ? AND status = 'pending'",
                ("accepted" if accept else "declined", now_iso(), space_id, user_id),
            )

    def leave_space(self, space_id: int, user_id: int) -> None:
        with self._lock, self.connect() as db:
            owner = db.execute("SELECT owner_id FROM shared_spaces WHERE id = ?", (space_id,)).fetchone()
            if owner and int(owner["owner_id"]) == user_id:
                db.execute("DELETE FROM shared_spaces WHERE id = ?", (space_id,))
            else:
                db.execute("DELETE FROM shared_space_members WHERE space_id = ? AND user_id = ?", (space_id, user_id))

    def list_spaces_for_user(self, user_id: int, include_pending: bool = True) -> List[Dict[str, Any]]:
        statuses = ("accepted", "pending") if include_pending else ("accepted",)
        placeholders = ",".join("?" for _ in statuses)
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT s.*, m.status, owner.display_name AS owner_name,
                           (SELECT COUNT(*) FROM shared_space_members sm WHERE sm.space_id=s.id AND sm.status='accepted') AS member_count
                    FROM shared_spaces s
                    JOIN shared_space_members m ON m.space_id=s.id AND m.user_id=?
                    JOIN profiles owner ON owner.id=s.owner_id
                    WHERE m.status IN ({placeholders})
                    ORDER BY s.name COLLATE NOCASE""",
                (user_id, *statuses),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_space(self, space_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as db:
            row = db.execute(
                "SELECT s.*, p.display_name AS owner_name FROM shared_spaces s JOIN profiles p ON p.id=s.owner_id WHERE s.id=?",
                (space_id,),
            ).fetchone()
        return dict(row) if row else None

    def space_members(self, space_id: int, accepted_only: bool = False) -> List[Dict[str, Any]]:
        where = "AND m.status='accepted'" if accepted_only else ""
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT p.id, p.username, p.display_name, p.avatar_seed, p.is_admin, m.status, m.invited_at, m.responded_at
                    FROM shared_space_members m JOIN profiles p ON p.id=m.user_id
                    WHERE m.space_id=? {where} ORDER BY p.display_name COLLATE NOCASE""",
                (space_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def is_space_member(self, space_id: int, user_id: int, accepted: bool = True) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT status FROM shared_space_members WHERE space_id=? AND user_id=?", (space_id, user_id)
            ).fetchone()
        return bool(row and (not accepted or row["status"] == "accepted"))

    def shared_series(self, space_id: int, viewer_id: int) -> List[Dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT us.series_path,
                          GROUP_CONCAT(p.display_name, '|||') AS owner_names,
                          GROUP_CONCAT(p.id, ',') AS owner_ids,
                          MAX(us.added_at) AS newest_added
                   FROM user_series us
                   JOIN shared_space_members sm ON sm.user_id=us.user_id AND sm.space_id=? AND sm.status='accepted'
                   JOIN profiles p ON p.id=us.user_id
                   LEFT JOIN shared_hidden_series hs ON hs.space_id=? AND hs.user_id=? AND hs.series_path=us.series_path
                   WHERE hs.series_path IS NULL
                   GROUP BY us.series_path ORDER BY newest_added DESC""",
                (space_id, space_id, viewer_id),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["owner_names"] = [name for name in str(item.get("owner_names") or "").split("|||") if name]
            item["owner_ids"] = [int(value) for value in str(item.get("owner_ids") or "").split(",") if value]
            result.append(item)
        return result

    def hide_shared_series(self, space_id: int, user_id: int, series_path: str, hidden: bool = True) -> None:
        with self._lock, self.connect() as db:
            if hidden:
                db.execute(
                    "INSERT OR REPLACE INTO shared_hidden_series(space_id,user_id,series_path,hidden_at) VALUES(?,?,?,?)",
                    (space_id, user_id, series_path, now_iso()),
                )
            else:
                db.execute(
                    "DELETE FROM shared_hidden_series WHERE space_id=? AND user_id=? AND series_path=?",
                    (space_id, user_id, series_path),
                )


    def hidden_shared_series(self, space_id: int, user_id: int) -> List[str]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT series_path FROM shared_hidden_series WHERE space_id=? AND user_id=? ORDER BY hidden_at DESC",
                (space_id, user_id),
            ).fetchall()
        return [str(row["series_path"]) for row in rows]

    def accessible_series(self, user_id: int, series_path: str) -> bool:
        if self.has_series(user_id, series_path):
            return True
        with self.connect() as db:
            row = db.execute(
                """SELECT 1
                   FROM shared_space_members mine
                   JOIN shared_space_members theirs ON theirs.space_id=mine.space_id AND theirs.status='accepted'
                   JOIN user_series us ON us.user_id=theirs.user_id AND us.series_path=?
                   WHERE mine.user_id=? AND mine.status='accepted' LIMIT 1""",
                (series_path, user_id),
            ).fetchone()
        return row is not None

    def first_profile(self) -> Optional[Profile]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM profiles ORDER BY is_admin DESC, id ASC LIMIT 1").fetchone()
        return self._profile(row) if row else None
