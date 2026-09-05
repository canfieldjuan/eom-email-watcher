import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const stylesheet = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

test("hidden UI state wins over component display rules", () => {
  assert.match(
    stylesheet,
    /(?:^|\n)\[hidden\]\s*\{[^}]*display:\s*none\s*!important;[^}]*\}/,
  );
});
