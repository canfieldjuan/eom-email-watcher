import assert from "node:assert/strict";
import test from "node:test";
import { ThreadReadClient } from "../src/threadReads.ts";
import { ThreadViewDom } from "../src/threadViewDom.ts";

class Element {
  tagName: string;
  children: Element[] = [];
  parent: Element | null = null;
  ownerDocument: any;
  dataset: Record<string, string> = {};
  attributes = new Map<string, string>();
  listeners = new Map<string, () => void>();
  textContent = ""; className = ""; id = ""; value = ""; type = "";
  hidden = false; disabled = false; tabIndex = 0; scrollTop = 0;
  constructor(tag: string, doc: any) { this.tagName = tag.toUpperCase(); this.ownerDocument = doc; }
  set innerHTML(_: string) { throw new Error("HTML sinks forbidden"); }
  append(...children: Element[]) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children: Element[]) { for (const child of this.children) child.parent = null; this.children = []; this.append(...children); }
  setAttribute(key: string, value: string) { this.attributes.set(key, value); }
  addEventListener(name: string, fn: () => void) { this.listeners.set(name, fn); }
  contains(other: Element) { return nodes(this).includes(other); }
  querySelectorAll(selector: string) {
    assert.equal(selector, "[data-thread-focus]"); return nodes(this).filter(n => n.dataset.threadFocus !== undefined);
  }
  focus() { this.ownerDocument.activeElement = this; }
  click() { assert.equal(this.disabled, false); this.focus(); this.listeners.get("click")?.(); }
}
function nodes(root: Element): Element[] { return [root, ...root.children.flatMap(nodes)]; }
function text(root: Element): string { return nodes(root).map(n => n.textContent).join("\n"); }
const hostile = '<img src="https://example.test/beacon" onerror="alert(1)"><script>evil()</script>';
const ref = { provider: "imap", account_id: "retained", mailbox_identity_key: null, thread_key: "thread" };
const vendor = { vendor_id: "vendor", display_name: `Vendor ${hostile}` };
const thread = { thread_ref: ref, relation: "linked", owner_vendor: { vendor_id: "other", display_name: `Owner ${hostile}` },
  subject: `Subject ${hostile}`, latest_received_at: null, message_count: 1 };
const message = { message_id: "canonical", subject: thread.subject, direction: "outbound", sender: `sender+${"x".repeat(180)}@example.test`,
  sender_name: hostile, attributed_vendor: null, received_at: null,
  body_state: { state: "stored", stored_chars: 4, source_chars: 8 }, copy_count: 1 };
const tick = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
function harness() {
  const doc: any = { activeElement: null, createElement: (tag: string) => new Element(tag, doc) };
  const root = new Element("section", doc);
  let failure = false, exited = false;
  const calls: any[] = [];
  const literal = `  ${hostile}\n> quoted\nhttps://example.test/no-link\n`;
  const client = new ThreadReadClient(async <T>(command: string, args: Record<string, any>) => {
    calls.push({ command, args }); if (failure) throw { code: "runtime_error" };
    const data = command === "threads_list" ? { items: [thread], next_cursor: null }
      : command === "threads_messages" ? { thread: { ...thread, history_notice: "History partial — only locally retained messages are shown" }, items: [message], next_cursor: null }
      : command === "threads_body" ? { thread_ref: ref, message_id: "canonical", body: { state: "stored", text: literal, stored_chars: [...literal].length, source_chars: [...literal].length + 5 } }
      : args.query.copy_ref ? { thread_ref: ref, message_id: "canonical", items: [{ part_id: "1", filename: hostile, media_type: "text/plain", byte_size: null }], next_cursor: null }
      : { thread_ref: ref, message_id: "canonical", items: [{ copy_ref: "source", locations: [hostile], summary: `Summary ${hostile}`, attachment_count: 1 }], next_cursor: null };
    return data as T;
  });
  const ui = new ThreadViewDom(root as unknown as HTMLElement, client, {
    accounts: () => [{ provider: "imap", account_id: "retained", display_name: "Retained account", connected: false, sent_scope: "unavailable" }],
    onExit: () => { exited = true; },
  });
  const button = (label: string) => {
    const found = nodes(root).find(n => n.tagName === "BUTTON" && n.textContent === label);
    assert.ok(found, `missing ${label}`); return found;
  };
  return { ui, root, doc, calls, literal, button, fail: () => { failure = true; }, exited: () => exited };
}

