import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { createContext, runInContext } from "node:vm";
import { paneHarness, deferred } from "./inboxThreadPaneFixture.ts";
import { nodes, text } from "./threadDomFixture.ts";
import { ThreadHistory } from "../src/threadHistory.ts";
import { inboxThreadOriginKey } from "../src/inboxThreadEntry.ts";

const main = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");

test("wide entry keeps messages available and same-origin activation focuses without reads", async () => {
  const h = paneHarness(); h.ui.updateLayout(1500);
  assert.equal(h.calls.length, 0); assert.equal(h.layout.placeholder.hidden, false);
  await h.ui.open(h.rows[0]); assert.equal(h.columns.hidden, false);
  assert.equal(h.layout.placeholder.hidden, true);
  assert.match(h.layout.source.textContent, /Origin a <img>.*full-a@example.test.*imap.*retained/);
  const count = h.calls.length; await h.ui.open(h.rows[0]); assert.equal(h.calls.length, count);
  h.listScroll.scrollTop = 700; h.ui.back(); assert.equal(h.listScroll.scrollTop, 700);
  assert.equal(h.doc.activeElement, h.triggers[0]); assert.equal(h.layout.placeholder.hidden, false);
});

test("same-thread revalidation restores only expanded bodies from fresh bounded pages and keeps reading position", async () => {
  const h = paneHarness(); h.ui.updateLayout(1500); await h.ui.open(h.rows[0]);
  await h.ui.state.loadMessages(true); await h.ui.state.openBody("a"); await h.ui.state.openBody("a-30");
  await h.ui.state.openCopies("a");
  const body = nodes(h.root).find(n => n.dataset.threadFocus === "inbox-thread:body-text:a-30")!;
  body.scrollTop = 80; body.focus(); h.layout.readerPane.scrollTop = 420;
  const before = h.calls.length;
  h.ui.invalidate(); h.ui.invalidate(); assert.equal(h.ui.state.bodies.size, 0);
  assert.doesNotMatch(text(h.root), /body0/); assert.equal(h.layout.source.textContent, "");
  h.revise(); h.ui.revalidateSource(); await h.ui.refresh();
  assert.equal(h.ui.state.messages.pages, 2); assert.equal(h.ui.state.messages.value?.items.length, 50);
  assert.deepEqual([...h.ui.state.bodies.keys()], ["a", "a-30"]); assert.equal(h.ui.state.copies.size, 0);
  assert.equal(h.ui.state.bodies.get("a-30")?.value?.body.state, "stored");
  assert.match(text(h.root), /body1/); assert.doesNotMatch(text(h.root), /body0/);
  assert.equal(h.layout.readerPane.scrollTop, 420);
  assert.equal(h.doc.activeElement.dataset.threadFocus, "inbox-thread:body-text:a-30");
  assert.equal(h.doc.activeElement.scrollTop, 80);
  assert.deepEqual(h.calls.slice(before).map(c => c.command), ["threads_resolve", "threads_messages", "threads_messages", "threads_body", "threads_body"]);
});

test("replay preserves the page bound for a growing partial last page and never hunts for a missing expanded body", async () => {
  for (const previousCount of [35, 50]) {
    const h = paneHarness(); let fresh = false;
    h.respond(async (command, query) => {
      const value = await h.defaultResponse(command, query);
      if (command === "threads_messages" && query.cursor) {
        if (!fresh) value.items = value.items.slice(0, previousCount - 25);
        else { value.items = value.items.filter((m: any) => m.message_id !== "a-30"); value.next_cursor = "new-page-3"; }
      }
      return value;
    });
    await h.ui.open(h.rows[0]); await h.ui.state.loadMessages(true); await h.ui.state.openBody("a-30");
    assert.equal(h.ui.state.messages.value?.items.length, previousCount);
    h.ui.invalidate(); fresh = true; const before = h.calls.length; await h.ui.refresh();
    assert.equal(h.ui.state.messages.value?.items.length, 49); assert.equal(h.ui.state.bodies.size, 0);
    assert.equal(h.calls.slice(before).filter(c => c.command === "threads_messages").length, 2);
    assert.equal(h.calls.slice(before).some(c => c.command === "threads_body"), false);
    assert.equal(h.ui.state.messages.value?.next_cursor, "new-page-3", "new page stays explicitly user-controlled");
  }
});

