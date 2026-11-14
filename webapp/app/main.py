"""MesoScope FastAPI application.

Endpoints are grouped by the four product modules:
  papers / jobs   -> library and extraction tasks
  records         -> review workbench (read, edit, export)
  search          -> cross-paper structured query
  ask             -> citation-grounded QA
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import queue
import re
import sqlite3
import threading
import traceback
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .db import Database, dumps, loads, now
from .auth import (
    Accounts,
    AuthError,
    SESSION_COOKIE,
    SESSION_DAYS,
    user_payload,
)
from .demo import DEMO_PAPERS
from .pdfgen import render_demo_pdf
from . import pagerender
from .extraction import (
    EXTRACTION_MODES,
    FIELD_KEYS,
    FIELD_LABELS,
    MODE_VALUES,
    PDFError,
    llm_status,
    load_paper,
    run_extraction,
    verify_evidence,
)
from .qa import Retriever, build_answer, citations_of, stream_answer
from .translate import (
    TranslateError,
    available_providers as translate_providers,
    stream_translate,
    translate as translate_text,
)
from .summarize import build_summary
from .settings import (
    SettingsError,
    apply_profile,
    clear_key,
    delete_profile,
    list_profiles,
    read_settings,
    save_settings,
    test_connection,
)

ROOT = Path(__file__).resolve().parents[2]
WEBAPP = ROOT / "webapp"
DATA_DIR = WEBAPP / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
STATIC_DIR = WEBAPP / "static"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
db = Database(DATA_DIR / "mesoscope.db")

app = FastAPI(title="MesoScope", version="1.0.0")
_JOB_LOCK = threading.Lock()

# 流式响应：关闭代理缓冲，否则 SSE 会被攒到最后一次性吐出
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _sse(event: str, payload: dict[str, Any]) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# 预翻译：抽取完成后在后台把每页译好存进数据库。
# 只用一个工作线程，既不会和抽取抢带宽，也不会堆积大量并发请求；
# 译文写入 paper_pages.zh_text，不常驻内存，所以不会让进程越跑越大。
_PREWARM_QUEUE: "queue.Queue[int]" = queue.Queue()
_PREWARM_SEEN: set[int] = set()
_PREWARM_LOCK = threading.Lock()
_PREWARM_WORKER: threading.Thread | None = None


def _prewarm_worker() -> None:
    while True:
        paper_fk = _PREWARM_QUEUE.get()
        try:
            _prewarm_paper(paper_fk)
        except Exception:  # noqa: BLE001 - a failed page must not kill the worker
            traceback.print_exc()
        finally:
            with _PREWARM_LOCK:
                _PREWARM_SEEN.discard(paper_fk)
            _PREWARM_QUEUE.task_done()


def _prewarm_paper(paper_fk: int) -> None:
    """Translate the pages of one paper that have no Chinese text yet."""
    if not llm_status()["available"]:
        return
    pages = db.query(
        "SELECT page, text FROM paper_pages"
        " WHERE paper_fk = ? AND zh_text = '' ORDER BY page",
        (paper_fk,),
    )
    for row in pages:
        source = (row["text"] or "").strip()
        if len(source) < 40:
            continue
        try:
            result = translate_text(source)
        except TranslateError:
            # 配额/网络问题：留给用户手动触发，不反复重试刷爆接口
            return
        db.execute(
            "UPDATE paper_pages SET zh_text = ?, zh_at = ? WHERE paper_fk = ? AND page = ?",
            (result["text"], now(), paper_fk, row["page"]),
        )


def _schedule_prewarm(paper_fk: int) -> None:
    """Queue background translation; silently ignores duplicates."""
    global _PREWARM_WORKER
    with _PREWARM_LOCK:
        if paper_fk in _PREWARM_SEEN:
            return
        _PREWARM_SEEN.add(paper_fk)
        if _PREWARM_WORKER is None or not _PREWARM_WORKER.is_alive():
            _PREWARM_WORKER = threading.Thread(
                target=_prewarm_worker, name="translate-prewarm", daemon=True
            )
            _PREWARM_WORKER.start()
    _PREWARM_QUEUE.put(paper_fk)
accounts = Accounts(db)


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #

def _token_of(request: Request) -> str | None:
    """Session token from the cookie, or the Authorization header."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return cookie
    header = request.headers.get("authorization") or ""
    return header[7:].strip() if header.lower().startswith("bearer ") else None


def current_user(request: Request) -> dict[str, Any]:
    """Resolve the signed-in user or fail with 401."""
    row = accounts.user_for_token(_token_of(request))
    if not row:
        raise HTTPException(401, "请先登录")
    return row


def optional_user(request: Request) -> dict[str, Any] | None:
    return accounts.user_for_token(_token_of(request))


def _set_session_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        path="/",
    )


@app.post("/api/auth/register")
async def api_register(request: Request) -> JSONResponse:
    """Create an account and sign in immediately.

    The very first account also adopts any papers that predate the account
    system, so an existing single-user library is not orphaned by the upgrade.
    Later accounts start empty.
    """
    body = await request.json()
    first_account = not db.one("SELECT id FROM users LIMIT 1")
    try:
        user = accounts.register(body)
    except AuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    adopted = 0
    if first_account:
        adopted = db.execute(
            "UPDATE papers SET owner_fk = ? WHERE owner_fk IS NULL", (user["id"],)
        )
        adopted = int(
            (db.one("SELECT COUNT(*) AS n FROM papers WHERE owner_fk = ?", (user["id"],)) or {})
            .get("n")
            or 0
        )

    token = accounts.open_session(user["id"])
    response = JSONResponse({"user": user, "token": token, "adopted_papers": adopted})
    _set_session_cookie(response, token)
    return response


