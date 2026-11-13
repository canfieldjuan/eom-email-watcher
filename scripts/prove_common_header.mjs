// Source-only browser regression. Build a fixture, serve its output with Vite,
// then run window.proveHeader() and window.proveFiltersLayout() in Chromium
// and WebKit (no native IPC). For native focus coverage call
// prepareHeaderTab(), send its returned key through the browser, then checkHeaderTab().
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { stripTypeScriptTypes, createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const repo = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = resolve(process.argv[2]);
const phase = process.argv[3] || "candidate";
const require = createRequire(join(repo, "desktop/package.json"));
const { parseAst } = await import(pathToFileURL(require.resolve("vite")));
const source = stripTypeScriptTypes(readFileSync(process.env.HEADER_MAIN || join(repo, "desktop/src/main.ts"), "utf8"));
const ast = parseAst(source);
const htmlNode = ast.body.find(n => n.type === "ExpressionStatement" && n.expression.type === "AssignmentExpression"
  && source.slice(n.expression.left.start, n.expression.left.end) === "app.innerHTML");
if (htmlNode.expression.right.expressions.length) throw Error("Unexpected HTML substitutions");
const html = htmlNode.expression.right.quasis[0].value.cooked;
const showView = ast.body.find(n => n.type === "FunctionDeclaration" && n.id?.name === "showView");
const tabs = ["inbox", "watchlist", "vendors", "expiry-ledger", "health", "settings"];
const tabNames = tabs.map(v => v.replace(/-([a-z])/g, (_, c) => c.toUpperCase()) + "Tab");
const handlers = ast.body.filter(n => n.type === "ExpressionStatement"
  && n.expression.type === "CallExpression" && n.expression.callee.property?.name === "addEventListener"
  && tabNames.includes(n.expression.callee.object?.name));
if (handlers.length !== tabs.length) throw Error("Missing production navigation listeners");
const headerCall = ast.body.find(n => n.type === "ExpressionStatement"
  && n.expression.type === "CallExpression" && n.expression.callee.name === "observeCommonHeader");
const filtersCall = ast.body.find(n => n.type === "ExpressionStatement"
  && n.expression.type === "CallExpression" && n.expression.callee.name === "positionInboxFilters");
const imports = `
import {InboxThreadEntry} from '/@fs/${repo}/desktop/src/inboxThreadEntry.ts';
import {ThreadReadClient} from '/@fs/${repo}/desktop/src/threadReads.ts';
import {ThreadHistory} from '/@fs/${repo}/desktop/src/threadHistory.ts';
${headerCall ? `import {observeCommonHeader} from '/@fs/${repo}/desktop/src/commonHeader.ts';` : ""}
${filtersCall ? `import {positionInboxFilters} from '/@fs/${repo}/desktop/src/inboxFilters.ts';` : ""}
`;
const fixture = `
const requiredElement = selector => {const e=document.querySelector(selector);if(!e)throw Error(selector);return e;};
const [inboxTab,watchlistTab,vendorsTab,expiryLedgerTab,healthTab,settingsTab]=${JSON.stringify(tabs)}.map(v=>requiredElement('#'+v+'-tab'));
const [inboxView,watchlistView,vendorsSection,expiryLedgerView,healthView,settingsView]=${JSON.stringify(tabs)}.map(v=>requiredElement('#'+v+'-view'));
const inboxFilterForm=requiredElement('#inbox-filter-form');
const inboxFiltersToggle=requiredElement('#inbox-filters-toggle');
const calls=[],configurationReady=true,refreshInboxThreadControls=()=>{};
const loadVendors=async()=>calls.push('vendors'),refreshConnectStatus=async()=>calls.push('connect'),
loadExpiryLedger=async()=>calls.push('expiry'),coiSetup={refresh:async()=>calls.push('coi')},
loadSettings=async()=>calls.push('settings'),loadAutostart=async()=>calls.push('autostart'),
loadCalendarConsents=async()=>calls.push('calendar'),loadGmailLabelState=async()=>calls.push('labels');
const openConnectHealth=()=>{showView('health');calls.push('health');};
const scope={mutation_epoch:1,mailbox_operation_revision:1};
const entry=new InboxThreadEntry({root:requiredElement('#inbox-thread-view'),columns:requiredElement('#inbox-message-pane'),
 heading:requiredElement('#inbox-heading'),client:new ThreadReadClient(async()=>{throw Error('Unexpected thread read');}),
 accounts:()=>[],snapshot:()=>({configured:true,visible:!inboxView.hidden,blocked:false,queryEpoch:1,itemsEpoch:1,items:[],scope,spanScope:scope}),
 refreshInbox:async()=>{throw Error('Unexpected Inbox reload');},scrollElements:()=>[requiredElement('#inbox-results')],
 layout:{grid:requiredElement('#inbox-columns'),readerPane:requiredElement('#inbox-reading-pane'),placeholder:requiredElement('#inbox-reader-empty'),
 source:requiredElement('#inbox-reader-source'),senders:requiredElement('#inbox-senders-pane'),senderToggle:requiredElement('#inbox-senders-toggle'),
 senderContent:requiredElement('#inbox-senders-content'),footer:requiredElement('#inbox-history-actions')}});
const threadHistory=new ThreadHistory({resume:async()=>{},suspend(){},refresh:async()=>{},beginMutation(){},endMutation:async()=>{}},entry,()=>{});
// Synthetic retained rows create real overflow for the production pane owner.
for(const selector of ['#inbox-list','#inbox-sender-results'])for(let i=0;i<80;i++){
 const row=document.createElement(selector==='#inbox-list'?'li':'button');row.textContent='Synthetic retained message '+i+' sender@example.test';
 requiredElement(selector).append(row);
}
${headerCall ? source.slice(headerCall.start, headerCall.end) : ""}
${filtersCall ? source.slice(filtersCall.start, filtersCall.end) : ""}
${source.slice(showView.start, showView.end)}
${handlers.map(n => source.slice(n.start, n.end)).join("\n")}
window.fixture={calls,entry,tabs:${JSON.stringify(tabs)},showView};
window.proveHeader=${proveHeader.toString()};
window.proveFiltersLayout=${proveFiltersLayout.toString()};
window.prepareHeaderTab=${prepareHeaderTab.toString()};
window.checkHeaderTab=${checkHeaderTab.toString()};
window.fixtureReady=true;
`;
mkdirSync(output, { recursive: true, mode: 0o700 });
for (const [extension, text] of [["html", `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="./${phase}.css"><div id="app">${html}</div><script type="module" src="./${phase}.js"></script>`],
  ["js", imports + fixture], ["css", readFileSync(process.env.HEADER_STYLES || join(repo, "desktop/src/styles.css"), "utf8")]]) {
  writeFileSync(join(output, phase + "." + extension), text, { mode: 0o600 });
}
writeFileSync(join(output, "vite.config.mjs"), `export default {server:{host:'127.0.0.1',port:1510,strictPort:true,fs:{allow:${JSON.stringify([repo, output])}}}};`, { mode: 0o600 });
console.log(JSON.stringify({ repo, output, phase, productionNavigationHandlers: handlers.length, productionHeaderOwner: !!headerCall }));

async function proveHeader() {
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const settle = async () => { await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))); };
  const rect = e => { const b = e.getBoundingClientRect(); return [b.x, b.y, b.width, b.height]; };
  const nav = document.querySelector('.view-tabs');
  const tabs = [...nav.querySelectorAll('button')];
  const rows = [];
  let expected;
  for (const view of fixture.tabs) {
    fixture.showView(view); scrollTo(0, 0); await settle();
    const bounds = rect(nav);
    if (!expected) expected = bounds;
    check(bounds.every((n, i) => Math.abs(n - expected[i]) < 1), 'view-dependent header bounds: ' + view);
    check(tabs.map(b => b.textContent).join('|') === 'Inbox|Watchlist|Vendors|Expiry Ledger|Health|Settings', 'destination labels/order');
    check(tabs.filter(b => b.getAttribute('aria-pressed') === 'true')[0]?.id === view + '-tab', 'selected accessibility');
    for (const tab of tabs) {
      const b = tab.getBoundingClientRect();
      check(b.x >= 0 && b.right <= innerWidth + 1 && b.y >= 0 && b.bottom <= innerHeight, 'unreachable destination: ' + tab.id);
      check(b.height >= 40, 'tiny destination: ' + tab.id);
    }
    const content = document.querySelector('#' + view + '-view');
    const spacer = document.createElement('div'); spacer.style.height = '1800px'; content.append(spacer);
    scrollTo(0, 400); await settle();
    const scrolled = rect(nav);
    check(scrolled.every((n, i) => Math.abs(n - expected[i]) < 1), 'header moved during content scrolling: ' + view);
    spacer.remove(); scrollTo(0, 0); await settle();
    rows.push({view, bounds, scrolled, contentWidth:content.getBoundingClientRect().width});
  }
  fixture.showView('inbox'); await settle();
  const grid = document.querySelector('#inbox-columns');
  const pane = document.querySelector('#inbox-results');
  const before = fixture.calls.length;
  dispatchEvent(new Event('resize')); await settle();
  check(fixture.calls.length === before, 'layout triggered domain reads');
  if (grid.dataset.fit === 'true') {
    check(grid.getBoundingClientRect().bottom <= innerHeight + 1, 'fitted Inbox exceeds viewport');
    check(pane.scrollHeight > pane.clientHeight, 'fixture did not exercise pane overflow');
  }
  check(rows.find(r=>r.view==='settings').contentWidth <= 760, 'Settings widened');
  // Native scrollIntoView/focus must clear the measured sticky header.
  const input=document.querySelector('#inbox-keyword');
  input.scrollIntoView({block:'start'}); input.focus(); await settle();
  const header=document.querySelector('.app-header');
  check(header && header.parentElement === document.querySelector('#app'), 'header still inside content shell');
  check(document.querySelector('#inbox-help')?.textContent === 'Locally retained messages from watched senders and Gmail labels appear here. Followed threads and Sent replies to vendors also appear when captured with Connect.', 'meaningful Inbox help lost or moved out of content');
  check(input.getBoundingClientRect().top >= header.getBoundingClientRect().bottom, 'focused content covered by header');
  return {pass:true,viewport:[innerWidth,innerHeight],textSize:getComputedStyle(document.documentElement).fontSize,
    rows,layout:grid.dataset.layout,fit:grid.dataset.fit,paneHeight:pane.clientHeight,headerHeight:header.getBoundingClientRect().height,layoutReads:fixture.calls.length-before};
}