test("shorter history ends replay early and a fresh no-text body cannot resurrect retained bytes", async () => {
  const h = paneHarness(); await h.ui.open(h.rows[0]); await h.ui.state.loadMessages(true); await h.ui.state.openBody("a");
  h.ui.invalidate(); const before = h.calls.length;
  h.respond(async (command, query) => {
    const value = await h.defaultResponse(command, query);
    if (command === "threads_messages") { value.items = value.items.slice(0, 5); value.next_cursor = null; }
    if (command === "threads_body") value.body = { state: "removal_pending" };
    return value;
  });
  await h.ui.refresh(); assert.equal(h.ui.state.messages.value?.items.length, 5);
  assert.equal(h.calls.slice(before).filter(c => c.command === "threads_messages").length, 1);
  assert.match(text(h.root), /Body removal pending/); assert.doesNotMatch(text(h.root), /body0/);
});

test("failed replay, stale cursor, changed context and missing origin discard intent without partial restoration", async () => {
  for (const mode of ["stale", "failure", "context", "message-context", "absent"]) {
    const h = paneHarness(); await h.ui.open(h.rows[0]); await h.ui.state.loadMessages(true); await h.ui.state.openBody("a");
    h.ui.invalidate(); const before = h.calls.length;
    if (mode === "absent") h.snapshot.items = [h.rows[1]];
    let failed = false;
    h.respond(async (command, query) => {
      if (!failed && command === "threads_messages" && query.cursor && ["stale", "failure"].includes(mode)) {
        failed = true; throw { code: mode === "stale" ? "stale_cursor" : "failed" };
      }
      const value = await h.defaultResponse(command, query);
      if (command === "threads_resolve" && mode === "context") value.context.thread.thread_ref.thread_key = "changed";
      if (command === "threads_messages" && mode === "message-context") value.thread.relation = "owned";
      return value;
    });
    h.ui.revalidateSource(); await h.ui.refresh();
    assert.equal(h.ui.state.bodies.size, 0, mode); assert.equal(h.ui.state.messages.value, null, mode);
    assert.equal(h.calls.slice(before).some(c => c.command === "threads_body"), false, mode);
    if (mode === "stale") assert.equal(h.calls.slice(before).filter(c => c.command === "threads_messages").length, 2, "no stale restart during replay");
  }
});

test("A to B during replay cannot repaint or focus A, and focus moved outside is not stolen", async () => {
  for (const mode of ["select", "focus"]) {
    const h = paneHarness(), pending = deferred(); await h.ui.open(h.rows[0]);
    await h.ui.state.loadMessages(true); await h.ui.state.openBody("a");
    nodes(h.root).find(n => n.dataset.threadFocus === "inbox-thread:body-text:a")!.focus();
    h.ui.invalidate();
    h.respond(async (command, query) => command === "threads_messages" && query.cursor ? pending.promise : h.defaultResponse(command, query));
    const replay = h.ui.refresh(); await new Promise(r => setImmediate(r));
    const outside = h.el("input");
    if (mode === "select") await h.ui.open(h.rows[1]); else outside.focus();
    pending.resolve(await h.defaultResponse("threads_messages", { inbox_thread: { message_ref: h.rows[0].thread_entry_ref }, cursor: "fresh-page-2" }));
    await replay;
    if (mode === "select") { assert.equal(h.ui.state.inboxOrigin?.message_id, "b"); assert.equal(h.ui.state.bodies.size, 0); }
    else { assert.equal(h.doc.activeElement, outside); assert.equal(h.ui.state.bodies.size, 1); }
  }
});

test("failed Inbox refresh, query close, Back and mutation holds discard pending body restoration", async () => {
  for (const mode of ["failure", "query", "back", "mutation"]) {
    const h = paneHarness(); await h.ui.open(h.rows[0]); await h.ui.state.openBody("a"); h.ui.invalidate();
    if (mode === "failure") h.ui.failRefresh(h.ui.refreshGeneration);
    if (mode === "query") { h.snapshot.queryEpoch++; h.ui.close(); }
    if (mode === "back") h.ui.back();
    if (mode === "mutation") { h.ui.beginMutation(); h.ui.beginMutation(); await h.ui.endMutation(false); await h.ui.endMutation(false); }
    const before = h.calls.length; await h.ui.refresh();
    assert.equal(h.ui.state.bodies.size, 0, mode);
    assert.equal(h.calls.slice(before).some(c => c.command === "threads_body"), false, mode);
  }
});

