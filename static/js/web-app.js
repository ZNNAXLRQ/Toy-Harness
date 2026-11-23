import { mountSidebar } from './sidebar.js';
import { code, el, kv, table } from './views.js';
import { createWebService } from './web-service.js';

const $ = (id) => document.getElementById(id);
const page = $('page');
const route = page.dataset.page;
const heading = $('page-title');
const service = createWebService();
const mode = document.querySelector('.badge.mode');
const intro = document.querySelector('.masthead p');
const footer = document.querySelector('main > footer');
const descriptions = {
  approvals: 'Web 不提供审批队列或人工审批操作；不允许的工具由服务端直接拒绝。',
  sessions: 'Web 不提供服务端会话列表、持久化或恢复；聊天仅在当前页面展示。',
  checkpoints: 'Web 不提供检查点或回滚操作。',
  subagents: 'Web 不提供子代理任务或结果查询。',
  audit: 'Web 不提供审计日志；只在聊天页显示最近一次请求随结果返回的事件摘要。',
  context: 'Web 不提供模型上下文查看或轮次恢复；每次请求只发送当前问题。',
};

function report(message = '') {
  $('error').textContent = message;
  $('error').hidden = !message;
}

function announced(message) { $('status').textContent = message; }

function initChrome() {
  document.body.dataset.mode = 'web';
  mode.textContent = 'Web 模式';
  document.querySelector('.header-tools .workspace').hidden = true;
  intro.textContent = '通过同源 HTTP 连接本机 Harness；不是静态演示。';
  footer.textContent = '本机 Harness Web · 不使用演示数据';
  $('loading').textContent = '正在加载 Web 页面…';
  $('skip').onclick = (event) => { event.preventDefault(); $('main').focus(); };
}

function initSidebar(sidebar) {
  const chat = sidebar.querySelector('.new-link');
  chat.href = 'index.html';
  chat.textContent = '返回聊天';
  if (route === 'chat') chat.setAttribute('aria-current', 'page');
  for (const link of sidebar.querySelectorAll('a')) {
    const url = new URL(link.href);
    url.searchParams.set('mode', 'web');
    link.href = url.href;
  }
  sidebar.querySelector('.nav-label').textContent = '本机 Web';
  const history = $('session-history');
  history.setAttribute('aria-label', 'Web 会话说明');
  history.append(el('p', { class: 'meta' }, '没有可恢复的会话'));
  const settings = sidebar.querySelector('.sidebar-foot');
  settings.setAttribute('aria-label', 'Web 设置');
  settings.querySelector('strong').textContent = 'Web 设置';
  settings.querySelector('small').textContent = '运行能力';
}

function initChat() {
  heading.textContent = '聊天与本轮事件';
  intro.textContent = '每次请求独立，模型不会收到前几轮。聊天只保留在当前页面；刷新、离开页面或关闭标签页都不可恢复。';
  $('chat-session').textContent = '单轮请求 · 仅发送当前问题';
  $('events-title').textContent = '本轮事件';
  document.querySelector('.event-panel .meta').textContent = '随结果返回 · 非实时推送';
  $('event-filter').closest('label').hidden = true;
  $('write').remove();
  const input = $('message');
  const send = $('send');
  const messages = $('chat-stream');
  const events = $('event-stream');
  input.removeAttribute('maxlength'); // The API limits Unicode code points, not UTF-16 units.
  input.placeholder = '输入单轮问题…';
  messages.setAttribute('aria-label', '当前页面的聊天记录');
  events.setAttribute('aria-label', '本轮事件摘要');
  const emptyChat = el('p', { class: 'empty' }, '还没有发送问题。');
  messages.append(emptyChat);
  const eventNote = (text) => events.replaceChildren(el('p', { class: 'empty' }, text));
  eventNote('暂无本轮事件。');

  const addMessage = (role, text) => {
    messages.append(el('article', { class: 'message' }, el('strong', {}, role),
      el('p', { class: 'message-text' }, text)));
    messages.scrollTop = messages.scrollHeight;
  };
  let busy = false;
  $('composer').onsubmit = async (event) => {
    event.preventDefault();
    if (busy) return;
    const prompt = input.value.trim();
    report();
    if (!prompt || Array.from(prompt).length > 8192) {
      report('请输入 1–8192 个字符。');
      input.focus();
      return;
    }
    busy = send.disabled = input.readOnly = true;
    send.textContent = '等待结果…';
    announced('正在运行本轮。');
    eventNote('正在运行；事件将在本轮结束后显示。');
    emptyChat.remove();
    addMessage('你', prompt);
    input.value = '';
    try {
      const { result, events: rows, error } = await service.run(prompt);
      if (result.success) addMessage('Harness', result.response || '（本轮无文本答复）');
      else {
        addMessage('未完成', error);
        report(error);
      }
      events.replaceChildren(el('p', { class: 'meta' }, `运行 ID：${result.run_id}`),
        ...(rows.length ? rows.map(({ seq, kind, name, status, approved, success }) =>
          el('article', { class: 'event', 'data-event': kind }, el('h3', {}, `#${seq} ${kind}`),
            el('p', { class: 'meta' }, [name, status, typeof approved === 'boolean' && `策略允许：${approved ? '是' : '否'}`,
              typeof success === 'boolean' && `成功：${success ? '是' : '否'}`].filter(Boolean).join(' · ')))) :
          [el('p', { class: 'empty' }, '本轮没有返回事件。')]));
      events.scrollTop = 0;
      announced(result.success ? '已收到本轮答复和事件。' : '本轮失败，已显示服务端返回的事件。');
    } catch (cause) {
      report(cause.message);
      addMessage('未收到结果', cause.message);
      eventNote('未收到本轮事件。');
      announced('未收到本轮结果。');
    } finally {
      busy = send.disabled = input.readOnly = false;
      send.textContent = '发送消息';
    }
  };
}

