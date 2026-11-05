import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { ThreadReadClient } from "../src/threadReads.ts";

const origin = { provider: "imap", account_id: "retained", mailbox_identity_key: null, message_id: "seed" };
const ref = { provider: origin.provider, account_id: origin.account_id, mailbox_identity_key: null, thread_key: "thread" };

test("Inbox resolver forwards every field without changing the selected origin", async () => {
  const calls: unknown[] = [];
  const result = { state: "resolved", message_ref: origin, context: { kind: "non_vendor", thread: {
    thread_ref: ref, relation: "non_vendor", owner_vendor: null, subject: "Newest", latest_received_at: null, message_count: 1,
  } } };
  const client = new ThreadReadClient(async <T>(command: string, args: Record<string, unknown>) => {
    calls.push({ command, args: structuredClone(args) }); return result as T;
  });
  const query = { message_ref: origin, limit: 3, cursor: "page", unexpected: true };
  assert.strictEqual(await client.resolve(query), result);
  assert.deepEqual(calls, [{ command: "threads_resolve", args: { query } }]);
});

test("all guarded child forms retain origin and even invalid runtime fields for host rejection", async () => {
  for (const selector of [{ vendor_id: "v", thread_ref: ref, inbox_message: origin },
    { inbox_thread: { thread_ref: ref, message_ref: origin } }]) {
    for (const operation of ["messages", "body", "copies", "inventory"]) {
      const calls: unknown[] = [];
      const failure = { code: "invalid_request" };
      const client = new ThreadReadClient(async (command, args) => { calls.push({ command, args }); throw failure; });
      const query = { ...selector, unexpected: "must reach host", ...(operation !== "messages" ? { message_id: "target" } : {}),
        ...(operation === "inventory" ? { copy_ref: "copy" } : {}), ...(operation !== "body" ? { limit: 3, cursor: "page" } : {}) };
      const method = operation === "inventory" ? "copies" : operation as "messages" | "body" | "copies";
      await assert.rejects(client[method](query as never), error => error === failure);
      assert.deepEqual(calls, [{ command: `threads_${method}`, args: { query } }]);
    }
  }
});

test("native resolver uses the existing local admission permit and is registered without a Connect gate", () => {
  const source = readFileSync(new URL("../src-tauri/src/lib.rs", import.meta.url), "utf8");
  const command = source.slice(source.indexOf("async fn threads_resolve("), source.indexOf("async fn threads_messages("));
  assert.ok(command.indexOf("require_admitted()?") >= 0);
  assert.ok(command.indexOf("require_admitted()?") < command.indexOf("engine.resolve_thread(query)"));
  assert.match(command, /query: ThreadResolveQuery/); assert.match(command, /Result<ThreadResolveResult, EngineError>/);
  assert.match(command, /spawn_blocking/); assert.doesNotMatch(command, /connect|entitlement|capabilit/i);
  assert.match(source.slice(source.indexOf("tauri::generate_handler![")), /\bthreads_resolve,/);
});

test("Inbox resolver errors propagate without dropping guards or selecting a fallback vendor", async () => {
  for (const code of ["stale_selection", "not_found", "stale_cursor", "invalid_request", "runtime_error"]) {
    const failure = { code }, calls: unknown[] = [];
    const client = new ThreadReadClient(async (command, args) => {
      calls.push({ command, args: structuredClone(args) }); throw failure;
    });
    const query = { message_ref: origin, vendor_id: "explicit-choice", cursor: "invalid-mixed-paging" };
    await assert.rejects(client.resolve(query as never), error => error === failure);
    assert.deepEqual(calls, [{ command: "threads_resolve", args: { query } }]);
  }
});
