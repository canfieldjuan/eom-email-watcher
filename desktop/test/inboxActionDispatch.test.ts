import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import test, { afterEach } from "node:test";
import { createContext, runInContext } from "node:vm";
import { invoke } from "@tauri-apps/api/core";
import { clearMocks, mockIPC } from "@tauri-apps/api/mocks";
import { parseAst } from "vite";
import * as automationDecision from "../src/automationDecision.ts";
import * as automationOutcome from "../src/automationOutcome.ts";
import * as calendarOutcome from "../src/calendarOutcome.ts";
import { confirmAction } from "../src/confirmation.ts";
import * as connectAvailability from "../src/connectAvailability.ts";
import * as connectIdentity from "../src/connectIdentity.ts";
import { collectCapabilityParameters } from "../src/connectParameters.ts";
import { inboxAdmissionLabel } from "../src/inboxAdmission.ts";
import * as inboxActionState from "../src/inboxActionState.ts";
import { inboxBodyTruncation } from "../src/inboxBodyTruncation.ts";
import { InboxConversationClient } from "../src/inboxConversations.ts";
import { InboxReload } from "../src/inboxReload.ts";
import { InboxRemovalClient, inboxRemovalStatus } from "../src/inboxRemoval.ts";
import { InboxThreadEntry, inboxConversationKey } from "../src/inboxThreadEntry.ts";
import { ThreadHistory } from "../src/threadHistory.ts";
import { ThreadReadClient } from "../src/threadReads.ts";
import { ThreadViewState } from "../src/threadView.ts";
import { Element, nodes, text } from "./threadDomFixture.ts";

const source = stripTypeScriptTypes(readFileSync(new URL("../src/main.ts", import.meta.url), "utf8"));
const ast = parseAst(source);
const owners = [
  "attachmentKey", "capabilityOutputKey", "clearMessageOwnedUiState", "capabilityGroups",
  "errorCode", "errorMessage", "errorRetryable", "stateLabel", "receivedLabel", "renderCapabilityResult",
  "showInboxActionNotice", "beginInboxActionRender", "finishInboxActionRender", "renderInboxReaderAction",
  "loadReaderAttachmentCapabilities", "performInboxReaderAction", "renderInboxMessageDetails",
  "deleteInboxItem", "inboxMutationInFlight", "inboxConversationRow", "loadInboxPage",
  "inboxStatusLabel", "runInboxRefresh", "refreshLoadedInboxSpan",
].map(name => {
  const node = ast.body.find(node => node.type === "FunctionDeclaration" && node.id?.name === name);
  assert.ok(node, name);
  return source.slice(node.start, node.end);
}).join("\n");
const fixture = JSON.parse(readFileSync(new URL("../../tests/data/inbox_thread_action_page.json", import.meta.url), "utf8"));

const fire = {
  fire_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", state: "awaiting_confirmation",
  state_version: 4, rule_id: "sent-only-rule", rule_version: 2, prepared_identity_sha256: "b".repeat(64),
};
const proposal = {
  run_id: "sent-only-run", proposal_sha256: "a".repeat(64), proposal_version: 3, state_version: 5,
  state: "awaiting_confirmation", status: "accepted", subject: "Fixture meeting", attendees: [],
  account_id: "calendar", account_display_name: "Fixture calendar", expires_at: null,
};
const capability = {
  protocol_version: 2,
  provider: { app_id: "fixture-app", version: "1.0", instance_id: "fixture-instance", name: "Fixture provider" },
  capability: {
    id: "invoice.extract", version: "2.0", action: { label: "Read invoice", description: "Fixture extraction" },
    effects: { external: true, confirmation_required: true },
    parameters: [{ name: "include_tax", label: "Include tax", value_type: "boolean", fixed_value: false }],
  },
};
const writeCommands = new Set([
  "inbox_removal_prepare", "inbox_delete", "calendar_proposal_decide",
  "attachment_capability_invoke", "automation_fire_decide",
]);
const dialog = "plugin:dialog|message";
const drain = () => new Promise<void>(resolve => setImmediate(resolve));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

class TestElement extends Element {
  get childElementCount() { return this.children.length; }
  removeAttribute(name: string) { this.attributes.delete(name); }
  override querySelectorAll(selector: string) {
    return selector === "[data-inbox-action]"
      ? nodes(this).filter(node => node.dataset.inboxAction !== undefined)
      : super.querySelectorAll(selector);
  }
}

