const $ = (id) => document.getElementById(id);

// All contract and user values become text nodes, never markup or executable attributes.
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value == null) continue;
    if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  node.append(...children.flat(Infinity).filter((child) => child != null && child !== false));
  return node;
}
export const button = (label, onClick, attrs = {}) => el('button', { type: 'button', onclick: onClick, ...attrs }, label);
const empty = (text) => el('p', { class: 'empty' }, text);
const meta = (text) => el('p', { class: 'meta' }, text);
const notice = (text) => el('p', { class: 'notice' }, text);
const labels = {
  idle: '空闲', open: '未完成', waiting_approval: '等待审批', completed: '已完成', failed: '失败', abandoned: '已放弃',
  pending: '待决定', awaiting_approval: '等待审批', approved: '已批准', denied: '已拒绝', dispatched: '已派发',
  unknown: '未知', success: '成功', cancelled: '已取消', running: '运行中',
};
const badge = (value) => el('span', { class: `badge ${Object.hasOwn(labels, value) ? value : ''}` },
  Object.hasOwn(labels, value) ? labels[value] : value);
const time = (unix) => `${new Date(unix * 1000).toISOString().slice(11, 19)} UTC`;
const json = (data) => JSON.stringify(data, null, 2) ?? '—';
export const code = (data) => el('pre', {}, typeof data === 'string' ? data : json(data));
const disclose = (label, data) => el('details', {}, el('summary', {}, label), code(data));
const pairs = (rows) => rows.flatMap(([key, value]) => [el('dt', {}, key), el('dd', {}, value ?? '—')]);
export const kv = (rows) => el('dl', { class: 'kv' }, pairs(rows));
const fill = (id, ...nodes) => $(id).replaceChildren(...nodes.filter(Boolean));
const list = (id, rows, message) => fill(id, ...(rows.length ? rows : [empty(message)]));
const table = (id, rows, message) => {
  const columns = [...$(id).closest('table').tHead.rows[0].cells].map((cell) => cell.textContent);
  fill(id, ...(rows.length ? rows.map((row) => el('tr', {}, row.map((value, i) =>
    el(i === 0 ? 'th' : 'td', { 'data-label': columns[i], scope: i === 0 ? 'row' : null }, value)))) :
    [el('tr', {}, el('td', { colspan: columns.length }, empty(message)))]));
};
const identity = (mark, name, subtitle) => el('div', { class: 'identity' },
  el('span', { class: 'avatar', 'aria-hidden': 'true' }, mark), el('div', {}, el('strong', {}, name), subtitle && meta(subtitle)));
const rowHead = (title, extra) => el('div', { class: 'row-head' }, el('h3', {}, title), extra);
const item = (...items) => el('article', { class: 'item stack' }, items);
const actions = (...items) => el('div', { class: 'actions' }, items);
// Stable sort preserves source order for records with equal creation times.
const scoped = (ui, rows, owner) => rows.filter((row) => ui.filters.scope !== 'current' || owner(row) === ui.sessionId)
  .sort((a, b) => (b.created_at ?? -Infinity) - (a.created_at ?? -Infinity));
const sessionName = (ui, id) => ui.sessions.find((row) => row.id === id)?.title ?? id;
const ownerLink = (ui, id) => id == null ? '无关联会话' : el('a', { href: ui.chatLink(id) }, `${sessionName(ui, id)} · ${id}`);
const taskItem = (ui, id, createdAt, ...nodes) => el('article', {
  class: 'item stack', 'data-session-id': id, 'data-created-at': createdAt,
}, meta(ownerLink(ui, id)), nodes);
const scope = (id, ui, render) => select(id, ui.filters.scope, (value) => { ui.filters.scope = value; render(); });
function select(id, value, change, options) {
  const control = $(id);
  if (options && !control.options.length) {
    control.replaceChildren(...options.map(([key, name]) => el('option', { value: key }, name)));
  }
  control.value = value;
  control.onchange = () => change(control.value);
}

