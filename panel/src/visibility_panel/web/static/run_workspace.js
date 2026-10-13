/* Shared run history, guarded delivery and measurement inspection.
 * Each page supplies its state, navigation and refresh bootstrap. */

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.text();
  let payload;
  try {
    payload = body ? JSON.parse(body) : {};
  } catch {
    throw new Error(`The local service returned an unreadable response (${response.status}).`);
  }
  if (!response.ok) {
    const error = new Error(payload.error || response.statusText);
    error.status = response.status;
    error.start_rejected = payload.start_rejected === true;
    if (typeof payload.readout_message === "string") error.readoutMessage = payload.readout_message;
    throw error;
  }
  return payload;
}

function byId(id) {
  return document.getElementById(id);
}

function setActionStatus(message, tone = "info", timeoutMs = 0) {
  const node = byId("actionStatus");
  clearTimeout(state.actionStatusTimer);
  state.actionStatusTimer = null;
  node.textContent = message || "";
  node.className = `action-status${message ? ` ${tone}` : " hidden"}`;
  if (message && timeoutMs > 0) {
    state.actionStatusTimer = setTimeout(() => setActionStatus(""), timeoutMs);
  }
}

function actionErrorMessage(action, error) {
  const detail = error?.message || String(error || "Unknown error");
  return `${action} failed: ${detail}`;
}

function reportActionError(action, error) {
  setActionStatus(actionErrorMessage(action, error), "error");
}

async function runUiAction(action, callback, options = {}) {
  try {
    const result = await callback();
    if (options.successMessage) setActionStatus(options.successMessage, "success", 4500);
    return result;
  } catch (error) {
    reportActionError(action, error);
    return null;
  }
}

function clear(node) {
  node.innerHTML = "";
}

function refreshSelectedReadoutIfChanged() {
  const job = state.jobs.find((item) => item.id === state.selectedJob);
  if (!job || !isVisibilityMeasurementJob(job)) return;
  const signature = JSON.stringify([job.status, job.returncode, job.run_config?.runner_execution_id ?? null]);
  if (signature === state.selectedJobSignature) return;
  const first = state.selectedJobSignature === undefined;
  state.selectedJobSignature = signature;
  // The first signature is recorded when the job is selected; later changes
  // (a worker finishing or dying) refresh the readout and the Refetch button.
  if (!first) void refreshFailureReadout(job.id);
}

function isVisibilityMeasurementJob(job) {
  const config = job.run_config || {};
  return config.runner_owned_snapshot === true || Boolean(
    config.runner_runtime && config.runner_measurement && String(config.run_id || "") === job.id
  );
}

function isRunHistoryView(view) {
  return view === "jobs" || view === "testing";
}

function jobBelongsToView(job, view) {
  return isVisibilityMeasurementJob(job) === (view === "testing");
}

function mountRunWorkspace(view) {
  if (state.jobWorkspaceView !== view) {
    state.selectedJobs[state.jobWorkspaceView] = state.selectedJob;
    state.jobWorkspaceView = view;
    state.selectedJob = state.selectedJobs[view];
    state.selectedJobSignature = undefined;
    state.logRequestId += 1;
    failureReadoutRequestId += 1;
    byId("measurementFailureReadout").hidden = true;
    clear(byId("measurementFailureContent"));
    delete byId("measurementFailureContent").dataset.jobId;
    byId("runHistoryPicker").open = !state.selectedJob;
    void refreshLog();
  }
  byId(`${view}View`).appendChild(byId("jobsWorkspace"));
  renderJobs();
}

function measurementStatusLabel(status) {
  const labels = {
    planned: "Planned",
    queued: "Queued",
    running: "Measuring",
    finalizing: "Finalizing",
    succeeded: "Measurement complete",
    partial: "Incomplete results",
    needs_attention: "Review required",
    failed: "Failed",
    cancelled: "Cancelled",
    stopping: "Stopping",
  };
  return Object.hasOwn(labels, status) ? labels[status] : status;
}

function isClientResearchJob(job) {
  return job.workflow_id === "client_research" || Boolean(job.run_config?.client_research);
}

function renderJobs() {
  const tbody = byId("jobRows");
  const actions = byId("selectedRunActions");
  const focused = document.activeElement;
  const focusKey = tbody.contains(focused) || actions?.contains(focused) ? focused?.dataset.jobControl : null;
  clear(tbody);
  if (actions) clear(actions);
  const visibleJobs = state.jobs.filter((job) => jobBelongsToView(job, state.jobWorkspaceView));
  const selectedJob = visibleJobs.find((job) => job.id === state.selectedJob);
  const testing = state.jobWorkspaceView === "testing";
  byId("runHistoryHelp").textContent = testing
    ? "All saved visibility engine runs. Select a run to inspect its chats or refetch unfinished measurements."
    : "Clinic research and other automations. Visibility runs are in Testing → Visibility engine.";
  const historyError = state.historyErrors[state.jobWorkspaceView];
  byId("runHistoryError").hidden = !historyError;
  byId("runHistoryError").textContent = historyError || "";
  if (byId("selectedRunTitle")) byId("selectedRunTitle").textContent = selectedJob
    ? (isClientResearchJob(selectedJob) || selectedJob.run_config?.visibility_test || selectedJob.run_config?.snapshot_selection_summary?.scope_kind === "diagnostic"
      ? selectedJob.label : isVisibilityMeasurementJob(selectedJob) ? "Area visibility" : selectedJob.label) : "Select a run to see its results";
  if (byId("selectedRunSummary")) byId("selectedRunSummary").textContent = selectedJob
    ? [selectedJob.run_config?.snapshot_selection_summary?.area_names?.join(" · "),
      isVisibilityMeasurementJob(selectedJob) ? measurementStatusLabel(selectedJob.status) : selectedJob.status,
      jobTimeLabel(selectedJob.started_at || selectedJob.created_at || ""), selectedJob.id].filter(Boolean).join(" · ")
    : "Choose a saved run above to review measurements or continue unfinished work.";
  if (byId("runHistorySelection")) byId("runHistorySelection").textContent = selectedJob
    ? `Change run · ${selectedJob.label} · ${jobTimeLabel(selectedJob.started_at || selectedJob.created_at || "") || selectedJob.id}` : "Choose a run";
  if (selectedJob && actions) {
    actions.appendChild(jobActionButton(selectedJob));
    if (isVisibilityMeasurementJob(selectedJob) && !["queued", "running", "finalizing", "stopping"].includes(selectedJob.status) && failureReadouts.get(selectedJob.id)?.host?.finalize_available === true) {
      actions.appendChild(finalizeOutputButton(selectedJob));
    }
    if (!state.testingOnly && isVisibilityMeasurementJob(selectedJob) && selectedJob.status === "succeeded"
      && !isClientResearchJob(selectedJob)
      && !selectedJob.run_config?.visibility_test
      && selectedJob.run_config?.snapshot_selection_summary?.scope_kind !== "diagnostic") actions.appendChild(prepareReportButton(selectedJob));
  }
  if (!visibleJobs.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.className = "muted";
    cell.textContent = historyError ? "Run history unavailable. Use Refresh to retry."
      : !state.historyLoaded.has(state.jobWorkspaceView) ? "Loading run history…"
      : testing ? "No visibility engine runs yet. Set up a run to collect results." : "No automation runs yet.";
    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }
  for (const job of visibleJobs) {
    const row = document.createElement("tr");
    if (state.selectedJob === job.id) row.classList.add("selected");
    row.addEventListener("click", () => {
      void runUiAction("Load job log", () => selectJob(job.id));
    });

    const workflow = document.createElement("td");
    const select = document.createElement("button");
    select.type = "button";
    select.className = "job-select secondary";
    select.textContent = job.label;
    select.dataset.jobControl = `${job.id}:select`;
    select.setAttribute("aria-label", `Select ${job.label}, job ${job.id}`);
    select.setAttribute("aria-pressed", String(state.selectedJob === job.id));
    select.addEventListener("click", (event) => {
      event.stopPropagation();
      void runUiAction("Load job log", () => selectJob(job.id));
    });
    workflow.appendChild(select);
    const areaNames = job.run_config?.snapshot_selection_summary?.area_names;
    if (Array.isArray(areaNames) && areaNames.length) {
      const scope = document.createElement("div");
      scope.className = "muted job-scope-summary";
      scope.textContent = areaNames.join(" · ");
      workflow.appendChild(scope);
    }

    const status = document.createElement("td");
    status.className = `status-${job.status}`;
    status.textContent = isVisibilityMeasurementJob(job) ? measurementStatusLabel(job.status) : job.status;
    status.title = job.status;

    const started = document.createElement("td");
    const startedValue = job.started_at || job.created_at || "";
    started.textContent = jobTimeLabel(startedValue);
    if (startedValue) started.title = startedValue;

    row.append(workflow, status, started);
    tbody.appendChild(row);
  }
  // Restore only a run control removed by this render; never move focus
  // away from a form, evidence dialog, or another workspace.
  if (focusKey && !focused.isConnected) {
    const controls = Array.from(byId("jobsWorkspace").querySelectorAll("[data-job-control]"));
    const replacement = controls.find((node) => node.dataset.jobControl === focusKey && !node.disabled)
      || byId("runHistorySelection");
    replacement?.focus({preventScroll: true});
  }
}

