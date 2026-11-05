import assert from "node:assert/strict";
import test from "node:test";
import { ThreadReadClient } from "../src/threadReads.ts";
import { ThreadViewState, threadBodyDisplay, threadBodyStateLabel } from "../src/threadView.ts";
import { ThreadHistory } from "../src/threadHistory.ts";

const origin = { provider: "imap", account_id: "retained", mailbox_identity_key: null, message_id: "seed" };
const ref = { provider: "imap", account_id: "retained", mailbox_identity_key: null, thread_key: "alias" };
const vendor = { vendor_id: "v", display_name: "Vendor" };
const other = { vendor_id: "other", display_name: "Other vendor" };
const item = (vendorMode: boolean) => ({ thread_ref: ref, relation: vendorMode ? "owned" : "non_vendor",
  owner_vendor: vendorMode ? vendor : null, subject: "Newest", latest_received_at: null, message_count: 2 });
const resolved = (vendorMode = false, message_ref = origin) => ({ state: "resolved", message_ref,
  context: vendorMode ? { kind: "vendor", vendor, thread: item(true) } : { kind: "non_vendor", thread: item(false) } });
const chooser = (items = [vendor], next_cursor: string | null = "next") => ({ state: "choose_vendor",
  message_ref: origin, thread_ref: ref, owner_vendor: vendor, items, next_cursor });
const messages = (vendorMode = false, next_cursor: string | null = null) => ({
  thread: { thread_ref: { ...ref, thread_key: "survivor" }, relation: vendorMode ? "owned" : "non_vendor",
    owner_vendor: vendorMode ? vendor : null, subject: "Newest", history_notice: "Local history" },
  items: [{ message_id: "seed", subject: "Older", direction: "inbound", sender: "sender@example.test",
    sender_name: null, attributed_vendor: null, received_at: null, body_state: { state: "not_followed" }, copy_count: 1 }],
  next_cursor,
});
const body = { thread_ref: ref, message_id: "seed", body: { state: "not_followed" } };
const copies = (next_cursor: string | null = null) => ({ thread_ref: ref, message_id: "seed",
  items: [{ copy_ref: "copy", locations: ["INBOX"], summary: null, attachment_count: 1 }], next_cursor });
const inventory = { thread_ref: ref, message_id: "seed", items: [], next_cursor: null };
const tick = () => new Promise<void>(resolve => setImmediate(resolve));
function harness() {
  const calls: { command: string; query: any; resolve: (value: any) => void; reject: (error: any) => void }[] = [];
  let onPaint = () => {};
  const state = new ThreadViewState(new ThreadReadClient(<T>(command: string, args: Record<string, unknown>) =>
    new Promise<T>((resolve, reject) => calls.push({ command, query: structuredClone(args.query), resolve, reject }))),
  () => onPaint());
  return { state, calls, onPaint: (fn: () => void) => { onPaint = fn; } };
}
async function enter(h: ReturnType<typeof harness>, vendorMode = false, current = () => true) {
  const pending = h.state.openInbox(origin, current);
  assert.equal(h.calls.at(-1)!.command, "threads_resolve");
  h.calls.at(-1)!.resolve(resolved(vendorMode)); await tick();
  assert.equal(h.calls.at(-1)!.command, "threads_messages");
  h.calls.at(-1)!.resolve(messages(vendorMode, "messages-next")); await pending;
}

test("zero/one vendor entries load directly and guard every child with a cloned complete origin", async () => {
  for (const vendorMode of [false, true]) {
    const h = harness(); await enter(h, vendorMode);
    assert.deepEqual(h.calls.map(c => c.command), ["threads_resolve", "threads_messages"]);
    const selector = (thread_ref: typeof ref) => vendorMode
      ? { vendor_id: "v", thread_ref, inbox_message: origin }
      : { inbox_thread: { thread_ref, message_ref: origin } };
    assert.deepEqual(h.calls[1].query, { ...selector(ref), cursor: null });
    const child = { ...selector({ ...ref, thread_key: "survivor" }), message_id: "seed" };
    let pending = h.state.openBody("seed"); assert.deepEqual(h.calls.at(-1)!.query, child);
    h.calls.at(-1)!.resolve(body); await pending;
    pending = h.state.openCopies("seed"); assert.deepEqual(h.calls.at(-1)!.query, { ...child, cursor: null });
    h.calls.at(-1)!.resolve(copies("copies-next")); await pending;
    pending = h.state.openInventory("seed", "copy");
    assert.deepEqual(h.calls.at(-1)!.query, { ...child, copy_ref: "copy", cursor: null });
    h.calls.at(-1)!.resolve(inventory); await pending;
    pending = h.state.loadCopies("seed", true);
    assert.deepEqual(h.calls.at(-1)!.query, { ...child, cursor: "copies-next" });
    h.calls.at(-1)!.resolve(copies()); await pending;
    pending = h.state.loadMessages(true);
    assert.deepEqual(h.calls.at(-1)!.query, { ...selector(ref), cursor: "messages-next" });
    h.calls.at(-1)!.resolve(messages(vendorMode)); await pending;
    const before = h.calls.length;
    await h.state.selectAccount({ provider: "gmail", account_id: "unrelated" }); await h.state.loadThreads();
    await h.state.openBody("unloaded-message");
    assert.equal(h.calls.length, before);
    const copy = h.state.inboxOrigin!; copy.account_id = "changed";
    assert.deepEqual(h.state.inboxOrigin, origin);
  }
});

