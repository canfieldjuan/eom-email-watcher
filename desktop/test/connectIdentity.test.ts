import assert from "node:assert/strict";
import test from "node:test";
import { capabilityInvocationKey, matchingCapabilityResult } from "../src/connectIdentity.ts";
import type { ConnectCapability } from "../src/connectTypes.ts";

const provider = { app_id: "fixture", version: "1.0.0", instance_id: "instance" };
const capability = { id: "document.translate", version: "1.0" };
const selected = { provider, capability, acknowledged_manifest_features: ["fixed-parameters-v1"] } as unknown as ConnectCapability;

test("different requirements retain separate desktop request IDs and result matches", () => {
  const features = ["fixed-parameters-v1"];
  assert.notEqual(capabilityInvocationKey("m", "2", provider, capability, {}, features),
    capabilityInvocationKey("m", "2", provider, capability, {}, []));
  const base = { protocol_version: 2, provider, capability_id: capability.id,
    capability_version: capability.version, parameters: {}, status: "requested" };
  const legacy = { ...base, job_id: "legacy", required_manifest_features: [] };
  const negotiated = { ...base, job_id: "negotiated", required_manifest_features: features };
  const attachment = { capability_results: [legacy, negotiated] };
  assert.equal(matchingCapabilityResult(attachment, selected)?.job_id, "negotiated");
  assert.equal(matchingCapabilityResult(attachment, { ...selected, acknowledged_manifest_features: [] })?.job_id, "legacy");
});
