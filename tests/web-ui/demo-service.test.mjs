import assert from 'node:assert/strict';
import test from 'node:test';
import { createDemoService } from '../../static/js/demo-service.js';

const pending = (snapshot) => snapshot.approvals.find((row) => row.decision == null);

test('demo workspace owns its sessions; subagent creation comes from its own start event', async () => {
  const service = createDemoService();
  const [workspace] = await service.listWorkspaces();
  assert.equal(workspace.id, 'demo-workspace');
  assert.deepEqual(workspace.sessions, await service.listSessions());
  for (const { id } of workspace.sessions) {
    const { events, subagents } = await service.getSession(id);
    for (const { task, created_at } of subagents) {
      assert.equal(task.parent_session_id, id);
      assert.equal(created_at, events.find((event) => event.type === 'subagent_started' && event.payload.run_id === task.id).created_at);
    }
  }
  const created = await service.createSession('独立会话');
  assert.deepEqual((await service.listWorkspaces())[0].sessions.at(-1), created);
  assert.deepEqual((await service.getSession(created.id)).messages, []);
  assert.equal(workspace.sessions.length, 3);
});

test('new session IDs do not resolve in independent instances but survive snapshot reload', async () => {
  let saved;
  const first = createDemoService({ save: (snapshot) => { saved = JSON.stringify(snapshot); } });
  const second = createDemoService();
  const a = await first.createSession('A');
  const b = await second.createSession('B');

  assert.notEqual(a.id, b.id);
  await assert.rejects(first.getSession(b.id), /找不到所选会话/);
  await assert.rejects(second.getSession(a.id), /找不到所选会话/);
  const reloaded = createDemoService({ snapshot: JSON.parse(saved) });
  assert.deepEqual((await reloaded.getSession(a.id)).session, a);
});

test('tab snapshots preserve approval boundaries, counters, and compensated calls', async () => {
  let saved;
  const open = () => createDemoService({ snapshot: saved && JSON.parse(saved), save: (snapshot) => { saved = JSON.stringify(snapshot); } });
  const approvals = open();
  const [first, second] = await approvals.listSessions();
  const untouched = await approvals.getSession(first.id);
  const initial = await approvals.getSession(second.id);
  const approval = pending(initial);
  await approvals.decide(second.id, approval.id, 'approved');

  const recovery = open();
  const decided = await recovery.getSession(second.id);
  assert.equal(decided.approvals[0].decision, 'approved');
  assert.equal(decided.tool_calls[0].status, 'awaiting_approval');
  assert.equal(decided.demo.note, initial.demo.note);
  await recovery.resume(second.id, approval.id);
  const resumed = await recovery.getSession(second.id);
  assert.equal(resumed.demo.note, approval.arguments.text);
  assert.equal(new Set(resumed.messages.map(({ id }) => id)).size, resumed.messages.length);
  assert.ok(resumed.events.at(-1).created_at > decided.events.at(-1).created_at);

  const checkpoints = open();
  const point = initial.checkpoints[0];
  assert.deepEqual((await checkpoints.rollback(second.id, point.id)).reverted, [approval.call_id]);
  const reloaded = open();
  assert.deepEqual(await reloaded.getSession(first.id), untouched);
  assert.deepEqual((await reloaded.getSession(second.id)).messages, resumed.messages);
  assert.equal((await reloaded.getSession(second.id)).demo.note, initial.demo.note);
  assert.deepEqual((await reloaded.rollback(second.id, point.id)).reverted, []);
});

test('fixtures are reproducible; session queries, writes, and listeners stay isolated', async () => {
  const service = createDemoService();
  const original = await service.listSessions();
  assert.deepEqual(original, await createDemoService().listSessions());
  const [first, second] = original;
  const otherBefore = await service.getSession(second.id);
  assert.deepEqual(otherBefore, await createDemoService().getSession(second.id));
  const fresh = await service.createSession('  <新会话>  ');
  assert.equal(fresh.title, '<新会话>');
  const firstEvents = [];
  const freshEvents = [];
  const unFirst = service.subscribe(first.id, (event) => firstEvents.push(event));
  const unFresh = service.subscribe(fresh.id, (event) => freshEvents.push(event));
  assert.equal((await service.getSession(fresh.id)).messages.length, 0);
  await service.send(fresh.id, '<img src=x onerror=alert(1)>');
  assert.deepEqual(firstEvents, []);
  assert.deepEqual(freshEvents.map(({ type, seq }) => [type, seq]), [
    ['user_message', 1], ['model_response', 2], ['turn_completed', 2],
  ]);
  assert.ok(freshEvents.every((event) => event.session_id === fresh.id));
  unFirst();
  unFresh();
  await service.send(fresh.id, 'second');
  assert.equal(freshEvents.length, 3);
  assert.deepEqual(await service.getSession(second.id), otherBefore);
  await assert.rejects(service.getSession('missing'), /找不到/);
});

