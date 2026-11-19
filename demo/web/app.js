/* Agent Quality Loop demo UI: four views, no build step, no external requests. */

const appEl = document.getElementById("app");

const state = {
  view: "turns",
  overview: null,
  turns: [],
  batches: [],
  selected: new Set(),
  batchId: null,
  detail: null,
  events: [],
  lastEventId: 0,
  report: null,
  drawerTurn: null,
  evidenceTurnId: null,
  evidenceTurn: null,
};

let pollTimer = null;

const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
  );

const fmtTime = (iso) => {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("zh-CN", { hour12: false });
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  let body = {};
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { detail: text };
  }
  if (!response.ok) {
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return body;
}

function toast(message, tone = "info") {
  const el = document.createElement("div");
  el.className = `toast ${tone === "error" ? "error" : ""}`;
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2800);
}

const STATE_META = {
  frozen: ["gray", "已冻结"],
  running: ["blue", "执行中"],
  converged: ["green", "converged"],
  stalled: ["amber", "stalled"],
  failed: ["red", "failed"],
  stopped: ["gray", "stopped"],
  error: ["red", "error"],
  queued: ["blue", "待处理"],
  skipped: ["gray", "已跳过"],
  passed: ["green", "通过"],
  passed_incidentally: ["green", "顺手修好"],
  budget_exhausted: ["amber", "预算耗尽"],
  rejected: ["red", "候选被拒"],
};

const EVENT_TONE = {
  "batch.created": "gray",
  "batch.frozen": "blue",
  "batch.started": "blue",
  "batch.regression": "purple",
  "batch.regression_done": "purple",
  "batch.error": "red",
  "batch.stopped": "amber",
  "item.skipped": "amber",
  "item.proposal_submitted": "blue",
  "item.candidate": "blue",
  "item.candidate_rejected": "red",
  "item.replay_trial": "blue",
  "item.accepted": "green",
  "item.rejected": "red",
  "item.retested": "blue",
  "item.passed_incidentally": "green",
  "item.budget_exhausted": "amber",
};

const SOURCE_LABEL = {
  persona: "虚拟人格",
  external_batch: "外部批测",
  trace_import: "会话导入",
};

const STATUS_TONE = {
  converged: "green",
  stalled: "amber",
  failed: "red",
  error: "red",
  stopped: "gray",
};

const REPORT_HEADLINE = {
  converged: "所有可回放轮次通过回归",
  stalled: "预算耗尽，仍有条目没有结论",
  failed: "回归存在明确失败",
  stopped: "人工停止，批次不算通过",
  error: "执行异常，没有结论",
};

function stateBadge(status) {
  const [tone, label] = STATE_META[status] || ["gray", status || "unknown"];
  return `<span class="badge ${tone}">${esc(label)}</span>`;
}

function eventTone(event) {
  if (event.kind === "batch.finished") {
    const status = event.data?.status;
    if (status === "converged") return "green";
    if (status === "failed" || status === "error") return "red";
    return "amber";
  }
  return EVENT_TONE[event.kind] || "gray";
}

/* ---------------------------------------------------------------- boot */

async function boot() {
  try {
    await bootstrap();
    await route();
  } catch (error) {
    appEl.innerHTML = `<div class="main"><div class="panel"><div class="empty"><h3>后端没有响应</h3><div>${esc(
      error.message,
    )}</div><div class="small" style="margin-top:10px">确认 <span class="mono">python serve.py</span> 仍在运行。</div></div></div></div>`;
  }
}

async function bootstrap() {
  const [overview, turns, batches] = await Promise.all([
    api("/api/overview"),
    api("/api/failed-turns"),
    api("/api/batches"),
  ]);
  state.overview = overview;
  state.turns = turns;
  state.batches = batches;
  state.selected = new Set(turns.map((turn) => turn.id));
  if (!state.batchId && batches.length) state.batchId = batches[0].id;
  if (!state.evidenceTurnId && turns.length) state.evidenceTurnId = turns[0].id;
}

window.addEventListener("hashchange", () => {
  route().catch((error) => toast(error.message, "error"));
});

