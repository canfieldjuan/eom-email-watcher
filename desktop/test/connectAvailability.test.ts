import assert from "node:assert/strict";
import test from "node:test";

import {
  CONNECT_ENTITLEMENT_REQUIRED,
  classifyCapabilityDiagnostic,
  durableCapabilityStatus,
} from "../src/connectAvailability.ts";

test("only the entitlement diagnostic renders a locked Connect state", () => {
  assert.equal(classifyCapabilityDiagnostic(CONNECT_ENTITLEMENT_REQUIRED), "locked");
  assert.equal(classifyCapabilityDiagnostic("connect_unavailable"), "unavailable");
  assert.equal(classifyCapabilityDiagnostic("provider_unavailable"), "unavailable");
  assert.equal(classifyCapabilityDiagnostic("connect_entitlement_required_extra"), "unavailable");
  assert.equal(classifyCapabilityDiagnostic(null), "none");
});

test("durable queue state controls capability status text", () => {
  assert.equal(
    durableCapabilityStatus(
      { status: "requested", dispatch_state: "waiting", queue_ahead: 2 },
      "Invoice Processor",
      "Read invoice",
    ),
    "Waiting for Invoice Processor, 2 ahead",
  );
  assert.equal(
    durableCapabilityStatus(
      { status: "requested", dispatch_state: "reconciling" },
      "Invoice Processor",
      "Read invoice",
    ),
    "Reconnecting to Invoice Processor",
  );
  assert.equal(
    durableCapabilityStatus(
      { status: "processing", dispatch_state: "provider_owned" },
      "Invoice Processor",
      "Read invoice",
    ),
    "Running Read invoice",
  );
  assert.equal(
    durableCapabilityStatus(
      {
        status: "failed",
        dispatch_state: "terminal",
        dispatch_error: { code: "PROVIDER_BUSY", message: "Another job is running." },
        error: { code: "connect_entitlement_required", message: "Connect license required." },
      },
      "Invoice Processor",
      "Read invoice",
    ),
    "Connect license required.",
  );
  assert.equal(
    durableCapabilityStatus(
      {
        status: "failed",
        dispatch_state: "terminal",
        dispatch_error: { code: "PROVIDER_BUSY", message: "Another job is running." },
        error: {
          code: "connect_queue_deadline_exceeded",
          message: "Queue expired.",
        },
      },
      "Invoice Processor",
      "Read invoice",
    ),
    "Another job is running.",
  );
});
