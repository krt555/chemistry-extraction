/* 化学文献信息提取 — front-end.
 * No framework on purpose: one file, no build step, easy to demo and to read.
 */

const S = {
  status: null,
  fields: [],
  modes: [],
  papers: [],
  paper: null,          // full payload of the paper open in the review view
  recordIndex: 0,
  page: 1,
  pageCache: new Map(), // `${paperFk}:${page}` -> {text, tables}
  pageView: "image",    // 原文面板：image=PDF 原版，text=纯文字，zh=中文译文
  zhCache: new Map(),   // `${paperFk}:${page}` -> 译文
  regionMode: false,    // 是否处于框选翻译状态
  translateProvider: "", // 当前选用的翻译服务
  askPicked: new Set(), // 问答范围：勾选的论文 id
  splitRatio: 0.5,      // 提取结果页左右占比
  zoom: 1,              // 原版视图的缩放比例
  highlight: null,      // 当前要在页面上框出的引文
  dirty: new Map(),     // field key -> new value
  activeField: null,
  pollers: new Map(),
  picked: new Set(),     // 批量操作选中的 paper id
  folders: [],          // 当前账号的文件夹
  unfiled: 0,           // 未归档论文数
  folderFilter: "",     // ""=全部, "none"=未归档, 其他为文件夹 id
  compareRows: [],      // all records available for comparison
  compareSel: new Set(),// selected record_id set
  settings: null,       // /api/settings/llm payload
  user: null,           // 当前登录账号；null 表示未登录
  sharedPapers: 0,      // 全库共享语料规模，用于问答页提示
};

const LIBRARY_VIEW_KEY = "mesoscope-library-view";
let libraryViewMode = "cards";
let reviewFieldQuery = "";
let reviewFieldMatchIds = null;
let reviewFieldSearchTimer = null;
let reviewFieldSearchRequest = 0;
let syncCompareSplit = () => {};
let regionHistoryItems = [];
let regionHistoryRequest = 0;
try {
  const savedLibraryView = localStorage.getItem(LIBRARY_VIEW_KEY);
  if (savedLibraryView === "list") libraryViewMode = savedLibraryView;
} catch (_) { }

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/** 绑定事件，但元素不存在时只告警。
 *
 * 这个文件在顶层顺序绑定大量监听器。过去只要有一个 id 消失
 * （例如 HTML 改版后 JS 仍是缓存的旧版），抛出的 TypeError 会中断脚本，
 * 其后的全部绑定都不再执行——表现就是「按钮点了没反应」。
 * 用它包一层，单个缺失不再连累其它功能。
 */
function on(id, event, handler, options) {
  const node = $(id);
  if (!node) {
    console.warn(`[app] 元素 #${id} 不存在，已跳过 ${event} 绑定`);
    return null;
  }
  node.addEventListener(event, handler, options);
  return node;
}

let appDialogResolve = null;
let appDialogMode = "confirm";
let appDialogPreviousFocus = null;

function closeAppDialog(value) {
  const resolve = appDialogResolve;
  appDialogResolve = null;
  $("appDialogMask").hidden = true;
  if (appDialogPreviousFocus && appDialogPreviousFocus.isConnected) {
    appDialogPreviousFocus.focus();
  }
  appDialogPreviousFocus = null;
  if (resolve) resolve(value);
}

function showAppDialog(options) {
  if (appDialogResolve) closeAppDialog(null);
  return new Promise((resolve) => {
    const inputRow = $("appDialogInputRow");
    const input = $("appDialogInput");
    const hasInput = options.input !== undefined;
    appDialogMode = hasInput ? "prompt" : "confirm";
    appDialogResolve = resolve;
    appDialogPreviousFocus = document.activeElement;
    $("appDialogTitle").textContent = options.title || (hasInput ? "输入内容" : "确认操作");
    $("appDialogMessage").textContent = options.message || "";
    $("appDialogConfirm").textContent = options.confirmLabel || (hasInput ? "确定" : "确认");
    $("appDialogCancel").textContent = options.cancelLabel || "取消";
    $("appDialogConfirm").className = options.danger ? "btn danger" : "btn primary";
    $("appDialogMask").querySelector(".app-dialog").classList.toggle("has-input", hasInput);
    inputRow.hidden = !hasInput;
    input.required = false;
    if (hasInput) {
      $("appDialogInputLabel").textContent = options.inputLabel || "名称";
      input.value = options.input;
      input.placeholder = options.placeholder || "";
      input.maxLength = options.maxLength || 200;
    } else {
      input.value = "";
      input.placeholder = "";
    }
    $("appDialogMask").hidden = false;
    requestAnimationFrame(() => (hasInput ? input : $("appDialogConfirm")).focus());
    if (hasInput) requestAnimationFrame(() => input.select());
  });
}

function confirmDialog(message, options = {}) {
  return showAppDialog({ ...options, message }).then((result) => result === true);
}

function promptDialog(message, options = {}) {
  return showAppDialog({ ...options, message, input: options.input ?? "" });
}

on("appDialogConfirm", "click", () => {
  closeAppDialog(appDialogMode === "prompt" ? $("appDialogInput").value : true);
});
function cancelAppDialog() {
  closeAppDialog(appDialogMode === "prompt" ? null : false);
}
on("appDialogCancel", "click", cancelAppDialog);
on("appDialogClose", "click", cancelAppDialog);
on("appDialogMask", "click", (event) => {
  if (event.target.id === "appDialogMask") cancelAppDialog();
});
document.addEventListener("keydown", (event) => {
  if ($("appDialogMask").hidden) return;
  if (event.key === "Escape") {
    event.stopImmediatePropagation();
    cancelAppDialog();
  } else if (event.key === "Enter" && appDialogMode === "prompt"
    && event.target === $("appDialogInput")) {
    event.preventDefault();
    event.stopImmediatePropagation();
    closeAppDialog($("appDialogInput").value);
  }
});

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    let payload = null;
    try {
      payload = await response.json();
      detail = payload.error || detail;
    } catch (_) { }
    const error = new Error(detail);
    // 结构化错误（如重复上传）把原始 payload 带给调用方，供其决定交互
    if (payload && payload.code) {
      error.code = payload.code;
      error.payload = payload;
    }
    error.status = response.status;
    // 会话过期：回到登录门，避免后续请求连环报错
    if (response.status === 401 && !path.startsWith("/api/auth/")) {
      handleSessionLost();
    }
    // 404/405 出现在本应存在的接口上，说明后端跑的是旧版代码。
    // 直接说清楚，别让用户对着裸状态码猜。
    if ((response.status === 404 || response.status === 405)
      && /^\/api\/(folders|papers\/move|translate)/.test(path)) {
      error.message =
        "这个功能在当前运行的后端里不存在：服务加载的是旧版代码。"
        + "请在项目目录重新启动服务（启动时会打印接口自检结果）。";
      error.staleBackend = true;
    }
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

function toast(message, kind = "") {
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  $("toastWrap").append(node);
  setTimeout(() => node.remove(), 4200);
}

function positionHelpPopover(help) {
  const popover = help.querySelector(".help-popover");
  const trigger = help.querySelector(".help-icon");
  if (!popover || !trigger) return;

  const triggerRect = trigger.getBoundingClientRect();
  const popoverRect = popover.getBoundingClientRect();
  const left = Math.max(8, Math.min(triggerRect.left, window.innerWidth - popoverRect.width - 8));
  let top = triggerRect.bottom + 8;
  if (top + popoverRect.height > window.innerHeight - 8) {
    top = triggerRect.top - popoverRect.height - 8;
  }
  popover.style.left = `${left}px`;
  popover.style.top = `${Math.max(8, top)}px`;
}

function setHelpOpen(help, open) {
  help.classList.toggle("is-open", open);
  const trigger = help.querySelector(".help-icon");
  if (trigger) trigger.setAttribute("aria-expanded", String(open));
  if (open) positionHelpPopover(help);
}

document.querySelectorAll(".help-tip").forEach((help) => {
  help.addEventListener("pointerenter", () => setHelpOpen(help, true));
  help.addEventListener("pointerleave", (event) => {
    if ((!event.relatedTarget || !help.contains(event.relatedTarget)) && !help.matches(":focus-within")) {
      setHelpOpen(help, false);
    }
  });
  help.addEventListener("focusin", () => setHelpOpen(help, true));
  help.addEventListener("focusout", (event) => {
    if (!event.relatedTarget || !help.contains(event.relatedTarget)) setHelpOpen(help, false);
  });
  help.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setHelpOpen(help, false);
  });
});

function repositionOpenHelp() {
  document.querySelectorAll(".help-tip.is-open").forEach(positionHelpPopover);
}

window.addEventListener("resize", repositionOpenHelp);
window.addEventListener("scroll", repositionOpenHelp, true);

/* ------------------------------------------------------------------ */
/* 账号：登录 / 注册 / 个人信息                                         */
/* ------------------------------------------------------------------ */

/* 会话由服务端 HttpOnly Cookie 维持，前端只保存展示用的账号信息。
   个人信息和文献库都存在服务端，所以换设备登录同一账号看到的内容一致。 */

let authMode = "login";

function setAuthMode(mode) {
  authMode = mode;
  document.querySelectorAll(".auth-tab").forEach((tab) =>
    tab.classList.toggle("active", tab.dataset.mode === mode));
  $("authExtra").hidden = mode !== "register";
  $("authSubmit").textContent = mode === "register" ? "注册并登录" : "登录";
  $("authPassword").setAttribute(
    "autocomplete", mode === "register" ? "new-password" : "current-password");
  authError("");
}

function authError(message) {
  const box = $("authError");
  box.hidden = !message;
  box.textContent = message || "";
}

function showAuthGate(mode) {
  if (mode) setAuthMode(mode);
  closeAccountPop();
  $("authGate").hidden = false;
  document.querySelector(".app").classList.add("locked");
  $("authUsername").focus();
}

function hideAuthGate() {
  $("authGate").hidden = true;
  document.querySelector(".app").classList.remove("locked");
}

/** 关闭登录弹窗：未登录也能浏览界面，随时点右上角头像再登录。 */
function dismissAuthGate() {
  hideAuthGate();
  authError("");
  $("authPassword").value = "";
}

on("authClose", "click", dismissAuthGate);
on("authGate", "click", (event) => {
  if (event.target.id === "authGate") dismissAuthGate();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("authGate").hidden) dismissAuthGate();
});

/* ---- 侧边栏底部头像菜单 ---- */

function closeAccountPop() {
  $("accountPop").hidden = true;
  $("avatarBtn").setAttribute("aria-expanded", "false");
}

function toggleAccountPop() {
  const pop = $("accountPop");
  const open = pop.hidden;
  pop.hidden = !open;
  $("avatarBtn").setAttribute("aria-expanded", String(open));
}

on("avatarBtn", "click", (event) => {
  event.stopPropagation();
  toggleAccountPop();
});

// 点菜单外部收起；菜单内部的点击不冒泡到这里
document.addEventListener("click", (event) => {
  if (!$("accountMenu").contains(event.target)) closeAccountPop();
});

on("popLogin", "click", () => showAuthGate("login"));
on("popRegister", "click", () => showAuthGate("register"));
on("popProfile", "click", () => { closeAccountPop(); openProfile(); });

/** 会话失效：清空内存数据并回到登录弹窗，防止显示上一个账号的残留。 */
function handleSessionLost() {
  if (!S.user) return;
  clearSessionState();
  renderAccountCard();
  showAuthGate("login");
  toast("登录已过期，请重新登录", "warn");
}

/** 退出 / 会话失效共用的内存清理。 */
function clearSessionState() {
  S.user = null;
  S.papers = [];
  S.paper = null;
  regionHistoryItems = [];
  regionHistoryRequest += 1;
  $("regionHistory").hidden = true;
  $("regionHistoryToggle").setAttribute("aria-expanded", "false");
  $("regionHistoryList").innerHTML = "";
  $("regionResult").hidden = true;
  resetAskHistory();
  S.picked.clear();
  S.pageCache.clear();
  S.pollers.forEach((timer) => clearInterval(timer));
  S.pollers.clear();
}

on("authTabs", "click", (event) => {
  const tab = event.target.closest(".auth-tab");
  if (tab) setAuthMode(tab.dataset.mode);
});

