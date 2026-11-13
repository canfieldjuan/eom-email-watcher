import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import type { InboxActionContext } from "../src/inboxThreadEntry.ts";
import { deferred, paneHarness } from "./inboxThreadPaneFixture.ts";
import { nodes } from "./threadDomFixture.ts";

const fixture = JSON.parse(readFileSync(new URL("../../tests/data/inbox_thread_action_page.json", import.meta.url), "utf8"));

test("the Inbox reader requests and mounts each admitted canonical email's own record", async () => {
  const mounted: string[] = [];
  const h = paneHarness((row, record) => {
    mounted.push(record.message_id);
    const extra = row.ownerDocument.createElement("div");
    extra.textContent = record.attachments[0].part_id;
    row.append(extra);
  });
  h.respond(async (command, query) => {
    if (command !== "threads_messages") return h.defaultResponse(command, query);
    const result = await h.defaultResponse(command, query);
    return { ...result, items: [
      ...fixture.action_page.items.map(message => ({
        ...message,
        inbox_action: { ...message.inbox_action, provider: "imap", account_id: "retained",
          thread_entry_ref: { provider: "imap", account_id: "retained",
            mailbox_identity_key: "identity", message_id: message.message_id } },
      })),
      { ...result.items[0], message_id: "content-only", inbox_action: null },
    ] };
  });
  await h.ui.open(h.rows[0]);
  assert.equal(h.calls.find(call => call.command === "threads_messages")?.query.include_inbox_actions, true);
  assert.deepEqual(mounted, ["newer", "m"]);
  const cards = nodes(h.root).filter(node => node.className === "thread-card");
  assert.ok(nodes(cards[0]).some(node => node.textContent === "second-part"));
  assert.ok(nodes(cards[1]).some(node => node.textContent === "first-part"));
  assert.equal(nodes(cards[2]).some(node => node.textContent.endsWith("-part")), false);
});

function readerFixture() {
  const actions = new Map<string, InboxActionContext>();
  const notices: { subject: string; reason: string }[] = [];
  const h = paneHarness((_row, record, context) => actions.set(record.message_id, context),
    (subject, reason) => notices.push({subject, reason}));
  let includeSent = true, foreign = false;
  const records = fixture.action_page.items.map(message => ({
    ...message,
    inbox_action: { ...message.inbox_action, provider: "imap", account_id: "retained",
      thread_entry_ref: { provider: "imap", account_id: "retained", mailbox_identity_key: "identity",
        message_id: message.message_id } },
  }));
  const reply = async (command, query) => {
    const result = await h.defaultResponse(command, query);
    if (command !== "threads_messages") return result;
    return { ...result, next_cursor: null, items: records.map(message => message.message_id === "newer"
      ? { ...message, inbox_action: !includeSent ? null : foreign
        ? { ...message.inbox_action, thread_entry_ref: { ...message.inbox_action.thread_entry_ref, mailbox_identity_key: "foreign" } }
        : message.inbox_action }
      : message) };
  };
  h.respond(reply);
  return { ...h, actions, notices, reply, removeSent: () => { includeSent = false; }, foreignSent: () => { foreign = true; } };
}

test("approval waits for the actual refresh and keeps the initiating email across body focus changes", async () => {
  const h = readerFixture();
  await h.ui.open(h.rows[0]);
  const sent = h.actions.get("newer")!;
  await h.ui.state.openBody("m");
  const gate = deferred();
  h.respond(async (command, query) => {
    if (command === "threads_messages") await gate.promise;
    return h.reply(command, query);
  });
  h.ui.invalidate();
  assert.equal(sent.current(), false);
  h.ui.revalidateSource();
  const refreshing = h.ui.refresh();
  let settled = false;
  const approving = sent.afterApproval().then(result => { settled = true; return result; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(settled, false);
  assert.match(h.notices[0].reason, /not sent yet/i);
  gate.resolve(undefined);
  await refreshing;
  assert.equal(await approving, true);
  assert.equal(sent.messageId, "newer");
});

test("pending approval aborts when its member loses eligibility, even when the other email remains", async () => {
  for (const change of ["removed", "foreign", "back", "hidden", "hold", "query", "failure"]) {
    const h = readerFixture();
    await h.ui.open(h.rows[0]);
    const sent = h.actions.get("newer")!;
    assert.equal(sent.current(), true);
    if (change === "back") h.ui.back();
    else if (change === "hidden") { h.doc.visibilityState = "hidden"; h.ui.invalidate(); }
    else if (change === "hold") h.ui.beginMutation();
    else if (change === "query") { h.snapshot.queryEpoch++; h.ui.invalidate(); }
    else {
      h.ui.invalidate();
      if (change === "failure") h.ui.failRefresh(h.ui.refreshGeneration);
      else {
        if (change === "removed") h.removeSent(); else h.foreignSent();
        h.ui.revalidateSource();
        await h.ui.refresh();
        assert.equal(h.actions.get("m")?.current(), true);
      }
    }
    assert.equal(await sent.afterApproval(), false, change);
    assert.equal(h.notices.at(-1)?.subject, sent.subject);
    assert.match(h.notices.at(-1)?.reason ?? "", /not sent/i);
  }
});

test("a fresh anchor renews the same conversation and an out-of-query conversation closes", async () => {
  const h = readerFixture();
  const thread = (await h.defaultResponse("threads_messages", {})).thread;
  const first = { ...h.rows[0], thread_ref: thread.thread_ref };
  h.snapshot.items = [first];
  await h.ui.open(first);
  assert.equal(h.ui.state.inboxOrigin?.message_id, first.message_id);
  h.ui.invalidate();
  const replacement = { ...h.rows[1], thread_ref: thread.thread_ref };
  h.snapshot.items = [replacement];
  h.respond(async (command, query) => {
    const reply = await h.reply(command, query);
    if (command === "threads_resolve") return { ...reply, context: { kind: "non_vendor", thread } };
    if (command === "threads_messages") return { ...reply, thread };
    return reply;
  });
  h.ui.revalidateSource();
  await h.ui.refresh();
  assert.equal(h.ui.state.inboxOrigin?.message_id, replacement.message_id);
  assert.equal(h.ui.state.messages.status, "ready");
  assert.equal(h.ui.isSelected(replacement), true);
  h.ui.invalidate();
  h.snapshot.items = [];
  h.ui.revalidateSource();
  assert.equal(h.ui.selected, false);
  assert.equal(h.ui.state.messages.value, null);
});
