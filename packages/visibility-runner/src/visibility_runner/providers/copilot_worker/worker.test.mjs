// Offline only. No official SDK import, credential lookup, or network request.
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { once } from "node:events";
import { PassThrough } from "node:stream";
import { CopilotWorker, reduceEvents, runProtocol, SDK_VERSION, RUNTIME_VERSION, LEGACY_REQUEST_POLICY, COST_REQUEST_POLICY } from "./worker.mjs";

let eventCounter = 0;
function event(type, data = {}, extra = {}) {
  eventCounter += 1;
  return { id: `e${eventCounter}`, type, data, timestamp: new Date(1700000000000 + eventCounter).toISOString(), ...extra };
}
function transcript(prompt = "Find clinics", content = "Clinic A and Clinic B") {
  return [event("user.message", { content: prompt, messageId: "user-1", interactionId: "interaction-1" }),
    event("assistant.turn_start", { turnId: "turn-1", interactionId: "interaction-1" }),
    event("assistant.message", { content, messageId: "answer-1", turnId: "turn-1", interactionId: "interaction-1", model: "resolved-model" }),
    event("assistant.turn_end", { turnId: "turn-1" }), event("session.idle")];
}
const request = { prompt: "Find clinics", provider_message_id: "user-1" };
// These are independent SDK-shaped responses, rather than an echo of the
// worker's requested allowlist. A runtime catalog may contain other tools;
// the restricted web session must expose exactly the two frozen web tools.
const WEB_TOOL_METADATA = [
  { name: "github-mcp-server-web_search", mcpServerName: "github-mcp-server", mcpToolName: "web_search", description: "Offline fixture GitHub web search" },
  { name: "web_fetch", description: "Offline fixture public URL fetch" },
];
const RUNTIME_TOOL_CATALOG = { tools: [
  WEB_TOOL_METADATA[1],
  { name: "bash", description: "Offline fixture shell tool" },
] };

async function rejectsCapability(operation, detail) {
  await assert.rejects(operation, (error) => {
    assert.equal(error.code, "copilot_source_capability_unavailable");
    assert.equal(error.submitted, false);
    assert.deepEqual(error.detail, detail);
    assert.equal(JSON.stringify(error).includes("private-fixture-credential"), false);
    assert.equal(JSON.stringify(error).includes("PRIVATE SDK DETAIL"), false);
    return true;
  });
}

function harness(t, options = {}) {
  const directory = options.directory || mkdtempSync(join(tmpdir(), "copilot-worker-offline-"));
  const sessions = options.sessions || new Map();
  const captures = { clients: [], sessionOptions: [], probeOptions: [], toolLists: [], sends: 0, resumes: 0, stops: 0, probesDestroyed: 0 };
  class FakeClient {
    constructor(config) {
      captures.clients.push(config);
      this.rpc = {
        account: { getQuota: async () => ({ quotaSnapshots: { chat: { isUnlimitedEntitlement: false, remainingPercentage: options.remaining ?? 50 } } }) },
        tools: { list: async (params) => {
          captures.toolLists.push(params);
          if (options.catalogError) throw options.catalogError;
          return structuredClone(Object.hasOwn(options, "catalog") ? options.catalog : RUNTIME_TOOL_CATALOG);
        } },
      };
    }
    async start() {}
    async getStatus() { return { version: RUNTIME_VERSION, protocolVersion: 3 }; }
    async getAuthStatus() { return { isAuthenticated: true, login: "tester" }; }
    async listModels() { if (options.modelsError) throw new Error("metadata unavailable"); return options.models || []; }
    async createSession(config) {
      const probe = config.sessionId.startsWith("capability-");
      (probe ? captures.probeOptions : captures.sessionOptions).push(config);
      const session = { sessionId: config.sessionId, config, events: [],
        rpc: { model: { getCurrent: async () => typeof options.currentModel === "function" ? options.currentModel(captures.sends) : options.currentModel || {} }, tools: {
          initializeAndValidate: async () => { if (probe ? options.probeInitializeToolsError : options.initializeToolsError) throw (probe ? options.probeInitializeToolsError : options.initializeToolsError); },
          getCurrentMetadata: async () => {
            const metadataKey = probe ? "probeMetadata" : "metadata";
            const metadataError = probe ? options.probeMetadataError : options.metadataError;
            if (metadataError) throw metadataError;
            if (Object.hasOwn(options, metadataKey)) return structuredClone(options[metadataKey]);
            const tools = (probe ? options.probeOffered : options.offered)
              ? (probe ? options.probeOffered : options.offered).map((tool) => typeof tool === "string" ? ({ name: tool, description: "Offline fixture session tool" }) : tool)
              : options.method === "github_copilot_web_v1" ? WEB_TOOL_METADATA : [];
            return { tools: structuredClone(tools) };
          },
        } },
        emit(value) { this.events.push(value); this.config.onEvent(value); },
        async send({ prompt }) {
          captures.sends += 1;
          if (options.send) return options.send(this, prompt);
          for (const value of transcript(prompt)) this.emit(value);
          return "user-1";
        },
        async getEvents() { return this.events.filter((value) => value.type !== "session.idle"); },
        async abort() { this.emit(event("session.idle", { aborted: true })); },
        async destroy() { if (probe) captures.probesDestroyed += 1; },
      };
      sessions.set(config.sessionId, session);
      return session;
    }
    async resumeSession(id, config) { captures.resumes += 1; const session = sessions.get(id); session.config = config; return session; }
    async stop() { captures.stops += 1; }
    async forceStop() {}
  }
  const worker = new CopilotWorker({ loadSdk: async () => ({ CopilotClient: FakeClient }),
    fetchIdentity: async () => ({ id: "123", login: "tester" }) });
  const params = { state_directory: directory, github_account_id: options.account || "123", account_ref: "student-primary",
    source_method: options.method || "github_copilot_model_only_v1", model: "auto", token: "private-fixture-credential",
    tool_call_limit: 2, sdk_version: SDK_VERSION, runtime_version: RUNTIME_VERSION };
  t.after(async () => { await worker.close(); if (!options.keep) rmSync(directory, { recursive: true, force: true }); });
  return { worker, params, directory, sessions, captures };
}

