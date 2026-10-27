import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { hasInboxAutomationConfirmation, inboxActionState } from "../src/inboxActionState.ts";
import { inboxClearView, inboxRemovalStatus } from "../src/inboxRemoval.ts";

const source = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

test("inbox filters use an initially collapsed native disclosure", () => {
  const panel = source.match(
    /<details class="inbox-filter-panel">([\s\S]*?)<\/details>/,
  );

  assert.ok(panel, "expected the inbox filter disclosure without open");
  assert.match(panel[1], /<summary>[\s\S]*Inbox filters[\s\S]*<\/summary>/);
  assert.match(panel[1], /<span class="inbox-filter-summary">Sender, priority, topic and more<\/span>/);
  assert.match(panel[1], /<form id="inbox-filter-form" class="inbox-filter-form">/);
  assert.ok(panel[1].indexOf("<summary>") < panel[1].indexOf("<form"));
});

test("shell view marker starts at inbox and follows only showView", () => {
  assert.ok(source.includes('<div class="shell" data-view="inbox">'), "shell must start with the inbox view marker");
  const show = source.match(/function showView\([\s\S]*?\n\}/)![0];
  assert.match(show, /requiredElement<HTMLElement>\("\.shell"\)\.dataset.view = view;/);
  assert.equal(source.match(/\.dataset\.view\s*=/g)?.length, 1);
});