on("authForm", "submit", async (event) => {
  event.preventDefault();
  const username = $("authUsername").value.trim();
  const password = $("authPassword").value;
  if (!username || !password) { authError("请填写用户名和密码"); return; }

  const body = { username, password };
  if (authMode === "register") {
    Object.assign(body, {
      display_name: $("authDisplayName").value.trim(),
      email: $("authEmail").value.trim(),
      institution: $("authInstitution").value.trim(),
      research_area: $("authResearchArea").value.trim(),
    });
  }

  await withBusy($("authSubmit"), authMode === "register" ? "注册中…" : "登录中…", async () => {
    authError("");
    try {
      const result = await api(`/api/auth/${authMode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      S.user = result.user;
      $("authPassword").value = "";
      hideAuthGate();
      closeAccountPop();
      renderAccountCard();
      if (result.adopted_papers) {
        toast(`欢迎，${S.user.display_name}！已将 ${result.adopted_papers} 篇既有论文归入你的账号`);
      } else {
        toast(`欢迎，${S.user.display_name}`);
      }
      await bootData();
    } catch (error) {
      authError(error.message);
    }
  });
});

function renderAccountCard() {
  const signedIn = !!S.user;
  const name = signedIn ? S.user.display_name : "";
  const letter = signedIn ? (name || "?").trim().charAt(0).toUpperCase() : "?";

  // 容错取值：缺任何一个节点都不该让整次渲染抛错
  const set = (id, text) => { const n = $(id); if (n) n.textContent = text; };
  set("accountAvatar", letter);
  set("popAvatar", letter);
  set("sidebarAccountName", signedIn ? S.user.username : "登录 / 注册");

  const accountName = $("sidebarAccountName");
  if (accountName) accountName.title = signedIn ? S.user.username : "登录 / 注册";

  const btn = $("avatarBtn");
  if (btn) {
    btn.classList.toggle("anon", !signedIn);
    btn.title = signedIn ? `${name}（${S.user.username}）· 账号菜单` : "登录 / 注册";
  }

  const anon = $("accountPopAnon");
  const user = $("accountPopUser");
  if (anon) anon.hidden = signedIn;
  if (user) user.hidden = !signedIn;

  if (signedIn) {
    set("accountName", name);
    set("accountMeta", S.user.institution || S.user.research_area || `@${S.user.username}`);
  }
}

/* ---- 个人信息弹窗 ---- */

function profileResult(message, kind) {
  const box = $("profileResult");
  box.hidden = !message;
  box.className = `setting-result ${kind || ""}`;
  box.textContent = message || "";
}

async function openProfile() {
  if (!S.user) { showAuthGate(); return; }
  profileResult("");
  $("profileOldPwd").value = "";
  $("profileNewPwd").value = "";
  $("profilePwdBlock").open = false;

  try {
    const me = await api("/api/auth/me");
    if (me.user) S.user = me.user;
    const stats = me.stats || {};
    $("profileStats").innerHTML = `
      <div><b>${esc(S.user.username)}</b><span>账号</span></div>
      <div><b>${stats.own_papers ?? 0}</b><span>我的论文</span></div>
      <div><b>${stats.own_records ?? 0}</b><span>我的记录</span></div>
      <div><b>${stats.all_papers ?? 0}</b><span>共享语料</span></div>`;
  } catch (_) { /* 统计失败不阻塞编辑 */ }

  $("profileDisplayName").value = S.user.display_name || "";
  $("profileEmail").value = S.user.email || "";
  $("profileInstitution").value = S.user.institution || "";
  $("profileResearchArea").value = S.user.research_area || "";
  $("profileBio").value = S.user.bio || "";
  $("profileMask").hidden = false;
}

function closeProfile() { $("profileMask").hidden = true; }

on("profileClose", "click", closeProfile);
on("profileCancel", "click", closeProfile);
on("profileMask", "click", (event) => {
  if (event.target.id === "profileMask") closeProfile();
});

on("profileSave", "click", () =>
  withBusy($("profileSave"), "保存中…", async () => {
    try {
      const result = await api("/api/auth/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          display_name: $("profileDisplayName").value,
          email: $("profileEmail").value,
          institution: $("profileInstitution").value,
          research_area: $("profileResearchArea").value,
          bio: $("profileBio").value,
        }),
      });
      S.user = result.user;
      renderAccountCard();
      profileResult("已保存，其他设备登录同一账号即可看到最新信息", "ok");
      toast("个人信息已更新");
    } catch (error) { profileResult(error.message, "err"); }
  }));

on("profilePwdSave", "click", () =>
  withBusy($("profilePwdSave"), "更新中…", async () => {
    try {
      await api("/api/auth/password", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          old_password: $("profileOldPwd").value,
          new_password: $("profileNewPwd").value,
        }),
      });
      $("profileOldPwd").value = "";
      $("profileNewPwd").value = "";
      profileResult("密码已更新；其他设备需要重新登录", "ok");
      toast("密码已更新");
    } catch (error) { profileResult(error.message, "err"); }
  }));

async function doLogout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch (_) { }
  clearSessionState();
  closeProfile();
  closeAccountPop();
  renderLibrary();
  renderAccountCard();
  renderAskScope();
  showAuthGate("login");
  toast("已退出登录");
}

on("profileLogout", "click", doLogout);
on("popLogout", "click", doLogout);

/* ------------------------------------------------------------------ */
/* theme                                                               */
/* ------------------------------------------------------------------ */

const THEME_KEY = "cheminfo-theme";

function applyTheme(name) {
  document.documentElement.dataset.theme = name;
  const dark = name === "dark";
  $("themeIcon").textContent = dark ? "☾" : "☀";
  $("themeLabel").textContent = dark ? "夜间模式" : "白天模式";
  try { localStorage.setItem(THEME_KEY, name); } catch (_) { }
}

on("themeToggle", "click", () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});

(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (_) { }
  if (!saved) {
    const prefersLight = window.matchMedia
      && window.matchMedia("(prefers-color-scheme: light)").matches;
    saved = prefersLight ? "light" : "dark";
  }
  applyTheme(saved);
})();

/* ------------------------------------------------------------------ */
/* navigation                                                          */
/* ------------------------------------------------------------------ */

const VIEW_META = {
  ask: "AI 助手",
  library: "文献库",
  review: "提取结果",
  compare: "样品对比",
  search: "跨论文检索",
  jobs: "任务与日志",
};

function activeViewName() {
  const active = document.querySelector(".nav-item.active");
  return active ? active.dataset.view : "";
}

const AI_DRAWER_WIDTH_KEY = "mesoscope-ai-drawer-width";
let syncAiDrawerSplit = () => {};

(function initAiDrawerSplit() {
  const app = document.querySelector(".app");
  const drawer = $("aiDrawer");
  const bar = $("aiDrawerSplit");
  if (!app || !drawer || !bar) return;

  let preferredWidth = null;
  let currentWidth = 0;
  try {
    const saved = parseFloat(localStorage.getItem(AI_DRAWER_WIDTH_KEY));
    if (Number.isFinite(saved)) preferredWidth = saved;
  } catch (_) { }

  const applyWidth = (width) => {
    if (!app.classList.contains("ai-drawer-open") || !app.clientWidth) return;
    const narrow = window.matchMedia("(max-width: 1100px)").matches;
    const sidebarWidth = app.classList.contains("sidebar-collapsed") ? 64 : 236;
    const minWidth = narrow ? 280 : 320;
    const maxWidth = Math.max(minWidth, Math.min(
      600,
      app.clientWidth - sidebarWidth - 6 - 320,
    ));
    currentWidth = Math.min(maxWidth, Math.max(minWidth, width));
    app.style.setProperty("--ai-drawer-width", `${currentWidth}px`);
    bar.setAttribute("aria-valuemin", String(minWidth));
    bar.setAttribute("aria-valuemax", String(Math.round(maxWidth)));
    bar.setAttribute("aria-valuenow", String(Math.round(currentWidth)));
  };

  const sync = () => {
    if (!app.classList.contains("ai-drawer-open")) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    if (preferredWidth === null) app.style.removeProperty("--ai-drawer-width");
    const defaultWidth = drawer.getBoundingClientRect().width || 360;
    applyWidth(preferredWidth === null ? defaultWidth : preferredWidth);
  };
  syncAiDrawerSplit = sync;

  let dragging = false;
  bar.addEventListener("pointerdown", (event) => {
    dragging = true;
    bar.setPointerCapture(event.pointerId);
    bar.classList.add("dragging");
    event.preventDefault();
  });
  bar.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const rect = app.getBoundingClientRect();
    applyWidth(rect.right - event.clientX - bar.offsetWidth / 2);
  });
  const stopDragging = () => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove("dragging");
    preferredWidth = currentWidth;
    try { localStorage.setItem(AI_DRAWER_WIDTH_KEY, String(currentWidth)); } catch (_) { }
  };
  bar.addEventListener("pointerup", stopDragging);
  bar.addEventListener("pointercancel", stopDragging);
  bar.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    applyWidth(currentWidth + (event.key === "ArrowLeft" ? 16 : -16));
    preferredWidth = currentWidth;
    try { localStorage.setItem(AI_DRAWER_WIDTH_KEY, String(currentWidth)); } catch (_) { }
  });
  window.addEventListener("resize", sync);
})();

function syncAiLauncher() {
  const launcher = $("aiLauncher");
  const open = document.querySelector(".app").classList.contains("ai-drawer-open");
  const label = open ? "关闭 AI 助手" : "打开 AI 助手";
  launcher.hidden = activeViewName() === "ask";
  launcher.setAttribute("aria-expanded", String(open));
  launcher.setAttribute("aria-label", label);
  launcher.title = `拖动调整位置，点击${open ? "关闭" : "打开"} AI 助手`;
}

function openAiDrawer() {
  if (activeViewName() === "ask") return;
  const app = document.querySelector(".app");
  $("aiDrawerContent").append($("view-ask"));
  $("view-ask").classList.add("active");
  app.classList.add("ai-drawer-open");
  syncAiDrawerSplit();
  syncAiLauncher();
  requestAnimationFrame(() => $("askInput").focus());
}

function closeAiDrawer() {
  const app = document.querySelector(".app");
  if (!app.classList.contains("ai-drawer-open")) return;
  $("askViewSlot").append($("view-ask"));
  $("view-ask").classList.toggle("active", activeViewName() === "ask");
  $("aiDrawerSplit").hidden = true;
  app.classList.remove("ai-drawer-open");
  syncAiLauncher();
}

let aiLauncherDrag = null;
let suppressAiLauncherClick = false;

on("aiLauncher", "pointerdown", (event) => {
  if (event.button !== 0) return;
  const rect = event.currentTarget.getBoundingClientRect();
  aiLauncherDrag = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    left: rect.left,
    top: rect.top,
    moved: false,
  };
  event.currentTarget.setPointerCapture(event.pointerId);
});

on("aiLauncher", "pointermove", (event) => {
  if (!aiLauncherDrag || aiLauncherDrag.pointerId !== event.pointerId) return;
  const dx = event.clientX - aiLauncherDrag.startX;
  const dy = event.clientY - aiLauncherDrag.startY;
  if (!aiLauncherDrag.moved && Math.hypot(dx, dy) < 4) return;
  aiLauncherDrag.moved = true;
  const launcher = event.currentTarget;
  const left = Math.max(8, Math.min(window.innerWidth - launcher.offsetWidth - 8,
    aiLauncherDrag.left + dx));
  const top = Math.max(8, Math.min(window.innerHeight - launcher.offsetHeight - 8,
    aiLauncherDrag.top + dy));
  launcher.style.left = `${left}px`;
  launcher.style.top = `${top}px`;
  launcher.style.right = "auto";
  launcher.style.bottom = "auto";
  launcher.classList.add("dragging");
  event.preventDefault();
});

function finishAiLauncherDrag(event) {
  if (!aiLauncherDrag || aiLauncherDrag.pointerId !== event.pointerId) return;
  const moved = aiLauncherDrag.moved;
  aiLauncherDrag = null;
  const launcher = $("aiLauncher");
  launcher.classList.remove("dragging");
  if (launcher.hasPointerCapture(event.pointerId)) launcher.releasePointerCapture(event.pointerId);
  if (moved) {
    suppressAiLauncherClick = true;
    setTimeout(() => { suppressAiLauncherClick = false; }, 500);
  }
}

on("aiLauncher", "pointerup", finishAiLauncherDrag);
on("aiLauncher", "pointercancel", finishAiLauncherDrag);
on("aiLauncher", "click", () => {
  if (suppressAiLauncherClick) { suppressAiLauncherClick = false; return; }
  const app = document.querySelector(".app");
  if (app.classList.contains("ai-drawer-open")) closeAiDrawer();
  else openAiDrawer();
});
on("aiDrawerClose", "click", closeAiDrawer);
window.addEventListener("resize", () => {
  const launcher = $("aiLauncher");
  if (!launcher.style.left) return;
  const rect = launcher.getBoundingClientRect();
  launcher.style.left = `${Math.max(8, Math.min(window.innerWidth - rect.width - 8, rect.left))}px`;
  launcher.style.top = `${Math.max(8, Math.min(window.innerHeight - rect.height - 8, rect.top))}px`;
});

function showView(name) {
  document.querySelectorAll(".nav-item").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) =>
    v.classList.toggle("active", v.id === `view-${name}`));
  $("viewTitle").textContent = VIEW_META[name];
  syncAiLauncher();
  hideBrief();
  if (name === "ask") $("askInput").focus();
  if (name === "jobs") loadJobs();
  if (name === "search") runSearch();
  if (name === "compare") {
    requestAnimationFrame(syncCompareSplit);
    loadCompare();
  }
  // 切到「提取结果」时该视图才真正可见，此刻才能量出候选区宽度。
  // 不依赖“选中某篇论文”触发重渲染，进入页面即可判断是否需要「展开」。
  if (name === "review") {
    const box = $("paperSwitch");
    const cards = box.querySelectorAll(".switch-card").length;
    if (cards) requestAnimationFrame(() => syncSwitchOverflow(cards));
  }
}

on("nav", "click", (event) => {
  const button = event.target.closest(".nav-item");
  if (button) showView(button.dataset.view);
});

function sidebarIsExpanded() {
  const app = document.querySelector(".app");
  if (app.classList.contains("sidebar-expanded")) return true;
  if (app.classList.contains("sidebar-collapsed")) return false;
  return !window.matchMedia("(max-width: 1100px)").matches;
}

function syncSidebarToggle() {
  const button = $("sidebarToggle");
  const expanded = sidebarIsExpanded();
  const label = expanded ? "收起侧边栏" : "展开侧边栏";
  button.setAttribute("aria-expanded", String(expanded));
  button.setAttribute("aria-label", label);
  button.title = label;
}

on("sidebarToggle", "click", () => {
  const app = document.querySelector(".app");
  const expanded = !sidebarIsExpanded();
  app.classList.toggle("sidebar-expanded", expanded);
  app.classList.toggle("sidebar-collapsed", !expanded);
  syncSidebarToggle();
  syncAiDrawerSplit();
});
window.addEventListener("resize", syncSidebarToggle);
syncSidebarToggle();

/* ------------------------------------------------------------------ */
/* status                                                              */
/* ------------------------------------------------------------------ */

async function loadStatus() {
  S.status = await api("/api/status");
  S.fields = S.status.fields;
  S.modes = S.status.modes || [];
  S.sharedPapers = S.status.shared_papers || 0;
  if (S.status.user) S.user = S.status.user;

  const llm = S.status.llm;
  $("engineDot").className = `dot ${llm.available ? "on" : "off"}`;
  $("engineName").textContent = llm.available ? "AI 抽取已就绪" : "离线规则模式";
  $("engineHint").textContent = llm.available
    ? `正在使用 ${llm.model} · AI 会通读全文并逐页核对`
    : "未连接 AI 服务，当前用内置关键词规则抽取：功能完整，但字段较少";
  $("openSettings").textContent = llm.available ? "⚙ 更换 AI 服务" : "⚙ 连接 AI 服务";
  $("openSettings").classList.toggle("call", !llm.available);

  const fieldSelect = $("searchField");
  if (fieldSelect.options.length <= 1) {
    S.fields.forEach((f) => fieldSelect.add(new Option(f.label, f.key)));
  }

  renderBulkModeMenus();
}

/* ------------------------------------------------------------------ */
/* extraction modes                                                    */
/* ------------------------------------------------------------------ */

function modeOf(value) {
  return S.modes.find((m) => m.value === value) || null;
}

async function startPaperExtraction(paperFk, mode) {
  const paper = S.papers.find((item) => item.id === paperFk);
  const meta = modeOf(mode);
  if (!paper || !meta) {
    toast("文献或抽取方式已失效，请刷新后重试", "err");
    return;
  }
  const trigger = document.querySelector(
    `.paper-item[data-id="${paperFk}"] [data-act="run-menu"]`);
  if (trigger && trigger.disabled) return;
  if (trigger) trigger.disabled = true;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paper_fk: paper.id, mode }),
    });
    toast(`任务已创建（${meta.short}），进度显示在“正在抽取”`);
    await loadPapers();
    pollJob(job.id);
  } catch (error) { toast(error.message, "err"); }
  finally {
    if (trigger && trigger.isConnected) trigger.disabled = false;
  }
}

function setPaperModeMenu(wrapper, open) {
  const card = wrapper.closest(".paper-item");
  const trigger = wrapper.querySelector(".paper-run-trigger, .bulk-run-trigger");
  if (open && trigger.disabled) return;
  wrapper.classList.toggle("open", open);
  if (card) card.classList.toggle("mode-menu-open", open);
  trigger.setAttribute("aria-expanded", String(open));
  if (open) {
    const menu = wrapper.querySelector(".paper-mode-menu");
    wrapper.classList.toggle("open-up",
      window.innerHeight - trigger.getBoundingClientRect().bottom < menu.offsetHeight + 12);
    if (card && card.classList.contains("paper-row")) {
      const rect = trigger.getBoundingClientRect();
      const left = Math.max(12, Math.min(
        window.innerWidth - menu.offsetWidth - 12, rect.left));
      const openUp = wrapper.classList.contains("open-up");
      menu.style.position = "fixed";
      menu.style.left = `${left}px`;
      menu.style.top = openUp ? "auto" : `${rect.bottom}px`;
      menu.style.bottom = openUp ? `${window.innerHeight - rect.top}px` : "auto";
    }
  } else {
    wrapper.classList.remove("open-up");
    const menu = wrapper.querySelector(".paper-mode-menu");
    menu.style.position = "";
    menu.style.left = "";
    menu.style.top = "";
    menu.style.bottom = "";
  }
}

function closePaperModeMenus(except = null) {
  document.querySelectorAll(".paper-run-dropdown.open, .bulk-run-dropdown.open").forEach((wrapper) => {
    if (wrapper !== except) setPaperModeMenu(wrapper, false);
  });
}

function paperModeOptionsHtml(action = "run-mode") {
  return S.modes.map((mode) => {
    return `<button type="button" class="paper-mode-option" role="menuitem"
      data-act="${action}" data-mode="${esc(mode.value)}">
      <strong>${esc(mode.label)}</strong>
      <span class="paper-mode-summary" role="tooltip">${esc(mode.summary)}</span>
    </button>`;
  }).join("");
}

function renderBulkModeMenus() {
  $("bulkModeMenuPicked").innerHTML = paperModeOptionsHtml("bulk-run-picked-mode");
  $("bulkModeMenuPending").innerHTML = paperModeOptionsHtml("bulk-run-pending-mode");
}

function positionModeSummary(option) {
  const rect = option.getBoundingClientRect();
  const rightRoom = window.innerWidth - rect.right - 12;
  const leftRoom = rect.left - 12;
  const useLeft = leftRoom > rightRoom;
  const available = useLeft ? leftRoom : rightRoom;
  const width = Math.max(120, Math.min(260, available));
  option.classList.toggle("summary-left", useLeft);
  option.style.setProperty("--mode-summary-width", `${width}px`);
}

document.addEventListener("click", (event) => {
  if (!event.target.closest(".paper-run-dropdown, .bulk-run-dropdown")) closePaperModeMenus();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const wrapper = document.querySelector(".paper-run-dropdown.open, .bulk-run-dropdown.open");
  if (!wrapper) return;
  setPaperModeMenu(wrapper, false);
  wrapper.querySelector(".paper-run-trigger, .bulk-run-trigger").focus();
});

function bindModeMenuEvents(root) {
  root.addEventListener("pointerover", (event) => {
    const option = event.target.closest(".paper-mode-option");
    if (option && !option.contains(event.relatedTarget)) positionModeSummary(option);
    const wrapper = event.target.closest(".paper-run-dropdown, .bulk-run-dropdown");
    if (!wrapper || wrapper.contains(event.relatedTarget)) return;
    closePaperModeMenus(wrapper);
    setPaperModeMenu(wrapper, true);
  });
  root.addEventListener("pointerout", (event) => {
    const wrapper = event.target.closest(".paper-run-dropdown, .bulk-run-dropdown");
    if (!wrapper || wrapper.contains(event.relatedTarget) || wrapper.matches(":focus-within")) return;
    setPaperModeMenu(wrapper, false);
  });
  root.addEventListener("focusin", (event) => {
    const option = event.target.closest(".paper-mode-option");
    if (option) positionModeSummary(option);
    const wrapper = event.target.closest(".paper-run-dropdown, .bulk-run-dropdown");
    if (!wrapper) return;
    closePaperModeMenus(wrapper);
    setPaperModeMenu(wrapper, true);
  });
  root.addEventListener("focusout", (event) => {
    const wrapper = event.target.closest(".paper-run-dropdown, .bulk-run-dropdown");
    if (!wrapper || wrapper.contains(event.relatedTarget) || wrapper.matches(":hover")) return;
    setPaperModeMenu(wrapper, false);
  });
}

/* ------------------------------------------------------------------ */
/* hover brief (简易版总结)                                            */
/* ------------------------------------------------------------------ */

/* 鼠标悬停在文献名字上即显示速览，不需要点击。
   气泡挂在 body 下，避免被卡片的 overflow / 栅格裁切。 */

let briefTimer = null;

function briefHtml(paper) {
  const brief = paper.summary_brief
    || (paper.summary ? paper.summary.split("\n")[0] : "");
  const rows = [
    `<div class="hb-head">
       <span class="hb-key">${esc(paper.paper_key)}</span>
       <span class="hb-dims">${paper.pages} 页 · ${paper.n_records || 0} 条记录</span>
     </div>`,
    `<div class="hb-title">${esc(paper.title)}</div>`,
  ];
  if (paper.renamed) {
    rows.push(`<div class="hb-origin">原名：${esc(paper.original_title)}</div>`);
  }
  rows.push(brief
    ? `<div class="hb-brief">${esc(brief)}</div>`
    : `<div class="hb-brief empty">还没有内容总结，点卡片上的「内容总结」生成。</div>`);
  if (paper.note) {
    rows.push(`<div class="hb-note"><b>备注</b>${esc(paper.note)}</div>`);
  }
  return rows.join("");
}

function placeBrief(anchor) {
  const box = $("hoverBrief");
  const rect = anchor.getBoundingClientRect();
  const size = box.getBoundingClientRect();
  const left = Math.max(8, Math.min(rect.left, window.innerWidth - size.width - 8));
  let top = rect.bottom + 8;
  if (top + size.height > window.innerHeight - 8) top = rect.top - size.height - 8;
  box.style.left = `${left}px`;
  box.style.top = `${Math.max(8, top)}px`;
}

function showBrief(anchor, paper) {
  if (!paper) return;
  const box = $("hoverBrief");
  box.innerHTML = briefHtml(paper);
  box.hidden = false;
  placeBrief(anchor);
}

function hideBrief() {
  clearTimeout(briefTimer);
  $("hoverBrief").hidden = true;
}

/** 悬停 220ms 后再弹，避免扫过列表时气泡乱闪。 */
function bindBriefHover(root) {
  root.addEventListener("pointerover", (event) => {
    const anchor = event.target.closest("[data-brief]");
    if (!anchor || anchor.contains(event.relatedTarget)) return;
    const paper = S.papers.find((p) => p.id === Number(anchor.dataset.brief));
    clearTimeout(briefTimer);
    briefTimer = setTimeout(() => showBrief(anchor, paper), 220);
  });
  root.addEventListener("pointerout", (event) => {
    const anchor = event.target.closest("[data-brief]");
    if (!anchor || anchor.contains(event.relatedTarget)) return;
    hideBrief();
  });
}

window.addEventListener("scroll", hideBrief, true);
window.addEventListener("resize", hideBrief);

/* ------------------------------------------------------------------ */
/* library: grouped by extraction state                                */
/* ------------------------------------------------------------------ */

/** 把任务状态归一成四种库内分组。 */
function groupOf(paper) {
  const job = paper.latest_job;
  if (job && (job.status === "running" || job.status === "queued")) return "running";
  if (paper.n_records > 0) return "done";
  if (job && job.status === "failed") return "failed";
  return "pending";
}

const STAGE_SEQUENCE = ["解析 PDF", "抽取", "后处理", "生成总结", "完成"];

/** 把后端阶段文字映射到固定的 5 步，便于显示“第几步”。 */
function stageStep(stage) {
  if (!stage) return 0;
  if (stage.includes("排队")) return 0;
  if (stage.includes("解析")) return 1;
  if (stage.includes("LLM") || stage.includes("规则") || stage.includes("Agent")) return 2;
  if (stage.includes("后处理") || stage.includes("证据")) return 3;
  if (stage.includes("总结")) return 4;
  if (stage.includes("完成")) return 5;
  return 1;
}

/** 渲染 5 步进度条的文字部分，已完成的步骤用对勾替代序号。 */
function stepsHtml(step) {
  return STAGE_SEQUENCE.map((label, index) => {
    const position = index + 1;
    const cls = step > position ? "done" : step === position ? "now" : "";
    const marker = step > position ? "✓" : `${position}.`;
    return `<span class="${cls}">${marker} ${esc(label)}</span>`;
  }).join("");
}

function runningCardHtml(paper) {
  const job = paper.latest_job;
  const pct = Math.max(3, Math.min(100, job.progress || 0));
  const queued = job.status === "queued";
  return `
  <div class="running-card" data-id="${paper.id}">
    <div class="running-top">
      <span class="running-key">${esc(paper.paper_key)}</span>
      <span class="running-title">${esc(paper.title)}</span>
      <span class="running-pct">${pct}%</span>
    </div>
    <div class="running-bar"><i style="width:${pct}%"></i></div>
    <div class="running-foot">
      <span class="running-stage">
        <i class="spin"></i>${esc(queued ? "排队中，等待空闲线程" : job.stage || "处理中")}
      </span>
      <span class="running-steps">${stepsHtml(stageStep(job.stage))}</span>
    </div>
    <div class="running-elapsed" data-started="${esc(job.created_at || "")}"></div>
  </div>`;
}

function paperCardHtml(paper, group) {
  const job = paper.latest_job;
  const picked = S.picked.has(paper.id);
  return `
  <div class="paper-card paper-item ${picked ? "picked" : ""}" data-id="${paper.id}">
    <div class="paper-top">
      <div>
        <label class="paper-pick">
          <input type="checkbox" data-pick="${paper.id}" ${picked ? "checked" : ""} />
          <span class="paper-key" data-brief="${paper.id}">${esc(paper.paper_key)}</span>
          ${paper.renamed ? '<span class="paper-renamed" title="已自定义命名">已命名</span>' : ""}
        </label>
        <div class="paper-title-row">
          <h3 class="paper-title" data-brief="${paper.id}">
            <button type="button" class="paper-title-link" data-act="open"
              ${paper.n_records ? "" : "disabled"}>${esc(paper.title)}</button>
          </h3>
          <button type="button" class="paper-rename-icon" data-act="rename"
            title="编辑文献名称与备注" aria-label="编辑文献名称与备注">
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M10.8 2.8a1.8 1.8 0 0 1 2.5 2.5l-7.7 7.8-3.4.8.8-3.4Z"/>
              <path d="m9.5 4.1 2.5 2.5"/>
            </svg>
          </button>
        </div>
      </div>
      <div class="paper-top-actions">
        ${statusBadge(job, paper)}
        <button type="button" class="paper-delete" data-act="del"
          title="删除文献" aria-label="删除文献">×</button>
      </div>
    </div>
    ${paper.note ? `<div class="paper-note" title="${esc(paper.note)}">${esc(paper.note)}</div>` : ""}
    <div class="paper-dims">
      <span>${paper.pages} 页</span><span>${paper.n_tables} 表</span>
      <span>${(paper.chars / 1000).toFixed(1)}k 字符</span>
      ${paper.n_records ? `<span>提取结果已复核 ${paper.n_reviewed}/${paper.n_records}</span>` : ""}
      ${paper.summary ? '<span class="paper-has-summary">已有总结</span>' : ""}
    </div>
    ${group === "failed" && job ? `<div class="paper-err">${esc(job.message || "未知错误")}</div>` : ""}
    <div class="paper-actions">
      <div class="paper-run-dropdown">
        <button type="button" class="btn sm paper-run-trigger" data-act="run-menu"
          aria-haspopup="menu" aria-expanded="false" aria-controls="paper-mode-menu-${paper.id}">
          ${group === "done" ? "重抽" : "抽取"}<span class="paper-run-caret" aria-hidden="true">▾</span>
        </button>
        <div class="paper-mode-menu" id="paper-mode-menu-${paper.id}" role="menu">
          ${paperModeOptionsHtml()}
        </div>
      </div>
      <button class="btn sm paper-summary-button" data-act="summary"
        title="${paper.summary ? "查看抽取时自动生成的内容总结" : "该论文还没有总结：运行抽取会自动生成，也可点此立即生成"}"
        >${paper.summary ? "内容总结" : "生成内容总结"}</button>
    </div>
  </div>`;
}

function paperRowHtml(paper, group) {
  const job = paper.latest_job;
  const picked = S.picked.has(paper.id);
  return `
    <tr class="paper-row paper-item ${picked ? "picked" : ""}" data-id="${paper.id}">
      <td class="library-paper-id">
        <input type="checkbox" data-pick="${paper.id}" ${picked ? "checked" : ""}
          aria-label="选择 ${esc(paper.paper_key)}" />
        <span class="paper-key" data-brief="${paper.id}">${esc(paper.paper_key)}</span>
      </td>
      <td class="library-paper-title">
        <div class="library-paper-title-line" data-brief="${paper.id}">
          <button type="button" class="paper-title-link" data-act="open" data-brief="${paper.id}"
            title="${esc(paper.title)}" ${paper.n_records ? "" : "disabled"}>
            ${esc(paper.title)}
          </button>
          <button type="button" class="paper-rename-icon" data-act="rename"
            title="编辑文献名称与备注" aria-label="编辑文献名称与备注">
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M10.8 2.8a1.8 1.8 0 0 1 2.5 2.5l-7.7 7.8-3.4.8.8-3.4Z"/>
              <path d="m9.5 4.1 2.5 2.5"/>
            </svg>
          </button>
        </div>
        ${paper.renamed ? '<span class="paper-renamed" title="已自定义命名">已命名</span>' : ""}
        ${paper.note ? `<div class="library-paper-note" title="${esc(paper.note)}">${esc(paper.note)}</div>` : ""}
        ${group === "failed" && job
      ? `<div class="paper-err">${esc(job.message || "未知错误")}</div>` : ""}
      </td>
      <td class="library-paper-status">${statusBadge(job, paper)}</td>
      <td class="library-paper-details">
        <span>${paper.pages} 页</span>
        <span>${paper.n_tables} 表</span>
        <span>${(paper.chars / 1000).toFixed(1)}k 字符</span>
        ${paper.n_records
      ? `<span>提取结果已复核 ${paper.n_reviewed}/${paper.n_records}</span>` : ""}
        ${paper.summary ? '<span class="paper-has-summary">已有总结</span>' : ""}
      </td>
      <td class="library-paper-actions">
        <div class="paper-actions table-paper-actions">
          <div class="paper-run-dropdown">
            <button type="button" class="btn sm paper-run-trigger" data-act="run-menu"
              aria-haspopup="menu" aria-expanded="false" aria-controls="paper-mode-menu-${paper.id}">
              ${group === "done" ? "重抽" : "抽取"}<span class="paper-run-caret" aria-hidden="true">▾</span>
            </button>
            <div class="paper-mode-menu" id="paper-mode-menu-${paper.id}" role="menu">
              ${paperModeOptionsHtml()}
            </div>
          </div>
          <button class="btn sm paper-summary-button" data-act="summary"
            title="${paper.summary ? "查看抽取时自动生成的内容总结" : "该论文还没有总结：运行抽取会自动生成，也可点此立即生成"}"
            >${paper.summary ? "内容总结" : "生成内容总结"}</button>
          <button type="button" class="paper-delete" data-act="del"
            title="删除文献" aria-label="删除文献">×</button>
        </div>
      </td>
    </tr>`;
}

function paperTableHtml(papers, group) {
  return `
    <div class="library-table-wrap">
      <table class="library-table">
        <thead>
          <tr>
            <th class="library-paper-id">论文</th>
            <th>文献名称</th>
            <th>状态</th>
            <th>信息</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>${papers.map((paper) => paperRowHtml(paper, group)).join("")}</tbody>
      </table>
    </div>`;
}

function statusBadge(job, paper) {
  if (paper && paper.n_records > 0 && job && job.status === "succeeded") {
    return `<span class="badge ok">${paper.n_records} 条记录</span>`;
  }
  if (!job) return '<span class="badge idle">未抽取</span>';
  const map = {
    queued: ["run", "排队中"],
    running: ["run", `${job.progress || 0}%`],
    succeeded: ["ok", `${job.n_records} 条记录`],
    failed: ["err", "失败"],
  };
  const [cls, text] = map[job.status] || ["idle", job.status];
  return `<span class="badge ${cls}">${esc(text)}</span>`;
}

async function loadPapers() {
  const data = await api("/api/papers");
  S.papers = data.papers;
  await loadFolders();
  renderLibrary();
  renderAskScope();
  if ($("reviewFieldSearch").value.trim()) updateReviewFieldSearch();
  else renderPaperSwitch();
  S.papers.forEach((p) => {
    const job = p.latest_job;
    if (job && (job.status === "running" || job.status === "queued")) pollJob(job.id);
  });
}

/* ---- 文件夹 ---- */

async function loadFolders() {
  try {
    const data = await api("/api/folders");
    S.folders = data.folders || [];
    S.unfiled = data.unfiled || 0;
  } catch (error) {
    S.folders = [];
    S.unfiled = 0;
  }
  renderFolderBar();
  renderFolderSelects();
}

/** 顶部文件夹筛选条；"全部"与"未归档"是两个固定项。 */
function renderFolderBar() {
  const chip = (id, label, count, active) => `
    <button class="folder-chip ${active ? "active" : ""}" data-folder="${id}">
      <span>${esc(label)}</span><em>${count}</em>
      ${id !== "" && id !== "none"
      ? `<i class="folder-edit" data-folder-act="rename" title="重命名">✎</i>
           <i class="folder-edit" data-folder-act="del" title="删除文件夹（论文不会被删）">×</i>`
      : ""}
    </button>`;

  const active = S.folderFilter;
  const parts = [chip("", "全部", S.papers.length, active === "")];
  S.folders.forEach((f) =>
    parts.push(chip(String(f.id), f.name, f.n_papers, active === String(f.id))));
  if (S.unfiled || S.folders.length) {
    parts.push(chip("none", "未归档", S.unfiled, active === "none"));
  }
  $("folderChips").innerHTML = parts.join("");
}

/** 批量移动、检索和记录列表共用文件夹选项。 */
function renderFolderSelects() {
  const options = (withNone) =>
    (withNone ? '<option value="">未归档（移出文件夹）</option>' : '<option value="">全部文件夹</option>')
    + S.folders.map((f) => `<option value="${f.id}">${esc(f.name)}</option>`).join("");

  const bulk = $("bulkFolder");
  if (bulk) {
    const keep = bulk.value;
    bulk.innerHTML = options(true);
    bulk.value = keep;
  }
  const search = $("searchFolder");
  if (search) {
    const keep = search.value;
    search.innerHTML = options(false)
      + '<option value="none">仅未归档</option>';
    search.value = keep;
  }
  renderPaperListFolderFilters();
}

function renderPaperListFolderFilters() {
  const options = '<option value="">全部文件夹</option><option value="none">未归档</option>'
    + S.folders.map((folder) =>
      `<option value="${folder.id}">${esc(folder.name)}</option>`).join("");
  ["reviewFolderFilter", "compareFolderFilter"].forEach((id) => {
    const select = $(id);
    if (!select) return;
    const keep = select.value;
    select.innerHTML = options;
    select.value = [...select.options].some((option) => option.value === keep) ? keep : "";
  });
}

on("folderChips", "click", async (event) => {
  const act = event.target.closest("[data-folder-act]");
  const chip = event.target.closest("[data-folder]");
  if (!chip) return;
  const id = chip.dataset.folder;

  if (act) {
    event.stopPropagation();
    const folder = S.folders.find((f) => String(f.id) === id);
    if (!folder) return;
    if (act.dataset.folderAct === "rename") {
      const name = await promptDialog("修改文件夹名称，不会影响其中的文献。", {
        title: "重命名文件夹",
        inputLabel: "文件夹名称",
        input: folder.name,
        placeholder: "输入文件夹名称",
        maxLength: 100,
        confirmLabel: "保存",
      });
      if (typeof name !== "string" || !name.trim() || name.trim() === folder.name) return;
      try {
        await api(`/api/folders/${folder.id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name.trim() }),
        });
        toast("已重命名");
        await loadPapers();
      } catch (error) { toast(error.message, "err"); }
      return;
    }
    if (!await confirmDialog(`删除文件夹「${folder.name}」？\n里面的论文不会被删除，会回到「未归档」。`, {
      title: "删除文件夹",
      confirmLabel: "删除",
      danger: true,
    })) return;
    try {
      await api(`/api/folders/${folder.id}`, { method: "DELETE" });
      if (S.folderFilter === id) S.folderFilter = "";
      toast("文件夹已删除，论文已回到未归档");
      await loadPapers();
    } catch (error) { toast(error.message, "err"); }
    return;
  }

  S.folderFilter = id;
  renderFolderBar();
  renderLibrary();
});

