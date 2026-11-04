import assert from "node:assert/strict";
import test from "node:test";
import { ThreadReadClient } from "../src/threadReads.ts";
import type { ThreadItem, ThreadMessage, ThreadRef } from "../src/threadReads.ts";
import { ThreadViewState, threadBodyDisplay } from "../src/threadView.ts";

const ref: ThreadRef = { provider: "imap", account_id: "account", mailbox_identity_key: null, thread_key: "thread" };
const vendor = { vendor_id: "vendor", display_name: "Vendor" };
const item = (thread_ref = ref): ThreadItem => ({ thread_ref, relation: "owned", owner_vendor: vendor,
  subject: "Subject", latest_received_at: null, message_count: 2 });
const message = (message_id = "canonical"): ThreadMessage => ({ message_id, subject: "Subject", direction: "inbound",
  sender: "sender@example.test", sender_name: null, attributed_vendor: null, received_at: null,
  body_state: { state: "stored", stored_chars: 4, source_chars: 4 }, copy_count: 2 });
const threads = (items = [item()], next_cursor: string | null = null) => ({ items, next_cursor });
const messages = (items = [message()], next_cursor: string | null = null, thread_ref = ref) => ({
  thread: { thread_ref, relation: "owned", owner_vendor: vendor, history_notice: "History partial" }, items, next_cursor,
});
const body = (text = "body") => ({ thread_ref: ref, message_id: "canonical",
  body: { state: "stored", text, stored_chars: [...text].length, source_chars: [...text].length } });
const copies = (copy_ref = "source", next_cursor: string | null = null) => ({ thread_ref: ref, message_id: "canonical",
  items: [{ copy_ref, locations: ["INBOX"], summary: "Copy summary", attachment_count: 1 }], next_cursor });
function harness() {
  const calls: { command: string; query: any; resolve: (value: any) => void; reject: (error: any) => void }[] = [];
  const client = new ThreadReadClient(<T>(command: string, args: Record<string, unknown>) => new Promise<T>((resolve, reject) => {
    calls.push({ command, query: structuredClone(args.query), resolve, reject });
  }));
  let paints = 0;
  return { calls, view: new ThreadViewState(client, () => { paints += 1; }), paints: () => paints };
}
async function enter(h: ReturnType<typeof harness>) {
  const pending = h.view.selectVendor(vendor); h.calls.at(-1)!.resolve(threads()); await pending;
  const opened = h.view.openThread(item()); h.calls.at(-1)!.resolve(messages()); await opened;
}

test("vendor/account/thread changes clear synchronously and fence every complete scope", async () => {
  const h = harness();
  const oldVendor = h.view.selectVendor(vendor);
  const newVendor = h.view.selectVendor({ vendor_id: "other", display_name: "Other" });
  h.calls[1].resolve(threads()); await newVendor;
  h.calls[0].resolve(threads([item({ ...ref, thread_key: "old-vendor" })])); await oldVendor;
  assert.equal(h.view.vendor?.vendor_id, "other");
  assert.equal(h.view.threads.value?.items[0].thread_ref.thread_key, "thread");
  const account = h.view.selectAccount({ provider: "gmail", account_id: "disconnected" });
  assert.equal(h.view.threads.value, null);
  assert.deepEqual(h.calls.at(-1)!.query, { vendor_id: "other", provider: "gmail", account_id: "disconnected", cursor: null });
  h.calls.at(-1)!.resolve(threads()); await account;
  for (const changed of [
    { ...ref, provider: "gmail" }, { ...ref, account_id: "other-account" },
    { ...ref, mailbox_identity_key: "a".repeat(64) }, { ...ref, thread_key: "other-thread" },
  ]) {
    const before = h.calls.length;
    const old = h.view.openThread(item(ref));
    const fresh = h.view.openThread(item(changed));
    assert.equal(h.view.messages.value, null);
    h.calls[before + 1].resolve(messages([message("fresh")], null, changed)); await fresh;
    h.calls[before].resolve(messages([message("old")])); await old;
    assert.equal(h.view.messages.value?.items[0].message_id, "fresh");
    assert.deepEqual(h.calls[before + 1].query.thread_ref, changed);
  }
});

test("collapse, reexpand, thread change and suspension cannot resurrect body text", async () => {
  const h = harness(); await enter(h);
  const first = h.view.openBody("canonical"); const firstCall = h.calls.at(-1)!;
  h.view.closeBody("canonical");
  const second = h.view.openBody("canonical"); const secondCall = h.calls.at(-1)!;
  secondCall.resolve(body("fresh")); await second;
  firstCall.resolve(body("old")); await first;
  assert.equal(h.view.bodies.get("canonical")?.value?.body.state, "stored");
  assert.deepEqual(h.view.bodies.get("canonical")?.value, body("fresh"));
  h.view.closeBody("canonical");
  const late = h.view.openBody("canonical"); const lateCall = h.calls.at(-1)!;
  h.view.suspend();
  assert.equal(h.view.bodies.size, 0);
  assert.equal(h.view.messages.value, null);
  lateCall.resolve(body()); await late;
  assert.equal(h.view.bodies.size, 0);
  const resume = h.view.resume(); h.calls.at(-1)!.resolve(messages()); await resume;
  assert.equal(h.view.messages.value?.items.length, 1);
});