test("retained vendor/account/linked scope and hostile headers render as text only", async () => {
  const h = harness(); await h.ui.open(vendor);
  assert.equal(h.root.hidden, false);
  assert.match(text(h.root), /History partial — only locally retained messages are shown/);
  assert.match(text(h.root), /Linked/); assert.ok(text(h.root).includes(thread.owner_vendor.display_name));
  assert.match(text(h.root), /Mailbox identity unavailable/);
  assert.ok(nodes(h.root).some(n => n.textContent === "Date unavailable · 1 logical message"));
  const open = h.button(thread.subject);
  assert.equal(open.attributes.get("aria-expanded"), "false");
  for (const control of nodes(h.root).filter(n => n.attributes.has("aria-controls"))) {
    assert.ok(nodes(h.root).some(n => n.id === control.attributes.get("aria-controls")));
  }
  open.click(); await tick();
  assert.ok(text(h.root).includes(message.sender));
  assert.ok(text(h.root).includes(hostile));
  assert.match(text(h.root), /Outbound/); assert.match(text(h.root), /No attributed vendor/);
  assert.ok(nodes(h.root).some(n => n.textContent === "Outbound · Date unavailable"));
  assert.match(text(h.root), /Sent mail unavailable/);
  assert.equal(nodes(h.root).filter(n => ["SCRIPT", "IMG", "IFRAME", "A", "FORM"].includes(n.tagName)).length, 0);
  assert.ok(h.calls.every(c => ["threads_list", "threads_messages"].includes(c.command)));
});

test("explicit body and copy controls preserve literal text, whitespace, labels and focus", async () => {
  const h = harness(); await h.ui.open(vendor); await h.ui.state.openThread(thread as any);
  const expand = h.button("Show body"); expand.click(); await tick();
  const collapse = h.button("Hide body");
  assert.equal(collapse.attributes.get("aria-expanded"), "true");
  const bodyId = collapse.attributes.get("aria-controls");
  assert.ok(nodes(h.root).some(n => n.id === bodyId));
  const literal = nodes(h.root).find(n => n.className === "thread-body-text");
  assert.equal(literal?.textContent, h.literal);
  assert.match(text(h.root), /Partial body: first \d+ of \d+ characters/);
  assert.equal(h.doc.activeElement, collapse);
  collapse.click(); await tick();
  assert.equal(nodes(h.root).some(n => n.className === "thread-body-text"), false);
  assert.equal(h.doc.activeElement, h.button("Show body"));
  h.button("Show copies (1)").click(); await tick();
  assert.ok(text(h.root).includes(`Summary ${hostile}`));
  h.button("Show attachment metadata (1)").click(); await tick();
  assert.ok(text(h.root).includes(hostile)); assert.match(text(h.root), /Size not recorded/);
  assert.equal(nodes(h.root).filter(n => ["SCRIPT", "IMG", "IFRAME", "A"].includes(n.tagName)).length, 0);
});

test("back retains account selection and restores its trigger; exit returns to vendor owner", async () => {
  const h = harness(); await h.ui.open(vendor);
  await h.ui.state.selectAccount({ provider: "imap", account_id: "retained" });
  h.button(thread.subject).click(); await tick();
  h.button("Back to threads").click(); await tick();
  assert.deepEqual(h.ui.state.account, { provider: "imap", account_id: "retained" });
  assert.equal(h.doc.activeElement, h.button(thread.subject));
  h.button("Back to vendors").click(); await tick();
  assert.equal(h.root.hidden, true); assert.equal(h.exited(), true);
});

test("failed local refresh clears headers/body and offers a scoped retry, never an empty state", async () => {
  const h = harness(); await h.ui.open(vendor); await h.ui.state.openThread(thread as any);
  await h.ui.state.openBody("canonical"); assert.ok(text(h.root).includes(h.literal));
  h.fail(); const refresh = h.ui.state.refresh();
  assert.equal(text(h.root).includes(h.literal), false);
  assert.equal(text(h.root).includes(thread.subject), false);
  await refresh;
  assert.match(text(h.root), /Could not load/); assert.ok(text(h.root).includes(vendor.display_name));
  assert.equal(nodes(h.root).some(n => n.className === "thread-body-text"), false);
  assert.equal(text(h.root).includes(thread.subject), false);
  h.button("Retry messages");
  assert.doesNotMatch(text(h.root), /No retained messages/);
});