test("wide shell applies only to inbox and expiry ledger above the phone breakpoint", () => {
  const wide = styles.match(/@media \(min-width: 681px\) \{([\s\S]*?)\n\}/)?.[1];
  assert.ok(wide, "wide shell must be inside the desktop media query");
  assert.match(wide, /^\s*\.shell\[data-view="inbox"\],\s*\.shell\[data-view="expiry-ledger"\]\s*\{\s*width: max\(80%, min\(760px, calc\(100% - 40px\)\)\);\s*\}\s*$/);
  assert.match(styles, /\.shell \{\s*width: min\(760px, calc\(100% - 40px\)\);/);
  assert.match(styles, /@media \(max-width: 680px\) \{\s*\.shell \{\s*width: min\(100% - 28px, 760px\);/);
});

test("inbox intro is clipped while retaining its accessible heading", () => {
  const intro = styles.match(/\.shell\[data-view="inbox"\] \.intro \{([^}]+)\}/)?.[1];
  assert.ok(intro, "inbox intro must have a view-keyed visually-hidden rule");
  for (const declaration of ["position: absolute;", "width: 1px;", "height: 1px;", "overflow: hidden;", "clip: rect(0 0 0 0);", "white-space: nowrap;"]) assert.ok(intro.includes(declaration), declaration);
  assert.doesNotMatch(intro, /display:\s*none|visibility:\s*hidden/);
  assert.match(source, /<header class="intro">[\s\S]*?<h1>Your signal inbox<\/h1>/);
});

test("expanded content keeps provenance, priority and actions without repeated row items", () => {
  const render = source.match(/function renderInbox\([\s\S]*?\n\}/)![0];
  const expanded = render.slice(0, render.indexOf("const summary =")) + render.slice(render.indexOf("const footer ="), render.indexOf("const toggle ="));
  assert.ok(!/\.append\([^;]*\b(?:sender|senderAddress|received|subject|category|state)\b/.test(expanded), "expanded content must not append sender, address, time, subject, category or state");
  for (const wiring of ["senderIdentity.append(sourceAccount)", "senderIdentity.append(admission)", "meta.append(senderIdentity)", "card.append(meta, summary)", "badges.append(badge)", "footer.append(badges, footerActions)", "footerActions.append(retryButton)", "footerActions.append(deleteButton)", "card.append(footer)"]) assert.ok(render.includes(wiring), wiring);
  for (const node of ["details", "calendarProposal", "attachments"]) assert.ok(render.includes(`card.append(${node})`), node);
  assert.match(render, /rowBadges.append\(category, state\)/);
  assert.match(render, /toggle.append\(identity, received, rowSubject, rowBadges\)/);
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
  assert.match(load, /query: \{ \.\.\.activeInboxQuery, cursor \}/);
  assert.match(load, /if \(!mailboxEffectRequestIsCurrent[^\n]+[\s\S]*?return false/);
  const failure = load.slice(load.indexOf("} catch (error)"), load.indexOf("if (!append) {\n    attachmentCapabilities"));
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
  const success = inboxLoad.slice(inboxLoad.indexOf("if (!append) {\n    attachmentCapabilities"));
  assert.match(success, /if \(!append\) \{[^}]*inboxPageState = "loaded";[^}]*\} else \{[^}]*\}[\s\S]*renderInbox\(inboxItems\)/, "first-page success must record loaded before rendering");
  const clear = source.match(/async function clearInboxHistory\([\s\S]*?\n\}/)![0];
  assert.match(clear, /const result = await inboxRemovalClient.clear\(\);\s+if \(!result\) return;\s+const view = inboxClearView\(inboxItems, result\);/);
  assert.match(clear, /inboxItems = view.retained;\s+if \(result.outcome === "completed"\) \{\s+expandedInboxMessages.clear\(\);\s+inboxPageState = "loaded";[^}]*\}\s+renderInbox\(inboxItems\);/, "only completed history clear must clear expansion and record loaded before rendering retained rows");
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
      const expandedInboxMessages = new Set(["held", "deleted"]);
      let inboxPageState = "failed", inboxNextCursor = "next", inboxCapabilityUnavailableCount = 3;
      let inboxClearInFlight = false, inboxRequestGeneration = 0, reloads = 0;
      const inboxLoadMore = { hidden: false }, inboxStatus = { textContent: "", dataset: {} };
      const renders = [], cleared = [];
      const confirmAction = async () => true, inboxMutationInFlight = () => false;
      const setInboxControlsBusy = () => {}, wakeInboxReload = () => {}, loadInbox = () => { reloads++; };
      const clearMessageOwnedUiState = id => cleared.push(id ?? "all");
      const renderInbox = items => renders.push({ ids: items.map(item => item.message_id),
        state: inboxPageState, expanded: [...expandedInboxMessages] });
      const inboxRemovalClient = { clear: async () => {
        if (outcome === "error") throw { message: "Existing failure" };
        return result;
      } };
      ${handler}
      await clearInboxHistory();
      return JSON.stringify({ renders, cleared, inboxNextCursor, inboxCapabilityUnavailableCount,
        inboxLoadMore, inboxStatus, inboxClearInFlight, reloads });
    })()`, { result, outcome, inboxClearView, errorMessage: (error: { message: string }) => error.message }));
    const completed = outcome === "completed";
    for (const render of observed.renders.slice(1)) {
      assert.deepEqual(render.ids, accepted ? [] : ["held", "deleted"]);
      assert.deepEqual(render.expanded, accepted ? [] : ["held", "deleted"]);
      assert.equal(render.state, completed ? "loaded" : "failed");
    }
    assert.equal(observed.renders.length, accepted ? 3 : 2);
    assert.deepEqual(observed.cleared, completed ? ["held", "deleted", "all"] : accepted ? ["held", "deleted"] : []);
    assert.equal(observed.inboxNextCursor, completed ? null : "next");
    assert.equal(observed.inboxCapabilityUnavailableCount, completed ? 0 : 3);
    assert.equal(observed.inboxLoadMore.hidden, completed);
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

test("Delete handler clears only its accepted cached item and always reloads the shared projection", async () => {
  const handler = source.match(/async function deleteInboxItem\([\s\S]*?\n\}/)![0]
    .replace("(item: InboxItem): Promise<void>", "(item)");
  for (const outcome of ["completed", "deferred", "unaccepted", "error"] as const) {
    const accepted = outcome === "completed" || outcome === "deferred";
    const result = accepted ? { request_id: "saved-key", intent_ids: ["one"], outcome,
      requested: 1, completed: outcome === "completed" ? 1 : 0,
      pending: outcome === "deferred" ? 1 : 0,
      message: outcome === "deferred" ? "Deletion pending until the local upgrade completes" : null } : undefined;
    const observed = JSON.parse(await runInNewContext(`(async () => {
      const item = { message_id: "selected", subject: "Selected message" };
      let inboxItems = [item, { message_id: "other", subject: "Other message" }];
      const expandedInboxMessages = new Set(["selected", "other"]), inboxDeletionsInFlight = new Set();
      let inboxRequestGeneration = 0, reloads = 0, calls = 0;
      const inboxStatus = { textContent: "", dataset: {} }, cleared = [];
      const confirmAction = async () => true, inboxMutationInFlight = () => false;
      const setInboxControlsBusy = () => {}, renderInbox = () => {}, wakeInboxReload = () => {}, loadInbox = () => { reloads++; };
      const clearMessageOwnedUiState = id => cleared.push(id);
      const inboxRemovalClient = { delete: async id => {
        calls++; if (id !== "selected") throw Error("Wrong item");
        if (outcome === "error") throw { message: "Existing failure" };
        return result;
      } };
      ${handler}
      await deleteInboxItem(item);
      return JSON.stringify({ ids: inboxItems.map(item => item.message_id), expanded: [...expandedInboxMessages],
        inflight: [...inboxDeletionsInFlight], reloads, inboxStatus, cleared, calls });
    })()`, { result, outcome, inboxRemovalStatus, errorMessage: (error: { message: string }) => error.message }));
    assert.deepEqual(observed.ids, accepted ? ["other"] : ["selected", "other"]);
    assert.deepEqual(observed.expanded, observed.ids);
    assert.deepEqual(observed.cleared, accepted ? ["selected"] : []);
    assert.deepEqual(observed.inflight, []);
    assert.equal(observed.calls, 1);
    assert.equal(observed.reloads, 1);
    assert.equal(observed.inboxStatus.dataset.kind,
      outcome === "completed" ? "success" : outcome === "deferred" ? "pending" : outcome === "error" ? "error" : undefined);
    if (outcome === "completed") assert.equal(observed.inboxStatus.textContent,
      'Deleted "Selected message" from local history. The source email was not changed.');
    if (outcome === "deferred") assert.match(observed.inboxStatus.textContent, /^0 completed; 1 pending\./);
    if (outcome === "error") assert.equal(observed.inboxStatus.textContent, "Existing failure");
  }
});

test("inbox empty text checks pending loading and failed before existing query texts", () => {
  const empty = source.match(/function renderInbox\([\s\S]*?inboxList.append\(empty\)/)![0];
  assert.match(empty, /empty.textContent = inboxPageState === "pending"\s+\? "Messages not loaded yet\."\s+: inboxPageState === "loading"\s+\? "Loading messages\u2026"\s+: inboxPageState === "failed"\s+\? "Messages could not be loaded\."\s+: activeInboxQuery.sender !== null/, "empty text must prioritize request state over query-derived absence");
  assert.match(empty, /\? inboxSenderEmptyText\(activeInboxQuery, activeInboxAccountSelection\)\s+: filtered\s+\? "No watched messages match these filters\."\s+: "No watched messages yet\. Add a sender or Gmail label, then run the watcher\.";/);
});

// R1's held-operation matrix in inboxReload.test.ts executes these handlers.
test("sender and removal completion share the refresh owner", () => {
  const selection = source.match(/function selectInboxSender[\s\S]*?\n\}/)![0];
  assert.match(selection, /clearInboxPageForAccountChange/);
  assert.doesNotMatch(source, /inboxReloadAfterMutation|resumeInboxReloadAfterMutation/);
  const deletion = source.match(/async function deleteInboxItem[\s\S]*?\n\}/)![0];
  const clearing = source.match(/async function clearInboxHistory[\s\S]*?\n\}/)![0];
  assert.match(deletion, /finally \{\s+inboxDeletionsInFlight.delete\(item.message_id\);\s+setInboxControlsBusy\(false\);\s+renderInbox\(inboxItems\);\s+void loadInbox\(\);/);
  assert.match(clearing, /finally \{\s+inboxClearInFlight = false;\s+setInboxControlsBusy\(false\);\s+renderInbox\(inboxItems\);\s+void loadInbox\(\);/);
});

test("Apply and Reset carry the sender and clear query expansion", () => {
  const query = source.match(/function queryFromInboxControls[\s\S]*?\n\}/)![0];
  assert.ok(query.includes("sender: activeInboxQuery.sender"), "control query retains selected sender");
  const commit = source.match(/function commitInboxQueryFromControls[\s\S]*?\n\}/)![0];
  assert.match(commit, /activeInboxQuery.sender !== nextQuery.sender/);
  assert.match(commit, /inboxQueryEpoch \+= 1/);
  assert.match(commit, /expandedInboxMessages.clear\(\)/);
  assert.match(source, /inboxReset.addEventListener[\s\S]*?inboxFilterForm.reset\(\);\s+commitInboxQueryFromControls\(\)/);
  assert.match(source, /inboxFilterForm.addEventListener\("submit"[\s\S]*?commitInboxQueryFromControls\(\)/);
});

test("span refresh stops when superseded before or during append", async () => {
  const body = source.match(/async function runInboxRefresh\([\s\S]*?\): Promise<boolean> \{([\s\S]*?)\n\}/)![1];
  for (const stopAt of [1, 2]) {
    for (const committed of [true, false]) {
      const run = new Function("stopAt", "committed", `
        let inboxItems = [1, 2, 3], inboxNextCursor = "next", inboxQueryEpoch = 0, calls = 0;
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

