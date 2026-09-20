import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { webcrypto } from "node:crypto";
import test from "node:test";

const source = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

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

test("Gmail label proof surface shows safe selector evidence without the opaque label ID", async () => {
  assert.match(source, /id="gmail-label-polling-state"/);
  assert.match(source, /Selector UUID:/);
  assert.match(source, /Label ID SHA-256:/);
  assert.match(source, /Selector-set revision:/);
  assert.match(source, /crypto\.subtle\.digest\("SHA-256"/);
  assert.doesNotMatch(
    source,
    /(?:textContent|innerText|innerHTML)\s*=\s*[^;\n]*selector\.label_id/,
  );

  const digestFunctionSource = source.match(
    /async function gmailLabelIdDigest\([^)]*\): Promise<string> \{([\s\S]*?)\n\}/,
  );
  assert.ok(digestFunctionSource);
  const digestLabelId = Function(
    "crypto",
    "TextEncoder",
    `return async function gmailLabelIdDigest(labelId) {${digestFunctionSource[1]}\n}`,
  )(webcrypto, TextEncoder) as (labelId: string) => Promise<string>;
  const rawLabelId = "Label_private-proof-123";
  const digest = await digestLabelId(rawLabelId);
  assert.equal(digest, "cd467470d08281da13ee3acd5fe743544fdf2e92e3de8b125c4dcdab4402dbd9");
  assert.doesNotMatch(digest, /Label_private-proof-123/);
});

test("Gmail proof evidence spans the section and wraps without changing sender-card truncation", () => {
  assert.match(source, /item\.className = "sender-card gmail-label-selector-card"/);
  assert.match(source, /className = "gmail-label-evidence"/);
  assert.match(
    styles,
    /#gmail-label-settings\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/s,
  );
  assert.match(
    styles,
    /\.gmail-label-selector-card \.gmail-label-evidence\s*\{[^}]*overflow:\s*visible[^}]*text-overflow:\s*clip[^}]*white-space:\s*normal[^}]*overflow-wrap:\s*anywhere/s,
  );
  assert.match(
    styles,
    /\.sender-card strong,\s*\.sender-card span\s*\{[^}]*text-overflow:\s*ellipsis[^}]*white-space:\s*nowrap/s,
  );
});

test("local sender observations stay authoritative across stale health responses", () => {
  assert.match(
    source,
    /const exactSenderCount = gmailLabelSenderCount\.count \?\? health\.watchlist_count/,
  );
  assert.match(source, /watchlist_count: exactSenderCount/);
  assert.match(source, /watchlistCount\.textContent = String\(exactSenderCount\)/);
  assert.doesNotMatch(source, /watchlistCount\.textContent = String\(health\.watchlist_count\)/);
  const reducerSource = source.match(
    /function gmailLabelSenderCountAfterObservation\([^)]*\): GmailLabelSenderCountState \{([\s\S]*?)\n\}\n\nfunction gmailLabelPollingStateMessage/,
  );
  assert.ok(reducerSource);
  const observe = Function(
    `return function gmailLabelSenderCountAfterObservation(current, incomingCount, source) {${reducerSource[1]}\n}`,
  )() as (
    current: { count: number | null; local_authoritative: boolean },
    incomingCount: number,
    source: "health" | "local",
  ) => { count: number | null; local_authoritative: boolean };

  let healthFirst = observe({ count: null, local_authoritative: false }, 0, "health");
  assert.deepEqual(healthFirst, { count: 0, local_authoritative: false });
  healthFirst = observe(healthFirst, 0, "local");
  healthFirst = observe(healthFirst, 1, "local");
  assert.deepEqual(observe(healthFirst, 0, "health"), {
    count: 1,
    local_authoritative: true,
  });

  let listFirst = observe({ count: null, local_authoritative: false }, 1, "local");
  listFirst = observe(listFirst, 0, "health");
  assert.deepEqual(listFirst, { count: 1, local_authoritative: true });

  let removed = observe({ count: 1, local_authoritative: true }, 0, "local");
  removed = observe(removed, 1, "health");
  assert.deepEqual(removed, { count: 0, local_authoritative: true });
});

test("label-only polling claim requires active selector, zero senders, and running scheduler", () => {
  const stateFunctionSource = source.match(
    /function gmailLabelPollingStateMessage\([^)]*\): GmailLabelPollingState \{([\s\S]*?)\n\}/,
  );
  assert.ok(stateFunctionSource);
  const pollingState = Function(
    `return function gmailLabelPollingStateMessage(activeSelectorCount, health) {${stateFunctionSource[1]}\n}`,
  )() as (
    activeSelectorCount: number | null,
    health: Record<string, unknown> | null,
  ) => { active: boolean; message: string };

  const active = pollingState(1, {
    watchlist_count: 0,
    polling: { enabled: true, interval_minutes: 120, next_check_unix_ms: 1_800_000_000_000 },
  });
  assert.equal(active.active, true);
  assert.match(active.message, /Label-only automatic polling is active/);
  assert.match(active.message, /zero exact senders/);
  assert.match(active.message, /scheduler is enabled and running/);

  for (const [activeSelectorCount, health, reason] of [
    [0, { watchlist_count: 0, polling: { enabled: true, next_check_unix_ms: 1 } }, /no active Gmail label selector/],
    [1, { watchlist_count: 1, polling: { enabled: true, next_check_unix_ms: 1 } }, /exact sender count is 1/],
    [1, { watchlist_count: 0, polling: { enabled: false, next_check_unix_ms: null } }, /scheduler is disabled/],
    [1, { watchlist_count: 0, polling: { enabled: true, next_check_unix_ms: null } }, /no next check is scheduled/],
    [null, { watchlist_count: 0, polling: { enabled: true, next_check_unix_ms: 1 } }, /status is unavailable/],
    [1, null, /status is unavailable/],
  ] as const) {
    const state = pollingState(activeSelectorCount, health);
    assert.equal(state.active, false);
    assert.match(state.message, reason);
    assert.doesNotMatch(state.message, /Label-only automatic polling is active/);
  }
});
