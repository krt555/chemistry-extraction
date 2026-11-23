"""Dev entrypoint: python -m webapp.run (or python webapp/run.py)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT, ROOT / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

# 每次新增接口时在这里登记，启动自检会据此判断加载到的是不是这份代码。
REQUIRED_ROUTES = (
    ("/api/folders", "文件夹归纳"),
    ("/api/papers/move", "移动到文件夹"),
    ("/api/papers/{paper_fk}/pages/{page}/image", "PDF 原版渲染"),
    ("/api/papers/{paper_fk}/pages/{page}/locate", "引文坐标定位"),
    ("/api/papers/{paper_fk}/pages/{page}/translate", "整页翻译"),
    ("/api/papers/{paper_fk}/pages/{page}/translate-region", "框选翻译"),
    ("/api/translate-region/history", "框选翻译记录"),
    ("/api/ask/stream", "问答流式输出"),
    ("/api/translate/stream", "翻译流式输出"),
)


def _selfcheck() -> bool:
    """Report which code and dependencies this process actually loaded.

    Running an old copy of the project (or a Python without PyMuPDF) produces
    confusing 404/405 errors and "渲染不成功" in the browser.  Printing the
    resolved paths plus the registered routes makes that obvious immediately.
    """
    print("—" * 58)
    print(f"项目目录 : {ROOT}")
    print(f"解释器   : {sys.executable}")

    ok = True
    try:
        import pymupdf  # noqa: F401
        print("PyMuPDF  : 已安装（PDF 原版渲染可用）")
    except ImportError:
        ok = False
        print("PyMuPDF  : 缺失 ！PDF 原版渲染会失败")
        print("           修复：pip install -r webapp/requirements.txt")

    try:
        from webapp.app.main import app
    except Exception as exc:  # pragma: no cover - defensive
        print(f"加载失败 : {exc}")
        print("—" * 58)
        return False

    registered = {getattr(route, "path", "") for route in app.routes}
    missing = [(path, label) for path, label in REQUIRED_ROUTES if path not in registered]
    if missing:
        ok = False
        print(f"接口自检 : 缺少 {len(missing)} 个接口，当前运行的似乎是旧版代码")
        for path, label in missing:
            print(f"           ✗ {label}（{path}）")
        print("           请确认是在上面这个项目目录启动的，而不是另一份副本。")
    else:
        print(f"接口自检 : 全部 {len(REQUIRED_ROUTES)} 个新接口已注册")
    print("—" * 58)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="化学文献信息提取 web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--skip-check", action="store_true", help="跳过启动自检"
    )
    args = parser.parse_args()

    if not args.skip_check:
        _selfcheck()

    import uvicorn

    print(f"化学文献信息提取 -> http://{args.host}:{args.port}")
    uvicorn.run(
        "webapp.app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