const eventTypes = {
  user_message: '用户消息', model_response: '模型响应', tool_start: '模拟工具开始', tool_end: '模拟工具结束',
  tool_skipped: '工具未派发', approval_required: '请求审批', approval_decided: '记录审批决定',
  turn_completed: '轮次完成', turn_failed: '轮次失败', subagent_started: '子代理启动', subagent_finished: '子代理结束',
  checkpoint_created: '创建检查点', rollback_performed: '完成模拟回滚',
};
const pendingFor = (s) => s.approvals.find((a) => a.call_id === s.turn?.pending_call_id);
function recovery(s, ui, resumeKey = 'resume') {
  const approval = pendingFor(s);
  if (approval && !approval.decision) {
    return button('查看待审批请求', (e) => ui.approval(approval, e.currentTarget), { 'data-focus': 'review', class: 'primary' });
  }
  if (s.turn?.status === 'open' || approval?.decision) {
    return button('恢复演示轮次', () => ui.resume(s.session.id, approval?.id), { 'data-focus': resumeKey, class: 'primary' });
  }
  return null;
}

function chat(s, ui) {
  const blocked = !['idle', 'completed'].includes(s.session.status);
  const message = $('message');
  message.disabled = $('write').disabled = $('send').disabled = blocked;
  if (message.value !== ui.draftText) message.value = ui.draftText;
  message.oninput = () => ui.draft(message.value);
  $('composer').onsubmit = (e) => { e.preventDefault(); ui.send(message.value, false); };
  $('write').onclick = () => { if (message.reportValidity()) ui.send(message.value, true); };
  fill('session-status', badge(s.session.status));
  $('chat-session').textContent = `会话 · ${s.session.title ?? s.session.id}`;
  const roles = { user: '你', assistant: '助手', tool: '工具观察', summary: '摘要', system: '系统' };
  list('chat-stream', s.messages.map((m, i) => el('article', {
    class: `message ${Object.hasOwn(roles, m.role) ? m.role : ''}${i === s.messages.length - 1 && s.session.status === 'completed' ? ' update' : ''}`,
  }, identity(m.role === 'user' ? 'U' : 'A', roles[m.role] ?? m.role,
    `#${m.seq} · ${time(m.created_at)}`), el('p', { class: 'message-text' }, m.content ?? '（无文本内容）'),
  m.tool_call_id && meta(m.tool_call_id))), '这是一条空会话。发送消息开始体验。');
  const next = recovery(s, ui);
  $('recovery').hidden = !next;
  fill('recovery-action', next);
  const render = (type) => list('event-stream', [...s.events].reverse().filter((e) => !type || e.type === type).map((event) =>
    el('article', { class: 'event', 'data-event': event.type }, rowHead(eventTypes[event.type] ?? event.type, meta(`#${event.seq} · ${time(event.created_at)}`)),
      disclose(`载荷 · ${event.type}`, event.payload))), '没有符合筛选条件的事件。');
  select('event-filter', ui.filters.event, (value) => { ui.filters.event = value; render(value); },
    [['', '全部事件'], ...Object.entries(eventTypes)]);
  render(ui.filters.event);
}

function approvals(s, ui) {
  const rows = ui.snapshots.flatMap((snapshot) => snapshot.approvals.map((approval) => ({ ...approval, snapshot })));
  const render = () => list('approval-list', scoped(ui, rows, (a) => a.session_id)
    .filter((a) => !ui.filters.approval || (a.decision ?? 'pending') === ui.filters.approval)
    .map((a) => taskItem(ui, a.session_id, a.created_at, rowHead(a.tool_name, badge(a.decision ?? 'pending')),
      meta(`${a.id} · 调用 ${a.call_id} · ${time(a.created_at)}`), el('p', {}, a.description),
      a.decided_by && meta(`决策人：${a.decided_by}`), disclose('参数', a.arguments),
      !a.decision && actions(button('审阅请求', (e) => ui.approval(a, e.currentTarget), { 'data-focus': a.id, class: 'primary' })),
      a.decision && a.id === pendingFor(a.snapshot)?.id && actions(recovery(a.snapshot, ui, `resume:${a.id}`)))),
  '没有符合筛选条件的审批记录。');
  scope('approval-scope', ui, render);
  select('approval-filter', ui.filters.approval, (value) => { ui.filters.approval = value; render(); });
  render();
}

