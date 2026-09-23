import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  canDecideAutomationFire,
  releaseAutomationRefreshFences,
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

test("decision refresh requires its own committed inbox projection", async () => {
  const ui = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
  const decisionRefresh = ui.slice(
    ui.indexOf("const refreshDecisionInbox = async"),
    ui.indexOf("const decide = async (decision: \"confirmed\""),
  );
  const loadInbox = ui.slice(ui.indexOf("async function loadInbox("), ui.indexOf("async function refreshLoadedInboxSpan("));
  const renderPoint = loadInbox.indexOf("renderInbox(inboxItems);");
  assert.notEqual(renderPoint, -1);
  assert.match(loadInbox, /\): Promise<boolean> \{/);
  assert.match(loadInbox, /if \(!mailboxEffectRequestIsCurrent\(generation, inboxRequestGeneration, effectScope\)\) return false;/);
  assert.match(loadInbox, /renderInbox\(inboxItems\);[\s\S]*return true;/);
  assert.doesNotMatch(loadInbox.slice(0, renderPoint), /\breturn(?:;| true;)/);
  assert.doesNotMatch(loadInbox.slice(renderPoint), /\breturn(?:;| false;)/);
  assert.match(decisionRefresh, /const committed = await loadInbox\(\);[\s\S]*if \(!committed\) \{/);
  assert.doesNotMatch(decisionRefresh, /inboxRequestGeneration/);
});

test("refresh fences require a later full committed query", () => {
  const required = new Map([[fire.fire_id, 5], ["later-fire", 7]]);
  assert.equal(releaseAutomationRefreshFences(required, 4, false), false);
  assert.equal(releaseAutomationRefreshFences(required, 5, false), false);
  assert.equal(releaseAutomationRefreshFences(required, 6, true), false);
  assert.equal(required.size, 2);
  assert.equal(releaseAutomationRefreshFences(required, 6, false), true);
  assert.equal(required.has(fire.fire_id), false);
  assert.equal(required.has("later-fire"), true);
  assert.equal(releaseAutomationRefreshFences(required, 7, false), false);
  assert.equal(releaseAutomationRefreshFences(required, 8, false), true);
  assert.equal(required.size, 0);
});

test("rerendered decision controls stay blocked until a later query commits", async () => {
  const ui = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
  const panel = ui.slice(ui.indexOf("for (const fire of attachment.automation_fires ?? [])"), ui.indexOf("attachments.append(row);"));
  const loadInbox = ui.slice(ui.indexOf("async function loadInbox("), ui.indexOf("async function refreshLoadedInboxSpan("));
  assert.match(ui, /const automationDecisionRefreshRequired = new Map<string, number>\(\)/);
  assert.match(panel, /automationDecisionsInFlight\.has\(fire\.fire_id\)[\s\S]*?confirm\.disabled = true;[\s\S]*?decline\.disabled = true;/);
  assert.match(panel, /automationDecisionRefreshRequired\.has\(fire\.fire_id\)[\s\S]*?showRefreshRetry\(\)/);
  assert.match(panel, /automationDecisionRefreshRequired\.set\(fire\.fire_id, inboxRequestGeneration\);[\s\S]*?renderInbox\(inboxItems\)/);
  assert.match(loadInbox, /renderInbox\(inboxItems\);[\s\S]*?releaseAutomationRefreshFences\(automationDecisionRefreshRequired, generation, append\)/);
});

test("a committed decision refresh reconciles the mounted panel after reservation release", async () => {
  const inFlight = new Set<string>();
  const stale = { code: "stale_automation_fire", message: "Prepared identity changed" };
  let reservedDuringRefresh = false;
  const outcome = await runAutomationDecision(
    fire,
    "confirmed",
    inFlight,
    async () => { throw stale; },
    async () => { reservedDuringRefresh = inFlight.has(fire.fire_id); },
  );
  assert.equal(reservedDuringRefresh, true);
  assert.equal(inFlight.has(fire.fire_id), false);
  assert.deepEqual(outcome, { status: "rejected", error: stale, refreshed: true });

  const ui = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
  const panel = ui.slice(ui.indexOf("for (const fire of attachment.automation_fires ?? [])"), ui.indexOf("attachments.append(row);"));
  assert.match(panel, /if \(!outcome\.refreshed\) \{[\s\S]*?automationDecisionRefreshRequired\.set\(fire\.fire_id, inboxRequestGeneration\);[\s\S]*?\} else \{\s*renderInbox\(inboxItems\);\s*\}/);
  assert.doesNotMatch(panel, /\} else \{\s*confirm\.disabled = false;\s*decline\.disabled = false;\s*\}/);
});

test("a refreshed confirmation does not overwrite current provider status", async () => {
  const ui = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
  const panel = ui.slice(ui.indexOf("for (const fire of attachment.automation_fires ?? [])"), ui.indexOf("attachments.append(row);"));
  assert.doesNotMatch(panel, /The provider action has not completed yet/);
  assert.match(panel, /else if \(outcome\.result\.state === "pending_dispatch"\) \{\s*if \(!outcome\.refreshed\) \{/);
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
  const panel = ui.slice(ui.indexOf("for (const fire of attachment.automation_fires ?? [])"), ui.indexOf("attachments.append(row);"));
  assert.match(panel, /status\.textContent = `\$\{automationOutcomeIdentity\(fire\?\.rule_id, fire\?\.rule_version\)\}: \$\{automationOutcomeStatus\(fire\?\.state\)\}`;[\s\S]*row\.append\(status\);[\s\S]*if \(!fire \|\| !canDecideAutomationFire\(fire\)\) continue;/);
  assert.doesNotMatch(panel, /fire\.(?:reason|job_id)/);
  assert.match(ui, /runAutomationDecision\(\s*fire,/);
  assert.match(ui, /invoke<AutomationDecisionResult>\("automation_fire_decide", \{ \.\.\.request \}\)/);
  assert.match(ui, /await loadInbox\(\)/);
  assert.match(ui, /if \(!outcome\.refreshed\) \{[\s\S]*?confirm\.disabled = true;[\s\S]*?decline\.disabled = true;/);
  assert.match(ui, /retryRefresh\.textContent = "Refresh inbox"/);
  assert.match(ui, /decisions\.replaceChildren\(retryRefresh\)/);
  assert.match(lib, /async fn automation_fire_decide\([\s\S]*?admission\.require_admitted\(\)\?/);
  assert.match(lib, /\.invoke_handler\(tauri::generate_handler!\[[\s\S]*?automation_fire_decide,/);
});
