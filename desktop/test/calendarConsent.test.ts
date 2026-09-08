import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  CALENDAR_CONSENT_PROFILES,
  calendarConsentControls,
  calendarConsentStateLabel,
  calendarConsentVisible,
  type CalendarConsentStatus,
} from "../src/calendarConsent.ts";

function status(overrides: Partial<CalendarConsentStatus> = {}): CalendarConsentStatus {
  return {
    account_id: "microsoft365-account",
    available: false,
    entitlement_active: true,
    profile: "read",
    scope: "Calendars.Read",
    state: "not_requested",
    ...overrides,
  };
}

test("calendar setup visibility distinguishes entitlement from existing grants", () => {
  assert.equal(calendarConsentVisible(status()), true);
  assert.equal(
    calendarConsentVisible(status({ entitlement_active: false, state: "not_requested" })),
    false,
  );
  assert.equal(
    calendarConsentVisible(status({ entitlement_active: false, state: "ready" })),
    true,
  );
  assert.equal(
    calendarConsentVisible(status({ entitlement_active: false, state: "revoked" })),
    true,
  );
});

test("calendar consent controls keep setup gated and revocation reachable", () => {
  assert.deepEqual(calendarConsentControls(status(), true), {
    connectVisible: true,
    connectEnabled: true,
    connectLabel: "Authorize",
    disconnectVisible: false,
  });
  assert.deepEqual(
    calendarConsentControls(
      status({ entitlement_active: false, state: "ready", available: false }),
      false,
    ),
    {
      connectVisible: false,
      connectEnabled: false,
      connectLabel: "Reconnect",
      disconnectVisible: true,
    },
  );
  assert.deepEqual(
    calendarConsentControls(status({ state: "consent_pending" }), false),
    {
      connectVisible: true,
      connectEnabled: false,
      connectLabel: "Continue",
      disconnectVisible: true,
    },
  );
});

test("available consent suppresses duplicate authorization and reports ready", () => {
  const ready = status({ state: "ready", available: true });
  assert.equal(calendarConsentControls(ready, true).connectVisible, false);
  assert.equal(calendarConsentStateLabel(ready), "Ready");
  assert.equal(
    calendarConsentStateLabel(status({ state: "ready", available: false })),
    "Consent saved; unavailable",
  );
});

test("calendar profile copy keeps write effects explicit and separate", () => {
  assert.deepEqual(
    CALENDAR_CONSENT_PROFILES.map(({ profile }) => profile),
    ["read", "proposal", "write"],
  );
  const write = CALENDAR_CONSENT_PROFILES.find(({ profile }) => profile === "write");
  assert.match(write?.effectNote ?? "", /send invitations/i);
  assert.match(write?.description ?? "", /explicit confirmation/i);
  for (const profile of CALENDAR_CONSENT_PROFILES.filter(({ profile }) => profile !== "write")) {
    assert.match(profile.effectNote, /Read-only/);
  }
});

test("desktop invokes only the typed calendar consent bridge and renders engine text safely", async () => {
  const source = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
  assert.match(source, /invoke<CalendarConsentStatus>\("calendar_consent_status"/);
  assert.match(source, /invoke<CalendarConsentStatus>\(`calendar_consent_\$\{action\}`/);
  assert.match(source, /state\.textContent = `\$\{calendarConsentStateLabel\(status\)\} · Scope:/);
  assert.match(source, /accountTitle\.textContent = account\.address \|\| account\.display_name/);
  assert.match(source, /mailbox reading remains connected/);
});
