import assert from "node:assert/strict";
import test from "node:test";

test("the account line names an unavailable Sent folder and nothing else about it", async () => {
  const { mailAccountDetail } = await import("../src/mailAccountDetail.ts").catch(() => ({}));
  assert.equal(typeof mailAccountDetail, "function", "mailAccountDetail export is required");
  const base = { display_name: "IMAP", active: true, connected: true };
  assert.equal(mailAccountDetail(base), "IMAP · Active · Connected");
  assert.equal(
    mailAccountDetail({ ...base, sent_scope: "unavailable" }),
    "IMAP · Active · Connected · Sent mail unavailable",
  );
  assert.equal(mailAccountDetail({ ...base, sent_scope: "available" }), "IMAP · Active · Connected");
  assert.equal(mailAccountDetail({ ...base, sent_scope: "not_polled" }), "IMAP · Active · Connected");
  assert.equal(
    mailAccountDetail({ ...base, active: false, connected: false, sent_scope: null }),
    "IMAP · Retained · Disconnected",
  );
});
