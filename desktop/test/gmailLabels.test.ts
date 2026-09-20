import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");

test("Gmail label settings use typed backend operations and no free-form label input", () => {
  assert.match(source, /invoke<GmailLabelSelectors>\("gmail_label_selectors_list"/);
  assert.match(source, /invoke<GmailLabelCatalog>\("gmail_labels_catalog"/);
  assert.match(source, /invoke<GmailLabelSelectorAdded>\("gmail_label_selector_add"/);
  assert.match(source, /invoke<GmailLabelSelectorRemoved>\("gmail_label_selector_remove"/);
  assert.match(source, /<select id="gmail-label-catalog"/);
  assert.doesNotMatch(source, /<input[^>]+id="gmail-label-(?:id|name|provider)"/);
});

test("active account generation invalidates late Gmail label responses", () => {
  assert.match(source, /gmailLabelGeneration \+= 1/);
  assert.match(source, /generation === gmailLabelGeneration/);
  assert.match(source, /response\.provider === scope\.provider/);
  assert.match(source, /response\.account_id === scope\.account_id/);
  assert.match(source, /invalidateGmailLabelState\(null\);[\s\S]*reconnectMailAccount/);
});

test("Gmail label UI explains bounded catch-up and renders admission provenance", () => {
  assert.match(
    source,
    /Applies on the next scheduled check and may include recent matching mail\. Adding a label does not start a full mailbox scan\./,
  );
  assert.match(source, /Admitted by Gmail label:/);
  assert.match(source, /Admitted by watched sender:/);
  assert.match(source, /item\.admission !== null/);
});

test("mutations are bound to rendered catalog rows and current revisions", () => {
  assert.match(
    source,
    /gmailLabelCatalogItems\.find\([\s\S]*item\.label_id === gmailLabelCatalogSelect\.value/,
  );
  assert.match(source, /expectedRevision: revision/);
  assert.match(source, /gmailLabelSelectorItems\.some\([\s\S]*selector\.selector_id/);
  assert.match(source, /catalog\.revision !== selectors\.revision/);
});

test("check status keeps Gmail recovery visible until catch-up finishes", () => {
  assert.match(source, /recovery_pending\?: boolean/);
  assert.match(source, /recovery_state\?: string/);
  assert.match(source, /recovery_failure_code\?: string/);
  assert.match(source, /recovery_next_retry_at\?: string/);
  assert.match(
    source,
    /function recoveryStatusMessage[\s\S]*Gmail catch-up[\s\S]*will retry/,
  );

  const recoveryFunctionSource = source.match(
    /function recoveryStatusMessage\([\s\S]*?\): string \| null \{([\s\S]*?)\n\}\n\nfunction checkResultMessage/,
  );
  assert.ok(recoveryFunctionSource);
  const functionSource = source.match(
    /function checkResultMessage\(result: CheckResult\): string \{([\s\S]*?)\n\}\n\nasync function runCheck/,
  );
  assert.ok(functionSource);
  const checkResultMessage = Function(
    `function recoveryStatusMessage(result, progress) {${recoveryFunctionSource[1]}\n}
     return function checkResultMessage(result) {${functionSource[1]}\n}`,
  )() as (result: Record<string, unknown>) => string;
  const baseline = {
    active: true,
    discovered: 2,
    summarized: 1,
    fallback_notified: 0,
    purged: 0,
    stale_cursor_recovered: true,
    pending_notifications: 0,
    delivered_notifications: 0,
    failed_notifications: 0,
    remaining_notifications: 0,
  };

  const collecting = checkResultMessage({
    ...baseline,
    recovery_pending: true,
    recovery_state: "collecting",
  });
  assert.match(collecting, /Gmail catch-up is still in progress/);
  assert.doesNotMatch(collecting, /Check complete/);

  for (const recovery_state of ["backoff", "degraded"]) {
    const retrying = checkResultMessage({
      ...baseline,
      recovery_pending: true,
      recovery_state,
      recovery_failure_code: "gmail_recovery_page_token_invalid",
      recovery_next_retry_at: "2026-09-20T03:00:00+00:00",
    });
    assert.match(retrying, /will retry after 2026-09-20T03:00:00\+00:00/);
    assert.match(retrying, /gmail_recovery_page_token_invalid/);
    assert.doesNotMatch(retrying, /Check complete/);
  }
});

test("scheduled checks render recovery before any completion status", () => {
  assert.match(
    source,
    /status: "complete" \| "delivery_failed" \| "check_failed" \| "recovery_pending"/,
  );
  const listener = source.match(
    /void listen<ScheduledCheckEvent>\("watcher:\/\/scheduled-check", \(event\) => \{([\s\S]*)\n\}\);\nvoid listen<\{ attempted: number \}>\("watcher:\/\/connect-queue"/,
  );
  assert.ok(listener);
  const recoveryBranch = listener[1].indexOf("recoveryStatusMessage({");
  const completionBranch = listener[1].indexOf('event.payload.status === "complete"');
  assert.ok(recoveryBranch >= 0);
  assert.ok(completionBranch >= 0);
  assert.ok(recoveryBranch < completionBranch);
});
