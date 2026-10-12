// Runner-owned stdio boundary. Importing this module has no SDK/network effects.
import { createHash, randomUUID } from "node:crypto";
import {
  appendFileSync, closeSync, existsSync, fsyncSync, mkdirSync, openSync,
  readFileSync, renameSync, statSync, unlinkSync, writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { once } from "node:events";

export const SDK_VERSION = "1.0.13";
export const RUNTIME_VERSION = "1.0.83";
export const LEGACY_REQUEST_POLICY = "github_copilot.sdk_default.v1";
export const COST_REQUEST_POLICY = "github_copilot.cost_optimized.v1";
// Versioned measurement behavior. Keep safety, web-tool guidance and general
// runtime instructions; remove only irrelevant coding/environment sections.
const costSystemMessage = () => ({ mode: "customize", sections: {
  preamble: { action: "replace", content: "You are GitHub Copilot. Answer the user's request." },
  code_change_rules: { action: "remove" },
  environment_context: { action: "remove" },
  tone: { action: "replace", content: "Answer directly and concisely. Preserve the detail needed to answer the request, provider names, relevant qualifications and citations. Avoid repeating the question or adding a closing offer." },
} });

function costOptions(reasoningEffort) {
  return { capi: { autoTier: "efficiency" }, contextTier: "default",
    systemMessage: costSystemMessage(),
    ...(reasoningEffort === "low" ? { reasoningEffort: "low" } : {}) };
}

function usageTotals(events) {
  if (!events.length) return null;
  const sum = (get) => {
    const values = events.map((event) => get(event.data));
    if (values.some((value) => !Number.isSafeInteger(value) || value < 0)) return null;
    const total = values.reduce((a, b) => a + b, 0);
    return Number.isSafeInteger(total) ? total : null;
  };
  const nano = sum((data) => data.copilotUsage?.totalNanoAiu);
  return { api_call_count: events.length,
    input_tokens: sum((data) => data.inputTokens), output_tokens: sum((data) => data.outputTokens),
    // Reasoning is already included in output; cache counters stay separate.
    reasoning_tokens: sum((data) => data.reasoningTokens),
    cache_read_tokens: sum((data) => data.cacheReadTokens), cache_write_tokens: sum((data) => data.cacheWriteTokens),
    total_nano_aiu: nano,
    ai_credits: nano === null ? null : `${Math.floor(nano / 1e9)}.${String(nano % 1e9).padStart(9, "0")}`,
  };
}
const MAX_LINE = 2 * 1024 * 1024;
const MAX_EVENTS = 20000;
const MAX_JOURNAL = 16 * 1024 * 1024;
const MAX_TEXT = 512 * 1024;
const WEB_TOOLS = ["web_search", "web_fetch"];
const WEB_TOOL_FILTER = ["mcp:github-mcp-server-web_search", "builtin:web_fetch"];
const sha = (text) => createHash("sha256").update(text, "utf8").digest("hex");
const identifier = (value) => {
  if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$/.test(value)) fail("copilot_identifier_invalid");
  return value;
};
export class WorkerError extends Error {
  constructor(code, submitted = false, detail = null) {
    super(code); this.code = code; this.submitted = submitted; this.detail = detail;
  }
}
function fail(code, submitted = false) { throw new WorkerError(code, submitted); }
function capabilityFailure(stage, reason, detail = {}) {
  throw new WorkerError("copilot_source_capability_unavailable", false, { stage, reason, ...detail });
}
function toolCatalog(metadata, stage) {
  if (!Array.isArray(metadata?.tools) || metadata.tools.some((tool) =>
    typeof tool?.name !== "string" || !/^[A-Za-z_][A-Za-z0-9_./:-]{0,159}$/.test(tool.name))) {
    capabilityFailure(stage, "metadata_malformed");
  }
  return metadata.tools.map((tool) => tool.name.replace(/^builtin:/, ""));
}
function diagnosticToolName(name) {
  // RPC metadata is untrusted. Preserve ordinary names, never opaque strings,
  // credential-shaped names or the SDK's descriptions and exception messages.
  return /^[a-z][a-z0-9_.-]{0,63}$/.test(name) && !/^(gh[pousr]_|github_pat_)/.test(name)
    ? name : "unrecognized_tool";
}
function webToolName(tool) {
  // Prove MCP origin as well as the model-facing name. An arbitrary custom
  // search tool must not satisfy the frozen GitHub web source contract.
  if (tool.mcpServerName === "github-mcp-server" && tool.mcpToolName === "web_search"
      && ["web_search", "github-mcp-server-web_search", "mcp:github-mcp-server-web_search"].includes(tool.name)
      && (!tool.namespacedName || tool.namespacedName === "mcp:github-mcp-server-web_search")) return "web_search";
  if (!tool.mcpServerName && !tool.mcpToolName && (!tool.namespacedName || tool.namespacedName === "builtin:web_fetch")
      && ["web_fetch", "builtin:web_fetch"].includes(tool.name)) return "web_fetch";
  return null;
}
function providerErrorCode(error) {
  const value = String(error?.message || error?.errorType || error || "");
  if (/quota|credit.*exhaust|insufficient.*credit|usage.*limit/i.test(value)) return "copilot_quota_exhausted";
  if (/rate.?limit|429|too many requests/i.test(value)) return "copilot_rate_limited";
  if (/entitlement|subscription.*required|copilot.*access.*denied/i.test(value)) return "copilot_entitlement_unavailable";
  if (/unauthori[sz]ed|authentication|token.*expired|token.*revoked|forbidden|401|403/i.test(value)) return "copilot_auth_required";
  return "copilot_terminal_error";
}
function atomicJson(path, value) {
  const temporary = `${path}.${process.pid}.tmp`;
  const fd = openSync(temporary, "w", 0o600);
  try { writeFileSync(fd, JSON.stringify(value)); fsyncSync(fd); } finally { closeSync(fd); }
  renameSync(temporary, path);
  const parent = openSync(dirname(path), "r");
  try { fsyncSync(parent); } finally { closeSync(parent); }
}
const loadJson = (path) => JSON.parse(readFileSync(path, "utf8"));
const utc = () => new Date().toISOString();

