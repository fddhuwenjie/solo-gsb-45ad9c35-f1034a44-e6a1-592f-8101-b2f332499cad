/* 低空航线空域时窗审查台 — 原生 JS + SVG */
"use strict";

const SVGNS = "http://www.w3.org/2000/svg";
const svg = document.getElementById("board");

const state = { route: { waypoints: [], speeds: [], departureTime: "", corridorHalfWidth: 0.3, unitScale: 100 }, nfzs: [] };
let analysis = null;
let mode = "select";
let selection = { type: null, id: null };
let view = { x: 0, y: 0, w: 1000, h: 620 };
let drawing = [];          // 绘制禁飞区中的顶点
let hover = null;          // 鼠标世界坐标（绘制预览）
let drag = null;           // 当前拖动
let analyzeTimer = null, saveTimer = null;
let panelDirty = true;
let workspaceRev = 0;      // 当前工作区修订号（GET 响应头 / PUT 响应体维护）
let currentRelease = null; // 当前发布（GET /api/release）
let voyages = [];          // 航次列表（GET /api/voyages，新登记在前）
// 航次起飞时刻输入框草稿：随当前发布号切换预填为该发布快照的起飞时刻；
// 用户录入后保留其值，发布号变化才重新预填
let voyageDepSeq = null;
let voyageDepDraft = "";
// 航次改期：选中的未结束航次与所见原时刻（乐观并发基线）、新时刻输入草稿、
// 最近一次被拒的原因与明细。被拒时输入保留，可修改后重新提交
let rescheduleTarget = null; // {id, originalDepartureTime}
let rescheduleDraft = "";
let rescheduleErr = null;
// 发布状态机：loading=尚未读到发布状态（页面可编辑但禁止一切工作区写请求）；
// published=确认已有发布（禁飞区只存草稿、航线条件保存）；
// none=确认从无发布（沿用原无条件自动保存）；error=读取失败（保留编辑、
// 提示刷新，绝不能当作 none 处理）
let releaseState = "loading";
// 发布状态就绪承诺：所有工作区写请求（自动保存/核准/发布/载入版本）都先等它。
// resolve 值为 releaseState；refreshRelease 可在用户点“重试/刷新”后重置
let releaseReady = null;
let resolveReleaseReady = null;
let savedNfzs = [];        // 工作区已落库的禁飞区（已有发布时，页面的禁飞区编辑只存草稿）
let serverRouteSeen = null; // 本页最近一次确认的“服务器航线”（编辑前所见基线）
let routeConflict = null;   // 双方都改航线时的未解决冲突；非空期间暂停自动保存
// 发布状态读取失败期间被挡下的防抖保存：编辑仍在页面。重试读到明确状态后
// 据此补一次防抖保存（loading 期间的保存会在就绪承诺放行后自行发出，无需补）
let writesWithheld = false;
// 启动协调（GET /api/state → GET /api/release 间修订号推进）取回最新工作区
// 失败：true 期间本页航线/禁飞区草稿保留，一切工作区写请求阻断直到重试成功
let workspaceCoordError = false;
// 启动协调发现分歧时 GET /api/state 读到的旧修订号，供“重试”再次协调使用
let coordSeenRev = null;

// 409 分流后确认“双方都改了航线”：保存中止并抛出本哨兵，
// 调用方据此知道冲突面板已展示、不得把它当作保存成功继续核准/发布
class RouteConflictError extends Error {
  constructor() { super("route conflict"); this.name = "RouteConflictError"; }
}

// 发布状态未明确（仍在读取或读取失败）：不得发起工作区写请求，
// 调用方据此静默跳过自动保存，或提示用户刷新后再试核准/发布
class ReleaseUnavailableError extends Error {
  constructor(msg) { super(msg || "release status unavailable"); this.name = "ReleaseUnavailableError"; }
}

// 启动协调失败：GET /api/state 与 GET /api/release 之间修订号已被他页推进，
// 但取回最新工作区的再次 GET /api/state 失败。此时绝不能放行待发保存
// （否则会用 /api/release 的新修订号把旧航线/旧禁飞区整份条件写回）：
// 本页航线与禁飞区草稿保留，自动保存及相邻核准/发布写入一律阻断，
// 侧栏提供“重试”，取回最新工作区分流成功后才恢复
class WorkspaceCoordError extends Error {
  constructor() {
    super("workspace coordination fetch failed");
    this.name = "WorkspaceCoordError";
  }
}

// 重置/生成“发布状态就绪”承诺：初始与用户点刷新时各调用一次
function resetReleaseReady() {
  releaseReady = new Promise((resolve) => { resolveReleaseReady = resolve; });
}
// 等待发布状态明确；明确前调用方不得发起任何工作区写请求。
// 读取失败时 reject（不得当作“从无发布”）；重试成功后该承诺会随新承诺放行。
// 启动协调失败（workspaceCoordError）同样 reject：保留草稿、阻断所有写入，
// 直到用户点“重试”取回最新工作区分流成功。
// 其余返回 published / none
async function waitReleaseReady() {
  const s = await releaseReady;
  if (workspaceCoordError) throw new WorkspaceCoordError();
  if (s !== "published" && s !== "none") {
    throw new ReleaseUnavailableError(s === "error"
      ? "发布状态读取失败，请刷新页面后重试" : "发布状态尚未确认");
  }
  return s;
}
// 任何被挡下的防抖保存都记账：状态由 error 恢复明确后统一补发一次
function noteWithheldWrite() { writesWithheld = true; }
resetReleaseReady();

// ----------------------------------------------------------- 工具
const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, parent) => {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") n.textContent = v; else n.setAttribute(k, v);
  }
  if (parent) parent.appendChild(n);
  return n;
};
const uid = (p) => p + Math.random().toString(36).slice(2, 8);
const clone = (o) => JSON.parse(JSON.stringify(o));
const sameJson = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const num = (v, d = 0) => { const n = parseFloat(v); return Number.isFinite(n) ? n : d; };
const r2 = (v) => Math.round(v * 100) / 100;
const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2200);
}

function toWorld(clientX, clientY) {
  const rect = svg.getBoundingClientRect();
  return {
    x: view.x + ((clientX - rect.left) / rect.width) * view.w,
    y: view.y + ((clientY - rect.top) / rect.height) * view.h,
  };
}
function screenScale() {
  const rect = svg.getBoundingClientRect();
  return view.w / rect.width; // 世界单位 / 像素
}

// ----------------------------------------------------------- API
// 变更类请求（自动保存、核准）经 opChain 串行：相邻操作按入队顺序完成，
// 后一个始终看到前一个返回的最新修订号
let opChain = Promise.resolve();
function enqueue(task) {
  const run = opChain.then(task);
  // 单个任务失败不中断后续任务；失败信息由任务内部自行提示
  opChain = run.then(() => {}, () => {});
  return run;
}
async function api(path, opts) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  return res.json();
}
async function apiRaw(path, opts) {
  return fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
}
function applyLoadedState(s) {
  Object.assign(state, s);
  if (!state.route) state.route = { waypoints: [], speeds: [], departureTime: "", corridorHalfWidth: 0.3, unitScale: 100 };
  state.route.waypoints ||= [];
  state.route.speeds ||= [];
  state.route.unitScale ||= 100;
  state.route.corridorHalfWidth ||= 0.3;
  state.nfzs ||= [];
}
async function loadState() {
  const res = await fetch("/api/state");
  workspaceRev = parseInt(res.headers.get("X-Workspace-Rev") || "0", 10) || 0;
  applyLoadedState(await res.json());
  savedNfzs = clone(state.nfzs);
  serverRouteSeen = clone(state.route);
}
function flushPendingSave() {
  // 核准/发布前协调：有待发的航线自动保存（防抖定时器）时，取消定时器并
  // 立即落库，使随后的核准携带最新修订号；没有待发保存时绝不额外发起 PUT
  // （核准不预存工作区），只排入队尾等在途请求完成——若在途保存落为未解决
  // 的航线冲突（routeConflict 被置位），则把冲突向上传播，中止相邻核准/发布
  //
  // 前提：发布状态必须已明确——发布状态未读取/读取失败时不发起任何工作区
  // 写请求（调用方会先 await waitReleaseReady 并提示刷新）。
  if (saveTimer !== null) {
    clearTimeout(saveTimer);
    saveTimer = null;
    if (routeConflict) return Promise.reject(new RouteConflictError());
    return enqueue(() => doSaveState());
  }
  return enqueue(() => {
    if (routeConflict) throw new RouteConflictError();
  });
}

// 合入他页已核准的禁飞区基线：本页没有禁飞区草稿（state.nfzs 与旧基线一致）
// 时直接采用服务器内容；有草稿时只更新基线，画布草稿原样保留，绝不落库草稿
function adoptServerNfzs(remoteNfzs) {
  const hadDraft = !sameJson(state.nfzs, savedNfzs);
  savedNfzs = clone(remoteNfzs);
  if (!hadDraft) state.nfzs = clone(savedNfzs);
}
// 展示双方航线冲突：记录服务器航线/修订号供二选一，暂停自动保存，
// 本页未保存航线与禁飞区草稿一律保留
function presentRouteConflict(remoteState, remoteRev) {
  routeConflict = { route: clone(remoteState.route || {}), rev: remoteRev };
  workspaceRev = remoteRev;
  // 合入他页已核准禁飞区基线：本页有草稿则只更新基线（草稿留在画布），
  // 无草稿则画布一并采用服务器内容，绝不把草稿写库
  adoptServerNfzs(remoteState.nfzs || []);
  serverRouteSeen = clone(remoteState.route || {});
  clearTimeout(saveTimer);
  saveTimer = null;
  renderRouteConflict();
}

