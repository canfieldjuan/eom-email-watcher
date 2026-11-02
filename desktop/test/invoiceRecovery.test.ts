import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import test from "node:test";
import vm from "node:vm";
import { parseAst } from "vite";
import * as availability from "../src/connectAvailability.ts";
import { activeCapabilityResult, capabilityInvocationKey, matchingCapabilityResult } from "../src/connectIdentity.ts";
import { hasInboxAutomationConfirmation, inboxActionState } from "../src/inboxActionState.ts";

// Execute production owners, including their nested click callbacks. The DOM/IPC
// fixture proves rendered text and actions, not installed IPC or browser layout.
const source = stripTypeScriptTypes(readFileSync(new URL("../src/main.ts", import.meta.url), "utf8"));
const ast = parseAst(source);
const names = ["attachmentKey", "capabilityOutputKey", "clearMessageOwnedUiState", "capabilityGroups",
  "renderCapabilityResult", "renderInbox", "loadAttachmentCapabilities", "loadInboxPage",
  "errorCode", "errorMessage", "errorRetryable"];
const code = names.map(name => {
  const node = ast.body.find(node => node.type === "FunctionDeclaration" && node.id?.name === name);
  assert.ok(node, name);
  return source.slice(node.start, node.end);
}).join("\n");

class Element {
  tag: string;
  children: Element[] = [];
  dataset = {};
  attributes = new Map();
  listeners = new Map();
  textContent = "";
  className = "";
  disabled = false;
  hidden = false;
  parent: Element | null = null;
  constructor(tag = "div") { this.tag = tag; }
  append(...children: Element[]) {
    for (const child of children) {
      if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
      child.parent = this;
      this.children.push(child);
    }
  }
  replaceChildren(...children: Element[]) {
    for (const child of this.children) child.parent = null;
    this.children = [];
    this.append(...children);
  }
  get childNodes() { return this.children; }
  get childElementCount() { return this.children.length; }
  setAttribute(key: string, value: string) { this.attributes.set(key, value); }
  removeAttribute(key: string) { this.attributes.delete(key); }
  addEventListener(name: string, callback: () => unknown) { this.listeners.set(name, callback); }
  async click() { assert.equal(this.disabled, false); await this.listeners.get("click")?.(); }
}

function nodes(root: Element): Element[] { return [root, ...root.children.flatMap(nodes)]; }
function rendered(root: Element): string { return nodes(root).map(node => node.textContent).join("\n"); }
const capability = (version: string, confirmation = false) => ({
  protocol_version: 2,
  provider: { app_id: "invoice-processor", version: "1.0.0", instance_id: "fixture-provider", name: "Invoice Processor" },
  capability: { id: "invoice.extract", version, action: { label: `Read invoice ${version}`, description: "Synthetic extraction" },
    parameters: [], effects: { external: false, confirmation_required: confirmation } },
});
const recovery = "Saved selection invoice.extract 1.0 is unavailable. Choose an available action to make a new selection.";

