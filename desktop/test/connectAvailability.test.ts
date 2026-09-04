import assert from "node:assert/strict";
import test from "node:test";

import {
  CONNECT_ENTITLEMENT_REQUIRED,
  classifyCapabilityDiagnostic,
} from "../src/connectAvailability.ts";

test("only the entitlement diagnostic renders a locked Connect state", () => {
  assert.equal(classifyCapabilityDiagnostic(CONNECT_ENTITLEMENT_REQUIRED), "locked");
  assert.equal(classifyCapabilityDiagnostic("connect_unavailable"), "unavailable");
  assert.equal(classifyCapabilityDiagnostic("provider_unavailable"), "unavailable");
  assert.equal(classifyCapabilityDiagnostic("connect_entitlement_required_extra"), "unavailable");
  assert.equal(classifyCapabilityDiagnostic(null), "none");
});
