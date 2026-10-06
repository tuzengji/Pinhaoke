"""Pinhaoke Course Search API.

Reads standalone SQLite databases (term-switched at request time):
  spring →
    - 2026春季学期本科生课程.db  (undergraduate, AS main)
    - 2026春季学期研究生课程.db  (graduate,      AS gr)
  summer →
    - 2026暑期本科生课程.db      (undergraduate, AS main)
  fall →
    - 2026秋季学期本科生课程.db  (undergraduate, AS main)
    - 2026秋季学期研究生课程.db  (graduate,      AS gr)

The DBs share the basic_info columns but have different detail schemas. Cross-DB
queries use ATTACH + UNION ALL. Each row carries a prefixed string id:
    "u<basic_info.id>"  spring undergrad
    "g<basic_info.id>"  spring graduate
    "s<basic_info.id>"  summer undergrad
    "a<basic_info.id>"  fall undergrad
    "r<basic_info.id>"  fall graduate

The prefix alone determines which DB the detail endpoint opens, so callers do
NOT need to pass ?term= when fetching a specific course.
"""
import base64
import fcntl
from contextlib import contextmanager
from copy import deepcopy
from functools import lru_cache
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "数据库"
UG_DB = DB_DIR / "2026春季学期本科生课程.db"
GR_DB = DB_DIR / "2026春季学期研究生课程.db"
SUMMER_DB = DB_DIR / "2026暑期本科生课程.db"
FALL_UG_DB = DB_DIR / "2026秋季学期本科生课程.db"
FALL_GR_DB = DB_DIR / "2026秋季学期研究生课程.db"
REVIEWS_DB = DB_DIR / "树洞课程评测.db"

# 留言板与六个只读正式库分离，不进入仓库。
# 生产由 systemd StateDirectory 提供 /var/lib/pinhaoke 并通过环境变量指定路径。
MESSAGES_DB_PATH = Path(
    os.environ.get("PINHAOKE_MESSAGES_DB", "") or (BASE_DIR / "留言板.db")
)
MESSAGE_MAX_LENGTH = 500
MESSAGE_PAGE_SIZE_MAX = 50
DEFAULT_NICKNAME = "路过的 PKUer"
NICKNAME_MAX_LENGTH = 30
# (窗口秒数, 每个 IP 哈希在窗口内的发布上限)
MESSAGE_RATE_LIMITS = ((3600, 5), (86400, 20))

# 访问统计库，同为可写数据、与只读正式库分离，不进入仓库。
STATS_DB_PATH = Path(
    os.environ.get("PINHAOKE_STATS_DB", "") or (BASE_DIR / "访问统计.db")
)
# 访问按北京时间分日，同一 IP 哈希当日只算一名访客、浏览量累加。
STATS_TZ_OFFSET_SECONDS = 8 * 3600
STATS_TREND_DAYS = 7

# 账户库：第三份可写数据，保存账号、密保、会话与课程收藏，不进入仓库。
ACCOUNTS_DB_PATH = Path(
    os.environ.get("PINHAOKE_ACCOUNTS_DB", "") or (BASE_DIR / "账户.db")
)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
SECURITY_QUESTION_MIN = 1
SECURITY_QUESTION_MAX = 3
SECURITY_QUESTION_TEXT_MAX = 60
SECURITY_ANSWER_MIN = 2
SECURITY_ANSWER_MAX = 64
# 每次 scrypt 约占 16 MiB 内存；n 提到 2**15 会超出 hashlib 默认 maxmem。
SCRYPT_PARAMS = {"n": 2 ** 14, "r": 8, "p": 1}
# 每个 worker 最多两次并发哈希，防止线程池并发把 2G 内存打爆。
_SCRYPT_GATE = threading.BoundedSemaphore(2)
_DUMMY_SECRET_HASH = None
SESSION_COOKIE = "pinhaoke_session"
SESSION_TTL_SECONDS = 180 * 86400
SESSION_REFRESH_SECONDS = 86400
FAVORITES_MAX = 300
COLLECTIONS_MAX = 50          # 每账号自定义收藏夹上限，不含默认夹
COLLECTION_NAME_MAX = 30      # 收藏夹名 strip() 后最大长度
DEFAULT_COLLECTION_NAME = "默认收藏夹"
# kind -> ((窗口秒数, 上限), ...)；subject 为 IP 哈希、username_key 或固定 "*"。
AUTH_RATE_LIMITS = {
    "register_ip": ((3600, 3), (86400, 10)),
    "login_fail_ip": ((900, 10),),
    "login_fail_user": ((900, 10),),
    "reset_lookup_ip": ((3600, 10),),
    "reset_fail_ip": ((3600, 20),),
    "reset_fail_user": ((3600, 5),),
    "secret_verify_global": ((60, 300),),
    "favorite_write_ip": ((3600, 300),),
}
AUTH_EVENT_RETENTION_SECONDS = 2 * 86400
_TERM_LABEL_RE = re.compile(r"^\d{4}(?:春季学期|暑期|秋季学期)")

# (alias, path, id_prefix)
TERM_DBS = {
    "spring": [
        ("main", UG_DB, "u"),
        ("gr",   GR_DB, "g"),
    ],
    "summer": [
        ("main", SUMMER_DB, "s"),
    ],
    "fall": [
        ("main", FALL_UG_DB, "a"),
        ("gr",   FALL_GR_DB, "r"),
    ],
}

VALID_TERMS = frozenset(TERM_DBS)
VALID_LANGS = frozenset({"zh", "en", "ja", "ko", "fr", "de", "es", "ru"})
VALID_WEEKDAYS = frozenset({"", "周一", "周二", "周三", "周四", "周五", "周六", "周日"})
# Class-period filter values look like "3-4": a session occupying periods 3 through 4.
# PKU numbers periods 1-13; 14 is accepted as headroom, matching parse_first_period().
PERIOD_RANGE_RE = re.compile(r"^(?:[1-9]|1[0-4])-(?:[1-9]|1[0-4])$")
# Every schedule slot is written as "周X" immediately followed by "N~M节" (verified across
# all five course databases), so the weekday token doubles as the left boundary of a range.
SCHEDULE_PERIOD_RE = re.compile(r"周[一二三四五六日](\d{1,2})~(\d{1,2})节")
VALID_SORTS = frozenset({
    "", "name_asc", "name_desc", "pinyin", "pinyin_desc",
    "credits_asc", "credits_desc", "time_asc", "random",
})
COURSE_ID_RE = re.compile(r"^[ugsar][1-9][0-9]{0,18}$")
REVIEW_QUERY_MAX_LENGTH = 120
# 默认列表把 2026 年（北京时间）质量分最高的若干树洞置顶，其余按时间倒序。
REVIEW_FEATURED_COUNT = 10
REVIEW_FEATURED_RANGE = (1767196800, 1798732800)
REVIEW_QUALITY_SCORE_SQL = (
    "(CASE t.post_kind WHEN 'review' THEN 120 ELSE 0 END)"
    " + t.relevant_reply_count * 40"
    " + MIN(LENGTH(t.content), 900) / 6"
)
_REVIEW_SEARCH_STRIP_RE = re.compile(
    r"[\s\u200b\u200c\u200d·•・_—–\-:：,，.。/\\《》<>\[\]【】'\"]+"
)

app = FastAPI(title="Pinhaoke")

HEALTH_CACHE_TTL_SECONDS = 300
_health_cache_lock = threading.Lock()
_health_cache_payload = None
_health_cache_checked_at = None


def _valid_text(value) -> bool:
    # JSON 可携带孤立代理字符，但 UTF-8 哈希、SQLite 和响应编码都无法保存它。
    # 不限制其余字符，保持已有密码（包括空白、控制字符）的验证语义。
    return isinstance(value, str) and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def _database_revision(paths) -> tuple:
    # 正式库只读。原子替换、原位更新和 WAL 写入都会使缓存键改变。
    revision = []
    for path in paths:
        path = path.resolve()
        for candidate in (path, Path(str(path) + "-wal")):
            try:
                info = candidate.stat()
            except FileNotFoundError:
                revision.append((str(candidate), None))
            else:
                revision.append((str(candidate), info.st_dev, info.st_ino, info.st_size,
                                 info.st_mtime_ns, info.st_ctime_ns))
    return tuple(revision)


@contextmanager
def get_db(term: str = "fall"):
    config = TERM_DBS.get(term)
    if config is None:
        raise HTTPException(status_code=422, detail="Invalid course query parameter")
    conn = None
    try:
        conn = sqlite3.connect(_readonly_uri(config[0][1]), uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        for alias, path, _ in config[1:]:
            conn.execute(f"ATTACH DATABASE ? AS {alias}", (_readonly_uri(path),))
        yield conn
    finally:
        if conn is not None:
            conn.close()


@contextmanager
def get_reviews_db():
    conn = None
    try:
        conn = sqlite3.connect(_readonly_uri(REVIEWS_DB), uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        if conn is not None:
            conn.close()


def _initialize_wal(conn, database_path: Path) -> None:
    # 必须读完 PRAGMA 结果，等待文件锁时不能持有 SQLite 读锁。
    if conn.execute("PRAGMA journal_mode").fetchall()[0][0] == "wal":
        return
    # WAL 切换需要排他锁；用独立空文件协调线程和 worker，不锁 SQLite 文件本身。
    # 保留文件以免等待者和新请求锁到不同 inode；关闭描述符即自动释放 flock。
    lock_path = Path(str(database_path.resolve()) + ".init.lock")
    with lock_path.open("a") as initialization_lock:
        fcntl.flock(initialization_lock.fileno(), fcntl.LOCK_EX)
        conn.execute("PRAGMA journal_mode = WAL").fetchall()


@contextmanager
def get_messages_db():
    conn = None
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        _initialize_wal(conn, MESSAGES_DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " posted_at INTEGER NOT NULL,"
            " content TEXT NOT NULL,"
            " ip_hash TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_ip_time"
            " ON messages(ip_hash, posted_at)"
        )
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            # 锁内复查，兼容两个 worker 同时首次访问旧留言库。
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN nickname TEXT NOT NULL"
                    " DEFAULT '路过的 PKUer'"
                )
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN parent_id INTEGER REFERENCES messages(id)"
                )
                conn.execute("CREATE INDEX idx_messages_parent ON messages(parent_id, id)")
                conn.execute("PRAGMA user_version = 1")
            conn.commit()
        if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
                conn.execute("ALTER TABLE messages ADD COLUMN course_key TEXT NOT NULL DEFAULT ''")
                conn.execute("CREATE INDEX idx_messages_course ON messages(course_key, parent_id, id)")
                conn.execute("PRAGMA user_version = 2")
            conn.commit()
        yield conn
    finally:
        if conn is not None:
            conn.close()


