import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import vm from "node:vm";
import test from "node:test";
import { parseAst } from "vite";
import { ThreadHistory } from "../src/threadHistory.ts";
import type { ThreadViewState } from "../src/threadView.ts";
import { inboxThreadOriginKey } from "../src/inboxThreadEntry.ts";

const source = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
const js = stripTypeScriptTypes(source), ast = parseAst(js);
function owner(name: string): string {
  const node = ast.body.find(n => n.type === "FunctionDeclaration" && n.id?.name === name);
  assert.ok(node, name); return js.slice(node.start, node.end);
}
const noop = () => {};
function harness(names: string[]) {
  const events: string[] = [], inboxEvents: string[] = [];
  const node = () => ({ disabled: false, hidden: false, textContent: "", dataset: {},
    attributes: new Map(), setAttribute(key: string, value: string) { this.attributes.set(key, value); },
    append: noop, prepend: noop, replaceChildren: noop, addEventListener: noop,
    focus: () => events.push("focus") });
  const shell = node();
  const context = vm.createContext({
    threadReader: { state: { beginMutation: () => events.push("begin"), endMutation: async () => { events.push("end"); },
      refresh: async () => { events.push("refresh"); }, suspend: () => events.push("suspend"), resume: async () => { events.push("resume"); },
      close: () => events.push("close"), vendor: { vendor_id: "v", display_name: "Before" } } },
    requiredElement: () => shell, document: { createElement: node },
    vendorRecords: [], vendorManagement: { hidden: true },
    setVendorsStatus: (message: string, kind: string) => events.push(`status:${kind}:${message}`),
    vendorListFence: { begin: () => 1, isLatest: () => true },
    vendorCardDrafts: { keepOnly: noop }, renderVendors: noop,
    vendorFailure: (error: unknown) => { throw error; },
    mailAccounts: [], mailProviders: [], mailAccountCatalogRevision: 0,
    reconcileGmailLabelScope: () => false, renderInboxAccountOptions: noop,
    reconcileInboxAccountScope: () => false, mailAccountList: node(), mailProviderActions: node(),
    mailAccountDetail: () => "Account", providerFor: () => undefined,
    inboxItemsEpoch: 0, inboxQueryEpoch: 0,
    configurationReady: true, operationInFlight: false, setBusy: noop,
    inboxMutationInFlight: () => false, confirmAction: async () => true,
    inboxDeletionsInFlight: new Set(), inboxClearInFlight: false, inboxRequestGeneration: 0,
    wakeInboxReload: noop, setInboxControlsBusy: noop, renderInbox: noop,
    inboxItems: [{ message_id: "m", subject: "Subject" }], expandedInboxMessages: new Set(),
    clearMessageOwnedUiState: noop, inboxRemovalStatus: () => ({ text: "Removed", kind: "success" }),
    inboxClearView: () => ({ retained: [], text: "Cleared", kind: "success" }),
    inboxStatus: node(), inboxLoadMore: node(), inboxPageState: "ready", inboxNextCursor: null,
    inboxCapabilityUnavailableCount: 0, errorMessage: () => "Failed",
    loadInbox: async () => { events.push("inbox"); },
    inboxRemovalClient: { delete: async () => { events.push("write"); return { outcome: "completed" }; },
      clear: async () => { events.push("write"); return { outcome: "completed" }; } },
    mailAccountMutationsInFlight: 0, mailAccountMutationEpoch: 0, healthRequestGeneration: 0,
    mailOperationInFlight: false, mailboxOperationRevisionReady: true, checkSupported: true,
    checkNow: node(), observeMailboxOperationRevision: () => true, observeMailboxOperationRevisionFromError: () => true,
    checkInFlight: false, healthStatus: node(), checkResultMessage: () => "Complete", loadHealth: async () => {},
    invoke: async () => { events.push("write"); return {}; },
    mailboxEffectScopeIsCurrent: () => true,
    currentMailboxEffectScope: () => ({ mutation_epoch: 0, mailbox_operation_revision: 1 }),
    inboxThreadSpanScope: null, refreshInboxThreadControls: noop,
    inboxThreadEntry: { revalidateSource: noop },
    inboxReload: { running: false, request: async () => { events.push("inbox"); return true; } },
    loadInboxPage: async () => { events.push("append"); return true; },
  });
  const inbox = {
    beginMutation: () => inboxEvents.push("begin"), endMutation: async () => { inboxEvents.push("end"); },
    refresh: async () => { inboxEvents.push("refresh"); }, invalidate: () => inboxEvents.push("invalidate"),
    suspend: () => inboxEvents.push("suspend"), resume: async () => { inboxEvents.push("resume"); },
    close: () => inboxEvents.push("close"),
  };
  context.inboxThreadState = inbox;
  context.threadHistory = new ThreadHistory(context.threadReader.state as ThreadViewState, inbox as unknown as ThreadViewState);
  for (const name of ["inboxView", "watchlistView", "vendorsSection", "expiryLedgerView", "healthView", "settingsView",
    "inboxTab", "watchlistTab", "vendorsTab", "expiryLedgerTab", "healthTab", "settingsTab"]) context[name] = node();
  vm.runInContext(names.map(owner).join("\n"), context);
  return { events, inboxEvents, context };
}

