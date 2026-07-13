"use strict";

const state = {
  runs: [],
  selectedId: null,
  run: null,
  events: [],
  eventSequences: new Set(),
  source: null,
  runFilter: "all",
  runSearch: "",
  eventSearch: "",
  view: "timeline",
  selectionGeneration: 0,
};

const byId = (id) => document.getElementById(id);

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function clear(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
}

function json(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch (_) {
    return String(value);
  }
}

function compactJson(value) {
  try {
    return JSON.stringify(value);
  } catch (_) {
    return String(value);
  }
}

function truncate(value, length = 64) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > length ? `${text.slice(0, length - 1)}…` : text;
}

function shortId(value) {
  const text = String(value || "");
  return text.length > 17 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text;
}

function statusLabel(status) {
  return {
    running: "live",
    succeeded: "passed",
    failed: "failed",
    incomplete: "incomplete",
    cancelled: "cancelled",
  }[status] || status || "unknown";
}

function formatClock(timestamp) {
  if (!timestamp) return "—";
  return new Date(timestamp * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatDate(timestamp) {
  if (!timestamp) return "—";
  return new Date(timestamp * 1000).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatDuration(milliseconds) {
  if (milliseconds === null || milliseconds === undefined || Number.isNaN(milliseconds)) return "—";
  if (milliseconds < 1000) return `${Math.round(milliseconds)}ms`;
  const seconds = milliseconds / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  const remaining = Math.round(seconds % 60);
  return `${minutes}m ${remaining}s`;
}

function formatCount(value) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat(undefined, { notation: value > 9999 ? "compact" : "standard" }).format(value);
}

function elapsedMilliseconds(run) {
  if (!run) return null;
  const end = run.completed_at || Date.now() / 1000;
  return Math.max(0, (end - run.started_at) * 1000);
}

function runTitle(run) {
  const metadata = run.metadata || {};
  if (metadata.task_name) return String(metadata.task_name);
  if (metadata.episode_id && metadata.role) return `${metadata.role} · ${metadata.episode_id}`;
  if (metadata.role) return `${metadata.role} episode`;
  if (run.prompt) return truncate(run.prompt, 72);
  return `${run.backend} agent run`;
}

function runSearchText(run) {
  return [
    run.run_id,
    run.backend,
    run.status,
    run.prompt,
    compactJson(run.metadata || {}),
  ].join(" ").toLowerCase();
}

function setConnection(mode, text) {
  const badge = byId("connection-badge");
  badge.classList.remove("connected", "disconnected");
  if (mode) badge.classList.add(mode);
  byId("connection-text").textContent = text;
}

async function api(path) {
  const response = await fetch(path, {
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed: ${response.status}`);
  return payload;
}

function showToast(message) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.classList.add("visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 1700);
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text || "");
    showToast(`${label} copied`);
  } catch (_) {
    showToast("Clipboard unavailable");
  }
}

async function loadRuns() {
  try {
    const payload = await api("/api/runs?limit=500");
    state.runs = payload.runs || [];
    renderRunList();
    if (!state.selectedId && state.runs.length) {
      await selectRun(state.runs[0].run_id);
    }
    if (state.selectedId) {
      const refreshed = state.runs.find((run) => run.run_id === state.selectedId);
      if (refreshed && state.run) {
        state.run = { ...state.run, ...refreshed };
        renderHeader();
        renderRunList();
      }
    }
    if (!state.source || state.source.readyState !== EventSource.OPEN) {
      setConnection("connected", "Database online");
    }
  } catch (error) {
    setConnection("disconnected", "Inspector offline");
    console.error(error);
  }
}

function filteredRuns() {
  const query = state.runSearch.trim().toLowerCase();
  return state.runs.filter((run) => {
    const statusMatches = state.runFilter === "all" || run.status === state.runFilter;
    return statusMatches && (!query || runSearchText(run).includes(query));
  });
}

function renderRunList() {
  const container = byId("run-list");
  clear(container);
  const runs = filteredRuns();
  byId("run-count").textContent = String(runs.length);

  if (!runs.length) {
    const empty = node("div", "timeline-empty", "No matching runs");
    container.appendChild(empty);
    return;
  }

  for (const run of runs) {
    const button = node("button", "run-item");
    button.type = "button";
    button.dataset.runId = run.run_id;
    button.classList.toggle("active", run.run_id === state.selectedId);
    button.setAttribute("aria-label", `Inspect ${runTitle(run)}`);

    const dot = node("span", `run-status-dot ${run.status}`);
    const main = node("span", "run-item-main");
    main.appendChild(node("span", "run-item-title", runTitle(run)));
    const meta = node("span", "run-item-meta");
    const role = run.metadata?.role || run.backend;
    meta.appendChild(node("span", "run-item-role", `${run.backend} · ${role}`));
    meta.appendChild(node("span", "", formatDate(run.started_at)));
    main.appendChild(meta);
    button.append(dot, main);
    button.addEventListener("click", () => selectRun(run.run_id));
    container.appendChild(button);
  }
}

async function selectRun(runId) {
  if (!runId) return;
  state.selectionGeneration += 1;
  const generation = state.selectionGeneration;
  state.selectedId = runId;
  closeStream();
  renderRunList();
  setConnection("", "Loading snapshot");
  try {
    const [detail, history] = await Promise.all([
      api(`/api/runs/${encodeURIComponent(runId)}`),
      api(`/api/runs/${encodeURIComponent(runId)}/events?limit=10000`),
    ]);
    if (generation !== state.selectionGeneration) return;
    state.run = detail.run;
    state.events = history.events || [];
    state.eventSequences = new Set(state.events.map((event) => event.sequence));
    renderInspector();
    connectStream(history.next_sequence || 0);
  } catch (error) {
    if (generation !== state.selectionGeneration) return;
    console.error(error);
    showToast(error.message);
    setConnection("disconnected", "Snapshot failed");
  }
}

function closeStream() {
  if (state.source) state.source.close();
  state.source = null;
}

function connectStream(afterSequence) {
  closeStream();
  if (!state.selectedId || !state.run) return;
  const url = `/api/runs/${encodeURIComponent(state.selectedId)}/stream?after_sequence=${afterSequence}`;
  const source = new EventSource(url);
  state.source = source;

  source.onopen = () => setConnection("connected", state.run?.status === "running" ? "Live stream" : "Snapshot complete");
  source.addEventListener("agent_event", (message) => {
    const payload = JSON.parse(message.data);
    const event = payload.event;
    if (!event || event.run_id !== state.selectedId || state.eventSequences.has(event.sequence)) return;
    state.eventSequences.add(event.sequence);
    state.events.push(event);
    state.events.sort((left, right) => left.sequence - right.sequence);
    renderInspector({ preserveScroll: true });
  });
  source.addEventListener("run_state", (message) => {
    const payload = JSON.parse(message.data);
    if (!payload.run || payload.run.run_id !== state.selectedId) return;
    state.run = payload.run;
    const index = state.runs.findIndex((run) => run.run_id === state.selectedId);
    if (index >= 0) state.runs[index] = payload.run;
    renderHeader();
    renderMetrics();
    renderContext();
    renderRunList();
  });
  source.addEventListener("stream_end", (message) => {
    const payload = JSON.parse(message.data);
    source.close();
    if (state.source === source) state.source = null;
    setConnection("connected", payload.status ? `${statusLabel(payload.status)} snapshot` : "Snapshot complete");
  });
  source.onerror = () => {
    if (state.source === source && state.run?.status === "running") {
      setConnection("disconnected", "Reconnecting");
    }
  };
}

function renderInspector(options = {}) {
  if (!state.run) return;
  byId("empty-state").classList.add("hidden");
  byId("inspector").classList.remove("hidden");
  renderHeader();
  renderMetrics();
  renderActivity(options);
  renderContext();
}

function renderHeader() {
  if (!state.run) return;
  const run = state.run;
  byId("backend-badge").textContent = run.backend || "unknown";
  const status = byId("run-status");
  status.textContent = statusLabel(run.status);
  status.className = `status-badge ${run.status || ""}`;
  byId("run-title").textContent = runTitle(run);
  byId("run-id").textContent = run.run_id;
  updateElapsed();
}

function updateElapsed() {
  if (!state.run) return;
  const age = formatDuration(elapsedMilliseconds(state.run));
  byId("live-age").textContent = state.run.status === "running" ? `${age} elapsed` : `${age} total`;
  byId("metric-duration").textContent = age;
}

function terminalEvent() {
  return [...state.events].reverse().find((event) => event.event_type === "turn_completed") || null;
}

function actionCount() {
  const calls = new Set();
  for (const event of state.events) {
    if (!["command_started", "command_run", "tool_call"].includes(event.event_type)) continue;
    calls.add(event.payload.call_id || `${event.event_type}:${event.sequence}`);
  }
  return calls.size;
}

function renderMetrics() {
  byId("metric-events").textContent = formatCount(state.events.length);
  byId("metric-actions").textContent = formatCount(actionCount());
  updateElapsed();

  const terminal = terminalEvent()?.payload || {};
  const usage = terminal.usage || {};
  const input = Number(usage.input_tokens || 0);
  const cached = Number(usage.cached_input_tokens || 0);
  const output = Number(usage.output_tokens || 0);
  const reasoning = Number(usage.reasoning_output_tokens || 0);
  const tokens = input + output + reasoning;
  byId("metric-tokens").textContent = tokens ? formatCount(tokens) : "—";
  byId("metric-token-detail").textContent = tokens ? `${formatCount(cached)} cached` : "provider total";
  byId("metric-cost").textContent = terminal.cost_usd !== null && terminal.cost_usd !== undefined
    ? `$${Number(terminal.cost_usd).toFixed(4)}`
    : "—";
}

function ensureAction(actions, items, key, event, kind) {
  let action = actions.get(key);
  if (!action) {
    action = {
      itemKind: "action",
      kind,
      key,
      firstSequence: event.sequence,
      lastSequence: event.sequence,
      recordedAt: event.recorded_at,
      title: kind === "command" ? "Pending command" : "Pending tool",
      status: "running",
      input: null,
      output: "",
      progress: [],
      callId: event.payload.call_id || null,
      processId: null,
      exitCode: null,
      durationMs: null,
      isError: false,
    };
    actions.set(key, action);
    items.push(action);
  }
  action.lastSequence = Math.max(action.lastSequence, event.sequence);
  return action;
}

function timelineModel() {
  const items = [];
  const actions = new Map();
  let textDeltaItem = null;

  for (const event of state.events) {
    const payload = event.payload || {};
    const callId = payload.call_id;
    let key;
    let action;
    switch (event.event_type) {
      case "command_started":
        key = `command:${callId || event.sequence}`;
        action = ensureAction(actions, items, key, event, "command");
        action.title = payload.command || "Shell command";
        action.callId = callId;
        action.processId = payload.process_id;
        action.cwd = payload.cwd;
        break;
      case "command_output_delta":
        key = `command:${callId || "unknown"}`;
        action = ensureAction(actions, items, key, event, "command");
        action.output += payload.delta || "";
        break;
      case "command_run":
        key = `command:${callId || event.sequence}`;
        action = ensureAction(actions, items, key, event, "command");
        action.title = payload.command || action.title;
        action.output = payload.output || action.output;
        action.callId = callId;
        action.processId = payload.process_id;
        action.exitCode = payload.exit_code;
        action.durationMs = payload.duration_ms;
        action.status = payload.exit_code === 0 ? "done" : "error";
        action.isError = payload.exit_code !== null && payload.exit_code !== 0;
        break;
      case "tool_call":
        key = `tool:${callId || event.sequence}`;
        action = ensureAction(actions, items, key, event, "tool");
        action.title = payload.name || "Tool call";
        action.input = payload.input;
        action.callId = callId;
        break;
      case "tool_progress":
        key = `tool:${callId || "unknown"}`;
        action = ensureAction(actions, items, key, event, "tool");
        if (payload.message) action.progress.push(payload.message);
        break;
      case "tool_result":
        key = `tool:${callId || "unknown"}`;
        action = ensureAction(actions, items, key, event, "tool");
        action.output = payload.output || "";
        action.status = payload.is_error ? "error" : "done";
        action.isError = Boolean(payload.is_error);
        break;
      case "text_delta":
        if (!textDeltaItem) {
          textDeltaItem = { itemKind: "event", kind: "assistant", eventType: "assistant_stream", firstSequence: event.sequence, lastSequence: event.sequence, recordedAt: event.recorded_at, title: "Assistant streaming", body: "" };
          items.push(textDeltaItem);
        }
        textDeltaItem.body += payload.text || "";
        textDeltaItem.lastSequence = event.sequence;
        break;
      default:
        textDeltaItem = null;
        items.push(eventItem(event));
    }
  }
  return items.sort((left, right) => left.firstSequence - right.firstSequence);
}

function eventItem(event) {
  const payload = event.payload || {};
  const base = {
    itemKind: "event",
    eventType: event.event_type,
    firstSequence: event.sequence,
    lastSequence: event.sequence,
    recordedAt: event.recorded_at,
    kind: "system",
    title: event.event_type.replaceAll("_", " "),
    body: "",
    payload,
  };
  switch (event.event_type) {
    case "assistant_text":
      return { ...base, kind: "assistant", title: "Assistant message", body: payload.text || "" };
    case "file_changed":
      return { ...base, kind: "file", title: `${payload.kind || "changed"} · ${payload.path || "file"}`, body: "" };
    case "session_started":
      return { ...base, title: "Provider session started", body: payload.session_id || "" };
    case "turn_completed":
      return { ...base, kind: payload.success ? "assistant" : "error", title: payload.success ? "Turn completed" : "Turn failed", body: payload.error || payload.final_text || "" };
    case "reasoning":
      return { ...base, title: "Reasoning", body: payload.content_omitted ? "Content omitted by monitor policy" : payload.text || "" };
    case "raw_event":
      return { ...base, title: `Provider event · ${payload.kind || "raw"}`, body: payload.data_omitted ? "Native payload omitted by monitor policy" : json(payload.data) };
    default:
      return { ...base, body: Object.keys(payload).length ? json(payload) : "" };
  }
}

function matchesEventFilter(item) {
  const query = state.eventSearch.trim().toLowerCase();
  if (!query) return true;
  return [item.kind, item.title, item.body, item.output, compactJson(item.input), compactJson(item.payload)]
    .join(" ")
    .toLowerCase()
    .includes(query);
}

function renderActivity(options = {}) {
  const timeline = byId("timeline");
  const raw = byId("raw-events");
  const previousDistance = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight;
  timeline.classList.toggle("hidden", state.view !== "timeline");
  raw.classList.toggle("hidden", state.view !== "raw");

  if (state.view === "raw") {
    renderRawEvents();
    return;
  }

  clear(timeline);
  const items = timelineModel().filter(matchesEventFilter);
  if (!items.length) {
    timeline.appendChild(node("div", "timeline-empty", state.events.length ? "No matching activity" : "Waiting for the first event…"));
    return;
  }
  for (const item of items) timeline.appendChild(renderTimelineItem(item));
  if (options.preserveScroll && previousDistance < 90) timeline.scrollTop = timeline.scrollHeight;
}

function renderTimelineItem(item) {
  const wrapper = node("article", "timeline-item");
  wrapper.appendChild(node("span", `timeline-node ${item.kind}${item.isError ? " error" : ""}`));
  const card = node("div", "event-card");
  if (item.itemKind === "action" && item.status === "running") card.classList.add("action-live");
  if (item.isError) card.classList.add("action-error");

  const header = node("div", "event-header");
  const heading = node("div", "event-heading");
  const type = node("span", "event-type", item.itemKind === "action" ? `${item.kind} action` : item.eventType);
  if (item.itemKind === "action") type.appendChild(node("span", `event-status ${item.status}`, statusLabel(item.status)));
  heading.append(type, node("div", "event-title", item.title));
  header.append(heading, node("time", "event-time", formatClock(item.recordedAt)));
  card.appendChild(header);

  if (item.itemKind === "action") renderActionBody(card, item);
  else if (item.body) {
    const body = node("div", "event-body");
    body.appendChild(node("pre", "output-block", item.body));
    card.appendChild(body);
  }

  const footer = node("div", "event-footer");
  footer.appendChild(node("span", "", item.firstSequence === item.lastSequence ? `seq ${item.firstSequence}` : `seq ${item.firstSequence}–${item.lastSequence}`));
  if (item.callId) footer.appendChild(node("span", "", `call ${shortId(item.callId)}`));
  if (item.processId) footer.appendChild(node("span", "", `process ${shortId(item.processId)}`));
  if (item.exitCode !== null && item.exitCode !== undefined) footer.appendChild(node("span", "", `exit ${item.exitCode}`));
  if (item.durationMs !== null && item.durationMs !== undefined) footer.appendChild(node("span", "", formatDuration(item.durationMs)));
  card.appendChild(footer);
  wrapper.appendChild(card);
  return wrapper;
}

function renderActionBody(card, item) {
  const hasInput = item.input !== null && item.input !== undefined;
  const progress = item.progress?.length ? item.progress.join("\n") : "";
  const output = item.output || progress;
  if (!hasInput && !output) return;
  const body = node("div", "event-body");
  if (hasInput) {
    body.appendChild(node("div", "block-label", "Input"));
    body.appendChild(node("pre", "payload-block", json(item.input)));
  }
  if (output) {
    body.appendChild(node("div", "block-label", item.status === "running" ? "Live output" : "Output"));
    body.appendChild(node("pre", "output-block", output));
  }
  card.appendChild(body);
}

function renderRawEvents() {
  const container = byId("raw-events");
  clear(container);
  const query = state.eventSearch.trim().toLowerCase();
  const events = state.events.filter((event) => !query || compactJson(event).toLowerCase().includes(query));
  if (!events.length) {
    container.appendChild(node("div", "timeline-empty", state.events.length ? "No matching events" : "No events recorded"));
    return;
  }
  for (const event of events) {
    const row = node("div", "raw-event");
    row.append(
      node("span", "raw-sequence", `#${event.sequence}`),
      node("span", "raw-type", event.event_type),
      node("span", "raw-payload", json(event.payload)),
    );
    container.appendChild(row);
  }
}

function appendDetail(list, label, value, className = "") {
  list.appendChild(node("dt", "", label));
  const detail = node("dd", className, value === null || value === undefined || value === "" ? "—" : value);
  detail.title = String(value ?? "");
  list.appendChild(detail);
}

function renderContext() {
  if (!state.run) return;
  const run = state.run;
  byId("run-prompt").textContent = run.prompt || "Prompt capture disabled";

  const execution = byId("execution-context");
  clear(execution);
  appendDetail(execution, "Backend", run.backend);
  appendDetail(execution, "Workspace", run.workspace);
  appendDetail(execution, "Started", formatDate(run.started_at));
  appendDetail(execution, "Last event", formatDate(run.last_event_at));
  appendDetail(execution, "Owner", `${run.owner_host}:${run.owner_pid}`);
  appendDetail(execution, "Owner alive", run.owner_alive === null ? "remote / n.a." : run.owner_alive ? "yes" : "no", run.owner_alive === false ? "bad" : "good");
  appendDetail(execution, "Sequence", run.last_sequence);

  const metadata = byId("run-metadata");
  clear(metadata);
  const entries = Object.entries(run.metadata || {});
  if (!entries.length) appendDetail(metadata, "Metadata", "none");
  for (const [key, value] of entries) appendDetail(metadata, key.replaceAll("_", " "), typeof value === "object" ? compactJson(value) : value);

  const terminal = terminalEvent()?.payload;
  const section = byId("terminal-section");
  section.classList.toggle("hidden", !terminal);
  if (terminal) byId("terminal-result").textContent = terminal.error || terminal.final_text || "No terminal text";
}

function bindControls() {
  byId("run-search").addEventListener("input", (event) => {
    state.runSearch = event.target.value;
    renderRunList();
  });
  byId("event-search").addEventListener("input", (event) => {
    state.eventSearch = event.target.value;
    renderActivity();
  });
  document.querySelectorAll(".filter-chip").forEach((button) => {
    button.addEventListener("click", () => {
      state.runFilter = button.dataset.status;
      document.querySelectorAll(".filter-chip").forEach((item) => item.classList.toggle("active", item === button));
      renderRunList();
    });
  });
  document.querySelectorAll(".view-tab").forEach((button) => {
    button.addEventListener("click", () => {
      state.view = button.dataset.view;
      document.querySelectorAll(".view-tab").forEach((item) => item.classList.toggle("active", item === button));
      renderActivity();
    });
  });
  byId("refresh-button").addEventListener("click", () => state.selectedId && selectRun(state.selectedId));
  byId("copy-run-id").addEventListener("click", () => copyText(state.run?.run_id, "Run ID"));
  byId("copy-prompt").addEventListener("click", () => copyText(state.run?.prompt, "Instruction"));
}

async function bootstrap() {
  bindControls();
  try {
    await api("/api/health");
    setConnection("connected", "Database online");
  } catch (error) {
    setConnection("disconnected", "Inspector offline");
    console.error(error);
  }
  await loadRuns();
  window.setInterval(loadRuns, 3000);
  window.setInterval(updateElapsed, 1000);
}

window.addEventListener("beforeunload", closeStream);
bootstrap();