@app.post("/api/auth/login")
async def api_login(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        row = accounts.login(body)
    except AuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    token = accounts.open_session(row["id"])
    response = JSONResponse({"user": user_payload(row), "token": token})
    _set_session_cookie(response, token)
    return response


@app.post("/api/auth/logout")
async def api_logout(request: Request) -> JSONResponse:
    accounts.close_session(_token_of(request) or "")
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/me")
def api_me(request: Request) -> dict[str, Any]:
    """Who am I — used by the front-end on boot to restore the session."""
    row = optional_user(request)
    if not row:
        return {"user": None}
    return {"user": user_payload(row), "stats": accounts.stats(row["id"])}


@app.put("/api/auth/profile")
async def api_update_profile(request: Request) -> dict[str, Any]:
    user = current_user(request)
    body = await request.json()
    try:
        return {"user": accounts.update_profile(user["id"], body)}
    except AuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@app.put("/api/auth/password")
async def api_change_password(request: Request) -> JSONResponse:
    """Change the password and re-issue this device's session."""
    user = current_user(request)
    body = await request.json()
    try:
        accounts.change_password(user["id"], body)
    except AuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    token = accounts.open_session(user["id"])
    response = JSONResponse({"ok": True, "token": token})
    _set_session_cookie(response, token)
    return response


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _next_paper_id() -> int:
    row = db.one("SELECT MAX(paper_id) AS top FROM papers")
    return int((row or {}).get("top") or 0) + 1


def _safe_title(filename: str) -> str:
    stem = Path(filename).stem.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", stem).strip()[:200] or "未命名论文"


def _owned_paper_row(paper_fk: int, user: dict[str, Any]) -> dict[str, Any]:
    """A paper the signed-in user owns; other people's papers read as 404.

    The shared corpus is only ever reached through the QA retriever, never
    through these per-paper endpoints.
    """
    row = db.one(
        "SELECT * FROM papers WHERE id = ? AND owner_fk = ?", (paper_fk, user["id"])
    )
    if not row:
        raise HTTPException(404, "论文不存在，或不属于当前账号")
    return row


def _paper_payload(row: dict[str, Any]) -> dict[str, Any]:
    stats = db.one(
        """
        SELECT
          (SELECT COUNT(*) FROM records WHERE paper_fk = ?) AS n_records,
          (SELECT COUNT(*) FROM records WHERE paper_fk = ? AND reviewed = 1) AS n_reviewed
        """,
        (row["id"], row["id"]),
    ) or {}
    job = db.one(
        "SELECT id, status, stage, progress, engine, model, message, n_records, created_at,"
        " finished_at, issues_json FROM jobs WHERE paper_fk = ? ORDER BY id DESC LIMIT 1",
        (row["id"],),
    )
    return {
        "id": row["id"],
        "paper_key": row["paper_key"],
        "paper_id": row["paper_id"],
        "title": row["title"],
        # 自定义命名后仍要能看到入库时的原始名，便于核对是哪份 PDF
        "original_title": row.get("original_title") or row["title"],
        "renamed": bool(
            (row.get("original_title") or "") and row["title"] != row.get("original_title")
        ),
        "note": row.get("note") or "",
        "summary": row.get("summary") or "",
        "summary_brief": row.get("summary_brief") or "",
        "summary_engine": row.get("summary_engine") or "",
        "summary_at": row.get("summary_at") or "",
        "filename": row["filename"],
        "pages": row["pages"],
        "chars": row["chars"],
        "n_tables": row["n_tables"],
        "source": row["source"],
        "created_at": row["created_at"],
        "folder_fk": row.get("folder_fk"),
        "n_records": int(stats.get("n_records") or 0),
        "n_reviewed": int(stats.get("n_reviewed") or 0),
        "latest_job": _job_payload(job) if job else None,
    }


def _job_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "stage": row["stage"],
        "progress": row["progress"],
        "engine": row.get("engine", ""),
        "model": row.get("model", ""),
        "message": row.get("message", ""),
        "n_records": row.get("n_records", 0),
        "issues": loads(row.get("issues_json"), []),
        "created_at": row.get("created_at", ""),
        "finished_at": row.get("finished_at", ""),
    }


def _record_payload(row: dict[str, Any], paper: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "id": row["id"],
        "record_id": row["record_id"],
        "record_index": row["record_index"],
        "paper_fk": row["paper_fk"],
        "fields": loads(row["fields_json"], {}),
        "evidence": loads(row["evidence_json"], {}),
        "confidence": loads(row["confidence_json"], {}),
        "reviewed": bool(row["reviewed"]),
        "updated_at": row["updated_at"],
    }
    if paper:
        payload["paper_key"] = paper["paper_key"]
        payload["paper_title"] = paper["title"]
        payload["paper_id"] = paper["paper_id"]
    return payload


def _store_pages(
    paper_fk: int,
    pages: list[str],    tables: list[Any] | None = None,
) -> None:
    """Persist page text plus the structured tables found on each page.

    Tables are kept as rows/columns (not the pipe-joined text that goes to the
    LLM) so the review panel can render a real HTML table.
    """
    by_page: dict[int, list[dict[str, Any]]] = {}
    for table in tables or []:
        by_page.setdefault(table.page, []).append(
            {"table_id": table.table_id, "rows": table.rows}
        )

    with db.connect() as conn:
        conn.execute("DELETE FROM paper_pages WHERE paper_fk = ?", (paper_fk,))
        conn.executemany(
            "INSERT INTO paper_pages (paper_fk, page, text, tables_json) VALUES (?, ?, ?, ?)",
            [
                (paper_fk, index, text, dumps(by_page.get(index, [])))
                for index, text in enumerate(pages, 1)
            ],
        )


def _materialize_pdf(row: dict[str, Any]) -> Path:
    """Local path for the PDF, writing the database blob out when needed.

    The loader works on files, while the canonical copy now lives in the
    database so it follows the account across devices.
    """
    target = UPLOAD_DIR / f"{row['paper_key']}.pdf"
    blob = row.get("pdf_blob")
    if blob:
        payload = bytes(blob)
        if not target.exists() or target.stat().st_size != len(payload):
            target.write_bytes(payload)
        return target

    legacy = Path(row.get("pdf_path") or "")
    if legacy and not legacy.is_absolute():
        legacy = ROOT / legacy
    if legacy and legacy.exists():
        return legacy
    raise RuntimeError("PDF 文件不存在，无法解析")


def _refresh_summary(
    paper_fk: int,
    title: str,
    pages: list[str],
    records: list[dict[str, Any]],
) -> dict[str, str]:
    """Build the content summary and store it on the paper.

    Shared by the extraction job (automatic) and the manual refresh endpoint,
    so both paths always produce the same thing.
    """
    result = build_summary(title=title, pages=pages, records=records)
    db.execute(
        "UPDATE papers SET summary = ?, summary_brief = ?, summary_engine = ?,"
        " summary_at = ? WHERE id = ?",
        (result["summary"], result["brief"], result["engine"], now(), paper_fk),
    )
    return result


# --------------------------------------------------------------------------- #
# papers
# --------------------------------------------------------------------------- #

@app.get("/api/status")
def api_status(request: Request) -> dict[str, Any]:
    """Engine state plus counts.

    Counts are per-account (what the library shows), except ``shared_papers``
    which reports the size of the corpus the assistant can draw on.
    """
    status = llm_status()
    user = optional_user(request)
    if user:
        counts = db.one(
            "SELECT (SELECT COUNT(*) FROM papers WHERE owner_fk = ?) AS papers,"
            " (SELECT COUNT(*) FROM records JOIN papers ON papers.id = records.paper_fk"
            "   WHERE papers.owner_fk = ?) AS records,"
            " (SELECT COUNT(*) FROM records JOIN papers ON papers.id = records.paper_fk"
            "   WHERE papers.owner_fk = ? AND records.reviewed = 1) AS reviewed,"
            " (SELECT COUNT(*) FROM jobs JOIN papers ON papers.id = jobs.paper_fk"
            "   WHERE papers.owner_fk = ? AND jobs.status = 'running') AS running",
            (user["id"], user["id"], user["id"], user["id"]),
        ) or {}
    else:
        counts = {"papers": 0, "records": 0, "reviewed": 0, "running": 0}
    shared = db.one("SELECT COUNT(*) AS n FROM papers") or {}
    return {
        "llm": status,
        "counts": {key: int(value or 0) for key, value in counts.items()},
        "shared_papers": int(shared.get("n") or 0),
        "user": user_payload(user) if user else None,
        "fields": [{"key": key, "label": FIELD_LABELS[key]} for key in FIELD_KEYS],
        "modes": EXTRACTION_MODES,
    }


# --------------------------------------------------------------------------- #
# in-app LLM settings
# --------------------------------------------------------------------------- #

@app.get("/api/settings/llm")
def api_get_llm_settings() -> dict[str, Any]:
    """Current credentials (key redacted) plus the provider presets."""
    return read_settings()


@app.put("/api/settings/llm")
async def api_put_llm_settings(request: Request) -> dict[str, Any]:
    """Save credentials to .env and apply them without a restart.

    ``test`` (default true) verifies the endpoint before persisting, so a typo
    cannot silently disable the LLM engine.
    """
    body = await request.json()
    api_key = body.get("api_key")
    base_url = str(body.get("base_url") or "")
    model = str(body.get("model") or "")
    should_test = body.get("test", True)

    probe: dict[str, Any] | None = None
    if should_test:
        probe = test_connection(api_key=api_key, base_url=base_url, model=model)
        if not probe["ok"]:
            raise HTTPException(400, probe["message"])

    try:
        settings = save_settings(
            api_key=api_key,
            base_url=base_url,
            model=model,
            provider=str(body.get("provider") or ""),
        )
    except SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"写入配置文件失败：{exc}") from exc

    return {"settings": settings, "llm": llm_status(), "test": probe}