test('approval is bound to its call; decision and audit precede dispatch during resume', async () => {
  const service = createDemoService();
  const [first, second] = await service.listSessions();
  const initial = await service.getSession(second.id);
  const approval = pending(initial);
  await service.send(first.id, '另一条内存便笺', { demoWrite: true });
  const other = pending(await service.getSession(first.id));
  await assert.rejects(service.decide(second.id, other.id, 'approved'), /不属于/);
  await assert.rejects(service.resume(second.id, approval.id), /尚未决定/);
  await assert.rejects(service.send(second.id, '新一轮'), /未结束/);
  await assert.rejects(service.decide(second.id, approval.id, 'unknown'), /未知/);
  assert.deepEqual(await service.getSession(second.id), initial);

  await service.decide(second.id, approval.id, 'approved');
  await assert.rejects(service.decide(second.id, approval.id, 'denied'), /已作出/);
  await assert.rejects(service.resume(second.id, other.id), /恢复入口/);
  const decided = await service.getSession(second.id);
  assert.equal(decided.session.status, 'waiting_approval');
  assert.equal(decided.tool_calls[0].status, 'awaiting_approval');
  assert.deepEqual(decided.demo, initial.demo);
  assert.equal(decided.events.at(-1).type, 'approval_decided');
  const [audit] = await service.queryAudit({ session_id: second.id });
  assert.equal(audit.actor, 'demo-user');
  assert.equal(audit.detail.approval_id, approval.id);
  assert.ok(audit.created_at < decided.events.at(-1).created_at);

  const results = await Promise.allSettled([service.resume(second.id, approval.id), service.resume(second.id, approval.id)]);
  assert.deepEqual(results.map(({ status }) => status), ['fulfilled', 'rejected']);
  const after = await service.getSession(second.id);
  assert.equal(after.session.status, 'completed');
  assert.equal(after.turn, null);
  assert.equal(after.demo.note, approval.arguments.text);
  assert.equal(after.tool_calls[0].status, 'completed');
  assert.equal(after.tool_calls[0].reversible, true);
  assert.deepEqual(after.events.slice(-4).map(({ type }) => type), ['tool_start', 'tool_end', 'model_response', 'turn_completed']);
  const observation = after.messages.find(({ role }) => role === 'tool');
  assert.equal(observation.tool_call_id, approval.call_id);
  assert.equal(JSON.parse(observation.content).status, 'success');
});

test('rejecting completes without dispatch or another model response', async () => {
  const service = createDemoService();
  const [, row] = await service.listSessions();
  const initial = await service.getSession(row.id);
  const approval = pending(initial);
  await service.decide(row.id, approval.id, 'denied');
  const outcome = await service.resume(row.id, approval.id);
  const snapshot = await service.getSession(row.id);
  assert.equal(snapshot.session.status, 'completed');
  assert.equal(snapshot.tool_calls[0].status, 'denied');
  assert.equal(snapshot.tool_calls[0].reversible, false);
  const events = snapshot.events.slice(initial.events.length);
  assert.deepEqual(events.map(({ type }) => type), ['approval_decided', 'tool_skipped', 'turn_completed']);
  assert.equal(outcome.status, 'completed');
  assert.match(outcome.content, /已拒绝/);
  assert.equal(outcome.pending_call_id, null);
  assert.equal(outcome.iterations, initial.outcome.iterations);
  assert.equal(outcome.tokens, initial.outcome.tokens);
  assert.deepEqual(snapshot.outcome, outcome);
  assert.equal(snapshot.turn, null);
  assert.deepEqual(events.at(-1).payload, outcome);
  const addedMessages = snapshot.messages.slice(initial.messages.length);
  assert.equal(addedMessages.length, 1);
  const [observation] = addedMessages;
  assert.equal(observation.role, 'tool');
  assert.equal(observation.tool_call_id, approval.call_id);
  assert.equal(JSON.parse(observation.content).status, 'denied');
  assert.equal(JSON.parse(observation.content).receipt, null);
  assert.equal(snapshot.demo.note, initial.demo.note);
});

test('interrupted turn can resume without creating another user message', async () => {
  const service = createDemoService();
  const [, , row] = await service.listSessions();
  await assert.rejects(service.resume(row.id, 'foreign-approval'));
  const outcome = await service.resume(row.id);
  assert.equal(outcome.status, 'completed');
  assert.deepEqual((await service.getSession(row.id)).messages.map(({ role, seq }) => [role, seq]), [['user', 1], ['assistant', 2]]);
  await assert.rejects(service.resume(row.id), /没有可用/);
});

