const SIDEBAR_PREFS_KEY = 'investment-research.sidebar-prefs.v1';
function readSidebarPrefs(){
  try{
    const raw=JSON.parse(localStorage.getItem(SIDEBAR_PREFS_KEY) || '{}');
    return {pinned:Array.isArray(raw.pinned)?raw.pinned.filter(Boolean):[],favorites:Array.isArray(raw.favorites)?raw.favorites.filter(Boolean):[]};
  }catch(_error){ return {pinned:[],favorites:[]}; }
}
function saveSidebarPrefs(){
  try{ localStorage.setItem(SIDEBAR_PREFS_KEY, JSON.stringify(state.sidebarPrefs)); }catch(_error){ /* storage may be disabled */ }
}
const state = { channelId: 'research-room', channels: [], messages: [], agents: [], tasks: [], artifacts: [], files: [], channelTasks: [], channelArtifacts: [], channelFiles: [], run: {}, modelSettings: {default_model:'ark-code-latest',models:[]}, eventSeq: 0, selectedThreadMessageId: null, workspaceView: 'channel', channelTab: 'chat', drawerMode: null, pendingAttachments: [], graph: null, taskFilters: {creator:'', assignee:'', channel:'', view:'board'}, collapsed: {}, fileChannel: '', graphChannel: '', graphSelection: null, removedAgents: [], skills: {}, threads: {}, openComment: null, bridgePort: 18789, agentFilter: 'all', channelMuted: false, sidebarPrefs: readSidebarPrefs() };
const $ = (id) => document.getElementById(id);
const agentColors = { planner:'#C8102E', fundamental:'#A60D28', industry_competition:'#8C3156', market_catalyst:'#C88A18', risk:'#8B2940', reviewer_arbiter:'#6F1630', report_writer:'#B12A46' };
const labels = { planner:'林序', fundamental:'陈实', industry_competition:'周衡', market_catalyst:'沈策', risk:'顾谨', reviewer_arbiter:'韩证', report_writer:'程章', system:'工作台', owner:'你' };
const roles = { planner:'任务规划 Agent', fundamental:'公司基本面 Agent', industry_competition:'行业竞品 Agent', market_catalyst:'市场催化 Agent', risk:'风险 Agent', reviewer_arbiter:'审查仲裁 Agent', report_writer:'报告 Agent' };
// Agents that accept a directly assigned task. Planner is excluded: it owns the
// full flow and is started from the channel, not from a comment.
const DIRECT_AGENT_IDS = new Set(['fundamental','industry_competition','market_catalyst','risk','reviewer_arbiter','report_writer']);
const PERMISSION_LABELS = {ask:'危险操作前询问', accept_edits:'自动接受文件修改', read_only:'只读'};
const CAPABILITY_LABELS = {
  chat:'对话', streaming:'流式输出', shell:'执行命令', file_read:'读取文件',
  file_write:'写入文件', diff:'查看 Diff', mcp:'MCP', skills:'Skills',
  resume_session:'恢复会话', approval:'操作审批',
};
function esc(value){ return String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
// Reference one symbol from the inline Lucide sprite in index.html.
function icon(name, extraClass=''){ return `<svg class="icon ${extraClass}" aria-hidden="true"><use href="#i-${name}"/></svg>`; }
function openContextDrawer(title='上下文'){
  const pane=document.querySelector('.context-pane');
  const scrim=$('drawer-scrim');
  const heading=$('context-title');
  if(heading) heading.innerHTML=`${icon('user')} ${esc(title)}`;
  state.drawerMode=title;
  if(pane){ pane.classList.add('is-open'); pane.setAttribute('aria-hidden','false'); }
  if(scrim) scrim.hidden=false;
}
function closeContextDrawer(){
  const pane=document.querySelector('.context-pane');
  const scrim=$('drawer-scrim');
  if(pane){ pane.classList.remove('is-open'); pane.setAttribute('aria-hidden','true'); }
  if(scrim) scrim.hidden=true;
  state.selectedThreadMessageId=null;
  state.drawerMode=null;
}
function initials(id){ return (labels[id] || id || '?').slice(0,2); }
// The research subject belongs to the open channel, not to a fixed default.
function currentCompany(){
  const channel=state.channels.find(item=>item.channel_id===state.channelId);
  // A direct channel's topic describes the conversation, not a research
  // subject, so it must not be offered as one.
  if(!channel || channel.kind==='direct') return '';
  return (channel.project_company || channel.topic || channel.name || '').trim();
}
function toast(text){ $('toast').textContent=text; $('toast').classList.add('show'); setTimeout(()=>$('toast').classList.remove('show'),2600); }
function kindLabel(kind){ return ({system_message:'工作台状态',user_message:'用户',agent_message:'Agent',task_dispatch:'任务派发',progress_update:'流程状态',task_update:'任务状态',review_issue:'审查 / 返工',artifact_delivery:'成果交付',report_delivery:'报告交付',run_failed:'运行失败'}[kind] || kind); }
function modelOptionsHtml(selected){ return state.modelSettings.models.map(model=>`<option value="${esc(model.id)}" ${model.id===selected?'selected':''}>${esc(model.name)} · ${esc(model.id)}${model.status==='retiring'?'（即将下线）':''}</option>`).join(''); }
function renderAgents(){ $('agent-count').textContent=state.agents.length; $('agent-list').innerHTML=state.agents.map(a=>`<div class="agent-row" data-agent="${esc(a.agent_id)}"><div class="agent-avatar" style="background:${agentColors[a.agent_id] || '#58746a'}">${esc(initials(a.agent_id))}</div><div class="agent-copy"><strong>${esc(a.name || labels[a.agent_id] || a.agent_id)}</strong><span>${esc(a.role || roles[a.agent_id] || 'Agent')}</span></div><i class="status-dot"></i></div>`).join(''); document.querySelectorAll('[data-agent]').forEach(el=>el.addEventListener('click',()=>showAgent(el.dataset.agent))); }
// A message carries its own comment queue. Commenting on what an Agent said
// assigns that Agent a task, so the conversation and the work are one thing —
// there is no separate thread view or task chip to chase.
function renderMessages(){
  const list=$('message-list');
  // A local Agent's transcript lives on the user's machine, so its channel
  // streams into this container instead of replaying stored messages.
  const localAgent=typeof localAgentFor==='function' ? localAgentFor(state.channelId) : null;
  if(localAgent){
    // Keep the streamed transcript across re-renders, but only while it belongs
    // to the Agent on screen — switching Agents starts a fresh one.
    const existing=document.getElementById('local-stream');
    if(!existing || existing.dataset.localAgent!==localAgent.agent_id){
      list.innerHTML=`<div class="local-intro"><div class="local-intro-copy">${icon('monitor')} 与本机 <strong>${esc(localAgent.name)}</strong>（${esc(localAgent.provider)}）对话<small>工作目录：${esc(localAgent.workspace||'')}</small></div><button type="button" class="secondary" id="local-cancel">中止当前任务</button></div><div id="local-stream" class="local-stream" data-local-agent="${esc(localAgent.agent_id)}"></div>`;
      $('local-cancel')?.addEventListener('click',()=>cancelLocalTurn());
    }
    return;
  }
  list.innerHTML=state.messages.map(m=>{
    const name=labels[m.author_id] || m.author_id;
    const body=esc(m.body).replace(/(@[A-Za-z_]+)/g,'<span class="mention">$1</span>');
    const replies=state.threads[m.message_id] || [];
    const open=state.openComment===m.message_id;
    const canAssign=DIRECT_AGENT_IDS.has(m.author_id);
    const summary=replies.length
      ? `${icon('reply')} ${replies.length} 条评论`
      : `${icon('reply')} 评论`;
    const repliesHtml=replies.map(reply=>{
      const queued=reply.metadata?.queue_position;
      const badge=queued?`<span class="queue-badge">队列第 ${esc(queued)} 位</span>`:'';
      return `<div class="comment"><span class="comment-avatar" style="background:${agentColors[reply.author_id] || '#6e7b76'}">${esc(initials(reply.author_id))}</span><div class="comment-main"><div class="comment-top"><strong>${esc(labels[reply.author_id]||reply.author_id)}</strong><time>${esc(new Date(reply.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}))}</time>${badge}</div><div class="comment-body">${esc(reply.body).replace(/(@[A-Za-z_]+)/g,'<span class="mention">$1</span>')}</div></div></div>`;
    }).join('');
    const composer=open
      ? `<form class="comment-composer" data-comment-form="${esc(m.message_id)}"><textarea rows="2" placeholder="${canAssign?`给 ${esc(name)} 派一条任务，它会按顺序处理…`:'写下评论…'}"></textarea><div class="comment-footer"><span>${canAssign?`发送后会给 ${esc(name)} 建一条任务，多条按队列依次执行`:'这条消息的作者不接收任务，评论只作记录'}</span><button type="submit" class="send">${icon('send')} 发送</button></div></form>`
      : '';
    return `<article class="message" data-message="${esc(m.message_id)}"><div class="message-avatar" style="background:${agentColors[m.author_id] || '#6e7b76'}">${esc(initials(m.author_id))}</div><div class="message-main"><div class="message-top"><strong>${esc(name)}</strong><time>${new Date(m.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</time></div><div class="message-body">${body}</div><div class="message-meta"><button class="comment-toggle ${open?'active':''}" data-comment-toggle="${esc(m.message_id)}">${summary}</button></div>${replies.length||open?`<div class="comment-thread">${repliesHtml}${composer}</div>`:''}</div></article>`;
  }).join('');
  // Raft opens message discussions in the right-hand thread drawer. Keeping
  // the main timeline fixed prevents a long reply chain from shifting every
  // later message and keeps the user's reading position stable.
  list.querySelectorAll('[data-comment-toggle]').forEach(el=>el.addEventListener('click',()=>showMessage(el.dataset.commentToggle)));
  list.querySelectorAll('[data-comment-form]').forEach(form=>form.addEventListener('submit',event=>{
    event.preventDefault();
    submitComment(form.dataset.commentForm, form.querySelector('textarea'));
  }));
  list.scrollTop=list.scrollHeight;
}

async function toggleComments(messageId){
  state.openComment=state.openComment===messageId ? null : messageId;
  if(state.openComment) await loadThread(messageId);
  renderMessages();
  document.querySelector(`[data-comment-form="${messageId}"] textarea`)?.focus();
}

async function loadThread(messageId){
  try{
    const response=await fetch(`/api/threads/${encodeURIComponent(messageId)}?channel_id=${encodeURIComponent(state.channelId)}`,{cache:'no-store'});
    if(!response.ok) return;
    const data=await response.json();
    state.threads={...state.threads,[messageId]:data.replies||[]};
  }catch(_error){ /* the comment box still opens; the list simply stays empty */ }
}

async function submitComment(messageId, textarea){
  const body=textarea.value.trim();
  if(!body) return;
  const parent=state.messages.find(item=>item.message_id===messageId);
  // Commenting under an Agent assigns that Agent the work, whoever is asking.
  const target=DIRECT_AGENT_IDS.has(parent?.author_id) ? [parent.author_id] : [];
  textarea.disabled=true;
  const response=await fetch(`/api/channels/${encodeURIComponent(state.channelId)}/messages`,{
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({body, thread_id:messageId, mentions:target,
      as_of_date:state.run?.as_of_date || new Date().toISOString().slice(0,10)}),
  });
  const result=await response.json();
  textarea.disabled=false;
  if(!response.ok){ toast(result.error || '评论失败'); return; }
  textarea.value='';
  await loadThread(messageId);
  await loadWorkspace(false);
  renderMessages();
  toast(result.notice || (result.status==='agents_started'
    ? `已给 ${labels[parent.author_id]||parent.author_id} 排入一条任务`
    : '评论已发送'));
}
function renderRun(){ const r=state.run || {}; const banner=$('run-banner'); if(r.error){ banner.className='run-banner failed'; $('run-label').textContent='本次运行失败，但记录已保留'; $('run-meta').textContent=r.error; } else if(r.running){ banner.className='run-banner running'; $('run-label').textContent='研究流程运行中'; $('run-meta').textContent=`${r.company || ''} · ${r.as_of_date || ''}`; } else if(r.report_available){ const fallback=Boolean(r.summary && r.summary.fallback_used); banner.className='run-banner'; $('run-label').textContent=fallback?'降级报告已交付':'报告已交付'; $('run-meta').textContent=`Run ${r.run_id || ''}`; } else { banner.className='run-banner'; $('run-label').textContent='等待研究任务'; $('run-meta').textContent='演示数据 / 本地真实运行入口'; } }
function renderContext(){ const r=state.run||{}; const current=state.channelTasks.find(t=>t.status==='running') || state.channelTasks[0]; $('context-content').innerHTML=`<div class="context-card"><h2>当前研究运行</h2><p class="muted">${r.running?'正在执行真实 CrewAI/OpenHarness 流程':'@Planner 可启动完整研究；@其他 Agent 可直接派发补充任务'}</p><div class="kv"><span>公司</span><strong>${esc(r.company || currentCompany() || '未指定')}</strong></div><div class="kv"><span>基准日</span><strong>${esc(r.as_of_date || '默认今天')}</strong></div><div class="kv"><span>状态</span><strong>${r.running?'运行中':r.report_available?'已交付':'等待中'}</strong></div>${current?`<h3>最近任务</h3><div class="kv"><span>任务</span><strong>${esc(current.title)}</strong></div><div class="kv"><span>负责人</span><strong>${esc(current.assignee_id)}</strong></div>`:'<p class="muted">当前频道尚未创建任务。</p>'}<h3>两种协作方式</h3><p><strong>@Planner</strong> 会启动七个 Agent 的完整研究闭环；<strong>@Fundamental 等其他 Agent</strong> 会复用频道最近一次 Run 的参数卡和证据，单独执行补充任务。一次 @多个 Agent 时会并行派发。</p>${r.report_available?'<button class="primary" id="context-report">打开 Markdown 报告</button>':''}</div>`; const btn=$('context-report'); if(btn) btn.addEventListener('click',openReport); }
function showAgent(id){
  openContextDrawer('Agent 资料');
  const a=state.agents.find(x=>x.agent_id===id)||{};
  const isCustom=a.type==='custom';
  const model=a.model||state.modelSettings.default_model;
  const avatar=a.avatar_path
    ? `<img class="profile-avatar" src="/${esc(a.avatar_path)}" alt="${esc(a.name||id)}">`
    : `<div class="profile-avatar profile-avatar-fallback" style="background:${agentColors[id] || '#58746a'}">${esc(initials(id))}</div>`;
  // A local Agent runs on the user's machine: its model, prompt and Skills
  // belong to that CLI, so the panel shows where it runs instead of offering
  // platform settings it does not have.
  const isLocal=a.runtime==='local';
  const kindLabel=isLocal?`本地 Agent · ${esc(a.provider||'')}`:(isCustom?'自定义':'系统内置');
  const localRows=isLocal
    ? `<div class="kv"><span>工作目录</span><strong class="kv-path">${esc(a.workspace||'未设置')}</strong></div><div class="kv"><span>Bridge</span><strong>${esc(a.bridge_id||'local')}</strong></div><div class="kv"><span>权限模式</span><strong>${esc(PERMISSION_LABELS[(a.connection_config||{}).permission_mode]||'未设置')}</strong></div>`
    : `<div class="kv"><span>当前模型</span><strong>${esc(model)}</strong></div>`;
  const capabilityRow=isLocal
    ? `<h3>能力</h3><div>${Object.entries(a.capabilities||{}).filter(([,on])=>on).map(([name])=>`<span class="tool-tag">${esc(CAPABILITY_LABELS[name]||name)}</span>`).join('')||'<span class="muted">未声明能力</span>'}</div>`
    : `<h3>工具白名单</h3><div>${(a.allowed_tools||[]).map(t=>`<span class="tool-tag">${esc(t)}</span>`).join('')||'<span class="muted">当前角色无直接工具</span>'}</div>`;
  const modelControl=isLocal
    ? '<p class="muted">模型与提示词由本机上的 Agent 自己管理，平台不参与配置。</p>'
    : `<h3>切换模型</h3><select id="agent-model-select">${modelOptionsHtml(model)}</select>`;
  const actions=isLocal
    ? ''
    : `<div class="context-actions"><button id="save-agent-model" class="primary" type="button">保存模型</button>${isCustom?'<button id="edit-agent" class="secondary" type="button">编辑资料</button>':''}</div>`;
  const skillCard=isLocal
    ? ''
    : `<div class="context-card"><div class="skill-head"><h2>Skill 插件</h2></div><label class="skill-upload-control" title="上传 Skill 插件"><span class="skill-upload-icon">${icon('upload')}</span><span class="skill-upload-copy"><strong>上传 Skill</strong><small>支持 SKILL.md 或插件压缩包</small></span><input id="skill-input" type="file" accept=".md,.markdown,.json,.yaml,.yml,.txt,.zip" hidden></label><p class="muted">上传后会作为该 Agent 的工作方法加载；不会自动执行未审核的代码。</p><div id="skill-list" class="skill-list"><p class="muted">正在加载插件…</p></div></div>`;
  const promptCard=(!isLocal && isCustom)
    ? `<h3>系统提示词</h3><div class="agent-prompt">${esc(a.system_prompt||'')}</div>`
    : '';

  $('context-content').innerHTML=`<div class="context-card"><div class="profile-head">${avatar}<label class="avatar-replace">更换头像<input id="agent-avatar-input" type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden></label></div><h2>${esc(a.name||labels[id]||id)}</h2><p class="muted">${esc(a.role||roles[id]||'Agent')}</p><div class="kv"><span>Agent ID</span><strong>${esc(id)}</strong></div><div class="kv"><span>类型</span><strong>${kindLabel}</strong></div>${localRows}<h3>个人简介</h3><p>${esc(a.profile||'暂无简介')}</p>${promptCard}${capabilityRow}${modelControl}<h3>状态</h3><p><span class="status-dot"></span> ${esc(taskStatusText(a.status))}${isLocal?'':` · ${esc(a.task_phase||'等待频道任务')}`}</p>${actions}</div>${skillCard}`;
  $('edit-agent')?.addEventListener('click',()=>openAgentEditor(id));
  $('save-agent-model')?.addEventListener('click',()=>saveAgentModel(id));
  $('agent-avatar-input')?.addEventListener('change',event=>saveAgentAvatar(id,event));
  // A local Agent's Skills live in its own CLI, so there is no Skill card to
  // wire up here.
  if(!isLocal) setupSkills(id);
}

// Skill plugins are instruction files attached to one Agent. The workbench
// stores and toggles them; it never executes an uploaded file.
async function loadSkills(agentId){
  const list=$('skill-list');
  if(!list) return;
  try{
    const response=await fetch(`/api/agents/${encodeURIComponent(agentId)}/skills`,{cache:'no-store'});
    if(!response.ok) throw new Error('unavailable');
    const skills=await response.json();
    state.skills={...state.skills,[agentId]:skills};
    list.innerHTML=skills.length ? skills.map(skill=>`<div class="skill-row ${skill.enabled?'':'skill-off'}"><span class="skill-glyph">${icon('puzzle')}</span><div class="skill-copy"><strong>${esc(skill.name)}</strong><small>${esc(skill.filename)} · ${esc(formatBytes(skill.size_bytes))}</small>${skill.description?`<p>${esc(skill.description)}</p>`:''}</div><div class="skill-actions"><label class="skill-switch"><input type="checkbox" data-skill-toggle="${esc(skill.skill_id)}" ${skill.enabled?'checked':''}><span>${skill.enabled?'已启用':'已停用'}</span></label><button type="button" class="skill-delete" data-skill-delete="${esc(skill.skill_id)}" title="删除">${icon('trash')}</button></div></div>`).join('')
      : '<p class="muted">还没有插件。上传一个 SKILL.md 试试。</p>';
    list.querySelectorAll('[data-skill-toggle]').forEach(input=>input.addEventListener('change',async()=>{
      const response=await fetch(`/api/skills/${encodeURIComponent(input.dataset.skillToggle)}`,{
        method:'PATCH', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({enabled:input.checked}),
      });
      if(!response.ok){ toast('插件状态更新失败'); }
      await loadSkills(agentId);
    }));
    list.querySelectorAll('[data-skill-delete]').forEach(button=>button.addEventListener('click',async()=>{
      const response=await fetch(`/api/skills/${encodeURIComponent(button.dataset.skillDelete)}`,{method:'DELETE'});
      if(!response.ok){ toast('删除插件失败'); return; }
      await loadSkills(agentId);
      toast('插件已删除');
    }));
  }catch(_error){
    list.innerHTML='<p class="muted">插件列表暂时不可用。</p>';
  }
}
function setupSkills(agentId){
  loadSkills(agentId);
  $('skill-input')?.addEventListener('change',async event=>{
    const file=event.target.files?.[0];
    event.target.value='';
    if(!file) return;
    try{
      const dataUrl=await fileDataUrl(file);
      const response=await fetch(`/api/agents/${encodeURIComponent(agentId)}/skills`,{
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({filename:file.name, data_url:dataUrl}),
      });
      const result=await response.json();
      if(!response.ok) throw new Error(result.error || '上传失败');
      await loadSkills(agentId);
      toast(`插件「${result.name}」已上传`);
    }catch(error){ toast(error.message || '插件上传失败'); }
  });
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

// The per-Agent DM composer lived in the profile pane; commenting under a
// message is now the way to queue an Agent work, so its helpers are gone.
// The 私信 channels themselves remain, opened from the sidebar.
async function saveAgentModel(agentId){
  const model=$('agent-model-select').value;
  const response=await fetch(`/api/agents/${encodeURIComponent(agentId)}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({model})});
  const result=await response.json();
  if(!response.ok){ toast(result.error || '模型切换失败'); return; }
  await loadWorkspace(false); showAgent(agentId); toast(`已将 ${result.name || agentId} 切换为 ${model}`);
}
function showMessage(id){ const m=state.messages.find(x=>x.message_id===id); if(!m)return; $('context-content').innerHTML=`<div class="context-card"><h2>${esc(kindLabel(m.message_kind))}</h2><p>${esc(m.body)}</p><h3>消息信息</h3><div class="kv"><span>作者</span><strong>${esc(labels[m.author_id]||m.author_id)}</strong></div><div class="kv"><span>时间</span><strong>${esc(m.created_at)}</strong></div><div class="kv"><span>关联任务</span><strong>${esc(m.metadata?.task_id||'无')}</strong></div><p class="muted">详细线程能力会在下一阶段加入；当前先把主频道、任务状态和真实运行结果打通。</p></div>`; }
async function loadWorkspace(show=true){
  const res=await fetch(`/api/workspace?channel_id=${encodeURIComponent(state.channelId)}`,{cache:'no-store'});
  const data=await res.json();
  state.channels=data.channels||[]; state.agents=data.agents||[];
  state.tasks=data.tasks||[]; state.artifacts=data.artifacts||[]; state.files=data.files||[];
  state.channelTasks=data.channel_tasks||[];
  state.channelArtifacts=data.channel_artifacts||[];
  state.channelFiles=data.channel_files||[];
  state.removedAgents=data.removed_agents||[];
  if(data.bridge_port) state.bridgePort=data.bridge_port;
  state.run=data.run||{}; state.modelSettings=data.model_settings||state.modelSettings;
  state.eventSeq=Math.max(state.eventSeq,Number(data.event_seq||0));
  renderModelOptions(); renderChannels(); applyChannelHeader(); renderAgents(); renderRun();
  renderTaskBoard(); renderFileBoard(); renderActivityBoard(); renderMembersBoard();
  if(state.workspaceView==='graph') loadGraph();
  if(state.selectedThreadMessageId){ refreshSelectedThread(false); }else{ renderContext(); }
  if(show) $('connection-state').textContent='已连接';
}
function currentDirectAgentId(){
  const channel=state.channels.find(item=>item.channel_id===state.channelId);
  if(channel?.kind!=='direct') return '';
  return String(channel.channel_id || '').replace(/^dm-/, '');
}
async function loadMessages(){ const res=await fetch(`/api/channels/${state.channelId}/messages`); state.messages=await res.json(); renderMessages(); }
async function openReport(){
  const runId=state.run.run_id||'';
  openContextDrawer('研究报告');
  $('context-content').innerHTML='<div class="empty-context"><h2>正在打开报告…</h2><p>报告保留在当前工作台内，不会被浏览器拦截。</p></div>';
  try{
    const res=await fetch(`/api/research/report?run_id=${encodeURIComponent(runId)}`,{cache:'no-store'});
    if(!res.ok) throw new Error('当前还没有报告');
    const text=await res.text();
    $('context-content').innerHTML=`<div class="context-card report-context-card"><div class="thread-title"><div><span class="eyebrow">Markdown</span><h2>${esc(state.run.company||currentCompany()||'研究报告')}</h2></div><button id="report-back" class="secondary">返回概览</button></div><pre class="report-preview">${esc(text)}</pre><div class="context-actions"><a class="primary drawer-download" href="/api/research/report?run_id=${encodeURIComponent(runId)}" download="${esc((state.run.company||'研究')+'报告.md')}">${icon('download')} 下载 Markdown</a></div></div>`;
    $('report-back')?.addEventListener('click',()=>{ openContextDrawer('上下文'); renderContext(); });
  }catch(error){
    $('context-content').innerHTML=`<div class="empty-context"><h2>报告暂时不可用</h2><p>${esc(error.message||'请稍后刷新重试')}</p><button id="report-back" class="secondary">返回概览</button></div>`;
    $('report-back')?.addEventListener('click',()=>{ openContextDrawer('上下文'); renderContext(); });
  }
}
// A "/" at the start of a direct message calls one of that Agent's Skills by
// name. The menu is built from the Skills actually installed on the Agent you
// are talking to, so it can only offer something that exists.
function slugifySkill(name){
  return String(name||'').trim().replace(/[^A-Za-z0-9_\-]+/g,'-').replace(/^-+|-+$/g,'').toLowerCase().slice(0,64);
}
async function skillsForCurrentAgent(){
  const agentId=currentDirectAgentId();
  if(!agentId) return [];
  if(!state.skills[agentId]){
    try{
      const response=await fetch(`/api/agents/${encodeURIComponent(agentId)}/skills`,{cache:'no-store'});
      state.skills={...state.skills,[agentId]:response.ok ? await response.json() : []};
    }catch(_error){ state.skills={...state.skills,[agentId]:[]}; }
  }
  return (state.skills[agentId]||[]).filter(item=>item.enabled);
}
async function renderSkillMenu(input,menu){
  const match=/^\/([^\s]*)$/.exec(input.value);
  if(!match || !currentDirectAgentId()){ return false; }
  const skills=await skillsForCurrentAgent();
  const partial=match[1].toLowerCase();
  const found=skills.filter(item=>slugifySkill(item.name).includes(partial)).slice(0,7);
  menu.innerHTML=found.length
    ? found.map(item=>`<div class="mention-option" data-skill="${esc(slugifySkill(item.name))}"><strong>/${esc(slugifySkill(item.name))}</strong> <span>${esc(item.description||item.name)}</span></div>`).join('')
    : '<div class="mention-empty">这个 Agent 还没有启用的技能。到资料卡里上传一个。</div>';
  menu.style.display='block';
  menu.querySelectorAll('[data-skill]').forEach(el=>el.addEventListener('click',()=>{
    input.value=`/${el.dataset.skill} `;
    menu.style.display='none';
    input.focus();
  }));
  return true;
}
function setupComposer(){
  const input=$('message-input'), menu=$('mention-menu');
  input.addEventListener('input',async()=>{
    const value=input.value;
    if(value.startsWith('/')){
      if(await renderSkillMenu(input,menu)) return;
      menu.style.display='none';
      return;
    }
    if(!value.includes('@')){menu.style.display='none';return;}
    const choices=channelAgentCandidates();
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
    // A local Agent's turn runs on the user's machine, so the same composer
    // sends it to the bridge instead of to this server. The page below is the
    // same chat page either way.
    const localAgent=typeof localAgentFor==='function' ? localAgentFor(state.channelId) : null;
    if(localAgent){
      input.value='';
      await sendToLocalAgent(localAgent, body);
      return;
    }
    // Direct messages must enter the Agent-message endpoint.  Sending them to
    // the generic channel endpoint only persisted the user's words and never
    // gave the target Agent an opportunity to reply.
    const directAgentId=currentDirectAgentId();
    const company=currentCompany();
    const endpoint=directAgentId
      ? `/api/agents/${encodeURIComponent(directAgentId)}/messages`
      : `/api/channels/${encodeURIComponent(state.channelId)}/messages`;
    const payload=directAgentId
      ? {body, attachment_ids:attachmentIds}
      : {body, company, as_of_date:new Date().toISOString().slice(0,10), attachment_ids:attachmentIds};
    const res=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
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
    }else if(directAgentId){
      toast(body.startsWith('/')
        ? `已调用技能 ${body.split(/\s+/)[0]}，${data.notice || 'Agent 正在按技能执行'}`
        : (data.notice || 'Agent 已收到私信'));
    }else{
      toast('消息已发送到频道');
    }
  });
}
// A research run is started by @Planner in the channel, so the old
// start-a-run buttons and their handler are gone with the panels that held them.
document.addEventListener('DOMContentLoaded',async()=>{ await loadWorkspace(); await loadMessages(); setupComposer(); connectEvents(); $('refresh')?.addEventListener('click',()=>{loadWorkspace();loadMessages();}); $('report-btn')?.addEventListener('click',openReport);
  // Local Agents live on the user's machine, so their reachability is checked
  // rather than assumed — but only once the roster is known.
  if(typeof startLocalStatusPolling==='function') startLocalStatusPolling(); });

function showTask(taskId){ const source=state.workspaceView==='channel'?state.channelTasks:state.tasks; const task=source.find(item=>item.task_id===taskId); if(!task){ toast('该任务不属于当前频道'); return; } openContextDrawer('任务详情'); $('context-content').innerHTML=`<div class="context-card"><h2>任务详情</h2><p>${esc(task.title)}</p><div class="kv"><span>任务 ID</span><strong>${esc(task.task_id)}</strong></div><div class="kv"><span>负责人</span><strong>${esc(labels[task.assignee_id]||task.assignee_id)}</strong></div><div class="kv"><span>状态</span><strong>${esc(task.status)}</strong></div><div class="kv"><span>运行 ID</span><strong>${esc(task.run_id||'等待运行')}</strong></div><h3>任务说明</h3><p>这是由频道消息触发的研究任务。任务状态来自本地 SQLite 协作记录，不是静态图片。</p></div>`; }
function showArtifact(artifactId){ const source=state.workspaceView==='channel'?state.channelArtifacts:state.artifacts; const artifact=source.find(item=>item.artifact_id===artifactId); if(!artifact){ toast('该交付物不属于当前频道'); return; } openContextDrawer('交付成果'); $('context-content').innerHTML=`<div class="context-card"><h2>交付成果</h2><p>${esc(artifact.title)}</p><div class="kv"><span>成果 ID</span><strong>${esc(artifact.artifact_id)}</strong></div><div class="kv"><span>提交 Agent</span><strong>${esc(labels[artifact.agent_id]||artifact.agent_id)}</strong></div><div class="kv"><span>状态</span><strong>${esc(artifact.status)}</strong></div><h3>成果摘要</h3><p>${esc(artifact.summary)}</p><h3>关联编号</h3><div>${(artifact.refs||[]).map(item=>`<span class="tool-tag">${esc(item)}</span>`).join('')||'<span class="muted">暂未记录</span>'}</div></div>`; }
function showFile(fileId){
  const sourceFiles=state.workspaceView==='channel'?state.channelFiles:state.files;
  const file=sourceFiles.find(item=>item.file_id===fileId);
  if(!file){ toast('该文件不属于当前频道'); return; }
  const channel=state.channels.find(item=>item.channel_id===file.channel_id);
  const source=FILE_SOURCE_LABELS[file.source]||file.source||'未知来源';
  const owner=labels[file.owner_id]||file.owner_id||'未知';
  openContextDrawer('文件详情');
  $('context-content').innerHTML=`<div class="context-card"><h2>${esc(file.filename)}</h2><p>${esc(file.summary||'暂无文件说明')}</p><div class="kv"><span>文件 ID</span><strong>${esc(file.file_id)}</strong></div><div class="kv"><span>频道</span><strong>${esc(channel?.name||file.channel_id)}</strong></div><div class="kv"><span>来源</span><strong>${esc(source)}</strong></div><div class="kv"><span>创建者</span><strong>${esc(owner)}</strong></div><div class="kv"><span>大小</span><strong>${esc(formatBytes(file.size_bytes))}</strong></div><div class="kv"><span>生成时间</span><strong>${esc(new Date(file.created_at).toLocaleString())}</strong></div><div class="context-actions"><a class="primary drawer-download" href="/api/files/${encodeURIComponent(file.file_id)}/download">${icon('download')} 下载文件</a></div></div>`;
}
// The timeline used to append a task card and an artifact card under every
// message. Both are reachable from the task pane, so the chat stays clean.

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
    // This banner is only ever about the full seven-Agent research run. Saying
    // "waiting for a research task" while a discussion is in full flow read as
    // if the whole channel were idle, so it now names what it is waiting for.
    banner.className='run-banner';
    $('run-label').textContent='未启动完整研究流程';
    $('run-meta').textContent='在频道里 @Planner 可启动；日常讨论和直接任务不需要它';
  }
  if(pause){
    pause.hidden=paused;
    pause.disabled=!r.running;
    pause.setAttribute('aria-disabled',String(!r.running));
  }
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
  // A local Agent lives on the user's machine, so it has states a hosted one
  // never has. They are named rather than collapsed into "offline".
  offline:'本机离线', bridge_offline:'Bridge 未运行', agent_not_found:'未找到该 Agent',
  connecting:'连接中', busy:'忙碌中', error:'连接异常', unknown:'尚未连接',
};
function taskStatusText(status){ return taskStatusLabels[status] || status || '在线'; }
function formatElapsed(seconds){
  const value=Number(seconds);
  if(!Number.isFinite(value) || value <= 0) return '刚刚开始';
  const mins=Math.floor(value/60), secs=value%60;
  return mins ? `${mins}分${String(secs).padStart(2,'0')}秒` : `${secs}秒`;
}
// The roster lives inside the pane-aware sidebar now, so both renderers point
// at the same place instead of writing to a container that no longer exists.
renderAgents = function(){ renderSidebar(); };
// Task state belongs to the task pane. The context pane keeps only what the
// chat itself cannot show: the run and how to work with the Agents.
// Only the facts about the current run. Anything explaining how to use the
// workbench lives where the action is, not in a permanent panel.
renderContext = function(){
  const r=state.run||{};
  const status=r.running?'运行中':r.report_available?'已交付':'等待中';
  $('context-content').innerHTML=`<div class="context-card"><h2>当前研究运行</h2><div class="kv"><span>标的</span><strong>${esc(r.company || currentCompany() || '未指定')}</strong></div><div class="kv"><span>基准日</span><strong>${esc(r.as_of_date || '默认今天')}</strong></div><div class="kv"><span>状态</span><strong>${esc(status)}</strong></div>${r.report_available?'<div class="context-actions"><button class="primary" id="context-report">打开报告</button></div>':''}</div>`;
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
    const threadCandidates=channelAgentCandidates().filter(agent=>agent.agent_id!=='planner');
    $('context-content').innerHTML=`<div class="thread-card"><div class="thread-title"><div><span class="eyebrow">线程交接</span><h2>${esc(kindLabel(data.root.message_kind))}</h2></div><button id="thread-back" class="secondary">返回概览</button></div><section><h3>原始请求</h3>${threadMessageHtml(data.root,data.root.message_id)}</section><section><h3>协作回复（${(data.replies||[]).length}）</h3>${(data.replies||[]).map(item=>threadMessageHtml(item,data.root.message_id)).join('')||'<p class="muted">暂时没有回复。Agent 接收、进度、交付和失败说明都会追加到此处。</p>'}</section><section><h3>关联任务</h3>${taskHtml}</section><section><h3>交付物</h3>${artifactHtml}</section><section><h3>证据编号</h3><div class="thread-refs">${refs.map(ref=>`<span class="tool-tag">${esc(ref)}</span>`).join('')||'<span class="muted">暂无新增 S/F/L/Risk 编号</span>'}</div></section><section class="thread-composer-section"><h3>继续协作</h3><form id="thread-composer" class="thread-composer"><textarea id="thread-input" rows="3" placeholder="例如：@Fundamental 请补充最近一年收入变化的原因"></textarea><div class="thread-agent-quick">${threadCandidates.map(agent=>`<button type="button" data-thread-mention="${esc(agent.agent_id)}">@${esc(agent.agent_id)}</button>`).join('')}</div><div class="thread-composer-footer"><span>线程内 @多个 Agent 会创建独立任务并行执行。</span><button type="submit" class="send">${icon('send')} 发送并派单</button></div></form></section></div>`;
    $('thread-back')?.addEventListener('click',()=>{ state.selectedThreadMessageId=null; openContextDrawer('上下文'); renderContext(); });
    setupThreadComposer(data.root.message_id);
  }catch(_error){
    $('context-content').innerHTML='<div class="empty-context"><h2>线程暂时不可用</h2><p>请刷新页面后重试。研究运行不会因此中断。</p></div>';
  }
}
showMessage = function(id){
  openContextDrawer('线程');
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
    const company=state.run?.company || currentCompany();
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

// A workspace view is controlled only by the left rail. Channel tabs are a
// separate state and never change the active rail button. This removes the old
// inline/full route split that made different pages render different shells.
const GLOBAL_VIEWS=new Set(['search','activity','tasks','members','graph']);
function activeContentPane(){ return state.workspaceView==='channel' ? state.channelTab : state.workspaceView; }
function applyViewState({focus=true}={}){
  const shell=document.querySelector('.app-shell');
  const channelOpen=state.workspaceView==='channel';
  const pane=activeContentPane();
  if(shell){
    shell.dataset.workspaceView=state.workspaceView;
    shell.dataset.channelTab=state.channelTab;
    shell.classList.toggle('no-sidebar',!channelOpen);
  }
  const sidebar=$('sidebar');
  if(sidebar) sidebar.hidden=!channelOpen;
  document.querySelectorAll('.rail-btn[data-view]').forEach(button=>{
    button.classList.toggle('active',button.dataset.view===state.workspaceView);
    button.setAttribute('aria-pressed',String(button.dataset.view===state.workspaceView));
  });
  document.querySelectorAll('.pane-tab[data-channel-tab]').forEach(tab=>{
    const active=channelOpen && tab.dataset.channelTab===state.channelTab;
    tab.classList.toggle('active',active);
    tab.setAttribute('aria-selected',String(active));
  });
  document.querySelectorAll('.workspace-pane').forEach(view=>{ view.hidden=view.dataset.workspacePane!==pane; });
  const channelChrome=[document.querySelector('.channel-head'),$('run-banner'),$('pane-tabs')];
  channelChrome.forEach(element=>{ if(element) element.hidden=!channelOpen; });
  const composer=$('composer');
  if(composer) composer.hidden=!(channelOpen && state.channelTab==='chat');
  if(!channelOpen) closeContextDrawer();
  renderSidebar();
  if(pane==='tasks') renderTaskBoard();
  if(pane==='files') renderFileBoard();
  if(pane==='activity') renderActivityBoard();
  if(pane==='members') renderMembersBoard();
  if(pane==='graph') loadGraph();
  if(pane==='search'){
    runSearch();
    if(focus) requestAnimationFrame(()=>$('search-input')?.focus());
  }
}
function setWorkspaceView(view,options={}){
  if(view==='chat') view='channel';
  if(view!=='channel' && !GLOBAL_VIEWS.has(view)) return;
  state.workspaceView=view;
  const shell=document.querySelector('.app-shell');
  if(shell) shell.dataset.lastNavigation=view;
  applyViewState(options);
}
function setChannelTab(tab){
  if(!['chat','tasks','files'].includes(tab)) return;
  state.workspaceView='channel';
  state.channelTab=tab;
  const shell=document.querySelector('.app-shell');
  if(shell) shell.dataset.lastNavigation=`channel:${tab}`;
  applyViewState({focus:false});
}
function setupNavigation(){
  document.querySelectorAll('.rail-btn[data-view]').forEach(button=>button.addEventListener('click',()=>setWorkspaceView(button.dataset.view)));
  document.querySelectorAll('.pane-tab[data-channel-tab]').forEach(tab=>tab.addEventListener('click',event=>{
    event.stopPropagation();
    setChannelTab(tab.dataset.channelTab);
  }));
  $('global-search')?.addEventListener('click',()=>setWorkspaceView('search'));
  $('open-graph')?.addEventListener('click',()=>setWorkspaceView('graph'));
  $('search-close')?.addEventListener('click',()=>setWorkspaceView('channel',{focus:false}));
  document.addEventListener('keydown',event=>{
    if((event.ctrlKey || event.metaKey) && event.key.toLowerCase()==='k'){
      event.preventDefault();
      setWorkspaceView('search');
    }else if(event.key==='Escape' && state.workspaceView==='search'){
      setWorkspaceView('channel',{focus:false});
    }
  });
  applyViewState({focus:false});
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
function taskDeleteButton(task){
  return `<button type="button" class="task-delete" data-delete-task="${esc(task.task_id)}" title="删除任务">${icon('trash')} 删除</button>`;
}
function taskCardHtml(task){
  const metadata=task.metadata||{};
  const assignee=labels[task.assignee_id]||task.assignee_id||'未指派';
  const elapsed=metadata.elapsed_seconds!=null?`<small>已运行 ${esc(formatElapsed(metadata.elapsed_seconds))}</small>`:'';
  const phase=metadata.phase?`<small>阶段：${esc(metadata.phase)}</small>`:'';
  // An Agent works its queue one job at a time, so a waiting task says where it sits.
  const queued=task.status==='queued' && metadata.queue_position
    ? `<small>队列第 ${esc(metadata.queue_position)} 位${metadata.queue_waiting!=null?`（前面还有 ${esc(metadata.queue_waiting)} 个）`:''}</small>`
    : '';
  const handoff=metadata.handoff_depth
    ? `<small>由 ${esc(labels[task.created_by]||task.created_by)} 转派</small>`
    : '';
  // A task that stopped for a reason says the reason on the card, so nobody
  // has to guess why it is not moving.
  const reason=metadata.reason ? `<small class="board-reason">${esc(metadata.reason)}</small>` : '';
  const answered=metadata.answered_in_discussion ? '<small>已在讨论中回应</small>' : '';
  return `<article class="board-card" data-board-task="${esc(task.task_id)}" data-status="${esc(task.status)}"><div class="board-card-head"><div class="board-card-id">${esc(task.task_id)}</div>${taskDeleteButton(task)}</div><strong>${esc(task.title)}</strong><div class="board-card-foot"><span class="board-assignee">${esc(assignee)}</span><span class="board-status">${esc(taskStatusText(task.status))}</span></div>${phase}${queued}${handoff}${answered}${reason}${elapsed}</article>`;
}
function memberOptions(selected, placeholder, ids){
  return `<option value="">${esc(placeholder)}</option>`+[...new Set(ids)].filter(Boolean).map(id=>`<option value="${esc(id)}" ${id===selected?'selected':''}>${esc(labels[id]||id)}</option>`).join('');
}
function channelOptions(selected, placeholder='全部频道'){
  return `<option value="">${esc(placeholder)}</option>`+state.channels.map(channel=>{
    const label=channel.kind==='direct' ? `与 ${channel.name} 的私信` : `#${channel.name}`;
    return `<option value="${esc(channel.channel_id)}" ${channel.channel_id===selected?'selected':''}>${esc(label)}</option>`;
  }).join('');
}
function currentChannel(){ return state.channels.find(item=>item.channel_id===state.channelId); }
function channelAgentCandidates(){
  const channel=currentChannel();
  const enabled=state.agents.filter(agent=>agent.enabled!==false);
  if(!channel) return [];
  const memberIds=new Set((channel.member_ids||[]).map(String));
  if(channel.kind==='direct'){
    const raw=String(channel.channel_id||'');
    if(raw.startsWith('dm-')) memberIds.add(raw.slice(3));
  }
  const mode=String(channel.channel_mode || (channel.channel_id==='research-room'?'research':'chat'));
  if(mode==='research' && memberIds.size===0) return enabled;
  return enabled.filter(agent=>memberIds.has(String(agent.agent_id)));
}
function currentChannelLabel(){
  const channel=currentChannel();
  if(!channel) return '当前频道';
  return channel.kind==='direct' ? `与 ${channel.name} 的私信` : `#${channel.name}`;
}
function taskSource(){ return state.workspaceView==='channel' ? state.channelTasks : state.tasks; }
function visibleTasks(){
  const {creator, assignee, channel}=state.taskFilters;
  const source=taskSource();
  return source.filter(task=>
    (!creator || task.created_by===creator) &&
    (!assignee || task.assignee_id===assignee) &&
    (state.workspaceView==='channel' || !channel || task.channel_id===channel));
}
function taskListHtml(tasks){
  if(!tasks.length) return '<div class="board-empty board-empty-wide">没有符合条件的任务。</div>';
  return `<div class="task-table"><div class="task-row task-head"><span>任务</span><span>负责人</span><span>创建者</span><span>频道</span><span>状态</span><span>更新时间</span><span>操作</span></div>${tasks.map(task=>{
    const channel=state.channels.find(item=>item.channel_id===task.channel_id);
    return `<div class="task-row" data-board-task="${esc(task.task_id)}" data-status="${esc(task.status)}"><span class="task-row-title"><b>${esc(task.title)}</b><small>${esc(task.task_id)}</small></span><span>${esc(labels[task.assignee_id]||task.assignee_id)}</span><span>${esc(labels[task.created_by]||task.created_by)}</span><span>${esc(channel?.name||task.channel_id)}</span><span><em class="task-status task-status-${esc(taskColumnKey(task.status))}">${esc(taskStatusText(task.status))}</em></span><span>${esc(new Date(task.updated_at).toLocaleString())}</span><span>${taskDeleteButton(task)}</span></div>`;
  }).join('')}</div>`;
}
function renderTaskBoard(){
  const board=$('task-board');
  const counter=$('tab-task-count');
  const source=taskSource();
  const inChannel=state.workspaceView==='channel';
  if(counter) counter.textContent=state.channelTasks.length;
  if(!board) return;
  const tasks=visibleTasks();
  const {creator, assignee, channel, view}=state.taskFilters;
  const channelFilter=inChannel
    ? `<span class="search-filter channel-scope">${icon('hash')} ${esc(currentChannelLabel())}</span>`
    : `<label class="search-filter">${icon('hash')}<select id="task-filter-channel">${channelOptions(channel)}</select></label>`;
  const toolbar=`<div class="task-toolbar"><span class="task-toolbar-title">${icon('tasks')} 任务 <b>${tasks.length}</b> / ${source.length}</span>${channelFilter}<label class="search-filter">${icon('user')}<select id="task-filter-creator">${memberOptions(creator,'全部创建者',source.map(item=>item.created_by))}</select></label><label class="search-filter">${icon('users')}<select id="task-filter-assignee">${memberOptions(assignee,'全部负责人',source.map(item=>item.assignee_id))}</select></label><div class="task-view-toggle"><button type="button" data-task-view="board" class="${view==='board'?'active':''}">${icon('check-square')} 看板</button><button type="button" data-task-view="list" class="${view==='list'?'active':''}">${icon('tasks')} 列表</button></div></div>`;
  const body=view==='list' ? taskListHtml(tasks) : `<div class="board-columns">${TASK_COLUMNS.map(column=>{
    const items=tasks.filter(task=>taskColumnKey(task.status)===column.key);
    const cards=items.map(taskCardHtml).join('') || `<div class="board-empty">没有${esc(column.label)}的任务。</div>`;
    return `<section class="board-column" data-column="${esc(column.key)}"><header><span class="board-chip board-chip-${esc(column.key)}">${esc(column.label)}</span><b>${items.length}</b></header><div class="board-column-body">${cards}</div></section>`;
  }).join('')}</div>`;
  board.innerHTML=toolbar+body;
  board.querySelectorAll('[data-board-task]').forEach(card=>card.addEventListener('click',()=>showTask(card.dataset.boardTask)));
  board.querySelectorAll('[data-delete-task]').forEach(button=>button.addEventListener('click',event=>{
    event.stopPropagation();
    deleteTask(button.dataset.deleteTask);
  }));
  board.querySelectorAll('[data-task-view]').forEach(button=>button.addEventListener('click',()=>{
    state.taskFilters={...state.taskFilters, view:button.dataset.taskView};
    renderTaskBoard();
  }));
  const bind=(id,key)=>$(id)?.addEventListener('change',event=>{
    state.taskFilters={...state.taskFilters, [key]:event.target.value};
    renderTaskBoard();
  });
  if(!inChannel) bind('task-filter-channel','channel');
  bind('task-filter-creator','creator');
  bind('task-filter-assignee','assignee');
}

function renderActivityBoard(){
  const board=$('activity-board');
  if(!board) return;
  const rows=[...state.tasks].sort((a,b)=>new Date(b.updated_at||0)-new Date(a.updated_at||0)).slice(0,30);
  board.innerHTML=`<div class="view-header"><div><span class="eyebrow">工作区</span><h1>${icon('activity')} 动态</h1><p>任务派发、状态变化和交付记录集中显示在这里。</p></div><button class="toolbar-btn" type="button" data-refresh-activity>${icon('refresh')} 刷新</button></div><div class="activity-list">${rows.map(task=>`<article class="activity-row" data-board-task="${esc(task.task_id)}"><span class="activity-icon">${icon('check-square')}</span><div><strong>${esc(task.title)}</strong><p>${esc(labels[task.assignee_id]||task.assignee_id||'未指派')} · ${esc(taskStatusText(task.status))}</p></div><time>${esc(task.updated_at?new Date(task.updated_at).toLocaleString():'')}</time></article>`).join('')||'<div class="empty-state"><h2>暂无动态</h2><p>Agent 开始工作后，状态会显示在这里。</p></div>'}</div>`;
  board.querySelectorAll('[data-board-task]').forEach(row=>row.addEventListener('click',()=>showTask(row.dataset.boardTask)));
  board.querySelector('[data-refresh-activity]')?.addEventListener('click',async()=>{ await loadWorkspace(false); renderActivityBoard(); toast('动态已刷新'); });
}
function renderMembersBoard(){
  const board=$('members-board');
  if(!board) return;
  const people=`<article class="member-card member-card-human"><div class="member-card-avatar">你</div><div><strong>你</strong><p>工作区所有者</p><span>人类成员</span></div><i class="status-dot"></i></article>`;
  const visible=filterAgents(state.agents);
  const agents=visible.map(agent=>{
    const builtIn=agent.type!=='custom' && agent.type!=='local';
    const actionLabel=builtIn?'移出工作区':'删除';
    return `<article class="member-card" data-member-agent="${esc(agent.agent_id)}"><div class="member-card-avatar" style="background:${agentColors[agent.agent_id]||'#8B2940'}">${esc(initials(agent.agent_id))}</div><div class="member-card-copy"><strong>${esc(agent.name||labels[agent.agent_id]||agent.agent_id)}</strong><p>${esc(agent.role||roles[agent.agent_id]||'Agent')}${agent.runtime==='local'?`<span class="runtime-badge">${esc(agent.provider||'local')}</span>`:''}</p><span>${esc(taskStatusText(agent.status||'online'))}</span></div><i class="status-dot"></i><div class="member-card-actions"><button type="button" class="secondary" data-member-dm="${esc(agent.agent_id)}">私信</button><button type="button" class="secondary" data-member-profile="${esc(agent.agent_id)}">资料</button><button type="button" class="secondary danger" data-member-delete="${esc(agent.agent_id)}">${actionLabel}</button></div></article>`;
  }).join('');
  board.innerHTML=`<div class="view-header"><div><span class="eyebrow">工作区</span><h1>${icon('users')} 成员</h1><p>1 位人类成员与 ${visible.length===state.agents.length?state.agents.length:`${visible.length}/${state.agents.length}`} 个 Agent。</p></div><button class="toolbar-btn" type="button" data-create-agent>${icon('plus')} 创建 Agent</button></div><section class="member-section"><h2>人类</h2><div class="member-grid">${people}</div></section><section class="member-section"><h2>Agent</h2>${agentFilterHtml(state.agents)}<div class="member-grid">${agents||'<div class="empty-state"><p>没有这一类的 Agent。</p></div>'}</div></section>`;
  board.querySelectorAll('[data-agent-filter]').forEach(button=>button.addEventListener('click',()=>{
    state.agentFilter=button.dataset.agentFilter;
    renderMembersBoard();
  }));
  board.querySelectorAll('[data-member-agent]').forEach(card=>card.addEventListener('click',()=>showAgent(card.dataset.memberAgent)));
  board.querySelectorAll('[data-member-dm]').forEach(button=>button.addEventListener('click',event=>{ event.stopPropagation(); startDirectMessage(button.dataset.memberDm); }));
  board.querySelectorAll('[data-member-profile]').forEach(button=>button.addEventListener('click',event=>{ event.stopPropagation(); showAgent(button.dataset.memberProfile); }));
  board.querySelectorAll('[data-member-delete]').forEach(button=>button.addEventListener('click',event=>{ event.stopPropagation(); deleteAgent(button.dataset.memberDelete); }));
  board.querySelector('[data-create-agent]')?.addEventListener('click',()=>typeof openAgentKindDialog==='function'?openAgentKindDialog():openAgentDialog());
}

// Workspace search over every record the workbench owns. Filters mirror what a
// person remembers about a hit — who sent it, what kind, which channel, when.
const SEARCH_KIND_META={
  message:{label:'消息', glyph:'message'}, task:{label:'任务', glyph:'tasks'},
  file:{label:'文件', glyph:'file'}, agent:{label:'成员', glyph:'user'},
  channel:{label:'频道', glyph:'hash'},
};
let searchDebounce=null;
function searchFilters(){
  return {
    q: $('search-input')?.value || '',
    scope: $('search-scope')?.value || 'all',
    sender: $('search-sender')?.value || '',
    channel_id: $('search-channel')?.value || '',
    since: $('search-since')?.value || '0',
    sort: $('search-sort')?.value || 'relevance',
  };
}
function renderSearchOptions(data){
  const sender=$('search-sender'), channel=$('search-channel');
  if(sender && sender.options.length<=1){
    sender.innerHTML='<option value="">全部发送者</option>'+(data.senders||[]).map(item=>`<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('');
  }
  if(channel && channel.options.length<=1){
    const publicChannels=(data.channels||[]).filter(item=>item.kind!=='direct');
    channel.innerHTML='<option value="">全部频道</option>'+publicChannels.map(item=>`<option value="${esc(item.id)}">#${esc(item.name)}</option>`).join('');
  }
}
function searchTimeLabel(value){
  if(!value) return '';
  const time=new Date(value);
  if(Number.isNaN(time.getTime())) return '';
  const seconds=Math.max(0,Math.floor((Date.now()-time.getTime())/1000));
  if(seconds<60) return '刚刚';
  if(seconds<3600) return `${Math.floor(seconds/60)} 分钟前`;
  if(seconds<86400) return `${Math.floor(seconds/3600)} 小时前`;
  if(seconds<86400*30) return `${Math.floor(seconds/86400)} 天前`;
  return time.toLocaleDateString();
}
function searchResultHtml(item){
  const meta=SEARCH_KIND_META[item.kind] || {label:item.kind, glyph:'file'};
  const when=searchTimeLabel(item.created_at);
  const where=item.channel_name
    ? (item.channel_kind==='direct' ? `与 ${esc(item.channel_name)} 的私信` : `#${esc(item.channel_name)}`)
    : '工作区';
  const owner=esc(item.owner_name || '—');
  const body=esc(item.body) || '<span class="muted">无正文</span>';
  return `<article class="search-hit search-result-card" data-hit-kind="${esc(item.kind)}" data-hit-ref="${esc(JSON.stringify(item.ref))}" data-hit-channel="${esc(item.channel_id)}"><header class="search-result-card__header"><span class="search-result-card__channel">${where}</span><span class="search-result-card__kind">${icon(meta.glyph)}<span>${esc(meta.label)}</span></span><strong>${owner}</strong>${when?`<time>${esc(when)}</time>`:''}</header><div class="search-result-card__content"><h3>${esc(item.title)}</h3><p>${body}</p></div></article>`;
}
async function runSearch(){
  const board=$('search-results');
  if(!board) return;
  const filters=searchFilters();
  const hasFilters=Boolean(filters.q || filters.sender || filters.channel_id || filters.since!=='0' || filters.scope!=='all' || filters.sort!=='relevance');
  const clearButton=$('search-clear');
  const sortSelect=$('search-sort');
  if(clearButton) clearButton.hidden=!hasFilters;
  if(sortSelect) sortSelect.disabled=!filters.q;
  document.querySelectorAll('.search-filter-component').forEach(label=>{
    const select=label.querySelector('select');
    if(!select) return;
    const isActive=(select.id==='search-scope' && select.value!=='all') ||
      (select.id==='search-sender' && Boolean(select.value)) ||
      (select.id==='search-channel' && Boolean(select.value)) ||
      (select.id==='search-since' && select.value!=='0') ||
      (select.id==='search-sort' && select.value!=='relevance');
    label.classList.toggle('search-filter-component--active',isActive);
  });
  if(!hasFilters){
    board.innerHTML=`<div class="search-empty">${icon('search','search-empty-icon')}<h2>搜索所有内容</h2><p>搜索频道、私信、人类成员、Agent 和消息历史。</p></div>`;
    return;
  }
  try{
    const response=await fetch('/api/search?'+new URLSearchParams(filters),{cache:'no-store'});
    if(!response.ok) throw new Error('search failed');
    const data=await response.json();
    renderSearchOptions(data);
    if(!data.total){
      board.innerHTML=`<div class="search-empty">${icon('search','search-empty-icon')}<h2>${filters.q?'没有匹配的内容':'搜索所有内容'}</h2><p>搜索频道、私信、成员、Agent、任务、文件和消息历史。</p></div>`;
      return;
    }
    const kindOrder=['message','task','file','agent','channel'];
    const groups=kindOrder.map(kind=>[kind,data.results.filter(item=>item.kind===kind)]).filter(([,items])=>items.length);
    const groupHtml=groups.map(([kind,items])=>`<section class="search-result-group" data-result-group="${esc(kind)}"><h2 class="search-result-group-title">${esc((SEARCH_KIND_META[kind]||{}).label||kind)}</h2><div class="search-result-list">${items.map(searchResultHtml).join('')}</div></section>`).join('');
    board.innerHTML=`<div class="search-summary result-summary"><span>共</span><b>${data.total}</b><span>条结果</span></div>${groupHtml}`;
    board.querySelectorAll('.search-hit').forEach(hit=>hit.addEventListener('click',()=>openSearchHit(hit)));
  }catch(_error){
    board.innerHTML='<div class="search-empty"><h2>搜索暂时不可用</h2><p>请刷新页面后重试。</p></div>';
  }
}
async function openSearchHit(element){
  let ref={};
  try{ ref=JSON.parse(element.dataset.hitRef || '{}'); }catch(_error){ ref={}; }
  const channelId=element.dataset.hitChannel;
  const kind=element.dataset.hitKind;
  if(kind==='agent' && ref.agent_id){ setWorkspaceView('channel',{focus:false}); showAgent(ref.agent_id); return; }
  if(channelId && channelId!==state.channelId && kind!=='agent'){
    state.channelId=channelId;
    state.eventSeq=0;
    renderChannels();
    await Promise.all([loadMessages(),loadWorkspace(false)]);
    connectEvents();
  }
  if(kind==='message' && ref.message_id){ setChannelTab('chat'); showMessage(ref.thread_id || ref.message_id); }
  else if(kind==='task' && ref.task_id){ setWorkspaceView('tasks',{focus:false}); showTask(ref.task_id); }
  else if(kind==='file'){ setChannelTab('files'); }
  else { setWorkspaceView('channel',{focus:false}); }
}
function setupSearch(){
  const inputs=['search-input','search-scope','search-sender','search-channel','search-since','search-sort'];
  inputs.forEach(id=>{
    const element=$(id);
    if(!element) return;
    const event=element.tagName==='SELECT' ? 'change' : 'input';
    element.addEventListener(event,()=>{
      clearTimeout(searchDebounce);
      searchDebounce=setTimeout(runSearch, event==='input' ? 220 : 0);
    });
  });
  $('search-clear')?.addEventListener('click',()=>{
    const defaults={
      'search-input':'', 'search-scope':'all', 'search-sender':'',
      'search-channel':'', 'search-since':'0', 'search-sort':'relevance',
    };
    Object.entries(defaults).forEach(([id,value])=>{ const input=$(id); if(input) input.value=value; });
    runSearch();
    $('search-input')?.focus();
  });
}

// Relationship graph: who handed work to whom.  Every edge comes from a real
// record — a task's creator/assignee pair, or a message and the Agents it
// mentioned.  Nodes sit on a circle so the layout is stable between refreshes
// instead of jittering the way a force simulation would.
const GRAPH_NODE_FILL={agent:'#C8102E', human:'#7257a8', system:'#6d8c7c'};
const GRAPH_NODE_RADIUS=22;
let graphSimulation=null;
// d3-force replaces a link's source/target with the node object once the
// simulation starts, so accept either form.
const idOf=(value)=>typeof value==='object' && value!==null ? value.id : value;

// Selecting an entity fills the side column. Everything there is read-only:
// the graph explains the workspace, it does not edit it.
function selectGraphEntity(kind, id){
  state.graphSelection=(state.graphSelection && state.graphSelection.kind===kind && state.graphSelection.id===id)
    ? null : {kind, id};
  renderGraphSide();
}
function graphEntityHtml(){
  const graph=state.graph || {};
  const selection=state.graphSelection;
  if(!selection) return '';
  if(selection.kind==='node'){
    const node=(graph.nodes||[]).find(item=>item.id===selection.id);
    if(!node) return '';
    const rows=[
      ['ID', node.id],
      ['类型', node.type==='agent' ? (node.agent_type==='custom'?'自定义 Agent':'系统内置 Agent') : (node.type==='human'?'人类':'系统')],
      ['职责', node.role],
      ['模型', node.model],
      ['派出 / 收到', `${node.out_degree} / ${node.in_degree}`],
    ].filter(([,value])=>value!=='' && value!=null)
     .map(([label,value])=>`<div class="kv"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('');
    const tools=(node.allowed_tools||[]).map(tool=>`<span class="tool-tag">${esc(tool)}</span>`).join('')
      || '<span class="muted">无直接工具</span>';
    const skills=(node.skills||[]).length
      ? node.skills.map(skill=>`<div class="graph-skill ${skill.enabled?'':'skill-off'}">${icon('puzzle')}<div><strong>${esc(skill.name)}</strong><small>${esc(skill.filename)} · ${skill.enabled?'已启用':'已停用'}</small>${skill.description?`<p>${esc(skill.description)}</p>`:''}</div></div>`).join('')
      : '<p class="muted">没有安装 Skill 插件。</p>';
    const prompt=node.system_prompt
      ? `<h3>系统提示词</h3><div class="agent-prompt">${esc(node.system_prompt)}</div>`
      : '';
    const profile=node.profile ? `<h3>个人简介</h3><p>${esc(node.profile)}</p>` : '';
    return `<div class="graph-panel graph-detail"><div class="graph-detail-head"><h3>实体详情</h3><button type="button" id="graph-detail-close">${icon('x')}</button></div><h2>${esc(node.name)}</h2>${rows}${profile}${prompt}${node.type==='agent'?`<h3>工具白名单</h3><div>${tools}</div><h3>Skill 插件</h3>${skills}`:''}<p class="graph-readonly">关系图只读展示，编辑请到聊天页的 Agent 资料。</p></div>`;
  }
  const edge=(graph.edges||[]).find(item=>`${idOf(item.source)}->${idOf(item.target)}`===selection.id);
  if(!edge) return '';
  const nameOf=id=>((graph.nodes||[]).find(item=>item.id===id)||{}).name || id;
  const tasks=(edge.task_ids||[]).map(taskId=>{
    const task=(graph.tasks||{})[taskId];
    if(!task) return '';
    return `<div class="thread-item"><strong>${esc(task.title)}</strong><span>${esc(task.task_id)} · ${esc(taskStatusText(task.status))}</span><small>负责人：${esc(labels[task.assignee_id]||task.assignee_id)}｜创建：${esc(labels[task.created_by]||task.created_by)}</small></div>`;
  }).join('') || '<p class="muted">这条关系没有任务记录。</p>';
  const messages=(edge.message_ids||[]).map(messageId=>{
    const message=(graph.messages||{})[messageId];
    if(!message) return '';
    return `<div class="thread-item"><strong>${esc(kindLabel(message.message_kind))}</strong><p>${esc(message.body)}</p><small>${esc(new Date(message.created_at).toLocaleString())}</small></div>`;
  }).join('') || '<p class="muted">这条关系没有 @提及记录。</p>';
  return `<div class="graph-panel graph-detail"><div class="graph-detail-head"><h3>关系详情</h3><button type="button" id="graph-detail-close">${icon('x')}</button></div><h2>${esc(nameOf(idOf(edge.source)))} → ${esc(nameOf(idOf(edge.target)))}</h2><div class="kv"><span>累计</span><strong>${edge.weight} 次</strong></div><div class="kv"><span>关系类型</span><strong>${esc(edge.relations.map(item=>item==='task'?'任务派发':'@提及').join('、'))}</strong></div><h3>任务（${(edge.task_ids||[]).length}）</h3>${tasks}<h3>@提及（${(edge.message_ids||[]).length}）</h3>${messages}</div>`;
}
function renderGraphSide(){
  const side=$('graph-side');
  if(!side) return;
  const graph=state.graph||{};
  const detail=graphEntityHtml();
  const members=(graph.top_members||[]).map(node=>`<div class="graph-rank-row"><span class="graph-dot" style="background:${GRAPH_NODE_FILL[node.type]||'#58746a'}"></span><span>${esc(node.name)}</span><b>${node.connections}</b></div>`).join('') || '<p class="muted">暂无成员。</p>';
  const channels=(graph.channels||[]).map(channel=>`<div class="graph-rank-row"><span>#${esc(channel.name)}</span><b>${channel.message_count} 条</b></div>`).join('') || '<p class="muted">暂无可见频道。</p>';
  side.innerHTML=detail || `<div class="graph-panel"><h3>连接最多的成员</h3>${members}</div><div class="graph-panel"><h3>最大的频道</h3>${channels}</div><p class="graph-readonly">点击结点查看实体资料，点击连线查看它们之间的任务。</p>`;
  $('graph-detail-close')?.addEventListener('click',()=>{ state.graphSelection=null; renderGraphSide(); });
}

// D3 owns the layout: a force simulation settles the nodes, and each one can be
// dragged and pinned. Re-rendering tears the previous simulation down first, or
// two of them would fight over the same nodes.
function mountForceGraph(container, graph){
  if(graphSimulation){ graphSimulation.stop(); graphSimulation=null; }
  const nodes=(graph.nodes||[]).map(node=>({...node}));
  const byId=new Map(nodes.map(node=>[node.id,node]));
  const links=(graph.edges||[])
    .filter(edge=>byId.has(edge.source) && byId.has(edge.target))
    .map(edge=>({...edge}));
  if(!nodes.length){
    container.innerHTML='<div class="board-empty board-empty-wide">这个范围还没有产生协作记录。派一个任务或 @一个 Agent 之后，关系会出现在这里。</div>';
    return;
  }
  const width=container.clientWidth || 900;
  const height=container.clientHeight || 560;
  const maxWeight=Math.max(1,...links.map(link=>link.weight));

  const svg=d3.select(container).append('svg')
    .attr('class','graph-svg')
    .attr('viewBox',[0,0,width,height])
    .attr('role','img')
    .attr('aria-label','Agent 协作关系图');

  const defs=svg.append('defs');
  defs.append('marker')
    .attr('id','graph-arrow').attr('viewBox','0 0 10 10')
    .attr('refX',GRAPH_NODE_RADIUS+9).attr('refY',5)
    .attr('markerWidth',6).attr('markerHeight',6).attr('orient','auto-start-reverse')
    .append('path').attr('d','M 0 0 L 10 5 L 0 10 z').attr('fill','#c07f8e');
  // One clip path per avatar keeps the image inside its circle.
  nodes.filter(node=>node.avatar_path).forEach(node=>{
    defs.append('clipPath').attr('id',`clip-${node.id}`)
      .append('circle').attr('r',GRAPH_NODE_RADIUS);
  });

  const viewport=svg.append('g');
  svg.call(d3.zoom().scaleExtent([0.3,3]).on('zoom',event=>viewport.attr('transform',event.transform)));

  const link=viewport.append('g').selectAll('line').data(links).join('line')
    .attr('class','graph-link')
    .attr('stroke','#d08a99')
    .attr('stroke-width',d=>1+(d.weight/maxWeight)*4)
    .attr('stroke-dasharray',d=>d.relations.includes('task')?null:'5 4')
    .attr('marker-end','url(#graph-arrow)')
    // A thin line is hard to hit, so widen only the clickable area.
    .style('stroke-linecap','round')
    .on('click',(event,d)=>{
      event.stopPropagation();
      selectGraphEntity('edge',`${idOf(d.source)}->${idOf(d.target)}`);
    });
  link.append('title').text(d=>`${idOf(d.source)} → ${idOf(d.target)}：${d.weight} 次（${d.relations.join('、')}）`);

  const node=viewport.append('g').selectAll('g').data(nodes).join('g')
    .attr('class','graph-node')
    .call(d3.drag()
      .on('start',(event,d)=>{
        if(!event.active) graphSimulation.alphaTarget(0.3).restart();
        d.fx=d.x; d.fy=d.y;
      })
      .on('drag',(event,d)=>{ d.fx=event.x; d.fy=event.y; })
      .on('end',(event,d)=>{
        if(!event.active) graphSimulation.alphaTarget(0);
        // Keep the node where it was dropped; double-click releases it.
        d.fx=event.x; d.fy=event.y;
      }))
    .on('click',(event,d)=>{ event.stopPropagation(); selectGraphEntity('node',d.id); })
    .on('dblclick',(event,d)=>{ d.fx=null; d.fy=null; graphSimulation.alpha(0.3).restart(); });

  node.append('circle')
    .attr('r',GRAPH_NODE_RADIUS)
    // A removed Agent keeps its node because its past work is a real record,
    // but it is greyed so it does not read as an active member.
    .attr('fill',d=>d.removed ? '#b9a7ad' : (GRAPH_NODE_FILL[d.type] || '#58746a'))
    .attr('stroke',d=>d.removed ? '#8d7b81' : null)
    .attr('stroke-dasharray',d=>d.removed ? '3 3' : null);
  node.filter(d=>d.avatar_path).append('image')
    .attr('href',d=>`/${d.avatar_path}`)
    .attr('x',-GRAPH_NODE_RADIUS).attr('y',-GRAPH_NODE_RADIUS)
    .attr('width',GRAPH_NODE_RADIUS*2).attr('height',GRAPH_NODE_RADIUS*2)
    .attr('preserveAspectRatio','xMidYMid slice')
    .attr('clip-path',d=>`url(#clip-${d.id})`);
  node.filter(d=>!d.avatar_path).append('text')
    .attr('text-anchor','middle').attr('dy',5)
    .attr('fill','#fff').attr('font-size',13).attr('font-weight',700)
    .text(d=>initials(d.id));
  node.append('text')
    .attr('text-anchor','middle').attr('dy',GRAPH_NODE_RADIUS+18)
    .attr('font-size',12)
    .attr('fill',d=>d.removed ? '#8d7b81' : '#24151a')
    .text(d=>d.removed ? `${d.name}（已移除）` : d.name);
  node.append('text')
    .attr('text-anchor','middle').attr('dy',GRAPH_NODE_RADIUS+34)
    .attr('font-size',10).attr('fill','#75666b').text(d=>`${d.connections} 个连接`);
  node.append('title').text(d=>`${d.name} · ${d.role}｜派出 ${d.out_degree}，收到 ${d.in_degree}`);

  graphSimulation=d3.forceSimulation(nodes)
    .force('link',d3.forceLink(links).id(d=>d.id).distance(150).strength(0.35))
    .force('charge',d3.forceManyBody().strength(-620))
    .force('center',d3.forceCenter(width/2,height/2))
    .force('collide',d3.forceCollide(GRAPH_NODE_RADIUS+26))
    .on('tick',()=>{
      link.attr('x1',d=>d.source.x).attr('y1',d=>d.source.y)
          .attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);
      node.attr('transform',d=>`translate(${d.x},${d.y})`);
    });
}

function renderGraphBoard(){
  const board=$('graph-board');
  const counter=$('tab-edge-count');
  const graph=state.graph;
  if(counter) counter.textContent=graph?.stats?.connections ?? 0;
  if(!board) return;
  if(!graph){ board.innerHTML='<div class="board-empty board-empty-wide">正在加载关系图…</div>'; return; }
  const stats=graph.stats||{};
  const scope=state.graphChannel;
  // Direct messages are excluded: a 1:1 conversation has no collaboration
  // structure to draw, so scoping the graph to one is never informative.
  const scopable=state.channels.filter(channel=>channel.kind!=='direct');
  const options=[{channel_id:'',name:'全部频道'},...scopable].map(channel=>
    `<option value="${esc(channel.channel_id)}" ${channel.channel_id===scope?'selected':''}>${channel.channel_id?'#':''}${esc(channel.name)}</option>`
  ).join('');
  board.innerHTML=`<div class="graph-toolbar"><span class="graph-title">${icon('graph')} 关系图 <em>EXPERIMENTAL</em></span><label class="search-filter">${icon('hash')}<select id="graph-channel">${options}</select></label><span class="graph-inline-stats"><b>${stats.humans ?? 0}</b> 人类 <b>${stats.agents ?? 0}</b> AGENT <b>${stats.connections ?? 0}</b> 连接</span><button type="button" id="graph-refresh" class="secondary">${icon('refresh')} 刷新</button></div><div class="graph-layout"><div class="graph-canvas"><div id="graph-canvas-host" class="graph-canvas-host"></div><div class="graph-legend"><span><i class="graph-line-solid"></i>任务派发</span><span><i class="graph-line-dashed"></i>@提及</span><span><i class="graph-dot" style="background:${GRAPH_NODE_FILL.agent}"></i>Agent</span><span><i class="graph-dot" style="background:${GRAPH_NODE_FILL.human}"></i>人类</span><span class="graph-hint">点击结点看资料，点击连线看任务；拖动固定，双击释放，滚轮缩放</span></div></div><aside class="graph-side" id="graph-side"></aside></div>`;

  $('graph-refresh')?.addEventListener('click',()=>loadGraph());
  $('graph-channel')?.addEventListener('change',event=>{
    state.graphChannel=event.target.value;
    state.graphSelection=null;
    loadGraph();
  });
  renderGraphSide();
  mountForceGraph($('graph-canvas-host'), graph);
}
async function loadGraph(){
  try{
    const query=state.graphChannel ? `?channel_id=${encodeURIComponent(state.graphChannel)}` : '';
    const response=await fetch(`/api/graph${query}`,{cache:'no-store'});
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
  if(counter) counter.textContent=state.channelFiles.length;
  if(!board) return;
  // Files under a channel tab are always scoped to that channel.  The global
  // search still has access to workspace files through its own endpoint.
  const files=state.channelFiles;
  const heading=`<div class="task-toolbar"><span class="task-toolbar-title">${icon('paperclip')} 文件 <b>${files.length}</b> / ${files.length}</span><span class="search-filter channel-scope">${icon('hash')} ${esc(currentChannelLabel())}（${files.length}）</span><button type="button" id="file-refresh" class="secondary">${icon('refresh')} 刷新</button></div>`;
  const bindToolbar=()=>{
    $('file-refresh')?.addEventListener('click',()=>loadWorkspace(false));
  };
  if(!files.length){
    board.innerHTML=heading+'<div class="board-empty board-empty-wide">这里还没有文件。在聊天框上传，或等 Agent 产出报告后自动同步到这里。</div>';
    bindToolbar();
    return;
  }
  board.innerHTML=heading+`<div class="file-table"><div class="file-row file-head"><span>文件</span><span>频道</span><span>来源</span><span>创建者</span><span>大小</span><span>时间</span><span></span></div>${files.map(file=>{
    const owner=labels[file.owner_id]||file.owner_id;
    const source=FILE_SOURCE_LABELS[file.source]||file.source;
    const glyph=String(file.media_type||'').startsWith('image/')?'image':'file';
    const channel=state.channels.find(item=>item.channel_id===file.channel_id);
    return `<div class="file-row" role="button" tabindex="0" data-file-id="${esc(file.file_id)}"><span class="file-name" title="${esc(file.summary||file.filename)}">${icon(glyph,'file-glyph')} ${esc(file.filename)}</span><span class="file-channel">${esc(channel?.name||file.channel_id)}</span><span><em class="file-source file-source-${esc(file.source)}">${esc(source)}</em></span><span>${esc(owner)}</span><span>${esc(formatBytes(file.size_bytes))}</span><span>${esc(new Date(file.created_at).toLocaleString())}</span><span class="file-actions"><a class="file-download" href="/api/files/${encodeURIComponent(file.file_id)}/download">${icon('download')} 下载</a><button type="button" class="file-delete" data-delete-file="${esc(file.file_id)}" title="删除">${icon('trash')}</button></span></div>`;
  }).join('')}</div>`;
  bindToolbar();
  board.querySelectorAll('[data-file-id]').forEach(row=>{
    const open=()=>showFile(row.dataset.fileId);
    row.addEventListener('click',event=>{ if(!event.target.closest('.file-actions')) open(); });
    row.addEventListener('keydown',event=>{ if(event.key==='Enter'||event.key===' '){ event.preventDefault(); open(); } });
  });
  board.querySelectorAll('[data-delete-file]').forEach(button=>button.addEventListener('click',event=>{
    event.stopPropagation();
    deleteFile(button.dataset.deleteFile);
  }));
}

async function deleteFile(fileId){
  const file=state.channelFiles.find(item=>item.file_id===fileId) ||
    state.files.find(item=>item.file_id===fileId);
  const name=file?.filename || fileId;
  // A file an Agent produced is part of the record of a run, so say which kind
  // is being removed rather than asking one generic question.
  const produced=file && file.source!=='upload';
  if(!window.confirm(produced
    ? `确定删除「${name}」吗？这是 Agent 产出的文件，删除后无法恢复。`
    : `确定删除「${name}」吗？此操作无法恢复。`)) return;
  const response=await fetch(`/api/files/${encodeURIComponent(fileId)}`,{method:'DELETE'});
  const result=await response.json().catch(()=>({}));
  if(!response.ok){ toast(result.error || '删除文件失败'); return; }
  await loadWorkspace(false);
  await loadMessages();
  renderFileBoard();
  toast('文件已删除');
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

// The channel workspace owns the only sidebar. Global rail views use the full
// centre width, so every page shares one predictable shell.
function sectionHtml(key, label, count, body, action=''){
  const collapsed=state.collapsed[key];
  return `<div class="sidebar-section" data-section="${esc(key)}"><div class="section-title"><button class="section-toggle" type="button" data-toggle-section="${esc(key)}">${icon('chevron',collapsed?'chevron-collapsed':'')} ${esc(label)}</button><span>${count!=null?`<b>${count}</b>`:''}${action}</span></div><div class="section-body" ${collapsed?'hidden':''}>${body}</div></div>`;
}
// Row actions stay hidden until hover so the list reads cleanly, and are real
// buttons rather than a context menu so they are reachable by keyboard.
function rowActionsHtml(kind, id, {edit=true}={}){
  return `<span class="row-actions">${edit?`<button type="button" class="row-action" data-edit-${kind}="${esc(id)}" title="编辑">${icon('pencil')}</button>`:''}<button type="button" class="row-action row-danger" data-delete-${kind}="${esc(id)}" title="删除">${icon('trash')}</button></span>`;
}
function sidebarItemId(channelOrAgent){ return channelOrAgent.channel_id || `dm-${channelOrAgent.agent_id}`; }
function sidebarPrefHas(kind,id){ return (state.sidebarPrefs[kind] || []).includes(id); }
function sidebarPrefToggle(kind,id){
  const values=new Set(state.sidebarPrefs[kind] || []);
  if(values.has(id)) values.delete(id); else values.add(id);
  state.sidebarPrefs={...state.sidebarPrefs,[kind]:[...values]};
  saveSidebarPrefs();
}
function sidebarItemActions(itemId, {deleteKind='', deleteId=''}={}){
  const favorite=sidebarPrefHas('favorites',itemId);
  const pinned=sidebarPrefHas('pinned',itemId);
  return `<span class="row-actions"><button type="button" class="row-action sidebar-pref-action ${favorite?'is-active':''}" data-toggle-favorite="${esc(itemId)}" title="${favorite?'取消收藏':'收藏'}">${icon('bookmark')}</button><button type="button" class="row-action sidebar-pref-action ${pinned?'is-active':''}" data-toggle-pin="${esc(itemId)}" title="${pinned?'取消置顶':'置顶'}">${icon('pin')}</button>${deleteKind?`<button type="button" class="row-action row-danger" data-delete-${deleteKind}="${esc(deleteId)}" title="删除">${icon('trash')}</button>`:''}</span>`;
}
function sidebarItemClass(itemId){ return `sidebar-item ${itemId===state.channelId?'active':''}`; }
function channelButtonHtml(channel){
  const itemId=sidebarItemId(channel);
  return `<div class="row-wrap ${sidebarItemClass(itemId)}" draggable="true" data-sidebar-item="${esc(itemId)}" data-item-type="channel" data-item-id="${esc(itemId)}"><button class="channel" data-channel="${esc(channel.channel_id)}">${icon('hash','channel-icon')} ${esc(channel.name)}</button>${sidebarItemActions(itemId,{deleteKind:'channel',deleteId:channel.channel_id})}</div>`;
}
function dmAvatarHtml(agent,agentId){
  const avatar=agent?.avatar_path?`<img src="/${esc(agent.avatar_path)}" alt="">`:esc(initials(agentId));
  return `<span class="dm-avatar" style="background:${agentColors[agentId] || '#58746a'}">${avatar}</span>`;
}
// A row under 私信 is a member, so deleting it removes the Agent rather than
// just the conversation. Deleting only the channel used to look like nothing
// happened: the Agent came straight back as a member without a conversation.
function dmButtonHtml(channel){
  const agentId=channel.channel_id.replace(/^dm-/,'');
  const agent=state.agents.find(item=>item.agent_id===agentId);
  const role=agent?.role || roles[agentId] || '';
  const itemId=sidebarItemId(channel);
  return `<div class="row-wrap ${sidebarItemClass(itemId)}" draggable="true" data-sidebar-item="${esc(itemId)}" data-item-type="direct" data-item-id="${esc(itemId)}"><button type="button" class="dm-profile-target" data-profile-agent="${esc(agentId)}" title="查看 ${esc(channel.name)} 资料">${dmAvatarHtml(agent,agentId)}</button><button type="button" class="channel dm-channel" data-dm-agent="${esc(agentId)}" data-status="${esc(agent?.status || 'online')}" data-runtime="${esc(agent?.runtime || 'hosted')}" title="打开与 ${esc(channel.name)} 的私信"><span class="dm-copy"><b>${esc(channel.name)}</b>${role?`<em>${esc(role)}</em>`:''}</span><i class="status-dot"></i></button>${sidebarItemActions(itemId,{deleteKind:'agent',deleteId:agentId})}</div>`;
}
function agentRowHtml(agent){ return dmAgentRowHtml(agent); }
function dmAgentRowHtml(agent){
  const status=agent.status || 'online';
  const detail=status==='running' && agent.task_phase ? agent.task_phase : taskStatusText(status);
  const name=agent.name || labels[agent.agent_id] || agent.agent_id;
  const role=agent.role || roles[agent.agent_id] || 'Agent';
  const itemId=sidebarItemId(agent);
  const localBadge=agent.runtime==='local'
    ? `<span class="runtime-badge">${esc(agent.provider||'local')}</span>` : '';
  return `<div class="row-wrap ${sidebarItemClass(itemId)}" draggable="true" data-sidebar-item="${esc(itemId)}" data-item-type="direct" data-item-id="${esc(itemId)}"><button type="button" class="dm-profile-target" data-profile-agent="${esc(agent.agent_id)}" title="查看 ${esc(name)} 资料">${dmAvatarHtml(agent,agent.agent_id)}</button><button type="button" class="channel dm-channel" data-dm-agent="${esc(agent.agent_id)}" data-status="${esc(status)}" data-runtime="${esc(agent.runtime||'hosted')}" title="打开与 ${esc(name)} 的私信"><span class="dm-copy"><b>${esc(name)}</b><em>${esc(role)}${localBadge}</em><small>${esc(detail)}</small></span><i class="status-dot"></i></button>${sidebarItemActions(itemId,{deleteKind:'agent',deleteId:agent.agent_id})}</div>`;
}
// Hosted and local Agents share one roster — the filter narrows it rather than
// splitting them into separate lists, so "how many Agents do I have" has one
// answer. It only appears once both kinds exist; before that it is noise.
const AGENT_FILTERS=[['all','全部'],['hosted','普通'],['local','本地']];
function agentFilterHtml(agents){
  if(!agents.some(agent=>agent.runtime==='local')) return '';
  const current=state.agentFilter || 'all';
  return `<div class="roster-filter">${AGENT_FILTERS.map(([key,label])=>
    `<button type="button" class="filter-chip${key===current?' active':''}" data-agent-filter="${key}">${esc(label)}</button>`).join('')}</div>`;
}
function filterAgents(agents){
  const current=state.agentFilter || 'all';
  if(current==='all') return agents;
  return agents.filter(agent=>(agent.runtime||'hosted')===current);
}
function renderSidebar(){
  const sidebar=$('sidebar');
  const channelOpen=state.workspaceView==='channel';
  if(sidebar) sidebar.hidden=!channelOpen;
  if(!channelOpen || !sidebar) return;
  $('sidebar-title').textContent='聊天';
  $('sidebar-subtitle').textContent='本地工作区';
  const action=$('sidebar-action');
  if(action){ action.hidden=false; action.title='新建私信'; }

  const projects=state.channels.filter(channel=>channel.kind!=='direct');
  const directs=state.channels.filter(channel=>channel.kind==='direct');
  const body=$('sidebar-body');
  const plus=id=>`<button id="${id}" class="section-add" type="button">${icon('plus')}</button>`;

  // A removed built-in Agent stays listed so the removal is not a one-way door.
  const removed=state.removedAgents || [];
  const removedRows=removed.map(agent=>`<div class="row-wrap removed-row"><div class="agent-row"><div class="agent-avatar" style="background:#b9a7ad">${esc(initials(agent.agent_id))}</div><div class="agent-copy"><strong>${esc(agent.name||agent.agent_id)}</strong><span class="agent-role-label">${esc(agent.role||'Agent')}</span></div></div><span class="row-actions row-actions-static"><button type="button" class="row-action" data-restore-agent="${esc(agent.agent_id)}" title="恢复">${icon('refresh')}</button></span></div>`).join('');
  const itemById=new Map([
    ...projects.map(item=>[sidebarItemId(item),item]),
    ...directs.map(item=>[sidebarItemId(item),item]),
    ...state.agents.map(item=>[sidebarItemId(item),item]),
  ]);
  const validIds=new Set(itemById.keys());
  const cleanPrefs={
    pinned:(state.sidebarPrefs.pinned||[]).filter(id=>validIds.has(id)),
    favorites:(state.sidebarPrefs.favorites||[]).filter(id=>validIds.has(id)),
  };
  if(JSON.stringify(cleanPrefs)!==JSON.stringify(state.sidebarPrefs)){
    state.sidebarPrefs=cleanPrefs;
    saveSidebarPrefs();
  }
  const pinnedIds=new Set(cleanPrefs.pinned);
  const visibleProjects=projects.filter(item=>!pinnedIds.has(sidebarItemId(item)));
  const visibleDirects=directs.filter(item=>!pinnedIds.has(sidebarItemId(item)));
  // 私信 lists conversations, not the roster. Padding it with every Agent that
  // had no conversation yet made it read as "the members of this channel",
  // which a direct message has nothing to do with: a DM is its own session
  // with one Agent, independent of any channel. New ones start from 成员 or
  // the + above.
  const directoryRows=visibleDirects.map(dmButtonHtml).join('');
  const renderItem=id=>{
    const item=itemById.get(id);
    if(!item) return '';
    return item.kind==='direct' ? dmButtonHtml(item) : item.channel_id ? channelButtonHtml(item) : dmAgentRowHtml(item);
  };
  const favoriteRows=cleanPrefs.favorites.map(renderItem).join('');
  const pinnedRows=cleanPrefs.pinned.map(renderItem).join('');
  const pinnedBody=`<div class="pinned-dropzone" data-pinned-drop><p class="sidebar-hint">${pinnedRows?'继续拖动频道或私信到这里置顶':'将频道或私信拖到这里置顶'}</p>${pinnedRows}</div>`;
  body.innerHTML=
    sectionHtml('favorites','已收藏',cleanPrefs.favorites.length,favoriteRows || '<p class="sidebar-hint">收藏的频道和私信会显示在这里。</p>')+
    sectionHtml('pinned','已置顶',cleanPrefs.pinned.length,pinnedBody)+
    sectionHtml('channels','频道',visibleProjects.length,visibleProjects.map(channelButtonHtml).join('') || '<p class="sidebar-hint">没有未置顶的频道。</p>',plus('create-channel'))+
    sectionHtml('dms','私信',visibleDirects.length,directoryRows || '<p class="sidebar-hint">还没有私信。点右上角 + 或到「成员」里找一个 Agent 单独聊。</p>',plus('create-dm'))+
    (removed.length ? sectionHtml('removedAgents','已移除',removed.length,removedRows) : '');
  bindSidebar();
}
function bindSidebar(){
  document.querySelectorAll('[data-toggle-section]').forEach(button=>button.addEventListener('click',()=>{
    const key=button.dataset.toggleSection;
    state.collapsed={...state.collapsed, [key]:!state.collapsed[key]};
    renderSidebar();
  }));
  document.querySelectorAll('[data-agent]').forEach(el=>el.addEventListener('click',()=>{
    if(el.dataset.agent!=='owner') showAgent(el.dataset.agent);
  }));
  document.querySelectorAll('[data-profile-agent]').forEach(button=>button.addEventListener('click',event=>{
    event.stopPropagation();
    showAgent(button.dataset.profileAgent);
  }));
  document.querySelectorAll('[data-dm-agent]').forEach(button=>button.addEventListener('click',async event=>{
    event.stopPropagation();
    await startDirectMessage(button.dataset.dmAgent);
  }));
  document.querySelectorAll('[data-toggle-favorite]').forEach(button=>button.addEventListener('click',event=>{
    event.stopPropagation();
    sidebarPrefToggle('favorites',button.dataset.toggleFavorite);
    renderSidebar();
    toast(sidebarPrefHas('favorites',button.dataset.toggleFavorite)?'已加入收藏':'已取消收藏');
  }));
  document.querySelectorAll('[data-toggle-pin]').forEach(button=>button.addEventListener('click',event=>{
    event.stopPropagation();
    sidebarPrefToggle('pinned',button.dataset.togglePin);
    renderSidebar();
    toast(sidebarPrefHas('pinned',button.dataset.togglePin)?'已置顶':'已取消置顶');
  }));
  document.querySelectorAll('[data-sidebar-item]').forEach(row=>{
    row.addEventListener('dragstart',event=>{
      event.dataTransfer?.setData('text/plain',row.dataset.itemId || '');
      if(event.dataTransfer) event.dataTransfer.effectAllowed='move';
      row.classList.add('is-dragging');
    });
    row.addEventListener('dragend',()=>row.classList.remove('is-dragging'));
  });
  document.querySelectorAll('[data-pinned-drop]').forEach(zone=>{
    zone.addEventListener('dragover',event=>{ event.preventDefault(); zone.classList.add('is-drag-over'); });
    zone.addEventListener('dragleave',()=>zone.classList.remove('is-drag-over'));
    zone.addEventListener('drop',event=>{
      event.preventDefault();
      zone.classList.remove('is-drag-over');
      const itemId=event.dataTransfer?.getData('text/plain');
      if(!itemId) return;
      if(!sidebarPrefHas('pinned',itemId)) sidebarPrefToggle('pinned',itemId);
      renderSidebar();
      toast('已置顶，可在“已置顶”中取消');
    });
  });
  document.querySelectorAll('[data-file-channel]').forEach(button=>button.addEventListener('click',()=>{
    state.fileChannel=button.dataset.fileChannel;
    renderSidebar();
    renderFileBoard();
  }));
  $('create-channel')?.addEventListener('click',openChannelDialog);
  // Creating an Agent now starts with choosing which kind; the hosted dialog
  // is one branch of that choice rather than the only path.
  $('create-agent')?.addEventListener('click',()=>{
    if(typeof openAgentKindDialog==='function') openAgentKindDialog();
    else openAgentDialog();
  });
  $('create-dm')?.addEventListener('click',openDirectMessageDialog);
  $('open-graph')?.addEventListener('click',()=>setWorkspaceView('graph'));
  document.querySelectorAll('[data-edit-channel]').forEach(button=>button.addEventListener('click',event=>{
    event.stopPropagation();
    openChannelEditor(button.dataset.editChannel);
  }));
  document.querySelectorAll('[data-delete-channel]').forEach(button=>button.addEventListener('click',event=>{
    event.stopPropagation();
    deleteChannel(button.dataset.deleteChannel);
  }));
  document.querySelectorAll('[data-edit-agent]').forEach(button=>button.addEventListener('click',event=>{
    event.stopPropagation();
    openAgentEditor(button.dataset.editAgent);
  }));
  document.querySelectorAll('[data-delete-agent]').forEach(button=>button.addEventListener('click',event=>{
    event.stopPropagation();
    deleteAgent(button.dataset.deleteAgent);
  }));
  document.querySelectorAll('[data-restore-agent]').forEach(button=>button.addEventListener('click',event=>{
    event.stopPropagation();
    restoreAgent(button.dataset.restoreAgent);
  }));
  bindChannelButtons();
}

function showSimpleOverlay(title,body){
  openContextDrawer(title);
  $('context-content').innerHTML=`<div class="context-card"><h2>${esc(title)}</h2><p>${esc(body)}</p></div>`;
}
function showMembersDrawer(){
  openContextDrawer(`成员（${state.agents.length+1}）`);
  const rows=state.agents.map(agent=>{
    const status=agent.status||'online';
    const avatar=agent.avatar_path?`<img src="/${esc(agent.avatar_path)}" alt="">`:esc(initials(agent.agent_id));
    return `<button type="button" class="drawer-member" data-drawer-agent="${esc(agent.agent_id)}"><span class="drawer-member-avatar" style="background:${agentColors[agent.agent_id]||'#6e7b76'}">${avatar}</span><span><strong>${esc(agent.name||labels[agent.agent_id]||agent.agent_id)}</strong><small>${esc(taskStatusText(status))}</small></span><i class="status-dot"></i></button>`;
  }).join('');
  $('context-content').innerHTML=`<div class="member-group"><h3>Agent</h3>${rows}</div><div class="member-group"><h3>人类</h3><div class="drawer-member drawer-member-static"><span class="drawer-member-avatar owner">你</span><span><strong>你</strong><small>所有者</small></span><i class="status-dot"></i></div></div>`;
  document.querySelectorAll('[data-drawer-agent]').forEach(button=>button.addEventListener('click',()=>showAgent(button.dataset.drawerAgent)));
}
function showChannelSettingsDrawer(){
  const channel=state.channels.find(item=>item.channel_id===state.channelId)||{};
  const name=channel.name||state.channelId;
  openContextDrawer('频道设置');
  $('context-content').innerHTML=`<div class="context-card channel-settings-card"><span class="eyebrow">频道</span><h2>设置</h2><p>#${esc(name)}</p><h3>频道信息</h3><p class="muted">在整个工作空间中显示的名称和描述。</p><label>名称<input value="${esc(name)}" disabled></label><label>描述<textarea rows="4" readonly>${esc(channel.description||channel.topic||'')}</textarea></label><h3>频道操作</h3><p class="muted">成员资格、可见性、归档和其他频道操作。</p><div class="context-actions"><button class="secondary" id="open-channel-editor">编辑频道</button><button class="secondary" id="hide-channel-action">隐藏 #${esc(name)}</button></div></div>`;
  $('open-channel-editor')?.addEventListener('click',()=>{ closeContextDrawer(); openChannelEditor(state.channelId); });
  $('hide-channel-action')?.addEventListener('click',()=>toast('本地版已保留隐藏频道入口'));
}
function drawerUtilityContent(kind){
  if(kind==='notifications'){
    const recent=state.messages.slice(-4).reverse();
    return `<div class="context-card"><h2>通知中心</h2>${recent.length?recent.map(item=>`<button class="drawer-link" type="button" data-pop-message="${esc(item.message_id)}"><strong>${esc(labels[item.author_id]||item.author_id)}</strong><span>${esc(item.body).slice(0,72)}</span></button>`).join(''):'<div class="empty-state"><p>暂时没有新通知。</p></div>'}</div>`;
  }
  if(kind==='help') return '<div class="context-card"><h2>帮助</h2><button class="drawer-link" type="button" data-help="mentions"><strong>如何 @Agent 协作</strong><span>在频道中直接提及一个或多个 Agent。</span></button><button class="drawer-link" type="button" data-help="tasks"><strong>如何创建任务</strong><span>将消息转为任务，或在 Agent 消息下继续派发。</span></button><button class="drawer-link" type="button" data-help="reports"><strong>如何查看报告</strong><span>研究完成后打开 Markdown 报告。</span></button></div>';
  return '<div class="context-card"><h2>设置</h2><button class="drawer-link" type="button" data-setting="workspace"><strong>工作空间设置</strong><span>名称、频道和默认行为。</span></button><button class="drawer-link" type="button" data-setting="models"><strong>模型与密钥</strong><span>密钥仅保存在本机后端。</span></button><button class="drawer-link" type="button" data-setting="appearance"><strong>外观</strong><span>当前使用统一浅红主题。</span></button></div>';
}
function showUtilityDrawer(kind){
  const titles={notifications:'通知中心',help:'帮助',settings:'设置'};
  openContextDrawer(titles[kind]||'上下文');
  state.drawerMode=kind;
  $('context-content').innerHTML=drawerUtilityContent(kind);
  document.querySelectorAll('[data-pop-message]').forEach(item=>item.addEventListener('click',()=>{ setChannelTab('chat'); showMessage(item.dataset.popMessage); }));
  document.querySelectorAll('[data-help]').forEach(item=>item.addEventListener('click',()=>showSimpleOverlay('帮助',item.dataset.help==='mentions'?'在频道输入 @Agent 名称即可派发任务；一次可 @多个 Agent。':item.dataset.help==='tasks'?'频道任务会显示在“任务”标签中，并保留负责人和状态。':'研究完成后可在“文件”或“查看报告”中打开 Markdown 报告。')));
  document.querySelectorAll('[data-setting]').forEach(item=>item.addEventListener('click',()=>showSimpleOverlay('设置',item.dataset.setting==='models'?'模型和 API Key 只保存在本机后端，不在页面中显示明文。':'该设置入口暂未接入持久化配置。')));
}
// 主题只换色号，形态由 tech.css 统一提供，所以这里不需要知道任何一套主题
// 长什么样——只要把 data-theme 挂到根元素上。色卡直接用该主题的三个主色画，
// 不用读名字就知道点开会变成什么。
const THEME_KEY='workbench.theme';
const THEMES=[
  {id:'crimson',   name:'投研红',   swatch:['#FFFFFF','#C8102E','#F6C5CF']},
  {id:'deepspace', name:'深空蓝',   swatch:['#070E16','#38BDF8','#0A1622']},
  {id:'amber',     name:'琥珀终端', swatch:['#100B04','#F59E0B','#1A1206']},
  {id:'graphite',  name:'石墨绿',   swatch:['#FBFDFC','#0D9488','#CFE6E2']},
];
function currentTheme(){
  return document.documentElement.dataset.theme || 'crimson';
}
function applyTheme(id){
  const theme=THEMES.some(item=>item.id===id) ? id : 'crimson';
  document.documentElement.dataset.theme=theme;
  try{ localStorage.setItem(THEME_KEY,theme); }catch(_error){ /* 私密模式：只在本次会话生效 */ }
  renderThemeMenu();
  // 关系图是画上去的，不吃 CSS 变量，换主题后要重画一次
  if(state.workspaceView==='graph' && typeof renderGraphBoard==='function') renderGraphBoard();
}
function renderThemeMenu(){
  const menu=$('theme-menu');
  if(!menu) return;
  const active=currentTheme();
  menu.innerHTML='<p class="theme-menu-title">主题</p>'+THEMES.map(theme=>
    `<button type="button" role="menuitem" class="theme-option${theme.id===active?' active':''}" data-theme-id="${esc(theme.id)}">
       <span class="theme-swatch">${theme.swatch.map(color=>`<i style="background:${esc(color)}"></i>`).join('')}</span>
       <span class="theme-name">${esc(theme.name)}</span>
     </button>`).join('');
  menu.querySelectorAll('[data-theme-id]').forEach(button=>button.addEventListener('click',()=>{
    applyTheme(button.dataset.themeId);
    $('theme-switch')?.classList.remove('is-open');
    toast(`已切换到「${THEMES.find(item=>item.id===button.dataset.themeId)?.name}」`);
  }));
}
function setupThemeSwitch(){
  const wrap=$('theme-switch'), toggle=$('theme-toggle');
  if(!wrap || !toggle) return;
  renderThemeMenu();
  toggle.addEventListener('click',event=>{
    event.stopPropagation();
    const open=wrap.classList.toggle('is-open');
    toggle.setAttribute('aria-expanded',String(open));
  });
  // 点别处就收起来，否则这个浮层会一直挡着左栏
  document.addEventListener('click',event=>{
    if(!wrap.contains(event.target)){
      wrap.classList.remove('is-open');
      toggle.setAttribute('aria-expanded','false');
    }
  });
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape') wrap.classList.remove('is-open');
  });
}

function setupChatChrome(){
  $('close-context')?.addEventListener('click',closeContextDrawer);
  $('drawer-scrim')?.addEventListener('click',closeContextDrawer);
  $('channel-members')?.addEventListener('click',showMembersDrawer);
  $('channel-settings')?.addEventListener('click',showChannelSettingsDrawer);
  $('channel-search')?.addEventListener('click',()=>{
    setWorkspaceView('search');
    requestAnimationFrame(()=>{
      const channel=$('search-channel');
      if(channel){ channel.value=state.channelId; channel.dispatchEvent(new Event('change')); }
      $('search-input')?.focus();
    });
  });
  $('channel-mute')?.addEventListener('click',()=>{
    state.channelMuted=!state.channelMuted;
    $('channel-muted-label').hidden=!state.channelMuted;
    $('channel-mute').classList.toggle('is-active',state.channelMuted);
    $('channel-mute').setAttribute('aria-pressed',String(state.channelMuted));
    $('channel-mute').title=state.channelMuted?'为此频道取消静音活动':'为此频道静音活动';
    toast(state.channelMuted?'频道已静音；直接提及仍会通知':'频道已取消静音');
  });
  document.querySelectorAll('[data-drawer]').forEach(button=>button.addEventListener('click',()=>showUtilityDrawer(button.dataset.drawer)));
  document.addEventListener('keydown',event=>{ if(event.key==='Escape') closeContextDrawer(); });
}

async function deleteChannel(channelId){
  const channel=state.channels.find(item=>item.channel_id===channelId);
  const label=channel?.kind==='direct' ? `与 ${channel.name} 的私信` : `频道「${channel?.name||channelId}」`;
  if(!window.confirm(`确定删除${label}吗？\n其中的消息、任务、文件记录会一并删除，且无法恢复。`)) return;
  const response=await fetch(`/api/channels/${encodeURIComponent(channelId)}`,{method:'DELETE'});
  const result=await response.json();
  if(!response.ok){ toast(result.error || '删除频道失败'); return; }
  // Land somewhere valid if the open channel was the one removed.
  if(state.channelId===channelId){
    const fallback=state.channels.find(item=>item.channel_id!==channelId && item.kind!=='direct');
    state.channelId=fallback?.channel_id || 'research-room';
    state.selectedThreadMessageId=null;
    state.eventSeq=0;
  }
  await loadWorkspace(false);
  await loadMessages();
  applyChannelHeader();
  connectEvents();
  toast('已删除');
}

async function deleteTask(taskId){
  const task=[...state.tasks,...state.channelTasks].find(item=>item.task_id===taskId);
  const title=task?.title || taskId;
  if(!window.confirm(`确定删除任务「${title}」吗？\n已生成的消息、文件和研究证据会保留。`)) return;
  let response;
  let result={};
  try{
    response=await fetch(`/api/tasks/${encodeURIComponent(taskId)}`,{method:'DELETE'});
    result=await response.json();
  }catch(error){
    toast('删除任务失败：无法连接本地服务');
    return;
  }
  if(!response.ok){ toast(result.error || '删除任务失败'); return; }
  await loadWorkspace(false);
  await loadMessages();
  renderTaskBoard();
  if(result.cancelling){
    toast(result.notice || '任务正在安全取消，结束后会自动移除');
  }else{
    toast('任务已删除');
  }
}

async function deleteAgent(agentId){
  const agent=state.agents.find(item=>item.agent_id===agentId);
  const name=agent?.name || agentId;
  // Removing a built-in role also removes it from the seven-Agent research
  // flow, so say so rather than letting the run fail later.
  const custom=agent?.type==='custom' || agent?.type==='local';
  const message=custom
    ? `确定删除 Agent「${name}」吗？此操作无法恢复。`
    : `确定把内置 Agent「${name}」移出工作区吗？\n它将不再出现，也不能接收任务，完整研究流程会缺少这个角色。\n插件定义保留在本地，之后可以恢复。`;
  if(!window.confirm(message)) return;
  const response=await fetch(`/api/agents/${encodeURIComponent(agentId)}`,{method:'DELETE'});
  const result=await response.json();
  if(!response.ok){ toast(result.error || '删除 Agent 失败'); return; }
  const directChannelId=`dm-${agentId}`;
  ['pinned','favorites'].forEach(key=>{
    const values=Array.isArray(state.sidebarPrefs[key])?state.sidebarPrefs[key]:[];
    state.sidebarPrefs[key]=values.filter(item=>item!==directChannelId && item!==agentId);
  });
  saveSidebarPrefs();
  if(state.channelId===directChannelId){
    const fallback=state.channels.find(item=>item.channel_id!==directChannelId && item.kind!=='direct');
    state.channelId=fallback?.channel_id || 'research-room';
    state.selectedThreadMessageId=null;
    closeContextDrawer();
  }
  await loadWorkspace(false);
  await loadMessages();
  applyChannelHeader();
  connectEvents();
  toast(result.notice || `Agent「${name}」已删除`);
}

async function restoreAgent(agentId){
  const response=await fetch(`/api/agents/${encodeURIComponent(agentId)}/restore`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const result=await response.json();
  if(!response.ok){ toast(result.error || '恢复失败'); return; }
  await loadWorkspace(false);
  toast(`「${result.name || agentId}」已恢复`);
}
function renderChannels(){ renderSidebar(); }

// The header, the eyebrow and the composer hint all name the open channel.
// They used to carry a hard-coded 科大讯飞 title that stayed put whichever
// channel was selected.
function applyChannelHeader(){
  const channel=state.channels.find(item=>item.channel_id===state.channelId);
  const direct=channel?.kind==='direct';
  const name=channel?.name || state.channelId;
  const heading=document.querySelector('.channel-head h1');
  const subtitle=document.querySelector('.channel-head p');
  if(heading) heading.textContent=name;
  if(subtitle){
    subtitle.textContent=channel?.topic
      || channel?.description
      || (direct ? `只有你和 ${name} 的一对一对话` : '人类与多个 Agent 的研究交接、任务和交付');
  }
  const input=$('message-input');
  if(input && direct) input.placeholder=`直接跟 ${name} 说…`;
  else if(input) input.placeholder=`发送消息至 #${name}`;
  const memberCount=$('member-count');
  if(memberCount) memberCount.textContent=String((channel?.member_ids||[]).length || state.agents.length+1);
  if(direct && state.workspaceView==='graph') setWorkspaceView('channel',{focus:false});
}
function bindChannelButtons(){
  const list=$('sidebar-body');
  if(!list) return;
  list.querySelectorAll('[data-channel]').forEach(button=>button.addEventListener('click',async()=>{
    if(button.dataset.channel===state.channelId) return;
    state.channelId=button.dataset.channel;
    state.selectedThreadMessageId=null;
    state.eventSeq=0;
    state.taskFilters={...state.taskFilters,creator:'',assignee:'',channel:''};
    state.fileChannel='';
    applyChannelHeader();
    renderChannels();
    await loadMessages();
    await loadWorkspace(false);
    connectEvents();
  }));
}

function avatarDataUrl(file){
  // An untouched file input still yields a File — empty, and with no image
  // type. Reading it produces a data URL the server rightly rejects, so an
  // Agent created without an avatar would fail on the avatar.
  if(!file || !file.size) return Promise.resolve('');
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

// The sidebar is re-rendered on every pane switch, so its buttons are bound
// each time rather than once at startup.
function openAgentDialog(){ resetAgentEditor(); $('agent-dialog').showModal(); }
let editingChannelId=null;
function openChannelDialog(){
  editingChannelId=null;
  const form=$('channel-form');
  form.reset();
  $('channel-dialog-title').textContent='创建协作频道';
  $('channel-dialog-subtitle').textContent='新频道只允许已加入的 Agent 讨论，不自动启动完整投研流程';
  $('channel-submit').textContent='创建频道';
  $('channel-members-field').hidden=false;
  $('channel-agent-options').innerHTML=state.agents.filter(agent=>agent.enabled!==false).map(agent=>`<label><input type="checkbox" name="member_ids" value="${esc(agent.agent_id)}"> <span>${esc(agent.name || agent.agent_id)} · ${esc(agent.role || 'Agent')}</span></label>`).join('');
  $('channel-dialog').showModal();
}
function openChannelEditor(channelId){
  const channel=state.channels.find(item=>item.channel_id===channelId);
  if(!channel) return;
  editingChannelId=channelId;
  const form=$('channel-form');
  form.reset();
  form.elements.name.value=channel.name || '';
  form.elements.topic.value=channel.topic || '';
  form.elements.description.value=channel.description || '';
  $('channel-dialog-title').textContent='编辑频道';
  $('channel-dialog-subtitle').textContent='修改频道名称和研究课题';
  $('channel-submit').textContent='保存修改';
  // Membership is fixed after creation; only the naming is editable.
  $('channel-members-field').hidden=true;
  $('channel-dialog').showModal();
}
function openDirectMessageDialog(){
  const existing=new Set(state.channels.filter(item=>item.kind==='direct').map(item=>item.channel_id));
  const options=state.agents.filter(agent=>agent.enabled!==false).map(agent=>{
    const started=existing.has(`dm-${agent.agent_id}`);
    return `<label><input type="radio" name="dm_agent" value="${esc(agent.agent_id)}" ${started?'':'required'}> <span>${esc(agent.name || agent.agent_id)} · ${esc(agent.role || 'Agent')}${started?'（已有对话）':''}</span></label>`;
  }).join('');
  $('dm-agent-options').innerHTML=options || '<p class="muted">没有可对话的 Agent。</p>';
  $('dm-dialog').showModal();
}
async function startDirectMessage(agentId){
  // GET creates the channel on first use, so opening a conversation needs no
  // separate create call.
  const response=await fetch(`/api/agents/${encodeURIComponent(agentId)}/messages`,{cache:'no-store'});
  if(!response.ok){ toast('无法创建私信'); return; }
  const data=await response.json();
  state.channelId=data.channel.channel_id;
  state.selectedThreadMessageId=null;
  state.eventSeq=0;
  state.taskFilters={...state.taskFilters,creator:'',assignee:'',channel:''};
  state.fileChannel='';
  setWorkspaceView('channel',{focus:false});
  await Promise.all([loadWorkspace(false),loadMessages()]);
  connectEvents();
  toast(`已打开与 ${data.agent.name || agentId} 的私信`);
}

function setupCreationDialogs(){
  const agentDialog=$('agent-dialog'), channelDialog=$('channel-dialog'), dmDialog=$('dm-dialog');
  $('sidebar-action')?.addEventListener('click',()=>{
    if(state.workspaceView==='channel') openDirectMessageDialog();
  });
  $('dm-form')?.addEventListener('submit',async event=>{
    event.preventDefault();
    const agentId=new FormData(event.currentTarget).get('dm_agent');
    if(!agentId){ toast('请选择一个 Agent'); return; }
    dmDialog.close();
    event.currentTarget.reset();
    await startDirectMessage(String(agentId));
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
    // currentTarget is nulled once the handler yields, so keep the element
    // itself rather than reaching for it again after an await.
    const formElement=event.currentTarget;
    const form=new FormData(formElement);
    const payload={name:form.get('name'),topic:form.get('topic'),description:form.get('description')};
    const editing=editingChannelId;
    const response=editing
      ? await fetch(`/api/channels/${encodeURIComponent(editing)}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
      : await fetch('/api/channels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,channel_mode:'chat',member_ids:form.getAll('member_ids')})});
    const result=await response.json();
    if(!response.ok){ toast(result.error || (editing?'保存频道失败':'创建频道失败')); return; }
    channelDialog.close();
    formElement.reset();
    editingChannelId=null;
    if(!editing){
      state.channelId=result.channel_id;
      state.eventSeq=0;
      state.taskFilters={...state.taskFilters,creator:'',assignee:'',channel:''};
      state.fileChannel='';
    }
    await loadWorkspace(false);
    await loadMessages();
    applyChannelHeader();
    connectEvents();
    toast(editing ? `频道「${result.name}」已更新` : `协作频道「${result.name}」已创建，仅邀请的 Agent 可参与聊天`);
  });
}

document.addEventListener('DOMContentLoaded',setupCreationDialogs);
// Load the graph once at startup so its tab count is real before the pane is
// ever opened, matching how the task and file counts behave.
document.addEventListener('DOMContentLoaded',()=>{ setupNavigation(); setupAttachments(); setupSearch(); setupChatChrome(); setupThemeSwitch(); loadGraph(); });

// Attachments belong to the message they were sent with, so they render in the
// timeline instead of only appearing in the files tab.
const renderMessagesBeforeAttachments = renderMessages;
renderMessages = function(){
  renderMessagesBeforeAttachments();
  document.querySelectorAll('.message').forEach(article=>{
    const message=state.messages.find(item=>item.message_id===article.dataset.message);
    // A message keeps its own copy of what was attached. Anything the channel
    // no longer holds was deleted, and rendering it would offer a download
    // that 404s.
    const present=new Set(state.channelFiles.map(item=>item.file_id));
    const attachments=(message?.metadata?.attachments || []).filter(item=>present.has(item.file_id));
    if(!attachments.length) return;
    const strip=document.createElement('div');
    strip.className='message-attachments';
    strip.innerHTML=attachments.map(item=>{
      const href=`/api/files/${encodeURIComponent(item.file_id)}/download`;
      return String(item.media_type||'').startsWith('image/')
        ? `<a class="attachment-thumb" href="${href}" target="_blank" rel="noreferrer"><img src="${href}" alt="${esc(item.filename)}"><span>${esc(item.filename)}</span></a>`
        : `<a class="attachment-file" href="${href}">${icon('file')}<b>${esc(item.filename)}</b><small>${esc(formatBytes(item.size_bytes))}</small></a>`;
    }).join('');
    article.querySelector('.message-main').appendChild(strip);
  });
};