@app.get("/api/settings/llm/profiles")
def api_list_profiles() -> dict[str, Any]:
    """Previously used connections, with every key redacted."""
    return {"profiles": list_profiles()}


@app.post("/api/settings/llm/profiles/{profile_id}/apply")
def api_apply_profile(profile_id: str, test: bool = True) -> dict[str, Any]:
    """Switch back to a saved connection without retyping the key."""
    try:
        result = apply_profile(profile_id, test=test)
    except SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"写入配置文件失败：{exc}") from exc
    return {**result, "llm": llm_status()}


@app.delete("/api/settings/llm/profiles/{profile_id}")
def api_delete_profile(profile_id: str) -> dict[str, Any]:
    """Forget one saved connection; the active settings are untouched."""
    try:
        profiles = delete_profile(profile_id)
    except SettingsError as exc:
        raise HTTPException(404, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"写入配置文件失败：{exc}") from exc
    return {"profiles": profiles}


@app.post("/api/settings/llm/test")
async def api_test_llm_settings(request: Request) -> dict[str, Any]:
    """Verify endpoint/key/model without saving anything."""
    body = await request.json()
    return test_connection(
        api_key=body.get("api_key"),
        base_url=body.get("base_url"),
        model=body.get("model"),
    )


@app.delete("/api/settings/llm")
def api_delete_llm_settings() -> dict[str, Any]:
    """Forget the stored key and fall back to the rule engine."""
    try:
        settings = clear_key()
    except OSError as exc:
        raise HTTPException(500, f"写入配置文件失败：{exc}") from exc
    return {"settings": settings, "llm": llm_status()}


@app.get("/api/papers")
def api_papers(request: Request) -> dict[str, Any]:
    """Only the signed-in user's own library."""
    user = current_user(request)
    rows = db.query(
        "SELECT * FROM papers WHERE owner_fk = ? ORDER BY paper_id", (user["id"],)
    )
    return {"papers": [_paper_payload(row) for row in rows]}


# --------------------------------------------------------------------------- #
# folders
# --------------------------------------------------------------------------- #

def _folder_row(folder_fk: int, user: dict[str, Any]) -> dict[str, Any]:
    row = db.one(
        "SELECT * FROM folders WHERE id = ? AND owner_fk = ?", (folder_fk, user["id"])
    )
    if not row:
        raise HTTPException(404, "文件夹不存在，或不属于当前账号")
    return row


def _clean_folder_name(raw: Any) -> str:
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not name:
        raise HTTPException(400, "文件夹名称不能为空")
    if len(name) > 60:
        raise HTTPException(400, "文件夹名称请控制在 60 个字符以内")
    return name


@app.get("/api/folders")
def api_folders(request: Request) -> dict[str, Any]:
    """Folders with how many papers each holds."""
    user = current_user(request)
    rows = db.query(
        """
        SELECT folders.*,
               (SELECT COUNT(*) FROM papers WHERE papers.folder_fk = folders.id) AS n_papers
        FROM folders WHERE owner_fk = ? ORDER BY name
        """,
        (user["id"],),
    )
    loose = db.one(
        "SELECT COUNT(*) AS n FROM papers WHERE owner_fk = ? AND folder_fk IS NULL",
        (user["id"],),
    ) or {}
    return {
        "folders": [
            {
                "id": row["id"],
                "name": row["name"],
                "n_papers": int(row.get("n_papers") or 0),
                "created_at": row["created_at"],
            }
            for row in rows
        ],
        "unfiled": int(loose.get("n") or 0),
    }


@app.post("/api/folders")
async def api_create_folder(request: Request) -> dict[str, Any]:
    user = current_user(request)
    body = await request.json()
    name = _clean_folder_name(body.get("name"))
    try:
        folder_fk = db.execute(
            "INSERT INTO folders (owner_fk, name, created_at) VALUES (?, ?, ?)",
            (user["id"], name, now()),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"已存在同名文件夹：{name}") from exc
    return {"id": folder_fk, "name": name, "n_papers": 0}


@app.put("/api/folders/{folder_fk}")
async def api_rename_folder(folder_fk: int, request: Request) -> dict[str, Any]:
    user = current_user(request)
    _folder_row(folder_fk, user)
    body = await request.json()
    name = _clean_folder_name(body.get("name"))
    try:
        db.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_fk))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"已存在同名文件夹：{name}") from exc
    return {"id": folder_fk, "name": name}


@app.delete("/api/folders/{folder_fk}")
def api_delete_folder(folder_fk: int, request: Request) -> dict[str, Any]:
    """Remove a folder; its papers go back to 未归档, they are never deleted."""
    user = current_user(request)
    _folder_row(folder_fk, user)
    with db.connect() as conn:
        conn.execute(
            "UPDATE papers SET folder_fk = NULL WHERE folder_fk = ? AND owner_fk = ?",
            (folder_fk, user["id"]),
        )
        conn.execute("DELETE FROM folders WHERE id = ?", (folder_fk,))
    return {"ok": True}


@app.post("/api/papers/move")
async def api_move_papers(request: Request) -> dict[str, Any]:
    """Move papers into a folder, or out of any folder when folder_fk is null."""
    user = current_user(request)
    body = await request.json()
    ids = body.get("paper_fks") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "请先选择要归纳的论文")

    raw_target = body.get("folder_fk")
    target: int | None = None
    if raw_target not in (None, "", 0, "0"):
        target = int(raw_target)
        _folder_row(target, user)

    moved = 0
    with db.connect() as conn:
        for raw in ids:
            try:
                paper_fk = int(raw)
            except (TypeError, ValueError):
                continue
            cursor = conn.execute(
                "UPDATE papers SET folder_fk = ? WHERE id = ? AND owner_fk = ?",
                (target, paper_fk, user["id"]),
            )
            moved += cursor.rowcount or 0
    return {"moved": moved, "folder_fk": target}


@app.get("/api/papers/{paper_fk}")
def api_paper(paper_fk: int, request: Request) -> dict[str, Any]:
    user = current_user(request)
    row = _owned_paper_row(paper_fk, user)
    payload = _paper_payload(row)
    records = db.query(
        "SELECT * FROM records WHERE paper_fk = ? ORDER BY record_index", (paper_fk,)
    )
    payload["records"] = [_record_payload(item, row) for item in records]
    payload["jobs"] = [
        _job_payload(job)
        for job in db.query("SELECT * FROM jobs WHERE paper_fk = ? ORDER BY id DESC", (paper_fk,))
    ]
    return payload


@app.get("/api/papers/{paper_fk}/pdf")
def api_paper_pdf(paper_fk: int, request: Request):
    """Serve the PDF from the database, falling back to the legacy file path."""
    user = current_user(request)
    row = _owned_paper_row(paper_fk, user)
    blob = row.get("pdf_blob")
    headers = {"Content-Disposition": f'inline; filename="{row["paper_key"]}.pdf"'}
    if blob:
        return Response(bytes(blob), media_type="application/pdf", headers=headers)

    # Papers uploaded before PDFs moved into the database still live on disk.
    path = Path(row["pdf_path"] or "")
    if path and not path.is_absolute():
        path = ROOT / path
    if path and path.exists():
        return FileResponse(path, media_type="application/pdf", filename=row["filename"])
    raise HTTPException(404, "PDF 文件不存在（可能是演示数据，无原始文件）")


