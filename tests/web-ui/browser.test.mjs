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

test('browser: direct pages, cross-page approval and recovery, checkpoint and tab isolation', {
  skip: !executable && 'Set TOY_HARNESS_BROWSER to run Chromium acceptance', timeout: 45_000,
}, async (t) => {
  assert.equal(typeof WebSocket, 'function', 'Browser acceptance requires Node.js 22+');
  const asset = /^\/(?:html\/[a-z]+\.html|css\/[a-z]+\.css|js\/[a-z-]+\.js)$/;
  const server = createServer(async (req, res) => {
    try {
      if (req.method !== 'GET' || !asset.test(req.url)) throw new Error('Not a static asset');
      const body = await readFile(new URL(req.url.slice(1), root));
      res.writeHead(200, { 'Content-Type': `${mime[req.url.split('.').at(-1)]}; charset=utf-8` }).end(body);
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
    const go = async (page) => { await click(`nav a[href="${page}.html"]`); await ready(page); };
    const click = (selector) => evaluate(`document.querySelector(${JSON.stringify(selector)}).click(); true`);
    const text = (selector) => evaluate(`document.querySelector(${JSON.stringify(selector)})?.textContent ?? ''`);
    const waitText = (selector, value) => until(async () => (await text(selector)).includes(value), value);
    return { run, evaluate, ready, open, go, click, text, waitText };
  };
  const main = client(await tab());
  const targets = {
    index: ['#chat-stream', '工作区'], approvals: ['#approval-list', '没有符合'], sessions: ['#session-list', '便笺更新'],
    checkpoints: ['#note', '初始版本'], subagents: ['#subagent-results', '已汇总'], tools: ['#tool-list', 'demo_note'],
    audit: ['#audit-list', '创建会话'], context: ['#context-summary', '前缀哈希'], settings: ['#runtime-config', '8080'],
  };
  for (const [page, [selector, expected]] of Object.entries(targets)) {
    await main.open(page);
    assert.ok((await main.text(selector)).includes(expected), `${page}: ${await main.text(selector)}`);
    assert.deepEqual(await main.evaluate('[...document.styleSheets].map(s => new URL(s.href).pathname)'), [`/css/${page}.css`]);
    await main.run('Emulation.setDeviceMetricsOverride', { width: 320, height: 820, deviceScaleFactor: 1, mobile: false });
    assert.equal(await main.evaluate('document.documentElement.scrollWidth <= innerWidth'), true, `${page} fits at 320px`);
    await main.run('Emulation.clearDeviceMetricsOverride');
  }

  const filter = (selector, value, event = 'input') => main.evaluate(`{
    const field = document.querySelector(${JSON.stringify(selector)});
    field.value = ${JSON.stringify(value)}; field.dispatchEvent(new Event(${JSON.stringify(event)}));
  }`);
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

  await main.go('approvals');
  await main.evaluate(`const select = document.querySelector('#session-select');
    select.selectedIndex = 1; select.dispatchEvent(new Event('change'));`);
  await main.waitText('#approval-list', '待决定');
  const selected = await main.evaluate('document.querySelector("#session-select").value');
  await main.click('#approval-list button');
  assert.equal(await main.evaluate('document.querySelector("dialog").open'), true);
  assert.equal(await main.evaluate('document.activeElement.id'), 'dialog-close');
  await main.run('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Tab', code: 'Tab', modifiers: 8, windowsVirtualKeyCode: 9 });
  assert.equal(await main.evaluate('document.activeElement.matches("#dialog-actions .primary")'), true);
  await main.run('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await until(() => main.evaluate('!document.querySelector("dialog").open && document.activeElement.matches("#approval-list button")'), 'Escape restores focus');
  await main.click('#approval-list button');
  await main.click('dialog button.primary');
  await main.waitText('#approval-list', '已批准');
  await main.go('checkpoints');
  assert.match(await main.text('#note'), /初始版本/);
  assert.match(await main.text('#checkpoint-list'), /更新便笺前/);
  await main.go('sessions');
  assert.equal(await main.evaluate('document.querySelector("#session-select").value'), selected);
  await main.click('[data-focus="resume"]');
  await main.waitText('#session-summary', '已完成');
  await main.go('checkpoints');
  assert.match(await main.text('#note'), /便笺已更新/);
  await main.click('#rollback-review');
  await main.click('dialog button.danger');
  await main.waitText('#rollback-result', '最近一次回滚');
  assert.match(await main.text('#note'), /初始版本/);
  await main.go('index');
  assert.match(await main.text('#chat-stream'), /便笺已在演示内存中更新/);
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
  await main.waitText('#checkpoint-list', '巡检完成 · 初始检查点');
  assert.doesNotMatch(await main.text('#checkpoint-list'), /更新便笺前/);
  assert.match(await main.text('#rollback-result'), /尚未回滚/);
  await main.go('approvals');
  assert.match(await main.text('#approval-list'), /没有符合/);
  const other = client(await tab());
  await other.open('approvals');
  await other.evaluate(`document.querySelector('#session-select').selectedIndex = 1;
    document.querySelector('#session-select').dispatchEvent(new Event('change'));`);
  await other.waitText('#approval-list', '待决定');
  assert.equal(await main.evaluate('document.querySelector("#session-select").selectedIndex'), 0);
  await other.go('context');
  await other.evaluate(`document.querySelector('#session-select').selectedIndex = 2;
    document.querySelector('#session-select').dispatchEvent(new Event('change'));`);
  await other.waitText('#context-session', '中断恢复示例');
  await other.click('[data-focus="resume"]');
  await other.waitText('#turn-summary', '已完成');
  await other.go('index');
  assert.match(await other.text('#chat-stream'), /已恢复这条未完成的演示轮次/);
  assert.deepEqual(exceptions, []);
  assert.ok(requests.length > 9);
  for (const { url, method } of requests) {
    if (url.startsWith('data:') || url === 'about:blank') continue;
    assert.equal(new URL(url).origin, origin, url);
    assert.equal(method, 'GET', url);
    assert.match(new URL(url).pathname, asset, url);
  }
});