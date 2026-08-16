const state = { channelId: 'research-room', channels: [], messages: [], agents: [], tasks: [], artifacts: [], files: [], run: {}, modelSettings: {default_model:'ark-code-latest',models:[]}, eventSeq: 0, selectedThreadMessageId: null, activePane: 'chat', pendingAttachments: [], graph: null };
const $ = (id) => document.getElementById(id);
const agentColors = { planner:'#C8102E', fundamental:'#A60D28', industry_competition:'#8C3156', market_catalyst:'#C88A18', risk:'#8B2940', reviewer_arbiter:'#6F1630', report_writer:'#B12A46' };
const labels = { planner:'林序', fundamental:'陈实', industry_competition:'周衡', market_catalyst:'沈策', risk:'顾谨', reviewer_arbiter:'韩证', report_writer:'程章', system:'工作台', owner:'你' };
const roles = { planner:'任务规划 Agent', fundamental:'公司基本面 Agent', industry_competition:'行业竞品 Agent', market_catalyst:'市场催化 Agent', risk:'风险 Agent', reviewer_arbiter:'审查仲裁 Agent', report_writer:'报告 Agent' };
function esc(value){ return String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function initials(id){ return (labels[id] || id || '?').slice(0,2); }
function toast(text){ $('toast').textContent=text; $('toast').classList.add('show'); setTimeout(()=>$('toast').classList.remove('show'),2600); }
function kindLabel(kind){ return ({system_message:'工作台状态',user_message:'用户',agent_message:'Agent',task_dispatch:'任务派发',progress_update:'流程状态',task_update:'任务状态',review_issue:'审查 / 返工',artifact_delivery:'成果交付',report_delivery:'报告交付',run_failed:'运行失败'}[kind] || kind); }
function modelOptionsHtml(selected){ return state.modelSettings.models.map(model=>`<option value="${esc(model.id)}" ${model.id===selected?'selected':''}>${esc(model.name)} · ${esc(model.id)}${model.status==='retiring'?'（即将下线）':''}</option>`).join(''); }
function renderAgents(){ $('agent-count').textContent=state.agents.length; $('agent-list').innerHTML=state.agents.map(a=>`<div class="agent-row" data-agent="${esc(a.agent_id)}"><div class="agent-avatar" style="background:${agentColors[a.agent_id] || '#58746a'}">${esc(initials(a.agent_id))}</div><div class="agent-copy"><strong>${esc(a.name || labels[a.agent_id] || a.agent_id)}</strong><span>${esc(a.role || roles[a.agent_id] || 'Agent')}</span></div><i class="status-dot"></i></div>`).join(''); document.querySelectorAll('[data-agent]').forEach(el=>el.addEventListener('click',()=>showAgent(el.dataset.agent))); }
function renderMessages(){ const list=$('message-list'); list.innerHTML=state.messages.map(m=>{ const name=labels[m.author_id] || m.author_id; const body=esc(m.body).replace(/(@[A-Za-z_]+)/g,'<span class="mention">$1</span>'); const meta=[]; if(m.metadata?.task_id) meta.push(`<span class="ref-chip">任务 ${esc(m.metadata.task_id)}</span>`); if(m.metadata?.run_id) meta.push(`<span class="ref-chip">Run ${esc(m.metadata.run_id)}</span>`); return `<article class="message" data-message="${esc(m.message_id)}"><div class="message-avatar" style="background:${agentColors[m.author_id] || '#6e7b76'}">${esc(initials(m.author_id))}</div><div class="message-main"><div class="message-top"><strong>${esc(name)}</strong><time>${new Date(m.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</time><span class="message-kind">${esc(kindLabel(m.message_kind))}</span></div><div class="message-body">${body}</div><div class="message-meta">${meta.join('')}<button class="secondary" data-message-detail="${esc(m.message_id)}">查看线程</button></div></div></article>`; }).join(''); document.querySelectorAll('[data-message-detail]').forEach(el=>el.addEventListener('click',()=>showMessage(el.dataset.messageDetail))); list.scrollTop=list.scrollHeight; }
function renderRun(){ const r=state.run || {}; const banner=$('run-banner'); if(r.error){ banner.className='run-banner failed'; $('run-label').textContent='本次运行失败，但记录已保留'; $('run-meta').textContent=r.error; } else if(r.running){ banner.className='run-banner running'; $('run-label').textContent='研究流程运行中'; $('run-meta').textContent=`${r.company || ''} · ${r.as_of_date || ''}`; } else if(r.report_available){ const fallback=Boolean(r.summary && r.summary.fallback_used); banner.className='run-banner'; $('run-label').textContent=fallback?'降级报告已交付':'报告已交付'; $('run-meta').textContent=`Run ${r.run_id || ''}`; } else { banner.className='run-banner'; $('run-label').textContent='等待研究任务'; $('run-meta').textContent='演示数据 / 本地真实运行入口'; } }
function renderContext(){ const r=state.run||{}; const current=state.tasks.find(t=>t.status==='running') || state.tasks[0]; $('context-content').innerHTML=`<div class="context-card"><h2>当前研究运行</h2><p class="muted">${r.running?'正在执行真实 CrewAI/OpenHarness 流程':'@Planner 可启动完整研究；@其他 Agent 可直接派发补充任务'}</p><div class="kv"><span>公司</span><strong>${esc(r.company || '科大讯飞')}</strong></div><div class="kv"><span>基准日</span><strong>${esc(r.as_of_date || '默认今天')}</strong></div><div class="kv"><span>状态</span><strong>${r.running?'运行中':r.report_available?'已交付':'等待中'}</strong></div>${current?`<h3>最近任务</h3><div class="kv"><span>任务</span><strong>${esc(current.title)}</strong></div><div class="kv"><span>负责人</span><strong>${esc(current.assignee_id)}</strong></div>`:''}<h3>两种协作方式</h3><p><strong>@Planner</strong> 会启动七个 Agent 的完整研究闭环；<strong>@Fundamental 等其他 Agent</strong> 会复用频道最近一次 Run 的参数卡和证据，单独执行补充任务。一次 @多个 Agent 时会并行派发。</p>${r.report_available?'<button class="primary" id="context-report">打开 Markdown 报告</button>':''}</div>`; const btn=$('context-report'); if(btn) btn.addEventListener('click',openReport); }
function showAgent(id){
  const a=state.agents.find(x=>x.agent_id===id)||{};
  const isCustom=a.type==='custom';
  const model=a.model||state.modelSettings.default_model;
  const avatar=a.avatar_path
    ? `<img class="profile-avatar" src="/${esc(a.avatar_path)}" alt="${esc(a.name||id)}">`
    : `<div class="profile-avatar profile-avatar-fallback" style="background:${agentColors[id] || '#58746a'}">${esc(initials(id))}</div>`;
  // Model and avatar are operator choices for every Agent, built-in included.
  // Prompt and contract stay editable only for custom Agents.
  $('context-content').innerHTML=`<div class="context-card"><div class="profile-head">${avatar}<label class="avatar-replace">更换头像<input id="agent-avatar-input" type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden></label></div><h2>${esc(a.name||labels[id]||id)}</h2><p class="muted">${esc(a.role||roles[id]||'Agent')}</p><div class="kv"><span>Agent ID</span><strong>${esc(id)}</strong></div><div class="kv"><span>类型</span><strong>${isCustom?'自定义':'系统内置'}</strong></div><div class="kv"><span>当前模型</span><strong>${esc(model)}</strong></div><h3>个人简介</h3><p>${esc(a.profile||'暂无简介')}</p>${isCustom?`<h3>系统提示词</h3><div class="agent-prompt">${esc(a.system_prompt||'')}</div>`:`<h3>工具白名单</h3><div>${(a.allowed_tools||[]).map(t=>`<span class="tool-tag">${esc(t)}</span>`).join('')||'<span class="muted">当前角色无直接工具</span>'}</div>`}<h3>切换模型</h3><select id="agent-model-select">${modelOptionsHtml(model)}</select><h3>状态</h3><p><span class="status-dot"></span> ${esc(taskStatusText(a.status))} · ${esc(a.task_phase||'等待频道任务')}</p><div class="context-actions"><button id="save-agent-model" class="primary" type="button">保存模型</button>${isCustom?'<button id="edit-agent" class="secondary" type="button">编辑资料</button>':''}</div></div><div class="context-card dm-card"><h2>单独对话</h2><p class="muted">只有你和 ${esc(a.name||id)} 的一对一频道，不进入项目频道。</p><div id="dm-list" class="dm-list"><p class="muted">正在加载对话…</p></div><form id="dm-composer" class="thread-composer"><textarea id="dm-input" rows="3" placeholder="直接跟 ${esc(a.name||id)} 说，例如：帮我核一下这条数据的来源"></textarea><div class="thread-composer-footer"><span>发送后会为它单独建一个任务。</span><button type="submit" class="send">发送</button></div></form></div>`;
  $('edit-agent')?.addEventListener('click',()=>openAgentEditor(id));
  $('save-agent-model')?.addEventListener('click',()=>saveAgentModel(id));
  $('agent-avatar-input')?.addEventListener('change',event=>saveAgentAvatar(id,event));
  setupDirectMessages(id);
}

async function saveAgentAvatar(agentId,event){
  const file=event.target.files?.[0];
  event.target.value='';
  if(!file) return;
  try{
    const avatar=await avatarDataUrl(file);
    const response=await fetch(`/api/agents/${encodeURIComponent(agentId)}`,{
      method:'PATCH', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({avatar_data_url:avatar}),
    });
    const result=await response.json();
    if(!response.ok) throw new Error(result.error || '头像更新失败');
    await loadWorkspace(false);
    showAgent(agentId);
    toast(`${result.name || agentId} 的头像已更新`);
  }catch(error){ toast(error.message || '头像更新失败'); }
}

// A direct message is a private 1:1 channel per Agent.  It reuses the same
// task pipeline as an @mention, so the reply is a real run, not a chat echo.
function directMessageHtml(message){
  const mine=message.author_type==='human';
  const body=esc(message.body).replace(/(@[A-Za-z_]+)/g,'<span class="mention">$1</span>');
  return `<div class="dm-message ${mine?'dm-mine':''}"><div class="dm-message-head"><strong>${esc(labels[message.author_id]||message.author_id)}</strong><time>${esc(new Date(message.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}))}</time></div><div class="dm-message-body">${body}</div></div>`;
}
async function loadDirectMessages(agentId){
  const list=$('dm-list');
  if(!list) return;
  try{
    const response=await fetch(`/api/agents/${encodeURIComponent(agentId)}/messages`,{cache:'no-store'});
    if(!response.ok) throw new Error('unavailable');
    const data=await response.json();
    list.innerHTML=(data.messages||[]).length
      ? data.messages.map(directMessageHtml).join('')
      : '<p class="muted">还没有单独对话。发第一条消息给它。</p>';
    list.scrollTop=list.scrollHeight;
  }catch(_error){
    list.innerHTML='<p class="muted">单独对话暂时不可用，请刷新后重试。</p>';
  }
}
function setupDirectMessages(agentId){
  loadDirectMessages(agentId);
  const form=$('dm-composer');
  const input=$('dm-input');
  if(!form || !input) return;
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    const body=input.value.trim();
    if(!body) return;
    const response=await fetch(`/api/agents/${encodeURIComponent(agentId)}/messages`,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({body, as_of_date:state.run?.as_of_date || new Date().toISOString().slice(0,10)}),
    });
    const result=await response.json();
    if(!response.ok){ toast(result.error || '发送失败'); return; }
    input.value='';
    await loadDirectMessages(agentId);
    toast(result.notice || (result.status==='agent_started' ? '已单独派给它一个任务' : '消息已发送'));
  });
}
async function saveAgentModel(agentId){
  const model=$('agent-model-select').value;
  const response=await fetch(`/api/agents/${encodeURIComponent(agentId)}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({model})});
  const result=await response.json();
  if(!response.ok){ toast(result.error || '模型切换失败'); return; }
  await loadWorkspace(false); showAgent(agentId); toast(`已将 ${result.name || agentId} 切换为 ${model}`);
}
function showMessage(id){ const m=state.messages.find(x=>x.message_id===id); if(!m)return; $('context-content').innerHTML=`<div class="context-card"><h2>${esc(kindLabel(m.message_kind))}</h2><p>${esc(m.body)}</p><h3>消息信息</h3><div class="kv"><span>作者</span><strong>${esc(labels[m.author_id]||m.author_id)}</strong></div><div class="kv"><span>时间</span><strong>${esc(m.created_at)}</strong></div><div class="kv"><span>关联任务</span><strong>${esc(m.metadata?.task_id||'无')}</strong></div><p class="muted">详细线程能力会在下一阶段加入；当前先把主频道、任务状态和真实运行结果打通。</p></div>`; }
async function loadWorkspace(show=true){ const res=await fetch(`/api/workspace?channel_id=${encodeURIComponent(state.channelId)}`,{cache:'no-store'}); const data=await res.json(); state.channels=data.channels||[]; state.agents=data.agents||[]; state.tasks=data.tasks||[]; state.artifacts=data.artifacts||[]; state.files=data.files||[]; state.run=data.run||{}; state.modelSettings=data.model_settings||state.modelSettings; state.eventSeq=Math.max(state.eventSeq,Number(data.event_seq||0)); renderModelOptions(); renderChannels(); renderAgents(); renderRun(); renderTaskBoard(); renderFileBoard(); if(state.activePane==='graph') loadGraph(); if(state.selectedThreadMessageId){ refreshSelectedThread(false); }else{ renderContext(); } if(show) $('connection-state').textContent='已连接'; }
async function loadMessages(){ const res=await fetch(`/api/channels/${state.channelId}/messages`); state.messages=await res.json(); renderMessages(); }
async function openReport(){ const res=await fetch(`/api/research/report?run_id=${encodeURIComponent(state.run.run_id||'')}`); if(!res.ok){toast('当前还没有报告');return;} const text=await res.text(); const win=window.open(); win.document.write(`<pre style="white-space:pre-wrap;font:14px/1.6 system-ui;padding:28px">${esc(text)}</pre>`); win.document.close(); }
function setupComposer(){
  const input=$('message-input'), menu=$('mention-menu');
  const choices=state.agents;
  input.addEventListener('input',()=>{
    const value=input.value;
    if(!value.includes('@')){menu.style.display='none';return;}
    const partial=value.split('@').pop().toLowerCase();
    const found=choices.filter(a=>(a.agent_id+a.name).toLowerCase().includes(partial)).slice(0,7);
    menu.innerHTML=found.map(a=>`<div class="mention-option" data-mention="${esc(a.agent_id)}"><strong>@${esc(a.agent_id)}</strong> <span>${esc(a.role||'')}</span></div>`).join('');
    menu.style.display=found.length?'block':'none';
    menu.querySelectorAll('[data-mention]').forEach(el=>el.addEventListener('click',()=>{
      input.value=input.value.replace(/@[^\s@]*$/,'@'+el.dataset.mention+' ');
      menu.style.display='none';
      input.focus();
    }));
  });
  $('composer').addEventListener('submit',async e=>{
    e.preventDefault();
    const body=input.value.trim();
    const attachmentIds=state.pendingAttachments.map(item=>item.file_id);
    if(!body && !attachmentIds.length)return;
    const company=body.includes('科大讯飞')?'科大讯飞':body.includes('宁德时代')?'宁德时代':'科大讯飞';
    const res=await fetch(`/api/channels/${state.channelId}/messages`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({body,company,as_of_date:new Date().toISOString().slice(0,10),attachment_ids:attachmentIds})});
    const data=await res.json();
    if(!res.ok){toast(data.error||'发送失败');return;}
    input.value='';
    state.pendingAttachments=[];
    renderAttachmentTray();
    menu.style.display='none';
    await loadWorkspace();
    await loadMessages();
    if(data.status==='started'){
      toast('已创建完整研究任务，页面会实时显示进度');
    }else if(data.status==='agents_started'){
      toast(`已向 ${data.agent_ids.length} 个 Agent 并行派发直接任务`);
    }else{
      toast('消息已发送到频道');
    }
  });
}
async function startDefault(){ const res=await fetch('/api/research/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({company:'科大讯飞',as_of_date:new Date().toISOString().slice(0,10),channel_id:state.channelId})}); const data=await res.json(); if(!res.ok)toast(data.error||'启动失败'); else toast('已启动真实研究流程'); await loadWorkspace(); await loadMessages(); }
document.addEventListener('DOMContentLoaded',async()=>{ await loadWorkspace(); await loadMessages(); setupComposer(); connectEvents(); $('refresh')?.addEventListener('click',()=>{loadWorkspace();loadMessages();}); $('report-btn')?.addEventListener('click',openReport); $('start-default')?.addEventListener('click',startDefault); $('new-research')?.addEventListener('click',startDefault); });

function showTask(taskId){ const task=state.tasks.find(item=>item.task_id===taskId); if(!task)return; $('context-content').innerHTML=`<div class="context-card"><h2>任务详情</h2><p>${esc(task.title)}</p><div class="kv"><span>任务 ID</span><strong>${esc(task.task_id)}</strong></div><div class="kv"><span>负责人</span><strong>${esc(labels[task.assignee_id]||task.assignee_id)}</strong></div><div class="kv"><span>状态</span><strong>${esc(task.status)}</strong></div><div class="kv"><span>运行 ID</span><strong>${esc(task.run_id||'等待运行')}</strong></div><h3>任务说明</h3><p>这是由频道消息触发的研究任务。任务状态来自本地 SQLite 协作记录，不是静态图片。</p></div>`; }
function showArtifact(artifactId){ const artifact=state.artifacts.find(item=>item.artifact_id===artifactId); if(!artifact)return; $('context-content').innerHTML=`<div class="context-card"><h2>交付成果</h2><p>${esc(artifact.title)}</p><div class="kv"><span>成果 ID</span><strong>${esc(artifact.artifact_id)}</strong></div><div class="kv"><span>提交 Agent</span><strong>${esc(labels[artifact.agent_id]||artifact.agent_id)}</strong></div><div class="kv"><span>状态</span><strong>${esc(artifact.status)}</strong></div><h3>成果摘要</h3><p>${esc(artifact.summary)}</p><h3>关联编号</h3><div>${(artifact.refs||[]).map(item=>`<span class="tool-tag">${esc(item)}</span>`).join('')||'<span class="muted">暂未记录</span>'}</div></div>`; }
const baseRenderMessages = renderMessages;
renderMessages = function(){ baseRenderMessages(); document.querySelectorAll('.message').forEach(article=>{ const message=state.messages.find(item=>item.message_id===article.dataset.message); if(!message)return; const meta=message.metadata||{}; if(meta.task_id){ const card=document.createElement('div'); card.className='task-card'; card.innerHTML=`<span>任务 ${esc(meta.task_id)}</span><button data-task-detail="${esc(meta.task_id)}">查看任务</button>`; article.querySelector('.message-main').appendChild(card); } if(meta.artifact_id){ const card=document.createElement('div'); card.className='artifact-card'; card.innerHTML=`<strong>交付成果：${esc(meta.artifact_id)}</strong><button data-artifact-detail="${esc(meta.artifact_id)}">查看成果</button>`; article.querySelector('.message-main').appendChild(card); } }); document.querySelectorAll('[data-task-detail]').forEach(el=>el.addEventListener('click',()=>showTask(el.dataset.taskDetail))); document.querySelectorAll('[data-artifact-detail]').forEach(el=>el.addEventListener('click',()=>showArtifact(el.dataset.artifactDetail))); };

// The chat list is the only scrolling surface.  Run controls stay in the header.
function renderRun(){
  const r=state.run || {};
  const banner=$('run-banner');
  const pause=$('pause-run');
  const resume=$('resume-run');
  const paused=Boolean(r.paused || r.pause_requested);
  if(r.error){
    banner.className='run-banner failed';
    $('run-label').textContent='本次运行失败，但记录已保留';
    $('run-meta').textContent=r.error;
  } else if(r.running && paused){
    banner.className='run-banner paused';
    $('run-label').textContent=r.paused?'项目已暂停':'正在请求暂停';
    $('run-meta').textContent=`${r.company || ''} · ${r.as_of_date || ''}`;
  } else if(r.running){
    banner.className='run-banner running';
    $('run-label').textContent='研究流程运行中';
    $('run-meta').textContent=`${r.company || ''} · ${r.as_of_date || ''}`;
  } else if(r.report_available){
    const fallback=Boolean(r.summary && r.summary.fallback_used);
    banner.className='run-banner';
    $('run-label').textContent=fallback?'降级报告已交付':'报告已交付';
    $('run-meta').textContent=`Run ${r.run_id || ''}`;
  } else {
    banner.className='run-banner';
    $('run-label').textContent='等待研究任务';
    $('run-meta').textContent='演示数据 / 本地真实运行入口';
  }
  if(pause) pause.hidden=!r.running || paused;
  if(resume) resume.hidden=!r.running || !paused;
}

async function setRunPause(path){
  const res=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const data=await res.json();
  if(!res.ok){ toast(data.error || '项目控制失败'); return; }
  await loadWorkspace();
  await loadMessages();
  toast(path.endsWith('/pause')?'已请求暂停，当前调用结束后会停在下一阶段':'已继续项目');
}
function pauseRun(){ return setRunPause('/api/research/pause'); }
function resumeRun(){ return setRunPause('/api/research/resume'); }
document.addEventListener('DOMContentLoaded',()=>{
  $('pause-run')?.addEventListener('click',pauseRun);
  $('resume-run')?.addEventListener('click',resumeRun);
});

// Realtime channel transport. Keep exactly one SSE connection, replay from the
// latest event cursor after reconnect, and batch bursts into one page refresh.
let liveEventSource = null;
let liveReconnectTimer = null;
let liveReconnectAttempt = 0;
let liveRefreshTimer = null;
let liveRefreshInFlight = false;
let liveRefreshQueued = false;

function scheduleRealtimeRefresh(){
  liveRefreshQueued = true;
  if(liveRefreshTimer || liveRefreshInFlight) return;
  liveRefreshTimer = setTimeout(async()=>{
    liveRefreshTimer = null;
    liveRefreshInFlight = true;
    liveRefreshQueued = false;
    try{
      await Promise.all([loadWorkspace(false), loadMessages()]);
    }catch(_error){
      $('connection-state').textContent='数据同步重试中';
    }finally{
      liveRefreshInFlight = false;
      if(liveRefreshQueued) scheduleRealtimeRefresh();
    }
  }, 90);
}

function handleEvent(event){
  try{
    const item=JSON.parse(event.data);
    state.eventSeq=Math.max(
      state.eventSeq,
      Number(item.event_seq || event.lastEventId || 0),
    );
    scheduleRealtimeRefresh();
  }catch(_error){
    // Heartbeats and malformed frames are ignored; reconnect will replay by cursor.
  }
}

function connectEvents(){
  if(liveEventSource && [EventSource.OPEN, EventSource.CONNECTING].includes(liveEventSource.readyState)) return;
  if(liveReconnectTimer){ clearTimeout(liveReconnectTimer); liveReconnectTimer=null; }
  const source=new EventSource(`/api/events?channel_id=${encodeURIComponent(state.channelId)}&after=${state.eventSeq}`);
  liveEventSource=source;
  $('connection-state').textContent='正在连接';
  source.onopen=()=>{
    liveReconnectAttempt=0;
    $('connection-state').textContent='实时连接';
  };
  source.onmessage=handleEvent;
  source.onerror=()=>{
    if(liveEventSource===source) liveEventSource=null;
    source.close();
    $('connection-state').textContent='等待重连';
    const base=Math.min(8000, 500 * (2 ** liveReconnectAttempt));
    const delay=base + Math.floor(Math.random() * 250);
    liveReconnectAttempt=Math.min(liveReconnectAttempt + 1, 5);
    if(!liveReconnectTimer){
      liveReconnectTimer=setTimeout(()=>{
        liveReconnectTimer=null;
        connectEvents();
      }, delay);
    }
  };
}

// Keep the reader's scroll position when older messages are being inspected.
// New messages only auto-scroll when the user was already close to the bottom.
const renderMessagesWithCardsAndDetails = renderMessages;
renderMessages = function(){
  const list=$('message-list');
  const previousTop=list.scrollTop;
  const wasNearBottom=list.scrollHeight-list.scrollTop-list.clientHeight < 100;
  renderMessagesWithCardsAndDetails();
  if(wasNearBottom){
    list.scrollTop=list.scrollHeight;
  }else{
    list.scrollTop=previousTop;
  }
};

// The right panel explains what an Agent actually handed off without exposing
// private chain-of-thought or full tool responses.
function showMessage(id){
  const m=state.messages.find(x=>x.message_id===id);
  if(!m) return;
  const projection=m.metadata?.result_projection || {};
  const evidence=[
    ['来源', projection.source_ids],
    ['事实', projection.fact_ids],
    ['逻辑', projection.logic_ids],
    ['催化', projection.catalyst_ids],
    ['风险', projection.risk_ids],
  ].filter(([,values])=>Array.isArray(values) && values.length);
  const evidenceHtml=evidence.length
    ? evidence.map(([label,values])=>`<div class="kv"><span>${esc(label)}</span><strong>${values.length} 条</strong></div>`).join('')
    : '<p class="muted">本条消息没有新增证据编号。</p>';
  const tools=(projection.tool_names || []).map(tool=>`<span class="tool-tag">${esc(tool)}</span>`).join('');
  $('context-content').innerHTML=`<div class="context-card"><h2>${esc(kindLabel(m.message_kind))}</h2><p>${esc(m.body)}</p><h3>消息信息</h3><div class="kv"><span>作者</span><strong>${esc(labels[m.author_id]||m.author_id)}</strong></div><div class="kv"><span>时间</span><strong>${esc(m.created_at)}</strong></div><div class="kv"><span>关联任务</span><strong>${esc(m.metadata?.task_id||'无')}</strong></div>${tools?`<h3>本轮使用工具</h3><div>${tools}</div>`:''}<h3>本轮证据增量</h3>${evidenceHtml}${projection.total_tokens?`<div class="kv"><span>模型 Token</span><strong>${esc(projection.total_tokens)}</strong></div>`:''}</div>`;
}

window.addEventListener('online',()=>{
  liveReconnectAttempt=0;
  if(liveEventSource){ liveEventSource.close(); liveEventSource=null; }
  connectEvents();
  scheduleRealtimeRefresh();
});
document.addEventListener('visibilitychange',()=>{
  if(document.visibilityState==='visible'){
    scheduleRealtimeRefresh();
    connectEvents();
  }
});

// Project real task liveness into the roster and context panel.  The timer is
// based on server-published elapsed_seconds; it never claims a tool ran unless
// the runtime returned a real tool trace.
const taskStatusLabels = {
  queued:'排队中', pending:'待办', running:'执行中', completed:'已交付', complete:'已交付',
  failed:'失败', blocked:'已阻塞', online:'在线',
  review:'待确认', awaiting_review:'待确认', partial:'部分完成',
};
function taskStatusText(status){ return taskStatusLabels[status] || status || '在线'; }
function formatElapsed(seconds){
  const value=Number(seconds);
  if(!Number.isFinite(value) || value <= 0) return '刚刚开始';
  const mins=Math.floor(value/60), secs=value%60;
  return mins ? `${mins}分${String(secs).padStart(2,'0')}秒` : `${secs}秒`;
}
renderAgents = function(){
  $('agent-count').textContent=state.agents.length;
  $('agent-list').innerHTML=state.agents.map(a=>{
    const status=a.status || 'online';
    const detail=status==='running' && a.task_phase ? a.task_phase : taskStatusText(status);
    const avatar=a.avatar_path?`<img src="/${esc(a.avatar_path)}" alt="">`:esc(initials(a.agent_id));
    return `<div class="agent-row" data-agent="${esc(a.agent_id)}" data-status="${esc(status)}"><div class="agent-avatar" style="background:${agentColors[a.agent_id] || '#58746a'}">${avatar}</div><div class="agent-copy"><strong>${esc(a.name || labels[a.agent_id] || a.agent_id)}</strong><span>${esc(a.role || roles[a.agent_id] || 'Agent')}</span><small class="agent-status-label">${esc(detail)}</small></div><i class="status-dot"></i></div>`;
  }).join('');
  document.querySelectorAll('[data-agent]').forEach(el=>el.addEventListener('click',()=>showAgent(el.dataset.agent)));
};
renderContext = function(){
  const r=state.run||{};
  const current=state.tasks.find(t=>t.status==='running') || state.tasks.find(t=>t.status==='queued') || state.tasks[0];
  const metadata=current?.metadata || {};
  $('context-content').innerHTML=`<div class="context-card"><h2>当前研究运行</h2><p class="muted">${r.running?'正在执行真实 CrewAI/OpenHarness 流程':'@Planner 可启动完整研究；@其他 Agent 可直接派发补充任务'}</p><div class="kv"><span>公司</span><strong>${esc(r.company || '科大讯飞')}</strong></div><div class="kv"><span>基准日</span><strong>${esc(r.as_of_date || '默认今天')}</strong></div><div class="kv"><span>流程状态</span><strong>${r.running?'运行中':r.report_available?'已交付':'等待中'}</strong></div>${current?`<h3>最近任务</h3><div class="kv"><span>任务</span><strong>${esc(current.title)}</strong></div><div class="kv"><span>负责人</span><strong>${esc(labels[current.assignee_id]||current.assignee_id)}</strong></div><div class="kv"><span>任务状态</span><strong>${esc(taskStatusText(current.status))}</strong></div>${metadata.phase?`<div class="kv"><span>当前阶段</span><strong>${esc(metadata.phase)}</strong></div>`:''}${metadata.elapsed_seconds!=null?`<div class="kv"><span>已运行</span><strong>${esc(formatElapsed(metadata.elapsed_seconds))}</strong></div>`:''}${metadata.last_activity_at?`<div class="kv"><span>最近活动</span><strong>${esc(metadata.last_activity_at)}</strong></div>`:''}`:''}<h3>协作方式</h3><p><strong>@Planner</strong> 启动完整研究；<strong>@其他 Agent</strong> 复用当前 Run 的参数卡和证据执行直接任务。页面显示的工具和证据只来自真实运行回执。</p>${r.report_available?'<button class="primary" id="context-report">打开 Markdown 报告</button>':''}</div>`;
  const btn=$('context-report');
  if(btn) btn.addEventListener('click',openReport);
};

// A conversation thread is a focused handoff record, not a copy of hidden
// model reasoning.  It lets a reviewer follow one request from @mention to
// task state, reply, artifact and evidence references without losing their
// position in the main channel.
function threadRefs(messages){
  const refs=new Set();
  messages.forEach(message=>{
    const projection=message.metadata?.result_projection || {};
    ['source_ids','fact_ids','logic_ids','catalyst_ids','risk_ids'].forEach(key=>{
      (projection[key] || []).forEach(value=>refs.add(value));
    });
  });
  return [...refs];
}
function threadMessageHtml(message, rootId){
  const isRoot=message.message_id===rootId;
  const body=esc(message.body).replace(/(@[A-Za-z_]+)/g,'<span class="mention">$1</span>');
  const mentions=(message.mentions || []).map(item=>`<span class="ref-chip">@${esc(item)}</span>`).join('');
  const projection=message.metadata?.result_projection || {};
  const failure=projection.failure_class || (message.metadata || {}).failure_class;
  const retryCount=projection.retry_count ?? (message.metadata || {}).retry_count;
  return `<article class="thread-message ${isRoot?'thread-root':''}"><div class="thread-message-head"><strong>${esc(labels[message.author_id]||message.author_id)}</strong><span>${esc(kindLabel(message.message_kind))}</span><time>${esc(new Date(message.created_at).toLocaleString())}</time></div><div class="thread-message-body">${body}</div>${mentions?`<div class="thread-mentions">${mentions}</div>`:''}${failure?`<div class="thread-warning">执行异常：${esc(failure)}${retryCount!=null?`；已重试 ${esc(retryCount)} 次`:''}</div>`:''}</article>`;
}
async function refreshSelectedThread(showLoading=true){
  const messageId=state.selectedThreadMessageId;
  if(!messageId) return;
  if(showLoading) $('context-content').innerHTML='<div class="empty-context"><h2>正在加载线程…</h2></div>';
  try{
    const res=await fetch(`/api/threads/${encodeURIComponent(messageId)}?channel_id=${encodeURIComponent(state.channelId)}`,{cache:'no-store'});
    if(!res.ok) throw new Error('thread unavailable');
    const data=await res.json();
    const allMessages=[data.root,...(data.replies||[])];
    const refs=threadRefs(allMessages);
    const taskHtml=(data.tasks||[]).length
      ? data.tasks.map(task=>`<div class="thread-item"><strong>${esc(task.title)}</strong><span>任务 ${esc(task.task_id)} · ${esc(taskStatusText(task.status))}</span><small>负责人：${esc(labels[task.assignee_id]||task.assignee_id)}</small></div>`).join('')
      : '<p class="muted">这条交接没有关联独立任务。</p>';
    const artifactHtml=(data.artifacts||[]).length
      ? data.artifacts.map(item=>`<div class="thread-item"><strong>${esc(item.title)}</strong><span>${esc(labels[item.agent_id]||item.agent_id)} · ${esc(item.status)}</span><p>${esc(item.summary || '暂无摘要')}</p></div>`).join('')
      : '<p class="muted">尚未产生交付物；后续回复会显示在这里。</p>';
    $('context-content').innerHTML=`<div class="thread-card"><div class="thread-title"><div><span class="eyebrow">线程交接</span><h2>${esc(kindLabel(data.root.message_kind))}</h2></div><button id="thread-back" class="secondary">返回概览</button></div><section><h3>原始请求</h3>${threadMessageHtml(data.root,data.root.message_id)}</section><section><h3>协作回复（${(data.replies||[]).length}）</h3>${(data.replies||[]).map(item=>threadMessageHtml(item,data.root.message_id)).join('')||'<p class="muted">暂时没有回复。Agent 接收、进度、交付和失败说明都会追加到此处。</p>'}</section><section><h3>关联任务</h3>${taskHtml}</section><section><h3>交付物</h3>${artifactHtml}</section><section><h3>证据编号</h3><div class="thread-refs">${refs.map(ref=>`<span class="tool-tag">${esc(ref)}</span>`).join('')||'<span class="muted">暂无新增 S/F/L/Risk 编号</span>'}</div></section><section class="thread-composer-section"><h3>继续协作</h3><form id="thread-composer" class="thread-composer"><textarea id="thread-input" rows="3" placeholder="例如：@Fundamental 请补充最近一年收入变化的原因"></textarea><div class="thread-agent-quick">${state.agents.filter(agent=>agent.agent_id!=='planner').map(agent=>`<button type="button" data-thread-mention="${esc(agent.agent_id)}">@${esc(agent.agent_id)}</button>`).join('')}</div><div class="thread-composer-footer"><span>线程内 @多个 Agent 会创建独立任务并行执行。</span><button type="submit" class="send">发送并派单</button></div></form></section></div>`;
    $('thread-back')?.addEventListener('click',()=>{ state.selectedThreadMessageId=null; renderContext(); });
    setupThreadComposer(data.root.message_id);
  }catch(_error){
    $('context-content').innerHTML='<div class="empty-context"><h2>线程暂时不可用</h2><p>请刷新页面后重试。研究运行不会因此中断。</p></div>';
  }
}
showMessage = function(id){
  state.selectedThreadMessageId=id;
  refreshSelectedThread(true);
};

function setupThreadComposer(rootMessageId){
  const form=$('thread-composer');
  const input=$('thread-input');
  if(!form || !input) return;
  document.querySelectorAll('[data-thread-mention]').forEach(button=>button.addEventListener('click',()=>{
    input.value=`${input.value}${input.value && !input.value.endsWith(' ') ? ' ' : ''}@${button.dataset.threadMention} `;
    input.focus();
  }));
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    const body=input.value.trim();
    if(!body) return;
    const company=state.run?.company || '科大讯飞';
    const asOfDate=state.run?.as_of_date || new Date().toISOString().slice(0,10);
    const response=await fetch(`/api/channels/${state.channelId}/messages`,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({body, company, as_of_date:asOfDate, thread_id:rootMessageId}),
    });
    const result=await response.json();
    if(!response.ok){ toast(result.error || '线程消息发送失败'); return; }
    input.value='';
    await Promise.all([loadWorkspace(false),loadMessages()]);
    await refreshSelectedThread(false);
    if(result.status==='agents_started'){
      toast(`已向 ${result.agent_ids.length} 个 Agent 派发线程任务`);
    }else if(body.includes('@planner')){
      toast('Planner 不会在线程内重启完整研究；请 @研究 Agent 发起定向补充。');
    }else{
      toast('线程回复已发送');
    }
  });
}

// Chat / Tasks / Files share the centre column.  Only the chat pane owns the
// composer, so switching to a board never leaves a send box pointing at a view
// that cannot receive a message.
function setPane(pane){
  state.activePane=pane;
  document.querySelectorAll('.pane-tab').forEach(tab=>tab.classList.toggle('active',tab.dataset.pane===pane));
  document.querySelectorAll('.pane-view').forEach(view=>{ view.hidden=view.dataset.paneView!==pane; });
  const composer=$('composer');
  if(composer) composer.hidden=pane!=='chat';
  if(pane==='tasks') renderTaskBoard();
  if(pane==='files') renderFileBoard();
  if(pane==='graph') loadGraph();
}
function setupPaneTabs(){
  document.querySelectorAll('.pane-tab').forEach(tab=>tab.addEventListener('click',()=>setPane(tab.dataset.pane)));
  document.querySelectorAll('.rail-btn').forEach((button,index)=>button.addEventListener('click',()=>{
    document.querySelectorAll('.rail-btn').forEach(item=>item.classList.remove('active'));
    button.classList.add('active');
    setPane(['chat','tasks','files'][index] || 'chat');
  }));
  $('open-graph')?.addEventListener('click',()=>setPane('graph'));
  setPane('chat');
}

// The board mirrors the four states an Agent task really moves through.  A
// status the backend has not published yet lands in 待办 rather than vanishing.
const TASK_COLUMNS=[
  {key:'queued', label:'待办', statuses:['queued','pending','blocked']},
  {key:'running', label:'进行中', statuses:['running']},
  {key:'review', label:'待确认', statuses:['review','awaiting_review','partial']},
  {key:'done', label:'完成', statuses:['completed','complete','failed']},
];
function taskColumnKey(status){
  const found=TASK_COLUMNS.find(column=>column.statuses.includes(String(status||'').toLowerCase()));
  return found ? found.key : 'queued';
}
function taskCardHtml(task){
  const metadata=task.metadata||{};
  const assignee=labels[task.assignee_id]||task.assignee_id||'未指派';
  const elapsed=metadata.elapsed_seconds!=null?`<small>已运行 ${esc(formatElapsed(metadata.elapsed_seconds))}</small>`:'';
  const phase=metadata.phase?`<small>阶段：${esc(metadata.phase)}</small>`:'';
  return `<article class="board-card" data-board-task="${esc(task.task_id)}" data-status="${esc(task.status)}"><div class="board-card-id">${esc(task.task_id)}</div><strong>${esc(task.title)}</strong><div class="board-card-foot"><span class="board-assignee">${esc(assignee)}</span><span class="board-status">${esc(taskStatusText(task.status))}</span></div>${phase}${elapsed}</article>`;
}
function renderTaskBoard(){
  const board=$('task-board');
  const counter=$('tab-task-count');
  if(counter) counter.textContent=state.tasks.length;
  if(!board) return;
  board.innerHTML=TASK_COLUMNS.map(column=>{
    const items=state.tasks.filter(task=>taskColumnKey(task.status)===column.key);
    const cards=items.map(taskCardHtml).join('') || `<div class="board-empty">没有${esc(column.label)}的任务。</div>`;
    return `<section class="board-column" data-column="${esc(column.key)}"><header><span class="board-chip board-chip-${esc(column.key)}">${esc(column.label)}</span><b>${items.length}</b></header><div class="board-column-body">${cards}</div></section>`;
  }).join('');
  board.querySelectorAll('[data-board-task]').forEach(card=>card.addEventListener('click',()=>showTask(card.dataset.boardTask)));
}

// Relationship graph: who handed work to whom.  Every edge comes from a real
// record — a task's creator/assignee pair, or a message and the Agents it
// mentioned.  Nodes sit on a circle so the layout is stable between refreshes
// instead of jittering the way a force simulation would.
const GRAPH_VIEWBOX={width:900, height:520};
function graphNodePositions(nodes){
  const centreX=GRAPH_VIEWBOX.width/2, centreY=GRAPH_VIEWBOX.height/2;
  const radius=Math.min(centreX,centreY)-70;
  if(nodes.length===1) return new Map([[nodes[0].id,{x:centreX,y:centreY}]]);
  return new Map(nodes.map((node,index)=>{
    const angle=(index/nodes.length)*Math.PI*2 - Math.PI/2;
    return [node.id,{x:centreX+radius*Math.cos(angle), y:centreY+radius*Math.sin(angle)}];
  }));
}
const GRAPH_NODE_FILL={agent:'#C8102E', human:'#7257a8', system:'#6d8c7c'};
function graphSvg(graph){
  const nodes=graph.nodes||[];
  if(!nodes.length) return '<div class="board-empty board-empty-wide">这个频道还没有产生协作记录。派一个任务或 @一个 Agent 之后，关系会出现在这里。</div>';
  const positions=graphNodePositions(nodes);
  const maxWeight=Math.max(1,...(graph.edges||[]).map(edge=>edge.weight));
  const edges=(graph.edges||[]).map(edge=>{
    const from=positions.get(edge.source), to=positions.get(edge.target);
    if(!from || !to) return '';
    // Stop short of the node circle so the arrowhead stays visible.
    const dx=to.x-from.x, dy=to.y-from.y;
    const length=Math.hypot(dx,dy) || 1;
    const endX=to.x-(dx/length)*30, endY=to.y-(dy/length)*30;
    const width=1+(edge.weight/maxWeight)*4;
    const dashed=edge.relations.includes('task')?'':'stroke-dasharray="5 4"';
    return `<line x1="${from.x}" y1="${from.y}" x2="${endX}" y2="${endY}" stroke="#d08a99" stroke-width="${width.toFixed(1)}" ${dashed} marker-end="url(#graph-arrow)"><title>${esc(edge.source)} → ${esc(edge.target)}：${edge.weight} 次（${esc(edge.relations.join('、'))}）</title></line>`;
  }).join('');
  const marks=nodes.map(node=>{
    const point=positions.get(node.id);
    const fill=GRAPH_NODE_FILL[node.type] || '#58746a';
    // The clip path must be declared before the image that references it,
    // otherwise the avatar renders as an unclipped square.
    const image=node.avatar_path
      ? `<clipPath id="graph-clip-${esc(node.id)}"><circle cx="${point.x}" cy="${point.y}" r="22"></circle></clipPath><image href="/${esc(node.avatar_path)}" x="${point.x-22}" y="${point.y-22}" width="44" height="44" preserveAspectRatio="xMidYMid slice" clip-path="url(#graph-clip-${esc(node.id)})"></image>`
      : `<text x="${point.x}" y="${point.y+5}" text-anchor="middle" fill="#fff" font-size="13" font-weight="700">${esc(initials(node.id))}</text>`;
    return `<g class="graph-node" data-graph-node="${esc(node.id)}"><circle cx="${point.x}" cy="${point.y}" r="22" fill="${fill}"></circle>${image}<text x="${point.x}" y="${point.y+40}" text-anchor="middle" font-size="12" fill="#24151a">${esc(node.name)}</text><text x="${point.x}" y="${point.y+56}" text-anchor="middle" font-size="10" fill="#75666b">${node.connections} 个连接</text><title>${esc(node.name)} · ${esc(node.role)}｜派出 ${node.out_degree}，收到 ${node.in_degree}</title></g>`;
  }).join('');
  return `<svg class="graph-svg" viewBox="0 0 ${GRAPH_VIEWBOX.width} ${GRAPH_VIEWBOX.height}" role="img" aria-label="Agent 协作关系图"><defs><marker id="graph-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#c07f8e"></path></marker></defs>${edges}${marks}</svg>`;
}
function renderGraphBoard(){
  const board=$('graph-board');
  const counter=$('tab-edge-count');
  const graph=state.graph;
  if(counter) counter.textContent=graph?.stats?.connections ?? 0;
  if(!board) return;
  if(!graph){ board.innerHTML='<div class="board-empty board-empty-wide">正在加载关系图…</div>'; return; }
  const stats=graph.stats||{};
  const members=(graph.top_members||[]).map(node=>`<div class="graph-rank-row"><span class="graph-dot" style="background:${GRAPH_NODE_FILL[node.type]||'#58746a'}"></span><span>${esc(node.name)}</span><b>${node.connections}</b></div>`).join('') || '<p class="muted">暂无成员。</p>';
  const channels=(graph.channels||[]).map(channel=>`<div class="graph-rank-row"><span>#${esc(channel.name)}</span><b>${channel.message_count} 条</b></div>`).join('') || '<p class="muted">暂无可见频道。</p>';
  board.innerHTML=`<div class="graph-layout"><div class="graph-canvas"><div class="graph-canvas-head"><span>关系 <b>${(graph.edges||[]).length}</b></span><button type="button" id="graph-refresh" class="secondary">刷新</button></div>${graphSvg(graph)}<div class="graph-legend"><span><i class="graph-line-solid"></i>任务派发</span><span><i class="graph-line-dashed"></i>@提及</span><span><i class="graph-dot" style="background:${GRAPH_NODE_FILL.agent}"></i>Agent</span><span><i class="graph-dot" style="background:${GRAPH_NODE_FILL.human}"></i>人类</span></div></div><aside class="graph-side"><div class="graph-stats"><div class="graph-stat"><b>${stats.humans ?? 0}</b><span>人类</span></div><div class="graph-stat"><b>${stats.agents ?? 0}</b><span>AGENT</span></div><div class="graph-stat"><b>${stats.connections ?? 0}</b><span>连接</span></div></div><div class="graph-panel"><h3>连接最多的成员</h3>${members}</div><div class="graph-panel"><h3>最大的频道</h3>${channels}</div></aside></div>`;
  $('graph-refresh')?.addEventListener('click',()=>loadGraph());
  board.querySelectorAll('[data-graph-node]').forEach(group=>group.addEventListener('click',()=>{
    const node=graph.nodes.find(item=>item.id===group.dataset.graphNode);
    if(node?.type==='agent') showAgent(node.id);
  }));
}
async function loadGraph(){
  try{
    const response=await fetch(`/api/graph?channel_id=${encodeURIComponent(state.channelId)}`,{cache:'no-store'});
    if(!response.ok) throw new Error('graph unavailable');
    state.graph=await response.json();
  }catch(_error){ state.graph={nodes:[],edges:[],stats:{}}; }
  renderGraphBoard();
}

// Every file the channel produced or received, whoever created it.
const FILE_SOURCE_LABELS={upload:'用户上传', agent_report:'Agent 报告', agent_intermediate:'Agent 中间文件'};
function formatBytes(size){
  const value=Number(size)||0;
  if(value<1024) return `${value} B`;
  if(value<1024*1024) return `${(value/1024).toFixed(1)} KB`;
  return `${(value/1024/1024).toFixed(1)} MB`;
}
function renderFileBoard(){
  const board=$('file-board');
  const counter=$('tab-file-count');
  if(counter) counter.textContent=state.files.length;
  if(!board) return;
  if(!state.files.length){
    board.innerHTML='<div class="board-empty board-empty-wide">这个频道还没有文件。在聊天框上传，或等 Agent 产出报告后自动同步到这里。</div>';
    return;
  }
  board.innerHTML=`<div class="file-table"><div class="file-row file-head"><span>文件</span><span>来源</span><span>创建者</span><span>大小</span><span>时间</span><span></span></div>${state.files.map(file=>{
    const owner=labels[file.owner_id]||file.owner_id;
    const source=FILE_SOURCE_LABELS[file.source]||file.source;
    return `<div class="file-row"><span class="file-name" title="${esc(file.summary||file.filename)}">${esc(file.filename)}</span><span><em class="file-source file-source-${esc(file.source)}">${esc(source)}</em></span><span>${esc(owner)}</span><span>${esc(formatBytes(file.size_bytes))}</span><span>${esc(new Date(file.created_at).toLocaleString())}</span><span><a class="file-download" href="/api/files/${encodeURIComponent(file.file_id)}/download">下载</a></span></div>`;
  }).join('')}</div>`;
}

// Attachments upload before the message is sent, so the message body and its
// files commit together and a failed upload never produces a dangling chip.
function fileDataUrl(file){
  return new Promise((resolve,reject)=>{
    const reader=new FileReader();
    reader.onload=()=>resolve(String(reader.result));
    reader.onerror=()=>reject(new Error(`${file.name} 读取失败`));
    reader.readAsDataURL(file);
  });
}
function renderAttachmentTray(){
  const tray=$('attachment-tray');
  if(!tray) return;
  tray.hidden=!state.pendingAttachments.length;
  tray.innerHTML=state.pendingAttachments.map(item=>`<span class="attachment-chip">${esc(item.filename)} <small>${esc(formatBytes(item.size_bytes))}</small><button type="button" data-drop-attachment="${esc(item.file_id)}">×</button></span>`).join('');
  tray.querySelectorAll('[data-drop-attachment]').forEach(button=>button.addEventListener('click',()=>{
    state.pendingAttachments=state.pendingAttachments.filter(item=>item.file_id!==button.dataset.dropAttachment);
    renderAttachmentTray();
  }));
}
async function uploadAttachments(fileList){
  for(const file of [...fileList].slice(0,10)){
    if(file.size>20*1024*1024){ toast(`${file.name} 超过 20 MB，未上传`); continue; }
    try{
      const dataUrl=await fileDataUrl(file);
      const response=await fetch(`/api/channels/${encodeURIComponent(state.channelId)}/files`,{
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({filename:file.name, data_url:dataUrl}),
      });
      const result=await response.json();
      if(!response.ok) throw new Error(result.error || '上传失败');
      state.pendingAttachments=[...state.pendingAttachments,result];
    }catch(error){ toast(error.message || `${file.name} 上传失败`); }
  }
  renderAttachmentTray();
  await loadWorkspace(false);
}
function setupAttachments(){
  const input=$('attachment-input');
  if(!input) return;
  input.addEventListener('change',async event=>{
    const files=event.target.files;
    if(files && files.length) await uploadAttachments(files);
    event.target.value='';
  });
}

function renderChannels(){
  const list=$('channel-list');
  if(!list) return;
  // Direct channels are reached from an Agent profile, not the channel list.
  list.innerHTML=state.channels.filter(channel=>channel.kind!=='direct').map(channel=>`<button class="channel ${channel.channel_id===state.channelId?'active':''}" data-channel="${esc(channel.channel_id)}"><span>#</span> ${esc(channel.name)}</button>`).join('');
  list.querySelectorAll('[data-channel]').forEach(button=>button.addEventListener('click',async()=>{
    if(button.dataset.channel===state.channelId) return;
    state.channelId=button.dataset.channel;
    state.selectedThreadMessageId=null;
    state.eventSeq=0;
    const channel=state.channels.find(item=>item.channel_id===state.channelId);
    document.querySelector('.channel-head h1').textContent=`# ${channel?.name || state.channelId}`;
    document.querySelector('.channel-head p').textContent=channel?.topic || '多 Agent 研究协作';
    document.querySelector('.composer-hint').textContent=`发送消息给 #${channel?.name || state.channelId}`;
    renderChannels();
    await loadMessages();
    await loadWorkspace(false);
    connectEvents();
  }));
}

function avatarDataUrl(file){
  if(!file) return Promise.resolve('');
  if(file.size>2*1024*1024) return Promise.reject(new Error('头像不能超过 2 MB'));
  return new Promise((resolve,reject)=>{ const reader=new FileReader(); reader.onload=()=>resolve(String(reader.result)); reader.onerror=()=>reject(new Error('头像读取失败')); reader.readAsDataURL(file); });
}

let editingAgentId=null;
function renderModelOptions(selected){
  const select=$('agent-form')?.elements.model;
  if(!select) return;
  const value=selected || select.value || state.modelSettings.default_model || 'ark-code-latest';
  select.innerHTML=state.modelSettings.models.map(model=>`<option value="${esc(model.id)}">${esc(model.name)} · ${esc(model.id)}${model.status==='retiring'?'（即将下线）':''}</option>`).join('') || '<option value="ark-code-latest">Ark Code Latest · Auto</option>';
  select.value=[...select.options].some(option=>option.value===value)?value:(state.modelSettings.default_model||'ark-code-latest');
  const model=state.modelSettings.models.find(item=>item.id===select.value);
  $('model-description').textContent=model?.description || '默认使用效果与速度双维度智能调度。';
}
function resetAgentEditor(){
  editingAgentId=null;
  const agentForm=$('agent-form');
  agentForm.reset();
  $('agent-dialog-title').textContent='创建 Agent';
  $('agent-dialog-subtitle').textContent='配置一个可以加入频道协作的研究成员';
  $('agent-submit').textContent='创建 Agent';
  renderModelOptions(state.modelSettings.default_model || 'ark-code-latest');
  const preview=$('agent-avatar-preview'); preview.hidden=true; preview.removeAttribute('src');
}
function openAgentEditor(agentId){
  const agent=state.agents.find(item=>item.agent_id===agentId);
  if(!agent || agent.type!=='custom') return;
  editingAgentId=agentId;
  const agentForm=$('agent-form');
  agentForm.elements.name.value=agent.name || '';
  agentForm.elements.profile.value=agent.profile || '';
  agentForm.elements.role.value=agent.role || '';
  agentForm.elements.system_prompt.value=agent.system_prompt || '';
  renderModelOptions(agent.model || state.modelSettings.default_model);
  agentForm.elements.avatar.value='';
  $('agent-dialog-title').textContent='编辑 Agent';
  $('agent-dialog-subtitle').textContent='修改资料、系统提示词，或上传新头像替换原图';
  $('agent-submit').textContent='保存修改';
  const preview=$('agent-avatar-preview');
  if(agent.avatar_path){ preview.src=`/${agent.avatar_path}`; preview.hidden=false; }else{ preview.hidden=true; preview.removeAttribute('src'); }
  $('agent-dialog').showModal();
}

function setupCreationDialogs(){
  const agentDialog=$('agent-dialog'), channelDialog=$('channel-dialog');
  $('create-agent')?.addEventListener('click',()=>{ resetAgentEditor(); agentDialog.showModal(); });
  $('create-channel')?.addEventListener('click',()=>{
    $('channel-agent-options').innerHTML=state.agents.filter(agent=>agent.enabled!==false).map(agent=>`<label><input type="checkbox" name="member_ids" value="${esc(agent.agent_id)}"> <span>${esc(agent.name || agent.agent_id)} · ${esc(agent.role || 'Agent')}</span></label>`).join('');
    channelDialog.showModal();
  });
  document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>{ const dialog=$(button.dataset.close); dialog.close(); if(dialog===agentDialog) resetAgentEditor(); }));
  $('agent-form').elements.avatar.addEventListener('change',async event=>{
    const file=event.target.files?.[0];
    if(!file) return;
    try{ const source=await avatarDataUrl(file); const preview=$('agent-avatar-preview'); preview.src=source; preview.hidden=false; }
    catch(error){ event.target.value=''; toast(error.message || '头像读取失败'); }
  });
  $('agent-form').elements.model.addEventListener('change',event=>renderModelOptions(event.target.value));
  $('agent-form')?.addEventListener('submit',async event=>{
    event.preventDefault();
    const form=new FormData(event.currentTarget);
    try{
      const avatar=await avatarDataUrl(form.get('avatar'));
      const payload={name:form.get('name'),profile:form.get('profile'),role:form.get('role'),system_prompt:form.get('system_prompt'),model:form.get('model'),avatar_data_url:avatar};
      const endpoint=editingAgentId?`/api/agents/${encodeURIComponent(editingAgentId)}`:'/api/agents';
      const request=editingAgentId?{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)};
      const response=await fetch(endpoint,request);
      const result=await response.json();
      if(!response.ok) throw new Error(result.error || '保存失败');
      const wasEditing=Boolean(editingAgentId);
      agentDialog.close(); resetAgentEditor(); await loadWorkspace(false); showAgent(result.agent_id); toast(`Agent「${result.name}」已${wasEditing?'更新':'创建'}`);
    }catch(error){ toast(error.message || '保存 Agent 失败'); }
  });
  $('channel-form')?.addEventListener('submit',async event=>{
    event.preventDefault();
    const form=new FormData(event.currentTarget);
    const payload={name:form.get('name'),topic:form.get('topic'),description:form.get('description'),member_ids:form.getAll('member_ids')};
    const response=await fetch('/api/channels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const result=await response.json();
    if(!response.ok){ toast(result.error || '创建频道失败'); return; }
    channelDialog.close(); event.currentTarget.reset(); state.channelId=result.channel_id; state.eventSeq=0; await loadWorkspace(false); await loadMessages(); renderChannels(); connectEvents(); toast(`研究频道「${result.name}」已创建，根任务等待启动`);
  });
}

