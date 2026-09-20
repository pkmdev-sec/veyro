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
      promptAsync: async (request) => {
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
    assert.equal(delivered.length, 1);
    process.exit(0);
  }
}

await finish(++turn);
assert.equal(delivered.length, 1, "plan must start a build turn");
await finish(turn);
assert.equal(delivered.length, 1, "duplicates must not deliver");
await finish(++turn);
assert.equal(delivered.length, 2, "failed checks must start a repair turn");
writeFileSync(join(directory, "done"), "ok");
await finish(++turn);
assert.equal(delivered.length, 2, "passing checks must not start more work");
const state = JSON.parse(readFileSync(new URL("state.json", pathToFileURL(config)), "utf8"));
assert.equal(state.phase, "completed");
assert.ok(!existsSync(join(directory, "adapter-error.json")));

if (scenario === "prime-success") {
  assert.equal(primeSignal.aborted, true, "completion must stop native background work");
}