// 发布状态确认后，若 GET /api/state 与 GET /api/release 两次读取之间工作区
// 修订号已变，不得把新修订号配旧航线写回。取回最新工作区，按分流口径处理：
//   · 合入他页已核准禁飞区（本页有草稿则只换基线、草稿不落库）；
//   · 服务器航线未变（仅禁飞区获核准，或本页改航线而远端未改）：采用最新
//     修订号，本页航线随后按最新修订号条件保存即可；
//   · 仅他页改航线（本页航线仍与编辑前所见一致）：直接采用远端航线，无需写库；
//   · 双方都改航线：走现有冲突选择流程（暂停自动保存、并列双方航线供选择，
//     草稿保留）。
// 返回 "ok" 表示已无分歧可放行写请求；"conflict" 表示已走冲突分流，必须中止
// 待发保存；"failed" 表示再次 GET /api/state 失败——保留本页航线与禁飞区草稿，
// 工作区写请求继续阻断（workspaceCoordError 置位），由用户点“重试”取回最新
// 工作区后再分流，绝不用“新修订号 + 旧航线/旧禁飞区”整份写回。
async function reconcileWorkspaceRevIfPublished(seenRev) {
  if (releaseState !== "published") return "ok";
  if (workspaceRev === seenRev) return "ok";
  coordSeenRev = seenRev;
  let fresh, freshState;
  try {
    fresh = await fetch("/api/state");
    if (!fresh.ok) throw new Error("HTTP " + fresh.status);
    freshState = await fresh.json();
  } catch (e) {
    // 协调所需的再次 GET 失败：不放行任何工作区写请求。workspaceRev 虽已采用
    // /api/release 的权威值，但此刻拿它条件保存就会把编辑前所见的旧航线与
    // 旧禁飞区整份写回（他页可能已核准禁飞区、改航线并推进修订号，条件 PUT
    // 会成功而非 409）。保留本页草稿并阻断，待重试取回最新工作区后再分流。
    workspaceCoordError = true;
    renderRelease();
    renderNfzDraft();
    return "failed";
  }
  workspaceCoordError = false;
  const freshRev = parseInt(fresh.headers.get("X-Workspace-Rev") || "0", 10) || workspaceRev;
  const remoteRoute = freshState.route || {};
  const remoteRouteChanged = !sameJson(remoteRoute, serverRouteSeen);
  const localRouteChanged = !sameJson(state.route, serverRouteSeen);
  // 任何情况下先合入他页已核准禁飞区（草稿保留在画布，绝不落库）
  adoptServerNfzs(freshState.nfzs || []);
  if (!remoteRouteChanged) {
    // 仅他页核准禁飞区（航线相同）：采用最新修订号；本页改了航线时随后按
    // 最新修订号条件保存，本页未改则无需写库
    workspaceRev = freshRev;
    if (!localRouteChanged) state.route = clone(remoteRoute);
    serverRouteSeen = clone(remoteRoute);
    renderPanel(); renderBoard(); runAnalyze(); renderNfzDraft();
    return "ok";
  }
  if (!localRouteChanged) {
    // 仅他页改航线：直接采用远端航线（无需写库），禁飞区草稿已在上文保留
    workspaceRev = freshRev;
    state.route = clone(remoteRoute);
    serverRouteSeen = clone(remoteRoute);
    renderPanel(); renderBoard(); runAnalyze(); renderNfzDraft();
    toast("检测到他页已保存新航线，已加载服务器最新航线（本页禁飞区草稿保留）");
    return "ok";
  }
  // 双方航线都变：沿现有冲突选择流程，保留本页航线与禁飞区草稿
  presentRouteConflict(freshState, freshRev);
  toast("航线在他页也被修改：已暂停自动保存，请选择处理方式");
  renderPanel(); renderBoard(); runAnalyze(); renderNfzDraft();
  return "conflict";
}

// 启动协调失败后用户点“重试”：重新取回最新工作区并按规则分流。
// 成功后恢复写请求门（ready 承诺放行）并重放被挡下的待发保存；
// 仍然失败则继续阻断、草稿保留；双方航线都变则进入冲突选择流程。
async function retryWorkspaceCoordination() {
  if (!workspaceCoordError) return;
  const outcome = await reconcileWorkspaceRevIfPublished(
    coordSeenRev == null ? workspaceRev : coordSeenRev);
  if (outcome === "failed") {
    toast("仍无法取回最新工作区，本页编辑已保留，写操作继续暂停，请稍后重试");
    return;
  }
  workspaceCoordError = false;
  const conflicted = outcome === "conflict";
  renderRelease();
  renderNfzDraft();
  // 冲突分流时也放行就绪门：routeConflict 本身会暂停自动保存；用户二选一后
  // 自动保存需要立即恢复，不能一直挂在就绪承诺上
  resolveReleaseReady(releaseState);
  replayWithheldWrite(conflicted);
  if (conflicted) return;
  toast("已取回最新工作区并完成协调，自动保存已恢复");
}

async function doSaveState() {
  // 发布状态未明确（读取中/读取失败）时绝不写工作区：
  // 读取中由 scheduleSave 在定时器回调里等待；走到这里仍未明确则直接中止，
  // 绝不能因为 currentRelease===null 就误走未发布的无条件 PUT 分支
  if (releaseState !== "published" && releaseState !== "none") {
    throw new ReleaseUnavailableError();
  }
  // 启动协调失败期间绝不写工作区：重试取回最新工作区分流成功前一律阻断，
  // 避免用 /api/release 的新修订号把编辑前所见的旧航线/旧禁飞区整份写回
  if (workspaceCoordError) throw new WorkspaceCoordError();
  if (routeConflict) throw new RouteConflictError();
  // 已有发布时：航线编辑仍自动保存，但禁飞区变更只存草稿，
  // 落库的禁飞区必须是已核准的 savedNfzs，不得顺带写入草稿内容；
  // 并携带所见修订号：他页已变更（如别处刚核准）时不得静默覆盖
  if (releaseState !== "published") {
    // 确认从无发布：沿用原无条件自动保存（不携带条件头）
    const payload = state;
    const res = await apiRaw("/api/state", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const r = await res.json().catch(() => ({}));
    if (res.ok && r && typeof r.rev === "number") {
      workspaceRev = r.rev;
      savedNfzs = clone(state.nfzs); // 未发布：整体落库，镜像同步
      serverRouteSeen = clone(state.route);
      routeConflict = null;
      renderRouteConflict();
      return;
    }
    throw new Error(r && r.error ? r.error : "工作区保存失败");
  }

  const localRoute = clone(state.route);
  const localDraft = clone(state.nfzs);
  const baseRoute = clone(serverRouteSeen);
  // 协调分流后本页航线与已确认的服务器航线一致（如仅他页改航线后本页未再编辑、
  // 或编辑又被撤销）：没有待保存的本地航线，绝不发起 PUT 把旧内容写回
  if (sameJson(localRoute, baseRoute)) {
    if (sameJson(state.route, localRoute)) state.route = localRoute;
    serverRouteSeen = clone(localRoute);
    renderNfzDraft();
    return;
  }
  let expectedRev = workspaceRev;
  let lastErr = null;
  // 条件写入；409 时取回服务器最新内容，按三种情况分流：
  //  1) 本页航线相对编辑前所见未改：无本地航线可保存，直接采用服务器最新
  //     航线与已核准禁飞区（本页禁飞区草稿保留），不再写库；
  //  2) 本页航线已改、服务器航线仍与编辑前所见一致（差异仅为他页核准的
  //     禁飞区）：合入已核准禁飞区后用最新修订号重条件写入，草稿不落库；
  //  3) 双方都改了航线：保留本页编辑，展示双方航线供选择，停止自动重试。
  // 情况 2 连续多轮 409 最多自动合入重试 2 次，仍冲突则保留编辑并提示。
  for (let staleRound = 0; staleRound <= 2; staleRound++) {
    const res = await apiRaw("/api/state", {
      method: "PUT",
      headers: { "Content-Type": "application/json",
                 "X-Expected-Workspace-Rev": String(expectedRev) },
      body: JSON.stringify({ route: localRoute, nfzs: savedNfzs }),
    });
    const r = await res.json().catch(() => ({}));
    if (res.ok && r && typeof r.rev === "number") {
      workspaceRev = r.rev;
      serverRouteSeen = clone(localRoute);
      routeConflict = null;
      // 仅在本页这期间未继续编辑时恢复引用，避免抹掉在途编辑
      if (state.route === localRoute || sameJson(state.route, localRoute)) {
        state.route = localRoute;
      }
      if (sameJson(state.nfzs, localDraft)) state.nfzs = localDraft;
      if (staleRound > 0) {
        toast("检测到他页已更新，已合入他页已核准的禁飞区，本页航线已保存");
        renderPanel(); renderBoard(); runAnalyze();
      }
      renderNfzDraft(); renderRouteConflict();
      return;
    }
    if (res.status === 409 && r.reason === "stale") {
      lastErr = r;
      const fresh = await fetch("/api/state");
      const freshRev = parseInt(fresh.headers.get("X-Workspace-Rev") || "0", 10) || expectedRev;
      const freshState = await fresh.json();
      const remoteRoute = freshState.route || {};
      adoptServerNfzs(freshState.nfzs || []); // 任何情况下都先合入已核准禁飞区基线
      if (sameJson(localRoute, baseRoute)) {
        // 情况 1：本页航线未改，没有需要保存的本地航线：采用服务器最新航线，
        // 本页禁飞区草稿已由 adoptServerNfzs 保留
        workspaceRev = freshRev;
        serverRouteSeen = clone(remoteRoute);
        if (state.route === localRoute || sameJson(state.route, localRoute)) {
          state.route = clone(remoteRoute);
        }
        routeConflict = null;
        toast("检测到他页已保存新航线，已加载服务器最新航线（本页禁飞区草稿保留）");
        renderRouteConflict(); renderNfzDraft();
        renderPanel(); renderBoard(); runAnalyze();
        return;
      }
      if (sameJson(remoteRoute, baseRoute)) {
        // 情况 2：服务器航线仍与本页编辑前所见一致，只有禁飞区在他页获核准
        expectedRev = freshRev;
        continue;
      }
      // 情况 3：双方都改了航线：暂停自动保存，本页航线与禁飞区草稿原样保留，
      // 展示双方航线，由用户选择加载服务器航线或确认提交本页航线
      presentRouteConflict(freshState, freshRev);
      toast("航线在他页也被修改：已暂停自动保存，请选择处理方式");
      renderPanel(); renderBoard(); runAnalyze(); renderNfzDraft();
      throw new RouteConflictError();
    }
    throw new Error(r && r.error ? r.error : "工作区保存失败");
  }
  // 多轮合入仍被 409：保留本页全部编辑，不覆盖任何一方，提示稍后处理
  toast("工作区在他处持续变更，本页编辑已保留，未保存：" + (lastErr?.error || "修订号冲突"));
  throw new Error(lastErr?.error || "stale workspace");
}
async function saveStateNow() {
  clearTimeout(saveTimer);
  saveTimer = null;
  // 启动协调失败时就绪承诺会一直挂起：显式抛出，由调用方（载入版本）提示
  // 先点侧栏“重试”，而不是让保存承诺静默悬挂
  if (workspaceCoordError) throw new WorkspaceCoordError();
  // 发布状态明确前不发工作区写请求；读取失败向上抛出，由调用方提示刷新
  await waitReleaseReady();
  // FIFO 队列保证不会与在途保存并发：后一个始终拿到前一个返回的修订号
  return enqueue(() => doSaveState());
}

function scheduleSave() {
  // 双方航线冲突未解决期间暂停自动保存，避免任何一方航线被自动写库覆盖
  if (routeConflict) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveTimer = null;
    // 发布状态可能尚未返回：保留页面编辑（state 不动），等状态明确后再决定
    // 走未发布无条件保存还是已发布条件保存；读取失败则放弃本次写请求，
    // 编辑仍在页面上，绝不把“读不到发布状态”当作“从无发布”
    waitReleaseReady().then(
      () => enqueue(() => doSaveState().catch((e) => {
        if (e instanceof RouteConflictError) return; // 冲突面板已提示，不再弹保存失败
        if (e instanceof ReleaseUnavailableError) return; // 已通过就绪门，理论上不会到这
        if (e instanceof WorkspaceCoordError) {
          noteWithheldWrite(); // 协调失败：草稿保留，重试取回后由 replay 统一补发
          return;
        }
        toast(e.message || "自动保存失败");
      })),
      () => {
        // 发布状态读取失败或启动协调失败：保留编辑，不写工作区；侧栏提示
        // 重试，重试取回明确状态/最新工作区后由 replayWithheldWrite 统一补发
        noteWithheldWrite();
      }
    );
  }, 500);
}
function scheduleAnalyze() {
  clearTimeout(analyzeTimer);
  analyzeTimer = setTimeout(runAnalyze, 350);
}
async function runAnalyze() {
  $("#analyzeStatus").textContent = "审查中…";
  try {
    analysis = await api("/api/analyze", { method: "POST", body: JSON.stringify(state) });
  } catch (e) {
    analysis = { ok: false, errors: [{ message: "分析请求失败：" + e }], entries: [] };
  }
  $("#analyzeStatus").textContent = "";
  renderBoard();
  renderResults();
  renderDynamicPanel();
}

