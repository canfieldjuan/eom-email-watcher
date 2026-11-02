import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  CONNECT_ENTITLEMENT_REQUIRED,
  capabilitySelectionRecovery,
  classifyCapabilityDiagnostic,
  durableCapabilityStatus,
} from "../src/connectAvailability.ts";

test("selection recovery uses exact structured refusal codes and requires terminal unsupported work", () => {
  const selection = { id: "invoice.extract", version: "1.0" };
  const expected = "Saved selection invoice.extract 1.0 is unavailable. Choose an available action to make a new selection.";
  for (const code of ["capability_unavailable", "provider_unavailable"]) {
    assert.equal(capabilitySelectionRecovery(code, selection), expected);
    assert.equal(capabilitySelectionRecovery(code, selection, true), null);
  }
  for (const code of ["UNSUPPORTED_CAPABILITY", "unsupported_capability"]) {
    assert.equal(capabilitySelectionRecovery(code, selection, false), expected);
    assert.equal(capabilitySelectionRecovery(code, selection, true), null);
    assert.equal(capabilitySelectionRecovery(code, selection), null);
  }
  for (const code of [null, "", "INTERRUPTED", "PROVIDER_BUSY", "runtime_error", "capability_unavailable_extra"]) {
    assert.equal(capabilitySelectionRecovery(code, selection, false), null);
  }
});

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

test("queue progress refreshes the full loaded inbox span", async () => {
  const source = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");

  assert.match(
    source,
    /while \(inboxItems\.length < intent.target && inboxNextCursor\) \{\s+if \(!await loadInboxPage\(true, effectScope\) \|\| !current\(\)\) return false;/,
  );
  assert.match(
    source,
    /listen<\{ attempted: number \}>\("watcher:\/\/connect-queue", \(\) => \{\s+if \(configurationReady\) scheduleConnectQueueRefresh\(\);/,
  );
});

test("configured navigation gates every engine-backed tab", async () => {
  const source = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
  const navigation = source.match(
    /function setConfiguredNavigation\(enabled: boolean\): void \{([\s\S]*?)\n\}/,
  );

  assert.ok(navigation, "expected the configured navigation owner");
  for (const tab of ["inboxTab", "watchlistTab", "expiryLedgerTab", "healthTab"]) {
    assert.match(navigation[1], new RegExp(`${tab}\\.disabled = !enabled;`));
  }
});