@app.get("/api/papers/{paper_fk}/pages/{page}")
def api_paper_page(paper_fk: int, page: int, request: Request) -> dict[str, Any]:
    user = current_user(request)
    _owned_paper_row(paper_fk, user)
    row = db.one(
        "SELECT text, tables_json FROM paper_pages WHERE paper_fk = ? AND page = ?",
        (paper_fk, page),
    )
    if not row:
        raise HTTPException(404, "该页文本不存在")
    return {
        "page": page,
        "text": row["text"],
        "tables": loads(row.get("tables_json"), []),
    }


@app.get("/api/papers/{paper_fk}/pages/{page}/image")
def api_paper_page_image(
    paper_fk: int,
    page: int,
    request: Request,
    dpi: int = Query(pagerender.DEFAULT_DPI, ge=72, le=300),
):
    """Rasterised page, so the reviewer sees the real layout and figures."""
    user = current_user(request)
    row = _owned_paper_row(paper_fk, user)
    try:
        pdf_path = _materialize_pdf(row)
        image = pagerender.render_page(pdf_path, page, dpi=dpi)
    except (RuntimeError, pagerender.PageRenderError) as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(
        image,
        media_type="image/png",
        # Cache key already encodes file mtime and dpi, so this is safe.
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.get("/api/papers/{paper_fk}/pages/{page}/locate")
def api_locate_quote(
    paper_fk: int,
    page: int,
    request: Request,
    quote: str = Query("", description="要在该页定位的引文"),
) -> dict[str, Any]:
    """Relative bounding boxes for one quote, used to draw the highlight."""
    user = current_user(request)
    row = _owned_paper_row(paper_fk, user)
    if not quote.strip():
        return {"found": False, "boxes": [], "mode": "empty"}
    try:
        pdf_path = _materialize_pdf(row)
        result = pagerender.locate_quote(pdf_path, page, quote)
        result["size"] = pagerender.page_size(pdf_path, page)
    except (RuntimeError, pagerender.PageRenderError) as exc:
        raise HTTPException(404, str(exc)) from exc
    return result


# --------------------------------------------------------------------------- #
# translation
# --------------------------------------------------------------------------- #

@app.get("/api/translate/providers")
def api_translate_providers(request: Request) -> dict[str, Any]:
    """Which translation backends are usable right now."""
    current_user(request)
    return {"providers": translate_providers()}


@app.post("/api/translate")
async def api_translate(request: Request) -> dict[str, Any]:
    """Translate an arbitrary English snippet into Chinese."""
    current_user(request)
    body = await request.json()
    try:
        return translate_text(str(body.get("text") or ""), body.get("provider"))
    except TranslateError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/papers/{paper_fk}/pages/{page}/translate")
def api_translate_page(
    paper_fk: int,
    page: int,
    request: Request,
    provider: str = Query("", description="翻译服务：llm / mymemory / deepl"),
) -> dict[str, Any]:
    """Chinese translation of one page's text layer."""
    user = current_user(request)
    _owned_paper_row(paper_fk, user)
    row = db.one(
        "SELECT text, zh_text FROM paper_pages WHERE paper_fk = ? AND page = ?",
        (paper_fk, page),
    )
    if not row:
        raise HTTPException(404, "该页文本不存在")
    # 抽取后后台已预翻译过的页面直接返回，无需再等模型
    if (row.get("zh_text") or "").strip():
        return {
            "text": row["zh_text"],
            "cached": True,
            "truncated": False,
            "provider": "prewarmed",
            "page": page,
        }
    try:
        result = translate_text(row["text"], provider or None)
    except TranslateError as exc:
        raise HTTPException(400, str(exc)) from exc
    result["page"] = page
    return result


@app.post("/api/papers/{paper_fk}/pages/{page}/translate-region")
async def api_translate_region(
    paper_fk: int, page: int, request: Request
) -> dict[str, Any]:
    """Translate only the text inside a user-drawn rectangle.

    The body carries the selection as 0..1 fractions of the page
    (``{"rect": {"x":, "y":, "w":, "h":}}``), matching what the front-end draws.
    """
    user = current_user(request)
    row = _owned_paper_row(paper_fk, user)
    body = await request.json()
    rect = body.get("rect") or {}
    if not isinstance(rect, dict):
        raise HTTPException(400, "选区参数不合法")

    try:
        pdf_path = _materialize_pdf(row)
        source = pagerender.text_in_rect(pdf_path, page, rect)
    except (RuntimeError, pagerender.PageRenderError) as exc:
        raise HTTPException(400, str(exc)) from exc

    if not source.strip():
        raise HTTPException(400, "选区内没有可识别的文字，请重新框选（图片区域无法翻译）")

    try:
        result = translate_text(source, body.get("provider"))
    except TranslateError as exc:
        raise HTTPException(400, str(exc)) from exc
    result["source"] = source
    result["page"] = page
    return result


@app.post("/api/papers")
async def api_upload(
    request: Request,
    file: UploadFile = File(...),
    allow_duplicate: bool = Query(False, description="为 true 时跳过重复校验，强制再传一份"),
) -> dict[str, Any]:
    user = current_user(request)
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "只接受 PDF 文件")

    payload = await file.read()
    if not payload:
        raise HTTPException(400, "文件内容为空")
    content_hash = hashlib.sha256(payload).hexdigest()

    # 重复判定只在当前账号的文献库内进行：别人上传过同一篇，不该阻止我也收录一份，
    # 也不该把别人的记录暴露给我。
    if not allow_duplicate:
        existing = db.one(
            "SELECT * FROM papers WHERE owner_fk = ? AND content_hash = ?"
            " AND content_hash != '' ORDER BY id LIMIT 1",
            (user["id"], content_hash),
        )
        reason = "content"
        if not existing:
            existing = db.one(
                "SELECT * FROM papers WHERE owner_fk = ? AND filename = ? ORDER BY id LIMIT 1",
                (user["id"], file.filename),
            )
            reason = "filename"
        if existing:
            raise HTTPException(
                409,
                {
                    "code": "duplicate_paper",
                    "reason": reason,
                    "filename": file.filename,
                    "existing": _paper_payload(existing),
                    "message": f"「{existing['title'] or file.filename}」已存在，是否仍要重复上传？",
                },
            )

    paper_id = _next_paper_id()
    paper_key = f"paper_{paper_id:03d}"
    # 解析需要一个真实文件路径，用临时文件；正文随后以 BLOB 入库，
    # 这样换设备登录也能取到原始 PDF。
    target = UPLOAD_DIR / f"{paper_key}.pdf"
    target.write_bytes(payload)

    try:
        paper = load_paper(target)
    except PDFError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(400, f"PDF 解析失败：{exc}") from exc

    stats = paper.stats()
    title = _safe_title(file.filename)
    paper_fk = db.execute(
        """
        INSERT INTO papers (paper_key, paper_id, owner_fk, title, original_title, filename,
                            pdf_path, pdf_blob, pages, chars, n_tables, content_hash,
                            source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'upload', ?)
        """,
        (
            paper_key,
            paper_id,
            user["id"],
            title,
            title,
            file.filename,
            str(target.relative_to(ROOT)).replace("\\", "/"),
            sqlite3.Binary(payload),
            int(stats["pages"]),
            int(stats["chars"]),
            int(stats["tables"]),
            content_hash,
            now(),
        ),
    )
    _store_pages(paper_fk, paper.pages_with_tables, paper.tables)
    return _paper_payload(_owned_paper_row(paper_fk, user))


TITLE_MAX = 200
NOTE_MAX = 2000


