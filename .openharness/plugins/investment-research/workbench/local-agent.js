// Connecting an Agent that already runs on the user's machine.
//
// The page talks to the Local Bridge directly. This server never proxies to
// the user's machine: the bridge is already on that machine, so a relay would
// add a hop without adding safety.
//
// The bridge refuses everything until paired, and pairing needs a code it
// prints in its own terminal — which is what proves the person driving this
// page can actually see that machine.

const BRIDGE_TOKEN_KEY = 'workbench.bridgeToken';
const localState = {
  bridgeUrl: null,
  token: null,
  pairRequestId: null,
  providers: [],
  detected: [],
  selected: null,
};

function bridgeToken() {
  if (localState.token) return localState.token;
  try { localState.token = localStorage.getItem(BRIDGE_TOKEN_KEY) || null; }
  catch (_error) { localState.token = null; }
  return localState.token;
}

function setBridgeToken(token) {
  localState.token = token;
  try {
    if (token) localStorage.setItem(BRIDGE_TOKEN_KEY, token);
    else localStorage.removeItem(BRIDGE_TOKEN_KEY);
  } catch (_error) { /* private mode: the pairing lasts this session only */ }
}

async function bridgeFetch(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const token = bridgeToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body) headers['Content-Type'] = 'application/json';
  const response = await fetch(`${localState.bridgeUrl}${path}`, { ...options, headers });
  let payload = {};
  try { payload = await response.json(); } catch (_error) { payload = {}; }
  return { ok: response.ok, status: response.status, payload };
}

// ---------------------------------------------------------------- step: bridge

function setLocalStep(step) {
  const hints = {
    bridge: '第 1 步 · 连接本机 Bridge',
    detect: '第 2 步 · 选择本机 Agent',
    configure: '第 3 步 · 配置并创建',
  };
  $('local-step-hint').textContent = hints[step] || '';
  document.querySelectorAll('.local-steps li').forEach(item => {
    item.classList.toggle('active', item.dataset.step === step);
  });
  document.querySelectorAll('[data-step-panel]').forEach(panel => {
    panel.hidden = panel.dataset.stepPanel !== step;
  });
}

function showBridgeState({ text, tone = 'idle', pair = false, next = false, retry = true }) {
  const status = $('bridge-status');
  status.textContent = text;
  status.dataset.tone = tone;
  $('bridge-pair').hidden = !pair;
  $('bridge-pair-start').hidden = !pair || Boolean(localState.pairRequestId);
  $('bridge-next').hidden = !next;
  $('bridge-retry').hidden = !retry;
}

async function probeBridge() {
  const port = state.bridgePort || 18789;
  localState.bridgeUrl = `http://127.0.0.1:${port}`;
  showBridgeState({ text: '正在查找本机 Bridge…', tone: 'idle', retry: false });
  let result;
  try {
    result = await bridgeFetch('/health');
  } catch (_error) {
    // A refused connection is the ordinary "not running" case, and the browser
    // reports it the same way as any network failure.
    showBridgeState({
      text: `没有检测到本机 Bridge（${localState.bridgeUrl}）。请在本机运行：oh local-bridge`,
      tone: 'offline',
    });
    return;
  }
  const health = result.payload || {};
  if (!result.ok && result.status !== 200) {
    showBridgeState({ text: `Bridge 响应异常：HTTP ${result.status}`, tone: 'error' });
    return;
  }
  if (!health.origin_allowed) {
    showBridgeState({
      text: '本机 Bridge 拒绝了当前页面的来源。请用允许的来源打开工作台，或在启动 Bridge 时用 --allow-origin 指定。',
      tone: 'error',
    });
    return;
  }
  if (health.paired) {
    showBridgeState({ text: `已连接本机 Bridge（v${health.version}），且已完成配对。`, tone: 'ok', next: true });
    return;
  }
  setBridgeToken(null);
  localState.pairRequestId = null;
  showBridgeState({
    text: `找到本机 Bridge（v${health.version}），还未配对。`,
    tone: 'warn', pair: true,
  });
}

async function startPairing() {
  $('pair-error').hidden = true;
  const result = await bridgeFetch('/pair/request', {
    method: 'POST',
    body: JSON.stringify({ client_name: 'AI帮投研助手 工作台' }),
  });
  if (!result.ok) {
    showPairError(result.payload.error || '发起配对失败');
    return;
  }
  localState.pairRequestId = result.payload.request_id;
  $('bridge-pair-start').hidden = true;
  showBridgeState({
    text: '配对码已打印在 Bridge 的终端窗口，请查看并输入。',
    tone: 'warn', pair: true,
  });
  $('pair-code').focus();
}

function showPairError(message) {
  const box = $('pair-error');
  box.textContent = message;
  box.hidden = false;
}

async function confirmPairing() {
  const code = ($('pair-code').value || '').trim();
  if (!localState.pairRequestId) { showPairError('请先点击「发起配对」。'); return; }
  if (!/^\d{6}$/.test(code)) { showPairError('请输入 6 位数字配对码。'); return; }
  const result = await bridgeFetch('/pair/confirm', {
    method: 'POST',
    body: JSON.stringify({ request_id: localState.pairRequestId, code }),
  });
  if (!result.ok) {
    showPairError(result.payload.error || '配对失败');
    return;
  }
  setBridgeToken(result.payload.token);
  localState.pairRequestId = null;
  $('pair-code').value = '';
  showBridgeState({ text: '配对成功，已连接本机 Bridge。', tone: 'ok', next: true });
}

