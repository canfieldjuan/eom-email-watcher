import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, writeFileSync, existsSync, mkdirSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { renderSavedHtml } from "../../scripts/coi_render_probe.mjs";
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
  const rendered = renderSavedHtml(html);
  assert.deepEqual(rendered.initial.map(row => ({ expiry: row[8], review: row[9] })), [
    { expiry: "Expired", review: "Extracted" },
    { expiry: "Expires today", review: "Extracted" },
    { expiry: "Upcoming", review: "Extracted" },
    { expiry: "Needs review", review: "Needs review" },
  ]);
  assert.equal(rendered.initial[0][1], items[0].insured);
  assert.deepEqual(rendered.after_reload, rendered.initial);
});


for (const marker of ["primary", "linked"]) {
  for (const alias of [false, true]) {
    test(`rendering rejects ${marker} worktree output, symlink=${alias}`, () => {
      const dir = mkdtempSync(join(tmpdir(), "coi-private-output-"));
      const repo = join(dir, "repo");
      mkdirSync(repo);
      if (marker === "primary") mkdirSync(join(repo, ".git"));
      else writeFileSync(join(repo, ".git"), "gitdir: /private/metadata");
      const input = join(dir, "response.json");
      writeFileSync(input, JSON.stringify({ ok: true, data: { items: rows() } }));
      const link = join(dir, "alias");
      if (alias) symlinkSync(repo, link, "dir");
      const output = join(alias ? link : repo, "output");
      const result = spawnSync(process.execPath, [script, input, output], { encoding: "utf8" });
      assert.notEqual(result.status, 0, "Git-contained output must be rejected");
      assert.equal(existsSync(output), false);
    });
  }
}

test("receipt binds embedded production stylesheet bytes", () => {
  const { result, output } = generate(rows());
  assert.equal(result.status, 0, result.stderr);
  const receipt = JSON.parse(readFileSync(join(output, "source-receipt.json"), "utf8"));
  const stylesheet = readFileSync(new URL("../src/styles.css", import.meta.url));
  assert.equal(receipt.sources["desktop/src/styles.css"], createHash("sha256").update(stylesheet).digest("hex"));
});

test("receipt binds executable harness sources and checkout HEAD", () => {
  const { result, output } = generate(rows());
  assert.equal(result.status, 0, result.stderr);
  const receipt = JSON.parse(readFileSync(join(output, "source-receipt.json"), "utf8"));
  const root = dirname(dirname(script));
  const head = spawnSync("git", ["-C", root, "rev-parse", "HEAD"], { encoding: "utf8" });
  assert.equal(head.status, 0, head.stderr);
  assert.equal(receipt.watcher_head, head.stdout.trim());
  for (const name of ["scripts/coi_rendering_fixture.mjs", "scripts/coi_render_probe.mjs", "scripts/coi_evidence.py"]) {
    const bytes = readFileSync(join(root, name));
    assert.equal(receipt.sources[name], createHash("sha256").update(bytes).digest("hex"));
  }
});

test("receipt binds every emitted artifact and detects changed rendered observations", () => {
  const { result, output } = generate(rows());
  assert.equal(result.status, 0, result.stderr);
  const receipt = JSON.parse(readFileSync(join(output, "source-receipt.json"), "utf8"));
  const names = ["engine-response.json", "index.html", "rendered-rows.json"];
  assert.deepEqual(Object.keys(receipt.artifacts).sort(), names);
  assert.deepEqual(Object.keys(receipt.artifacts).sort(), readdirSync(output).filter(name => name !== "source-receipt.json").sort());
  for (const name of names) {
    assert.equal(receipt.artifacts[name], createHash("sha256").update(readFileSync(join(output, name))).digest("hex"));
  }
  const path = join(output, "rendered-rows.json");
  for (const changed of ["", "{", JSON.stringify({ initial: [], after_reload: [] })]) {
    writeFileSync(path, changed);
    assert.notEqual(receipt.artifacts["rendered-rows.json"], createHash("sha256").update(readFileSync(path)).digest("hex"));
  }
});


test("generator delegates directory admission and creation to the shared owner", () => {
  const source = readFileSync(script, "utf8");
  assert.ok(source.includes("scripts/coi_evidence.py"));
  assert.doesNotMatch(source, /\bmkdir(?:Sync)?\s*\(/);
});


test("shared directory result preserves escaped and trailing-newline paths", () => {
  const dir = mkdtempSync(join(tmpdir(), "coi-output-encoding-"));
  const input = join(dir, "response.json");
  writeFileSync(input, JSON.stringify({ ok: true, data: { items: rows() } }));
  const output = join(dir, 'quote"back\\slash\n');
  const result = spawnSync(process.execPath, [script, input, output], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  assert.ok(existsSync(join(output, "index.html")));
});