function currentView() {
  const view = location.hash.replace(/^#\/?/, "");
  return ["turns", "console", "evidence", "report"].includes(view) ? view : "turns";
}

async function route() {
  state.view = currentView();
  if (!state.overview) await bootstrap();
  if (["console", "evidence", "report"].includes(state.view)) ensureBatch();
  if (state.view === "console") await loadBatch(true);
  if (state.view === "evidence") {
    await loadBatch(true);
    await ensureEvidence();
  }
  if (state.view === "report") await loadReport();
  render();
  ensurePolling();
}

function ensureBatch() {
  if (!state.batchId && state.batches.length) {
    state.batchId = state.batches[0].id;
  }
}

function selectBatch(id) {
  if (state.batchId === id) return;
  state.batchId = id;
  state.detail = null;
  state.events = [];
  state.lastEventId = 0;
  state.report = null;
}

async function loadBatch(reset = false) {
  if (!state.batchId) {
    state.detail = null;
    return;
  }
  if (reset) {
    state.events = [];
    state.lastEventId = 0;
  }
  state.detail = await api(`/api/batches/${state.batchId}`);
  const fresh = await api(`/api/batches/${state.batchId}/events?after_id=${state.lastEventId}`);
  if (fresh.length) {
    state.events = state.events.concat(fresh);
    state.lastEventId = fresh[fresh.length - 1].id;
  }
  state.batches = await api("/api/batches");
  state.overview = await api("/api/overview");
}

async function loadReport() {
  if (!state.batchId) {
    state.report = null;
    return;
  }
  try {
    state.report = await api(`/api/batches/${state.batchId}/report`);
  } catch {
    state.report = null;
  }
}

async function ensureEvidence() {
  if (!state.evidenceTurnId) return;
  try {
    state.evidenceTurn = await api(`/api/failed-turns/${state.evidenceTurnId}`);
  } catch {
    state.evidenceTurn = null;
  }
}

/* --------------------------------------------------------------- render */

function render() {
  document.querySelectorAll(".drawer, .drawer-backdrop").forEach((node) => node.remove());
  appEl.innerHTML = shell(viewHtml());
  bind();
  if (state.drawerTurn) renderDrawer();
  if (state.view === "console") scrollTimeline();
}

function shell(content) {
  const counts = state.overview?.counts || {};
  const link = (view, label, count) =>
    `<a href="#/${view}" class="${state.view === view ? "active" : ""}">${label}${
      count !== undefined ? `<span class="count">${count}</span>` : ""
    }</a>`;
  return `<div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="logo">◍</div>
        <div><strong>Quality Loop</strong><small>agent repair demo</small></div>
      </div>
      <nav class="nav">
        ${link("turns", "失败轮次", counts.failed_turns ?? 0)}
        ${link("console", "修复控制台")}
        ${link("evidence", "证据与资产")}
        ${link("report", "批次报告")}
      </nav>
      <div class="side-foot"><span class="dot"></span> 离线运行 · 虚构数据</div>
    </aside>
    <main class="main">${content}</main>
  </div>`;
}

function viewHtml() {
  if (state.view === "console") return consoleView();
  if (state.view === "evidence") return evidenceView();
  if (state.view === "report") return reportView();
  return turnsView();
}

function scrollTimeline() {
  const timeline = document.getElementById("timeline");
  if (timeline) timeline.scrollTop = timeline.scrollHeight;
}

function emptyState(title, text) {
  return `<div class="panel"><div class="empty"><h3>${esc(title)}</h3><div>${esc(
    text,
  )}</div></div></div>`;
}

function statCard(value, label) {
  return `<div class="stat"><div class="n">${value ?? 0}</div><div class="l">${esc(label)}</div></div>`;
}

function batchSelect() {
  if (!state.batches.length) return "";
  return `<select id="batch-select">${state.batches
    .map(
      (batch) =>
        `<option value="${esc(batch.id)}" ${batch.id === state.batchId ? "selected" : ""}>${esc(
          batch.cycle,
        )} · ${esc(batch.status)}</option>`,
    )
    .join("")}</select>`;
}

/* ---------------------------------------------------------------- turns */

function turnsView() {
  const counts = state.overview?.counts || {};
  return `
  <div class="page-head">
    <div>
      <h1>失败轮次</h1>
      <p class="sub">从观测平台导入的失败轮次。是否可回放由冻结阶段判定：没有回放主键的轮次仍会进入批次，但会被跳过并写进报告。</p>
    </div>
    <div class="row">
      <button class="btn ghost" id="btn-import">从观测平台重新导入</button>
      <button class="btn ghost" id="btn-create">用所选建批次</button>
      <button class="btn primary" id="btn-demo">一键演示</button>
    </div>
  </div>
  <div class="cards">
    ${statCard(counts.cases, "会话")}
    ${statCard(counts.failed_turns, "失败轮次")}
    ${statCard(counts.replayable, "可回放")}
    ${statCard(counts.unreplayable, "不可回放 · 冻结时跳过")}
  </div>
  <div class="panel">
    <div class="panel-head">
      <span>${state.turns.length} 条失败轮次</span>
      <span class="small muted">已选 <span id="selected-count">${state.selected.size}</span> 条 · 点行查看证据</span>
    </div>
    <table>
      <thead><tr>
        <th style="width:38px"></th><th>用例</th><th>轮次</th><th>评分</th><th>失败原因</th><th>回放</th><th>来源</th>
      </tr></thead>
      <tbody>${state.turns.map(turnRow).join("") || `<tr><td colspan="7"><div class="empty">还没有导入数据。点右上角「重新导入」。</div></td></tr>`}</tbody>
    </table>
  </div>`;
}

function turnRow(turn) {
  return `<tr class="clickable" data-turn="${esc(turn.id)}">
    <td><input type="checkbox" data-check="${esc(turn.id)}" ${
      state.selected.has(turn.id) ? "checked" : ""
    }></td>
    <td><strong>${esc(turn.case_title)}</strong><div class="small muted mono">${esc(turn.session_id)}</div></td>
    <td>第 ${turn.turn_index + 1} 轮</td>
    <td><span class="badge red">${esc(turn.score_name)} = ${esc(turn.score_value)}</span></td>
    <td><div class="clamp small">${esc(turn.reason)}</div></td>
    <td>${
      turn.replayable
        ? `<span class="badge green">可回放</span><div class="small mono muted">${esc(turn.replay_key)}</div>`
        : `<span class="badge gray">不可回放</span><div class="small muted">无主键</div>`
    }</td>
    <td class="small muted">${esc(SOURCE_LABEL[turn.case_source] || turn.case_source)}</td>
  </tr>`;
}

/* -------------------------------------------------------------- console */

function consoleView() {
  if (!state.batches.length) {
    return emptyState("还没有批次", "去「失败轮次」点「一键演示」，会创建并启动一个批次。");
  }
  const detail = state.detail;
  if (!detail) return emptyState("正在加载批次", "");
  const batch = detail.batch;
  const counts = batch.counts || {};
  return `
  <div class="page-head">
    <div>
      <h1>修复控制台</h1>
      <p class="sub">逐条提案、三次回放、接受后重测剩余，最后整集回归。只有 converged 算通过。</p>
    </div>
    <div class="row">
      ${batchSelect()}
      ${batch.status === "frozen" ? `<button class="btn primary" id="btn-start">启动批次</button>` : ""}
      ${batch.status === "running" ? `<button class="btn" id="btn-stop">停止</button>` : ""}
      <button class="btn ghost" id="btn-refresh">刷新</button>
    </div>
  </div>
  <div class="cards">
    <div class="stat"><div class="n" style="font-size:16px;padding:4px 0">${stateBadge(batch.status)}</div><div class="l">${esc(batch.stop_reason || "尚未结束")}</div></div>
    <div class="stat"><div class="n">${counts.passed ?? 0}</div><div class="l">提案通过</div></div>
    <div class="stat"><div class="n">${counts.passed_incidentally ?? 0}</div><div class="l">顺手修好</div></div>
    <div class="stat"><div class="n">seq ${batch.asset?.seq ?? "-"}</div><div class="l">当前资产 · ${esc(batch.asset?.hash || "")}</div></div>
  </div>
  <div class="console-grid">
    <div class="panel">
      <div class="panel-head"><span>失败轮次账本</span><span class="small muted">${detail.items.length} 条 · ${counts.skipped ?? 0} 条跳过</span></div>
      <div class="panel-body">${detail.items.map(itemCard).join("") || '<div class="empty small">无条目</div>'}</div>
    </div>
    <div class="panel">
      <div class="panel-head"><span>执行时间线</span><span class="small muted">${state.events.length} 条事件</span></div>
      <div class="timeline" id="timeline">${state.events.map(eventRow).join("") || '<div class="empty small">还没有事件。</div>'}</div>
    </div>
  </div>`;
}

function itemCard(item) {
  const attemptLabel =
    item.state === "passed_incidentally"
      ? '<span class="trial">未单独提案 · 重测通过</span>'
      : item.state === "skipped"
        ? '<span class="trial">冻结时跳过</span>'
        : `<span class="trial">尝试 ${item.attempts} 次</span>`;
  return `<div class="item-card">
    <header>
      <div>
        <strong>${esc(item.case_title)}</strong>
        <div class="small muted mono">${esc(item.session_id)} · 第 ${item.turn_index + 1} 轮</div>
      </div>
      ${stateBadge(item.state)}
    </header>
    <div class="small muted" style="margin-top:8px">${esc(item.reason)}</div>
    <div class="attempts">
      ${attemptLabel}
      ${(item.verification || [])
        .map(
          (trial) =>
            `<span class="trial ${trial.passed ? "pass" : "fail"}">回放 ${trial.trial} ${
              trial.passed ? "✓" : "✗"
            }</span>`,
        )
        .join("")}
      ${item.changes?.length ? `<span class="trial">改动 ${esc(item.changes.join(", "))}</span>` : ""}
      ${item.replayable ? "" : '<span class="trial">不可回放</span>'}
    </div>
    ${
      item.error_history?.length
        ? `<details style="margin-top:10px"><summary class="small">错误历史（${item.error_history.length}）</summary>${item.error_history
            .map((entry) => `<div class="small muted" style="margin-top:4px">· ${esc(entry)}</div>`)
            .join("")}</details>`
        : ""
    }
  </div>`;
}

function eventRow(event) {
  return `<div class="event ${eventTone(event)}">
    <div class="t">${fmtTime(event.ts)}</div>
    <div class="d"></div>
    <div class="msg">${esc(event.message)}<span class="kind">${esc(event.kind)}</span></div>
  </div>`;
}

/* ------------------------------------------------------------- evidence */

function evidenceView() {
  if (!state.turns.length) {
    return emptyState("还没有失败轮次", "先回到「失败轮次」导入数据。");
  }
  const turn = state.evidenceTurn;
  return `
  <div class="page-head">
    <div>
      <h1>证据与资产</h1>
      <p class="sub">失败轮次的原始对话、评分证据和冻结的回放主键；下方是批次资产如何一步步演进。</p>
    </div>
    <div class="row">${batchSelect()}</div>
  </div>
  <div class="console-grid">
    <div class="panel">
      <div class="panel-head"><span>失败轮次</span><span class="small muted">${state.turns.length} 条</span></div>
      <div class="list">${state.turns
        .map(
          (item) => `<button class="list-item ${item.id === state.evidenceTurnId ? "active" : ""}" data-case="${esc(
            item.id,
          )}"><strong>${esc(item.case_title)}</strong><div class="small muted">第 ${
            item.turn_index + 1
          } 轮 · ${item.replayable ? "可回放" : "不可回放"}</div></button>`,
        )
        .join("")}</div>
    </div>
    <div class="panel">
      <div class="panel-head"><span>${turn ? esc(turn.case_title) : "选择一条失败轮次"}</span>${
        turn ? (turn.replayable ? '<span class="badge green">可回放</span>' : '<span class="badge gray">不可回放</span>') : ""
      }</div>
      <div class="panel-body">${
        turn ? evidenceBody(turn) : '<div class="empty">从左侧选择一条失败轮次。</div>'
      }</div>
    </div>
  </div>
  <div class="section panel">
    <div class="panel-head"><span>资产演进</span><span class="small muted">${esc(
      state.batchId || "未选择批次",
    )}</span></div>
    <div class="panel-body" id="assets">${assetsHtml()}</div>
  </div>`;
}