async function proveFiltersLayout() {
  const select = selector => document.querySelector(selector);
  const form = select("#inbox-filter-form");
  const trigger = select("#inbox-filters-toggle");
  const close = select("#inbox-filters-close");
  const sender = select("#inbox-sender");
  const account = select("#inbox-account");
  const header = select(".app-header");
  const viewport = document.documentElement;
  const panes = ["#inbox-columns", "#inbox-sender-results", "#inbox-results", "#inbox-reading-pane"].map(select);
  const settle = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const workspace = () => JSON.stringify({
    document: [viewport.scrollHeight, viewport.scrollWidth, scrollY, scrollX],
    panes: panes.map(pane => {
      const { x, y, width, height } = pane.getBoundingClientRect();
      return [x, y, width, height, pane.scrollTop, pane.scrollLeft];
    }),
    calls: fixture.calls.length,
  });
  fixture.showView("inbox");
  account.add(new Option("Example Bookkeeping Account — owner@example.test", "account-fixture", true, true));
  sender.value = "unapplied draft";
  trigger.focus();
  await settle();
  panes.forEach(pane => { pane.scrollTop = 40; });
  const before = workspace();
  check(!form.matches(":popover-open"), "Filters should start closed");
  trigger.click();
  await settle();
  check(workspace() === before, "opening Filters moved the workspace or started a read");
  const bounds = form.getBoundingClientRect();
  check(bounds.height > 0 && bounds.top >= header.getBoundingClientRect().bottom
    && bounds.bottom <= viewport.clientHeight + 1 && bounds.left >= 0
    && bounds.right <= viewport.clientWidth + 1, "Filters is outside the visible space below the header");
  check(form.scrollWidth <= form.clientWidth + 1, "Filters has horizontal overflow");
  form.scrollTop = form.scrollHeight;
  await settle();
  check(workspace() === before, "scrolling Filters moved the workspace");
  close.click();
  await settle();
  check(workspace() === before && form.getClientRects().length === 0, "closing Filters moved the workspace");
  check(document.activeElement === trigger && sender.value === "unapplied draft", "closing Filters lost focus or draft");
  trigger.click();
  fixture.showView("watchlist");
  check(form.getClientRects().length === 0, "Filters remained visible after leaving Inbox");
  return { pass: true, viewport: [viewport.clientWidth, viewport.clientHeight], scrollY };
}

