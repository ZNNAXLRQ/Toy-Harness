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
  const emptyConversation = $(".empty-conversation", feed);
  const timeline = $("#event-timeline");
  const timelineEmpty = $("#timeline-empty");
  const eventCount = $("#event-count");
  const streamIndicator = $("#stream-indicator");
  const viewButtons = [...document.querySelectorAll("[data-workspace-view]")];
  const workspaceView = $("#workspace-view");
  const workspaceViewTitle = $("#workspace-view-title");
  const workspaceContent = $("#workspace-view-content");
  const conversation = $("#chat-conversation");
  const sessionTitle = $("#session-title");
  const newSessionButton = $("#new-session");
  const progressTemplate = $("#agent-progress-template");

  let mode = "approval";
  let config = { provider: "demo", demo_prompt: "" };
  let source = null;
  let activeRun = null;
  let seenSequences = new Set();
  let eventTotal = 0;
  const toolsByCall = new Map();
  const approvalsById = new Map();
  const requestsByIteration = new Map();
  const sessions = [];
  const auditEvents = [];
  let progressCard = null;
  let runStatus = "idle";
  let selectedView = "chat";
  let currentSession = null;

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
    if (state === "running" || state === "waiting") runStatus = state;
    else if (state === "complete" || state === "failed") runStatus = state;
    refreshView();
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
    if (role === "user" && progressTemplate && !progressCard) {
      progressCard = progressTemplate.content.firstElementChild.cloneNode(true);
      feed.append(progressCard);
      updateProgress("任务已提交", "等待服务端事件", "", "");
    }
    scrollTo(article);
    return article;
  }

  function updateProgress(phase, tool, observation, next) {
    if (!progressCard) return;
    for (const [hook, value] of Object.entries({
      "[data-agent-phase]": phase,
      "[data-agent-tool]": tool,
      "[data-agent-observation]": observation,
      "[data-agent-next]": next,
    })) {
      const node = $(hook, progressCard);
      if (node) node.textContent = value;
    }
  }

  function updateModeNote() {
    modeNote.textContent =
      mode === "approval"
        ? "读取自动执行，写入前等待审批。"
        : "当前 Web 工具将直接执行。";
  }

  function selectMode(nextMode) {
    if (runStatus === "running" || runStatus === "waiting") return;
    mode = nextMode;
    for (const option of modeButtons) {
      const selected = option.dataset.mode === mode;
      option.classList.toggle("is-selected", selected);
      option.setAttribute("aria-pressed", String(selected));
    }
    updateModeNote();
    refreshView();
  }

  function refreshView() {
    if (!workspaceView || !workspaceContent) return;
    const active = runStatus === "running" || runStatus === "waiting";
    if (newSessionButton) newSessionButton.disabled = active;
    modeButtons.forEach((button) => {
      button.disabled = active;
    });
    if (workspaceViewTitle)
      workspaceViewTitle.textContent =
        {
          sessions: "会话记录",
          tools: "工具",
          audit: "审计日志",
          rollback: "回滚",
          subagents: "子 Agent",
          settings: "设置",
        }[selectedView] || "";
    if (conversation) conversation.hidden = selectedView !== "chat";
    workspaceView.hidden = selectedView === "chat";
    workspaceContent.replaceChildren();
    const line = (text) => {
      const p = document.createElement("p");
      p.textContent = text;
      workspaceContent.append(p);
    };
    if (selectedView === "sessions") {
      if (!sessions.length) line("还没有本页提交的运行记录。");
      sessions.forEach((s) => {
        const p = document.createElement("p");
        p.textContent = `${s.prompt} · ${s.status}${s.result ? ` · ${s.result}` : ""}`;
        workspaceContent.append(p);
      });
    } else if (selectedView === "tools") {
      line("网页可调用读取文件、列出文件和写入文件。");
      line(
        mode === "approval"
          ? "当前为需要审批，写入前需确认。"
          : "当前为全权限，网页可用工具自动执行。",
      );
    } else if (selectedView === "audit") {
      line(
        auditEvents.length
          ? "本次运行已收到的审批与工具事件："
          : "本次运行尚未收到审批或工具事件。",
      );
      auditEvents.forEach((entry) => line(entry));
      line("记录范围为当前页面的本轮运行。");
    } else if (selectedView === "rollback")
      line("当前 Web 运行不提供回滚操作。");
    else if (selectedView === "subagents")
      line("当前 Web 运行不提供子代理信息。");
    else if (selectedView === "settings") {
      line(
        "模型提供方：" +
          (config.provider === "demo" ? "演示" : config.provider || "未知"),
      );
      const settingsModes = document.createElement("div");
      settingsModes.className = "mode-picker settings-modes";
      settingsModes.setAttribute("role", "group");
      settingsModes.setAttribute("aria-label", "工具调用权限");
      for (const [value, label] of [
        ["approval", "需要审批"],
        ["full", "全权限"],
      ]) {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.mode = value;
        button.textContent = label;
        button.className = "mode-option";
        button.classList.toggle("is-selected", mode === value);
        button.disabled = active;
        button.setAttribute("aria-pressed", String(mode === value));
        settingsModes.append(button);
      }
      workspaceContent.append(settingsModes);
    }
  }

  function setView(name) {
    selectedView = name;
    viewButtons.forEach((button) => {
      const selected = button.dataset.workspaceView === name;
      if (selected) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    refreshView();
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
    const session = currentSession;
    if (session) {
      session.status = state === "complete" ? "已完成" : "失败";
      session.result = state === "complete" ? "收到最终答复" : label;
      refreshView();
    }
    runButton.disabled = false;
    modeButtons.forEach((button) => {
      button.disabled = false;
    });
    runButton.querySelector("span:first-child").textContent = "再次发送";
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
      case "user_message": {
        const message = safeString(payload.content ?? payload.message ?? "");
        if (sessionTitle) sessionTitle.textContent = message || "新会话";
        if (currentSession) currentSession.prompt = message;
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
        updateProgress(
          "任务已送达",
          "等待模型处理",
          "服务端已确认收到任务",
          "等待模型请求事件",
        );
        break;
      }
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
        updateProgress(
          `第 ${payload.iteration ?? "?"} 轮：模型处理中`,
          "模型请求",
          "已收到模型请求事件",
          "等待模型响应",
        );
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
        updateProgress(
          "已收到模型响应",
          names.length ? names.join("、") : "无工具调用",
          safeString(desc),
          names.length ? "等待工具事件或下一轮模型请求" : "等待任务完成事件",
        );
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
        makeApproval(payload, stamp);
        auditEvents.push(`审批请求：${safeString(payload.tool_name, "工具")}`);
        refreshView();
        setState("等待审批", "waiting");
        updateProgress(
          "等待审批",
          safeString(payload.tool_name, "工具"),
          "服务端发出了审批请求",
          "批准调用或拒绝",
        );
        if (selectedView !== "chat") setView("chat");
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
        auditEvents.push(
          `审批决定：${decision} · ${safeString(payload.tool_name || payload.call_id, "调用")}`,
        );
        refreshView();
        const record = approvalsById.get(safeString(payload.approval_id));
        if (record && record.actions.isConnected) {
          record.actions.remove();
          const status = document.createElement("p");
          status.className = "decision-label";
          status.textContent = decision;
          record.card.append(status);
        }
        setState(
          timedOut
            ? "审批已超时，等待任务结束"
            : denied
              ? "审批已拒绝，等待任务结束"
              : "正在恢复执行",
          "running",
        );
        updateProgress(
          `审批${decision}`,
          safeString(payload.tool_name || payload.call_id, "工具"),
          `收到服务端审批决定：${decision}`,
          denied || timedOut ? "运行已停止" : "等待后续运行事件",
        );
        break;
      }
      case "tool_start":
        updateToolCard(safeString(payload.call_id), payload, type, stamp);
        auditEvents.push(
          `工具开始：${safeString(payload.name || payload.tool_name, "工具")}`,
        );
        refreshView();
        updateProgress(
          "工具执行中",
          safeString(payload.name || payload.tool_name, "工具"),
          "已收到工具开始事件",
          "等待工具结果事件",
        );
        setState("工具执行中", "running");
        break;
      case "tool_end":
        updateToolCard(safeString(payload.call_id), payload, type, stamp);
        auditEvents.push(
          `工具结果：${safeString(payload.name || payload.tool_name, "工具")} · ${safeString(payload.status, "状态未知")}`,
        );
        refreshView();
        updateProgress(
          "工具已返回",
          safeString(payload.name || payload.tool_name, "工具"),
          safeString(payload.status, "工具结果已收到"),
          "等待下一轮模型请求或任务完成",
        );
        break;
      case "tool_skipped":
        updateToolCard(safeString(payload.call_id), payload, type, stamp);
        auditEvents.push(
          `工具未执行：${safeString(payload.name || payload.tool_name, "工具")} · ${safeString(payload.reason, "")}`,
        );
        refreshView();
        updateProgress(
          "工具未执行",
          safeString(payload.name || payload.tool_name, "工具"),
          safeString(payload.reason, "收到工具跳过事件"),
          "等待后续事件",
        );
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
        if (currentSession)
          currentSession.result = safeString(payload.content, "任务完成");
        updateProgress("任务已完成", "", "收到任务完成事件", "可开始新会话");
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
        updateProgress(
          "任务失败",
          "",
          safeString(reason),
          "可检查事件时间线后重新运行",
        );
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
        if (currentSession && currentSession.status === "运行中") {
          currentSession.status = "事件流关闭";
          currentSession.result = "未收到终态事件";
        }
        setState("事件流已关闭", "failed");
        setStream("事件流已关闭", "error");
        runButton.disabled = false;
        runButton.querySelector("span:first-child").textContent = "再次发送";
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
      }
      refreshView();
    } catch {
      providerLabel.textContent = "服务配置暂不可用";
      setStream("无法读取服务配置", "error");
    }
  }

  modeButtons.forEach((button) =>
    button.addEventListener("click", () => selectMode(button.dataset.mode)),
  );

  demoExample.addEventListener("click", () => {
    promptInput.value = config.demo_prompt || "";
    promptInput.focus();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (runButton.disabled) return;
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
    runStatus = "running";
    if (sessionTitle) sessionTitle.textContent = prompt;
    modeButtons.forEach((button) => {
      button.disabled = true;
    });
    activeRun = null;
    seenSequences = new Set();
    toolsByCall.clear();
    approvalsById.clear();
    auditEvents.length = 0;
    progressCard = null;
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
      currentSession = { prompt, status: "运行中", result: "" };
      sessions.unshift(currentSession);
      refreshView();
      runButton.disabled = true;
      runButton.querySelector("span:first-child").textContent = "运行中";
      connectEvents(activeRun);
    } catch (error) {
      runButton.disabled = false;
      runButton.querySelector("span:first-child").textContent = "重试运行";
      setState("启动失败", "failed");
      setStream("无法创建事件流", "error");
      showError(error.message);
      runStatus = "failed";
      sessions.unshift({ prompt, status: "启动失败", result: error.message });
      currentSession = sessions[0];
      refreshView();
    }
  });

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-workspace-view]");
    if (button && !button.disabled) setView(button.dataset.workspaceView);
    const settingMode = event.target.closest(
      "#workspace-view-content [data-mode]",
    );
    if (settingMode && !settingMode.disabled) {
      selectMode(settingMode.dataset.mode);
    }
  });
  if (newSessionButton)
    newSessionButton.addEventListener("click", () => {
      if (runStatus === "running" || runStatus === "waiting") return;
      if (source) {
        source.close();
        source = null;
      }
      feed.replaceChildren(emptyConversation.cloneNode(true));
      timeline.replaceChildren();
      timelineEmpty.hidden = false;
      eventCount.textContent = "0";
      eventTotal = 0;
      seenSequences.clear();
      toolsByCall.clear();
      approvalsById.clear();
      requestsByIteration.clear();
      auditEvents.length = 0;
      activeRun = null;
      currentSession = null;
      progressCard = null;
      promptInput.value = config.provider === "demo" ? config.demo_prompt : "";
      hideError();
      if (sessionTitle) sessionTitle.textContent = "新会话";
      runStatus = "idle";
      modeButtons.forEach((button) => {
        button.disabled = false;
      });
      setState("等待任务", "");
      setStream("就绪", "");
      runButton.querySelector("span:first-child").textContent = "发送";
      setView("chat");
    });

  setView("chat");
  loadConfig();
})();
