import assert from "node:assert/strict";
import test from "node:test";
import {
  InboxRemovalClient, inboxRemovalRecordStatus,
  type InboxClearResult, type InboxRemovalResult, type RemovalInvoke, type SavedRemoval,
} from "../src/inboxRemoval.ts";

const receipt: InboxClearResult = {
  request_id: "signed-key", intent_ids: [], outcome: "completed",
  requested: 0, completed: 0, pending: 0, deleted: 0, message: null,
};

test("new Clear saves via native prepare before dispatching its key", async () => {
  const calls: Array<[string, Record<string, unknown> | undefined]> = [];
  const invoke: RemovalInvoke = async <T>(command: string, args?: Record<string, unknown>) => {
    calls.push([command, args]);
    return (command === "inbox_removal_prepare" ? { request_id: "signed-key" } : receipt) as T;
  };
  const client = new InboxRemovalClient(invoke);
  assert.deepEqual(await client.clear(), receipt);
  assert.deepEqual(calls, [
    ["inbox_removal_prepare", { kind: "clear", messageId: null }],
    ["inbox_clear", { requestId: "signed-key" }],
  ]);
});

const savedClear: SavedRemoval = { request_id: "signed-key", kind: "clear", message_id: null };
const lookupArgs = { requestId: "signed-key", kind: "clear", messageId: null };
const codeOf = (error: unknown): string | null => (error as { code?: string })?.code ?? null;
const messageOf = (error: unknown): string => (error as { message: string }).message;
const notFound = { code: "not_found", message: "Request not found" };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

test("Delete preserves its literal parameter across prepare and mutation", async () => {
  const calls: Array<[string, Record<string, unknown> | undefined]> = [];
  const messageId = " alias id\t";
  const invoke: RemovalInvoke = async <T>(command: string, args?: Record<string, unknown>) => {
    calls.push([command, args]);
    return (command === "inbox_removal_prepare" ? { request_id: "signed-key" } : receipt) as T;
  };
  const client = new InboxRemovalClient(invoke);
  assert.deepEqual(await client.delete(messageId), receipt);
  assert.deepEqual(calls, [
    ["inbox_removal_prepare", { kind: "delete", messageId }],
    ["inbox_delete", { requestId: "signed-key", messageId }],
  ]);
});

test("lost committed reply and restart recover only status with the original key", async () => {
  const calls: Array<[string, Record<string, unknown> | undefined]> = [];
  const pending: InboxRemovalResult = {
    ...receipt, outcome: "deferred", requested: 2, completed: 1, pending: 1,
    intent_ids: ["held", "done"], message: "Deletion pending until the local upgrade completes",
  };
  const invoke: RemovalInvoke = async <T>(command: string, args?: Record<string, unknown>) => {
    calls.push([command, args]);
    if (command === "inbox_removal_prepare") return { request_id: "signed-key" } as T;
    if (command === "inbox_clear") throw { code: "runtime_error", message: "Lost reply" };
    if (command === "inbox_removal_saved") return [savedClear] as T;
    assert.equal(command, "inbox_removal_status");
    return pending as T;
  };
  const first = new InboxRemovalClient(invoke, () => {}, codeOf);
  assert.deepEqual(await first.clear(), pending);
  assert.deepEqual(calls, [
    ["inbox_removal_prepare", { kind: "clear", messageId: null }],
    ["inbox_clear", { requestId: "signed-key" }],
    ["inbox_removal_status", lookupArgs],
  ]);
  calls.length = 0;
  const restarted = new InboxRemovalClient(invoke, () => {}, codeOf);
  await restarted.restore();
  assert.deepEqual(calls, [["inbox_removal_saved", undefined], ["inbox_removal_status", lookupArgs]]);
  assert.deepEqual(restarted.statuses(), [{ request: savedClear, state: "found", receipt: pending }]);
  assert.deepEqual(inboxRemovalRecordStatus(restarted.statuses()[0], messageOf), {
    text: "1 completed; 1 pending. Deletion pending until the local upgrade completes", kind: "pending",
  });
});

