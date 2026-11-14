"""End-to-end check of accounts, per-user isolation and the shared corpus."""
import json
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

BASE = "http://127.0.0.1:8021"


class Client:
    """One browser-like session: its own cookie jar."""

    def __init__(self, name):
        self.name = name
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )

    def call(self, path, method="GET", body=None, raw=None, ctype=None):
        data = json.dumps(body).encode() if body is not None else raw
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=120) as r:
                payload = r.read().decode()
                return r.status, (json.loads(payload) if payload else None)
        except urllib.error.HTTPError as e:
            payload = e.read().decode()
            try:
                return e.code, json.loads(payload)
            except json.JSONDecodeError:
                return e.code, payload


def section(title):
    print("\n" + "=" * 12, title)


anon = Client("anon")
section("1. 未登录时所有数据接口必须 401")
for path in ["/api/papers", "/api/jobs", "/api/search", "/api/export?fmt=csv"]:
    print(f"  GET {path:<28} ->", anon.call(path)[0])
print("  POST /api/ask                ->", anon.call("/api/ask", "POST", {"question": "x"})[0])
print("  GET  /api/auth/me            ->", anon.call("/api/auth/me"))
print("  GET  /api/status (匿名可读)   ->", anon.call("/api/status")[1]["counts"])

section("2. 注册首个账号：接管存量 25 篇论文")
alice = Client("alice")
st, d = alice.call(
    "/api/auth/register",
    "POST",
    {
        "username": "alice",
        "password": "secret123",
        "display_name": "爱丽丝",
        "email": "alice@lab.edu",
        "institution": "复旦大学",
        "research_area": "介孔二氧化硅",
    },
)
print("  register ->", st, "| adopted:", d.get("adopted_papers"), "| user:", d["user"]["display_name"])
ALICE_TOKEN = d["token"]
print("  alice 文献库:", len(alice.call("/api/papers")[1]["papers"]), "篇")

section("3. 注册第二个账号：文献库必须为空")
bob = Client("bob")
st, d = bob.call(
    "/api/auth/register",
    "POST",
    {"username": "bob", "password": "secret456", "display_name": "鲍勃"},
)
print("  register ->", st, "| adopted:", d.get("adopted_papers"))
print("  bob 文献库:", len(bob.call("/api/papers")[1]["papers"]), "篇  <- 必须是 0")
print("  bob 任务日志:", len(bob.call("/api/jobs")[1]["jobs"]))
print("  bob 检索结果:", bob.call("/api/search")[1]["total"])

section("4. 跨账号越权访问必须 404")
alice_paper = alice.call("/api/papers")[1]["papers"][0]
pid = alice_paper["id"]
print(f"  alice 的论文 id={pid} ({alice_paper['paper_key']})")
for method, path in [
    ("GET", f"/api/papers/{pid}"),
    ("GET", f"/api/papers/{pid}/pdf"),
    ("GET", f"/api/papers/{pid}/pages/1"),
    ("DELETE", f"/api/papers/{pid}"),
]:
    print(f"  bob {method:<6} {path:<30} ->", bob.call(path, method)[0])
print("  bob PATCH 改名 ->", bob.call(f"/api/papers/{pid}", "PATCH", {"title": "hacked"})[0])
print("  bob 生成总结   ->", bob.call(f"/api/papers/{pid}/summary", "POST")[0])
print("  bob 建抽取任务 ->", bob.call("/api/jobs", "POST", {"paper_fk": pid, "mode": "rules"})[0])
print("  alice 标题未被篡改:", alice.call(f"/api/papers/{pid}")[1]["title"])

section("5. 跨设备共享：同账号另一台设备用 token 登录")
device2 = Client("alice-phone")
st, d = device2.call("/api/auth/login", "POST", {"username": "alice", "password": "secret123"})
print("  login ->", st)
print("  另一设备看到的文献库:", len(device2.call("/api/papers")[1]["papers"]), "篇  <- 应与 alice 一致")
me = device2.call("/api/auth/me")[1]
print("  个人信息同步:", me["user"]["display_name"], "/", me["user"]["institution"], "/", me["user"]["research_area"])

section("6. 修改个人信息，另一设备立刻可见")
alice.call("/api/auth/profile", "PUT", {"institution": "复旦大学化学系", "bio": "研究介孔材料合成"})
print("  device2 读到:", device2.call("/api/auth/me")[1]["user"]["institution"])
print("  bio:", device2.call("/api/auth/me")[1]["user"]["bio"])

section("7. AI 问答：检索全库共享语料")
st, d = bob.call("/api/ask", "POST", {"question": "比表面积最高的样品是多少？"})
print("  bob 提问 ->", st, "| scope:", d["scope"], "| 语料库论文数:", d["corpus_papers"])
print("  bob 自己文献库是 0 篇，但仍拿到", len(d["citations"]), "条引文")
print("  引文来源:", [(c["paper_key"], c["shared"]) for c in d["citations"]][:4])
print("  答案首行:", d["answer"].splitlines()[0][:100])

section("8. 限定单篇问答仍受归属限制")
print("  bob 指定 alice 的论文 ->", bob.call("/api/ask", "POST", {"question": "孔径多大", "paper_fk": pid})[0])
st, d = alice.call("/api/ask", "POST", {"question": "孔径多大", "paper_fk": pid})
print("  alice 指定自己的论文 ->", st, "| scope:", d["scope"], "| 语料:", d["corpus_papers"], "篇")

section("9. 登出后会话失效")
print("  logout ->", alice.call("/api/auth/logout", "POST")[0])
print("  登出后访问 /api/papers ->", alice.call("/api/papers")[0])
print("  device2 仍然有效 ->", device2.call("/api/papers")[0], "(独立会话，不受影响)")

section("10. 密码错误与改密")
print("  错误密码登录 ->", Client("x").call("/api/auth/login", "POST", {"username": "alice", "password": "wrong"}))
st, d = device2.call("/api/auth/password", "PUT", {"old_password": "secret123", "new_password": "newpass456"})
print("  改密 ->", st)
print("  旧密码登录 ->", Client("y").call("/api/auth/login", "POST", {"username": "alice", "password": "secret123"})[0])
print("  新密码登录 ->", Client("z").call("/api/auth/login", "POST", {"username": "alice", "password": "newpass456"})[0])