document.addEventListener('DOMContentLoaded',setupCreationDialogs);
document.addEventListener('DOMContentLoaded',()=>{ setupPaneTabs(); setupAttachments(); });

// Attachments belong to the message they were sent with, so they render in the
// timeline instead of only appearing in the files tab.
const renderMessagesBeforeAttachments = renderMessages;
renderMessages = function(){
  renderMessagesBeforeAttachments();
  document.querySelectorAll('.message').forEach(article=>{
    const message=state.messages.find(item=>item.message_id===article.dataset.message);
    const attachments=message?.metadata?.attachments || [];
    if(!attachments.length) return;
    const strip=document.createElement('div');
    strip.className='message-attachments';
    strip.innerHTML=attachments.map(item=>{
      const href=`/api/files/${encodeURIComponent(item.file_id)}/download`;
      return String(item.media_type||'').startsWith('image/')
        ? `<a class="attachment-thumb" href="${href}" target="_blank" rel="noreferrer"><img src="${href}" alt="${esc(item.filename)}"><span>${esc(item.filename)}</span></a>`
        : `<a class="attachment-file" href="${href}"><b>${esc(item.filename)}</b><small>${esc(formatBytes(item.size_bytes))}</small></a>`;
    }).join('');
    article.querySelector('.message-main').appendChild(strip);
  });
};
