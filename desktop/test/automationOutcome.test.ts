import assert from "node:assert/strict";
import test from "node:test";
import { automationOutcomeIdentity, automationOutcomeStatus } from "../src/automationOutcome.ts";

const firstRuleId = "11111111-1111-4111-8111-111111111111";
const secondRuleId = "22222222-2222-4222-8222-222222222222";

test("outcome identities distinguish multiple fires on one attachment", () => {
  const first = `${automationOutcomeIdentity(firstRuleId, 1)}: ${automationOutcomeStatus("completed")}`;
  const second = `${automationOutcomeIdentity(secondRuleId, 2)}: ${automationOutcomeStatus("completed")}`;
  assert.notEqual(first, second);
  assert.equal(first, `Automation rule ${firstRuleId} (version 1): Automation completed.`);
  assert.equal(second, `Automation rule ${secondRuleId} (version 2): Automation completed.`);
});

test("outcome identity rejects malformed metadata without echoing it", () => {
  for (const ruleId of [null, "", "<img src=x>", "A".repeat(200), "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".toUpperCase()]) {
    assert.equal(automationOutcomeIdentity(ruleId, 1), "Automation (rule identity unavailable)");
  }
  for (const version of [null, 0, -1, 1.5, "2", Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(
      automationOutcomeIdentity(firstRuleId, version),
      `Automation rule ${firstRuleId} (version unavailable)`,
    );
  }
});

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
