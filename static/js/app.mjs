import { service } from './service.mjs';
import { button, code, el, empty, kv, pages, views } from './views.mjs';

const $ = (id) => document.getElementById(id);
const nav = $('nav');
const page = $('page');
const title = $('page-title');
const selector = $('session-select');
const dialog = $('dialog');
const errorBox = $('error');
const dialogError = $('dialog-error');
const filters = { event: '', approval: '', tool: '', scope: 'current', audit: '' };
const drafts = new Map();
let sessionId;
let route;
let sessionRows = [];
let unsubscribe;
let loadVersion = 0;
let refreshTimer;
let busy = false;
let returnFocus;

function error(message = '', target = errorBox) {
  target.textContent = message;
  target.hidden = !message;
}
const focus = (node) => node?.focus({ preventScroll: true });
const announce = (message) => { $('status').textContent = message; };
const routeFromHash = () => {
  const name = location.hash.slice(1);
  return Object.hasOwn(pages, name) ? name : 'chat';
};
nav.append(...Object.entries(pages).map(([name, [label, , , icon]]) => el('a', { href: `#${name}` },
  el('span', { class: 'nav-icon', 'aria-hidden': 'true' }, icon), label)));
$('skip').addEventListener('click', (event) => { event.preventDefault(); $('main').focus(); });

function showRoute(navigate = false) {
  route = routeFromHash();
  const [label, heading, description] = pages[route];
  title.textContent = heading;
  $('breadcrumb').textContent = label;
  $('page-description').textContent = description;
  document.title = `${label} · Toy Harness`;
  for (const anchor of nav.querySelectorAll('a')) {
    if (anchor.hash === `#${route}`) anchor.setAttribute('aria-current', 'page');
    else anchor.removeAttribute('aria-current');
  }
  if (navigate) {
    if (dialog.open && !busy) dialog.close();
    error();
    page.replaceChildren(empty('正在加载…'));
    title.focus();
    void refresh();
  }
}
window.addEventListener('hashchange', () => showRoute(true));

function setOptions(rows) {
  const options = rows.map(({ id, title: name }) => el('option', { value: id }, name ?? id));
  selector.replaceChildren(...(options.length ? options : [el('option', {}, '暂无会话')]));
  selector.value = sessionId ?? '';
  selector.disabled = !options.length;
}
function switchSession(id) {
  if (id === sessionId) return;
  if (dialog.open && !busy) dialog.close();
  unsubscribe?.();
  clearTimeout(refreshTimer);
  sessionId = id;
  selector.value = id;
  error();
  page.replaceChildren(empty('正在加载所选会话…'));
  try {
    unsubscribe = service.subscribe(id, () => {
      if (sessionId !== id || busy) return;
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => void refresh(), 0);
    });
  } catch (cause) { error(`事件订阅失败：${cause.message}`); }
  void refresh({ focusSelect: document.activeElement === document.body });
}
selector.addEventListener('change', () => switchSession(selector.value));

function capturePage() {
  const active = document.activeElement;
  return {
    focusId: page.contains(active) ? active.id : '',
    focusKey: page.contains(active) ? active.dataset.focus : '',
    inputRange: active?.id === 'message' || active?.id === 'tool-search' ? [active.selectionStart, active.selectionEnd] : null,
    chat: $('chat-stream')?.scrollTop,
    events: $('event-stream')?.scrollTop,
    fields: ['new-session', 'checkpoint-description'].map((id) => [id, $(id)?.value]),
    open: new Set([...page.querySelectorAll('details[open] > summary')].map((section) => section.parentElement.textContent)),
  };
}
function keyed(key) {
  return [...page.querySelectorAll('[data-focus]')].find((node) => node.dataset.focus === key);
}
function restorePage(before, options) {
  const stream = $('chat-stream');
  const eventStream = $('event-stream');
  if (stream) stream.scrollTop = options.follow || before.chat == null ? stream.scrollHeight : before.chat;
  if (eventStream) eventStream.scrollTop = options.follow ? 0 : before.events ?? 0;
  for (const details of page.querySelectorAll('details')) details.open = before.open.has(details.textContent);
  for (const [id, value] of before.fields) {
    if ($(id) && value != null && options.clearField !== id) $(id).value = value;
  }
  const next = options.focusSelect ? selector : options.focusKey ? keyed(options.focusKey)
    : $(before.focusId) ?? keyed(before.focusKey);
  if (next && !next.disabled) {
    focus(next);
    if (before.inputRange?.every((x) => x != null) && next.id === before.focusId) next.setSelectionRange(...before.inputRange);
  } else if (options.focusKey || before.focusId || before.focusKey) {
    focus([keyed('review'), $('message'), title].find((node) => node && !node.disabled));
  }
}

