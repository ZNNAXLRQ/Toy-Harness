import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { setTimeout as sleep } from 'node:timers/promises';

const executable = process.env.TOY_HARNESS_BROWSER;
const root = new URL('../../static/', import.meta.url);
const mime = { html: 'text/html', css: 'text/css', js: 'application/javascript' };

async function until(check, description) {
  for (let i = 0; i < 100; i++) {
    const value = await check();
    if (value) return value;
    await sleep(50);
  }
  assert.fail(`Timed out: ${description}`);
}

async function browser(t) {
  const profile = await mkdtemp(join(tmpdir(), 'toy-harness-browser-'));
  const process = spawn(executable, ['--headless', '--disable-background-networking', '--no-first-run',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
  let launchError;
  process.once('error', (error) => { launchError = error; });
  t.after(async () => {
    if (process.pid && process.exitCode == null && process.signalCode == null) {
      const exited = once(process, 'exit');
      process.kill();
      await exited;
    }
    await rm(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  });
  const [port, browserPath] = await until(async () => {
    if (launchError) throw launchError;
    const file = await readFile(join(profile, 'DevToolsActivePort'), 'utf8').catch(() => '');
    const lines = file.trim().split(/\r?\n/);
    return lines[1] && lines;
  }, 'Chromium DevTools startup');
  const socket = new WebSocket(`ws://127.0.0.1:${port}${browserPath}`);
  t.after(() => socket.close());
  await once(socket, 'open');
  let id = 0;
  const pending = new Map();
  const exceptions = [];
  const requests = [];
  socket.addEventListener('message', ({ data }) => {
    const message = JSON.parse(data);
    if (message.method === 'Runtime.exceptionThrown') exceptions.push(message.params.exceptionDetails);
    if (message.method === 'Network.requestWillBeSent') requests.push(message.params.request);
    if (!pending.has(message.id)) return;
    const [resolve, reject] = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(message.error.message));
    else resolve(message.result);
  });
  const command = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    const message = { id: ++id, method, params, sessionId };
    pending.set(id, [resolve, reject]);
    socket.send(JSON.stringify(message));
  });
  const tab = async () => {
    const { targetId } = await command('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await command('Target.attachToTarget', { targetId, flatten: true });
    const run = (method, params) => command(method, params, sessionId);
    await run('Runtime.enable');
    await run('Network.enable');
    return run;
  };
  return { tab, exceptions, requests };
}

test('browser: workspace history, task ownership, settings keyboard, responsive pages and tab isolation', {
  skip: !executable && 'Set TOY_HARNESS_BROWSER to run Chromium acceptance', timeout: 45_000,
}, async (t) => {
  assert.equal(typeof WebSocket, 'function', 'Browser acceptance requires Node.js 22+');
  const asset = /^\/(?:html\/[a-z]+\.html|css\/[a-z]+\.css|js\/[a-z-]+\.js)$/;
  const server = createServer(async (req, res) => {
    try {
      const { pathname } = new URL(req.url, 'http://127.0.0.1');
      if (req.method !== 'GET' || !asset.test(pathname)) throw new Error('Not a static asset');
      const body = await readFile(new URL(pathname.slice(1), root));
      res.writeHead(200, { 'Content-Type': `${mime[pathname.split('.').at(-1)]}; charset=utf-8` }).end(body);
    } catch { res.writeHead(404).end(); }
  }).listen(0, '127.0.0.1');
  t.after(() => { server.close(); server.closeAllConnections(); });
  await once(server, 'listening');
  const origin = `http://127.0.0.1:${server.address().port}`;
  const { tab, exceptions, requests } = await browser(t);

  const client = (run) => {
    const evaluate = async (expression) => {
      const { result, exceptionDetails } = await run('Runtime.evaluate', { expression, returnByValue: true });
      assert.ok(!exceptionDetails, exceptionDetails?.exception?.description);
      return result.value;
    };
    const ready = (page) => until(() => evaluate(`location.pathname === '/html/${page}.html' &&
      document.querySelector('#page:not([hidden])[aria-busy="false"]') !== null`), `${page} renders`);
    const open = async (page) => { await run('Page.navigate', { url: `${origin}/html/${page}.html` }); await ready(page); };
    const go = async (page) => {
      await click(page === 'index' ? '#session-history a[aria-current]' : page === 'settings' ? '.sidebar-foot'
        : `.task-nav a[href*="/${page}.html?"]`);
      await ready(page);
    };
    const click = (selector) => until(() => evaluate(`{
      const target = document.querySelector(${JSON.stringify(selector)});
      if (target && !target.disabled) target.click();
      !!target && !target.disabled;
    }`), `click ${selector}`);
    const text = (selector) => evaluate(`document.querySelector(${JSON.stringify(selector)})?.textContent ?? ''`);
    const waitText = (selector, value) => until(async () => (await text(selector)).includes(value), value);
    const selected = () => evaluate('document.querySelector("#session-select").value');
    const opened = (id) => until(() => evaluate(`document.querySelector('#page[data-page="chat"]:not([hidden])[aria-busy="false"]') &&
      new URLSearchParams(location.search).get('session') === ${JSON.stringify(id)} &&
      document.querySelector('#session-history [aria-current="page"]')?.dataset.sessionId === ${JSON.stringify(id)}`), `chat ${id}`);
    const chat = async (id) => { await click(`#session-history [data-session-id="${id}"]`); await opened(id); };
    return { run, evaluate, ready, open, go, click, text, waitText, selected, opened, chat };
  };
  const main = client(await tab());
  const targets = {
    index: ['#chat-stream', '工作区'], approvals: ['#approval-list', '待决定'], sessions: ['#session-list', '便笺更新'],
    checkpoints: ['#note', '初始版本'], subagents: ['#subagent-results', '已汇总'], tools: ['#tool-list', 'demo_note'],
    audit: ['#audit-list', '创建会话'], context: ['#context-summary', '前缀哈希'], settings: ['#runtime-config', '8080'],
  };
  for (const [page, [selector, expected]] of Object.entries(targets)) {
    await main.open(page);
    assert.ok((await main.text(selector)).includes(expected), `${page}: ${await main.text(selector)}`);
    assert.deepEqual(await main.evaluate('[...document.styleSheets].map(s => new URL(s.href).pathname)'), [`/css/${page}.css`]);
    assert.equal(await main.evaluate('new URL(document.styleSheets[0].cssRules[0].styleSheet.href).pathname'), '/css/sidebar.css');
    assert.deepEqual(await main.evaluate(`[...document.querySelector('.sidebar').children].map(el => el.className)`),
      ['brand', 'button primary new-link', 'nav-label', 'history-nav', 'task-nav', 'sidebar-foot']);
    assert.equal(await main.text('.nav-label'), '工作区');
    assert.deepEqual(await main.evaluate(`[...document.querySelectorAll('.sidebar [aria-current="page"]')]
      .map(link => new URL(link.href).pathname)`), [`/html/${page}.html`]);
    assert.equal(await main.evaluate(`[...document.querySelectorAll('.sidebar a')].every(link => {
      const url = new URL(link.href);
      return url.origin === location.origin && url.searchParams.has('session');
    })`), true);
    await main.run('Emulation.setDeviceMetricsOverride', { width: 320, height: 820, deviceScaleFactor: 1, mobile: false });
    assert.equal(await main.evaluate('document.documentElement.scrollWidth <= innerWidth'), true, `${page} fits at 320px`);
    assert.equal(await main.evaluate('document.querySelector(".sidebar-foot").checkVisibility()'), true);
    await main.run('Emulation.clearDeviceMetricsOverride');
  }

  const filter = (selector, value, event = 'input') => main.evaluate(`{
    const field = document.querySelector(${JSON.stringify(selector)});
    field.value = ${JSON.stringify(value)}; field.dispatchEvent(new Event(${JSON.stringify(event)}));
  }`);
  const [first, selected, interrupted] = await main.evaluate('[...document.querySelector("#session-select").options].map(o => o.value)');
  const key = (key, code, windowsVirtualKeyCode) => main.run('Input.dispatchKeyEvent', { type: 'rawKeyDown', key, code, windowsVirtualKeyCode });
  await main.go('tools');
  await main.go('settings');
  await main.evaluate('document.querySelector("#llm-tab").focus()');
  await key('ArrowRight', 'ArrowRight', 39);
  assert.equal(await main.evaluate('document.activeElement.id === "runtime-tab" && !document.querySelector("#runtime-panel").hidden && document.querySelector("#llm-panel").hidden'), true);
  await key('Home', 'Home', 36);
  assert.equal(await main.evaluate('document.activeElement.id === "llm-tab" && !document.querySelector("#llm-panel").hidden'), true);
  await key('End', 'End', 35);
  await key('Tab', 'Tab', 9);
  assert.equal(await main.evaluate('document.activeElement.id'), 'runtime-panel');
  await main.go('sessions');
  assert.equal(await main.evaluate('document.querySelectorAll("#session-list > tr").length'), 3);
  await filter('#session-filter', 'open', 'change');
  assert.match(await main.text('#session-list'), /中断恢复示例/);
  assert.doesNotMatch(await main.text('#session-list'), /便笺更新/);
  await filter('#session-search', 'not found');
  assert.match(await main.text('#session-list'), /没有符合/);
  await main.go('tools');
  await filter('#tool-search', 'demo_note');
  assert.equal(await main.evaluate('document.querySelectorAll("#tool-list > tr").length'), 1);
  assert.doesNotMatch(await main.text('#tool-list'), /demo_read/);
  await main.go('audit');
  await filter('#audit-scope', 'all', 'change');
  await main.waitText('#audit-list', '启动子代理');
  await filter('#audit-action', 'session_created', 'change');
  assert.equal(await main.evaluate('document.querySelectorAll("#audit-list > tr").length'), 3);
  await filter('#audit-search', '便笺更新');
  assert.equal(await main.evaluate('document.querySelectorAll("#audit-list > tr").length'), 1);

  await main.click('.new-link');
  await main.ready('sessions');
  await until(() => main.evaluate('document.activeElement.id === "new-session"'), 'create form focused');
  await main.evaluate(`document.querySelector('#new-session').value = '浏览器新会话'; document.querySelector('#new-session-form').requestSubmit()`);
  await main.ready('index');
  await main.waitText('#chat-stream', '空会话');
  const created = await main.selected();
  await main.opened(created);
  assert.equal(await main.evaluate('document.querySelector("#session-history a").dataset.sessionId'), created);
  const createdUrl = await main.evaluate('document.querySelector("#session-history a[aria-current]").href');
  await main.evaluate(`document.querySelector('#message').value = '只在新会话'; document.querySelector('#send').click()`);
  await main.waitText('#chat-stream', '收到这条演示消息');
  await main.chat(first);
  assert.doesNotMatch(await main.text('#chat-stream'), /只在新会话/);
  await main.evaluate('history.back(); true');
  await main.opened(created);
  await main.waitText('#chat-stream', '只在新会话');
  await main.evaluate('history.forward(); true');
  await main.opened(first);

  const assertScope = async (scope, rows, nodeType = 'article') => {
    const read = () => main.evaluate(`[...document.querySelectorAll('${rows} > ${nodeType}')].flatMap(row => {
      const owner = row.dataset.sessionId ?? new URL(row.querySelector('a[href*="session="]')?.href ?? location.href).searchParams.get('session');
      const stamp = row.dataset.createdAt ?? Date.parse(row.querySelector('time')?.dateTime) / 1000;
      return Number.isFinite(Number(stamp)) ? [[owner, Number(stamp)]] : [];
    })`);
    assert.equal(await main.evaluate(`document.querySelector('${scope}').value`), 'all');
    const all = await read();
    assert.ok(new Set(all.map(([id]) => id)).size > 1);
    assert.deepEqual(all, [...all].sort((a, b) => b[1] - a[1]));
    await filter(scope, 'current', 'change');
    assert.deepEqual(await read(), all.filter(([id]) => id === first));
    await filter('#session-select', selected, 'change');
    await until(async () => JSON.stringify(await read()) === JSON.stringify(all.filter(([id]) => id === selected)), 'scope follows selected session');
    await main.evaluate('history.back(); true');
    await until(async () => await main.selected() === first &&
      JSON.stringify(await read()) === JSON.stringify(all.filter(([id]) => id === first)), 'back restores scope owner');
    await filter(scope, 'all', 'change');
  };
  await main.go('subagents');
  await assertScope('#subagent-scope', '#subagent-list');
  await main.click(`#subagent-list [data-session-id="${interrupted}"] button`);
  assert.match(await main.text('#subagent-detail'), /中断恢复示例/);
  await main.go('audit');
  await assertScope('#audit-scope', '#audit-list', 'tr');

  await main.chat(created);
  await main.evaluate(`document.querySelector('#message').value = '第二个审批'; document.querySelector('#write').click()`);
  await main.waitText('#chat-stream', '第二个审批');
  await main.chat(first);
  await main.go('approvals');
  await assertScope('#approval-scope', '#approval-list');
  await main.waitText('#approval-list', '待决定');
  const review = `#approval-list [data-session-id="${selected}"] button`;
  await main.click(review);
  assert.equal(await main.evaluate('document.querySelector("dialog").open'), true);
  assert.match(await main.text('#dialog-body'), /便笺更新/);
  assert.equal(await main.evaluate('document.activeElement.id'), 'dialog-close');
  await main.run('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Tab', code: 'Tab', modifiers: 8, windowsVirtualKeyCode: 9 });
  assert.equal(await main.evaluate('document.activeElement.matches("#dialog-actions .primary")'), true);
  await main.run('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await until(() => main.evaluate('!document.querySelector("dialog").open && document.activeElement.matches("#approval-list button")'), 'Escape restores focus');
  await main.click(review);
  await main.click('dialog button.primary');
  await main.waitText('#approval-list', '已批准');
  await main.go('checkpoints');
  await assertScope('#checkpoint-scope', '#checkpoint-list');
  assert.match(await main.text('#note'), /初始版本/);
  assert.match(await main.text('#checkpoint-list'), /更新便笺前/);
  const point = await main.evaluate(`[...document.querySelector('#rollback-target').options].find(option => option.value.startsWith('${selected}:')).value`);
  await filter('#rollback-target', point, 'change');
  assert.equal(await main.evaluate('document.querySelector("#rollback-review").disabled'), true);
  assert.equal(await main.evaluate('document.querySelector("#rollback-blocked").hidden'), false);
  await main.go('approvals');
  assert.equal(await main.selected(), first);
  await main.click(`#approval-list [data-session-id="${selected}"] [data-focus^="resume:"]`);
  await until(() => main.evaluate(`!document.querySelector('#approval-list [data-session-id="${selected}"] [data-focus^="resume:"]')`), 'approval owner resumed');
  await main.go('checkpoints');
  assert.match(await main.text('#note'), /初始版本/);
  await filter('#rollback-target', point, 'change');
  await main.click('#rollback-review');
  assert.match(await main.text('#dialog-body'), /便笺更新/);
  await main.click('dialog button.danger');
  await main.waitText('#rollback-result', '最近一次回滚');
  assert.match(await main.text('#rollback-result'), /便笺更新/);
  assert.match(await main.text('#note'), /初始版本/);
  await main.chat(selected);
  assert.match(await main.text('#chat-stream'), /便笺已在演示内存中更新/);
  await main.go('checkpoints');
  assert.match(await main.text('#note'), /初始版本/);
  await main.go('index');
  for (let i = 0; i < 3; i++) {
    await main.evaluate(`document.querySelector('#message').value = '裁剪测试 ${i}'; document.querySelector('#send').click()`);
    await main.waitText('#chat-stream', `收到这条演示消息：\n裁剪测试 ${i}`);
  }
  await main.go('context');
  const summary = await main.evaluate(`Object.fromEntries([...document.querySelectorAll('#context-summary dt')]
    .map(dt => [dt.textContent, dt.nextElementSibling.textContent]))`);
  assert.equal(summary['剔除历史条数'], '3');
  const tail = JSON.parse(await main.text('#context-messages'));
  assert.equal(tail.length, 7);
  assert.ok(!tail.some((message) => message.role === 'tool' || message.tool_calls));
  await main.go('checkpoints');
  await main.evaluate('window.beforeReload = true');
  await main.run('Page.reload');
  await until(() => main.evaluate('!window.beforeReload'), 'new document after reload');
  await main.waitText('#rollback-result', '最近一次回滚');
  assert.match(await main.text('#note'), /初始版本/);

  await main.evaluate(`document.querySelector('#session-select').selectedIndex = 0;
    document.querySelector('#session-select').dispatchEvent(new Event('change'));`);
  await until(async () => await main.selected() === first && await main.evaluate('document.querySelector("#page").getAttribute("aria-busy") === "false"'), 'first checkpoint session');
  await filter('#checkpoint-scope', 'current', 'change');
  await main.waitText('#checkpoint-list', '巡检完成 · 初始检查点');
  assert.doesNotMatch(await main.text('#checkpoint-list'), /更新便笺前/);
  assert.match(await main.text('#rollback-result'), /尚未回滚/);
  await main.go('approvals');
  await filter('#approval-scope', 'current', 'change');
  assert.match(await main.text('#approval-list'), /没有符合/);
  const other = client(await tab());
  await other.open('approvals');
  await other.waitText('#approval-list', '待决定');
  assert.doesNotMatch(await other.text('#session-history'), /浏览器新会话/);
  assert.equal(await main.evaluate('document.querySelector("#session-select").selectedIndex'), 0);
  await other.click('.new-link');
  await other.ready('sessions');
  await other.evaluate(`document.querySelector('#new-session').value = '独立标签页新会话'; document.querySelector('#new-session-form').requestSubmit()`);
  await other.ready('index');
  const otherCreated = await other.selected();
  await other.opened(otherCreated);
  const otherUrl = await other.evaluate('document.querySelector("#session-history a[aria-current]").href');

  await other.run('Page.navigate', { url: createdUrl });
  await other.waitText('#error', '找不到链接中的会话');
  await main.run('Page.navigate', { url: otherUrl });
  await main.waitText('#error', '找不到链接中的会话');
  await main.run('Page.navigate', { url: createdUrl });
  await main.opened(created);
  await main.waitText('#chat-stream', '只在新会话');
  await other.run('Page.navigate', { url: otherUrl });
  await other.opened(otherCreated);
  await other.waitText('#chat-stream', '空会话');

  await other.chat(interrupted);
  await other.go('context');
  await other.waitText('#context-session', '中断恢复示例');
  await other.click('[data-focus="resume"]');
  await other.waitText('#turn-summary', '已完成');
  await other.go('index');
  assert.match(await other.text('#chat-stream'), /已恢复这条未完成的演示轮次/);

  const layout = client(await tab());
  const measure = () => layout.evaluate(`{
    const history = document.querySelector('#session-history');
    ({ count: history.children.length, height: history.getBoundingClientRect().height,
      scrollHeight: history.scrollHeight, clientHeight: history.clientHeight,
      taskTop: document.querySelector('.task-nav').getBoundingClientRect().top + scrollY,
      overflowY: getComputedStyle(history).overflowY,
      listWidths: [history.scrollWidth, history.clientWidth],
      pageWidths: [document.documentElement.scrollWidth, innerWidth] });
  }`);
  const setLayoutWidth = (width) => layout.run('Emulation.setDeviceMetricsOverride', {
    width, height: 820, deviceScaleFactor: 1, mobile: false,
  });
  const measureBothWidths = async () => {
    await setLayoutWidth(1280);
    const desktop = await measure();
    await setLayoutWidth(320);
    return [desktop, await measure()];
  };
  await layout.open('tools');
  const few = await measureBothWidths();
  await layout.evaluate('sessionStorage.setItem("toy-harness.demo.v1.state", JSON.stringify({ sessions: [] }))');
  await layout.run('Page.navigate', { url: `${origin}/html/tools.html?layout=empty` });
  await until(() => layout.evaluate(`location.search === '?layout=empty' &&
    !document.querySelector('#page')?.hidden && document.querySelector('#session-history')?.children.length === 0`), 'empty workspace renders');
  const empty = await measureBothWidths();
  await layout.evaluate(`import('/js/service.js').then(async ({ service }) => {
    for (let i = 0; i < 20; i++) await service.createSession('LongSessionName'.repeat(5) + i);
    location.replace('tools.html?layout=many');
  }); true`);
  await until(() => layout.evaluate(`new URLSearchParams(location.search).get('layout') === 'many' &&
    document.querySelector('#session-history')?.children.length === 20`), 'populated workspace renders');
  const many = await measureBothWidths();
  for (const [width, crowded] of many.entries()) {
    assert.equal(few[width].count, 3);
    assert.equal(empty[width].count, 0);
    assert.equal(crowded.count, 20);
    assert.ok(few[width].height > 0);
    for (const sample of [few[width], empty[width], crowded]) {
      assert.equal(sample.height, few[width].height);
      assert.equal(sample.taskTop, few[width].taskTop);
      assert.equal(sample.overflowY, 'auto');
      assert.ok(sample.listWidths[0] <= sample.listWidths[1], JSON.stringify(sample));
      assert.ok(sample.pageWidths[0] <= sample.pageWidths[1], JSON.stringify(sample));
    }
    assert.ok(crowded.scrollHeight > crowded.clientHeight);
  }

  const last = await layout.evaluate(`{
    const history = document.querySelector('#session-history');
    document.querySelector('.new-link').focus();
    history.scrollTop = 0;
    history.lastElementChild.dataset.sessionId;
  }`);
  assert.equal(await layout.evaluate(`{
    const history = document.querySelector('#session-history');
    history.lastElementChild.getBoundingClientRect().top >= history.getBoundingClientRect().bottom;
  }`), true);
  for (let i = 0; i < 21; i++) {
    await layout.run('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
    await layout.run('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
    if (await layout.evaluate(`document.activeElement.dataset.sessionId === ${JSON.stringify(last)}`)) break;
  }
  assert.equal(await layout.evaluate(`{
    const history = document.querySelector('#session-history');
    document.activeElement === history.lastElementChild && history.scrollTop > 0 &&
      document.activeElement.getBoundingClientRect().top < history.getBoundingClientRect().bottom;
  }`), true);
  await layout.run('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
  await layout.opened(last);

  assert.deepEqual(exceptions, []);
  assert.ok(requests.length > 9);
  for (const path of ['/html/sidebar.html', '/css/sidebar.css']) {
    assert.ok(requests.some(({ url }) => url === `${origin}${path}`), `${path} loaded from the static server`);
  }
  for (const { url, method } of requests) {
    if (url.startsWith('data:') || url === 'about:blank') continue;
    assert.equal(new URL(url).origin, origin, url);
    assert.equal(method, 'GET', url);
    assert.match(new URL(url).pathname, asset, url);
  }
});