// ----------------------------------------------------------- SVG 渲染
function starPath(cx, cy, rOuter, rInner) {
  const pts = [];
  for (let i = 0; i < 10; i++) {
    const r = i % 2 === 0 ? rOuter : rInner;
    const a = -Math.PI / 2 + (i * Math.PI) / 5;
    pts.push(`${r2(cx + r * Math.cos(a))},${r2(cy + r * Math.sin(a))}`);
  }
  return "M" + pts.join("L") + "Z";
}

function renderBoard() {
  svg.innerHTML = "";
  svg.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`);

  // 网格
  const step = 100;
  const gx0 = Math.floor(view.x / step) * step;
  const gy0 = Math.floor(view.y / step) * step;
  for (let x = gx0; x <= view.x + view.w; x += step) {
    el("line", { x1: x, y1: view.y, x2: x, y2: view.y + view.h, class: "grid-line" }, svg);
  }
  for (let y = gy0; y <= view.y + view.h; y += step) {
    el("line", { x1: view.x, y1: y, x2: view.x + view.w, y2: y, class: "grid-line" }, svg);
  }
  // 背景事件层
  el("rect", {
    x: view.x - 5000, y: view.y - 5000, width: view.w + 10000, height: view.h + 10000,
    fill: "transparent", "data-action": "bg",
  }, svg);

  const wps = state.route.waypoints;
  const halfW = num(state.route.corridorHalfWidth, 0.3) * num(state.route.unitScale, 100);
  const conflictNfz = new Set((analysis?.entries || []).map((e) => e.nfzId));

  // 禁飞区
  for (const nz of state.nfzs) {
    const v = nz.vertices || [];
    if (v.length >= 2) {
      const isSel = selection.type === "nfz" && selection.id === nz.id;
      let cls = "nfz-poly " + (isSel ? "selected" : "ok");
      el("polygon", {
        points: v.map((p) => `${r2(p[0])},${r2(p[1])}`).join(" "),
        class: cls,
        fill: nz.enabled === false ? "rgba(150,160,180,.05)"
          : conflictNfz.has(nz.id) ? "rgba(255,77,109,.10)" : "rgba(255,255,255,.05)",
        stroke: nz.enabled === false ? "#5b6880" : conflictNfz.has(nz.id) ? "#ff4d6d"
          : isSel ? "#ffd166" : "#8fa3c2",
        "data-action": "nfz-pick", "data-id": nz.id,
      }, svg);
      const cx = v.reduce((s, p) => s + p[0], 0) / v.length;
      const cy = v.reduce((s, p) => s + p[1], 0) / v.length;
      el("text", { x: r2(cx), y: r2(cy - 4), "text-anchor": "middle", class: "nfz-label", text: nz.name }, svg);
      el("text", { x: r2(cx), y: r2(cy + 10), "text-anchor": "middle", class: "nfz-label",
        text: `${num(nz.minAlt)}–${num(nz.maxAlt)}m` }, svg);
      if (isSel) {
        const pr = Math.max(6, 7 * screenScale());
        v.forEach((p, i) => {
          el("circle", { cx: r2(p[0]), cy: r2(p[1]), r: r2(pr), class: "nfz-vertex",
            "data-action": "nfz-vertex", "data-id": nz.id, "data-i": i }, svg);
        });
      }
    }
  }

  // 航线扫掠走廊（半宽描边近似缓冲区）
  if (wps.length >= 2) {
    const pts = wps.map((w) => `${r2(num(w.x))},${r2(num(w.y))}`).join(" ");
    el("polyline", { points: pts, class: "route-corridor",
      "stroke-width": r2(halfW * 2), "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    el("polyline", { points: pts, class: "route-line" }, svg);
  }

  // 冲突区间（沿航段的红色粗线段）与见证位置
  for (const e of analysis?.entries || []) {
    const a = wps[e.segIndex], b = wps[e.segIndex + 1];
    if (!a || !b) continue;
    const xa = num(a.x) + (num(b.x) - num(a.x)) * e.enterFrac;
    const ya = num(a.y) + (num(b.y) - num(a.y)) * e.enterFrac;
    const xb = num(a.x) + (num(b.x) - num(a.x)) * e.exitFrac;
    const yb = num(a.y) + (num(b.y) - num(a.y)) * e.exitFrac;
    el("line", { x1: r2(xa), y1: r2(ya), x2: r2(xb), y2: r2(yb),
      class: "conflict-overlay", "stroke-width": r2(halfW * 2), "stroke-linecap": "butt" }, svg);
  }
  const first = analysis?.first;
  if (first) {
    const w = first.witness, r = Math.max(9, 11 * screenScale());
    el("circle", { cx: w.x, cy: w.y, r: r2(r * 1.7), class: "witness-ring" }, svg);
    el("path", { d: starPath(w.x, w.y, r, r * 0.45), class: "witness" }, svg);
    el("text", { x: r2(w.x + 14 * screenScale()), y: r2(w.y - 14 * screenScale()),
      class: "witness-label", text: `首个冲突 ${fmtTime(first.enterTime)}` }, svg);
  }

  // 航点
  const pr = Math.max(6, 7 * screenScale());
  wps.forEach((w, i) => {
    const isSel = selection.type === "wp" && selection.id === w.id;
    el("circle", { cx: r2(num(w.x)), cy: r2(num(w.y)), r: r2(pr),
      class: "wp-dot" + (isSel ? " selected" : ""),
      "data-action": "wp-drag", "data-id": w.id }, svg);
    el("text", { x: r2(num(w.x) + 10 * screenScale()), y: r2(num(w.y) - 10 * screenScale()),
      class: "wp-label", text: `${i + 1}. ${w.name || "WP"} ${num(w.alt)}m` }, svg);
  });

  // 绘制预览
  if (mode === "drawNfz" && drawing.length) {
    const pts = drawing.map((p) => `${r2(p[0])},${r2(p[1])}`);
    if (hover) pts.push(`${r2(hover.x)},${r2(hover.y)}`);
    if (pts.length >= 2) el("polyline", { points: pts.join(" "), class: "draw-preview" }, svg);
    drawing.forEach((p) => el("circle", { cx: p[0], cy: p[1], r: r2(pr), class: "nfz-vertex" }, svg));
  }
}

// ----------------------------------------------------------- 视图操作
function fitView() {
  const pts = [];
  state.route.waypoints.forEach((w) => pts.push([num(w.x), num(w.y)]));
  state.nfzs.forEach((n) => (n.vertices || []).forEach((p) => pts.push([num(p[0]), num(p[1])])));
  if (!pts.length) { view = { x: 0, y: 0, w: 1000, h: 620 }; renderBoard(); return; }
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const [x, y] of pts) { minX = Math.min(minX, x); minY = Math.min(minY, y); maxX = Math.max(maxX, x); maxY = Math.max(maxY, y); }
  const pad = 90;
  view = { x: minX - pad, y: minY - pad, w: Math.max(maxX - minX + pad * 2, 200), h: Math.max(maxY - minY + pad * 2, 200) };
  renderBoard();
}

// ----------------------------------------------------------- 侧栏
function renderPanel() {
  panelDirty = false;
  const r = state.route;
  $("#depTime").value = r.departureTime || "";
  $("#corridorW").value = r.corridorHalfWidth ?? 0.3;
  $("#unitScale").value = r.unitScale ?? 100;

  // 航点卡片
  const wpBox = $("#wpList");
  wpBox.innerHTML = "";
  r.waypoints.forEach((w, i) => {
    const card = document.createElement("div");
    card.className = "wp-card" + (selection.type === "wp" && selection.id === w.id ? " selected" : "");
    card.innerHTML = `
      <div class="card-head">
        <b>${i + 1}.</b>
        <input data-f="name" value="${(w.name || "").replace(/"/g, "&quot;")}" style="flex:1;background:#0e1726;color:#d7e0ee;border:1px solid #31415c;border-radius:4px;padding:2px 6px;font-size:12px">
        <button class="del-btn" title="删除">✕</button>
      </div>
      <div class="form-row"><label>x / y</label>
        <input data-f="x" type="number" value="${w.x ?? 0}">
        <input data-f="y" type="number" value="${w.y ?? 0}">
      </div>
      <div class="form-row"><label>高度 m</label>
        <input data-f="alt" type="number" value="${w.alt ?? 0}">
      </div>`;
    card.querySelector(".del-btn").onclick = () => { deleteWaypoint(i); };
    card.querySelectorAll("input").forEach((inp) => {
      inp.oninput = () => {
        const f = inp.dataset.f;
        w[f] = f === "name" ? inp.value : num(inp.value, w[f] ?? 0);
        renderBoard(); scheduleAnalyze(); scheduleSave();
      };
      inp.onfocus = () => { selection = { type: "wp", id: w.id }; renderBoard(); markPanelDirty(); };
    });
    wpBox.appendChild(card);

    if (i < r.waypoints.length - 1) {
      const seg = document.createElement("div");
      seg.className = "seg-row";
      const spd = r.speeds[i] ?? 20;
      seg.innerHTML = `
        <span>→段${i + 1} 速度</span>
        <input type="number" step="1" value="${spd}">
        <span>km/h</span>
        <span class="seg-eta" data-seg="${i}"></span>`;
      const inp = seg.querySelector("input");
      inp.oninput = () => { r.speeds[i] = num(inp.value, 20); scheduleAnalyze(); scheduleSave(); };
      wpBox.appendChild(seg);
    }
  });

  // 禁飞区卡片
  const nfzBox = $("#nfzList");
  nfzBox.innerHTML = "";
  state.nfzs.forEach((nz, i) => {
    const card = document.createElement("div");
    card.className = "nfz-card" + (selection.type === "nfz" && selection.id === nz.id ? " selected" : "");
    card.innerHTML = `
      <div class="card-head">
        <input type="checkbox" ${nz.enabled !== false ? "checked" : ""} title="启用">
        <input data-f="name" value="${(nz.name || "").replace(/"/g, "&quot;")}" style="flex:1;background:#0e1726;color:#d7e0ee;border:1px solid #31415c;border-radius:4px;padding:2px 6px;font-size:12px">
        <button class="del-btn" title="删除">✕</button>
      </div>
      <div class="form-row"><label>高度层 m</label>
        <input data-f="minAlt" type="number" value="${nz.minAlt ?? 0}">
        <input data-f="maxAlt" type="number" value="${nz.maxAlt ?? 300}">
      </div>
      <div class="form-row"><label>生效起</label>
        <input data-f="startTime" type="datetime-local" step="60" value="${nz.startTime || ""}"></div>
      <div class="form-row"><label>生效止</label>
        <input data-f="endTime" type="datetime-local" step="60" value="${nz.endTime || ""}"></div>
      <div class="summary">${(nz.vertices || []).length} 个顶点${selection.type === "nfz" && selection.id === nz.id ? "（黄色顶点可在画布拖动）" : ""}</div>`;
    card.querySelector(".del-btn").onclick = () => { state.nfzs.splice(i, 1); afterNfzStructuralChange(); };
    card.querySelector('input[type="checkbox"]').onchange = (e) => {
      nz.enabled = e.target.checked; renderBoard(); scheduleAnalyze(); saveOrDraftNfz();
    };
    card.querySelectorAll('input:not([type="checkbox"])').forEach((inp) => {
      inp.oninput = () => {
        const f = inp.dataset.f;
        nz[f] = f === "name" || f === "startTime" || f === "endTime" ? inp.value : num(inp.value);
        renderBoard(); scheduleAnalyze(); saveOrDraftNfz();
      };
      inp.onfocus = () => { selection = { type: "nfz", id: nz.id }; renderBoard(); markPanelDirty(); };
    });
    nfzBox.appendChild(card);
  });

  renderDynamicPanel();
  renderNfzDraft();
}

// 仅刷新文本，不重建表单（避免输入失焦）
function renderDynamicPanel() {
  const r = state.route;
  const info = analysis?.route;
  $("#routeSummary").innerHTML = info
    ? `航段数 ${(r.waypoints.length - 1) || 0} ｜ 总航程 ${info.length} ｜ 总时长 ${info.durationMinutes ?? "—"} 分钟` : "";
  (document.querySelectorAll(".seg-eta") || []).forEach((span) => {
    const i = num(span.dataset.seg);
    const d = info?.distances?.[i], t = info?.times?.[i + 1];
    span.textContent = `${d != null ? d + "km" : ""} ETA ${fmtTime(t)}`;
  });
}

function markPanelDirty() { panelDirty = true; }
function refreshPanelIfDirty() { if (panelDirty) renderPanel(); }

function renderResults() {
  const box = $("#resultBox");
  if (!analysis) { box.innerHTML = ""; return; }
  let html = "";
  for (const er of analysis.errors || []) {
    html += `<div class="error-line">⚠ ${er.message}</div>`;
  }
  if (analysis.first) {
    const f = analysis.first;
    html += `<div class="result-bad result-first">
      <div class="big">🚫 首个冲突：${f.nfzName}</div>
      <div>航段 ${f.segIndex + 1} ｜ ${fmtTime(f.enterTime)} → ${fmtTime(f.exitTime)}（${f.durationMinutes ?? 0} 分钟）</div>
      <div>高度重叠 ${f.altLow}–${f.altHigh} m</div>
      <div>见证位置 (${f.witness.x}, ${f.witness.y})，高度 ${f.witness.alt} m</div>
    </div>`;
    html += (analysis.entries || []).map((e, i) => `
      <div class="entry-line ${i === 0 ? "first" : ""}">
        ${i === 0 ? "★ " : ""}${e.nfzName} · 航段${e.segIndex + 1} ·
        ${fmtTime(e.enterTime)}→${fmtTime(e.exitTime)} ·
        高度 ${e.altLow}–${e.altHigh}m · 见证 (${e.witness.x},${e.witness.y})
      </div>`).join("");
  } else if (!(analysis.errors || []).length) {
    html += `<div class="result-good">✅ 未发现平面 ∩ 高度 ∩ 时窗的三维冲突</div>`;
  }
  box.innerHTML = html;
}

// ----------------------------------------------------------- 双页航线冲突
// 409 分流发现“双方都改了航线”后：本页航线、禁飞区草稿与服务器最新航线
// 三者都在页面保留，自动保存暂停，由用户二选一。选择任一项都不会清掉
// 禁飞区草稿；确认提交始终用所见最新修订号做条件写入，再遇 409 时继续
// 保留本页编辑并重新展示冲突，绝不被当成保存成功。
function summarizeRoute(route) {
  const wps = (route && route.waypoints) || [];
  const names = wps.map((w) => w.name || "WP").join(" → ");
  const parts = [`${wps.length} 个航点`];
  if ((route && route.departureTime) || wps.length) {
    parts.push(`起飞 ${(route && route.departureTime) || "—"}`);
  }
  parts.push(names || "（空航线）");
  return parts.join(" ｜ ");
}

function renderRouteConflict() {
  const box = $("#routeConflictBox");
  if (!box) return;
  if (!routeConflict) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="conflict-card">
    <div class="conflict-head">⚠ 双页航线冲突，自动保存已暂停</div>
    <div class="summary">他页已先保存航线（工作区修订 ${routeConflict.rev}）。本页未保存航线、禁飞区草稿均已保留，请选择：</div>
    <div class="conflict-side">
      <b>本页航线（未保存）</b>
      <div class="summary">${esc(summarizeRoute(state.route))}</div>
    </div>
    <div class="conflict-side">
      <b>服务器最新航线（他页已保存）</b>
      <div class="summary">${esc(summarizeRoute(routeConflict.route))}</div>
    </div>
    <div class="draft-actions">
      <button id="btnConflictRemote">加载服务器航线</button>
      <button id="btnConflictLocal" class="minor">确认提交本页航线</button>
    </div>
  </div>`;
  $("#btnConflictRemote").onclick = chooseRemoteRoute;
  $("#btnConflictLocal").onclick = confirmLocalRoute;
}

