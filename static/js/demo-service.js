// Contract-shaped fixtures. Optional tab snapshots; no provider or tool execution.
const EPOCH = 1767607200;
const ACTOR = 'demo-user';
const NOTE_TOOL = 'demo_note';
const CONTEXT_HISTORY_LIMIT = 8;
const tools = [
  { name: 'demo_read', description: '查看示例便笺文本（目录条目，不可运行）。', parameters: { type: 'object', properties: {} } },
  { name: NOTE_TOOL, description: '模拟更新内存便笺；需要审批，并支持检查点补偿。', parameters: {
    type: 'object', properties: { text: { type: 'string' } }, required: ['text'], additionalProperties: false,
  } },
  { name: 'demo_review', description: '示例子代理的只读检查条目。', parameters: {
    type: 'object', properties: { topic: { type: 'string' } }, required: ['topic'],
  } },
];
const documentFixture = {
  version: 'demo-agents-v1', source: 'example/AGENTS.md', skills: ['demo-review'],
  content: '# 示例工作区\n仅查看示例资料。模拟写入必须经人工审批。',
};

function prepareContext({ prefix, messages, tool_calls }) {
  const callsByMessage = new Map();
  for (const call of tool_calls) {
    const calls = callsByMessage.get(call.message_id) ?? [];
    calls.push({ id: call.id, type: 'function', function: { name: call.tool_name, arguments: call.arguments } });
    callsByMessage.set(call.message_id, calls);
  }
  const requestIndexByCall = new Map();
  const history = messages.map(({ id, role, content, tool_call_id }, index) => {
    const calls = role === 'assistant' ? callsByMessage.get(id) : undefined;
    for (const call of calls ?? []) requestIndexByCall.set(call.id, index);
    return { role, content, ...(tool_call_id ? { tool_call_id } : {}), ...(calls ? { tool_calls: calls } : {}) };
  });

  let start = Math.max(0, history.length - CONTEXT_HISTORY_LIMIT);
  // Keep the newest suffix within the limit; if the cut splits a call group,
  // advance past its observations rather than including an orphaned tool message.
  for (let i = start; i < history.length; i++) {
    const { role, tool_call_id } = history[i];
    if (role === 'tool' && (requestIndexByCall.get(tool_call_id) ?? -1) < start) start = i + 1;
  }
  const recent = history.slice(start);
  const estimated_tokens = Math.ceil(JSON.stringify([...prefix, ...recent]).length / 4);
  return { prefix, messages: recent, estimated_tokens, trimmed_messages: start, summarized: false };
}

