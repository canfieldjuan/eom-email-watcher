import type { InboxMessageRef, ThreadReadInvoke, ThreadRef } from "./threadReads.ts";

export interface InboxConversationQuery {
  limit?: number; cursor?: string | null; provider?: string | null; account_id?: string | null;
  sender?: string | null; sender_query?: string | null; keyword?: string | null;
  priority?: "urgent" | "high" | "normal" | "low" | "untriaged" | null;
  category?: "invoice" | "scheduling" | "customer_request" | "automated_notice" | "informational" | "other" | "unclassified" | null;
  status?: "pending" | "analyzed" | "summarized" | "skipped" | null;
}
export interface ConversationCounts {
  messages: number; matching_messages: number; pending_messages: number; analysis_errors: number;
  action_required: number; attachments: number; truncated_messages: number; unknown_truncation_messages: number;
}
export interface InboxConversation {
  thread_ref: ThreadRef;
  latest: { message_ref: InboxMessageRef; subject: string; sender: string; sender_name: string | null; received_at: string | null };
  matching_message_ref: InboxMessageRef;
  senders: { address: string; name: string | null }[];
  counts: ConversationCounts;
}
export interface InboxConversationPage {
  kind: "inbox_conversations_v1";
  items: InboxConversation[];
  next_cursor: string | null;
  totals: { conversations: number; messages: number; matching_messages: number };
}

const countKeys = ["messages", "matching_messages", "pending_messages", "analysis_errors", "action_required", "attachments", "truncated_messages", "unknown_truncation_messages"] as const;
const fail = (): never => { throw new Error("Invalid Inbox conversation projection"); };
function object(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.keys(value).length !== keys.length || !keys.every(k => Object.hasOwn(value, k))) return fail();
  return value as Record<string, unknown>;
}
const text = (v: unknown): v is string => typeof v === "string";
const nullableText = (v: unknown) => v === null || text(v);
const count = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
function reference(value: unknown, key: "message_id" | "thread_key"): Record<string, unknown> {
  const r = object(value, ["provider", "account_id", "mailbox_identity_key", key]);
  if (![r.provider, r.account_id, r[key]].every(v => text(v) && v.length > 0)) return fail();
  const identity = r.mailbox_identity_key;
  if (identity !== null && !(text(identity) && identity.length === 64
    && [...identity].every(c => "0123456789abcdef".includes(c)))) return fail();
  return r;
}

/** One runtime admission owner; never cast a flat/partial response into conversation authority. */
export function decodeInboxConversationPage(value: unknown): InboxConversationPage {
  const p = object(value, ["kind", "items", "next_cursor", "totals"]);
  if (p.kind !== "inbox_conversations_v1" || !Array.isArray(p.items) || p.items.length > 100
    || !(p.next_cursor === null || text(p.next_cursor) && p.next_cursor.length > 0 && p.next_cursor.length <= 4096)) return fail();
  const totals = object(p.totals, ["conversations", "messages", "matching_messages"]);
  if (!Object.values(totals).every(count)) return fail();
  const t = totals as unknown as InboxConversationPage["totals"];
  if (p.items.length > t.conversations || t.conversations > t.matching_messages || t.matching_messages > t.messages
    || t.conversations === 0 && t.messages !== 0 || p.items.length === 0 && p.next_cursor !== null) return fail();
  const keys = new Set<string>(); let messages = 0, matching = 0;
  for (const item of p.items) {
    const r = object(item, ["thread_ref", "latest", "matching_message_ref", "senders", "counts"]);
    const ref = reference(r.thread_ref, "thread_key");
    const latest = object(r.latest, ["message_ref", "subject", "sender", "sender_name", "received_at"]);
    if (!text(latest.subject) || !text(latest.sender) || !nullableText(latest.sender_name) || !nullableText(latest.received_at)
      || !Array.isArray(r.senders) || r.senders.length === 0) return fail();
    const senders = new Set<string>();
    for (const sender of r.senders) {
      const s = object(sender, ["address", "name"]);
      if (!text(s.address) || !nullableText(s.name)) return fail();
      const key = JSON.stringify([s.address, s.name]);
      if (senders.has(key)) return fail();
      senders.add(key);
    }
    if (!senders.has(JSON.stringify([latest.sender, latest.sender_name]))) return fail();
    for (const origin of [reference(latest.message_ref, "message_id"), reference(r.matching_message_ref, "message_id")]) {
      if (!["provider", "account_id", "mailbox_identity_key"].every(k => origin[k] === ref[k])) return fail();
    }
    const c = object(r.counts, countKeys);
    if (!Object.values(c).every(count)) return fail();
    const n = c as unknown as ConversationCounts;
    if (n.messages === 0 || n.matching_messages === 0
      || countKeys.filter(k => k !== "messages" && k !== "attachments").some(k => n[k] > n.messages)
      || n.truncated_messages + n.unknown_truncation_messages > n.messages) return fail();
    const key = JSON.stringify([ref.provider, ref.account_id, ref.mailbox_identity_key, ref.thread_key]);
    if (keys.has(key)) return fail(); keys.add(key);
    messages += n.messages; matching += n.matching_messages;
  }
  if (messages > t.messages || matching > t.matching_messages) return fail();
  return value as InboxConversationPage;
}

export class InboxConversationClient {
  private readonly invoke: ThreadReadInvoke;
  constructor(invoke: ThreadReadInvoke) { this.invoke = invoke; }
  async query(query: InboxConversationQuery): Promise<InboxConversationPage> {
    // Forward every runtime field to the strict host; do not sanitize away invalid selectors.
    return decodeInboxConversationPage(await this.invoke<unknown>("inbox_conversations_query", { query }));
  }
}