async function chooseRemoteRoute() {
  if (!routeConflict) return;
  // 仅切换本页航线为服务器最新：不发起写入（服务器本就是它），
  // 禁飞区草稿原样保留（state.nfzs 不动），基线/修订号已在冲突发生时同步
  state.route = clone(routeConflict.route);
  serverRouteSeen = clone(routeConflict.route);
  routeConflict = null;
  if (selection.type === "wp") selection = { type: null, id: null };
  renderRouteConflict();
  renderPanel(); renderBoard(); runAnalyze(); renderNfzDraft();
  toast("已加载服务器最新航线（禁飞区草稿保留），自动保存已恢复");
}

async function confirmLocalRoute() {
  if (!routeConflict) return;
  // 用所见最新修订号条件写入本页航线；再遇 409 时 doSaveState 会重新
  // 分流：仅禁飞区被他页核准则合入，航线仍冲突则继续保留并再次提示
  const rev = routeConflict.rev;
  routeConflict = null;
  renderRouteConflict();
  try {
    await enqueue(() => doSaveStateWith(rev));
  } catch (e) {
    if (e instanceof RouteConflictError) return; // 冲突面板已再次展示
    toast(e.message || "提交失败");
  }
}

// 以显式指定的“所见最新修订号”发起一次条件保存（其余同 doSaveState）
async function doSaveStateWith(rev) {
  workspaceRev = rev;
  return doSaveState();
}

