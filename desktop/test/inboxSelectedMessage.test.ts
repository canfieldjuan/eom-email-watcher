import assert from "node:assert/strict";
import test from "node:test";
import { paneHarness } from "./inboxThreadPaneFixture.ts";
import { deferred } from "./inboxThreadPaneFixture.ts";
import { detailsHarness, tick, testFire, testCapability, productionFunctions } from "./inboxMessageDetailsFixture.ts";
import { nodes, text } from "./threadDomFixture.ts";
import { parseAst } from "vite";

test("selection context exists independently of thread resolution and survives only the same lifetime", async () => {
  const h = paneHarness();
  h.respond(async () => { throw new Error("thread unavailable"); });
  await h.ui.open(h.rows[0]);
  const selected = h.ui.selection();
  assert.ok(selected);
  assert.equal(selected.row, h.rows[0]);
  assert.ok(Object.isFrozen(selected.context));
  assert.ok(Object.isFrozen(selected.context.origin));
  assert.equal(h.ui.checkSelection(selected.context).status, "ready");
  h.snapshot.spanScope = null;
  h.ui.invalidate();
  assert.equal(h.ui.selection(), null, "old details cannot authorize a new action");
  assert.equal(h.ui.checkSelection(selected.context).status, "waiting");
  h.snapshot.items = h.rows.map(row => ({ ...row }));
  h.snapshot.spanScope = { ...h.snapshot.scope! };
  h.ui.revalidateSource();
  assert.equal(h.ui.selection()!.context, selected.context);
  assert.equal(h.ui.checkSelection(selected.context).status, "ready");
  h.ui.back();
  await h.ui.open(h.rows[0]);
  assert.equal(h.ui.checkSelection(selected.context).status, "refused");
});

test("compact rows mount no details/actions; selected origin owns every existing action family once", async () => {
  const h = detailsHarness();
  assert.equal(h.section.hidden, true); assert.equal(h.content.childElementCount, 0);
  assert.doesNotMatch(text(h.list), /Saved a|Saved b|Create event|Confirm automation|Delete locally|Retry analysis|Run fixture/);
  await h.open();
  for (const label of ["Create event", "Decline", "Open", "Run fixture", "Confirm automation", "Decline automation", "Retry analysis", "Delete locally"])
    assert.equal(nodes(h.content).filter(node => node.tagName === "BUTTON" && node.textContent === label).length, 1, label);
  assert.match(h.text(), /Saved a/); assert.doesNotMatch(h.text(), /Saved b/);
  assert.match(h.text(), /No action required/); assert.match(h.text(), /0B/);
  assert.equal(h.calls.length, 0, "render/selection alone does not run native actions");
  h.ui.back(); assert.equal(h.content.childElementCount, 0); assert.equal(h.section.hidden, true);
});