function safeEvent(event, token) {
  // Never store model reasoning, internal auth objects, or opaque SDK state.
  const keys = ["content", "messageId", "interactionId", "turnId", "model", "toolRequests",
    "toolCallId", "toolName", "arguments", "result", "success", "error", "aborted",
    "citations", "serverTools", "phase", "chunkCount", "chunkIndex", "apiCallId",
    "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "cost",
    "duration", "billingType", "quotaSnapshots", "limit", "resetDate", "errorType", "message",
    "autoTier", "reasoningEffort"];
  const data = {};
  for (const key of keys) if (event.data?.[key] !== undefined) data[key] = event.data[key];
  // Preserve only numeric billing counters, never opaque billing/auth objects
  // or private reasoning text. Missing counters remain unknown.
  if (event.type === "assistant.usage") {
    if (Number.isSafeInteger(event.data?.reasoningTokens) && event.data.reasoningTokens >= 0) data.reasoningTokens = event.data.reasoningTokens;
    const nano = event.data?.copilotUsage?.totalNanoAiu;
    if (Number.isSafeInteger(nano) && nano >= 0) data.copilotUsage = { totalNanoAiu: nano };
  }
  let encoded = JSON.stringify({ id: event.id, type: event.type, parentId: event.parentId,
    agentId: event.agentId, timestamp: event.timestamp, ephemeral: event.ephemeral, data });
  if (token) encoded = encoded.replaceAll(token, "[redacted]");
  encoded = encoded.replace(/\b(?:gh[ousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,})\b/g, "[redacted]");
  if (Buffer.byteLength(encoded) > MAX_LINE / 2) fail("copilot_event_too_large", null);
  return JSON.parse(encoded);
}

/** Pure conservative reducer, shared by live capture and recovery tests.
 * A terminal message + matching turn_end + subsequent non-aborted session idle
 * are required, with one exactly matching user message and no outstanding tools.
 * Partial streams, idle alone, subagents, earlier turns and errors cannot accept.
 */
export function reduceEvents(events, request) {
  const root = events.filter((event) => !event.agentId);
  const users = root.filter((event) => event.type === "user.message");
  const user = users.find((event) => request.provider_message_id
    ? event.data?.messageId === request.provider_message_id
    : event.data?.content === request.prompt);
  if (!user || users.length !== 1 || user.data.content !== request.prompt) {
    return { status: "ambiguous", code: "copilot_user_turn_unproven" };
  }
  const after = root.slice(root.indexOf(user) + 1);
  const pendingTools = new Set();
  let terminal = null, endedTurn = null, idleAfterTerminal = false, currentTurn = null;
  for (const event of after) {
    const data = event.data || {};
    if (["session.error", "session.abort"].includes(event.type)) return { status: "failed", code: providerErrorCode(data) };
    if (event.type === "tool.execution_start") pendingTools.add(data.toolCallId);
    if (event.type === "tool.execution_complete") pendingTools.delete(data.toolCallId);
    if (event.type === "assistant.turn_start") {
      if (user.data.interactionId && data.interactionId && user.data.interactionId !== data.interactionId) {
        return { status: "ambiguous", code: "copilot_turn_identity_mismatch" };
      }
      currentTurn = data.turnId;
      terminal = null;
      endedTurn = null;
      idleAfterTerminal = false;
    }
    if (event.type === "assistant.message") {
      if (user.data.interactionId && data.interactionId && user.data.interactionId !== data.interactionId) continue;
      const turnId = data.turnId || currentTurn;
      if (!turnId || (currentTurn && turnId !== currentTurn)) continue;
      if (data.toolRequests?.length || ["analysis", "thinking", "reasoning"].includes(data.phase)) {
        terminal = null;
        continue;
      }
      if (data.chunkCount > 1) {
        // Reconstruct only the final model call's prose chunks, never progress
        // from preceding tool calls. Without explicit call identity fail closed.
        if (!data.apiCallId) return { status: "ambiguous", code: "copilot_chunk_identity_missing" };
        const chunks = after.filter((item) => item.type === "assistant.message"
          && item.data?.apiCallId === data.apiCallId && !item.data?.toolRequests?.length
          && !["analysis", "thinking", "reasoning"].includes(item.data?.phase));
        if (data.chunkIndex !== data.chunkCount - 1) continue;
        if (chunks.length !== data.chunkCount || chunks.some((item, index) => item.data.chunkIndex !== index)) {
          return { status: "ambiguous", code: "copilot_chunks_incomplete" };
        }
        terminal = { ...data, content: chunks.map((item) => item.data.content).join(""), turnId };
      } else terminal = { ...data, turnId };
      idleAfterTerminal = false;
    }
    if (event.type === "assistant.turn_end") endedTurn = data.turnId;
    if (event.type === "session.idle") {
      if (data.aborted) return { status: "failed", code: "copilot_cancelled" };
      idleAfterTerminal = Boolean(terminal && endedTurn === terminal.turnId && pendingTools.size === 0);
    }
  }
  if (!terminal || !idleAfterTerminal || !terminal.content?.trim()) {
    return { status: "ambiguous", code: "copilot_terminal_answer_unproven" };
  }
  if (Buffer.byteLength(terminal.content) > MAX_TEXT) return { status: "failed", code: "copilot_source_too_large" };
  return { status: "completed", text: terminal.content, source_sha256: sha(terminal.content),
    provider_message_id: user.data.messageId || request.provider_message_id || null,
    assistant_message_id: terminal.messageId, turn_id: terminal.turnId,
    resolved_model: terminal.model || null, citations: terminal.citations || null,
    server_tools: terminal.serverTools || null, terminal_state: "turn_end_and_session_idle" };
}

