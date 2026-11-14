"""SQLite storage for the MesoScope web app.

One file, no ORM: the schema is small and the demo must start with zero setup.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL DEFAULT '',
    institution   TEXT NOT NULL DEFAULT '',
    research_area TEXT NOT NULL DEFAULT '',
    bio           TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_fk    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS papers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_key     TEXT NOT NULL UNIQUE,
    paper_id      INTEGER NOT NULL,
    owner_fk      INTEGER REFERENCES users(id) ON DELETE CASCADE,
    title         TEXT NOT NULL DEFAULT '',
    original_title TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    summary_brief TEXT NOT NULL DEFAULT '',
    summary_engine TEXT NOT NULL DEFAULT '',
    summary_at    TEXT NOT NULL DEFAULT '',
    filename      TEXT NOT NULL DEFAULT '',
    pdf_path      TEXT NOT NULL DEFAULT '',
    pdf_blob      BLOB,
    pages         INTEGER NOT NULL DEFAULT 0,
    chars         INTEGER NOT NULL DEFAULT 0,
    n_tables      INTEGER NOT NULL DEFAULT 0,
    content_hash  TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT 'upload',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_pages (
    paper_fk  INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    page      INTEGER NOT NULL,
    text      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (paper_fk, page)
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_fk     INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'queued',
    stage        TEXT NOT NULL DEFAULT 'queued',
    progress     INTEGER NOT NULL DEFAULT 0,
    mode         TEXT NOT NULL DEFAULT 'anchored',
    engine       TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    message      TEXT NOT NULL DEFAULT '',
    n_records    INTEGER NOT NULL DEFAULT 0,
    usage_json   TEXT NOT NULL DEFAULT '{}',
    issues_json  TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_fk        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    job_fk          INTEGER,
    record_id       TEXT NOT NULL UNIQUE,
    record_index    INTEGER NOT NULL DEFAULT 1,
    fields_json     TEXT NOT NULL DEFAULT '{}',
    evidence_json   TEXT NOT NULL DEFAULT '{}',
    confidence_json TEXT NOT NULL DEFAULT '{}',
    reviewed        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    record_fk  INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_paper ON records(paper_fk);
CREATE INDEX IF NOT EXISTS idx_jobs_paper ON jobs(paper_fk);
CREATE INDEX IF NOT EXISTS idx_pages_paper ON paper_pages(paper_fk);
"""

# 旧库升级：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，
# 这里按「列名 -> ALTER 语句」做一次幂等迁移。
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("papers", "content_hash", "ALTER TABLE papers ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''"),
    # 自定义命名与备注：title 可被用户改写，original_title 保留入库时的原始名
    ("papers", "original_title", "ALTER TABLE papers ADD COLUMN original_title TEXT NOT NULL DEFAULT ''"),
    ("papers", "note", "ALTER TABLE papers ADD COLUMN note TEXT NOT NULL DEFAULT ''"),
    # 内容总结：brief 供悬停速览，summary 为完整版
    ("papers", "summary", "ALTER TABLE papers ADD COLUMN summary TEXT NOT NULL DEFAULT ''"),
    ("papers", "summary_brief", "ALTER TABLE papers ADD COLUMN summary_brief TEXT NOT NULL DEFAULT ''"),
    ("papers", "summary_engine", "ALTER TABLE papers ADD COLUMN summary_engine TEXT NOT NULL DEFAULT ''"),
    ("papers", "summary_at", "ALTER TABLE papers ADD COLUMN summary_at TEXT NOT NULL DEFAULT ''"),
    # 账号体系：论文归属到用户；PDF 正文二进制入库，便于跨设备共享
    ("papers", "owner_fk", "ALTER TABLE papers ADD COLUMN owner_fk INTEGER REFERENCES users(id)"),
    ("papers", "pdf_blob", "ALTER TABLE papers ADD COLUMN pdf_blob BLOB"),
)

POST_MIGRATION_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_papers_hash ON papers(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_papers_owner ON papers(owner_fk)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_fk)",
)

_LOCK = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns missing from databases created by earlier versions."""
        for table, column, statement in MIGRATIONS:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(statement)
        for statement in POST_MIGRATION_SQL:
            conn.execute(statement)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Serialized connection.

        SQLite under a threaded uvicorn worker plus a background extraction
        thread is only safe with one writer at a time, so every call takes the
        process lock.  The demo workload is tiny and this removes every
        "database is locked" failure mode.
        """
        with _LOCK:
            conn = sqlite3.connect(self.path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.connect() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid or 0)


def loads(raw: Any, fallback: Any) -> Any:
    if raw in (None, ""):
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
