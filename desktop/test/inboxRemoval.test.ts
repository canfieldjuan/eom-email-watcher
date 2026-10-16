import assert from "node:assert/strict";
import test from "node:test";

test("completed clear retains message counts and source-email wording", async () => {
  const { inboxClearView } = await import("../src/inboxRemoval.ts");
  for (const completed of [0, 1, 2]) {
    const result = inboxClearView([{ message_id: "removed" }], {
      request_id: "saved-signed-key", intent_ids: completed ? ["saved-intent"] : [],
      deleted: completed, requested: completed, completed, pending: 0,
      outcome: "completed", message: null,
    });
    assert.deepEqual(result.retained, []);
    assert.equal(result.kind, "success");
    assert.equal(result.text, `Cleared ${completed} local message${completed === 1 ? "" : "s"}. Source email was not changed.`);
  }
});

for (const completed of [0, 1]) {
  test(`deferred clear hides every accepted target and reports ${completed} completed separately`, async () => {
    const { inboxClearView } = await import("../src/inboxRemoval.ts");
    const result = inboxClearView([{ message_id: "held" }, { message_id: "deleted" }], {
      request_id: "saved-signed-key", intent_ids: ["saved-intent"],
      deleted: completed, requested: 2, completed, pending: 2 - completed,
      outcome: "deferred", message: "Deletion pending until the local upgrade completes",
    });
    assert.deepEqual(result.retained, []);
    assert.equal(result.kind, "pending");
    assert.match(result.text, new RegExp(`${completed} completed`));
    assert.match(result.text, new RegExp(`${2 - completed} pending`));
    assert.ok(result.text.includes("Deletion pending until the local upgrade completes"));
    assert.ok(!result.text.includes("refused"));
    assert.ok(!result.text.includes("Cleared"));
  });
}