test("actual delete/clear and check owners clear thread reads before writes and settle on success or error", async () => {
  for (const name of ["deleteInboxItem", "clearInboxHistory", "runCheck"]) {
    for (const fail of [false, true]) {
      const h = harness([name]);
      if (fail) {
        const failure = async () => { h.events.push("write"); throw new Error("synthetic failure"); };
        h.context.inboxRemovalClient = { delete: failure, clear: failure }; h.context.invoke = failure;
      }
      await h.context[name]({ message_id: "m", subject: "Subject" });
      assert.equal(h.events[0], "begin", `${name}: ${h.events}`);
      assert.deepEqual(h.inboxEvents, ["begin", "end"], name);
      assert.equal(h.events.filter(x => x === "begin").length, 1);
      assert.equal(h.events.filter(x => x === "end").length, 1);
      assert.ok(h.events.indexOf("end") > h.events.indexOf("write"));
    }
  }
});

test("vendor/watchlist and account mutation owners retain matched thread holds including failures", async () => {
  const h = harness(["beginOperation", "finishOperation", "runMailAccountMutation"]);
  assert.equal(h.context.beginOperation(), true); assert.equal(h.context.beginOperation(), false);
  assert.deepEqual(h.events, ["begin"]); h.context.finishOperation(); assert.deepEqual(h.events, ["begin", "end"]);
  for (const fail of [false, true]) {
    h.events.length = 0;
    const pending = h.context.runMailAccountMutation(async () => {
      assert.deepEqual(h.events, ["begin"]); h.events.push("write");
      if (fail) throw new Error("failure"); return { mailbox_operation_revision: 3 };
    });
    if (fail) await assert.rejects(pending); else await pending;
    assert.deepEqual(h.events, ["begin", "write", "end"]);
  }
});

test("history refresh observes current scope only and append does not disturb the thread view", async () => {
  const h = harness(["refreshThreadHistory", "loadInbox"]);
  await h.context.loadInbox(); assert.deepEqual(h.events, ["refresh", "inbox"]);
  h.events.length = 0; await h.context.loadInbox(true); assert.deepEqual(h.events, ["append"]);
  h.events.length = 0; h.context.mailboxEffectScopeIsCurrent = () => false;
  await h.context.loadInbox(false, { mutation_epoch: -1 }); assert.deepEqual(h.events, ["inbox"]);
});

test("actual view lifecycle resumes Vendors and suspends other or unconfigured views", () => {
  const h = harness(["showView"]);
  h.context.showView("vendors"); assert.deepEqual(h.events, ["resume"]);
  assert.deepEqual(h.inboxEvents, ["close"]); h.inboxEvents.length = 0;
  assert.equal(h.context.vendorsSection.hidden, false); assert.equal(h.context.inboxView.hidden, true);
  assert.equal(h.context.vendorsTab.attributes.get("aria-pressed"), "true");
  h.events.length = 0; h.context.showView("inbox"); assert.deepEqual(h.events, ["suspend"]);
  assert.deepEqual(h.inboxEvents, ["resume"]); h.inboxEvents.length = 0;
  assert.equal(h.context.vendorsSection.hidden, true); assert.equal(h.context.inboxView.hidden, false);
  h.events.length = 0; h.context.configurationReady = false; h.context.showView("vendors");
  assert.deepEqual(h.events, ["suspend"]);
});

test("actual account renderer refreshes only changed thread account tuples", () => {
  const baseline = { provider: "imap", account_id: "one", display_name: "Retained", address: null,
    connected: false, active: false, sent_scope: "not_polled", last_check: null };
  for (const change of [{ provider: "gmail" }, { account_id: "two" }, { display_name: "Renamed" },
    { address: "changed@example.test" }, { connected: true }, { active: true }, { sent_scope: "unavailable" }]) {
    const h = harness(["refreshThreadHistory", "renderMailAccounts"]);
    h.context.mailAccounts = [baseline];
    h.context.renderMailAccounts({ accounts: [{ ...baseline, last_check: "new check" }], providers: [] });
    assert.deepEqual(h.events, [], `unchanged tuple: ${JSON.stringify(change)}`);
    h.context.renderMailAccounts({ accounts: [{ ...baseline, ...change }], providers: [] });
    assert.deepEqual(h.events, ["refresh"], JSON.stringify(change));
  }
});

test("actual span refresh revalidates threads and respects configuration readiness", async () => {
  const h = harness(["refreshThreadHistory", "refreshLoadedInboxSpan"]);
  await h.context.refreshLoadedInboxSpan(); assert.deepEqual(h.events, ["refresh", "inbox"]);
  h.events.length = 0; h.context.configurationReady = false;
  await h.context.refreshLoadedInboxSpan(); assert.deepEqual(h.events, ["inbox"]);
});