function harness(refusal: { code: string | null; message: string; retryable?: boolean } = {
  code: "capability_unavailable", message: "Opaque fixture reason",
}) {
  const old = capability("1.0");
  const current = capability("2.0", true);
  let catalog = [old];
  const invocations: Record<string, any>[] = [];
  const confirmations: string[] = [];
  let allowConfirmation = true;
  let invocationHold: Promise<void> | null = null;
  const item = { message_id: "message-1", provider: "fixture", account_id: "fixture",
    sender: "sender@example.test", received_at: "2026-11-02T00:00:00Z", subject: "Synthetic invoice",
    admission: null, action_required: null, calendar_proposal: null, status: "notified",
    attachments: [{ part_id: "part-1", filename: "synthetic.pdf", byte_size: 4, media_type: "application/pdf",
      capability_results: [] as any[], automation_fires: [] }] };
  const context = vm.createContext({
    ...availability, activeCapabilityResult, capabilityInvocationKey, matchingCapabilityResult,
    hasInboxAutomationConfirmation, inboxActionState, crypto,
    document: { createElement: (tag: string) => new Element(tag) },
    inboxItems: [item], inboxList: new Element("ul"), inboxStatus: new Element(), inboxLoadMore: new Element(),
    inboxProposalExpiryTimer: null, expandedInboxMessages: new Set([item.message_id]),
    attachmentCapabilities: new Map(), attachmentCapabilityDiagnostics: new Map(),
    attachmentSelectionRefusals: new Map(), attachmentInvocationsInFlight: new Set(), attachmentRequestIds: new Map(),
    capabilityOutputPresentations: new Map(), capabilityOutputPresentationsInFlight: new Set(),
    capabilityOutputPreviews: new Map(), capabilityOutputViewButtons: new Map(),
    inboxDeletionsInFlight: new Set(), inboxMutationInFlight: () => false,
    inboxReloadBlocked: () => false,
    activeInboxQuery: { provider: "fixture", account_id: "fixture", limit: 25 },
    inboxPageState: "loaded", inboxPageStateGeneration: 0, inboxQueryEpoch: 0, inboxRequestGeneration: 0,
    inboxItemsEpoch: 0, inboxNextCursor: null, inboxCapabilityUnavailableCount: 0,
    mailboxEffectScopeIsCurrent: () => true, mailboxEffectRequestIsCurrent: () => true,
    refreshInboxRemovalStatuses: async () => {}, setInboxControlsBusy: () => {},
    releaseAutomationRefreshFences: () => false, automationDecisionRefreshRequired: new Map(),
    inboxStatusLabel: () => "Inbox loaded.", receivedLabel: (value: string) => value,
    stateLabel: () => "Notified", inboxBodyTruncation: () => null,
    collectCapabilityParameters: () => ({}),
    confirmAction: async (message: string) => { confirmations.push(message); return allowConfirmation; },
    invoke: async (operation: string, args: Record<string, any>) => {
      if (operation === "inbox_query") return { items: [item], next_cursor: null };
      if (operation === "attachment_capabilities") return { items: catalog, diagnostic: null };
      assert.equal(operation, "attachment_capability_invoke");
      invocations.push(structuredClone(args));
      await invocationHold;
      if (args.capability.version === "1.0") throw refusal;
      return { status: "completed", outputs: [], job_id: args.requestId };
    },
  });
  vm.runInContext(code, context);
  context.loadInbox = () => context.loadInboxPage(false);
  const render = () => context.renderInbox(context.inboxItems);
  const refresh = () => context.loadInboxPage(false);
  const button = (label: string) => {
    const found = nodes(context.inboxList).find(node => node.tag === "button" && node.textContent === label);
    assert.ok(found, `missing button ${label}`);
    return found;
  };
  context.attachmentCapabilities.set(context.attachmentKey("message-1", "part-1"), catalog);
  render();
  return { context, item, old, current, invocations, confirmations, render, refresh, button,
    setCatalog: (items: typeof catalog) => { catalog = items; },
    holdInvocation: (promise: Promise<void>) => { invocationHold = promise; },
    setConfirmation: (value: boolean) => { allowConfirmation = value; } };
}