const originalWindow = globalThis.window;
afterEach(() => {
  clearMocks();
  globalThis.window = originalWindow;
});

function harness() {
  const page = structuredClone(fixture.action_page);
  const [sent, other] = page.items;
  const thread = page.thread;
  const mailbox = thread.thread_ref;
  sent.inbox_action.calendar_proposal = { ...proposal };
  sent.inbox_action.attachments[0].automation_fires = [{ ...fire }];
  let sentRetained = true;
  let pageHold: ReturnType<typeof deferred<void>> | undefined;
  let openHold: ReturnType<typeof deferred<void>> | undefined;
  let approval: ReturnType<typeof deferred<string>> | undefined;
  const calls: { command: string; args: Record<string, unknown> }[] = [];
  const unexpected: string[] = [];
  globalThis.window = {} as Window & typeof globalThis;
  mockIPC(async (command, payload) => {
    const args = (payload ?? {}) as Record<string, any>;
    calls.push({ command, args: structuredClone(args) });
    switch (command) {
      case "inbox_conversations_query": {
        const latest = sentRetained ? sent : other;
        const members = sentRetained ? [sent, other] : [other];
        return {
          kind: "inbox_conversations_v1", next_cursor: null,
          totals: { conversations: 1, messages: members.length, matching_messages: 1 },
          items: [{
            thread_ref: mailbox, matching_message_ref: other.inbox_action.thread_entry_ref,
            latest: { message_ref: latest.inbox_action.thread_entry_ref, subject: latest.subject,
              sender: latest.sender, sender_name: latest.sender_name, received_at: latest.received_at },
            senders: members.map(member => ({ address: member.sender, name: member.sender_name })),
            counts: { messages: members.length, matching_messages: 1, pending_messages: 0,
              analysis_errors: 0, action_required: 0, attachments: members.length,
              truncated_messages: 0, unknown_truncation_messages: 0 },
          }],
        };
      }
      case "threads_resolve":
        return { state: "resolved", message_ref: args.query.message_ref,
          context: { kind: "vendor", vendor: thread.owner_vendor, thread } };
      case "threads_messages": {
        const hold = pageHold;
        pageHold = undefined;
        await hold?.promise;
        return structuredClone({ ...page, items: sentRetained ? [sent, other] : [other] });
      }
      case "attachment_capabilities":
        return { items: args.messageId === sent.message_id ? [capability] : [], diagnostic: null };
      case dialog:
        assert.ok(approval, "confirmation must be controlled by the test");
        return approval.promise;
      case "attachment_open":
        await openHold?.promise;
        return { filename: "synthetic.pdf" };
      case "inbox_removal_prepare":
        return { request_id: "fixture-removal" };
      case "inbox_delete":
        sentRetained = false;
        return { request_id: args.requestId, intent_ids: ["fixture-intent"], outcome: "completed",
          requested: 1, completed: 1, pending: 0, message: null, deleted: true, message_id: args.messageId };
      case "inbox_removal_acknowledge":
        return;
      case "calendar_proposal_decide":
        sent.inbox_action.calendar_proposal.state = "completed";
        return { state: "completed" };
      case "attachment_capability_invoke":
        return { status: "completed", outputs: [] };
      case "automation_fire_decide":
        sent.inbox_action.attachments[0].automation_fires[0].state = "pending_dispatch";
        return { state: "pending_dispatch" };
      default:
        unexpected.push(command);
        throw new Error("Unexpected mocked command " + command);
    }
  });

  const doc = { activeElement: null, visibilityState: "visible", createElement: (tag: string) => new TestElement(tag, doc) };
  const root = doc.createElement("section"), columns = doc.createElement("div"), notices = doc.createElement("div");
  const scope = { mutation_epoch: 1, mailbox_operation_revision: 3 };
  const noop = () => {};
  const ctx = createContext({
    ...automationDecision, ...automationOutcome, ...calendarOutcome, ...connectAvailability, ...connectIdentity,
    ...inboxActionState, inboxAdmissionLabel, inboxBodyTruncation, collectCapabilityParameters,
    invoke, confirmAction, inboxConversationKey, inboxRemovalStatus, crypto, document: doc,
    inboxActionNotices: notices, inboxStatus: doc.createElement("p"), inboxLoadMore: doc.createElement("button"),
    inboxConversationClient: new InboxConversationClient(invoke), inboxRemovalClient: new InboxRemovalClient(invoke),
    activeInboxQuery: { provider: mailbox.provider, account_id: mailbox.account_id, limit: 25 },
    inboxItems: [], inboxItemsEpoch: null, inboxQueryEpoch: 1, inboxThreadSpanScope: null, inboxNextCursor: null,
    inboxTotals: null, inboxPageState: "pending", inboxPageStateGeneration: 0, inboxRequestGeneration: 0,
    inboxClearInFlight: false, configurationReady: true, inboxDeletionsInFlight: new Set(),
    inboxReaderActionsInFlight: new Set(), automationDecisionsInFlight: new Set(),
    automationDecisionRefreshRequired: new Map(), inboxProposalExpiryTimer: null, inboxRenderProposalExpiry: null,
    capabilityOutputPresentations: new Map(), capabilityOutputPresentationsInFlight: new Map(),
    capabilityOutputPreviews: new Map(), capabilityOutputViewButtons: new Map(),
    attachmentCapabilities: new Map(), attachmentCapabilityDiagnostics: new Map(), attachmentSelectionRefusals: new Map(),
    attachmentInvocationsInFlight: new Set(), attachmentRequestIds: new Map(), readerAttachmentDiscovery: new Map(),
    readerAttachmentKeys: new Set(), readerAttachmentQueue: [], readerAttachmentRequests: 0,
    currentMailboxEffectScope: () => scope, mailboxEffectScopeIsCurrent: () => true,
    mailboxEffectRequestIsCurrent: (generation: number, current: number) => generation === current,
    inboxReloadBlocked: () => ctx.inboxMutationInFlight(), refreshInboxThreadControls: noop,
    captureInboxScroll: () => ({ top: 0, revision: 0 }), restoreInboxScroll: noop,
    renderInbox: noop, setInboxControlsBusy: noop, wakeInboxReload: () => ctx.inboxReload.wake(),
    refreshInboxRemovalStatuses: noop,
  });
  runInContext(owners, ctx);
  const client = new ThreadReadClient(invoke);
  const entry = new InboxThreadEntry({
    root: root as unknown as HTMLElement, columns: columns as unknown as HTMLElement,
    heading: doc.createElement("h2") as unknown as HTMLElement, client, accounts: () => [],
    snapshot: () => ({
      configured: true, visible: true, blocked: ctx.inboxReloadBlocked() || history.mutationHeld,
      queryEpoch: ctx.inboxQueryEpoch, itemsEpoch: ctx.inboxItemsEpoch, items: ctx.inboxItems.map(ctx.inboxConversationRow),
      scope, spanScope: ctx.inboxThreadSpanScope,
    }),
    refreshInbox: ctx.refreshLoadedInboxSpan, scrollElements: () => [],
    renderAction: ctx.renderInboxReaderAction, beforeRender: ctx.beginInboxActionRender,
    afterRender: ctx.finishInboxActionRender,
    actionNotSent: (subject, reason) => ctx.showInboxActionNotice(subject, reason, "warning"),
  });
  const history = new ThreadHistory(new ThreadViewState(client, noop), entry);
  ctx.inboxThreadEntry = entry;
  ctx.threadHistory = history;
  ctx.inboxReload = new InboxReload({
    epoch: () => ctx.inboxQueryEpoch, blocked: ctx.inboxReloadBlocked,
    currentScope: ctx.mailboxEffectScopeIsCurrent, run: ctx.runInboxRefresh,
  });

  const cards = () => nodes(root).filter(node => node.className === "thread-card");
  const button = (label: string) => {
    const found = nodes(root).find(node => node.tagName === "BUTTON" && node.textContent === label);
    assert.ok(found, label);
    return found;
  };
  return {
    sent, other, calls, cards, button, refresh: () => ctx.refreshLoadedInboxSpan(),
    open: async () => { await entry.open(ctx.inboxConversationRow(ctx.inboxItems[0])); await drain(); },
    removeSent: () => { sentRetained = false; },
    holdPage: () => (pageHold = deferred<void>()),
    holdOpen: () => (openHold = deferred<void>()),
    holdApproval: () => (approval = deferred<string>()),
    count: (command: string) => calls.filter(call => call.command === command).length,
    writes: () => calls.filter(call => writeCommands.has(call.command)),
    notices: () => text(notices),
    assertMocked: () => assert.deepEqual(unexpected, []),
  };
}

