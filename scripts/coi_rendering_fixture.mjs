/** Build a source-rendering fixture from production markup/functions and an engine response.
 * This is explicitly not an installed Tauri proof. No renderer implementation is copied here.
 * Usage: node scripts/coi_rendering_fixture.mjs ENGINE_RESPONSE_JSON NEW_OUTPUT_DIRECTORY
 */
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { stripTypeScriptTypes } from 'node:module';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

const [responsePath, outputPath] = process.argv.slice(2);
if (!responsePath || !outputPath) throw new Error('Engine response and new output directory required');
const root = fileURLToPath(new URL('../', import.meta.url));
const source = await readFile(resolve(root, 'desktop/src/main.ts'), 'utf8');
const responseText = await readFile(responsePath, 'utf8');
const response = JSON.parse(responseText);
if (response.ok !== true || !Array.isArray(response.data?.items)) {
  throw new Error('Expected a successful certificate.expiry_ledger.list response');
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
const adapter = `
const expiryLedgerRows = document.getElementById('expiry-ledger-rows');
const expiryLedgerTableWrap = document.getElementById('expiry-ledger-table-wrap');
const expiryLedgerStatus = document.getElementById('expiry-ledger-status');
${functions}
async function loadSavedResponse() {
  const response = await fetch('./engine-response.json');
  if (!response.ok) throw new Error('Saved response unavailable');
  const result = await response.json();
  if (result.ok !== true) throw new Error('Engine response failed');
  renderExpiryLedger(result.data.items);
}
document.getElementById('reload').addEventListener('click', loadSavedResponse);
await loadSavedResponse();
`;
const html = `<!doctype html><html lang="en"><meta charset="utf-8"><title>COI source rendering proof</title>
<link rel="stylesheet" href="styles.css"><body><main style="padding:24px">
<p>Source rendering proof using a saved engine response. Installed desktop integration is not exercised.</p>
<button id="reload">Reload saved engine response</button>${markup}
</main><script type="module" src="renderer.js"></script></body></html>`;
await mkdir(outputPath, { mode: 0o700 });
const hash = value => createHash('sha256').update(value).digest('hex');
for (const [name, content] of Object.entries({
  'index.html': html, 'renderer.js': adapter, 'engine-response.json': responseText,
  'styles.css': await readFile(resolve(root, 'desktop/src/styles.css'), 'utf8'),
  'source-receipt.json': JSON.stringify({source_sha256: hash(source), response_sha256: hash(responseText),
    scope: 'production renderer and markup; saved engine response; no Tauri IPC or installed shell'}, null, 2),
})) await writeFile(resolve(outputPath, name), content, { mode: 0o600, flag: 'wx' });
console.log('Rendering fixture written with production source and response receipts');
