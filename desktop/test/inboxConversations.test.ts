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
    (p:any)=>p.items[0].counts.unknown_truncation_messages=3,
    (p:any)=>p.items.push(structuredClone(p.items[0])),
    (p:any)=>p.kind="flat",
    (p:any)=>p.totals.conversations=0,
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
