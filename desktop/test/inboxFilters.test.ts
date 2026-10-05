import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

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
  assert.ok(selection.indexOf("clearInboxPageForAccountChange(") < selection.indexOf("void loadInbox()"));
  const clear = source.match(/function clearInboxPageForAccountChange[\s\S]*?\n\}/)![0];
  assert.match(clear, /inboxRequestGeneration \+= 1/);
  assert.match(clear, /inboxQueryEpoch \+= 1/);
  assert.match(clear, /inboxItems = \[\]/);
  assert.match(clear, /inboxNextCursor = null/);
  assert.match(clear, /inboxLoadMore.hidden = true/);
  assert.match(clear, /renderInbox\(inboxItems\)/);
  const load = source.match(/async function loadInbox\([\s\S]*?\n\}/)![0];
  assert.match(load, /query: \{ \.\.\.activeInboxQuery, cursor \}/);
  assert.match(load, /if \(!mailboxEffectRequestIsCurrent[^\n]+return false/);
  const failure = load.slice(load.indexOf("} catch (error)"), load.indexOf("if (!append) {\n    attachmentCapabilities"));
  assert.match(failure, /inboxStatus.dataset.kind = "error"/);
  assert.doesNotMatch(failure, /inboxItems =/);
});

test("sender selection defers reload until the final inbox mutation finishes", () => {
  const selection = source.match(/function selectInboxSender\([\s\S]*?\n\}/)![0];
  assert.match(
    selection,
    /if \(inboxMutationInFlight\(\)\) \{\s+inboxReloadAfterMutation = true;\s+\} else \{\s+void loadInbox\(\);\s+\}/,
    "sender selection must defer its reload while a mutation is in flight",
  );
  assert.match(source, /let inboxReloadAfterMutation = false;/);
  const resume = source.match(/function resumeInboxReloadAfterMutation\([\s\S]*?\n\}/)![0];
  assert.match(resume, /if \(inboxMutationInFlight\(\) \|\| !inboxReloadAfterMutation\) return;/);
  assert.match(resume, /inboxReloadAfterMutation = false;\s+void loadInbox\(\);/);
  const deletion = source.match(/async function deleteInboxItem\([\s\S]*?\n\}/)![0];
  const clearing = source.match(/async function clearInboxHistory\([\s\S]*?\n\}/)![0];
  assert.match(deletion, /finally \{\s+inboxDeletionsInFlight.delete\(item.message_id\);\s+setInboxControlsBusy\(false\);\s+renderInbox\(inboxItems\);\s+resumeInboxReloadAfterMutation\(\);/);
  assert.match(clearing, /finally \{\s+inboxClearInFlight = false;\s+setInboxControlsBusy\(false\);\s+renderInbox\(inboxItems\);\s+resumeInboxReloadAfterMutation\(\);/);
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
  const body = source.match(/async function refreshLoadedInboxSpan\(\): Promise<void> \{([\s\S]*?)\n\}/)![1];
  for (const stopAt of [1, 2]) {
    for (const committed of [true, false]) {
      const run = new Function("stopAt", "committed", `
        let inboxItems = [1, 2, 3], inboxNextCursor = "next", inboxQueryEpoch = 0, calls = 0;
        async function loadInbox() {
          calls++;
          inboxItems = calls === 1 ? [1] : [1, 2];
          if (calls === stopAt) { if (committed) inboxQueryEpoch++; return committed; }
          if (calls > stopAt) inboxNextCursor = null;
          return true;
        }
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
  const load = source.match(/async function loadInbox\([\s\S]*?\n\}/)![0];
  assert.doesNotMatch(load, /expandedInboxMessages/);
  assert.match(source, /await invoke<void>\("inbox_delete"[^\n]+\n\s+expandedInboxMessages.delete\(item.message_id\)/);
  assert.match(source, /await invoke<number>\("inbox_clear"\);\s+expandedInboxMessages.clear\(\)/);
});

test("collapsed row feeds only projected states and the render clock to one chip", () => {
  const chip = source.match(/const actionState = inboxActionState\(([\s\S]*?)\n    \);/)?.[1];
  assert.ok(chip, "collapsed row computes its action chip");
  for (const field of ["fire?.state", "result?.status", "state:", "status:", "expires_at:", "renderStartedAt"]) assert.ok(chip.includes(field), field);
  assert.doesNotMatch(chip, /reason|job_id|last_error|\.\.\./);
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
