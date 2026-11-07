import assert from "node:assert/strict";
import test from "node:test";

const senders = [{ email: "bob@acme.com", name: "Bob" }, { email: "other@acme.com", name: null }];

test("sender navigation selects exact addresses and All senders", async () => {
  const { inboxSenderNav } = await import("../src/inboxSenderNav.ts").catch(() => ({}));
  assert.equal(typeof inboxSenderNav, "function", "inboxSenderNav export is required");
  const all = inboxSenderNav(senders, null);
  assert.equal(all.sender, null);
  assert.equal(all.heading, "All senders");
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
  assert.equal(inboxSenderNav([], null, "Watchlist unavailable").heading, "All senders");
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

test("finder is literal, case insensitive and ordered while selection uses the complete watchlist", async () => {
  const { inboxSenderNav } = await import("../src/inboxSenderNav.ts");
  const watched = [
    { email: "jimbob@acme.com", name: "Same name" },
    { email: "bob@acme.com", name: "Same name" },
    { email: "special+[tag]@example.test", name: "Literal .*" },
  ];
  assert.deepEqual(inboxSenderNav(watched, null, null, "SAME NAME").items.map(i => i.sender),
    [null, "jimbob@acme.com", "bob@acme.com"]);
  assert.deepEqual(inboxSenderNav(watched, null, null, "[tag]").items.map(i => i.sender), [null, watched[2].email]);
  assert.deepEqual(inboxSenderNav(watched, null, null, ".*").items.map(i => i.sender), [null, watched[2].email]);
  const filtered = inboxSenderNav(watched, "bob@acme.com", null, "special+");
  assert.equal(filtered.sender, "bob@acme.com");
  assert.deepEqual(filtered.items.map(i => i.sender), [null, "bob@acme.com", watched[2].email]);
  assert.equal(filtered.items[1].pinned, true);
  assert.equal(filtered.items[1].selected, true);
  const matching = inboxSenderNav(watched, "bob@acme.com", null, "bob");
  assert.deepEqual(matching.items.map(i => i.sender), [null, "jimbob@acme.com", "bob@acme.com"]);
  assert.equal(matching.items.some(i => i.pinned), false);
});

test("empty, no match and failed refresh remain distinct, including an unavailable selected address", async () => {
  const { inboxSenderNav } = await import("../src/inboxSenderNav.ts");
  assert.equal(inboxSenderNav([], null).emptyText, "No watched senders yet.");
  const none = inboxSenderNav(senders, "bob@acme.com", null, "missing");
  assert.equal(none.emptyText, "No watched senders match your search.");
  assert.equal(none.items[1].sender, "bob@acme.com");
  const failed = inboxSenderNav([], "bob@acme.com", "Watchlist unavailable", "missing");
  assert.equal(failed.sender, "bob@acme.com");
  assert.equal(failed.emptyText, null);
  assert.equal(failed.items[1].sender, "bob@acme.com");
  assert.equal(failed.items[1].pinned, true);
  assert.equal(inboxSenderNav(senders.slice(1), "bob@acme.com", null, "bob").sender, null);
});

test("large finder lists preserve complete addresses, stable order and a single selected nonmatch", async () => {
  const { inboxSenderNav } = await import("../src/inboxSenderNav.ts");
  const watched = Array.from({ length: 225 }, (_, i) => ({ email: `sender-${i}@example.test`, name: `Sender ${i}` }));
  const long = "x".repeat(490) + "@example.test";
  watched.splice(90, 0, { email: long, name: "Long sender" });
  const before = JSON.stringify(watched);
  const model = inboxSenderNav(watched, long, null, "sender-2");
  assert.equal(model.items[1].sender, long);
  assert.equal(model.items.filter(i => i.sender === long).length, 1);
  assert.deepEqual(model.items.slice(2).map(i => i.sender), watched.filter(i => i.email.includes("sender-2")).map(i => i.email));
  assert.deepEqual(inboxSenderNav(watched, long, null, "").items.slice(1).map(i => i.sender), watched.map(i => i.email));
  assert.equal(JSON.stringify(watched), before);
});
