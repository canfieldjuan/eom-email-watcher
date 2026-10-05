import assert from "node:assert/strict";
import test from "node:test";
import { CoiRequestGate, coiDefinition, editableCoiFields, isCoiProvider, providerKey } from "../src/coiRules.ts";

const provider = { app_id: "invoice-processor", version: "0.1.0", instance_id: "11111111-1111-4111-8111-111111111111" };
const fields = { name: "COI", mailbox: { provider: "gmail", account_id: "mailbox-a" }, sender: "broker@example.com", subject: "Straße Σ COI", provider, confirmEach: true };
const summary = { rule_id: "22222222-2222-4222-8222-222222222222", version: 7, enabled: true, system: false, valid: true, name: "COI", invalid_reason: null };
const capability = { protocol_version: 2 as const, provider: { ...provider, name: "Invoice Processor" }, capability: { id: "certificate.extract", version: "1.0", action: { label: "Extract", description: "" }, accepts: [{ media_type: "application/pdf", max_bytes: 32 * 1024 * 1024 }], produces: ["application/vnd.local-connect.certificate+json"], parameters: [], effects: { external: false, confirmation_required: false } } };

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
