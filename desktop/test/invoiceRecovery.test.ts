import assert from "node:assert/strict";
import test from "node:test";
import { inboxThreadOriginKey } from "../src/inboxThreadEntry.ts";
import { detailsHarness } from "./inboxMessageDetailsFixture.ts";
import { nodes, text as rendered } from "./threadDomFixture.ts";

const capability = (version: string, confirmation = false) => ({
  protocol_version: 2,
  provider: { app_id: "invoice-processor", version: "1.0.0", instance_id: "fixture-provider", name: "Invoice Processor" },
  capability: { id: "invoice.extract", version, action: { label: `Read invoice ${version}`, description: "Synthetic extraction" },
    parameters: [], effects: { external: false, confirmation_required: confirmation } },
});
const recovery = "Saved selection invoice.extract 1.0 is unavailable. Choose an available action to make a new selection.";

function harness(refusal: { code: string | null; message: string; retryable?: boolean } = {
  code: "capability_unavailable", message: "Opaque fixture reason",
}) {
  const h = detailsHarness(), old = capability("1.0"), current = capability("2.0", true);
  let catalog = [old], allowConfirmation = true, invocationHold: Promise<void> | null = null, replacementStatus = "completed";
  const invocations: Record<string, any>[] = [], confirmations: string[] = [], opened: unknown[] = [];
  const item = { thread_entry_ref: { provider: "fixture", account_id: "fixture", mailbox_identity_key: null, message_id: "message-1" },
    message_id: "message-1", provider: "fixture", account_id: "fixture", sender: "sender@example.test",
    received_at: "2026-11-02T00:00:00Z", subject: "Synthetic invoice", admission: null, status: "analyzed", summary: "Invoice",
    attachments: [{ part_id: "part-1", filename: "synthetic.pdf", media_type: "application/pdf", byte_size: 123,
      capability_results: [] as any[], automation_fires: [] }] };
  h.snapshot.items = [item]; h.c.readerDetails = h.content;
  h.c.confirmAction = async (message: string) => { confirmations.push(message); return allowConfirmation; };
  h.invoke(async (operation: string, args: Record<string, any>) => {
    if (operation === "inbox_query") return { items: [item], next_cursor: null };
    if (operation === "attachment_capabilities") return { items: catalog, diagnostic: null };
    assert.equal(operation, "attachment_capability_invoke");
    invocations.push(structuredClone(args)); await invocationHold;
    if (args.capability.version === "1.0") throw refusal;
    return { status: replacementStatus, outputs: [], job_id: args.requestId };
  });
  h.c.attachmentCapabilities.set(h.c.attachmentKey(item.message_id, "part-1"), catalog);
  void h.ui.open(item);
  const open = h.ui.open.bind(h.ui);
  h.ui.open = async row => { opened.push(row); await open(row); };
  const render = () => h.c.renderInbox(h.c.inboxItems), refresh = () => h.c.loadInbox();
  const button = (label: string) => { const found = [...nodes(h.list), ...nodes(h.content)]
    .find(node => node.tagName === "BUTTON" && node.textContent === label);
    assert.ok(found, `missing button ${label}`); return found; };
  render();
  return { context: h.c, item, old, current, opened, invocations, confirmations, render, refresh, button,
    setCatalog: (items: typeof catalog) => { catalog = items; },
    holdInvocation: (promise: Promise<void>) => { invocationHold = promise; },
    setReplacementStatus: (status: string) => { replacementStatus = status; },
    setConfirmation: (value: boolean) => { allowConfirmation = value; } };
}

