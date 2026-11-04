import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { ThreadReadClient } from "../src/threadReads.ts";
import type { ThreadListQuery, ThreadCopiesQuery, ThreadReadInvoke } from "../src/threadReads.ts";

const ref = {
  provider: "imap", account_id: "retained-account",
  mailbox_identity_key: null, thread_key: "alias:thread/β",
};
const listQuery: ThreadListQuery = {
  vendor_id: "11111111-1111-4111-8111-111111111111", provider: "imap",
  account_id: "retained-account", limit: 7, cursor: "list-cursor",
};

test("thread list forwards the whole query and preserves locally retained metadata", async () => {
  const response = { items: [{ thread_ref: ref, relation: "linked",
    owner_vendor: { vendor_id: "other", display_name: "Other owner" },
    subject: "<literal subject>", latest_received_at: null, message_count: 2 }],
    next_cursor: "next-page" };
  const calls: unknown[] = [];
  const invoke: ThreadReadInvoke = async <T>(command: string, args: Record<string, unknown>) => {
    calls.push(structuredClone({ command, args }));
    return response as T;
  };
  const client = new ThreadReadClient(invoke);
  assert.strictEqual(await client.list(listQuery), response);
  assert.deepEqual(calls, [{ command: "threads_list", args: { query: listQuery } }]);
  calls.length = 0;
  await client.list({ vendor_id: "vendor" });
  assert.deepEqual(calls, [{ command: "threads_list", args: { query: { vendor_id: "vendor" } } }]);
});

test("copy and inventory reads preserve identity, selector, resolved ref and empty pages", async () => {
  for (const identity of [null, "b".repeat(64)]) {
    for (const copy_ref of [null, "source-1"]) {
      for (const empty of [false, true]) {
        const query: ThreadCopiesQuery = { vendor_id: "vendor", thread_ref: {
          ...ref, mailbox_identity_key: identity }, message_id: "canonical-message",
          copy_ref, limit: 3, cursor: "copy-cursor" };
        const item = copy_ref === null
          ? { copy_ref: "source-1", locations: ["INBOX", "Sent"], summary: null, attachment_count: 2 }
          : { part_id: "part-1", filename: "invoice.pdf", media_type: "application/pdf", byte_size: null };
        const response = { thread_ref: { ...query.thread_ref, thread_key: "resolved-survivor" },
          message_id: "canonical-message", items: empty ? [] : [item], next_cursor: null };
        const calls: unknown[] = [];
        const invoke: ThreadReadInvoke = async <T>(command: string, args: Record<string, unknown>) => {
          calls.push(structuredClone({ command, args }));
          return response as T;
        };
        assert.strictEqual(await new ThreadReadClient(invoke).copies(query), response);
        assert.deepEqual(calls, [{ command: "threads_copies", args: { query } }]);
      }
    }
  }
});

test("read errors reach the caller without retries, fallback or silently dropped fields", async () => {
  for (const code of ["stale_cursor", "not_found", "invalid_request"]) {
    const error = { code, message: "Local history changed" };
    const calls: unknown[] = [];
    const invoke: ThreadReadInvoke = async (command, args) => {
      calls.push(structuredClone({ command, args }));
      throw error;
    };
    const client = new ThreadReadClient(invoke);
    // A runtime caller can bypass TS. Preserve unknown scope for the host's
    // strict parser to reject; do not turn it into a different valid query.
    const query = { ...listQuery, mailbox_identity_key: "unexpected selector" };
    await assert.rejects(client.list(query), caught => caught === error);
    const copies = { vendor_id: "vendor", thread_ref: { ...ref, extra_scope: "unexpected" },
      message_id: "canonical-message" };
    await assert.rejects(client.copies(copies), caught => caught === error);
    assert.deepEqual(calls, [
      { command: "threads_list", args: { query } },
      { command: "threads_copies", args: { query: copies } },
    ]);
  }
});