async function loadOfficialSdk() {
  // SDK 1.0.13 requires its installed platform package; it does not download on
  // first use. Verify manifests before constructing any runtime process.
  let entry;
  try { entry = fileURLToPath(import.meta.resolve("@github/copilot-sdk")); }
  catch { fail("copilot_sdk_not_installed"); }
  const manifest = loadJson(join(dirname(entry), "..", "package.json"));
  if (manifest.version !== SDK_VERSION || manifest.copilotCliVersion !== RUNTIME_VERSION) fail("copilot_version_mismatch");
  return import("@github/copilot-sdk");
}

export class CopilotWorker {
  constructor({ loadSdk = loadOfficialSdk, fetchIdentity = null } = {}) {
    this.loadSdk = loadSdk;
    this.fetchIdentity = fetchIdentity || (async (token) => {
      const response = await fetch("https://api.github.com/user", {
        headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json",
          "User-Agent": "visibility-runner-copilot", "X-GitHub-Api-Version": "2022-11-28" },
        signal: AbortSignal.timeout(15000),
      });
      if (!response.ok) fail(response.status === 401 || response.status === 403 ? "copilot_auth_required" : "copilot_identity_unavailable");
      const value = await response.json();
      return { id: String(value.id), login: value.login };
    });
    this.sessions = new Map();
    this.client = null;
    this.initializing = false;
    this.activeSend = null;
    this.lockPath = null;
  }

  async initialize(params) {
    if (this.client || this.initializing) fail("copilot_already_initialized");
    this.initializing = true;
    if (!params.token || params.model !== "auto" || params.sdk_version !== SDK_VERSION || params.runtime_version !== RUNTIME_VERSION
      || !["github_copilot_web_v1", "github_copilot_model_only_v1"].includes(params.source_method)
      || !Number.isInteger(params.tool_call_limit) || params.tool_call_limit < 1 || params.tool_call_limit > 100
      || ![LEGACY_REQUEST_POLICY, COST_REQUEST_POLICY].includes(params.request_policy_version === undefined ? LEGACY_REQUEST_POLICY : params.request_policy_version)) fail("copilot_source_config_invalid");
    this.requestPolicyVersion = params.request_policy_version ?? LEGACY_REQUEST_POLICY;
    this.params = { ...params };
    this.token = params.token;
    delete this.params.token;
    this.directory = resolve(params.state_directory);
    mkdirSync(this.directory, { recursive: true, mode: 0o700 });
    this.lockPath = join(this.directory, "worker.lock");
    if (existsSync(this.lockPath)) {
      const previous = loadJson(this.lockPath);
      let alive = true;
      try { process.kill(previous.pid, 0); } catch (error) { if (error.code === "ESRCH") alive = false; }
      if (alive) { this.lockPath = null; fail("copilot_account_busy"); }
      unlinkSync(this.lockPath);
    }
    const lock = openSync(this.lockPath, "wx", 0o600);
    try { writeFileSync(lock, JSON.stringify({ pid: process.pid })); fsyncSync(lock); } finally { closeSync(lock); }
    this.configHash = sha(JSON.stringify({ account: params.github_account_id, ref: params.account_ref,
      method: params.source_method, model: params.model, sdk: SDK_VERSION, runtime: RUNTIME_VERSION,
      tools: this.toolNames(), limit: params.tool_call_limit,
      // Omitted/default legacy settings retain the exact historical hash.
      ...(this.requestPolicyVersion === COST_REQUEST_POLICY ? { request_policy: {
        version: COST_REQUEST_POLICY, ...costOptions(null), reasoning_policy: "low_if_auto_advertises_support",
      } } : {}) }));
    const identity = await this.fetchIdentity(this.token);
    if (!/^[1-9][0-9]*$/.test(identity?.id || "") || !/^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/.test(identity?.login || "")) fail("copilot_account_identity_unproven");
    if (String(params.github_account_id).toLowerCase() !== String(identity.login).toLowerCase()
      && String(params.github_account_id) !== identity.id) fail("copilot_account_identity_mismatch");
    this.identity = identity;
    const { CopilotClient } = await this.loadSdk();
    mkdirSync(join(this.directory, "workspace"), { recursive: true, mode: 0o700 });
    this.client = new CopilotClient({ mode: "empty", baseDirectory: join(this.directory, "runtime"),
      workingDirectory: join(this.directory, "workspace"), useLoggedInUser: false,
      gitHubToken: this.token, logLevel: "none", builtinPluginDirectories: [],
    });
    await this.client.start();
    const status = await this.client.getStatus();
    if (status.version !== RUNTIME_VERSION) fail("copilot_version_mismatch");
    const auth = await this.client.getAuthStatus();
    if (!auth.isAuthenticated || (auth.login && auth.login.toLowerCase() !== identity.login.toLowerCase())) fail("copilot_account_identity_mismatch");
    const quota = await this.checkQuota();
    if (this.toolNames().length) await this.checkRuntimeTools();
    this.reasoningSelection = "sdk_default";
    this.reasoningEffort = null;
    if (this.requestPolicyVersion === COST_REQUEST_POLICY) {
      try {
        const models = await this.client.listModels();
        const auto = models.find((model) => model.id === "auto");
        if (auto?.capabilities?.supports?.reasoningEffort === true && Array.isArray(auto.supportedReasoningEfforts) && auto.supportedReasoningEfforts.includes("low")) {
          this.reasoningEffort = "low";
          this.reasoningSelection = "auto_advertises_low";
        } else this.reasoningSelection = "auto_does_not_advertise_low";
      } catch { this.reasoningSelection = "model_metadata_unavailable"; }
    }
    if (this.toolNames().length) await this.checkWebCapability();
    this.preflight = { status: "ready", platform: "github_copilot", account_ref: params.account_ref,
      github_account_id: identity.id, github_login: identity.login, source_method: params.source_method,
      model: "auto", sdk_version: SDK_VERSION, runtime_version: RUNTIME_VERSION,
      tool_configuration_hash: this.configHash, allowed_tools: this.toolNames(), quota,
      protocol_version: status.protocolVersion,
      request_policy_version: this.requestPolicyVersion,
      request_policy: this.policyEvidence() };
    this.initializing = false;
    return this.preflight;
  }

  toolNames() { return this.params.source_method === "github_copilot_web_v1" ? WEB_TOOLS : []; }

  async checkRuntimeTools() {
    let metadata;
    try { metadata = await this.client.rpc.tools.list({ model: "auto" }); }
    catch { capabilityFailure("runtime_tool_catalog", "metadata_rpc_failed"); }
    const offered = toolCatalog(metadata, "runtime_tool_catalog");
    // This RPC lists built-ins only. Prove MCP search in a restricted session.
    const missing = ["web_fetch"].filter((tool) => !offered.includes(tool));
    if (missing.length) capabilityFailure("runtime_tool_catalog", "missing_required_tools", { missing_tools: missing });
  }

  async checkWebCapability() {
    const record = { owner: { request_policy: { reasoning_effort: this.reasoningEffort } }, toolCalls: 0 };
    try {
      const config = this.sessionConfig(`capability-${randomUUID()}`, record);
      config.onEvent = () => {};
      record.session = await this.client.createSession(config);
      if (record.session.sessionId !== config.sessionId) fail("copilot_session_identity_mismatch");
      await this.checkTools(record);
    } catch (error) {
      if (error instanceof WorkerError) throw error;
      capabilityFailure("session_tools", "metadata_rpc_failed");
    } finally {
      if (record.session) {
        try { await record.session.destroy(); }
        catch { capabilityFailure("session_tools", "metadata_rpc_failed"); }
      }
    }
  }

  policyEvidence() {
    return { version: this.requestPolicyVersion,
      requested_auto_tier: this.requestPolicyVersion === COST_REQUEST_POLICY ? "efficiency" : "sdk_default",
      reasoning_effort: this.reasoningEffort, reasoning_selection: this.reasoningSelection,
      system_message_sha256: this.requestPolicyVersion === COST_REQUEST_POLICY ? sha(JSON.stringify(costSystemMessage())) : null };
  }

  async checkQuota() {
    let quota;
    try { quota = await this.client.rpc.account.getQuota({ gitHubToken: this.token }); }
    catch { fail("copilot_quota_unavailable"); }
    const entries = Object.values(quota?.quotaSnapshots || {});
    if (!entries.length) fail("copilot_quota_unavailable");
    const applicable = Object.entries(quota.quotaSnapshots).filter(([key]) => /chat|premium|credit|token/i.test(key)).map(([, value]) => value);
    if (!applicable.length) fail("copilot_quota_unavailable");
    if (applicable.some((value) => !value.isUnlimitedEntitlement && value.remainingPercentage <= 0)) fail("copilot_quota_exhausted");
    return quota;
  }

  sessionConfig(sessionId, record) {
    return { sessionId, model: "auto", gitHubToken: this.token, streaming: true,
      ...(this.requestPolicyVersion === COST_REQUEST_POLICY
        ? costOptions(record.owner.request_policy.reasoning_effort) : {}),
      workingDirectory: join(this.directory, "workspace"),
      availableTools: this.toolNames().length ? WEB_TOOL_FILTER : [],
      ...(this.toolNames().length ? { githubMcpToolConfig: {
        enableAllTools: false, additionalTools: ["web_search"], additionalToolsets: [],
      } } : { disabledMcpServers: ["github-mcp-server"] }),
      tools: [], mcpServers: {}, customAgents: [], skillDirectories: [], includedBuiltinSkills: [],
      enableConfigDiscovery: false, enableOnDemandInstructionDiscovery: false, enableFileHooks: false,
      enableHostGitOperations: false, enableSessionStore: false, enableSkills: false,
      skipEmbeddingRetrieval: true, embeddingCacheStorage: "in-memory", memory: { enabled: false },
      remoteSession: "off", requestExtensions: false, requestCanvasRenderer: false,
      onEvent: (event) => this.capture(record, event),
      onPermissionRequest: (request) => {
        if (this.toolNames().length && request.kind === "url" && !request.managedApprovalRequired && !request.requestSandboxBypass) {
          try {
            const url = new URL(request.url);
            if (["https:", "http:"].includes(url.protocol) && !url.username && !url.password
              && !/^(localhost|127\.|0\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|\[)/i.test(url.hostname)
              && url.hostname.includes(".")) return { kind: "approve-once" };
          } catch { /* deny malformed URL */ }
        }
        return { kind: "reject", feedback: "This visibility source permits public web access only." };
      },
      onUserInputRequest: () => { fail("copilot_interactive_input_forbidden", null); },
      hooks: { onPreToolUse: (input) => {
        if (!record.authorizedToolNames?.has(input.toolName) || record.toolCalls >= this.params.tool_call_limit) {
          this.captureFailure(record, "copilot_tool_policy_violation");
          return { permissionDecision: "deny", permissionDecisionReason: "Frozen source tool policy" };
        }
        record.toolCalls += 1;
        return { permissionDecision: "allow" };
      } },
    };
  }

  record(sessionId) {
    identifier(sessionId);
    const directory = join(this.directory, "sessions", sessionId);
    const ownerPath = join(directory, "owner.json");
    if (!existsSync(ownerPath)) fail("copilot_session_not_owned");
    const owner = loadJson(ownerPath);
    if (owner.config_hash !== this.configHash) fail("copilot_session_identity_mismatch");
    if (this.requestPolicyVersion === COST_REQUEST_POLICY
      && (owner.request_policy?.version !== COST_REQUEST_POLICY
        || owner.request_policy?.requested_auto_tier !== "efficiency"
        || ![null, "low"].includes(owner.request_policy?.reasoning_effort)
        || owner.request_policy?.system_message_sha256 !== sha(JSON.stringify(costSystemMessage())))) fail("copilot_session_identity_mismatch");
    const record = { sessionId, directory, owner, events: [], ids: new Set(), toolCalls: 0,
      journal: join(directory, "events.jsonl"), requestPath: join(directory, "request.json"), session: null };
    if (existsSync(record.journal)) {
      if (statSync(record.journal).size > MAX_JOURNAL) fail("copilot_journal_too_large", null);
      for (const line of readFileSync(record.journal, "utf8").trim().split("\n").filter(Boolean)) {
        try {
          const event = JSON.parse(line);
          if (!record.ids.has(event.id)) { record.ids.add(event.id); record.events.push(event); }
        } catch { record.captureFailure = "copilot_journal_corrupt"; }
      }
    }
    record.toolCalls = record.events.filter((event) => event.type === "tool.execution_start").length;
    if (existsSync(join(directory, "capture-failure.json"))) record.captureFailure = loadJson(join(directory, "capture-failure.json")).code;
    return record;
  }

  captureFailure(record, code) {
    record.captureFailure = code;
    try { atomicJson(join(record.directory, "capture-failure.json"), { code, timestamp: utc() }); }
    catch { record.captureFailure = "copilot_evidence_write_failed"; }
  }

  capture(record, event) {
    if (!event?.id || !event?.type || record.ids.has(event.id)) return;
    // A stream-start notification is not an emitted token. Measure the first
    // nonempty root assistant text delta before discarding its ephemeral body.
    // Missing streaming deltas remain unknown, including after process restart.
    if (event.type === "assistant.message_delta") {
      if (!event.agentId && !event.data?.parentToolCallId
        && typeof event.data?.deltaContent === "string" && event.data.deltaContent.length > 0
        && record.startedAt !== undefined && record.firstTokenAt === undefined) {
        record.firstTokenAt = performance.now();
      }
      return;
    }
    // Deltas are ephemeral previews, never the source; preserving terminal
    // messages and usage avoids writing thousands of redundant stream fragments.
    if (["assistant.reasoning_delta", "assistant.reasoning", "assistant.intent"].includes(event.type)) return;
    try {
      if (event.type === "assistant.message" && event.data?.content?.includes(this.token)) fail("copilot_secret_in_response", null);
      const saved = safeEvent(event, this.token);
      if (event.type === "assistant.message" && saved.data.content !== event.data.content) fail("copilot_secret_in_response", null);
      const encoded = `${JSON.stringify(saved)}\n`;
      if (record.events.length >= MAX_EVENTS || (existsSync(record.journal) && statSync(record.journal).size + Buffer.byteLength(encoded) > MAX_JOURNAL)) fail("copilot_journal_too_large", null);
      const fd = openSync(record.journal, "a", 0o600);
      try { appendFileSync(fd, encoded); fsyncSync(fd); } finally { closeSync(fd); }
      record.ids.add(saved.id);
      record.events.push(saved);
      record.notify?.();
    } catch (error) {
      this.captureFailure(record, error.code || "copilot_evidence_write_failed");
      record.notify?.();
    }
  }

  async create_session({ session_id: sessionId }) {
    if (!this.preflight) fail("copilot_not_initialized");
    identifier(sessionId);
    const directory = join(this.directory, "sessions", sessionId);
    if (existsSync(join(directory, "owner.json"))) fail("copilot_session_already_exists");
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    atomicJson(join(directory, "owner.json"), { session_id: sessionId, config_hash: this.configHash,
      github_account_id: this.identity.id, account_ref: this.params.account_ref, created_at: utc(),
      ...(this.requestPolicyVersion === COST_REQUEST_POLICY ? { request_policy: this.policyEvidence() } : {}) });
    const record = this.record(sessionId);
    this.sessions.set(sessionId, record);
    record.session = await this.client.createSession(this.sessionConfig(sessionId, record));
    if (record.session.sessionId !== sessionId) fail("copilot_session_identity_mismatch");
    await this.checkTools(record);
    return { ...this.preflight, status: "created", session_id: sessionId,
      request_policy_runtime: record.policyRuntime || null };
  }

  async checkTools(record) {
    let metadata;
    try {
      await record.session.rpc.tools.initializeAndValidate();
      metadata = await record.session.rpc.tools.getCurrentMetadata();
    } catch { capabilityFailure("session_tools", "metadata_rpc_failed"); }
    toolCatalog(metadata, "session_tools");
    const offered = metadata.tools.map(webToolName);
    const unexpected = metadata.tools.filter((tool, index) => !this.toolNames().includes(offered[index])).map((tool) => tool.name);
    const missing = this.toolNames().filter((tool) => !offered.includes(tool));
    if (unexpected.length) capabilityFailure("session_tools", "unexpected_tools", {
      unexpected_tools: [...new Set(unexpected.map(diagnosticToolName))] });
    if (missing.length) capabilityFailure("session_tools", "missing_required_tools", { missing_tools: missing });
    record.authorizedToolNames = new Set(metadata.tools.flatMap((tool) => [tool.name, tool.namespacedName].filter(Boolean)));
    record.admitted = true;
    await this.readPolicyRuntime(record);
  }

  async readPolicyRuntime(record, timeoutMs = 1000) {
    if (this.requestPolicyVersion !== COST_REQUEST_POLICY) return null;
    record.policyRuntime = null;
    if (timeoutMs <= 0) return null;
    let timer;
    try {
      const current = await Promise.race([
        record.session.rpc.model.getCurrent(),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("metadata timeout")), timeoutMs); }),
      ]);
      record.policyRuntime = {
        auto_tier: ["efficiency", "balance", "intelligence"].includes(current.autoTier) ? current.autoTier : null,
        reasoning_effort: ["low", "medium", "high", "xhigh", "max"].includes(current.reasoningEffort) ? current.reasoningEffort : null,
      };
    } catch { /* A routing preference is not proof of the effective tier. */ }
    finally { clearTimeout(timer); }
    return record.policyRuntime;
  }

  async getRecord(sessionId) {
    if (this.sessions.has(sessionId)) {
      const record = this.sessions.get(sessionId);
      if (!record.admitted) fail("copilot_source_capability_unavailable");
      return record;
    }
    const record = this.record(sessionId);
    this.sessions.set(sessionId, record);
    record.session = await this.client.resumeSession(sessionId, this.sessionConfig(sessionId, record));
    if (record.session.sessionId !== sessionId) fail("copilot_session_identity_mismatch", null);
    await this.checkTools(record);
    return record;
  }

  result(record, request) {
    const reduced = record.captureFailure ? { status: "failed", code: record.captureFailure } : reduceEvents(record.events, request);
    const usage = record.events.filter((event) => event.type === "assistant.usage");
    const tools = record.events.filter((event) => event.type.startsWith("tool.")).map((event) => ({ id: event.id, type: event.type }));
    return { ...reduced, platform: "github_copilot", session_id: record.sessionId,
      request_id: request.request_id, account_ref: this.params.account_ref, github_account_id: this.identity.id,
      github_login: this.identity.login, source_method: this.params.source_method, model_policy: "auto",
      sdk_version: SDK_VERSION, runtime_version: RUNTIME_VERSION, tool_configuration_hash: this.configHash,
      request_policy_version: this.requestPolicyVersion,
      request_policy: record.owner.request_policy || this.policyEvidence(),
      request_policy_runtime: record.policyRuntime || null,
      usage_totals: usageTotals(usage),
      usage: usage.length ? usage : null, tools, event_cursor: record.events.at(-1)?.id || null,
      evidence_path: record.journal, evidence_sha256: existsSync(record.journal) ? sha(readFileSync(record.journal, "utf8")) : null,
      submitted_at: request.submitted_at,
      observed_at: utc(), first_token_seconds: record.firstTokenAt !== undefined && record.startedAt !== undefined ? (record.firstTokenAt - record.startedAt) / 1000 : null,
      completion_seconds: record.startedAt ? (performance.now() - record.startedAt) / 1000 : null };
  }

  async send_once({ session_id: sessionId, prompt, request_id: requestId, timeout_ms: timeoutMs }) {
    identifier(requestId);
    if (typeof prompt !== "string" || !prompt.trim() || Buffer.byteLength(prompt) > MAX_TEXT || !Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 3600000) fail("copilot_prompt_invalid");
    if (prompt.includes(this.token)) fail("copilot_secret_in_prompt");
    const record = await this.getRecord(sessionId);
    if (existsSync(record.requestPath)) {
      const previous = loadJson(record.requestPath);
      if (previous.request_id !== requestId || previous.prompt_sha256 !== sha(prompt)) fail("copilot_send_fence_conflict", null);
      return this.inspect_session({ session_id: sessionId, request_id: requestId });
    }
    if (this.activeSend) fail("copilot_account_busy");
    this.activeSend = sessionId;
    try {
      await this.checkQuota();
      const history = await record.session.getEvents();
      if (history.some((event) => event.type === "user.message")) fail("copilot_session_not_fresh");
    } catch (error) {
      this.activeSend = null;
      throw error;
    }
    const request = { request_id: requestId, prompt, prompt_sha256: sha(prompt),
      submitted_at: utc(), state: "dispatch_intent", provider_message_id: null };
    // Durable fence precedes the only call to SDK send. Even an exception before
    // the SDK acknowledges its ID leaves the attempt ambiguous and inspectable.
    try { atomicJson(record.requestPath, request); }
    catch (error) { this.activeSend = null; throw error; }
    record.startedAt = performance.now();
    let timer;
    let finish;
    const completed = new Promise((resolvePromise) => { finish = resolvePromise; });
    record.notify = () => {
      const result = this.result(record, request);
      if (request.provider_message_id && result.status !== "ambiguous") finish(result);
    };
    timer = setTimeout(() => finish({ ...this.result(record, request), status: "ambiguous", code: "copilot_source_deadline" }), timeoutMs);
    // Do not await dispatch outside the timer. Promise settlement after timeout
    // still records its provider ID for a later inspect; it never sends again.
    Promise.resolve().then(() => record.session.send({ prompt, mode: "immediate", agentMode: "interactive" }))
      .then((id) => {
        request.provider_message_id = id;
        request.state = "submitted";
        atomicJson(record.requestPath, request);
        record.notify?.();
      }).catch((error) => {
        const code = providerErrorCode(error);
        finish({ ...this.result(record, request), status: "ambiguous", code: code === "copilot_terminal_error" ? "copilot_dispatch_unconfirmed" : code });
      });
    try {
      const result = await completed;
      // Auto may activate while the first turn runs. Snapshot after completion,
      // not just at session creation, within the existing response deadline.
      result.request_policy_runtime = await this.readPolicyRuntime(record,
        Math.max(0, Math.min(1000, timeoutMs - (performance.now() - record.startedAt))));
      atomicJson(join(record.directory, "last-result.json"), result);
      return result;
    } finally {
      clearTimeout(timer);
      record.notify = null;
      // An uncertain request still owns the source slot until reconciliation or
      // cancellation proves it is over. Starting another session cannot hide it.
      const state = this.result(record, request);
      if (state.status !== "ambiguous") this.activeSend = null;
    }
  }

  async inspect_session({ session_id: sessionId, request_id: requestId }) {
    identifier(requestId);
    const record = await this.getRecord(sessionId);
    if (!existsSync(record.requestPath)) return { status: "not_submitted", submitted: false, session_id: sessionId, request_id: requestId };
    const request = loadJson(record.requestPath);
    if (request.request_id !== requestId) fail("copilot_send_fence_conflict", null);
    try {
      const events = await record.session.getEvents();
      // History order is authoritative; local ephemeral events (including idle
      // and usage) retain their original chronological position by timestamp.
      for (const event of events) this.capture(record, event);
      record.events.sort((a, b) => (a.timestamp || "").localeCompare(b.timestamp || ""));
    } catch { /* A saved terminal journal may still prove completion. */ }
    await this.readPolicyRuntime(record);
    const result = this.result(record, request);
    atomicJson(join(record.directory, "last-result.json"), result);
    if (result.status !== "ambiguous" && this.activeSend === sessionId) this.activeSend = null;
    return result;
  }

  async cancel({ session_id: sessionId }) {
    const record = await this.getRecord(sessionId);
    try { await record.session.abort(); }
    catch { return { status: "ambiguous", code: "copilot_cancel_unconfirmed", session_id: sessionId }; }
    if (this.activeSend === sessionId) this.activeSend = null;
    return { status: "cancel_requested", session_id: sessionId };
  }

  async close() {
    if (this.closing) return this.closing;
    this.closing = this.closeOwned();
    return this.closing;
  }

  async closeOwned() {
    if (this.client) {
      let timer;
      const timeout = new Promise((resolvePromise) => { timer = setTimeout(resolvePromise, 4000); });
      try { await Promise.race([this.client.stop().catch(() => {}), timeout]); }
      finally { clearTimeout(timer); }
      await this.client.forceStop().catch(() => {});
    }
    if (this.lockPath && existsSync(this.lockPath)) {
      if (loadJson(this.lockPath).pid === process.pid) unlinkSync(this.lockPath);
    }
    this.token = "";
    return { status: "closed" };
  }
}

