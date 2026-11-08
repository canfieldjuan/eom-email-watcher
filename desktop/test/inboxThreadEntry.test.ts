import assert from "node:assert/strict";
import test from "node:test";
import { InboxThreadEntry, inboxThreadOriginKey, type InboxThreadSnapshot } from "../src/inboxThreadEntry.ts";
import { ThreadReadClient } from "../src/threadReads.ts";
import { ThreadHistory } from "../src/threadHistory.ts";
import { ThreadViewDom } from "../src/threadViewDom.ts";
import { Element, nodes, text } from "./threadDomFixture.ts";

const origin = { provider: "imap", account_id: "retained", mailbox_identity_key: "mailbox", message_id: "seed" };
const row = { ...origin, thread_entry_ref: origin };
const ref = { provider: "imap", account_id: "retained", mailbox_identity_key: "mailbox", thread_key: "thread" };
const vendor = { vendor_id: "owner", display_name: "Owner <img src=x onerror=evil()>" };
const linked = { vendor_id: "linked", display_name: "Linked vendor" };
const thread = { thread_ref: ref, subject: "Newest subject", relation: "non_vendor", owner_vendor: null,
  latest_received_at: null, message_count: 2 };
const message = { message_id: "seed", subject: "Oldest subject", direction: "inbound", sender: "sender@example.test",
  sender_name: null, attributed_vendor: null, received_at: null, body_state: { state: "not_followed" }, copy_count: 1 };