test("GitHub MCP web search is admitted without pretending it is a built-in tool", async (t) => {
  const h = harness(t, { method: "github_copilot_web_v1" });
  await h.worker.initialize(h.params);
  assert.equal(h.captures.probeOptions.length, 1);
  assert.equal(h.captures.probesDestroyed, 1);
  assert.deepEqual(h.captures.probeOptions[0].availableTools, ["mcp:github-mcp-server-web_search", "builtin:web_fetch"]);
  assert.equal(h.captures.sends, 0);
  await h.worker.create_session({ session_id: "source" });
  assert.equal((await h.worker.send_once({ session_id: "source", request_id: "request", prompt: request.prompt, timeout_ms: 100 })).status, "completed");
});

for (const [name, tools, detail] of [
  ["missing search", [WEB_TOOL_METADATA[1]], { reason: "missing_required_tools", missing_tools: ["web_search"] }],
  ["builtin spoof", [{ name: "builtin:web_search" }, WEB_TOOL_METADATA[1]], { reason: "unexpected_tools", unexpected_tools: ["unrecognized_tool"] }],
  ["foreign MCP search", [{ name: "web_search", mcpServerName: "foreign", mcpToolName: "web_search" }, WEB_TOOL_METADATA[1]], { reason: "unexpected_tools", unexpected_tools: ["web_search"] }],
  ["fetch alias spoof", [WEB_TOOL_METADATA[0], { ...WEB_TOOL_METADATA[1], namespacedName: "builtin:bash" }], { reason: "unexpected_tools", unexpected_tools: ["web_fetch"] }],
  ["unproved alias", [{ ...WEB_TOOL_METADATA[0], namespacedName: "builtin:bash" }, WEB_TOOL_METADATA[1]], { reason: "unexpected_tools", unexpected_tools: ["github-mcp-server-web_search"] }],
]) {
  test(`no-prompt capability probe rejects ${name} before a measurement session`, async (t) => {
    const h = harness(t, { method: "github_copilot_web_v1", probeMetadata: { tools } });
    await rejectsCapability(h.worker.initialize(h.params), { stage: "session_tools", ...detail });
    assert.equal(h.captures.probeOptions.length, 1);
    assert.equal(h.captures.probesDestroyed, 1);
    assert.equal(h.captures.sessionOptions.length, 0);
    assert.equal(h.captures.sends, 0);
    assert.equal(existsSync(join(h.directory, "sessions")), false);
  });
}

test("probe RPC failure closes its no-prompt session and preserves only a safe category", async (t) => {
  const h = harness(t, { method: "github_copilot_web_v1", probeMetadataError: new Error("PRIVATE SDK DETAIL private-fixture-credential") });
  await rejectsCapability(h.worker.initialize(h.params), { stage: "session_tools", reason: "metadata_rpc_failed" });
  assert.equal(h.captures.probesDestroyed, 1);
  assert.equal(h.captures.sends, 0);
});

test("terminal answer requires a bound user, ended turn and subsequent idle", () => {
  const values = transcript();
  assert.equal(reduceEvents(values, request).status, "completed");
  assert.equal(reduceEvents(values.slice(0, -1), request).status, "ambiguous");
  assert.equal(reduceEvents([values[0], values[4]], request).status, "ambiguous");
  assert.equal(reduceEvents(values, { ...request, provider_message_id: "other" }).status, "ambiguous");
});

