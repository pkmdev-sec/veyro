import { execFile } from "node:child_process";
import { appendFileSync, existsSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { promisify } from "node:util";

const execute = promisify(execFile);

export function recordModel(binding, provider, model, endpoint, purpose) {
  appendFileSync(join(dirname(binding.config), "model-requests.jsonl"),
    JSON.stringify({ timestamp: Date.now(), provider, model, endpoint,
      purpose: typeof purpose === "string" ? purpose : undefined }) + "\n", { mode: 0o600 });
}

export function recordFailure(binding, reason) {
  const path = join(dirname(binding.config), "adapter-error.json");
  if (!existsSync(path)) {
    writeFileSync(path, JSON.stringify({ reason }) + "\n", { mode: 0o600 });
  }
}

export function recordContextReduction(binding, beforeBytes, afterBytes) {
  appendFileSync(join(dirname(binding.config), "context-events.jsonl"),
    JSON.stringify({ timestamp: Date.now(), beforeBytes, afterBytes }) + "\n", { mode: 0o600 });
}

export async function block(binding, reason) {
  try {
    await execute(binding.python, [binding.checker, binding.config, "--block", reason],
      { maxBuffer: 1024 * 1024 });
  } catch {
    recordFailure(binding, "checker_transport");
  }
}


export function checker(binding) {
  return async (session, turn, claim = false, signal) => {
    try {
      const { stdout } = await execute(binding.python, [
        binding.checker, binding.config, session, turn, ...(claim ? ["claim"] : []),
      ], { maxBuffer: 1024 * 1024, signal });
      const result = JSON.parse(stdout);
      if (!["continue", "blocked", "ignore"].includes(result.action)) {
        throw new Error("invalid checkpoint decision");
      }
      if (result.action === "continue" && typeof result.message !== "string") {
        throw new Error("missing continuation message");
      }
      return result;
    } catch (error) {
      recordFailure(binding, "checker_transport");
      return { action: "blocked", reason: "checker_transport", error: String(error) };
    }
  };
}
