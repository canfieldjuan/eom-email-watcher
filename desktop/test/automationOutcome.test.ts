import assert from "node:assert/strict";
import test from "node:test";
import { automationOutcomeStatus } from "../src/automationOutcome.ts";

test("automation outcome text distinguishes the projected lifecycle without premature completion", () => {
  assert.equal(automationOutcomeStatus("pending_dispatch"), "Automation queued for dispatch.");
  assert.equal(automationOutcomeStatus("entitlement_paused"), "Automation paused.");
  assert.equal(automationOutcomeStatus("awaiting_confirmation"), "Automation awaiting confirmation.");
  assert.equal(automationOutcomeStatus("submitted"), "Automation submitted. Outcome pending.");
  assert.equal(automationOutcomeStatus("completed"), "Automation completed.");
  assert.equal(automationOutcomeStatus("failed"), "Automation failed.");
  assert.equal(automationOutcomeStatus("declined"), "Automation declined. No action was submitted.");
  assert.equal(automationOutcomeStatus("manual_review"), "Automation needs manual review.");
  assert.equal(automationOutcomeStatus("source_unavailable"), "Automation source unavailable.");
});

test("unknown and malformed states use generic text without interpolating input", () => {
  for (const state of [undefined, null, "", "provider secret", 0, false, {}, ["completed"]]) {
    assert.equal(automationOutcomeStatus(state), "Automation status unavailable.");
  }
});