export async function runProtocol(worker = new CopilotWorker(), input = process.stdin, output = process.stdout) {
  let buffered = "", pending = 0, queuedWrites = 0, writing = Promise.resolve(), closing = false;
  const write = (message) => {
    if (queuedWrites >= 16) { void shutdown(); return Promise.resolve(); }
    queuedWrites += 1;
    let line = JSON.stringify(message) + "\n";
    if (Buffer.byteLength(line) > MAX_LINE) line = JSON.stringify({ protocol: 1, id: message.id,
      error: { code: "copilot_result_too_large", submitted: null } }) + "\n";
    writing = writing.then(async () => { if (!output.write(line)) await once(output, "drain"); }).finally(() => { queuedWrites -= 1; });
    return writing;
  };
  const shutdown = async () => {
    if (closing) return;
    closing = true;
    try { await worker.close(); } finally { input.destroy(); }
  };
  // Pipe EOF is parent death/close. No global process-name kills; only this SDK
  // instance is stopped. The Python owner additionally reaps its process group.
  input.on("end", shutdown);
  input.on("error", shutdown);
  input.setEncoding("utf8");
  input.on("data", (chunk) => {
    buffered += chunk;
    if (Buffer.byteLength(buffered) > MAX_LINE && !buffered.includes("\n")) { void shutdown(); return; }
    let index;
    while ((index = buffered.indexOf("\n")) >= 0) {
      const line = buffered.slice(0, index); buffered = buffered.slice(index + 1);
      let message;
      try {
        if (Buffer.byteLength(line) > MAX_LINE) fail("copilot_message_too_large");
        message = JSON.parse(line);
        if (message.protocol !== 1 || typeof message.id !== "string" || message.id.length > 160
          || !["initialize", "create_session", "send_once", "inspect_session", "cancel", "close"].includes(message.command)) fail("copilot_protocol_invalid");
        if (pending >= 8) fail("copilot_command_queue_full");
        pending += 1;
        Promise.resolve().then(() => worker[message.command](message.params || {}))
          .then((result) => write({ protocol: 1, id: message.id, result }))
          .catch((error) => write({ protocol: 1, id: message.id,
            error: { code: error instanceof WorkerError ? error.code : "copilot_sdk_error",
              submitted: error instanceof WorkerError ? error.submitted : null,
              ...(error instanceof WorkerError && error.detail ? { detail: error.detail } : {}) } }))
          .finally(() => { pending -= 1; });
      } catch (error) {
        void write({ protocol: 1, id: message?.id || "invalid", error: { code: error.code || "copilot_protocol_invalid", submitted: false } });
      }
    }
  });
  return shutdown;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const worker = new CopilotWorker();
  const shutdown = await runProtocol(worker);
  // If the Python owner dies, SDK shutdown itself may be stuck. The worker is
  // launched as a process-group leader; escalating that exact group cannot
  // target an unrelated Copilot instance. Without an owned group, ESRCH is safe.
  process.stdin.once("end", () => {
    setTimeout(() => {
      try { process.kill(-process.pid, "SIGKILL"); } catch { process.exit(1); }
    }, 6000).unref();
  });
  for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"]) process.once(signal, () => { void shutdown().finally(() => process.exit(0)); });
  process.on("uncaughtException", () => { void shutdown().finally(() => process.exit(1)); });
  process.on("unhandledRejection", () => { void shutdown().finally(() => process.exit(1)); });
}
