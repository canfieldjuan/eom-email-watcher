import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import test from "node:test";

const script = fileURLToPath(new URL("../../scripts/coi_rendering_fixture.mjs", import.meta.url));
const statuses = ["expired", "expires_today", "upcoming", "review"];
const rows = () => statuses.map((expiry_status, policy_ordinal) => ({
  policy_id: `policy-${policy_ordinal}`, policy_ordinal, expiry_status,
  review_state: expiry_status === "review" ? "needs_review" : "extracted",
  insured: "Public fixture", source_available: true,
}));

function generate(items: unknown) {
  const dir = mkdtempSync(join(tmpdir(), "coi-render-proof-"));
  const input = join(dir, "response.json");
  const output = join(dir, "output");
  writeFileSync(input, JSON.stringify({ ok: true, data: { items } }), { mode: 0o600 });
  const result = spawnSync(process.execPath, [script, input, output], { encoding: "utf8" });
  return { result, output };
}

for (const missing of statuses) {
  test(`missing ${missing} refuses evidence before writing output`, () => {
    const { result, output } = generate(rows().filter(row => row.expiry_status !== missing));
    assert.notEqual(result.status, 0, "incomplete coverage must fail");
    assert.equal(existsSync(output), false);
  });
}

test("empty and mixed invalid rows refuse evidence", () => {
  for (const items of [[], [rows()[0]], [...rows(), null], [...rows(), { expiry_status: "typo" }]]) {
    const { result, output } = generate(items);
    assert.notEqual(result.status, 0, "invalid coverage must fail");
    assert.equal(existsSync(output), false);
  }
});

test("both review states are required independently of expiry", () => {
  for (const state of ["extracted", "needs_review"]) {
    const { result, output } = generate(rows().map(row => ({ ...row, review_state: state })));
    assert.notEqual(result.status, 0, "incomplete review coverage must fail");
    assert.equal(existsSync(output), false);
  }
});

test("complete fixture renders and reloads from inline data without HTTP or modules", () => {
  const items = rows();
  items[0].insured = '</script><script>throw new Error("injected")</script>';
  const { result, output } = generate(items);
  assert.equal(result.status, 0, result.stderr);
  const html = readFileSync(join(output, "index.html"), "utf8");
  const inline = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  assert.equal(inline.length, 1, "disk-loadable artifact must contain its executable script");
  const element = () => ({
    children: [] as any[], dataset: {} as Record<string, string>, textContent: "", hidden: false,
    append(...values: any[]) { this.children.push(...values); },
    replaceChildren() { this.children = []; },
    addEventListener(_name: string, callback: () => void) { this.reload = callback; },
    reload: () => {},
  });
  const elements = Object.fromEntries([
    "expiry-ledger-rows", "expiry-ledger-table-wrap", "expiry-ledger-status", "reload",
  ].map(id => [id, element()]));
  runInNewContext(inline[0][1], { document: {
    getElementById: (id: string) => elements[id], createElement: element,
  } });
  const observed = () => elements["expiry-ledger-rows"].children.map(row => ({
    expiry: row.children[8].textContent, review: row.children[9].textContent,
  }));
  assert.deepEqual(observed(), [
    { expiry: "Expired", review: "Extracted" },
    { expiry: "Expires today", review: "Extracted" },
    { expiry: "Upcoming", review: "Extracted" },
    { expiry: "Needs review", review: "Needs review" },
  ]);
  assert.equal(elements["expiry-ledger-rows"].children[0].children[1].textContent, items[0].insured);
  elements.reload.reload();
  assert.equal(elements["expiry-ledger-rows"].children.length, items.length);
});
