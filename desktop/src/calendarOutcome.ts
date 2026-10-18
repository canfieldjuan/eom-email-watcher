export interface CalendarWriteOutcome {
  run_id: string;
  state_version: number;
  transaction_id: string;
  status: "authorized" | "writing" | "unresolved" | "reconciling" | "completed" | "failed" | "cancelled";
  failure_code: string | null;
  graph_event_id: string | null;
}

export function calendarOutcomeNotice(
  outcome: CalendarWriteOutcome | null | undefined,
  surface: "proposal" | "decision",
  state?: string,
): { text: string; kind: "success" | "warning" | "error" } {
  // A fresh proposal can coexist with the previous terminal current-write row.
  // Its own pending/declined copy must not describe that previous admission.
  if (outcome?.status === "cancelled" &&
      (state === "awaiting_confirmation" || state === "declined")) {
    outcome = null;
  }
  if (outcome?.status === "cancelled") {
    return {
      text: outcome.failure_code === "thread_unfollowed"
        ? "Not added: you unfollowed this thread"
        : "No calendar event has been created.",
      kind: "warning",
    };
  }
  const status = outcome?.status ?? state;
  if (status === "completed") {
    return { text: surface === "proposal" ? "The calendar event was created." : "Calendar event created.", kind: "success" };
  }
  if (status === "failed") {
    return { text: surface === "proposal" ? "Microsoft definitively rejected the event creation request." : "Microsoft rejected the calendar event creation.", kind: "error" };
  }
  if (status === "declined") {
    return { text: surface === "proposal" ? "You declined this calendar proposal. No event was created." : "Calendar proposal declined.", kind: "success" };
  }
  if (surface === "proposal") {
    return {
      text: status === "writing" || status === "unresolved" || status === "reconciling"
        ? "The write result is uncertain. Email Watcher will reconcile it without creating a second event."
        : "No calendar event has been created.",
      kind: "warning",
    };
  }
  return { text: "Calendar decision saved; Email Watcher will reconcile the result.", kind: "warning" };
}


export function latestCalendarOutcome(
  decision: CalendarWriteOutcome | null | undefined,
  refreshed: CalendarWriteOutcome | null | undefined,
): CalendarWriteOutcome | null | undefined {
  return decision && refreshed && decision.run_id === refreshed.run_id &&
    refreshed.state_version >= decision.state_version ? refreshed : decision;
}
