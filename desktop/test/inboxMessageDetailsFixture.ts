import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { createContext, runInContext } from "node:vm";
import { parseAst } from "vite";
import { paneHarness } from "./inboxThreadPaneFixture.ts";
import { nodes, text } from "./threadDomFixture.ts";
import { inboxThreadOriginKey } from "../src/inboxThreadEntry.ts";
import { InboxReload } from "../src/inboxReload.ts";
import { ThreadHistory } from "../src/threadHistory.ts";
import { hasInboxAutomationConfirmation, inboxActionState } from "../src/inboxActionState.ts";
import { inboxBodyTruncation } from "../src/inboxBodyTruncation.ts";
import * as availability from "../src/connectAvailability.ts";
import { activeCapabilityResult, capabilityInvocationKey, matchingCapabilityResult } from "../src/connectIdentity.ts";
import { collectCapabilityParameters } from "../src/connectParameters.ts";
import { canDecideAutomationFire, releaseAutomationRefreshFences, runAutomationDecision } from "../src/automationDecision.ts";
import { automationOutcomeIdentity, automationOutcomeStatus } from "../src/automationOutcome.ts";
import { calendarOutcomeNotice, latestCalendarOutcome } from "../src/calendarOutcome.ts";
import { inboxRemovalStatus, inboxClearView } from "../src/inboxRemoval.ts";

const source = stripTypeScriptTypes(readFileSync(new URL("../src/main.ts", import.meta.url), "utf8"));
const ast = parseAst(source);
export function productionFunctions(names: string[]): string {
  return names.map(name => {
    const node = ast.body.find(node => node.type === "FunctionDeclaration" && node.id?.name === name);
    assert.ok(node, name); return source.slice(node.start, node.end);
  }).join("\n");
}

export const testCapability = {
  protocol_version: 2, provider: { app_id: "fixture-app", version: "1", instance_id: "fixture-instance", name: "Fixture app" },
  capability: { id: "fixture.action", version: "1", action: { label: "Run fixture", description: "Fixture action" },
    parameters: [{ name: "zero", fixed_value: 0 }, { name: "no", fixed_value: false }, { name: "empty", fixed_value: "" }],
    effects: { external: true, confirmation_required: true } },
};
export const testFire = { fire_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", rule_id: "fixture-rule", rule_version: 2,
  state: "awaiting_confirmation", state_version: 3, prepared_identity_sha256: "a".repeat(64) };