// ---------------------------------------------------------------- step: detect

const DETECT_STATUS_TEXT = {
  available: '已安装',
  not_installed: '未检测到',
  error: '检测异常',
};

async function detectLocalAgents() {
  const list = $('detect-list');
  list.innerHTML = '<p class="muted">正在检测本机 Agent…</p>';
  const result = await bridgeFetch('/agents/detect');
  if (!result.ok) {
    list.innerHTML = `<p class="pair-error">${esc(result.payload.error || '检测失败')}</p>`;
    return;
  }
  localState.detected = result.payload.agents || [];
  if (!localState.detected.length) {
    list.innerHTML = '<p class="muted">Bridge 没有返回任何 Agent 类型。</p>';
    return;
  }
  list.innerHTML = localState.detected.map(agent => {
    const usable = agent.status === 'available';
    const statusText = DETECT_STATUS_TEXT[agent.status] || agent.status;
    const detail = agent.version
      ? `版本 ${esc(agent.version)}`
      : esc(agent.detail || '');
    return `<div class="detect-row ${usable ? '' : 'detect-off'}" data-detect="${esc(agent.provider)}">
      <span class="detect-dot detect-${esc(agent.status)}"></span>
      <div class="detect-copy"><strong>${esc(agent.display_name)}</strong><small>${statusText}${detail ? ` · ${detail}` : ''}</small></div>
      ${usable ? `<button type="button" class="primary" data-connect="${esc(agent.provider)}">连接</button>`
               : '<span class="detect-hint">不可用</span>'}
    </div>`;
  }).join('');
  list.querySelectorAll('[data-connect]').forEach(button => {
    button.addEventListener('click', () => chooseProvider(button.dataset.connect));
  });
}

function chooseProvider(provider) {
  localState.selected = localState.detected.find(item => item.provider === provider) || null;
  if (!localState.selected) return;
  setLocalStep('configure');
  const form = $('local-agent-form');
  form.reset();
  form.elements.name.value = `My ${localState.selected.display_name}`;
  $('local-form-error').hidden = true;
  $('local-avatar-preview').hidden = true;
  form.elements.name.focus();
}

// ------------------------------------------------------------- step: configure

async function submitLocalAgent(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  const errorBox = $('local-form-error');
  errorBox.hidden = true;

  if (!localState.selected) {
    errorBox.textContent = '请先选择一个本机 Agent。';
    errorBox.hidden = false;
    return;
  }

  let avatar = '';
  try {
    avatar = await avatarDataUrl(form.get('avatar'));
  } catch (error) {
    errorBox.textContent = error.message || '头像读取失败';
    errorBox.hidden = false;
    return;
  }

  const payload = {
    agent_type: 'local',
    provider: localState.selected.provider,
    name: form.get('name'),
    profile: form.get('profile'),
    workspace: form.get('workspace'),
    permission_mode: form.get('permission_mode'),
    bridge_id: localState.bridgeUrl,
    avatar_data_url: avatar,
  };
  const response = await fetch('/api/agents', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) {
    errorBox.textContent = result.error || '创建失败';
    errorBox.hidden = false;
    return;
  }
  $('local-agent-dialog').close();
  formElement.reset();
  await loadWorkspace(false);
  showAgent(result.agent_id);
  toast(`本地 Agent「${result.name}」已创建`);
}

// ------------------------------------------------------------------- wiring

function openAgentKindDialog() {
  $('agent-kind-dialog').showModal();
}

async function openLocalAgentDialog() {
  localState.selected = null;
  localState.pairRequestId = null;
  setLocalStep('bridge');
  $('local-agent-dialog').showModal();
  await probeBridge();
}

function setupLocalAgent() {
  $('choose-hosted')?.addEventListener('click', () => {
    $('agent-kind-dialog').close();
    openAgentDialog();
  });
  $('choose-local')?.addEventListener('click', () => {
    $('agent-kind-dialog').close();
    openLocalAgentDialog();
  });
  $('bridge-retry')?.addEventListener('click', probeBridge);
  $('bridge-pair-start')?.addEventListener('click', startPairing);
  $('pair-confirm')?.addEventListener('click', confirmPairing);
  $('pair-code')?.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); confirmPairing(); }
  });
  $('bridge-next')?.addEventListener('click', async () => {
    setLocalStep('detect');
    await detectLocalAgents();
  });
  $('detect-refresh')?.addEventListener('click', detectLocalAgents);
  $('local-agent-form')?.addEventListener('submit', submitLocalAgent);
  $('local-agent-form')?.elements.avatar.addEventListener('change', async event => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const preview = $('local-avatar-preview');
      preview.src = await avatarDataUrl(file);
      preview.hidden = false;
    } catch (error) {
      event.target.value = '';
      toast(error.message || '头像读取失败');
    }
  });
  document.querySelectorAll('[data-close="agent-kind-dialog"],[data-close="local-agent-dialog"]')
    .forEach(button => button.addEventListener('click', () => $(button.dataset.close).close()));
}

document.addEventListener('DOMContentLoaded', setupLocalAgent);
