// Only authored text leaves this mapper. Engine reasons and job IDs are not UI copy.
export function automationOutcomeStatus(state: unknown): string {
  switch (state) {
    case "pending_dispatch":
      return "Automation queued for dispatch.";
    case "entitlement_paused":
      return "Automation paused.";
    case "awaiting_confirmation":
      return "Automation awaiting confirmation.";
    case "submitted":
      return "Automation submitted. Outcome pending.";
    case "completed":
      return "Automation completed.";
    case "failed":
      return "Automation failed.";
    case "declined":
      return "Automation declined. No action was submitted.";
    case "manual_review":
      return "Automation needs manual review.";
    case "source_unavailable":
      return "Automation source unavailable.";
    default:
      return "Automation status unavailable.";
  }
}
