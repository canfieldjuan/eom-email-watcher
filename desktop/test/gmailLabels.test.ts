import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { webcrypto } from "node:crypto";
import test from "node:test";

const source = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

function gmailLabelLoadHarness(
  invoke: (operation: string) => Promise<Record<string, unknown>>,
) {
  const match = source.match(
    /async function loadGmailLabelState\(\): Promise<void> \{([\s\S]*?)\n\}\n\nasync function addGmailLabelSelector/,
  );
  assert.ok(match);
  const body = match[1]
    .replace(/invoke<[^>]+>/g, "invoke")
    .replace(/: GmailLabelSelectors \| null/g, "")
    .replace(/: GmailLabelSelector\[\]/g, "");
  return Function(
    "invoke",
    `let gmailLabelScope = { provider: "gmail", account_id: "gmail-default" };
     let gmailLabelGeneration = 1;
     let gmailLabelLoadSequence = 0;
     let gmailLabelCatalogVerified = false;
     let gmailLabelRevision = null;
     const gmailLabelStatus = { textContent: "", dataset: {} };
     const selectorRenders = [];
     function renderGmailLabelSelectors(items) { selectorRenders.push(items); }
     function renderGmailLabelCatalog(_items) {}
     function renderGmailLabelPollingState() {}
     function refreshGmailLabelControls() {}
     function errorMessage(error) { return String(error); }
     function gmailLabelScopeMatches(response, scope, generation) {
       return generation === gmailLabelGeneration &&
         gmailLabelScope?.provider === scope.provider &&
         gmailLabelScope.account_id === scope.account_id &&
         response.provider === scope.provider &&
         response.account_id === scope.account_id;
     }
     function gmailLabelRefreshIsCurrent(initial, catalog, revalidated) {
       return initial.revision === catalog.revision &&
         catalog.revision === revalidated.revision &&
         revalidated.catalog_state === "current";
     }
     async function loadGmailLabelState() {${body}\n}
     return {
       load: loadGmailLabelState,
       bumpGeneration: () => { gmailLabelGeneration += 1; },
       snapshot: () => ({
         selectorRenders: selectorRenders.map((items) => structuredClone(items)),
         revision: gmailLabelRevision,
         catalogVerified: gmailLabelCatalogVerified,
       }),
     };`,
  )(invoke) as {
    load: () => Promise<void>;
    bumpGeneration: () => void;
    snapshot: () => {
      selectorRenders: Array<Array<Record<string, unknown>>>;
      revision: number | null;
      catalogVerified: boolean;
    };
  };
}

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
  assert.match(source, /gmailLabelRefreshIsCurrent\(selectors, catalog, revalidatedSelectors\)/);
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
    /function recoveryStatusMessage\([\s\S]*?\): string \| null \{([\s\S]*?)\n\}\n\nfunction inactiveCheckMessage/,
  );
  assert.ok(recoveryFunctionSource);
  const inactiveFunctionSource = source.match(
    /function inactiveCheckMessage\([^)]*\): string \| null \{([\s\S]*?)\n\}/,
  );
  assert.ok(inactiveFunctionSource);
  const functionSource = source.match(
    /function checkResultMessage\(result: CheckResult\): string \{([\s\S]*?)\n\}\n\nasync function runCheck/,
  );
  assert.ok(functionSource);
  const checkResultMessage = Function(
    `function recoveryStatusMessage(result, progress) {${recoveryFunctionSource[1]}\n}
     function inactiveCheckMessage(reason) {${inactiveFunctionSource[1]}\n}
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

test("manual checks explain inactive saved Gmail labels and fail closed on unknown reasons", () => {
  const inactiveFunctionSource = source.match(
    /function inactiveCheckMessage\([^)]*\): string \| null \{([\s\S]*?)\n\}/,
  );
  assert.ok(inactiveFunctionSource);
  const inactiveCheckMessage = Function(
    `return function inactiveCheckMessage(reason) {${inactiveFunctionSource[1]}\n}`,
  )() as (reason: string | undefined) => string | null;

  assert.match(
    inactiveCheckMessage("gmail_label_selectors_inactive") ?? "",
    /Saved Gmail labels are inactive[\s\S]*refresh[\s\S]*select/i,
  );
  assert.match(
    inactiveCheckMessage("future_inactive_reason") ?? "",
    /could not understand the inactive check state/i,
  );
  assert.equal(inactiveCheckMessage(undefined), null);

  const recoveryBranch = source.indexOf("if (result.recovery_pending)", source.indexOf("function checkResultMessage"));
  const inactiveBranch = source.indexOf("inactiveCheckMessage(result.reason)", source.indexOf("function checkResultMessage"));
  const genericInactiveBranch = source.indexOf("if (!result.active)", source.indexOf("function checkResultMessage"));
  assert.ok(recoveryBranch >= 0);
  assert.ok(inactiveBranch > recoveryBranch);
  assert.ok(genericInactiveBranch > inactiveBranch);
  assert.match(
    source.slice(recoveryBranch, genericInactiveBranch),
    /!result\.active && result\.reason !== undefined[\s\S]*inactiveCheckMessage\(result\.reason\)/,
  );
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

test("scheduled checks render inactive saved Gmail labels before completion", () => {
  assert.match(
    source,
    /status: "complete" \| "delivery_failed" \| "check_failed" \| "recovery_pending" \| "inactive"/,
  );
  const listener = source.match(
    /void listen<ScheduledCheckEvent>\("watcher:\/\/scheduled-check", \(event\) => \{([\s\S]*)\n\}\);\nvoid listen<\{ attempted: number \}>\("watcher:\/\/connect-queue"/,
  );
  assert.ok(listener);
  const recoveryBranch = listener[1].indexOf("recoveryStatusMessage({");
  const inactiveBranch = listener[1].indexOf("inactiveCheckMessage(event.payload.reason)");
  const completionBranch = listener[1].indexOf('event.payload.status === "complete"');
  assert.ok(recoveryBranch >= 0);
  assert.ok(inactiveBranch > recoveryBranch);
  assert.ok(completionBranch > inactiveBranch);
  assert.match(
    listener[1],
    /event\.payload\.status === "inactive"[\s\S]*inactiveCheckMessage\(event\.payload\.reason\)/,
  );
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

test("sender observation versions reject crossed health and accept later fresh health", () => {
  assert.match(
    source,
    /const exactSenderCount = gmailLabelSenderCount\.count \?\? health\.watchlist_count/,
  );
  assert.match(source, /watchlist_count: exactSenderCount/);
  assert.match(source, /watchlistCount\.textContent = String\(exactSenderCount\)/);
  assert.doesNotMatch(source, /watchlistCount\.textContent = String\(health\.watchlist_count\)/);
  const localReducerSource = source.match(
    /function gmailLabelSenderCountAfterLocalObservation\([^)]*\): GmailLabelSenderCountState \{([\s\S]*?)\n\}\n\nfunction gmailLabelSenderCountAfterHealth/,
  );
  assert.ok(localReducerSource);
  const observeLocal = Function(
    `return function gmailLabelSenderCountAfterLocalObservation(current, incomingCount) {${localReducerSource[1]}\n}`,
  )() as (
    current: { count: number | null; observation_version: number },
    incomingCount: number,
  ) => { count: number | null; observation_version: number };
  const healthReducerSource = source.match(
    /function gmailLabelSenderCountAfterHealth\([^)]*\): GmailLabelSenderCountState \{([\s\S]*?)\n\}\n\nfunction gmailLabelPollingStateMessage/,
  );
  assert.ok(healthReducerSource);
  const observeHealth = Function(
    `return function gmailLabelSenderCountAfterHealth(current, incomingCount, requestObservationVersion) {${healthReducerSource[1]}\n}`,
  )() as (
    current: { count: number | null; observation_version: number },
    incomingCount: number,
    requestObservationVersion: number,
  ) => { count: number | null; observation_version: number };

  const initial = { count: null, observation_version: 0 };
  const provisionalHealth = observeHealth(initial, 0, 0);
  assert.deepEqual(provisionalHealth, { count: 0, observation_version: 0 });

  const afterAdd = observeLocal(provisionalHealth, 1);
  assert.deepEqual(observeHealth(afterAdd, 0, 0), {
    count: 1,
    observation_version: 1,
  });

  const afterRemove = observeLocal(afterAdd, 0);
  assert.deepEqual(observeHealth(afterRemove, 1, 1), {
    count: 0,
    observation_version: 2,
  });

  assert.deepEqual(observeHealth(afterRemove, 3, 2), {
    count: 3,
    observation_version: 2,
  });

  assert.deepEqual(observeLocal(provisionalHealth, 2), {
    count: 2,
    observation_version: 1,
  });
});

test("health captures sender version and preserves health request generation ordering", () => {
  assert.match(
    source,
    /function renderHealth\(health: HealthStatus, senderObservationVersion: number\)/,
  );
  const loadHealthSource = source.match(
    /async function loadHealth\([\s\S]*?\): Promise<boolean> \{([\s\S]*?)\n\}\n\nfunction recoveryStatusMessage/,
  );
  assert.ok(loadHealthSource);
  const capturedVersion = loadHealthSource[1].indexOf(
    "const senderObservationVersion = gmailLabelSenderCount.observation_version",
  );
  const invokeHealth = loadHealthSource[1].indexOf('await invoke<HealthStatus>("health_get")');
  const generationGuard = loadHealthSource[1].indexOf(
    "if (requestGeneration !== healthRequestGeneration) return false",
  );
  const render = loadHealthSource[1].indexOf(
    "renderHealth(health, senderObservationVersion)",
  );
  assert.ok(capturedVersion >= 0);
  assert.ok(invokeHealth > capturedVersion);
  assert.ok(generationGuard > invokeHealth);
  assert.ok(render > generationGuard);
});

test("check readiness distinguishes unconfigured from label or recovery configured watchers", () => {
  const readinessSource = source.match(
    /function watcherPrerequisitesReady\([^)]*\): boolean \{([\s\S]*?)\n\}/,
  );
  assert.ok(readinessSource);
  const prerequisitesReady = Function(
    `return function watcherPrerequisitesReady(exactSenderCount, gmailLabelWatchConfigured, mailReady, databaseReady) {${readinessSource[1]}\n}`,
  )() as (
    exactSenderCount: number,
    gmailLabelWatchConfigured: boolean,
    mailReady: boolean,
    databaseReady: boolean,
  ) => boolean;

  assert.equal(prerequisitesReady(0, false, false, false), true);
  assert.equal(prerequisitesReady(0, true, false, true), false);
  assert.equal(prerequisitesReady(0, true, true, false), false);
  assert.equal(prerequisitesReady(0, true, true, true), true);
  assert.equal(prerequisitesReady(1, false, false, true), false);
  assert.equal(prerequisitesReady(1, false, true, true), true);
  assert.match(source, /health\.gmail\.label_watch_configured/);
});

test("catalog success revalidates selectors before declaring Gmail labels current", () => {
  const consistencySource = source.match(
    /function gmailLabelRefreshIsCurrent\([^)]*\): boolean \{([\s\S]*?)\n\}/,
  );
  assert.ok(consistencySource);
  const refreshIsCurrent = Function(
    `return function gmailLabelRefreshIsCurrent(initialSelectors, catalog, revalidatedSelectors) {${consistencySource[1]}\n}`,
  )() as (
    initialSelectors: { revision: number; catalog_state: string },
    catalog: { revision: number },
    revalidatedSelectors: { revision: number; catalog_state: string },
  ) => boolean;

  const unavailable = { revision: 7, catalog_state: "unavailable" };
  const current = { revision: 7, catalog_state: "current" };
  assert.equal(refreshIsCurrent(unavailable, { revision: 7 }, current), true);
  assert.equal(
    refreshIsCurrent(unavailable, { revision: 7 }, { revision: 7, catalog_state: "unavailable" }),
    false,
  );
  assert.equal(
    refreshIsCurrent(current, { revision: 8 }, { revision: 8, catalog_state: "current" }),
    false,
  );
  assert.equal(
    refreshIsCurrent(current, { revision: 7 }, { revision: 7, catalog_state: "invalid_catalog" }),
    false,
  );

  const loadSource = source.match(
    /async function loadGmailLabelState\(\): Promise<void> \{([\s\S]*?)\n\}\n\nasync function addGmailLabelSelector/,
  );
  assert.ok(loadSource);
  const selectorReads = [
    ...loadSource[1].matchAll(
      /invoke<GmailLabelSelectors>\(\s*"gmail_label_selectors_list"/g,
    ),
  ].map((match) => match.index ?? -1);
  const catalogRead = loadSource[1].indexOf(
    'invoke<GmailLabelCatalog>("gmail_labels_catalog"',
  );
  assert.equal(selectorReads.length, 3);
  assert.ok(selectorReads[0] < catalogRead && catalogRead < selectorReads[1]);
  assert.ok(selectorReads[2] > selectorReads[1]);
  assert.match(loadSource[1], /renderGmailLabelSelectors\(revalidatedSelectors\.items\)/);
});

test("catalog failure re-lists durable selectors and renders them inert", async () => {
  const active = {
    provider: "gmail",
    account_id: "gmail-default",
    revision: 7,
    catalog_state: "current",
    items: [
      {
        selector_id: "selector-1",
        label_id: "Label_1",
        display_name: "Invoices",
        status: "active",
        admission_active: true,
      },
    ],
  };
  const unavailable = {
    ...active,
    catalog_state: "unavailable",
    items: [
      {
        ...active.items[0],
        status: "validation_unavailable",
        admission_active: false,
      },
    ],
  };
  let selectorReads = 0;
  const harness = gmailLabelLoadHarness(async (operation) => {
    if (operation === "gmail_label_selectors_list") {
      selectorReads += 1;
      return selectorReads === 1 ? active : unavailable;
    }
    assert.equal(operation, "gmail_labels_catalog");
    throw new Error("Gmail labels are temporarily unavailable");
  });

  await harness.load();

  const snapshot = harness.snapshot();
  assert.equal(selectorReads, 2);
  assert.equal(snapshot.catalogVerified, false);
  assert.equal(snapshot.revision, 7);
  assert.deepEqual(snapshot.selectorRenders.at(-1), unavailable.items);
});

test("late catalog-failure re-list cannot overwrite a newer account generation", async () => {
  const active = {
    provider: "gmail",
    account_id: "gmail-default",
    revision: 7,
    catalog_state: "current",
    items: [
      {
        selector_id: "selector-1",
        label_id: "Label_1",
        display_name: "Invoices",
        status: "active",
        admission_active: true,
      },
    ],
  };
  let resolveRelist: ((value: Record<string, unknown>) => void) | undefined;
  let selectorReads = 0;
  const harness = gmailLabelLoadHarness(async (operation) => {
    if (operation === "gmail_label_selectors_list") {
      selectorReads += 1;
      if (selectorReads === 1) return active;
      return new Promise<Record<string, unknown>>((resolve) => {
        resolveRelist = resolve;
      });
    }
    assert.equal(operation, "gmail_labels_catalog");
    throw new Error("Gmail labels are temporarily unavailable");
  });

  const loading = harness.load();
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(selectorReads, 2);
  assert.ok(resolveRelist);
  const rendersBeforeNewGeneration = harness.snapshot().selectorRenders.length;
  harness.bumpGeneration();
  resolveRelist({
    ...active,
    catalog_state: "unavailable",
    items: [
      {
        ...active.items[0],
        status: "validation_unavailable",
        admission_active: false,
      },
    ],
  });
  await loading;

  assert.equal(harness.snapshot().selectorRenders.length, rendersBeforeNewGeneration);
});

test("failed catalog recovery renders cached selectors explicitly inert", async () => {
  const active = {
    provider: "gmail",
    account_id: "gmail-default",
    revision: 7,
    catalog_state: "current",
    items: [
      {
        selector_id: "selector-1",
        label_id: "Label_1",
        display_name: "Invoices",
        status: "active",
        admission_active: true,
      },
    ],
  };
  let selectorReads = 0;
  const harness = gmailLabelLoadHarness(async (operation) => {
    if (operation === "gmail_label_selectors_list") {
      selectorReads += 1;
      if (selectorReads === 1) return active;
      throw new Error("Selector state unavailable");
    }
    assert.equal(operation, "gmail_labels_catalog");
    throw new Error("Gmail labels are temporarily unavailable");
  });

  await harness.load();

  const snapshot = harness.snapshot();
  assert.equal(selectorReads, 2);
  assert.equal(snapshot.revision, null);
  assert.deepEqual(snapshot.selectorRenders.at(-1), [
    {
      ...active.items[0],
      status: "validation_unavailable",
      admission_active: false,
    },
  ]);
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
