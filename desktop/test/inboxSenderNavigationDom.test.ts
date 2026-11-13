import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
import { createContext, runInContext } from "node:vm";
import test from "node:test";
import { inboxSenderNav } from "../src/inboxSenderNav.ts";
import { Element, nodes, text } from "./threadDomFixture.ts";

const source = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
const functions = ["renderInboxSenderNavigation", "selectInboxSender", "clearInboxPageForAccountChange"]
  .map(name => source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n\\}`))![0]).join("\n");
const listeners = source.slice(source.indexOf('inboxSenderSearch.addEventListener("input"'),
  source.indexOf('inboxFilterForm.addEventListener("submit"'));
const watched = [{ email: "bob@acme.com", name: "Same name" }, { email: "jimbob@acme.com", name: "Same name" },
  { email: "special+[tag]@example.test", name: "<img src=x onerror=bad()>" }];
function harness() {
  const doc: any = { activeElement: null, createElement: (tag: string) => new Element(tag, doc) };
  const el = (tag = "div") => new Element(tag, doc);
  const c = createContext({ document: doc, inboxSenderNav, watchedSenders: [...watched],
    inboxSenderNavigation: el("nav"), inboxSenderSearch: el("input"), inboxSenderAll: el("button"),
    inboxSenderPinned: el(), inboxSenderResults: el(), inboxSenderEmpty: el("p"), inboxSenderError: el("p"), inboxHeading: el("h2"),
    inboxSenderNavigationError: null, busy: false, inboxQueryEpoch: 8, inboxRequestGeneration: 10,
    activeInboxQuery: { sender: "bob@acme.com", provider: "imap", account_id: "retained", sender_query: "bob",
      keyword: "invoice", priority: "high", category: "invoice", status: "notified", limit: 50 },
    inboxItems: [{ message_id: "old" }], inboxNextCursor: "next", inboxPageState: "loaded", inboxCapabilityUnavailableCount: 0,
    expandedInboxMessages: new Set(["old"]), inboxLoadMore: el("button"), inboxStatus: el("p"),
    renders: [], calls: [], cleared: [], threadCloses: 0 });
  c.inboxSenderNavigation.append(c.inboxSenderSearch, c.inboxSenderAll, c.inboxSenderPinned, c.inboxSenderResults, c.inboxSenderEmpty);
  c.inboxMutationInFlight = () => c.busy;
  c.threadHistory = { clearInboxSelection: () => c.threadCloses++ };
  c.clearMessageOwnedUiState = () => c.cleared.push("all");
  c.renderInbox = items => c.renders.push(items.map(i => i.message_id));
  c.loadInbox = () => c.calls.push({ query: structuredClone(c.activeInboxQuery), ids: c.inboxItems.map(i => i.message_id),
    epoch: c.inboxQueryEpoch, cursor: c.inboxNextCursor, expanded: [...c.expandedInboxMessages] });
  runInContext(stripTypeScriptTypes(functions + "\n" + listeners), c);
  c.renderInboxSenderNavigation();
  return { c, doc,
    find(value: string) { c.inboxSenderSearch.focus(); c.inboxSenderSearch.value = value; c.inboxSenderSearch.listeners.get("input")(); },
    button(sender: string) { const b = nodes(c.inboxSenderNavigation).find(n => n.dataset.inboxSender === sender); assert.ok(b, sender); return b; },
  };
}

test("actual finder handlers do not commit queries or close threads; matching and pinned controls use text only", () => {
  const h = harness(), { c } = h;
  const before = JSON.stringify(c.activeInboxQuery);
  h.find("[tag]");
  assert.equal(c.inboxQueryEpoch, 8); assert.equal(c.inboxRequestGeneration, 10);
  assert.equal(c.inboxNextCursor, "next"); assert.equal(c.expandedInboxMessages.has("old"), true);
  assert.equal(JSON.stringify(c.activeInboxQuery), before); assert.equal(c.calls.length, 0); assert.equal(c.threadCloses, 0);
  assert.equal(h.doc.activeElement, c.inboxSenderSearch);
  assert.deepEqual(nodes(c.inboxSenderResults).filter(n => n.tagName === "BUTTON").map(n => n.dataset.inboxSender), [watched[2].email]);
  assert.equal(h.button("bob@acme.com").parent, c.inboxSenderPinned);
  assert.ok(text(c.inboxSenderResults).includes(watched[2].name));
  assert.equal(nodes(c.inboxSenderNavigation).filter(n => ["IMG", "SCRIPT", "A"].includes(n.tagName)).length, 0);
  let prevented = 0;
  c.inboxSenderSearch.listeners.get("keydown")({ key: "Enter", preventDefault() { prevented++; } });
  assert.equal(prevented, 1); assert.equal(c.calls.length, 0);
  h.find(""); assert.equal(c.inboxSenderPinned.hidden, true);
  assert.equal(nodes(c.inboxSenderResults).filter(n => n.tagName === "BUTTON").length, 3);
});

test("exact selection and All use the existing scope-clear owner and preserve every secondary predicate", () => {
  const h = harness(), { c } = h;
  const before = { ...c.activeInboxQuery };
  h.button("jimbob@acme.com").click();
  assert.deepEqual({ ...c.activeInboxQuery }, { ...before, sender: "jimbob@acme.com" });
  assert.equal(c.calls.length, 1); assert.equal(c.threadCloses, 1);
  assert.equal(c.calls[0].ids.length, 0); assert.equal(c.calls[0].cursor, null);
  assert.equal(c.calls[0].epoch, 9); assert.equal(h.doc.activeElement, h.button("jimbob@acme.com"));
  assert.equal(h.button("jimbob@acme.com").attributes.get("aria-pressed"), "true");
  c.inboxSenderAll.click();
  assert.deepEqual({ ...c.activeInboxQuery }, { ...before, sender: null });
  assert.equal(c.calls.length, 2); assert.equal(c.threadCloses, 2);
  assert.equal(c.inboxSenderAll.attributes.get("aria-pressed"), "true");
  assert.equal(h.doc.activeElement, c.inboxSenderAll);
});

test("no match retains selection; failed refresh keeps it; successful removal alone resets scope", () => {
  const h = harness(), { c } = h;
  h.find("missing"); assert.equal(c.inboxSenderEmpty.hidden, false);
  assert.match(c.inboxSenderEmpty.textContent, /No watched senders match/);
  assert.equal(c.calls.length, 0); assert.equal(c.activeInboxQuery.sender, "bob@acme.com");
  h.button("bob@acme.com").focus();
  c.watchedSenders = []; c.renderInboxSenderNavigation("Watchlist unavailable");
  assert.equal(c.inboxSenderEmpty.hidden, true); assert.equal(c.inboxSenderError.hidden, false);
  assert.equal(c.calls.length, 0); assert.equal(h.doc.activeElement, h.button("bob@acme.com"));
  c.renderInboxSenderNavigation();
  assert.equal(c.calls.length, 1); assert.equal(c.activeInboxQuery.sender, null);
  assert.equal(c.inboxSenderError.hidden, true); assert.equal(c.inboxSenderEmpty.textContent, "No watched senders yet.");
  assert.equal(h.doc.activeElement, c.inboxSenderAll);
});

test("navigation refresh preserves scroll and keyboard focus, including pin transitions and held mutations", () => {
  const h = harness(), { c } = h;
  c.inboxSenderResults.scrollTop = 130; h.button("bob@acme.com").focus();
  c.inboxSenderSearch.value = "special"; c.renderInboxSenderNavigation();
  assert.equal(h.doc.activeElement, h.button("bob@acme.com"));
  assert.equal(h.button("bob@acme.com").parent, c.inboxSenderPinned);
  assert.equal(c.inboxSenderResults.scrollTop, 130);
  c.busy = true; h.find("");
  assert.equal(c.inboxSenderAll.disabled, true);
  assert.equal(nodes(c.inboxSenderResults).filter(n => n.tagName === "BUTTON").every(n => n.disabled), true);
  assert.equal(c.calls.length, 0); assert.equal(c.threadCloses, 0);
});

test("the actual thread-entry options save the bounded results region along with outer scroll owners", () => {
  const expr = source.match(/scrollElements: \(\) => (\[[\s\S]*?\]),\n\}\);/)?.[1];
  assert.ok(expr, "thread entry scroll owner callback");
  const c = createContext({ inboxResults: {}, inboxSenderResults: {}, inboxSenderNavigation: {}, inboxView: {}, document: { scrollingElement: {} } });
  const saved = runInContext(expr, c);
  assert.ok(saved.includes(c.inboxSenderResults));
  assert.ok(saved.includes(c.inboxResults)); assert.ok(saved.includes(c.document.scrollingElement));
  assert.equal(new Set(saved).size, saved.length);
});
