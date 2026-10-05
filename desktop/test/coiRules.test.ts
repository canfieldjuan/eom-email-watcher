import assert from "node:assert/strict";
import test from "node:test";
import { CoiRequestGate, coiDefinition, editableCoiFields, isCoiProvider, providerKey } from "../src/coiRules.ts";
import * as coiRules from "../src/coiRules.ts";
import { readFileSync } from "node:fs";

const provider = { app_id: "invoice-processor", version: "0.1.0", instance_id: "11111111-1111-4111-8111-111111111111" };
const fields = { name: "COI", mailbox: { provider: "gmail", account_id: "mailbox-a" }, sender: "broker@example.com", subject: "Straße Σ COI", provider, confirmEach: true };
const summary = { rule_id: "22222222-2222-4222-8222-222222222222", version: 7, enabled: true, system: false, valid: true, name: "COI", invalid_reason: null };
const capability = { protocol_version: 2 as const, provider: { ...provider, name: "Invoice Processor" }, capability: { id: "certificate.extract", version: "1.0", action: { label: "Extract", description: "" }, accepts: [{ media_type: "application/pdf", max_bytes: 32 * 1024 * 1024 }], produces: ["application/vnd.local-connect.certificate+json"], parameters: [], effects: { external: false, confirmation_required: false } } };

test("entitlement controls new, enabled and paused rules without blocking pause", () => {
  assert.equal(typeof coiRules.coiControlState, "function", "coiControlState export is required");
  for (const selected of [null, summary, { ...summary, enabled: false }]) {
    assert.deepEqual(coiRules.coiControlState(false, selected), {
      saveEnabled: false, toggleEnabled: selected?.enabled === true, locked: true,
    });
    assert.deepEqual(coiRules.coiControlState(true, selected), {
      saveEnabled: true, toggleEnabled: selected !== null, locked: false,
    });
  }
});

const setupSource = readFileSync(new URL("../src/coiSetup.ts", import.meta.url), "utf8");
const mainSource = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");

test("unknown entitlement stays hidden until a reported locked status", () => {
  assert.ok(/let automationsActive: boolean \| null = null;/.test(setupSource), "entitlement state must start as null");
  assert.ok(/locked\.hidden = automationsActive !== false;/.test(setupSource), "locked line requires a reported false status");
});

test("entitlement refresh drives COI controls and the locked View Connect callback", () => {
  assert.match(setupSource, /invoke<\{ automations_active: boolean \}>\("connect_entitlement_status"\)/);
  assert.match(setupSource, /automationsActive = entitlement\.automations_active/);
  assert.match(setupSource, /coiControlState\(automationsActive === true, current\?\.summary \?\? null\)/);
  assert.match(setupSource, /save\.disabled = !controls\.saveEnabled \|\|/);
  assert.match(setupSource, /toggle\.disabled = !controls\.toggleEnabled/);
  assert.match(setupSource, /locked\.hidden = automationsActive !== false/);
  assert.match(setupSource, /Automations locked[\s\S]*?data-action="view-connect">View Connect/);
  assert.match(setupSource, /onViewConnect\?: \(\) => void/);
  assert.match(setupSource, /\[data-action="view-connect"\].*addEventListener\("click", \(\) => onViewConnect\?\.\(\)\)/);
});

test("entitlement refusal refreshes the lock and retains the engine message", () => {
  assert.match(setupSource, /"code" in error && error\.code === "automation_entitlement_required"\) \{\s*await refresh\(\);\s*message\(errorMessage\(error\), true\)/);
});

