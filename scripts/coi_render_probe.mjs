/** Execute the embedded production renderer in a minimal DOM, including reload.
 * This records cell text; it does not establish browser layout or installed IPC.
 */
import { runInNewContext } from 'node:vm';

export function renderSavedHtml(html) {
  const inline = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (inline.length !== 1) throw new Error('Expected one embedded executable renderer');
  const element = () => ({
    children: [], dataset: {}, textContent: '', hidden: false,
    append(...values) { this.children.push(...values); },
    replaceChildren() { this.children = []; },
    addEventListener(_name, callback) { this.reload = callback; },
    reload: () => {},
  });
  const elements = Object.fromEntries([
    'expiry-ledger-rows', 'expiry-ledger-table-wrap', 'expiry-ledger-status', 'reload',
  ].map(id => [id, element()]));
  runInNewContext(inline[0][1], { document: {
    getElementById: id => elements[id], createElement: element,
  } }, { timeout: 5000 });
  const rows = () => elements['expiry-ledger-rows'].children.map(row =>
    row.children.map(cell => cell.textContent));
  const initial = rows();
  elements.reload.reload();
  return { initial, after_reload: rows() };
}