async function prepareHeaderTab(id, from, backwards = false, overlap = 16) {
  const pane = document.getElementById(id);
  const start = document.getElementById(from);
  const header = document.querySelector(".app-header");
  const settle = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  fixture.showView("inbox");
  await settle();
  if (id === "inbox-sender-results" && document.getElementById("inbox-senders-content").hidden) {
    document.getElementById("inbox-senders-toggle").click();
    await settle();
  }
  if (!pane.getClientRects().length || !start.getClientRects().length) throw Error("Tab fixture target is hidden");
  const spacer = document.createElement("div");
  spacer.style.height = "520px";
  document.getElementById("inbox-view").append(spacer);
  const sample = () => ({
    target: pane.id, active: document.activeElement.id,
    paneTop: pane.getBoundingClientRect().top, headerBottom: header.getBoundingClientRect().bottom,
    paneScroll: pane.scrollTop, y: scrollY, calls: fixture.calls.length,
  });
  start.focus({ preventScroll: true });
  await settle();
  pane.scrollTop = 80;
  scrollBy(0, pane.getBoundingClientRect().top - header.getBoundingClientRect().bottom + overlap);
  await settle();
  const before = sample();
  if (before.active !== from || (overlap > 0 && before.paneTop >= before.headerBottom)) {
    throw Error("Tab fixture did not reach its starting position");
  }
  let native;
  pane.addEventListener("focus", () => { native = sample(); }, { once: true });
  const corrections = [];
  const scroll = window.scrollBy;
  window.scrollBy = function (...args) {
    corrections.push(sample());
    return scroll.apply(this, args);
  };
  fixture.focusProbe = { sample, before, settle, spacer, native: () => native, corrections, scroll };
  return { before, key: backwards ? "Shift+Tab" : "Tab" };
}

async function checkHeaderTab() {
  const { sample, before, settle, spacer, native, corrections, scroll } = fixture.focusProbe;
  await settle();
  const after = sample();
  window.scrollBy = scroll;
  spacer.remove();
  const nativeFocus = native();
  const evidence = JSON.stringify({ before, nativeFocus, after });
  if (after.active !== after.target) throw Error("Native Tab reached the wrong target: " + evidence);
  if (after.paneTop < after.headerBottom + 4) throw Error("Native Tab leaves the focus outline under the sticky header: " + evidence);
  if (after.calls !== before.calls || after.paneScroll !== before.paneScroll) {
    throw Error("Focus clearance changed calls or pane scroll: " + evidence);
  }
  if (corrections.some(position => position.paneTop >= position.headerBottom + 12)) {
    throw Error("Already-clear native focus caused an extra document scroll: " + evidence);
  }
  return { before, nativeFocus, after, corrections, pass: true };
}