on("folderNew", "click", async () => {
  const name = await promptDialog("新建文件夹用于归纳论文。", {
    title: "新建文件夹",
    inputLabel: "文件夹名称",
    placeholder: "例如：SBA-15 系列",
    maxLength: 100,
    confirmLabel: "创建",
  });
  if (!name || !name.trim()) return;
  try {
    await api("/api/folders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    toast(`已创建文件夹「${name.trim()}」`);
    await loadPapers();
  } catch (error) { toast(error.message, "err"); }
});

/** 当前文件夹筛选下可见的论文。 */
function visiblePapers() {
  if (S.folderFilter === "") return S.papers;
  if (S.folderFilter === "none") return S.papers.filter((p) => !p.folder_fk);
  return S.papers.filter((p) => String(p.folder_fk) === S.folderFilter);
}

function renderLibrary() {
  const buckets = { running: [], done: [], pending: [], failed: [] };
  const papers = visiblePapers();
  papers.forEach((p) => buckets[groupOf(p)].push(p));

  $("libraryEmpty").hidden = S.papers.length > 0;

  $("groupRunning").hidden = !buckets.running.length;
  $("runningCount").textContent = buckets.running.length;
  $("runningList").innerHTML = buckets.running.map(runningCardHtml).join("");

  const fill = (group, groupId, gridId, countId) => {
    const items = buckets[group];
    $(groupId).hidden = !items.length;
    $(countId).textContent = items.length;
    $(gridId).innerHTML = libraryViewMode === "list"
      ? paperTableHtml(items, group)
      : items.map((p) => paperCardHtml(p, group)).join("");
  };
  // 待抽取排在已完成前面：这是用户最需要动手处理的一组
  fill("pending", "groupPending", "pendingGrid", "pendingCount");
  fill("done", "groupDone", "doneGrid", "doneCount");
  fill("failed", "groupFailed", "failedGrid", "failedCount");

  syncLibraryViewMode();
  syncBulkBar();
}

function syncLibraryViewMode() {
  document.querySelectorAll("#pendingGrid, #doneGrid, #failedGrid").forEach((grid) => {
    grid.classList.toggle("list-mode", libraryViewMode === "list");
  });
  document.querySelectorAll("[data-library-mode]").forEach((button) => {
    const active = button.dataset.libraryMode === libraryViewMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

on("libraryViewToggle", "click", (event) => {
  const button = event.target.closest("[data-library-mode]");
  if (!button) return;
  libraryViewMode = button.dataset.libraryMode;
  try { localStorage.setItem(LIBRARY_VIEW_KEY, libraryViewMode); } catch (_) { }
  renderLibrary();
});
syncLibraryViewMode();

/* ---- 批量操作 ---- */

/** 只有不在运行中的论文可被批量选择；且限定在当前文件夹筛选内。 */
function selectablePapers() {
  return visiblePapers().filter((p) => groupOf(p) !== "running");
}

function syncBulkBar() {
  const selectable = selectablePapers();
  $("bulkBar").hidden = selectable.length < 2;

  // 清理已消失的选中项
  const valid = new Set(selectable.map((p) => p.id));
  [...S.picked].forEach((id) => { if (!valid.has(id)) S.picked.delete(id); });

  const count = S.picked.size;
  $("bulkCount").textContent = `已选 ${count} 篇`;
  $("bulkRunLabel").textContent = count ? `一键抽取 (${count})` : "一键抽取";
  $("bulkRun").disabled = count === 0;
  $("bulkRun").closest(".bulk-run-dropdown").classList.toggle("disabled", count === 0);
  if (!count) setPaperModeMenu($("bulkRun").closest(".bulk-run-dropdown"), false);
  $("bulkDelete").disabled = count === 0;
  $("bulkMove").disabled = count === 0;
  $("bulkDelete").textContent = count ? `一键删除 (${count})` : "一键删除";
  $("bulkMove").textContent = count ? `移动 (${count})` : "移动";

  const all = $("bulkSelectAll");
  all.checked = count > 0 && count === selectable.length;
  all.indeterminate = count > 0 && count < selectable.length;

  const pending = visiblePapers().filter((p) => groupOf(p) === "pending").length;
  const btn = $("bulkRunPending");
  btn.disabled = pending === 0;
  $("bulkRunPendingLabel").textContent = pending
    ? `抽取全部待抽取 (${pending})` : "抽取全部待抽取";
  btn.closest(".bulk-run-dropdown").classList.toggle("disabled", pending === 0);
  if (!pending) setPaperModeMenu(btn.closest(".bulk-run-dropdown"), false);
}

on("bulkMove", "click", async () => {
  const ids = [...S.picked];
  if (!ids.length) return;
  const raw = $("bulkFolder").value;
  const label = raw
    ? ($("bulkFolder").selectedOptions[0] || {}).textContent || "该文件夹"
    : "未归档";
  try {
    const result = await api("/api/papers/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paper_fks: ids, folder_fk: raw ? Number(raw) : null }),
    });
    toast(`已把 ${result.moved} 篇论文移动到「${label.trim()}」`);
    S.picked.clear();
    await loadPapers();
  } catch (error) { toast(error.message, "err"); }
});

on("bulkSelectAll", "change", (event) => {
  S.picked.clear();
  if (event.target.checked) selectablePapers().forEach((p) => S.picked.add(p.id));
  renderLibrary();
});

function handlePickChange(event) {
  const box = event.target.closest("[data-pick]");
  if (!box) return;
  const id = Number(box.dataset.pick);
  if (box.checked) S.picked.add(id); else S.picked.delete(id);
  box.closest(".paper-item").classList.toggle("picked", box.checked);
  syncBulkBar();
}

async function runBatch(ids, mode) {
  if (!ids.length) return;
  const meta = modeOf(mode);
  const name = meta ? meta.short : mode;
  try {
    const result = await api("/api/jobs/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paper_fks: ids, mode }),
    });
    const queued = result.queued || [];
    toast(queued.length
      ? `已排队 ${queued.length} 个任务（${name}），进度见“正在抽取”`
      : "没有新任务被排队");
    (result.skipped || []).forEach((item) =>
      toast(`跳过 ${item.paper_fk}：${item.reason}`, "warn"));
    S.picked.clear();
    await loadPapers();
    await loadStatus();
    queued.forEach((item) => pollJob(item.job_fk));
  } catch (error) { toast(error.message, "err"); }
}

on("bulkBar", "click", (event) => {
  const trigger = event.target.closest("[data-bulk-run]");
  if (trigger) {
    if (trigger.disabled) return;
    const wrapper = trigger.closest(".bulk-run-dropdown");
    closePaperModeMenus(wrapper);
    setPaperModeMenu(wrapper, true);
    return;
  }

  const option = event.target.closest(
    '.paper-mode-option[data-act^="bulk-run-"]');
  if (!option) return;
  setPaperModeMenu(option.closest(".bulk-run-dropdown"), false);
  if (option.dataset.act === "bulk-run-picked-mode") {
    runBatch([...S.picked], option.dataset.mode);
  } else {
    const ids = visiblePapers()
      .filter((p) => groupOf(p) === "pending")
      .map((p) => p.id);
    if (!ids.length) { toast("没有待抽取的论文", "warn"); return; }
    runBatch(ids, option.dataset.mode);
  }
});

on("bulkDelete", "click", async () => {
  const ids = [...S.picked];
  if (!ids.length) return;
  if (!await confirmDialog(`删除选中的 ${ids.length} 篇论文及其全部抽取记录？此操作不可撤销。`, {
    title: "批量删除文献",
    confirmLabel: "删除",
    danger: true,
  })) return;
  try {
    const result = await api("/api/papers/batch-delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paper_fks: ids }),
    });
    toast(`已删除 ${(result.deleted || []).length} 篇论文`);
    (result.blocked || []).forEach((item) =>
      toast(`${item.paper_key} 未删除：${item.reason}`, "warn"));
    if (S.paper && ids.includes(S.paper.id)) { S.paper = null; renderFields(); }
    S.picked.clear();
    await loadPapers();
    await loadStatus();
  } catch (error) { toast(error.message, "err"); }
});

async function handleCardAction(event) {
  const card = event.target.closest(".paper-item");
  const button = event.target.closest("button[data-act]");
  if (!card || !button) return;
  const id = Number(card.dataset.id);

  if (button.dataset.act === "run-menu") {
    const wrapper = button.closest(".paper-run-dropdown");
    closePaperModeMenus(wrapper);
    setPaperModeMenu(wrapper, true);
    return;
  }

  if (button.dataset.act === "run-mode") {
    const wrapper = button.closest(".paper-run-dropdown");
    setPaperModeMenu(wrapper, false);
    startPaperExtraction(id, button.dataset.mode);
    return;
  }

  if (button.dataset.act === "open") {
    showView("review");
    await openPaper(id);
  }

  if (button.dataset.act === "summary") {
    hideBrief();
    await openSummary(id);
  }

  if (button.dataset.act === "rename") {
    hideBrief();
    openRename(id);
  }

  if (button.dataset.act === "del") {
    const paper = S.papers.find((p) => p.id === id);
    if (!await confirmDialog(`删除「${paper ? paper.paper_key : id}」及其全部抽取记录？`, {
      title: "删除文献",
      confirmLabel: "删除",
      danger: true,
    })) return;
    await api(`/api/papers/${id}`, { method: "DELETE" });
    if (S.paper && S.paper.id === id) { S.paper = null; renderFields(); }
    toast("已删除");
    await loadPapers();
    await loadStatus();
  }
}

["doneGrid", "pendingGrid", "failedGrid"].forEach((id) => {
  const grid = $(id);
  grid.addEventListener("click", handleCardAction);
  grid.addEventListener("change", handlePickChange);
  bindModeMenuEvents(grid);
  bindBriefHover(grid);
});
bindModeMenuEvents($("bulkBar"));

/* ------------------------------------------------------------------ */
/* 自定义命名与备注                                                     */
/* ------------------------------------------------------------------ */

let renameTarget = null;

function openRename(paperFk) {
  const paper = S.papers.find((p) => p.id === paperFk);
  if (!paper) return;
  renameTarget = paperFk;
  $("renameSub").textContent = `${paper.paper_key} · 只改显示名称，不会改动 PDF 原文件`;
  $("renameInput").value = paper.title || "";
  $("renameOriginal").textContent = paper.original_title || paper.title || "—";
  $("noteInput").value = paper.note || "";
  $("renameMask").hidden = false;
  $("renameInput").focus();
  $("renameInput").select();
}

function closeRename() {
  $("renameMask").hidden = true;
  renameTarget = null;
}

async function patchPaper(paperFk, body) {
  const updated = await api(`/api/papers/${paperFk}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  // 就地更新缓存，保证悬停速览立刻用上新名称/备注
  const index = S.papers.findIndex((p) => p.id === paperFk);
  if (index >= 0) S.papers[index] = { ...S.papers[index], ...updated };
  return updated;
}

on("renameSave", "click", () =>
  withBusy($("renameSave"), "保存中…", async () => {
    if (!renameTarget) return;
    try {
      const updated = await patchPaper(renameTarget, {
        title: $("renameInput").value,
        note: $("noteInput").value,
      });
      closeRename();
      renderLibrary();
      renderPaperSwitch();
      renderAskScope();
      if (S.paper && S.paper.id === updated.id) {
        S.paper = { ...S.paper, ...updated };
      }
      toast(`已更新「${updated.title}」`);
    } catch (error) { toast(error.message, "err"); }
  }));

on("renameReset", "click", async () => {
  if (!renameTarget) return;
  try {
    const updated = await patchPaper(renameTarget, { reset_title: true });
    $("renameInput").value = updated.title;
    renderLibrary();
    renderPaperSwitch();
    renderAskScope();
    toast("已恢复导入时的原始名称");
  } catch (error) { toast(error.message, "err"); }
});

on("renameCancel", "click", closeRename);
on("renameClose", "click", closeRename);
on("renameMask", "click", (event) => {
  if (event.target.id === "renameMask") closeRename();
});

/* ------------------------------------------------------------------ */
/* 内容总结                                                             */
/* ------------------------------------------------------------------ */

let summaryTarget = null;

function renderSummary(paper) {
  $("summarySub").textContent = `${paper.paper_key} · ${paper.title}`;
  const brief = $("summaryBrief");
  brief.hidden = !paper.summary_brief;
  brief.textContent = paper.summary_brief ? `速览：${paper.summary_brief}` : "";
  $("summaryText").innerHTML = (paper.summary || "")
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => `<p>${esc(line)}</p>`)
    .join("") || '<p class="placeholder">暂无总结内容。</p>';
}

/** 已有总结先直接展示，没有才去生成，避免每次点开都重算。 */
async function openSummary(paperFk, refresh = false) {
  const paper = S.papers.find((p) => p.id === paperFk);
  if (!paper) return;
  summaryTarget = paperFk;
  $("summaryMask").hidden = false;

  if (paper.summary && !refresh) {
    renderSummary(paper);
    return;
  }

  $("summarySub").textContent = `${paper.paper_key} · ${paper.title}`;
  $("summaryBrief").hidden = true;
  $("summaryText").innerHTML =
    '<p class="placeholder">正在生成内容总结…（AI 模式下通常需要几十秒）</p>';

  try {
    const updated = await api(`/api/papers/${paperFk}/summary?refresh=true`, { method: "POST" });
    const index = S.papers.findIndex((p) => p.id === paperFk);
    if (index >= 0) S.papers[index] = { ...S.papers[index], ...updated };
    renderSummary(updated);
    renderLibrary();
  } catch (error) {
    $("summaryText").innerHTML = `<p class="placeholder">生成失败：${esc(error.message)}</p>`;
  }
}

function closeSummary() {
  $("summaryMask").hidden = true;
  summaryTarget = null;
}

on("summaryRefresh", "click", () =>
  withBusy($("summaryRefresh"), "生成中…", async () => {
    if (summaryTarget) await openSummary(summaryTarget, true);
  }));
on("summaryDone", "click", closeSummary);
on("summaryClose", "click", closeSummary);
on("summaryMask", "click", (event) => {
  if (event.target.id === "summaryMask") closeSummary();
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!$("renameMask").hidden) closeRename();
  else if (!$("summaryMask").hidden) closeSummary();
});

/** 显示已用时长；LLM 阶段额外提示耗时属正常。 */
function renderElapsed(node, startedAt, slow) {
  if (!node) return;
  const started = Date.parse(startedAt || "");
  if (!started) { node.textContent = ""; return; }
  const seconds = Math.max(0, Math.round((Date.now() - started) / 1000));
  const text = seconds < 60
    ? `${seconds} 秒`
    : `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, "0")} 秒`;
  node.textContent = slow
    ? `已用 ${text} · LLM 单篇推理通常需要 1–3 分钟，请耐心等待`
    : `已用 ${text}`;
}

function pollJob(jobId) {
  if (S.pollers.has(jobId)) return;
  const timer = setInterval(async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      const card = document.querySelector(`.running-card[data-id="${job.paper_fk}"]`);
      if (card && (job.status === "running" || job.status === "queued")) {
        // 原地更新进度，避免整页重绘导致进度条跳动
        const pct = Math.max(3, Math.min(100, job.progress || 0));
        card.querySelector(".running-pct").textContent = `${pct}%`;
        const bar = card.querySelector(".running-bar > i");
        bar.style.width = `${pct}%`;
        const stage = card.querySelector(".running-stage");
        stage.innerHTML = `<i class="spin"></i>${esc(job.stage || "处理中")}`;
        card.querySelector(".running-steps").innerHTML = stepsHtml(stageStep(job.stage));
        // LLM 调用是个长耗时黑盒，百分比会长时间停住；
        // 用流动条纹 + 已用时长说明“仍在运行”，避免看起来像卡死。
        const slow = /LLM|Agent|规则/.test(job.stage || "");
        bar.classList.toggle("indeterminate", slow);
        renderElapsed(card.querySelector(".running-elapsed"), job.started_at || job.created_at, slow);
      }
      if (job.status === "succeeded" || job.status === "failed") {
        clearInterval(timer);
        S.pollers.delete(jobId);
        if (job.status === "succeeded") {
          toast(`${job.paper_key || "论文"} 抽取完成：${job.n_records} 条记录（${job.engine}）`);
          (job.issues || []).forEach((issue) => toast(issue, "warn"));
        } else {
          toast(`抽取失败：${job.message}`, "err");
        }
        await loadPapers();
        await loadStatus();
        if (S.paper && S.paper.id === job.paper_fk) await openPaper(job.paper_fk);
      }
    } catch (error) {
      clearInterval(timer);
      S.pollers.delete(jobId);
    }
  }, 1000);
  S.pollers.set(jobId, timer);
}

/* ------------------------------------------------------------------ */
/* upload                                                              */
/* ------------------------------------------------------------------ */

/** 弹出「XX 已存在，是否仍要上传」确认框，返回 "upload" | "skip"。 */
function askDuplicate(info, remaining) {
  return new Promise((resolve) => {
    const mask = $("dupMask");
    const existing = info.existing || {};
    const name = existing.title || info.filename || "该文献";
    const reasonText = info.reason === "filename"
      ? "库中已有同名文件"
      : "库中已有内容完全相同的文件";

    $("dupQuestion").innerHTML =
      `「<b>${esc(name)}</b>」已存在，是否要重复上传？`;
    $("dupSub").textContent = reasonText;
    $("dupExisting").innerHTML = `
      <div class="dup-row"><span>已有记录</span><b>${esc(existing.paper_key || "—")}</b></div>
      <div class="dup-row"><span>原文件名</span><b>${esc(existing.filename || "—")}</b></div>
      <div class="dup-row"><span>规模</span><b>${existing.pages || 0} 页 · ${existing.n_records || 0} 条记录</b></div>
      <div class="dup-row"><span>本次文件</span><b>${esc(info.filename || "—")}</b></div>`;

    // 一次选多个文件时，允许一次性决定剩余的重复项
    const allWrap = $("dupApplyAllWrap");
    const allBox = $("dupApplyAll");
    allBox.checked = false;
    allWrap.hidden = remaining <= 0;
    if (!allWrap.hidden) {
      $("dupApplyAllText").textContent = `本批剩余 ${remaining} 个文件中的重复项都按此处理`;
    }

    mask.hidden = false;
    const finish = (choice) => {
      mask.hidden = true;
      document.removeEventListener("keydown", onKey);
      resolve({ choice, applyAll: allBox.checked });
    };
    const onKey = (e) => {
      if (e.key === "Escape") finish("skip");
      if (e.key === "Enter") finish("upload");
    };

    $("dupConfirm").onclick = () => finish("upload");
    $("dupSkip").onclick = () => finish("skip");
    $("dupClose").onclick = () => finish("skip");
    mask.onclick = (e) => { if (e.target === mask) finish("skip"); };
    document.addEventListener("keydown", onKey);
    $("dupConfirm").focus();
  });
}

async function postPaper(file, allowDuplicate) {
  const form = new FormData();
  form.append("file", file);
  const path = allowDuplicate ? "/api/papers?allow_duplicate=true" : "/api/papers";
  return api(path, { method: "POST", body: form });
}

let activeUploadBatches = 0;

function setUploadBusy(busy) {
  activeUploadBatches = Math.max(0, activeUploadBatches + (busy ? 1 : -1));
  const zone = $("uploadZone");
  const status = $("uploadStatus");
  const isBusy = activeUploadBatches > 0;
  status.hidden = !isBusy;
  zone.classList.toggle("loading", isBusy);
}

async function uploadFiles(files) {
  let bulkChoice = null;   // 勾选「剩余都按此处理」后记住的决定
  let skipped = 0;

  setUploadBusy(true);
  try {
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      try {
        const paper = await postPaper(file, false);
        toast(`${paper.paper_key} 已解析：${paper.pages} 页，可在“待抽取”中启动任务`);
      } catch (error) {
        if (error.code !== "duplicate_paper") {
          toast(`${file.name}：${error.message}`, "err");
          continue;
        }

        let choice = bulkChoice;
        if (!choice) {
          const answer = await askDuplicate(error.payload, files.length - i - 1);
          choice = answer.choice;
          if (answer.applyAll) bulkChoice = choice;
        }

        if (choice === "skip") {
          skipped += 1;
          continue;
        }
        try {
          const paper = await postPaper(file, true);
          toast(`${paper.paper_key} 已重复上传：${paper.pages} 页`, "warn");
        } catch (retryError) {
          toast(`${file.name}：${retryError.message}`, "err");
        }
      }
    }

    if (skipped) toast(`已跳过 ${skipped} 个重复文献`, "warn");
    await loadPapers();
    await loadStatus();
  } finally {
    setUploadBusy(false);
  }
}

/** 需要登录的操作统一走这里：未登录时提示并弹出登录框。 */
function requireLogin(action = "使用该功能") {
  if (S.user) return true;
  toast(`请先登录后再${action}`, "warn");
  showAuthGate("login");
  return false;
}

on("fileInput", "change", (event) => {
  if (!requireLogin("上传论文")) { event.target.value = ""; return; }
  if (event.target.files.length) uploadFiles([...event.target.files]);
  event.target.value = "";
});

const zone = $("uploadZone");
["dragenter", "dragover"].forEach((type) =>
  zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.add("hot"); }));
["dragleave", "drop"].forEach((type) =>
  zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.remove("hot"); }));
zone.addEventListener("drop", (event) => {
  if (!requireLogin("上传论文")) return;
  const files = [...event.dataTransfer.files].filter((f) => f.name.toLowerCase().endsWith(".pdf"));
  if (files.length) uploadFiles(files); else toast("请拖入 PDF 文件", "warn");
});

on("seedBtn", "click", async () => {
  if (!requireLogin("载入演示数据")) return;
  try {
    await api("/api/demo/seed", { method: "POST" });
    toast("演示数据已载入，抽取进度见“正在抽取”");
    await loadPapers(); await loadStatus();
  } catch (error) { toast(error.message, "err"); }
});

/* ------------------------------------------------------------------ */
/* AI service settings                                                 */
/* ------------------------------------------------------------------ */

function settingResult(message, kind) {
  const box = $("settingResult");
  box.hidden = false;
  box.className = `setting-result ${kind}`;
  box.textContent = message;
}

function clearSettingResult() {
  $("settingResult").hidden = true;
}

function renderSettingState() {
  const s = S.settings;
  const box = $("settingState");
  if (!s) { box.innerHTML = ""; return; }
  if (s.has_key) {
    const source = s.key_source === "env"
      ? "（来自系统环境变量，保存后将以配置文件为准）"
      : s.key_source === "legacy"
        ? "（来自旧变量 OPENCODE_GO_API_KEY）"
        : "";
    box.className = "setting-state ok";
    box.innerHTML = `<b>✓ 已连接 AI 服务</b>
      <span>当前密钥 <code>${esc(s.key_masked)}</code> ${esc(source)}</span>
      <span>模型 <code>${esc(s.model)}</code></span>`;
  } else {
    box.className = "setting-state warn";
    box.innerHTML = `<b>！尚未连接 AI 服务</b>
      <span>现在抽取会使用离线关键词规则，字段较少。填入密钥即可启用 AI。</span>`;
  }
  $("settingClear").hidden = !s.has_key;
}

/** 选中服务商后自动回填地址与模型，用户只需粘贴密钥。 */
function applyPreset(id) {
  const preset = (S.settings.presets || []).find((p) => p.id === id);
  if (!preset) return;
  $("providerHint").textContent = preset.hint || "";
  if (preset.id === "custom") return;
  $("settingBaseUrl").value = preset.base_url;
  $("settingModel").value = preset.model;
}

function matchPresetId(baseUrl) {
  const found = (S.settings.presets || [])
    .find((p) => p.base_url && p.base_url === String(baseUrl || "").replace(/\/+$/, ""));
  return found ? found.id : "custom";
}

/** 历史配置里只有脱敏信息；切换时由后端按 id 取回真实密钥。 */
function renderProfiles() {
  const block = $("profileBlock");
  const list = $("profileList");
  const profiles = (S.settings && S.settings.profiles) || [];
  if (!profiles.length) {
    block.hidden = true;
    list.innerHTML = "";
    return;
  }
  block.hidden = false;
  list.innerHTML = profiles.map((p) => `
    <div class="profile-item ${p.active ? "active" : ""}" data-profile="${esc(p.id)}">
      <div class="profile-main">
        <div class="profile-title">
          <span>${esc(p.provider_label)}</span>
          <span class="profile-key">${esc(p.key_masked)}</span>
          ${p.active ? '<span class="profile-badge">使用中</span>' : ""}
        </div>
        <div class="profile-sub" title="${esc(p.base_url)}">
          ${esc(p.model)} · ${esc(p.base_url)}${p.last_used_at ? ` · ${esc(formatStamp(p.last_used_at))}` : ""}
        </div>
      </div>
      <div class="profile-actions">
        <button class="btn sm" data-profile-act="use" ${p.active ? "disabled" : ""}>
          ${p.active ? "当前" : "使用"}
        </button>
        <button class="btn sm danger" data-profile-act="del" title="从历史记录中删除">删除</button>
      </div>
    </div>`).join("");
}

on("profileList", "click", async (event) => {
  const button = event.target.closest("[data-profile-act]");
  const item = event.target.closest("[data-profile]");
  if (!button || !item) return;
  const id = item.dataset.profile;

  if (button.dataset.profileAct === "use") {
    await withBusy(button, "连接中…", async () => {
      clearSettingResult();
      try {
        const result = await api(
          `/api/settings/llm/profiles/${encodeURIComponent(id)}/apply`,
          { method: "POST" },
        );
        S.settings = result.settings;
        renderSettingState();
        renderProfiles();
        $("settingBaseUrl").value = S.settings.base_url;
        $("settingModel").value = S.settings.model;
        $("settingProvider").value = matchPresetId(S.settings.base_url);
        $("settingKey").value = "";
        settingResult(
          `${result.test ? result.test.message + "；" : ""}已切换到这个配置`,
          "ok",
        );
        toast("已切换 AI 配置，无需重启");
        await loadStatus();
        renderLibrary();
      } catch (error) { settingResult(error.message, "err"); }
    });
    return;
  }

  if (button.dataset.profileAct === "del") {
    if (!await confirmDialog("从历史记录中删除这个配置？当前正在使用的设置不会改变。", {
      title: "删除 AI 配置",
      confirmLabel: "删除",
      danger: true,
    })) return;
    try {
      const result = await api(`/api/settings/llm/profiles/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      S.settings = { ...S.settings, profiles: result.profiles };
      renderProfiles();
      toast("已从历史记录中删除");
    } catch (error) { settingResult(error.message, "err"); }
  }
});

