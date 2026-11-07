import assert from "node:assert/strict";
import test from "node:test";
import { paneHarness, deferred } from "./inboxThreadPaneFixture.ts";
import { nodes, text } from "./threadDomFixture.ts";

const tick = () => new Promise(resolve => setImmediate(resolve));

test("refresh invalidation with no selected reader leaves current rows usable and makes no thread reads", async () => {
  const h = paneHarness(); h.ui.invalidate(); h.ui.invalidate(); await h.ui.refresh();
  assert.equal(h.ui.selected, false); assert.equal(h.calls.length, 0); assert.equal(h.root.hidden, true);
  assert.equal(h.ui.canOpen(h.rows[0]), true);
  await h.ui.open(h.rows[0]); assert.equal(h.ui.state.inboxOrigin?.message_id, "a");
});

test("overlapping same-origin refreshes restore only the latest and read each expanded body once", async () => {
  for (const olderFirst of [false, true]) {
    const h = paneHarness(), old = deferred(), fresh = deferred();
    await h.ui.open(h.rows[0]); await h.ui.state.openBody("a"); h.ui.invalidate();
    let resolves = 0;
    h.respond((command, query) => command === "threads_resolve"
      ? (++resolves === 1 ? old.promise : fresh.promise) : h.defaultResponse(command, query));
    const before = h.calls.length, older = h.ui.refresh(), latest = h.ui.refresh();
    const response = await h.defaultResponse("threads_resolve", { message_ref: h.rows[0].thread_entry_ref });
    if (olderFirst) { old.resolve(response); await older; assert.equal(h.ui.state.bodies.size, 0); }
    fresh.resolve(response); await latest;
    h.layout.readerPane.scrollTop = 73;
    if (!olderFirst) { old.resolve(response); await older; }
    assert.equal(h.calls.slice(before).filter(c => c.command === "threads_body").length, 1);
    assert.equal(h.ui.state.bodies.size, 1); assert.equal(h.layout.readerPane.scrollTop, 73);
  }
});

test("A completion cannot transfer scroll or focus to B during body reads, including an empty replay batch", async t => {
  for (const expanded of [true, false]) {
    const h = paneHarness(), body = deferred(); await h.ui.open(h.rows[0]);
    if (expanded) await h.ui.state.openBody("a");
    h.ui.reader.focusHeading(); h.layout.readerPane.scrollTop = 420; h.ui.invalidate();
    const restore = t.mock.method(h.ui.reader, "restoreReadingPosition");
    let switched = Promise.resolve();
    if (expanded) {
      h.respond((command, query) => command === "threads_body" ? body.promise : h.defaultResponse(command, query));
    } else {
      // Schedule selection at the empty batch's await boundary; keep all read/entry owners real.
      const all = Promise.all;
      let armed = true;
      t.mock.method(Promise, "all", function (values: any[]) {
        if (armed && values.length === 0) { armed = false; switched = h.ui.open(h.rows[1]); }
        return all.call(Promise, values);
      });
    }
    const refreshing = h.ui.refresh();
    if (expanded) {
      await tick(); assert.equal(h.ui.state.bodies.get("a")?.status, "loading");
      switched = h.ui.open(h.rows[1]); await switched;
      body.resolve(await h.defaultResponse("threads_body", { message_id: "a" }));
    }
    await refreshing; await switched;
    assert.equal(restore.mock.callCount(), 0, "A must not restore any position after B is selected");
    assert.equal(h.ui.state.inboxOrigin?.message_id, "b"); assert.equal(h.ui.state.bodies.size, 0);
    assert.equal(h.layout.readerPane.scrollTop, 0);
    assert.equal(h.doc.activeElement.dataset.threadFocus, "inbox-thread:heading");
    assert.doesNotMatch(text(h.root), /body0/);
    t.mock.restoreAll();
  }
});

test("a failed body re-read discards the whole restoration instead of restoring partial success", async () => {
  const h = paneHarness(); await h.ui.open(h.rows[0]);
  await h.ui.state.openBody("a"); await h.ui.state.openBody("a-1");
  nodes(h.root).find(n => n.dataset.threadFocus === "inbox-thread:body-text:a")!.focus();
  h.layout.readerPane.scrollTop = 420; h.ui.invalidate();
  h.respond(async (command, query) => {
    if (command === "threads_body" && query.message_id === "a-1") throw new Error("Body unavailable");
    return h.defaultResponse(command, query);
  });
  await h.ui.refresh();
  assert.equal(h.ui.state.resolution.status, "error"); assert.equal(h.ui.state.messages.value, null);
  assert.equal(h.ui.state.bodies.size, 0); assert.doesNotMatch(text(h.root), /body0/);
  assert.equal(h.layout.source.textContent, "");
  const before = h.calls.length; await h.ui.refresh();
  assert.equal(h.calls.slice(before).some(c => c.command === "threads_body"), false, "failed intent cannot replay later");
});

test("fresh replay stops at the previously loaded count even when a larger page budget remains", async () => {
  const h = paneHarness(); let fresh = false;
  h.respond(async (command, query) => {
    const value = await h.defaultResponse(command, query);
    if (command === "threads_messages" && !fresh) {
      value.items = value.items.slice(0, 2);
      if (query.cursor === "third") value.items = value.items.map((item: any, index: number) => ({ ...item, message_id: `a-${50 + index}` }));
      value.next_cursor = query.cursor === "third" ? null : query.cursor ? "third" : "second";
    }
    return value;
  });
  await h.ui.open(h.rows[0]); await h.ui.state.loadMessages(true); await h.ui.state.loadMessages(true);
  assert.equal(h.ui.state.messages.pages, 3); assert.equal(h.ui.state.messages.value?.items.length, 6);
  h.ui.invalidate(); fresh = true; const before = h.calls.length; await h.ui.refresh();
  assert.equal(h.calls.slice(before).filter(c => c.command === "threads_messages").length, 1);
  assert.equal(h.ui.state.messages.value?.items.length, 25);
  assert.equal(h.ui.state.messages.value?.next_cursor, "fresh-page-2");
});

test("invalidation during a replay page cannot fetch further pages or damage B's fresh messages", async () => {
  const h = paneHarness(), page = deferred(); await h.ui.open(h.rows[0]); await h.ui.state.loadMessages(true);
  h.ui.invalidate();
  h.respond((command, query) => command === "threads_messages" && query.cursor
    && query.inbox_thread.message_ref.message_id === "a" ? page.promise : h.defaultResponse(command, query));
  const replay = h.ui.refresh(); await tick();
  await h.ui.open(h.rows[1]);
  const before = h.calls.length;
  page.resolve(await h.defaultResponse("threads_messages", { inbox_thread: { message_ref: h.rows[0].thread_entry_ref }, cursor: "fresh-page-2" }));
  await replay;
  assert.equal(h.calls.length, before, "cancelled replay cannot page through the newer selection");
  assert.equal(h.ui.state.inboxOrigin?.message_id, "b"); assert.equal(h.ui.state.messages.status, "ready");
  assert.equal(h.ui.state.messages.value?.items.length, 25); assert.equal(h.ui.state.messages.pages, 1);
});