const body = "<script>hostile()</script>\nhttps://example.test/no-link";
const tick = () => new Promise<void>(resolve => setImmediate(resolve));
function deferred() {
  let resolve!: (value: any) => void, reject!: (reason: any) => void;
  const promise = new Promise<any>((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}
function harness() {
  const doc: any = { activeElement: null, createElement: (tag: string) => new Element(tag, doc) };
  const root = new Element("section", doc), columns = new Element("div", doc), heading = new Element("h2", doc);
  const trigger = new Element("button", doc); trigger.dataset.inboxThreadOrigin = inboxThreadOriginKey(origin);
  columns.append(heading, trigger); columns.scrollTop = 73; columns.scrollLeft = 12;
  const snapshot: InboxThreadSnapshot = { configured: true, visible: true, blocked: false, queryEpoch: 4,
    itemsEpoch: 4, items: [row], scope: { mutation_epoch: 2, mailbox_operation_revision: 9 },
    spanScope: { mutation_epoch: 2, mailbox_operation_revision: 9 } };
  const calls: { command: string; query: any }[] = [];
  let respond = async (command: string, query: any): Promise<any> => command === "threads_resolve"
    ? { state: "resolved", message_ref: origin, context: { kind: "non_vendor", thread } }
    : command === "threads_messages" ? { thread, items: [message], next_cursor: null }
    : command === "threads_body" ? { thread_ref: ref, message_id: query.message_id,
      body: { state: "stored", text: body, stored_chars: body.length, source_chars: body.length } }
    : { thread_ref: ref, message_id: query.message_id, items: [], next_cursor: null };
  const client = new ThreadReadClient(async <T>(command: string, args: any) => {
    calls.push({ command, query: structuredClone(args.query) }); return await respond(command, args.query) as T;
  });
  let refreshes = 0, afterRefresh: () => Promise<void> = async () => {};
  const ui = new InboxThreadEntry({ root: root as any, columns: columns as any, heading: heading as any, client,
    accounts: () => [], snapshot: () => snapshot, scrollElements: () => [columns as any],
    refreshInbox: async () => {
      refreshes++; snapshot.spanScope = null; ui.state.invalidate(); await tick();
      snapshot.spanScope = snapshot.scope; ui.revalidateSource(); await ui.state.refresh();
      await afterRefresh();
    } });
  const button = (label: string) => {
    const found = nodes(root).find(n => n.tagName === "BUTTON" && n.textContent === label);
    assert.ok(found, `missing ${label}: ${text(root)}`); return found;
  };
  return { ui, root, columns, heading, trigger, doc, snapshot, calls, client, button,
    refreshes: () => refreshes, afterRefresh: (fn: typeof afterRefresh) => { afterRefresh = fn; },
    respond: (fn: typeof respond) => { respond = fn; }, defaultResponse: respond };
}

test("explicit row entry preserves Inbox DOM and scroll; Back returns to the complete origin trigger", async () => {
  const h = harness(); const children = [...h.columns.children];
  await h.ui.open(row);
  assert.equal(h.columns.hidden, true); assert.equal(h.root.hidden, false);
  assert.deepEqual(h.calls.map(c => c.command), ["threads_resolve", "threads_messages"]);
  assert.deepEqual(h.calls[0].query.message_ref, origin);
  assert.deepEqual(h.calls[1].query, { inbox_thread: { thread_ref: ref, message_ref: origin }, cursor: null });
  assert.equal(h.doc.activeElement.dataset.threadFocus, "inbox-thread:heading");
  h.columns.scrollTop = 0; h.columns.scrollLeft = 0;
  h.button("Back to Inbox").click();
  assert.equal(h.root.hidden, true); assert.equal(h.columns.hidden, false);
  assert.deepEqual(h.columns.children, children); assert.equal(h.columns.scrollTop, 73); assert.equal(h.columns.scrollLeft, 12);
  assert.equal(h.doc.activeElement, h.trigger); assert.equal(h.ui.state.inboxOrigin, null);
});

test("entry refuses stale rows, incomplete spans, mismatched identity, hidden/configuration and mutation scopes", async () => {
  const changes = [ { configured: false }, { visible: false }, { blocked: true }, { itemsEpoch: 3 }, { spanScope: null },
    { scope: null }, { scope: { mutation_epoch: 3, mailbox_operation_revision: 9 } },
    { scope: { mutation_epoch: 2, mailbox_operation_revision: 10 } }, { items: [] },
    ...["provider", "account_id", "mailbox_identity_key", "message_id"].map(key => ({
      items: [{ ...row, thread_entry_ref: { ...origin, [key]: "different" } }],
    })) ];
  for (const change of changes) {
    const h = harness(); Object.assign(h.snapshot, change);
    assert.equal(h.ui.canOpen(row), false, JSON.stringify(change)); await h.ui.open(row);
    assert.equal(h.calls.length, 0); assert.equal(h.columns.hidden, false);
  }
  const h = harness();
  h.snapshot.items = [{ ...row, account_id: "wrong-account" }];
  assert.equal(h.ui.canOpen(row), false, "outer row must also match its origin");
});

test("Back falls back to Inbox heading when the exact row disappeared, never a same-id other account", async () => {
  const h = harness(); await h.ui.open(row);
  h.trigger.dataset.inboxThreadOrigin = inboxThreadOriginKey({ ...origin, account_id: "other" });
  h.ui.back(); assert.equal(h.doc.activeElement, h.heading);
});

test("pending entry cannot cross a refreshed query epoch or a changed complete source identity", async () => {
  for (const change of ["epoch", "provider", "account_id", "mailbox_identity_key", "message_id"]) {
    const h = harness(), pending = deferred();
    h.respond(async (command, query) => command === "threads_resolve" ? pending.promise : h.defaultResponse(command, query));
    const opening = h.ui.open(row);
    if (change === "epoch") { h.snapshot.queryEpoch++; h.snapshot.itemsEpoch = h.snapshot.queryEpoch; }
    else h.snapshot.items = [{ ...row, thread_entry_ref: { ...origin, [change]: "replacement" } }];
    pending.resolve(await h.defaultResponse("threads_resolve", {})); await opening;
    assert.equal(h.ui.state.selectedThread, null, change); assert.equal(h.calls.length, 1, change);
  }
});

test("Back and leaving Inbox fence deferred resolution/messages and never reopen on return", async () => {
  for (const pendingCommand of ["threads_resolve", "threads_messages"]) {
    for (const leave of ["back", "hide"]) {
      const h = harness(), pending = deferred();
      h.respond(async (command, query) => command === pendingCommand ? pending.promise : h.defaultResponse(command, query));
      const opening = h.ui.open(row); await tick();
      const vendorUi = new ThreadViewDom(new Element("section", h.doc) as any, h.client, { accounts: () => [], onExit: () => {} });
      const history = new ThreadHistory(vendorUi.state, h.ui.state);
      if (leave === "back") h.ui.back(); else { h.snapshot.visible = false; history.setView("settings", true); }
      const focus = h.doc.activeElement;
      pending.resolve(await h.defaultResponse(pendingCommand, {})); await opening;
      assert.equal(h.ui.state.inboxOrigin, null); assert.equal(h.root.hidden, true); assert.equal(h.columns.hidden, false);
      assert.equal(h.doc.activeElement, focus);
      h.snapshot.visible = true; history.setView("inbox", true); await tick();
      assert.equal(h.root.hidden, true);
      assert.equal(h.calls.filter(c => c.command === "threads_messages").length, pendingCommand === "threads_messages" ? 1 : 0);
    }
  }
});

test("complete-span source revalidation clears bodies and rejects removed origins without auto paging", async () => {
  const h = harness(); await h.ui.open(row); await h.ui.state.openBody("seed");
  assert.ok(text(h.root).includes(body));
  const pending = deferred(); h.respond(async (command, query) => command === "threads_body" ? pending.promise : h.defaultResponse(command, query));
  h.ui.state.closeBody("seed"); const loading = h.ui.state.openBody("seed");
  h.snapshot.spanScope = null; h.ui.state.invalidate(); assert.equal(text(h.root).includes(body), false);
  h.snapshot.items = []; h.snapshot.spanScope = h.snapshot.scope;
  h.ui.revalidateSource(); await h.ui.state.refresh();
  pending.resolve(await h.defaultResponse("threads_body", { message_id: "seed" })); await loading;
  assert.match(text(h.root), /originating message is unavailable in the current Inbox results/);
  assert.equal(h.ui.state.bodies.size, 0); assert.equal(text(h.root).includes(body), false);
  assert.equal(h.calls.filter(c => c.command === "threads_resolve").length, 1);
  h.snapshot.items = [row]; h.button("Retry thread").click(); await tick(); await tick();
  assert.equal(h.refreshes(), 1); assert.match(text(h.root), /Opened from Inbox/);
});

test("new mailbox span rebinds only after invalidation; old message and body responses cannot reappear", async () => {
  for (const command of ["threads_messages", "threads_body"]) {
    const h = harness(); await h.ui.open(row); const pending = deferred();
    h.respond(async (name, query) => name === command ? pending.promise : h.defaultResponse(name, query));
    const loading = command === "threads_messages" ? h.ui.state.loadMessages() : h.ui.state.openBody("seed");
    h.snapshot.scope = { mutation_epoch: 3, mailbox_operation_revision: 10 }; h.snapshot.spanScope = null;
    h.ui.state.invalidate(); h.snapshot.spanScope = h.snapshot.scope; h.ui.revalidateSource();
    pending.resolve(await h.defaultResponse(command, { message_id: "seed" })); await loading;
    assert.equal(h.ui.state.messages.value, null); assert.equal(h.ui.state.bodies.size, 0);
    h.respond(h.defaultResponse); await h.ui.state.refresh();
    assert.match(text(h.root), /Opened from Inbox/); assert.equal(text(h.root).includes(body), false);
  }
});

test("nested holds block all entry/children until source span commits and both holds release", async () => {
  const h = harness(); await h.ui.open(row); await h.ui.state.openBody("seed");
  const vendorUi = new ThreadViewDom(new Element("section", h.doc) as any, h.client, { accounts: () => [], onExit: () => {} });
  const history = new ThreadHistory(vendorUi.state, h.ui.state);
  history.beginMutation(); history.beginMutation(); h.snapshot.blocked = true;
  assert.equal(text(h.root).includes(body), false); assert.equal(h.ui.canOpen(row), false);
  h.snapshot.scope = { mutation_epoch: 3, mailbox_operation_revision: 10 }; h.snapshot.spanScope = h.snapshot.scope;
  const count = h.calls.length; h.ui.revalidateSource(); await history.commitInboxSpan();
  await h.ui.state.openBody("seed"); await history.endMutation();
  assert.equal(history.mutationHeld, true); assert.equal(h.calls.length, count);
  h.snapshot.blocked = false; await history.endMutation();
  assert.equal(history.mutationHeld, false); assert.equal(h.calls.length, count + 2);
});

test("paged chooser shows owned/linked scope; lost explicit choice errors until fresh-origin Retry", async () => {
  const h = harness(); let lost = false;
  h.respond(async (command, query) => {
    if (command !== "threads_resolve") return { thread: { ...thread, relation: "linked", owner_vendor: vendor }, items: [message], next_cursor: null };
    if (query.vendor_id) {
      if (lost) throw { code: "not_found" };
      return { state: "resolved", message_ref: origin, context: { kind: "vendor", vendor: linked,
        thread: { ...thread, relation: "linked", owner_vendor: vendor } } };
    }
    return { state: "choose_vendor", message_ref: origin, thread_ref: ref, owner_vendor: vendor,
      items: query.cursor ? [linked] : [vendor], next_cursor: query.cursor ? null : "next-vendor" };
  });
  await h.ui.open(row); assert.match(text(h.root), /Choose a vendor/); assert.match(text(h.root), /Owned/);
  assert.equal(h.calls.length, 1); h.button("Load more vendors").click(); await tick();
  assert.equal(h.calls[1].query.cursor, "next-vendor"); assert.ok(text(h.root).includes(`Linked · Owner: ${vendor.display_name}`));
  h.button(linked.display_name).click(); await tick();
  assert.equal(h.calls[2].query.vendor_id, linked.vendor_id); assert.match(text(h.root), /Opened from Inbox/);
  assert.equal(h.doc.activeElement.dataset.threadFocus, "inbox-thread:message-heading");
  lost = true; h.button("Refresh local history").click(); await tick(); await tick();
  assert.match(text(h.root), /Could not load retained thread/); assert.doesNotMatch(text(h.root), /Opened from Inbox/);
  const before = h.calls.length; h.button("Retry thread").click(); await tick(); await tick();
  assert.ok(h.calls.slice(before).some(c => c.command === "threads_resolve" && !Object.hasOwn(c.query, "vendor_id")));
  assert.match(text(h.root), /Choose a vendor/); assert.equal(h.ui.state.selectedThread, null);
});

test("origin marker follows explicit thread paging and independent readers have unique region/focus IDs", async () => {
  const h = harness();
  h.respond(async (command, query) => command === "threads_messages"
    ? { thread, items: [query.cursor ? message : { ...message, message_id: "earlier" }], next_cursor: query.cursor ? null : "next" }
    : h.defaultResponse(command, query));
  await h.ui.open(row); assert.match(text(h.root), /not on the loaded thread pages yet/);
  assert.equal(h.calls.length, 2); h.button("Load more messages").click(); await tick();
  assert.match(text(h.root), /Opened from Inbox/); assert.doesNotMatch(text(h.root), /not on the loaded/);
  assert.deepEqual(h.ui.state.messages.value!.items.map(x => x.message_id), ["earlier", "seed"]);
  const vendorRoot = new Element("section", h.doc);
  const vendorUi = new ThreadViewDom(vendorRoot as any, new ThreadReadClient(async <T>(command) => (command === "threads_list"
    ? { items: [{ ...thread, relation: "owned", owner_vendor: vendor }], next_cursor: null }
    : { thread: { ...thread, relation: "owned", owner_vendor: vendor }, items: [message], next_cursor: null }) as T),
  { accounts: () => [], onExit: () => {} });
  await vendorUi.open(vendor); await vendorUi.state.openThread({ ...thread, relation: "owned", owner_vendor: vendor });
  const all = [...nodes(h.root), ...nodes(vendorRoot)], ids = all.filter(n => n.id).map(n => n.id);
  assert.equal(new Set(ids).size, ids.length);
  const keys = all.map(n => n.dataset.threadFocus).filter(Boolean); assert.equal(new Set(keys).size, keys.length);
  for (const root of [h.root, vendorRoot]) for (const control of nodes(root).filter(n => n.attributes.has("aria-controls"))) {
    assert.ok(nodes(root).some(n => n.id === control.attributes.get("aria-controls")));
  }
  h.button("Show body").click(); await tick();
  assert.equal(nodes(h.root).filter(n => ["SCRIPT", "IMG", "A", "IFRAME"].includes(n.tagName)).length, 0);
  assert.equal(vendorUi.state.bodies.size, 0);
});

test("a late Retry continuation cannot overwrite a newer explicit vendor choice or Inbox entry", async () => {
  for (const supersede of ["choice", "entry", "back"]) {
    const h = harness(), pending = deferred();
    h.respond(async (command, query) => command === "threads_resolve" && !query.vendor_id
      ? { state: "choose_vendor", message_ref: origin, thread_ref: ref, owner_vendor: vendor, items: [vendor, linked], next_cursor: null }
      : command === "threads_resolve" ? { state: "resolved", message_ref: origin,
        context: { kind: "vendor", vendor: linked, thread: { ...thread, relation: "linked", owner_vendor: vendor } } }
      : h.defaultResponse(command, query));
    await h.ui.open(row);
    h.afterRefresh(() => pending.promise);
    const retry = h.ui.retry(); await tick(); await tick();
    if (supersede === "choice") { h.button(linked.display_name).click(); await tick(); }
    else if (supersede === "entry") await h.ui.open(row);
    else h.ui.back();
    const count = h.calls.length; pending.resolve(undefined); await retry;
    assert.equal(h.calls.length, count, supersede);
    if (supersede === "choice") assert.equal(h.ui.state.vendor?.vendor_id, linked.vendor_id);
    if (supersede === "back") assert.equal(h.root.hidden, true);
  }
});


test("a fresh partial row cannot impersonate an origin whose mailbox identity is explicitly null", async () => {
  const h = harness();
  const nullable = { ...origin, mailbox_identity_key: null };
  const valid = { ...row, thread_entry_ref: nullable };
  h.snapshot.items = [valid];
  assert.equal(h.ui.canOpen(valid), true, "explicit null is an admitted native identity");
  const partial: any = { ...valid, thread_entry_ref: { ...nullable } };
  delete partial.thread_entry_ref.mailbox_identity_key;
  h.snapshot.items = [partial];
  assert.equal(h.ui.canOpen(valid), false, "missing is not explicit null");
  await h.ui.open(valid); assert.equal(h.calls.length, 0);
});

test("complete origin admission rejects partial and falsy fields without coercing native identity", () => {
  for (const key of ["provider", "account_id", "message_id", "mailbox_identity_key"]) {
    for (const value of [undefined, null, "", false, 0]) {
      const h = harness();
      const candidate: any = { ...row, thread_entry_ref: { ...origin, [key]: value } };
      Object.assign(candidate, { provider: candidate.thread_entry_ref.provider,
        account_id: candidate.thread_entry_ref.account_id, message_id: candidate.thread_entry_ref.message_id });
      h.snapshot.items = [candidate];
      assert.equal(h.ui.canOpen(candidate), key === "mailbox_identity_key" && value === null, `${key}/${value}`);
    }
  }
  const h = harness(); assert.equal(h.ui.canOpen(row), true);
});

test("pending read cannot commit when a nullable origin is replaced by a partial fresh row", async () => {
  const h = harness(), pending = deferred();
  const nullable = { ...origin, mailbox_identity_key: null };
  const valid = { ...row, thread_entry_ref: nullable };
  h.snapshot.items = [valid];
  h.respond(async (command, query) => command === "threads_resolve" ? pending.promise : h.defaultResponse(command, query));
  const opening = h.ui.open(valid);
  assert.equal(h.calls.length, 1);
  const partial: any = { ...valid, thread_entry_ref: { ...nullable } };
  delete partial.thread_entry_ref.mailbox_identity_key;
  h.snapshot.items = [partial];
  pending.resolve({ state: "resolved", message_ref: nullable, context: { kind: "non_vendor", thread } });
  await opening;
  assert.equal(h.calls.length, 1, "incomplete fresh row cannot authorize the message read");
  assert.equal(h.ui.state.selectedThread, null);
});