// ----------------------------------------------------------- 结构变更
function afterStructuralChange() {
  renderBoard(); renderPanel(); scheduleAnalyze(); scheduleSave();
}
// 禁飞区结构变更：已有发布时只留草稿，不触发落库
function afterNfzStructuralChange() {
  renderBoard(); renderPanel(); scheduleAnalyze(); saveOrDraftNfz();
}
function deleteWaypoint(i) {
  const r = state.route;
  r.waypoints.splice(i, 1);
  if (i < r.speeds.length) r.speeds.splice(i, 1);
  if (selection.type === "wp") selection = { type: null, id: null };
  afterStructuralChange();
}
function appendWaypoint(w) {
  const r = state.route;
  r.waypoints.push(w);
  if (r.speeds.length < r.waypoints.length - 1) r.speeds.push(20);
  afterStructuralChange();
}

// ----------------------------------------------------------- 画布交互
svg.addEventListener("mousedown", (e) => {
  const t = e.target.closest("[data-action]");
  const action = t?.dataset.action;
  const w = toWorld(e.clientX, e.clientY);

  if (action === "wp-drag") {
    const id = t.dataset.id;
    const wp = state.route.waypoints.find((x) => x.id === id);
    selection = { type: "wp", id };
    drag = { kind: "wp", wp, startX: e.clientX, startY: e.clientY, moved: false };
    refreshPanelIfDirty(); renderBoard();
  } else if (action === "nfz-vertex") {
    const nz = state.nfzs.find((n) => n.id === t.dataset.id);
    drag = { kind: "vertex", nz, i: +t.dataset.i, startX: e.clientX, startY: e.clientY, moved: false };
  } else if (action === "nfz-pick") {
    selection = { type: "nfz", id: t.dataset.id };
    markPanelDirty(); renderBoard();
  } else {
    // 背景：开始平移
    drag = { kind: "pan", view: { ...view }, startX: e.clientX, startY: e.clientY };
  }
});

svg.addEventListener("mousemove", (e) => {
  const w = toWorld(e.clientX, e.clientY);
  hover = w;
  if (!drag) {
    if (mode === "drawNfz" && drawing.length) renderBoard();
    return;
  }
  const dxPx = e.clientX - drag.startX, dyPx = e.clientY - drag.startY;
  if (Math.abs(dxPx) + Math.abs(dyPx) > 3) drag.moved = true;

  if (drag.kind === "pan") {
    const rect = svg.getBoundingClientRect();
    view.x = drag.view.x - (dxPx / rect.width) * view.w;
    view.y = drag.view.y - (dyPx / rect.height) * view.h;
    renderBoard();
  } else if (drag.kind === "wp") {
    drag.wp.x = r2(w.x); drag.wp.y = r2(w.y);
    renderBoard();
  } else if (drag.kind === "vertex") {
    drag.nz.vertices[drag.i] = [r2(w.x), r2(w.y)];
    renderBoard();
  }
});

window.addEventListener("mouseup", (e) => {
  if (!drag) return;
  const wasClick = !drag.moved;
  const d = drag;
  drag = null;
  if (wasClick && d.kind === "pan") {
    const w = toWorld(e.clientX, e.clientY);
    if (mode === "addWp") {
      appendWaypoint({ id: uid("w"), name: "WP", x: r2(w.x), y: r2(w.y), alt: 200 });
    } else if (mode === "drawNfz") {
      drawing.push([r2(w.x), r2(w.y)]);
      renderBoard();
    } else {
      selection = { type: null, id: null };
      markPanelDirty(); renderBoard();
    }
  } else if (d.kind === "wp") {
    if (d.moved) { scheduleAnalyze(); scheduleSave(); }
  } else if (d.kind === "vertex") {
    if (d.moved) { scheduleAnalyze(); saveOrDraftNfz(); }
  }
});

svg.addEventListener("dblclick", (e) => {
  if (mode === "drawNfz" && drawing.length >= 3) {
    // 双击的第一次 click 已落下同位置末点，去重
    if (typeof e.clientX === "number") {
      const w = toWorld(e.clientX, e.clientY);
      const last = drawing[drawing.length - 1];
      if (Math.hypot(last[0] - w.x, last[1] - w.y) < 5) drawing.pop();
    }
    const verts = drawing.map((p) => [r2(p[0]), r2(p[1])]);
    if (verts.length >= 3) {
      state.nfzs.push({
        id: uid("n"), name: `禁飞区-${state.nfzs.length + 1}`, vertices: verts,
        minAlt: 0, maxAlt: 300, startTime: "", endTime: "", enabled: true,
      });
      drawing = [];
      afterNfzStructuralChange();
    }
  }
});

svg.addEventListener("wheel", (e) => {
  e.preventDefault();
  const w = toWorld(e.clientX, e.clientY);
  const factor = e.deltaY > 0 ? 1.15 : 1 / 1.15;
  view = {
    x: w.x - (w.x - view.x) * factor,
    y: w.y - (w.y - view.y) * factor,
    w: view.w * factor, h: view.h * factor,
  };
  renderBoard();
}, { passive: false });

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (drawing.length) { drawing = []; renderBoard(); }
    else { setMode("select"); }
  } else if (e.key === "Enter" && mode === "drawNfz" && drawing.length >= 3) {
    svg.dispatchEvent(new Event("dblclick"));
  }
});

// ----------------------------------------------------------- 顶部控件
function setMode(m) {
  mode = m;
  document.querySelectorAll(".mode").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
  svg.style.cursor = m === "select" ? "default" : "crosshair";
  const hints = {
    select: "选择/拖动：拖动航点与禁飞区顶点重算；拖空白平移；滚轮缩放",
    addWp: "点击画布追加航点（Esc 结束）",
    drawNfz: "单击依次落点，双击或回车完成禁飞多边形（Esc 取消）",
  };
  $("#modeHint").textContent = hints[m];
  if (m !== "drawNfz") drawing = [];
  renderBoard();
}
document.querySelectorAll(".mode").forEach((b) => b.onclick = () => setMode(b.dataset.mode));
$("#btnFit").onclick = fitView;
$("#btnAnalyze").onclick = runAnalyze;

$("#depTime").oninput = (e) => { state.route.departureTime = e.target.value; scheduleAnalyze(); scheduleSave(); };
$("#corridorW").oninput = (e) => { state.route.corridorHalfWidth = num(e.target.value, 0.3); renderBoard(); scheduleAnalyze(); scheduleSave(); };
$("#unitScale").oninput = (e) => { state.route.unitScale = num(e.target.value, 100); renderBoard(); scheduleAnalyze(); scheduleSave(); };

$("#btnAddNfz").onclick = () => {
  const cx = view.x + view.w / 2, cy = view.y + view.h / 2, s = Math.min(view.w, view.h) / 8;
  state.nfzs.push({
    id: uid("n"), name: `禁飞区-${state.nfzs.length + 1}`,
    vertices: [[r2(cx - s), r2(cy - s)], [r2(cx + s), r2(cy - s)], [r2(cx + s), r2(cy + s)], [r2(cx - s), r2(cy + s)]],
    minAlt: 0, maxAlt: 300, startTime: "", endTime: "", enabled: true,
  });
  selection = { type: "nfz", id: state.nfzs[state.nfzs.length - 1].id };
  afterNfzStructuralChange();
};

// ----------------------------------------------------------- 发布
const esc = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// 发布状态读取：与 GET /api/state 之间工作区修订号可能已被他页推进。
// 读取失败与“确认从无发布”必须区分：失败时保留编辑、侧栏提示刷新，
// 绝不能当作未发布而放行无条件 PUT。
async function loadRelease() {
  let r = null;
  try {
    r = await api("/api/release");
  } catch (e) {
    r = null;
  }
  if (!r || !r.ok) {
    releaseState = "error";
    resolveReleaseReady(releaseState);
    renderRelease();
    renderNfzDraft();
    return;
  }
  const seenRev = workspaceRev; // GET /api/state 读到的修订号
  if (r.release) {
    releaseState = "published";
    currentRelease = r.release;
    if (typeof r.workspaceRev === "number") workspaceRev = r.workspaceRev;
    renderRelease();
    renderNfzDraft();
    renderVoyages();
    // 两次读取之间修订号已变：先取回最新工作区（合入他页已核准禁飞区；
    // 仅他页改航线则直接采用远端；双方航线都变则走冲突选择流程并暂停自动
    // 保存），完成后再放行写请求门，避免“新修订号 + 编辑前所见旧航线/旧
    // 禁飞区”被立刻条件写回。再次 GET 失败时不放开门，阻断一切写入直到重试
    let outcome = "ok";
    if (typeof r.workspaceRev === "number") {
      outcome = await reconcileWorkspaceRevIfPublished(seenRev);
    }
    if (outcome === "failed") return; // 就绪承诺保持挂起：所有写请求继续等待
    resolveReleaseReady(releaseState);
    renderNfzDraft();
    replayWithheldWrite(outcome === "conflict");
  } else {
    releaseState = "none";
    currentRelease = null;
    if (typeof r.workspaceRev === "number") workspaceRev = r.workspaceRev;
    resolveReleaseReady(releaseState);
    renderRelease();
    renderNfzDraft();
    renderVoyages();
    replayWithheldWrite(false);
  }
}