function jobActionButton(job) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary";
  button.dataset.jobControl = `${job.id}:action`;
  button.disabled = state.pendingJobActions.has(job.id);
  if (["queued", "running", "finalizing", "stopping"].includes(job.status)) {
    button.textContent = job.status === "finalizing" ? "Finalizing" : (job.status === "stopping" ? "Stopping" : "Stop");
    button.disabled = button.disabled || ["finalizing", "stopping"].includes(job.status);
    button.classList.add("danger");
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      void runJobAction(job.id, "Stop job", button, () => stopJob(job.id, job.run_config?.runner_execution_id));
    });
  } else {
    const continuingMeasurement = isVisibilityMeasurementJob(job);
    button.textContent = continuingMeasurement ? "Refetch failed and remaining" : "Resume";
    if (continuingMeasurement) button.title = "Automatically refetches failed and remaining measurements for up to 5 rounds, stopping when all are filled. After 5 rounds, resolve remaining problems manually. Closes Chrome and all tabs once, then reopens this run's saved profiles. Measurement runs in the background; automatic Copilot verification may briefly use Chrome and the mouse.";
    const host = failureReadouts.get(job.id)?.host;
    if (continuingMeasurement && host?.refetch_available !== true) {
      button.disabled = true;
      button.title = host?.blocked_reason || (host?.settings_match_current_runtime === false
        ? "This run's frozen execution settings differ from the current settings; the engine refuses Refetch until they match."
        : (host ? "No Refetch is currently available. See failures and remaining work for details." : "Select this job to verify current ownership and recovery availability."));
    }
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      void runJobAction(job.id, continuingMeasurement ? "Refetch failed and remaining" : "Resume job", button, () => continuingMeasurement ? refetchJob(job.id) : resumeJob(job.id));
    });
  }
  return button;
}

function finalizeOutputButton(job) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary";
  button.dataset.jobControl = `${job.id}:finalize`;
  button.textContent = "Finish saved output";
  button.title = "Finalize completed measurements locally. This sends no measurement prompts.";
  button.disabled = state.pendingJobActions.has(job.id);
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    void runJobAction(job.id, "Finish saved output", button, () => finalizeJobOutput(job.id, job.run_config?.runner_execution_id));
  });
  return button;
}

