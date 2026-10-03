import assert from "node:assert/strict";
import test from "node:test";

const senders = [{ email: "bob@acme.com", name: "Bob" }, { email: "other@acme.com", name: null }];

test("sender navigation selects exact addresses and All messages", async () => {
  const { inboxSenderNav } = await import("../src/inboxSenderNav.ts").catch(() => ({}));
  assert.equal(typeof inboxSenderNav, "function", "inboxSenderNav export is required");
  const all = inboxSenderNav(senders, null);
  assert.equal(all.sender, null);
  assert.equal(all.heading, "All messages");
  assert.deepEqual(all.items.map((item) => item.sender), [null, "bob@acme.com", "other@acme.com"]);
  const selected = inboxSenderNav(senders, "bob@acme.com");
  assert.equal(selected.sender, "bob@acme.com");
  assert.equal(selected.heading, "Bob (bob@acme.com)");
  assert.deepEqual(selected.items.map((item) => item.selected), [false, true, false]);
  assert.equal(selected.items[2].label, "other@acme.com");
});

test("removal falls back to All but a failed watchlist preserves selection", async () => {
  const { inboxSenderNav } = await import("../src/inboxSenderNav.ts").catch(() => ({}));
  assert.equal(typeof inboxSenderNav, "function", "inboxSenderNav export is required");
  assert.equal(inboxSenderNav(senders.slice(1), "bob@acme.com").sender, null);
  const failed = inboxSenderNav(senders, "bob@acme.com", "Watchlist unavailable");
  assert.equal(failed.sender, "bob@acme.com");
  assert.equal(failed.error, "Watchlist unavailable");
  assert.equal(failed.items[0].sender, null);
  assert.equal(inboxSenderNav([], null, "Watchlist unavailable").heading, "All messages");
});

test("sender empty text names secondary filters before account scope", async () => {
  const { inboxSenderEmptyText } = await import("../src/inboxSenderNav.ts").catch(() => ({}));
  assert.equal(typeof inboxSenderEmptyText, "function", "inboxSenderEmptyText export is required");
  for (const key of ["keyword", "sender_query", "priority", "category", "status"]) {
    assert.equal(inboxSenderEmptyText({ [key]: "value" }, "active"), "No retained messages from this sender match these filters.");
    assert.equal(inboxSenderEmptyText({ [key]: "value" }, "all"), "No retained messages from this sender match these filters.");
  }
});

test("sender empty text distinguishes active, explicit, and all accounts", async () => {
  const { inboxSenderEmptyText } = await import("../src/inboxSenderNav.ts").catch(() => ({}));
  assert.equal(typeof inboxSenderEmptyText, "function", "inboxSenderEmptyText export is required");
  for (const selection of ["active", '["gmail","account"]']) {
    assert.equal(inboxSenderEmptyText({}, selection), "No retained messages from this sender in this account.");
  }
  assert.equal(inboxSenderEmptyText({ keyword: null }, "all"), "No retained messages from this sender.");
});