test('rollback compensates in reverse order, preserves history, and cannot cross sessions', async () => {
  const service = createDemoService();
  const [row, other] = await service.listSessions();
  const initial = await service.getSession(row.id);
  const cp = await service.createCheckpoint(row.id, 'before');
  assert.equal(cp.seq, initial.messages.at(-1).seq);
  await assert.rejects(service.rollback(other.id, cp.id), /不属于/);
  const calls = [];
  for (const text of ['first', '<script>second</script>']) {
    await service.send(row.id, text, { demoWrite: true });
    await assert.rejects(service.rollback(row.id, cp.id), /未结束|当前轮次/);
    const approval = pending(await service.getSession(row.id));
    calls.push(approval.call_id);
    await service.decide(row.id, approval.id, 'approved');
    await service.resume(row.id, approval.id);
  }
  const before = await service.getSession(row.id);
  const result = await service.rollback(row.id, cp.id);
  assert.deepEqual(result, { checkpoint: cp, reverted: [...calls].reverse(), failed: [] });
  const after = await service.getSession(row.id);
  assert.equal(after.demo.note, initial.demo.note);
  assert.deepEqual(after.messages, before.messages);
  assert.deepEqual(after.tool_calls, before.tool_calls);
  assert.equal(after.events.at(-1).type, 'rollback_performed');
  assert.equal((await service.queryAudit({ session_id: row.id }))[0].action, 'rollback_performed');
  assert.deepEqual((await service.rollback(row.id, cp.id)).reverted, []);
});

test('read-only queries return detached contract records and context trims only the history tail', async () => {
  const service = createDemoService();
  const [row] = await service.listSessions();
  const snapshot = await service.getSession(row.id);
  const tools = await service.listTools();
  assert.equal(tools[0].type, 'function');
  tools[0].function.parameters.type = '<mutated>';
  assert.equal((await service.listTools())[0].function.parameters.type, 'object');
  assert.equal(service.execute, undefined);
  const config = await service.getSettings();
  assert.equal(config.server.port, 8080);
  assert.ok(!JSON.stringify(config).includes('api_key'));
  const own = await service.queryAudit({ session_id: row.id });
  assert.ok(own.every((entry) => entry.session_id === row.id));
  const all = await service.queryAudit({ limit: 2 });
  assert.equal(all.length, 2);
  assert.ok(all[0].created_at > all[1].created_at);
  own[0].detail.run_id = 'mutated';
  assert.notEqual((await service.queryAudit({ session_id: row.id }))[0].detail.run_id, 'mutated');
  assert.equal(snapshot.subagents[0].task.id, snapshot.subagents[0].result.run_id);
  for (let i = 0; i < 5; i++) await service.send(row.id, `消息 ${i}`);
  const after = await service.getSession(row.id);
  assert.deepEqual(after.context.prefix, snapshot.context.prefix);
  assert.equal(after.session.prefix_hash, snapshot.session.prefix_hash);
  assert.equal(after.session.agents_md_version, after.agents_document.version);
  assert.equal(after.context.messages.length, 8);
  assert.equal(after.context.trimmed_messages, 4);
  after.messages[0].content = 'mutated';
  assert.equal((await service.getSession(row.id)).messages[0].content, snapshot.messages[0].content);
});

test('context reconstructs assistant tool calls and keeps their observations together at the trim boundary', async () => {
  const service = createDemoService();
  const [, row] = await service.listSessions();
  const initial = await service.getSession(row.id);
  const approval = pending(initial);
  const [call] = initial.tool_calls;
  const [requester, ...otherMessages] = initial.messages.filter(({ role }) => role === 'assistant');
  assert.equal(otherMessages.length, 0);
  assert.equal(requester.id, call.message_id);
  const projectedRequest = { role: 'assistant', content: requester.content, tool_calls: [{
    id: call.id, type: 'function', function: { name: call.tool_name, arguments: call.arguments },
  }] };
  assert.deepEqual(initial.context.messages.at(-1), projectedRequest);

  await service.decide(row.id, approval.id, 'approved');
  await service.resume(row.id, approval.id);
  const untrimmed = await service.getSession(row.id);
  const observation = untrimmed.messages[2];
  const projectedObservation = { role: 'tool', content: observation.content, tool_call_id: call.id };
  assert.deepEqual(untrimmed.context.messages.slice(1, 3), [projectedRequest, projectedObservation]);
  assert.equal(untrimmed.context.trimmed_messages, 0);

  for (let i = 0; i < 3; i++) await service.send(row.id, `普通消息 ${i}`);
  const trimmed = await service.getSession(row.id);
  assert.equal(trimmed.messages.at(-8).tool_call_id, call.id);
  assert.deepEqual(trimmed.context.prefix, initial.context.prefix);
  assert.equal(trimmed.context.trimmed_messages, 3);
  assert.deepEqual(trimmed.context.messages, trimmed.messages.slice(3).map(({ role, content }) => ({ role, content })));
});