// error 期间被挡下的防抖保存：状态恢复明确后若仍有未保存编辑，补发一次。
// 航线冲突已分流时（自动保存已暂停）不补发，沿用冲突选择流程；禁飞区草稿
// 本就不落库，发布明确后由草稿面板继续提示核准。
function replayWithheldWrite(conflicted) {
  if (!writesWithheld) return;
  writesWithheld = false;
  if (conflicted || routeConflict) return;
  const routeDirty = !sameJson(state.route, serverRouteSeen);
  if (releaseState === "none") {
    const anythingDirty = routeDirty || !sameJson(state.nfzs, savedNfzs);
    if (anythingDirty) scheduleSave();
  } else if (releaseState === "published" && routeDirty) {
    scheduleSave();
  }
}

// 用户在读取失败提示上点“重试/刷新”：重新读取前先复位就绪承诺，
// 期间一切工作区写请求继续被挡住，直到再次明确（成功或仍失败）
function refreshRelease() {
  if (releaseState !== "error") return loadRelease();
  releaseState = "loading";
  resetReleaseReady();
  renderRelease();
  renderNfzDraft();
  return loadRelease();
}

// err 非空时在发布卡片上方展示最近一次发布失败的原因与明细
function renderRelease(err) {
  const box = $("#releaseBox");
  let html = "";
  if (releaseState === "loading") {
    box.innerHTML = `<div class="summary">正在确认发布状态…（发布状态明确前暂不自动保存工作区，编辑仍保留在本页）</div>`;
    return;
  }
  if (workspaceCoordError) {
    box.innerHTML = `<div class="error-line">⚠ 启动协调失败：两次读取之间工作区已被他页更新（修订号 ${workspaceRev}），但取回最新工作区失败。本页航线与禁飞区草稿已保留，自动保存及相邻核准/发布一律暂停——绝不会用新修订号把旧航线/旧禁飞区写回。<button id="btnCoordRetry" class="link-btn">重试</button>取回最新工作区后继续。</div>`;
    const btn = $("#btnCoordRetry");
    if (btn) btn.onclick = () => retryWorkspaceCoordination();
    return;
  }
  if (releaseState === "error") {
    box.innerHTML = `<div class="error-line">⚠ 发布状态读取失败：本页编辑已保留，但在确认发布状态前不会保存工作区，请刷新页面或<button id="btnReleaseRetry" class="link-btn">重试</button>。</div>`;
    const btn = $("#btnReleaseRetry");
    if (btn) btn.onclick = () => refreshRelease();
    return;
  }
  if (err) {
    html += `<div class="error-line">⚠ 发布失败：${esc(err.error || "未知原因")}</div>`;
    for (const e of err.errors || []) {
      html += `<div class="error-line">⚠ ${esc(e.message)}</div>`;
    }
    for (const e of err.entries || []) {
      html += `<div class="entry-line">✈ ${esc(e.nfzName)} · 航段${e.segIndex + 1} ·
        ${fmtTime(e.enterTime)}→${fmtTime(e.exitTime)} · 高度 ${e.altLow}–${e.altHigh}m</div>`;
    }
    if (err.details) {
      for (const [k, v] of Object.entries(err.details)) {
        const label = k === "workspaceRev" ? "工作区修订号" : "已发布号";
        html += `<div class="entry-line">${label}：读取时 ${v.expected} → 当前 ${v.actual}</div>`;
      }
    }
  }
  if (currentRelease) {
    const r = currentRelease;
    const route = (r.analysis && r.analysis.route) || {};
    html += `<div class="release-card">
      <div class="rel-head">#${r.seq} ｜ 来源：${esc(r.sourceVersionName)}（版本 ${r.sourceVersionId}）</div>
      <div class="summary">发布于 ${r.createdAt} ｜ 工作区修订 ${r.workspaceRev}</div>
      <div class="result-good" style="margin-top:6px">✅ 审查通过：无三维冲突
        ｜ 总航程 ${route.length ?? "—"} km ｜ 总时长 ${route.durationMinutes ?? "—"} 分钟</div>
    </div>`;
  } else {
    html += `<div class="summary">暂无发布</div>`;
  }
  box.innerHTML = html;
}

async function publishVersion(v) {
  // 先让在途航线自动保存落库（发布仍取当前工作区禁飞区），再在同一条串行队列里发布。
  // 在途保存若落为未解决的双方航线冲突，不得继续发布。
  if (routeConflict) {
    toast("请先解决航线冲突，再发布航线");
    return;
  }
  if (workspaceCoordError) {
    toast("启动协调未完成：请先点侧栏“重试”取回最新工作区，未继续发布");
    return;
  }
  try {
    await waitReleaseReady();
    await flushPendingSave();
  } catch (e) {
    if (e instanceof WorkspaceCoordError) {
      toast("启动协调未完成：请先点侧栏“重试”取回最新工作区，未继续发布");
      return;
    }
    if (e instanceof ReleaseUnavailableError) {
      toast("发布状态尚未确认或读取失败，请刷新页面后重试，未继续发布");
      return;
    }
    if (e instanceof RouteConflictError) {
      toast("航线自动保存与他页冲突，已暂停，请先选择处理方式，未继续发布");
      return;
    }
    toast("自动保存未完成：" + (e.message || "保存失败") + "，未继续发布");
    return;
  }
  return enqueue(async () => {
    let r;
    try {
      r = await api("/api/release", {
        method: "POST",
        body: JSON.stringify({
          versionId: v.id,
          workspaceRev,
          releaseSeq: currentRelease ? currentRelease.seq : 0,
        }),
      });
    } catch (e) {
      toast("发布请求失败：" + e);
      return;
    }
    if (r.ok) {
      currentRelease = r.release;
      releaseState = "published";
      renderRelease();
      renderNfzDraft(); // 进入“已有发布”状态：此后的禁飞区变更先存草稿
      loadVoyages();    // 新发布出现后航次登记面板切换到该发布号
      toast(`已发布 #${r.release.seq}（来源：${r.release.sourceVersionName}）`);
    } else {
      if (r.reason === "stale") {
        // 已发布号/工作区已变化：先同步最新状态，再展示失败原因
        try {
          const cur = await api("/api/release");
          if (cur && cur.ok) {
            currentRelease = cur.release;
            releaseState = cur.release ? "published" : "none";
            if (typeof cur.workspaceRev === "number") workspaceRev = cur.workspaceRev;
          }
        } catch (e) { /* 忽略 */ }
      }
      renderRelease(r);
      toast("发布失败：" + (r.error || "未知原因"));
    }
  });
}

// ----------------------------------------------------------- 禁飞区草稿核准
// 已有发布时，禁飞区的增删/拖动/修改（含载入版本带来的变化）只存为页面草稿，
// 提交核准通过后才替换工作区禁飞区；尚未发布时沿用原自动保存。
function saveOrDraftNfz() {
  if (releaseState === "published") renderNfzDraft(); // 留作草稿，不落库
  else if (releaseState === "none") scheduleSave();
  // loading / error：发布状态未明确，编辑只保留在页面，不发工作区写请求；
  // 状态确认后（loadRelease 的合流/冲突分流）再决定草稿去留，绝不按未发布处理
}
function nfzDraftDirty() {
  return !!currentRelease && JSON.stringify(state.nfzs) !== JSON.stringify(savedNfzs);
}

// err 非空时展示最近一次核准失败的原因与明细（草稿保留，可继续修改）
function renderNfzDraft(err) {
  const box = $("#nfzDraftBox");
  if (!box) return;
  if (!currentRelease) { box.innerHTML = ""; return; }
  let html = "";
  if (err) {
    html += `<div class="error-line">⚠ 核准未通过：${esc(err.error || "未知原因")}</div>`;
    for (const e of err.errors || []) {
      html += `<div class="error-line">⚠ ${esc(e.message)}</div>`;
    }
    for (const e of err.entries || []) {
      html += `<div class="entry-line">✈ ${esc(e.nfzName)} · 航段${e.segIndex + 1} ·
        ${fmtTime(e.enterTime)}→${fmtTime(e.exitTime)} · 高度 ${e.altLow}–${e.altHigh}m</div>`;
    }
    if (err.details) {
      for (const [k, v] of Object.entries(err.details)) {
        const label = k === "workspaceRev" ? "工作区修订号" : "已发布号";
        html += `<div class="entry-line">${label}：读取时 ${v.expected} → 当前 ${v.actual}</div>`;
      }
    }
    // 候选禁飞区与未结束航次（含旧发布航次）冲突：逐个航次展示冲突明细
    for (const vc of err.voyageConflicts || []) {
      html += `<div class="error-line">⚠ 航次 #${vc.voyageId}（发布 #${vc.releaseSeq}，起飞 ${fmtTime(vc.departureTime)}）：${vc.entries.length} 处冲突</div>`;
      for (const e of vc.entries || []) {
        html += `<div class="entry-line">✈ ${esc(e.nfzName)} · 航段${e.segIndex + 1} ·
          ${fmtTime(e.enterTime)}→${fmtTime(e.exitTime)} · 高度 ${e.altLow}–${e.altHigh}m</div>`;
      }
    }
  }
  if (nfzDraftDirty()) {
    html += `<div class="draft-card">
      <div class="draft-head">● 禁飞区草稿未核准</div>
      <div class="summary">变更仅暂存于本页，尚未落库；提交后将与发布 #${currentRelease.seq} 的航线组合审查，通过才替换工作区禁飞区。</div>
      <div class="draft-actions">
        <button id="btnNfzApprove">提交核准</button>
        <button id="btnNfzDiscard" class="minor">放弃草稿</button>
      </div>
    </div>`;
  } else {
    html += `<div class="summary">已有发布 #${currentRelease.seq} ｜ 工作区修订 ${workspaceRev}：此后的禁飞区变更先存为草稿，核准后生效。</div>`;
  }
  box.innerHTML = html;
  const approveBtn = $("#btnNfzApprove"), discardBtn = $("#btnNfzDiscard");
  if (approveBtn) approveBtn.onclick = submitNfzApproval;
  if (discardBtn) discardBtn.onclick = discardNfzDraft;
}