@app.patch("/api/papers/{paper_fk}")
async def api_rename_paper(paper_fk: int, request: Request) -> dict[str, Any]:
    """Custom display name and free-form note for one paper.

    ``title`` empty (or omitted together with a ``reset_title`` flag) restores
    the name produced at import time, so a rename is never destructive.
    """
    row = _owned_paper_row(paper_fk, current_user(request))
    body = await request.json()

    original = row.get("original_title") or row["title"]
    updates: dict[str, Any] = {}

    if body.get("reset_title"):
        updates["title"] = original
    elif "title" in body:
        title = re.sub(r"\s+", " ", str(body.get("title") or "")).strip()[:TITLE_MAX]
        updates["title"] = title or original

    if "note" in body:
        updates["note"] = str(body.get("note") or "").strip()[:NOTE_MAX]

    if not updates:
        raise HTTPException(400, "没有需要更新的字段（title / note）")

    assignments = ", ".join(f"{key} = ?" for key in updates)
    db.execute(
        f"UPDATE papers SET {assignments}, original_title = ? WHERE id = ?",
        (*updates.values(), original, paper_fk),
    )
    return _paper_payload(db.one("SELECT * FROM papers WHERE id = ?", (paper_fk,)) or {})


@app.post("/api/papers/{paper_fk}/summary")
def api_build_summary(
    paper_fk: int, request: Request, refresh: bool = Query(True)
) -> dict[str, Any]:
    """Generate (or return the cached) content summary for one paper."""
    user = current_user(request)
    row = _owned_paper_row(paper_fk, user)
    if not refresh and (row.get("summary") or ""):
        return _paper_payload(row)

    pages = [
        item["text"]
        for item in db.query(
            "SELECT text FROM paper_pages WHERE paper_fk = ? ORDER BY page", (paper_fk,)
        )
    ]
    if not any((page or "").strip() for page in pages):
        raise HTTPException(400, "这篇论文还没有可用的正文文本，无法生成总结")

    records = [
        _record_payload(item)
        for item in db.query(
            "SELECT * FROM records WHERE paper_fk = ? ORDER BY record_index", (paper_fk,)
        )
    ]
    _refresh_summary(paper_fk, row["title"], pages, records)
    return _paper_payload(_owned_paper_row(paper_fk, user))


@app.delete("/api/papers/{paper_fk}")
def api_delete_paper(paper_fk: int, request: Request) -> dict[str, Any]:
    row = _owned_paper_row(paper_fk, current_user(request))
    db.execute("DELETE FROM papers WHERE id = ?", (paper_fk,))
    path = ROOT / (row["pdf_path"] or "")
    if row["source"] == "upload" and path.exists() and UPLOAD_DIR in path.parents:
        path.unlink(missing_ok=True)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #

def _run_job(job_fk: int, paper_fk: int, mode: str) -> None:
    def progress(stage: str, percent: int) -> None:
        db.execute(
            "UPDATE jobs SET stage = ?, progress = ? WHERE id = ?", (stage, percent, job_fk)
        )

    try:
        row = db.one("SELECT * FROM papers WHERE id = ?", (paper_fk,))
        if not row:
            raise RuntimeError("论文记录已被删除")
        db.execute(
            "UPDATE jobs SET status='running', stage='解析 PDF', progress=15 WHERE id=?",
            (job_fk,),
        )
        path = _materialize_pdf(row)
        paper = load_paper(path)
        _store_pages(paper_fk, paper.pages_with_tables, paper.tables)

        result = run_extraction(paper, mode=mode, progress=progress)
        records = result["records"]

        # Deterministic evidence grounding before anything reaches the UI.
        dropped = 0
        for record in records:
            kept: dict[str, Any] = {}
            for key, items in record["evidence"].items():
                valid = [
                    item for item in items if verify_evidence(paper, item["page"], item["quote"])
                ]
                if valid:
                    kept[key] = valid
                else:
                    dropped += len(items)
            record["evidence"] = kept
        issues = list(result["issues"])
        if dropped:
            issues.append(f"证据校验丢弃 {dropped} 条无法在原页定位的引文")

        with db.connect() as conn:
            conn.execute("DELETE FROM records WHERE paper_fk = ?", (paper_fk,))
            stamp = now()
            for record in records:
                conn.execute(
                    """
                    INSERT INTO records (paper_fk, job_fk, record_id, record_index, fields_json,
                                         evidence_json, confidence_json, reviewed, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        paper_fk,
                        job_fk,
                        record["record_id"],
                        record["record_index"],
                        dumps(record["fields"]),
                        dumps(record["evidence"]),
                        dumps(record["confidence"]),
                        stamp,
                        stamp,
                    ),
                )

        # 抽取完成后顺手生成内容总结：用户不需要再手动点一次「内容总结」。
        # 这一步失败不影响抽取结果，只记一条 issue。
        progress("生成内容总结", 95)
        try:
            _refresh_summary(paper_fk, row["title"], paper.pages_with_tables, records)
        except Exception as exc:  # noqa: BLE001 - summary is a nice-to-have
            issues.append(f"内容总结生成失败（可稍后在卡片上手动重试）：{exc}")

        db.execute(
            """
            UPDATE jobs SET status='succeeded', stage='完成', progress=100, engine=?, model=?,
                            n_records=?, usage_json=?, issues_json=?, finished_at=?, message=''
            WHERE id=?
            """,
            (
                result["engine"],
                result["model"],
                len(records),
                dumps(result["usage"]),
                dumps(issues),
                now(),
                job_fk,
            ),
        )
        # 抽取已经交付，译文在后台慢慢补；用户点开「中文」时多半已经就绪。
        _schedule_prewarm(paper_fk)
    except Exception as exc:  # noqa: BLE001 - job must record its own failure
        traceback.print_exc()
        db.execute(
            """
            UPDATE jobs SET status='failed', stage='失败', progress=100, message=?,
                            finished_at=? WHERE id=?
            """,
            (str(exc)[:500], now(), job_fk),
        )


@app.post("/api/jobs")
async def api_create_job(request: Request) -> dict[str, Any]:
    user = current_user(request)
    body = await request.json()
    paper_fk = int(body.get("paper_fk") or 0)
    mode = str(body.get("mode") or "anchored")
    if mode not in MODE_VALUES:
        raise HTTPException(400, "mode 不合法")
    _owned_paper_row(paper_fk, user)

    with _JOB_LOCK:
        active = db.one(
            "SELECT id FROM jobs WHERE paper_fk = ? AND status IN ('queued','running')",
            (paper_fk,),
        )
        if active:
            raise HTTPException(409, "该论文已有进行中的任务")
        job_fk = db.execute(
            "INSERT INTO jobs (paper_fk, status, stage, progress, mode, created_at)"
            " VALUES (?, 'queued', '排队中', 5, ?, ?)",
            (paper_fk, mode, now()),
        )

    thread = threading.Thread(target=_run_job, args=(job_fk, paper_fk, mode), daemon=True)
    thread.start()
    job = db.one("SELECT * FROM jobs WHERE id = ?", (job_fk,))
    return _job_payload(job or {})


@app.get("/api/jobs/{job_fk}")
def api_job(job_fk: int, request: Request) -> dict[str, Any]:
    user = current_user(request)
    row = db.one(
        """
        SELECT jobs.*, papers.paper_key, papers.title
        FROM jobs JOIN papers ON papers.id = jobs.paper_fk
        WHERE jobs.id = ? AND papers.owner_fk = ?
        """,
        (job_fk, user["id"]),
    )
    if not row:
        raise HTTPException(404, "任务不存在")
    payload = _job_payload(row)
    payload["paper_fk"] = row["paper_fk"]
    payload["paper_key"] = row["paper_key"]
    payload["title"] = row["title"]
    payload["usage"] = loads(row["usage_json"], {})
    return payload


@app.get("/api/jobs")
def api_jobs(request: Request, limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    user = current_user(request)
    rows = db.query(
        """
        SELECT jobs.*, papers.paper_key, papers.title
        FROM jobs JOIN papers ON papers.id = jobs.paper_fk
        WHERE papers.owner_fk = ?
        ORDER BY jobs.id DESC LIMIT ?
        """,
        (user["id"], limit),
    )
    jobs = []
    for row in rows:
        payload = _job_payload(row)
        payload.update(
            {"paper_fk": row["paper_fk"], "paper_key": row["paper_key"], "title": row["title"]}
        )
        jobs.append(payload)
    return {"jobs": jobs}


# --------------------------------------------------------------------------- #
# records / review
# --------------------------------------------------------------------------- #

def _owned_record_row(record_fk: int, user: dict[str, Any]) -> dict[str, Any]:
    """A record belonging to one of the user's own papers."""
    row = db.one(
        """
        SELECT records.* FROM records JOIN papers ON papers.id = records.paper_fk
        WHERE records.id = ? AND papers.owner_fk = ?
        """,
        (record_fk, user["id"]),
    )
    if not row:
        raise HTTPException(404, "记录不存在，或不属于当前账号")
    return row


@app.put("/api/records/{record_fk}")
async def api_update_record(record_fk: int, request: Request) -> dict[str, Any]:
    user = current_user(request)
    body = await request.json()
    row = _owned_record_row(record_fk, user)

    fields = loads(row["fields_json"], {})
    incoming = body.get("fields") or {}
    changes: list[tuple[str, Any, Any]] = []
    for key, value in incoming.items():
        if key not in FIELD_KEYS:
            continue
        clean = value.strip() if isinstance(value, str) else value
        if clean == "":
            clean = None
        if fields.get(key) != clean:
            changes.append((key, fields.get(key), clean))
            fields[key] = clean

    reviewed = body.get("reviewed")
    stamp = now()
    with db.connect() as conn:
        conn.execute(
            "UPDATE records SET fields_json = ?, reviewed = ?, updated_at = ? WHERE id = ?",
            (
                dumps(fields),
                1 if (reviewed if reviewed is not None else row["reviewed"]) else 0,
                stamp,
                record_fk,
            ),
        )
        for key, old, new in changes:
            conn.execute(
                "INSERT INTO edits (record_fk, field, old_value, new_value, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (record_fk, key, old, new, stamp),
            )

    updated = db.one("SELECT * FROM records WHERE id = ?", (record_fk,))
    paper = db.one("SELECT * FROM papers WHERE id = ?", (row["paper_fk"],))
    payload = _record_payload(updated or {}, paper)
    payload["n_changes"] = len(changes)
    return payload


@app.get("/api/records/{record_fk}/history")
def api_record_history(record_fk: int, request: Request) -> dict[str, Any]:
    _owned_record_row(record_fk, current_user(request))
    rows = db.query(
        "SELECT id, field, old_value, new_value, created_at FROM edits WHERE record_fk = ?"
        " ORDER BY id DESC",
        (record_fk,),
    )
    edits = [
        {
            **row,
            "label": FIELD_LABELS.get(row["field"], row["field"]),
        }
        for row in rows
    ]
    return {"edits": edits, "total": len(edits)}


@app.post("/api/records/{record_fk}/revert")
async def api_revert_edit(record_fk: int, request: Request) -> dict[str, Any]:
    """Restore one field to the value it had before a given edit."""
    user = current_user(request)
    body = await request.json()
    edit_id = int(body.get("edit_id") or 0)
    record = _owned_record_row(record_fk, user)
    edit = db.one(
        "SELECT * FROM edits WHERE id = ? AND record_fk = ?", (edit_id, record_fk)
    )
    if not edit:
        raise HTTPException(404, "修改记录不存在")

    fields = loads(record["fields_json"], {})
    key = edit["field"]
    current = fields.get(key)
    target = edit["old_value"]
    if current == target:
        raise HTTPException(409, "该字段当前已是撤销后的值")

    fields[key] = target
    stamp = now()
    with db.connect() as conn:
        conn.execute(
            "UPDATE records SET fields_json = ?, updated_at = ? WHERE id = ?",
            (dumps(fields), stamp, record_fk),
        )
        # 撤销本身也记入历史，保持可追溯
        conn.execute(
            "INSERT INTO edits (record_fk, field, old_value, new_value, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (record_fk, key, current, target, stamp),
        )

    updated = db.one("SELECT * FROM records WHERE id = ?", (record_fk,))
    paper = db.one("SELECT * FROM papers WHERE id = ?", (record["paper_fk"],))
    payload = _record_payload(updated or {}, paper)
    payload["reverted_field"] = key
    payload["label"] = FIELD_LABELS.get(key, key)
    return payload


# --------------------------------------------------------------------------- #
# batch operations
# --------------------------------------------------------------------------- #

@app.post("/api/jobs/batch")
async def api_batch_jobs(request: Request) -> dict[str, Any]:
    """Queue extraction for several papers at once."""
    user = current_user(request)
    body = await request.json()
    ids = body.get("paper_fks") or []
    mode = str(body.get("mode") or "anchored")
    if mode not in MODE_VALUES:
        raise HTTPException(400, "mode 不合法")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "paper_fks 不能为空")

    queued: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for raw in ids:
        try:
            paper_fk = int(raw)
        except (TypeError, ValueError):
            continue
        paper = db.one(
            "SELECT paper_key FROM papers WHERE id = ? AND owner_fk = ?",
            (paper_fk, user["id"]),
        )
        if not paper:
            skipped.append({"paper_fk": paper_fk, "reason": "论文不存在或不属于当前账号"})
            continue
        with _JOB_LOCK:
            active = db.one(
                "SELECT id FROM jobs WHERE paper_fk = ? AND status IN ('queued','running')",
                (paper_fk,),
            )
            if active:
                skipped.append({"paper_fk": paper_fk, "reason": "已有进行中的任务"})
                continue
            job_fk = db.execute(
                "INSERT INTO jobs (paper_fk, status, stage, progress, mode, created_at)"
                " VALUES (?, 'queued', '排队中', 5, ?, ?)",
                (paper_fk, mode, now()),
            )
        threading.Thread(
            target=_run_job, args=(job_fk, paper_fk, mode), daemon=True
        ).start()
        queued.append({"paper_fk": paper_fk, "job_fk": job_fk, "paper_key": paper["paper_key"]})

    return {"queued": queued, "skipped": skipped, "mode": mode}


@app.post("/api/papers/batch-delete")
async def api_batch_delete(request: Request) -> dict[str, Any]:
    """Delete several papers and their records at once."""
    user = current_user(request)
    body = await request.json()
    ids = body.get("paper_fks") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "paper_fks 不能为空")

    deleted: list[str] = []
    blocked: list[dict[str, Any]] = []
    for raw in ids:
        try:
            paper_fk = int(raw)
        except (TypeError, ValueError):
            continue
        row = db.one(
            "SELECT * FROM papers WHERE id = ? AND owner_fk = ?", (paper_fk, user["id"])
        )
        if not row:
            continue
        active = db.one(
            "SELECT id FROM jobs WHERE paper_fk = ? AND status IN ('queued','running')",
            (paper_fk,),
        )
        if active:
            blocked.append({"paper_key": row["paper_key"], "reason": "任务进行中"})
            continue
        db.execute("DELETE FROM papers WHERE id = ?", (paper_fk,))
        path = ROOT / (row["pdf_path"] or "")
        if row["source"] == "upload" and path.exists() and UPLOAD_DIR in path.parents:
            path.unlink(missing_ok=True)
        deleted.append(row["paper_key"])

    return {"deleted": deleted, "blocked": blocked}