for (const code of ["capability_unavailable", "provider_unavailable"]) {
  test(`refused selection stays visible after catalog refresh: ${code}`, async () => {
    const h = harness({ code, message: "Opaque fixture reason" });
    h.setCatalog([h.current]); // Cutover after rendering, before the user clicks.
    await h.button("Read invoice 1.0").click();
    assert.ok(rendered(h.context.inboxList).includes(recovery), rendered(h.context.inboxList));
    assert.equal(h.invocations.length, 1);
    assert.deepEqual(h.invocations[0].capability, { id: "invoice.extract", version: "1.0" });
    assert.equal(h.context.attachmentInvocationsInFlight.size, 0);
    assert.equal(h.context.attachmentRequestIds.size, 0, "authoritative refusal ends the old UI request");
    for (const catalog of [[], [h.current], [h.old, h.current]]) {
      h.setCatalog(catalog);
      await h.refresh();
      assert.ok(rendered(h.context.inboxList).includes(recovery));
      assert.equal(h.invocations.length, 1, "catalog discovery cannot invoke a replacement");
      assert.equal(nodes(h.context.inboxList).some(node => node.textContent.startsWith("Running ") || node.textContent.startsWith("Resume ")), false);
    }
    h.setCatalog([h.current]);
    await h.refresh();
    h.setConfirmation(false);
    await h.button("Read invoice 2.0").click();
    assert.equal(h.invocations.length, 1, "declined replacement submits nothing");
    h.setConfirmation(true);
    await h.button("Read invoice 2.0").click();
    assert.equal(h.invocations.length, 2);
    assert.deepEqual(h.invocations[1].capability, { id: "invoice.extract", version: "2.0" });
    assert.notEqual(h.invocations[0].requestId, h.invocations[1].requestId);
    assert.equal(h.invocations[1].confirmed, true);
    assert.equal(h.confirmations.length, 2);
  });
}

test("unrelated errors cannot manufacture recovery from free-text messages", async () => {
  for (const code of ["runtime_error", "capability_unavailable_extra", "", null]) {
    const h = harness({ code, message: "invoice.extract 1.0 unavailable; choose 2.0" });
    await h.button("Read invoice 1.0").click();
    await h.refresh();
    assert.equal(rendered(h.context.inboxList).includes(recovery), false);
    assert.equal(h.context.attachmentRequestIds.size, 1, "uncertain errors retain reconciliation identity");
  }
});

test("unsupported selection recovery requires a nonretryable refusal", async () => {
  for (const retryable of [true, false, undefined]) {
    const h = harness({ code: "unsupported_capability", message: "Opaque fixture reason", retryable });
    await h.button("Read invoice 1.0").click();
    await h.refresh();
    assert.equal(rendered(h.context.inboxList).includes(recovery), retryable === false);
    assert.equal(h.context.attachmentRequestIds.size, retryable === false ? 0 : 1);
  }
});

test("catalog refresh during an invocation cannot erase its later refusal or submit new work", async () => {
  const h = harness();
  let release!: () => void;
  h.holdInvocation(new Promise<void>(resolve => { release = resolve; }));
  const invoking = h.button("Read invoice 1.0").click();
  h.setCatalog([h.current]);
  await h.refresh();
  assert.equal(h.invocations.length, 1);
  release();
  await invoking;
  assert.ok(rendered(h.context.inboxList).includes(recovery));
  assert.equal(h.invocations.length, 1);
});

test("refusal belongs to its attachment and clears with message-owned UI state", async () => {
  const h = harness();
  await h.button("Read invoice 1.0").click();
  h.item.attachments.push({ ...h.item.attachments[0], part_id: "part-2" });
  await h.refresh();
  assert.equal(nodes(h.context.inboxList).filter(node => node.textContent === recovery).length, 1);
  h.context.clearMessageOwnedUiState("another-message");
  h.render();
  assert.ok(rendered(h.context.inboxList).includes(recovery));
  h.context.clearMessageOwnedUiState("message-1");
  h.render();
  assert.equal(rendered(h.context.inboxList).includes(recovery), false);
});

test("persisted unsupported legacy result renders its reason and recovery without current discovery", async () => {
  const h = harness();
  h.item.attachments[0].capability_results.push({
    protocol_version: 2, provider: h.old.provider, capability_id: "invoice.extract", capability_version: "1.0",
    job_id: "old-terminal-job", status: "failed", dispatch_state: "terminal", summary: null,
    error: { code: "UNSUPPORTED_CAPABILITY", message: "Original terminal refusal" },
  });
  for (const catalog of [[], [h.current]]) {
    h.setCatalog(catalog);
    await h.refresh();
    assert.ok(rendered(h.context.inboxList).includes("Original terminal refusal"));
    assert.ok(rendered(h.context.inboxList).includes(recovery));
    assert.equal(nodes(h.context.inboxList).some(node => node.textContent.startsWith("Resume ")), false);
    assert.equal(h.invocations.length, 0);
  }
});
