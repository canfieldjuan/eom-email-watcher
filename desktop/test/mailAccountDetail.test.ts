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

test("an unreadable Sent folder shows the recorded pause before stale scope", async () => {
  const { mailAccountDetail } = await import("../src/mailAccountDetail.ts");
  const base = { display_name: "IMAP", active: true, connected: true };
  for (const sent_scope of ["available", "unavailable", "not_polled"] as const) {
    assert.equal(
      mailAccountDetail({ ...base, sent_scope, sent_poll_failure: "unreadable" }),
      "IMAP · Active · Connected · Sent folder can't be opened; thread history is paused",
    );
  }
  assert.equal(
    mailAccountDetail({ ...base, sent_scope: "available", sent_poll_failure: null }),
    "IMAP · Active · Connected",
  );
});


test("Health renders the source owner notice literally and clears after adoption", async () => {
  const { mailAccountDetail } = await import("../src/mailAccountDetail.ts");
  const base = { display_name: "Windows", active: true, connected: true };
  const notice = "Source-lock upgrade pending; oversized thread deletion is deferred <literal>";
  assert.equal(mailAccountDetail({ ...base, source_lock_notice: notice }), `Windows · Active · Connected · ${notice}`);
  assert.equal(mailAccountDetail({ ...base, source_lock_notice: null }), "Windows · Active · Connected");
});
