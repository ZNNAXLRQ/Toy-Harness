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
const rowHead = (title, extra) => el('div', { class: 'row-head' }, el('h3', {}, title), extra);
const item = (...items) => el('article', { class: 'item stack' }, items);
const actions = (...items) => el('div', { class: 'actions' }, items);
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
function recovery(s, ui) {
  const approval = pendingFor(s);
  if (approval && !approval.decision) {
    return button('查看待审批请求', (e) => ui.approval(approval, e.currentTarget), { 'data-focus': 'review', class: 'primary' });
  }
  if (s.turn?.status === 'open' || approval?.decision) {
    return button('恢复演示轮次', () => ui.resume(approval?.id), { 'data-focus': 'resume', class: 'primary' });
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
  const roles = { user: '你', assistant: '助手', tool: '工具观察', summary: '摘要', system: '系统' };
  list('chat-stream', s.messages.map((m) => el('article', { class: `message ${Object.hasOwn(roles, m.role) ? m.role : ''}` },
    meta(`${roles[m.role] ?? m.role} · #${m.seq} · ${time(m.created_at)}`), el('p', {}, m.content ?? '（无文本内容）'),
    m.tool_call_id && meta(m.tool_call_id))), '这是一条空会话。发送消息开始体验。');
  const next = recovery(s, ui);
  $('recovery').hidden = !next;
  fill('recovery-action', next);
  const render = (type) => list('event-stream', [...s.events].reverse().filter((e) => !type || e.type === type).map((event) =>
    el('article', { class: 'event' }, rowHead(eventTypes[event.type] ?? event.type, meta(`#${event.seq} · ${time(event.created_at)}`)),
      disclose(`载荷 · ${event.type}`, event.payload))), '没有符合筛选条件的事件。');
  select('event-filter', ui.filters.event, (value) => { ui.filters.event = value; render(value); },
    [['', '全部事件'], ...Object.entries(eventTypes)]);
  render(ui.filters.event);
}

function approvals(s, ui) {
  const render = (value) => list('approval-list', [...s.approvals].reverse().filter((a) => !value || (a.decision ?? 'pending') === value)
    .map((a) => item(rowHead(a.tool_name, badge(a.decision ?? 'pending')),
      meta(`${a.id} · 调用 ${a.call_id} · ${time(a.created_at)}`), el('p', {}, a.description),
      a.decided_by && meta(`决策人：${a.decided_by}`), disclose('参数', a.arguments),
      !a.decision && actions(button('审阅请求', (e) => ui.approval(a, e.currentTarget), { 'data-focus': a.id, class: 'primary' })),
      a.decision && a === pendingFor(s) && actions(recovery(s, ui)))), '没有符合筛选条件的审批记录。');
  select('approval-filter', ui.filters.approval, (value) => { ui.filters.approval = value; render(value); });
  render(ui.filters.approval);
}

function sessions(s, ui) {
  list('session-list', ui.sessions.map((row) => item(rowHead(row.title ?? row.id, badge(row.status)),
    meta(`${row.id} · 更新于 ${time(row.updated_at)}`),
    actions(row.id === s.session.id ? meta('当前会话') : button('切换会话', () => ui.switchSession(row.id), { 'data-focus': row.id })))),
  '暂无会话。');
  $('new-session-form').onsubmit = (e) => { e.preventDefault(); ui.createSession($('new-session').value); };
  fill('session-summary', ...pairs([['会话 ID', s.session.id], ['消息数', s.messages.length],
    ['创建时间', time(s.session.created_at)], ['状态', badge(s.session.status)]]));
  fill('recovery-action', recovery(s, ui) ?? empty('当前没有待恢复的轮次。'));
}

function checkpoints(s, ui) {
  $('checkpoint-form').onsubmit = (e) => { e.preventDefault(); ui.checkpoint($('checkpoint-description').value); };
  list('checkpoint-list', [...s.checkpoints].reverse().map((c) => item(rowHead(c.description ?? '未命名检查点', badge(`#${c.seq}`)),
    meta(`${c.id} · ${time(c.created_at)}`), actions(button('模拟回滚至此处', (e) => ui.rollback(c, e.currentTarget),
      { 'data-focus': c.id, disabled: Boolean(s.turn) })))), '尚无检查点。');
  $('note').textContent = s.demo?.note ?? '—';
  $('rollback-blocked').hidden = !s.turn;
  const result = s.demo?.rollback;
  fill('rollback-result', result ? item(rowHead('最近一次回滚', badge(result.failed.length ? 'failed' : 'success')),
    kv([['检查点', result.checkpoint.id], ['已补偿', result.reverted.join('、') || '无'], ['失败数', result.failed.length]]),
    result.failed.length > 0 && disclose('失败详情', result.failed)) : empty('当前会话尚未回滚。'));
  list('tool-call-list', s.tool_calls.map((call) => item(rowHead(call.tool_name, badge(call.status)),
    meta(`${call.id} · ${call.reversible ? '带补偿凭据' : '无补偿凭据'}`), disclose('原始参数', call.arguments))), '暂无工具调用。');
}

function subagents(s) {
  list('subagent-list', s.subagents.map(({ task, result }) => item(
    rowHead(task.id, badge(result?.status ?? 'running')), el('p', {}, task.prompt),
    kv([['父会话', task.parent_session_id], ['父调用', task.parent_call_id], ['允许工具', task.allowed_tools.join('、') || '无'],
      ['深度', task.depth], ['迭代上限', task.max_iterations], ['已报告步数', result?.steps], ['Tokens', result?.tokens]]),
    result ? el('p', {}, result.summary) : notice('尚无结果；此条目是运行中示例。'))), '此会话没有子代理任务。');
}

function tools(s, ui) {
  const render = (value) => {
    const term = value.trim().toLowerCase();
    list('tool-list', ui.tools.map((t) => t.function).filter((t) => `${t.name} ${t.description}`.toLowerCase().includes(term))
      .map((t) => item(rowHead(t.name, badge('只读目录')), el('p', {}, t.description), disclose('参数 Schema', t.parameters))),
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
  const render = (action) => list('audit-list', ui.audit.filter((a) => !action || a.action === action)
    .map((a) => item(rowHead(labels[a.action] ?? a.action, meta(time(a.created_at))),
      kv([['记录 ID', a.id], ['会话', a.session_id], ['操作者', a.actor], ['动作', a.action]]),
      a.detail != null && disclose('审计详情', a.detail))), '没有符合筛选条件的审计记录。');
  select('audit-scope', ui.filters.scope, ui.auditScope);
  select('audit-action', ui.filters.audit, (value) => { ui.filters.audit = value; render(value); },
    [['', '全部动作'], ...Object.entries(labels)]);
  render(ui.filters.audit);
}

function context(s, ui) {
  const { context: ctx, agents_document: doc, session } = s;
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
  const config = ui.settings;
  const profile = config.llm.providers[config.llm.active];
  fill('llm-config', ...pairs([['llm.active', config.llm.active], ['adapter', profile.adapter],
    ['model', profile.model ?? '适配器默认'], ['base_url', profile.base_url ?? '适配器默认'], ['timeout_seconds', profile.timeout_seconds],
    ['max_retries', profile.max_retries]]));
  fill('runtime-config', ...pairs([['server.port', config.server.port], ['database.path', config.database.path],
    ['logging.level', config.logging.level]]));
}

export const views = { chat, approvals, sessions, checkpoints, subagents, tools, audit, context, settings };