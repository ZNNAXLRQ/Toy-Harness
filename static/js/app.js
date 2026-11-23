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
const filters = { event: '', approval: '', tool: '', scope: 'all', audit: '', auditSearch: '',
  sessionSearch: '', sessionStatus: '', subagent: '' };
const drafts = new Map();
let sessionId;
let sessionRows = [];
let unsubscribe;
let loadVersion = 0;
let refreshTimer;
let busy = false;
let returnFocus;
const sessionPages = new Set(['approvals', 'checkpoints', 'subagents']);
const urlSession = () => new URLSearchParams(location.search).get('session');
function sessionLink(page, id) {
  const url = new URL(page, location.href);
  url.searchParams.set('session', id);
  return `${url.pathname}${url.search}${url.hash}`;
}
const chatLink = (id) => sessionLink('index.html', id);

function error(message = '', target = errorBox) {
  target.textContent = message;
  target.hidden = !message;
}
const focus = (node) => node?.focus({ preventScroll: true });
const announce = (message) => { $('status').textContent = message; };
let focusNewSession = route === 'sessions' && location.hash === '#new-session';
window.addEventListener('hashchange', () => {
  if (route === 'sessions' && location.hash === '#new-session') $('new-session').focus();
});
$('skip').addEventListener('click', (event) => { event.preventDefault(); $('main').focus(); });
// Keep a cached document from briefly exposing stale actions before it reloads.
window.addEventListener('pagehide', (event) => {
  if (event.persisted) {
    page.hidden = true;
    $('session-history').replaceChildren();
    $('loading').hidden = false;
  }
});
// Back/forward may restore the old JS instance instead of booting a new document.
window.addEventListener('pageshow', (event) => { if (event.persisted) location.reload(); });

function renderNavigation(rows) {
  const options = rows.map(({ id, title: name }) => el('option', { value: id }, name ?? id));
  selector.replaceChildren(...(options.length ? options : [el('option', {}, '暂无会话')]));
  selector.value = sessionId ?? '';
  selector.disabled = !options.length;
  $('session-history').replaceChildren(...[...rows].sort((a, b) => b.created_at - a.created_at)
    .map(({ id, title: name }) => el('a', {
      href: chatLink(id), 'data-session-id': id,
      'aria-current': id === sessionId ? (route === 'chat' ? 'page' : 'true') : null,
    }, name ?? id)));
  if (sessionId) {
    for (const link of document.querySelectorAll('.task-nav a, .new-link, .sidebar-foot, [data-current-session]')) {
      link.href = sessionLink(link.href, sessionId);
    }
  }
}
function switchSession(id, historyMethod) {
  if (historyMethod) history[historyMethod](null, '', sessionLink(location.href, id));
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
  void refresh({ follow: true });
}
selector.addEventListener('change', () => {
  if (route === 'chat') location.assign(chatLink(selector.value));
  else switchSession(selector.value, 'pushState');
});
window.addEventListener('popstate', () => {
  const id = urlSession();
  if (sessionRows.some((row) => row.id === id)) switchSession(id);
});

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
  const next = options.focusKey ? keyed(options.focusKey)
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
    const [workspaces, toolRows, auditRows, settings] = await Promise.all([
      service.listWorkspaces(),
      route === 'tools' ? service.listTools() : null,
      route === 'audit' ? service.queryAudit({ limit: Infinity }) : null,
      route === 'settings' ? service.getSettings() : null,
    ]);
    const [workspace] = workspaces;
    const rows = workspace.sessions;
    const snapshots = await Promise.all((sessionPages.has(route) ? rows.map((row) => row.id)
      : id && !['tools', 'settings', 'audit'].includes(route) ? [id] : []).map((key) => service.getSession(key)));
    const snapshot = snapshots.find((s) => s.session.id === id);
    if (version !== loadVersion) return;
    sessionRows = rows;
    $('workspace-name').textContent = workspace.title;
    renderNavigation(sessionRows);
    const before = capturePage();
    const ui = {
      sessions: sessionRows, snapshots, sessionId: id, chatLink, tools: toolRows, audit: auditRows, settings,
      filters, draftText: drafts.get(id) ?? '',
      draft: (value) => drafts.set(id, value),
      send: (content, demoWrite) => perform(() => service.send(id, content, { demoWrite }), {
        message: demoWrite ? '已生成模拟审批请求。' : '演示消息已发送。', follow: true,
        after: () => drafts.delete(id), focusKey: demoWrite ? 'review' : undefined,
      }),
      createSession: (name) => perform(() => service.createSession(name), {
        redirect: (row) => chatLink(row.id),
      }),
      resume: (ownerId, approvalId) => perform(() => service.resume(ownerId, approvalId), {
        message: '已处理演示恢复。', follow: true,
      }),
      approval: openApproval,
      checkpoint: (description) => perform(() => service.createCheckpoint(id, description), {
        message: '已创建演示检查点。', clearField: 'checkpoint-description',
      }),
      rollback: openRollback,
    };
    if (!snapshot && !['tools', 'settings', 'audit'].includes(route)) throw new Error('暂无可查看的会话。');
    views[route](snapshot, ui);
    page.hidden = false;
    $('loading').hidden = true;
    restorePage(before, options);
    if (focusNewSession) {
      focusNewSession = false;
      $('new-session').focus();
    }
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
    if (options.redirect) { location.assign(options.redirect(value)); return; }
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