test("a hidden document retains only intent and cannot read or restore until visible revalidation", async () => {
  const h = paneHarness(); await h.ui.open(h.rows[0]); await h.ui.state.openBody("a");
  h.doc.visibilityState = "hidden"; h.ui.invalidate(); const before = h.calls.length;
  h.ui.revalidateSource(); await h.ui.refresh();
  assert.equal(h.calls.length, before); assert.doesNotMatch(text(h.root), /body0/);
  h.doc.visibilityState = "visible"; h.revise(); h.ui.revalidateSource(); await h.ui.refresh();
  assert.match(text(h.root), /body1/); assert.equal(h.ui.state.bodies.size, 1);
});

test("the actual Inbox span owner cannot restore an earlier refresh after a newer invalidation", async () => {
  const h = paneHarness(), pending = deferred(); await h.ui.open(h.rows[0]); await h.ui.state.openBody("a");
  let restored = Promise.resolve(); const commits: boolean[] = [];
  const history = new ThreadHistory({} as any, h.ui);
  const c = createContext({ inboxThreadEntry: h.ui, inboxQueryEpoch: h.snapshot.queryEpoch,
    inboxItems: h.rows, inboxNextCursor: null, currentMailboxEffectScope: () => h.snapshot.scope,
    mailboxEffectScopeIsCurrent: () => true, refreshInboxThreadControls() {},
    loadInboxPage: () => pending.promise,
    threadHistory: { commitInboxSpan: (restore = true) => { commits.push(restore); restored = history.commitInboxSpan(restore); } } });
  Object.defineProperty(c, "inboxThreadSpanScope", { get: () => h.snapshot.spanScope, set: value => { h.snapshot.spanScope = value; } });
  runInContext(stripTypeScriptTypes(main.match(/async function runInboxRefresh\([\s\S]*?\n\}/)![0]), c);
  h.ui.invalidate(); h.snapshot.spanScope = null; const before = h.calls.length;
  const older = c.runInboxRefresh({ kind: "first" }, null, () => true);
  h.ui.invalidate(); pending.resolve(true); await older;
  assert.deepEqual(commits, [false]); assert.equal(h.calls.length, before); assert.equal(h.ui.state.bodies.size, 0);
  c.loadInboxPage = async () => true;
  await c.runInboxRefresh({ kind: "first" }, null, () => true); await restored;
  assert.deepEqual(commits, [false, true]); assert.equal(h.ui.state.bodies.size, 1);
  assert.equal(h.calls.slice(before).filter(c => c.command === "threads_body").length, 1);
});

test("Back during a current span reload enables Open thread without restoring the cancelled reader", async () => {
  for (const back of [false, true]) {
    const h = paneHarness(), page = deferred(); await h.ui.open(h.rows[0]); await h.ui.state.openBody("a");
    const history = new ThreadHistory({} as any, h.ui); let restored = Promise.resolve();
    const c = createContext({ inboxThreadEntry: h.ui, inboxQueryEpoch: h.snapshot.queryEpoch, inboxItemsEpoch: h.snapshot.itemsEpoch,
      inboxItems: h.rows, inboxList: h.columns, inboxThreadOriginKey, inboxNextCursor: null,
      currentMailboxEffectScope: () => h.snapshot.scope, mailboxEffectScopeIsCurrent: () => true,
      captureInboxScroll: () => ({ top: 0, revision: 0 }), restoreInboxScroll() {}, loadInboxPage: () => page.promise,
      renderSelectedInboxMessage() {},
      threadHistory: { commitInboxSpan: (restore = true) => { restored = history.commitInboxSpan(restore); return restored; } } });
    Object.defineProperty(c, "inboxThreadSpanScope", { get: () => h.snapshot.spanScope, set: value => { h.snapshot.spanScope = value; } });
    runInContext(stripTypeScriptTypes(["runInboxRefresh", "refreshInboxThreadControls"].map(name =>
      main.match(new RegExp(`(?:async )?function ${name}\\([\\s\\S]*?\\n\\}`))![0]).join("\n")), c);
    h.ui.invalidate(); h.snapshot.spanScope = null; c.refreshInboxThreadControls();
    assert.ok(h.triggers.every(button => button.disabled));
    const before = h.calls.length, refreshing = c.runInboxRefresh({ kind: "span", target: 2 }, null, () => true);
    if (back) h.ui.back();
    page.resolve(true); await refreshing; await restored;
    assert.deepEqual(h.snapshot.spanScope, h.snapshot.scope);
    assert.ok(h.triggers.every(button => !button.disabled));
    assert.equal(h.ui.selected, !back); assert.equal(h.ui.state.bodies.size, back ? 0 : 1);
    if (back) { assert.equal(h.calls.length, before); assert.equal(h.root.hidden, true); }
    await h.ui.open(h.rows[1]); assert.equal(h.ui.state.inboxOrigin?.message_id, "b");
  }
});

