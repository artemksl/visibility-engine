/* Standalone visibility and client research panel. Opening it only reads saved state. */
const state = {
  testingOnly: true,
  activeView: "testing",
  jobWorkspaceView: "testing",
  jobs: [],
  selectedJob: null,
  selectedJobs: {testing: null},
  savedJobHistory: new Map(),
  historyLoaded: new Set(),
  historyErrors: {},
  historyRequestIds: {testing: 0},
  refreshRequestId: 0,
  logRequestId: 0,
  displayedLogJob: null,
  pendingJobActions: new Set(),
  actionStatusTimer: null,
};

function navigateTo(view = "testing", updateHash = true) {
  state.activeView = "testing";
  if (updateHash && location.hash !== "#testing") location.hash = "#testing";
}

async function refresh() {
  const requestId = ++state.refreshRequestId;
  await loadRunHistory("testing", requestId);
  if (requestId !== state.refreshRequestId) return;
  state.jobs = Array.from(state.savedJobHistory.values()).filter(isVisibilityMeasurementJob)
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  renderJobs();
  refreshSelectedReadoutIfChanged();
  if (state.selectedJob) await refreshLog();
}

async function refreshCurrentView() {
  const button = byId("refresh");
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = "Refreshing…";
  try {
    {
      await refresh();
      if (state.selectedJob) await refreshFailureReadout(state.selectedJob);
    }
  } catch (error) {
    reportActionError("Refresh", error);
  } finally {
    button.disabled = false;
    button.textContent = "Refresh";
  }
}

let statusPollPending = false;
async function pollStatus() {
  if (statusPollPending || state.activeView !== "testing") return;
  statusPollPending = true;
  try {
    await refresh();
    const job = state.jobs.find((item) => item.id === state.selectedJob);
    if (job && ["queued", "running", "finalizing", "stopping"].includes(job.status)
      && Date.now() - (state.lastMeasurementPoll || 0) >= 10000) {
      state.lastMeasurementPoll = Date.now();
      await refreshFailureReadout(job.id);
    }
  } catch (error) {
    reportActionError("Refresh run history", error);
  } finally {
    statusPollPending = false;
  }
}

byId("refresh").addEventListener("click", refreshCurrentView);
window.addEventListener("hashchange", () => navigateTo("testing", false));
navigateTo("testing", false);
void runUiAction("Load run history", refresh);
setInterval(() => {
  if (!document.hidden) void pollStatus();
}, 2500);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) void pollStatus();
});
