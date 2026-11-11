import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { decodeInboxConversationPage, InboxConversationClient } from "../src/inboxConversations.ts";
import type { InboxConversationPage } from "../src/inboxConversations.ts";
import type { ThreadReadInvoke } from "../src/threadReads.ts";

const fixture = JSON.parse(readFileSync(new URL("../../tests/data/inbox_conversation_page.json", import.meta.url), "utf8"));

test("client decodes the actual Python fixture and forwards the entire runtime query", async () => {
  const calls: unknown[] = [];
  const invoke: ThreadReadInvoke = async <T>(command: string, args: Record<string, unknown>) => { calls.push({command,args}); return fixture as T; };
  const query = { sender: "older@example.test", priority: "high" as const, limit: 1, unexpected: "host must reject" };
  assert.equal(await new InboxConversationClient(invoke).query(query), fixture);
  assert.deepEqual(calls, [{command:"inbox_conversations_query",args:{query}}]);
  const page = decodeInboxConversationPage(fixture);
  assert.equal(page.items[0].counts.messages, 2);
  assert.equal(Object.hasOwn(page, "latest"), false);
});

test("closed page and nested models reject partial, mixed and legacy wire data", () => {
  const paths = [[], ["items",0], ["items",0,"thread_ref"], ["items",0,"latest"],
    ["items",0,"latest","message_ref"], ["items",0,"matching_message_ref"],
    ["items",0,"senders",0], ["items",0,"counts"], ["totals"]];
  for (const path of paths) {
    const target = (page: any) => path.reduce((v,k)=>v[k],page);
    for (const mode of ["extra","missing"]) {
      const page = structuredClone(fixture), row = target(page);
      if (mode === "extra") row.extra = true; else delete row[Object.keys(row)[0]];
      assert.throws(()=>decodeInboxConversationPage(page),undefined,JSON.stringify([path,mode]));
    }
  }
  assert.throws(()=>decodeInboxConversationPage({items:[],next_cursor:null}));
  const mixed = structuredClone(fixture); mixed.items.push({});
  assert.throws(()=>decodeInboxConversationPage(mixed));
});

test("member counts, explicit nullable identity and cross-scope references fail closed", () => {
  for (const bad of [-1, 0, false, "", null, 1.5, Number.MAX_SAFE_INTEGER+1]) {
    const page = structuredClone(fixture); page.items[0].counts.messages = bad;
    assert.throws(()=>decodeInboxConversationPage(page),undefined,String(bad));
  }
  for (const mutation of [
    (p:any)=>p.items[0].latest.message_ref.account_id="other",
    (p:any)=>p.items[0].matching_message_ref.mailbox_identity_key=null,
    (p:any)=>p.items[0].counts.matching_messages=3,
    (p:any)=>p.kind="flat",
    (p:any)=>p.items[0].latest.sender="absent@example.test",
    (p:any)=>p.items[0].senders.push(structuredClone(p.items[0].senders[0])),
    (p:any)=>{
      p.items[0].thread_ref.mailbox_identity_key="invalid";
      p.items[0].latest.message_ref.mailbox_identity_key="invalid";
      p.items[0].matching_message_ref.mailbox_identity_key="invalid";
    },
  ]) {
    const page = structuredClone(fixture); mutation(page);
    assert.throws(()=>decodeInboxConversationPage(page));
  }
  const legacy: InboxConversationPage = structuredClone(fixture);
  legacy.items[0].thread_ref.mailbox_identity_key = null;
  legacy.items[0].latest.message_ref.mailbox_identity_key = null;
  legacy.items[0].matching_message_ref.mailbox_identity_key = null;
  assert.equal(decodeInboxConversationPage(legacy),legacy);
  assert.deepEqual(decodeInboxConversationPage({kind:"inbox_conversations_v1",items:[],next_cursor:null,
    totals:{conversations:0,messages:0,matching_messages:0}}).items,[]);
});

function pageWithRows(size: number): InboxConversationPage {
  const page: InboxConversationPage = structuredClone(fixture);
  const sample = page.items[0];
  page.items = Array.from({ length: size }, (_, index) => {
    const row = structuredClone(sample);
    row.thread_ref.thread_key = `conversation-${index}`;
    row.latest.message_ref.message_id = `latest-${index}`;
    row.matching_message_ref.message_id = `latest-${index}`;
    return row;
  });
  page.next_cursor = null;
  page.totals = {
    conversations: size,
    messages: size * sample.counts.messages,
    matching_messages: size * sample.counts.matching_messages,
  };
  return page;
}

function assertInvalidPage(page: unknown, label?: string): void {
  assert.throws(
    () => decodeInboxConversationPage(page),
    { name: "Error", message: "Invalid Inbox conversation projection" },
    label,
  );
}

test("decoder caps the page size with distinct rows and consistent totals", () => {
  const allowed = pageWithRows(100);
  assert.equal(decodeInboxConversationPage(allowed), allowed);
  assertInvalidPage(pageWithRows(101));
});

test("decoder accepts null and bounded cursor strings", () => {
  const page = pageWithRows(1);
  for (const cursor of [null, "x", "x".repeat(4096)]) {
    const allowed = { ...page, next_cursor: cursor };
    assert.equal(decodeInboxConversationPage(allowed), allowed);
  }
  assertInvalidPage({ ...page, next_cursor: "" }, "empty cursor");
  assertInvalidPage({ ...page, next_cursor: "x".repeat(4097) }, "overlong cursor");
  assertInvalidPage({ ...page, next_cursor: ["x"] }, "array with a valid length");
});