function openModal(trigger, heading, description, body, buttons, variant = '') {
  if (dialog.open || busy) return;
  returnFocus = trigger;
  $('dialog-title').textContent = heading;
  $('dialog-description').textContent = description;
  $('dialog-body').replaceChildren(body);
  dialog.dataset.variant = variant;
  $('dialog-actions').replaceChildren(...buttons);
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
function openApproval(approval, trigger) {
  const id = approval.session_id;
  const decide = (decision) => perform(() => service.decide(id, approval.id, decision), {
    modal: true, message: '已记录审批决定；恢复演示轮次后才会继续。',
    focusKey: route === 'approvals' ? `resume:${approval.id}` : 'resume',
    after: () => { if (filters.approval && filters.approval !== decision) filters.approval = ''; },
  });
  openModal(trigger, '需要审批', '审阅模拟工具请求。此处只记录决定，不执行真实操作。',
    el('div', { class: 'stack approval-body' },
      el('div', { class: 'inset' }, kv([['所属会话', ownerLabel(id)], ['工具', approval.tool_name], ['审批 ID', approval.id], ['调用 ID', approval.call_id]]),
        el('h3', {}, '拟提交的参数'), code(approval.arguments)),
      el('p', {}, approval.description), el('span', { class: 'badge pending' }, '操作 · 模拟便笺更新'),
      el('p', { class: 'meta' }, '批准或拒绝后都需要手动恢复轮次；拒绝不会派发此工具。')),
    [button('拒绝', () => decide('denied'), { class: 'danger' }), button('批准', () => decide('approved'), { class: 'primary' })], 'approval');
}
function ownerLabel(id) {
  return `${sessionRows.find((row) => row.id === id)?.title ?? '会话'} · ${id}`;
}
function openRollback(point, trigger) {
  const id = point.session_id;
  openModal(trigger, '确认模拟回滚', '这个操作只补偿检查点之后的示例便笺写入。历史和审计不会删除。',
    kv([['所属会话', ownerLabel(id)], ['检查点', point.description ?? point.id], ['消息序号', point.seq]]),
    [button('取消', closeModal), button('确认模拟回滚', () => perform(() => service.rollback(id, point.id), {
      modal: true, message: '已处理模拟回滚。', focusKey: 'rollback-review',
    }), { class: 'danger' })]);
}

async function start() {
  error();
  $('retry').hidden = true;
  try {
    const [workspace] = await service.listWorkspaces();
    sessionRows = workspace.sessions;
    const requested = urlSession();
    const selected = selectedSession.read();
    if (requested !== null && !sessionRows.some(({ id }) => id === requested)) {
      renderNavigation(sessionRows);
      throw new Error('此标签页找不到链接中的会话；请从工作区历史打开可用会话。');
    }
    if (sessionRows.length && !sessionId) {
      const row = sessionRows.find(({ id }) => id === (requested ?? selected)) ?? sessionRows[0];
      switchSession(row.id, requested === null ? 'replaceState' : undefined);
    }
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