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

test("TypeScript requires explicit identity, paired account selectors and correct copy result types", () => {
  const directory = mkdtempSync(join(tmpdir(), "thread-types-"));
  try {
    const modulePath = fileURLToPath(new URL("../src/threadReads.ts", import.meta.url));
    const fixture = join(directory, "contract.ts");
    writeFileSync(fixture, `
import { ThreadReadClient } from ${JSON.stringify(modulePath)};
import type { ThreadRef, ThreadListQuery, ThreadCopy, ThreadInventory, ThreadCopyPage } from ${JSON.stringify(modulePath)};
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
