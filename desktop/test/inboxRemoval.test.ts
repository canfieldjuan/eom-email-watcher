import assert from "node:assert/strict";
import test from "node:test";

const message = "Can't delete this yet, the local upgrade is still finishing; try again later";

test("a partial clear keeps held messages and displays the engine refusal", async () => {
  const owner = await import("../src/inboxRemoval.ts").catch(() => ({}));
  assert.equal(typeof owner.inboxClearView, "function", "clear must project refused units");
  const result = owner.inboxClearView([{ message_id: "held" }, { message_id: "deleted" }], {
    deleted: 1, refused: 1, held_message_ids: ["held"], outcome: "partial", message,
  });
  assert.deepEqual(result.retained, [{ message_id: "held" }]);
  assert.equal(result.kind, "error");
  assert.match(result.text, /Deleted 1 local message/);
  assert.ok(result.text.includes(message));
  assert.ok(!result.text.includes("Cleared"));
});

test("a refused clear changes no displayed held message and completed clear keeps its wording", async () => {
  const { inboxClearView } = await import("../src/inboxRemoval.ts").catch(() => ({}));
  assert.equal(typeof inboxClearView, "function");
  const items = [{ message_id: "held" }];
  const refused = inboxClearView(items, {
    deleted: 0, refused: 1, held_message_ids: ["held"], outcome: "refused", message,
  });
  assert.deepEqual(refused.retained, items);
  assert.equal(refused.text, message);
  const completed = inboxClearView(items, {
    deleted: 1, refused: 0, held_message_ids: [], outcome: "completed", message: null,
  });
  assert.deepEqual(completed.retained, []);
  assert.equal(completed.kind, "success");
  assert.equal(completed.text, "Cleared 1 local message. Source email was not changed.");
});