function evidenceBody(turn) {
  return `
  <div class="kv" style="margin-bottom:16px">
    <div class="muted">评分</div><div><span class="badge red">${esc(turn.score_name)} = ${esc(turn.score_value)}</span></div>
    <div class="muted">理由</div><div>${esc(turn.reason)}</div>
    <div class="muted">回放主键</div><div class="mono">${esc(turn.replay_key || "无（永久不可自动验证）")}</div>
    <div class="muted">指纹</div><div class="mono">${esc(turn.fingerprint)}</div>
  </div>
  <div>${conversation(turn.case_turns, turn.turn_index)}</div>`;
}

function conversation(turns, flagIndex) {
  return (turns || [])
    .map((turn) => {
      const tools = (turn.tools || [])
        .map(
          (tool) =>
            `<div class="mono small">↳ ${esc(tool.name)}(${esc(JSON.stringify(tool.args))}) ${
              tool.ok ? "ok" : "fail"
            }</div>`,
        )
        .join("");
      const flagged = turn.index === flagIndex;
      return `<div class="turn user"><div class="role">用户 · 第 ${turn.index + 1} 轮</div>${esc(
        turn.user,
      )}</div>
      <div class="turn ${flagged ? "flag" : ""}"><div class="role">助手${
        flagged ? " · 失败轮次" : ""
      }</div>${esc(turn.assistant)}${tools ? `<div class="tools">${tools}</div>` : ""}</div>`;
    })
    .join("");
}

