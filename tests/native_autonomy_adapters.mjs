import assert from "node:assert/strict";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import createPrime from "../src/veyro/integrations/prime-agent.mjs";
import createOpenCode from "../src/veyro/integrations/opencode.mjs";

const [scenario, config, python, checkerPath] = process.argv.slice(2);
const options = JSON.parse(readFileSync(config, "utf8"));
const binding = { config, python, checker: checkerPath, deadline: options.deadline };
const directory = options.repository;
const delivered = [];
let turn = 0;
let finish;
let primeSignal;

if (scenario === "opencode-local-bounds") {
  Object.assign(binding, { codingModel: "qwen3:14b", maxMessageBytes: 512 });
  const hooks = await createOpenCode(binding)({ directory, client: { session: {} } });
  const system = { system: ["large ".repeat(10000), "<available_skills>unsafe</available_skills>"] };
  await hooks["experimental.chat.system.transform"](
    { model: { providerID: "veyro-local" } }, system);
  assert.equal(system.system.length, 1);
  assert.ok(Buffer.byteLength(system.system[0]) < 2000);
  assert.ok(!system.system[0].includes("available_skills"));
  const first = { info: { role: "user" }, parts: [{ type: "text", text: "original task" }] };
  const recent = { info: { role: "assistant" }, parts: [{ type: "text", text: "recent" }] };
  const messages = { messages: [
    first,
    { info: { role: "assistant" }, parts: [{ type: "text", text: "old".repeat(1000) }] },
    recent,
  ] };
  await hooks["experimental.chat.messages.transform"]({}, messages);
  assert.equal(messages.messages[0], first);
  assert.equal(messages.messages.at(-1), recent);
  assert.ok(Buffer.byteLength(JSON.stringify(messages.messages)) <= binding.maxMessageBytes);
  await assert.rejects(() => hooks["experimental.chat.messages.transform"]({}, {
    messages: [{ info: { role: "user" }, parts: [{ type: "text", text: "x".repeat(1000) }] }],
  }));
  assert.equal(JSON.parse(readFileSync(new URL("adapter-error.json", pathToFileURL(config)))).reason,
    "model_context_limit");
  process.exit(0);
}

if (scenario.startsWith("prime")) {
  const handlers = {};
  let session = "root";
  const abort = new AbortController();
  primeSignal = abort.signal;
  const ctx = {
    sessionManager: { getSessionId: () => session },
    signal: abort.signal,
    ui: { setStatus() {}, notify() {} },
    setTimeout: (fn, ms) => { const timer = setTimeout(fn, ms); timer.unref(); return timer; },
    clearTimeout,
    abort: () => abort.abort(),
  };
  createPrime(binding)({
    on: (name, handler) => { handlers[name] = handler; },
    sendUserMessage: (message) => delivered.push(message),
  });
  await handlers.before_agent_start({}, ctx);
  finish = (id) => handlers.agent_end({ messages: [
    { role: "assistant", stopReason: "stop", timestamp: id },
  ] }, ctx);
  if (scenario === "prime-blocked") {
    options.max_continuations = 1;
    writeFileSync(config, JSON.stringify(options));
    await finish(1);
    await finish(2);
    assert.equal(abort.signal.aborted, true, "blocked work must abort the native loop");
    await finish(3);
    assert.equal(delivered.length, 1, "abort must not produce more continuations");
    process.exit(0);
  }
  if (scenario === "prime-aborted") {
    abort.abort();
    await finish(1);
    assert.equal(delivered.length, 0);
    process.exit(0);
  }
  if (scenario === "prime-switch") {
    const pending = finish(1);
    session = "other";
    await pending;
    assert.equal(delivered.length, 0);
    process.exit(0);
  }
} else {
  const hooks = await createOpenCode(binding)({
    directory,
    client: { session: {
      prompt: async (request) => {
        delivered.push(request.body.parts[0].text);
        return { data: undefined };
      },
      abort: async () => ({}),
    } },
  });
  await hooks["chat.message"]({ sessionID: "root", agent: "build" });
  finish = async (id) => {
    await hooks.event({ event: { type: "message.updated", properties: { info: {
      sessionID: "root", role: "assistant", id: String(id), time: { completed: 1 },
    } } } });
    await hooks.event({ event: { type: "session.idle", properties: { sessionID: "root" } } });
  };
  if (scenario === "opencode-cancel") {
    const pending = finish(1);
    await delay(10);
    await hooks.event({ event: { type: "session.error", properties: { sessionID: "root" } } });
    await pending;
    assert.equal(delivered.length, 0);
    process.exit(0);
  }
  if (scenario === "opencode-busy") {
    const pending = finish(1);
    await delay(10);
    await hooks.event({ event: { type: "session.idle", properties: { sessionID: "foreign" } } });
    await hooks.event({ event: { type: "session.idle", properties: { sessionID: "root" } } });
    await pending;
    assert.equal(delivered.length, 0);
    process.exit(0);
  }
}

await finish(++turn);
const expectedDeliveries = scenario.startsWith("prime") ? 1 : 0;
assert.equal(delivered.length, expectedDeliveries,
  "only in-process adapters deliver their own repair turn");
await finish(turn);
assert.equal(delivered.length, expectedDeliveries, "duplicates must not deliver");
writeFileSync(join(directory, "done"), "ok");
await finish(++turn);
assert.equal(delivered.length, expectedDeliveries, "passing checks must not start more work");
const state = JSON.parse(readFileSync(new URL("state.json", pathToFileURL(config)), "utf8"));
assert.equal(state.phase, "blocked");
assert.ok(!existsSync(join(directory, "adapter-error.json")));

if (scenario === "prime-success") {
  assert.equal(primeSignal.aborted, true, "review boundary must stop native background work");
}
