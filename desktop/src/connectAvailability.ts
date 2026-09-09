export const CONNECT_ENTITLEMENT_REQUIRED = "connect_entitlement_required";

export type CapabilityDiscoveryPresentation = "none" | "locked" | "unavailable";

export function classifyCapabilityDiagnostic(
  diagnosticCode: string | null,
): CapabilityDiscoveryPresentation {
  if (diagnosticCode === null) return "none";
  if (diagnosticCode === CONNECT_ENTITLEMENT_REQUIRED) return "locked";
  return "unavailable";
}

export interface DurableCapabilityState {
  status: "requested" | "accepted" | "processing" | "completed" | "failed";
  dispatch_state?: "waiting" | "dispatching" | "reconciling" | "provider_owned" | "terminal";
  queue_ahead?: number;
  dispatch_error?: { code: string; message: string } | null;
  error?: { code: string; message: string } | null;
}

export function durableCapabilityStatus(
  result: DurableCapabilityState,
  providerLabel: string,
  actionLabel: string,
): string | null {
  if (result.dispatch_state === "waiting") {
    const ahead = Math.max(0, result.queue_ahead ?? 0);
    return `Waiting for ${providerLabel}, ${ahead} ahead`;
  }
  if (result.dispatch_state === "reconciling") {
    return `Reconnecting to ${providerLabel}`;
  }
  if (
    result.dispatch_state === "dispatching" ||
    result.dispatch_state === "provider_owned" ||
    result.status === "accepted" ||
    result.status === "processing"
  ) {
    return `Running ${actionLabel}`;
  }
  if (result.status === "failed") {
    return result.dispatch_error?.message ?? result.error?.message ?? "Local capability failed";
  }
  return null;
}