function sessions(s, ui) {
  const input = $('session-search');
  const render = () => {
    const term = ui.filters.sessionSearch.trim().toLowerCase();
    table('session-list', ui.sessions.filter((row) => (!ui.filters.sessionStatus || row.status === ui.filters.sessionStatus) &&
      `${row.title ?? ''} ${row.id}`.toLowerCase().includes(term)).map((row) => [
      identity('▤', row.title ?? row.id, row.id), time(row.updated_at), badge(row.status),
      el('a', { href: ui.chatLink(row.id), class: 'button', 'data-focus': row.id }, row.id === s.session.id ? '打开当前会话' : '打开会话'),
    ]), '没有符合筛选条件的会话。');
  };
  input.value = ui.filters.sessionSearch;
  input.oninput = () => { ui.filters.sessionSearch = input.value; render(); };
  select('session-filter', ui.filters.sessionStatus, (value) => { ui.filters.sessionStatus = value; render(); },
    [['', '全部状态'], ...['idle', 'open', 'waiting_approval', 'completed', 'failed', 'abandoned'].map((key) => [key, labels[key]])]);
  render();
  $('new-session-form').onsubmit = (e) => { e.preventDefault(); ui.createSession($('new-session').value); };
  fill('session-summary', ...pairs([['会话 ID', s.session.id], ['消息数', s.messages.length],
    ['创建时间', time(s.session.created_at)], ['状态', badge(s.session.status)]]));
  fill('recovery-action', recovery(s, ui) ?? empty('当前没有待恢复的轮次。'));
}

function checkpoints(s, ui) {
  $('checkpoint-form').onsubmit = (e) => { e.preventDefault(); ui.checkpoint($('checkpoint-description').value); };
  const points = scoped(ui, ui.snapshots.flatMap((snapshot) => snapshot.checkpoints), (c) => c.session_id);
  scope('checkpoint-scope', ui, () => checkpoints(s, ui));
  const target = $('rollback-target');
  const selected = target.value;
  $('checkpoint-count').textContent = `${points.length} 个检查点`;
  list('checkpoint-list', points.map((c) => taskItem(ui, c.session_id, c.created_at, identity('●', c.description ?? '未命名检查点',
    `${c.id} · #${c.seq} · ${time(c.created_at)}`))), '尚无检查点。');
  const key = (c) => `${c.session_id}:${c.id}`;
  target.replaceChildren(...(points.length ? points.map((c) => el('option', { value: key(c) },
    `${sessionName(ui, c.session_id)} · ${c.description ?? '未命名检查点'} · ${c.id}`)) :
    [el('option', { value: '' }, '暂无检查点')]));
  target.value = points.some((c) => key(c) === selected) ? selected : points[0] ? key(points[0]) : '';
  target.disabled = !points.length;
  const restore = $('rollback-review');
  const updateTarget = () => {
    const point = points.find((c) => key(c) === target.value);
    const owner = ui.snapshots.find((snapshot) => snapshot.session.id === point?.session_id);
    const blocked = Boolean(owner?.turn);
    restore.disabled = !point || blocked;
    $('rollback-blocked').hidden = !blocked;
    restore.onclick = () => { if (point && !blocked) ui.rollback(point, restore); };
    const result = owner?.demo?.rollback;
    fill('rollback-result', result ? item(rowHead('目标会话最近一次回滚', badge(result.failed.length ? 'failed' : 'success')),
      kv([['所属会话', ownerLink(ui, point.session_id)], ['检查点', result.checkpoint.id],
        ['已补偿', result.reverted.join('、') || '无'], ['失败数', result.failed.length]]),
      result.failed.length > 0 && disclose('失败详情', result.failed)) : empty(point ? '目标会话尚未回滚。' : '没有可查看的回滚结果。'));
  };
  target.onchange = updateTarget;
  updateTarget();
  fill('checkpoint-session', ownerLink(ui, s.session.id));
  $('note').textContent = s.demo?.note ?? '—';
  list('tool-call-list', s.tool_calls.map((call) => item(rowHead(call.tool_name, badge(call.status)),
    meta(`${call.id} · ${call.reversible ? '带补偿凭据' : '无补偿凭据'}`), disclose('原始参数', call.arguments))), '暂无工具调用。');
}

