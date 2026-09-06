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