for (const code of ["capability_unavailable", "provider_unavailable"]) {
  test(`refused selection stays visible after catalog refresh: ${code}`, async () => {
    const h = harness({ code, message: "Opaque fixture reason", retryable: false });
    h.setCatalog([h.current]); // Cutover after rendering, before the user clicks.
    await h.button("Read invoice 1.0").click();
    assert.ok(rendered(h.context.readerDetails).includes(recovery), rendered(h.context.readerDetails));
    assert.equal(h.invocations.length, 1);
    assert.deepEqual(h.invocations[0].capability, { id: "invoice.extract", version: "1.0" });
    assert.equal(h.context.attachmentInvocationsInFlight.size, 0);
    assert.equal(h.context.attachmentRequestIds.size, 0, "authoritative refusal ends the old UI request");
    for (const catalog of [[], [h.current], [h.old, h.current]]) {
      h.setCatalog(catalog);
      await h.refresh();
      assert.ok(rendered(h.context.readerDetails).includes(recovery));
      assert.equal(h.invocations.length, 1, "catalog discovery cannot invoke a replacement");
      assert.equal(nodes(h.context.readerDetails).some(node => node.textContent.startsWith("Running ") || node.textContent.startsWith("Resume ")), false);
    }
    h.setCatalog([h.current]);
    await h.refresh();
    h.setConfirmation(false);
    await h.button("Read invoice 2.0").click();
    assert.equal(h.invocations.length, 1, "declined replacement submits nothing");
    h.setConfirmation(true);
    await h.button("Read invoice 2.0").click();
    assert.equal(h.invocations.length, 2);
    assert.deepEqual(h.invocations[1].capability, { id: "invoice.extract", version: "2.0" });
    assert.notEqual(h.invocations[0].requestId, h.invocations[1].requestId);
    assert.equal(h.invocations[1].confirmed, true);
    assert.equal(h.confirmations.length, 2);
  });
}

test("unrelated errors cannot manufacture recovery from free-text messages", async () => {
  for (const code of ["runtime_error", "capability_unavailable_extra", "", null]) {
    const h = harness({ code, message: "invoice.extract 1.0 unavailable; choose 2.0" });
    await h.button("Read invoice 1.0").click();
    await h.refresh();
    assert.equal(rendered(h.context.readerDetails).includes(recovery), false);
    assert.equal(h.context.attachmentRequestIds.size, 1, "uncertain errors retain reconciliation identity");
  }
});

test("unsupported selection recovery requires a nonretryable refusal", async () => {
  for (const retryable of [true, false, undefined]) {
    const h = harness({ code: "unsupported_capability", message: "Opaque fixture reason", retryable });
    await h.button("Read invoice 1.0").click();
    await h.refresh();
    assert.equal(rendered(h.context.readerDetails).includes(recovery), retryable === false);
    assert.equal(h.context.attachmentRequestIds.size, retryable === false ? 0 : 1);
  }
});

test("temporary or unspecified provider absence preserves its request without reselection copy", async () => {
  for (const retryable of [true, undefined]) {
    const h = harness({ code: "provider_unavailable", message: "Provider is temporarily unavailable", retryable });
    await h.button("Read invoice 1.0").click();
    await h.refresh();
    assert.equal(rendered(h.context.readerDetails).includes(recovery), false);
    assert.equal(h.context.attachmentRequestIds.size, 1);
    await h.button("Read invoice 1.0").click();
    assert.equal(h.invocations[1].requestId, h.invocations[0].requestId);
  }
});

test("successful deliberate replacement clears the notice while declining preserves it", async () => {
  for (const status of ["requested", "accepted", "processing", "completed"]) {
    const h = harness();
    await h.button("Read invoice 1.0").click();
    h.setCatalog([h.current]);
    h.setReplacementStatus(status);
    await h.refresh();
    h.setConfirmation(false);
    await h.button("Read invoice 2.0").click();
    assert.ok(rendered(h.context.readerDetails).includes(recovery));
    assert.equal(h.invocations.length, 1);
    h.setConfirmation(true);
    await h.button("Read invoice 2.0").click();
    assert.equal(rendered(h.context.readerDetails).includes(recovery), false, status);
    await h.refresh();
    assert.equal(rendered(h.context.readerDetails).includes(recovery), false, status);
    assert.equal(h.invocations.length, 2);
  }
});

test("an earlier replacement success cannot erase a newer concurrent refusal", async () => {
  const h = harness();
  await h.button("Read invoice 1.0").click();
  h.setCatalog([h.old, h.current]);
  await h.refresh();
  let release!: () => void;
  h.holdInvocation(new Promise<void>(resolve => { release = resolve; }));
  const earlierSuccess = h.button("Read invoice 2.0").click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.invocations.length, 2);
  h.holdInvocation(Promise.resolve());
  await h.button("Read invoice 1.0").click();
  release();
  await earlierSuccess;
  assert.equal(h.invocations.length, 3);
  assert.ok(rendered(h.context.readerDetails).includes(recovery));
});

