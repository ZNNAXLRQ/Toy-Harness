const errors = new Map([
  ['invalid_request', '请求无效。请输入 1–8192 个字符。'],
  ['provider_unavailable', '服务端模型未能初始化，请检查服务端配置。'],
  ['tool_denied', '工具被服务端拒绝，本轮已结束。'],
  ['run_failed', 'Agent 未能完成本轮。'],
  ['internal_error', 'Web 服务内部错误。'],
]);

const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const invalid = () => new Error('Web 服务返回了无效的数据。');

export function createWebService(fetcher = globalThis.fetch.bind(globalThis)) {
  async function request(path, { prompt } = {}) {
    const options = { mode: 'same-origin', redirect: 'error', cache: 'no-store' };
    if (prompt !== undefined) Object.assign(options, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt }),
    });
    let response;
    try { response = await fetcher(path, options); }
    catch { throw new Error('无法连接同源 Web 服务；请确认服务仍在运行。'); }

    const data = await response.json().catch(() => null);
    const result = data?.result;
    const error = !response.ok
      ? `${errors.get(result?.error ?? data?.error) ?? 'Web 请求失败。'}（HTTP ${response.status}）`
      : null;
    if (error && !(prompt !== undefined && result?.success === false)) throw new Error(error);
    if (!object(data)) throw invalid();
    return { data, error };
  }

  return {
    async getRuntime() {
      const { data } = await request('/api/runtime');
      const caps = data.capabilities;
      if (!['offline', 'configured'].includes(data.provider_mode) || caps?.single_turn !== true ||
          caps.tool_policy !== 'read_only' || caps.event_delivery !== 'with_result' || caps.session_resume !== false) {
        throw new Error('此页面不支持 Web 服务返回的运行能力。');
      }
      return data.provider_mode;
    },
    async listTools() {
      const { data } = await request('/api/tools');
      if (!Array.isArray(data.tools) || !data.tools.every((tool) => typeof tool?.enabled === 'boolean' &&
          typeof tool.schema?.function?.name === 'string' && object(tool.schema.function.parameters))) throw invalid();
      return data.tools;
    },
    async run(prompt) {
      const { data: { result, events }, error } = await request('/api/runs', { prompt });
      if (typeof result?.run_id !== 'string' || typeof result.response !== 'string' || result.success !== !error ||
          !Array.isArray(events) || !events.every((event) => event?.run_id === result.run_id &&
            Number.isInteger(event.seq) && typeof event.kind === 'string')) throw invalid();
      return { result, events, error };
    },
  };
}