function subagents(s, ui) {
  const all = ui.snapshots.flatMap((snapshot) => snapshot.subagents);
  const key = (task) => `${task.parent_session_id}:${task.id}`;
  const render = () => {
    const rows = scoped(ui, all, ({ task }) => task.parent_session_id);
    const current = rows.find(({ task }) => key(task) === ui.filters.subagent) ?? rows[0];
    $('subagent-count').textContent = `${rows.length} 个任务`;
    list('subagent-list', rows.map(({ task, result, created_at }) => taskItem(ui, task.parent_session_id, created_at, el('div', { class: 'row-head' },
      identity('↗', task.id, task.prompt), badge(result?.status ?? 'running')), actions(button('查看详情', () => {
      ui.filters.subagent = key(task);
      render();
      $('subagent-detail-title').focus();
    }, { 'aria-controls': 'subagent-detail', 'aria-current': current.task === task ? 'true' : null })))), '没有符合范围的子代理任务。');
    fill('subagent-detail', current ? kv([['任务', current.task.id], ['状态', badge(current.result?.status ?? 'running')],
      ['父会话', ownerLink(ui, current.task.parent_session_id)], ['父调用', current.task.parent_call_id], ['允许工具', current.task.allowed_tools.join('、') || '无'],
      ['深度', current.task.depth], ['迭代上限', current.task.max_iterations], ['已报告步数', current.result?.steps], ['Tokens', current.result?.tokens]]) :
      empty('没有可查看的任务。'));
    list('subagent-results', rows.filter(({ result }) => result).map(({ task, result, created_at }) =>
      taskItem(ui, task.parent_session_id, created_at, rowHead(task.id, badge(result.status)), el('p', {}, result.summary))), '没有已返回的结果。');
  };
  scope('subagent-scope', ui, render);
  render();
}

function tools(s, ui) {
  const render = (value) => {
    const term = value.trim().toLowerCase();
    table('tool-list', ui.tools.map((t) => t.function).filter((t) => `${t.name} ${t.description}`.toLowerCase().includes(term))
      .map((t) => [identity('✣', t.name), t.description, t.name === 'demo_note' ? el('span', { class: 'badge pending' }, '需要审批') : '不执行',
        badge(t.name === 'demo_note' ? '模拟' : '只读目录'), disclose('参数 Schema', t.parameters)]),
    '没有匹配的工具。');
  };
  const input = $('tool-search');
  input.value = ui.filters.tool;
  input.oninput = () => { ui.filters.tool = input.value; render(input.value); };
  render(input.value);
}