@contextmanager
def get_stats_db():
    conn = None
    try:
        conn = sqlite3.connect(STATS_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        _initialize_wal(conn, STATS_DB_PATH)
        # 每天每个访客一行；同一访客当天多次访问只累加 views，不新增行。
        conn.execute(
            "CREATE TABLE IF NOT EXISTS visit_days ("
            " day TEXT NOT NULL,"
            " ip_hash TEXT NOT NULL,"
            " views INTEGER NOT NULL DEFAULT 0,"
            " last_at INTEGER NOT NULL,"
            " PRIMARY KEY (day, ip_hash))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_visit_days_day ON visit_days(day)"
        )
        yield conn
    finally:
        if conn is not None:
            conn.close()


_ACCOUNTS_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    password_changed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS security_questions (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer_hash TEXT NOT NULL,
    PRIMARY KEY (user_id, position)
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS favorites (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fav_key TEXT NOT NULL,
    course_id TEXT NOT NULL,
    term TEXT NOT NULL,
    term_label TEXT NOT NULL,
    level TEXT NOT NULL,
    course_code TEXT NOT NULL,
    class_no TEXT NOT NULL,
    teacher TEXT NOT NULL,
    course_name TEXT NOT NULL,
    credits REAL,
    schedule TEXT,
    department TEXT,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, fav_key)
);
CREATE TABLE IF NOT EXISTS auth_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_events_lookup ON auth_events(kind, subject, at);
CREATE INDEX IF NOT EXISTS idx_auth_events_at ON auth_events(at);
"""

# schema v2：引入收藏夹与多对多映射。逐条 CREATE ... IF NOT EXISTS，供迁移在事务内执行。
_ACCOUNTS_SCHEMA_V2 = [
    """
    CREATE TABLE IF NOT EXISTS collections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        is_default INTEGER NOT NULL DEFAULT 0,
        position INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        UNIQUE(user_id, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_collections_user ON collections(user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_collections_one_default"
    " ON collections(user_id) WHERE is_default = 1",
    """
    CREATE TABLE IF NOT EXISTS favorite_collections (
        user_id INTEGER NOT NULL,
        fav_key TEXT NOT NULL,
        collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        added_at INTEGER NOT NULL,
        PRIMARY KEY (user_id, fav_key, collection_id),
        FOREIGN KEY (user_id, fav_key) REFERENCES favorites(user_id, fav_key) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fav_coll_collection ON favorite_collections(collection_id)",
]

# 为每个已有收藏的用户补出默认夹，并把其现有收藏归入默认夹；WHERE NOT EXISTS 保证可重试。
_BACKFILL_DEFAULT_COLLECTIONS = """
    INSERT INTO collections (user_id, name, is_default, position, created_at)
    SELECT DISTINCT f.user_id, :name, 1, 0, :now FROM favorites f
    WHERE NOT EXISTS (
        SELECT 1 FROM collections c WHERE c.user_id = f.user_id AND c.is_default = 1
    )
"""
_BACKFILL_DEFAULT_MEMBERSHIPS = """
    INSERT INTO favorite_collections (user_id, fav_key, collection_id, added_at)
    SELECT f.user_id, f.fav_key, c.id, :now FROM favorites f
    JOIN collections c ON c.user_id = f.user_id AND c.is_default = 1
    WHERE NOT EXISTS (
        SELECT 1 FROM favorite_collections fc
        WHERE fc.user_id = f.user_id AND fc.fav_key = f.fav_key AND fc.collection_id = c.id
    )
"""


def _migrate_accounts_db(conn) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            for statement in _ACCOUNTS_SCHEMA_V1.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("PRAGMA user_version = 1")
        conn.commit()
        version = 1
    if version < 2:
        # 需要回填，改用显式事务；锁内重查版本，避免多进程首连重复回填。
        now = int(time.time())
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            for statement in _ACCOUNTS_SCHEMA_V2:
                conn.execute(statement)
            conn.execute(_BACKFILL_DEFAULT_COLLECTIONS, {"now": now, "name": DEFAULT_COLLECTION_NAME})
            conn.execute(_BACKFILL_DEFAULT_MEMBERSHIPS, {"now": now})
            conn.execute("PRAGMA user_version = 2")
        conn.commit()
        version = 2
    if version < 3:
        # 保留与已有开发版 v3 兼容的列；当前发布不改变收藏落点行为。
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] < 3:
            conn.execute("ALTER TABLE users ADD COLUMN last_collection_id INTEGER")
            conn.execute("PRAGMA user_version = 3")
        conn.commit()
        version = 3
    if version < 4:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] < 4:
            conn.execute(
                "ALTER TABLE users ADD COLUMN nickname TEXT NOT NULL DEFAULT '路过的 PKUer'"
            )
            conn.execute("PRAGMA user_version = 4")
        conn.commit()
        version = 4
    if version < 5:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] < 5:
            conn.execute("""CREATE TABLE IF NOT EXISTS timetable_courses (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                course_key TEXT NOT NULL,
                snapshot TEXT NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY(user_id, course_key)
            )""")
            conn.execute("PRAGMA user_version = 5")
        conn.commit()
        version = 5
    if version < 6:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] < 6:
            conn.execute("ALTER TABLE timetable_courses ADD COLUMN customization TEXT NOT NULL DEFAULT '{}'")
            conn.execute("ALTER TABLE timetable_courses ADD COLUMN is_custom INTEGER NOT NULL DEFAULT 0")
            conn.execute("PRAGMA user_version = 6")
        conn.commit()


@contextmanager
def get_accounts_db():
    conn = None
    try:
        conn = sqlite3.connect(ACCOUNTS_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        _initialize_wal(conn, ACCOUNTS_DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")
        _migrate_accounts_db(conn)
        yield conn
    finally:
        if conn is not None:
            conn.close()


def check_database_health() -> dict:
    databases = []
    for term, entries in TERM_DBS.items():
        for alias, path, prefix in entries:
            conn = sqlite3.connect(_readonly_uri(path), uri=True)
            try:
                tables = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                basic = conn.execute("SELECT COUNT(*) FROM basic_info").fetchone()[0]
                detail = conn.execute("SELECT COUNT(*) FROM detail_info").fetchone()[0]
                relations_match = conn.execute(
                    """
                    SELECT
                        NOT EXISTS (
                            SELECT id FROM basic_info
                            EXCEPT
                            SELECT course_id FROM detail_info
                        )
                        AND NOT EXISTS (
                            SELECT course_id FROM detail_info
                            EXCEPT
                            SELECT id FROM basic_info
                        )
                        AND (SELECT COUNT(*) FROM basic_info)
                            = (SELECT COUNT(DISTINCT id) FROM basic_info)
                        AND (SELECT COUNT(*) FROM detail_info)
                            = (SELECT COUNT(DISTINCT course_id) FROM detail_info)
                    """
                ).fetchone()[0]
                foreign_key_violation = conn.execute(
                    "PRAGMA foreign_key_check"
                ).fetchone()
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                conn.close()
            if (
                not {"basic_info", "detail_info", "translations"}.issubset(tables)
                or basic != detail
                or not relations_match
                or foreign_key_violation is not None
                or integrity != "ok"
            ):
                raise RuntimeError(f"Unhealthy database: {path.name}")
            databases.append(
                {
                    "term": term,
                    "level": alias,
                    "prefix": prefix,
                    "file": path.name,
                    "integrity": integrity,
                    "basic": basic,
                    "detail": detail,
                }
            )

    conn = sqlite3.connect(_readonly_uri(REVIEWS_DB), uri=True)
    try:
        review_tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        review_threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        review_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        review_snapshot_replies = conn.execute(
            "SELECT COUNT(*) FROM thread_replies"
        ).fetchone()[0]
        review_posts = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE kind='post'"
        ).fetchone()[0]
        review_highlights = conn.execute(
            "SELECT COUNT(*) FROM entry_highlights"
        ).fetchone()[0]
        review_aliases = dict(
            conn.execute(
                "SELECT entity_type, COUNT(DISTINCT normalized_alias) "
                "FROM entity_aliases GROUP BY entity_type"
            )
        )
        invalid_review_highlight = False
        previous_highlight_key = None
        previous_highlight_end = 0
        for entry_key, content, start, end in conn.execute(
            """
            SELECT h.entry_key, e.content, h.start_offset, h.end_offset
            FROM entry_highlights h
            JOIN entries e ON e.entry_key=h.entry_key
            ORDER BY h.entry_key, h.start_offset, h.end_offset
            """
        ):
            if entry_key != previous_highlight_key:
                previous_highlight_key = entry_key
                previous_highlight_end = 0
            if (
                start < previous_highlight_end
                or start < 0
                or end <= start
                or end > len(content)
            ):
                invalid_review_highlight = True
                break
            previous_highlight_end = end
        review_metadata = dict(conn.execute("SELECT key, value FROM metadata"))
        review_foreign_key_violation = conn.execute(
            "PRAGMA foreign_key_check"
        ).fetchone()
        review_integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    required_review_tables = {
        "metadata", "threads", "entries", "thread_replies", "thread_courses",
        "entry_courses", "course_catalog", "entity_aliases", "entry_highlights",
    }
    try:
        review_metadata_matches = (
            int(review_metadata.get("matched_threads", -1)) == review_threads
            and int(review_metadata.get("matched_entries", -1)) == review_entries
            and int(review_metadata.get("snapshot_replies", -1))
            == review_snapshot_replies
            and int(review_metadata.get("course_highlights", -1))
            + int(review_metadata.get("teacher_highlights", -1)) == review_highlights
            and int(review_metadata.get("course_aliases", -1))
            == review_aliases.get("course", 0)
            and int(review_metadata.get("teacher_aliases", -1))
            == review_aliases.get("teacher", 0)
            and int(review_metadata.get("course_alias_highlights", -1))
            <= int(review_metadata.get("course_highlights", -1))
            and int(review_metadata.get("teacher_alias_highlights", -1))
            <= int(review_metadata.get("teacher_highlights", -1))
            and review_metadata.get("highlight_version") == "3"
        )
    except (TypeError, ValueError):
        review_metadata_matches = False
    if (
        not required_review_tables.issubset(review_tables)
        or review_threads != review_posts
        or review_entries < review_threads
        or not review_metadata_matches
        or invalid_review_highlight
        or review_foreign_key_violation is not None
        or review_integrity != "ok"
    ):
        raise RuntimeError(f"Unhealthy database: {REVIEWS_DB.name}")

    reviews = {
        "file": REVIEWS_DB.name,
        "integrity": review_integrity,
        "threads": review_threads,
        "entries": review_entries,
        "snapshot_replies": review_snapshot_replies,
        "highlights": review_highlights,
        "snapshot_date": review_metadata.get("snapshot_date", ""),
    }
    return {"status": "ok", "databases": databases, "reviews": reviews}


def get_cached_database_health() -> dict:
    global _health_cache_payload, _health_cache_checked_at

    now = time.monotonic()
    with _health_cache_lock:
        if (
            _health_cache_payload is not None
            and _health_cache_checked_at is not None
            and now - _health_cache_checked_at < HEALTH_CACHE_TTL_SECONDS
        ):
            return _health_cache_payload

        payload = check_database_health()
        _health_cache_payload = payload
        _health_cache_checked_at = now
        return payload


# ---- shared SELECT fragments --------------------------------------------------

# Common basic columns plus course_type. Undergrad has it natively; graduate
# synthesises '研究生课'. pnp only exists on undergrad.
#
# Spring UG and summer UG share the exact same schema and live in `main` when
# their term is active — only the id prefix differs ('u' vs 's'), so both list
# SELECTs come from this one template.
UG_DETAIL_COLUMNS = (
    "english_name", "prerequisites", "intro_cn", "intro_en", "grading",
    "ge_series", "language", "textbook", "reference_book", "syllabus",
    "evaluation",
)
SPRING_GR_DETAIL_COLUMNS = (
    "english_name", "weekly_hours", "total_hours", "term", "audience",
    "reference_book", "intro", "extra_notes",
)
FALL_GR_DETAIL_COLUMNS = (*SPRING_GR_DETAIL_COLUMNS, "syllabus")
UG_VISIBLE_BASIC_COLUMNS = (
    "course_name", "credits", "department", "major", "grade", "schedule",
    "classroom", "enrollment", "pnp", "notes",
)
GR_VISIBLE_BASIC_COLUMNS = tuple(
    column for column in UG_VISIBLE_BASIC_COLUMNS if column != "pnp"
)


def _nonblank_score_sql(alias: str, columns: tuple[str, ...]) -> list[str]:
    return [
        f"CASE WHEN {alias}.{column} IS NOT NULL "
        f"AND TRIM(CAST({alias}.{column} AS TEXT)) != '' THEN 1 ELSE 0 END"
        for column in columns
    ]


def _completeness_score_sql(
    basic_columns: tuple[str, ...],
    detail_columns: tuple[str, ...],
) -> str:
    terms = [
        *_nonblank_score_sql("b", basic_columns),
        *_nonblank_score_sql("d", detail_columns),
    ]
    return " + ".join(terms)


_UG_LIST_SELECT = """
    SELECT
        '{prefix}' || b.id     AS id,
        b.course_type          AS course_type,
        b.course_code          AS course_code,
        b.class_no             AS class_no,
        b.course_name          AS course_name,
        b.category             AS category,
        b.credits              AS credits,
        b.teacher              AS teacher,
        b.department           AS department,
        b.major                AS major,
        b.grade                AS grade,
        b.schedule             AS schedule,
        b.classroom            AS classroom,
        b.weekdays             AS weekdays,
        b.first_period         AS first_period,
        b.enrollment           AS enrollment,
        b.pnp                  AS pnp,
        b.notes                AS notes,
        d.english_name         AS english_name,
        d.grading              AS grading,
        d.language             AS language,
        ''                     AS audience,
        '{prefix}'             AS _level,
        ({completeness_score}) AS completeness_score
    FROM basic_info b
    LEFT JOIN detail_info d ON d.course_id = b.id
"""

_UG_COMPLETENESS_SCORE_SQL = _completeness_score_sql(
    UG_VISIBLE_BASIC_COLUMNS,
    UG_DETAIL_COLUMNS,
)
LIST_SELECT_UG = _UG_LIST_SELECT.format(
    prefix="u",
    completeness_score=_UG_COMPLETENESS_SCORE_SQL,
)
LIST_SELECT_SUMMER = _UG_LIST_SELECT.format(
    prefix="s",
    completeness_score=_UG_COMPLETENESS_SCORE_SQL,
)
LIST_SELECT_FALL_UG = _UG_LIST_SELECT.format(
    prefix="a",
    completeness_score=_UG_COMPLETENESS_SCORE_SQL,
)

_GR_LIST_SELECT = """
    SELECT
        '{prefix}' || b.id     AS id,
        '研究生课'             AS course_type,
        b.course_code          AS course_code,
        b.class_no             AS class_no,
        b.course_name          AS course_name,
        b.category             AS category,
        b.credits              AS credits,
        b.teacher              AS teacher,
        b.department           AS department,
        b.major                AS major,
        b.grade                AS grade,
        b.schedule             AS schedule,
        b.classroom            AS classroom,
        b.weekdays             AS weekdays,
        b.first_period         AS first_period,
        b.enrollment           AS enrollment,
        ''                     AS pnp,
        b.notes                AS notes,
        d.english_name         AS english_name,
        ''                     AS grading,
        ''                     AS language,
        d.audience             AS audience,
        '{prefix}'             AS _level,
        ({completeness_score}) AS completeness_score
    FROM {ns}.basic_info b
    LEFT JOIN {ns}.detail_info d ON d.course_id = b.id
"""

LIST_SELECT_GR = _GR_LIST_SELECT.format(
    prefix="g",
    ns="gr",
    completeness_score=_completeness_score_sql(
        GR_VISIBLE_BASIC_COLUMNS,
        SPRING_GR_DETAIL_COLUMNS,
    ),
)
LIST_SELECT_FALL_GR = _GR_LIST_SELECT.format(
    prefix="r",
    ns="gr",
    completeness_score=_completeness_score_sql(
        GR_VISIBLE_BASIC_COLUMNS,
        FALL_GR_DETAIL_COLUMNS,
    ),
)

# Pre-built UNION expressions for each term.
TERM_UNION_SQL = {
    "spring": f"({LIST_SELECT_UG} UNION ALL {LIST_SELECT_GR})",
    "summer": f"({LIST_SELECT_SUMMER})",
    "fall": f"({LIST_SELECT_FALL_UG} UNION ALL {LIST_SELECT_FALL_GR})",
}

TERM_LIST_SELECTS = {
    "spring": (("main", LIST_SELECT_UG), ("gr", LIST_SELECT_GR)),
    "summer": (("main", LIST_SELECT_SUMMER),),
    "fall": (("main", LIST_SELECT_FALL_UG), ("gr", LIST_SELECT_FALL_GR)),
}


# ---- filters ------------------------------------------------------------------


def _period_option_sort_key(bounds: tuple[int, int]) -> tuple[bool, int, int]:
    """Two-period ranges such as 1-2 and 3-4 come first; every other range follows by start and end."""
    start, end = bounds
    return (end - start != 1, start, end)


def _period_options(schedules) -> list[str]:
    """Distinct class-period ranges ("N-M") found in schedule text.

    Two-period ranges (1-2, 3-4, 10-11 ...) are listed first because they cover most classes;
    the remaining ranges follow. Both groups are sorted by start then end. Options come from the
    same "周X N~M节" shape the `period` filter matches, so every option returned here selects at
    least one course of the same term.
    """
    ranges = set()
    for schedule in schedules:
        for start, end in SCHEDULE_PERIOD_RE.findall(schedule or ""):
            start, end = int(start), int(end)
            if 1 <= start <= end <= 14:
                ranges.add((start, end))
    return [f"{start}-{end}" for start, end in sorted(ranges, key=_period_option_sort_key)]


def _period_bounds(period: object) -> tuple[int, int] | None:
    """Validate a `period` filter such as "10-11"; return (start, end), or None when empty."""
    if not isinstance(period, str):
        raise HTTPException(status_code=422, detail="Invalid period")
    if not period:
        return None
    if not PERIOD_RANGE_RE.match(period):
        raise HTTPException(status_code=422, detail="Invalid period")
    start, end = (int(part) for part in period.split("-"))
    if start > end:
        raise HTTPException(status_code=422, detail="Invalid period")
    return start, end


@lru_cache(maxsize=6)
def _filter_options(term, revision):
    # 只缓存公开课程选项；数据库或 WAL 版本变化后重新读取，不保留连接或可变列表。
    with get_db(term) as conn:
        c = conn.cursor()

        def col(sql: str) -> list:
            return [r[0] for r in c.execute(sql)]

        has_graduate_db = any(alias == "gr" for alias, _, _ in TERM_DBS[term])
        if has_graduate_db:
            course_types = col(
                """SELECT DISTINCT course_type FROM basic_info
                   WHERE course_type != ''
                   UNION
                   SELECT '研究生课'
                   ORDER BY 1"""
            )
            categories = col(
                """SELECT DISTINCT category FROM basic_info WHERE category != ''
                   UNION
                   SELECT DISTINCT category FROM gr.basic_info WHERE category != ''
                   ORDER BY 1"""
            )
            departments = col(
                """SELECT DISTINCT department FROM basic_info WHERE department != ''
                   UNION
                   SELECT DISTINCT department FROM gr.basic_info WHERE department != ''
                   ORDER BY 1"""
            )
            credits = col(
                """SELECT DISTINCT credits FROM basic_info
                   UNION
                   SELECT DISTINCT credits FROM gr.basic_info
                   ORDER BY 1"""
            )
        else:  # summer — single DB
            course_types = col(
                """SELECT DISTINCT course_type FROM basic_info
                   WHERE course_type != '' ORDER BY 1"""
            )
            categories = col(
                """SELECT DISTINCT category FROM basic_info
                   WHERE category != '' ORDER BY 1"""
            )
            departments = col(
                """SELECT DISTINCT department FROM basic_info
                   WHERE department != '' ORDER BY 1"""
            )
            credits = col("SELECT DISTINCT credits FROM basic_info ORDER BY 1")

        # grading only exists on undergrad detail_info, which is `main` in both terms.
        gradings = col(
            """SELECT DISTINCT grading FROM detail_info
               WHERE grading != '' ORDER BY grading"""
        )

        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

        if has_graduate_db:
            schedules = col(
                """SELECT schedule FROM basic_info WHERE schedule != ''
                   UNION ALL
                   SELECT schedule FROM gr.basic_info WHERE schedule != ''"""
            )
        else:
            schedules = col("SELECT schedule FROM basic_info WHERE schedule != ''")
        periods = _period_options(schedules)

    payload = {
        "course_types": course_types,
        "categories": categories,
        "departments": departments,
        "credits": credits,
        "gradings": gradings,
        "weekdays": weekdays,
        "periods": periods,
    }
    return tuple((key, tuple(values)) for key, values in payload.items())


@app.get("/api/filters")
def get_filters(
    term: str = Query(
        "fall",
        description="spring | summer | fall",
        pattern=r"^(?:spring|summer|fall)$",
    ),
):
    if term not in VALID_TERMS:
        raise HTTPException(status_code=422, detail="Invalid course query parameter")
    revision = _database_revision(path for _, path, _ in TERM_DBS[term])
    payload = {key: list(values) for key, values in _filter_options(term, revision)}
    # Filter universes only change when DBs are rebuilt. 1 hour browser cache
    # keeps cold-load fast without making a redeploy require a hard refresh.
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=3600"})


# ---- list ---------------------------------------------------------------------


def _like(s: str) -> str:
    """Escape LIKE wildcards so `100%` searches literal '%', not "any chars"."""
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _build_source_where(filters: dict[str, object]) -> tuple[str, list[object]]:
    """Build the source-row predicate used only to identify matching groups."""
    conds = []
    params: list[object] = []
    q = str(filters.get("q") or "")
    if q:
        like = _like(q)
        conds.append(
            "(s.course_name LIKE ? ESCAPE '\\' "
            "OR s.display_course_name LIKE ? ESCAPE '\\' "
            "OR s.english_name LIKE ? ESCAPE '\\' "
            "OR s.teacher LIKE ? ESCAPE '\\' "
            "OR s.classroom LIKE ? ESCAPE '\\' "
            "OR s.display_classroom LIKE ? ESCAPE '\\' "
            "OR s.course_code LIKE ? ESCAPE '\\')"
        )
        params.extend([like] * 7)
    classroom = str(filters.get("classroom") or "")
    if classroom:
        like = _like(classroom)
        conds.append(
            "(s.classroom LIKE ? ESCAPE '\\' "
            "OR s.display_classroom LIKE ? ESCAPE '\\')"
        )
        params.extend([like, like])
    type_ = str(filters.get("type") or "")
    if type_:
        conds.append("s.course_type = ?")
        params.append(type_)
    category = str(filters.get("category") or "")
    if category:
        conds.append("s.category = ?")
        params.append(category)
    credits = filters.get("credits")
    if credits not in (None, ""):
        conds.append("s.credits = ?")
        params.append(credits)
    department = str(filters.get("department") or "")
    if department:
        conds.append("s.department = ?")
        params.append(department)
    weekday = str(filters.get("weekday") or "")
    if weekday:
        conds.append("s.weekdays LIKE ?")
        params.append(f"%{weekday}%")
    period = filters.get("period")
    if period:
        start, end = period
        # Slots read "周三10~11节". Anchoring on the weekday token keeps "1~12节" from matching
        # "11~12节"; with a weekday filter the same slot has to carry both.
        weekday_token = weekday if weekday else "周_"
        conds.append("s.schedule LIKE ?")
        params.append(f"%{weekday_token}{start}~{end}节%")
    grading = str(filters.get("grading") or "")
    if grading:
        conds.append("s.grading = ?")
        params.append(grading)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


def _group_key_sql(alias: str) -> str:
    whitespace = "CHAR(9) || CHAR(10) || CHAR(11) || CHAR(12) || CHAR(13) || ' '"
    return (
        f"{alias}._level || CHAR(31) || {alias}.course_code || CHAR(31) || "
        f"{alias}.class_no || CHAR(31) || "
        f"CASE WHEN TRIM(COALESCE({alias}.teacher, ''), {whitespace}) = '' "
        f"THEN {alias}.id ELSE {alias}.teacher END"
    )


def _translated_source_select(base_select: str, ns: str, lang: str) -> tuple[str, list[object]]:
    if lang == "zh":
        display_columns = """
            t.course_name AS display_course_name,
            t.classroom AS display_classroom,
            t.notes AS display_notes
        """
        joins = ""
        params: list[object] = []
    else:
        display_columns = """
            COALESCE(NULLIF(TRIM(name_tr.text), ''), t.course_name) AS display_course_name,
            COALESCE(NULLIF(TRIM(room_tr.text), ''), t.classroom) AS display_classroom,
            COALESCE(NULLIF(TRIM(notes_tr.text), ''), t.notes) AS display_notes
        """
        joins = f"""
            LEFT JOIN {ns}.translations AS name_tr
              ON name_tr.course_id = CAST(SUBSTR(t.id, 2) AS INTEGER)
             AND name_tr.field = 'course_name' AND name_tr.lang = ?
            LEFT JOIN {ns}.translations AS room_tr
              ON room_tr.course_id = CAST(SUBSTR(t.id, 2) AS INTEGER)
             AND room_tr.field = 'classroom' AND room_tr.lang = ?
            LEFT JOIN {ns}.translations AS notes_tr
              ON notes_tr.course_id = CAST(SUBSTR(t.id, 2) AS INTEGER)
             AND notes_tr.field = 'notes' AND notes_tr.lang = ?
        """
        params = [lang, lang, lang]

    sql = f"""
        SELECT t.*,
               {display_columns},
               {_group_key_sql("t")} AS group_key
        FROM ({base_select}) AS t
        {joins}
    """
    return sql, params


def _build_course_query(
    term: str,
    lang: str,
    filters: dict[str, object],
) -> tuple[str, list[object], str]:
    """Return source-row SQL, ordered parameters, and matching-group predicate."""
    if lang == "zh":
        source_sql, source_params = _translated_source_select(TERM_UNION_SQL[term], "main", lang)
    else:
        selects = []
        source_params = []
        for ns, base_select in TERM_LIST_SELECTS[term]:
            select_sql, select_params = _translated_source_select(base_select, ns, lang)
            selects.append(select_sql)
            source_params.extend(select_params)
        source_sql = " UNION ALL ".join(selects)

    matching_where, filter_params = _build_source_where(filters)
    return source_sql, [*source_params, *filter_params], matching_where


_FALLBACK_TEXT_COLUMNS = (
    ("display_course_name", "course_name"),
    ("course_name", "original_course_name"),
    ("department", "department"),
    ("major", "major"),
    ("grade", "grade"),
    ("schedule", "schedule"),
    ("display_classroom", "classroom"),
    ("weekdays", "weekdays"),
    ("enrollment", "enrollment"),
    ("pnp", "pnp"),
    ("display_notes", "notes"),
    ("english_name", "english_name"),
    ("grading", "grading"),
    ("language", "language"),
    ("audience", "audience"),
)
_FALLBACK_VALUE_COLUMNS = ("credits", "first_period")
_CARD_FALLBACK_TEXT_COLUMNS = (
    "display_course_name", "department", "major", "grade", "schedule",
    "display_classroom", "enrollment", "pnp", "display_notes", "english_name",
    "language", "audience",
)
_CARD_FALLBACK_VALUE_COLUMNS = ("credits",)


def _fallback_fill_count_sql(representative: str, candidate: str) -> str:
    text_terms = [
        f"CASE WHEN TRIM(COALESCE({representative}.{column}, '')) = '' "
        f"AND TRIM(COALESCE({candidate}.{column}, '')) != '' THEN 1 ELSE 0 END"
        for column in _CARD_FALLBACK_TEXT_COLUMNS
    ]
    value_terms = [
        f"CASE WHEN {representative}.{column} IS NULL "
        f"AND {candidate}.{column} IS NOT NULL THEN 1 ELSE 0 END"
        for column in _CARD_FALLBACK_VALUE_COLUMNS
    ]
    return " + ".join((*text_terms, *value_terms))


def _coherent_text(column: str, alias: str) -> str:
    return (
        f"CASE WHEN TRIM(COALESCE(representative.{column}, '')) != '' "
        f"THEN representative.{column} ELSE fallback.{column} END AS {alias}"
    )


def _coherent_value(column: str) -> str:
    return (
        f"CASE WHEN representative.{column} IS NOT NULL "
        f"THEN representative.{column} ELSE fallback.{column} END AS {column}"
    )


def _matching_group_ctes(source_sql: str, matching_where: str) -> str:
    return f"""
        WITH source AS (
            {source_sql}
        ), matching_groups AS (
            SELECT DISTINCT s.group_key
            FROM source AS s
            {matching_where}
        )
    """


def _count_course_sql(source_sql: str, matching_where: str) -> str:
    matching_group_ctes = _matching_group_ctes(source_sql, matching_where)
    return f"{matching_group_ctes} SELECT COUNT(*) FROM matching_groups"


def _grouped_course_ctes(source_sql: str, matching_where: str) -> str:
    text_columns = tuple(
        _coherent_text(column, alias)
        for column, alias in _FALLBACK_TEXT_COLUMNS
    )
    value_columns = tuple(
        _coherent_value(column) for column in _FALLBACK_VALUE_COLUMNS
    )
    scalar_columns = ",\n".join((*text_columns, *value_columns))
    fill_count = _fallback_fill_count_sql("representative", "candidate")

    matching_group_ctes = _matching_group_ctes(source_sql, matching_where)
    # 无筛选时所有非空组都已命中，省去 DISTINCT 与回联；空键沿用 JOIN 的排除语义。
    group_match = (
        "JOIN matching_groups USING (group_key)" if matching_where
        else "WHERE s.group_key IS NOT NULL"
    )
    return f"""
        {matching_group_ctes}, ranked AS (
            SELECT s.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.group_key
                       ORDER BY s.completeness_score DESC,
                                CAST(SUBSTR(s.id, 2) AS INTEGER),
                                s.id
                   ) AS representative_rank
            FROM source AS s
            {group_match}
        ), fallback_candidates AS (
            SELECT candidate.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY candidate.group_key
                       ORDER BY ({fill_count}) DESC,
                                candidate.completeness_score DESC,
                                CAST(SUBSTR(candidate.id, 2) AS INTEGER),
                                candidate.id
                   ) AS fallback_rank
            FROM ranked AS candidate
            JOIN ranked AS representative
              ON representative.group_key = candidate.group_key
             AND representative.representative_rank = 1
            WHERE candidate.id != representative.id
              AND ({fill_count}) > 0
        ), badge_values AS (
            SELECT DISTINCT group_key, 'course_type' AS badge_kind, course_type AS badge_value
            FROM ranked
            WHERE TRIM(COALESCE(course_type, '')) != ''
            UNION ALL
            SELECT DISTINCT group_key, 'category' AS badge_kind, category AS badge_value
            FROM ranked
            WHERE TRIM(COALESCE(category, '')) != ''
        ), badge_sequences AS (
            SELECT group_key, badge_kind,
                   GROUP_CONCAT(badge_value) OVER (
                       PARTITION BY group_key, badge_kind
                       ORDER BY badge_value COLLATE BINARY
                       ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                   ) AS badge_list
            FROM badge_values
        ), badges AS (
            SELECT group_key,
                   MAX(CASE WHEN badge_kind = 'course_type' THEN badge_list END) AS course_type,
                   MAX(CASE WHEN badge_kind = 'category' THEN badge_list END) AS category
            FROM badge_sequences
            GROUP BY group_key
        ), grouped AS (
            SELECT representative.id AS id,
                   fallback.id AS fallback_id,
                   badges.course_type AS course_type,
                   badges.category AS category,
                   representative.course_code AS course_code,
                   representative.class_no AS class_no,
                   representative.teacher AS teacher,
                   {scalar_columns}
            FROM ranked AS representative
            LEFT JOIN fallback_candidates AS fallback
              ON fallback.group_key = representative.group_key
             AND fallback.fallback_rank = 1
            LEFT JOIN badges USING (group_key)
            WHERE representative.representative_rank = 1
        )
    """


def _course_order_by(sort: str, term: str, random_seed: int) -> str:
    if sort == "random":
        seed = int(random_seed)
        mul = ((seed * 1664525) % 999983) or 7
        add = (seed * 1013904223) % 999983
        return (
            f"((CAST(SUBSTR(id, 2) AS INTEGER) * {mul} "
            f"+ CASE SUBSTR(id, 1, 1) "
            f"WHEN 'g' THEN 333331 WHEN 's' THEN 666661 "
            f"WHEN 'a' THEN 111113 WHEN 'r' THEN 444449 ELSE 0 END "
            f"+ {add}) % 999983), id"
        )

    name_asc = "(course_name = '' OR course_name IS NULL), course_name COLLATE NOCASE, id"
    name_desc = "(course_name = '' OR course_name IS NULL), course_name COLLATE NOCASE DESC, id"
    sort_map = {
        "name_asc": name_asc,
        "pinyin": name_asc,
        "name_desc": name_desc,
        "pinyin_desc": name_desc,
        "credits_asc": f"(credits IS NULL), credits ASC, {name_asc}",
        "credits_desc": f"(credits IS NULL), credits DESC, {name_asc}",
        "time_asc": f"(first_period IS NULL), first_period, {name_asc}",
    }
    if sort in sort_map:
        return sort_map[sort]

    sociology_priority = ""
    if term == "summer":
        sociology_priority = (
            "(CASE WHEN department = '社会学系' "
            "OR department = '中国社会科学调查中心' "
            "OR original_course_name LIKE '%社会学%' THEN 0 ELSE 1 END), "
        )
    return f"{sociology_priority}{name_asc}"


def _validate_list_params(
    term: str,
    lang: str,
    weekday: str,
    sort: str,
    credits: str,
    page: int,
    page_size: int,
) -> float | None:
    try:
        invalid_choice = (
            term not in VALID_TERMS
            or lang not in VALID_LANGS
            or weekday not in VALID_WEEKDAYS
            or sort not in VALID_SORTS
        )
    except TypeError:
        invalid_choice = True
    if (
        invalid_choice
        or isinstance(page, bool)
        or not isinstance(page, int)
        or not 1 <= page <= 10000
        or isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 200
    ):
        raise HTTPException(status_code=422, detail="Invalid course query parameter")
    if not isinstance(credits, str):
        raise HTTPException(status_code=422, detail="Invalid credits")
    if not credits:
        return None
    try:
        value = float(credits)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid credits") from exc
    if not math.isfinite(value):
        raise HTTPException(status_code=422, detail="Invalid credits")
    return value


@lru_cache(maxsize=32)
def _course_page_rows(term, count_sql, list_sql, params, page_size, offset, revision):
    # 缓存只读源行，响应列表每次重新生成，课程留言状态仍实时查询。
    with get_db(term) as conn:
        total = conn.execute(count_sql, params).fetchone()[0]
        rows = conn.execute(list_sql, (*params, page_size, offset)).fetchall()
    return total, tuple(rows)


@app.get("/api/courses")
def list_courses(
    q: str = Query("", description="Search query (course name / teacher / classroom / course code / english name)"),
    type: str = Query("", description="Course type filter"),
    category: str = Query("", description="Category filter"),
    credits: str = Query("", description="Credits filter"),
    department: str = Query("", description="Department filter"),
    weekday: str = Query("", description="Weekday filter", pattern=r"^(?:|周[一二三四五六日])$"),
    period: str = Query(
        "",
        description="Class-period filter such as 3-4 (a session occupying periods 3 through 4); with weekday, the same session must match both",
        pattern=r"^(?:|(?:[1-9]|1[0-4])-(?:[1-9]|1[0-4]))$",
    ),
    grading: str = Query("", description="Grading filter"),
    classroom: str = Query("", description="Classroom filter (LIKE, classroom column only)"),
    sort: str = Query(
        "",
        description="Sort: name_asc | name_desc | pinyin | pinyin_desc | credits_asc | credits_desc | time_asc | random",
        pattern=r"^(?:|name_asc|name_desc|pinyin|pinyin_desc|credits_asc|credits_desc|time_asc|random)$",
    ),
    random_seed: int = Query(0, description="Seed used by sort=random; same seed → same order"),
    lang: str = Query("zh", description="Display language", pattern=r"^(?:zh|en|ja|ko|fr|de|es|ru)$"),
    term: str = Query("fall", description="spring | summer | fall", pattern=r"^(?:spring|summer|fall)$"),
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(50, ge=1, le=200),
    response: Response = None,
):
    credits_value = _validate_list_params(term, lang, weekday, sort, credits, page, page_size)
    if any(not _valid_text(value) for value in (q, type, category, department, grading, classroom)):
        raise HTTPException(status_code=422, detail="Invalid course query parameter")
    # Translation is disabled site-wide; accept old lang links but use source fields.
    lang = "zh"
    period_bounds = _period_bounds(period)

    filters = {
        "q": q,
        "type": type,
        "category": category,
        "credits": credits_value,
        "department": department,
        "weekday": weekday,
        "period": period_bounds,
        "grading": grading,
        "classroom": classroom,
    }
    source_sql, params, matching_where = _build_course_query(term, lang, filters)
    ctes = _grouped_course_ctes(source_sql, matching_where)
    order_by = _course_order_by(sort, term, random_seed)
    offset = (page - 1) * page_size

    count_sql = _count_course_sql(source_sql, matching_where)
    list_sql = f"{ctes} SELECT * FROM grouped ORDER BY {order_by} LIMIT ? OFFSET ?"

    revision = _database_revision(path for _, path, _ in TERM_DBS[term])
    total, rows = _course_page_rows(term, count_sql, list_sql, tuple(params), page_size, offset, revision)

    courses = []
    for r in rows:
        rid = r["id"]
        ct_raw = r["course_type"] or ""
        cat_raw = r["category"] or ""
        courses.append(
            {
                "id": rid,
                "course_type": [s for s in ct_raw.split(",") if s],
                "course_code": r["course_code"],
                "course_name": r["course_name"],
                "english_name": r["english_name"],
                "category": [s for s in cat_raw.split(",") if s],
                "credits": r["credits"],
                "teacher": r["teacher"],
                "class_no": r["class_no"],
                "department": r["department"],
                "schedule": r["schedule"],
                "classroom": r["classroom"],
                "enrollment": r["enrollment"],
                "pnp": r["pnp"],
                "notes": r["notes"],
                "major": r["major"],
                "grade": r["grade"],
                "language": r["language"],
                "audience": r["audience"],
            }
        )

    _annotate_course_corrections(courses)
    if response is not None:
        response.headers["Cache-Control"] = "no-store"
    return {"total": total, "page": page, "page_size": page_size, "courses": courses}


# ---- detail -------------------------------------------------------------------


# id prefix → term whose DB set contains that level's courses.
_PREFIX_TERM = {"u": "spring", "g": "spring", "s": "summer", "a": "fall", "r": "fall"}


def _parse_id(course_id: str):
    """Return (term, level, local_id) or (None, None, None) on parse failure.

    level is the single-char id prefix ('u' / 'g' / 's' / 'a' / 'r'); term tells get_db()
    which DBs to open.
    """
    if not isinstance(course_id, str) or not COURSE_ID_RE.fullmatch(course_id):
        return None, None, None
    prefix = course_id[0]
    term = _PREFIX_TERM.get(prefix)
    if term is None:
        return None, None, None
    local_id = int(course_id[1:])
    if local_id > 2**63 - 1:
        return None, None, None
    return term, prefix, local_id


# Fields the translations table can override per (course_id, field, lang).
# Only includes fields the frontend actually renders — keep in sync with the
# detail SELECTs and modal rendering in index.html.
TRANSLATABLE_FIELDS = (
    "course_name", "notes", "pnp", "classroom", "major",
    "prerequisites", "ge_series", "audience", "term",
    "syllabus", "evaluation",
    "intro_cn", "extra_notes", "textbook", "reference_book",
)


def _apply_translations(cur, ns: str, local_id: int, lang: str, out: dict):
    """Replace translatable fields in `out` with values from translations table."""
    if lang == "zh":
        return
    rows = cur.execute(
        f"SELECT field, text FROM {ns}.translations "
        f"WHERE course_id=? AND lang=?",
        (local_id, lang),
    ).fetchall()
    for field, text in rows:
        if field in TRANSLATABLE_FIELDS and isinstance(text, str) and text.strip():
            out[field] = text


# Shared SELECT for "UG-shape" detail rows (spring undergrad + summer undergrad).
# Only selects fields the frontend renders — see TRANSLATABLE_FIELDS and the
# modal renderer in index.html.
_UG_DETAIL_SELECT = """
    SELECT '{prefix}' || b.id AS id, b.course_type, b.course_code, b.class_no,
           b.course_name, b.category, b.credits, b.teacher,
           b.department, b.major, b.grade,
           b.schedule, b.classroom,
           b.enrollment, b.pnp, b.notes,
           d.english_name, d.prerequisites, d.intro_cn, d.intro_en,
           d.grading, d.ge_series, d.language,
           d.textbook, d.reference_book,
           d.syllabus, d.evaluation
    FROM basic_info b
    LEFT JOIN detail_info d ON d.course_id = b.id
    WHERE b.id = ?
"""

_GR_DETAIL_SELECT = """
    SELECT '{prefix}' || b.id AS id, '研究生课' AS course_type,
           b.course_code, b.class_no, b.course_name, b.category,
           b.credits, b.teacher, b.department, b.major, b.grade,
           b.schedule, b.classroom,
           b.enrollment, '' AS pnp, b.notes,
           d.english_name,
           d.weekly_hours, d.total_hours, d.term, d.audience,
           '' AS textbook, d.reference_book,
           d.intro AS intro_cn, d.extra_notes,
           {syllabus_expr} AS syllabus
    FROM gr.basic_info b
    LEFT JOIN gr.detail_info d ON d.course_id = b.id
    WHERE b.id = ?
"""


@app.get("/api/courses/{course_id}")
def get_course_detail(
    course_id: str,
    lang: str = Query("zh", pattern=r"^(?:zh|en|ja|ko|fr|de|es|ru)$"),
):
    if lang not in VALID_LANGS:
        raise HTTPException(status_code=422, detail="Invalid course query parameter")
    # Preserve source text even for previously shared non-Chinese URLs.
    lang = "zh"
    term, level, local_id = _parse_id(course_id)
    if level is None:
        raise HTTPException(status_code=404, detail="Course not found")

    with get_db(term) as conn:
        cur = conn.cursor()
        if level in ("g", "r"):
            syllabus_expr = "d.syllabus" if level == "r" else "''"
            sql = _GR_DETAIL_SELECT.format(prefix=level, syllabus_expr=syllabus_expr)
        else:  # 'u' / 's' / 'a' share the UG shape; the prefix is the level itself
            sql = _UG_DETAIL_SELECT.format(prefix=level)
        row = cur.execute(sql, (local_id,)).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Course not found")

        out = dict(row)

        # Replace every translatable field with its translation when present.
        ns = "gr" if level in ("g", "r") else "main"
        _apply_translations(cur, ns, local_id, lang, out)

    # Ensure all keys exist for both levels so frontend gating is consistent.
    for k in (
        "english_name", "prerequisites", "intro_cn", "intro_en",
        "grading", "ge_series", "language",
        "textbook", "reference_book",
        "syllabus", "evaluation",
        "weekly_hours", "total_hours", "term", "audience", "extra_notes",
    ):
        out.setdefault(k, "")
    return out


# ---- Treehole course reviews -------------------------------------------------

def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_review_query(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").lower()
    return _REVIEW_SEARCH_STRIP_RE.sub("", normalized)


def _validate_review_query(q: str) -> str:
    if not _valid_text(q) or len(q) > REVIEW_QUERY_MAX_LENGTH:
        raise HTTPException(status_code=422, detail="Invalid review query parameter")
    return q.strip()


def _validate_review_pagination(page: int, page_size: int) -> None:
    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or not 1 <= page <= 10000
        or isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 100
    ):
        raise HTTPException(status_code=422, detail="Invalid review query parameter")


def _review_search_where(query: str):
    if not query:
        return "", []
    raw_pattern = f"%{_escape_like(query)}%"
    conditions = [
        "t.content LIKE ? ESCAPE '\\'",
        "EXISTS (SELECT 1 FROM entries se WHERE se.pid=t.pid "
        "AND se.content LIKE ? ESCAPE '\\')",
    ]
    params = [raw_pattern, raw_pattern]
    normalized = _normalize_review_query(query)
    if normalized:
        conditions.append(
            "EXISTS (SELECT 1 FROM thread_courses stc WHERE stc.pid=t.pid "
            "AND stc.search_name LIKE ? ESCAPE '\\')"
        )
        params.append(f"%{_escape_like(normalized)}%")
    return "WHERE " + " OR ".join(conditions), params


def _load_review_threads(conn, rows):
    if not rows:
        return []
    pids = [row["pid"] for row in rows]
    placeholders = ",".join("?" for _ in pids)
    courses_by_pid = {pid: [] for pid in pids}
    for course in conn.execute(
        f"SELECT pid, course_name FROM thread_courses "
        f"WHERE pid IN ({placeholders}) ORDER BY pid, course_name",
        pids,
    ):
        courses_by_pid[course["pid"]].append(course["course_name"])

    entries_by_pid = {pid: [] for pid in pids}
    entry_by_key = {}
    for entry in conn.execute(
        f"SELECT entry_key, pid, kind, cid, floor, posted_at, content "
        f"FROM entries WHERE pid IN ({placeholders}) "
        "ORDER BY pid, CASE kind WHEN 'post' THEN 0 ELSE 1 END, floor, cid",
        pids,
    ):
        item = {
            "kind": entry["kind"],
            "cid": entry["cid"],
            "floor": entry["floor"],
            "posted_at": entry["posted_at"],
            "content": entry["content"],
            "courses": [],
            "highlights": [],
        }
        entries_by_pid[entry["pid"]].append(item)
        entry_by_key[entry["entry_key"]] = item

    entry_keys = list(entry_by_key)
    entry_placeholders = ",".join("?" for _ in entry_keys)
    for course in conn.execute(
        f"SELECT entry_key, course_name FROM entry_courses "
        f"WHERE entry_key IN ({entry_placeholders}) ORDER BY entry_key, course_name",
        entry_keys,
    ):
        entry_by_key[course["entry_key"]]["courses"].append(course["course_name"])

    for highlight in conn.execute(
        f"SELECT entry_key, start_offset, end_offset, entity_type, match_kind "
        f"FROM entry_highlights WHERE entry_key IN ({entry_placeholders}) "
        "ORDER BY entry_key, start_offset, end_offset",
        entry_keys,
    ):
        entry_by_key[highlight["entry_key"]]["highlights"].append(
            {
                "start_offset": highlight["start_offset"],
                "end_offset": highlight["end_offset"],
                "entity_type": highlight["entity_type"],
                "match_kind": highlight["match_kind"],
            }
        )

    results = []
    for row in rows:
        entries = entries_by_pid[row["pid"]]
        post_entry = next(entry for entry in entries if entry["kind"] == "post")
        results.append({
            "pid": row["pid"],
            "source_month": row["source_month"],
            "posted_at": row["posted_at"],
            "content": row["content"],
            "highlights": post_entry["highlights"],
            "source_url": row["source_url"],
            "post_kind": row["post_kind"],
            "relevant_reply_count": row["relevant_reply_count"],
            "courses": courses_by_pid[row["pid"]],
            "entries": entries,
        })
    return results


@lru_cache(maxsize=32)
def _review_page(query, page, page_size, revision):
    where, params = _review_search_where(query)
    offset = (page - 1) * page_size
    with get_reviews_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM threads t {where}", params
        ).fetchone()[0]
        if query:
            rows = conn.execute(
                f"SELECT t.* FROM threads t {where} "
                "ORDER BY t.posted_at DESC, t.pid DESC LIMIT ? OFFSET ?",
                [*params, page_size, offset],
            ).fetchall()
        else:
            rows = conn.execute(
                "WITH featured AS ("
                f"  SELECT t.pid, {REVIEW_QUALITY_SCORE_SQL} AS quality"
                "  FROM threads t"
                "  WHERE t.posted_at >= ? AND t.posted_at < ?"
                "  ORDER BY quality DESC, t.pid DESC LIMIT ?"
                ") "
                "SELECT t.* FROM threads t "
                "LEFT JOIN featured f ON f.pid = t.pid "
                "ORDER BY (f.pid IS NULL), f.quality DESC, f.pid DESC, "
                "t.posted_at DESC, t.pid DESC LIMIT ? OFFSET ?",
                [*REVIEW_FEATURED_RANGE, REVIEW_FEATURED_COUNT, page_size, offset],
            ).fetchall()
        threads = _load_review_threads(conn, rows)
    return {"total": total, "page": page, "page_size": page_size,
            "query": query, "threads": threads}


@app.get("/api/reviews")
def list_reviews(
    q: str = Query("", max_length=REVIEW_QUERY_MAX_LENGTH),
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(20, ge=1, le=100),
):
    query = _validate_review_query(q)
    _validate_review_pagination(page, page_size)
    revision = _database_revision((REVIEWS_DB,))
    # 响应不能共享可变列表，避免调用方污染其他请求的缓存。
    return deepcopy(_review_page(query, page, page_size, revision))


@app.get("/api/review-courses")
def list_review_courses(
    q: str = Query("", max_length=REVIEW_QUERY_MAX_LENGTH),
    limit: int = Query(12, ge=1, le=50),
):
    query = _validate_review_query(q)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise HTTPException(status_code=422, detail="Invalid review query parameter")

    normalized = _normalize_review_query(query)
    where = ""
    params = []
    if query:
        raw_pattern = f"%{_escape_like(query)}%"
        conditions = ["course_name LIKE ? ESCAPE '\\'"]
        params = [raw_pattern]
        if normalized:
            conditions.append("search_name LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(normalized)}%")
        where = "WHERE " + " OR ".join(conditions)

    with get_reviews_db() as conn:
        rows = conn.execute(
            "SELECT course_name, course_codes, thread_count, entry_count "
            f"FROM course_catalog {where} "
            "ORDER BY thread_count DESC, entry_count DESC, course_name LIMIT ?",
            [*params, limit],
        ).fetchall()
    return [
        {
            "course_name": row["course_name"],
            "course_codes": [code for code in row["course_codes"].split(",") if code],
            "thread_count": row["thread_count"],
            "entry_count": row["entry_count"],
        }
        for row in rows
    ]


@app.get("/api/reviews/meta")
def get_review_meta():
    with get_reviews_db() as conn:
        metadata = dict(conn.execute("SELECT key, value FROM metadata"))
        date_range = conn.execute(
            "SELECT "
            "date(MIN(posted_at), 'unixepoch', '+8 hours') AS start_date, "
            "date(MAX(posted_at), 'unixepoch', '+8 hours') AS end_date "
            "FROM entries"
        ).fetchone()

    integer_keys = (
        "source_shards", "source_posts", "source_replies", "matched_threads",
        "matched_entries", "matched_replies", "snapshot_replies", "catalog_courses",
        "uncachedReplyDifference", "highlighted_entries", "course_highlights",
        "teacher_highlights", "course_aliases", "teacher_aliases",
        "course_alias_highlights", "teacher_alias_highlights",
    )
    payload = {
        "snapshot_date": metadata.get("snapshot_date", ""),
        "start_date": date_range["start_date"] or "",
        "end_date": date_range["end_date"] or "",
        "classifier_version": metadata.get("classifier_version", ""),
        "highlight_version": metadata.get("highlight_version", ""),
    }
    for key in integer_keys:
        output_key = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
        payload[output_key] = int(metadata.get(key, 0))
    payload["cached_reply_coverage_percent"] = float(
        metadata.get("cachedReplyCoveragePercent", 0)
    )
    return payload


@app.get("/api/reviews/{pid}")
def get_review_thread(pid: int):
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 2**63 - 1:
        raise HTTPException(status_code=422, detail="Invalid review thread id")

    with get_reviews_db() as conn:
        thread = conn.execute(
            "SELECT pid, source_month, posted_at, content, source_url, post_kind "
            "FROM threads WHERE pid=?",
            (pid,),
        ).fetchone()
        if thread is None:
            raise HTTPException(status_code=404, detail="Review thread not found")
        rows = conn.execute(
            "SELECT cid, floor, posted_at, content "
            "FROM thread_replies WHERE pid=? ORDER BY ordinal",
            (pid,),
        ).fetchall()

    replies = [dict(row) for row in rows]
    return {
        "pid": thread["pid"],
        "source_month": thread["source_month"],
        "posted_at": thread["posted_at"],
        "content": thread["content"],
        "source_url": thread["source_url"],
        "post_kind": thread["post_kind"],
        "reply_count": len(replies),
        "replies": replies,
    }


# Message board -------------------------------------------------------------------


def _validate_message_pagination(page: int, page_size: int) -> None:
    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or not 1 <= page <= 10000
        or isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= MESSAGE_PAGE_SIZE_MAX
    ):
        raise HTTPException(status_code=422, detail="Invalid message query parameter")


def _validate_message_content(payload) -> str:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Invalid message payload")
    content = payload.get("content")
    if not _valid_text(content):
        raise HTTPException(status_code=422, detail="Invalid message payload")
    content = content.strip()
    if not content or len(content) > MESSAGE_MAX_LENGTH:
        raise HTTPException(status_code=422, detail="Invalid message payload")
    return content


def _client_ip_hash(request) -> str:
    # 仅用于发布频率限制，不进入任何 API 响应。
    ip = ""
    if request is not None:
        ip = (request.headers.get("x-real-ip") or "").strip()
        if not ip and request.client is not None:
            ip = request.client.host or ""
    return hashlib.sha256((ip or "unknown").encode("utf-8")).hexdigest()


def _beijing_day(timestamp: int) -> str:
    # 用北京时间把时间戳归到 YYYY-MM-DD，与页面日期口径一致。
    return time.strftime(
        "%Y-%m-%d", time.gmtime(timestamp + STATS_TZ_OFFSET_SECONDS)
    )


def _is_bot_user_agent(user_agent: str) -> bool:
    ua = (user_agent or "").lower()
    if not ua:
        return True
    return any(
        token in ua
        for token in ("bot", "spider", "crawl", "slurp", "curl", "wget", "python-")
    )


def record_visit(request) -> None:
    # 记录一次页面访问；任何异常都不能影响页面返回。
    if request is None:
        return
    try:
        if _is_bot_user_agent(request.headers.get("user-agent", "")):
            return
        now = int(time.time())
        day = _beijing_day(now)
        ip_hash = _client_ip_hash(request)
        with get_stats_db() as conn:
            conn.execute(
                "INSERT INTO visit_days (day, ip_hash, views, last_at)"
                " VALUES (?, ?, 1, ?)"
                " ON CONFLICT(day, ip_hash) DO UPDATE SET"
                " views = views + 1, last_at = excluded.last_at",
                (day, ip_hash, now),
            )
            conn.commit()
    except (sqlite3.Error, OSError, ValueError):
        return


def _visit_stats_payload() -> dict:
    now = int(time.time())
    today = _beijing_day(now)
    trend_days = [
        _beijing_day(now - offset * 86400)
        for offset in range(STATS_TREND_DAYS - 1, -1, -1)
    ]
    window_start = trend_days[0]
    with get_stats_db() as conn:
        today_row = conn.execute(
            "SELECT COALESCE(SUM(views), 0) AS views, COUNT(*) AS visitors"
            " FROM visit_days WHERE day = ?",
            (today,),
        ).fetchone()
        week_row = conn.execute(
            "SELECT COALESCE(SUM(views), 0) AS views,"
            " COUNT(DISTINCT ip_hash) AS visitors"
            " FROM visit_days WHERE day >= ?",
            (window_start,),
        ).fetchone()
        total_row = conn.execute(
            "SELECT COALESCE(SUM(views), 0) AS views,"
            " COUNT(DISTINCT ip_hash) AS visitors FROM visit_days"
        ).fetchone()
        by_day = {
            row["day"]: row["views"]
            for row in conn.execute(
                "SELECT day, COALESCE(SUM(views), 0) AS views"
                " FROM visit_days WHERE day >= ? GROUP BY day",
                (window_start,),
            )
        }
    trend = [{"day": day, "views": by_day.get(day, 0)} for day in trend_days]
    return {
        "today": {"views": today_row["views"], "visitors": today_row["visitors"]},
        "week": {"views": week_row["views"], "visitors": week_row["visitors"]},
        "total": {"views": total_row["views"], "visitors": total_row["visitors"]},
        "trend": trend,
    }


@app.get("/api/stats")
def get_stats():
    return JSONResponse(
        _visit_stats_payload(), headers={"Cache-Control": "no-store"}
    )


def _message_nickname(request) -> str:
    if _request_token(request):
        with get_accounts_db() as conn:
            session = _current_session(conn, request, int(time.time()))
            if session is not None:
                return session["nickname"]
    return DEFAULT_NICKNAME


def _require_root_message(conn, message_id: int) -> None:
    if isinstance(message_id, bool) or not isinstance(message_id, int) or not 1 <= message_id <= 2**63 - 1:
        raise HTTPException(status_code=404, detail="Message not found")
    if conn.execute(
        "SELECT 1 FROM messages WHERE id=? AND parent_id IS NULL", (message_id,)
    ).fetchone() is None:
        raise HTTPException(status_code=404, detail="Message not found")


def _insert_message(content: str, ip_hash: str, nickname: str = DEFAULT_NICKNAME,
                    parent_id: int | None = None, course_key: str = "") -> dict:
    now = int(time.time())
    with get_messages_db() as conn:
        # 回复与留言共享频率限制；串行化检查与写入以防并发绕过。
        conn.execute("BEGIN IMMEDIATE")
        if parent_id is not None:
            _require_root_message(conn, parent_id)
            course_key = conn.execute("SELECT course_key FROM messages WHERE id=?", (parent_id,)).fetchone()[0]
        for window, limit in MESSAGE_RATE_LIMITS:
            recent = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE ip_hash=? AND posted_at>?",
                (ip_hash, now - window),
            ).fetchone()[0]
            if recent >= limit:
                raise HTTPException(
                    status_code=429, detail="Too many messages, please retry later"
                )
        cursor = conn.execute(
            "INSERT INTO messages (posted_at, content, ip_hash, nickname, parent_id, course_key)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (now, content, ip_hash, nickname, parent_id, course_key),
        )
        conn.commit()
        message_id = cursor.lastrowid
    return {"id": message_id, "posted_at": now, "content": content,
            "nickname": nickname, "reply_count": 0}


@app.get("/api/messages")
def list_messages(
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(20, ge=1, le=MESSAGE_PAGE_SIZE_MAX),
):
    _validate_message_pagination(page, page_size)
    offset = (page - 1) * page_size
    with get_messages_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM messages WHERE parent_id IS NULL AND course_key=''").fetchone()[0]
        rows = conn.execute(
            "SELECT m.id, m.posted_at, m.content, m.nickname,"
            " (SELECT COUNT(*) FROM messages r WHERE r.parent_id=m.id) AS reply_count"
            " FROM messages m WHERE m.parent_id IS NULL AND m.course_key=''"
            " ORDER BY m.id DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "messages": [dict(row) for row in rows],
    }


@app.post("/api/messages", status_code=201)
def create_message(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    content = _validate_message_content(payload)
    return _insert_message(content, _client_ip_hash(request), _message_nickname(request))


@app.get("/api/messages/{message_id}/replies")
def list_message_replies(
    message_id: int,
    before_id: int = Query(0, ge=0, le=2**63 - 1),
    page_size: int = Query(20, ge=1, le=MESSAGE_PAGE_SIZE_MAX),
):
    _validate_message_pagination(1, page_size)
    if isinstance(before_id, bool) or not isinstance(before_id, int) or not 0 <= before_id <= 2**63 - 1:
        raise HTTPException(status_code=422, detail="Invalid message query parameter")
    with get_messages_db() as conn:
        _require_root_message(conn, message_id)
        rows = conn.execute(
            "SELECT id, posted_at, content, nickname, 0 AS reply_count FROM messages"
            " WHERE parent_id=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT ?",
            (message_id, before_id, before_id, page_size + 1),
        ).fetchall()
    return {"replies": [dict(row) for row in rows[:page_size]],
            "has_more": len(rows) > page_size}


@app.post("/api/messages/{message_id}/replies", status_code=201)
def create_message_reply(message_id: int, request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    content = _validate_message_content(payload)
    return _insert_message(content, _client_ip_hash(request), _message_nickname(request), message_id)


def _course_message_identity(term_label, level, course_code, class_no) -> str:
    return json.dumps([str(value or "").strip() for value in (term_label, level, course_code, class_no)],
                      ensure_ascii=False, separators=(",", ":"))


def _annotate_course_corrections(courses: list[dict]) -> None:
    # 一页最多 200 门课，复用列表已有的课程身份，一次索引查询即可获得补充状态。
    if not courses:
        return
    keys = []
    for course in courses:
        term, prefix, _ = _parse_id(course["id"])
        keys.append(_course_message_identity(_term_label(term), _level_of_prefix(prefix),
                                             course["course_code"], course["class_no"]))
    unique_keys = list(dict.fromkeys(keys))
    placeholders = ",".join("?" for _ in unique_keys)
    try:
        with get_messages_db() as conn:
            corrected = {row[0] for row in conn.execute(
                f"SELECT DISTINCT course_key FROM messages WHERE parent_id IS NULL AND course_key IN ({placeholders})",
                unique_keys,
            )}
    except (sqlite3.Error, OSError):
        # 留言库暂不可用时仍提供课程搜索；未知状态不能当作已确认没有补充。
        for course in courses:
            course["has_course_corrections"] = None
        return
    for course, key in zip(courses, keys):
        course["has_course_corrections"] = key in corrected


def _course_message_key(course_id: str) -> str:
    # 绑定真实学期、培养层次、课程号和班号；换教师或重建本地 ID 不丢失讨论。
    snapshot = _favorite_snapshot(course_id)
    return _course_message_identity(*(snapshot[field] for field in ("term_label", "level", "course_code", "class_no")))


@app.get("/api/courses/{course_id}/messages")
def list_course_messages(
    course_id: str,
    before_id: int = Query(0, ge=0, le=2**63 - 1),
    page_size: int = Query(5, ge=1, le=MESSAGE_PAGE_SIZE_MAX),
):
    _validate_message_pagination(1, page_size)
    if isinstance(before_id, bool) or not isinstance(before_id, int) or not 0 <= before_id <= 2**63 - 1:
        raise HTTPException(status_code=422, detail="Invalid message query parameter")
    key = _course_message_key(course_id)
    with get_messages_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM messages WHERE course_key=? AND parent_id IS NULL", (key,)).fetchone()[0]
        rows = conn.execute(
            "SELECT m.id, m.posted_at, m.content, m.nickname,"
            " (SELECT COUNT(*) FROM messages r WHERE r.parent_id=m.id) AS reply_count"
            " FROM messages m WHERE m.course_key=? AND m.parent_id IS NULL"
            " AND (?=0 OR m.id<?) ORDER BY m.id DESC LIMIT ?",
            (key, before_id, before_id, page_size + 1),
        ).fetchall()
    return _no_store({"messages": [dict(row) for row in rows[:page_size]],
                      "total": total, "has_more": len(rows) > page_size})


@app.post("/api/courses/{course_id}/messages", status_code=201)
def create_course_message(course_id: str, request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    content = _validate_message_content(payload)
    key = _course_message_key(course_id)
    return _no_store(_insert_message(content, _client_ip_hash(request), _message_nickname(request), course_key=key),
                     status_code=201)


@app.get("/api/changelog")
def get_changelog():
    return _no_store(json.loads((BASE_DIR / "changelog.json").read_text(encoding="utf-8")))


# Accounts and favorites ----------------------------------------------------------
#
# 第三个可写库。服务器是收藏的唯一事实来源：客户端只提交课程 ID，快照由服务器从只读
# 课程库读取；密码与密保答案只存 scrypt 哈希，会话只存 sha256，任何响应都不含 IP、
# 哈希、token 明文或密保答案。


def _scrypt(secret: str, salt: bytes, params: dict) -> bytes:
    with _SCRYPT_GATE:
        return hashlib.scrypt(secret.encode("utf-8"), salt=salt, dklen=32, **params)


def _hash_secret(secret: str) -> str:
    params = SCRYPT_PARAMS
    salt = secrets.token_bytes(16)
    digest = _scrypt(secret, salt, params)
    return "scrypt${}${}${}${}${}".format(
        params["n"],
        params["r"],
        params["p"],
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def _parse_secret_hash(stored):
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return None
        params = {"n": int(n), "r": int(r), "p": int(p)}
        salt = base64.b64decode(salt_b64, validate=True)
        digest = base64.b64decode(hash_b64, validate=True)
    except (ValueError, AttributeError, TypeError):
        return None
    if not salt or not digest:
        return None
    return params, salt, digest


def _verify_secret(secret: str, stored: str) -> bool:
    parsed = _parse_secret_hash(stored)
    if parsed is None or not isinstance(secret, str):
        return False
    params, salt, expected = parsed
    try:
        digest = _scrypt(secret, salt, params)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def _secret_needs_rehash(stored: str) -> bool:
    parsed = _parse_secret_hash(stored)
    return parsed is None or parsed[0] != SCRYPT_PARAMS


def _dummy_secret_hash() -> str:
    # 用户不存在时也对它校验一次，让“用户不存在”与“密码错误”耗时接近。
    global _DUMMY_SECRET_HASH
    if _DUMMY_SECRET_HASH is None or _secret_needs_rehash(_DUMMY_SECRET_HASH):
        _DUMMY_SECRET_HASH = _hash_secret(secrets.token_urlsafe(16))
    return _DUMMY_SECRET_HASH


def _normalize_answer(text: str) -> str:
    # 答案不区分大小写、全半角与空白。
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _invalid_account_payload():
    return HTTPException(status_code=422, detail="Invalid account payload")


def _payload_dict(payload) -> dict:
    if not isinstance(payload, dict):
        raise _invalid_account_payload()
    return payload


def _validate_username(value):
    if not isinstance(value, str):
        raise _invalid_account_payload()
    username = value.strip()
    if not USERNAME_RE.fullmatch(username):
        raise _invalid_account_payload()
    return username, username.lower()


def _validate_password(value, username_key: str) -> str:
    if not _valid_text(value):
        raise _invalid_account_payload()
    if not PASSWORD_MIN_LENGTH <= len(value) <= PASSWORD_MAX_LENGTH:
        raise _invalid_account_payload()
    if value.casefold() == username_key:
        raise _invalid_account_payload()
    return value


def _validate_password_input(value) -> str:
    # 校验已有密码时只检查形状，不重复套用注册规则。
    if not _valid_text(value) or not 1 <= len(value) <= PASSWORD_MAX_LENGTH:
        raise _invalid_account_payload()
    return value


def _validate_questions(value, username_key: str):
    if not isinstance(value, list):
        raise _invalid_account_payload()
    if not SECURITY_QUESTION_MIN <= len(value) <= SECURITY_QUESTION_MAX:
        raise _invalid_account_payload()
    questions = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise _invalid_account_payload()
        question = item.get("question")
        answer = item.get("answer")
        if not _valid_text(question) or not _valid_text(answer):
            raise _invalid_account_payload()
        question = " ".join(question.split())
        if not 1 <= len(question) <= SECURITY_QUESTION_TEXT_MAX:
            raise _invalid_account_payload()
        if question.casefold() in seen:
            raise _invalid_account_payload()
        seen.add(question.casefold())
        normalized = _normalize_answer(answer)
        if not SECURITY_ANSWER_MIN <= len(normalized) <= SECURITY_ANSWER_MAX:
            raise _invalid_account_payload()
        if normalized == username_key:
            raise _invalid_account_payload()
        questions.append((question, normalized))
    return questions


def _enforce_rate_limit(conn, kind: str, subject: str, now: int) -> None:
    for window, limit in AUTH_RATE_LIMITS[kind]:
        recent = conn.execute(
            "SELECT COUNT(*) FROM auth_events WHERE kind=? AND subject=? AND at>?",
            (kind, subject, now - window),
        ).fetchone()[0]
        if recent >= limit:
            raise HTTPException(
                status_code=429,
                detail="Too many attempts, please retry later",
                headers={"Retry-After": str(window)},
            )


def _record_event(conn, kind: str, subject: str, now: int, count: int = 1) -> None:
    conn.executemany(
        "INSERT INTO auth_events (kind, subject, at) VALUES (?, ?, ?)",
        [(kind, subject, now)] * count,
    )


def _purge_expired(conn, now: int) -> None:
    conn.execute(
        "DELETE FROM auth_events WHERE at < ?", (now - AUTH_EVENT_RETENTION_SECONDS,)
    )
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))


def _session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _request_token(request) -> str:
    cookies = getattr(request, "cookies", None)
    if not cookies:
        return ""
    try:
        token = cookies.get(SESSION_COOKIE)
    except AttributeError:
        return ""
    return token if isinstance(token, str) else ""


def _new_session(conn, user_id: int, now: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, last_seen_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (_session_token_hash(token), user_id, now, now, now + SESSION_TTL_SECONDS),
    )
    return token


def _current_session(conn, request, now: int):
    """Return the joined session/user row for a valid cookie, else None."""
    token = _request_token(request)
    if not token:
        return None
    token_hash = _session_token_hash(token)
    row = conn.execute(
        "SELECT s.token_hash, s.user_id, s.last_seen_at, s.expires_at,"
        " u.username, u.username_key, u.password_hash, u.nickname"
        " FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= now:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
        conn.commit()
        return None
    return row


def _require_user(conn, request, now: int):
    session = _current_session(conn, request, now)
    if session is None:
        raise HTTPException(status_code=401, detail="Login required")
    return session


def _cookie_secure(request) -> bool:
    forwarded = ""
    try:
        forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0]
    except AttributeError:
        forwarded = ""
    forwarded = forwarded.strip().lower()
    if forwarded:
        return forwarded == "https"
    url = getattr(request, "url", None)
    return getattr(url, "scheme", "") == "https"


def _set_session_cookie(response, request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        secure=_cookie_secure(request),
        httponly=True,
        samesite="lax",
    )


def _clear_session_cookie(response, request) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=_cookie_secure(request),
        httponly=True,
        samesite="lax",
    )


def _request_host(request) -> str:
    headers = request.headers
    host = headers.get("x-forwarded-host") or headers.get("host") or ""
    return host.split(",")[0].strip().lower()


def _require_trusted_origin(request) -> None:
    # SameSite=Lax 之外的第二道 CSRF 防线：浏览器发起的变更请求必带 Origin，
    # 其主机必须与 Host 一致；两者都缺的非浏览器客户端没有 cookie 可被利用。
    headers = request.headers
    source = (headers.get("origin") or "").strip() or (headers.get("referer") or "").strip()
    if not source:
        return
    if source.lower() == "null":
        raise HTTPException(status_code=403, detail="Untrusted origin")
    netloc = urlsplit(source).netloc.lower()
    host = _request_host(request)
    if not netloc or not host or netloc != host:
        raise HTTPException(status_code=403, detail="Untrusted origin")


def _no_store(payload, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload, status_code=status_code, headers={"Cache-Control": "no-store"}
    )


def _empty_no_store(status_code: int = 204) -> Response:
    return Response(status_code=status_code, headers={"Cache-Control": "no-store"})


def _verify_current_password(conn, session, password: str, request, now: int) -> None:
    # 已登录用户修改密码、密保或删号前必须再次证明持有密码；失败计入登录失败限流，
    # 防止被盗会话暴力猜密码。
    ip_hash = _client_ip_hash(request)
    _enforce_rate_limit(conn, "login_fail_ip", ip_hash, now)
    _enforce_rate_limit(conn, "login_fail_user", session["username_key"], now)
    _enforce_rate_limit(conn, "secret_verify_global", "*", now)
    _record_event(conn, "secret_verify_global", "*", now)
    conn.commit()
    if not _verify_secret(password, session["password_hash"]):
        _record_event(conn, "login_fail_ip", ip_hash, now)
        _record_event(conn, "login_fail_user", session["username_key"], now)
        conn.commit()
        raise HTTPException(status_code=401, detail="当前密码错误")


def _begin_verified_account_write(conn, session) -> None:
    # scrypt 在写锁外执行；提交敏感变更前锁内复查，防止过时验证覆盖改密或退出。
    conn.execute("BEGIN IMMEDIATE")
    current = conn.execute(
        "SELECT u.password_hash FROM sessions s JOIN users u ON u.id=s.user_id"
        " WHERE s.token_hash=? AND s.user_id=? AND s.expires_at>?",
        (session["token_hash"], session["user_id"], int(time.time())),
    ).fetchone()
    if current is None or current["password_hash"] != session["password_hash"]:
        raise HTTPException(status_code=401, detail="账号状态已变化，请重新登录后重试")


def _public_questions(conn, user_id: int) -> list:
    rows = conn.execute(
        "SELECT position, question FROM security_questions WHERE user_id=? ORDER BY position",
        (user_id,),
    ).fetchall()
    return [{"position": row["position"], "question": row["question"]} for row in rows]


def _hash_questions(questions) -> list:
    return [(question, _hash_secret(answer)) for question, answer in questions]


def _store_questions(conn, user_id: int, hashed_questions) -> None:
    conn.execute("DELETE FROM security_questions WHERE user_id=?", (user_id,))
    conn.executemany(
        "INSERT INTO security_questions (user_id, position, question, answer_hash)"
        " VALUES (?, ?, ?, ?)",
        [
            (user_id, index + 1, question, answer_hash)
            for index, (question, answer_hash) in enumerate(hashed_questions)
        ],
    )


def _term_label(term: str) -> str:
    # 学期槽位（spring/summer/fall）会指向新学年，快照另存 “2026秋季学期” 这样的字面。
    config = TERM_DBS.get(term)
    if config:
        match = _TERM_LABEL_RE.match(config[0][1].stem)
        if match:
            return match.group(0)
    return term


def _favorite_key(term, level, course_code, class_no, teacher) -> str:
    parts = (term, level, course_code, class_no, teacher)
    return "|".join(("" if part is None else str(part)).strip() for part in parts)


def _level_of_prefix(prefix: str) -> str:
    return "gr" if prefix in ("g", "r") else "ug"


def _favorite_snapshot(course_id: str) -> dict:
    term, prefix, _ = _parse_id(course_id)
    if prefix is None:
        raise HTTPException(status_code=422, detail="Invalid course id")
    detail = get_course_detail(course_id, "zh")
    level = _level_of_prefix(prefix)
    course_code = str(detail.get("course_code") or "").strip()
    class_no = str(detail.get("class_no") or "").strip()
    teacher = str(detail.get("teacher") or "").strip()
    return {
        "fav_key": _favorite_key(term, level, course_code, class_no, teacher),
        "course_id": course_id,
        "term": term,
        "term_label": _term_label(term),
        "level": level,
        "course_code": course_code,
        "class_no": class_no,
        "teacher": teacher,
        "course_name": str(detail.get("course_name") or "").strip(),
        "credits": detail.get("credits"),
        "schedule": detail.get("schedule") or "",
        "department": detail.get("department") or "",
    }


def _favorites_list(conn, user_id: int) -> list:
    rows = conn.execute(
        "SELECT fav_key, course_id, term, term_label, level, course_code, class_no,"
        " teacher, course_name, credits, schedule, department, added_at"
        " FROM favorites WHERE user_id=? ORDER BY added_at DESC, fav_key",
        (user_id,),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["id"] = item.pop("course_id")
        item["available"] = True
        items.append(item)
    return items


def _enrich_saved_courses(items: list) -> None:
    by_term = {}
    for item in items:
        by_term.setdefault(item.get("term"), []).append(item)
    for term, group in by_term.items():
        if term not in TERM_DBS:
            continue
        aliases = {prefix: alias for alias, _, prefix in TERM_DBS[term]}
        try:
            eligible = []
            for item in group:
                _, prefix, _ = _parse_id(item.get("id", ""))
                if prefix not in aliases or item.get("term_label") != _term_label(term):
                    item["available"] = False
                else:
                    eligible.append((prefix, item))
            if not eligible:
                continue

            # 先缩到所需课程号，再按主页完整规则归并；同一身份的所有源记录仍保留。
            codes = tuple(dict.fromkeys(item["course_code"] for _, item in eligible))
            base_sql = " UNION ALL ".join(
                select for alias, select in TERM_LIST_SELECTS[term] if alias in aliases.values()
            )
            source_sql, params = _translated_source_select(base_sql, "main", "zh")
            source_sql = (
                f"SELECT * FROM ({source_sql}) WHERE course_code IN "
                f"({','.join('?' for _ in codes)})"
            )
            sql = f"{_grouped_course_ctes(source_sql, '')} SELECT * FROM grouped"
            with get_db(term) as course_conn:
                rows = course_conn.execute(sql, (*params, *codes)).fetchall()
            by_identity = {}
            for row in rows:
                identity = (
                    row["id"][0], str(row["course_code"]), str(row["class_no"]),
                    (row["teacher"] or "").strip(),
                )
                by_identity.setdefault(identity, []).append(row)
            for prefix, item in eligible:
                identity = (
                    prefix, str(item["course_code"]), str(item["class_no"]),
                    (item["teacher"] or "").strip(),
                )
                candidates = by_identity.get(identity, [])
                if not candidates:
                    item["available"] = False
                    continue
                # 空教师的源记录在主页保持独立，优先保留原 ID 对应的一组。
                source = next((row for row in candidates if row["id"] == item["id"]), None)
                if source is None:
                    source = min(candidates, key=lambda row: int(row["id"][1:]))
                item["id"] = source["id"]
                item["available"] = True
                for field in ("course_name", "teacher", "credits", "schedule", "classroom", "department"):
                    item[field] = source[field]
                for field in ("course_type", "category"):
                    item[field] = [value for value in (source[field] or "").split(",") if value]
        except (sqlite3.Error, OSError, HTTPException):
            for item in group:
                item["available"] = False


def _refresh_favorite_ids(conn, user_id: int, items: list) -> None:
    """课程库重建后 ID 会漂移；按稳定键回写新 ID，学期字面变化时标记不可用。"""
    by_term = {}
    for item in items:
        by_term.setdefault(item["term"], []).append(item)
    changed = False
    for term, group in by_term.items():
        config = TERM_DBS.get(term)
        if config is None:
            for item in group:
                item["available"] = False
            continue
        alias_by_prefix = {prefix: alias for alias, _, prefix in config}
        current_label = _term_label(term)
        try:
            with get_db(term) as course_conn:
                for item in group:
                    if item["term_label"] != current_label:
                        item["available"] = False
                        continue
                    _, prefix, local_id = _parse_id(item["id"])
                    alias = alias_by_prefix.get(prefix)
                    if alias is None:
                        item["available"] = False
                        continue
                    exists = course_conn.execute(
                        f"SELECT 1 FROM {alias}.basic_info WHERE id=?", (local_id,)
                    ).fetchone()
                    if exists:
                        continue
                    row = course_conn.execute(
                        f"SELECT MIN(id) FROM {alias}.basic_info"
                        " WHERE course_code=? AND CAST(class_no AS TEXT)=?"
                        " AND COALESCE(teacher, '')=?",
                        (item["course_code"], item["class_no"], item["teacher"]),
                    ).fetchone()
                    if row is None or row[0] is None:
                        item["available"] = False
                        continue
                    new_id = f"{prefix}{row[0]}"
                    conn.execute(
                        "UPDATE favorites SET course_id=? WHERE user_id=? AND fav_key=?",
                        (new_id, user_id, item["fav_key"]),
                    )
                    item["id"] = new_id
                    changed = True
        except (sqlite3.Error, OSError, HTTPException):
            for item in group:
                item["available"] = False
    if changed:
        conn.commit()


def _validate_collection_name(value) -> str:
    if not _valid_text(value):
        raise _invalid_account_payload()
    name = value.strip()
    if not 1 <= len(name) <= COLLECTION_NAME_MAX:
        raise _invalid_account_payload()
    return name


def _validate_collection_id(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**63 - 1:
        raise _invalid_account_payload()
    return value


def _ensure_default_collection(conn, user_id: int, now: int) -> int:
    row = conn.execute(
        "SELECT id FROM collections WHERE user_id=? AND is_default=1", (user_id,)
    ).fetchone()
    if row is not None:
        return row["id"]
    cursor = conn.execute(
        "INSERT INTO collections (user_id, name, is_default, position, created_at)"
        " VALUES (?, ?, 1, 0, ?)",
        (user_id, DEFAULT_COLLECTION_NAME, now),
    )
    return cursor.lastrowid


def _collections_list(conn, user_id: int) -> list:
    rows = conn.execute(
        "SELECT c.id, c.name, c.is_default, c.position, COUNT(fc.fav_key) AS count"
        " FROM collections c"
        " LEFT JOIN favorite_collections fc ON fc.collection_id = c.id"
        " WHERE c.user_id=?"
        " GROUP BY c.id, c.name, c.is_default, c.position"
        " ORDER BY c.is_default DESC, c.position, c.id",
        (user_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "is_default": bool(row["is_default"]),
            "position": row["position"],
            "count": row["count"],
        }
        for row in rows
    ]


def _collection_memberships(conn, user_id: int) -> dict:
    rows = conn.execute(
        "SELECT fav_key, collection_id FROM favorite_collections WHERE user_id=?",
        (user_id,),
    ).fetchall()
    memberships: dict = {}
    for row in rows:
        memberships.setdefault(row["fav_key"], []).append(row["collection_id"])
    return memberships


def _owned_collection_ids(conn, user_id: int, raw) -> list:
    if not isinstance(raw, list):
        raise _invalid_account_payload()
    ids = list(dict.fromkeys(_validate_collection_id(value) for value in raw))
    if not ids:
        return []
    owned = {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM collections WHERE user_id=?",
            (user_id,),
        ).fetchall()
    }
    if any(value not in owned for value in ids):
        raise HTTPException(status_code=404, detail="Collection not found")
    return ids


def _favorites_payload(conn, user_id: int) -> dict:
    items = _favorites_list(conn, user_id)
    _refresh_favorite_ids(conn, user_id, items)
    _enrich_saved_courses(items)
    memberships = _collection_memberships(conn, user_id)
    for item in items:
        item["collection_ids"] = memberships.get(item["fav_key"], [])
    return {
        "favorites": items,
        "limit": FAVORITES_MAX,
        "collections": _collections_list(conn, user_id),
        "collections_limit": COLLECTIONS_MAX,
    }


TIMETABLE_LIMIT = 100
_TIMETABLE_SLOT_RE = re.compile(
    r"(?:(?P<weeks>\d{1,2}(?:[~～-]\d{1,2})?(?:[,，、]\d{1,2}(?:[~～-]\d{1,2})?)*)周\s*)?"
    r"(?P<parity>每周|单周|双周)?\s*周(?P<day>[一二三四五六日天])\s*"
    r"(?P<start>\d{1,2})(?:[~～-](?P<end>\d{1,2}))?节"
)


def _timetable_sessions(schedule: str) -> dict:
    sessions = []
    for match in _TIMETABLE_SLOT_RE.finditer(schedule or ""):
        start, end = int(match['start']), int(match['end'] or match['start'])
        if not 1 <= start <= end <= 14:
            continue
        weeks = None
        if match['weeks']:
            weeks = set()
            for part in re.split(r'[,，、]', match['weeks']):
                bounds = re.split(r'[~～-]', part)
                a, b = int(bounds[0]), int(bounds[-1])
                if not 0 <= a <= b <= 30:
                    weeks = None
                    break
                weeks.update(range(a, b + 1))
            if weeks is None:
                continue
        parity = match['parity'] or '每周'
        if weeks is not None:
            weeks = sorted(w for w in weeks if parity == '每周' or w % 2 == (1 if parity == '单周' else 0))
        sessions.append({"day": "一二三四五六日".index(match['day'].replace('天', '日')) + 1,
                         "start": start, "end": end, "weeks": weeks, "parity": parity,
                         "label": match.group(0).strip()})
    expected = len(re.findall(r'周[一二三四五六日天]', schedule or ''))
    return {"sessions": sessions, "unparsed": not sessions or len(sessions) != expected}


def _timetable_payload(conn, user_id):
    items = []
    overrides = []
    for row in conn.execute("SELECT course_key, snapshot, added_at, customization, is_custom FROM timetable_courses WHERE user_id=? ORDER BY added_at, course_key", (user_id,)):
        item = json.loads(row['snapshot'])
        item.update(course_key=row['course_key'], added_at=row['added_at'], is_custom=bool(row['is_custom']))
        items.append(item)
        overrides.append(json.loads(row['customization']))
    # Identity and source enrichment always use the untouched source snapshot.
    _enrich_saved_courses([item for item in items if not item['is_custom']])
    for item, changes in zip(items, overrides):
        item['source_available'] = not item['is_custom'] and item.get('available', False)
        item['is_edited'] = bool(changes)
        item.update(changes)
        if item['is_custom'] or 'schedule' in changes:
            item['available'] = True
        item.update(_timetable_sessions(item.get('schedule', '')))
    return {"courses": items, "limit": TIMETABLE_LIMIT}


def _timetable_course_key(payload):
    key = _payload_dict(payload).get('course_key')
    if not _valid_text(key) or not 1 <= len(key) <= 2000:
        raise HTTPException(status_code=422, detail="Invalid course key")
    return key


def _timetable_schedule(sessions):
    if not isinstance(sessions, list) or len(sessions) > 12:
        raise HTTPException(status_code=422, detail='上课时间最多设置 12 段')
    labels = []
    for slot in sessions:
        if not isinstance(slot, dict):
            raise HTTPException(status_code=422, detail='请检查上课时间')
        day, start, end = (slot.get(key) for key in ('day', 'start', 'end'))
        weeks, parity = slot.get('weeks'), slot.get('parity', '每周')
        if (any(type(value) is not int for value in (day, start, end))
                or not 1 <= day <= 7 or not 1 <= start <= end <= 14
                or parity not in ('每周', '单周', '双周')):
            raise HTTPException(status_code=422, detail='星期须为周一至周日，节次须在 1–14 节内且结束不早于开始')
        if weeks is not None:
            if (not isinstance(weeks, list) or not 1 <= len(weeks) <= 31
                    or any(type(week) is not int or not 0 <= week <= 30 for week in weeks)):
                raise HTTPException(status_code=422, detail='周次须在 0–30 周内')
            weeks = sorted(set(weeks))
        active = list(range(31)) if weeks is None else weeks
        if not any(parity == '每周' or week % 2 == (1 if parity == '单周' else 0) for week in active):
            raise HTTPException(status_code=422, detail='所选周次与单双周设置不匹配')
        prefix = ','.join(map(str, weeks)) + '周 ' if weeks is not None else ''
        labels.append(f"{prefix}{parity}周{'一二三四五六日'[day - 1]}{start}~{end}节")
    return '\n'.join(labels)


def _timetable_changes(value, *, custom=False):
    value = _payload_dict(value)
    limits = {'course_name': 200, 'teacher': 100, 'classroom': 200, 'schedule': 4000, 'notes': 1000}
    allowed = set(limits) | {'sessions'} | ({'term'} if custom else set())
    if set(value) - allowed or ('sessions' in value and 'schedule' in value):
        raise HTTPException(status_code=422, detail='包含不支持的课表字段')
    result = {}
    for field, limit in limits.items():
        if field not in value:
            continue
        text = value[field]
        if (not _valid_text(text) or len(text) > limit
                or any(unicodedata.category(char) == 'Cc' and char not in '\n\r\t' for char in text)):
            raise HTTPException(status_code=422, detail=f'请检查课程信息长度与格式（{field}）')
        result[field] = text.strip()
    if 'course_name' in result and not result['course_name']:
        raise HTTPException(status_code=422, detail='请填写课程名称')
    if 'term' in value:
        if not isinstance(value['term'], str) or value['term'] not in TERM_DBS:
            raise HTTPException(status_code=422, detail='请选择有效学期')
        result['term'] = value['term']
        result['term_label'] = _term_label(value['term'])
    if 'sessions' in value:
        result['schedule'] = _timetable_schedule(value['sessions'])
    return result


@contextmanager
def _timetable_write(request):
    _require_trusted_origin(request)
    with get_accounts_db() as conn:
        # Recheck ownership/session under the same lock as every personal edit.
        conn.execute('BEGIN IMMEDIATE')
        now = int(time.time())
        session = _require_user(conn, request, now)
        subject = _client_ip_hash(request)
        _enforce_rate_limit(conn, 'favorite_write_ip', subject, now)
        _record_event(conn, 'favorite_write_ip', subject, now)
        yield conn, session['user_id'], now


@app.post('/api/timetable/custom')
def create_custom_timetable_course(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    payload = _payload_dict(payload)
    nonce = payload.get('request_id')
    if not isinstance(nonce, str) or not re.fullmatch(r'[a-f0-9]{32}', nonce):
        raise HTTPException(status_code=422, detail='Invalid request id')
    fields = _timetable_changes(payload.get('course'), custom=True)
    if not fields.get('course_name') or 'term' not in fields:
        raise HTTPException(status_code=422, detail='请填写课程名称并选择学期')
    key = 'custom:' + nonce
    snapshot = {'id': key, 'teacher': '', 'classroom': '', 'schedule': '', 'notes': '', **fields}
    with _timetable_write(request) as (conn, user_id, now):
        exists = conn.execute('SELECT 1 FROM timetable_courses WHERE user_id=? AND course_key=?', (user_id, key)).fetchone()
        if not exists and conn.execute('SELECT COUNT(*) FROM timetable_courses WHERE user_id=?', (user_id,)).fetchone()[0] >= TIMETABLE_LIMIT:
            raise HTTPException(status_code=409, detail='课表课程已达上限（100 门）')
        # Retrying after a lost response cannot create a second custom course.
        conn.execute('INSERT INTO timetable_courses(user_id,course_key,snapshot,added_at,is_custom) VALUES(?,?,?,?,1) '
                     'ON CONFLICT(user_id,course_key) DO NOTHING',
                     (user_id, key, json.dumps(snapshot, ensure_ascii=False), now))
        conn.commit()
        return _no_store(_timetable_payload(conn, user_id))


@app.post('/api/timetable/update')
def update_timetable_course(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    key = _timetable_course_key(payload)
    with _timetable_write(request) as (conn, user_id, _):
        row = conn.execute('SELECT snapshot, customization, is_custom FROM timetable_courses WHERE user_id=? AND course_key=?', (user_id, key)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='课程已从你的课表移除，请重新加载')
        changes = _timetable_changes(payload.get('changes'), custom=bool(row['is_custom']))
        column = 'snapshot' if row['is_custom'] else 'customization'
        stored = json.loads(row[column])
        stored.update(changes)
        conn.execute(f'UPDATE timetable_courses SET {column}=? WHERE user_id=? AND course_key=?',
                     (json.dumps(stored, ensure_ascii=False), user_id, key))
        conn.commit()
        return _no_store(_timetable_payload(conn, user_id))


@app.post('/api/timetable/reset')
def reset_timetable_course(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    key = _timetable_course_key(payload)
    with _timetable_write(request) as (conn, user_id, _):
        row = conn.execute('SELECT is_custom FROM timetable_courses WHERE user_id=? AND course_key=?', (user_id, key)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='课程已从你的课表移除，请重新加载')
        if row['is_custom']:
            raise HTTPException(status_code=409, detail='自定义课程没有选课网原始信息')
        conn.execute("UPDATE timetable_courses SET customization='{}' WHERE user_id=? AND course_key=?", (user_id, key))
        conn.commit()
        return _no_store(_timetable_payload(conn, user_id))


@app.get("/api/timetable")
def get_timetable(request: Request):
    with get_accounts_db() as conn:
        session = _require_user(conn, request, int(time.time()))
        return _no_store(_timetable_payload(conn, session['user_id']))


@app.post("/api/timetable")
def add_timetable_course(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    course_id = _payload_dict(payload).get('id')
    if not isinstance(course_id, str) or not COURSE_ID_RE.fullmatch(course_id):
        raise HTTPException(status_code=422, detail="Invalid course id")
    now = int(time.time())
    with get_accounts_db() as conn:
        session = _require_user(conn, request, now)
        user_id = session['user_id']
        _enforce_rate_limit(conn, 'favorite_write_ip', _client_ip_hash(request), now)
        _record_event(conn, 'favorite_write_ip', _client_ip_hash(request), now)
        conn.commit()
        snapshot = _favorite_snapshot(course_id)
        snapshot['id'] = snapshot.pop('course_id')
        _enrich_saved_courses([snapshot])
        key = snapshot['fav_key']
        conn.execute('BEGIN IMMEDIATE')
        exists = conn.execute('SELECT 1 FROM timetable_courses WHERE user_id=? AND course_key=?', (user_id, key)).fetchone()
        if not exists and conn.execute('SELECT COUNT(*) FROM timetable_courses WHERE user_id=?', (user_id,)).fetchone()[0] >= TIMETABLE_LIMIT:
            raise HTTPException(status_code=409, detail='课表课程已达上限（100 门）')
        conn.execute("INSERT INTO timetable_courses(user_id,course_key,snapshot,added_at) VALUES(?,?,?,?) "
                     "ON CONFLICT(user_id,course_key) DO UPDATE SET snapshot=excluded.snapshot",
                     (user_id, key, json.dumps(snapshot, ensure_ascii=False), now))
        conn.commit()
        return _no_store(_timetable_payload(conn, user_id))


@app.post("/api/timetable/remove")
def remove_timetable_course(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    key = _payload_dict(payload).get('course_key')
    if not _valid_text(key) or not 1 <= len(key) <= 2000:
        raise HTTPException(status_code=422, detail="Invalid course key")
    with get_accounts_db() as conn:
        now = int(time.time())
        session = _require_user(conn, request, now)
        _enforce_rate_limit(conn, 'favorite_write_ip', _client_ip_hash(request), now)
        _record_event(conn, 'favorite_write_ip', _client_ip_hash(request), now)
        conn.execute('DELETE FROM timetable_courses WHERE user_id=? AND course_key=?', (session['user_id'], key))
        conn.commit()
        return _no_store(_timetable_payload(conn, session['user_id']))


@app.get("/api/account")
def get_account(request: Request):
    now = int(time.time())
    if not _request_token(request):
        # 匿名访客不碰账户库。
        return _no_store({"authenticated": False})
    with get_accounts_db() as conn:
        session = _current_session(conn, request, now)
        if session is None:
            response = _no_store({"authenticated": False})
            _clear_session_cookie(response, request)
            return response
        refreshed = False
        if now - session["last_seen_at"] >= SESSION_REFRESH_SECONDS:
            conn.execute(
                "UPDATE sessions SET last_seen_at=?, expires_at=? WHERE token_hash=?",
                (now, now + SESSION_TTL_SECONDS, session["token_hash"]),
            )
            conn.commit()
            refreshed = True
        payload = {
            "authenticated": True,
            "username": session["username"],
            "nickname": session["nickname"],
            "questions": _public_questions(conn, session["user_id"]),
        }
        payload.update(_favorites_payload(conn, session["user_id"]))
    response = _no_store(payload)
    if refreshed:
        _set_session_cookie(response, request, _request_token(request))
    return response


@app.post("/api/account/nickname")
def change_nickname(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    data = _payload_dict(payload)
    nickname = data.get("nickname")
    if (not isinstance(nickname, str) or not 1 <= len(nickname.strip()) <= NICKNAME_MAX_LENGTH
            or any(unicodedata.category(char) in {"Cc", "Cs"} for char in nickname)):
        raise HTTPException(status_code=422, detail="昵称需为 1 到 30 个字，不能包含换行或控制字符")
    nickname = nickname.strip()
    with get_accounts_db() as conn:
        session = _require_user(conn, request, int(time.time()))
        conn.execute("UPDATE users SET nickname=? WHERE id=?", (nickname, session["user_id"]))
        conn.commit()
    return _no_store({"nickname": nickname})


@app.post("/api/auth/register", status_code=201)
def register_account(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    data = _payload_dict(payload)
    username, username_key = _validate_username(data.get("username"))
    password = _validate_password(data.get("password"), username_key)
    questions = _validate_questions(data.get("questions"), username_key)
    now = int(time.time())
    ip_hash = _client_ip_hash(request)
    with get_accounts_db() as conn:
        _enforce_rate_limit(conn, "register_ip", ip_hash, now)
        _enforce_rate_limit(conn, "secret_verify_global", "*", now)
        _record_event(conn, "register_ip", ip_hash, now)
        conn.commit()
        taken = conn.execute(
            "SELECT 1 FROM users WHERE username_key=?", (username_key,)
        ).fetchone()
        if taken:
            raise HTTPException(status_code=409, detail="Username already taken")
        _record_event(conn, "secret_verify_global", "*", now, count=1 + len(questions))
        conn.commit()
        password_hash = _hash_secret(password)
        hashed_questions = _hash_questions(questions)
        try:
            cursor = conn.execute(
                "INSERT INTO users (username, username_key, password_hash,"
                " created_at, password_changed_at) VALUES (?, ?, ?, ?, ?)",
                (username, username_key, password_hash, now, now),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            raise HTTPException(status_code=409, detail="Username already taken")
        user_id = cursor.lastrowid
        _store_questions(conn, user_id, hashed_questions)
        _ensure_default_collection(conn, user_id, now)
        token = _new_session(conn, user_id, now)
        _purge_expired(conn, now)
        conn.commit()
    response = _no_store({"username": username}, status_code=201)
    _set_session_cookie(response, request, token)
    return response


@app.post("/api/auth/login")
def login_account(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    data = _payload_dict(payload)
    username, username_key = _validate_username(data.get("username"))
    password = _validate_password_input(data.get("password"))
    now = int(time.time())
    ip_hash = _client_ip_hash(request)
    with get_accounts_db() as conn:
        _enforce_rate_limit(conn, "login_fail_ip", ip_hash, now)
        _enforce_rate_limit(conn, "login_fail_user", username_key, now)
        _enforce_rate_limit(conn, "secret_verify_global", "*", now)
        _record_event(conn, "secret_verify_global", "*", now)
        conn.commit()
        user = conn.execute(
            "SELECT id, username, password_hash FROM users WHERE username_key=?",
            (username_key,),
        ).fetchone()
        stored = user["password_hash"] if user is not None else _dummy_secret_hash()
        verified = _verify_secret(password, stored)
        if user is None or not verified:
            _record_event(conn, "login_fail_ip", ip_hash, now)
            _record_event(conn, "login_fail_user", username_key, now)
            conn.commit()
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        upgraded_hash = _hash_secret(password) if _secret_needs_rehash(stored) else None
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        if current is None or current["password_hash"] != stored:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        old_token = _request_token(request)
        if old_token:
            conn.execute(
                "DELETE FROM sessions WHERE token_hash=?", (_session_token_hash(old_token),)
            )
        if upgraded_hash is not None:
            conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (upgraded_hash, user["id"]),
            )
        token = _new_session(conn, user["id"], now)
        _purge_expired(conn, now)
        conn.commit()
    response = _no_store({"username": user["username"]})
    _set_session_cookie(response, request, token)
    return response


@app.post("/api/auth/logout", status_code=204)
def logout_account(request: Request):
    _require_trusted_origin(request)
    token = _request_token(request)
    if token:
        with get_accounts_db() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE token_hash=?", (_session_token_hash(token),)
            )
            conn.commit()
    response = _empty_no_store()
    _clear_session_cookie(response, request)
    return response


@app.post("/api/auth/password", status_code=204)
def change_password(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    data = _payload_dict(payload)
    now = int(time.time())
    with get_accounts_db() as conn:
        session = _require_user(conn, request, now)
        current = _validate_password_input(data.get("current_password"))
        new_password = _validate_password(data.get("new_password"), session["username_key"])
        _verify_current_password(conn, session, current, request, now)
        password_hash = _hash_secret(new_password)
        _begin_verified_account_write(conn, session)
        conn.execute(
            "UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
            (password_hash, now, session["user_id"]),
        )
        conn.execute(
            "DELETE FROM sessions WHERE user_id=? AND token_hash<>?",
            (session["user_id"], session["token_hash"]),
        )
        conn.commit()
    return _empty_no_store()


@app.post("/api/auth/questions", status_code=204)
def change_questions(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    data = _payload_dict(payload)
    now = int(time.time())
    with get_accounts_db() as conn:
        session = _require_user(conn, request, now)
        current = _validate_password_input(data.get("current_password"))
        questions = _validate_questions(data.get("questions"), session["username_key"])
        _verify_current_password(conn, session, current, request, now)
        _record_event(conn, "secret_verify_global", "*", now, count=len(questions))
        conn.commit()
        hashed_questions = _hash_questions(questions)
        _begin_verified_account_write(conn, session)
        _store_questions(conn, session["user_id"], hashed_questions)
        conn.commit()
    return _empty_no_store()


@app.post("/api/auth/reset/questions")
def reset_questions(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    data = _payload_dict(payload)
    _, username_key = _validate_username(data.get("username"))
    now = int(time.time())
    ip_hash = _client_ip_hash(request)
    with get_accounts_db() as conn:
        _enforce_rate_limit(conn, "reset_lookup_ip", ip_hash, now)
        _record_event(conn, "reset_lookup_ip", ip_hash, now)
        conn.commit()
        user = conn.execute(
            "SELECT id, username FROM users WHERE username_key=?", (username_key,)
        ).fetchone()
        if user is None:
            raise HTTPException(status_code=404, detail="Account not found")
        questions = _public_questions(conn, user["id"])
    return _no_store({"username": user["username"], "questions": questions})


@app.post("/api/auth/reset", status_code=204)
def reset_password(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    data = _payload_dict(payload)
    _, username_key = _validate_username(data.get("username"))
    position = data.get("position")
    if (
        isinstance(position, bool)
        or not isinstance(position, int)
        or not 1 <= position <= SECURITY_QUESTION_MAX
    ):
        raise _invalid_account_payload()
    answer = data.get("answer")
    if not _valid_text(answer) or not 1 <= len(answer) <= 200:
        raise _invalid_account_payload()
    new_password = _validate_password(data.get("new_password"), username_key)
    normalized = _normalize_answer(answer)
    now = int(time.time())
    ip_hash = _client_ip_hash(request)
    with get_accounts_db() as conn:
        _enforce_rate_limit(conn, "reset_fail_ip", ip_hash, now)
        _enforce_rate_limit(conn, "reset_fail_user", username_key, now)
        _enforce_rate_limit(conn, "secret_verify_global", "*", now)
        _record_event(conn, "secret_verify_global", "*", now)
        conn.commit()
        row = conn.execute(
            "SELECT u.id, u.password_hash, q.answer_hash FROM users u"
            " LEFT JOIN security_questions q ON q.user_id = u.id AND q.position=?"
            " WHERE u.username_key=?",
            (position, username_key),
        ).fetchone()
        stored = row["answer_hash"] if row is not None and row["answer_hash"] else None
        verified = _verify_secret(normalized, stored or _dummy_secret_hash())
        if stored is None or not verified:
            _record_event(conn, "reset_fail_ip", ip_hash, now)
            _record_event(conn, "reset_fail_user", username_key, now)
            conn.commit()
            raise HTTPException(status_code=401, detail="密保答案错误")
        password_hash = _hash_secret(new_password)
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT u.password_hash, q.answer_hash FROM users u"
            " JOIN security_questions q ON q.user_id=u.id AND q.position=? WHERE u.id=?",
            (position, row["id"]),
        ).fetchone()
        if (current is None or current["password_hash"] != row["password_hash"]
                or current["answer_hash"] != stored):
            raise HTTPException(status_code=401, detail="账号状态已变化，请重新验证密保后重试")
        conn.execute(
            "UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
            (password_hash, now, row["id"]),
        )
        conn.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
        conn.execute(
            "DELETE FROM auth_events WHERE kind='reset_fail_user' AND subject=?",
            (username_key,),
        )
        _purge_expired(conn, now)
        conn.commit()
    return _empty_no_store()


@app.post("/api/auth/delete", status_code=204)
def delete_account(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    data = _payload_dict(payload)
    now = int(time.time())
    with get_accounts_db() as conn:
        session = _require_user(conn, request, now)
        password = _validate_password_input(data.get("password"))
        _verify_current_password(conn, session, password, request, now)
        _begin_verified_account_write(conn, session)
        # 外键级联删除会话、密保与收藏。
        conn.execute("DELETE FROM users WHERE id=?", (session["user_id"],))
        conn.commit()
    response = _empty_no_store()
    _clear_session_cookie(response, request)
    return response


@app.get("/api/favorites")
def list_favorites(request: Request):
    now = int(time.time())
    with get_accounts_db() as conn:
        session = _require_user(conn, request, now)
        payload = _favorites_payload(conn, session["user_id"])
    return _no_store(payload)


@app.post("/api/favorites")
def add_favorite(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    data = _payload_dict(payload)
    course_id = data.get("id")
    if not isinstance(course_id, str) or not COURSE_ID_RE.fullmatch(course_id):
        raise HTTPException(status_code=422, detail="Invalid course id")
    now = int(time.time())
    ip_hash = _client_ip_hash(request)
    with get_accounts_db() as conn:
        session = _require_user(conn, request, now)
        _enforce_rate_limit(conn, "favorite_write_ip", ip_hash, now)
        _record_event(conn, "favorite_write_ip", ip_hash, now)
        conn.commit()
        snapshot = _favorite_snapshot(course_id)
        user_id = session["user_id"]
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM favorites WHERE user_id=? AND fav_key=?",
            (user_id, snapshot["fav_key"]),
        ).fetchone()
        if existing is None:
            count = conn.execute(
                "SELECT COUNT(*) FROM favorites WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            if count >= FAVORITES_MAX:
                conn.rollback()
                raise HTTPException(status_code=409, detail="Favorites limit reached")
        conn.execute(
            "INSERT INTO favorites (user_id, fav_key, course_id, term, term_label, level,"
            " course_code, class_no, teacher, course_name, credits, schedule, department,"
            " added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, fav_key) DO UPDATE SET"
            " course_id=excluded.course_id, term_label=excluded.term_label,"
            " course_name=excluded.course_name, credits=excluded.credits,"
            " schedule=excluded.schedule, department=excluded.department",
            (
                user_id, snapshot["fav_key"], snapshot["course_id"], snapshot["term"],
                snapshot["term_label"], snapshot["level"], snapshot["course_code"],
                snapshot["class_no"], snapshot["teacher"], snapshot["course_name"],
                snapshot["credits"], snapshot["schedule"], snapshot["department"], now,
            ),
        )
        default_id = _ensure_default_collection(conn, user_id, now)
        conn.execute(
            "INSERT OR IGNORE INTO favorite_collections (user_id, fav_key, collection_id, added_at)"
            " VALUES (?, ?, ?, ?)",
            (user_id, snapshot["fav_key"], default_id, now),
        )
        conn.commit()
        payload = _favorites_payload(conn, user_id)
    return _no_store(payload, status_code=200 if existing else 201)


@app.post("/api/favorites/remove")
def remove_favorite(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    data = _payload_dict(payload)
    fav_key = data.get("fav_key")
    if not _valid_text(fav_key) or not 1 <= len(fav_key) <= 300:
        raise _invalid_account_payload()
    now = int(time.time())
    with get_accounts_db() as conn:
        session = _require_user(conn, request, now)
        conn.execute(
            "DELETE FROM favorites WHERE user_id=? AND fav_key=?",
            (session["user_id"], fav_key),
        )
        conn.commit()
        payload = _favorites_payload(conn, session["user_id"])
    return _no_store(payload)


@app.post("/api/favorites/set-collections")
def set_favorite_collections(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    data = _payload_dict(payload)
    fav_key = data.get("fav_key")
    if not _valid_text(fav_key) or not 1 <= len(fav_key) <= 300:
        raise _invalid_account_payload()
    now = int(time.time())
    ip_hash = _client_ip_hash(request)
    with get_accounts_db() as conn:
        session = _require_user(conn, request, now)
        _enforce_rate_limit(conn, "favorite_write_ip", ip_hash, now)
        _record_event(conn, "favorite_write_ip", ip_hash, now)
        conn.commit()
        user_id = session["user_id"]
        conn.execute("BEGIN IMMEDIATE")
        collection_ids = _owned_collection_ids(conn, user_id, data.get("collection_ids"))
        exists = conn.execute(
            "SELECT 1 FROM favorites WHERE user_id=? AND fav_key=?", (user_id, fav_key)
        ).fetchone()
        if exists is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Favorite not found")
        conn.execute(
            "DELETE FROM favorite_collections WHERE user_id=? AND fav_key=?",
            (user_id, fav_key),
        )
        if collection_ids:
            conn.executemany(
                "INSERT OR IGNORE INTO favorite_collections"
                " (user_id, fav_key, collection_id, added_at) VALUES (?, ?, ?, ?)",
                [(user_id, fav_key, cid, now) for cid in collection_ids],
            )
        else:
            # 不属于任何收藏夹即取消收藏，复合外键会顺带清掉残留映射。
            conn.execute(
                "DELETE FROM favorites WHERE user_id=? AND fav_key=?", (user_id, fav_key)
            )
        conn.commit()
        result = _favorites_payload(conn, user_id)
    return _no_store(result)


@app.post("/api/collections", status_code=201)
def create_collection(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    data = _payload_dict(payload)
    name = _validate_collection_name(data.get("name"))
    now = int(time.time())
    ip_hash = _client_ip_hash(request)
    with get_accounts_db() as conn:
        session = _require_user(conn, request, now)
        _enforce_rate_limit(conn, "favorite_write_ip", ip_hash, now)
        _record_event(conn, "favorite_write_ip", ip_hash, now)
        conn.commit()
        user_id = session["user_id"]
        conn.execute("BEGIN IMMEDIATE")
        count = conn.execute(
            "SELECT COUNT(*) FROM collections WHERE user_id=? AND is_default=0", (user_id,)
        ).fetchone()[0]
        if count >= COLLECTIONS_MAX:
            conn.rollback()
            raise HTTPException(status_code=409, detail="Collections limit reached")
        position = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM collections WHERE user_id=?", (user_id,)
        ).fetchone()[0]
        try:
            conn.execute(
                "INSERT INTO collections (user_id, name, is_default, position, created_at)"
                " VALUES (?, ?, 0, ?, ?)",
                (user_id, name, position, now),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            raise HTTPException(status_code=409, detail="Collection name already exists")
        conn.commit()
        result = _favorites_payload(conn, user_id)
    return _no_store(result, status_code=201)


@app.post("/api/collections/rename")
def rename_collection(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    data = _payload_dict(payload)
    collection_id = _validate_collection_id(data.get("collection_id"))
    name = _validate_collection_name(data.get("name"))
    now = int(time.time())
    ip_hash = _client_ip_hash(request)
    with get_accounts_db() as conn:
        session = _require_user(conn, request, now)
        _enforce_rate_limit(conn, "favorite_write_ip", ip_hash, now)
        _record_event(conn, "favorite_write_ip", ip_hash, now)
        conn.commit()
        user_id = session["user_id"]
        conn.execute("BEGIN IMMEDIATE")
        owned = conn.execute(
            "SELECT 1 FROM collections WHERE id=? AND user_id=?", (collection_id, user_id)
        ).fetchone()
        if owned is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Collection not found")
        try:
            conn.execute(
                "UPDATE collections SET name=? WHERE id=? AND user_id=?",
                (name, collection_id, user_id),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            raise HTTPException(status_code=409, detail="Collection name already exists")
        conn.commit()
        result = _favorites_payload(conn, user_id)
    return _no_store(result)


@app.post("/api/collections/remove")
def remove_collection(request: Request, payload: dict = Body(...)):
    _require_trusted_origin(request)
    data = _payload_dict(payload)
    collection_id = _validate_collection_id(data.get("collection_id"))
    now = int(time.time())
    ip_hash = _client_ip_hash(request)
    with get_accounts_db() as conn:
        session = _require_user(conn, request, now)
        _enforce_rate_limit(conn, "favorite_write_ip", ip_hash, now)
        _record_event(conn, "favorite_write_ip", ip_hash, now)
        conn.commit()
        user_id = session["user_id"]
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT is_default FROM collections WHERE id=? AND user_id=?", (collection_id, user_id)
        ).fetchone()
        if row is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Collection not found")
        if row["is_default"]:
            conn.rollback()
            raise HTTPException(status_code=409, detail="Cannot delete default collection")
        conn.execute(
            "DELETE FROM collections WHERE id=? AND user_id=?", (collection_id, user_id)
        )
        # 删夹级联清该夹映射；仅存在于该夹的课程随之取消收藏。
        conn.execute(
            "DELETE FROM favorites WHERE user_id=? AND fav_key NOT IN"
            " (SELECT fav_key FROM favorite_collections WHERE user_id=?)",
            (user_id, user_id),
        )
        conn.commit()
        result = _favorites_payload(conn, user_id)
    return _no_store(result)


# Static files ------------------------------------------------------------------

@app.get("/api/health")
def get_health():
    try:
        payload = get_cached_database_health()
    except (RuntimeError, sqlite3.Error):
        return JSONResponse({"status": "error"}, status_code=503, headers={"Cache-Control": "no-store"})
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


app.mount("/Images", StaticFiles(directory=str(BASE_DIR / "Images")), name="images")


# 页面无 Cache-Control 时浏览器会按启发式缓存旧版；no-cache 强制每次回源验证,
# 未变化时命中 ETag 返回 304,保证部署后用户刷新即见新版。
PAGE_CACHE_HEADERS = {"Cache-Control": "no-cache"}


@app.get("/")
def root(request: Request = None):
    record_visit(request)
    return FileResponse(BASE_DIR / "index.html", headers=PAGE_CACHE_HEADERS)


@app.get("/reviews", include_in_schema=False)
def reviews_page(request: Request = None):
    record_visit(request)
    return FileResponse(BASE_DIR / "reviews.html", headers=PAGE_CACHE_HEADERS)
