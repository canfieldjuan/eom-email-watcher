import assert from "node:assert/strict";
import test from "node:test";

const now = Date.parse("2026-10-03T12:00:00Z");
const labels = ["Needs your confirmation", "Action failed", "Needs review", "Action paused", "Action running", "Completed"];
const fireStates = [
  ["awaiting_confirmation"], ["failed"], ["manual_review", "source_unavailable"],
  ["entitlement_paused"], ["pending_dispatch", "submitted"], ["completed"],
];

test("every projected fire state maps to authored chip text", async () => {
  const { inboxActionState } = await import("../src/inboxActionState.ts").catch(() => ({}));
  assert.equal(typeof inboxActionState, "function", "inboxActionState export is required");
  fireStates.forEach((states, index) => states.forEach((state) => {
    assert.equal(inboxActionState([state], [], null, now), labels[index], state);
  }));
  assert.equal(inboxActionState(["declined"], [], null, now), null);
});

test("every Connect status maps to authored chip text", async () => {
  const { inboxActionState } = await import("../src/inboxActionState.ts").catch(() => ({}));
  assert.equal(typeof inboxActionState, "function", "inboxActionState export is required");
  for (const status of ["requested", "accepted", "processing", "completed", "failed"]) {
    const expected = status === "failed" ? "Action failed" : status === "completed" ? "Completed" : "Action running";
    assert.equal(inboxActionState([], [status], null, now), expected, status);
  }
});

test("every accepted proposal state maps independently of provider text", async () => {
  const { inboxActionState } = await import("../src/inboxActionState.ts").catch(() => ({}));
  assert.equal(typeof inboxActionState, "function", "inboxActionState export is required");
  const states = [["awaiting_confirmation"], ["failed"], ["manual_review", "unresolved"], [],
    ["write_authorized", "writing", "reconciling"], ["completed"]];
  states.forEach((values, index) => values.forEach((state) => {
    assert.equal(inboxActionState([], [], { state, status: "accepted", expires_at: null }, now), labels[index]);
  }));
  assert.equal(inboxActionState([], [], { state: "declined", status: "accepted" }, now), null);
});

test("chip precedence holds for mixed sources in either order", async () => {
  const { inboxActionState } = await import("../src/inboxActionState.ts").catch(() => ({}));
  assert.equal(typeof inboxActionState, "function", "inboxActionState export is required");
  for (let index = 0; index < labels.length; index++) {
    const states = fireStates.slice(index).flat();
    for (const values of [states, [...states].reverse()]) {
      assert.equal(inboxActionState(values, ["completed"], null, now), labels[index]);
    }
  }
  assert.equal(inboxActionState(["completed"], ["failed"], { state: "awaiting_confirmation", status: "accepted" }, now), labels[0]);
  assert.equal(inboxActionState(["manual_review"], ["failed"], null, now), labels[1]);
});

test("proposal expiry uses the render clock and exact expiry boundary", async () => {
  const { inboxActionState } = await import("../src/inboxActionState.ts").catch(() => ({}));
  assert.equal(typeof inboxActionState, "function", "inboxActionState export is required");
  const proposal = { state: "awaiting_confirmation", status: "accepted", expires_at: new Date(now).toISOString() };
  assert.equal(inboxActionState([], [], proposal, now - 1), labels[0]);
  assert.equal(inboxActionState([], [], proposal, now), null);
  assert.equal(inboxActionState([], [], proposal, now + 1), null);
  assert.equal(inboxActionState([], [], { ...proposal, expires_at: "invalid" }, now), labels[0]);
  assert.equal(inboxActionState([], [], { ...proposal, state: "failed" }, now), labels[1]);
});

test("proposal status is closed and no suggestions needs review", async () => {
  const { inboxActionState } = await import("../src/inboxActionState.ts").catch(() => ({}));
  assert.equal(typeof inboxActionState, "function", "inboxActionState export is required");
  assert.equal(inboxActionState([], [], { state: "manual_review", status: "no_suggestions" }, now), labels[2]);
  for (const status of [undefined, null, "unknown", false, {}, ["accepted"]]) {
    assert.equal(inboxActionState([], [], { state: "awaiting_confirmation", status }, now), null);
  }
});

test("unknown and malformed values contribute nothing, even in mixed input", async () => {
  const { inboxActionState } = await import("../src/inboxActionState.ts").catch(() => ({}));
  assert.equal(typeof inboxActionState, "function", "inboxActionState export is required");
  for (const value of [undefined, null, "", 0, false, {}, ["completed"], "toString", "provider secret"]) {
    assert.equal(inboxActionState([value], [value], value, now), null);
    assert.equal(inboxActionState([], [], { state: value, status: "accepted" }, now), null);
    assert.equal(inboxActionState([value, "completed"], [value], value, now), labels[5]);
  }
});
