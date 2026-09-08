export type CalendarConsentProfile = "read" | "proposal" | "write";

export type CalendarConsentState =
  | "not_requested"
  | "consent_pending"
  | "ready"
  | "rejected"
  | "revoked";

export interface CalendarConsentStatus {
  account_id: string;
  available: boolean;
  entitlement_active: boolean;
  profile: CalendarConsentProfile;
  scope: string;
  state: CalendarConsentState;
}

export interface CalendarConsentProfileDefinition {
  profile: CalendarConsentProfile;
  title: string;
  description: string;
  actionLabel: string;
  effectNote: string;
}

export const CALENDAR_CONSENT_PROFILES: readonly CalendarConsentProfileDefinition[] = [
  {
    profile: "read",
    title: "Read calendar",
    description: "Read calendar changes and availability for scheduling requests.",
    actionLabel: "calendar reading",
    effectNote: "Read-only. This cannot create or change calendar events.",
  },
  {
    profile: "proposal",
    title: "Find meeting times",
    description: "Ask Microsoft for candidate meeting times, including shared calendars.",
    actionLabel: "meeting-time proposals",
    effectNote: "Read-only. This cannot create or change calendar events.",
  },
  {
    profile: "write",
    title: "Create calendar events",
    description: "Permit event creation only after a separate explicit confirmation.",
    actionLabel: "event creation",
    effectNote: "Writing an event can send invitations to every listed attendee.",
  },
] as const;

export function calendarConsentVisible(status: CalendarConsentStatus): boolean {
  return status.entitlement_active || status.state !== "not_requested";
}

export function calendarConsentControls(
  status: CalendarConsentStatus,
  mailboxConnected: boolean,
): {
  connectVisible: boolean;
  connectEnabled: boolean;
  connectLabel: string;
  disconnectVisible: boolean;
} {
  const verbs: Record<CalendarConsentState, string> = {
    not_requested: "Authorize",
    consent_pending: "Continue",
    ready: "Reconnect",
    rejected: "Retry",
    revoked: "Reconnect",
  };
  return {
    connectVisible: status.entitlement_active && !status.available,
    connectEnabled: status.entitlement_active && mailboxConnected,
    connectLabel: verbs[status.state],
    disconnectVisible: status.state !== "not_requested",
  };
}

export function calendarConsentStateLabel(status: CalendarConsentStatus): string {
  if (status.state === "ready" && !status.available) return "Consent saved; unavailable";
  const labels: Record<CalendarConsentState, string> = {
    not_requested: "Not authorized",
    consent_pending: "Consent pending",
    ready: "Ready",
    rejected: "Consent rejected",
    revoked: "Consent revoked",
  };
  return labels[status.state];
}