test("optimized Auto requests trim only intended sections and preserve the exact prompt", async (t) => {
  const h = harness(t, { models: [{ id: "auto", capabilities: { supports: { reasoningEffort: true } }, supportedReasoningEfforts: ["low", "medium"] }],
    currentModel: { autoTier: "efficiency", reasoningEffort: "low" } });
  await h.worker.initialize({ ...h.params, request_policy_version: COST_REQUEST_POLICY });
  await h.worker.create_session({ session_id: "optimized" });
  const options = h.captures.sessionOptions[0];
  assert.deepEqual(options.capi, { autoTier: "efficiency" });
  assert.equal(options.reasoningEffort, "low");
  assert.equal(options.contextTier, "default");
  assert.equal(options.systemMessage.mode, "customize");
  assert.deepEqual(options.systemMessage.sections.code_change_rules, { action: "remove" });
  for (const section of ["safety", "guidelines", "runtime_instructions", "tool_instructions", "tool_efficiency"]) assert.equal(options.systemMessage.sections[section], undefined);
  assert.equal(options.maxOutputTokens, undefined);
  assert.equal(options.sessionLimits, undefined);
  assert.deepEqual(options.availableTools, []);
  const prompt = "  Recommend good clinics for laser hair removal in Marylebone.  ";
  const result = await h.worker.send_once({ session_id: "optimized", prompt, request_id: "optimized-request", timeout_ms: 100 });
  assert.equal(result.status, "completed");
  assert.equal(h.sessions.get("optimized").events.find((event) => event.type === "user.message").data.content, prompt);
  assert.equal(result.request_policy_version, COST_REQUEST_POLICY);
  assert.equal(result.request_policy.requested_auto_tier, "efficiency");
  assert.deepEqual(result.request_policy_runtime, { auto_tier: "efficiency", reasoning_effort: "low" });
});

test("Auto reasoning is omitted without explicit advertised low support", async (t) => {
  for (const options of [{}, { modelsError: true },
    { models: [{ id: "auto", capabilities: { supports: { reasoningEffort: true } }, supportedReasoningEfforts: ["high"] }] },
    { models: [{ id: "other-model", capabilities: { supports: { reasoningEffort: true } }, supportedReasoningEfforts: ["low"] }] }]) {
    const h = harness(t, options);
    await h.worker.initialize({ ...h.params, request_policy_version: COST_REQUEST_POLICY });
    await h.worker.create_session({ session_id: "unsupported" });
    assert.equal(h.captures.sessionOptions[0].reasoningEffort, undefined);
    const result = await h.worker.send_once({ session_id: "unsupported", prompt: request.prompt, request_id: "r", timeout_ms: 100 });
    assert.equal(result.request_policy.reasoning_effort, null);
    assert.equal(result.request_policy_runtime.auto_tier, null);
  }
});

test("completed policy evidence reflects Auto activation during the first turn", async (t) => {
  const h = harness(t, { currentModel: (sends) => ({ autoTier: sends ? "efficiency" : "balance" }) });
  await h.worker.initialize({ ...h.params, request_policy_version: COST_REQUEST_POLICY });
  const created = await h.worker.create_session({ session_id: "auto-activation" });
  assert.equal(created.request_policy_runtime.auto_tier, "balance");
  const result = await h.worker.send_once({ session_id: "auto-activation", prompt: request.prompt, request_id: "request", timeout_ms: 100 });
  assert.equal(result.request_policy_runtime.auto_tier, "efficiency");
  const inspected = await h.worker.inspect_session({ session_id: "auto-activation", request_id: "request" });
  assert.equal(inspected.request_policy_runtime.auto_tier, "efficiency");
  assert.equal(h.captures.sends, 1);
});

test("unavailable post-turn metadata does not claim the requested Auto tier or reject an answer", async (t) => {
  const h = harness(t, { currentModel: (sends) => {
    if (sends) throw new Error("metadata unavailable");
    return { autoTier: "balance" };
  } });
  await h.worker.initialize({ ...h.params, request_policy_version: COST_REQUEST_POLICY });
  await h.worker.create_session({ session_id: "auto-unknown" });
  const result = await h.worker.send_once({ session_id: "auto-unknown", prompt: request.prompt, request_id: "request", timeout_ms: 100 });
  assert.equal(result.status, "completed");
  assert.equal(result.request_policy_runtime, null);
});