test("actual row controls track nested mutation release and current-source eligibility", async () => {
  const h = harness(["refreshInboxThreadControls"]);
  const origin = { provider: "imap", account_id: "retained", mailbox_identity_key: "identity", message_id: "m" };
  const button = { disabled: true, dataset: { inboxThreadOrigin: inboxThreadOriginKey(origin) } };
  const other = { disabled: false, dataset: { inboxThreadOrigin: inboxThreadOriginKey({ ...origin, account_id: "other" }) } };
  h.context.inboxThreadOriginKey = inboxThreadOriginKey;
  h.context.inboxItems = [{ ...origin, thread_entry_ref: origin }];
  h.context.inboxList = { querySelectorAll: () => [button, other] };
  let sourceCurrent = true;
  const history = new ThreadHistory(h.context.threadReader.state, h.context.inboxThreadState, h.context.refreshInboxThreadControls);
  h.context.inboxThreadEntry.canOpen = () => sourceCurrent && !history.mutationHeld;
  h.context.refreshInboxThreadControls(); assert.equal(button.disabled, false); assert.equal(other.disabled, true);
  history.beginMutation(); history.beginMutation(); assert.equal(button.disabled, true);
  await history.endMutation(); assert.equal(button.disabled, true);
  await history.endMutation(); assert.equal(button.disabled, false);
  sourceCurrent = false; h.context.refreshInboxThreadControls(); assert.equal(button.disabled, true);
});

test("actual refresh commits only a complete current span and query changes close the entry before rendering", async () => {
  for (const mode of ["current", "obsolete", "failed", "span"]) {
    const h = harness(["runInboxRefresh"]);
    let pages = 0;
    h.context.inboxNextCursor = mode === "span" ? "more" : null;
    h.context.loadInboxPage = async () => {
      pages += 1;
      if (pages === 2) { h.context.inboxItems.push({ message_id: "second" }); h.context.inboxNextCursor = null; }
      return mode !== "failed";
    };
    const result = await h.context.runInboxRefresh({ kind: "span", target: 2 }, null, () => mode !== "obsolete");
    assert.equal(result, mode === "current" || mode === "span");
    assert.deepEqual(h.inboxEvents, result ? ["refresh"] : [], mode);
    assert.equal(pages, mode === "span" ? 2 : 1);
  }
  const h = harness(["clearInboxPageForAccountChange"]);
  h.context.renderInbox = () => assert.deepEqual(h.inboxEvents, ["close"]);
  h.context.clearInboxPageForAccountChange("Changed");
  assert.equal(h.context.inboxQueryEpoch, 1);
  assert.deepEqual(h.inboxEvents, ["close"]);
  const commit = owner("commitInboxQueryFromControls");
  assert.ok(commit.indexOf("threadHistory.clearInboxSelection()") < commit.indexOf("renderInbox(inboxItems)"));
});

test("actual vendor reload closes removed selection or updates and refreshes retained selection", async () => {
  for (const present of [false, true]) {
    const h = harness(["refreshThreadHistory", "reconcileThreadVendor", "loadVendors"]);
    const fresh = { vendor_id: "v", display_name: "Renamed" };
    h.context.invoke = async (operation: string) => {
      assert.equal(operation, "vendors_list"); return present ? [fresh] : [];
    };
    await h.context.loadVendors();
    if (present) {
      assert.deepEqual(h.events, ["refresh", "status:success:Vendors are up to date."]);
      assert.equal(h.context.threadReader.state.vendor.display_name, "Renamed");
      assert.equal(h.context.threadReader.state.vendor.vendor_id, "v");
      assert.equal(h.context.vendorManagement.hidden, true);
    } else {
      assert.deepEqual(h.events, ["close", "status:error:The selected vendor is no longer available.", "focus"]);
      assert.equal(h.context.vendorManagement.hidden, false);
    }
  }
});

test("entry is read class, lifecycle/settings hooks exist, and approved copy preserves sender distinctions", () => {
  const render = owner("renderVendors");
  assert.ok(render.indexOf('"View threads"') < render.indexOf("if (view.canUpdate)"));
  assert.match(render, /openVendorThreads/);
  assert.match(source, /window\.addEventListener\("focus", \(\) => \{\s*refreshThreadHistory\(\)/);
  assert.match(source, /document\.visibilityState !== "visible"\) return;\s*refreshThreadHistory\(\)/);
  const settings = source.slice(source.indexOf('settingsForm.addEventListener("submit"'), source.indexOf('gmailLabelCatalogSelect.addEventListener'));
  assert.ok(settings.indexOf("threadHistory.beginMutation()") < settings.indexOf('"settings_update"'));
  assert.match(settings, /finally \{\s*void threadHistory\.endMutation\(\)/);
  assert.ok(source.includes("Locally retained messages from watched senders and Gmail labels appear here. Followed threads and Sent replies to vendors also appear when captured with Connect."));
  assert.ok(source.includes("Watching an address keeps messages from that sender in the Inbox. Vendor records can also capture followed threads and Sent replies while Connect is active."));
  assert.match(source, /aria-label="Recent retained messages"/);
  assert.match(source, /inboxSenderEmptyText\(activeInboxQuery, activeInboxAccountSelection\)/);
  assert.doesNotMatch(source, /No watched messages/);
});