async function openSettings() {
  clearSettingResult();
  $("settingKey").value = "";
  $("settingKey").type = "password";
  try {
    S.settings = await api("/api/settings/llm");
  } catch (error) {
    toast(`读取设置失败：${error.message}`, "err");
    return;
  }

  const provider = $("settingProvider");
  provider.innerHTML = (S.settings.presets || [])
    .map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("");
  $("settingBaseUrl").value = S.settings.base_url;
  $("settingModel").value = S.settings.model;
  provider.value = matchPresetId(S.settings.base_url);
  const current = (S.settings.presets || []).find((p) => p.id === provider.value);
  $("providerHint").textContent = current ? current.hint || "" : "";

  renderSettingState();
  renderProfiles();
  $("settingsMask").hidden = false;
}

function closeSettings() { $("settingsMask").hidden = true; }

on("openSettings", "click", openSettings);
on("settingsClose", "click", closeSettings);
on("settingsMask", "click", (event) => {
  if (event.target.id === "settingsMask") closeSettings();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("settingsMask").hidden) closeSettings();
});

on("settingProvider", "change", (event) => {
  clearSettingResult();
  applyPreset(event.target.value);
});

on("settingKeyPeek", "click", () => {
  const input = $("settingKey");
  input.type = input.type === "password" ? "text" : "password";
});

function settingPayload() {
  return {
    api_key: $("settingKey").value,
    base_url: $("settingBaseUrl").value,
    model: $("settingModel").value,
    provider: $("settingProvider").value,
  };
}

/** 置灰按钮并给出“正在连接”反馈：这一步要真发网络请求，可能等几秒。 */
async function withBusy(button, label, task) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try { return await task(); }
  finally { button.disabled = false; button.textContent = original; }
}

on("settingTest", "click", () =>
  withBusy($("settingTest"), "连接中…", async () => {
    clearSettingResult();
    try {
      const result = await api("/api/settings/llm/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settingPayload()),
      });
      settingResult(result.message, result.ok ? "ok" : "err");
    } catch (error) { settingResult(error.message, "err"); }
  }));

on("settingSave", "click", () =>
  withBusy($("settingSave"), "测试并保存中…", async () => {
    clearSettingResult();
    try {
      const result = await api("/api/settings/llm", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...settingPayload(), test: true }),
      });
      S.settings = result.settings;
      renderSettingState();
      renderProfiles();
      $("settingKey").value = "";
      settingResult(
        `${result.test ? result.test.message + "；" : ""}已保存，现在就可以用 AI 抽取了`,
        "ok",
      );
      toast("AI 服务已连接，无需重启");
      await loadStatus();
      renderLibrary();
    } catch (error) { settingResult(error.message, "err"); }
  }));

on("settingClear", "click", async () => {
  if (!await confirmDialog("清除已保存的密钥？之后抽取会改用离线关键词规则。", {
    title: "清除 AI 密钥",
    confirmLabel: "清除密钥",
    danger: true,
  })) return;
  try {
    const result = await api("/api/settings/llm", { method: "DELETE" });
    S.settings = result.settings;
    renderSettingState();
    renderProfiles();
    settingResult("密钥已清除，当前使用离线规则引擎。历史配置仍保留，可随时切回。", "ok");
    await loadStatus();
    renderLibrary();
  } catch (error) { settingResult(error.message, "err"); }
});

/* ------------------------------------------------------------------ */
/* first-run guide                                                     */
/* ------------------------------------------------------------------ */

const GUIDE_KEY = "cheminfo-guide-hidden";

(function initGuide() {
  let hidden = null;
  try { hidden = localStorage.getItem(GUIDE_KEY); } catch (_) { }
  if (hidden === "1") $("guideBar").hidden = true;
})();

on("guideHide", "click", () => {
  $("guideBar").hidden = true;
  try { localStorage.setItem(GUIDE_KEY, "1"); } catch (_) { }
});

/* ------------------------------------------------------------------ */
/* review (提取结果)                                                   */
/* ------------------------------------------------------------------ */

/** 卡片式论文切换器：默认只占一行，多出来的折叠，可展开。 */
function renderPaperSwitch() {
  const box = $("paperSwitch");
  const more = $("switchMore");
  const query = reviewFieldQuery;
  if (query && reviewFieldMatchIds === null) {
    box.innerHTML = '<span class="group-note">正在搜索抽取字段…</span>';
    more.hidden = true;
    return;
  }
  const folder = $("reviewFolderFilter").value;
  const usable = S.papers.filter((paper) => {
    if (!paper.n_records) return false;
    if (folder === "none" && paper.folder_fk) return false;
    if (folder && folder !== "none" && String(paper.folder_fk) !== folder) return false;
    if (!query) return true;
    const metadata = [paper.title, paper.filename, paper.paper_key]
      .join(" ").toLowerCase().includes(query);
    return metadata || reviewFieldMatchIds.has(Number(paper.id));
  });
  if (!usable.length) {
    box.innerHTML = `<span class="group-note">${S.papers.some((paper) => paper.n_records)
      ? "没有符合筛选条件的文献。" : "还没有已完成抽取的论文。"}</span>`;
    more.hidden = true;
    return;
  }
  box.innerHTML = usable.map((p) => `
    <button class="switch-card ${S.paper && S.paper.id === p.id ? "active" : ""}"
            data-id="${p.id}" data-brief="${p.id}">
      <span class="switch-top">
        <span class="switch-key">${esc(p.paper_key)}</span>
        <span class="switch-count">${p.n_reviewed}/${p.n_records}</span>
      </span>
      <span class="switch-title">${esc(p.title)}</span>
    </button>`).join("");
  // 布局尚未结算时量不到真实宽度，推到下一帧
  requestAnimationFrame(() => syncSwitchOverflow(usable.length));
}

function updateReviewFieldSearch() {
  clearTimeout(reviewFieldSearchTimer);
  const rawQuery = $("reviewFieldSearch").value.trim();
  reviewFieldQuery = rawQuery.toLowerCase();
  reviewFieldMatchIds = rawQuery ? null : new Set();
  const requestId = ++reviewFieldSearchRequest;
  renderPaperSwitch();
  if (!rawQuery) return;

  reviewFieldSearchTimer = setTimeout(async () => {
    try {
      const result = await api(`/api/search?q=${encodeURIComponent(rawQuery)}`);
      if (requestId !== reviewFieldSearchRequest) return;
      reviewFieldMatchIds = new Set((result.results || []).map((row) => Number(row.paper_fk)));
    } catch (error) {
      if (requestId !== reviewFieldSearchRequest) return;
      reviewFieldMatchIds = new Set();
      toast(`字段搜索失败：${error.message}`, "err");
    }
    renderPaperSwitch();
  }, 250);
}

on("reviewFolderFilter", "change", renderPaperSwitch);
on("reviewFieldSearch", "input", updateReviewFieldSearch);

/** 判断一行是否装不下，据此显示“展开 / 收起”按钮。 */
function syncSwitchOverflow(total) {
  const box = $("paperSwitch");
  const more = $("switchMore");
  const expanded = !box.classList.contains("collapsed");

  if (expanded) {
    more.hidden = false;
    more.textContent = "收起";
    return;
  }

  // 视图被 display:none 隐藏时量不到宽度（clientWidth 为 0），
  // 此时不能据此判定“没有溢出”，否则按钮会被永久藏起来，
  // 直到某次在可见状态下重新渲染才出现。保留当前状态，等视图可见再量。
  if (!box.clientWidth) return;

  // 折叠时是 nowrap，溢出表现为横向超宽
  const overflowing = box.scrollWidth > box.clientWidth + 2;
  more.hidden = !overflowing;
  if (!overflowing) return;

  // nowrap 下所有卡片 offsetTop 相同，只能按横向可见范围统计
  const cards = [...box.querySelectorAll(".switch-card")];
  const limit = box.clientWidth + 2;
  const base = cards.length ? cards[0].offsetLeft : 0;
  const visible = cards.filter((c) => c.offsetLeft - base + c.offsetWidth <= limit).length;
  const hidden = Math.max(0, total - visible);
  more.textContent = hidden ? `展开 +${hidden}` : "展开";

  // 当前选中的论文若被折叠在外，横向滚动到可见位置
  const active = box.querySelector(".switch-card.active");
  if (active) active.scrollIntoView({ inline: "nearest", block: "nearest" });
}

