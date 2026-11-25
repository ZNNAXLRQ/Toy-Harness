(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const form = $("#run-form");
  const promptInput = $("#prompt");
  const modeButtons = [...document.querySelectorAll("[data-mode]")];
  const runButton = $("#run-button");
  const runState = $("#run-state");
  const providerLabel = $("#provider-label");
  const formError = $("#form-error");
  const modeNote = $("#mode-note");
  const demoExample = $("#demo-example");
  const feed = $("#conversation-feed");
  const timeline = $("#event-timeline");
  const timelineEmpty = $("#timeline-empty");
  const eventCount = $("#event-count");
  const streamIndicator = $("#stream-indicator");

  let mode = "approval";
  let config = { provider: "demo", demo_prompt: "" };
  let source = null;
  let activeRun = null;
  let seenSequences = new Set();
  let eventTotal = 0;
  const toolsByCall = new Map();
  const approvalsById = new Map();
  const requestsByIteration = new Map();

  const labels = {
    user_message: "用户任务",
    model_request: "模型请求",
    model_response: "模型响应",
    approval_required: "等待审批",
    approval_decided: "审批决定",
    tool_start: "工具开始",
    tool_end: "工具结果",
    tool_skipped: "工具未执行",
    turn_completed: "任务完成",
    turn_failed: "任务失败",
  };

  function safeString(value, fallback = "") {
    if (value === null || value === undefined) return fallback;
    if (typeof value === "string") return value;
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }

  function setState(label, state = "") {
    runState.textContent = label;
    runState.dataset.state = state;
  }

  function setStream(label, state = "") {
    streamIndicator.dataset.state = state;
    streamIndicator.lastElementChild.textContent = label;
  }

  function scrollTo(element) {
    const behavior = window.matchMedia("(prefers-reduced-motion: reduce)")
      .matches
      ? "auto"
      : "smooth";
    element.scrollIntoView({ block: "nearest", behavior });
  }

  function showError(message) {
    formError.textContent = message;
    formError.hidden = false;
  }

  function hideError() {
    formError.textContent = "";
    formError.hidden = true;
  }

  function addMessage(role, label, content, meta = "") {
    const empty = $(".empty-conversation", feed);
    if (empty) empty.remove();
    const article = document.createElement("article");
    article.className = `message ${role}`;
    const heading = document.createElement("div");
    heading.className = "message-label";
    heading.textContent = label;
    const body = document.createElement("div");
    body.className = "message-body";
    body.textContent = safeString(content);
    article.append(heading, body);
    if (meta) {
      const foot = document.createElement("div");
      foot.className = "message-meta";
      foot.textContent = meta;
      article.append(foot);
    }
    feed.append(article);
    scrollTo(article);
    return article;
  }

  function formattedTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  function addTimelineItem(type, title, description, timestamp) {
    timelineEmpty.hidden = true;
    const item = document.createElement("li");
    item.className = "timeline-item";
    item.dataset.event = type;
    const rail = document.createElement("span");
    rail.className = "timeline-rail";
    rail.setAttribute("aria-hidden", "true");
    const dot = document.createElement("span");
    dot.className = "timeline-dot";
    rail.append(dot);
    const content = document.createElement("div");
    content.className = "timeline-content";
    const titleRow = document.createElement("div");
    titleRow.className = "timeline-title-row";
    const titleNode = document.createElement("span");
    titleNode.className = "timeline-title";
    titleNode.textContent = title;
    const time = document.createElement("time");
    time.className = "timeline-time";
    time.textContent = formattedTime(timestamp);
    titleRow.append(titleNode, time);
    content.append(titleRow);
    const desc = document.createElement("p");
    desc.className = "timeline-description";
    desc.textContent = safeString(description);
    content.append(desc);
    item.append(rail, content);
    timeline.append(item);
    return { item, content, titleNode, desc };
  }

  function addDetails(parent, label, value) {
    const details = document.createElement("details");
    details.className = "timeline-details";
    const summary = document.createElement("summary");
    summary.textContent = label;
    const pre = document.createElement("pre");
    pre.textContent = safeString(value, "无数据");
    details.append(summary, pre);
    parent.append(details);
    return details;
  }

  function updateToolCard(callId, payload, type, timestamp) {
    let card = toolsByCall.get(callId);
    if (!card) {
      card = addTimelineItem(
        type,
        payload.name || payload.tool_name || "工具调用",
        "",
        timestamp,
      );
      card.item.dataset.callId = callId;
      card.item.dataset.tone = type === "tool_skipped" ? "warning" : "active";
      toolsByCall.set(callId, card);
    }
    const toolName = payload.name || payload.tool_name || "工具";
    card.titleNode.textContent = `${toolName} · ${type === "tool_end" ? "已返回" : type === "tool_skipped" ? "未执行" : "执行中"}`;
    card.desc.textContent =
      type === "tool_end"
        ? `状态：${safeString(payload.status, "未知")}${Number.isFinite(payload.elapsed_ms) ? ` · ${payload.elapsed_ms} ms` : ""}`
        : type === "tool_skipped"
          ? safeString(payload.reason, "该调用未执行")
          : "工具正在处理本次调用。";
    if (type === "tool_end")
      card.item.dataset.tone =
        payload.status === "error" || payload.status === "failed"
          ? "error"
          : "done";
    if (type === "tool_skipped") card.item.dataset.tone = "warning";
    if (payload.arguments !== undefined && !card.argumentsDetails)
      card.argumentsDetails = addDetails(
        card.content,
        "查看调用参数",
        payload.arguments,
      );
    if (type === "tool_end" && payload.result !== undefined) {
      if (card.resultDetails) card.resultDetails.remove();
      card.resultDetails = addDetails(
        card.content,
        "查看工具结果",
        payload.result,
      );
    }
    return card;
  }

  function makeApproval(payload, timestamp) {
    const empty = $(".empty-conversation", feed);
    if (empty) empty.remove();
    const approvalId = safeString(payload.approval_id);
    const card = document.createElement("section");
    card.className = "approval-card";
    card.dataset.approvalId = approvalId;
    const title = document.createElement("div");
    title.className = "approval-title";
    const heading = document.createElement("strong");
    heading.textContent = `是否允许 ${safeString(payload.tool_name, "工具")}？`;
    const tag = document.createElement("code");
    tag.className = "tool-tag";
    tag.textContent = safeString(payload.tool_name, "tool");
    title.append(heading, tag);
    const summary = document.createElement("p");
    summary.className = "approval-summary";
    summary.textContent = "请检查即将执行的调用参数，再选择是否继续。";
    const params = document.createElement("details");
    params.className = "json-details";
    params.open = true;
    const summaryToggle = document.createElement("summary");
    summaryToggle.textContent = "完整调用参数";
    const pre = document.createElement("pre");
    pre.textContent = safeString(payload.arguments, "无参数");
    params.append(summaryToggle, pre);
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    const approve = document.createElement("button");
    approve.type = "button";
    approve.className = "approve-button";
    approve.textContent = "批准调用";
    const deny = document.createElement("button");
    deny.type = "button";
    deny.className = "deny-button";
    deny.textContent = "拒绝";
    actions.append(approve, deny);
    const error = document.createElement("p");
    error.className = "approval-error";
    error.hidden = true;
    error.setAttribute("role", "alert");
    const time = formattedTime(timestamp);
    if (time) {
      const foot = document.createElement("p");
      foot.className = "decision-label";
      foot.textContent = `请求时间 ${time}`;
      card.append(title, summary, params, actions, error, foot);
    } else card.append(title, summary, params, actions, error);
    const record = { card, actions, error, buttons: [approve, deny] };
    approvalsById.set(approvalId, record);
    const decide = (decision) => submitDecision(approvalId, decision, record);
    approve.addEventListener("click", () => decide("approved"));
    deny.addEventListener("click", () => decide("denied"));
    feed.append(card);
    scrollTo(card);
  }

  async function submitDecision(approvalId, decision, record) {
    record.error.hidden = true;
    record.error.textContent = "";
    record.buttons.forEach((button) => {
      button.disabled = true;
    });
    try {
      const response = await fetch(
        `/api/runs/${encodeURIComponent(activeRun)}/approvals/${encodeURIComponent(approvalId)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision }),
        },
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok)
        throw new Error(
          body.detail ||
            body.error ||
            `审批请求失败（HTTP ${response.status}）`,
        );
      record.actions.remove();
      const decided = document.createElement("p");
      decided.className = "decision-label";
      decided.textContent = `已提交${body.decision === "denied" ? "拒绝" : "批准"}决定，等待运行结果。`;
      record.card.append(decided);
    } catch (error) {
      record.error.textContent = `${error.message}。可重试，当前审批仍保留。`;
      record.error.hidden = false;
      record.buttons.forEach((button) => {
        button.disabled = false;
      });
    }
  }

  function terminal(state, label, indicator) {
    setState(label, state);
    runButton.disabled = false;
    runButton.querySelector("span:first-child").textContent = "再次运行";
    setStream(indicator, state === "failed" ? "error" : "");
    if (source) {
      source.close();
      source = null;
    }
  }

  function handleEvent(type, raw) {
    let event;
    try {
      event = JSON.parse(raw.data);
    } catch {
      addTimelineItem(
        "stream_error",
        "无法读取服务端事件",
        "收到的数据格式无效。",
        new Date().toISOString(),
      );
      return;
    }
    // EventSource reconnects can replay the last record; seq is unique within each run.
    if (event.seq !== undefined && event.seq !== null) {
      const sequence = String(event.seq);
      if (seenSequences.has(sequence)) return;
      seenSequences.add(sequence);
    }
    const payload = event.payload || {};
    eventTotal += 1;
    eventCount.textContent = String(eventTotal);
    const title = labels[type] || type.replaceAll("_", " ");
    const stamp = event.created_at;

    switch (type) {
      case "user_message":
        addMessage(
          "user",
          "你的任务",
          payload.content ?? payload.message ?? "",
          formattedTime(stamp),
        );
        addTimelineItem(
          type,
          title,
          payload.content ?? payload.message ?? "任务已送达。",
          stamp,
        );
        setState("正在执行", "running");
        break;
      case "model_request": {
        const info = [`第 ${payload.iteration ?? "?"} 轮`, payload.provider]
          .filter(Boolean)
          .join(" · ");
        const row = addTimelineItem(
          type,
          title,
          info || "模型正在处理上下文。",
          stamp,
        );
        row.item.dataset.tone = "active";
        requestsByIteration.set(String(payload.iteration), row.item);
        setState("模型处理中", "running");
        break;
      }
      case "model_response": {
        const calls = Array.isArray(payload.tool_calls)
          ? payload.tool_calls
          : [];
        const names = calls.map(
          (call) => call.function?.name || call.name || "工具",
        );
        const desc = calls.length
          ? `提出 ${calls.length} 个工具调用：${names.join("、")}`
          : payload.content || "模型返回了响应。";
        const request = requestsByIteration.get(String(payload.iteration));
        if (request) request.dataset.tone = "done";
        const row = addTimelineItem(
          type,
          title,
          `第 ${payload.iteration ?? "?"} 轮 · ${safeString(desc)}`,
          stamp,
        );
        row.item.dataset.tone = "done";
        break;
      }
      case "approval_required": {
        const row = addTimelineItem(
          type,
          `需要审批 · ${safeString(payload.tool_name, "工具")}`,
          "等待你检查参数并决定是否继续。",
          stamp,
        );
        row.item.dataset.tone = "warning";
        if (mode === "approval") {
          makeApproval(payload, stamp);
          setState("等待审批", "waiting");
        }
        break;
      }
      case "approval_decided": {
        const timedOut = ["timeout", "timed_out", "expired"].includes(
          payload.decision,
        );
        const denied = payload.decision === "denied";
        const decision = timedOut ? "已超时" : denied ? "已拒绝" : "已批准";
        const row = addTimelineItem(
          type,
          `审批${decision}`,
          `工具：${safeString(payload.call_id, payload.tool_name || "调用")} · 决定由 ${safeString(payload.actor, "用户")} 提交。`,
          stamp,
        );
        row.item.dataset.tone = denied || timedOut ? "error" : "done";
        const record = approvalsById.get(safeString(payload.approval_id));
        if (record && record.actions.isConnected) {
          record.actions.remove();
          const status = document.createElement("p");
          status.className = "decision-label";
          status.textContent = decision;
          record.card.append(status);
        }
        setState(
          timedOut ? "审批已超时" : denied ? "审批已拒绝" : "正在恢复执行",
          denied || timedOut ? "failed" : "running",
        );
        break;
      }
      case "tool_start":
        updateToolCard(safeString(payload.call_id), payload, type, stamp);
        setState("工具执行中", "running");
        break;
      case "tool_end":
        updateToolCard(safeString(payload.call_id), payload, type, stamp);
        break;
      case "tool_skipped":
        updateToolCard(safeString(payload.call_id), payload, type, stamp);
        break;
      case "turn_completed": {
        const row = addTimelineItem(
          type,
          title,
          `共 ${payload.iterations ?? "?"} 轮 · 执行完成。`,
          stamp,
        );
        row.item.dataset.tone = "done";
        if (payload.content)
          addMessage("assistant", "Agent · 最终答复", payload.content);
        terminal("complete", "已完成", "事件流已结束");
        break;
      }
      case "turn_failed": {
        const reason = payload.error || "任务未能完成。";
        const row = addTimelineItem(
          type,
          title,
          `${payload.code ? `${payload.code} · ` : ""}${reason}`,
          stamp,
        );
        row.item.dataset.tone = "error";
        for (const record of approvalsById.values()) {
          if (!record.actions.isConnected) continue;
          record.actions.remove();
          const status = document.createElement("p");
          status.className = "decision-label";
          status.textContent =
            payload.code === "approval_timeout"
              ? "审批已超时"
              : "任务已终止，调用未执行";
          record.card.append(status);
        }
        terminal(
          "failed",
          payload.code === "approval_denied"
            ? "审批已拒绝"
            : payload.code === "approval_timeout"
              ? "审批超时"
              : "执行失败",
          "事件流已结束",
        );
        break;
      }
      default:
        addTimelineItem(type, title, payload, stamp);
    }
  }

  function connectEvents(runId) {
    if (source) source.close();
    const connection = new EventSource(
      `/api/runs/${encodeURIComponent(runId)}/events`,
    );
    source = connection;
    setStream("正在连接事件流", "reconnecting");
    connection.onopen = () => {
      if (source === connection) setStream("实时事件流已连接", "connected");
    };
    connection.onerror = () => {
      if (source !== connection) return;
      if (connection.readyState !== EventSource.CLOSED) {
        setStream("连接中断 · 正在自动重连", "reconnecting");
      } else {
        source = null;
        setState("事件流已关闭", "failed");
        setStream("事件流已关闭", "error");
        runButton.disabled = false;
        runButton.querySelector("span:first-child").textContent = "再次运行";
      }
    };
    Object.keys(labels).forEach((type) =>
      connection.addEventListener(type, (event) => {
        if (source === connection) handleEvent(type, event);
      }),
    );
  }

  async function loadConfig() {
    try {
      const response = await fetch("/api/config");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      config = { ...config, ...data };
      const isDemo = config.provider === "demo";
      providerLabel.textContent = isDemo
        ? "演示环境 · Demo"
        : "在线模型 · DeepSeek Flash";
      providerLabel.parentElement.querySelector(
        ".environment-dot",
      ).style.background = isDemo ? "#c18c43" : "#4c9a7c";
      if (isDemo) {
        demoExample.hidden = false;
        if (!promptInput.value) promptInput.value = config.demo_prompt || "";
        modeNote.textContent =
          mode === "approval"
            ? "演示任务会读取并更新 notes.txt；读取自动执行，写入前会展示参数并等待你的决定。"
            : "演示任务会读取并更新 notes.txt；所选模式允许工具直接执行。";
      }
    } catch {
      providerLabel.textContent = "服务配置暂不可用";
      setStream("无法读取服务配置", "error");
    }
  }

  modeButtons.forEach((button) =>
    button.addEventListener("click", () => {
      mode = button.dataset.mode;
      modeButtons.forEach((option) => {
        const selected = option === button;
        option.classList.toggle("is-selected", selected);
        option.setAttribute("aria-pressed", String(selected));
      });
      modeNote.textContent =
        mode === "approval"
          ? config.provider === "demo"
            ? "演示任务会读取并更新 notes.txt；读取自动执行，写入前会展示参数并等待你的决定。"
            : "读取与目录查看会自动执行；写入文件前会展示完整参数并等待你的决定。"
          : "工具调用将自动执行，不会显示审批卡片。请确认任务指令范围明确。";
    }),
  );

  demoExample.addEventListener("click", () => {
    promptInput.value = config.demo_prompt || "";
    promptInput.focus();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    hideError();
    const prompt = promptInput.value.trim();
    if (!prompt) {
      showError("请先输入任务指令。");
      promptInput.focus();
      return;
    }
    if (source) {
      source.close();
      source = null;
    }
    activeRun = null;
    seenSequences = new Set();
    toolsByCall.clear();
    approvalsById.clear();
    requestsByIteration.clear();
    eventTotal = 0;
    eventCount.textContent = "0";
    timeline.replaceChildren();
    timelineEmpty.hidden = false;
    feed.replaceChildren();
    runButton.disabled = true;
    runButton.querySelector("span:first-child").textContent = "正在启动…";
    setState("创建任务中", "running");
    setStream("正在创建事件流", "reconnecting");
    try {
      const response = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, mode }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok)
        throw new Error(
          data.detail ||
            data.error ||
            `创建任务失败（HTTP ${response.status}）`,
        );
      if (!data.run_id) throw new Error("服务端没有返回 run_id。");
      activeRun = data.run_id;
      runButton.disabled = true;
      runButton.querySelector("span:first-child").textContent = "运行中";
      connectEvents(activeRun);
    } catch (error) {
      runButton.disabled = false;
      runButton.querySelector("span:first-child").textContent = "重试运行";
      setState("启动失败", "failed");
      setStream("无法创建事件流", "error");
      showError(error.message);
    }
  });

  loadConfig();
})();