test("Health and both View Connect routes open fresh Connect health", () => {
  assert.match(mainSource, /function openConnectHealth\(\): void \{\s*showView\("health"\);\s*void Promise\.all\(\[loadHealth\(\), refreshConnectStatus\(\)\]\);\s*connectHealth\.scrollIntoView/);
  assert.match(mainSource, /healthTab\.addEventListener\("click", openConnectHealth\)/);
  assert.match(mainSource, /viewConnect\.addEventListener\("click", openConnectHealth\)/);
  assert.match(mainSource, /mountCoiSetup\([^;]*errorMessage, openConnectHealth\)/);
});

test("active Connect health explains Automations inclusion and keeps inactive copy", () => {
  const render = mainSource.split("function renderConnectStatus(")[1].split("function applyConnectStatus(")[0];
  assert.match(render, /connectDetail\.textContent = detail \+ \(status\.active\s*\? \(status\.automations_active\s*\? " Automations are included\."\s*: " Automations are not included in this license, so rules can't be created or turned on\."\)\s*: ""\)/);
  assert.match(mainSource, /interface ConnectEntitlementStatus \{[^}]*automations_active: boolean/);
});

test("scoped form round trips without losing exact provider, subject or confirmation", () => {
  assert.deepEqual(editableCoiFields({ summary, definition: coiDefinition(fields) }), fields);
  const automatic = { ...fields, subject: "", confirmEach: false };
  assert.deepEqual(editableCoiFields({ summary, definition: coiDefinition(automatic) }), automatic);
  assert.notEqual(providerKey(provider), providerKey({ ...provider, instance_id: "replacement" }));
  assert.notEqual(providerKey(provider), providerKey({ ...provider, version: "0.2.0" }));
});

test("form refuses lossy edits instead of dropping unrepresented predicates and parameters", () => {
  const changes = [
    (d: any) => { d.conditions.push({ field: "priority", op: "equals", value: "high" }); },
    (d: any) => { d.conditions.push(d.conditions[0]); },
    (d: any) => { d.conditions[0].op = "domain_equals"; },
    (d: any) => { d.conditions[1].op = "starts_with"; },
    (d: any) => { d.conditions[1].value = ""; },
    (d: any) => { d.action.parameters = { mode: "extended" }; },
    (d: any) => { d.action.capability.version = "2.0"; },
    (d: any) => { d.action.capability.id = "invoice.extract"; },
    (d: any) => { d.scope = {}; },
    (d: any) => { d.scope.account_id = null; },
    (d: any) => { d.extra = true; },
    (d: any) => { d.conditions[0].extra = true; },
  ];
  for (const change of changes) {
    const d = coiDefinition(fields); change(d);
    assert.equal(editableCoiFields({ summary, definition: d }), null);
  }
  for (const version of [0, -1, false, "", Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(editableCoiFields({ summary: { ...summary, version: version as number }, definition: coiDefinition(fields) }), null);
  }
  for (const override of [{ system: true }, { valid: false }]) {
    assert.equal(editableCoiFields({ summary: { ...summary, ...override }, definition: coiDefinition(fields) }), null);
  }
});

test("catalog selection accepts the actual contract and rejects incompatible alternatives", () => {
  assert.equal(isCoiProvider(capability), true);
  for (const max_bytes of [0, -1, false, "", Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(isCoiProvider({ ...capability, capability: { ...capability.capability, accepts: [{ media_type: "application/pdf", max_bytes: max_bytes as number }] } }), false);
  }
  for (const changes of [
    { id: "document.summarize" }, { version: "2.0" }, { produces: [] },
    { accepts: [{ media_type: "image/png", max_bytes: 100 }] },
    { effects: { external: true, confirmation_required: true } },
    { parameters: [{ name: "required", required: true, value_type: "string" as const, label: "", description: "" }] },
  ]) assert.equal(isCoiProvider({ ...capability, capability: { ...capability.capability, ...changes } }), false);
  assert.equal(isCoiProvider({ ...capability, capability: { ...capability.capability, effects: { external: false, confirmation_required: true } } }), true);
});

test("mutation reserves before awaiting and requires a fresh snapshot after uncertain completion", async () => {
  const gate = new CoiRequestGate();
  let release!: () => void;
  let calls = 0;
  const pending = gate.run(async () => { calls++; await new Promise<void>((resolve) => { release = resolve; }); }, true);
  await assert.rejects(gate.run(async () => { calls++; }, true), /Refresh/);
  assert.equal(calls, 1);
  release(); await pending;
  assert.equal(gate.needsRefresh, true);
  await assert.rejects(gate.run(async () => {}, true), /Refresh/);
  gate.refreshed();
  await assert.rejects(gate.run(async () => { throw new Error("reply lost"); }, true), /reply lost/);
  assert.equal(gate.busy, false);
  assert.equal(gate.needsRefresh, true);
  await gate.run(async () => { gate.refreshed(); });
  await gate.run(async () => { calls++; gate.refreshed(); }, true);
  assert.equal(calls, 2);
});
