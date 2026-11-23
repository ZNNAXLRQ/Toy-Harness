import { selectedSession, service } from './service.js';
import { button, code, el, kv, views } from './views.js';

const $ = (id) => document.getElementById(id);
const page = $('page');
const route = page.dataset.page;
const title = $('page-title');
const selector = $('session-select');
const dialog = $('dialog');
const errorBox = $('error');
const dialogError = $('dialog-error');
const filters = { event: '', approval: '', tool: '', scope: 'current', audit: '' };
const drafts = new Map();
let sessionId;
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
$('skip').addEventListener('click', (event) => { event.preventDefault(); $('main').focus(); });
// Back/forward may restore the old JS instance instead of booting a new document.
window.addEventListener('pageshow', (event) => { if (event.persisted) location.reload(); });

function setOptions(rows) {
  const options = rows.map(({ id, title: name }) => el('option', { value: id }, name ?? id));
  selector.replaceChildren(...(options.length ? options : [el('option', {}, '暂无会话')]));
  selector.value = sessionId ?? '';
  selector.disabled = !options.length;
}
function switchSession(id) {
  if (id === sessionId) return;
  if (dialog?.open && !busy) dialog.close();
  unsubscribe?.();
  clearTimeout(refreshTimer);
  sessionId = id;
  selectedSession.save(id);
  selector.value = id;
  error();
  page.hidden = true;
  $('loading').hidden = false;
  $('new-session')?.form.reset();
  $('checkpoint-description')?.form.reset();
  try {
    unsubscribe = service.subscribe(id, () => {
      if (sessionId !== id || busy) return;
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => void refresh(), 0);
    });
  } catch (cause) { error(`事件订阅失败：${cause.message}`); }
  void refresh({ focusSelect: selector.options.length > 1 && document.activeElement === document.body, follow: true });
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
    open: new Set([...page.querySelectorAll('details[open]')].map(disclosureKey)),
  };
}
const disclosureKey = (details) => details.querySelector('pre[id]')?.id ?? details.textContent;
function keyed(key) {
  return [...page.querySelectorAll('[data-focus]')].find((node) => node.dataset.focus === key);
}
function restorePage(before, options) {
  const stream = $('chat-stream');
  const eventStream = $('event-stream');
  if (stream) stream.scrollTop = options.follow || before.chat == null ? stream.scrollHeight : before.chat;
  if (eventStream) eventStream.scrollTop = options.follow ? 0 : before.events ?? 0;
  for (const details of page.querySelectorAll('details')) details.open = before.open.has(disclosureKey(details));
  if (options.clearField) $(options.clearField).value = '';
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
  $('retry').hidden = true;
  page.setAttribute('aria-busy', 'true');
  try {
    const [rows, snapshot, toolRows, auditRows, settings] = await Promise.all([
      service.listSessions(), id ? service.getSession(id) : null,
      route === 'tools' ? service.listTools() : null,
      route === 'audit' ? service.queryAudit({ session_id: filters.scope === 'current' ? id : undefined }) : null,
      route === 'settings' ? service.getSettings() : null,
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
    if (!snapshot && !['tools', 'settings', 'audit'].includes(route)) throw new Error('暂无可查看的会话。');
    views[route](snapshot, ui);
    page.hidden = false;
    $('loading').hidden = true;
    restorePage(before, options);
  } catch (cause) {
    if (version !== loadVersion) return;
    error(`无法加载此页面：${cause.message}`);
    page.hidden = true;
    $('loading').hidden = true;
    $('retry').hidden = false;
  } finally {
    if (version === loadVersion) page.setAttribute('aria-busy', 'false');
  }
}

async function perform(operation, options = {}) {
  if (busy) return;
  clearTimeout(refreshTimer);
  busy = true;
  const id = sessionId;
  const modal = options.modal && dialog?.open;
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
    const stillHere = id === sessionId;
    await refresh(stillHere ? options : undefined);
  } catch (cause) {
    if (modal && dialog.open) error(cause.message, dialogError);
    else if (id === sessionId) error(cause.message);
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
$('dialog-close')?.addEventListener('click', closeModal);
dialog?.addEventListener('cancel', (event) => { if (busy) event.preventDefault(); });
dialog?.addEventListener('close', () => {
  if (dialog.open) return;
  if (returnFocus?.isConnected) focus(returnFocus);
  else if (returnFocus && document.activeElement === document.body) focus(title);
  returnFocus = null;
});
dialog?.addEventListener('keydown', (event) => {
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
  error();
  $('retry').hidden = true;
  try {
    sessionRows = await service.listSessions();
    const selected = selectedSession.read();
    if (sessionRows.length && !sessionId) switchSession((sessionRows.find(({ id }) => id === selected) ?? sessionRows[0]).id);
    else await refresh();
  } catch (cause) {
    error(`无法启动演示：${cause.message}`);
    $('loading').hidden = true;
    $('retry').hidden = false;
    page.setAttribute('aria-busy', 'false');
  }
}
$('retry').addEventListener('click', () => void start());
void start();