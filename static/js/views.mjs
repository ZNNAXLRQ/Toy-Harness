export const pages = {
  chat: ['聊天', '聊天与实时事件', '发送演示消息，并在同一时间线查看模型、工具与审批事件。', '◉'],
  approvals: ['审批', '审批队列', '记录批准或拒绝；决定本身不会派发工具。', '◇'],
  sessions: ['会话与恢复', '会话与恢复', '切换独立会话、查看历史，并继续挂起的演示轮次。', '▤'],
  checkpoints: ['检查点与回滚', '检查点与回滚', '只补偿检查点之后可逆的模拟写入，不删除消息历史。', '↶'],
  subagents: ['子代理', '子代理进度', '查看父会话中的运行任务、工具白名单与结果摘要。', '◎'],
  tools: ['工具目录', '只读工具目录', '检查工具描述与参数 Schema；此处不能运行或注册工具。', '▦'],
  audit: ['审计日志', '审计日志', '查看会话创建、审批决策、子代理和回滚记录。', '≡'],
  context: ['上下文与恢复', '上下文与恢复', '检查会话级稳定前缀、消息尾部和未完成轮次。', '◫'],
  settings: ['设置', '设置 · 只读契约', '展示配置字段的示例默认值；不读取本机配置，不接受或保存密钥。', '⚙'],
};

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
export const link = (label, page) => el('a', { href: `#${page}` }, label);
export const empty = (text) => el('p', { class: 'empty' }, text);
const meta = (text) => el('p', { class: 'meta' }, text);
const notice = (text) => el('p', { class: 'notice' }, text);
const labels = {
  idle: '空闲', open: '未完成', waiting_approval: '等待审批', completed: '已完成', failed: '失败', abandoned: '已放弃',
  pending: '待决定', awaiting_approval: '等待审批', approved: '已批准', denied: '已拒绝', dispatched: '已派发',
  unknown: '未知', success: '成功', cancelled: '已取消', running: '运行中',
};
export const badge = (value) => el('span', { class: `badge ${Object.hasOwn(labels, value) ? value : ''}` },
  Object.hasOwn(labels, value) ? labels[value] : value);
const time = (unix) => `${new Date(unix * 1000).toISOString().slice(11, 19)} UTC`;
const json = (data) => JSON.stringify(data, null, 2) ?? '—';
export const code = (data) => el('pre', {}, typeof data === 'string' ? data : json(data));
const disclose = (label, data) => el('details', {}, el('summary', {}, label), code(data));
export const kv = (rows) => el('dl', { class: 'kv' }, rows.flatMap(([key, value]) => [el('dt', {}, key), el('dd', {}, value ?? '—')]));
const heading = (title, extra) => el('div', { class: 'section-head' }, el('h2', {}, title), extra);
const rowHead = (title, extra) => el('div', { class: 'row-head' }, el('h3', {}, title), extra);
const panel = (title, ...items) => el('section', { class: 'card panel' }, title && el('h2', {}, title), items);
const item = (...items) => el('article', { class: 'item stack' }, items);
const stack = (rows, message) => el('div', { class: 'stack' }, rows.length ? rows : empty(message));
const field = (label, control, grow = false) => el('label', { class: `field${grow ? ' grow' : ''}` }, label, control);
const actions = (...items) => el('div', { class: 'actions' }, items);
const select = (value, options, change, id) => {
  const control = el('select', { id, onchange: (event) => change(event.target.value) },
    options.map(([key, name]) => el('option', { value: key }, name)));
  control.value = value;
  return control;
};
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
  const message = el('textarea', { id: 'message', maxlength: '4000', required: true, disabled: blocked,
    placeholder: '输入演示消息…', oninput: (e) => ui.draft(e.target.value) });
  message.value = ui.draftText;
  const send = (demoWrite) => ui.send(message.value, demoWrite);
  const composer = el('form', { class: 'composer stack', onsubmit: (e) => { e.preventDefault(); send(false); } },
    field('消息', message), actions(
      button('模拟更新便笺', () => { if (message.reportValidity()) send(true); }, { disabled: blocked, 'data-focus': 'write' }),
      el('button', { type: 'submit', class: 'primary', disabled: blocked, 'data-focus': 'send' }, '发送消息')));
  const roles = { user: '你', assistant: '助手', tool: '工具观察', summary: '摘要', system: '系统' };
  const messages = el('div', { id: 'chat-stream', class: 'chat-stream', tabindex: '0', 'aria-label': '消息历史' },
    s.messages.length ? s.messages.map((m) => el('article', { class: `message ${Object.hasOwn(roles, m.role) ? m.role : ''}` },
      meta(`${roles[m.role] ?? m.role} · #${m.seq} · ${time(m.created_at)}`), el('p', {}, m.content ?? '（无文本内容）'),
      m.tool_call_id && meta(m.tool_call_id))) : empty('这是一条空会话。发送消息开始体验。'));
  const next = recovery(s, ui);
  const thread = el('section', { class: 'card chat-panel' }, heading('会话消息', badge(s.session.status)), messages,
    next && el('div', { class: 'composer actions' }, next, link('上下文与恢复', 'context')), composer);

  const stream = el('div', { class: 'event-stream', id: 'event-stream', tabindex: '0', 'aria-label': '实时事件历史' });
  const renderEvents = (type) => {
    const events = s.events.filter((event) => !type || event.type === type);
    stream.replaceChildren(...(events.length ? [...events].reverse().map((event) => el('article', { class: 'event' },
      rowHead(eventTypes[event.type] ?? event.type, meta(`#${event.seq} · ${time(event.created_at)}`)),
      disclose(`载荷 · ${event.type}`, event.payload))) : [empty('没有符合筛选条件的事件。')]));
  };
  const filter = select(ui.filters.event, [['', '全部事件'], ...Object.entries(eventTypes)], (value) => {
    ui.filters.event = value;
    renderEvents(value);
  }, 'event-filter');
  renderEvents(ui.filters.event);
  return el('div', { class: 'grid split' }, thread,
    panel(null, heading('实时事件', badge('本地订阅')), el('div', { class: 'filters' }, field('事件类型', filter, true)), stream));
}