on("switchMore", "click", () => {
  const box = $("paperSwitch");
  box.classList.toggle("collapsed");
  syncSwitchOverflow(box.querySelectorAll(".switch-card").length);
});

window.addEventListener("resize", () => {
  const box = $("paperSwitch");
  if (box.querySelector(".switch-card")) {
    syncSwitchOverflow(box.querySelectorAll(".switch-card").length);
  }
});

on("paperSwitch", "click", async (event) => {
  const card = event.target.closest(".switch-card");
  if (!card) return;
  hideBrief();
  const id = Number(card.dataset.id);
  if (S.paper && S.paper.id === id) return;
  if (S.dirty.size && !await confirmDialog("当前记录有未保存的修改，切换会丢失。继续？", {
    title: "放弃未保存的修改",
    confirmLabel: "继续切换",
  })) return;
  await openPaper(id);
});

bindBriefHover($("paperSwitch"));

function renderAskScope() {
  const scope = $("askScope");
  const current = scope.value;
  const shared = S.sharedPapers
    ? `全部论文（共享语料 ${S.sharedPapers} 篇）`
    : "全部论文（共享语料）";
  const folders = S.folders.length
    ? `<optgroup label="按文件夹">` + S.folders
      .map((f) => `<option value="folder:${f.id}">📁 ${esc(f.name)}（${f.n_papers} 篇）</option>`)
      .join("") + "</optgroup>"
    : "";
  const papers = S.papers.length
    ? `<optgroup label="单篇论文">` + S.papers
      .map((p) => `<option value="${p.id}">${esc(p.paper_key)} · ${esc(p.title)}</option>`)
      .join("") + "</optgroup>"
    : "";
  scope.innerHTML = `<option value="">${esc(shared)}</option>` + folders + papers;
  if (current && [...scope.options].some((o) => o.value === current)) scope.value = current;
  syncAskScopeLabel();
}

/** 问答范围说明文字；多选优先于下拉。 */
function syncAskScopeLabel() {
  const label = $("askScopeLabel");
  if (S.askPicked.size) {
    label.textContent = `已勾选 ${S.askPicked.size} 篇论文`;
    return;
  }
  const option = $("askScope").selectedOptions[0];
  label.textContent = option ? option.textContent : "全部论文（共享语料）";
}

/** 把当前范围翻译成请求体。 */
function askScopePayload() {
  if (S.askPicked.size) return { paper_fks: [...S.askPicked] };
  const value = $("askScope").value;
  if (!value) return {};
  if (value.startsWith("folder:")) return { folder_fk: Number(value.slice(7)) };
  return { paper_fk: Number(value) };
}

function renderAskPickList() {
  const box = $("askPickList");
  if (!S.papers.length) {
    box.innerHTML = '<div class="placeholder">你的文献库还没有论文。</div>';
    return;
  }
  const groups = new Map();
  S.papers.forEach((p) => {
    const folder = S.folders.find((f) => f.id === p.folder_fk);
    const name = folder ? folder.name : "未归档";
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(p);
  });
  box.innerHTML = [...groups.entries()].map(([name, items]) => `
    <div class="ask-pick-group">
      <div class="ask-pick-group-head">${esc(name)}</div>
      ${items.map((p) => `
        <label class="ask-pick-item">
          <input type="checkbox" data-ask-pick="${p.id}"
                 ${S.askPicked.has(p.id) ? "checked" : ""} />
          <span>${esc(p.paper_key)}</span>
          <em title="${esc(p.title)}">${esc(p.title)}</em>
        </label>`).join("")}
    </div>`).join("");
}

on("askPickBtn", "click", () => {
  const panel = $("askPick");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) renderAskPickList();
});

on("askPickList", "change", (event) => {
  const box = event.target.closest("[data-ask-pick]");
  if (!box) return;
  const id = Number(box.dataset.askPick);
  if (box.checked) S.askPicked.add(id); else S.askPicked.delete(id);
  syncAskScopeLabel();
});

on("askPickClear", "click", () => {
  S.askPicked.clear();
  renderAskPickList();
  syncAskScopeLabel();
});

on("askPickDone", "click", () => {
  $("askPick").hidden = true;
  syncAskScopeLabel();
  if (S.askPicked.size) toast(`问答将只参考勾选的 ${S.askPicked.size} 篇论文`);
});

async function openPaper(paperFk) {
  S.paper = await api(`/api/papers/${paperFk}`);
  S.recordIndex = 0;
  S.dirty.clear();
  S.activeField = null;
  $("regionResult").hidden = true;

  const job = S.paper.latest_job;
  $("reviewMeta").innerHTML = [
    `${S.paper.pages} 页`,
    `${S.paper.n_records} 条记录`,
    `${S.paper.n_reviewed} 条已复核`,
    job ? `引擎 ${esc(job.engine || "—")}` : "未抽取",
    job && job.model ? `模型 ${esc(job.model)}` : "",
  ].filter(Boolean).map((t) => `<span>${t}</span>`).join("");

  renderPaperSwitch();
  renderRecordTabs();
  renderFields();
  await showPage(firstEvidencePage() || 1);
  if (!$("regionHistory").hidden && $("regionHistoryScope").value === "current") {
    loadRegionHistory();
  }
}

function currentRecord() {
  return S.paper && S.paper.records ? S.paper.records[S.recordIndex] : null;
}

function firstEvidencePage() {
  const record = currentRecord();
  if (!record) return null;
  for (const key of S.fields.map((f) => f.key)) {
    const items = record.evidence[key];
    if (items && items.length) return items[0].page;
  }
  return null;
}

function renderRecordTabs() {
  const records = (S.paper && S.paper.records) || [];
  $("recordTabs").innerHTML = records.map((r, i) =>
    `<button class="record-tab ${i === S.recordIndex ? "active" : ""} ${r.reviewed ? "done" : ""}"
             data-i="${i}">样品 ${r.record_index}</button>`).join("");
}

on("recordTabs", "click", async (event) => {
  const tab = event.target.closest(".record-tab");
  if (!tab) return;
  if (S.dirty.size && !await confirmDialog("当前记录有未保存的修改，切换会丢失。继续？", {
    title: "放弃未保存的修改",
    confirmLabel: "继续切换",
  })) return;
  S.recordIndex = Number(tab.dataset.i);
  S.dirty.clear();
  renderRecordTabs();
  renderFields();
  if (!$("historyBox").hidden) loadHistory();
  showPage(firstEvidencePage() || S.page);
});

function renderFields() {
  const record = currentRecord();
  const list = $("fieldList");
  if (!record) {
    list.innerHTML = '<div class="placeholder">这篇论文还没有抽取记录，请先在文献库创建抽取任务。</div>';
    $("reviewFoot").hidden = true;
    return;
  }
  $("reviewFoot").hidden = false;
  $("reviewedFlag").checked = record.reviewed;

  list.innerHTML = S.fields.map((f) => {
    const value = record.fields[f.key];
    const items = record.evidence[f.key] || [];
    const conf = record.confidence[f.key];
    return `
    <div class="field-row ${value ? "" : "empty-val"}" data-key="${f.key}">
      <label class="field-label" title="${esc(f.key)}">${esc(f.label)}</label>
      <input class="field-input" data-key="${f.key}"
             value="${esc(value ?? "")}" placeholder="null（未报道）" />
      <div class="field-side">
        ${conf !== undefined ? `<span class="conf">${(conf * 100).toFixed(0)}%</span>` : ""}
        ${items.map((item, index) => `<span class="page-pill" data-page="${item.page}"
             data-key="${f.key}" data-idx="${index}">p.${item.page}</span>`).join("")}
      </div>
    </div>`;
  }).join("");
}

on("fieldList", "click", (event) => {
  const pill = event.target.closest(".page-pill");
  const row = event.target.closest(".field-row");
  if (pill) {
    event.stopPropagation();
    focusField(pill.dataset.key, Number(pill.dataset.page), Number(pill.dataset.idx));
  } else if (row) {
    focusField(row.dataset.key);
  }
});

on("fieldList", "input", (event) => {
  const input = event.target.closest(".field-input");
  if (!input) return;
  const record = currentRecord();
  const original = record.fields[input.dataset.key] ?? "";
  const changed = input.value !== String(original);
  input.classList.toggle("dirty", changed);
  if (changed) S.dirty.set(input.dataset.key, input.value);
  else S.dirty.delete(input.dataset.key);
});

/** Field -> original text: the core bidirectional-locating interaction. */
async function focusField(key, page = null, index = 0) {
  const record = currentRecord();
  if (!record) return;
  S.activeField = key;
  document.querySelectorAll(".field-row").forEach((row) =>
    row.classList.toggle("active", row.dataset.key === key));

  const items = record.evidence[key] || [];
  const item = items[index] || items[0];
  if (!item) {
    $("evidenceBox").hidden = true;
    $("evidenceMiss").hidden = true;
    if (page) await showPage(page);
    else toast("该字段没有证据引文（可能为 null 或人工填写）", "warn");
    return;
  }
  $("evidenceBox").hidden = false;
  $("evidenceQuote").textContent = item.quote;
  $("evidenceZh").hidden = true;
  $("evidenceTranslate").textContent = "译";
  await showPage(page || item.page, item.quote);
}

async function showPage(page, highlight = null) {
  if (!S.paper) return;
  const total = S.paper.pages;
  S.page = Math.max(1, Math.min(total, page));
  S.highlight = highlight;
  $("regionResult").hidden = true;
  $("regionBox").hidden = true;
  $("pageLabel").textContent = `${S.page} / ${total}`;

  if (S.pageView === "image") await renderPageImage();
  else if (S.pageView === "zh") await renderPageZh();
  else await renderPageText();
}

