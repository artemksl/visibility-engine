/* Platform tests use the existing guarded job endpoints and inspector. */
(() => {
  const form = byId("visibilityTestForm");
  if (!form) return;
  const labels = {chatgpt: "ChatGPT", gemini: "Gemini", copilot: "Copilot", "copilot-gemini": "GitHub Copilot + Gemini"};
  const storageKey = "visibility-test-pending-start-v1";
  let pending = null;
  let storageError = "";
  let busy = false;
  let previewBusy = false;
  let previewVersion = 0;
  let repeatCount = "1";
  let resultJob = null;
  let profilesReady = false;

  function status(message, tone = "info") {
    const node = byId("visibilityTestStatus");
    node.textContent = message;
    node.dataset.tone = tone;
  }

  function promptLines() {
    return byId("visibilityTestPrompts").value.split(/\r?\n/).filter((text) => text.trim());
  }

  function promptDescription(body) {
    if (Array.isArray(body.prompts)) {
      const unique = new Set(body.prompts).size;
      return `${body.prompts.length} prompts (${unique} distinct ${unique === 1 ? "text" : "texts"})`;
    }
    const count = body.prompt_count;
    return count === 1 ? "1 prompt" : `${count} repetitions of one prompt`;
  }

  function fields() {
    const platform = byId("visibilityTestPlatform").value;
    if (!Object.hasOwn(labels, platform)) throw new Error("Choose a supported platform.");
    if (!profilesReady) throw new Error("Configured profiles are not available. Reload after checking local settings.");
    const selected_profile_ids = [...form.querySelectorAll('input[name="visibilityTestProfile"]:checked')].map((input) => input.value);
    if (!selected_profile_ids.length) throw new Error("Select at least one profile.");
    if (byId("visibilityTestMode").value === "list") {
      const prompts = promptLines();
      if (!prompts.length) throw new Error("Enter at least one prompt, one per line.");
      return {platform, body: {prompts, selected_profile_ids}};
    }
    const raw = byId("visibilityTestCount").value.trim();
    const count = Number(raw);
    if (!raw || !Number.isSafeInteger(count) || count < 1) {
      throw new Error("Enter a positive whole number of prompts.");
    }
    const prompt = byId("visibilityTestPrompt").value;
    if (!prompt.trim()) throw new Error("Enter a prompt.");
    return {platform, body: {prompt_count: count, prompt, selected_profile_ids}};
  }

  function readPending() {
    const encoded = sessionStorage.getItem(storageKey);
    if (!encoded) return null;
    const value = JSON.parse(encoded);
    if (!value || !Object.hasOwn(labels, value.platform) || !value.body
      || !/^[A-Za-z0-9_-]{8,128}$/.test(value.body.request_id || "")) {
      throw new Error("The saved test request is unreadable. Check run history before replacing it.");
    }
    return value;
  }

  function remember(request) {
    // Persist before sending; a reload must retain the same delivery identity.
    if (request) sessionStorage.setItem(storageKey, JSON.stringify(request));
    else sessionStorage.removeItem(storageKey);
    pending = request;
  }

  function renderPending(message = "") {
    const notice = byId("visibilityTestPending");
    notice.textContent = storageError || message || (pending
      ? `${labels[pending.platform]} test (${promptDescription(pending.body)}): the start outcome is unknown. A job may already be running. Retry the same test to find it; the original settings will be reused.`
      : "");
    notice.hidden = !notice.textContent;
    byId("visibilityTestStart").textContent = busy ? "Starting…" : pending ? "Retry same test" : "Start test";
    byId("visibilityTestStart").disabled = busy || Boolean(storageError) || (!profilesReady && !pending);
    byId("visibilityTestStart").formNoValidate = Boolean(pending);
    byId("visibilityTestPreview").disabled = busy || previewBusy || Boolean(pending) || Boolean(storageError) || !profilesReady;
    form.setAttribute("aria-busy", String(busy));
  }

  function updateFields() {
    const list = byId("visibilityTestMode").value === "list";
    const count = byId("visibilityTestCount");
    if (list && !count.disabled) repeatCount = count.value;
    if (!list && count.disabled) count.value = repeatCount;
    count.disabled = list;
    if (list) count.value = String(promptLines().length);
    byId("visibilityTestRepeatFields").hidden = list;
    byId("visibilityTestListFields").hidden = !list;
    byId("visibilityTestCountLabel").textContent = list ? "Number of prompts" : "Repetitions of this prompt";
    byId("visibilityTestCountHelp").textContent = list
      ? "Each listed prompt starts one fresh conversation, distributed across the selected profiles. Repeated lines run separately; retries may add requests."
      : "The same prompt runs once per repetition, across the selected profiles. Parallel tabs can show the same prompt. Choose Different prompts to supply a list.";
    const platform = byId("visibilityTestPlatform").value;
    byId("visibilityTestPlatformHelp").textContent = platform === "copilot-gemini"
      ? "GitHub Copilot answers, then Gemini extracts clinic names from the saved answer. Uses your configured GitHub Copilot account and source settings."
      : `${labels[platform] || "The platform"} answers and extracts clinic names in the same conversation.`;
  }

  function invalidatePreview() {
    previewVersion += 1;
    previewBusy = false;
    byId("visibilityTestSummary").hidden = true;
    byId("visibilityTestSummary").textContent = "";
    if (!pending && !busy && !storageError) status("");
    updateFields();
    renderPending();
  }

  async function preview() {
    if (busy || pending || storageError) return;
    const version = ++previewVersion;
    let request;
    try {
      request = fields();
    } catch (error) {
      status(error.message, "error");
      return;
    }
    previewBusy = true;
    renderPending();
    status("Checking test settings…");
    try {
      const result = await requestJson(`/api/testing/visibility/${request.platform}/preview`, {
        method: "POST", body: JSON.stringify({...request.body, request_id: crypto.randomUUID()}),
      });
      if (version !== previewVersion) return;
      const summary = byId("visibilityTestSummary");
      const profileCount = result.selected_profile_ids.length;
      summary.textContent = `${labels[request.platform]} · ${promptDescription(request.body)} · ${result.initial_request_counts.source} answers + ${result.initial_request_counts.extraction} extractions. Distributed across ${profileCount} ${profileCount === 1 ? "profile" : "profiles"}. Retries may add requests.`;
      summary.hidden = false;
      status("Preview ready. No prompts have been sent.");
    } catch (error) {
      if (version === previewVersion) status(error.message, "error");
    } finally {
      if (version === previewVersion) {
        previewBusy = false;
        renderPending();
      }
    }
  }

  async function showResult(job) {
    // Seed the returned identity even if history refresh temporarily fails.
    state.savedJobHistory.set(job.id, job);
    if (!state.jobs.some((item) => item.id === job.id)) state.jobs.unshift(job);
    navigateTo("testing");
    await selectJob(job.id);
  }

  async function start(event) {
    event.preventDefault();
    if (busy || storageError) return;
    let request;
    try {
      request = pending;
      if (!request) {
        const selection = fields();
        request = {platform: selection.platform, body: {...selection.body, request_id: crypto.randomUUID()}};
        remember(request);
      }
    } catch (error) {
      status(error.message, "error");
      return;
    }
    busy = true;
    const selectedBeforeStart = state.selectedJob;
    invalidatePreview();
    renderPending("Starting test. An interrupted response can be retried using this same saved request.");
    status("Starting test…");
    let accepted = false;
    try {
      const response = await requestJson(`/api/testing/visibility/${request.platform}`, {
        method: "POST", body: JSON.stringify(request.body),
      });
      if (!response.job?.id) throw new Error("The service did not return a job identity.");
      resultJob = response.job;
      accepted = true;
      remember(null);
      byId("visibilityTestResult").hidden = false;
      status(`Test started: ${labels[request.platform]} · ${promptDescription(request.body)}.`, "success");
      if (state.activeView === "testing" && state.selectedJob === selectedBeforeStart) {
        await showResult(resultJob);
      }
    } catch (error) {
      if (!accepted && (error.status === 400 || error.start_rejected === true)) {
        try { remember(null); }
        catch (storageFailure) { storageError = `Cannot clear the saved test request: ${storageFailure.message}`; }
        status(error.message, "error");
      } else if (pending) {
        status(`Start outcome is unknown. Use Retry same test. ${error.message}`, "error");
      } else {
        status(`Test was accepted; its results could not be opened. ${error.message}`, "error");
      }
    } finally {
      busy = false;
      renderPending();
    }
  }

  form.addEventListener("submit", (event) => void start(event));
  byId("visibilityTestPreview").addEventListener("click", () => void preview());
  for (const id of ["visibilityTestPlatform", "visibilityTestMode", "visibilityTestCount", "visibilityTestPrompt", "visibilityTestPrompts"]) {
    byId(id).addEventListener("input", invalidatePreview);
    byId(id).addEventListener("change", invalidatePreview);
  }
  byId("visibilityTestProfiles").addEventListener("change", invalidatePreview);
  byId("visibilityTestResult").addEventListener("click", (event) => {
    event.preventDefault();
    if (resultJob) void showResult(resultJob).catch((error) => status(error.message, "error"));
  });
  try { pending = readPending(); }
  catch (error) { storageError = error.message; }
  updateFields();
  renderPending();
  async function loadProfiles() {
    const choices = byId("visibilityTestProfileChoices");
    try {
      const options = await requestJson("/api/testing/visibility");
      const ids = options.profile_ids;
      if (!Array.isArray(ids) || !ids.length || new Set(ids).size !== ids.length
        || ids.some((id) => typeof id !== "string" || !/^[a-z0-9]+(?:_[a-z0-9]+)*$/.test(id))) {
        throw new Error("Configured profile selection is invalid.");
      }
      choices.replaceChildren(...ids.map((id) => {
        const label = document.createElement("label");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.name = "visibilityTestProfile";
        input.value = id;
        input.checked = true;
        label.append(input, document.createTextNode(id));
        return label;
      }));
      profilesReady = true;
    } catch (error) {
      choices.textContent = error.message;
      status("Check config/settings.json and reload to select profiles.", "error");
    } finally {
      renderPending();
    }
  }
  void loadProfiles();
})();