test("mutation holds clear all pages and children, defer reads, and revalidate once settled", async () => {
  const h = harness(); await enter(h);
  const text = h.view.openBody("canonical"); h.calls.at(-1)!.resolve(body()); await text;
  const pending = h.view.openCopies("canonical"); const old = h.calls.at(-1)!;
  h.view.beginMutation(); h.view.beginMutation();
  assert.equal(h.view.messages.value, null);
  assert.equal(h.view.bodies.size, 0);
  assert.equal(h.view.copies.size, 0);
  const count = h.calls.length;
  await h.view.refresh(); await h.view.endMutation();
  assert.equal(h.calls.length, count);
  old.resolve(copies()); await pending;
  const settled = h.view.endMutation();
  assert.equal(h.calls.length, count + 1);
  h.calls.at(-1)!.reject({ code: "not_found" }); await settled;
  assert.equal(h.view.messages.status, "error");
  assert.equal(h.view.messages.value, null);
  assert.equal(h.view.bodies.size, 0);
});

test("pages append stable data; stale cursors restart only their current list once", async () => {
  const h = harness();
  const start = h.view.selectVendor(vendor); h.calls.at(-1)!.resolve(threads([item()], "page2")); await start;
  const more = h.view.loadThreads(true); const failed = h.calls.at(-1)!;
  assert.equal(failed.query.cursor, "page2");
  failed.reject({ code: "stale_cursor" }); await Promise.resolve(); await Promise.resolve();
  assert.equal(h.view.threads.value, null);
  assert.equal(h.calls.at(-1)!.query.cursor, null);
  h.calls.at(-1)!.resolve(threads([item({ ...ref, thread_key: "new" })], "page3")); await more;
  assert.equal(h.view.threads.changed, true);
  assert.deepEqual(h.view.threads.value?.items.map(i => i.thread_ref.thread_key), ["new"]);
  const append = h.view.loadThreads(true);
  h.calls.at(-1)!.resolve(threads([item({ ...ref, thread_key: "last" })])); await append;
  assert.deepEqual(h.view.threads.value?.items.map(i => i.thread_ref.thread_key), ["new", "last"]);
  const error = h.view.loadThreads(); h.calls.at(-1)!.reject({ code: "stale_cursor" }); await error;
  assert.equal(h.view.threads.status, "error");
  assert.equal(h.view.threads.value, null);
});

test("late stale errors cannot restart a superseded selection; duplicate appends are ignored", async () => {
  const h = harness();
  const start = h.view.selectVendor(vendor); h.calls.at(-1)!.resolve(threads([item()], "next")); await start;
  const append = h.view.loadThreads(true); const old = h.calls.at(-1)!;
  await h.view.loadThreads(true);
  const count = h.calls.length;
  const change = h.view.selectAccount({ provider: "imap", account_id: "new" });
  old.reject({ code: "stale_cursor" }); await append;
  assert.equal(h.calls.length, count + 1);
  h.calls.at(-1)!.resolve(threads([])); await change;
  assert.deepEqual(h.view.threads.value?.items, []);
});

test("resolved aliases scope child reads while message cursors keep their request binding", async () => {
  const h = harness();
  const start = h.view.selectVendor(vendor); h.calls.at(-1)!.resolve(threads()); await start;
  const resolved = { ...ref, thread_key: "survivor" };
  const opened = h.view.openThread(item()); h.calls.at(-1)!.resolve(messages([message()], "next", resolved)); await opened;
  const text = h.view.openBody("canonical"); assert.deepEqual(h.calls.at(-1)!.query.thread_ref, resolved);
  h.calls.at(-1)!.resolve({ ...body(), thread_ref: resolved }); await text;
  const more = h.view.loadMessages(true); assert.deepEqual(h.calls.at(-1)!.query.thread_ref, ref);
  h.calls.at(-1)!.resolve(messages([message("second")], null, resolved)); await more;
  assert.deepEqual(h.view.messages.value?.items.map(m => m.message_id), ["canonical", "second"]);
  const back = h.view.backToThreads();
  assert.equal(h.view.selectedThread, null);
  assert.equal(h.view.vendor?.vendor_id, "vendor");
  assert.equal(h.view.bodies.size, 0);
  h.calls.at(-1)!.resolve(threads()); await back;
});

test("copy and inventory lifetimes are independent and collapse fences late pages", async () => {
  const h = harness(); await enter(h);
  const open = h.view.openCopies("canonical"); h.calls.at(-1)!.resolve(copies("source", "next")); await open;
  const inventory = h.view.openInventory("canonical", "source"); const inventoryCall = h.calls.at(-1)!;
  assert.equal(inventoryCall.query.copy_ref, "source");
  const page = h.view.loadCopies("canonical", true); const pageCall = h.calls.at(-1)!;
  h.view.closeCopies("canonical");
  inventoryCall.resolve({ ...copies(), items: [{ part_id: "1", filename: "file", media_type: "text/plain", byte_size: null }] });
  pageCall.resolve(copies("late")); await Promise.all([inventory, page]);
  assert.equal(h.view.copies.size, 0);
  assert.equal(h.view.inventory.size, 0);
  const count = h.calls.length;
  await h.view.openBody("physical-copy-id"); await h.view.openCopies("physical-copy-id");
  await h.view.openInventory("canonical", "missing-copy");
  assert.equal(h.calls.length, count);
});