/** Whitespace-tolerant quote highlighting: PDF text layers break lines mid-sentence. */
function highlightQuote(text, quote) {
  // Retrieval snippets are elided with "…"; strip the markers and use the
  // longest clean fragment as the needle.
  const needle = String(quote || "")
    .split("…")
    .map((part) => part.trim())
    .sort((a, b) => b.length - a.length)[0]
    ?.replace(/\s+/g, " ") || "";
  if (!needle) return esc(text);

  const build = (probe) => {
    const pattern = probe.split(" ").map((word) =>
      word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+");
    try { return new RegExp(pattern, "i"); } catch (_) { return null; }
  };

  const words = needle.split(" ");
  const sizes = [...new Set([
    words.length,
    Math.ceil(words.length * 0.6),
    Math.ceil(words.length * 0.3),
    Math.min(8, words.length),
  ])].filter((n) => n >= 3);
  for (const size of sizes) {
    const regex = build(words.slice(0, size).join(" "));
    if (!regex) continue;
    const match = regex.exec(text);
    if (match) {
      return esc(text.slice(0, match.index)) +
        "<mark>" + esc(match[0]) + "</mark>" +
        esc(text.slice(match.index + match[0].length));
    }
  }
  return esc(text);
}

/* ---- 原版视图：页面图片 + 坐标高亮 ---- */

/** 证据框按页面比例定位，所以缩放时不需要重新请求坐标。 */
function paintMarks(boxes) {
  const marks = $("pageMarks");
  marks.innerHTML = (boxes || []).map((box) =>
    `<i style="left:${(box.x * 100).toFixed(3)}%;top:${(box.y * 100).toFixed(3)}%;`
    + `width:${(box.w * 100).toFixed(3)}%;height:${(box.h * 100).toFixed(3)}%"></i>`
  ).join("");
}

async function renderPageImage() {
  const canvas = $("pageCanvas");
  const image = $("pageImage");
  const hint = $("pageImageHint");

  image.style.width = `${S.zoom * 100}%`;
  const src = `/api/papers/${S.paper.id}/pages/${S.page}/image?dpi=150`;

  if (image.dataset.pageSrc !== src) {
    hint.hidden = false;
    hint.textContent = "正在渲染该页…";
    canvas.hidden = true;

    // 用 fetch 取图而不是直接给 <img> 赋 src：失败时后端返回的是 JSON 错误，
    // <img> 的 onerror 看不到内容，用户只会看到笼统的「渲染不成功」。
    try {
      const response = await fetch(src, { credentials: "same-origin" });
      if (!response.ok) {
        let reason = `HTTP ${response.status}`;
        try { reason = (await response.json()).error || reason; } catch (_) { }
        if (response.status === 404 || response.status === 405) {
          reason += "（若反复出现，多半是后端跑的是旧版代码，请在项目目录重启服务）";
        }
        hint.innerHTML =
          `无法渲染该页：${esc(reason)}<br/>可切换到上方「文本」视图继续查看文字。`;
        delete image.dataset.pageSrc;
        return;
      }
      const blob = await response.blob();
      const previous = image.dataset.objectUrl;
      if (previous) URL.revokeObjectURL(previous);
      const url = URL.createObjectURL(blob);
      image.dataset.objectUrl = url;
      image.dataset.pageSrc = src;
      image.src = url;
      hint.hidden = true;
      canvas.hidden = false;
    } catch (error) {
      hint.innerHTML =
        `无法渲染该页：${esc(error.message)}<br/>可切换到上方「文本」视图继续查看文字。`;
      return;
    }
  } else {
    hint.hidden = true;
    canvas.hidden = false;
  }

  paintMarks([]);
  $("evidenceMiss").hidden = true;
  $("regionBox").hidden = true;
  if (!S.highlight) return;

  // 坐标由后端用 PyMuPDF 在真实页面上搜索得到，比前端字符串匹配更准。
  try {
    const params = new URLSearchParams({ quote: S.highlight });
    const result = await api(
      `/api/papers/${S.paper.id}/pages/${S.page}/locate?${params}`);
    if (result.found) {
      paintMarks(result.boxes);
      requestAnimationFrame(() => {
        const first = $("pageMarks").querySelector("i");
        if (first) first.scrollIntoView({ block: "center", behavior: "smooth" });
      });
    } else {
      $("evidenceMiss").hidden = false;
      $("evidenceMiss").textContent =
        "这句话在本页找不到精确位置（PDF 文字层可能被拆行），已跳到该页，请手动查找。";
    }
  } catch (error) {
    $("evidenceMiss").hidden = false;
    $("evidenceMiss").textContent = `定位失败：${error.message}`;
  }
}

/* ---- 文本视图：纯文字 + 结构化表格 ---- */

/** 把抽到的行列还原成真正的表格，而不是竖线拼接的字符串。 */
function tableHtml(table) {
  const rows = table.rows || [];
  if (!rows.length) return "";
  const [header, ...body] = rows;
  return `
    <div class="page-table-wrap">
      <div class="page-table-cap">表格 ${esc(table.table_id)}</div>
      <table class="page-table">
        <thead><tr>${header.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
        <tbody>${body.map((row) =>
    `<tr>${row.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody>
      </table>
    </div>`;
}

async function renderPageText() {
  const box = $("pageText");
  const cacheKey = `${S.paper.id}:${S.page}`;
  let data = S.pageCache.get(cacheKey);
  if (data === undefined) {
    try {
      data = await api(`/api/papers/${S.paper.id}/pages/${S.page}`);
    } catch (error) {
      data = { text: "", tables: [] };
    }
    S.pageCache.set(cacheKey, data);
  }

  const text = data.text || "";
  const tables = data.tables || [];
  if (!text.trim() && !tables.length) {
    box.innerHTML = '<div class="placeholder">该页没有可用文本层（可能是纯图片页）。可切换到「原版」视图查看。</div>';
    return;
  }

  // 表格由下方结构化渲染，正文里的 [[TABLE x]] 文本块不再重复显示。
  // page_with_tables() 把表格统一拼在正文之后，所以从第一个标记处截断即可；
  // 旧写法用贪婪正则向后吞到段落边界，页面没有空行时会连正文一起删掉。
  const marker = text.search(/\[\[TABLE /);
  const body = (marker >= 0 && tables.length ? text.slice(0, marker) : text).trim();
  box.innerHTML =
    `<div class="page-text-body">${S.highlight ? highlightQuote(body, S.highlight) : esc(body)}</div>`
    + tables.map(tableHtml).join("");

  const mark = box.querySelector("mark");
  if (mark) mark.scrollIntoView({ block: "center", behavior: "smooth" });
  else box.scrollTop = 0;
}

/* ---- 视图切换与缩放 ---- */

const PAGE_VIEW_KEY = "cheminfo-page-view";
const PROVIDER_KEY = "cheminfo-translate-provider";

/** 翻译服务列表：没有 AI 密钥时也能用免费的 MyMemory。 */
async function loadTranslateProviders() {
  const select = $("translateProvider");
  if (!select) return;
  try {
    const data = await api("/api/translate/providers");
    let saved = null;
    try { saved = localStorage.getItem(PROVIDER_KEY); } catch (_) { }
    select.innerHTML = (data.providers || []).map((p) =>
      `<option value="${esc(p.id)}" title="${esc(p.note)}" ${p.ready ? "" : "disabled"}>
        ${esc(p.label)}${p.ready ? "" : "（未配置）"}
      </option>`).join("");
    const usable = (data.providers || []).filter((p) => p.ready).map((p) => p.id);
    select.value = usable.includes(saved) ? saved : (usable[0] || "");
    S.translateProvider = select.value;
  } catch (_) {
    select.innerHTML = '<option value="">翻译服务不可用</option>';
  }
}

on("translateProvider", "change", (event) => {
  S.translateProvider = event.target.value;
  try { localStorage.setItem(PROVIDER_KEY, S.translateProvider); } catch (_) { }
  // 换了服务，之前的译文缓存不再适用
  S.zhCache.clear();
  if (S.pageView === "zh") showPage(S.page, S.highlight);
});

function syncPageView() {
  const isImage = S.pageView === "image";
  const isZh = S.pageView === "zh";
  $("pageImageWrap").hidden = !isImage;
  $("pageText").hidden = isImage || isZh;
  $("pageZh").hidden = !isZh;
  // 缩放与框选只对原版视图有意义
  $("pageTools").hidden = !isImage;
  if (!isImage) {
    closeTranslationTools();
    $("regionHistory").hidden = true;
    $("regionHistoryToggle").setAttribute("aria-expanded", "false");
  }
  document.querySelectorAll("[data-pageview]").forEach((button) =>
    button.classList.toggle("active", button.dataset.pageview === S.pageView));
  try { localStorage.setItem(PAGE_VIEW_KEY, S.pageView); } catch (_) { }
}

/* ---- 中文视图：整页译文 ---- */

async function renderPageZh() {
  const box = $("pageZh");
  const cacheKey = `${S.paper.id}:${S.page}`;
  const hit = S.zhCache.get(cacheKey);
  if (hit !== undefined) {
    box.innerHTML = `<div class="page-text-body">${esc(hit)}</div>`;
    box.scrollTop = 0;
    return;
  }

  box.innerHTML = '<div class="placeholder">正在翻译本页…</div>';
  let acc = "";
  let started = false;
  try {
    await readStream(
      `/api/papers/${S.paper.id}/pages/${S.page}/translate/stream`,
      { provider: S.translateProvider || undefined },
      {
        delta: (payload) => {
          acc += payload.text || "";
          if (!started) {
            box.innerHTML = '<div class="page-text-body" id="zhBody"></div>';
            started = true;
          }
          const body = document.getElementById("zhBody");
          if (body) body.textContent = acc;
        },
        error: (payload) => {
          box.innerHTML =
            `<div class="placeholder">翻译失败：${esc(payload.message || "未知错误")}<br/>`
            + "可在工具菜单里换一个翻译服务，或先连接 AI 服务。</div>";
          acc = "";
        },
      },
    );
    if (acc) S.zhCache.set(cacheKey, acc);
  } catch (error) {
    box.innerHTML =
      `<div class="placeholder">翻译失败：${esc(error.message)}<br/>`
      + "可在工具菜单里换一个翻译服务，或先连接 AI 服务。</div>";
  }
}

/* ---- 证据引文翻译 ---- */

on("evidenceTranslate", "click", async () => {
  const quote = $("evidenceQuote").textContent.trim();
  if (!quote) return;
  const zh = $("evidenceZh");
  const button = $("evidenceTranslate");
  if (!zh.hidden) { zh.hidden = true; button.textContent = "译"; return; }

  await withBusy(button, "翻译中…", async () => {
    try {
      const result = await api("/api/translate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: quote, provider: S.translateProvider || undefined }),
      });
      zh.hidden = false;
      zh.textContent = result.text;
      button.textContent = "收起";
    } catch (error) {
      toast(error.message, "err");
    }
  });
});

(function initPageView() {
  let saved = null;
  try { saved = localStorage.getItem(PAGE_VIEW_KEY); } catch (_) { }
  if (saved === "text" || saved === "image" || saved === "zh") S.pageView = saved;
  syncPageView();
})();

on("pageViewToggle", "click", async (event) => {
  const button = event.target.closest("[data-pageview]");
  if (!button || button.dataset.pageview === S.pageView) return;
  S.pageView = button.dataset.pageview;
  syncPageView();
  await showPage(S.page, S.highlight);
});

const REGION_HISTORY_SPLIT_KEY = "mesoscope-region-history-height";
const REGION_RESULT_SPLIT_KEY = "mesoscope-region-result-height";
let syncWorkbenchCross = () => {};
let syncRegionResultSplit = () => {};
let applyRegionHistoryHeight = () => {};

/** 翻译记录和抽取记录之间的分隔条可拖动调整高度。 */
(function initRegionHistorySplit() {
  const panel = document.querySelector(".field-panel");
  const history = $("regionHistory");
  const bar = $("regionHistorySplit");
  const recordsHead = document.querySelector(".field-records .panel-head");
  const footer = $("reviewFoot");
  if (!panel || !history || !bar || !recordsHead || !footer) return;

  let ratio = 0.38;
  try {
    const saved = parseFloat(localStorage.getItem(REGION_HISTORY_SPLIT_KEY));
    if (Number.isFinite(saved)) ratio = saved;
  } catch (_) { }

  const getBounds = () => {
    const panelHeight = panel.clientHeight;
    const fixedHeight = recordsHead.offsetHeight
      + (footer.hidden ? 0 : footer.offsetHeight)
      + 6;
    const maxHeight = Math.max(100, panelHeight - fixedHeight - 100);
    return { panelHeight, minHeight: Math.min(100, maxHeight), maxHeight };
  };

  const applyHeight = (height) => {
    const { panelHeight, minHeight, maxHeight } = getBounds();
    if (!panelHeight) return;
    const clampedHeight = Math.min(maxHeight, Math.max(minHeight, height));
    ratio = clampedHeight / panelHeight;
    history.style.flex = `0 0 ${ratio * 100}%`;
    bar.setAttribute("aria-valuemin", String(Math.round(minHeight)));
    bar.setAttribute("aria-valuemax", String(Math.round(maxHeight)));
    bar.setAttribute("aria-valuenow", String(Math.round(clampedHeight)));
    syncWorkbenchCross();
  };
  applyRegionHistoryHeight = applyHeight;

  const applySavedHeight = () => {
    const { panelHeight } = getBounds();
    if (panelHeight) applyHeight(panelHeight * ratio);
  };

  let dragging = false;
  bar.addEventListener("pointerdown", (event) => {
    dragging = true;
    bar.setPointerCapture(event.pointerId);
    bar.classList.add("dragging");
    event.preventDefault();
  });
  bar.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const panelTop = panel.getBoundingClientRect().top + panel.clientTop;
    applyHeight(event.clientY - panelTop);
  });
  const stopDragging = () => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove("dragging");
    try { localStorage.setItem(REGION_HISTORY_SPLIT_KEY, String(ratio)); } catch (_) { }
  };
  bar.addEventListener("pointerup", stopDragging);
  bar.addEventListener("pointercancel", stopDragging);
  bar.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    applyHeight(history.getBoundingClientRect().height + (event.key === "ArrowUp" ? -16 : 16));
    try { localStorage.setItem(REGION_HISTORY_SPLIT_KEY, String(ratio)); } catch (_) { }
  });

  on("regionHistoryToggle", "click", () => requestAnimationFrame(() => {
    applySavedHeight();
    syncWorkbenchCross();
  }));
  window.addEventListener("resize", () => {
    applySavedHeight();
    syncWorkbenchCross();
  });
  requestAnimationFrame(applySavedHeight);
})();

function setZoom(value) {
  S.zoom = Math.max(0.5, Math.min(3, Number(value.toFixed(2))));
  $("zoomInput").value = String(Math.round(S.zoom * 100));
  $("pageImage").style.width = `${S.zoom * 100}%`;
}

/* ---- 页面工具与左右分栏 ---- */

const SPLIT_KEY = "cheminfo-split";
let applyWorkbenchSplit = () => {};

/** 左右面板比例可拖动；记住用户的选择。 */
(function initSplit() {
  const bar = $("workbenchSplit");
  const bench = document.querySelector(".workbench");
  if (!bar || !bench) return;

  const apply = (ratio) => {
    const value = Math.min(0.75, Math.max(0.25, ratio));
    bench.style.gridTemplateColumns =
      `minmax(0, ${value}fr) 6px minmax(0, ${1 - value}fr)`;
    S.splitRatio = value;
    syncWorkbenchCross();
    syncRegionResultSplit();
  };
  applyWorkbenchSplit = apply;

  let saved = null;
  try { saved = parseFloat(localStorage.getItem(SPLIT_KEY)); } catch (_) { }
  apply(Number.isFinite(saved) ? saved : 0.5);

  let dragging = false;
  bar.addEventListener("pointerdown", (event) => {
    dragging = true;
    bar.setPointerCapture(event.pointerId);
    bar.classList.add("dragging");
  });
  bar.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const rect = bench.getBoundingClientRect();
    apply((event.clientX - rect.left) / rect.width);
  });
  const stop = () => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove("dragging");
    try { localStorage.setItem(SPLIT_KEY, String(S.splitRatio)); } catch (_) { }
  };
  bar.addEventListener("pointerup", stop);
  bar.addEventListener("pointercancel", stop);
  // 键盘可达：左右方向键微调
  bar.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    apply(S.splitRatio + (event.key === "ArrowLeft" ? -0.03 : 0.03));
    try { localStorage.setItem(SPLIT_KEY, String(S.splitRatio)); } catch (_) { }
  });
})();

/** 交点拖动时同时更新左右栏宽度和翻译记录高度。 */
(function initWorkbenchCrossHandle() {
  const bench = document.querySelector(".workbench");
  const verticalBar = $("workbenchSplit");
  const historyPanel = $("regionHistory");
  const horizontalBar = $("regionHistorySplit");
  const handle = $("workbenchCrossHandle");
  if (!bench || !verticalBar || !historyPanel || !horizontalBar || !handle) return;
  const fieldPanel = historyPanel.parentElement;
  if (!fieldPanel) return;

  const syncPosition = () => {
    if (historyPanel.hidden || !bench.clientWidth) {
      horizontalBar.hidden = true;
      handle.hidden = true;
      return;
    }

    const benchRect = bench.getBoundingClientRect();
    const verticalRect = verticalBar.getBoundingClientRect();
    const historyRect = historyPanel.getBoundingClientRect();
    const fieldRect = fieldPanel.getBoundingClientRect();
    const crossX = verticalRect.left + verticalRect.width / 2 - benchRect.left;
    const crossY = historyRect.bottom - benchRect.top;
    horizontalBar.style.left = `${crossX}px`;
    horizontalBar.style.top = `${crossY - 3}px`;
    horizontalBar.style.width = `${Math.max(0, fieldRect.right - benchRect.left - crossX - 12)}px`;
    handle.style.left = `${crossX}px`;
    handle.style.top = `${crossY}px`;
    horizontalBar.hidden = false;
    handle.hidden = false;
  };
  syncWorkbenchCross = syncPosition;

  const saveRatios = () => {
    try {
      localStorage.setItem(SPLIT_KEY, String(S.splitRatio));
      localStorage.setItem(
        REGION_HISTORY_SPLIT_KEY,
        String(historyPanel.getBoundingClientRect().height / historyPanel.parentElement.clientHeight),
      );
    } catch (_) { }
  };

  let dragging = false;
  handle.addEventListener("pointerenter", () => bench.classList.add("cross-hover"));
  handle.addEventListener("pointerleave", () => bench.classList.remove("cross-hover"));
  handle.addEventListener("focus", () => bench.classList.add("cross-focus"));
  handle.addEventListener("blur", () => bench.classList.remove("cross-focus"));
  handle.addEventListener("pointerdown", (event) => {
    dragging = true;
    handle.setPointerCapture(event.pointerId);
    bench.classList.add("cross-dragging");
    event.preventDefault();
  });
  handle.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const benchRect = bench.getBoundingClientRect();
    const panel = historyPanel.parentElement;
    const panelTop = panel.getBoundingClientRect().top + panel.clientTop;
    applyWorkbenchSplit((event.clientX - benchRect.left) / benchRect.width);
    applyRegionHistoryHeight(event.clientY - panelTop);
    syncPosition();
    syncRegionResultSplit();
  });
  const stopDragging = () => {
    if (!dragging) return;
    dragging = false;
    bench.classList.remove("cross-dragging");
    saveRatios();
  };
  handle.addEventListener("pointerup", stopDragging);
  handle.addEventListener("pointercancel", stopDragging);
  handle.addEventListener("keydown", (event) => {
    const horizontalDelta = event.key === "ArrowLeft" ? -0.03 : event.key === "ArrowRight" ? 0.03 : 0;
    const verticalDelta = event.key === "ArrowUp" ? -16 : event.key === "ArrowDown" ? 16 : 0;
    if (!horizontalDelta && !verticalDelta) return;
    event.preventDefault();
    if (horizontalDelta) applyWorkbenchSplit(S.splitRatio + horizontalDelta);
    if (verticalDelta) {
      applyRegionHistoryHeight(
        historyPanel.getBoundingClientRect().height + verticalDelta,
      );
    }
    syncPosition();
    saveRatios();
  });

  new MutationObserver(syncPosition).observe(historyPanel, {
    attributes: true,
    attributeFilter: ["hidden"],
  });
  window.addEventListener("resize", syncPosition);
  requestAnimationFrame(syncPosition);
})();

/** 框选翻译结果栏可拖动调整高度，并可在与宽度分隔条的交点联动调整。 */
(function initRegionResultSplit() {
  const bench = document.querySelector(".workbench");
  const pdfPanel = document.querySelector(".pdf-panel");
  const verticalBar = $("workbenchSplit");
  const result = $("regionResult");
  const bar = $("regionResultSplit");
  const handle = $("workbenchResultCrossHandle");
  const evidenceBox = $("evidenceBox");
  if (!bench || !pdfPanel || !verticalBar || !result || !bar || !handle) return;
  const panelHead = pdfPanel.querySelector(".panel-head");
  if (!panelHead || !evidenceBox) return;

  let ratio = 0.34;
  try {
    const saved = parseFloat(localStorage.getItem(REGION_RESULT_SPLIT_KEY));
    if (Number.isFinite(saved)) ratio = saved;
  } catch (_) { }

  const getBounds = () => {
    const panelHeight = pdfPanel.clientHeight;
    const fixedHeight = panelHead.offsetHeight
      + (evidenceBox.hidden ? 0 : evidenceBox.offsetHeight);
    const maxHeight = Math.max(100, panelHeight - fixedHeight - 120);
    return { panelHeight, minHeight: Math.min(100, maxHeight), maxHeight };
  };

  const applyHeight = (height) => {
    const { panelHeight, minHeight, maxHeight } = getBounds();
    if (!panelHeight) return;
    const clampedHeight = Math.min(maxHeight, Math.max(minHeight, height));
    ratio = clampedHeight / panelHeight;
    result.style.flex = `0 0 ${ratio * 100}%`;
    bar.setAttribute("aria-valuemin", String(Math.round(minHeight)));
    bar.setAttribute("aria-valuemax", String(Math.round(maxHeight)));
    bar.setAttribute("aria-valuenow", String(Math.round(clampedHeight)));
    syncPosition();
  };

  const applySavedHeight = () => {
    const { panelHeight } = getBounds();
    if (panelHeight) applyHeight(panelHeight * ratio);
  };

  const syncPosition = () => {
    if (result.hidden || !bench.clientWidth || !pdfPanel.clientHeight) {
      bar.hidden = true;
      handle.hidden = true;
      bench.classList.remove("result-cross-hover", "result-cross-dragging", "result-cross-focus");
      return;
    }

    const benchRect = bench.getBoundingClientRect();
    const verticalRect = verticalBar.getBoundingClientRect();
    const panelRect = pdfPanel.getBoundingClientRect();
    const resultRect = result.getBoundingClientRect();
    const crossX = verticalRect.left + verticalRect.width / 2 - benchRect.left;
    const crossY = resultRect.top - benchRect.top;
    bar.style.left = `${panelRect.left - benchRect.left + 12}px`;
    bar.style.top = `${crossY - 3}px`;
    bar.style.width = `${Math.max(0, crossX - (panelRect.left - benchRect.left + 12))}px`;
    handle.style.left = `${crossX}px`;
    handle.style.top = `${crossY}px`;
    bar.hidden = false;
    handle.hidden = false;
  };
  syncRegionResultSplit = syncPosition;

  const saveRatio = () => {
    try { localStorage.setItem(REGION_RESULT_SPLIT_KEY, String(ratio)); } catch (_) { }
  };

  let dragging = false;
  bar.addEventListener("pointerdown", (event) => {
    dragging = true;
    bar.setPointerCapture(event.pointerId);
    bar.classList.add("dragging");
    event.preventDefault();
  });
  bar.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const rect = pdfPanel.getBoundingClientRect();
    const panelBottom = rect.top + pdfPanel.clientTop + pdfPanel.clientHeight;
    applyHeight(panelBottom - event.clientY);
  });
  const stopDragging = () => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove("dragging");
    saveRatio();
  };
  bar.addEventListener("pointerup", stopDragging);
  bar.addEventListener("pointercancel", stopDragging);
  bar.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    applyHeight(result.getBoundingClientRect().height + (event.key === "ArrowUp" ? 16 : -16));
    saveRatio();
  });

  const saveRatios = () => {
    try {
      localStorage.setItem(SPLIT_KEY, String(S.splitRatio));
      localStorage.setItem(REGION_RESULT_SPLIT_KEY, String(ratio));
    } catch (_) { }
  };

  let crossDragging = false;
  handle.addEventListener("pointerenter", () => bench.classList.add("result-cross-hover"));
  handle.addEventListener("pointerleave", () => bench.classList.remove("result-cross-hover"));
  handle.addEventListener("focus", () => bench.classList.add("result-cross-focus"));
  handle.addEventListener("blur", () => bench.classList.remove("result-cross-focus"));
  handle.addEventListener("pointerdown", (event) => {
    crossDragging = true;
    handle.setPointerCapture(event.pointerId);
    bench.classList.add("result-cross-dragging");
    event.preventDefault();
  });
  handle.addEventListener("pointermove", (event) => {
    if (!crossDragging) return;
    const benchRect = bench.getBoundingClientRect();
    const panelRect = pdfPanel.getBoundingClientRect();
    const panelBottom = panelRect.top + pdfPanel.clientTop + pdfPanel.clientHeight;
    applyWorkbenchSplit((event.clientX - benchRect.left) / benchRect.width);
    applyHeight(panelBottom - event.clientY);
  });
  const stopCrossDragging = () => {
    if (!crossDragging) return;
    crossDragging = false;
    bench.classList.remove("result-cross-dragging");
    saveRatios();
  };
  handle.addEventListener("pointerup", stopCrossDragging);
  handle.addEventListener("pointercancel", stopCrossDragging);
  handle.addEventListener("keydown", (event) => {
    const horizontalDelta = event.key === "ArrowLeft" ? -0.03 : event.key === "ArrowRight" ? 0.03 : 0;
    const verticalDelta = event.key === "ArrowUp" ? 16 : event.key === "ArrowDown" ? -16 : 0;
    if (!horizontalDelta && !verticalDelta) return;
    event.preventDefault();
    if (horizontalDelta) applyWorkbenchSplit(S.splitRatio + horizontalDelta);
    if (verticalDelta) applyHeight(result.getBoundingClientRect().height + verticalDelta);
    syncPosition();
    saveRatios();
  });

  new MutationObserver(() => {
    if (!result.hidden) applySavedHeight();
    syncPosition();
  }).observe(result, { attributes: true, attributeFilter: ["hidden"] });
  window.addEventListener("resize", () => {
    applySavedHeight();
    syncPosition();
  });
  requestAnimationFrame(syncPosition);
})();

const MOBILE_STACK_SPLIT_KEY = "mesoscope-mobile-workbench-height";
const MOBILE_REGION_RESULT_WIDTH_KEY = "mesoscope-mobile-region-result-width";
const MOBILE_REGION_HISTORY_WIDTH_KEY = "mesoscope-mobile-region-history-width";
let setMobileStackRatio = () => {};
let moveMobileStackToY = () => {};
let persistMobileStackRatio = () => {};
let syncMobileCrossHandles = () => {};

/** 窄屏时，分隔原文定位和抽取记录的上下高度。 */
(function initMobileStackSplit() {
  const bench = document.querySelector(".workbench");
  const bar = $("mobileStackSplit");
  if (!bench || !bar) return;

  let ratio = 0.5;
  try {
    const saved = parseFloat(localStorage.getItem(MOBILE_STACK_SPLIT_KEY));
    if (Number.isFinite(saved)) ratio = saved;
  } catch (_) { }

  const availableHeight = () => {
    const gap = parseFloat(getComputedStyle(bench).rowGap) || 0;
    return bench.clientHeight - bar.offsetHeight - gap * 2;
  };
  const apply = (nextRatio) => {
    const available = availableHeight();
    if (available <= 0) return;
    const minPanelHeight = Math.min(200, available / 2);
    const firstHeight = Math.min(
      available - minPanelHeight,
      Math.max(minPanelHeight, available * nextRatio),
    );
    ratio = firstHeight / available;
    bench.style.gridTemplateRows = `${firstHeight}px ${bar.offsetHeight}px ${available - firstHeight}px`;
    bar.setAttribute("aria-valuemin", String(Math.round(minPanelHeight)));
    bar.setAttribute("aria-valuemax", String(Math.round(available - minPanelHeight)));
    bar.setAttribute("aria-valuenow", String(Math.round(firstHeight)));
    syncMobileCrossHandles();
  };
  setMobileStackRatio = apply;
  moveMobileStackToY = (clientY) => {
    const rect = bench.getBoundingClientRect();
    const available = availableHeight();
    if (available > 0) apply((clientY - rect.top) / available);
  };
  persistMobileStackRatio = () => {
    try { localStorage.setItem(MOBILE_STACK_SPLIT_KEY, String(ratio)); } catch (_) { }
  };
  const sync = () => {
    if (!window.matchMedia("(max-width: 1100px)").matches) {
      bench.style.removeProperty("grid-template-rows");
      return;
    }
    apply(ratio);
  };

  let dragging = false;
  bar.addEventListener("pointerdown", (event) => {
    dragging = true;
    bar.setPointerCapture(event.pointerId);
    bar.classList.add("dragging");
    event.preventDefault();
  });
  bar.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const rect = bench.getBoundingClientRect();
    const available = availableHeight();
    if (available <= 0) return;
    apply((event.clientY - rect.top) / available);
  });
  const stopDragging = () => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove("dragging");
    persistMobileStackRatio();
  };
  bar.addEventListener("pointerup", stopDragging);
  bar.addEventListener("pointercancel", stopDragging);
  bar.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    const available = availableHeight();
    if (available <= 0) return;
    const currentHeight = bench.querySelector(".pdf-panel").getBoundingClientRect().height;
    apply((currentHeight + (event.key === "ArrowUp" ? -16 : 16)) / available);
    persistMobileStackRatio();
  });
  window.addEventListener("resize", sync);
  requestAnimationFrame(sync);
})();

/** 窄屏两栏堆叠时，展开的辅助面板可分别调整自己的宽度。 */
(function initMobileNestedSplits() {
  const bench = document.querySelector(".workbench");
  const pdfArea = document.querySelector(".pdf-content-area");
  const fieldPanel = document.querySelector(".field-panel");
  const stackBar = $("mobileStackSplit");
  const regionResult = $("regionResult");
  const regionHistory = $("regionHistory");
  const resultBar = $("mobileRegionResultSplit");
  const historyBar = $("mobileRegionHistorySplit");
  const resultCross = $("mobileResultCrossHandle");
  const historyCross = $("mobileHistoryCrossHandle");
  if (!bench || !pdfArea || !fieldPanel || !stackBar || !regionResult || !regionHistory
    || !resultBar || !historyBar || !resultCross || !historyCross) return;

  const isNarrow = () => window.matchMedia("(max-width: 1100px)").matches;
  const loadRatio = (key, fallback) => {
    try {
      const saved = parseFloat(localStorage.getItem(key));
      return Number.isFinite(saved) ? saved : fallback;
    } catch (_) {
      return fallback;
    }
  };
  let resultRatio = loadRatio(MOBILE_REGION_RESULT_WIDTH_KEY, 0.38);
  let historyRatio = loadRatio(MOBILE_REGION_HISTORY_WIDTH_KEY, 0.38);

  const setWidth = (container, variable, nextRatio, bar) => {
    const width = container.clientWidth;
    if (!width) return;
    const minimum = Math.min(140, Math.max(100, width * 0.25));
    const maximum = Math.max(minimum, width - bar.offsetWidth - minimum);
    const paneWidth = Math.min(maximum, Math.max(minimum, width * nextRatio));
    const ratio = paneWidth / width;
    container.style.setProperty(variable, `${ratio * 100}%`);
    bar.setAttribute("aria-valuemin", String(Math.round(minimum)));
    bar.setAttribute("aria-valuemax", String(Math.round(maximum)));
    bar.setAttribute("aria-valuenow", String(Math.round(paneWidth)));
    return ratio;
  };

  const resizePaneAtX = (side, clientX) => {
    const container = side === "right" ? pdfArea : fieldPanel;
    const bar = side === "right" ? resultBar : historyBar;
    const rect = container.getBoundingClientRect();
    const width = container.clientWidth;
    if (!width) return;
    const paneWidth = side === "right" ? rect.right - clientX : clientX - rect.left;
    const variable = side === "right"
      ? "--mobile-region-result-width" : "--mobile-region-history-width";
    const ratio = setWidth(container, variable, paneWidth / width, bar);
    if (ratio === undefined) return;
    if (side === "right") resultRatio = ratio;
    else historyRatio = ratio;
    syncMobileCrossHandles();
  };

  const placeCrossHandle = (handle, bar, open) => {
    const visible = isNarrow() && open && !stackBar.hidden;
    handle.hidden = !visible;
    if (!visible) return;
    const benchRect = bench.getBoundingClientRect();
    const barRect = bar.getBoundingClientRect();
    const stackRect = stackBar.getBoundingClientRect();
    handle.style.left = `${barRect.left + barRect.width / 2 - benchRect.left}px`;
    handle.style.top = `${stackRect.top + stackRect.height / 2 - benchRect.top}px`;
  };
  const syncCrossPositions = () => {
    placeCrossHandle(resultCross, resultBar, !regionResult.hidden);
    placeCrossHandle(historyCross, historyBar, !regionHistory.hidden);
  };
  syncMobileCrossHandles = syncCrossPositions;

  const sync = () => {
    const mobile = isNarrow();
    const resultOpen = mobile && !regionResult.hidden;
    const historyOpen = mobile && !regionHistory.hidden;
    pdfArea.classList.toggle("mobile-result-open", resultOpen);
    fieldPanel.classList.toggle("mobile-history-open", historyOpen);
    resultBar.hidden = !resultOpen;
    historyBar.hidden = !historyOpen;
    if (resultOpen) {
      resultRatio = setWidth(pdfArea, "--mobile-region-result-width", resultRatio, resultBar)
        || resultRatio;
    }
    if (historyOpen) {
      historyRatio = setWidth(fieldPanel, "--mobile-region-history-width", historyRatio, historyBar)
        || historyRatio;
    }
    if (mobile) {
      syncWorkbenchCross();
      syncRegionResultSplit();
    }
    syncCrossPositions();
  };

  const bindWidthDrag = (bar, container, pane, side, key, getRatio) => {
    let dragging = false;
    bar.addEventListener("pointerdown", (event) => {
      dragging = true;
      bar.setPointerCapture(event.pointerId);
      bar.classList.add("dragging");
      event.preventDefault();
    });
    bar.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      resizePaneAtX(side, event.clientX);
      syncCrossPositions();
    });
    const stopDragging = () => {
      if (!dragging) return;
      dragging = false;
      bar.classList.remove("dragging");
      try { localStorage.setItem(key, String(getRatio())); } catch (_) { }
    };
    bar.addEventListener("pointerup", stopDragging);
    bar.addEventListener("pointercancel", stopDragging);
    bar.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const width = container.clientWidth;
      const nextWidth = pane.getBoundingClientRect().width
        + (event.key === "ArrowRight" ? 16 : -16);
      const variable = side === "right"
        ? "--mobile-region-result-width" : "--mobile-region-history-width";
      const nextRatio = setWidth(container, variable, nextWidth / width, bar);
      if (nextRatio !== undefined) setRatio(nextRatio);
      try { localStorage.setItem(key, String(getRatio())); } catch (_) { }
      syncCrossPositions();
    });
  };

  bindWidthDrag(
    resultBar, pdfArea, regionResult, "right", MOBILE_REGION_RESULT_WIDTH_KEY,
    () => resultRatio,
  );
  bindWidthDrag(
    historyBar, fieldPanel, regionHistory, "left", MOBILE_REGION_HISTORY_WIDTH_KEY,
    () => historyRatio,
  );

  const bindCombinedDrag = (handle, side) => {
    const hoverClass = side === "right"
      ? "mobile-cross-hover-result" : "mobile-cross-hover-history";
    const focusClass = side === "right"
      ? "mobile-cross-focus-result" : "mobile-cross-focus-history";
    const draggingClass = side === "right"
      ? "mobile-cross-dragging-result" : "mobile-cross-dragging-history";
    let dragging = false;
    handle.addEventListener("pointerenter", () => bench.classList.add(hoverClass));
    handle.addEventListener("pointerleave", () => bench.classList.remove(hoverClass));
    handle.addEventListener("focus", () => bench.classList.add(focusClass));
    handle.addEventListener("blur", () => bench.classList.remove(focusClass));
    handle.addEventListener("pointerdown", (event) => {
      dragging = true;
      handle.setPointerCapture(event.pointerId);
      bench.classList.add(draggingClass);
      event.preventDefault();
    });
    handle.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      moveMobileStackToY(event.clientY);
      resizePaneAtX(side, event.clientX);
    });
    const stopDragging = () => {
      if (!dragging) return;
      dragging = false;
      bench.classList.remove(draggingClass);
      persistMobileStackRatio();
      const key = side === "right"
        ? MOBILE_REGION_RESULT_WIDTH_KEY : MOBILE_REGION_HISTORY_WIDTH_KEY;
      const ratio = side === "right" ? resultRatio : historyRatio;
      try { localStorage.setItem(key, String(ratio)); } catch (_) { }
    };
    handle.addEventListener("pointerup", stopDragging);
    handle.addEventListener("pointercancel", stopDragging);
    handle.addEventListener("keydown", (event) => {
      const widthDelta = event.key === "ArrowRight" ? 16 : event.key === "ArrowLeft" ? -16 : 0;
      const heightDelta = event.key === "ArrowDown" ? 16 : event.key === "ArrowUp" ? -16 : 0;
      if (!widthDelta && !heightDelta) return;
      event.preventDefault();
      if (widthDelta) {
        const pane = side === "right" ? regionResult : regionHistory;
        const rect = pane.getBoundingClientRect();
        const clientX = side === "right"
          ? rect.right - widthDelta
          : rect.left + widthDelta;
        resizePaneAtX(side, clientX);
      }
      if (heightDelta) {
        const currentHeight = bench.querySelector(".pdf-panel").getBoundingClientRect().height;
        const available = bench.clientHeight - stackBar.offsetHeight
          - (parseFloat(getComputedStyle(bench).rowGap) || 0) * 2;
        if (available > 0) setMobileStackRatio((currentHeight + heightDelta) / available);
      }
      persistMobileStackRatio();
      const key = side === "right"
        ? MOBILE_REGION_RESULT_WIDTH_KEY : MOBILE_REGION_HISTORY_WIDTH_KEY;
      const ratio = side === "right" ? resultRatio : historyRatio;
      try { localStorage.setItem(key, String(ratio)); } catch (_) { }
    });
  };
  bindCombinedDrag(resultCross, "right");
  bindCombinedDrag(historyCross, "left");

  new MutationObserver(sync).observe(regionResult, {
    attributes: true,
    attributeFilter: ["hidden"],
  });
  new MutationObserver(sync).observe(regionHistory, {
    attributes: true,
    attributeFilter: ["hidden"],
  });
  window.addEventListener("resize", sync);
  requestAnimationFrame(sync);
})();

/* ---- SSE 流式读取 ---- */

/** 读取 Server-Sent Events，把 delta 逐段交给回调。 */
async function readStream(path, body, handlers = {}) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!response.ok || !response.body) {
    let detail = `HTTP ${response.status}`;
    try { detail = (await response.json()).error || detail; } catch (_) { }
    throw new Error(detail);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE 以空行分帧
    let split;
    while ((split = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      let event = "message";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (!data) continue;
      let payload;
      try { payload = JSON.parse(data); } catch (_) { continue; }
      const handler = handlers[event];
      if (handler) handler(payload);
    }
  }
}

on("zoomIn", "click", () => setZoom(S.zoom + 0.25));
on("zoomOut", "click", () => setZoom(S.zoom - 0.25));

on("zoomInput", "change", (event) => {
  const input = event.currentTarget;
  const value = Number(input.value);
  if (!Number.isFinite(value) || value < 50 || value > 300) {
    input.value = String(Math.round(S.zoom * 100));
    input.reportValidity();
    return;
  }
  setZoom(value / 100);
});
on("zoomInput", "keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    event.currentTarget.blur();
  }
});

const pageImageWrap = $("pageImageWrap");
pageImageWrap.addEventListener("wheel", (event) => {
  if (!event.ctrlKey || S.pageView !== "image" || pageImageWrap.hidden) return;
  event.preventDefault();
  setZoom(S.zoom * Math.pow(1.0015, -event.deltaY));
}, { passive: false });

/* ---- 框选翻译：在页面图上拖一个矩形，只翻译该区域 ---- */

function syncRegionMode() {
  $("regionMode").classList.toggle("on", S.regionMode);
  $("translationToolsToggle").classList.toggle("on", S.regionMode);
  $("pageCanvas").classList.toggle("picking", S.regionMode);
}

function positionTranslationTools() {
  const trigger = $("translationToolsToggle");
  const popover = $("translationToolsPopover");
  const rect = trigger.getBoundingClientRect();
  const width = popover.offsetWidth;
  const left = Math.max(8, Math.min(window.innerWidth - width - 8, rect.left));
  const below = window.innerHeight - rect.bottom;
  popover.style.left = `${left}px`;
  if (below >= popover.offsetHeight + 10) {
    popover.style.top = `${rect.bottom + 5}px`;
    popover.style.bottom = "auto";
  } else {
    popover.style.top = "auto";
    popover.style.bottom = `${window.innerHeight - rect.top + 5}px`;
  }
}

function closeTranslationTools() {
  $("translationToolsPopover").hidden = true;
  $("translationToolsToggle").setAttribute("aria-expanded", "false");
  $("translateProviderPanel").hidden = true;
  $("translateProviderToggle").setAttribute("aria-expanded", "false");
}

function setTranslationToolsOpen(open) {
  const popover = $("translationToolsPopover");
  popover.hidden = !open;
  $("translationToolsToggle").setAttribute("aria-expanded", String(open));
  if (open) positionTranslationTools();
  else {
    $("translateProviderPanel").hidden = true;
    $("translateProviderToggle").setAttribute("aria-expanded", "false");
  }
}

on("translationToolsToggle", "click", () => {
  setTranslationToolsOpen($("translationToolsPopover").hidden);
});

on("translateProviderToggle", "click", () => {
  const panel = $("translateProviderPanel");
  panel.hidden = !panel.hidden;
  $("translateProviderToggle").setAttribute("aria-expanded", String(!panel.hidden));
  if (!panel.hidden) positionTranslationTools();
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".translation-tools-dropdown")) closeTranslationTools();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || $("translationToolsPopover").hidden) return;
  closeTranslationTools();
  $("translationToolsToggle").focus();
});

on("regionMode", "click", () => {
  S.regionMode = !S.regionMode;
  syncRegionMode();
  closeTranslationTools();
  toast(S.regionMode
    ? "框选翻译已开启：在页面上拖出一个区域"
    : "框选翻译已关闭");
});

on("regionClose", "click", () => { $("regionResult").hidden = true; });

function renderRegionHistory(translations) {
  const list = $("regionHistoryList");
  if (!translations.length) {
    list.innerHTML = '<div class="placeholder">暂无框选翻译记录</div>';
    return;
  }
  list.innerHTML = translations.map((item, index) => {
    const name = item.paper_title || item.filename || item.paper_key || "文献";
    const translatedText = String(item.translated_text || "").replace(/\s+/g, " ").trim();
    const sourceText = String(item.source || "").replace(/\s+/g, " ").trim();
    return `
      <button type="button" class="region-history-item" data-region-history-index="${index}"
        title="${esc(name)} · 第 ${item.page} 页">
        <span class="region-history-paper">
          <span title="${esc(name)}">${esc(name)}</span><span>第 ${item.page} 页</span>
        </span>
        <span class="region-history-preview" title="${esc(translatedText)}">${esc(translatedText)}</span>
        <span class="region-history-preview region-history-source"
          title="原文：${esc(sourceText)}">原文：${esc(sourceText)}</span>
      </button>`;
  }).join("");
}

async function loadRegionHistory() {
  const list = $("regionHistoryList");
  const requestId = ++regionHistoryRequest;
  const scope = $("regionHistoryScope").value;
  if (scope === "current" && !S.paper) {
    regionHistoryItems = [];
    list.innerHTML = '<div class="placeholder">请先打开一篇文献</div>';
    return;
  }
  list.innerHTML = '<div class="placeholder">正在加载翻译记录…</div>';
  try {
    const query = scope === "current" && S.paper
      ? `?paper_fk=${encodeURIComponent(S.paper.id)}` : "";
    const result = await api(`/api/translate-region/history${query}`);
    if (requestId !== regionHistoryRequest) return;
    regionHistoryItems = result.translations || [];
    renderRegionHistory(regionHistoryItems);
  } catch (error) {
    if (requestId === regionHistoryRequest) {
      list.innerHTML = `<div class="placeholder">读取翻译记录失败：${esc(error.message)}</div>`;
    }
  }
}

on("regionHistoryToggle", "click", () => {
  const panel = $("regionHistory");
  panel.hidden = !panel.hidden;
  $("regionHistoryToggle").setAttribute("aria-expanded", String(!panel.hidden));
  if (!panel.hidden) loadRegionHistory();
  closeTranslationTools();
});
on("regionHistoryClose", "click", () => {
  $("regionHistory").hidden = true;
  $("regionHistoryToggle").setAttribute("aria-expanded", "false");
});
on("regionHistoryScope", "change", loadRegionHistory);
on("regionHistoryList", "click", async (event) => {
  const button = event.target.closest("[data-region-history-index]");
  if (!button) return;
  const item = regionHistoryItems[Number(button.dataset.regionHistoryIndex)];
  if (!item) return;
  try {
    showView("review");
    if (!S.paper || S.paper.id !== item.paper_fk) await openPaper(item.paper_fk);
    await showPage(item.page);
    const rect = item.rect;
    const box = $("regionBox");
    box.hidden = false;
    box.style.left = `${rect.x * 100}%`;
    box.style.top = `${rect.y * 100}%`;
    box.style.width = `${rect.w * 100}%`;
    box.style.height = `${rect.h * 100}%`;
    $("regionResult").hidden = false;
    $("regionBody").innerHTML = `
      <div class="region-zh">${esc(item.translated_text)}</div>
      <details class="region-src" open>
        <summary>查看选中的英文原文</summary>
        <div>${esc(item.source)}</div>
      </details>`;
    box.scrollIntoView({ block: "center", behavior: "smooth" });
  } catch (error) {
    toast(`打开翻译记录失败：${error.message}`, "err");
  }
});

(function initRegionPicker() {
  const canvas = $("pageCanvas");
  const box = $("regionBox");
  if (!canvas || !box) return;

  let start = null;

  // 相对坐标：与高亮框一致，缩放后依然正确
  const relative = (event) => {
    const rect = canvas.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
      y: Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height)),
    };
  };

  const paint = (a, b) => {
    const x = Math.min(a.x, b.x);
    const y = Math.min(a.y, b.y);
    box.hidden = false;
    box.style.left = `${x * 100}%`;
    box.style.top = `${y * 100}%`;
    box.style.width = `${Math.abs(b.x - a.x) * 100}%`;
    box.style.height = `${Math.abs(b.y - a.y) * 100}%`;
  };

  canvas.addEventListener("pointerdown", (event) => {
    if (!S.regionMode || event.button !== 0) return;
    event.preventDefault();
    start = relative(event);
    paint(start, start);
    canvas.setPointerCapture(event.pointerId);
  });

  canvas.addEventListener("pointermove", (event) => {
    if (!start) return;
    paint(start, relative(event));
  });

  canvas.addEventListener("pointerup", async (event) => {
    if (!start) return;
    const end = relative(event);
    const rect = {
      x: Math.min(start.x, end.x),
      y: Math.min(start.y, end.y),
      w: Math.abs(end.x - start.x),
      h: Math.abs(end.y - start.y),
    };
    start = null;
    try { canvas.releasePointerCapture(event.pointerId); } catch (_) { }

    if (rect.w < 0.01 || rect.h < 0.005) {
      box.hidden = true;
      toast("选区太小，请拖出一个更大的区域", "warn");
      return;
    }
    await translateRegion(rect);
  });
})();

async function translateRegion(rect) {
  const panel = $("regionResult");
  const body = $("regionBody");
  const paperFk = S.paper.id;
  const page = S.page;
  panel.hidden = false;
  body.innerHTML = '<div class="placeholder">正在翻译选中区域…</div>';

  try {
    const result = await api(
      `/api/papers/${paperFk}/pages/${page}/translate-region`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rect, provider: S.translateProvider || undefined }),
      });
    if (!S.paper || S.paper.id !== paperFk || S.page !== page) return;
    body.innerHTML = `
      <div class="region-zh">${esc(result.text)}</div>
      <details class="region-src">
        <summary>查看选中的英文原文</summary>
        <div>${esc(result.source)}</div>
      </details>`;
    if (!$("regionHistory").hidden) loadRegionHistory();
  } catch (error) {
    if (S.paper && S.paper.id === paperFk && S.page === page) {
      body.innerHTML = `<div class="placeholder">${esc(error.message)}</div>`;
    } else {
      toast(`框选翻译失败：${error.message}`, "err");
    }
  }
}

on("pagePrev", "click", () => showPage(S.page - 1));
on("pageNext", "click", () => showPage(S.page + 1));

on("saveRecord", "click", async () => {
  const record = currentRecord();
  if (!record) return;
  const fields = {};
  S.dirty.forEach((value, key) => { fields[key] = value; });
  try {
    const updated = await api(`/api/records/${record.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fields, reviewed: $("reviewedFlag").checked }),
    });
    S.paper.records[S.recordIndex] = { ...updated, paper_key: S.paper.paper_key };
    S.dirty.clear();
    renderRecordTabs();
    renderFields();
    toast(updated.n_changes ? `已保存 ${updated.n_changes} 处修改` : "已保存");
    if (!$("historyBox").hidden) await loadHistory();
    await loadPapers();
    await loadStatus();
  } catch (error) { toast(error.message, "err"); }
});

