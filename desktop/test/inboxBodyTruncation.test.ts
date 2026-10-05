import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const load = async () => {
  const { inboxBodyTruncation } = await import("../src/inboxBodyTruncation.ts").catch(() => ({}));
  assert.equal(typeof inboxBodyTruncation, "function", "inboxBodyTruncation export is required");
  return inboxBodyTruncation;
};

test("a truncated analysis gets a badge and a note with grouped counts", async () => {
  const inboxBodyTruncation = await load();
  assert.deepEqual(
    inboxBodyTruncation(
      { body_truncated: true, body_analyzed_chars: 20000, body_source_chars: 54321 },
      "en-US",
    ),
    {
      badge: "Partial summary",
      note:
        "Summary based on the first 20,000 of 54,321 characters. " +
        "Read the full email in your mail app.",
    },
  );
});

test("whole, unknown, and legacy analyses render nothing", async () => {
  const inboxBodyTruncation = await load();
  assert.equal(
    inboxBodyTruncation({ body_truncated: false, body_analyzed_chars: 120, body_source_chars: 120 }),
    null,
  );
  assert.equal(
    inboxBodyTruncation({ body_truncated: null, body_analyzed_chars: null, body_source_chars: null }),
    null,
  );
  assert.equal(inboxBodyTruncation({}), null);
});

test("malformed counts never render a partial summary", async () => {
  const inboxBodyTruncation = await load();
  for (const counts of [
    { body_analyzed_chars: null, body_source_chars: 10 },
    { body_analyzed_chars: 10, body_source_chars: null },
    { body_analyzed_chars: 10, body_source_chars: 10 },
    { body_analyzed_chars: 11, body_source_chars: 10 },
    { body_analyzed_chars: -1, body_source_chars: 10 },
    { body_analyzed_chars: 1.5, body_source_chars: 10 },
    { body_analyzed_chars: "10", body_source_chars: 20 },
  ]) {
    assert.equal(
      inboxBodyTruncation({ body_truncated: true, ...counts } as never),
      null,
      JSON.stringify(counts),
    );
  }
});

test("the inbox renders the badge in the row and the note in the card as text", () => {
  const source = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
  assert.match(source, /import \{ inboxBodyTruncation \} from "\.\/inboxBodyTruncation";/);
  assert.match(source, /const truncation = inboxBodyTruncation\(item\);/);
  assert.match(source, /truncationNote\.textContent = truncation\.note;/);
  assert.match(source, /if \(truncationNote\) card\.append\(truncationNote\);/);
  assert.match(source, /partial\.textContent = truncation\.badge;\s*rowBadges\.append\(partial\);/);
  assert.doesNotMatch(source, /truncation\.(note|badge)[^;]*innerHTML/);
});