test("a different capability success cannot clear the invoice selection refusal", async () => {
  const h = harness();
  await h.button("Read invoice 1.0").click();
  h.setCatalog([{
    ...h.current,
    capability: { ...h.current.capability, id: "document.translate",
      action: { label: "Translate document", description: "Synthetic translation" } },
  }]);
  await h.refresh();
  await h.button("Translate document").click();
  assert.equal(h.invocations.length, 2);
  assert.ok(rendered(h.context.readerDetails).includes(recovery));
});

test("catalog refresh during an invocation cannot erase its later refusal or submit new work", async () => {
  const h = harness();
  let release!: () => void;
  h.holdInvocation(new Promise<void>(resolve => { release = resolve; }));
  const invoking = h.button("Read invoice 1.0").click();
  h.setCatalog([h.current]);
  await h.refresh();
  assert.equal(h.invocations.length, 1);
  release();
  await invoking;
  assert.ok(rendered(h.context.readerDetails).includes(recovery));
  assert.equal(h.invocations.length, 1);
});

test("refusal belongs to its attachment and clears with message-owned UI state", async () => {
  const h = harness();
  await h.button("Read invoice 1.0").click();
  h.item.attachments.push({ ...h.item.attachments[0], part_id: "part-2" });
  await h.refresh();
  assert.equal(nodes(h.context.readerDetails).filter(node => node.textContent === recovery).length, 1);
  h.context.clearMessageOwnedUiState("another-message");
  h.render();
  assert.ok(rendered(h.context.readerDetails).includes(recovery));
  h.context.clearMessageOwnedUiState("message-1");
  h.render();
  assert.equal(rendered(h.context.readerDetails).includes(recovery), false);
});

test("persisted unsupported legacy result renders its reason and recovery without current discovery", async () => {
  const h = harness();
  h.item.attachments[0].capability_results.push({
    protocol_version: 2, provider: h.old.provider, capability_id: "invoice.extract", capability_version: "1.0",
    job_id: "old-terminal-job", status: "failed", dispatch_state: "terminal", summary: null,
    error: { code: "UNSUPPORTED_CAPABILITY", message: "Original terminal refusal", retryable: false },
  });
  for (const catalog of [[], [h.current]]) {
    h.setCatalog(catalog);
    await h.refresh();
    assert.ok(rendered(h.context.readerDetails).includes("Original terminal refusal"));
    assert.ok(rendered(h.context.readerDetails).includes(recovery));
    assert.equal(nodes(h.context.readerDetails).some(node => node.textContent.startsWith("Resume ")), false);
    assert.equal(h.invocations.length, 0);
  }
});

test("persisted provider errors consume stored retryability without inventing a terminal signal", async () => {
  for (const retryable of [false, true, undefined]) {
    const h = harness();
    h.item.attachments[0].capability_results.push({
      protocol_version: 2, provider: h.old.provider, capability_id: "invoice.extract", capability_version: "1.0",
      job_id: "stored-provider-job", status: "failed", dispatch_state: "terminal", summary: null,
      error: { code: "provider_unavailable", message: "Stored provider reason", retryable },
    });
    h.setCatalog([h.current]);
    await h.refresh();
    assert.equal(rendered(h.context.readerDetails).includes(recovery), retryable === false);
    assert.ok(rendered(h.context.readerDetails).includes("Stored provider reason"));
    assert.equal(h.invocations.length, 0);
  }
});


test("Open thread is a separate current-row action and does not invoke saved analysis actions", async () => {
  const h = harness();
  const button = h.button("Open thread");
  assert.equal(button.attributes.get("aria-controls"), "inbox-thread-view");
  assert.equal((button.dataset as any).inboxThreadOrigin, inboxThreadOriginKey(h.item.thread_entry_ref));
  await button.click();
  assert.deepEqual(h.opened, [h.item]); assert.deepEqual(h.invocations, []); assert.deepEqual(h.confirmations, []);
  assert.ok(rendered(h.context.readerDetails).includes("Invoice"));
});