on("exportJsonl", "click", () =>
  window.open(S.paper ? `/api/export?fmt=jsonl&paper_fk=${S.paper.id}` : "/api/export?fmt=jsonl"));
on("exportCsv", "click", () =>
  window.open(S.paper ? `/api/export?fmt=csv&paper_fk=${S.paper.id}` : "/api/export?fmt=csv"));

/* ------------------------------------------------------------------ */
/* edit history                                                        */
/* ------------------------------------------------------------------ */

function formatStamp(iso) {
  const parsed = Date.parse(iso || "");
  if (!parsed) return "";
  const d = new Date(parsed);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function valueHtml(value) {
  return value === null || value === undefined || value === ""
    ? '<code class="history-empty-val">（空）</code>'
    : `<code>${esc(value)}</code>`;
}

async function loadHistory() {
  const record = currentRecord();
  const list = $("historyList");
  if (!record) {
    list.innerHTML = '<div class="placeholder">没有选中记录。</div>';
    $("historyCount").textContent = "0";
    return;
  }
  try {
    const data = await api(`/api/records/${record.id}/history`);
    $("historyCount").textContent = data.total;
    list.innerHTML = data.edits.length
      ? data.edits.map((e) => `
        <div class="history-item" data-edit="${e.id}">
          <div class="history-item-top">
            <span class="history-field">${esc(e.label)}</span>
            <span class="history-time">${esc(formatStamp(e.created_at))}</span>
            <button class="history-revert" data-edit="${e.id}">撤销此项</button>
          </div>
          <div class="history-diff">
            <span class="history-old"><b>修改前</b>${valueHtml(e.old_value)}</span>
            <span class="history-new"><b>修改后</b>${valueHtml(e.new_value)}</span>
          </div>
        </div>`).join("")
      : '<div class="placeholder">这条记录还没有人工修改。保存字段修改后会在此留痕。</div>';
  } catch (error) {
    list.innerHTML = `<div class="placeholder">读取失败：${esc(error.message)}</div>`;
  }
}

on("historyToggle", "click", async () => {
  const box = $("historyBox");
  box.hidden = !box.hidden;
  if (!box.hidden) await loadHistory();
});

on("historyClose", "click", () => { $("historyBox").hidden = true; });

on("historyList", "click", async (event) => {
  const button = event.target.closest(".history-revert");
  if (!button) return;
  const record = currentRecord();
  if (!record) return;
  if (S.dirty.size && !await confirmDialog("当前有未保存的修改，撤销会重新加载字段。继续？", {
    title: "放弃未保存的修改",
    confirmLabel: "继续撤销",
  })) return;
  try {
    const updated = await api(`/api/records/${record.id}/revert`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ edit_id: Number(button.dataset.edit) }),
    });
    S.paper.records[S.recordIndex] = { ...updated, paper_key: S.paper.paper_key };
    S.dirty.clear();
    renderFields();
    await loadHistory();
    toast(`已撤销「${updated.label}」的这次修改`);
  } catch (error) { toast(error.message, "err"); }
});

/* ------------------------------------------------------------------ */
/* compare (样品对比)                                                  */
/* ------------------------------------------------------------------ */

