import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { createContext, runInContext } from "node:vm";
import test from "node:test";
import { Element, nodes, text } from "./threadDomFixture.ts";
const source = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
const names = ["renderInboxFilterScope", "removeInboxFilter", "commitInboxQueryFromControls", "queryFromInboxControls",
  "optionalFilterValue", "clearInboxPageForAccountChange", "inboxAccountForSelection"];
function harness() {
  const code = names.map(name => { const m = source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n\\}`)); assert.ok(m, `required owner ${name}`); return m[0]; }).join("\n");
  const doc: any = { activeElement: null, createElement: (tag: string) => new Element(tag, doc) };
  const el = () => new Element("div", doc), control = (value: string) => Object.assign(new Element("input", doc), { value, options: [] });
  const c = createContext({ document: doc, inboxFilterScope: el(), inboxFilterPanel: { open: false }, inboxFiltersToggle: el(),
    inboxPreviousResults: el(), inboxKeywordInput: control("draft keyword"), inboxSenderInput: control("draft sender"),
    inboxPrioritySelect: control("low"), inboxCategorySelect: control("other"), inboxStateSelect: control("pending"),
    inboxAccountSelect: control("all"), inboxPageSizeSelect: control("100"), inboxMutationInFlight: () => false,
    mailAccounts: [{ provider: "imap", account_id: "one", active: true, address: "one@example.test", display_name: "One" },
      { provider: "gmail", account_id: "two", active: false, address: "two@example.test", display_name: "Two" }],
    unavailableAccountFilter: { provider: "__no_active_account__", account_id: "__no_active_account__" },
    activeInboxAccountSelection: "active", activeInboxQuery: { limit: 50, provider: "imap", account_id: "one", sender: "exact@example.test",
      keyword: "committed <img>", sender_query: "contains", priority: "high", category: "invoice", status: "summarized" },
    inboxItems: [{ message_id: "retained" }], inboxQueryEpoch: 3, inboxItemsEpoch: 3, inboxRequestGeneration: 4,
    inboxNextCursor: "next", inboxPageState: "loaded", inboxCapabilityUnavailableCount: 0,
    inboxLoadMore: el(), inboxStatus: el(), calls: [], closes: 0, clearMessageOwnedUiState: () => {} });
  c.inboxPredicateControls = { keyword: c.inboxKeywordInput, sender_query: c.inboxSenderInput, priority: c.inboxPrioritySelect,
    category: c.inboxCategorySelect, status: c.inboxStateSelect };
  c.inboxPrioritySelect.options = [{ value: "high", textContent: "High" }, { value: "low", textContent: "Low" }];
  c.inboxCategorySelect.options = [{ value: "invoice", textContent: "Invoice" }, { value: "other", textContent: "Other" }];
  c.inboxStateSelect.options = [{ value: "summarized", textContent: "Notified" }, { value: "pending", textContent: "Pending" }];
  c.inboxFilterScope.querySelectorAll = () => nodes(c.inboxFilterScope).filter(n => n.tagName === "BUTTON");
  c.threadHistory = { clearInboxSelection: () => c.closes++ };
  c.loadInbox = () => c.calls.push({ query: structuredClone(c.activeInboxQuery), ids: c.inboxItems.map(i => i.message_id), epoch: c.inboxQueryEpoch });
  c.renderInbox = () => c.renderInboxFilterScope();
  runInContext(stripTypeScriptTypes(code), c); c.renderInboxFilterScope();
  return { c, doc, button: (key: string) => { const b = nodes(c.inboxFilterScope).find(n => n.dataset.inboxFilterFocus === key); assert.ok(b, key); return b; } };
}

test("visible scope describes committed predicates, account and page size, not conflicting draft controls", () => {
  const { c, button } = harness(); const content = text(c.inboxFilterScope);
  for (const value of ["committed <img>", "contains", "High", "Invoice", "Notified", "Active", "one@example.test", "50 per page"]) assert.ok(content.includes(value), value);
  assert.doesNotMatch(content, /draft keyword|draft sender|100 per page/);
  assert.equal(nodes(c.inboxFilterScope).some(n => n.tagName === "IMG"), false);
  assert.equal(nodes(c.inboxFilterScope).some(n => n.dataset.inboxFilterFocus === "remove-account"), false);
  button("account").click(); assert.equal(c.inboxFilterPanel.open, true); assert.equal(c.document.activeElement, c.inboxAccountSelect);
  assert.equal(c.calls.length, 0);
});

test("each predicate removal commits only that criterion and draft while retaining account, sender and other drafts", () => {
  for (const key of ["keyword", "sender_query", "priority", "category", "status"]) {
    const { c, button } = harness(); const before = { ...c.activeInboxQuery }, drafts = Object.fromEntries(Object.entries(c.inboxPredicateControls).map(([k, el]: any) => [k, el.value]));
    button(key).click();
    assert.deepEqual({ ...c.activeInboxQuery }, { ...before, [key]: null });
    for (const [k, el] of Object.entries(c.inboxPredicateControls) as any) assert.equal(el.value, k === key ? "" : drafts[k], k);
    assert.equal(c.inboxAccountSelect.value, "all"); assert.equal(c.inboxPageSizeSelect.value, "100");
    assert.equal(c.activeInboxAccountSelection, "active"); assert.equal(c.calls.length, 1); assert.equal(c.closes, 1);
    assert.equal(c.inboxPreviousResults.hidden, false); assert.equal(c.inboxItems.length, 1);
    assert.ok(c.document.activeElement === c.inboxFiltersToggle || c.inboxFilterScope.contains(c.document.activeElement));
  }
});

test("removing explicit or All-account scope returns to resolved Active and preserves unrelated committed/draft values", () => {
  for (const selection of ['["gmail","two"]', "all"]) {
    const { c, button } = harness(); c.activeInboxAccountSelection = selection;
    Object.assign(c.activeInboxQuery, selection === "all" ? { provider: null, account_id: null } : { provider: "gmail", account_id: "two" });
    c.renderInboxFilterScope(); const before = { ...c.activeInboxQuery };
    button("remove-account").click();
    assert.deepEqual({ ...c.activeInboxQuery }, { ...before, provider: "imap", account_id: "one" });
    assert.equal(c.activeInboxAccountSelection, "active"); assert.equal(c.inboxAccountSelect.value, "active");
    assert.equal(c.inboxKeywordInput.value, "draft keyword"); assert.equal(c.inboxPrioritySelect.value, "low");
    assert.equal(c.inboxItems.length, 0); assert.equal(c.inboxPreviousResults.hidden, true);
    assert.equal(c.calls[0].ids.length, 0); assert.equal(c.closes, 1);
  }
});

test("missing Active remains unavailable and explicit retained scope is named from the committed query", () => {
  const { c, button } = harness(); c.mailAccounts = [];
  c.activeInboxAccountSelection = "all"; c.activeInboxQuery.provider = null; c.activeInboxQuery.account_id = null;
  c.renderInboxFilterScope(); button("remove-account").click();
  assert.equal(c.activeInboxQuery.provider, "__no_active_account__"); assert.equal(c.activeInboxQuery.account_id, "__no_active_account__");
  assert.match(text(c.inboxFilterScope), /No active account/);
  c.activeInboxAccountSelection = '["imap","retained"]'; c.activeInboxQuery.provider = "imap"; c.activeInboxQuery.account_id = "retained";
  c.renderInboxFilterScope(); assert.match(text(c.inboxFilterScope), /imap.*retained/);
});

test("query submit commits all drafts through the existing owner; Previous results ends only with a current page", () => {
  const { c } = harness(); c.commitInboxQueryFromControls();
  assert.deepEqual({ ...c.activeInboxQuery }, { provider: null, account_id: null, limit: 100, sender: "exact@example.test",
    keyword: "draft keyword", sender_query: "draft sender", priority: "low", category: "other", status: "pending" });
  assert.equal(c.inboxItems.length, 0); assert.equal(c.inboxPreviousResults.hidden, true);
  c.inboxItems = [{ message_id: "old" }]; c.inboxItemsEpoch = c.inboxQueryEpoch - 1; c.renderInboxFilterScope();
  assert.equal(c.inboxPreviousResults.hidden, false);
  c.inboxPageState = "failed"; c.renderInboxFilterScope(); assert.equal(c.inboxPreviousResults.hidden, false);
  c.inboxItemsEpoch = c.inboxQueryEpoch; c.renderInboxFilterScope(); assert.equal(c.inboxPreviousResults.hidden, true);
});

test("busy label removal cannot bypass disabled filter controls", () => {
  const { c } = harness(), before = JSON.stringify(c.activeInboxQuery);
  c.inboxKeywordInput.disabled = true; c.removeInboxFilter("keyword");
  assert.equal(JSON.stringify(c.activeInboxQuery), before); assert.equal(c.calls.length, 0);
  assert.equal(c.inboxKeywordInput.value, "draft keyword");
});