const families = [
  { label: "Create event", command: "calendar_proposal_decide" },
  { label: "Run fixture", command: "attachment_capability_invoke" },
  { label: "Confirm automation", command: "automation_fire_decide" },
];
for (const family of families) {
  for (const refresh of ["scheduled", "connect"]) for (const order of ["refresh-first", "approval-first"]) {
    test(`${family.label}: ${refresh}, ${order} preserves the approved original target exactly once`, async () => {
      const h = detailsHarness(), dialog = deferred(), page = deferred(); await h.open();
      h.c.confirmAction = () => dialog.promise;
      h.button(family.label).click();
      let held = true;
      h.invoke(async (command, args) => command === "inbox_query" && held ? page.promise : h.defaultInvoke(command, args));
      const original = structuredClone(h.rows[0]);
      const fresh = structuredClone(h.rows) as any[]; fresh[0].calendar_proposal.state_version = 99;
      fresh[0].attachments[0].automation_fires[0].state_version = 99;
      const refreshing = refresh === "scheduled" ? h.c.loadInbox() : (h.c.scheduleConnectQueueRefresh(), h.c.connectQueueRefresh);
      assert.equal(h.content.childElementCount, 0, "invalidated details are removed immediately");
      if (order === "approval-first") {
        dialog.resolve(true); await tick();
        assert.match(h.notice.textContent, /Origin a.*Waiting.*not sent yet/);
        assert.equal(h.calls.filter(call => call.command === family.command).length, 0);
        assert.equal(h.calls.filter(call => call.command === "inbox_query").length, 1, "approval makes no extra refresh");
        assert.equal(h.refreshes(), 0, "waiting never requests another origin refresh");
      }
      held = false; page.resolve({ items: fresh, next_cursor: null }); await refreshing;
      if (order === "refresh-first") dialog.resolve(true);
      await tick(); await tick();
      const sent = h.calls.filter(call => call.command === family.command);
      assert.equal(sent.length, 1);
      if (family.command === "calendar_proposal_decide") assert.deepEqual(sent[0].args, { decision: "confirm", messageId: "a",
        proposalSha256: original.calendar_proposal.proposal_sha256, proposalVersion: 7, runId: "calendar-run", stateVersion: 9 });
      if (family.command === "attachment_capability_invoke") {
        assert.equal(sent[0].args.messageId, "a"); assert.equal(sent[0].args.partId, "part");
        assert.deepEqual(sent[0].args.parameters, { zero: 0, no: false, empty: "" }); assert.equal(sent[0].args.confirmed, true);
      }
      if (family.command === "automation_fire_decide") assert.deepEqual(sent[0].args,
        { fireId: testFire.fire_id, expectedVersion: 3, preparedIdentitySha256: "a".repeat(64), decision: "confirmed" });
    });
  }
  for (const cause of ["back", "switch", "a-b-a", "hide", "query", "scope", "hold", "absent", "failure"]) {
    test(`${family.label}: ${cause} after input gives no dispatch and a durable named refusal`, async () => {
      const h = detailsHarness(), dialog = deferred(); await h.open(); h.c.confirmAction = () => dialog.promise;
      h.button(family.label).click();
      if (cause === "back") h.ui.back();
      if (cause === "switch" || cause === "a-b-a") { await h.open(1); if (cause === "a-b-a") await h.open(); }
      if (cause === "hide") { h.snapshot.visible = false; h.ui.close(); h.snapshot.visible = true; await h.open(); }
      if (cause === "query") { h.snapshot.queryEpoch++; h.ui.invalidate(); }
      if (cause === "scope") { h.snapshot.scope = { mutation_epoch: 10, mailbox_operation_revision: 11 }; h.ui.invalidate(); }
      if (cause === "hold") { h.snapshot.blocked = true; h.ui.beginMutation(); }
      if (cause === "absent") { h.snapshot.spanScope = null; h.ui.invalidate(); h.snapshot.items = [h.rows[1]];
        h.snapshot.spanScope = h.snapshot.scope; h.ui.revalidateSource(); }
      if (cause === "failure") { h.snapshot.spanScope = null; h.ui.invalidate(); h.ui.failInboxRefresh(h.ui.refreshGeneration); }
      const focus = h.doc.activeElement; dialog.resolve(true); await tick();
      assert.equal(h.calls.filter(call => call.command === family.command).length, 0);
      assert.match(h.notice.textContent, /Origin a.*Not sent: .+/); assert.equal(h.doc.activeElement, focus);
      h.c.inboxStatus.textContent = "A later refresh"; assert.match(h.notice.textContent, /Origin a.*Not sent:/);
      assert.doesNotMatch(h.text(), /Not sent:/, "refusal never paints as B's selected result");
    });
  }
}

test("complete origin permits the contract's explicit null identity but refuses missing/partial/falsy identity fields", async () => {
  for (const identity of [null, "mailbox", undefined, "", false, 0]) {
    const h = paneHarness(); h.rows[0].thread_entry_ref.mailbox_identity_key = identity as any;
    await h.ui.open(h.rows[0]); assert.equal(h.ui.selection() !== null, identity === null || identity === "mailbox", String(identity));
  }
});

