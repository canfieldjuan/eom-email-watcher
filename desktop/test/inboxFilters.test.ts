import { stripTypeScriptTypes } from "node:module";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { inboxClearStatus, inboxRemovalStatus } from "../src/inboxRemoval.ts";

const source = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

test("compact filters start collapsed while keyword and Search stay visible and share the same form", () => {
  const panel = source.match(
    /<details id="inbox-filter-panel" class="inbox-filter-panel">([\s\S]*?)<\/details>/,
  );

  assert.ok(panel, "expected the inbox filter disclosure without open");
  assert.match(panel[1], /<summary id="inbox-filters-toggle">Filters<\/summary>/);
  assert.doesNotMatch(panel[1], /id="inbox-keyword"/);
  assert.match(source, /<input id="inbox-keyword"[^>]*maxlength="200"[^>]*form="inbox-filter-form"/);
  assert.match(source, /<button class="inbox-search-submit" type="submit" form="inbox-filter-form">Search<\/button>/);
  assert.match(panel[1], /Sender contains[\s\S]*id="inbox-sender"[^>]*maxlength="320"/);
  assert.match(panel[1], /<form id="inbox-filter-form" class="inbox-filter-form">/);
  assert.ok(panel[1].indexOf("<summary ") < panel[1].indexOf("<form"));
});

test("shell view marker starts at inbox and follows only showView", () => {
  assert.ok(source.includes('<div class="shell" data-view="inbox">'), "shell must start with the inbox view marker");
  const show = source.match(/function showView\([\s\S]*?\n\}/)![0];
  assert.match(show, /const shell = requiredElement<HTMLElement>\("\.shell"\);[\s\S]*shell\.dataset\.view = view;/);
  assert.equal(source.match(/\.dataset\.view\s*=(?!=)/g)?.length, 1);
});