function approvals(s, ui) {
  const list = el('div', { class: 'stack' });
  const render = (value) => {
    const rows = [...s.approvals].reverse().filter((a) => !value || (a.decision ?? 'pending') === value);
    list.replaceChildren(...(rows.length ? rows.map((a) => item(rowHead(a.tool_name, badge(a.decision ?? 'pending')),
      meta(`${a.id} · ${a.call_id} · ${time(a.created_at)}`), el('p', {}, a.description),
      a.decided_by && meta(`决策人：${a.decided_by}`), disclose('参数', a.arguments),
      !a.decision && actions(button('审阅请求', (e) => ui.approval(a, e.currentTarget), { 'data-focus': a.id, class: 'primary' })),
      a.decision && a === pendingFor(s) && actions(recovery(s, ui)))) : [empty('没有符合筛选条件的审批记录。')]));
  };
  const filter = select(ui.filters.approval, [['', '全部记录'], ['pending', '待决定'], ['approved', '已批准'], ['denied', '已拒绝']],
    (value) => { ui.filters.approval = value; render(value); }, 'approval-filter');
  render(ui.filters.approval);
  return panel('当前会话的审批', el('div', { class: 'filters' }, field('决定', filter)), list);
}

function sessions(s, ui) {
  const input = el('input', { id: 'new-session', maxlength: '80', placeholder: '未填写时使用「新会话」' });
  return el('div', { class: 'grid equal' }, panel('所有演示会话', stack(ui.sessions.map((row) => item(
    rowHead(row.title ?? row.id, badge(row.status)), meta(`${row.id} · 更新于 ${time(row.updated_at)}`),
    actions(row.id === s.session.id ? meta('当前会话') : button('切换会话', () => ui.switchSession(row.id), { 'data-focus': row.id })))),
  '暂无会话。')),
  el('div', { class: 'stack' }, panel('创建演示会话', el('form', { class: 'stack', onsubmit: (e) => {
    e.preventDefault(); ui.createSession(input.value);
  } }, field('会话名称', input), actions(el('button', { type: 'submit', class: 'primary', 'data-focus': 'new-session-submit' }, '新建会话')))),
  panel('当前会话与历史', el('div', { class: 'stack' }, kv([['会话 ID', s.session.id], ['消息数', s.messages.length],
    ['创建时间', time(s.session.created_at)], ['状态', badge(s.session.status)]]),
  actions(link('查看聊天历史', 'chat'), link('查看上下文', 'context')), recovery(s, ui) ?? empty('当前没有待恢复的轮次。')))));
}

function checkpoints(s, ui) {
  const input = el('input', { id: 'checkpoint-description', maxlength: '100', placeholder: '例如：变更前（可选）' });
  const result = s.demo?.rollback;
  return el('div', { class: 'grid equal' }, panel('会话检查点', el('form', { class: 'filters', onsubmit: (e) => {
    e.preventDefault(); ui.checkpoint(input.value);
  } }, field('检查点说明', input, true), el('button', { type: 'submit', class: 'primary', 'data-focus': 'new-checkpoint' }, '创建检查点')),
  stack([...s.checkpoints].reverse().map((c) => item(rowHead(c.description ?? '未命名检查点', badge(`#${c.seq}`)),
    meta(`${c.id} · ${time(c.created_at)}`), actions(button('模拟回滚至此处', (e) => ui.rollback(c, e.currentTarget),
      { 'data-focus': c.id, disabled: Boolean(s.turn) })))), '尚无检查点。')),
  panel('模拟工作区', el('div', { class: 'stack' }, notice('演示便笺只存在于内存。回滚按逆序补偿，不重写历史。'),
    s.demo && code(s.demo.note), s.turn && notice('当前轮次未结束，暂不能回滚。'),
    result ? item(rowHead('最近一次回滚', badge(result.failed.length ? 'failed' : 'success')),
      kv([['检查点', result.checkpoint.id], ['已补偿', result.reverted.join('、') || '无'], ['失败数', result.failed.length]]),
      result.failed.length > 0 && disclose('失败详情', result.failed)) : empty('当前会话尚未回滚。'),
    el('h3', {}, '工具调用历史'), stack(s.tool_calls.map((call) => item(rowHead(call.tool_name, badge(call.status)),
      meta(`${call.id} · ${call.reversible ? '带补偿凭据' : '无补偿凭据'}`), disclose('原始参数', call.arguments))), '暂无工具调用。'))));
}