function assetsHtml() {
  const assets = state.detail?.assets || [];
  if (!assets.length) return '<div class="empty small">该批次还没有资产记录。</div>';
  return assets
    .map(
      (asset) => `<div class="asset">
      <div class="row">
        <span class="badge blue">seq ${asset.seq}</span>
        <span class="mono small muted">${esc(asset.content_hash)}</span>
        <span class="badge gray">${esc(asset.origin)}</span>
        <span class="spacer"></span>
        <span class="small muted">${fmtTime(asset.created_at)}</span>
      </div>
      <details style="margin-top:8px"><summary class="small">查看提示词</summary><pre class="prompt" style="margin-top:8px">${esc(
        asset.content,
      )}</pre></details>
    </div>`,
    )
    .join("");
}

/* --------------------------------------------------------------- report */

function reportView() {
  if (!state.batches.length) {
    return emptyState("还没有批次", "先创建一个修复批次。");
  }
  const report = state.report;
  return `
  <div class="page-head">
    <div>
      <h1>批次报告</h1>
      <p class="sub">终态、逐条证据、回归结果，以及这次没有验证什么。报告只对已结束的批次生成。</p>
    </div>
    <div class="row">${batchSelect()}${
      report ? '<button class="btn ghost" id="btn-download">下载 JSON</button>' : ""
    }</div>
  </div>
  ${
    report
      ? reportBody(report)
      : emptyState("这个批次还没有报告", "批次结束后报告自动生成；运行中的进度去「修复控制台」看。")
  }`;
}