const families = [
  { name: "Delete", label: "Delete locally", commands: ["inbox_removal_prepare", "inbox_delete"],
    target: (messageId: string) => [{ kind: "delete", messageId }, { requestId: "fixture-removal", messageId }] },
  { name: "Calendar", label: "Create event", commands: ["calendar_proposal_decide"],
    target: (messageId: string) => [{ messageId, proposalSha256: proposal.proposal_sha256,
      proposalVersion: proposal.proposal_version, runId: proposal.run_id, stateVersion: proposal.state_version, decision: "confirm" }] },
  { name: "Connect", label: "Read invoice", commands: ["attachment_capability_invoke"],
    target: (messageId: string, partId: string) => [{ messageId, partId, confirmed: true, parameters: { include_tax: false },
      provider: { app_id: capability.provider.app_id, version: capability.provider.version, instance_id: capability.provider.instance_id },
      capability: { id: capability.capability.id, version: capability.capability.version } }] },
  { name: "Automation", label: "Confirm automation", commands: ["automation_fire_decide"],
    target: () => [{ fireId: fire.fire_id, expectedVersion: fire.state_version,
      preparedIdentitySha256: fire.prepared_identity_sha256, decision: "confirmed" }] },
];

for (const family of families) {
  for (const invalidate of ["Back", "member removal"] as const) {
    test(family.name + " refuses approval after " + invalidate, async () => {
      const h = harness();
      await h.refresh();
      await h.open();
      const approval = h.holdApproval();
      const action = h.button(family.label);
      assert.ok(h.cards()[0].contains(action), "the initiating control belongs to Sent-only mail");
      action.click();
      assert.equal(h.count(dialog), 1);
      assert.deepEqual(h.writes(), [], "no mutation before approval");

      if (invalidate === "Back") {
        h.button("Back to Inbox").click();
        assert.equal(h.cards().length, 0);
      } else {
        h.removeSent();
        await h.refresh();
        assert.equal(h.cards().length, 1);
        assert.ok(text(h.cards()[0]).includes(h.other.subject));
      }

      approval.resolve("Continue");
      await drain();
      assert.deepEqual(h.writes(), [], "stale approval must never dispatch or prepare a mutation");
      assert.ok(h.notices().includes(h.sent.subject + " · Action not sent."), h.notices());
      assert.ok(!h.notices().includes(h.other.subject));
      h.assertMocked();
    });
  }

  test(family.name + " dispatches its original target once after an unchanged refresh", async () => {
    const h = harness();
    await h.refresh();
    await h.open();
    const approval = h.holdApproval();
    const action = h.button(family.label);
    action.click();
    action.click();
    assert.equal(h.count(dialog), 1);

    const page = h.holdPage();
    const refresh = h.refresh();
    await drain();
    assert.equal(h.count("threads_messages"), 2, "the next message page is in flight");
    approval.resolve("Continue");
    await drain();
    assert.deepEqual(h.writes(), [], "approval waits for the committed message page");
    assert.ok(h.notices().includes(h.sent.subject + " · Waiting for the Inbox refresh."));
    page.resolve();
    await refresh;
    await drain();

    assert.deepEqual(h.writes().map(call => call.command), family.commands);
    const actual = h.writes().map(({ args }) => ({ ...args }));
    if (family.name === "Connect") {
      assert.match(String(actual[0].requestId), /^[0-9a-f-]{36}$/);
      delete actual[0].requestId;
    }
    assert.deepEqual(actual, family.target(h.sent.message_id, h.sent.inbox_action.attachments[0].part_id));
    h.assertMocked();
  });
}

test("a detached attachment Open button cannot issue IPC after Back", async () => {
  const h = harness();
  await h.refresh();
  await h.open();
  const open = h.button("Open");
  h.button("Back to Inbox").click();
  assert.equal(h.cards().length, 0);
  open.click();
  await drain();
  assert.equal(h.count("attachment_open"), 0, "a stale control must never open an attachment");
  h.assertMocked();
});

test("rerendering a pending attachment Open cannot dispatch it twice", async () => {
  const h = harness();
  await h.refresh();
  await h.open();
  const result = h.holdOpen();
  const original = h.button("Open");
  original.click();
  assert.equal(h.count("attachment_open"), 1);
  await h.refresh();
  const replacement = h.button("Open");
  assert.notEqual(replacement, original, "the mounted control was replaced");
  replacement.click();
  assert.equal(h.count("attachment_open"), 1, "the in-flight guard survives remount");
  result.resolve();
  await drain();
  assert.deepEqual(h.calls.filter(call => call.command === "attachment_open").map(call => call.args), [
    { messageId: h.sent.message_id, partId: h.sent.inbox_action.attachments[0].part_id },
  ]);
  h.assertMocked();
});