function subagents(s) {
  return panel('当前会话的子代理任务', stack(s.subagents.map(({ task, result }) => item(
    rowHead(task.id, badge(result?.status ?? 'running')), el('p', {}, task.prompt),
    kv([['父会话', task.parent_session_id], ['父调用', task.parent_call_id], ['允许工具', task.allowed_tools.join('、') || '无'],
      ['深度', task.depth], ['迭代上限', task.max_iterations], ['已报告步数', result?.steps], ['Tokens', result?.tokens]]),
    result ? el('p', {}, result.summary) : notice('尚无结果；此条目是运行中示例。'))), '此会话没有子代理任务。'));
}

function tools(s, ui) {
  const list = el('div', { class: 'stack' });
  const render = (value) => {
    const term = value.trim().toLowerCase();
    const rows = ui.tools.map((t) => t.function).filter((t) => `${t.name} ${t.description}`.toLowerCase().includes(term));
    list.replaceChildren(...(rows.length ? rows.map((t) => item(rowHead(t.name, badge('只读目录')),
      el('p', {}, t.description), disclose('参数 Schema', t.parameters))) : [empty('没有匹配的工具。')]));
  };
  const input = el('input', { id: 'tool-search', type: 'search', placeholder: '名称或描述', oninput: (e) => {
    ui.filters.tool = e.target.value; render(e.target.value);
  } });
  input.value = ui.filters.tool;
  render(input.value);
  return panel('已注册的示例工具', el('div', { class: 'filters' }, field('搜索工具', input, true)), list);
}

function audit(s, ui) {
  const labels = { session_created: '创建会话', approval_decided: '审批决定', subagent_started: '启动子代理',
    subagent_finished: '子代理结束', rollback_performed: '模拟回滚' };
  const list = el('div', { class: 'stack' });
  const render = (action) => {
    const rows = ui.audit.filter((a) => !action || a.action === action);
    list.replaceChildren(...(rows.length ? rows.map((a) => item(rowHead(labels[a.action] ?? a.action, meta(time(a.created_at))),
      kv([['记录 ID', a.id], ['会话', a.session_id], ['操作者', a.actor], ['动作', a.action]]),
      a.detail != null && disclose('审计详情', a.detail))) : [empty('没有符合筛选条件的审计记录。')]));
  };
  const scope = select(ui.filters.scope, [['current', '当前会话'], ['all', '所有会话']], ui.auditScope, 'audit-scope');
  const action = select(ui.filters.audit, [['', '全部动作'], ...Object.entries(labels)], (value) => {
    ui.filters.audit = value; render(value);
  }, 'audit-action');
  render(ui.filters.audit);
  return panel('审计记录', el('div', { class: 'filters' }, field('会话范围', scope), field('动作', action)), list);
}

function context(s, ui) {
  const { context: ctx, agents_document: doc, session } = s;
  return el('div', { class: 'grid equal' }, panel('上下文快照', el('div', { class: 'stack' },
    kv([['前缀哈希', session.prefix_hash], ['AGENTS.md 版本', session.agents_md_version], ['估算 Tokens', ctx.estimated_tokens],
      ['剔除历史条数', ctx.trimmed_messages], ['已摘要', ctx.summarized ? '是' : '否']]),
    disclose('稳定前缀', ctx.prefix), disclose('消息尾部', ctx.messages))),
  el('div', { class: 'stack' }, panel('AGENTS.md', kv([['来源', doc.source], ['文档版本', doc.version], ['Skills', doc.skills.join('、') || '无']]),
    disclose('查看文档内容', doc.content)), panel('轮次恢复', el('div', { class: 'stack' },
    kv([['会话状态', badge(session.status)], ['未完成轮次', s.turn?.status], ['等待的调用', s.turn?.pending_call_id],
      ['迭代次数', s.turn?.iteration ?? s.outcome?.iterations], ['本轮用量（非持久化）', s.outcome?.tokens], ['错误', s.outcome?.error]]),
    recovery(s, ui) ?? empty('没有待恢复的轮次。'), actions(link('查看会话历史', 'chat'), link('返回会话列表', 'sessions'))))));
}

function settings(s, ui) {
  const config = ui.settings;
  const profile = config.llm.providers[config.llm.active];
  return el('div', { class: 'grid equal' }, panel('LLM Profile', kv([['llm.active', config.llm.active], ['adapter', profile.adapter],
    ['model', profile.model ?? '适配器默认'], ['base_url', profile.base_url ?? '适配器默认'], ['timeout_seconds', profile.timeout_seconds],
    ['max_retries', profile.max_retries]])), panel('运行配置', kv([['server.port', config.server.port],
    ['database.path', config.database.path], ['logging.level', config.logging.level]])));
}

export const views = { chat, approvals, sessions, checkpoints, subagents, tools, audit, context, settings };