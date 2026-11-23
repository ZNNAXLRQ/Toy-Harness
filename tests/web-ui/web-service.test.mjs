import assert from 'node:assert/strict';
import test from 'node:test';
import { createWebService } from '../../static/js/web-service.js';

test('Web adapter submits only the current prompt and retains events on a failed run', async () => {
  const events = [{ seq: 1, run_id: 'run-1', kind: 'tool_end' }];
  const service = createWebService(async (path, options) => {
    assert.equal(path, '/api/runs');
    assert.equal(options.mode, 'same-origin');
    assert.equal(options.redirect, 'error');
    assert.equal(options.method, 'POST');
    assert.equal(options.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(options.body), { prompt: 'Current prompt' });
    return Response.json({ result: { run_id: 'run-1', success: false, response: '', error: 'tool_denied' }, events },
      { status: 403 });
  });
  const run = await service.run('Current prompt');
  assert.equal(run.result.success, false);
  assert.deepEqual(run.events, events);
  assert.match(run.error, /HTTP 403/);
});

test('Web adapter rejects unavailable, unknown and invalid API responses without demo fallback', async () => {
  const disconnected = createWebService(async () => { throw new Error('private network details'); });
  await assert.rejects(disconnected.getRuntime(), /无法连接同源 Web 服务/);
  const unavailable = createWebService(async () => Response.json({ error: 'provider_unavailable' }, { status: 503 }));
  await assert.rejects(unavailable.run('hello'), /模型未能初始化.*HTTP 503/);
  const incompatible = createWebService(async () => Response.json({ provider_mode: 'offline', capabilities: {} }));
  await assert.rejects(incompatible.getRuntime(), /不支持/);
  const invalid = createWebService(async () => new Response('<html>'));
  await assert.rejects(invalid.listTools(), /无效的数据/);
});