test("decoder rejects a cursor on an otherwise valid empty page", () => {
  const page = pageWithRows(0);
  assert.equal(decodeInboxConversationPage(page), page);
  assertInvalidPage({ ...page, next_cursor: "cursor" });
});

test("decoder refuses non-integer or non-numeric totals before comparing them", () => {
  const page = pageWithRows(1);
  assert.equal(decodeInboxConversationPage(page), page);
  for (const field of ["conversations", "matching_messages", "messages"] as const) {
    const totals = { ...page.totals, [field]: String(page.totals[field]) };
    assertInvalidPage({ ...page, totals }, field);
  }
  for (const messages of [2.5, Number.MAX_SAFE_INTEGER + 1]) {
    assertInvalidPage({ ...page, totals: { ...page.totals, messages } });
  }
});

test("decoder compares the returned row count to the conversation total", () => {
  const page = pageWithRows(2);
  assert.equal(decodeInboxConversationPage(page), page);
  page.totals.conversations = 1;
  assertInvalidPage(page);
});

test("decoder independently checks the order and zero case of global totals", () => {
  const empty = pageWithRows(0);
  const totals = { conversations: 1, matching_messages: 1, messages: 2 };
  const allowed = { ...empty, totals };
  assert.equal(decodeInboxConversationPage(allowed), allowed);
  assertInvalidPage({ ...allowed, totals: { ...totals, conversations: 2 } });
  assertInvalidPage({ ...allowed, totals: { ...totals, matching_messages: 3 } });

  assert.equal(decodeInboxConversationPage(empty), empty);
  assertInvalidPage({ ...empty, totals: { ...empty.totals, messages: 1 } });
});

test("decoder requires nonempty reference text even when the scopes agree", () => {
  const allowed = pageWithRows(1);
  assert.equal(decodeInboxConversationPage(allowed), allowed);
  for (const field of ["provider", "account_id"] as const) {
    const page = structuredClone(allowed);
    const row = page.items[0];
    row.thread_ref[field] = "";
    row.latest.message_ref[field] = "";
    row.matching_message_ref[field] = "";
    assertInvalidPage(page, field);
  }
  const missingThreadKey = structuredClone(allowed);
  missingThreadKey.items[0].thread_ref.thread_key = "";
  assertInvalidPage(missingThreadKey);
  const missingMessageId = structuredClone(allowed);
  missingMessageId.items[0].latest.message_ref.message_id = "";
  assertInvalidPage(missingMessageId);
});

test("decoder checks header field types on a valid conversation", () => {
  const page = pageWithRows(1);
  const row = page.items[0];
  assert.equal(decodeInboxConversationPage(page), page);
  for (const field of ["subject", "sender", "sender_name", "received_at"] as const) {
    const malformed = { ...row, latest: { ...row.latest, [field]: 7 } };
    assertInvalidPage({ ...page, items: [malformed] }, field);
  }
});

test("decoder requires a sender list containing the latest sender", () => {
  const page = pageWithRows(1);
  assert.equal(decodeInboxConversationPage(page), page);
  page.items[0].senders = [];
  assertInvalidPage(page);
});

test("decoder validates attachment counts independently of member counts", () => {
  const page = pageWithRows(1);
  const row = page.items[0];
  assert.equal(decodeInboxConversationPage(page), page);
  for (const attachments of [-1, false, "2", null, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
    const malformed = { ...row, counts: { ...row.counts, attachments } };
    assertInvalidPage({ ...page, items: [malformed] }, String(attachments));
  }
});

test("decoder refuses a returned conversation with zero matches or zero messages", () => {
  const page = pageWithRows(1);
  const row = page.items[0];
  assert.equal(decodeInboxConversationPage(page), page);
  assertInvalidPage({
    ...page,
    items: [{ ...row, counts: { ...row.counts, matching_messages: 0 } }],
  });
  const zeroCounts = Object.fromEntries(Object.keys(row.counts).map(field => [field, 0]));
  assertInvalidPage({ ...page, items: [{ ...row, counts: zeroCounts }] });
});

test("decoder rejects truncation overlap when each count fits the row", () => {
  const page = pageWithRows(1);
  const counts = page.items[0].counts;
  counts.truncated_messages = 1;
  counts.unknown_truncation_messages = 1;
  assert.equal(decodeInboxConversationPage(page), page);
  counts.unknown_truncation_messages = 2;
  assertInvalidPage(page);
});

test("decoder rejects duplicate thread references even when totals cover both rows", () => {
  const page = pageWithRows(2);
  assert.equal(decodeInboxConversationPage(page), page);
  page.items[1].thread_ref = structuredClone(page.items[0].thread_ref);
  assertInvalidPage(page);
});

test("decoder compares page message and match sums independently to global totals", () => {
  const matches = pageWithRows(2);
  assert.equal(decodeInboxConversationPage(matches), matches);
  matches.totals.matching_messages -= 1;
  assertInvalidPage(matches, "matching-message subtotal");

  const messages = pageWithRows(2);
  for (const row of messages.items) row.counts.matching_messages = 1;
  messages.totals.matching_messages = 2;
  assert.equal(decodeInboxConversationPage(messages), messages);
  messages.totals.messages -= 1;
  assertInvalidPage(messages, "message subtotal");
});