function itemSummary(item) {
  if (item.state === "passed") {
    const rejected = item.attempts > 1 ? `；前 ${item.attempts - 1} 次被拒` : "";
    return `${item.verification?.length || 3}/${item.verification?.length || 3} 回放通过，接受候选${rejected}`;
  }
  if (item.state === "passed_incidentally") {
    return "未单独提案；由其他条目的新资产重测通过";
  }
  if (item.state === "skipped") {
    return item.reason || "无回放主键，冻结时跳过";
  }
  return (item.error_history || []).slice(-1)[0] || item.reason;
}

function reportBody(report) {
  const counts = report.counts || {};
  const checks = report.regression?.checks || [];
  const passed = checks.filter((check) => check.passed).length;
  return `
  <div class="report-banner ${esc(report.status)}">
    <span class="badge ${STATUS_TONE[report.status] || "gray"}">${esc(report.status)}</span>
    <h2>${esc(REPORT_HEADLINE[report.status] || report.status)}</h2>
    <p>${
      report.status === "converged" ? "" : `${esc(report.stop_reason)} · `
    }批次 ${esc(report.batch_id)} · 结束于 ${fmtTime(report.ended_at)}</p>
  </div>
  <div class="cards">
    <div class="stat"><div class="n">seq ${report.asset.start_seq} → ${report.asset.final_seq}</div><div class="l">资产演进 · ${esc(
      report.asset.final_hash,
    )}</div></div>
    <div class="stat"><div class="n">${counts.passed ?? 0} + ${
      counts.passed_incidentally ?? 0
    }</div><div class="l">提案通过 + 顺手修好</div></div>
    <div class="stat"><div class="n">${counts.skipped ?? 0}</div><div class="l">不可回放跳过</div></div>
    <div class="stat"><div class="n">${passed}/${checks.length}</div><div class="l">整集回归</div></div>
  </div>
  <div class="section panel">
    <div class="panel-head"><span>逐条证据</span><span class="small muted">预算 ${report.budget.max_attempts} 次 / 条，验证 ${report.budget.verify_trials} 次回放</span></div>
    <table>
      <thead><tr><th>用例</th><th>状态</th><th>尝试</th><th>回放</th><th>说明</th></tr></thead>
      <tbody>${report.items
        .map(
          (item) => `<tr>
        <td><strong>${esc(item.case_title)}</strong><div class="small muted mono">${esc(
          item.session_id,
        )} · 第 ${item.turn_index + 1} 轮</div></td>
        <td>${stateBadge(item.state)}</td>
        <td>${item.attempts || "—"}</td>
        <td>${
          (item.verification || [])
            .map(
              (trial) =>
                `<span class="trial ${trial.passed ? "pass" : "fail"}">${trial.trial} ${
                  trial.passed ? "✓" : "✗"
                }</span>`,
            )
            .join(" ") || '<span class="muted small">未执行</span>'
        }</td>
        <td class="small muted">${esc(itemSummary(item))}</td>
      </tr>`,
        )
        .join("")}</tbody>
    </table>
  </div>
  <div class="console-grid section">
    <div class="panel">
      <div class="panel-head"><span>整集回归</span><span class="small muted">${passed}/${checks.length} 通过</span></div>
      <div class="panel-body">
        ${
          checks
            .map(
              (check) => `<div class="row" style="padding:6px 0;border-bottom:1px solid var(--line)">
            <span class="badge ${check.passed ? "green" : "red"}">${check.passed ? "通过" : "失败"}</span>
            <span class="small">${esc(check.case_title)} · 第 ${check.turn_index + 1} 轮</span>
            <span class="spacer"></span><span class="mono small muted">${esc(check.request_id)}</span>
          </div>`,
            )
            .join("") || '<div class="empty small">没有回归记录。</div>'
        }
        ${
          (report.regression?.excluded || []).length
            ? `<div style="margin-top:14px"><div class="small muted" style="margin-bottom:6px">排除（不计入通过率）</div>${report.regression.excluded
                .map(
                  (item) => `<div class="row" style="padding:5px 0">
              <span class="badge gray">排除</span>
              <span class="small">${esc(item.case_title)} · 第 ${item.turn_index + 1} 轮</span>
              <span class="spacer"></span><span class="small muted">${esc(item.reason)}</span>
            </div>`,
                )
                .join("")}</div>`
            : ""
        }
      </div>
    </div>
    <div class="panel">
      <div class="panel-head"><span>这份报告没有验证什么</span><span class="small muted">局限声明</span></div>
      <div class="panel-body">
        <ul style="margin:0;padding-left:18px" class="small">${report.limitations
          .map((line) => `<li style="margin-bottom:6px">${esc(line)}</li>`)
          .join("")}</ul>
        <details style="margin-top:14px"><summary class="small">最终提示词</summary><pre class="prompt" style="margin-top:8px">${esc(
          report.asset.final_prompt,
        )}</pre></details>
      </div>
    </div>
  </div>`;
}