test("actual focus and visibility handlers route selected readers through the loaded-span owner", async () => {
  const callbacks = new Map<string, Function>(); const events: string[] = [];
  const account = deferred();
  const c = createContext({ window: { addEventListener: (name: string, fn: Function) => callbacks.set(name, fn) },
    document: { visibilityState: "visible", addEventListener: (name: string, fn: Function) => callbacks.set(name, fn) },
    configurationReady: true, inboxThreadSpanScope: {}, settingsView: { hidden: true },
    threadHistory: { prepareInboxRefresh: (vendor = true) => events.push(`invalidate:${vendor}`) },
    inboxThreadEntry: { selected: true, refreshGeneration: 1, failRefresh: () => events.push("failed") },
    refreshInboxThreadControls() {}, loadMailAccounts: () => account.promise,
    refreshLoadedInboxSpan: async () => { events.push("span"); }, loadInbox: async () => { events.push("first"); },
    refreshThreadHistory: () => events.push("visible-refresh"), refreshConnectStatus() {} });
  const handlers = main.slice(main.indexOf('window.addEventListener("focus"'), main.indexOf('window.setInterval(() => {'));
  runInContext(stripTypeScriptTypes(handlers), c);
  callbacks.get("focus")!(); assert.deepEqual(events, ["invalidate:true"]);
  account.resolve(true); await new Promise(r => setImmediate(r)); assert.deepEqual(events, ["invalidate:true", "span"]);
  c.document.visibilityState = "hidden"; callbacks.get("visibilitychange")!(); assert.equal(events.at(-1), "invalidate:false");
  c.document.visibilityState = "visible"; callbacks.get("visibilitychange")!(); assert.equal(events.at(-1), "visible-refresh");
  c.inboxThreadEntry.selected = false; callbacks.get("focus")!(); await new Promise(r => setImmediate(r));
  assert.equal(events.at(-1), "first");
});

test("content-width transitions preserve the reader and latest hidden-list scroll without fetching", async () => {
  const h = paneHarness(); h.ui.updateLayout(1500); await h.ui.open(h.rows[0]);
  await h.ui.state.openBody("a"); const count = h.calls.length;
  h.listScroll.scrollTop = 525; h.triggers[0].focus(); h.ui.updateLayout(600);
  assert.equal(h.columns.hidden, true); assert.equal(h.layout.readerPane.hidden, false);
  assert.equal(h.layout.senders.hidden, true); assert.notEqual(h.doc.activeElement, h.triggers[0]);
  assert.equal(h.ui.state.bodies.size, 1); assert.equal(h.calls.length, count);
  h.listScroll.scrollTop = 0; h.ui.updateLayout(1000);
  assert.equal(h.columns.hidden, false); assert.equal(h.listScroll.scrollTop, 525);
  h.ui.updateLayout(600); h.listScroll.scrollTop = 0; h.ui.back();
  assert.equal(h.listScroll.scrollTop, 525); assert.equal(h.layout.readerPane.hidden, true);
  assert.equal(h.columns.hidden, false); assert.equal(h.calls.length, count);
});
