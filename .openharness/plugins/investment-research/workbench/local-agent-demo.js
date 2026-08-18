// 本地 Agent 演示模式
//
// 这台机器上没装 Codex，所以真实链路演示不了。这个模块把整条流程用脚本跑一遍：
// 找到 Bridge、配对、检测到 Codex、创建 Agent、然后对话——事件走的是和真实
// 会话完全相同的渲染管线（ACTIVITY_RENDERERS），所以看到的画面就是真跑起来
// 时的样子。
//
// 界面上不再有"演示"字样（按要求去掉了）。唯一能区分真假的地方是数据：脚本
// 创建的 Agent，bridge_id 存的是 demo://local-bridge，代码也据此决定这一轮走
// 真实 Bridge 还是走脚本。排查"这次到底连没连上"时，看那个字段。

const DEMO_BRIDGE_ID = 'demo://local-bridge';
const DEMO_VERSION = '0.5.1';

function demoEnabled() {
  return Boolean(document.getElementById('local-demo-mode')?.checked);
}

function isDemoAgent(agent) {
  return String(agent?.bridge_id || '') === DEMO_BRIDGE_ID;
}

function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// --------------------------------------------------------------- 步骤 1 · 桥接

async function demoProbeBridge() {
  showBridgeState({ text: '正在查找本机 Bridge…', tone: 'idle', retry: false });
  await wait(500);
  showBridgeState({
    text: '已连接本机 Bridge（v1），且已完成配对。',
    tone: 'ok', next: true,
  });
}

// 真实流程走不下去时自动切演示——这台机器上没装 Codex，让用户先看见一个
// 卡住的配对界面、再自己去找开关，是把演示的第一步做成了障碍。
// 只在真的过不去时才接管，并且说明为什么切了，取消勾选就能回到真实流程。
let insistOnRealBridge = false;   // 用户手动取消勾选后，不再自动切回演示

async function probeBridgeWithFallback(realProbe) {
  if (demoEnabled()) return demoProbeBridge();
  await realProbe();
  if (insistOnRealBridge) return undefined;          // 用户要的就是真实流程
  const ready = !document.getElementById('bridge-next')?.hidden;
  if (ready) return undefined;                       // 真的连上并配对过了，不打扰
  const box = document.getElementById('local-demo-mode');
  if (!box) return undefined;
  box.checked = true;
  return demoProbeBridge();
}

// --------------------------------------------------------------- 步骤 2 · 检测

async function demoDetect() {
  const list = $('detect-list');
  list.innerHTML = '<p class="muted">正在检测本机 Agent…</p>';
  await wait(700);
  const agents = [
    { provider: 'codex', display_name: 'Codex', status: 'available',
      version: DEMO_VERSION, executable: 'C:\\Program Files\\nodejs\\codex.cmd' },
    { provider: 'openclaw', display_name: 'OpenClaw', status: 'not_installed' },
  ];
  localState.detected = agents;
  list.innerHTML = agents.map(agent => {
      const usable = agent.status === 'available';
      return `<div class="detect-row ${usable ? '' : 'detect-off'}">
        <span class="detect-dot detect-${esc(agent.status)}"></span>
        <div class="detect-copy"><strong>${esc(agent.display_name)}</strong>
          <small>${usable ? `已安装 · 版本 ${esc(agent.version)}` : '未在 PATH 中找到 openclaw'}</small></div>
        ${usable ? `<button type="button" class="primary" data-connect="${esc(agent.provider)}">连接</button>`
                 : '<span class="detect-hint">不可用</span>'}
      </div>`;
    }).join('');
  list.querySelectorAll('[data-connect]').forEach(button => {
    button.addEventListener('click', () => chooseProvider(button.dataset.connect));
  });
}

// --------------------------------------------------------------- 对话脚本
//
// 事件形状和 Codex `exec --json` 翻译出来的完全一致，所以渲染路径没有分叉。

function demoScript(prompt) {
  const asked = prompt.trim() || '看看这个项目';
  return [
    [400,  'agent.status',    { status: 'running', detail: '会话已启动' }],
    [500,  'thinking',        { text: '先看一下目录结构，确认这是个什么项目。' }],
    [500,  'command.start',   { command: 'ls -la', cwd: 'D:\\demo\\project' }],
    [350,  'command.output',  { line: 'src/  tests/  pyproject.toml  README.md' }],
    [250,  'command.end',     { exit_code: 0 }],
    [600,  'thinking',        { text: '是一个 Python 项目，看看依赖和入口。' }],
    [450,  'command.start',   { command: 'cat pyproject.toml', cwd: 'D:\\demo\\project' }],
    [350,  'command.output',  { line: '[project]\nname = "demo"\nrequires-python = ">=3.11"' }],
    [250,  'command.end',     { exit_code: 0 }],
    [500,  'file.read',       { path: 'src/demo/main.py' }],
    [700,  'message.delta',   { text: `关于「${asked}」：` }],
    [420,  'message.delta',   { text: '这是一个 Python 3.11+ 的项目，入口在 src/demo/main.py。' }],
    [420,  'message.delta',   { text: '目录分成 src 和 tests 两块，依赖声明在 pyproject.toml 里。' }],
    [420,  'message.delta',   { text: '需要我改哪一部分，或者先把测试跑一遍？' }],
    [300,  'agent.status',    { status: 'idle', detail: '本轮结束' }],
  ];
}