test("a disappeared origin or failed Inbox refresh cannot revive an old approval after later recovery", async () => {
  for (const cause of ["absent", "failed"]) {
    const h = detailsHarness(), dialog = deferred(); await h.open();
    h.c.confirmAction = () => dialog.promise; h.button("Create event").click();
    h.snapshot.spanScope = null; h.ui.invalidate();
    if (cause === "absent") {
      h.snapshot.items = [h.rows[1]]; h.snapshot.spanScope = h.snapshot.scope; h.ui.revalidateSource();
    } else h.ui.failInboxRefresh(h.ui.refreshGeneration);
    h.snapshot.spanScope = null; h.ui.invalidate(); h.snapshot.items = h.rows;
    h.snapshot.spanScope = h.snapshot.scope; h.ui.revalidateSource();
    dialog.resolve(true); await tick(); await tick();
    assert.equal(h.calls.filter(call => call.command === "calendar_proposal_decide").length, 0, cause);
    assert.match(h.notice.textContent, /Origin a.*Not sent:/);
  }
});

for (const family of families) for (const outcome of ["success", "refusal", "transport", "refresh-failed"]) {
  test(`${family.label}: dispatched A survives selection changes; ${outcome} cannot paint or focus B`, async () => {
    const h = detailsHarness(), completion = deferred(); await h.open();
    let dispatched = false;
    h.invoke(async (command, args) => {
      if (command === family.command) { dispatched = true; return completion.promise; }
      if (command === "inbox_query" && outcome === "refresh-failed") throw new Error("Fixture refresh failed");
      return h.defaultInvoke(command, args);
    });
    h.button(family.label).click(); await tick(); assert.equal(dispatched, true);
    await h.open(1); h.layout.readerPane.scrollTop = 71; h.listScroll.scrollTop = 42;
    const focus = h.el("button"); h.doc.body.append(focus); focus.focus();
    const identity = h.ui.selection()!.context;
    if (outcome === "refusal" || outcome === "transport") {
      // The command promise rejects after B has become current.
      completion.resolve(Promise.reject({ code: outcome === "refusal" ? "stale_automation_fire" : "runtime_error", message: `Fixture ${outcome}` }));
    } else completion.resolve(await h.defaultInvoke(family.command, {}));
    await tick(); await tick();
    assert.equal(h.calls.filter(call => call.command === family.command).length, 1);
    assert.equal(h.doc.activeElement, focus); assert.equal(h.layout.readerPane.scrollTop, 71);
    assert.equal(h.listScroll.scrollTop, 42);
    assert.equal(h.notice.textContent.includes("Origin a"), true, "outcome belongs to A");
    assert.doesNotMatch(h.text(), /Saved a|Fixture refusal|Fixture transport|Not sent:/);
    if (outcome !== "refresh-failed") { assert.equal(h.ui.selection()!.context, identity); assert.match(h.text(), /Saved b/); }
  });
}

test("same operation is reserved before confirmation, across remounts and A-B-A, but terminal completion permits a new request", async () => {
  const h = detailsHarness(), dialog = deferred(), completion = deferred(); await h.open();
  let dialogs = 0; h.c.confirmAction = () => { dialogs++; return dialog.promise; };
  h.invoke(async (command, args) => command === "attachment_capability_invoke" ? completion.promise : h.defaultInvoke(command, args));
  h.button("Run fixture").click(); h.c.renderInbox(h.c.inboxItems); h.button("Run fixture").click();
  assert.equal(dialogs, 1); dialog.resolve(true); await tick();
  await h.open(1); await h.open(); h.button("Run fixture").click(); await tick();
  assert.equal(dialogs, 1); assert.equal(h.calls.filter(call => call.command === "attachment_capability_invoke").length, 1);
  completion.resolve({ status: "completed", outputs: [] }); await tick(); await tick();
  h.c.confirmAction = async () => true; h.invoke(h.defaultInvoke); h.button("Run fixture").click(); await tick(); await tick();
  const calls = h.calls.filter(call => call.command === "attachment_capability_invoke");
  assert.equal(calls.length, 2); assert.notEqual(calls[0].args.requestId, calls[1].args.requestId);
});