test("messages and bodies preserve complete selectors, resolved refs and every body state", async () => {
  const unstored = ["pending", "source_gone", "outside_folders", "removal_pending", "mailbox_identity_unavailable"];
  for (const identity of [null, "b".repeat(64)]) {
    for (const body of [...unstored.map(state => ({ state })),
      { state: "stored", text: "", stored_chars: 0, source_chars: 0 },
      { state: "stored", text: "🙂e\u0301", stored_chars: 3, source_chars: 8 }]) {
      const thread_ref = { ...ref, mailbox_identity_key: identity };
      const resolved = { ...thread_ref, thread_key: "survivor" };
      const bodyQuery = { vendor_id: "vendor", thread_ref, message_id: "canonical" };
      const messagesQuery = { vendor_id: "vendor", thread_ref, limit: 7, cursor: "messages-cursor" };
      const bodyResult = { thread_ref: resolved, message_id: "canonical", body };
      const { text: _text, ...body_state } = body as typeof body & { text?: string };
      const messagesResult = { thread: { thread_ref: resolved, relation: "linked",
        owner_vendor: { vendor_id: "owner", display_name: "Owner" }, subject: "Newest visible subject", history_notice: "Partial history" },
        items: [{ message_id: "canonical", subject: "<literal subject>", direction: "outbound",
          sender: "sender@example.test", sender_name: null, attributed_vendor: null,
          received_at: null, body_state, copy_count: 2 }], next_cursor: "next" };
      const calls: unknown[] = [];
      const invoke: ThreadReadInvoke = async <T>(command: string, args: Record<string, unknown>) => {
        calls.push(structuredClone({ command, args }));
        return (command === "threads_body" ? bodyResult : messagesResult) as T;
      };
      const client = new ThreadReadClient(invoke);
      assert.strictEqual(await client.messages(messagesQuery), messagesResult);
      assert.strictEqual(await client.body(bodyQuery), bodyResult);
      assert.deepEqual(calls, [
        { command: "threads_messages", args: { query: messagesQuery } },
        { command: "threads_body", args: { query: bodyQuery } },
      ]);
    }
  }
});

test("message and body errors propagate once without narrowing invalid runtime selectors", async () => {
  for (const code of ["stale_cursor", "not_found", "invalid_request", "runtime_error"]) {
    const error = { code, message: "Local read failed" };
    const calls: unknown[] = [];
    const client = new ThreadReadClient(async (command, args) => {
      calls.push(structuredClone({ command, args }));
      throw error;
    });
    const messages = { vendor_id: "vendor", thread_ref: { ...ref, unknown: "scope" }, unexpected: true };
    const body = { ...messages, message_id: "canonical", cursor: "invalid-body-paging" };
    await assert.rejects(client.messages(messages), caught => caught === error);
    await assert.rejects(client.body(body), caught => caught === error);
    assert.deepEqual(calls, [
      { command: "threads_messages", args: { query: messages } },
      { command: "threads_body", args: { query: body } },
    ]);
  }
});