/** Real production renderer/handlers, selection owner, refresh scheduler and lifecycle; synthetic IPC only. */
export function detailsHarness() {
  const h = paneHarness(), calls: { command: string; args: any }[] = [], confirmations: string[] = [];
  h.doc.body = h.el("body"); h.doc.body.append(h.layout.grid);
  const section = h.el("section"), content = h.el(), heading = h.el("h3"), notice = h.el("p"), list = h.el("ul");
  section.hidden = true; section.append(heading, h.layout.source, content);
  h.layout.readerPane.append(section); h.columns.append(list); h.doc.body.append(notice);
  const elements = new Map([["#inbox-selected-message", section], ["#inbox-selected-message-content", content],
    ["#inbox-selected-message-heading", heading], ["#inbox-action-notice", notice]]);
  const proposal = { status: "accepted", state: "awaiting_confirmation", subject: "Fixture event", expires_at: null,
    attendees: ["invitee@example.test"], account_address: "calendar@example.test", account_display_name: "Calendar",
    account_id: "calendar-account", start: "2026-12-01T10:00:00Z", end: "2026-12-01T11:00:00Z", timezone: "UTC",
    proposal_sha256: "b".repeat(64), proposal_version: 7, run_id: "calendar-run", state_version: 9 };
  for (const row of h.rows) Object.assign(row, { sender_name: "Fixture sender", admission: null, summary: `Saved ${row.message_id}`,
    action_required: 0, priority: "normal", category: "general", status: "pending", analysis_retryable: false,
    calendar_proposal: structuredClone(proposal), calendar_write_outcome: null,
    attachments: [{ part_id: "part", filename: "same-name.pdf", media_type: "application/pdf", byte_size: 0,
      capability_results: [], automation_fires: [structuredClone({ ...testFire, fire_id: row.message_id === "a"
        ? testFire.fire_id : "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" })] }] });
  let fixtures: any[] = h.rows;
  const c = createContext({ document: h.doc, window: { clearTimeout() {}, setTimeout() { return 1; } }, crypto,
    structuredClone, requiredElement: (selector: string) => { assert.ok(elements.has(selector), selector); return elements.get(selector); },
    renderedInboxSelection: null, inboxThreadEntry: h.ui, inboxList: list, inboxResults: h.listScroll,
    inboxStatus: h.el("p"), inboxLoadMore: h.el("button"), inboxProposalExpiryTimer: null,
    inboxRenderedEpoch: null, inboxScrollRevision: 0, inboxRestoredScrollTop: 0,
    inboxPageState: "loaded", inboxPageStateGeneration: 0, inboxRequestGeneration: 0, inboxNextCursor: null,
    inboxCapabilityUnavailableCount: 0, activeInboxQuery: { provider: "imap", account_id: "retained", sender: null, limit: 25 },
    configurationReady: true, inboxClearInFlight: false, inboxDeletionsInFlight: new Set(),
    attachmentCapabilities: new Map(), attachmentCapabilityDiagnostics: new Map(), attachmentSelectionRefusals: new Map(),
    attachmentInvocationsInFlight: new Set(), attachmentRequestIds: new Map(),
    capabilityOutputPresentations: new Map(), capabilityOutputPresentationsInFlight: new Set(),
    capabilityOutputPreviews: new Map(), capabilityOutputViewButtons: new Map(),
    automationDecisionsInFlight: new Set(), automationDecisionRefreshRequired: new Map(),
    inboxThreadOriginKey, hasInboxAutomationConfirmation, inboxActionState, inboxBodyTruncation,
    ...availability, activeCapabilityResult, capabilityInvocationKey, matchingCapabilityResult, collectCapabilityParameters,
    canDecideAutomationFire, releaseAutomationRefreshFences, runAutomationDecision, automationOutcomeIdentity, automationOutcomeStatus,
    calendarOutcomeNotice, latestCalendarOutcome, inboxRemovalStatus, inboxClearView,
    currentMailboxEffectScope: () => h.snapshot.scope, mailboxEffectScopeIsCurrent: () => true,
    mailboxEffectRequestIsCurrent: (generation: number, current: number) => generation === current,
    inboxReloadBlocked: () => h.snapshot.blocked, renderInboxFilterScope() {}, setInboxControlsBusy() {},
    refreshInboxRemovalStatuses: async () => {}, inboxStatusLabel: () => "Inbox refreshed", receivedLabel: (value: string) => value,
    stateLabel: () => "Pending", openConnectHealth() {},
    confirmAction: async (message: string) => { confirmations.push(message); return true; },
  });
  for (const [name, field] of [["inboxItems", "items"], ["inboxItemsEpoch", "itemsEpoch"],
    ["inboxQueryEpoch", "queryEpoch"], ["inboxThreadSpanScope", "spanScope"]] as const) {
    Object.defineProperty(c, name, { get: () => h.snapshot[field], set: value => { (h.snapshot as any)[field] = value; } });
  }
  const defaultInvoke = async (command: string, _args: any): Promise<any> => {
    if (command === "inbox_query") return { items: structuredClone(fixtures), next_cursor: null };
    if (command === "attachment_capabilities") return { items: [structuredClone(testCapability)], diagnostic: null };
    if (command === "calendar_proposal_decide") return { state: "completed" };
    if (command === "automation_fire_decide") return { state: "pending_dispatch" };
    if (command === "attachment_capability_invoke") return { status: "completed", outputs: [] };
    if (command === "attachment_open") return { filename: "same-name.pdf" };
    if (command === "capability_output_present") return { presentation: { kind: "text", text: "Fresh output" } };
    if (command === "capability_output_export") return { display_name: "Output", filename: "safe.txt" };
  };
  let invoke = defaultInvoke;
  c.invoke = async (command: string, args: any) => { calls.push({ command, args: structuredClone(args) }); return invoke(command, args); };
  runInContext(productionFunctions(["setInboxDetailButton", "runInboxCalendarDecision", "runInboxAttachmentOpen", "runInboxCapabilityAction", "refreshDecisionInbox", "runInboxAutomationRefresh", "runInboxAutomationDecision", "runInboxAnalysisRetry", "runInboxOutputAction", "attachmentKey", "capabilityOutputKey", "clearMessageOwnedUiState", "capabilityGroups",
    "renderOutputPresentation", "renderCapabilityResult", "inboxMessageStatus", "renderInboxMessageDetails",
    "renderSelectedInboxMessage", "captureInboxScroll", "restoreInboxScroll", "renderInbox", "loadAttachmentCapabilities",
    "loadInboxPage", "runInboxRefresh", "refreshInboxThreadControls", "loadInbox", "refreshLoadedInboxSpan",
    "scheduleConnectQueueRefresh", "wakeInboxReload", "inboxMutationInFlight", "deleteInboxItem", "clearInboxHistory",
    "errorCode", "errorMessage", "errorRetryable"]), c);
  c.connectQueueRefresh = null; c.connectQueueRefreshAgain = false;
  c.inboxReload = new InboxReload({ epoch: () => c.inboxQueryEpoch, blocked: c.inboxReloadBlocked,
    currentScope: c.mailboxEffectScopeIsCurrent, run: c.runInboxRefresh });
  const vendor = { refresh: async () => {}, beginMutation() {}, endMutation: async () => {} };
  c.threadHistory = new ThreadHistory(vendor as any, h.ui, () => c.refreshInboxThreadControls());
  let held = false;
  Object.defineProperty(h.snapshot, "blocked", { get: () => held || c.threadHistory.mutationHeld, set: value => { held = value; } });
  // Production callbacks, not a test reimplementation of the context decision.
  (h.ui as any).options.onSelectionChange = c.refreshInboxThreadControls;
  (h.ui as any).options.onActionNotice = (context: any, message: string, kind: string) => {
    const status = c.inboxMessageStatus(context); status.textContent = message; status.dataset.kind = kind;
  };
  for (const row of h.rows) c.attachmentCapabilities.set(c.attachmentKey(row.message_id, "part"), [structuredClone(testCapability)]);
  c.renderInbox(h.rows);
  const button = (label: string) => { const found = nodes(content).find(n => n.tagName === "BUTTON" && n.textContent === label);
    assert.ok(found, `${label}: ${text(content)}`); return found; };
  return { ...h, c, calls, confirmations, section, content, notice, list, button, text: () => text(content), defaultInvoke,
    fixtures: (items: any[]) => { fixtures = items; }, invoke: (fn: typeof invoke) => { invoke = fn; },
    open: (index = 0) => h.ui.open(h.rows[index]),
  };
}

export const tick = () => new Promise<void>(resolve => setImmediate(resolve));
