import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import vm from "node:vm";
import test from "node:test";
import { parseAst } from "vite";

const source = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
const js = stripTypeScriptTypes(source), ast = parseAst(js);
function owner(name: string): string {
  const node = ast.body.find(n => n.type === "FunctionDeclaration" && n.id?.name === name);
  assert.ok(node, name); return js.slice(node.start, node.end);
}
const noop = () => {};
function harness(names: string[]) {
  const events: string[] = [];
  const node = () => ({ disabled: false, hidden: false, textContent: "", dataset: {}, setAttribute: noop });
  const context = vm.createContext({
    threadReader: { state: { beginMutation: () => events.push("begin"), endMutation: async () => { events.push("end"); },
      refresh: async () => { events.push("refresh"); }, suspend: () => events.push("suspend"), resume: async () => { events.push("resume"); }, vendor: { vendor_id: "v" } } },
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
    inboxReload: { running: false, request: async () => { events.push("inbox"); return true; } },
    loadInboxPage: async () => { events.push("append"); return true; },
  });
  vm.runInContext(names.map(owner).join("\n"), context);
  return { events, context };
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

test("entry is read class, lifecycle/settings hooks exist, and approved copy preserves sender distinctions", () => {
  const render = owner("renderVendors");
  assert.ok(render.indexOf('"View threads"') < render.indexOf("if (view.canUpdate)"));
  assert.match(render, /openVendorThreads/);
  assert.match(owner("showView"), /state\.resume\(/); assert.match(owner("showView"), /state\.suspend\(/);
  assert.match(owner("renderMailAccounts"), /refreshThreadHistory\(/);
  assert.match(owner("loadVendors"), /reconcileThreadVendor\(/);
  assert.match(source, /window\.addEventListener\("focus", \(\) => \{\s*refreshThreadHistory\(\)/);
  assert.match(source, /document\.visibilityState !== "visible"\) return;\s*refreshThreadHistory\(\)/);
  const settings = source.slice(source.indexOf('settingsForm.addEventListener("submit"'), source.indexOf('gmailLabelCatalogSelect.addEventListener'));
  assert.ok(settings.indexOf("state.beginMutation()") < settings.indexOf('"settings_update"'));
  assert.match(settings, /finally \{\s*void threadReader\.state\.endMutation\(\)/);
  assert.ok(source.includes("Locally retained messages from watched senders and Gmail labels appear here. Followed threads and Sent replies to vendors also appear when captured with Connect."));
  assert.ok(source.includes("Watching an address keeps messages from that sender in the Inbox. Vendor records can also capture followed threads and Sent replies while Connect is active."));
  assert.match(source, /aria-label="Recent retained messages"/);
  assert.match(source, /inboxSenderEmptyText\(activeInboxQuery, activeInboxAccountSelection\)/);
  assert.doesNotMatch(source, /No watched messages/);
});
