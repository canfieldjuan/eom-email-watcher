import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

test("calendar proposals render exact native confirmation controls", () => {
  assert.match(source, /if \(item\.calendar_proposal\)/);
  assert.match(source, /textContent = "Calendar proposal"/);
  assert.match(source, /subject\.textContent = `Event title: \$\{proposal\.subject\}`/);
  assert.match(source, /textContent = "No calendar event has been created\."/);
  assert.match(source, /proposal\.empty_reason \|\| "No meeting time satisfied the request\."/);
  assert.match(
    source,
    /formatter\.format\(new Date\(proposal\.end\)\)\} \(\$\{proposal\.timezone\}\)/,
  );
  assert.match(
    source,
    /Exact interval: \$\{proposal\.start\} – \$\{proposal\.end\}/,
  );
  assert.match(source, /attendees\.textContent = proposal\.attendees\.length/);
  assert.match(
    source,
    /proposal\.state === "awaiting_confirmation" && hasSuggestion && proposal\.attendees\.length > 0/,
  );
  assert.match(source, /invitationWarning\.hidden = !showInvitationWarning/);
  assert.match(source, /Creating this event will send meeting invitations from/);
  assert.match(source, /attendees,\s+invitationWarning,\s+calendar/);
  assert.match(source, /calendar\.textContent = `Calendar owner:/);
  assert.match(source, /Account identity: \$\{proposal\.account_id\}/);
  assert.match(source, /location\.textContent = "Location: Not specified"/);
  assert.match(source, /onlineMeeting\.textContent = "Teams link: No"/);
  assert.match(source, /proposal\.state === "awaiting_confirmation" && hasSuggestion/);
  assert.doesNotMatch(source, /hasSuggestion && !expired/);
  assert.match(source, /confirm\.textContent = expired \? "Recheck proposal" : "Create event"/);
  assert.match(source, /otherwise it will refresh the proposal without creating an event/);
  assert.match(source, /decline\.textContent = "Decline"/);
  assert.match(source, /This will send meeting invitations from/);
  assert.match(
    source,
    /proposal\.state === "writing" \|\|\s+proposal\.state === "unresolved"/,
  );
  assert.match(source, /invoke<CalendarDecisionResult>\("calendar_proposal_decide"/);
  assert.match(source, /proposalSha256: proposal\.proposal_sha256/);
  assert.match(source, /proposalVersion: proposal\.proposal_version/);
  assert.match(source, /stateVersion: proposal\.state_version/);
  assert.match(source, /result\.state === "failed"/);
  assert.match(source, /await loadInbox\(\);\s+inboxStatus\.textContent = statusMessage/);
  assert.match(source, /inboxStatus\.dataset\.kind = "warning"/);
  assert.match(styles, /\.calendar-proposal\s*\{/);
  assert.match(styles, /\.calendar-proposal-invitation-warning\s*\{/);
  assert.match(styles, /\.calendar-proposal-actions\s*\{/);
});
