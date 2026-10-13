// OS operations only. Provider protocol and send decisions stay in worker.mjs.
import { closeSync, fsyncSync, openSync, renameSync } from "node:fs";
import { dirname } from "node:path";
import { spawnSync } from "node:child_process";

function windowsOperation(operation, ...arguments_) {
  const python = process.env.VISIBILITY_PLATFORM_PYTHON;
  const bridge = process.env.VISIBILITY_PLATFORM_BRIDGE;
  if (!python || !bridge) throw new Error("copilot_platform_bridge_unavailable");
  // Both paths are frozen by the Python owner, never resolved from credentials
  // or supplied as protocol parameters. Failure cannot authorize a send.
  const result = spawnSync(python, [bridge, operation, ...arguments_], {
    stdio: "ignore", timeout: 15000, windowsHide: true,
  });
  if (result.error || result.status !== 0) throw new Error("copilot_platform_operation_failed");
}

export function publishComplete(temporary, path) {
  if (process.platform === "win32") {
    windowsOperation("publish", temporary, path);
    return;
  }
  renameSync(temporary, path);
  const parent = openSync(dirname(path), "r");
  try { fsyncSync(parent); } finally { closeSync(parent); }
}

export function terminateOwnedGroup(pid) {
  if (process.platform === "win32") {
    windowsOperation("terminate", String(pid));
    return;
  }
  process.kill(-pid, "SIGKILL");
}
