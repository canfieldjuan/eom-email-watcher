// Status text is authored copy; rule identity is admitted only in canonical form.
const RULE_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export function automationOutcomeIdentity(ruleId: unknown, version: unknown): string {
  if (typeof ruleId !== "string" || !RULE_ID_PATTERN.test(ruleId)) {
    return "Automation (rule identity unavailable)";
  }
  if (typeof version !== "number" || !Number.isSafeInteger(version) || version < 1) {
    return `Automation rule ${ruleId} (version unavailable)`;
  }
  return `Automation rule ${ruleId} (version ${version})`;
}

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