test("chooser pages append, require a current candidate, restart stale cursors once and never pick a fallback", async () => {
  const h = harness(); let pending = h.state.openInbox(origin, () => true);
  h.calls[0].resolve(chooser()); await pending;
  assert.equal(h.state.selectedThread, null);
  await h.state.chooseVendor("absent"); assert.equal(h.calls.length, 1);
  pending = h.state.resolveInbox(true); const count = h.calls.length;
  await h.state.resolveInbox(true); assert.equal(h.calls.length, count);
  assert.deepEqual(h.calls.at(-1)!.query, { message_ref: origin, cursor: "next" });
  h.calls.at(-1)!.resolve(chooser([other], "last")); await pending;
  assert.deepEqual(h.state.resolution.value?.state === "choose_vendor" && h.state.resolution.value.items, [vendor, other]);
  pending = h.state.resolveInbox(true); h.calls.at(-1)!.reject({ code: "stale_cursor" }); await tick();
  assert.equal(h.state.resolution.value, null); assert.equal(h.state.resolution.changed, true);
  assert.deepEqual(h.calls.at(-1)!.query, { message_ref: origin, cursor: null });
  h.calls.at(-1)!.resolve(chooser([other], null)); await pending;
  pending = h.state.chooseVendor("other");
  assert.deepEqual(h.calls.at(-1)!.query, { message_ref: origin, vendor_id: "other" });
  const before = h.calls.length; h.calls.at(-1)!.reject({ code: "stale_selection" }); await pending;
  assert.equal(h.calls.length, before); assert.equal(h.state.resolution.status, "error");
  assert.equal(h.state.selectedThread, null); assert.equal(h.state.messages.value, null);
});

test("resolver responses cannot chain after replacement, external query/revision invalidation or reentrant hide", async () => {
  for (const settleOldFirst of [false, true]) {
    const h = harness();
    const old = h.state.openInbox(origin, () => true), oldCall = h.calls[0];
    const freshOrigin = { ...origin, account_id: "other", mailbox_identity_key: "a".repeat(64), message_id: "new" };
    const fresh = h.state.openInbox(freshOrigin, () => true), freshCall = h.calls[1];
    if (settleOldFirst) { oldCall.resolve(resolved()); await old; assert.equal(h.calls.length, 2); }
    freshCall.resolve(chooser([], null)); await fresh;
    if (!settleOldFirst) { oldCall.resolve(resolved()); await old; }
    assert.deepEqual(h.state.inboxOrigin, freshOrigin); assert.equal(h.calls.length, 2);
  }
  for (const invalidate of ["query", "revision", "hidden", "back", "paint"]) {
    const h = harness(); let current = true;
    const pending = h.state.openInbox(origin, () => current);
    if (invalidate === "hidden") h.state.suspend();
    else if (invalidate === "back") await h.state.backToThreads();
    else if (invalidate === "paint") h.onPaint(() => h.state.suspend());
    else current = false;
    // Avoid recursive notification while testing invalidation from the commit notification.
    if (invalidate === "paint") h.onPaint(() => { h.onPaint(() => {}); h.state.suspend(); });
    h.calls[0].resolve(resolved()); await pending;
    assert.equal(h.calls.length, 1, invalidate); assert.equal(h.state.selectedThread, null);
    assert.equal(h.state.messages.value, null);
  }
});

test("a repeated resolution supersedes an older request even within the same entry", async () => {
  const h = harness(); const old = h.state.openInbox(origin, () => true);
  const fresh = h.state.resolveInbox();
  h.calls[1].resolve(resolved()); await tick();
  h.calls[2].resolve(messages()); await fresh;
  h.calls[0].resolve(resolved()); await tick();
  assert.equal(h.calls.length, 3, "older resolver must not launch another message read");
  await old;
  assert.equal(h.state.messages.status, "ready");
});

