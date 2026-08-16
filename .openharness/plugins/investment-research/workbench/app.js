const state = { channelId: 'research-room', channels: [], messages: [], agents: [], tasks: [], artifacts: [], files: [], run: {}, modelSettings: {default_model:'ark-code-latest',models:[]}, eventSeq: 0, selectedThreadMessageId: null, activePane: 'chat', pendingAttachments: [], graph: null, taskFilters: {creator:'', assignee:'', channel:'', view:'board'}, collapsed: {}, fileChannel: '', graphChannel: '', graphSelection: null, removedAgents: [], skills: {}, threads: {}, openComment: null, railExpanded: false };
const $ = (id) => document.getElementById(id);
const agentColors = { planner:'#C8102E', fundamental:'#A60D28', industry_competition:'#8C3156', market_catalyst:'#C88A18', risk:'#8B2940', reviewer_arbiter:'#6F1630', report_writer:'#B12A46' };
const labels = { planner:'林序', fundamental:'陈实', industry_competition:'周衡', market_catalyst:'沈策', risk:'顾谨', reviewer_arbiter:'韩证', report_writer:'程章', system:'工作台', owner:'你' };
const roles = { planner:'任务规划 Agent', fundamental:'公司基本面 Agent', industry_competition:'行业竞品 Agent', market_catalyst:'市场催化 Agent', risk:'风险 Agent', reviewer_arbiter:'审查仲裁 Agent', report_writer:'报告 Agent' };
// Agents that accept a directly assigned task. Planner is excluded: it owns the
// full flow and is started from the channel, not from a comment.
const DIRECT_AGENT_IDS = new Set(['fundamental','industry_competition','market_catalyst','risk','reviewer_arbiter','report_writer']);
function esc(value){ return String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
// Reference one symbol from the inline Lucide sprite in index.html.
function icon(name, extraClass=''){ return `<svg class="icon ${extraClass}" aria-hidden="true"><use href="#i-${name}"/></svg>`; }
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
  list.querySelectorAll('[data-comment-toggle]').forEach(el=>el.addEventListener('click',()=>toggleComments(el.dataset.commentToggle)));
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
function renderContext(){ const r=state.run||{}; const current=state.tasks.find(t=>t.status==='running') || state.tasks[0]; $('context-content').innerHTML=`<div class="context-card"><h2>当前研究运行</h2><p class="muted">${r.running?'正在执行真实 CrewAI/OpenHarness 流程':'@Planner 可启动完整研究；@其他 Agent 可直接派发补充任务'}</p><div class="kv"><span>公司</span><strong>${esc(r.company || currentCompany() || '未指定')}</strong></div><div class="kv"><span>基准日</span><strong>${esc(r.as_of_date || '默认今天')}</strong></div><div class="kv"><span>状态</span><strong>${r.running?'运行中':r.report_available?'已交付':'等待中'}</strong></div>${current?`<h3>最近任务</h3><div class="kv"><span>任务</span><strong>${esc(current.title)}</strong></div><div class="kv"><span>负责人</span><strong>${esc(current.assignee_id)}</strong></div>`:''}<h3>两种协作方式</h3><p><strong>@Planner</strong> 会启动七个 Agent 的完整研究闭环；<strong>@Fundamental 等其他 Agent</strong> 会复用频道最近一次 Run 的参数卡和证据，单独执行补充任务。一次 @多个 Agent 时会并行派发。</p>${r.report_available?'<button class="primary" id="context-report">打开 Markdown 报告</button>':''}</div>`; const btn=$('context-report'); if(btn) btn.addEventListener('click',openReport); }
function showAgent(id){
  const a=state.agents.find(x=>x.agent_id===id)||{};
  const isCustom=a.type==='custom';
  const model=a.model||state.modelSettings.default_model;
  const avatar=a.avatar_path
    ? `<img class="profile-avatar" src="/${esc(a.avatar_path)}" alt="${esc(a.name||id)}">`
    : `<div class="profile-avatar profile-avatar-fallback" style="background:${agentColors[id] || '#58746a'}">${esc(initials(id))}</div>`;
  // Model and avatar are operator choices for every Agent, built-in included.
  // Prompt and contract stay editable only for custom Agents.
  $('context-content').innerHTML=`<div class="context-card"><div class="profile-head">${avatar}<label class="avatar-replace">更换头像<input id="agent-avatar-input" type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden></label></div><h2>${esc(a.name||labels[id]||id)}</h2><p class="muted">${esc(a.role||roles[id]||'Agent')}</p><div class="kv"><span>Agent ID</span><strong>${esc(id)}</strong></div><div class="kv"><span>类型</span><strong>${isCustom?'自定义':'系统内置'}</strong></div><div class="kv"><span>当前模型</span><strong>${esc(model)}</strong></div><h3>个人简介</h3><p>${esc(a.profile||'暂无简介')}</p>${isCustom?`<h3>系统提示词</h3><div class="agent-prompt">${esc(a.system_prompt||'')}</div>`:`<h3>工具白名单</h3><div>${(a.allowed_tools||[]).map(t=>`<span class="tool-tag">${esc(t)}</span>`).join('')||'<span class="muted">当前角色无直接工具</span>'}</div>`}<h3>切换模型</h3><select id="agent-model-select">${modelOptionsHtml(model)}</select><h3>状态</h3><p><span class="status-dot"></span> ${esc(taskStatusText(a.status))} · ${esc(a.task_phase||'等待频道任务')}</p><div class="context-actions"><button id="save-agent-model" class="primary" type="button">保存模型</button>${isCustom?'<button id="edit-agent" class="secondary" type="button">编辑资料</button>':''}</div></div><div class="context-card"><div class="skill-head"><h2>Skill 插件</h2><label class="attach-btn" title="上传 Skill 插件">${icon('upload')} 上传<input id="skill-input" type="file" accept=".md,.markdown,.json,.yaml,.yml,.txt,.zip" hidden></label></div><p class="muted">上传 SKILL.md 或打包的插件；启用后随该 Agent 的角色提示词一起加载。</p><div id="skill-list" class="skill-list"><p class="muted">正在加载插件…</p></div></div>`;
  $('edit-agent')?.addEventListener('click',()=>openAgentEditor(id));
  $('save-agent-model')?.addEventListener('click',()=>saveAgentModel(id));
  $('agent-avatar-input')?.addEventListener('change',event=>saveAgentAvatar(id,event));
  setupSkills(id);
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
async function loadWorkspace(show=true){ const res=await fetch(`/api/workspace?channel_id=${encodeURIComponent(state.channelId)}&files=all`,{cache:'no-store'}); const data=await res.json(); state.channels=data.channels||[]; state.agents=data.agents||[]; state.tasks=data.tasks||[]; state.artifacts=data.artifacts||[]; state.files=data.files||[]; state.removedAgents=data.removed_agents||[]; state.run=data.run||{}; state.modelSettings=data.model_settings||state.modelSettings; state.eventSeq=Math.max(state.eventSeq,Number(data.event_seq||0)); renderModelOptions(); renderChannels(); applyChannelHeader(); renderAgents(); renderRun(); renderTaskBoard(); renderFileBoard(); if(state.activePane==='graph') loadGraph(); if(state.selectedThreadMessageId){ refreshSelectedThread(false); }else{ renderContext(); } if(show) $('connection-state').textContent='已连接'; }
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
    // The研究标的 comes from the open channel's topic, not a hard-coded name.
    const company=currentCompany();
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
// A research run is started by @Planner in the channel, so the old
// start-a-run buttons and their handler are gone with the panels that held them.
document.addEventListener('DOMContentLoaded',async()=>{ await loadWorkspace(); await loadMessages(); setupComposer(); connectEvents(); $('refresh')?.addEventListener('click',()=>{loadWorkspace();loadMessages();}); $('report-btn')?.addEventListener('click',openReport); });

function showTask(taskId){ const task=state.tasks.find(item=>item.task_id===taskId); if(!task)return; $('context-content').innerHTML=`<div class="context-card"><h2>任务详情</h2><p>${esc(task.title)}</p><div class="kv"><span>任务 ID</span><strong>${esc(task.task_id)}</strong></div><div class="kv"><span>负责人</span><strong>${esc(labels[task.assignee_id]||task.assignee_id)}</strong></div><div class="kv"><span>状态</span><strong>${esc(task.status)}</strong></div><div class="kv"><span>运行 ID</span><strong>${esc(task.run_id||'等待运行')}</strong></div><h3>任务说明</h3><p>这是由频道消息触发的研究任务。任务状态来自本地 SQLite 协作记录，不是静态图片。</p></div>`; }
function showArtifact(artifactId){ const artifact=state.artifacts.find(item=>item.artifact_id===artifactId); if(!artifact)return; $('context-content').innerHTML=`<div class="context-card"><h2>交付成果</h2><p>${esc(artifact.title)}</p><div class="kv"><span>成果 ID</span><strong>${esc(artifact.artifact_id)}</strong></div><div class="kv"><span>提交 Agent</span><strong>${esc(labels[artifact.agent_id]||artifact.agent_id)}</strong></div><div class="kv"><span>状态</span><strong>${esc(artifact.status)}</strong></div><h3>成果摘要</h3><p>${esc(artifact.summary)}</p><h3>关联编号</h3><div>${(artifact.refs||[]).map(item=>`<span class="tool-tag">${esc(item)}</span>`).join('')||'<span class="muted">暂未记录</span>'}</div></div>`; }
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
    $('context-content').innerHTML=`<div class="thread-card"><div class="thread-title"><div><span class="eyebrow">线程交接</span><h2>${esc(kindLabel(data.root.message_kind))}</h2></div><button id="thread-back" class="secondary">返回概览</button></div><section><h3>原始请求</h3>${threadMessageHtml(data.root,data.root.message_id)}</section><section><h3>协作回复（${(data.replies||[]).length}）</h3>${(data.replies||[]).map(item=>threadMessageHtml(item,data.root.message_id)).join('')||'<p class="muted">暂时没有回复。Agent 接收、进度、交付和失败说明都会追加到此处。</p>'}</section><section><h3>关联任务</h3>${taskHtml}</section><section><h3>交付物</h3>${artifactHtml}</section><section><h3>证据编号</h3><div class="thread-refs">${refs.map(ref=>`<span class="tool-tag">${esc(ref)}</span>`).join('')||'<span class="muted">暂无新增 S/F/L/Risk 编号</span>'}</div></section><section class="thread-composer-section"><h3>继续协作</h3><form id="thread-composer" class="thread-composer"><textarea id="thread-input" rows="3" placeholder="例如：@Fundamental 请补充最近一年收入变化的原因"></textarea><div class="thread-agent-quick">${state.agents.filter(agent=>agent.agent_id!=='planner').map(agent=>`<button type="button" data-thread-mention="${esc(agent.agent_id)}">@${esc(agent.agent_id)}</button>`).join('')}</div><div class="thread-composer-footer"><span>线程内 @多个 Agent 会创建独立任务并行执行。</span><button type="submit" class="send">${icon('send')} 发送并派单</button></div></form></section></div>`;
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

// Chat / Tasks / Files share the centre column.  Only the chat pane owns the
// composer, so switching to a board never leaves a send box pointing at a view
// that cannot receive a message.
function setPane(pane){
  state.activePane=pane;
  document.querySelectorAll('.pane-tab').forEach(tab=>tab.classList.toggle('active',tab.dataset.pane===pane));
  document.querySelectorAll('.rail-btn').forEach(button=>button.classList.toggle('active',button.dataset.pane===pane));
  document.querySelectorAll('.pane-view').forEach(view=>{ view.hidden=view.dataset.paneView!==pane; });
  const composer=$('composer');
  if(composer) composer.hidden=pane!=='chat';
  // Every pane except chat takes over the column, so the channel chrome and
  // the pane tabs step aside — a files view shows files and nothing else.
  const chromeHidden=pane!=='chat';
  const tabs=$('pane-tabs');
  if(tabs) tabs.hidden=chromeHidden;
  const head=document.querySelector('.channel-head');
  const banner=$('run-banner');
  if(head) head.hidden=chromeHidden;
  if(banner) banner.hidden=chromeHidden;
  renderSidebar();
  if(pane==='tasks') renderTaskBoard();
  if(pane==='files') renderFileBoard();
  if(pane==='graph') loadGraph();
  if(pane==='search'){ runSearch(); $('search-input')?.focus(); }
}
// The rail starts collapsed to icons. Expanding shows each label, which is
// clearer for anyone who does not already know the glyphs. The choice is
// remembered so it does not reset on every reload.
const RAIL_KEY='workbench.railExpanded';
function applyRail(){
  const shell=document.querySelector('.app-shell');
  const toggle=$('rail-toggle');
  const expanded=state.railExpanded;
  if(shell) shell.classList.toggle('rail-expanded',expanded);
  if(toggle){
    toggle.setAttribute('aria-expanded',String(expanded));
    toggle.title=expanded?'收起菜单':'展开菜单';
    const label=toggle.querySelector('.rail-label');
    if(label) label.textContent='收起菜单';
  }
}
function setupRail(){
  try{ state.railExpanded=localStorage.getItem(RAIL_KEY)==='1'; }
  catch(_error){ state.railExpanded=false; }
  applyRail();
  $('rail-toggle')?.addEventListener('click',()=>{
    state.railExpanded=!state.railExpanded;
    try{ localStorage.setItem(RAIL_KEY, state.railExpanded?'1':'0'); }catch(_error){ /* private mode */ }
    applyRail();
    // The graph sizes its canvas from the container, so re-layout after the
    // column width changes.
    if(state.activePane==='graph') setTimeout(()=>renderGraphBoard(),160);
  });
}

function setupPaneTabs(){
  document.querySelectorAll('.pane-tab').forEach(tab=>tab.addEventListener('click',()=>setPane(tab.dataset.pane)));
  // The rail declares its target pane, so inserting a button never shifts the
  // mapping the way an index-based lookup did.
  document.querySelectorAll('.rail-btn').forEach(button=>button.addEventListener('click',()=>setPane(button.dataset.pane)));
  $('open-graph')?.addEventListener('click',()=>setPane('graph'));
  $('search-close')?.addEventListener('click',()=>setPane('chat'));
  document.addEventListener('keydown',event=>{
    if((event.ctrlKey || event.metaKey) && event.key.toLowerCase()==='k'){
      event.preventDefault();
      setPane('search');
    }else if(event.key==='Escape' && state.activePane==='search'){
      setPane('chat');
    }
  });
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
  // An Agent works its queue one job at a time, so a waiting task says where it sits.
  const queued=task.status==='queued' && metadata.queue_position
    ? `<small>队列第 ${esc(metadata.queue_position)} 位${metadata.queue_waiting!=null?`（前面还有 ${esc(metadata.queue_waiting)} 个）`:''}</small>`
    : '';
  const handoff=metadata.handoff_depth
    ? `<small>由 ${esc(labels[task.created_by]||task.created_by)} 转派</small>`
    : '';
  return `<article class="board-card" data-board-task="${esc(task.task_id)}" data-status="${esc(task.status)}"><div class="board-card-id">${esc(task.task_id)}</div><strong>${esc(task.title)}</strong><div class="board-card-foot"><span class="board-assignee">${esc(assignee)}</span><span class="board-status">${esc(taskStatusText(task.status))}</span></div>${phase}${queued}${handoff}${elapsed}</article>`;
}
function memberOptions(selected, placeholder, ids){
  return `<option value="">${esc(placeholder)}</option>`+[...new Set(ids)].filter(Boolean).map(id=>`<option value="${esc(id)}" ${id===selected?'selected':''}>${esc(labels[id]||id)}</option>`).join('');
}
function visibleTasks(){
  const {creator, assignee, channel}=state.taskFilters;
  return state.tasks.filter(task=>
    (!creator || task.created_by===creator) &&
    (!assignee || task.assignee_id===assignee) &&
    (!channel || task.channel_id===channel));
}
function taskListHtml(tasks){
  if(!tasks.length) return '<div class="board-empty board-empty-wide">没有符合条件的任务。</div>';
  return `<div class="task-table"><div class="task-row task-head"><span>任务</span><span>负责人</span><span>创建者</span><span>频道</span><span>状态</span><span>更新时间</span></div>${tasks.map(task=>{
    const channel=state.channels.find(item=>item.channel_id===task.channel_id);
    return `<div class="task-row" data-board-task="${esc(task.task_id)}" data-status="${esc(task.status)}"><span class="task-row-title"><b>${esc(task.title)}</b><small>${esc(task.task_id)}</small></span><span>${esc(labels[task.assignee_id]||task.assignee_id)}</span><span>${esc(labels[task.created_by]||task.created_by)}</span><span>${esc(channel?.name||task.channel_id)}</span><span><em class="task-status task-status-${esc(taskColumnKey(task.status))}">${esc(taskStatusText(task.status))}</em></span><span>${esc(new Date(task.updated_at).toLocaleString())}</span></div>`;
  }).join('')}</div>`;
}
function renderTaskBoard(){
  const board=$('task-board');
  const counter=$('tab-task-count');
  if(counter) counter.textContent=state.tasks.length;
  if(!board) return;
  const tasks=visibleTasks();
  const {creator, assignee, channel, view}=state.taskFilters;
  const toolbar=`<div class="task-toolbar"><span class="task-toolbar-title">${icon('tasks')} 任务 <b>${tasks.length}</b> / ${state.tasks.length}</span><label class="search-filter">${icon('hash')}<select id="task-filter-channel">${memberOptions(channel,'频道',state.channels.map(item=>item.channel_id))}</select></label><label class="search-filter">${icon('user')}<select id="task-filter-creator">${memberOptions(creator,'创建者',state.tasks.map(item=>item.created_by))}</select></label><label class="search-filter">${icon('users')}<select id="task-filter-assignee">${memberOptions(assignee,'负责人',state.tasks.map(item=>item.assignee_id))}</select></label><div class="task-view-toggle"><button type="button" data-task-view="board" class="${view==='board'?'active':''}">${icon('check-square')} 看板</button><button type="button" data-task-view="list" class="${view==='list'?'active':''}">${icon('tasks')} 列表</button></div></div>`;
  const body=view==='list' ? taskListHtml(tasks) : `<div class="board-columns">${TASK_COLUMNS.map(column=>{
    const items=tasks.filter(task=>taskColumnKey(task.status)===column.key);
    const cards=items.map(taskCardHtml).join('') || `<div class="board-empty">没有${esc(column.label)}的任务。</div>`;
    return `<section class="board-column" data-column="${esc(column.key)}"><header><span class="board-chip board-chip-${esc(column.key)}">${esc(column.label)}</span><b>${items.length}</b></header><div class="board-column-body">${cards}</div></section>`;
  }).join('')}</div>`;
  board.innerHTML=toolbar+body;
  board.querySelectorAll('[data-board-task]').forEach(card=>card.addEventListener('click',()=>showTask(card.dataset.boardTask)));
  board.querySelectorAll('[data-task-view]').forEach(button=>button.addEventListener('click',()=>{
    state.taskFilters={...state.taskFilters, view:button.dataset.taskView};
    renderTaskBoard();
  }));
  const bind=(id,key)=>$(id)?.addEventListener('change',event=>{
    state.taskFilters={...state.taskFilters, [key]:event.target.value};
    renderTaskBoard();
  });
  bind('task-filter-channel','channel');
  bind('task-filter-creator','creator');
  bind('task-filter-assignee','assignee');
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
    sender.innerHTML='<option value="">发送者</option>'+(data.senders||[]).map(item=>`<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('');
  }
  if(channel && channel.options.length<=1){
    channel.innerHTML='<option value="">频道</option>'+(data.channels||[]).map(item=>`<option value="${esc(item.id)}">${item.kind==='direct'?'@':'#'}${esc(item.name)}</option>`).join('');
  }
}
function searchResultHtml(item){
  const meta=SEARCH_KIND_META[item.kind] || {label:item.kind, glyph:'file'};
  const when=item.created_at ? new Date(item.created_at).toLocaleString() : '';
  const where=item.channel_name ? `#${esc(item.channel_name)}` : '工作区';
  return `<article class="search-hit" data-hit-kind="${esc(item.kind)}" data-hit-ref="${esc(JSON.stringify(item.ref))}" data-hit-channel="${esc(item.channel_id)}"><span class="search-hit-kind">${icon(meta.glyph)} ${esc(meta.label)}</span><div class="search-hit-main"><strong>${esc(item.title)}</strong><p>${esc(item.body) || '<span class="muted">无正文</span>'}</p><div class="search-hit-meta"><span>${esc(item.owner_name || '—')}</span><span>${where}</span><span>${esc(when)}</span></div></div></article>`;
}
async function runSearch(){
  const board=$('search-results');
  if(!board) return;
  const filters=searchFilters();
  try{
    const response=await fetch('/api/search?'+new URLSearchParams(filters),{cache:'no-store'});
    if(!response.ok) throw new Error('search failed');
    const data=await response.json();
    renderSearchOptions(data);
    if(!data.total){
      board.innerHTML=`<div class="search-empty">${icon('search','search-empty-icon')}<h2>${filters.q?'没有匹配的内容':'搜索所有内容'}</h2><p>搜索频道、私信、成员、Agent、任务、文件和消息历史。</p></div>`;
      return;
    }
    const counts=Object.entries(data.counts).map(([kind,count])=>`<span class="search-count">${esc((SEARCH_KIND_META[kind]||{}).label||kind)} <b>${count}</b></span>`).join('');
    board.innerHTML=`<div class="search-summary">共 <b>${data.total}</b> 条结果${counts}</div>${data.results.map(searchResultHtml).join('')}`;
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
  if(kind==='agent' && ref.agent_id){ setPane('chat'); showAgent(ref.agent_id); return; }
  if(channelId && channelId!==state.channelId && kind!=='agent'){
    state.channelId=channelId;
    state.eventSeq=0;
    renderChannels();
    await Promise.all([loadMessages(),loadWorkspace(false)]);
    connectEvents();
  }
  if(kind==='message' && ref.message_id){ setPane('chat'); showMessage(ref.thread_id || ref.message_id); }
  else if(kind==='task' && ref.task_id){ setPane('tasks'); showTask(ref.task_id); }
  else if(kind==='file'){ setPane('files'); }
  else { setPane('chat'); }
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
  if(counter) counter.textContent=state.files.length;
  if(!board) return;
  // The sidebar owns the channel filter; an empty selection means every channel.
  const files=state.fileChannel
    ? state.files.filter(file=>file.channel_id===state.fileChannel)
    : state.files;
  // The files pane owns its channel filter: it is a whole-window view with no
  // sidebar to put one in.
  const counts=new Map();
  state.files.forEach(file=>counts.set(file.channel_id,(counts.get(file.channel_id)||0)+1));
  const options=[{channel_id:'',name:'全部频道'},...state.channels].map(channel=>{
    const count=channel.channel_id ? (counts.get(channel.channel_id)||0) : state.files.length;
    const prefix=channel.channel_id ? (channel.kind==='direct'?'@':'#') : '';
    return `<option value="${esc(channel.channel_id)}" ${channel.channel_id===state.fileChannel?'selected':''}>${prefix}${esc(channel.name)}（${count}）</option>`;
  }).join('');
  const heading=`<div class="task-toolbar"><span class="task-toolbar-title">${icon('paperclip')} 文件 <b>${files.length}</b> / ${state.files.length}</span><label class="search-filter">${icon('hash')}<select id="file-channel">${options}</select></label><button type="button" id="file-refresh" class="secondary">${icon('refresh')} 刷新</button></div>`;
  const bindToolbar=()=>{
    $('file-channel')?.addEventListener('change',event=>{
      state.fileChannel=event.target.value;
      renderFileBoard();
    });
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
    return `<div class="file-row"><span class="file-name" title="${esc(file.summary||file.filename)}">${icon(glyph,'file-glyph')} ${esc(file.filename)}</span><span class="file-channel">${esc(channel?.name||file.channel_id)}</span><span><em class="file-source file-source-${esc(file.source)}">${esc(source)}</em></span><span>${esc(owner)}</span><span>${esc(formatBytes(file.size_bytes))}</span><span>${esc(new Date(file.created_at).toLocaleString())}</span><span><a class="file-download" href="/api/files/${encodeURIComponent(file.file_id)}/download">${icon('download')} 下载</a></span></div>`;
  }).join('')}</div>`;
  bindToolbar();
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

// The sidebar belongs to the pane, not to the workspace: the chat pane needs
// channels, the files pane needs a channel filter, the graph pane needs the
// member roster, and the task and search panes are workspace-wide and take the
// full width instead.
// Only chat keeps the workspace sidebar. Files and the graph are whole-window
// views: each drops the sidebar, the context pane and the channel chrome, and
// carries its own channel filter in its toolbar.
const SIDEBAR_BY_PANE={
  chat:{title:'聊天', subtitle:'本地工作区', action:'新建私信'},
};
const FULL_BLEED_PANES=new Set(['graph','files']);
function sectionHtml(key, label, count, body, action=''){
  const collapsed=state.collapsed[key];
  return `<div class="sidebar-section" data-section="${esc(key)}"><div class="section-title"><button class="section-toggle" type="button" data-toggle-section="${esc(key)}">${icon('chevron',collapsed?'chevron-collapsed':'')} ${esc(label)}</button><span>${count!=null?`<b>${count}</b>`:''}${action}</span></div><div class="section-body" ${collapsed?'hidden':''}>${body}</div></div>`;
}
// Row actions stay hidden until hover so the list reads cleanly, and are real
// buttons rather than a context menu so they are reachable by keyboard.
function rowActionsHtml(kind, id, {edit=true}={}){
  return `<span class="row-actions">${edit?`<button type="button" class="row-action" data-edit-${kind}="${esc(id)}" title="编辑">${icon('pencil')}</button>`:''}<button type="button" class="row-action row-danger" data-delete-${kind}="${esc(id)}" title="删除">${icon('trash')}</button></span>`;
}
function channelButtonHtml(channel){
  return `<div class="row-wrap ${channel.channel_id===state.channelId?'active':''}"><button class="channel" data-channel="${esc(channel.channel_id)}">${icon('hash','channel-icon')} ${esc(channel.name)}</button>${rowActionsHtml('channel',channel.channel_id)}</div>`;
}
function dmButtonHtml(channel){
  const agentId=channel.channel_id.replace(/^dm-/,'');
  const agent=state.agents.find(item=>item.agent_id===agentId);
  const avatar=agent?.avatar_path?`<img src="/${esc(agent.avatar_path)}" alt="">`:esc(initials(agentId));
  const role=agent?.role || roles[agentId] || '';
  // A direct channel is named after its Agent, so only deleting makes sense.
  return `<div class="row-wrap ${channel.channel_id===state.channelId?'active':''}"><button class="channel dm-channel" data-channel="${esc(channel.channel_id)}"><span class="dm-avatar" style="background:${agentColors[agentId] || '#58746a'}">${avatar}</span><b>${esc(channel.name)}</b>${role?`<em>${esc(role)}</em>`:''}</button>${rowActionsHtml('channel',channel.channel_id,{edit:false})}</div>`;
}
function agentRowHtml(agent){
  const status=agent.status || 'online';
  const detail=status==='running' && agent.task_phase ? agent.task_phase : taskStatusText(status);
  const name=agent.name || labels[agent.agent_id] || agent.agent_id;
  const role=agent.role || roles[agent.agent_id] || 'Agent';
  const avatar=agent.avatar_path?`<img src="/${esc(agent.avatar_path)}" alt="">`:esc(initials(agent.agent_id));
  // Every Agent can be removed. Only a custom one can have its profile edited;
  // a built-in role's prompt and contract belong to the plugin.
  const actions=rowActionsHtml('agent',agent.agent_id,{edit:agent.type==='custom'});
  // Name and role sit side by side on one line. A busy Agent shows its live
  // status in the role's place instead, so the roster never hides real state
  // to save a line — CSS picks one of the two from data-status.
  return `<div class="row-wrap"><div class="agent-row" data-agent="${esc(agent.agent_id)}" data-status="${esc(status)}" title="${esc(name)} · ${esc(role)}｜${esc(detail)}"><div class="agent-avatar" style="background:${agentColors[agent.agent_id] || '#58746a'}">${avatar}</div><div class="agent-copy"><strong>${esc(name)}</strong><span class="agent-role-label">${esc(role)}</span><small class="agent-status-label">${esc(detail)}</small></div><i class="status-dot"></i></div>${actions}</div>`;
}
function renderSidebar(){
  const shell=document.querySelector('.app-shell');
  const sidebar=$('sidebar');
  const config=SIDEBAR_BY_PANE[state.activePane];
  const fullBleed=FULL_BLEED_PANES.has(state.activePane);
  if(shell){
    shell.classList.toggle('no-sidebar',!config);
    shell.classList.toggle('full-bleed',fullBleed);
  }
  if(sidebar) sidebar.hidden=!config;
  const context=document.querySelector('.context-pane');
  if(context) context.hidden=fullBleed;
  if(!config || !sidebar) return;
  $('sidebar-title').textContent=config.title;
  $('sidebar-subtitle').textContent=config.subtitle;
  const action=$('sidebar-action');
  if(action){ action.hidden=!config.action; action.title=config.action || ''; }

  const projects=state.channels.filter(channel=>channel.kind!=='direct');
  const directs=state.channels.filter(channel=>channel.kind==='direct');
  const body=$('sidebar-body');
  const plus=id=>`<button id="${id}" class="section-add" type="button">${icon('plus')}</button>`;

  // A removed built-in Agent stays listed so the removal is not a one-way door.
  const removed=state.removedAgents || [];
  const removedRows=removed.map(agent=>`<div class="row-wrap removed-row"><div class="agent-row"><div class="agent-avatar" style="background:#b9a7ad">${esc(initials(agent.agent_id))}</div><div class="agent-copy"><strong>${esc(agent.name||agent.agent_id)}</strong><span class="agent-role-label">${esc(agent.role||'Agent')}</span></div></div><span class="row-actions row-actions-static"><button type="button" class="row-action" data-restore-agent="${esc(agent.agent_id)}" title="恢复">${icon('refresh')}</button></span></div>`).join('');
  body.innerHTML=
    sectionHtml('channels','频道',projects.length,projects.map(channelButtonHtml).join(''),plus('create-channel'))+
    sectionHtml('dms','私信',directs.length,directs.map(dmButtonHtml).join('') || '<p class="sidebar-hint">还没有私信。点右上角 + 发起一条。</p>',plus('create-dm'))+
    sectionHtml('agents','Agent',state.agents.length,state.agents.map(agentRowHtml).join(''),plus('create-agent'))+
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
  document.querySelectorAll('[data-file-channel]').forEach(button=>button.addEventListener('click',()=>{
    state.fileChannel=button.dataset.fileChannel;
    renderSidebar();
    renderFileBoard();
  }));
  $('create-channel')?.addEventListener('click',openChannelDialog);
  $('create-agent')?.addEventListener('click',openAgentDialog);
  $('create-dm')?.addEventListener('click',openDirectMessageDialog);
  $('open-graph')?.addEventListener('click',()=>setPane('graph'));
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

async function deleteAgent(agentId){
  const agent=state.agents.find(item=>item.agent_id===agentId);
  const name=agent?.name || agentId;
  // Removing a built-in role also removes it from the seven-Agent research
  // flow, so say so rather than letting the run fail later.
  const message=agent?.type==='custom'
    ? `确定删除 Agent「${name}」吗？此操作无法恢复。`
    : `确定把内置 Agent「${name}」移出工作区吗？\n它将不再出现，也不能接收任务，完整研究流程会缺少这个角色。\n插件定义保留在本地，之后可以恢复。`;
  if(!window.confirm(message)) return;
  const response=await fetch(`/api/agents/${encodeURIComponent(agentId)}`,{method:'DELETE'});
  const result=await response.json();
  if(!response.ok){ toast(result.error || '删除 Agent 失败'); return; }
  await loadWorkspace(false);
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
  const eyebrow=document.querySelector('.channel-head .eyebrow');
  const heading=document.querySelector('.channel-head h1');
  const subtitle=document.querySelector('.channel-head p');
  const hint=document.querySelector('.composer-hint');
  if(eyebrow) eyebrow.textContent=direct?'私信':'投研协作频道';
  if(heading) heading.textContent=`${direct?'@':'#'} ${name}`;
  if(subtitle){
    subtitle.textContent=channel?.topic
      || channel?.description
      || (direct ? `只有你和 ${name} 的一对一对话` : '人类与多个 Agent 的研究交接、任务和交付');
  }
  if(hint) hint.textContent=`发送消息给 ${direct?'@':'#'}${name}`;
  const input=$('message-input');
  if(input && direct) input.placeholder=`直接跟 ${name} 说…`;
  else if(input) input.placeholder='输入消息，例如：@Fundamental 请补充最近一年经营变化';
  // The tab row belongs to the channel view. A 1:1 conversation has no
  // collaboration structure, so its graph tab goes; the rail keeps the
  // workspace-wide graph reachable from anywhere.
  const graphTab=document.querySelector('.pane-tab[data-pane="graph"]');
  if(graphTab) graphTab.hidden=direct;
  if(direct && state.activePane==='graph') setPane('chat');
}
function bindChannelButtons(){
  const list=$('sidebar-body');
  if(!list) return;
  list.querySelectorAll('[data-channel]').forEach(button=>button.addEventListener('click',async()=>{
    if(button.dataset.channel===state.channelId) return;
    state.channelId=button.dataset.channel;
    state.selectedThreadMessageId=null;
    state.eventSeq=0;
    applyChannelHeader();
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

// The sidebar is re-rendered on every pane switch, so its buttons are bound
// each time rather than once at startup.
function openAgentDialog(){ resetAgentEditor(); $('agent-dialog').showModal(); }
let editingChannelId=null;
function openChannelDialog(){
  editingChannelId=null;
  const form=$('channel-form');
  form.reset();
  $('channel-dialog-title').textContent='创建研究频道';
  $('channel-dialog-subtitle').textContent='一个频道对应一个研究课题和根任务';
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
  setPane('chat');
  await Promise.all([loadWorkspace(false),loadMessages()]);
  connectEvents();
  showAgent(agentId);
  toast(`已打开与 ${data.agent.name || agentId} 的私信`);
}

function setupCreationDialogs(){
  const agentDialog=$('agent-dialog'), channelDialog=$('channel-dialog'), dmDialog=$('dm-dialog');
  $('sidebar-action')?.addEventListener('click',()=>{
    if(state.activePane==='chat') openDirectMessageDialog();
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
      : await fetch('/api/channels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,member_ids:form.getAll('member_ids')})});
    const result=await response.json();
    if(!response.ok){ toast(result.error || (editing?'保存频道失败':'创建频道失败')); return; }
    channelDialog.close();
    formElement.reset();
    editingChannelId=null;
    if(!editing){ state.channelId=result.channel_id; state.eventSeq=0; }
    await loadWorkspace(false);
    await loadMessages();
    applyChannelHeader();
    connectEvents();
    toast(editing ? `频道「${result.name}」已更新` : `研究频道「${result.name}」已创建，根任务等待启动`);
  });
}

document.addEventListener('DOMContentLoaded',setupCreationDialogs);
// Load the graph once at startup so its tab count is real before the pane is
// ever opened, matching how the task and file counts behave.
document.addEventListener('DOMContentLoaded',()=>{ setupRail(); setupPaneTabs(); setupAttachments(); setupSearch(); loadGraph(); });

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
        : `<a class="attachment-file" href="${href}">${icon('file')}<b>${esc(item.filename)}</b><small>${esc(formatBytes(item.size_bytes))}</small></a>`;
    }).join('');
    article.querySelector('.message-main').appendChild(strip);
  });
};