test("output reads clear preview bytes on invalidation and a late A preview cannot enter B or a renewed A lifetime", async () => {
  for (const returnA of [false, true]) {
    const h = detailsHarness(), completion = deferred();
    const result = { protocol_version: 2, status: "completed", provider: { app_id: "fixture", version: "1", instance_id: "instance" },
      capability_id: "fixture.action", capability_version: "1", job_id: "job", outputs: [{ artifact_id: "artifact", display_name: "same-name.txt",
        media_type: "text/plain", byte_size: 5 }] };
    (h.rows[0] as any).attachments[0].capability_results = [result]; await h.open();
    h.invoke(async (command, args) => command === "capability_output_present" ? completion.promise : h.defaultInvoke(command, args));
    h.button("View").click(); await tick(); await h.open(1); if (returnA) await h.open();
    const focus = h.doc.activeElement;
    completion.resolve({ presentation: { kind: "text", text: "Old A output bytes" } }); await tick();
    assert.doesNotMatch(h.text(), /Old A output bytes/); assert.equal(h.c.capabilityOutputPresentations.size, 0);
    assert.equal(h.doc.activeElement, focus);
    h.snapshot.spanScope = null; h.ui.invalidate(); assert.equal(h.content.childElementCount, 0);
    assert.equal(h.c.capabilityOutputPreviews.size, 0);
  }
});

test("an older terminal job cannot erase a newer uncertain request identity during a rerender", async () => {
  const h = detailsHarness(), completion = deferred();
  (h.rows[0] as any).attachments[0].capability_results = [{ protocol_version: 2, provider: testCapability.provider,
    capability_id: testCapability.capability.id, capability_version: "1", job_id: "older-terminal", status: "completed",
    parameters: { zero: 0, no: false, empty: "" }, outputs: [] }];
  await h.open();
  h.invoke(async (command, args) => command === "attachment_capability_invoke" ? completion.promise : h.defaultInvoke(command, args));
  h.button("Run fixture").click(); await tick();
  const first = h.calls.find(call => call.command === "attachment_capability_invoke")!;
  h.c.renderInbox(h.c.inboxItems);
  completion.resolve(Promise.reject({ code: "runtime_error", message: "Transport uncertain" })); await tick(); await tick();
  assert.equal([...h.c.attachmentRequestIds.values()].includes(first.args.requestId), true);
  h.invoke(h.defaultInvoke); h.button("Run fixture").click(); await tick(); await tick();
  const calls = h.calls.filter(call => call.command === "attachment_capability_invoke");
  assert.equal(calls.length, 2); assert.equal(calls[1].args.requestId, first.args.requestId);
});

test("a fresh row with a missing identity cannot impersonate an origin whose identity is explicitly null", async () => {
  const h = paneHarness(); h.rows[0].thread_entry_ref.mailbox_identity_key = null as any;
  await h.ui.open(h.rows[0]); const context = h.ui.selection()!.context;
  h.snapshot.spanScope = null; h.ui.invalidate();
  const partial = structuredClone(h.rows[0]); delete (partial.thread_entry_ref as any).mailbox_identity_key;
  h.snapshot.items = [partial]; h.snapshot.spanScope = h.snapshot.scope; h.ui.revalidateSource();
  assert.equal(h.ui.selection(), null); assert.equal(h.ui.checkSelection(context).status, "refused");
});

test("delete approval during a hold is visibly not sent, and accepted removal clears details before dispatch through nested holds", async () => {
  for (const blockedBeforeApproval of [true, false]) {
    const h = detailsHarness(), dialog = deferred(), deletion = deferred(); await h.open();
    h.c.confirmAction = () => dialog.promise;
    let calls = 0;
    h.c.inboxRemovalClient = { delete: async (id: string) => {
      calls++; assert.equal(id, "a"); assert.equal(h.content.childElementCount, 0);
      assert.equal(h.c.capabilityOutputPreviews.size, 0); return deletion.promise;
    } };
    h.button("Delete locally").click();
    if (blockedBeforeApproval) h.c.threadHistory.beginMutation();
    dialog.resolve(true); await tick();
    if (blockedBeforeApproval) { assert.equal(calls, 0); assert.match(h.notice.textContent, /Origin a.*Not sent:.*holding|Origin a.*Not sent:.*held/); }
    else {
      assert.equal(calls, 1); h.c.threadHistory.beginMutation();
      deletion.resolve({ outcome: "deferred", requested: 1, completed: 0, pending: 1, message: "Pending synthetic removal" });
      await tick(); await tick();
      assert.equal(h.c.threadHistory.mutationHeld, true); assert.equal(h.content.childElementCount, 0);
      assert.match(h.notice.textContent, /Origin a.*pending/i);
    }
    await h.c.threadHistory.endMutation();
  }
});