test("unknown Clear shows the approved note and only a new explicit Clear prepares a new key", async () => {
  const calls: string[] = [];
  let sequence = 0;
  const invoke: RemovalInvoke = async <T>(command: string, args?: Record<string, unknown>) => {
    calls.push(command);
    if (command === "inbox_removal_prepare") return { request_id: `key-${++sequence}` } as T;
    if (command === "inbox_removal_status") throw notFound;
    assert.equal(command, "inbox_clear");
    if (args?.requestId === "key-1") throw new Error("Lost before commit");
    assert.equal(args?.requestId, "key-2");
    return { ...receipt, request_id: "key-2" } as T;
  };
  const client = new InboxRemovalClient(invoke, () => {}, codeOf);
  assert.equal(await client.clear(), undefined);
  assert.deepEqual(calls, ["inbox_removal_prepare", "inbox_clear", "inbox_removal_status"]);
  assert.deepEqual(inboxRemovalRecordStatus(client.statuses()[0], messageOf), {
    text: "Your last Clear didn't go through; nothing was deleted.", kind: "error",
  });
  await client.clear();
  assert.deepEqual(calls, [
    "inbox_removal_prepare", "inbox_clear", "inbox_removal_status", "inbox_removal_prepare", "inbox_clear",
  ]);
  assert.deepEqual(client.statuses().map(record => [record.request.request_id, record.state]), [["key-2", "found"]]);
});

for (const code of ["busy", "invalid_request", "runtime_error", "not_found_extra"]) {
  test(`${code} during status preserves the error without a missing note or resubmit`, async () => {
    const error = { code, message: `Existing ${code} message` };
    const calls: string[] = [];
    const invoke: RemovalInvoke = async <T>(command: string) => {
      calls.push(command);
      if (command === "inbox_removal_saved") return [savedClear] as T;
      assert.equal(command, "inbox_removal_status");
      throw error;
    };
    const client = new InboxRemovalClient(invoke, () => {}, codeOf);
    await client.restore();
    assert.deepEqual(calls, ["inbox_removal_saved", "inbox_removal_status"]);
    assert.deepEqual(client.statuses(), [{ request: savedClear, state: "error", error }]);
    assert.deepEqual(inboxRemovalRecordStatus(client.statuses()[0], messageOf), { text: error.message, kind: "error" });
  });
}

test("failed preparation never dispatches or invents a receipt, and does not poison the queue", async () => {
  let failed = false;
  const calls: string[] = [];
  const error = new Error("Journal unavailable");
  const invoke: RemovalInvoke = async <T>(command: string) => {
    calls.push(command);
    if (!failed) { failed = true; throw error; }
    assert.equal(command, "inbox_removal_saved");
    return [] as T;
  };
  const client = new InboxRemovalClient(invoke);
  await assert.rejects(client.clear(), error);
  assert.deepEqual(client.statuses(), []);
  await client.restore();
  assert.deepEqual(calls, ["inbox_removal_prepare", "inbox_removal_saved"]);
});

test("two Clear receipts stay separate with only backend counts, even when an earlier one is pending", async () => {
  let sequence = 0;
  const invoke: RemovalInvoke = async <T>(command: string) => {
    if (command === "inbox_removal_prepare") return { request_id: `key-${++sequence}` } as T;
    assert.equal(command, "inbox_clear");
    return {
      ...receipt, request_id: `key-${sequence}`, requested: sequence, completed: 0,
      pending: sequence, outcome: "deferred", intent_ids: [`intent-${sequence}`],
    } as T;
  };
  const client = new InboxRemovalClient(invoke);
  await client.clear();
  await client.clear();
  assert.deepEqual(client.statuses().map(record => record.state === "found" && [
    record.receipt.request_id, record.receipt.requested, record.receipt.completed, record.receipt.pending,
  ]), [["key-1", 1, 0, 1], ["key-2", 2, 0, 2]]);
});

test("recovery cannot inspect the client's prepared key before its sole dispatch", async () => {
  const prepared = deferred<{ request_id: string }>();
  const preparing = deferred<void>();
  const calls: string[] = [];
  const invoke: RemovalInvoke = async <T>(command: string) => {
    calls.push(command);
    if (command === "inbox_removal_prepare") { preparing.resolve(); return await prepared.promise as T; }
    if (command === "inbox_removal_saved") return [savedClear] as T;
    return receipt as T;
  };
  const client = new InboxRemovalClient(invoke);
  const clearing = client.clear();
  await preparing.promise;
  const restoring = client.restore();
  assert.equal(restoring, client.restore());
  await Promise.resolve();
  assert.deepEqual(calls, ["inbox_removal_prepare"]);
  prepared.resolve({ request_id: "signed-key" });
  await Promise.all([clearing, restoring]);
  assert.deepEqual(calls, ["inbox_removal_prepare", "inbox_clear", "inbox_removal_saved", "inbox_removal_status"]);
});