test("TypeScript requires complete selectors and closed message/body/copy shapes", () => {
  const directory = mkdtempSync(join(tmpdir(), "thread-types-"));
  try {
    const modulePath = fileURLToPath(new URL("../src/threadReads.ts", import.meta.url));
    const fixture = join(directory, "contract.ts");
    writeFileSync(fixture, `
import { ThreadReadClient } from ${JSON.stringify(modulePath)};
import type { ThreadRef, ThreadListQuery, ThreadCopy, ThreadInventory, ThreadCopyPage, ThreadBody, ThreadBodyState, ThreadMessage, ThreadMessagePage, ThreadBodyResponse, ThreadContext } from ${JSON.stringify(modulePath)};
declare const client: ThreadReadClient;
const ref: ThreadRef = { provider: "imap", account_id: "account", mailbox_identity_key: null, thread_key: "thread" };
const all: ThreadListQuery = { vendor_id: "vendor" };
client.list(all);
client.list({ vendor_id: "vendor", provider: null, account_id: null });
client.list({ vendor_id: "vendor", provider: "imap", account_id: "retained" });
const base = { vendor_id: "vendor", thread_ref: ref, message_id: "canonical" };
const copies: Promise<ThreadCopyPage<ThreadCopy>> = client.copies(base);
const explicit: typeof copies = client.copies({ ...base, copy_ref: null });
const inventory: Promise<ThreadCopyPage<ThreadInventory>> = client.copies({ ...base, copy_ref: "source" });
// @ts-expect-error identity must be present even when unresolved
const missing: ThreadRef = { provider: "imap", account_id: "account", thread_key: "thread" };
// @ts-expect-error account selector is a pair
client.list({ vendor_id: "vendor", provider: "imap" });
// @ts-expect-error account selector is a pair
client.list({ vendor_id: "vendor", account_id: "account" });
// @ts-expect-error list query has no identity selector
client.list({ vendor_id: "vendor", mailbox_identity_key: null });
// @ts-expect-error copy_ref determines inventory result, never copy metadata
const wrong: typeof copies = client.copies({ ...base, copy_ref: "source" });
// @ts-expect-error inventory rows cannot substitute for copy rows
const wrongAgain: typeof inventory = client.copies(base);
// @ts-expect-error selected message is always canonical and required
client.copies({ vendor_id: "vendor", thread_ref: ref });
const messages: Promise<ThreadMessagePage> = client.messages({ vendor_id: "vendor", thread_ref: ref, limit: 4, cursor: null });
const body: Promise<ThreadBodyResponse> = client.body(base);
const stored: ThreadBody = { state: "stored", text: "", stored_chars: 0, source_chars: 0 };
const metadata: ThreadBodyState = { state: "stored", stored_chars: 0, source_chars: 0 };
const unavailable: ThreadBody = { state: "mailbox_identity_unavailable" };
const context: ThreadContext = { thread_ref: ref, relation: "owned", owner_vendor: { vendor_id: "v", display_name: "Vendor" }, subject: "Newest", history_notice: "Partial" };
const { subject: _subject, ...withoutSubject } = context;
// @ts-expect-error snapshot title is required
const missingTitle: ThreadContext = withoutSubject;
// @ts-expect-error snapshot title cannot be null
const nullTitle: ThreadContext = { ...context, subject: null };
// @ts-expect-error snapshot title must be a string
const numericTitle: ThreadContext = { ...context, subject: 123 };
// @ts-expect-error body must select a canonical message
client.body({ vendor_id: "vendor", thread_ref: ref });
// @ts-expect-error body does not paginate
client.body({ ...base, cursor: "cursor" });
// @ts-expect-error message list has no message selector
client.messages({ vendor_id: "vendor", thread_ref: ref, message_id: "canonical" });
// @ts-expect-error stored full body requires text
const missingText: ThreadBody = { state: "stored", stored_chars: 0, source_chars: 0 };
// @ts-expect-error metadata never has text
const metadataText: ThreadBodyState = { state: "stored", stored_chars: 0, source_chars: 0, text: "hidden" };
// @ts-expect-error nonstored state never has text
const pendingText: ThreadBody = { state: "pending", text: "hidden" };
// @ts-expect-error nonstored state never has stored lengths
const pendingLength: ThreadBodyState = { state: "pending", stored_chars: 0, source_chars: 0 };
// @ts-expect-error body states are closed
const unknown: ThreadBody = { state: "unknown" };
declare const message: ThreadMessage;
const senderName: string | null = message.sender_name;
const attributed: { vendor_id: string; display_name: string } | null = message.attributed_vendor;
const received: string | null = message.received_at;
// @ts-expect-error directions are closed
const wrongDirection: ThreadMessage = { ...message, direction: "sideways" };
// @ts-expect-error sender_name is required even when null
const missingName: ThreadMessage = { message_id: "canonical", subject: "", direction: "inbound", sender: "a@example.test", attributed_vendor: null, received_at: null, body_state: unavailable, copy_count: 1 };
`);
    const compiler = fileURLToPath(new URL("../node_modules/typescript/lib/tsc.js", import.meta.url));
    const result = spawnSync(process.execPath, [compiler, "--noEmit", "--strict", "--skipLibCheck",
      "--target", "ES2022", "--module", "ESNext", "--moduleResolution", "bundler",
      "--allowImportingTsExtensions", fixture], { encoding: "utf8", cwd: directory });
    assert.equal(result.status, 0, result.stdout + result.stderr + String(result.error ?? ""));
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
