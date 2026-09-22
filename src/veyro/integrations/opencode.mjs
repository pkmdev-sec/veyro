import {
  block,
  checker,
  recordContextReduction,
  recordFailure,
  recordModel,
} from "./checker.mjs";

const LOCAL_SYSTEM = `You are Veyro's bounded local coding worker.
Inspect only the current repository. Implement the user's task with the available file and shell tools.
Run the stated verification commands. Fix failures before finishing.
Never weaken checks, edit .veyro state, or claim that an unexecuted command passed.
Keep prose brief. /no_think`;

function byteLength(value) {
  return Buffer.byteLength(JSON.stringify(value));
}

function reduceMessages(messages, limit) {
  const beforeBytes = byteLength(messages);
  if (beforeBytes <= limit) return { beforeBytes, afterBytes: beforeBytes };
  if (messages.length < 2) {
    throw new Error(`local model message exceeds ${limit} byte context budget`);
  }
  const first = messages[0];
  const last = messages.at(-1);
  if (byteLength([first, last]) > limit) {
    throw new Error(`local model message exceeds ${limit} byte context budget`);
  }
  const recent = [last];
  for (let index = messages.length - 2; index > 0; index -= 1) {
    const candidate = [first, messages[index], ...recent];
    if (byteLength(candidate) <= limit) recent.unshift(messages[index]);
  }
  messages.splice(0, messages.length, first, ...recent);
  return { beforeBytes, afterBytes: byteLength(messages) };
}

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
    const modelTurns = new Map();
    return {
      "experimental.chat.system.transform": async (input, output) => {
        if (binding.codingModel && input.model.providerID === "veyro-local") {
          output.system.splice(0, output.system.length, LOCAL_SYSTEM);
        }
      },
      "experimental.chat.messages.transform": async (_input, output) => {
        if (!binding.codingModel) return;
        try {
          const sizes = reduceMessages(output.messages, binding.maxMessageBytes);
          if (sizes.afterBytes < sizes.beforeBytes) {
            recordContextReduction(binding, sizes.beforeBytes, sizes.afterBytes);
          }
        } catch (error) {
          recordFailure(binding, "model_context_limit");
          throw error;
        }
      },
      "chat.params": async (input, output) => {
        if (!binding.codingModel || input.model.providerID !== "veyro-local") return;
        const previous = modelTurns.get(input.sessionID);
        const sameMessage = previous && previous.messageID === input.message?.id;
        const turn = sameMessage ? previous.turn + 1 : 1;
        modelTurns.set(input.sessionID, { messageID: input.message?.id, turn });
        const outputLimit = turn >= (binding.maxModelSteps ?? 8) ? 128 : 1024;
        output.temperature = 0;
        output.maxOutputTokens = Math.min(output.maxOutputTokens ?? outputLimit, outputLimit);
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
            void block(binding, "deadline");
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
            await block(binding, "native_session_error");
            return;
          }
          if (event.type !== "session.idle" || event.properties.sessionID !== owner
              || busy || stopped || !latest?.time?.completed) return;
          if (latest.error) {
            stopped = true;
            clearTimeout(timer);
            recordFailure(binding, "native_assistant_error");
            await block(binding, "native_assistant_error");
            return;
          }
          busy = true;
          checking = true;
          const turnGeneration = generation;
          activeCheck = new AbortController();
          const result = await check(owner, latest.id, false, activeCheck.signal);
          activeCheck = undefined;
          if (stopped || turnGeneration !== generation) return;
          if (result.action === "blocked") {
            stopped = true;
            clearTimeout(timer);
          }
        } catch {
          stopped = true;
          clearTimeout(timer);
          recordFailure(binding, "native_delivery_error");
          await block(binding, "native_delivery_error");
        } finally {
          if (checking) busy = false;
        }
      },
    };
  };
}