# --------------------------------------------------------------------------- #
# cross-paper structured search
# --------------------------------------------------------------------------- #

def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


@app.get("/api/search")
def api_search(
    request: Request,
    q: str = Query("", description="字段值关键词"),
    field: str = Query("", description="限定字段 key"),
    sa_min: float | None = None,
    sa_max: float | None = None,
    pore_min: float | None = None,
    pore_max: float | None = None,
    reviewed_only: bool = False,
    paper_fk: int | None = Query(None, description="只搜某一篇论文"),
    folder_fk: int | None = Query(None, description="只搜某个文件夹"),
    unfiled: bool = Query(False, description="只搜未归档的论文"),
    paper_q: str = Query("", description="按论文名称/文件名筛选"),
) -> dict[str, Any]:
    user = current_user(request)
    sql = """
        SELECT records.*, papers.paper_key, papers.title, papers.paper_id AS paper_num,
               papers.filename, papers.folder_fk
        FROM records JOIN papers ON papers.id = records.paper_fk
        WHERE papers.owner_fk = ?
    """
    params: list[Any] = [user["id"]]
    if paper_fk:
        sql += " AND papers.id = ?"
        params.append(paper_fk)
    if folder_fk:
        _folder_row(folder_fk, user)
        sql += " AND papers.folder_fk = ?"
        params.append(folder_fk)
    elif unfiled:
        sql += " AND papers.folder_fk IS NULL"
    sql += " ORDER BY papers.paper_id, records.record_index"
    rows = db.query(sql, tuple(params))

    needle = q.strip().lower()
    paper_needle = paper_q.strip().lower()
    results = []
    for row in rows:
        fields = loads(row["fields_json"], {})
        if reviewed_only and not row["reviewed"]:
            continue
        # 按文件搜索：论文显示名、原始文件名、paper_key 任一命中即可
        if paper_needle:
            haystack = " ".join(
                str(row.get(key) or "") for key in ("title", "filename", "paper_key")
            ).lower()
            if paper_needle not in haystack:
                continue
        if needle:
            if field and field in FIELD_KEYS:
                haystack = str(fields.get(field) or "").lower()
            else:
                haystack = " ".join(str(value or "") for value in fields.values()).lower()
            if needle not in haystack:
                continue
        area = _to_number(fields.get("surface_area"))
        pore = _to_number(fields.get("pore_size"))
        if sa_min is not None and (area is None or area < sa_min):
            continue
        if sa_max is not None and (area is None or area > sa_max):
            continue
        if pore_min is not None and (pore is None or pore < pore_min):
            continue
        if pore_max is not None and (pore is None or pore > pore_max):
            continue
        payload = _record_payload(row, {"paper_key": row["paper_key"], "title": row["title"], "paper_id": row["paper_num"]})
        payload["surface_area_num"] = area
        payload["pore_size_num"] = pore
        payload["folder_fk"] = row.get("folder_fk")
        results.append(payload)
    return {"results": results, "total": len(results)}


