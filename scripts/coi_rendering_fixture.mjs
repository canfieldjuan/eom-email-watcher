/** Build a source-rendering fixture from production markup/functions and an engine response.
 * This is explicitly not an installed Tauri proof. No renderer implementation is copied here.
 * Usage: node scripts/coi_rendering_fixture.mjs ENGINE_RESPONSE_JSON NEW_OUTPUT_DIRECTORY
 * Requires Python 3 (COI_PROOF_PYTHON overrides the interpreter).
 * Open NEW_OUTPUT_DIRECTORY/index.html directly. All data, code and styles are embedded.
 */
import { readFile, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { renderSavedHtml } from './coi_render_probe.mjs';
import { createHash } from 'node:crypto';
import { stripTypeScriptTypes } from 'node:module';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

const [responsePath, requestedOutputPath] = process.argv.slice(2);
if (!responsePath || !requestedOutputPath) throw new Error('Engine response and new output directory required');
const root = fileURLToPath(new URL('../', import.meta.url));
const source = await readFile(resolve(root, 'desktop/src/main.ts'), 'utf8');
const responseText = await readFile(responsePath, 'utf8');
const response = JSON.parse(responseText);
if (response.ok !== true || !Array.isArray(response.data?.items)) {
  throw new Error('Expected a successful certificate.expiry_ledger.list response');
}
// Validate the proof's input coverage before writing any artifact. The production
// renderer below remains the owner of labels and rendering behavior.
const requiredExpiry = ['expired', 'expires_today', 'upcoming', 'review'];
const requiredReview = ['extracted', 'needs_review'];
const expiryStates = new Set();
const reviewStates = new Set();
for (const row of response.data.items) {
  if (!row || !requiredExpiry.includes(row.expiry_status) || !requiredReview.includes(row.review_state)) {
    throw new Error('Invalid expiry/review state in engine response');
  }
  expiryStates.add(row.expiry_status);
  reviewStates.add(row.review_state);
}
if (requiredExpiry.some(state => !expiryStates.has(state)) || requiredReview.some(state => !reviewStates.has(state))) {
  throw new Error('Engine response must cover all expiry and review states');
}
function between(start, end) {
  const a = source.indexOf(start);
  const b = source.indexOf(end, a);
  if (a < 0 || b < a || source.indexOf(start, a + start.length) >= 0) {
    throw new Error('Production renderer boundary changed; inspect before updating this fixture');
  }
  return source.slice(a, b);
}
const markup = between('    <section id="expiry-ledger-view"', '\n    <section id="health-view"')
  .replace('aria-labelledby="expiry-ledger-tab" hidden', 'aria-label="Expiry Ledger"');
const functions = stripTypeScriptTypes(between('function expiryStatusLabel(', 'async function loadExpiryLedger('));
// Escape HTML's script terminator at the embedding boundary, without changing
// the saved response or relying on fetch/module loading from a file: document.
const embeddedResponse = JSON.stringify(responseText).replaceAll('<', '\\u003c');
const adapter = `
const expiryLedgerRows = document.getElementById('expiry-ledger-rows');
const expiryLedgerTableWrap = document.getElementById('expiry-ledger-table-wrap');
const expiryLedgerStatus = document.getElementById('expiry-ledger-status');
${functions}
function loadSavedResponse() {
  const result = JSON.parse(${embeddedResponse});
  renderExpiryLedger(result.data.items);
}
document.getElementById('reload').addEventListener('click', loadSavedResponse);
loadSavedResponse();
`;
const styles = await readFile(resolve(root, 'desktop/src/styles.css'), 'utf8');
const html = `<!doctype html><html lang="en"><meta charset="utf-8"><title>COI source rendering proof</title>
<style>${styles}</style><body><main style="padding:24px">
<p>Source rendering proof using a saved engine response. Installed desktop integration is not exercised.</p>
<button id="reload">Reload saved engine response</button>${markup}
</main><script>${adapter}</script></body></html>`;
const rendered = renderSavedHtml(html);
// The Python owner admits and creates the destination for both proof tools.
const outputPath = JSON.parse(execFileSync(
  process.env.COI_PROOF_PYTHON ?? (process.platform === 'win32' ? 'python' : 'python3'),
  [resolve(root, 'scripts/coi_evidence.py'), requestedOutputPath],
  { encoding: 'utf8' },
));
const hash = value => createHash('sha256').update(value).digest('hex');
for (const [name, content] of Object.entries({
  'index.html': html, 'engine-response.json': responseText,
  'rendered-rows.json': JSON.stringify(rendered, null, 2),
  'source-receipt.json': JSON.stringify({source_sha256: hash(source), styles_sha256: hash(styles), response_sha256: hash(responseText),
    artifact_sha256: hash(html),
    expiry_states: [...expiryStates].sort(), review_states: [...reviewStates].sort(),
    scope: 'production renderer and markup; saved engine response; no Tauri IPC or installed shell'}, null, 2),
})) await writeFile(resolve(outputPath, name), content, { mode: 0o600, flag: 'wx' });
console.log('Rendering fixture written with production source and response receipts');