test("the single context decision rejects forged, hidden, blocked, query and scope changes before collecting any input", async () => {
  for (const cause of ["forged", "configured", "visible", "document", "blocked", "query", "scope", "hold-ended"]) {
    const h = paneHarness(); await h.ui.open(h.rows[0]); let context = h.ui.selection()!.context;
    if (cause === "forged") context = structuredClone(context);
    if (cause === "configured") h.snapshot.configured = false;
    if (cause === "visible") h.snapshot.visible = false;
    if (cause === "document") h.doc.visibilityState = "hidden";
    if (cause === "blocked") h.snapshot.blocked = true;
    if (cause === "query") { h.snapshot.queryEpoch++; h.snapshot.itemsEpoch = h.snapshot.queryEpoch; }
    if (cause === "scope") { h.snapshot.scope = { mutation_epoch: 5, mailbox_operation_revision: 6 }; h.snapshot.spanScope = h.snapshot.scope; }
    if (cause === "hold-ended") { h.ui.beginMutation(); await h.ui.endMutation(false); }
    assert.equal(h.ui.checkSelection(context).status, "refused", cause);
    let input = 0, sent = 0;
    await h.ui.runAction(context, "fixture", () => { input++; return true; }, async () => { sent++; });
    assert.equal(input, 0, cause); assert.equal(sent, 0, cause);
  }
});

test("fresh complete-origin lookalikes cannot replace any member of the admitted row or native tuple", async () => {
  for (const field of ["provider", "account_id", "mailbox_identity_key", "message_id"]) {
    for (const outer of [false, true]) {
      if (outer && field === "mailbox_identity_key") continue;
      const h = paneHarness(); await h.ui.open(h.rows[0]); const context = h.ui.selection()!.context;
      const replacement = structuredClone(h.rows[0]);
      if (outer) (replacement as any)[field] = "replacement";
      else (replacement.thread_entry_ref as any)[field] = "replacement";
      h.snapshot.items = [replacement];
      assert.equal(h.ui.checkSelection(context).status, "refused", `${outer}:${field}`);
    }
  }
});

test("only null cancels a prepared step; admitted false, zero and empty string reach dispatch unchanged", async () => {
  const h = paneHarness(); await h.ui.open(h.rows[0]); const context = h.ui.selection()!.context, sent: unknown[] = [];
  for (const target of [null, false, 0, ""]) await h.ui.runAction(context, "fixture", () => target, async value => { sent.push(value); });
  assert.deepEqual(sent, [false, 0, ""]);
});

test("DOM construction owns no asynchronous closures that can retain old row or preview nodes", () => {
  const ast = parseAst(productionFunctions(["renderInboxMessageDetails", "renderCapabilityResult"]));
  const asyncClosures: string[] = [];
  const visit = (value: any) => {
    if (!value || typeof value !== "object") return;
    if ((value.type === "ArrowFunctionExpression" || value.type === "FunctionExpression") && value.async)
      asyncClosures.push(value.type);
    for (const child of Object.values(value)) if (Array.isArray(child)) child.forEach(visit); else visit(child);
  };
  visit(ast); assert.deepEqual(asyncClosures, []);
});