@app.get("/api/export")
def api_export(
    request: Request,
    fmt: str = Query("jsonl", pattern="^(jsonl|csv)$"),
    paper_fk: int | None = None,
):
    user = current_user(request)
    sql = """
        SELECT records.*, papers.paper_key, papers.paper_id AS paper_num, papers.pdf_path
        FROM records JOIN papers ON papers.id = records.paper_fk
        WHERE papers.owner_fk = ?
    """
    params: tuple = (user["id"],)
    if paper_fk:
        sql += " AND records.paper_fk = ?"
        params = (user["id"], paper_fk)
    sql += " ORDER BY papers.paper_id, records.record_index"
    rows = db.query(sql, params)

    if fmt == "jsonl":
        buffer = io.StringIO()
        for row in rows:
            # Exactly the contract in docs/extraction-contract.md
            buffer.write(
                json.dumps(
                    {
                        "paper_id": row["paper_num"],
                        "paper_key": row["paper_key"],
                        "record_id": row["record_id"],
                        "record_index": row["record_index"],
                        "pdf_path": f"papers/{row['paper_key']}.pdf",
                        "fields": loads(row["fields_json"], {}),
                        "evidence": loads(row["evidence_json"], {}),
                        "confidence": loads(row["confidence_json"], {}),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return StreamingResponse(
            io.BytesIO(buffer.getvalue().encode("utf-8")),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="predictions.jsonl"'},
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["paper_key", "record_id"] + [FIELD_LABELS[key] for key in FIELD_KEYS])
    for row in rows:
        fields = loads(row["fields_json"], {})
        writer.writerow(
            [row["paper_key"], row["record_id"]] + [fields.get(key) or "" for key in FIELD_KEYS]
        )
    return StreamingResponse(
        io.BytesIO(buffer.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="records.csv"'},
    )


# --------------------------------------------------------------------------- #
# QA
# --------------------------------------------------------------------------- #

def _build_retriever(
    paper_fk: int | None,
    user: dict[str, Any],
    *,
    folder_fk: int | None = None,
    paper_fks: list[int] | None = None,
) -> Retriever:
    """Pages available for question answering.

    Scope rules, deliberately asymmetric from the library view:

    * no scope at all — the **whole shared corpus**, every account's papers,
      so the assistant benefits from everything the community uploaded;
    * ``paper_fk`` / ``paper_fks`` / ``folder_fk`` — the caller's own papers
      only, because narrowing the scope is a library action.
    """
    sql = """
        SELECT paper_pages.page, paper_pages.text, papers.paper_key, papers.title,
               papers.id AS paper_fk, papers.owner_fk
        FROM paper_pages JOIN papers ON papers.id = paper_pages.paper_fk
    """
    params: tuple = ()
    if paper_fks:
        marks = ",".join("?" for _ in paper_fks)
        sql += f" WHERE papers.id IN ({marks}) AND papers.owner_fk = ?"
        params = (*paper_fks, user["id"])
    elif folder_fk:
        sql += " WHERE papers.folder_fk = ? AND papers.owner_fk = ?"
        params = (folder_fk, user["id"])
    elif paper_fk:
        sql += " WHERE papers.id = ? AND papers.owner_fk = ?"
        params = (paper_fk, user["id"])
    chunks = [row for row in db.query(sql, params) if (row["text"] or "").strip()]
    return Retriever(chunks)


def _ask_scope(body: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """Resolve and validate the question's scope from the request body."""
    paper_fk = body.get("paper_fk")
    paper_fk = int(paper_fk) if paper_fk else None
    if paper_fk:
        _owned_paper_row(paper_fk, user)

    folder_fk = body.get("folder_fk")
    folder_fk = int(folder_fk) if folder_fk else None
    if folder_fk:
        _folder_row(folder_fk, user)

    raw_list = body.get("paper_fks") or []
    paper_fks: list[int] = []
    if isinstance(raw_list, list):
        for item in raw_list:
            try:
                candidate = int(item)
            except (TypeError, ValueError):
                continue
            _owned_paper_row(candidate, user)
            paper_fks.append(candidate)

    if paper_fks:
        label = "selected"
    elif folder_fk:
        label = "folder"
    elif paper_fk:
        label = "single"
    else:
        label = "shared"
    return {
        "paper_fk": paper_fk,
        "folder_fk": folder_fk,
        "paper_fks": paper_fks,
        "label": label,
    }


@app.post("/api/ask")
async def api_ask(request: Request) -> dict[str, Any]:
    user = current_user(request)
    body = await request.json()
    question = str(body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    top_k = int(body.get("top_k") or 4)

    scope = _ask_scope(body, user)
    retriever = _build_retriever(
        scope["paper_fk"], user,
        folder_fk=scope["folder_fk"], paper_fks=scope["paper_fks"],
    )
    hits = retriever.search(question, top_k=max(1, min(8, top_k)))
    result = build_answer(question, hits, llm_status()["available"])
    result["question"] = question
    result["scope"] = scope["label"]
    # 标出哪些引文来自别人上传的论文：文献库里看不到它们，
    # 但问答会用到，必须让用户知道来源不在自己库中。
    own = {
        row["id"]
        for row in db.query("SELECT id FROM papers WHERE owner_fk = ?", (user["id"],))
    }
    for citation in result.get("citations", []):
        match = db.one(
            "SELECT id FROM papers WHERE paper_key = ?", (citation.get("paper_key"),)
        )
        citation["shared"] = bool(match and match["id"] not in own)
    result["corpus_papers"] = len({chunk["paper_fk"] for chunk in retriever.chunks})
    return result


@app.post("/api/ask/stream")
async def api_ask_stream(request: Request):
    """Same as /api/ask, but streams the answer token by token (SSE)."""
    user = current_user(request)
    body = await request.json()
    question = str(body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    top_k = int(body.get("top_k") or 4)

    scope = _ask_scope(body, user)
    retriever = _build_retriever(
        scope["paper_fk"], user,
        folder_fk=scope["folder_fk"], paper_fks=scope["paper_fks"],
    )
    hits = retriever.search(question, top_k=max(1, min(8, top_k)))

    own = {
        row["id"]
        for row in db.query("SELECT id FROM papers WHERE owner_fk = ?", (user["id"],))
    }
    citations = citations_of(hits)
    for citation in citations:
        match = db.one(
            "SELECT id FROM papers WHERE paper_key = ?", (citation.get("paper_key"),)
        )
        citation["shared"] = bool(match and match["id"] not in own)

    def events():
        # 先把引文发给前端：检索已经完成，用户可以立刻看到来源
        yield _sse("meta", {
            "citations": citations,
            "scope": scope["label"],
            "corpus_papers": len({chunk["paper_fk"] for chunk in retriever.chunks}),
        })
        if not hits:
            yield _sse("delta", {"text": "检索到的页面中没有相关信息。可以换个说法，或先确认该论文已完成抽取。"})
            yield _sse("done", {"engine": "retrieval"})
            return

        if llm_status()["available"]:
            try:
                for piece in stream_answer(question, hits):
                    yield _sse("delta", {"text": piece})
                yield _sse("done", {"engine": "llm"})
                return
            except Exception as exc:  # 退回抽取式，不让对话中断
                yield _sse("notice", {"text": f"AI 回答失败，已改用检索式：{exc}"})

        fallback = build_answer(question, hits, False)
        yield _sse("delta", {"text": fallback["answer"]})
        yield _sse("done", {"engine": "retrieval"})

    return StreamingResponse(events(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.post("/api/translate/stream")
async def api_translate_stream(request: Request):
    """Stream a translation so long passages appear as they arrive."""
    current_user(request)
    body = await request.json()
    text = str(body.get("text") or "")
    provider = body.get("provider")

    def events():
        try:
            for piece in stream_translate(text, provider):
                yield _sse("delta", {"text": piece})
            yield _sse("done", {})
        except TranslateError as exc:
            yield _sse("error", {"message": str(exc)})

    return StreamingResponse(events(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.post("/api/papers/{paper_fk}/pages/{page}/translate/stream")
async def api_translate_page_stream(paper_fk: int, page: int, request: Request):
    """Stream one page's translation."""
    user = current_user(request)
    _owned_paper_row(paper_fk, user)
    row = db.one(
        "SELECT text, zh_text FROM paper_pages WHERE paper_fk = ? AND page = ?",
        (paper_fk, page),
    )
    if not row:
        raise HTTPException(404, "该页文本不存在")
    body = await request.json() if await request.body() else {}
    provider = (body or {}).get("provider")
    ready = (row.get("zh_text") or "").strip()

    def events():
        if ready:
            yield _sse("delta", {"text": ready})
            yield _sse("done", {"cached": True})
            return
        try:
            for piece in stream_translate(row["text"], provider):
                yield _sse("delta", {"text": piece})
            yield _sse("done", {})
        except TranslateError as exc:
            yield _sse("error", {"message": str(exc)})

    return StreamingResponse(events(), media_type="text/event-stream", headers=_SSE_HEADERS)


# --------------------------------------------------------------------------- #
# demo corpus
# --------------------------------------------------------------------------- #

@app.post("/api/demo/seed")
def api_seed(request: Request, run: bool = True) -> dict[str, Any]:
    """Create the demo papers as real PDFs under the signed-in account."""
    user = current_user(request)
    created: list[dict[str, Any]] = []
    for item in DEMO_PAPERS:
        existing = db.one(
            "SELECT id FROM papers WHERE owner_fk = ? AND filename = ?",
            (user["id"], item["filename"]),
        )
        if existing:
            continue
        paper_id = _next_paper_id()
        paper_key = f"paper_{paper_id:03d}"
        target = UPLOAD_DIR / f"{paper_key}.pdf"
        render_demo_pdf(item["pages"], target)

        try:
            paper = load_paper(target)
        except PDFError as exc:  # pragma: no cover - demo text is known-good
            target.unlink(missing_ok=True)
            raise HTTPException(500, f"演示 PDF 生成失败：{exc}") from exc

        stats = paper.stats()
        payload = target.read_bytes()
        paper_fk = db.execute(
            """
            INSERT INTO papers (paper_key, paper_id, owner_fk, title, original_title, filename,
                                pdf_path, pdf_blob, pages, chars, n_tables, content_hash,
                                source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'demo', ?)
            """,
            (
                paper_key,
                paper_id,
                user["id"],
                item["title"],
                item["title"],
                item["filename"],
                str(target.relative_to(ROOT)).replace("\\", "/"),
                sqlite3.Binary(payload),
                int(stats["pages"]),
                int(stats["chars"]),
                int(stats["tables"]),
                hashlib.sha256(payload).hexdigest(),
                now(),
            ),
        )
        _store_pages(paper_fk, paper.pages_with_tables, paper.tables)
        created.append({"paper_fk": paper_fk, "paper_key": paper_key})

        if run:
            job_fk = db.execute(
                "INSERT INTO jobs (paper_fk, status, stage, progress, mode, created_at)"
                " VALUES (?, 'queued', '排队中', 5, 'anchored', ?)",
                (paper_fk, now()),
            )
            threading.Thread(
                target=_run_job, args=(job_fk, paper_fk, "anchored"), daemon=True
            ).start()

    if not created:
        return {"created": [], "message": "演示数据已存在"}
    return {"created": created, "message": f"已载入 {len(created)} 篇演示论文"}


# --------------------------------------------------------------------------- #
# static front-end
# --------------------------------------------------------------------------- #

def _asset_version() -> str:
    """Short hash of the CSS+JS bundle, used as a cache-busting query.

    Without this the browser can keep a stale app.js next to a fresh
    index.html; the old script then fails on elements that no longer exist
    and every later event binding is silently skipped.
    """
    digest = hashlib.sha256()
    for name in ("style.css", "app.js"):
        path = STATIC_DIR / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


@app.get("/")
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("__ASSET_V__", _asset_version())
    # The shell itself must never be cached, or the new version is never seen.
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    _ = request
    detail = exc.detail
    # 结构化 detail（如重复上传确认）整体下发，前端据此弹确认框；
    # 同时保留 error 字段，兼容只读取文案的旧调用方。
    if isinstance(detail, dict):
        body = dict(detail)
        body.setdefault("error", detail.get("message", "请求失败"))
        return JSONResponse(body, status_code=exc.status_code)
    return JSONResponse({"error": detail}, status_code=exc.status_code)


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
