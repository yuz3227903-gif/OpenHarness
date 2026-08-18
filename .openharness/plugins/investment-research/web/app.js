const AGENT_IDS = [
  "planner",
  "fundamental",
  "industry_competition",
  "market_catalyst",
  "risk",
  "reviewer_arbiter",
  "report_writer",
];

const LABELS = {
  succeeded: "已完成",
  failed: "失败",
  invalid_output: "格式异常",
  blocked: "受阻",
  running: "运行中",
  waiting: "等待",
};

const $ = (selector) => document.querySelector(selector);
let reportRunId = null;
let pollTimer = null;

function number(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
}

function setAgentState(agentId, status, details = {}) {
  const card = document.querySelector(`[data-agent="${agentId}"]`);
  if (!card) return;
  card.classList.remove("is-running", "is-done", "is-warning", "is-error");
  if (status === "running") card.classList.add("is-running");
  else if (status === "succeeded" && details.pass !== false) card.classList.add("is-done");
  else if (status === "succeeded" || status === "invalid_output" || status === "blocked") card.classList.add("is-warning");
  else if (status === "failed") card.classList.add("is-error");
  const state = card.querySelector(".agent-state");
  const suffix = details.tool_count ? ` · ${details.tool_count} tools` : "";
  const label = details.output_status === "fallback"
    ? "本地兜底交付"
    : (LABELS[status] || status || "等待");
  state.textContent = `${label}${suffix}`;
}

function inferRunningAgent(stage = "") {
  if (stage.includes("planner")) return ["planner"];
  if (stage.includes("parallel")) return ["fundamental", "industry_competition", "market_catalyst"];
  if (stage.includes("risk")) return ["risk"];
  if (stage.includes("reviewer") || stage.includes("supplement")) return ["reviewer_arbiter"];
  if (stage.includes("report_writer")) return ["report_writer"];
  return [];
}

async function loadReport(runId, available) {
  if (!available) {
    $("#report-meta").textContent = "尚未生成报告。";
    $("#report-content").textContent = "运行完成后，报告会在这里显示。";
    $("#download-report").classList.add("is-disabled");
    return;
  }
  $("#download-report").classList.remove("is-disabled");
  if (reportRunId === runId) return;
  try {
    const response = await fetch(`/api/research/report?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    $("#report-content").textContent = await response.text();
    reportRunId = runId;
  } catch (error) {
    $("#report-content").textContent = `报告读取失败：${error.message}`;
  }
}

function renderSnapshot(snapshot) {
  const summary = snapshot.summary || {};
  const progress = snapshot.progress || {};
  const agents = snapshot.agents || {};
  const runId = summary.run_id || progress.run_id || "—";
  const running = Boolean(snapshot.running);

  $("#run-id").textContent = runId;
  $("#pipeline-status").textContent = running
    ? "运行中"
    : summary.pipeline_status || summary.terminal_state || "尚未运行";
  $("#report-quality").textContent = summary.report_quality || "—";
  $("#live-message").textContent = progress.message || snapshot.error || "已读取最近一次运行记录。";

  AGENT_IDS.forEach((agentId) => setAgentState(agentId, "waiting"));
  Object.entries(agents).forEach(([agentId, details]) => {
    setAgentState(agentId, details.execution_status, details);
  });
  if (running) {
    inferRunningAgent(String(progress.stage || "")).forEach((agentId) => {
      if (!agents[agentId]) setAgentState(agentId, "running");
    });
  }

  const totalTokens = Object.values(agents).reduce(
    (sum, item) => sum + Number(item.total_tokens || 0),
    0,
  );
  $("#total-tokens").textContent = totalTokens ? number(totalTokens) : "—";

  const button = $("#run-button");
  button.disabled = running;
  $("#run-button-label").textContent = running ? "完整研究运行中" : "启动新的完整研究";
  $("#report-meta").textContent = snapshot.report_available
    ? `${runId} · ${summary.report_quality || "unknown"} · ${number(snapshot.report_bytes)} bytes`
    : "尚未生成报告。";
  loadReport(runId, snapshot.report_available);
}

async function pollStatus() {
  try {
    const response = await fetch(`/api/research/status?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderSnapshot(await response.json());
  } catch (error) {
    $("#live-message").textContent = `无法读取运行状态：${error.message}`;
  }
}

async function loadHealth() {
  const container = $("#runtime-state");
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    const health = await response.json();
    const ready = response.ok && health.ready_for_real_run;
    container.classList.toggle("is-ready", ready);
    container.classList.toggle("is-error", !ready);
    $("#runtime-text").textContent = ready
      ? `${health.model} · 环境就绪`
      : "运行环境未就绪";
  } catch (_) {
    container.classList.add("is-error");
    $("#runtime-text").textContent = "无法连接本地服务";
  }
}

async function startResearch() {
  const button = $("#run-button");
  button.disabled = true;
  $("#run-button-label").textContent = "正在启动";
  reportRunId = null;
  try {
    const response = await fetch("/api/research/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    $("#live-message").textContent = payload.message;
    await pollStatus();
  } catch (error) {
    $("#live-message").textContent = `启动失败：${error.message}`;
    button.disabled = false;
    $("#run-button-label").textContent = "重新启动完整研究";
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  $("#run-button").addEventListener("click", startResearch);
  await Promise.all([loadHealth(), pollStatus()]);
  pollTimer = window.setInterval(pollStatus, 3000);
});

window.addEventListener("beforeunload", () => window.clearInterval(pollTimer));