test("old sessions keep their historical configuration and cannot resume under optimized policy", async (t) => {
  const old = harness(t, { keep: true });
  await old.worker.initialize(old.params);
  await old.worker.create_session({ session_id: "legacy" });
  const hash = old.worker.configHash;
  assert.equal(old.captures.sessionOptions[0].capi, undefined);
  assert.equal(old.captures.sessionOptions[0].systemMessage, undefined);
  await old.worker.close();
  const same = harness(t, { directory: old.directory, sessions: old.sessions, keep: true });
  await same.worker.initialize({ ...same.params, request_policy_version: LEGACY_REQUEST_POLICY });
  assert.equal(same.worker.configHash, hash);
  await same.worker.getRecord("legacy");
  await same.worker.close();
  const changed = harness(t, { directory: old.directory, sessions: old.sessions });
  await changed.worker.initialize({ ...changed.params, request_policy_version: COST_REQUEST_POLICY });
  assert.notEqual(changed.worker.configHash, hash);
  await assert.rejects(changed.worker.getRecord("legacy"), /copilot_session_identity_mismatch/);
  assert.equal(changed.captures.resumes, 0);
});

test("optimized resume preserves the reasoning chosen before the original send", async (t) => {
  const first = harness(t, { keep: true, models: [{ id: "auto", capabilities: { supports: { reasoningEffort: true } }, supportedReasoningEfforts: ["low"] }] });
  await first.worker.initialize({ ...first.params, request_policy_version: COST_REQUEST_POLICY });
  await first.worker.create_session({ session_id: "resume-low" });
  await first.worker.close();
  const second = harness(t, { directory: first.directory, sessions: first.sessions });
  await second.worker.initialize({ ...second.params, request_policy_version: COST_REQUEST_POLICY });
  await second.worker.getRecord("resume-low");
  assert.equal(second.sessions.get("resume-low").config.reasoningEffort, "low");
  assert.equal(second.captures.sends, 0);
});

test("invalid request policy fails before account or SDK work", async (t) => {
  const h = harness(t);
  await assert.rejects(h.worker.initialize({ ...h.params, request_policy_version: "unversioned" }), /copilot_source_config_invalid/);
  assert.equal(h.captures.clients.length, 0);
});

test("numeric usage records exact AI credits without private reasoning or double counting", async (t) => {
  const h = harness(t, { send: async (session, prompt) => {
    const values = transcript(prompt);
    for (const value of values.slice(0, -1)) session.emit(value);
    const usage = event("assistant.usage", { model: "model", apiCallId: "api-1", inputTokens: 3761, outputTokens: 724,
      cacheReadTokens: 0, cacheWriteTokens: 3758, reasoningTokens: 453, cost: 1,
      copilotUsage: { totalNanoAiu: 162801000, secret: "DO NOT PERSIST" }, reasoning: "PRIVATE REASONING" });
    session.emit(usage); session.emit(usage); // Ephemeral replay must not double charge the summary.
    session.emit(values.at(-1));
    return "user-1";
  } });
  await h.worker.initialize(h.params); await h.worker.create_session({ session_id: "usage" });
  const result = await h.worker.send_once({ session_id: "usage", prompt: request.prompt, request_id: "usage-request", timeout_ms: 100 });
  assert.deepEqual(result.usage_totals, { api_call_count: 1, input_tokens: 3761, output_tokens: 724,
    cache_read_tokens: 0, cache_write_tokens: 3758, reasoning_tokens: 453,
    total_nano_aiu: 162801000, ai_credits: "0.162801000" });
  assert.equal(result.usage[0].data.cost, 1); // Legacy multiplier is not AI credits.
  const journal = readFileSync(join(h.directory, "sessions", "usage", "events.jsonl"), "utf8");
  assert.equal(journal.includes("DO NOT PERSIST"), false);
  assert.equal(journal.includes("PRIVATE REASONING"), false);
});

test("absent or partial billing counters remain unknown", async (t) => {
  const h = harness(t, { send: async (session, prompt) => {
    const values = transcript(prompt);
    for (const value of values.slice(0, -1)) session.emit(value);
    session.emit(event("assistant.usage", { inputTokens: 10, outputTokens: 5, cost: 1, copilotUsage: { totalNanoAiu: 1000 } }));
    session.emit(event("assistant.usage", { inputTokens: 20, outputTokens: 7, cost: 1 }));
    session.emit(values.at(-1)); return "user-1";
  } });
  await h.worker.initialize(h.params); await h.worker.create_session({ session_id: "unknown-cost" });
  const result = await h.worker.send_once({ session_id: "unknown-cost", prompt: request.prompt, request_id: "request", timeout_ms: 100 });
  assert.equal(result.usage_totals.api_call_count, 2);
  assert.equal(result.usage_totals.input_tokens, 30);
  assert.equal(result.usage_totals.ai_credits, null);
  assert.equal(result.usage_totals.reasoning_tokens, null);
});

