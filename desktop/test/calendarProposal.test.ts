import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { stripTypeScriptTypes } from "node:module";
import { calendarOutcomeNotice, latestCalendarOutcome, type CalendarWriteOutcome } from "../src/calendarOutcome.ts";

const source = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

test("calendar proposals render exact native confirmation controls", () => {
  assert.match(source, /if \(item\.calendar_proposal\)/);
  assert.match(source, /textContent = "Calendar proposal"/);
  assert.match(source, /subject\.textContent = `Event title: \$\{proposal\.subject\}`/);
  assert.equal(calendarOutcomeNotice(null, "proposal").text, "No calendar event has been created.");
  assert.match(source, /proposal\.empty_reason \|\| "No meeting time satisfied the request\."/);
  assert.match(source, /let inboxProposalExpiryTimer: number \| null = null/);
  assert.match(source, /window\.clearTimeout\(inboxProposalExpiryTimer\)/);
  assert.match(source, /const renderStartedAt = Date\.now\(\)/);
  assert.match(source, /proposalExpiresAt > renderStartedAt/);
  assert.match(source, /Math\.max\(nextProposalExpiry - Date\.now\(\) \+ 1, 0\)/);
  assert.match(source, /inboxProposalExpiryTimer = window\.setTimeout/);
  assert.match(source, /renderInbox\(inboxItems\)/);
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
    /proposal\.state === "awaiting_confirmation" &&\s+hasSuggestion &&\s+!expired &&\s+proposal\.attendees\.length > 0/,
  );
  assert.match(source, /invitationWarning\.hidden = !showInvitationWarning/);
  assert.match(source, /Creating this event will send meeting invitations from/);
  assert.match(source, /attendees,\s+invitationWarning,\s+calendar/);
  assert.match(source, /calendar\.textContent = `Calendar owner:/);
  assert.match(source, /Account identity: \$\{proposal\.account_id\}/);
  assert.match(source, /location\.textContent = "Location: Not specified"/);
  assert.match(source, /onlineMeeting\.textContent = "Teams link: No"/);
  assert.match(source, /proposal\.state === "awaiting_confirmation" && hasSuggestion/);
  assert.doesNotMatch(
    source,
    /proposal\.state === "awaiting_confirmation" && hasSuggestion && proposal\.attendees\.length > 0/,
  );
  assert.match(source, /confirm\.textContent = expired \? "Recheck proposal" : "Create event"/);
  assert.match(source, /otherwise it will refresh the proposal without creating an event/);
  assert.match(source, /decline\.textContent = "Decline"/);
  assert.match(source, /This will send meeting invitations from/);
  assert.equal(calendarOutcomeNotice(null, "proposal", "writing").text,
    "The write result is uncertain. Email Watcher will reconcile it without creating a second event.");
  assert.match(source, /invoke<CalendarDecisionResult>\("calendar_proposal_decide"/);
  assert.match(source, /proposalSha256: proposal\.proposal_sha256/);
  assert.match(source, /proposalVersion: proposal\.proposal_version/);
  assert.match(source, /stateVersion: proposal\.state_version/);
  assert.equal(calendarOutcomeNotice(null, "decision", "failed").text,
    "Microsoft rejected the calendar event creation.");
  assert.match(source, /await loadInbox\(\);[\s\S]*?latestCalendarOutcome\(result\.calendar_write_outcome, refreshed\?\.calendar_write_outcome\)/);
  assert.match(source, /inboxStatus\.dataset\.kind = "warning"/);
  assert.match(styles, /\.calendar-proposal\s*\{/);
  assert.match(styles, /\.calendar-proposal-invitation-warning\s*\{/);
  assert.match(styles, /\.calendar-proposal-actions\s*\{/);
});


const cancelled: CalendarWriteOutcome = {
  run_id: "11111111-1111-4111-8111-111111111111", state_version: 6,
  transaction_id: "22222222-2222-4222-8222-222222222222", status: "cancelled",
  failure_code: "thread_unfollowed", graph_event_id: null,
};

test("calendar outcome formatter distinguishes unfollow, generic loss and provider outcomes", () => {
  for (const surface of ["proposal", "decision"] as const) {
    assert.equal(calendarOutcomeNotice(cancelled, surface).text, "Not added: you unfollowed this thread");
    for (const failure_code of [null, "source_unavailable", "", "thread_unfollowed "]) {
      assert.equal(calendarOutcomeNotice({ ...cancelled, failure_code }, surface).text,
        "No calendar event has been created.");
    }
    assert.notEqual(calendarOutcomeNotice({ ...cancelled, status: "writing" }, surface).text,
      "Not added: you unfollowed this thread");
  }
  for (const state of ["writing", "unresolved", "reconciling"] as const) {
    assert.equal(calendarOutcomeNotice({ ...cancelled, status: state }, "proposal").text,
      "The write result is uncertain. Email Watcher will reconcile it without creating a second event.");
  }
  assert.equal(calendarOutcomeNotice({ ...cancelled, status: "completed" }, "proposal").text,
    "The calendar event was created.");
});

test("production calendar segment renders outcome without payload or confirmation action", () => {
  const start = source.indexOf("let calendarProposal: HTMLElement | null = null;");
  const end = source.indexOf('const attachments = document.createElement("ul");', start);
  assert.ok(start >= 0 && end > start);
  const segment = stripTypeScriptTypes(source.slice(start, end) + "globalThis.rendered = calendarProposal;");
  for (const outcome of [cancelled, { ...cancelled, status: "completed" }, { ...cancelled, status: "unresolved" }]) {
    const nodes: any[] = [];
    const context: any = {
      item: { calendar_proposal: null, calendar_write_outcome: outcome }, calendarOutcomeNotice,
      document: { createElement(tag: string) {
        const node = { tag, textContent: "", children: [] as any[],
          set innerHTML(_value: string) { assert.fail("calendar status must render text"); },
          setAttribute() {}, append(...children: any[]) { this.children.push(...children); } };
        nodes.push(node);
        return node;
      } },
    };
    runInNewContext(segment, context);
    assert.equal(context.rendered.children[0].textContent, calendarOutcomeNotice(outcome as CalendarWriteOutcome, "proposal").text);
    assert.equal(nodes.filter(node => node.tag === "button").length, 0);
    assert.equal(nodes.some(node => node.textContent.includes("Meeting request")), false);
  }
});


test("calendar decision refresh cannot overwrite a newer durable outcome", async () => {
  const reply = { ...cancelled, status: "unresolved" as const };
  const completed = { ...reply, status: "completed" as const, state_version: 9, graph_event_id: "event" };
  assert.equal(latestCalendarOutcome(completed, reply), completed);
  assert.equal(latestCalendarOutcome(reply, { ...completed, run_id: "foreign" }), reply);
  const start = source.indexOf('const result = await invoke<CalendarDecisionResult>("calendar_proposal_decide"');
  const end = source.indexOf("} catch (error)", start);
  assert.ok(start >= 0 && end > start);
  const segment = stripTypeScriptTypes(`(async () => { ${source.slice(start, end)} })()`);
  const status = { textContent: "", dataset: {} as Record<string, string> };
  let refreshed = false;
  await runInNewContext(segment, {
    item: { message_id: "m" }, proposal: {}, decision: "confirm",
    invoke: async () => ({ state: "unresolved", calendar_write_outcome: reply }),
    loadInbox: async () => { refreshed = true; },
    inboxItems: [{ message_id: "m", calendar_write_outcome: completed }],
    inboxStatus: status, calendarOutcomeNotice, latestCalendarOutcome,
  });
  assert.equal(refreshed, true);
  assert.equal(status.textContent, "Calendar event created.");
  assert.equal(status.dataset.kind, "success");
  assert.doesNotMatch(source.slice(start, source.indexOf('decline.addEventListener', start)),
    /(?:confirm|decline)\.disabled = false/);
});


test("fresh proposal decisions retain their wording over an older cancelled admission", () => {
  assert.equal(calendarOutcomeNotice(cancelled, "proposal", "declined").text,
    "You declined this calendar proposal. No event was created.");
  assert.equal(calendarOutcomeNotice(cancelled, "decision", "declined").text,
    "Calendar proposal declined.");
  assert.equal(calendarOutcomeNotice(cancelled, "proposal", "awaiting_confirmation").text,
    "No calendar event has been created.");
});