// 提交核准绝不预先 PUT 工作区：先等在途的航线自动保存落库（协调顺序与修订号），
// 再携带“所见”工作区修订号与发布号提交；被拒绝时展示原因与冲突/并发明细，
// 草稿原样保留。核准操作本身不改变工作区航线或发布快照——通过时由服务端
// 仅原子替换工作区禁飞区。
async function submitNfzApproval() {
  // 未解决的航线冲突不得被当成保存成功而继续核准：
  // flush 期间若在途保存落为冲突（RouteConflictError），直接中止
  if (routeConflict) {
    toast("请先解决航线冲突，再提交禁飞区核准");
    return;
  }
  if (workspaceCoordError) {
    toast("启动协调未完成：请先点侧栏“重试”取回最新工作区，未继续核准");
    return;
  }
  try {
    await waitReleaseReady();
    await flushPendingSave();
  } catch (e) {
    if (e instanceof WorkspaceCoordError) {
      toast("启动协调未完成：请先点侧栏“重试”取回最新工作区，未继续核准");
      return;
    }
    if (e instanceof ReleaseUnavailableError) {
      toast("发布状态尚未确认或读取失败，请刷新页面后重试，未继续核准");
      return;
    }
    if (e instanceof RouteConflictError) {
      toast("航线自动保存与他页冲突，已暂停，请先选择处理方式，未继续核准");
      return;
    }
    toast("自动保存未完成：" + (e.message || "保存失败") + "，未继续核准");
    return;
  }
  return enqueue(() => doNfzApproval());
}

async function doNfzApproval() {
  const candidate = clone(state.nfzs);
  const seq = currentRelease ? currentRelease.seq : 0;
  let r;
  try {
    r = await api("/api/nfz-approval", {
      method: "POST",
      body: JSON.stringify({ nfzs: candidate, workspaceRev, releaseSeq: seq }),
    });
  } catch (e) {
    toast("核准请求失败：" + e);
    return;
  }
  if (r.ok) {
    workspaceRev = r.rev;
    savedNfzs = clone(candidate); // 草稿已成为工作区禁飞区（航线/发布快照未动）
    renderNfzDraft();
    toast(`禁飞区变更已核准（工作区修订 ${r.rev}）`);
    return;
  }
  if (r.reason === "stale") {
    // 工作区修订号/发布号已变化：同步最新号与他页已核准禁飞区（草稿保留），再展示明细
    try {
      const [relRes, stRes] = await Promise.all([api("/api/release"), fetch("/api/state")]);
      const srev = parseInt(stRes.headers.get("X-Workspace-Rev") || "0", 10) || workspaceRev;
      const fresh = await stRes.json();
      workspaceRev = srev;
      serverRouteSeen = clone(fresh.route || serverRouteSeen);
      // 已核准基线以服务器为准：本页无草稿时画布一并采用，有草稿则只换基线、草稿不覆盖
      adoptServerNfzs(fresh.nfzs || []);
      if (relRes && relRes.ok) {
        currentRelease = relRes.release;
        releaseState = relRes.release ? "published" : "none";
      }
    } catch (e) { /* 仍展示下方失败明细 */ }
    renderRelease();
  }
  renderNfzDraft(r); // 草稿保留，可继续修改后重新提交
  toast("核准未通过：" + (r.error || "未知原因"));
}

function discardNfzDraft() {
  state.nfzs = clone(savedNfzs); // 还原为工作区已核准内容（无需落库，服务端本就是它）
  if (selection.type === "nfz") selection = { type: null, id: null };
  renderPanel(); renderBoard(); runAnalyze();
  toast("已放弃禁飞区草稿");
}

// ----------------------------------------------------------- 航次登记
// 航次以“当前发布号 + 起飞时刻”登记：服务端取该发布快照航线替换起飞时刻，
// 与工作区已核准禁飞区重新审查，安全时落库航次编号/发布号/起飞时刻/航线快照。
// 无发布、发布号已变、时刻无效或审查报错/冲突均拒绝且不留记录。
async function loadVoyages() {
  let r = null;
  try {
    r = await api("/api/voyages");
  } catch (e) {
    r = null;
  }
  if (r && r.ok) {
    voyages = r.voyages || [];
  }
  renderVoyages();
}

function renderVoyageRegister(err) {
  const box = $("#voyageRegisterBox");
  if (!box) return;
  let html = "";
  if (err) {
    html += `<div class="error-line">⚠ 航次登记被拒绝：${esc(err.error || "未知原因")}</div>`;
    for (const e of err.errors || []) {
      html += `<div class="error-line">⚠ ${esc(e.message)}</div>`;
    }
    for (const e of err.entries || []) {
      html += `<div class="entry-line">✈ ${esc(e.nfzName)} · 航段${e.segIndex + 1} ·
        ${fmtTime(e.enterTime)}→${fmtTime(e.exitTime)} · 高度 ${e.altLow}–${e.altHigh}m</div>`;
    }
    if (err.details) {
      for (const [k, v] of Object.entries(err.details)) {
        const label = k === "workspaceRev" ? "工作区修订号" : "已发布号";
        html += `<div class="entry-line">${label}：读取时 ${v.expected} → 当前 ${v.actual}</div>`;
      }
    }
  }
  if (releaseState === "loading") {
    html += `<div class="summary">正在确认发布状态…</div>`;
  } else if (releaseState === "error") {
    html += `<div class="error-line">⚠ 发布状态读取失败，请刷新页面或在「当前发布」处重试后再登记航次。</div>`;
  } else if (!currentRelease) {
    html += `<div class="summary">尚无已发布航线，暂不能登记航次。</div>`;
  } else {
    // 发布号变化（含首次出现发布）时预填该发布快照的起飞时刻，之后保留用户草稿
    if (voyageDepSeq !== currentRelease.seq) {
      voyageDepSeq = currentRelease.seq;
      voyageDepDraft = (currentRelease.state && currentRelease.state.route
        && currentRelease.state.route.departureTime) || "";
    }
    html += `<div class="summary">按当前发布 #${currentRelease.seq} 登记：服务端以该发布快照航线替换起飞时刻，与工作区已核准禁飞区重新审查，安全才落库。</div>
      <div class="form-row" style="margin-top:6px">
        <label>起飞时刻</label>
        <input type="datetime-local" id="voyageDep" step="60" style="flex:1;min-width:0"
               value="${esc(voyageDepDraft)}">
      </div>
      <div class="draft-actions">
        <button id="btnVoyageRegister">登记航次</button>
      </div>`;
  }
  box.innerHTML = html;
  const dep = $("#voyageDep");
  if (dep) {
    dep.oninput = () => { voyageDepDraft = dep.value; };
    $("#btnVoyageRegister").onclick = submitVoyageRegister;
  }
}

function renderVoyageList() {
  const box = $("#voyageListBox");
  if (!box) return;
  if (!voyages.length) {
    box.innerHTML = `<div class="summary">暂无已登记航次。</div>`;
    return;
  }
  box.innerHTML = voyages.map((v) => {
    const status = v.open
      ? `<span class="voyage-open">执行中</span>`
      : `<span class="voyage-ended">已结束 ${esc(v.endedAt)}</span>`;
    const btn = v.open
      ? ` <button data-act="resched" data-id="${v.id}" class="minor" style="padding:2px 8px;font-size:11px">改期</button>` +
        ` <button data-act="end" data-id="${v.id}" class="minor" style="padding:2px 8px;font-size:11px">结束航次</button>`
      : "";
    return `<div class="voyage-item${v.open ? " open" : ""}">
      <div class="vmeta">
        <div class="vname">航次 #${v.id} ｜ 发布 #${v.releaseSeq} ${status}</div>
        <div class="vtime">起飞 ${fmtTime(v.departureTime)} ｜ 登记于 ${esc(v.createdAt)}</div>
      </div>${btn}
    </div>`;
  }).join("");
  box.querySelectorAll("[data-act='end']").forEach((b) => {
    b.onclick = () => endVoyage(+b.dataset.id);
  });
  box.querySelectorAll("[data-act='resched']").forEach((b) => {
    b.onclick = () => startReschedule(+b.dataset.id);
  });
}