test("pending tools, subagent prose, mismatched turns and terminal errors do not accept", () => {
  let values = transcript();
  values.splice(2, 0, event("tool.execution_start", { toolCallId: "tool-1" }));
  assert.equal(reduceEvents(values, request).status, "ambiguous");
  values = transcript(); values[2].agentId = "subagent";
  assert.equal(reduceEvents(values, request).status, "ambiguous");
  values = transcript(); values[3].data.turnId = "wrong";
  assert.equal(reduceEvents(values, request).status, "ambiguous");
  values = transcript(); values.push(event("session.error", { error: "fixture" }));
  assert.equal(reduceEvents(values, request).status, "failed");
});

test("returned source is exact and tool progress is excluded", () => {
  const values = transcript("Find clinics", "  Clínique A\nClinic A  ");
  values.splice(2, 0, event("assistant.message", { content: "Searching…", turnId: "turn-1", toolRequests: [{ toolCallId: "t" }] }));
  const result = reduceEvents(values, request);
  assert.equal(result.text, "  Clínique A\nClinic A  ");
  assert.match(result.source_sha256, /^[a-f0-9]{64}$/);
});

test("multiple user turns cannot be silently attributed to one observation", () => {
  const values = transcript(); values.push(event("user.message", { content: "different", messageId: "user-2" }));
  assert.equal(reduceEvents(values, request).status, "ambiguous");
});

test("source sends once, persists fence, then returns saved completion on repeat", async (t) => {
  const { worker, params, directory, captures } = harness(t);
  await worker.initialize(params); await worker.create_session({ session_id: "session-1" });
  const input = { session_id: "session-1", prompt: request.prompt, request_id: "request-1", timeout_ms: 100 };
  const answer = await worker.send_once(input);
  assert.equal(answer.status, "completed");
  assert.equal(answer.first_token_seconds, null); // Final prose is not a streaming-token timestamp.
  assert.equal((await worker.send_once(input)).status, "completed");
  assert.equal(captures.sends, 1);
  assert.equal(JSON.parse(readFileSync(join(directory, "sessions/session-1/request.json"))).provider_message_id, "user-1");
  await assert.rejects(worker.send_once({ ...input, prompt: "Different" }), { code: "copilot_send_fence_conflict" });
});

test("first-token timing uses the first nonempty root delta, not a message-start notification", async (t) => {
  let expectedSeconds;
  const { worker, params, directory } = harness(t, { send: async (session, prompt) => {
    const values = transcript(prompt);
    session.emit(values[0]); session.emit(values[1]);
    const record = worker.sessions.get(session.sessionId);
    session.emit(event("assistant.message_start", { messageId: "answer-1" }));
    assert.equal(record.firstTokenAt, undefined);
    session.emit(event("assistant.message_delta", { messageId: "answer-1", deltaContent: "" }));
    session.emit(event("assistant.message_delta", { messageId: "subagent-answer", deltaContent: "Other" }, { agentId: "subagent" }));
    assert.equal(record.firstTokenAt, undefined);
    session.emit(event("assistant.message_delta", { messageId: "answer-1", deltaContent: "Clinic" }));
    const firstTokenAt = record.firstTokenAt;
    assert.equal(typeof firstTokenAt, "number");
    session.emit(event("assistant.message_delta", { messageId: "answer-1", deltaContent: " A" }));
    assert.equal(record.firstTokenAt, firstTokenAt);
    expectedSeconds = (firstTokenAt - record.startedAt) / 1000;
    for (const value of values.slice(2)) session.emit(value);
    return "user-1";
  } });
  await worker.initialize(params); await worker.create_session({ session_id: "session-1" });
  const result = await worker.send_once({ session_id: "session-1", prompt: request.prompt, request_id: "request-1", timeout_ms: 500 });
  assert.equal(result.status, "completed");
  assert.equal(result.first_token_seconds, expectedSeconds);
  assert.ok(result.first_token_seconds >= 0);
  assert.equal(readFileSync(join(directory, "sessions/session-1/events.jsonl"), "utf8").includes("assistant.message_delta"), false);
});

test("deadline covers SDK dispatch; uncertain send is inspectable without resend", async (t) => {
  let acknowledge;
  const { worker, params, sessions, captures } = harness(t, { send: (session, prompt) => new Promise((resolvePromise) => { acknowledge = resolvePromise; }) });
  await worker.initialize(params); await worker.create_session({ session_id: "session-1" });
  const input = { session_id: "session-1", prompt: request.prompt, request_id: "request-1", timeout_ms: 10 };
  assert.equal((await worker.send_once(input)).status, "ambiguous");
  const session = sessions.get("session-1");
  for (const value of transcript()) session.emit(value);
  acknowledge("user-1"); await new Promise((resolvePromise) => setImmediate(resolvePromise));
  assert.equal((await worker.inspect_session(input)).status, "completed");
  await worker.send_once(input);
  assert.equal(captures.sends, 1);
});