test("attachment open, analysis retry, declines and safe export preserve their exact native targets without new consent", async () => {
  const fixtures = [
    { label: "Open", command: "attachment_open", payload: { messageId: "a", partId: "part" } },
    { label: "Retry analysis", command: "analysis_requeue", payload: { messageId: "a" } },
    { label: "Decline", command: "calendar_proposal_decide", payload: { messageId: "a", proposalSha256: "b".repeat(64),
      proposalVersion: 7, runId: "calendar-run", stateVersion: 9, decision: "decline" } },
    { label: "Decline automation", command: "automation_fire_decide", payload: { fireId: testFire.fire_id,
      expectedVersion: 3, preparedIdentitySha256: "a".repeat(64), decision: "declined" } },
    { label: "Safe export", command: "capability_output_export", payload: { messageId: "a", partId: "part", jobId: "job", artifactId: "artifact" } },
  ];
  for (const fixture of fixtures) {
    const h = detailsHarness();
    (h.rows[0] as any).attachments[0].capability_results = [{ protocol_version: 2, provider: testCapability.provider,
      capability_id: "fixture.action", capability_version: "1", job_id: "job", status: "completed",
      outputs: [{ artifact_id: "artifact", display_name: "Output", media_type: "text/plain", byte_size: 5 }] }];
    await h.open(); h.button(fixture.label).click(); await tick(); await tick();
    assert.deepEqual(h.calls.filter(call => call.command === fixture.command).map(call => call.args), [fixture.payload], fixture.label);
    assert.equal(h.confirmations.length, 0, fixture.label);
  }
});

test("a chosen provider tuple is captured before consent and cannot be replaced by a later select change", async () => {
  const h = detailsHarness(), dialog = deferred(), other = structuredClone(testCapability);
  other.provider.instance_id = "another-instance";
  h.c.attachmentCapabilities.set(h.c.attachmentKey("a", "part"), [testCapability, other]); await h.open();
  const select = nodes(h.content).find(node => node.tagName === "SELECT")!;
  select.value = "1"; select.listeners.get("change")!();
  h.c.confirmAction = () => dialog.promise; h.button("Run fixture").click();
  select.value = "0"; select.listeners.get("change")!(); dialog.resolve(true); await tick(); await tick();
  const sent = h.calls.find(call => call.command === "attachment_capability_invoke")!;
  assert.deepEqual(sent.args.provider, { app_id: other.provider.app_id, version: "1", instance_id: "another-instance" });
});

test("an eligible selected row keeps empty/unanalysed details across loading, failed, ambiguous and vendor thread lookups", async () => {
  for (const kind of ["loading", "failed", "ambiguous", "vendor"]) {
    const h = detailsHarness(), lookup = deferred();
    Object.assign(h.rows[0], { attachments: [], summary: null, calendar_proposal: null });
    h.respond(async (command, query) => {
      if (command !== "threads_resolve") return h.defaultResponse(command, query);
      if (kind === "loading") return lookup.promise;
      if (kind === "failed") throw new Error("Thread unavailable");
      const value = await h.defaultResponse(command, query);
      if (kind === "vendor") return { ...value, context: { kind: "vendor", vendor: { vendor_id: "vendor", display_name: "Vendor" },
        thread: { ...value.context.thread, relation: "owned", owner_vendor: { vendor_id: "vendor", display_name: "Vendor" } } } };
      return { state: "choose_vendor", message_ref: h.rows[0].thread_entry_ref, thread_ref: value.context.thread.thread_ref,
        owner_vendor: { vendor_id: "v1", display_name: "One" },
        items: [{ vendor_id: "v1", display_name: "One" }, { vendor_id: "v2", display_name: "Two" }], next_cursor: null };
    });
    const opening = h.open(); if (kind !== "loading") await opening;
    assert.equal(h.section.hidden, false); assert.equal(h.ui.selection()!.row, h.rows[0]);
    assert.match(h.text(), /Local analysis has not completed yet/); assert.match(h.text(), /No action required/);
    assert.match(h.layout.source.textContent, /Origin a/); h.button("Retry analysis"); h.button("Delete locally");
    assert.equal(h.calls.length, 0);
    if (kind === "loading") { lookup.resolve(await h.defaultResponse("threads_resolve", { message_ref: h.rows[0].thread_entry_ref })); await opening; }
  }
});
