import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");

test("inbox filters use an initially open native disclosure", () => {
  const panel = source.match(
    /<details class="inbox-filter-panel" open>([\s\S]*?)<\/details>/,
  );

  assert.ok(panel, "expected the inbox filter disclosure");
  assert.match(panel[1], /<summary>[\s\S]*Inbox filters[\s\S]*<\/summary>/);
  assert.match(panel[1], /<form id="inbox-filter-form" class="inbox-filter-form">/);
  assert.ok(panel[1].indexOf("<summary>") < panel[1].indexOf("<form"));
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
  const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(styles, /\.inbox-columns\s*\{[^}]*grid-template-columns:/);
  assert.match(styles, /@media \(max-width: 760px\)/);
});