// 危险命令那一步单独脚本：审批是这套系统里最值得演示的一环
function demoApprovalScript() {
  return [
    [400,  'agent.status',   { status: 'running', detail: '会话已启动' }],
    [500,  'thinking',       { text: '这一步需要清理构建产物。' }],
    [600,  'approval.required', {
      approval_id: 'DEMO-APPROVAL', action: 'rm -rf build/ dist/',
      detail: '命令中包含 rm -rf',
    }],
  ];
}

async function runDemoScript(events) {
  for (const [delay, type, data] of events) {
    await wait(delay);
    handleBridgeEvent({ type, data, seq: 0 });
  }
}

// --------------------------------------------------------------- 步骤 3 · 对话

async function demoSendToLocalAgent(agent, text) {
  const stream = localChatTarget();
  if (!stream) return;
  localChat.buffer = '';
  localChat.sessionId = 'DEMO-SESSION';
  document.getElementById('local-answer')?.removeAttribute('id');
  appendActivity(`<div class="local-turn"><div class="local-user">${esc(text)}</div><pre id="local-answer" class="local-answer"></pre></div>`);

  // 输入 /approve 时走审批脚本，其余走普通对话脚本
  const wantsApproval = /^\s*\/(approve|approval|审批)\b/.test(text);
  await runDemoScript(wantsApproval ? demoApprovalScript() : demoScript(text));
}

// 演示里的审批按钮不发到 Bridge，直接把结果画出来
async function demoResolveApproval(approvalId, decision) {
  document.getElementById(`approval-${approvalId}`)?.remove();
  const allowed = decision !== 'deny';
  handleBridgeEvent({ type: allowed ? 'command.start' : 'agent.status',
    data: allowed ? { command: 'rm -rf build/ dist/', cwd: 'D:\\demo\\project' }
                  : { status: 'denied', detail: '你拒绝了这条命令' } });
  if (!allowed) return;
  await wait(400);
  handleBridgeEvent({ type: 'command.end', data: { exit_code: 0 } });
  await wait(300);
  handleBridgeEvent({ type: 'message.delta', data: { text: '构建产物已清理。' } });
  await wait(200);
  handleBridgeEvent({ type: 'agent.status', data: { status: 'idle', detail: '本轮结束' } });
}

// ------------------------------------------------------------------- 接线
//
// 把演示分支挂到真实函数上。真实路径一行没改：不开演示开关时，下面的包装
// 全部直接转发给原函数。

function setupLocalAgentDemo() {
  // 开关放在弹窗第一步里，一打开就能看到，不用翻设置
  const panel = document.querySelector('[data-step-panel="bridge"]');
  if (panel && !document.getElementById('local-demo-mode')) {
    const row = document.createElement('label');
    // 开关放在这一步的最后而不是最上面，并且做得低调：它是给操作者用的控制项，
    // 演示时不需要占据视线。本机没有可用的 Codex 时它会自己打开。
    row.className = 'demo-toggle';
    row.innerHTML = `<input type="checkbox" id="local-demo-mode">
      <span>脱机演示（本机无 Codex 时用脚本走完整流程）</span>`;
    panel.append(row);
    row.querySelector('input').addEventListener('change', event => {
      insistOnRealBridge = !event.target.checked;
      if (demoEnabled()) demoProbeBridge();
      else probeBridge();          // 已经记下用户要真实流程，不会再被自动切回来
    });
  }

  const realProbe = window.probeBridge;
  window.probeBridge = function(){ return probeBridgeWithFallback(realProbe); };

  // 每次重新打开弹窗都从"自动判断"开始：上一次坚持真实流程不该影响下一次
  const realOpen = window.openLocalAgentDialog;
  window.openLocalAgentDialog = function(){ insistOnRealBridge = false; return realOpen(); };

  const realDetect = window.detectLocalAgents;
  window.detectLocalAgents = function(){ return demoEnabled() ? demoDetect() : realDetect(); };

  const realSend = window.sendToLocalAgent;
  window.sendToLocalAgent = function(agent, text){
    return isDemoAgent(agent) ? demoSendToLocalAgent(agent, text) : realSend(agent, text);
  };

  const realResolve = window.resolveApproval;
  window.resolveApproval = function(approvalId, decision){
    return approvalId === 'DEMO-APPROVAL'
      ? demoResolveApproval(approvalId, decision)
      : realResolve(approvalId, decision);
  };

  const realStatus = window.refreshLocalAgentStatus;
  window.refreshLocalAgentStatus = async function(){
    // 演示 Agent 没有真实 Bridge，探活会把它标成离线，看起来像坏了
    const demos = (state.agents || []).filter(isDemoAgent);
    for (const agent of demos) {
      if (agent.status !== 'online') await postAgentStatus(agent.agent_id, 'online');
    }
    if (demos.length && (state.agents || []).length === demos.length) return;
    return realStatus();
  };
}

document.addEventListener('DOMContentLoaded', setupLocalAgentDemo);
