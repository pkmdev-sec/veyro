import { checker, recordFailure, recordModel } from "./checker.mjs";

export default function create(binding) {
  return function veyro(api) {
    if (binding.codingModel) {
      api.registerProvider("veyro-local", {
        name: "Veyro local Qwen",
        baseUrl: binding.codingBaseUrl,
        apiKey: "ollama",
        api: "openai-completions",
        models: [{
          id: binding.codingModel,
          name: binding.codingModel,
          reasoning: false,
          input: ["text"],
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          contextWindow: 32768,
          maxTokens: 4096,
          compat: {
            supportsStore: false,
            supportsDeveloperRole: false,
            supportsReasoningEffort: false,
            supportsStrictMode: false,
            maxTokensField: "max_tokens",
          },
        }],
      });
      api.on("before_provider_request", (event, ctx) => {
        if (ctx.model?.provider !== "veyro-local") return;
        recordModel(binding, ctx.model.provider, ctx.model.id, ctx.model.baseUrl);
        const payload = event.payload;
        if (payload && typeof payload === "object" && !Array.isArray(payload)) {
          const messages = Array.isArray(payload.messages) ? payload.messages.map((message) =>
            message.role === "system" && typeof message.content === "string"
              ? { ...message, content: message.content + "\n/no_think" } : message) : payload.messages;
          return { ...payload, ...(messages ? { messages } : {}),
            temperature: 0, reasoning_effort: "none" };
        }
      });
    }
    const check = checker(binding);
    let timer;
    let stopped = false;
    api.on("before_agent_start", async (_event, ctx) => {
      await check(ctx.sessionManager.getSessionId(), "start", true);
      if (!timer && !stopped) {
        timer = ctx.setTimeout(() => {
          stopped = true;
          recordFailure(binding, "deadline");
          ctx.abort();
        }, Math.max(0, binding.deadline * 1000 - Date.now()));
      }
    });
    api.on("agent_end", async (event, ctx) => {
      const last = event.messages.findLast((message) => message.role === "assistant");
      if (stopped || !last) return;
      if (ctx.signal?.aborted) {
        stopped = true;
        ctx.clearTimeout(timer);
        recordFailure(binding, "native_aborted");
        return;
      }
      if (last.stopReason === "error" || last.stopReason === "aborted") {
        stopped = true;
        ctx.clearTimeout(timer);
        recordFailure(binding, `native_${last.stopReason}`);
        return;
      }
      const signal = ctx.signal;
      const session = ctx.sessionManager.getSessionId();
      const result = await check(session, String(last.timestamp), false, signal);
      if (stopped || signal?.aborted || ctx.sessionManager.getSessionId() !== session) return;
      ctx.ui.setStatus("veyro", `Veyro: ${result.reason}`);
      if (result.action === "continue") {
        api.sendUserMessage(result.message, { deliverAs: "followUp" });
      } else if (result.action === "complete" || result.action === "blocked") {
        stopped = true;
        ctx.clearTimeout(timer);
        ctx.abort();
      }
      if (result.action === "blocked") {
        ctx.ui.notify(`Veyro stopped: ${result.reason}`, "warning");
      }
    });
  };
}