test("recovery without dispatch acknowledgement requires exactly matching single user", async (t) => {
  const { worker, params, sessions, captures } = harness(t, { send: async () => { throw new Error("connection lost"); } });
  await worker.initialize(params); await worker.create_session({ session_id: "session-1" });
  const input = { session_id: "session-1", prompt: request.prompt, request_id: "request-1", timeout_ms: 100 };
  assert.equal((await worker.send_once(input)).status, "ambiguous");
  for (const value of transcript()) sessions.get("session-1").emit(value);
  assert.equal((await worker.inspect_session(input)).status, "completed");
  assert.equal(captures.sends, 1);
});

test("restart reuses private session ownership and durable ephemeral completion", async (t) => {
  const first = harness(t, { keep: true });
  await first.worker.initialize(first.params); await first.worker.create_session({ session_id: "session-1" });
  const input = { session_id: "session-1", prompt: request.prompt, request_id: "request-1", timeout_ms: 100 };
  await first.worker.send_once(input); await first.worker.close();
  const second = harness(t, { directory: first.directory, sessions: first.sessions });
  await second.worker.initialize(second.params);
  assert.equal((await second.worker.inspect_session(input)).status, "completed");
  assert.equal(second.captures.sends, 0);
  assert.equal(second.captures.resumes, 1);
  await assert.rejects(second.worker.inspect_session({ ...input, session_id: "unowned" }), { code: "copilot_session_not_owned" });
});

test("account mismatch prevents any SDK client startup", async (t) => {
  const { worker, params, captures } = harness(t, { account: "wrong-account" });
  await assert.rejects(worker.initialize(params), { code: "copilot_account_identity_mismatch" });
  assert.equal(captures.clients.length, 0);
});

test("recovery refuses changed method/configuration for an owned session", async (t) => {
  const first = harness(t, { keep: true });
  await first.worker.initialize(first.params); await first.worker.create_session({ session_id: "session-1" });
  await first.worker.close();
  const second = harness(t, { directory: first.directory, sessions: first.sessions, method: "github_copilot_web_v1" });
  await second.worker.initialize(second.params);
  await assert.rejects(second.worker.inspect_session({ session_id: "session-1", request_id: "request-1" }), { code: "copilot_session_identity_mismatch" });
  assert.equal(second.captures.resumes, 0);
  assert.equal(second.captures.sends, 0);
});

test("source evidence never silently changes answer bytes when secrets are encountered", async (t) => {
  const { worker, params } = harness(t, { send: async (session, prompt) => {
    for (const value of transcript(prompt, "private-fixture-credential")) session.emit(value);
    return "user-1";
  } });
  await worker.initialize(params); await worker.create_session({ session_id: "session-1" });
  const result = await worker.send_once({ session_id: "session-1", prompt: request.prompt, request_id: "request-1", timeout_ms: 100 });
  assert.equal(result.status, "failed");
  assert.equal(result.code, "copilot_secret_in_response");
  assert.equal(result.text, undefined);
});

test("web method fails closed when exact offered web tools are unavailable", async (t) => {
  const { worker, params, captures } = harness(t, { method: "github_copilot_web_v1", offered: ["builtin:web_fetch"] });
  await worker.initialize(params);
  await assert.rejects(worker.create_session({ session_id: "session-1" }), { code: "copilot_source_capability_unavailable" });
  await assert.rejects(worker.send_once({ session_id: "session-1", request_id: "request-1", prompt: request.prompt, timeout_ms: 100 }), { code: "copilot_source_capability_unavailable" });
  assert.equal(captures.sends, 0);
});

test("web capability is checked against the runtime catalog before any session or source fence", async (t) => {
  const { worker, params, captures, directory } = harness(t, {
    method: "github_copilot_web_v1", catalog: { tools: [] },
  });
  await rejectsCapability(worker.initialize(params), {
    stage: "runtime_tool_catalog", reason: "missing_required_tools", missing_tools: ["web_fetch"],
  });
  assert.deepEqual(captures.toolLists, [{ model: "auto" }]);
  assert.equal(captures.sessionOptions.length, 0);
  assert.equal(captures.sends, 0);
  assert.equal(existsSync(join(directory, "sessions")), false);
  await assert.rejects(worker.create_session({ session_id: "unavailable" }));
  assert.equal(captures.sessionOptions.length, 0);
});