test("every Inbox child fences late success/errors and current stale selection clears the whole reader", async () => {
  for (const kind of ["messages", "body", "copies", "inventory"]) {
    for (const late of [false, true]) {
      const h = harness(); await enter(h, true);
      if (kind === "inventory") { const p = h.state.openCopies("seed"); h.calls.at(-1)!.resolve(copies()); await p; }
      const pending = kind === "messages" ? h.state.loadMessages(true) : kind === "body" ? h.state.openBody("seed")
        : kind === "copies" ? h.state.openCopies("seed") : h.state.openInventory("seed", "copy");
      const call = h.calls.at(-1)!;
      if (late) {
        const fresh = h.state.openInbox({ ...origin, message_id: "new" }, () => true);
        h.calls.at(-1)!.resolve(chooser()); await fresh;
      }
      const before = h.calls.length;
      call.reject({ code: "stale_selection" }); await pending;
      assert.equal(h.calls.length, before); assert.equal(h.state.messages.value, null);
      assert.equal(h.state.selectedThread, null); assert.equal(h.state.bodies.size, 0);
      assert.equal(h.state.copies.size, 0); assert.equal(h.state.inventory.size, 0);
      assert.equal(h.state.resolution.status, late ? "ready" : "error", `${kind}, late=${late}`);
    }
    const h = harness(); await enter(h);
    if (kind === "inventory") { const p = h.state.openCopies("seed"); h.calls.at(-1)!.resolve(copies()); await p; }
    const pending = kind === "messages" ? h.state.loadMessages(true) : kind === "body" ? h.state.openBody("seed")
      : kind === "copies" ? h.state.openCopies("seed") : h.state.openInventory("seed", "copy");
    const call = h.calls.at(-1)!; h.state.beginMutation();
    call.resolve(kind === "messages" ? messages() : kind === "body" ? body : kind === "copies" ? copies() : inventory);
    await pending; assert.equal(h.state.messages.value, null); assert.equal(h.state.bodies.size, 0);
    assert.equal(h.state.copies.size, 0); assert.equal(h.state.inventory.size, 0);
  }
});

test("refresh retains an explicit vendor choice and child stale-cursor restart retains origin and mode", async () => {
  const h = harness(); await enter(h, true);
  let pending = h.state.loadMessages(true); h.calls.at(-1)!.reject({ code: "stale_cursor" }); await tick();
  assert.deepEqual(h.calls.at(-1)!.query, { vendor_id: "v", thread_ref: ref, inbox_message: origin, cursor: null });
  assert.equal(h.state.messages.changed, true);
  const before = h.calls.length; h.calls.at(-1)!.reject({ code: "stale_cursor" }); await pending;
  assert.equal(h.calls.length, before); assert.equal(h.state.messages.status, "error");
  pending = h.state.refresh(); assert.equal(h.state.selectedThread, null);
  assert.deepEqual(h.calls.at(-1)!.query, { message_ref: origin, vendor_id: "v" });
  h.calls.at(-1)!.reject({ code: "stale_selection" }); await pending;
  assert.equal(h.state.resolution.status, "error");
  assert.equal(threadBodyDisplay({ state: "not_followed" }).text, null);
  assert.equal(threadBodyStateLabel({ state: "not_followed" }), "Body not stored (thread not followed)");
  for (const field of ["text", "stored_chars", "source_chars", "marker"]) {
    assert.throws(() => threadBodyDisplay({ state: "not_followed", [field]: "residual" }));
  }
});

test("shared lifecycle keeps readers independent and waits for a committed span plus all mutation holds", async () => {
  const v = harness(), i = harness(); const history = new ThreadHistory(v.state, i.state);
  await enter(i);
  const vendorOpen = v.state.selectVendor(vendor); v.calls.at(-1)!.resolve({ items: [], next_cursor: null }); await vendorOpen;
  history.setView("inbox", true); i.calls.at(-1)!.resolve(chooser()); await tick();
  assert.equal(v.state.threads.value, null); assert.equal(i.state.resolution.status, "ready");
  history.beginMutation(); history.beginMutation(); const before = i.calls.length;
  await history.commitInboxSpan(); await history.endMutation(); assert.equal(i.calls.length, before);
  const release = history.endMutation(); await tick(); assert.equal(i.calls.length, before + 1);
  i.calls.at(-1)!.resolve(chooser()); await release;
  history.prepareInboxRefresh(); assert.equal(i.state.resolution.value, null);
  assert.equal(i.calls.length, before + 1);
  const committed = history.commitInboxSpan(); assert.equal(i.calls.length, before + 2);
  i.calls.at(-1)!.resolve(chooser()); await committed;
  history.beginMutation(); await history.endMutation(); assert.equal(i.calls.length, before + 2);
  history.setView("settings", true); history.refresh(); assert.equal(i.calls.length, before + 2);
  assert.equal(i.state.messages.value, null); assert.equal(v.state.threads.value, null);
});