test("body labels and Unicode counts are exact; malformed states clear previous text as errors", async () => {
  const labels = {
    pending: "Body not stored yet", source_gone: "Body not stored (source no longer available)",
    outside_folders: "Body not stored (outside the admitted folders)",
    removal_pending: "Body removal pending until the local upgrade completes",
    mailbox_identity_unavailable: "Body not stored (mailbox identity unavailable)",
  };
  for (const [state, label] of Object.entries(labels)) assert.deepEqual(threadBodyDisplay({ state }), { label, text: null });
  assert.deepEqual(threadBodyDisplay({ state: "stored", text: "", stored_chars: 0, source_chars: 0 }), { label: null, text: "" });
  assert.deepEqual(threadBodyDisplay({ state: "stored", text: "🙂e\u0301", stored_chars: 3, source_chars: 8 }),
    { label: "Partial body: first 3 of 8 characters", text: "🙂e\u0301" });
  for (const invalid of [null, {}, { state: "unknown", text: "hidden" }, { state: "pending", text: "hidden" },
    { state: "stored", text: "🙂", stored_chars: 2, source_chars: 2 },
    { state: "stored", text: "x".repeat(200_001), stored_chars: 200_001, source_chars: 200_001 }]) {
    assert.throws(() => threadBodyDisplay(invalid));
    const h = harness(); await enter(h);
    const good = h.view.openBody("canonical"); h.calls.at(-1)!.resolve(body()); await good;
    const bad = h.view.openBody("canonical");
    assert.equal(h.view.bodies.get("canonical")?.value, null);
    h.calls.at(-1)!.resolve({ ...body(), body: invalid }); await bad;
    assert.equal(h.view.bodies.get("canonical")?.status, "error");
    assert.equal(h.view.bodies.get("canonical")?.value, null);
  }
});

test("opening a thread cancels parent page retries; malformed message metadata clears children", async () => {
  const h = harness();
  const start = h.view.selectVendor(vendor); h.calls.at(-1)!.resolve(threads([item()], "next")); await start;
  const parent = h.view.loadThreads(true); const parentCall = h.calls.at(-1)!;
  const open = h.view.openThread(item()); const count = h.calls.length;
  parentCall.reject({ code: "stale_cursor" }); await parent;
  assert.equal(h.calls.length, count);
  h.calls.at(-1)!.resolve(messages([message()], "next")); await open;
  const text = h.view.openBody("canonical"); h.calls.at(-1)!.resolve(body()); await text;
  const append = h.view.loadMessages(true);
  h.calls.at(-1)!.resolve(messages([{ ...message("second"), body_state: { state: "pending", text: "hidden" } } as any])); await append;
  assert.equal(h.view.messages.status, "error");
  assert.equal(h.view.messages.value, null);
  assert.equal(h.view.bodies.size, 0);
});

test("message and copy stale cursors clear child reads and restart their exact selector", async () => {
  const h = harness();
  const start = h.view.selectVendor(vendor); h.calls.at(-1)!.resolve(threads()); await start;
  const open = h.view.openThread(item()); h.calls.at(-1)!.resolve(messages([message()], "next-message")); await open;
  const text = h.view.openBody("canonical"); const textCall = h.calls.at(-1)!;
  const next = h.view.loadMessages(true); const messageQuery = h.calls.at(-1)!.query;
  h.calls.at(-1)!.reject({ code: "stale_cursor" }); await Promise.resolve(); await Promise.resolve();
  assert.equal(h.view.bodies.size, 0);
  assert.deepEqual(h.calls.at(-1)!.query, { ...messageQuery, cursor: null });
  h.calls.at(-1)!.resolve(messages()); await next;
  textCall.resolve(body()); await text;
  assert.equal(h.view.bodies.size, 0);
  const copy = h.view.openCopies("canonical"); h.calls.at(-1)!.resolve(copies("source", "next-copy")); await copy;
  const detail = h.view.openInventory("canonical", "source"); const detailCall = h.calls.at(-1)!;
  const more = h.view.loadCopies("canonical", true); const copyQuery = h.calls.at(-1)!.query;
  h.calls.at(-1)!.reject({ code: "stale_cursor" }); await Promise.resolve(); await Promise.resolve();
  assert.equal(h.view.inventory.size, 0);
  assert.deepEqual(h.calls.at(-1)!.query, { ...copyQuery, cursor: null });
  h.calls.at(-1)!.reject({ code: "stale_cursor" }); await more;
  assert.equal(h.view.copies.get("canonical")?.status, "error");
  detailCall.resolve({ ...copies(), items: [] }); await detail;
  assert.equal(h.view.inventory.size, 0);
});