async function runJobAction(jobId, label, button, callback) {
  if (state.pendingJobActions.has(jobId)) return;
  state.pendingJobActions.add(jobId);
  const originalText = button.textContent;
  button.disabled = true;
  const messages = {
    "Stop job": ["Stopping...", "Stop requested"],
    "Refetch failed and remaining": ["Requesting Refetch...", "Automatic Refetch requested (up to 5 rounds)"],
    "Prepare report": ["Preparing report...", "Report prepared for review"],
    "Finish saved output": ["Finalizing...", "Local finalization request recorded"],
    "Resume job": ["Resuming...", "Job resumed"],
  };
  const [pendingMessage, completedMessage] = messages[label] || [`${label}...`, `${label} completed`];
  button.textContent = pendingMessage;
  setActionStatus(`${label}...`);
  try {
    await callback();
    setActionStatus(completedMessage, "success", 4500);
  } catch (error) {
    reportActionError(label, error);
  } finally {
    state.pendingJobActions.delete(jobId);
    renderJobs();
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

async function stopJob(jobId, executionId) {
  await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/stop`, {
    method: "POST", body: JSON.stringify(executionId ? {execution_id: executionId} : {}),
  });
  await refresh();
}

async function resumeJob(jobId) {
  const payload = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/resume`, { method: "POST", body: "{}" });
  await refresh();
  navigateTo(isVisibilityMeasurementJob(payload.job) ? "testing" : "jobs");
  await selectJob(payload.job.id);
}

const pendingRefetchRequests = new Map();
const pendingFinalizeRequests = new Map();
const failureReadouts = new Map();
const failureReadoutViews = new Map();
const verificationControlRequests = new Map();
let failureReadoutRequestId = 0;

function pendingFinalizeRequest(jobId) {
  if (pendingFinalizeRequests.has(jobId)) return pendingFinalizeRequests.get(jobId);
  try {
    const request = JSON.parse(sessionStorage.getItem(`visibility-finalize:${jobId}`) || "null");
    if (request && typeof request.request_id === "string" && typeof request.expected_execution_id === "string") {
      pendingFinalizeRequests.set(jobId, request);
      return request;
    }
  } catch (_) { /* Retain in-memory identity when storage is unavailable. */ }
  return null;
}

function rememberFinalizeRequest(jobId, request) {
  if (request) pendingFinalizeRequests.set(jobId, request);
  else pendingFinalizeRequests.delete(jobId);
  try {
    if (request) sessionStorage.setItem(`visibility-finalize:${jobId}`, JSON.stringify(request));
    else sessionStorage.removeItem(`visibility-finalize:${jobId}`);
  } catch (_) { /* Storage failure never authorizes a replacement request. */ }
}

async function finalizeJobOutput(jobId, executionId) {
  let request = pendingFinalizeRequest(jobId);
  if (!request) {
    if (!executionId) throw new Error("Select the job again to verify its execution before finalizing");
    request = {request_id: crypto.randomUUID(), expected_execution_id: executionId};
    rememberFinalizeRequest(jobId, request);
  }
  try {
    await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/finalize`, {method: "POST", body: JSON.stringify(request)});
  } catch (error) {
    if (error.status === 409) rememberFinalizeRequest(jobId, null);
    throw error;
  }
  rememberFinalizeRequest(jobId, null);
  failureReadouts.delete(jobId);
  await refresh();
  await refreshFailureReadout(jobId);
}

function pendingRefetchRequest(jobId) {
  if (pendingRefetchRequests.has(jobId)) return pendingRefetchRequests.get(jobId);
  try {
    const request = JSON.parse(sessionStorage.getItem(`visibility-refetch:${jobId}`) || "null");
    if (request && typeof request === "object" && !Array.isArray(request) && request.run_id === jobId && typeof request.request_id === "string") {
      pendingRefetchRequests.set(jobId, request);
      return request;
    }
  } catch (_) { /* Browser storage may be unavailable. Keep in-memory delivery. */ }
  return null;
}

function rememberRefetchRequest(jobId, request) {
  if (request) pendingRefetchRequests.set(jobId, request);
  else pendingRefetchRequests.delete(jobId);
  try {
    if (request) sessionStorage.setItem(`visibility-refetch:${jobId}`, JSON.stringify(request));
    else sessionStorage.removeItem(`visibility-refetch:${jobId}`);
  } catch (_) { /* In-memory identity still survives ordinary network failures. */ }
}

async function refetchJob(jobId) {
  let request = pendingRefetchRequest(jobId);
  if (!request) {
    const requestId = crypto.randomUUID();
    const payload = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/refetch-preview`, {
      method: "POST", body: JSON.stringify({ request_id: requestId }),
    });
    request = payload.preview;
    rememberRefetchRequest(jobId, request);
  }
  // Re-delivery retains this request identity. A distinct completed action
  // gets a new preview and finite pass on the next click.
  let payload;
  try {
    payload = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/refetch`, {
      method: "POST", body: JSON.stringify({ request }),
    });
  } catch (error) {
    // A definite stale/blocked rejection permits a new user click to obtain
    // fresh scope. A lost connection retains the same delivery identity.
    if (error.status === 409) rememberRefetchRequest(jobId, null);
    throw error;
  }
  rememberRefetchRequest(jobId, null);
  failureReadouts.delete(jobId);
  await refresh();
  await refreshFailureReadout(jobId);
}

async function refreshFailureReadout(jobId) {
  if (state.selectedJob !== jobId) return;
  const requestId = ++failureReadoutRequestId;
  const panel = byId("measurementFailureReadout");
  if (!panel) return;
  const job = state.jobs.find((item) => item.id === jobId);
  panel.hidden = !job || !isVisibilityMeasurementJob(job);
  if (panel.hidden) return;
  const content = byId("measurementFailureContent");
  if (!failureReadouts.has(jobId) || content.dataset?.jobId !== jobId) {
    clear(content);
    content.textContent = `Loading measurement results for ${jobId}…`;
  }
  if (content.dataset) content.dataset.jobId = jobId;
  try {
    const payload = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/failures`);
    if (state.selectedJob !== jobId || requestId !== failureReadoutRequestId) return;
    failureReadouts.set(jobId, payload.readout);
    const focused = typeof document !== "undefined" ? document.activeElement : null;
    const focusKey = content.contains?.(focused) ? focused.getAttribute("data-readout-key") : null;
    const selection = focusKey === "search" ? [focused.selectionStart, focused.selectionEnd] : null;
    clear(content);
    renderFailureReadout(content, payload.readout, jobId);
    if (focusKey) {
      const replacement = Array.from(content.querySelectorAll("[data-readout-key]")).find((node) => node.getAttribute("data-readout-key") === focusKey);
      replacement?.focus({preventScroll: true});
      if (selection && replacement) replacement.setSelectionRange(...selection);
    }
    renderJobs();
  } catch (error) {
    if (state.selectedJob !== jobId || requestId !== failureReadoutRequestId) return;
    failureReadouts.delete(jobId);
    renderJobs();
    clear(content);
    content.textContent = error.readoutMessage || "Failure readout unavailable. Ownership and recovery must be verified before execution.";
  }
}