/** 归一化后再比较：避免大小写、空白、全角差异造成假“差异项”。 */
function normValue(value) {
  if (value === null || value === undefined || value === "") return "";
  return String(value).trim().toLowerCase().replace(/\s+/g, " ");
}

const COMPARE_SPLIT_KEY = "mesoscope-compare-split-width";

(function initCompareSplit() {
  const layout = document.querySelector(".compare-layout");
  const bar = $("compareSplit");
  if (!layout || !bar) return;

  let preferredWidth = null;
  let currentWidth = 0;
  try {
    const saved = parseFloat(localStorage.getItem(COMPARE_SPLIT_KEY));
    if (Number.isFinite(saved)) preferredWidth = saved;
  } catch (_) { }

  const bounds = () => {
    const gap = parseFloat(getComputedStyle(layout).columnGap) || 0;
    const max = Math.min(520, layout.clientWidth - gap * 2 - 6 - 300);
    return { gap, min: 200, max: Math.max(200, max) };
  };

  const applyWidth = (width) => {
    if (!layout.clientWidth) return;
    const { min, max } = bounds();
    currentWidth = Math.min(max, Math.max(min, width));
    layout.style.gridTemplateColumns =
      `minmax(0, ${currentWidth}px) 6px minmax(0, 1fr)`;
    bar.setAttribute("aria-valuemin", String(min));
    bar.setAttribute("aria-valuemax", String(Math.round(max)));
    bar.setAttribute("aria-valuenow", String(Math.round(currentWidth)));
  };

  const applyCurrentWidth = () => {
    const defaultWidth = window.matchMedia("(max-width: 1180px)").matches ? 240 : 290;
    applyWidth(preferredWidth === null ? defaultWidth : preferredWidth);
  };
  syncCompareSplit = applyCurrentWidth;

  let dragging = false;
  bar.addEventListener("pointerdown", (event) => {
    dragging = true;
    bar.setPointerCapture(event.pointerId);
    bar.classList.add("dragging");
    event.preventDefault();
  });
  bar.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const rect = layout.getBoundingClientRect();
    const gap = parseFloat(getComputedStyle(layout).columnGap) || 0;
    applyWidth(event.clientX - rect.left - gap - bar.offsetWidth / 2);
  });
  const stopDragging = () => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove("dragging");
    preferredWidth = currentWidth;
    try { localStorage.setItem(COMPARE_SPLIT_KEY, String(currentWidth)); } catch (_) { }
  };
  bar.addEventListener("pointerup", stopDragging);
  bar.addEventListener("pointercancel", stopDragging);
  bar.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    applyWidth(currentWidth + (event.key === "ArrowLeft" ? -16 : 16));
    preferredWidth = currentWidth;
    try { localStorage.setItem(COMPARE_SPLIT_KEY, String(currentWidth)); } catch (_) { }
  });
  window.addEventListener("resize", applyCurrentWidth);
  requestAnimationFrame(applyCurrentWidth);
})();

async function loadCompare() {
  let data;
  try { data = await api("/api/search"); }
  catch (error) { toast(error.message, "err"); return; }

  S.compareRows = data.results;
  // 默认选中前若干条，演示时打开即有内容
  if (!S.compareSel.size) {
    S.compareRows.slice(0, Math.min(4, S.compareRows.length))
      .forEach((r) => S.compareSel.add(r.record_id));
  } else {
    const valid = new Set(S.compareRows.map((r) => r.record_id));
    [...S.compareSel].forEach((id) => { if (!valid.has(id)) S.compareSel.delete(id); });
  }
  renderPickList();
  renderCompare();
}

function renderPickList() {
  const box = $("pickList");
  if (!S.compareRows.length) {
    box.innerHTML = '<div class="placeholder">还没有抽取记录。</div>';
    return;
  }
  const rows = filteredCompareRows();
  if (!rows.length) {
    box.innerHTML = '<div class="placeholder">没有符合筛选条件的文献或样品。</div>';
    return;
  }
  // 按论文分组，跨论文选取时结构清楚
  const groups = new Map();
  rows.forEach((r) => {
    if (!groups.has(r.paper_key)) {
      groups.set(r.paper_key, { paperFk: r.paper_fk, title: r.paper_title, items: [] });
    }
    groups.get(r.paper_key).items.push(r);
  });

  box.innerHTML = [...groups.entries()].map(([key, group]) => `
    <div class="pick-group">
      <div class="pick-group-head" data-brief="${group.paperFk}">${esc(key)}</div>
      <div class="pick-group-title" data-brief="${group.paperFk}"
        title="${esc(group.title)}">${esc(group.title)}</div>
      ${group.items.map((r) => {
    const hint = r.fields.mesostructure || r.fields.surfactant || "—";
    return `
        <label class="pick-item ${S.compareSel.has(r.record_id) ? "on" : ""}"
               data-rid="${esc(r.record_id)}">
          <input type="checkbox" ${S.compareSel.has(r.record_id) ? "checked" : ""} />
          <span class="pick-name">r${String(r.record_index).padStart(2, "0")}</span>
          <span class="pick-hint" title="${esc(hint)}">${esc(hint)}</span>
        </label>`;
  }).join("")}
    </div>`).join("");
}

bindBriefHover($("pickList"));

function filteredCompareRows() {
  const folder = $("compareFolderFilter").value;
  const query = $("compareFieldSearch").value.trim().toLowerCase();
  return S.compareRows.filter((row) => {
    if (folder === "none" && row.folder_fk != null) return false;
    if (folder && folder !== "none" && String(row.folder_fk) !== folder) return false;
    if (!query) return true;
    const metadata = [row.paper_title, row.paper_key].join(" ").toLowerCase();
    const fields = Object.values(row.fields || {})
      .map((value) => String(value ?? ""))
      .join(" ")
      .toLowerCase();
    return metadata.includes(query) || fields.includes(query);
  });
}

on("compareFolderFilter", "change", renderPickList);
on("compareFieldSearch", "input", renderPickList);

on("pickList", "change", (event) => {
  const item = event.target.closest(".pick-item");
  if (!item) return;
  const rid = item.dataset.rid;
  if (event.target.checked) S.compareSel.add(rid); else S.compareSel.delete(rid);
  item.classList.toggle("on", event.target.checked);
  renderCompare();
});

on("pickAll", "click", () => {
  filteredCompareRows().forEach((r) => S.compareSel.add(r.record_id));
  renderPickList(); renderCompare();
});
on("pickNone", "click", () => {
  S.compareSel.clear();
  renderPickList(); renderCompare();
});
on("diffOnly", "change", renderCompare);

function selectedRecords() {
  return S.compareRows.filter((r) => S.compareSel.has(r.record_id));
}

/** 条件矩阵：行=字段，列=样品；逐行判断是否所有样品取值一致。 */
function renderCompare() {
  const picked = selectedRecords();
  const body = $("compareBody");
  const legend = $("compareLegend");

  if (picked.length < 2) {
    legend.hidden = true;
    body.innerHTML = '<div class="placeholder">在左侧勾选 2 条以上记录，这里会生成条件矩阵并高亮差异项。</div>';
    return;
  }
  legend.hidden = false;

  const rows = S.fields.map((f) => {
    const values = picked.map((r) => r.fields[f.key]);
    const distinct = new Set(values.map(normValue));
    // 全为空不算差异，也不算有效共同条件
    const allEmpty = [...distinct].every((v) => v === "");
    return { field: f, values, diff: distinct.size > 1, allEmpty, distinct: distinct.size };
  });

  const diffRows = rows.filter((r) => r.diff);
  const sameRows = rows.filter((r) => !r.diff && !r.allEmpty);
  const shown = $("diffOnly").checked ? diffRows : rows;

  const header = `
    <thead>
      <tr>
        <th class="field-col">字段</th>
        ${picked.map((r) => `
          <th>
            <span class="ch-key">${esc(r.paper_key)} · r${String(r.record_index).padStart(2, "0")}</span>
            <span class="ch-title" title="${esc(r.paper_title)}">${esc(r.paper_title)}</span>
          </th>`).join("")}
      </tr>
    </thead>`;

  const bodyRows = shown.map((row) => `
    <tr class="${row.diff ? "row-diff" : ""}">
      <th class="field-col">
        ${esc(row.field.label)}${row.diff ? `<span class="diff-tag">${row.distinct} 种</span>` : ""}
      </th>
      ${row.values.map((value) => `
        <td class="${row.diff ? "cell-diff" : ""}">
          ${value ? esc(value) : '<span class="null">未报道</span>'}
        </td>`).join("")}
    </tr>`).join("");

  const summary = `
    <div class="compare-summary">
      已选 ${picked.length} 条样品 ·
      <b>${diffRows.length} 个差异项</b>（${diffRows.map((r) => esc(r.field.label)).join("、") || "无"}） ·
      <em>${sameRows.length} 个共同条件</em>
    </div>`;

  body.innerHTML = summary +
    `<table class="compare-table">${header}<tbody>${bodyRows}</tbody></table>`;
}

on("exportCompare", "click", () => {
  const picked = selectedRecords();
  if (picked.length < 2) { toast("请先勾选至少 2 条记录", "warn"); return; }

  const header = ["字段", ...picked.map((r) => `${r.paper_key}_r${String(r.record_index).padStart(2, "0")}`), "是否差异项"];
  const lines = [header];
  S.fields.forEach((f) => {
    const values = picked.map((r) => r.fields[f.key] ?? "");
    const diff = new Set(values.map(normValue)).size > 1;
    lines.push([f.label, ...values, diff ? "差异" : "一致"]);
  });

  const csv = lines
    .map((row) => row.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(","))
    .join("\r\n");
  // BOM 让 Excel 正确识别 UTF-8
  const blob = new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "sample-comparison.csv";
  link.click();
  URL.revokeObjectURL(url);
  toast(`已导出 ${picked.length} 条样品的对比表`);
});

/* ------------------------------------------------------------------ */
/* cross-paper search                                                  */
/* ------------------------------------------------------------------ */

const SEARCH_COLUMNS = ["mesostructure", "surfactant", "silica_source", "surface_area", "pore_size", "synthesis_temperature", "morphology"];

async function runSearch() {
  const params = new URLSearchParams();
  const q = $("searchQ").value.trim();
  if (q) params.set("q", q);
  // 按文件搜索：匹配论文显示名、原始文件名或 paper_key
  const paperQ = $("searchPaperQ").value.trim();
  if (paperQ) params.set("paper_q", paperQ);
  const folder = $("searchFolder").value;
  if (folder === "none") params.set("unfiled", "true");
  else if (folder) params.set("folder_fk", folder);
  if ($("searchField").value) params.set("field", $("searchField").value);
  [["saMin", "sa_min"], ["saMax", "sa_max"], ["poreMin", "pore_min"], ["poreMax", "pore_max"]]
    .forEach(([id, key]) => { if ($(id).value.trim()) params.set(key, $(id).value.trim()); });
  if ($("reviewedOnly").checked) params.set("reviewed_only", "true");

  let data;
  try { data = await api(`/api/search?${params}`); }
  catch (error) { toast(error.message, "err"); return; }

  const labels = Object.fromEntries(S.fields.map((f) => [f.key, f.label]));
  const isNumeric = (key) => key === "surface_area" || key === "pore_size";
  $("searchTable").querySelector("thead").innerHTML =
    `<tr><th>论文</th><th>样品</th>${SEARCH_COLUMNS
      .map((k) => `<th class="${isNumeric(k) ? "num" : ""}">${esc(labels[k])}</th>`)
      .join("")}</tr>`;

  $("searchTable").querySelector("tbody").innerHTML = data.results.length
    ? data.results.map((r) => `
      <tr>
        <td class="link" data-open="${r.paper_fk}" data-brief="${r.paper_fk}">${esc(r.paper_key)}</td>
        <td>r${String(r.record_index).padStart(2, "0")}</td>
        ${SEARCH_COLUMNS.map((k) => {
      const value = r.fields[k];
      const cls = isNumeric(k) ? "num" : "";
      return value
        ? `<td class="${cls}" title="${esc(value)}">${esc(value)}</td>`
        : `<td class="${cls}"><span class="null">—</span></td>`;
    }).join("")}
      </tr>`).join("")
    : `<tr><td colspan="${SEARCH_COLUMNS.length + 2}"><div class="placeholder">没有符合条件的记录。</div></td></tr>`;

  const stats = data.results
    .map((r) => r.surface_area_num).filter((v) => typeof v === "number");
  $("searchSummary").textContent = stats.length
    ? `命中 ${data.total} 条记录 · 比表面积 ${Math.min(...stats)}–${Math.max(...stats)} m²/g（均值 ${(stats.reduce((a, b) => a + b, 0) / stats.length).toFixed(0)}）`
    : `命中 ${data.total} 条记录`;
}

on("searchBtn", "click", runSearch);
on("searchQ", "keydown", (e) => { if (e.key === "Enter") runSearch(); });
on("searchPaperQ", "keydown", (e) => { if (e.key === "Enter") runSearch(); });
on("searchFolder", "change", runSearch);
bindBriefHover($("searchTable"));
on("searchTable", "click", async (event) => {
  const cell = event.target.closest("[data-open]");
  if (!cell) return;
  showView("review");
  await openPaper(Number(cell.dataset.open));
});

/* ------------------------------------------------------------------ */
/* jobs                                                                */
/* ------------------------------------------------------------------ */

async function loadJobs() {
  const data = await api("/api/jobs");
  $("jobTable").querySelector("tbody").innerHTML = data.jobs.length
    ? data.jobs.map((j) => `
      <tr>
        <td>${j.id}</td>
        <td class="link" data-open="${j.paper_fk}" data-brief="${j.paper_fk}">${esc(j.paper_key)}</td>
        <td>${statusBadge(j)}</td>
        <td>${esc(j.engine || "—")}</td>
        <td>${esc(j.model || "—")}</td>
        <td class="num">${j.n_records}</td>
        <td title="${esc((j.issues || []).join(" / ") || j.message)}">${esc((j.issues || [])[0] || j.message || "—")}</td>
        <td>${esc(formatStamp(j.created_at) || "—")}</td>
        <td>${esc(formatStamp(j.finished_at) || "—")}</td>
      </tr>`).join("")
    : '<tr><td colspan="9"><div class="placeholder">还没有任务记录。</div></td></tr>';
}

bindBriefHover($("jobTable"));
on("jobTable", "click", async (event) => {
  const cell = event.target.closest("[data-open]");
  if (!cell) return;
  showView("review");
  await openPaper(Number(cell.dataset.open));
});

/* ------------------------------------------------------------------ */
/* ask panel                                                           */
/* ------------------------------------------------------------------ */

/* AI 助手已独立为首页视图，范围选择与对话区都在 #view-ask 内 */

function resetAskHistory() {
  const body = $("askBody");
  if (!body) return;
  body.querySelectorAll(".ask-msg:not([data-chat-welcome])").forEach((message) => message.remove());
}

function chatCitationsHtml(citations) {
  return (citations || []).map((citation) => {
    const own = paperFkOf(citation.paper_key);
    const shared = citation.shared || !own;
    return `
      <div class="cite ${shared ? "shared" : ""}" data-paper="${own}" data-page="${citation.page}"
           data-quote="${esc(citation.quote)}">
        <b>${esc(citation.paper_key)} · p.${citation.page}</b>
        ${citation.title ? `<span class="cite-title">${esc(citation.title)}</span>` : ""}
        ${shared ? '<span class="cite-tag">共享语料</span>' : ""}
        <br/>${esc(citation.quote)}
      </div>`;
  }).join("");
}

function renderChatTurn(turn) {
  const body = $("askBody");
  const citations = chatCitationsHtml(turn.citations);
  const corpus = turn.corpus_papers
    ? ` · 本次检索覆盖 ${turn.corpus_papers} 篇论文` : "";
  const engine = turn.engine === "llm" ? "AI + 检索" : "检索（抽取式）";
  body.insertAdjacentHTML("beforeend", `
    <div class="ask-msg user">${esc(turn.question)}</div>
    <div class="ask-msg bot">
      <p>${answerHtml(turn.answer || "")}</p>
      ${citations ? `<div class="cite-list">${citations}</div>` : ""}
      ${turn.notice ? `<p class="muted">${esc(turn.notice)}</p>` : ""}
      <p class="muted">回答引擎：${engine}${corpus} · 点击自己库中的引文可跳转原文</p>
    </div>`);
}

async function loadChatHistory() {
  resetAskHistory();
  try {
    const data = await api("/api/chat/history");
    (data.turns || []).forEach(renderChatTurn);
    $("askBody").scrollTop = $("askBody").scrollHeight;
  } catch (error) {
    toast(`读取聊天记录失败：${error.message}`, "err");
  }
}

on("askScope", "change", () => {
  // 下拉和多选互斥：改下拉即放弃之前的勾选
  if (S.askPicked.size) {
    S.askPicked.clear();
    renderAskPickList();
  }
  syncAskScopeLabel();
});

on("askBody", "click", async (event) => {
  const chip = event.target.closest(".chip");
  if (chip) { $("askInput").value = chip.textContent; $("askForm").requestSubmit(); return; }
  const cite = event.target.closest(".cite");
  if (!cite) return;
  const paperFk = Number(cite.dataset.paper);
  const page = Number(cite.dataset.page);
  if (!paperFk) {
    toast("这条引文来自其他用户共享的论文，不在你的文献库中，无法跳转原文", "warn");
    return;
  }
  showView("review");
  // openPaper() navigates to the record's first evidence page, so the jump to
  // the cited page must happen after it resolves.
  await openPaper(paperFk);
  await showPage(page, cite.dataset.quote);
});

on("askForm", "submit", async (event) => {
  event.preventDefault();
  const question = $("askInput").value.trim();
  if (!question) return;
  if (!requireLogin("提问")) return;
  $("askInput").value = "";

  const body = $("askBody");
  body.insertAdjacentHTML("beforeend", `<div class="ask-msg user">${esc(question)}</div>`);
  const pending = document.createElement("div");
  pending.className = "ask-msg bot";
  pending.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
  body.append(pending);
  body.scrollTop = body.scrollHeight;

  try {
    const payload = askScopePayload();
    payload.question = question;

    let answer = "";
    let meta = { citations: [], corpus_papers: 0 };
    let engine = "retrieval";
    let notice = "";
    let paintFrame = 0;
    let pendingStreaming = true;

    const renderPending = () => {
      paintFrame = 0;
      const corpus = meta.corpus_papers
        ? ` · 本次检索覆盖 ${meta.corpus_papers} 篇论文` : "";
      const cites = chatCitationsHtml(meta.citations);
      pending.innerHTML = `
        <p>${answer ? answerHtml(answer) : '<span class="typing"><i></i><i></i><i></i></span>'}</p>
        ${cites ? `<div class="cite-list">${cites}</div>` : ""}
        ${notice ? `<p class="muted">${esc(notice)}</p>` : ""}
        ${pendingStreaming ? "" : `<p class="muted">回答引擎：${esc(engine === "llm" ? "AI + 检索" : "检索（抽取式）")}${corpus} · 点击自己库中的引文可跳转原文</p>`}`;
      body.scrollTop = body.scrollHeight;
    };

    const paint = (streaming, immediate = false) => {
      pendingStreaming = streaming;
      if (immediate) {
        if (paintFrame) cancelAnimationFrame(paintFrame);
        renderPending();
      } else if (!paintFrame) {
        paintFrame = requestAnimationFrame(renderPending);
      }
    };

    await readStream("/api/ask/stream", payload, {
      meta: (data) => { meta = data; paint(true); },
      delta: (data) => { answer += data.text || ""; paint(true); },
      notice: (data) => { notice = data.text || ""; },
      done: (data) => { engine = data.engine || engine; paint(false, true); },
    });
    paint(false, true);
  } catch (error) {
    pending.innerHTML = `<p>出错了：${esc(error.message)}</p>`;
  }
  body.scrollTop = body.scrollHeight;
});

function paperFkOf(paperKey) {
  const paper = S.papers.find((p) => p.paper_key === paperKey);
  return paper ? paper.id : "";
}

/** 回答正文：先转义再把 **小节标题** 还原成粗体，保证内容总结一眼可见。 */
function answerHtml(text) {
  return esc(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\n/g, "<br/>");
}

/* ------------------------------------------------------------------ */

/** 登录后加载该账号的数据。 */
async function bootData() {
  await loadStatus();
  await loadTranslateProviders();
  await loadPapers();
  await loadChatHistory();
  renderAccountCard();
  // AI 助手是首页；提取结果视图仍预载一篇，切过去即有内容
  if (S.papers.length) {
    const withRecords = S.papers.find((p) => p.n_records > 0);
    if (withRecords) await openPaper(withRecords.id);
  }
  showView("ask");
}

(async function boot() {
  try {
    // Cookie 仍有效就直接进入，否则弹登录框（可关掉，之后点右上角头像再登录）
    const me = await api("/api/auth/me");
    renderAccountCard();
    if (me.user) {
      S.user = me.user;
      hideAuthGate();
      await bootData();
    } else {
      await loadStatus();   // 引擎状态与共享语料规模不需要登录
      renderAccountCard();
      showView("ask");
      showAuthGate("login");
    }
  } catch (error) {
    renderAccountCard();
    showAuthGate();
    authError(`初始化失败：${error.message}`);
  }
})();
