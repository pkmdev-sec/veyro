import { checker, recordFailure, recordModel } from "./checker.mjs";

export default function create(binding) {
  return async function veyro({ client, directory }) {
    const check = checker(binding);
    let owner;
    let model;
    let agent;
    let latest;
    let busy = false;
    let stopped = false;
    let timer;
    let generation = 0;
    let activeCheck;
    return {
      "experimental.chat.system.transform": async (input, output) => {
        if (binding.codingModel && input.model.providerID === "veyro-local") {
          output.system.push("/no_think");
        }
      },
      "chat.params": async (input, output) => {
        if (!binding.codingModel || input.model.providerID !== "veyro-local") return;
        output.temperature = 0;
        output.options.reasoningEffort = "none";
        recordModel(binding, input.model.providerID, input.model.id, input.model.api.url || binding.codingBaseUrl, input.agent);
      },
      "chat.message": async (input) => {
        if (owner && owner !== input.sessionID) return;
        if (!owner) {
          owner = input.sessionID;
          await check(owner, "start", true);
          timer = setTimeout(() => {
            stopped = true;
            clearTimeout(timer);
            recordFailure(binding, "deadline");
            void client.session.abort({ path: { id: owner }, query: { directory },
              throwOnError: true }).catch(() => recordFailure(binding, "abort_failed"));
          }, Math.max(0, binding.deadline * 1000 - Date.now()));
          timer.unref();
        }
        generation += 1;
        activeCheck?.abort();
        model = input.model;
        agent = input.agent;
      },
      event: async ({ event }) => {
        let checking = false;
        try {
          if (event.type === "message.updated") {
            const info = event.properties.info;
            if (info.sessionID === owner && info.role === "assistant") latest = info;
            return;
          }
          if ((event.type === "session.error" && event.properties.sessionID === owner)
              || (event.type === "session.deleted" && event.properties.info.id === owner)) {
            stopped = true;
            clearTimeout(timer);
            activeCheck?.abort();
            recordFailure(binding, "native_session_error");
            return;
          }
          if (event.type !== "session.idle" || event.properties.sessionID !== owner
              || busy || stopped || !latest?.time?.completed) return;
          if (latest.error) {
            stopped = true;
            clearTimeout(timer);
            recordFailure(binding, "native_assistant_error");
            return;
          }
          busy = true;
          checking = true;
          const turnGeneration = generation;
          activeCheck = new AbortController();
          const result = await check(owner, latest.id, false, activeCheck.signal);
          activeCheck = undefined;
          if (stopped || turnGeneration !== generation) return;
          if (result.action === "continue") {
            await client.session.promptAsync({
              path: { id: owner }, query: { directory },
              body: { agent, model, parts: [{ type: "text", text: result.message }] },
              throwOnError: true,
            });
          } else if (result.action === "complete" || result.action === "blocked") {
            stopped = true;
            clearTimeout(timer);
          }
        } catch {
          stopped = true;
          clearTimeout(timer);
          recordFailure(binding, "native_delivery_error");
        } finally {
          if (checking) busy = false;
        }
      },
    };
  };
}