/* --------------------------------------------------------------- drawer */

function renderDrawer() {
  const turn = state.drawerTurn;
  const backdrop = document.createElement("div");
  backdrop.className = "drawer-backdrop";
  const drawer = document.createElement("aside");
  drawer.className = "drawer";
  drawer.innerHTML = `<button class="icon-btn" id="drawer-close">×</button>
    <h2>${esc(turn.case_title)}</h2>
    <div class="small muted mono">${esc(turn.session_id)} · 第 ${turn.turn_index + 1} 轮</div>
    <div style="margin:14px 0">${stateBadge("failed")} ${
      turn.replayable ? '<span class="badge green">可回放</span>' : '<span class="badge gray">不可回放</span>'
    }</div>
    <div class="kv" style="margin-bottom:16px">
      <div class="muted">评分</div><div><span class="badge red">${esc(turn.score_name)} = ${esc(
        turn.score_value,
      )}</span></div>
      <div class="muted">理由</div><div>${esc(turn.reason)}</div>
      <div class="muted">回放主键</div><div class="mono">${esc(turn.replay_key || "无")}</div>
      <div class="muted">指纹</div><div class="mono">${esc(turn.fingerprint)}</div>
      <div class="muted">来源</div><div>${esc(turn.case_source)}</div>
    </div>
    ${conversation(turn.case_turns, turn.turn_index)}`;
  document.body.appendChild(backdrop);
  document.body.appendChild(drawer);
  const close = () => {
    state.drawerTurn = null;
    render();
  };
  backdrop.addEventListener("click", close);
  drawer.querySelector("#drawer-close").addEventListener("click", close);
}

/* ------------------------------------------------------------- bindings */

