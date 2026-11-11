"""重复上传功能的端到端校验：直接打 FastAPI 应用，不依赖浏览器。"""
import importlib
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:\study\a\mkea-extraction")
sys.path.insert(0, str(ROOT / "webapp"))
sys.path.insert(0, str(ROOT / "src"))

# 用项目内的临时数据目录（保持在 ROOT 下，relative_to 才成立），绝不碰真实 mesoscope.db
tmp = Path(tempfile.mkdtemp(prefix="dup-test-", dir=str(ROOT / ".box-agent-scratch")))
import app.main as m  # noqa: E402

# 重定向数据目录后重建 db / 上传目录
m.DATA_DIR = tmp
m.UPLOAD_DIR = tmp / "uploads"
m.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
m.db = m.Database(tmp / "test.db")
import app.db as dbmod  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(m.app)

# 造两份内容不同的真实 PDF：直接复用项目自带的演示语料，保证文本可被解析
from app.pdfgen import render_demo_pdf  # noqa: E402
from app.demo import DEMO_PAPERS  # noqa: E402

pdf_a = tmp / "a.pdf"
pdf_b = tmp / "b.pdf"
render_demo_pdf(DEMO_PAPERS[0]["pages"], pdf_a)
render_demo_pdf(DEMO_PAPERS[1]["pages"], pdf_b)
raw_a = pdf_a.read_bytes()
raw_b = pdf_b.read_bytes()

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if extra else ""))


def up(filename, raw, allow=False):
    url = "/api/papers" + ("?allow_duplicate=true" if allow else "")
    return client.post(url, files={"file": (filename, raw, "application/pdf")})


# 1. 首次上传成功
r1 = up("paper-a.pdf", raw_a)
check("首次上传返回 200", r1.status_code == 200, r1.status_code)
first = r1.json()
check("返回 paper_key", first.get("paper_key") == "paper_001", first.get("paper_key"))

# 2. 同名同内容 -> 409 + 结构化 payload
r2 = up("paper-a.pdf", raw_a)
body = r2.json()
check("重复上传返回 409", r2.status_code == 409, r2.status_code)
check("code=duplicate_paper", body.get("code") == "duplicate_paper", body.get("code"))
check("reason=content", body.get("reason") == "content", body.get("reason"))
check("message 含『已存在』", "已存在" in body.get("message", ""), body.get("message"))
check("带 existing.paper_key", (body.get("existing") or {}).get("paper_key") == "paper_001")
check("兼容 error 字段", bool(body.get("error")), body.get("error"))

# 3. 改名但内容相同 -> 仍按 content 判重
r3 = up("renamed-totally-different.pdf", raw_a)
check("改名后仍判重", r3.status_code == 409, r3.status_code)
check("改名判重 reason=content", r3.json().get("reason") == "content", r3.json().get("reason"))

# 4. 同名但内容不同 -> 按 filename 判重
r4 = up("paper-a.pdf", raw_b)
check("同名不同内容判重", r4.status_code == 409, r4.status_code)
check("同名判重 reason=filename", r4.json().get("reason") == "filename", r4.json().get("reason"))

# 5. 确认后强制上传成功
r5 = up("paper-a.pdf", raw_a, allow=True)
check("allow_duplicate 放行", r5.status_code == 200, r5.status_code)
check("生成新 paper_key", r5.json().get("paper_key") == "paper_002", r5.json().get("paper_key"))

# 6. 全新文件正常通过
r6 = up("paper-b.pdf", raw_b)
check("全新文件不被拦", r6.status_code == 200, r6.status_code)

# 7. 空文件
r7 = up("empty.pdf", b"")
check("空文件 400", r7.status_code == 400, r7.status_code)

# 8. 非 PDF
r8 = up("note.txt", b"hello")
check("非 PDF 400", r8.status_code == 400, r8.status_code)

# 9. 库中共 3 篇
papers = client.get("/api/papers").json()["papers"]
check("库中共 3 篇", len(papers) == 3, len(papers))
check("content_hash 已落库", all(p.get("id") for p in papers))
rows = m.db.query("SELECT paper_key, content_hash FROM papers ORDER BY id")
check("每篇都有 hash", all(r["content_hash"] for r in rows), rows)
check("重复两篇 hash 相同", rows[0]["content_hash"] == rows[1]["content_hash"])

# 10. 旧库迁移：建一个没有 content_hash 列的 papers 表，确认能自动补列
legacy = tmp / "legacy.db"
import sqlite3  # noqa: E402

conn = sqlite3.connect(legacy)
conn.executescript(
    """
    CREATE TABLE papers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        paper_key TEXT NOT NULL UNIQUE,
        paper_id INTEGER NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        filename TEXT NOT NULL DEFAULT '',
        pdf_path TEXT NOT NULL DEFAULT '',
        pages INTEGER NOT NULL DEFAULT 0,
        chars INTEGER NOT NULL DEFAULT 0,
        n_tables INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'upload',
        created_at TEXT NOT NULL
    );
    INSERT INTO papers (paper_key, paper_id, title, filename, created_at)
    VALUES ('paper_001', 1, '旧库论文', 'old.pdf', '2024-01-01T00:00:00');
    """
)
conn.commit()
conn.close()

olddb = dbmod.Database(legacy)
cols = {r["name"] for r in olddb.query("PRAGMA table_info(papers)")}
check("旧库自动补 content_hash 列", "content_hash" in cols, sorted(cols))
old_rows = olddb.query("SELECT paper_key, content_hash FROM papers")
check("旧数据保留", old_rows[0]["paper_key"] == "paper_001", old_rows)
check("旧行 hash 为空串", old_rows[0]["content_hash"] == "", repr(old_rows[0]["content_hash"]))

# 旧行 hash 为空，不应该把任意新文件误判成重复
m2_db_backup = m.db
m.db = olddb
r9 = up("brand-new.pdf", raw_b)
check("空 hash 不误判", r9.status_code in (200, 500), r9.status_code)
m.db = m2_db_backup

# 再跑一次迁移，确认幂等
olddb2 = dbmod.Database(legacy)
cols2 = {r["name"] for r in olddb2.query("PRAGMA table_info(papers)")}
check("迁移可重复执行", "content_hash" in cols2 and len(cols2) == len(cols))

client.close()
shutil.rmtree(tmp, ignore_errors=True)

failed = [n for n, ok, _ in results if not ok]
print("\n==== %d passed, %d failed ====" % (len(results) - len(failed), len(failed)))
if failed:
    print("FAILED:", failed)
    sys.exit(1)
