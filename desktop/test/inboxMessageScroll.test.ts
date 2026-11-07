import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { createContext, runInContext } from "node:vm";
import test from "node:test";
import { hasInboxAutomationConfirmation, inboxActionState } from "../src/inboxActionState.ts";
import { inboxBodyTruncation } from "../src/inboxBodyTruncation.ts";
import { inboxThreadOriginKey } from "../src/inboxThreadEntry.ts";
import { Element, nodes } from "./threadDomFixture.ts";

const source = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
const render = source.match(/function renderInbox\([\s\S]*?\n\}/)![0];
const loadPage = source.match(/async function loadInboxPage\([\s\S]*?\n\}/)![0];
const refresh = source.match(/async function runInboxRefresh\([\s\S]*?\n\}/)![0];
function harness() {
  class Node extends Element {
    get childNodes() { return this.children; }
    get childElementCount() { return this.children.length; }
    append(...children: Element[]) {
      for (const child of children) {
        if (child.parent) child.parent.children = child.parent.children.filter(n => n !== child);
      }
      super.append(...children);
    }
  }
  const doc: any = { activeElement: null, createElement: (tag: string) => new Node(tag, doc) };
  const list = new Node("ul", doc);
  let offset = 0;
  let moreHidden = true;
  // Force a layout/clamp when rows are removed. Browser geometry is checked separately.
  const results = { get scrollTop() { return offset; }, set scrollTop(value: number) {
    offset = Math.max(0, Math.min(value, list.children.length * 100 + (moreHidden ? 0 : 42) - 400));
  } };
  const more = { get hidden() { return moreHidden; }, set hidden(value: boolean) {
    moreHidden = value; results.scrollTop = offset;
  } };
  const replace = list.replaceChildren.bind(list);
  list.replaceChildren = (...children) => { replace(...children); results.scrollTop = offset; };
  const c = createContext({ document: doc, inboxList: list, inboxResults: results,
    renderInboxFilterScope() {}, inboxProposalExpiryTimer: null,
    capabilityOutputPreviews: new Map(), capabilityOutputViewButtons: new Map(), capabilityOutputPresentations: new Map(),
    activeInboxQuery: { provider: "imap", account_id: "retained", sender: null, limit: 25 }, inboxPageState: "loaded",
    inboxQueryEpoch: 1, inboxItemsEpoch: 1, inboxRenderedEpoch: null,
    inboxScrollRevision: 0, inboxRestoredScrollTop: 0,
    inboxMutationInFlight: () => false, receivedLabel: (value: string) => value, stateLabel: () => "Notified",
    hasInboxAutomationConfirmation, inboxActionState, inboxBodyTruncation, inboxThreadOriginKey,
    inboxDeletionsInFlight: new Set(), expandedInboxMessages: new Set(), inboxThreadEntry: {
      canOpen: () => true, isSelected: () => false, refreshGeneration: 0, revalidateSource() {}, failRefresh() {},
    }, currentMailboxEffectScope: () => ({}), refreshInboxThreadControls() {}, threadHistory: { commitInboxSpan() {} },
    inboxLoadMore: more, inboxStatus: new Node("p", doc), inboxNextCursor: "120", inboxRequestGeneration: 1,
    mailboxEffectScopeIsCurrent: () => true, mailboxEffectRequestIsCurrent: () => true, inboxReloadBlocked: () => false,
    setInboxControlsBusy() {}, refreshInboxRemovalStatuses() {}, releaseAutomationRefreshFences: () => false,
    automationDecisionRefreshRequired: new Map(), inboxStatusLabel: () => "Loaded", inboxCapabilityUnavailableCount: 0,
    attachmentCapabilities: new Map(), attachmentCapabilityDiagnostics: new Map(),
    loadAttachmentCapabilities: async () => ({ capabilities: new Map(), diagnostics: new Map(), unavailable: 0 }),
  });
  const scrollHelpers = ["captureInboxScroll", "restoreInboxScroll"].map(name =>
    source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n\\}`))?.[0] ?? "").join("\n");
  runInContext(stripTypeScriptTypes(scrollHelpers + "\n" + render + "\n" + loadPage + "\n" + refresh), c);
  const rows = (count: number) => Array.from({ length: count }, (_, i) => ({ message_id: `row-${i}`,
    subject: `Subject ${i}`, sender: "sender@example.test", sender_name: "Sender", received_at: "2026-11-07T00:00:00Z",
    provider: "imap", account_id: "retained", thread_entry_ref: { provider: "imap", account_id: "retained",
      mailbox_identity_key: "mailbox", message_id: `row-${i}` }, status: "notified", priority: "normal",
    summary: "Retained analysis", attachments: [], admission: null, action_required: null, calendar_proposal: null,
  }));
  return { c, list, results, rows, more };
}

test("actual row renderer retains the message scroll owner across append and capability rerenders", () => {
  const h = harness();
  h.c.renderInbox(h.rows(120)); h.results.scrollTop = 850;
  h.c.expandedInboxMessages.add("row-8");
  h.c.renderInbox(h.rows(145));
  assert.equal(h.results.scrollTop, 850);
  assert.equal(h.list.children.length, 145);
  assert.equal(nodes(h.list).find(n => n.id === "inbox-message-8")?.hidden, false);
  h.c.renderInbox(h.rows(145)); assert.equal(h.results.scrollTop, 850);
});

test("actual row renderer lets the browser clamp shorter and empty results", () => {
  const h = harness(); h.c.renderInbox(h.rows(120)); h.results.scrollTop = 850;
  h.c.renderInbox(h.rows(7)); assert.equal(h.results.scrollTop, 300);
  h.c.renderInbox([]); assert.equal(h.results.scrollTop, 0);
  assert.equal(h.list.children[0].textContent, "No retained messages match these filters.");
});

test("last Load more page preserves its bottom offset before hiding the paging control", async () => {
  const h = harness(); h.more.hidden = false;
  h.c.inboxItems = h.rows(120); h.c.renderInbox(h.c.inboxItems); h.results.scrollTop = 1e6;
  const before = h.results.scrollTop;
  const calls: any[] = [];
  h.c.invoke = async (command: string, args: any) => {
    calls.push({ command, args }); return { items: h.rows(145).slice(120), next_cursor: null };
  };
  assert.equal(await h.c.loadInboxPage(true, {}), true);
  assert.equal(h.results.scrollTop, before);
  assert.equal(h.list.children.length, 145); assert.equal(h.more.hidden, true);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [{ command: "inbox_query", args: { query: {
    provider: "imap", account_id: "retained", sender: null, limit: 25, cursor: "120",
  } } }]);
});

test("same-query refresh keeps scroll but the committed first page of a new query starts at the top", async () => {
  const h = harness(); h.c.inboxItems = h.rows(145); h.c.renderInbox(h.c.inboxItems);
  h.results.scrollTop = 850;
  h.c.invoke = async () => ({ items: h.rows(25), next_cursor: "25" });
  assert.equal(await h.c.loadInboxPage(false, {}), true);
  assert.equal(h.c.inboxItemsEpoch, 1); assert.equal(h.results.scrollTop, 850);
  h.c.inboxQueryEpoch = 2;
  h.c.renderInbox(h.c.inboxItems); // Previous results still belong to epoch 1.
  assert.equal(h.results.scrollTop, 850);
  assert.equal(await h.c.loadInboxPage(false, {}), true);
  assert.equal(h.c.inboxItemsEpoch, 2); assert.equal(h.results.scrollTop, 0);
  h.results.scrollTop = 450; h.c.renderInbox(h.c.inboxItems);
  assert.equal(h.results.scrollTop, 450, "later renders belong to the newly committed epoch");
});

test("full-span refresh restores deep message scroll after intermediate page clamps unless the user moves it", async () => {
  for (const mode of ["keep", "user", "shorter", "new-query"]) {
    const h = harness(); h.c.inboxItems = h.rows(120); h.c.renderInbox(h.c.inboxItems);
    h.results.scrollTop = 8500;
    if (mode === "new-query") h.c.inboxQueryEpoch++;
    h.c.invoke = async (_command: string, args: any) => {
      const offset = Number(args.query.cursor ?? 0), count = mode === "shorter" ? 60 : 120;
      if (offset === 25 && mode === "user") h.results.scrollTop = 650;
      return { items: h.rows(count).slice(offset, offset + 25), next_cursor: offset + 25 < count ? String(offset + 25) : null };
    };
    assert.equal(await h.c.runInboxRefresh({ kind: "span", target: 120 }, null, () => true), true);
    assert.equal(h.results.scrollTop, mode === "user" ? 650 : mode === "shorter" ? 5600 : mode === "new-query" ? 0 : 8500, mode);
  }
});
