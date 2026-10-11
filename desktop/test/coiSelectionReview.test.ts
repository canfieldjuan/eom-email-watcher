import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { stripTypeScriptTypes } from "node:module";
import * as rules from "../src/coiRules.ts";

// Execute the production mount/render path with only the DOM operations it uses.
function element() {
  return { value: "", textContent: "", hidden: false, disabled: false, checked: false,
    dataset: {}, options: [] as any[], listeners: new Map<string, Function>(),
    append(item: any) { this.options.push(item); },
    replaceChildren() { this.options = []; },
    addEventListener(event: string, callback: Function) { this.listeners.set(event, callback); },
  };
}
const source = readFileSync(new URL("../src/coiSetup.ts", import.meta.url), "utf8");
const compiled = stripTypeScriptTypes(source).replace(/import[\s\S]*?from "[^"\n]+";/g, "")
  .replace("export function mountCoiSetup", "exports.mountCoiSetup = function mountCoiSetup");

function mount(invoke: (command: string, args?: any) => Promise<any>) {
  const elements = new Map<string, ReturnType<typeof element>>();
  const root = { set innerHTML(html: string) {
    for (const [match] of html.matchAll(/data-(?:field|action)="[^"]+"/g)) elements.set(`[${match}]`, element());
    elements.set("fieldset", element()); elements.set("form", element());
  }, querySelector(selector: string) { return elements.get(selector) ?? null; } };
  const exports: any = {};
  runInNewContext(compiled, { exports, ...rules, document: { createElement: element } });
  const mounted = exports.mountCoiSetup(root, invoke, String);
  return { elements, mounted };
}

test("saved rule review renders the frozen selection even when not editable", async () => {
  const selection = { protocol_version: 2, acknowledged_manifest_features: ["fixed-parameters-v1"],
    provider: { app_id: "fixture", name: "Fixture", version: "1.0", instance_id: "saved" },
    capability: { id: "document.translate", version: "1.0", parameters: [
      { name: "empty", value_type: "string", required: true, fixed_value: "" },
      { name: "zero", value_type: "integer", required: true, fixed_value: 0 },
      { name: "off", value_type: "boolean", required: true, fixed_value: false },
      { name: "<img src=x onerror=boom>", value_type: "string", required: true, fixed_value: "<script>boom</script>" },
      { name: "editable", value_type: "string", required: false },
    ] },
  };
  for (const snapshot of [selection, null, undefined]) {
    const definition = { opaque: "not editable" };
    const original = JSON.stringify(definition);
    const rule = { summary: { rule_id: "saved", version: 1, enabled: false, system: false,
      valid: true, name: "Saved", invalid_reason: null }, definition,
      ...(snapshot === undefined ? {} : { selection: snapshot }) };
    const invoke = async (command: string) => {
      switch (command) {
        case "mail_accounts_list": return { accounts: [] };
        case "watchlist_list": return [];
        case "connect_catalog": return { items: [], diagnostic: null };
        case "automation_rules_list": return { revision: 1, rules: [rule.summary] };
        case "connect_entitlement_status": return { automations_active: false };
        case "automation_rules_get": return { rule };
        default: throw new Error(command);
      }
    };
    const { elements, mounted } = mount(invoke);
    elements.get('[data-field="saved"]')!.value = "saved";
    await mounted.refresh();
    const review = elements.get('[data-field="selection"]');
    assert.ok(review, "saved selection review element is missing");
    assert.equal(review.hidden, false);
    if (snapshot) {
      for (const text of ["fixed-parameters-v1", 'empty (string): ""', "zero (integer): 0", "off (boolean): false", "<script>boom</script>"])
        assert.ok(review.textContent.includes(text), `missing ${text}`);
      assert.equal(review.textContent.includes("editable (string)"), false);
    } else assert.match(review.textContent, /Legacy\/none/);
    assert.equal(JSON.stringify(rule.definition), original);
    assert.equal(elements.get("fieldset")!.disabled, true);
  }
});


test("required fixed COI parameters survive provider listing and the live pre-save recheck", async () => {
  for (const fixed_value of ["", 0, false]) for (const liveState of ["fixed", "editable", "mixed-fixed-first", "mixed-editable-first"]) {
    const provider = { app_id: "invoice-processor", name: "Fixture", version: "1.0", instance_id: "saved" };
    const parameter = { name: "pin", value_type: typeof fixed_value === "boolean" ? "boolean" : typeof fixed_value === "number" ? "integer" : "string", required: true, fixed_value };
    const selected = { protocol_version: 2, provider, acknowledged_manifest_features: ["fixed-parameters-v1"],
      capability: { id: "certificate.extract", version: "1.0", parameters: [parameter],
        accepts: [{ media_type: "application/pdf", max_bytes: 1024 }],
        produces: ["application/vnd.local-connect.certificate+json"],
        effects: { external: false, confirmation_required: false } } };
    const calls: string[] = [];
    let catalogs = 0;
    const invoke = async (command: string, args?: any) => {
      calls.push(command);
      switch (command) {
        case "mail_accounts_list": return { accounts: [{ provider: "gmail", account_id: "mailbox", address: "owner@example.com", connected: true, active: true }] };
        case "watchlist_list": return [{ email: "broker@example.com", admission_active: true }];
        case "connect_catalog": {
          const cap = structuredClone(selected);
          const editable = { name: "user", value_type: "string", required: true };
          if (++catalogs === 1) {
            const mixedProviders = [true, false].map((fixedFirst) => ({
              ...structuredClone(selected),
              provider: { ...provider, instance_id: `mixed-${fixedFirst}` },
              capability: { ...selected.capability, parameters: fixedFirst ? [parameter, editable] : [editable, parameter] },
            }));
            return { items: [cap, ...mixedProviders], diagnostic: null };
          }
          if (liveState === "editable") delete (cap.capability.parameters[0] as any).fixed_value;
          if (liveState.startsWith("mixed")) {
            (cap.capability.parameters as any[]) = liveState === "mixed-fixed-first"
              ? [parameter, editable] : [editable, parameter];
          }
          return { items: [cap], diagnostic: null };
        }
        case "automation_rules_list": return { rules: [], revision: 0 };
        case "connect_entitlement_status": return { automations_active: true };
        case "automation_rules_prepare": return { definition: args.definition };
        case "automation_rules_put": return { rule: { summary: { rule_id: "new" }, definition: args.definition, selection: selected } };
        case "automation_rules_get": return { rule: { summary: { rule_id: "new", valid: true, version: 1 }, definition: {}, selection: selected } };
        default: throw new Error(command);
      }
    };
    const { elements, mounted } = mount(invoke);
    await mounted.refresh();
    const providers = elements.get('[data-field="provider"]')!;
    assert.equal(providers.options.length, 2, "only the required fixed provider must be listed; mixed providers must be excluded");
    providers.value = rules.providerKey(provider);
    elements.get('[data-field="mailbox"]')!.value = JSON.stringify(["gmail", "mailbox"]);
    elements.get('[data-field="sender"]')!.value = "broker@example.com";
    elements.get('[data-field="name"]')!.value = "COI";
    elements.get("form")!.listeners.get("input")!();
    elements.get("form")!.listeners.get("submit")!({ preventDefault() {} });
    // Drain the production async mutation, without replacing its selection predicate.
    for (let tick = 0; tick < 10; tick++) await new Promise((resolve) => setImmediate(resolve));
    assert.ok(catalogs >= 2, "live pre-save catalog must be checked");
    assert.equal(calls.includes("automation_rules_put"), liveState === "fixed", `live ${liveState} must use the eligibility owner`);
    if (liveState !== "fixed") assert.match(elements.get('[data-field="status"]')!.textContent, /provider is unavailable/);
  }
});
