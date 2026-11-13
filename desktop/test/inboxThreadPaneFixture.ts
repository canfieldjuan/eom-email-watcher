import assert from "node:assert/strict";
import { InboxThreadEntry, inboxThreadOriginKey, type InboxThreadSnapshot } from "../src/inboxThreadEntry.ts";
import type { InboxItem } from "../src/inboxItem.ts";
import type { InboxActionContext } from "../src/inboxThreadEntry.ts";
import { ThreadReadClient } from "../src/threadReads.ts";
import { Element, nodes } from "./threadDomFixture.ts";

export function paneHarness(renderAction?: (row: HTMLElement, record: InboxItem, context: InboxActionContext) => void,
  actionNotSent?: (subject: string, reason: string) => void) {
  const events = new Map<string, Function[]>();
  const doc: any = { activeElement: null, addEventListener: (type: string, fn: Function) => {
    events.set(type, [...(events.get(type) ?? []), fn]);
  } };
  class Node extends Element {
    style = { setProperty() {} };
    focus() { super.focus(); for (const fn of events.get("focusin") ?? []) fn({ target: this }); }
  }
  doc.createElement = (tag: string) => new Node(tag, doc);
  const el = (tag = "div") => new Node(tag, doc);
  const root = el("section"), columns = el(), heading = el("h2"), listScroll = el();
  const layout = { grid: el(), readerPane: el("section"), placeholder: el("p"), source: el("p"),
    senders: el("aside"), senderToggle: el("button"), senderContent: el(), footer: el() };
  layout.senders.append(layout.senderToggle, layout.senderContent);
  layout.readerPane.append(layout.placeholder, layout.source, root);
  layout.grid.append(layout.senders, columns, layout.readerPane);
  const origin = (id: string) => ({ provider: "imap", account_id: "retained", mailbox_identity_key: "identity", message_id: id });
  const rows = ["a", "b"].map(id => ({ ...origin(id), thread_entry_ref: origin(id), subject: `Origin ${id} <img>`,
    sender: `full-${id}@example.test`, received_at: "2026-11-07T00:00:00Z" }));
  const triggers = rows.map(row => { const button = el("button"); button.dataset.inboxThreadOrigin = inboxThreadOriginKey(row.thread_entry_ref); return button; });
  columns.append(listScroll, ...triggers);
  const snapshot: InboxThreadSnapshot = { configured: true, visible: true, blocked: false, queryEpoch: 4, itemsEpoch: 4,
    items: rows, scope: { mutation_epoch: 1, mailbox_operation_revision: 3 }, spanScope: { mutation_epoch: 1, mailbox_operation_revision: 3 } };
  const calls: { command: string; query: any }[] = [];
  const thread = (id: string) => ({ thread_ref: { ...origin(id), message_id: undefined, thread_key: `thread-${id}` },
    subject: `Newest ${id}`, relation: "non_vendor", owner_vendor: null, latest_received_at: null, message_count: 50 });
  const cleanThread = (id: string) => { const value = thread(id); delete value.thread_ref.message_id; return value; };
  let revision = 0;
  const defaultResponse = async (command: string, query: any): Promise<any> => {
    const id = query.message_ref?.message_id ?? query.inbox_thread?.message_ref.message_id ?? "a";
    const selected = cleanThread(id);
    if (command === "threads_resolve") return { state: "resolved", message_ref: origin(id), context: { kind: "non_vendor", thread: selected } };
    if (command === "threads_messages") return { thread: selected, items: Array.from({ length: 25 }, (_, i) => {
      const n = i + (query.cursor ? 25 : 0);
      return { message_id: n === 0 ? id : `${id}-${n}`, subject: `Message ${n}`, sender: "sender@example.test", sender_name: null,
        received_at: null, direction: "inbound", attributed_vendor: null, copy_count: 1,
        body_state: { state: "stored", stored_chars: 5, source_chars: 5 } };
    }), next_cursor: query.cursor ? null : "fresh-page-2" };
    if (command === "threads_body") return { thread_ref: selected.thread_ref, message_id: query.message_id,
      body: { state: "stored", text: `body${revision}`, stored_chars: 5, source_chars: 5 } };
    return { thread_ref: selected.thread_ref, message_id: query.message_id, items: [], next_cursor: null };
  };
  let respond = defaultResponse, refreshes = 0;
  const ui = new InboxThreadEntry({ root: root as any, columns: columns as any, heading: heading as any,
    layout: layout as any, accounts: () => [], snapshot: () => snapshot, scrollElements: () => [listScroll as any],
    renderAction, actionNotSent, client: new ThreadReadClient(async <T>(command: string, args: any) => {
      calls.push({ command, query: structuredClone(args.query) }); return await respond(command, args.query) as T;
    }), refreshInbox: async () => { refreshes++; } });
  const button = (label: string) => { const value = nodes(root).find(n => n.tagName === "BUTTON" && n.textContent === label); assert.ok(value, label); return value; };
  return { ui, root, columns, heading, layout, rows, triggers, listScroll, snapshot, calls, doc, el, button,
    defaultResponse, respond: (fn: typeof respond) => { respond = fn; }, revise: () => { revision++; }, refreshes: () => refreshes };
}

export function deferred() {
  let resolve!: (value: any) => void;
  const promise = new Promise<any>(r => { resolve = r; });
  return { promise, resolve };
}