function bind() {
  document.getElementById("batch-select")?.addEventListener("change", async (event) => {
    selectBatch(event.target.value);
    try {
      if (state.view === "console" || state.view === "evidence") await loadBatch(true);
      if (state.view === "report") await loadReport();
      render();
      ensurePolling();
    } catch (error) {
      toast(error.message, "error");
    }
  });

  if (state.view === "turns") bindTurns();
  if (state.view === "console") bindConsole();
  if (state.view === "evidence") bindEvidence();
  if (state.view === "report") bindReport();
}

function bindTurns() {
  document.querySelectorAll("[data-check]").forEach((checkbox) => {
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => {
      const id = checkbox.dataset.check;
      if (checkbox.checked) state.selected.add(id);
      else state.selected.delete(id);
      const counter = document.getElementById("selected-count");
      if (counter) counter.textContent = state.selected.size;
    });
  });

  document.querySelectorAll("[data-turn]").forEach((row) => {
    row.addEventListener("click", async () => {
      try {
        state.drawerTurn = await api(`/api/failed-turns/${row.dataset.turn}`);
        render();
      } catch (error) {
        toast(error.message, "error");
      }
    });
  });

  document.getElementById("btn-import")?.addEventListener("click", async () => {
    try {
      const result = await api("/api/import", { method: "POST" });
      state.overview = await api("/api/overview");
      state.turns = await api("/api/failed-turns");
      state.batches = await api("/api/batches");
      state.selected = new Set(state.turns.map((turn) => turn.id));
      render();
      toast(`导入完成：新增 ${result.cases_new} 个会话、${result.failed_turns_new} 条失败轮次`);
    } catch (error) {
      toast(error.message, "error");
    }
  });

  document.getElementById("btn-create")?.addEventListener("click", () => {
    createBatch(false).catch((error) => toast(error.message, "error"));
  });
  document.getElementById("btn-demo")?.addEventListener("click", () => {
    createBatch(true).catch((error) => toast(error.message, "error"));
  });
}

async function createBatch(start) {
  const ids = start ? state.turns.map((turn) => turn.id) : Array.from(state.selected);
  if (!ids.length) {
    toast("先选择至少一条失败轮次", "error");
    return;
  }
  const stamp = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  const cycle = `${start ? "演示迭代" : "手工批次"} · ${stamp}`;
  const created = await api("/api/batches", {
    method: "POST",
    body: JSON.stringify({ cycle, failed_turn_ids: ids }),
  });
  if (start) {
    await api(`/api/batches/${created.batch_id}/start`, { method: "POST" });
  }
  selectBatch(created.batch_id);
  state.batches = await api("/api/batches");
  if (location.hash === "#/console") {
    await route();
  } else {
    location.hash = "#/console";
  }
}

function bindConsole() {
  document.getElementById("btn-start")?.addEventListener("click", async () => {
    try {
      await api(`/api/batches/${state.batchId}/start`, { method: "POST" });
      toast("批次已启动");
      await loadBatch(false);
      render();
      ensurePolling();
    } catch (error) {
      toast(error.message, "error");
    }
  });

  document.getElementById("btn-stop")?.addEventListener("click", async () => {
    try {
      await api(`/api/batches/${state.batchId}/stop`, { method: "POST" });
      toast("已请求停止，会在当前步骤后停下");
    } catch (error) {
      toast(error.message, "error");
    }
  });

  document.getElementById("btn-refresh")?.addEventListener("click", async () => {
    try {
      await loadBatch(false);
      render();
      ensurePolling();
    } catch (error) {
      toast(error.message, "error");
    }
  });
}

function bindEvidence() {
  document.querySelectorAll("[data-case]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.evidenceTurnId = button.dataset.case;
      await ensureEvidence();
      render();
    });
  });
}

function bindReport() {
  document.getElementById("btn-download")?.addEventListener("click", () => {
    if (!state.report) return;
    const blob = new Blob([JSON.stringify(state.report, null, 2)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `report-${state.report.batch_id}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
  });
}

/* -------------------------------------------------------------- polling */

function ensurePolling() {
  const running =
    state.view === "console" && state.detail && state.detail.batch.status === "running";
  if (running && !pollTimer) {
    pollTimer = setInterval(refreshConsole, 1000);
  }
  if (!running && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function refreshConsole() {
  if (state.view !== "console" || !state.batchId) return;
  try {
    await loadBatch(false);
    render();
    ensurePolling();
  } catch {
    /* keep the last good view; the next tick retries */
  }
}

boot();