// 航次改期表单：选中未结束航次后展示；新时刻输入草稿与被拒明细都保留在页面
function renderVoyageReschedule() {
  const box = $("#voyageRescheduleBox");
  if (!box) return;
  if (!rescheduleTarget) { box.innerHTML = ""; return; }
  const v = voyages.find((x) => x.id === rescheduleTarget.id);
  let html = "";
  if (rescheduleErr) {
    html += `<div class="error-line">⚠ 改期被拒绝：${esc(rescheduleErr.error || "未知原因")}</div>`;
    for (const e of rescheduleErr.errors || []) {
      html += `<div class="error-line">⚠ ${esc(e.message)}</div>`;
    }
    for (const e of rescheduleErr.entries || []) {
      html += `<div class="entry-line">✈ ${esc(e.nfzName)} · 航段${e.segIndex + 1} ·
        ${fmtTime(e.enterTime)}→${fmtTime(e.exitTime)} · 高度 ${e.altLow}–${e.altHigh}m</div>`;
    }
    if (rescheduleErr.details) {
      for (const [, val] of Object.entries(rescheduleErr.details)) {
        html += `<div class="entry-line">原时刻：提交时 ${fmtTime(val.expected)} → 当前 ${fmtTime(val.actual)}</div>`;
      }
    }
  }
  html += `<div class="draft-card">
    <div class="draft-head">改期航次 #${rescheduleTarget.id}${v ? `（发布 #${v.releaseSeq}）` : ""}</div>
    <div class="summary">原起飞时刻 ${fmtTime(rescheduleTarget.originalDepartureTime)}：服务端保留航次编号、登记时间与原发布号，仅替换该航次航线快照中的起飞时刻，用当前已核准禁飞区重新审查。</div>
    <div class="form-row" style="margin-top:6px">
      <label>新起飞时刻</label>
      <input type="datetime-local" id="rescheduleDep" step="60" style="flex:1;min-width:0"
             value="${esc(rescheduleDraft)}">
    </div>
    <div class="draft-actions">
      <button id="btnRescheduleSubmit">提交改期</button>
      <button id="btnRescheduleCancel" class="minor">取消</button>
    </div>
  </div>`;
  box.innerHTML = html;
  const dep = $("#rescheduleDep");
  dep.oninput = () => { rescheduleDraft = dep.value; };
  $("#btnRescheduleSubmit").onclick = submitReschedule;
  $("#btnRescheduleCancel").onclick = cancelReschedule;
}

function renderVoyages(err) {
  const root = $("#voyageBox");
  if (!root) return;
  root.innerHTML = `<div id="voyageRegisterBox"></div><div id="voyageRescheduleBox" style="margin-top:8px"></div><div id="voyageListBox" style="margin-top:8px"></div>`;
  renderVoyageRegister(err);
  renderVoyageReschedule();
  renderVoyageList();
}

async function submitVoyageRegister() {
  if (!currentRelease) return;
  const depVal = voyageDepDraft || "";
  if (!depVal) {
    renderVoyages({ error: "请先填写起飞时刻" });
    return;
  }
  const seq = currentRelease.seq;
  let r;
  try {
    r = await api("/api/voyages", {
      method: "POST",
      body: JSON.stringify({ releaseSeq: seq, departureTime: depVal }),
    });
  } catch (e) {
    renderVoyages({ error: "登记请求失败：" + e });
    return;
  }
  if (r.ok) {
    toast(`已登记航次 #${r.voyage.id}（发布 #${r.voyage.releaseSeq}）`);
    await loadVoyages();
    return;
  }
  if (r.reason === "stale" || r.reason === "no_release") {
    // 发布号已变化（如他页刚发布）：同步最新发布后再展示拒绝明细
    try {
      const cur = await api("/api/release");
      if (cur && cur.ok) {
        currentRelease = cur.release;
        releaseState = cur.release ? "published" : "none";
        if (typeof cur.workspaceRev === "number") workspaceRev = cur.workspaceRev;
      }
    } catch (e) { /* 仍展示下方明细 */ }
    renderRelease();
    renderNfzDraft();
  }
  renderVoyages(r);
  toast("航次登记被拒绝：" + (r.error || "未知原因"));
}

// ----------------------------------------------------------- 航次改期
// 侧栏选取未结束航次，输入新起飞时刻并携带所见原时刻提交；服务端仅替换该
// 航次航线快照中的起飞时刻并用当前已核准禁飞区重审（即使已有新发布，也沿用
// 该航次自己的航线几何）。被拒时原因与明细展示在表单上方，新时刻输入保留。
function startReschedule(id) {
  const v = voyages.find((x) => x.id === id);
  if (!v || !v.open) return;
  rescheduleTarget = { id: v.id, originalDepartureTime: v.departureTime };
  rescheduleDraft = v.departureTime || "";
  rescheduleErr = null;
  renderVoyages();
}

function cancelReschedule() {
  rescheduleTarget = null;
  rescheduleDraft = "";
  rescheduleErr = null;
  renderVoyages();
}

async function submitReschedule() {
  if (!rescheduleTarget) return;
  const depVal = rescheduleDraft || "";
  if (!depVal) {
    rescheduleErr = { error: "请先填写新的起飞时刻" };
    renderVoyageReschedule();
    return;
  }
  const { id, originalDepartureTime } = rescheduleTarget;
  let r;
  try {
    r = await api(`/api/voyages/${id}/reschedule`, {
      method: "POST",
      body: JSON.stringify({ departureTime: depVal, originalDepartureTime }),
    });
  } catch (e) {
    rescheduleErr = { error: "改期请求失败：" + e };
    renderVoyageReschedule();
    return;
  }
  if (r.ok) {
    toast(`航次 #${id} 已改期至 ${fmtTime(r.voyage.departureTime)}`);
    rescheduleTarget = null;
    rescheduleDraft = "";
    rescheduleErr = null;
    await loadVoyages();
    return;
  }
  rescheduleErr = r;
  if (r.reason === "stale" || r.reason === "voyage_not_found" || r.reason === "voyage_ended") {
    // 原时刻被他页改动 / 航次已结束或不存在：同步最新航次列表；
    // 航次仍未结束时采用最新原时刻作为下次提交基线，新时刻输入保留
    try { await loadVoyages(); } catch (e) { /* 仍展示下方明细 */ }
    const fresh = voyages.find((x) => x.id === id);
    if (rescheduleTarget && fresh && fresh.open) {
      rescheduleTarget.originalDepartureTime = fresh.departureTime;
    }
  }
  renderVoyages();
  toast("航次改期被拒绝：" + (r.error || "未知原因"));
}

async function endVoyage(id) {
  let r;
  try {
    r = await api(`/api/voyages/${id}/end`, { method: "POST" });
  } catch (e) {
    toast("结束航次请求失败：" + e);
    return;
  }
  if (r.ok) {
    toast(`航次 #${id} 已结束，不再阻拦后续禁飞区核准`);
    if (rescheduleTarget && rescheduleTarget.id === id) {
      rescheduleTarget = null;
      rescheduleDraft = "";
      rescheduleErr = null;
    }
    await loadVoyages();
  } else {
    toast("结束航次失败：" + (r.error || "未知原因"));
  }
}

// ----------------------------------------------------------- 版本
async function loadVersions() {
  const list = await api("/api/versions");
  const box = $("#versionList");
  box.innerHTML = "";
  if (!list.length) { box.innerHTML = '<div class="summary">暂无保存版本</div>'; return; }
  for (const v of list) {
    const item = document.createElement("div");
    item.className = "version-item";
    item.innerHTML = `<div class="vmeta"><div class="vname"></div><div class="vtime"></div></div>
      <button data-act="pub">发布</button><button data-act="load">载入</button><button data-act="del">删除</button>`;
    item.querySelector(".vname").textContent = v.name;
    item.querySelector(".vtime").textContent = v.createdAt;
    item.querySelector('[data-act="pub"]').onclick = () => publishVersion(v);
    item.querySelector('[data-act="load"]').onclick = async () => {
      if (!confirm(`载入版本「${v.name}」？当前未另存的内容将被覆盖。`)) return;
      const full = await api(`/api/versions/${v.id}`);
      Object.assign(state, full.state);
      selection = { type: null, id: null };
      try {
        await saveStateNow(); // 已有发布时仅落库航线，版本带来的禁飞区变化留作草稿
      } catch (e) {
        if (e instanceof WorkspaceCoordError) {
          renderPanel(); fitView(); runAnalyze();
          toast("启动协调未完成：版本已载入但未保存，请先点侧栏“重试”取回最新工作区");
          return;
        }
        if (e instanceof ReleaseUnavailableError) {
          renderPanel(); fitView(); runAnalyze();
          toast("发布状态尚未确认或读取失败：版本已载入但未保存，请刷新页面后重试");
          return;
        }
        if (e instanceof RouteConflictError) {
          renderPanel(); fitView(); runAnalyze();
          toast("载入的航线与他页已保存航线冲突，已暂停自动保存，请选择加载或确认提交");
          return;
        }
        toast("版本已载入，但自动保存失败：" + (e.message || "冲突"));
      }
      renderPanel(); fitView(); runAnalyze();
      toast(currentRelease && nfzDraftDirty()
        ? `已载入版本 ${v.name}（禁飞区变化已存为草稿）` : "已载入版本 " + v.name);
    };
    item.querySelector('[data-act="del"]').onclick = async () => {
      if (!confirm(`删除版本「${v.name}」？`)) return;
      await api(`/api/versions/${v.id}`, { method: "DELETE" });
      loadVersions();
    };
    box.appendChild(item);
  }
}
$("#btnSaveVer").onclick = async () => {
  const name = $("#verName").value.trim();
  const r = await api("/api/versions", { method: "POST", body: JSON.stringify({ name, state }) });
  if (r.ok) { toast("已保存：" + r.name); $("#verName").value = ""; loadVersions(); }
};

// ----------------------------------------------------------- 启动
(async function init() {
  setMode("select");
  await loadState();
  renderPanel();
  fitView();
  // 发布状态未返回期间页面已可编辑，但所有工作区写请求都经 releaseReady
  // 门控等待，先在发布卡片处显示“确认中”，绝不显示成“暂无发布”
  renderRelease();
  loadVersions();
  loadRelease(); // 不阻塞渲染：其返回前自动保存/核准/发布一律等待，不误写工作区
  loadVoyages();
  runAnalyze();
})();