test("web admission preserves the frozen policy when the independent runtime catalog offers other tools", async (t) => {
  const { worker, params, captures } = harness(t, { method: "github_copilot_web_v1" });
  const ready = await worker.initialize({ ...params, request_policy_version: COST_REQUEST_POLICY });
  assert.deepEqual(captures.toolLists, [{ model: "auto" }]);
  assert.equal(ready.source_method, "github_copilot_web_v1");
  assert.equal(ready.request_policy_version, COST_REQUEST_POLICY);
  assert.deepEqual(ready.allowed_tools, ["web_search", "web_fetch"]);
  await worker.create_session({ session_id: "web-capable" });
  assert.deepEqual(captures.sessionOptions[0].availableTools, ["mcp:github-mcp-server-web_search", "builtin:web_fetch"]);
  assert.deepEqual(captures.sessionOptions[0].mcpServers, {});
  assert.deepEqual(captures.sessionOptions[0].githubMcpToolConfig, {
    enableAllTools: false, additionalTools: ["web_search"], additionalToolsets: [],
  });
  assert.equal(captures.sessionOptions[0].onPermissionRequest({ kind: "shell" }).kind, "reject");
  const result = await worker.send_once({ session_id: "web-capable", request_id: "request-1", prompt: request.prompt, timeout_ms: 100 });
  assert.equal(result.status, "completed");
  assert.equal(result.source_method, "github_copilot_web_v1");
  assert.equal(captures.sends, 1);
});

for (const [name, catalog] of [
  ["null tools", { tools: null }],
  ["non-array tools", { tools: {} }],
  ["non-string tool name", { tools: [{ name: 42, description: "PRIVATE SDK DETAIL" }] }],
]) {
  test(`malformed runtime tool catalog (${name}) fails before sessions with a safe category`, async (t) => {
    const { worker, params, captures } = harness(t, { method: "github_copilot_web_v1", catalog });
    await rejectsCapability(worker.initialize(params), { stage: "runtime_tool_catalog", reason: "metadata_malformed" });
    assert.equal(captures.sessionOptions.length, 0);
    assert.equal(captures.sends, 0);
  });
}

test("runtime capability RPC failure exposes only the safe category and never authorizes model-only fallback", async (t) => {
  const { worker, params, captures } = harness(t, { method: "github_copilot_web_v1",
    catalogError: new Error("PRIVATE SDK DETAIL private-fixture-credential") });
  await rejectsCapability(worker.initialize({ ...params, request_policy_version: COST_REQUEST_POLICY }), {
    stage: "runtime_tool_catalog", reason: "metadata_rpc_failed",
  });
  await assert.rejects(worker.create_session({ session_id: "no-fallback" }));
  assert.equal(captures.clients.length, 1);
  assert.equal(captures.sessionOptions.length, 0);
  assert.equal(captures.sends, 0);
});

for (const [name, options, detail] of [
  ["missing search", { offered: ["web_fetch"] }, { reason: "missing_required_tools", missing_tools: ["web_search"] }],
  ["unexpected shell", { offered: [...WEB_TOOL_METADATA, { name: "bash" }] }, { reason: "unexpected_tools", unexpected_tools: ["bash"] }],
  ["null metadata", { metadata: { tools: null } }, { reason: "metadata_malformed" }],
  ["non-string name", { metadata: { tools: [{ name: 42, description: "PRIVATE SDK DETAIL" }] } }, { reason: "metadata_malformed" }],
  ["validation RPC failure", { initializeToolsError: new Error("PRIVATE SDK DETAIL private-fixture-credential") }, { reason: "metadata_rpc_failed" }],
  ["metadata RPC failure", { metadataError: new Error("PRIVATE SDK DETAIL private-fixture-credential") }, { reason: "metadata_rpc_failed" }],
]) {
  test(`restricted web session (${name}) retains a safe diagnosis and cannot send`, async (t) => {
    const { worker, params, captures } = harness(t, { method: "github_copilot_web_v1", ...options });
    await worker.initialize(params);
    await rejectsCapability(worker.create_session({ session_id: "restricted" }), { stage: "session_tools", ...detail });
    await assert.rejects(worker.send_once({ session_id: "restricted", request_id: "blocked", prompt: request.prompt, timeout_ms: 100 }), {
      code: "copilot_source_capability_unavailable", submitted: false,
    });
    assert.deepEqual(captures.sessionOptions[0].availableTools, ["mcp:github-mcp-server-web_search", "builtin:web_fetch"]);
    assert.equal(captures.sessionOptions.length, 1);
    assert.equal(captures.sends, 0);
  });
}