export function createDemoService({ snapshot, save } = {}) {
  const sessions = new Map(snapshot?.sessions.map((state) => [
    state.row.id, { ...state, compensated: new Set(state.compensated) },
  ]));
  const listeners = new Map();
  let tick = snapshot?.tick ?? 0;
  let serial = snapshot?.serial ?? 0;
  const now = () => EPOCH + tick++;
  const uid = (kind) => `${kind}-${String(++serial).padStart(3, '0')}`;
  const lastSeq = (state) => state.messages.at(-1)?.seq ?? 0;
  const session = (id) => {
    const state = sessions.get(id);
    if (!state) throw new Error('找不到所选会话。');
    return state;
  };
  function audit(state, action, detail = null, actor = 'demo-system') {
    state.audit.push({ id: uid('audit'), session_id: state.row.id, actor, action, detail, created_at: now() });
  }
  function publish(state, type, payload, seq = lastSeq(state)) {
    const event = { type, session_id: state.row.id, seq, payload, created_at: now() };
    state.events.push(event);
    // Notify after the operation's synchronous writes; observers cannot break the state machine.
    for (const notify of listeners.get(state.row.id) ?? []) {
      queueMicrotask(() => {
        if (!listeners.get(state.row.id)?.has(notify)) return;
        try { notify(structuredClone(event)); } catch (error) { console.error('Demo event listener:', error); }
      });
    }
  }
  function addSession(title) {
    const id = uid('session');
    const time = now();
    const state = {
      row: { id, title, status: 'idle', agents_md_version: documentFixture.version,
        prefix_hash: 'demo-prefix-v1', created_at: time, updated_at: time },
      messages: [], tool_calls: [], approvals: [], events: [], checkpoints: [], audit: [], subagents: [],
      observations: [], compensated: new Set(), note: '示例便笺 · 初始版本', turn: null, outcome: null, rollback: null,
      prefix: [{ role: 'system', content: documentFixture.content }],
    };
    sessions.set(id, state);
    audit(state, 'session_created', { title }, ACTOR);
    return state;
  }
  function append(state, role, content, tool_call_id = null) {
    const message = { id: uid('msg'), session_id: state.row.id, seq: lastSeq(state) + 1, role,
      content, tool_call_id, created_at: now() };
    state.messages.push(message);
    state.row.updated_at = message.created_at;
    return message;
  }
  function setOutcome(state, status, content = null, pending_call_id = null) {
    state.row.status = status;
    state.row.updated_at = now();
    state.outcome = { session_id: state.row.id, status, content, pending_call_id,
      iterations: state.turn.iteration, tokens: state.turn.iteration * 24, error: null };
    state.turn = ['open', 'waiting_approval'].includes(status)
      ? { ...state.turn, status, pending_call_id }
      : null;
    return state.outcome;
  }
  function model(state, content, tool_calls = []) {
    const response = { content, tool_calls, finish_reason: tool_calls.length ? 'tool_calls' : 'stop', tokens: 24 };
    state.turn.iteration++;
    const message = append(state, 'assistant', response.content);
    for (const call of response.tool_calls) {
      state.tool_calls.push({ id: call.id, session_id: state.row.id, message_id: message.id,
        tool_name: call.name, arguments: call.arguments, status: 'pending', reversible: false, created_at: now() });
    }
    publish(state, 'model_response', response);
    return message;
  }
  function begin(state, content) {
    if (!['idle', 'completed'].includes(state.row.status)) throw new Error('请先处理当前未结束的轮次。');
    if (typeof content !== 'string' || !content.trim()) throw new Error('请输入消息。');
    state.turn = { session_id: state.row.id, status: 'open', iteration: 0, pending_call_id: null, notes: {} };
    state.row.status = 'open';
    append(state, 'user', content);
    publish(state, 'user_message', { content });
  }
  function finishTurn(state, content) {
    const outcome = setOutcome(state, 'completed', content);
    publish(state, 'turn_completed', outcome);
    return outcome;
  }
  function complete(state, content) {
    model(state, content);
    return finishTurn(state, content);
  }
  function checkpoint(state, description = null) {
    const row = { id: uid('cp'), session_id: state.row.id, seq: lastSeq(state), description, created_at: now() };
    state.checkpoints.push(row);
    publish(state, 'checkpoint_created', row);
    return row;
  }
  function requestChange(state, text) {
    const arguments_ = { text };
    const id = uid('call');
    model(state, '我准备模拟更新便笺，请先确认。', [{ id, name: NOTE_TOOL, arguments: JSON.stringify(arguments_) }]);
    state.tool_calls.at(-1).status = 'awaiting_approval';
    const approval = { id: uid('approval'), session_id: state.row.id, call_id: id, tool_name: NOTE_TOOL,
      arguments: arguments_, description: '仅修改此会话的内存便笺，不会写入任何文件。', created_at: now(),
      decision: null, decided_by: null };
    state.approvals.push(approval);
    const outcome = setOutcome(state, 'waiting_approval', null, id);
    publish(state, 'approval_required', approval);
    return outcome;
  }
  function recordDecision(state, approvalId, decision) {
    if (!['approved', 'denied'].includes(decision)) throw new Error('未知的审批决定。');
    const approval = state.approvals.find(({ id }) => id === approvalId);
    if (!approval || approval.call_id !== state.turn?.pending_call_id || state.turn.status !== 'waiting_approval') {
      throw new Error('此审批不属于当前等待的轮次。');
    }
    if (approval.decision) throw new Error('此审批已作出决定，请恢复轮次。');
    Object.assign(approval, { decision, decided_by: ACTOR });
    audit(state, 'approval_decided', { approval_id: approvalId, call_id: approval.call_id, decision }, ACTOR);
    publish(state, 'approval_decided', approval);
    return approval;
  }
  function resume(state, approvalId) {
    if (state.turn?.status === 'open' && approvalId == null) {
      const interrupted = state.tool_calls.filter(({ status }) => status === 'dispatched');
      if (interrupted.length) {
        for (const call of interrupted) call.status = 'unknown';
        const outcome = setOutcome(state, 'failed');
        outcome.error = '派发后没有观察结果；状态未知，不重放。';
        publish(state, 'turn_failed', outcome);
        return outcome;
      }
      return complete(state, '已恢复这条未完成的演示轮次。没有调用任何真实服务。');
    }
    const approval = state.approvals.find(({ id }) => id === approvalId);
    const call = state.tool_calls.find(({ id }) => id === state.turn?.pending_call_id);
    if (state.turn?.status !== 'waiting_approval' || call?.status !== 'awaiting_approval'
        || approval?.call_id !== call.id || !approval.decision) {
      throw new Error('没有可用的恢复入口，或审批尚未决定／已消费。');
    }
    const approved = approval.decision === 'approved';
    const previous = state.note;
    if (approved) {
      call.status = 'approved';
      call.status = 'dispatched';
      publish(state, 'tool_start', { call_id: call.id, tool_name: call.tool_name });
      state.note = approval.arguments.text;
    }
    const observation = { status: approved ? 'success' : 'denied', result: approved ? state.note : '用户拒绝；未派发。',
      receipt: approved ? JSON.stringify({ previous }) : null };
    const message = append(state, 'tool', JSON.stringify(observation), call.id);
    state.observations.push({ call_id: call.id, seq: message.seq, ...observation });
    Object.assign(call, { status: approved ? 'completed' : 'denied', reversible: approved });
    publish(state, approved ? 'tool_end' : 'tool_skipped', { call_id: call.id, status: observation.status, result: observation.result });
    if (!approved) return finishTurn(state, '已拒绝，示例便笺没有改变。');
    return complete(state, '便笺已在演示内存中更新。可到检查点页模拟撤销。');
  }
  function rollback(state, checkpointId) {
    const point = state.checkpoints.find(({ id }) => id === checkpointId);
    if (!point) throw new Error('此检查点不属于当前会话。');
    if (state.turn) throw new Error('请先结束或恢复当前轮次，再模拟回滚。');
    const result = { checkpoint: point, reverted: [], failed: [] };
    for (const record of [...state.observations].reverse()) {
      if (record.seq <= point.seq || !record.receipt || state.compensated.has(record.call_id)) continue;
      state.note = JSON.parse(record.receipt).previous;
      state.compensated.add(record.call_id);
      result.reverted.push(record.call_id);
    }
    state.rollback = result;
    audit(state, 'rollback_performed', result, ACTOR);
    publish(state, 'rollback_performed', result);
    return result;
  }

  if (!snapshot) seed();

  function seed() {
    const first = addSession('工作区巡检');
    begin(first, '梳理一下工作区，看看有哪些待办。');
    complete(first, '示例工作区已整理：可以查看子代理摘要、检查点和审计记录。\n试试发送一条消息，或模拟更新便笺来体验审批。');
    checkpoint(first, '巡检完成 · 初始检查点');
    const review = { id: uid('run'), parent_session_id: first.row.id, parent_call_id: uid('call'),
      prompt: '整理示例工作区的目录摘要。', allowed_tools: ['demo_review'], max_iterations: 8, depth: 1, context: {} };
    publish(first, 'subagent_started', { run_id: review.id });
    audit(first, 'subagent_started', { run_id: review.id });
    const result = { run_id: review.id, status: 'success', summary: '已汇总示例目录，未修改文件。', tokens: 156, steps: 3, detail: null };
    first.subagents.push({ task: review, result });
    publish(first, 'subagent_finished', result);
    audit(first, 'subagent_finished', { run_id: review.id, status: result.status });
    const pending = addSession('便笺更新');
    checkpoint(pending, '更新便笺前');
    begin(pending, '请模拟更新示例便笺。');
    requestChange(pending, '便笺已更新 · 审批演示');
    const interrupted = addSession('中断恢复示例');
    begin(interrupted, '这条示例消息尚未收到模型回复。');
    interrupted.subagents.push({ task: { id: uid('run'), parent_session_id: interrupted.row.id,
      parent_call_id: uid('call'), prompt: '等待恢复后整理示例资料。', allowed_tools: ['demo_review'],
      max_iterations: 8, depth: 1, context: {} }, result: null });
    publish(interrupted, 'subagent_started', { run_id: interrupted.subagents[0].task.id });
    audit(interrupted, 'subagent_started', { run_id: interrupted.subagents[0].task.id });
  }

  function commit(result) {
    save?.({ tick, serial, sessions: [...sessions.values()].map((state) => ({
      ...state, compensated: [...state.compensated],
    })) });
    return structuredClone(result);
  }

  return {
    mode: 'demo',
    async listSessions() { return structuredClone([...sessions.values()].map(({ row }) => row)); },
    async createSession(title) { return commit(addSession(title?.trim() || '新会话').row); },
    async getSession(id) {
      const s = session(id);
      return structuredClone({ session: s.row, messages: s.messages, tool_calls: s.tool_calls, approvals: s.approvals,
        turn: s.turn, outcome: s.outcome, events: s.events, checkpoints: s.checkpoints, subagents: s.subagents,
        agents_document: documentFixture, context: prepareContext(s),
        demo: { note: s.note, rollback: s.rollback } });
    },
    subscribe(id, listener) {
      session(id);
      const current = listeners.get(id) ?? new Set();
      listeners.set(id, current);
      const notify = (event) => listener(event);
      current.add(notify);
      return () => {
        current.delete(notify);
        if (!current.size && listeners.get(id) === current) listeners.delete(id);
      };
    },
    async send(id, content, { demoWrite = false } = {}) {
      const s = session(id);
      begin(s, content);
      return commit(demoWrite ? requestChange(s, content)
        : complete(s, `收到这条演示消息：\n${content}\n\n这是本地固定规则回复，没有请求模型。`));
    },
    async decide(id, approvalId, decision) { return commit(recordDecision(session(id), approvalId, decision)); },
    async resume(id, approvalId = null) { return commit(resume(session(id), approvalId)); },
    async createCheckpoint(id, description) { return commit(checkpoint(session(id), description?.trim() || null)); },
    async rollback(id, checkpointId) { return commit(rollback(session(id), checkpointId)); },
    async listTools() { return structuredClone(tools.map((tool) => ({ type: 'function', function: tool }))); },
    async queryAudit({ session_id, limit = 100 } = {}) {
      const rows = session_id == null ? [...sessions.values()].flatMap((s) => s.audit) : session(session_id).audit;
      return structuredClone([...rows].sort((a, b) => b.created_at - a.created_at).slice(0, limit));
    },
    async getSettings() {
      return { server: { port: 8080 }, database: { path: './data/harness.db' }, logging: { level: 'INFO' },
        llm: { active: 'deepseek', providers: { deepseek: { adapter: 'deepseek', base_url: null,
          model: null, timeout_seconds: 60, max_retries: 0 } } } };
    },
  };
}