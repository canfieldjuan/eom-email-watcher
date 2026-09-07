import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

test("calendar proposals render as native non-writing inbox content", () => {
  assert.match(source, /if \(item\.calendar_proposal\)/);
  assert.match(source, /textContent = "Calendar proposal"/);
  assert.match(source, /textContent = "No calendar event has been created\."/);
  assert.match(source, /proposal\.empty_reason \|\| "No meeting time satisfied the request\."/);
  assert.match(
    source,
    /formatter\.format\(new Date\(proposal\.end\)\)\} \(\$\{proposal\.timezone\}\)/,
  );
  assert.match(source, /attendees\.textContent = proposal\.attendees\.length/);
  assert.match(source, /calendar\.textContent = `Calendar:/);
  assert.match(styles, /\.calendar-proposal\s*\{/);
});