function audit(s, ui) {
  const labels = { session_created: '创建会话', approval_decided: '审批决定', subagent_started: '启动子代理',
    subagent_finished: '子代理结束', rollback_performed: '模拟回滚' };
  const input = $('audit-search');
  const render = () => {
    const term = ui.filters.auditSearch.trim().toLowerCase();
    table('audit-list', scoped(ui, ui.audit, (a) => a.session_id).filter((a) => (!ui.filters.audit || a.action === ui.filters.audit) &&
      `${a.actor} ${a.action} ${labels[a.action] ?? ''} ${a.session_id} ${ui.sessions.find((row) => row.id === a.session_id)?.title ?? ''}`.toLowerCase().includes(term))
      .map((a) => [el('time', { datetime: new Date(a.created_at * 1000).toISOString() }, time(a.created_at)), a.actor,
        el('strong', { class: 'event-label' }, labels[a.action] ?? a.action),
        [meta(ownerLink(ui, a.session_id)), meta(a.id), a.detail != null && disclose('审计详情', a.detail)],
        a.detail?.decision || a.detail?.status ? badge(a.detail.decision ?? a.detail.status) : '—']), '没有符合筛选条件的审计记录。');
  };
  input.value = ui.filters.auditSearch;
  input.oninput = () => { ui.filters.auditSearch = input.value; render(); };
  scope('audit-scope', ui, render);
  select('audit-action', ui.filters.audit, (value) => { ui.filters.audit = value; render(); },
    [['', '全部动作'], ...Object.entries(labels)]);
  render();
}

function context(s, ui) {
  const { context: ctx, agents_document: doc, session } = s;
  $('context-session').textContent = session.title ?? session.id;
  $('context-tokens').textContent = ctx.estimated_tokens;
  fill('context-summary', ...pairs([['前缀哈希', session.prefix_hash], ['AGENTS.md 版本', session.agents_md_version],
    ['估算 Tokens', ctx.estimated_tokens], ['剔除历史条数', ctx.trimmed_messages], ['已摘要', ctx.summarized ? '是' : '否']]));
  $('context-prefix').textContent = json(ctx.prefix);
  $('context-messages').textContent = json(ctx.messages);
  fill('agents-summary', ...pairs([['来源', doc.source], ['文档版本', doc.version], ['Skills', doc.skills.join('、') || '无']]));
  $('agents-content').textContent = doc.content;
  fill('turn-summary', ...pairs([['会话状态', badge(session.status)], ['未完成轮次', s.turn?.status], ['等待的调用', s.turn?.pending_call_id],
    ['迭代次数', s.turn?.iteration ?? s.outcome?.iterations], ['本轮用量（非持久化）', s.outcome?.tokens], ['错误', s.outcome?.error]]));
  fill('recovery-action', recovery(s, ui) ?? empty('没有待恢复的轮次。'));
}

function settings(s, ui) {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const activate = (index) => {
    tabs.forEach((tab, i) => {
      tab.setAttribute('aria-selected', String(i === index));
      tab.tabIndex = i === index ? 0 : -1;
      $(tab.getAttribute('aria-controls')).hidden = i !== index;
    });
    tabs[index].focus();
  };
  tabs.forEach((tab, i) => {
    tab.onclick = () => activate(i);
    tab.onkeydown = (event) => {
      const next = { ArrowLeft: (i + tabs.length - 1) % tabs.length, ArrowRight: (i + 1) % tabs.length,
        Home: 0, End: tabs.length - 1 };
      if (!Object.hasOwn(next, event.key)) return;
      event.preventDefault();
      activate(next[event.key]);
    };
  });
  const config = ui.settings;
  const profile = config.llm.providers[config.llm.active];
  fill('llm-config', ...pairs([['llm.active', config.llm.active], ['adapter', profile.adapter],
    ['model', profile.model ?? '适配器默认'], ['base_url', profile.base_url ?? '适配器默认'], ['timeout_seconds', profile.timeout_seconds],
    ['max_retries', profile.max_retries]]));
  fill('runtime-config', ...pairs([['server.port', config.server.port], ['database.path', config.database.path],
    ['logging.level', config.logging.level]]));
}

export const views = { chat, approvals, sessions, checkpoints, subagents, tools, audit, context, settings };