test("Inbox uses available width while Expiry Ledger keeps its existing width rule", () => {
  const wide = styles.match(/@media \(min-width: 681px\) \{([\s\S]*?)\n\}/)?.[1];
  assert.ok(wide, "wide shell must be inside the desktop media query");
  assert.match(wide, /^\s*\.shell\[data-view="expiry-ledger"\]\s*\{\s*width: max\(80%, min\(760px, calc\(100% - 40px\)\)\);\s*\}\s*$/);
  assert.match(styles, /\.shell\[data-view="inbox"\] \{\s*width: calc\(100% - 32px\);/);
  assert.match(styles, /\.shell \{\s*width: min\(760px, calc\(100% - 40px\)\);/);
  assert.match(styles, /@media \(max-width: 680px\) \{\s*\.shell \{\s*width: min\(100% - 28px, 760px\);/);
});

// Accepted R0a replaces the clipped, view-dependent intro. Its shared-header
// replacement is exercised against real layout by scripts/prove_common_header.mjs.

test("conversation rows keep their cues and the reader owns the existing detail controls", () => {
  const render = source.match(/function renderInbox\([\s\S]*?\n\}/)![0];
  const details = source.match(/function renderInboxMessageDetails\([\s\S]*?\n\}/)![0];
  assert.match(source, /renderInboxMessageDetails\(item, extras, renderStartedAt, context\)/);
  assert.doesNotMatch(render, /renderInboxMessageDetails|Open thread|aria-expanded/);
  assert.match(render, /trigger.append\(identity, time, subject, badges\)/);
  for (const wiring of ["senderIdentity.append(sourceAccount)", "senderIdentity.append(admission)",
    "card.append(meta, summary)", "badges.append(badge, category)", "footer.append(badges, footerActions)",
    "footerActions.append(retryButton)", "footerActions.append(deleteButton)", "card.append(footer)"]) {
    assert.ok(details.includes(wiring), wiring);
  }
  for (const node of ["details", "calendarProposal", "attachments"]) assert.ok(details.includes("card.append(" + node + ")"), node);
});


test("sender selection clears the scope before requesting its first page", () => {
  const selection = source.match(/function selectInboxSender\(sender: string \| null\): void \{([\s\S]*?)\n\}/)?.[1];
  assert.ok(selection, "sender selection handler exists");
  assert.match(selection, /activeInboxQuery = \{ \.\.\.activeInboxQuery, sender \}/);
  assert.doesNotMatch(selection, /sender_query/);
  assert.match(selection, /clearInboxPageForAccountChange\(/);
  assert.doesNotMatch(selection, /void loadInbox|inboxReloadAfterMutation/);
  const clear = source.match(/function clearInboxPageForAccountChange[\s\S]*?\n\}/)![0];
  assert.match(clear, /inboxRequestGeneration \+= 1/);
  assert.match(clear, /inboxQueryEpoch \+= 1/);
  assert.match(clear, /inboxItems = \[\]/);
  assert.match(clear, /inboxNextCursor = null/);
  assert.match(clear, /inboxLoadMore.hidden = true/);
  assert.match(clear, /renderInbox\(inboxItems\)[\s\S]*void loadInbox\(\)/);
  const load = source.match(/async function loadInboxPage\([\s\S]*?\n\}/)![0];
  assert.match(load, /inboxConversationClient.query\(\{ \.\.\.activeInboxQuery, cursor \}\)/);
  assert.match(load, /if \(!mailboxEffectRequestIsCurrent[^\n]+[\s\S]*?return false/);
  const failure = load.slice(load.indexOf("} catch (error)"), load.indexOf("if (!append) {\n    inboxItems = page.items"));
  assert.match(failure, /inboxStatus.dataset.kind = "error"/);
  assert.doesNotMatch(failure, /inboxItems =/);
});

const inboxLoad = source.match(/async function loadInboxPage\([\s\S]*?\n\}/)![0];

test("inbox page state starts loading and scope clearing records pending before rendering", () => {
  assert.ok(source.includes('let inboxPageState: "pending" | "loading" | "loaded" | "failed" = "loading";'), "page state must start loading");
  const clear = source.match(/function clearInboxPageForAccountChange[\s\S]*?\n\}/)![0];
  assert.match(clear, /inboxPageState = "pending";[\s\S]*renderInbox\(inboxItems\)/, "scope clear must record pending before rendering");
});

test("inbox first-page loading follows unchanged guards and owns its generation before requesting", () => {
  assert.match(inboxLoad, /if \(!mailboxEffectScopeIsCurrent\(effectScope\)\) return false;\s+if \(inboxReloadBlocked\(\)\) return false;\s+if \(append && !inboxNextCursor\) return false;\s+const generation = \+\+inboxRequestGeneration;/);
  assert.doesNotMatch(inboxLoad, /inboxReloadAfterMutation/);
  const beforeRequest = inboxLoad.slice(inboxLoad.indexOf("const generation"), inboxLoad.indexOf("page = await"));
  assert.match(beforeRequest, /if \(!append\) \{\s+inboxPageState = "loading";\s+inboxPageStateGeneration = generation;\s+if \(inboxItems.length === 0\) renderInbox\(inboxItems\);\s+\}/, "first-page request must own loading and render an empty list");
});

test("inbox current first-page failure records failed even with retained rows", () => {
  const failure = inboxLoad.slice(inboxLoad.indexOf("} catch (error)"), inboxLoad.indexOf('inboxStatus.dataset.kind = "error"'));
  assert.match(failure, /return false;[\s\S]*?if \(!append\) \{\s+inboxPageState = "failed";\s+if \(inboxItems.length === 0\) renderInbox\(inboxItems\);/, "current first-page failure must record failed outside the empty-row condition");
  assert.doesNotMatch(failure, /inboxItems =/);
});

test("inbox stale success and failure release only their own first-page loading state", () => {
  const releases = inboxLoad.match(/if \(!mailboxEffectRequestIsCurrent\(generation, inboxRequestGeneration, effectScope\)\) \{\s+if \(!append && inboxPageState === "loading" && inboxPageStateGeneration === generation\) \{\s+inboxPageState = "pending";\s+if \(inboxItems.length === 0\) renderInbox\(inboxItems\);\s+\}\s+return false;\s+\}/g);
  assert.equal(releases?.length, 2, "both stale returns must release only their own loading state");
});

test("inbox first-page and clear-history success record loaded before rendering", () => {
  const success = inboxLoad.slice(inboxLoad.indexOf("if (!append) {\n    inboxItems = page.items"));
  assert.match(success, /if \(!append\) \{[^}]*inboxPageState = "loaded";[^}]*\} else \{[\s\S]*renderInbox\(inboxItems\)/, "first-page success must record loaded before rendering");
  const clear = source.match(/async function clearInboxHistory\([\s\S]*?\n\}/)![0];
  assert.match(clear, /const result = await inboxRemovalClient.clear\(\);\s+if \(!result\) return;\s+const view = inboxClearStatus\(result\);/);
  assert.match(clear, /inboxItems = \[\];[\s\S]*if \(result.outcome === "completed"\) inboxPageState = "loaded";\s*renderInbox\(inboxItems\)/);
});

test("clear handler hides accepted targets, preserves unaccepted state, and resets only completed state", async () => {
  const handler = source.match(/async function clearInboxHistory\([\s\S]*?\n\}/)![0]
    .replace("(): Promise<void>", "()");
  const pending = "Deletion pending until the local upgrade completes";
  for (const outcome of ["deferred", "completed", "unaccepted", "error"] as const) {
    const accepted = outcome === "deferred" || outcome === "completed";
    const result = accepted ? { outcome, request_id: "saved-key", intent_ids: ["one", "two"],
      requested: 2, completed: outcome === "completed" ? 2 : 1,
      pending: outcome === "completed" ? 0 : 1,
      message: outcome === "completed" ? null : pending } : undefined;
    const observed = JSON.parse(await runInNewContext(`(async () => {
      let inboxItems = [{ message_id: "held" }, { message_id: "deleted" }];
      let inboxPageState = "failed", inboxNextCursor = "next", inboxTotals = {conversations:2};
      let inboxClearInFlight = false, inboxRequestGeneration = 0, reloads = 0;
      const inboxLoadMore = { hidden: false }, inboxStatus = { textContent: "", dataset: {} };
      const renders = [], cleared = [];
      const confirmAction = async () => true, inboxMutationInFlight = () => false;
      const threadHistory = { beginMutation() {}, async endMutation() {} };
      const setInboxControlsBusy = () => {}, wakeInboxReload = () => {}, loadInbox = () => { reloads++; };
      const clearMessageOwnedUiState = id => cleared.push(id ?? "all");
      const renderInbox = items => renders.push({ ids: items.map(item => item.message_id),
        state: inboxPageState });
      const inboxRemovalClient = { clear: async () => {
        if (outcome === "error") throw { message: "Existing failure" };
        return result;
      } };
      ${handler}
      await clearInboxHistory();
      return JSON.stringify({ renders, cleared, inboxNextCursor, inboxTotals,
        inboxLoadMore, inboxStatus, inboxClearInFlight, reloads });
    })()`, { result, outcome, inboxClearStatus, errorMessage: (error: { message: string }) => error.message }));
    const completed = outcome === "completed";
    for (const render of observed.renders.slice(1)) {
      assert.deepEqual(render.ids, accepted ? [] : ["held", "deleted"]);
      assert.equal(render.state, completed ? "loaded" : "failed");
    }
    assert.equal(observed.renders.length, accepted ? 3 : 2);
    assert.deepEqual(observed.cleared, accepted ? ["all"] : []);
    assert.equal(observed.inboxNextCursor, accepted ? null : "next");
    assert.deepEqual(observed.inboxTotals, accepted ? null : {conversations:2});
    assert.equal(observed.inboxLoadMore.hidden, accepted);
    assert.equal(observed.inboxStatus.dataset.kind,
      completed ? "success" : outcome === "deferred" ? "pending" : outcome === "error" ? "error" : undefined);
    assert.equal(observed.inboxClearInFlight, false);
    assert.equal(observed.reloads, 1);
    if (completed) assert.match(observed.inboxStatus.textContent, /^Cleared 2 local messages\./);
    else if (outcome === "deferred") {
      assert.ok(observed.inboxStatus.textContent.includes(pending));
      assert.match(observed.inboxStatus.textContent, /^1 completed; 1 pending\./);
      assert.doesNotMatch(observed.inboxStatus.textContent, /Cleared/);
    } else assert.equal(observed.inboxStatus.textContent, outcome === "error" ? "Existing failure" : "");
  }
});

test("Delete targets its exact email and leaves conversation membership to refresh", async () => {
  const handler = stripTypeScriptTypes(source.match(/function deleteInboxItem\([\s\S]*?\n\}/)![0]);
  for (const outcome of ["completed", "deferred", "unaccepted", "error"] as const) {
    const accepted = outcome === "completed" || outcome === "deferred";
    const result = accepted ? { request_id: "saved-key", intent_ids: ["one"], outcome,
      requested: 1, completed: outcome === "completed" ? 1 : 0,
      pending: outcome === "deferred" ? 1 : 0,
      message: outcome === "deferred" ? "Deletion pending until the local upgrade completes" : null } : undefined;
    const observed = JSON.parse(await runInNewContext(`(async () => {
      const item = { message_id: "selected", subject: "Selected message" };
      let inboxItems = [item, { message_id: "other", subject: "Other message" }];
      const inboxDeletionsInFlight = new Set();
      let inboxRequestGeneration = 0, reloads = 0, calls = 0;
      const inboxStatus = { textContent: "", dataset: {} }, cleared = [];
      const confirmAction = async () => true, inboxMutationInFlight = () => false;
      const threadHistory = { beginMutation() {}, async endMutation() {} };
      const setInboxControlsBusy = () => {}, wakeInboxReload = () => {}, refreshLoadedInboxSpan = () => { reloads++; };
      const showInboxActionNotice = (subject, text, kind) => { inboxStatus.textContent = subject + " · " + text; inboxStatus.dataset.kind = kind; };
      const performInboxReaderAction = (_context, _key, run) => run();
      const clearMessageOwnedUiState = id => cleared.push(id);
      const inboxRemovalClient = { delete: async id => {
        calls++; if (id !== "selected") throw Error("Wrong item");
        if (outcome === "error") throw { message: "Existing failure" };
        return result;
      } };
      ${handler}
      await deleteInboxItem({messageId:item.message_id,subject:item.subject,current:()=>true,afterApproval:async()=>true});
      return JSON.stringify({ ids: inboxItems.map(item => item.message_id),
        inflight: [...inboxDeletionsInFlight], reloads, inboxStatus, cleared, calls });
    })()`, { result, outcome, inboxRemovalStatus, errorMessage: (error: { message: string }) => error.message }));
    assert.deepEqual(observed.ids, ["selected", "other"]);
    assert.deepEqual(observed.cleared, accepted ? ["selected"] : []);
    assert.deepEqual(observed.inflight, []);
    assert.equal(observed.calls, 1);
    assert.equal(observed.reloads, 1);
    assert.equal(observed.inboxStatus.dataset.kind,
      outcome === "completed" ? "success" : outcome === "deferred" ? "pending" : outcome === "error" ? "error" : undefined);
    if (outcome === "completed") assert.equal(observed.inboxStatus.textContent,
      'Selected message · Deleted from local history. The source email was not changed.');
    if (outcome === "deferred") assert.match(observed.inboxStatus.textContent, /0 completed; 1 pending\./);
    if (outcome === "error") assert.equal(observed.inboxStatus.textContent, "Selected message · Existing failure");
  }
});

test("inbox empty text checks pending loading and failed before existing query texts", () => {
  const empty = source.match(/function renderInbox\([\s\S]*?inboxList.append\(empty\)/)![0];
  assert.match(empty, /empty.textContent = inboxPageState === "pending"\s+\? "Conversations not loaded yet\."\s+: inboxPageState === "loading"\s+\? "Loading conversations\u2026"\s+: inboxPageState === "failed"\s+\? "Conversations could not be loaded\."\s+: activeInboxQuery.sender !== null/, "empty text must prioritize request state over query-derived absence");
  assert.match(empty, /\? inboxSenderEmptyText\(activeInboxQuery, activeInboxAccountSelection\)\s+: filtered\s+\? "No retained conversations match these filters\."\s+: "No retained conversations yet\.";/);
});

// R1's held-operation matrix in inboxReload.test.ts executes these handlers.
test("sender and removal completion share the refresh owner", () => {
  const selection = source.match(/function selectInboxSender[\s\S]*?\n\}/)![0];
  assert.match(selection, /clearInboxPageForAccountChange/);
  assert.doesNotMatch(source, /inboxReloadAfterMutation|resumeInboxReloadAfterMutation/);
  const deletion = source.match(/function deleteInboxItem[\s\S]*?\n\}/)![0];
  const clearing = source.match(/async function clearInboxHistory[\s\S]*?\n\}/)![0];
  assert.match(deletion, /finally \{\s+void threadHistory\.endMutation\(\);\s+inboxDeletionsInFlight.delete\(messageId\);\s+setInboxControlsBusy\(false\);\s+void refreshLoadedInboxSpan\(\);/);
  assert.match(clearing, /finally \{\s+void threadHistory\.endMutation\(\);\s+inboxClearInFlight = false;\s+setInboxControlsBusy\(false\);\s+renderInbox\(inboxItems\);\s+void loadInbox\(\);/);
});

test("Apply and Reset carry the sender and clear reader selection", () => {
  const query = source.match(/function queryFromInboxControls[\s\S]*?\n\}/)![0];
  assert.ok(query.includes("sender: activeInboxQuery.sender"), "control query retains selected sender");
  const commit = source.match(/function commitInboxQueryFromControls[\s\S]*?\n\}/)![0];
  assert.match(commit, /activeInboxQuery.sender !== nextQuery.sender/);
  assert.match(commit, /inboxQueryEpoch \+= 1/);
  assert.match(commit, /threadHistory.clearInboxSelection\(\)/);
  assert.match(source, /inboxReset.addEventListener[\s\S]*?inboxFilterForm.reset\(\);\s+commitInboxQueryFromControls\(\)/);
  assert.match(source, /inboxFilterForm.addEventListener\("submit"[\s\S]*?commitInboxQueryFromControls\(\)/);
});

test("span refresh stops when superseded before or during append", async () => {
  const body = source.match(/async function runInboxRefresh\([\s\S]*?\): Promise<boolean> \{([\s\S]*?)\n\}/)![1];
  for (const stopAt of [1, 2]) {
    for (const committed of [true, false]) {
      const run = new Function("stopAt", "committed", `
        let inboxItems = [1, 2, 3], inboxNextCursor = "next", inboxQueryEpoch = 0, inboxItemsEpoch = 0, calls = 0;
        const captureInboxScroll = () => ({ top: 0, revision: 0 });
        const currentMailboxEffectScope = () => ({ mutation_epoch: 0, mailbox_operation_revision: 1 });
        const inboxThreadEntry = { refreshGeneration: 0, failRefresh: () => {} };
        async function loadInboxPage() {
          calls++;
          inboxItems = calls === 1 ? [1] : [1, 2];
          if (calls === stopAt) { if (committed) inboxQueryEpoch++; return committed; }
          if (calls > stopAt) inboxNextCursor = null;
          return true;
        }
        const intent = { kind: "span", target: 3 }, effectScope = null;
        const current = () => inboxQueryEpoch === 0;
        return (async () => { await (async () => { ${body} })(); return calls; })();
      `);
      assert.equal(await run(stopAt, committed), stopAt, "superseded span must stop at its interrupted load");
    }
  }
});

test("list uses only projected conversation counts for its cues", () => {
  const render = source.match(/function renderInbox\([\s\S]*?\n\}/)![0];
  for (const field of ["messages", "matching_messages", "pending_messages", "analysis_errors",
    "action_required", "attachments", "truncated_messages"]) assert.ok(render.includes("counts." + field));
  assert.doesNotMatch(render, /automation_fires|capability_results|calendar_proposal|inbox_action/);
});

test("watchlist updates and failures render navigation with a responsive layout", async () => {
  assert.ok(/watchedSenders = senders;\s+renderInboxSenderNavigation\(\)/.test(source), "watchlist changes refresh sender navigation");
  assert.match(source, /function loadSenders[^]*?catch \(error\) \{\s+renderInboxSenderNavigation\(errorMessage\(error\)\)/);
  assert.match(source, /setAttribute\("aria-pressed", String\(item.selected\)\)/);
  assert.match(source, /if \(model.sender !== activeInboxQuery.sender\) selectInboxSender\(model.sender\)/);
  assert.match(source, /id="inbox-senders-toggle"[^>]*aria-controls="inbox-senders-content"/);
  assert.match(styles, /#inbox-columns\[data-layout="wide"\][^}]*grid-template-columns:/);
  assert.match(styles, /#inbox-columns\[data-layout="split"\][^}]*grid-template-columns:/);
});
