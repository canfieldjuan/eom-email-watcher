import type { ConnectProviderIdentity, ConnectCapabilityRef, ConnectCapability } from "./connectTypes";

export interface CapabilityResultIdentity {
  protocol_version?: number;
  provider?: ConnectProviderIdentity;
  capability_id: string;
  capability_version: string;
  parameters?: Record<string, string | number | boolean>;
  status: string;
  required_manifest_features?: string[];
}

function canonicalCapabilityParameters(
  parameters: Record<string, string | number | boolean>,
): string {
  return JSON.stringify(
    Object.entries(parameters).sort(([left], [right]) => left.localeCompare(right)),
  );
}

export function capabilityInvocationKey(
  messageId: string,
  partId: string,
  provider: ConnectProviderIdentity,
  capability: ConnectCapabilityRef,
  parameters: Record<string, string | number | boolean>,
  requiredManifestFeatures: readonly string[] = [],
): string {
  return JSON.stringify([
    messageId,
    partId,
    provider.app_id,
    provider.version,
    provider.instance_id,
    capability.id,
    capability.version,
    canonicalCapabilityParameters(parameters),
    [...requiredManifestFeatures].sort(),
  ]);
}

export function matchingCapabilityResult<T extends CapabilityResultIdentity>(
  attachment: { capability_results?: T[] },
  selected: ConnectCapability,
  parameters?: Record<string, string | number | boolean>,
): T | undefined {
  const matches = (attachment.capability_results ?? []).filter(
    (result) =>
      result.protocol_version === 2 &&
      JSON.stringify([...(result.required_manifest_features ?? [])].sort()) ===
        JSON.stringify([...(selected.acknowledged_manifest_features ?? [])].sort()) &&
      result.provider?.app_id === selected.provider.app_id &&
      result.provider.version === selected.provider.version &&
      result.provider.instance_id === selected.provider.instance_id &&
      result.capability_id === selected.capability.id &&
      result.capability_version === selected.capability.version &&
      (parameters === undefined ||
        canonicalCapabilityParameters(result.parameters ?? {}) ===
          canonicalCapabilityParameters(parameters)),
  );
  return matches.find(activeCapabilityResult) ?? matches[0];
}

export function activeCapabilityResult(result: { status: string } | undefined): boolean {
  return ["requested", "accepted", "processing"].includes(result?.status ?? "");
}
