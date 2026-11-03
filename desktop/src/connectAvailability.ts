import type { ConnectCapabilityRef } from "./connectTypes";

export const CONNECT_ENTITLEMENT_REQUIRED = "connect_entitlement_required";

export type CapabilityDiscoveryPresentation = "none" | "locked" | "unavailable";

export function classifyCapabilityDiagnostic(
  diagnosticCode: string | null,
): CapabilityDiscoveryPresentation {
  if (diagnosticCode === null) return "none";
  if (diagnosticCode === CONNECT_ENTITLEMENT_REQUIRED) return "locked";
  return "unavailable";
}

export function capabilitySelectionRecovery(
  code: string | null,
  selection: ConnectCapabilityRef,
  retryable: boolean | null = null,
): string | null {
  if (retryable === true) return null;
  switch (code) {
    case "capability_unavailable":
      break;
    case "provider_unavailable":
    case "unsupported_capability":
    case "UNSUPPORTED_CAPABILITY":
      if (retryable !== false) return null;
      break;
    default:
      return null;
  }
  return `Saved selection ${selection.id} ${selection.version} is unavailable. Choose an available action to make a new selection.`;
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
    if (result.error?.code === "connect_queue_deadline_exceeded") {
      return result.dispatch_error?.message ?? result.error.message;
    }
    return result.error?.message ?? result.dispatch_error?.message ?? "Local capability failed";
  }
  return null;
}
