export const CONNECT_ENTITLEMENT_REQUIRED = "connect_entitlement_required";

export type CapabilityDiscoveryPresentation = "none" | "locked" | "unavailable";

export function classifyCapabilityDiagnostic(
  diagnosticCode: string | null,
): CapabilityDiscoveryPresentation {
  if (diagnosticCode === null) return "none";
  if (diagnosticCode === CONNECT_ENTITLEMENT_REQUIRED) return "locked";
  return "unavailable";
}