function renderFailureReadout(container, readout, jobId) {
  // The public engine projection is authoritative. Render typed safe fields;
  // never insert HTML or raw diagnostics from an exception/evidence payload.
  const fields = new Set(["run_id", "scope_digest", "revision", "revision_digest", "workflow_version", "policy_version", "sampled_at", "state", "status", "reason", "reason_code", "blocked_reason", "work_item_id", "item_id", "operation_id", "adapter_id", "outcome", "certainty", "action", "next_action", "allowed_action", "remaining", "consumed", "authorized", "attempt_id", "pass_id", "execution_id", "count", "label", "code", "result_available", "worker_state", "ownership_state", "refetch_available", "finalize_available", "kind", "recorded_at", "captured_at", "recovered_at", "external_outcome_unknown", "additional_attempt_allowance", "additional_effect_allowance", "lifetime_attempts", "lifetime_effects", "unknown_effects", "authorized_attempts", "authorized_effects", "remaining_attempts", "remaining_effects", "max_attempts", "max_effects", "lifetime_ordinal", "pass_ordinal", "effect_id", "ownership_status", "resource_id", "adapter_version", "parser_version", "validator_version", "deadline_seconds", "deadline_at", "input_digest", "output_contract", "heartbeat_at", "progress_at", "resource_scope", "compatibility_id", "frozen", "policy_sha256", "matches_current_runtime", "mismatch", "descriptor_limit", "settings_match_current_runtime"]);
  const sections = new Set(["summary", "host", "worker", "ownership", "resources", "items", "failures", "operations", "attempts", "effects", "history", "next_actions", "allowances", "budgets", "counts", "versions", "work", "actions", "accounting", "preview", "selections", "passes", "implementations", "authorized_cost", "reserved_cost", "actual_cost", "unknown_cost", "execution_policy"]);
  const arrays = new Set(["unresolved_attempt_ids", "checked_attempt_ids", "replacement_attempt_ids", "dependency_ids", "historical_unresolved_attempt_ids", "actionable_unresolved_attempt_ids", "resource_scopes"]);
  for (const field of ["reconciliation_attempt_id", "reconciliation_pass_id", "original_attempt_token", "revision_id", "local_only", "ownership_release_available"]) fields.add(field);
  for (const field of ["stage", "source_saved", "source_method", "github_account_id", "model", "source_sha256"]) fields.add(field);
  arrays.add("replaces_attempt_ids");
  const view = failureReadoutViews.get(jobId) || {query: "", openItems: new Set()};
  if (!view.openingItems) view.openingItems = new Set();
  failureReadoutViews.set(jobId, view);
  const humanize = (value) => String(value || "").replaceAll("_", " ");
  function safeCapabilityDetail(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    if (!["runtime_tool_catalog", "session_tools"].includes(value.stage)
        || !["missing_required_tools", "unexpected_tools", "metadata_malformed", "metadata_rpc_failed"].includes(value.reason)) return null;
    const detail = {stage: value.stage, reason: value.reason};
    const key = {missing_required_tools: "missing_tools", unexpected_tools: "unexpected_tools"}[value.reason];
    if (key) {
      const names = value[key];
      if (!Array.isArray(names) || !names.length || names.length > 100
          || names.some((name) => typeof name !== "string" || !/^[a-z][a-z0-9_.-]{0,63}$/.test(name)
            || /^(?:gh[pousr]_|github_pat_)/.test(name)
            || (key === "missing_tools" && !["web_search", "web_fetch"].includes(name)))) return null;
      detail[key] = [...new Set(names)];
    }
    return detail;
  }
  function capabilityMessage(item) {
    if (item.reason_code !== "copilot_source_capability_unavailable") return "";
    const capability = safeCapabilityDetail(item.capability_detail);
    if (capability?.reason === "missing_required_tools") {
      return `GitHub Copilot could not start: the runtime is missing required tools (${capability.missing_tools.join(", ")}). No source prompt was sent.`;
    }
    return "GitHub Copilot could not start because source capability checks failed. No source prompt was sent.";
  }
  const sourceSaved = (item) => typeof item.source_saved === "boolean" ? item.source_saved : item.context?.source_saved === true;
  const platformName = (value) => ({chatgpt: "GPT", gemini: "Gemini", github_copilot: "GitHub Copilot (SDK)", copilot: "Copilot", google_ai_overview: "Google AI"}[value] || humanize(value) || "Measurement");
  function disclosure(node, label, populate, className = "", itemId = null) {
    const section = document.createElement("details");
    section.className = className;
    const title = document.createElement("summary");
    title.textContent = label;
    if (itemId) {
      title.setAttribute("data-readout-key", itemId);
      // Native toggle events are deferred. Record the intended state on the
      // click so a concurrent readout refresh cannot lose this interaction.
      title.addEventListener("click", () => {
        if (section.open) view.openItems.delete(itemId);
        else view.openItems.add(itemId);
      });
    }
    section.appendChild(title);
    let populated = false;
    function toggle() {
      if (itemId) {
        if (section.open) view.openItems.add(itemId);
        else view.openItems.delete(itemId);
      }
      if (!section.open || populated) return;
      populated = true;
      populate(section);
    }
    section.addEventListener("toggle", toggle);
    if (itemId && view.openItems.has(itemId)) {
      section.open = true;
      toggle();
    }
    node.appendChild(section);
    return section;
  }
  function evidenceLinks(references, node) {
    for (const reference of references) {
      if (typeof reference !== "string" || !/^(evidence|artifacts|conversations)\//.test(reference)) continue;
      const row = document.createElement("div");
      row.className = "recovery-evidence";
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = reference.startsWith("conversations/") ? "Open saved conversation" : "Read saved evidence";
      const output = document.createElement("div");
      output.setAttribute("aria-live", "polite");
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const result = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/recovery-link`, {method: "POST", body: JSON.stringify({reference})});
          clear(output);
          if (result.link.kind === "conversation") {
            const url = new URL(result.link.url);
            if (url.protocol !== "https:" || url.username || url.password || !["chatgpt.com", "gemini.google.com", "copilot.com", "copilot.microsoft.com"].includes(url.hostname)) throw new Error("Unrecognized conversation link");
            // noopener can return null even when the tab opened. Keep a real
            // user-activated link in either case, without claiming success.
            const note = document.createElement("p");
            note.textContent = "If a new tab did not open, use or copy this saved conversation link:";
            const fallback = document.createElement("a");
            fallback.href = url.href;
            fallback.target = "_blank";
            fallback.rel = "noopener noreferrer";
            fallback.textContent = url.href;
            output.appendChild(note);
            output.appendChild(fallback);
            try { window.open(url.href, "_blank", "noopener,noreferrer"); }
            catch (_) { /* The visible link remains usable if popups are disabled. */ }
          } else if (result.link.kind === "artifact") {
            const evidence = document.createElement("pre");
            evidence.textContent = result.link.text;
            output.appendChild(evidence);
          } else throw new Error("Unrecognized recovery evidence");
        } catch (error) { reportActionError("Open recovery evidence", error); }
        finally { button.disabled = false; }
      });
      row.appendChild(button);
      row.appendChild(output);
      node.appendChild(row);
    }
  }
  function add(value, node, depth = 0) {
    if (!value || typeof value !== "object" || depth > 7) return;
    for (const [key, item] of Object.entries(value)) {
      if ((fields.has(key) || (sections.has(key) && typeof item === "number")) && ["string", "number", "boolean"].includes(typeof item)) {
        const line = document.createElement("p");
        line.textContent = `${key.replaceAll("_", " ")}: ${String(item)}`;
        node.appendChild(line);
      } else if (arrays.has(key) && Array.isArray(item)) {
        const line = document.createElement("p");
        line.textContent = `${key.replaceAll("_", " ")}: ${item.filter((entry) => typeof entry === "string").join(", ") || "none"}`;
        node.appendChild(line);
      } else if (key === "evidence_links" && Array.isArray(item)) {
        evidenceLinks(item, node);
      } else if (key === "capability_detail") {
        const capability = safeCapabilityDetail(item);
        if (!capability) continue;
        const line = document.createElement("p");
        const tools = capability.missing_tools || capability.unexpected_tools;
        line.textContent = `Capability check: ${capability.stage}; ${capability.reason}${tools ? `; tools: ${tools.join(", ")}` : ""}`;
        node.appendChild(line);
      } else if (sections.has(key) && item && typeof item === "object") {
        disclosure(node, `${humanize(key)}${Array.isArray(item) ? ` (${item.length})` : ""}`, (section) => {
          if (["authorized_cost", "reserved_cost", "actual_cost", "unknown_cost"].includes(key)) {
            for (const [dimension, amount] of Object.entries(item)) {
              if (!/^[a-zA-Z0-9_.-]{1,64}$/.test(dimension) || typeof amount !== "number" || !Number.isFinite(amount)) continue;
              const line = document.createElement("p");
              line.textContent = `${dimension}: ${amount}`;
              section.appendChild(line);
            }
          } else if (Array.isArray(item)) {
            item.forEach((entry, index) => disclosure(section,
              `${index + 1}. ${humanize(entry.kind || entry.action || entry.status || key)}${entry.item_id ? ` · ${entry.item_id}` : ""}`,
              (body) => add(entry, body, depth + 1)));
          } else add(item, section, depth + 1);
        });
      }
    }
  }
  const items = Array.isArray(readout.items) ? readout.items : [];
  // Use authoritative outcome status. A blocked item can have next_action=retain;
  // that means no recovery was offered, not that the measurement succeeded.
  const groupFor = (item) => item.status === "succeeded" ? "successful"
    : ["failed", "blocked", "cancelled"].includes(item.status) ? "failed" : "pending";
  const statusName = (value) => ({succeeded: "Successful", failed: "Failed", blocked: "Blocked — review needed",
    cancelled: "Cancelled", active: "In progress", never_started: "Not started"}[value] || humanize(value) || "Status unavailable");
  const actionName = (item) => item.status === "succeeded" ? "Saved result retained"
    : item.context?.platform === "github_copilot" && sourceSaved(item)
      && ["execute", "replace"].includes(item.next_action) ? "Retry parsing the saved answer"
    : ({execute: "Eligible for refetch", reconcile: "Check the previous attempt", reconcile_or_replace: "Check the previous attempt",
      replace: "Eligible for refetch", wait: "Wait for the current attempt", retain: "No refetch currently available"}[item.next_action] || humanize(item.next_action));
  // Older readouts without a plan ordinal still get a stable number within the
  // complete item set, before status grouping or search.
  const fallbackOrdinals = new Map();
  const platformCounts = new Map();
  for (const item of [...items].sort((a, b) => String(a.item_id).localeCompare(String(b.item_id)))) {
    const platform = item.context?.platform || "unknown";
    const next = (platformCounts.get(platform) || 0) + 1;
    platformCounts.set(platform, next);
    fallbackOrdinals.set(item.item_id, next);
  }
  const itemLabel = (item) => `${platformName(item.context?.platform)} ${item.context?.platform_ordinal || fallbackOrdinals.get(item.item_id)}`;
  const totals = {failed: 0, successful: 0, pending: 0};
  for (const item of items) totals[groupFor(item)] += 1;
  const overview = document.createElement("p");
  overview.className = "measurement-overview";
  overview.textContent = items.length
    ? `${items.length} measurements · ${totals.successful} successful · ${totals.failed} failed${totals.pending ? ` · ${totals.pending} in progress or waiting` : ""}`
    : "No measurement outcomes are available yet.";
  container.appendChild(overview);
  const autoRecovery = readout.host?.auto_recovery;
  if (autoRecovery && Number.isInteger(autoRecovery.round) && Number.isInteger(autoRecovery.limit)
      && autoRecovery.round >= 0 && autoRecovery.limit > 0 && autoRecovery.round <= autoRecovery.limit
      && ["running", "completed", "exhausted", "cancelled", "blocked", "error"].includes(autoRecovery.status)) {
    const notice = document.createElement("p");
    notice.className = "measurement-notice measurement-auto-recovery";
    notice.setAttribute("role", "status");
    const outcome = {
      running: "Retrying failed and remaining measurements; saved results are retained.",
      completed: "All measurements filled.",
      exhausted: "Resolve the remaining problems manually before requesting another Refetch.",
      cancelled: "Automatic recovery cancelled. Saved results are retained.",
      blocked: "Automatic recovery stopped. Review the remaining problems.",
      error: "Automatic recovery stopped. Review the remaining problems.",
    }[autoRecovery.status];
    notice.textContent = `Automatic recovery · round ${autoRecovery.round}/${autoRecovery.limit}. ${outcome}`;
    container.appendChild(notice);
  }
  if (Array.isArray(readout.verification) && readout.verification.length) {
    const panel = document.createElement("section");
    panel.className = "measurement-verification";
    panel.setAttribute("aria-label", "Copilot verification");
    const heading = document.createElement("h3");
    heading.textContent = readout.verification.every(episode => episode.verification_cleared === true)
      ? "Copilot blocker cleared; tab retained" : "Copilot tab retained";
    panel.appendChild(heading);
    const instruction = document.createElement("p");
    instruction.textContent = "This saved blocker may already be gone. Check the held tab to inspect its current state; leave the prompt unchanged. Supported sign-in steps run in the background; verification briefly uses Chrome and the mouse. An expired operation requires explicit recovery even after the blocker clears.";
    panel.appendChild(instruction);
    for (const episode of readout.verification) {
      const row = document.createElement("div");
      row.className = "measurement-verification-row";
      const label = document.createElement("p");
      const expired = episode.deadline_expired || episode.status === "expired_held";
      const time = expired ? "Deadline expired; tab retained for inspection and recovery"
        : typeof episode.remaining_seconds === "number" ? `${Math.ceil(episode.remaining_seconds)} seconds remain; tab retained`
        : "Tab retained; deadline cannot be confirmed";
      const dispatch = episode.dispatch_state === "not_admitted" ? "Dispatch has not been admitted"
        : "Send outcome must be reconciled";
      const blocker = {verification: "Human verification", sign_in: "Sign-in", cookies: "Cookie notice", other: "Unrecognized blocker"}[episode.blocker_kind || "verification"];
      const commands = Array.isArray(episode.commands) ? [...episode.commands].sort((a, b) =>
        String(a.requested_at || "").localeCompare(String(b.requested_at || ""))) : [];
      const lastCheck = commands.filter(command => command.action === "check" && command.status === "completed").at(-1);
      const presence = episode.verification_cleared === true ? "Last check found no blocker; tab remains held for explicit recovery"
        : lastCheck ? "Last check still found a blocker" : "Current blocker has not been rechecked";
      label.textContent = `${episode.profile_id} · Last detected: ${blocker} · turn ${episode.turn || "?"} · ${humanize(episode.phase)} · ${time}. ${presence}. ${dispatch}.${episode.reason ? ` Last stop: ${humanize(episode.reason)}.` : ""}`;
      row.appendChild(label);
      const result = document.createElement("p");
      result.setAttribute("aria-live", "polite");
      const latest = commands.at(-1);
      if (latest) result.textContent = latest.status === "queued" ? "Verification action queued for the worker."
        : latest.status === "refused" ? "The action was refused. Refresh and check ownership."
        : latest.action === "check" ? (latest.verification_cleared ? "Last check found no blocker. An expired operation still requires explicit recovery." : "The last access check found a blocker.")
        : latest.reason === "target_absent" ? "The original tab is already closed."
        : latest.action === "show" ? "The held tab was shown." : "The held tab was closed.";
      for (const [action, title] of [["show", "Show held tab"], ["check", "Check held tab"], ["close", "Close held tab"]]) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary";
        button.textContent = title;
        button.disabled = !episode.actions?.includes(action);
        if (action === "show") button.title = "Your click focuses only this held Copilot tab.";
        if (action === "close") button.title = "Closes this tab after pending actions settle; cancels its active observation.";
        const controlKey = JSON.stringify([jobId, episode.episode_id, episode.revision, episode.execution_id, action]);
        if (!verificationControlRequests.has(controlKey)) {
          if (verificationControlRequests.size >= 256) verificationControlRequests.delete(verificationControlRequests.keys().next().value);
          verificationControlRequests.set(controlKey, crypto.randomUUID());
        }
        const controlRequestId = verificationControlRequests.get(controlKey);
        button.addEventListener("click", async () => {
          if (state.selectedJob !== jobId) return;
          button.disabled = true;
          result.textContent = "Requesting verification action…";
          try {
            const payload = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/verification-control`, {
              method: "POST", body: JSON.stringify({action, episode_id: episode.episode_id,
                revision: episode.revision, execution_id: episode.execution_id, request_id: controlRequestId})});
            if (payload.outcome?.status !== "queued") verificationControlRequests.delete(controlKey);
            if (payload.outcome?.status === "refused") throw new Error("The action was refused. Refresh and check ownership.");
            result.textContent = payload.outcome?.status === "queued" ? "Verification action queued for the worker." : "Verification action completed.";
            await refreshFailureReadout(jobId);
          } catch (error) {
            result.textContent = error.message || "Verification changed. Refresh before acting.";
            setActionStatus(result.textContent, "error");
            button.disabled = false;
            await refreshFailureReadout(jobId);
          }
        });
        row.appendChild(button);
      }
      row.appendChild(result);
      panel.appendChild(row);
    }
    container.appendChild(panel);
  }
  if (readout.host?.startup_error) {
    const notice = document.createElement("p");
    notice.className = "error";
    notice.textContent = readout.host.startup_error;
    container.appendChild(notice);
  }
  if (readout.host?.automatic_stop?.reason_code === "operation_deadline_exceeded") {
    const notice = document.createElement("p");
    notice.className = "measurement-notice measurement-automatic-stop";
    notice.setAttribute("role", "status");
    const stopped = ["finished", "stopped"].includes(readout.host.worker_state);
    notice.textContent = `${stopped ? "This attempt stopped automatically" : "An automatic stop was requested"} because a browser operation exceeded its deadline. Saved results are retained. Review the failed measurements before refetching.`;
    container.appendChild(notice);
  }
  if (readout.host?.blocked_reason) {
    const notice = document.createElement("p");
    notice.className = "measurement-notice";
    notice.textContent = `Refetch unavailable: ${humanize(readout.host.blocked_reason)}.`;
    container.appendChild(notice);
  }
  if (items.length) {
    const controls = document.createElement("div");
    controls.className = "recovery-filters";
    const searchLabel = document.createElement("label");
    searchLabel.textContent = "Search measurements";
    const search = document.createElement("input");
    search.type = "search";
    search.className = "measurement-search";
    search.placeholder = "Platform, area, treatment, profile, or error";
    search.value = view.query;
    search.setAttribute("data-readout-key", "search");
    searchLabel.appendChild(search);
    controls.appendChild(searchLabel);
    container.appendChild(controls);
    const count = document.createElement("p");
    count.className = "measurement-search-count muted";
    count.setAttribute("aria-live", "polite");
    container.appendChild(count);
    const groups = document.createElement("div");
    groups.className = "measurement-result-groups";
    container.appendChild(groups);
    const descriptions = {
      failed: ["Failed", "Failed, blocked, or cancelled. Open a row to see what happened."],
      successful: ["Successful", "Saved results. These are kept when you refetch."],
      pending: ["In progress & waiting", "Still running or not yet completed."],
    };
    function renderItem(item, node) {
      const context = item.context || {};
      const navigation = item.navigation || {};
      const hasChat = navigation.kind === "conversation";
      const record = document.createElement("div");
      record.className = `measurement-record${hasChat ? " has-chat" : ""}`;
      node.appendChild(record);
      disclosure(record, itemLabel(item), (body) => {
        const detail = document.createElement("div");
        detail.className = "measurement-item-details";
        body.appendChild(detail);
        const facts = document.createElement("dl");
        facts.className = "measurement-context";
        function fact(label, value) {
          if (value === undefined || value === null || value === "") return;
          const key = document.createElement("dt");
          const text = document.createElement("dd");
          key.textContent = label;
          text.textContent = String(value);
          facts.appendChild(key);
          facts.appendChild(text);
        }
        fact("Area", context.area_name || "Area context unavailable");
        if (context.subject_kind === "clinic_archetype") {
          fact("Search category", context.clinic_archetype_plural || context.subject_name || "General clinic search");
        } else {
          fact("Treatment", context.treatment_name || context.subject_name || "Treatment context unavailable");
        }
        fact("Search level", {local: "Local area", greater_area: "Wider area", city: "City"}[context.geography_tier] || humanize(context.geography_tier));
        if (Array.isArray(context.origin_area_names) && (context.origin_area_names.length > 1 || context.origin_area_names[0] !== context.area_name)) {
          fact("Selected areas", context.origin_area_names.filter((value) => typeof value === "string").join(" · "));
        }
        fact("Repetition", context.run_ordinal);
        fact(context.platform === "github_copilot" ? "Gemini parser profile" : "Profile", context.profile_id);
        if (context.platform === "github_copilot") {
          fact("GitHub account", context.github_account_id || item.github_account_id);
          fact("Method", context.source_method || item.source_method);
          fact("Stage", context.stage || item.stage);
        }
        fact("Status", statusName(item.status));
        fact("Next step", actionName(item));
        const limit = item.usage_limit;
        if (item.limit_reason === "usage_limit" && limit && (limit.scope === "text" || limit.scope === "unknown") && limit.blocking === true && limit.text_allowed !== true) {
          fact("Usage limit", limit.scope === "text" ? "Text responses unavailable" : "Provider usage limit");
          fact("Provider reset time", typeof limit.reset_display === "string" && limit.reset_display.length <= 100 ? `${limit.reset_display} (timezone not stated)` : "Not stated by provider");
        }
        detail.appendChild(facts);
        if (item.reason_code) {
          const reason = document.createElement("p");
          reason.className = "measurement-outcome";
          reason.textContent = `Reason: ${capabilityMessage(item) || humanize(item.reason_code)}`;
          detail.appendChild(reason);
        }
        disclosure(detail, "Search prompt", (promptBody) => {
          const prompt = document.createElement("pre");
          prompt.className = "recovery-prompt";
          prompt.textContent = typeof context.prompt_text === "string" ? context.prompt_text : "Prompt context unavailable";
          promptBody.appendChild(prompt);
        }, "measurement-prompt-detail", `${item.item_id}:prompt`);
        disclosure(detail, "Attempt details", (attemptBody) => {
          for (const key of ["prompt_id", "semantic_prompt_id", "semantic_slot_id", "packet_id"]) {
            if (typeof context[key] !== "string") continue;
            const line = document.createElement("p");
            line.textContent = `${humanize(key)}: ${context[key]}`;
            attemptBody.appendChild(line);
          }
          add(item, attemptBody);
        }, "measurement-diagnostics", `${item.item_id}:attempts`);
      }, "recovery-observation", item.item_id);
      if (context.platform === "github_copilot") {
        const saved = document.createElement("div");
        saved.className = "measurement-record-actions";
        const note = document.createElement("p");
        const hasEvidence = Array.isArray(item.evidence_links) && item.evidence_links.some(
          (reference) => typeof reference === "string" && /^(evidence|artifacts|conversations)\//.test(reference));
        note.textContent = sourceSaved(item)
          ? `SDK answer saved.${hasEvidence ? " Read the available source and parser evidence below." : " No evidence link is available."}`
          : `No SDK answer was saved.${hasEvidence ? " Available diagnostic evidence is listed below." : ""}`;
        saved.appendChild(note);
        evidenceLinks(item.evidence_links || [], saved);
        record.appendChild(saved);
        return;
      }
      const actions = document.createElement("div");
      actions.className = "measurement-record-actions";
      actions.setAttribute("role", "group");
      actions.setAttribute("aria-label", `${itemLabel(item)} chat actions`);
      if (!hasChat) {
        const unavailable = document.createElement("span");
        unavailable.className = "measurement-chat-unavailable";
        unavailable.textContent = "Chat not available";
        actions.appendChild(unavailable);
      }
      const open = document.createElement("button");
      open.type = "button";
      open.className = "secondary measurement-open-record";
      open.textContent = hasChat ? "Open chat" : "Open profile & platform";
      open.disabled = navigation.available !== true || view.openingItems.has(item.item_id);
      open.setAttribute("data-readout-key", `${item.item_id}:open-record`);
      open.setAttribute("data-record-open", item.item_id);
      open.setAttribute("data-record-job", jobId);
      open.setAttribute("data-open-available", String(navigation.available === true));
      open.title = navigation.reason || `Open ${hasChat ? "saved chat" : platformName(navigation.platform || context.platform)} in ${navigation.profile_id || context.profile_id || "the assigned profile"}`;
      open.addEventListener("click", async () => {
        if (navigation.available !== true || view.openingItems.has(item.item_id)) return;
        view.openingItems.add(item.item_id);
        open.disabled = true;
        try {
          const result = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/open-record`, {
            method: "POST", body: JSON.stringify({item_id: item.item_id}),
          });
          const opened = result.navigation;
          setActionStatus(`Opened ${opened.kind === "conversation" ? "chat" : platformName(opened.platform)} in ${opened.profile_id}`, "success", 5000);
        } catch (error) {
          reportActionError("Open measurement", error);
        } finally {
          view.openingItems.delete(item.item_id);
          open.disabled = navigation.available !== true;
          // A polling refresh can replace the clicked button while Chrome opens.
          for (const current of container.querySelectorAll?.("[data-record-open]") || []) {
            if (current.getAttribute("data-record-job") === jobId && current.getAttribute("data-record-open") === item.item_id) {
              current.disabled = current.getAttribute("data-open-available") !== "true";
            }
          }
        }
      });
      actions.appendChild(open);
      record.appendChild(actions);
      if (navigation.available !== true) {
        const reason = document.createElement("p");
        reason.className = "measurement-navigation-reason";
        reason.textContent = navigation.reason || "Profile and platform unavailable";
        record.appendChild(reason);
      }
    }
    function renderItems() {
      clear(groups);
      const query = search.value.trim().toLocaleLowerCase();
      const matches = items.filter((item) => [itemLabel(item), item.item_id, item.status, item.reason_code, item.next_action,
        ...Object.values(item.context || {}).flatMap((value) => Array.isArray(value) ? value : [value])]
        .filter((value) => typeof value === "string" || typeof value === "number").join(" ").toLocaleLowerCase().includes(query));
      count.textContent = query ? `${matches.length} of ${items.length} measurements match` : "";
      count.hidden = !query;
      for (const group of ["failed", "successful", "pending"]) {
        if (group === "pending" && !totals.pending) continue;
        const entries = matches.filter((item) => groupFor(item) === group).sort((a, b) =>
          String(a.context?.platform || "").localeCompare(String(b.context?.platform || ""))
          || (a.context?.platform_ordinal || fallbackOrdinals.get(a.item_id)) - (b.context?.platform_ordinal || fallbackOrdinals.get(b.item_id)));
        const section = document.createElement("section");
        section.className = `measurement-result-group is-${group}`;
        section.setAttribute("aria-label", descriptions[group][0]);
        const header = document.createElement("header");
        const title = document.createElement("h4");
        title.textContent = descriptions[group][0];
        const badge = document.createElement("span");
        badge.className = "measurement-group-count";
        badge.textContent = query ? `${entries.length} / ${totals[group]}` : String(totals[group]);
        header.appendChild(title);
        header.appendChild(badge);
        section.appendChild(header);
        const description = document.createElement("p");
        description.className = "measurement-group-description";
        description.textContent = descriptions[group][1];
        section.appendChild(description);
        const rows = document.createElement("div");
        rows.className = "measurement-group-list";
        section.appendChild(rows);
        groups.appendChild(section);
        if (entries.length) {
          entries.forEach((item) => renderItem(item, rows));
        } else {
          const empty = document.createElement("p");
          empty.className = "measurement-group-empty muted";
          empty.textContent = query ? "No matches in this section." : group === "successful" ? "No successful measurements yet." : "No failed measurements.";
          rows.appendChild(empty);
        }
      }
    }
    search.addEventListener("input", () => {
      view.query = search.value;
      renderItems();
    });
    renderItems();
  }
  const {items: _items, ...metadata} = readout;
  disclosure(container, "Technical details", (body) => add(metadata, body), "measurement-technical", "technical");

  renderOwnershipRelease(container, readout, jobId);
}

function renderOwnershipRelease(container, readout, jobId) {
  // Eligibility comes from the host's current engine ownership projection.
  // Unknown ownership alone also covers unrelated integrity failures.
  const ownershipState = readout?.host?.ownership_state ?? readout?.worker?.ownership_status;
  const workerState = readout?.host?.worker_state;
  const ownership = readout?.host?.ownership || {};
  if (readout?.host?.ownership_release_available !== true || ownershipState !== "unknown"
      || ["launching", "running", "unresponsive"].includes(workerState)
      || ownership.status === "alive" || ownership.state === "owned" || readout?.worker?.status === "alive"
      || readout?.host?.settings_match_current_runtime === false) return;
  const section = document.createElement("details");
  section.open = true;
  const title = document.createElement("summary");
  title.textContent = "blocked ownership";
  section.appendChild(title);
  const note = document.createElement("p");
  const explanations = {
    owner_record_corrupt: "The saved ownership record is damaged or unreadable.",
    process_identity_invalid: "The saved process identity is invalid.",
    process_identity_incomplete: "The saved process identity is incomplete.",
    boot_identity_unavailable: "The machine boot identity could not be verified.",
    process_inspection_unavailable: "The operating system could not verify the saved process identity.",
  };
  note.textContent = `${explanations[ownership.reason_code] || "The saved owner could not be verified."} Release it only if you know that worker is gone; the engine rechecks ownership and archives the old record with your reason.`;
  section.appendChild(note);
  const reason = document.createElement("input");
  reason.type = "text";
  reason.setAttribute("aria-label", "Reason for releasing blocked ownership");
  reason.placeholder = "Reason (8 to 500 characters), e.g. machine rebooted 2026-09-15, worker gone";
  reason.maxLength = 500;
  reason.style.width = "100%";
  section.appendChild(reason);
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "Release blocked ownership";
  button.addEventListener("click", async () => {
    if (reason.value.trim().length < 8) { setActionStatus("A reason of at least 8 characters is required", "error", 4500); return; }
    button.disabled = true;
    try {
      const result = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/release-ownership`, {method: "POST", body: JSON.stringify({reason: reason.value.trim()})});
      await refreshFailureReadout(jobId);
      const available = failureReadouts.get(jobId)?.host?.ownership_state === "available";
      if (available) setActionStatus("Ownership is available; refresh verified the engine's current state", "success", 6000);
      else if (result.outcome?.released) setActionStatus("The engine recorded the release, but ownership is still blocked or could not be verified. See the failure readout.", "error", 8000);
      else setActionStatus(`Nothing released: ${String(result.outcome?.reason_code || "unchanged")}`, "error", 6000);
    } catch (error) { reportActionError("Release blocked ownership", error); }
    finally { button.disabled = false; }
  });
  section.appendChild(button);
  container.appendChild(section);
}

async function selectJob(jobId) {
  const job = state.jobs.find((item) => item.id === jobId);
  if (!job) return;
  const view = isVisibilityMeasurementJob(job) ? "testing" : "jobs";
  if (state.activeView !== view) navigateTo(view);
  state.selectedJob = jobId;
  state.selectedJobs[view] = jobId;
  state.selectedJobSignature = undefined;
  refreshSelectedReadoutIfChanged();
  renderJobs();
  const picker = byId("runHistoryPicker");
  if (picker) {
    picker.open = false;
    byId("runHistorySelection")?.focus({preventScroll: true});
  }
  await Promise.all([refreshLog(), refreshFailureReadout(jobId)]);
}

document.addEventListener("client-research:open-run", event => {
  const jobId = event.detail?.jobId;
  if (typeof jobId !== "string" || !jobId) return;
  void runUiAction("Open research run", async () => {
    navigateTo("testing");
    const previousSelection = state.selectedJob;
    await refresh();
    if (state.activeView !== "testing" || state.selectedJob !== previousSelection) return;
    const job = state.jobs.find(item => item.id === jobId);
    if (!job || !isVisibilityMeasurementJob(job)) throw new Error("This research run is unavailable. Refresh its check progress and try again.");
    await selectJob(jobId);
  });
});

async function refreshLog() {
  const label = byId("selectedJob");
  const view = byId("logView");
  if (!state.selectedJob) {
    state.logRequestId += 1;
    label.textContent = "None selected";
    view.textContent = "";
    state.displayedLogJob = null;
    view.setAttribute("aria-busy", "false");
    return;
  }
  const jobId = state.selectedJob;
  const requestId = (state.logRequestId += 1);
  label.textContent = jobId;
  if (state.displayedLogJob !== jobId) {
    view.textContent = `Loading log for ${jobId}…`;
    state.displayedLogJob = null;
  }
  view.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/log`);
    const text = await response.text();
    if (requestId !== state.logRequestId || state.selectedJob !== jobId) return;
    if (!response.ok) throw new Error(text || `Log request failed (${response.status})`);
    view.textContent = text;
    state.displayedLogJob = jobId;
    view.scrollTop = view.scrollHeight;
  } catch (error) {
    if (requestId !== state.logRequestId || state.selectedJob !== jobId) return;
    view.textContent = `Could not load log for ${jobId}: ${error.message || error}`;
    state.displayedLogJob = null;
  } finally {
    if (requestId === state.logRequestId && state.selectedJob === jobId) view.setAttribute("aria-busy", "false");
  }
}

async function loadRunHistory(view, refreshId = null) {
  const requestId = ++state.historyRequestIds[view];
  try {
    const payload = await requestJson(`/api/jobs?scope=${view === "testing" ? "visibility" : "automation"}`);
    if (requestId !== state.historyRequestIds[view] || (refreshId !== null && refreshId !== state.refreshRequestId)) return false;
    for (const job of payload.jobs || []) state.savedJobHistory.set(job.id, job);
    state.historyLoaded.add(view);
    delete state.historyErrors[view];
    return true;
  } catch (error) {
    if (requestId !== state.historyRequestIds[view] || (refreshId !== null && refreshId !== state.refreshRequestId)) return false;
    state.historyErrors[view] = `Could not load full run history: ${error.message || error}. Use Refresh to retry.`;
    return false;
  }
}

function jobTimeLabel(value) {
  // Compact local date+time for the jobs table. Falls back to the raw ISO string (never a guess)
  // if the value cannot be parsed; the full ISO is preserved in the cell's title for precision.
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}
