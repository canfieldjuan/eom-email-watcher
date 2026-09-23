import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  canDecideAutomationFire,
  runAutomationDecision,
  type AutomationDecisionProjection,
} from "../src/automationDecision.ts";

const fire: AutomationDecisionProjection = {
  fire_id: "11111111-1111-4111-8111-111111111111",
  state: "awaiting_confirmation",
  state_version: 4,
  prepared_identity_sha256: "a".repeat(64),
};

test("only prepared awaiting fires offer a decision", () => {
  assert.equal(canDecideAutomationFire(fire), true);
  assert.equal(canDecideAutomationFire({ ...fire, state: "pending_dispatch" }), false);
  assert.equal(canDecideAutomationFire({ ...fire, prepared_identity_sha256: null }), false);
  assert.equal(canDecideAutomationFire({ ...fire, prepared_identity_sha256: "A".repeat(64) }), false);
  assert.equal(canDecideAutomationFire({ ...fire, fire_id: "" }), false);
  assert.equal(canDecideAutomationFire({ ...fire, state_version: 0 }), false);
  assert.equal(canDecideAutomationFire({ ...fire, state_version: Number.MAX_SAFE_INTEGER }), true);
  assert.equal(canDecideAutomationFire({ ...fire, state_version: Number.MAX_SAFE_INTEGER + 1 }), false);
});

for (const [decision, state] of [
  ["confirmed", "pending_dispatch"],
  ["declined", "declined"],
] as const) {
  test(`${decision} forwards exact projected identity and refreshes`, async () => {
    const requests: unknown[] = [];
    let refreshed = 0;
    const outcome = await runAutomationDecision(
      fire,
      decision,
      new Set(),
      async (request) => {
        requests.push(request);
        return { fire_id: fire.fire_id, state, state_version: 5 };
      },
      async () => { refreshed += 1; },
    );
    assert.deepEqual(requests, [{
      fireId: fire.fire_id,
      expectedVersion: fire.state_version,
      preparedIdentitySha256: fire.prepared_identity_sha256,
      decision,
    }]);
    assert.equal(refreshed, 1);
    assert.deepEqual(outcome, {
      status: "submitted",
      result: { fire_id: fire.fire_id, state, state_version: 5 },
      refreshed: true,
    });
  });
}

test("stale identity error refreshes and never reports submission", async () => {
  const stale = { code: "stale_automation_fire", message: "Prepared identity changed" };
  let refreshed = 0;
  const outcome = await runAutomationDecision(
    fire,
    "confirmed",
    new Set(),
    async () => { throw stale; },
    async () => { refreshed += 1; },
  );
  assert.equal(refreshed, 1);
  assert.deepEqual(outcome, { status: "rejected", error: stale, refreshed: true });
});

test("refresh failure preserves the decision result and requires a fresh inbox", async () => {
  const refreshError = new Error("inbox unavailable");
  const outcome = await runAutomationDecision(
    fire,
    "confirmed",
    new Set(),
    async () => ({ fire_id: fire.fire_id, state: "pending_dispatch", state_version: 5 }),
    async () => { throw refreshError; },
  );
  assert.deepEqual(outcome, {
    status: "submitted",
    result: { fire_id: fire.fire_id, state: "pending_dispatch", state_version: 5 },
    refreshed: false,
    refreshError,
  });
});

test("a second click while the first is pending cannot submit twice", async () => {
  let release!: (value: { fire_id: string; state: string; state_version: number }) => void;
  const pending = new Promise<{ fire_id: string; state: string; state_version: number }>((resolve) => {
    release = resolve;
  });
  const inFlight = new Set<string>();
  let submissions = 0;
  const submit = async () => { submissions += 1; return pending; };
  const first = runAutomationDecision(fire, "confirmed", inFlight, submit, async () => {});
  const second = await runAutomationDecision(fire, "confirmed", inFlight, submit, async () => {});
  assert.deepEqual(second, { status: "ignored" });
  assert.equal(submissions, 1);
  release({ fire_id: fire.fire_id, state: "pending_dispatch", state_version: 5 });
  assert.equal((await first).status, "submitted");
  assert.equal(inFlight.size, 0);
});

test("inbox renders attachment-scoped decisions through the admitted Tauri command", async () => {
  const ui = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
  const lib = await readFile(new URL("../src-tauri/src/lib.rs", import.meta.url), "utf8");
  assert.match(ui, /for \(const fire of attachment\.automation_fires \?\? \[\]\)/);
  assert.match(ui, /if \(!canDecideAutomationFire\(fire\)\) continue/);
  assert.match(ui, /runAutomationDecision\(\s*fire,/);
  assert.match(ui, /invoke<AutomationDecisionResult>\("automation_fire_decide", \{ \.\.\.request \}\)/);
  assert.match(ui, /await loadInbox\(\)/);
  assert.match(ui, /if \(!outcome\.refreshed\) \{[\s\S]*?confirm\.disabled = true;[\s\S]*?decline\.disabled = true;/);
  assert.match(ui, /retryRefresh\.textContent = "Refresh inbox"/);
  assert.match(ui, /decisions\.replaceChildren\(retryRefresh\)/);
  assert.match(lib, /async fn automation_fire_decide\([\s\S]*?admission\.require_admitted\(\)\?/);
  assert.match(lib, /\.invoke_handler\(tauri::generate_handler!\[[\s\S]*?automation_fire_decide,/);
});