test("GitHub MCP search with builtin-prefixed fetch satisfies the exact web capability contract", async (t) => {
  const { worker, params, captures } = harness(t, { method: "github_copilot_web_v1",
    offered: [WEB_TOOL_METADATA[0], "builtin:web_fetch"] });
  await worker.initialize(params);
  await worker.create_session({ session_id: "prefixed" });
  const result = await worker.send_once({ session_id: "prefixed", request_id: "request-1", prompt: request.prompt, timeout_ms: 100 });
  assert.equal(result.status, "completed");
  assert.equal(captures.sends, 1);
});

test("capability diagnosis crosses the correlated stdio boundary without raw SDK or credential text", { timeout: 1000 }, async (t) => {
  const { worker, params, captures } = harness(t, { method: "github_copilot_web_v1",
    catalogError: new Error("PRIVATE SDK DETAIL private-fixture-credential") });
  const input = new PassThrough();
  const output = new PassThrough();
  const shutdown = await runProtocol(worker, input, output);
  t.after(shutdown);
  const response = once(output, "data");
  input.write(`${JSON.stringify({ protocol: 1, id: "capability-probe", command: "initialize", params })}\n`);
  const [bytes] = await response;
  const encoded = bytes.toString("utf8");
  assert.deepEqual(JSON.parse(encoded), { protocol: 1, id: "capability-probe", error: {
    code: "copilot_source_capability_unavailable", submitted: false,
    detail: { stage: "runtime_tool_catalog", reason: "metadata_rpc_failed" },
  } });
  assert.equal(encoded.includes("PRIVATE SDK DETAIL"), false);
  assert.equal(encoded.includes("private-fixture-credential"), false);
  assert.equal(captures.sessionOptions.length, 0);
  assert.equal(captures.sends, 0);
});

test("concurrent protocol sends cannot pass the same account admission check", async (t) => {
  const { worker, params, captures } = harness(t, { send: () => new Promise(() => {}) });
  await worker.initialize(params);
  await worker.create_session({ session_id: "session-1" });
  await worker.create_session({ session_id: "session-2" });
  const first = worker.send_once({ session_id: "session-1", request_id: "request-1", prompt: request.prompt, timeout_ms: 10 });
  await assert.rejects(worker.send_once({ session_id: "session-2", request_id: "request-2", prompt: request.prompt, timeout_ms: 10 }), { code: "copilot_account_busy" });
  assert.equal((await first).status, "ambiguous");
  assert.equal(captures.sends, 1);
});

test("empty mode isolates repo, tools, plugins, auth and persistent cross-session memory", async (t) => {
  const { worker, params, captures } = harness(t);
  await worker.initialize(params); await worker.create_session({ session_id: "session-1" });
  assert.equal(captures.clients[0].mode, "empty");
  assert.equal(captures.clients[0].useLoggedInUser, false);
  const config = captures.sessionOptions[0];
  for (const key of ["enableConfigDiscovery", "enableSkills", "enableFileHooks", "enableHostGitOperations", "enableSessionStore"]) assert.equal(config[key], false);
  assert.deepEqual(config.availableTools, []);
  assert.deepEqual(config.disabledMcpServers, ["github-mcp-server"]);
  assert.equal(captures.probeOptions.length, 0);
  assert.deepEqual(config.mcpServers, {});
  assert.equal(config.onPermissionRequest({ kind: "shell" }).kind, "reject");
});

test("quota exhaustion blocks before source fence or send and never enables overage", async (t) => {
  const { worker, params, captures, directory } = harness(t, { remaining: 0 });
  await assert.rejects(worker.initialize(params), { code: "copilot_quota_exhausted" });
  assert.equal(captures.sends, 0);
  assert.equal(existsSync(join(directory, "sessions")), false);
});

test("web permissions reject file, private URL and managed approval requests; tool cap is finite", async (t) => {
  const { worker, params, captures } = harness(t, { method: "github_copilot_web_v1" });
  await worker.initialize(params); await worker.create_session({ session_id: "session-1" });
  const config = captures.sessionOptions[0];
  assert.equal(config.onPermissionRequest({ kind: "url", url: "https://example.com" }).kind, "approve-once");
  for (const url of ["file:///etc/passwd", "http://127.0.0.1", "http://10.0.0.1"]) assert.equal(config.onPermissionRequest({ kind: "url", url }).kind, "reject");
  assert.equal(config.onPermissionRequest({ kind: "url", url: "https://example.com", managedApprovalRequired: true }).kind, "reject");
  const hook = config.hooks.onPreToolUse;
  assert.equal(hook({ toolName: "github-mcp-server-web_search" }).permissionDecision, "allow");
  assert.equal(hook({ toolName: "web_fetch" }).permissionDecision, "allow");
  assert.equal(hook({ toolName: "github-mcp-server-web_search" }).permissionDecision, "deny");
});