function initTools(tools) {
  $('tools-title').textContent = '已注册的 Web 工具';
  page.querySelector('.meta').textContent = '来自实际工具注册表；只能通过聊天由 Agent 调用 Web 允许的工具。';
  page.querySelector('.panel-foot').textContent = '当前限制：只读。标为「未启用」的工具不可调用；此页不提供执行入口。';
  const columns = page.querySelector('thead tr');
  columns.deleteCell(2);
  const input = $('tool-search');
  const render = () => {
    const term = input.value.trim().toLowerCase();
    table('tool-list', tools.filter(({ schema: { function: { name, description } } }) =>
      `${name} ${description ?? ''}`.toLowerCase().includes(term))
      .map(({ schema: { function: { name, description, parameters } }, enabled }) => [name, description ?? '—',
        el('span', { class: 'badge' }, enabled ? '已启用 · 只读' : '未启用'),
        el('details', {}, el('summary', {}, '参数 Schema'), code(parameters))]), '没有匹配的工具。');
  };
  input.oninput = render;
  render();
}

function initSettings(provider) {
  const section = el('section', { class: 'card panel stack' }, el('h2', {}, 'Web 运行能力'),
    kv([['Provider 模式', provider === 'offline' ? '离线 FakeProvider（脚本化回复）' : '服务端配置的 Provider'],
      ['聊天', '单轮'], ['工具策略', '只读'], ['事件返回', '随结果返回'], ['会话恢复', '不支持']]),
    el('p', { class: 'meta' }, '此页面不读取或修改模型配置，也不显示或接收密钥。'));
  page.replaceChildren(section);
}

let sidebar;
async function start() {
  report();
  $('retry').hidden = true;
  $('loading').hidden = false;
  page.setAttribute('aria-busy', 'true');
  try {
    if (!sidebar) {
      sidebar = await mountSidebar($('sidebar-slot'));
      initSidebar(sidebar);
    }
    if (Object.hasOwn(descriptions, route)) {
      page.replaceChildren(el('section', { class: 'card panel stack' }, el('h2', {}, 'Web 尚未接入'),
        el('p', { class: 'empty' }, descriptions[route])));
    } else {
      const provider = await service.getRuntime();
      mode.textContent = provider === 'offline' ? 'Web 模式 · 离线' : 'Web 模式 · 已配置 Provider';
      if (route === 'chat') initChat();
      else if (route === 'tools') initTools(await service.listTools());
      else if (route === 'settings') initSettings(provider);
    }
    page.hidden = false;
  } catch (cause) {
    report(`无法加载 Web 页面：${cause.message}`);
    $('retry').hidden = false;
  } finally {
    page.setAttribute('aria-busy', 'false');
    $('loading').hidden = true;
  }
}

initChrome();
// Do not restore a cached page's in-memory transcript on back/forward navigation.
window.addEventListener('pageshow', (event) => { if (event.persisted) location.reload(); });
$('retry').onclick = start;
void start();