async function refresh(options = {}) {
  const version = ++loadVersion;
  const id = sessionId;
  const current = route;
  page.setAttribute('aria-busy', 'true');
  try {
    const [rows, snapshot, toolRows, auditRows, settings] = await Promise.all([
      service.listSessions(), id ? service.getSession(id) : null,
      current === 'tools' ? service.listTools() : null,
      current === 'audit' ? service.queryAudit({ session_id: filters.scope === 'current' ? id : undefined }) : null,
      current === 'settings' ? service.getSettings() : null,
    ]);
    if (version !== loadVersion) return;
    sessionRows = rows;
    setOptions(sessionRows);
    const before = capturePage();
    const ui = {
      sessions: sessionRows, tools: toolRows, audit: auditRows, settings,
      filters, draftText: drafts.get(id) ?? '',
      draft: (value) => drafts.set(id, value),
      send: (content, demoWrite) => perform(() => service.send(id, content, { demoWrite }), {
        message: demoWrite ? '已生成模拟审批请求。' : '演示消息已发送。', follow: true,
        after: () => drafts.delete(id), focusKey: demoWrite ? 'review' : undefined,
      }),
      switchSession,
      createSession: (name) => perform(() => service.createSession(name), {
        message: '已创建演示会话。', after: (row) => switchSession(row.id),
      }),
      resume: (approvalId) => perform(() => service.resume(id, approvalId), {
        message: '已处理演示恢复。', follow: true,
      }),
      approval: (approval, trigger) => openApproval(id, approval, trigger),
      checkpoint: (description) => perform(() => service.createCheckpoint(id, description), {
        message: '已创建演示检查点。', clearField: 'checkpoint-description',
      }),
      rollback: (point, trigger) => openRollback(id, point, trigger),
      auditScope: (value) => { filters.scope = value; void refresh(); },
    };
    page.replaceChildren(snapshot || ['tools', 'settings', 'audit'].includes(current)
      ? views[current](snapshot, ui) : empty('暂无可查看的会话。'));
    restorePage(before, options);
  } catch (cause) {
    if (version !== loadVersion) return;
    error(`无法加载此页面：${cause.message}`);
    page.replaceChildren(button('重试', () => { error(); void refresh(); }));
  } finally {
    if (version === loadVersion) page.setAttribute('aria-busy', 'false');
  }
}

async function perform(operation, options = {}) {
  if (busy) return;
  clearTimeout(refreshTimer);
  busy = true;
  const id = sessionId;
  const fromRoute = route;
  const modal = options.modal && dialog.open;
  error('', modal ? dialogError : errorBox);
  if (modal) dialog.setAttribute('aria-busy', 'true');
  try {
    const value = await operation();
    options.after?.(value);
    if (modal) {
      returnFocus = null;
      dialog.close();
    }
    announce(options.message);
    const stillHere = id === sessionId && fromRoute === route;
    await refresh(stillHere ? options : undefined);
  } catch (cause) {
    if (modal && dialog.open) error(cause.message, dialogError);
    else if (id === sessionId && fromRoute === route) error(cause.message);
  } finally {
    busy = false;
    if (modal) dialog.removeAttribute('aria-busy');
  }
}

function openModal(trigger, heading, description, body, ...buttons) {
  if (dialog.open || busy) return;
  returnFocus = trigger;
  $('dialog-title').textContent = heading;
  $('dialog-description').textContent = description;
  $('dialog-body').replaceChildren(body);
  $('dialog-actions').replaceChildren(button('取消', closeModal), ...buttons);
  error('', dialogError);
  dialog.showModal();
  focus($('dialog-close'));
}
function closeModal() { if (!busy) dialog.close(); }
$('dialog-close').addEventListener('click', closeModal);
dialog.addEventListener('cancel', (event) => { if (busy) event.preventDefault(); });
dialog.addEventListener('close', () => {
  if (dialog.open) return;
  if (returnFocus?.isConnected) focus(returnFocus);
  else if (returnFocus && document.activeElement === document.body) focus(title);
  returnFocus = null;
});
dialog.addEventListener('keydown', (event) => {
  if (event.key !== 'Tab') return;
  const controls = [...dialog.querySelectorAll('button:not(:disabled)')];
  const edge = event.shiftKey ? controls[0] : controls.at(-1);
  if (document.activeElement === edge) {
    event.preventDefault();
    focus(event.shiftKey ? controls.at(-1) : controls[0]);
  }
});
function openApproval(id, approval, trigger) {
  const decide = (decision) => perform(() => service.decide(id, approval.id, decision), {
    modal: true, message: '已记录审批决定；恢复演示轮次后才会继续。', focusKey: 'resume',
    after: () => { if (filters.approval && filters.approval !== decision) filters.approval = ''; },
  });
  openModal(trigger, '确认模拟工具请求', '示例模式：只记录决定，不执行真实操作。决定后可手动恢复轮次。',
    el('div', { class: 'stack' }, el('p', {}, approval.description),
      kv([['工具', approval.tool_name], ['审批 ID', approval.id], ['调用 ID', approval.call_id]]), code(approval.arguments)),
    button('拒绝', () => decide('denied'), { class: 'danger' }), button('批准', () => decide('approved'), { class: 'primary' }));
}
function openRollback(id, point, trigger) {
  openModal(trigger, '确认模拟回滚', '这个操作只补偿检查点之后的示例便笺写入。历史和审计不会删除。',
    kv([['检查点', point.description ?? point.id], ['消息序号', point.seq]]),
    button('确认模拟回滚', () => perform(() => service.rollback(id, point.id), {
      modal: true, message: '已处理模拟回滚。', focusKey: point.id,
    }), { class: 'danger' }));
}

async function start() {
  showRoute();
  try {
    sessionRows = await service.listSessions();
    if (sessionRows.length) switchSession(sessionRows[0].id);
    else await refresh();
  } catch (cause) {
    error(`无法启动演示：${cause.message}`);
    page.replaceChildren(button('重试', () => void start()));
    page.setAttribute('aria-busy', 'false');
  }
}
void start();