test("expansion survives renders and first-page reloads until query, delete, or clear", () => {
  assert.ok(source.includes("const expandedInboxMessages = new Set<string>()"), "module-level expansion set exists");
  const render = source.match(/function renderInbox\([\s\S]*?\n\}/)![0];
  assert.match(render, /expandedInboxMessages.has\(item.message_id\)/);
  assert.match(render, /setAttribute\("aria-expanded", String\(!content.hidden\)\)/);
  assert.match(render, /setAttribute\("aria-controls", content.id\)/);
  assert.doesNotMatch(render, /expandedInboxMessages.clear|expandedInboxMessages.*filter/);
  const load = source.match(/async function loadInboxPage\([\s\S]*?\n\}/)![0];
  assert.doesNotMatch(load, /expandedInboxMessages/);
  assert.match(source, /const result = await inboxRemovalClient.delete\(item.message_id\);\s+if \(!result\) return;\s+expandedInboxMessages.delete\(item.message_id\)/);
  const clear = source.match(/async function clearInboxHistory\([\s\S]*?\n\}/)![0];
  assert.match(clear, /if \(!retained.has\(item.message_id\)\) \{\s+expandedInboxMessages.delete\(item.message_id\)/);
  assert.match(clear, /if \(result.outcome === "completed"\) \{\s+expandedInboxMessages.clear\(\)/);
});

test("collapsed row feeds only projected states and the render clock to one chip", () => {
  const detailsStart = source.indexOf('const details = document.createElement("div");');
  const detailsEnd = source.indexOf("const deadlineValue =", detailsStart);
  const chipStart = source.indexOf("const actionState = inboxActionState(");
  const chipEnd = source.indexOf("toggle.append(", chipStart);
  assert.ok(detailsStart >= 0 && detailsEnd > detailsStart && chipStart > detailsEnd && chipEnd > chipStart);
  const renderStartedAt = Date.parse("2026-10-12T12:00:00Z");
  const privateFields = { reason: "private reason", job_id: "private job", last_error: "private error" };
  const cases = [
    {
      attachments: [
        { automation_fires: [{ state: "awaiting_confirmation", ...privateFields }, { state: "completed", ...privateFields }], capability_results: [{ status: "processing", ...privateFields }] },
        { automation_fires: [{ state: "failed", ...privateFields }, null] },
        {},
      ],
      proposal: { state: "awaiting_confirmation", status: "accepted", expires_at: "2026-10-13T12:00:00Z", ...privateFields },
      fires: ["awaiting_confirmation", "completed", "failed", undefined], results: ["processing"],
    },
    { attachments: [], proposal: null, fires: [], results: [] },
    { attachments: [{}], proposal: { state: "awaiting_confirmation", status: "accepted", expires_at: null, ...privateFields }, fires: [], results: [] },
  ];
  for (const fixture of cases) {
    let cueStates: readonly unknown[] | undefined;
    let chipStates: readonly unknown[] | undefined;
    let chipResults: readonly unknown[] | undefined;
    let chipProposal: Record<string, unknown> | null | undefined;
    let chipClock: number | undefined;
    const chips: string[] = [];
    runInNewContext(source.slice(detailsStart, detailsEnd) + source.slice(chipStart, chipEnd), {
      item: { attachments: fixture.attachments, calendar_proposal: fixture.proposal, action_required: 0, suggested_action: "" },
      renderStartedAt,
      hasInboxAutomationConfirmation: (states: readonly unknown[]) => {
        cueStates = states;
        return hasInboxAutomationConfirmation(states);
      },
      inboxActionState: (states: readonly unknown[], results: readonly unknown[], proposal: Record<string, unknown> | null, clock: number) => {
        chipStates = states; chipResults = results; chipProposal = proposal; chipClock = clock;
        return inboxActionState(states, results, proposal, clock);
      },
      document: { createElement: () => ({ dataset: {}, textContent: "", append() {} }) },
      rowBadges: { append: (node: { textContent: string }) => chips.push(node.textContent) },
    });
    assert.ok(cueStates && chipStates && chipResults);
    assert.strictEqual(chipStates, cueStates, "cue and chip consume the same projected array");
    assert.deepEqual(Array.from(chipStates), fixture.fires);
    assert.deepEqual(Array.from(chipResults), fixture.results);
    const expectedProposal = fixture.proposal ? {
      state: fixture.proposal.state, status: fixture.proposal.status, expires_at: fixture.proposal.expires_at,
    } : null;
    assert.deepEqual(chipProposal ? { ...chipProposal } : null, expectedProposal);
    assert.equal(chipClock, renderStartedAt);
    const expectedChip = inboxActionState(fixture.fires, fixture.results, expectedProposal, renderStartedAt);
    assert.deepEqual(chips, expectedChip === null ? [] : [expectedChip]);
  }
  assert.match(source, /attachmentCount.textContent = `\$\{item.attachments.length\} attachment/);
  assert.match(source, /content.append\(\.\.\.Array.from\(card.childNodes\)\)/);
});

test("watchlist updates and failures render navigation with a responsive layout", async () => {
  assert.ok(/watchedSenders = senders;\s+renderInboxSenderNavigation\(\)/.test(source), "watchlist changes refresh sender navigation");
  assert.match(source, /function loadSenders[^]*?catch \(error\) \{\s+renderInboxSenderNavigation\(errorMessage\(error\)\)/);
  assert.match(source, /setAttribute\("aria-pressed", String\(item.selected\)\)/);
  assert.match(source, /if \(model.sender !== activeInboxQuery.sender\) selectInboxSender\(model.sender\)/);
  assert.ok(/\.inbox-columns\s*\{[^}]*grid-template-columns: minmax\(220px, 300px\) minmax\(0, 1fr\);/.test(styles), "sender column must use minmax(220px, 300px)");
  assert.match(styles, /@media \(max-width: 760px\)\s*\{\s*\.inbox-columns\s*\{\s*grid-template-columns: minmax\(0, 1fr\);/);
});