test("a renderer failure is not classified as a lost reply or used to repeat an RPC", async () => {
  const calls: string[] = [];
  let renders = 0;
  const rendererError = new Error("Renderer failed");
  const invoke: RemovalInvoke = async <T>(command: string) => {
    calls.push(command);
    return (command === "inbox_removal_prepare" ? { request_id: "signed-key" } : receipt) as T;
  };
  const client = new InboxRemovalClient(invoke, () => { if (++renders === 2) throw rendererError; });
  await assert.rejects(client.clear(), rendererError);
  assert.deepEqual(calls, ["inbox_removal_prepare", "inbox_clear"]);
});

test("an older missing Clear recovered after a fresh Clear cannot describe the fresh one as lost", async () => {
  const listing = deferred<SavedRemoval[]>();
  const listed = deferred<void>();
  const calls: string[] = [];
  const invoke: RemovalInvoke = async <T>(command: string) => {
    calls.push(command);
    if (command === "inbox_removal_saved") { listed.resolve(); return await listing.promise as T; }
    if (command === "inbox_removal_prepare") return { request_id: "new-key" } as T;
    if (command === "inbox_removal_status") throw notFound;
    assert.equal(command, "inbox_clear");
    return { ...receipt, request_id: "new-key" } as T;
  };
  const client = new InboxRemovalClient(invoke, () => {}, codeOf);
  const restoring = client.restore();
  await listed.promise;
  const clearing = client.clear();
  listing.resolve([savedClear]);
  await Promise.all([restoring, clearing]);
  assert.deepEqual(calls, ["inbox_removal_saved", "inbox_removal_prepare", "inbox_clear", "inbox_removal_status"]);
  assert.deepEqual(client.statuses().map(record => [record.request.request_id, record.state]), [["new-key", "found"]]);
});

test("restart reports a missing latest Clear without hiding an older pending receipt", async () => {
  const earlier = { ...savedClear, request_id: "older-pending" };
  const calls: string[] = [];
  const pending: InboxRemovalResult = {
    ...receipt, request_id: earlier.request_id, outcome: "deferred", requested: 1,
    completed: 0, pending: 1, intent_ids: ["earlier-intent"],
  };
  const invoke: RemovalInvoke = async <T>(command: string, args?: Record<string, unknown>) => {
    calls.push(command);
    if (command === "inbox_removal_saved") return [earlier, savedClear] as T;
    assert.equal(command, "inbox_removal_status");
    if (args?.requestId === earlier.request_id) return pending as T;
    throw notFound;
  };
  const client = new InboxRemovalClient(invoke, () => {}, codeOf);
  await client.restore();
  assert.deepEqual(client.statuses(), [
    { request: earlier, state: "found", receipt: pending },
    { request: savedClear, state: "missing", error: notFound },
  ]);
  assert.deepEqual(calls, ["inbox_removal_saved", "inbox_removal_status", "inbox_removal_status"]);
  assert.equal(inboxRemovalRecordStatus(client.statuses()[1], messageOf).text,
    "Your last Clear didn't go through; nothing was deleted.");
});

test("missing Delete preserves its existing error rather than using the Clear note", async () => {
  const saved: SavedRemoval = { request_id: "delete-key", kind: "delete", message_id: "literal id" };
  const invoke: RemovalInvoke = async <T>(command: string, args?: Record<string, unknown>) => {
    if (command === "inbox_removal_saved") return [saved] as T;
    assert.equal(command, "inbox_removal_status");
    assert.deepEqual(args, { requestId: saved.request_id, kind: "delete", messageId: saved.message_id });
    throw notFound;
  };
  const client = new InboxRemovalClient(invoke, () => {}, codeOf);
  await client.restore();
  assert.deepEqual(client.statuses(), [{ request: saved, state: "missing", error: notFound }]);
  assert.deepEqual(inboxRemovalRecordStatus(client.statuses()[0], messageOf), { text: notFound.message, kind: "error" });
});
