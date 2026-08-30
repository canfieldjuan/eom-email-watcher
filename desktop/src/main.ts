import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import "./styles.css";

interface WatchedSender {
  email: string;
  name: string | null;
}

interface InboxAttachment {
  part_id: string;
  attachment_id: string | null;
  filename: string;
  media_type: string;
  byte_size: number;
  capability_results: AttachmentCapabilityResult[];
}

interface ConnectWarning {
  code: string;
  message: string;
}

interface ConnectSummary {
  summary_version: string;
  text: string;
  warnings: ConnectWarning[];
}

interface AttachmentCapabilityResult {
  job_id?: string;
  capability_id: string;
  capability_version: string;
  protocol_version?: number;
  provider?: ConnectProviderIdentity;
  parameters?: Record<string, string | number | boolean>;
  status: "requested" | "accepted" | "processing" | "completed" | "failed";
  updated_at: string;
  summary: ConnectSummary | null;
  outputs?: ConnectOutputMetadata[];
  error: { code: string; message: string } | null;
}

interface ConnectProviderIdentity {
  app_id: string;
  version: string;
  instance_id: string;
}

interface ConnectProvider extends ConnectProviderIdentity {
  name: string;
}

interface ConnectCapabilityRef {
  id: string;
  version: string;
}

interface ConnectParameter {
  name: string;
  value_type: "string" | "integer" | "boolean";
  required: boolean;
  label: string;
  description: string;
}

interface ConnectCapabilityDeclaration extends ConnectCapabilityRef {
  action: { label: string; description: string };
  accepts: { media_type: string; max_bytes: number }[];
  produces: string[];
  parameters: ConnectParameter[];
  effects: { external: boolean; confirmation_required: boolean };
}

interface ConnectCapability {
  protocol_version: 2;
  provider: ConnectProvider;
  capability: ConnectCapabilityDeclaration;
}

interface ConnectOutputMetadata {
  artifact_id: string;
  media_type: string;
  display_name: string;
  byte_size: number;
  sha256: string;
}

interface ConnectCapabilities {
  items: ConnectCapability[];
  diagnostic: { code: string } | null;
}

interface ConnectInvocationResult {
  protocol_version: 2;
  job_id: string;
  provider: ConnectProviderIdentity;
  capability: ConnectCapabilityRef;
  status: "completed";
  outputs: ConnectOutputMetadata[];
}

type ConnectOutputPresentation =
  | { kind: "document_summary"; summary: ConnectSummary }
  | { kind: "text"; text: string }
  | { kind: "opaque" };

interface ConnectOutputView {
  job_id: string;
  output: ConnectOutputMetadata;
  presentation: ConnectOutputPresentation;
}

interface RevealedCapabilityOutput {
  display_name: string;
  filename: string;
}

interface InboxItem {
  message_id: string;
  received_at: string;
  sender: string;
  sender_name: string | null;
  subject: string;
  status: string;
  priority: string | null;
  summary: string | null;
  action_required: number | null;
  suggested_action: string | null;
  deadline_text: string | null;
  deadline_iso: string | null;
  fallback_notified_at: string | null;
  notified_at: string | null;
  last_error: string | null;
  analysis_retryable: boolean | null;
  analysis_error_code: string | null;
  analysis_retry_after_seconds: number | null;
  attachments: InboxAttachment[];
}

interface OpenedAttachment {
  filename: string;
}

interface GmailAuthorization {
  baseline_initialized: boolean;
  connected: boolean;
}

interface HealthStatus {
  database: {
    ok: boolean;
    initialized: boolean;
  };
  gmail: {
    credentials_configured: boolean;
    connected: boolean;
  };
  last_check: string | null;
  local_model: {
    authentication_required: boolean;
    detail: string;
    endpoint: string;
    model: string;
    ok: boolean;
    token_configured: boolean;
  };
  notifications: {
    delivery: string;
    enabled: boolean;
    host_delivery_ready: boolean;
    ntfy_configured: boolean;
  };
  production_check_supported: boolean;
  watchlist_count: number;
  polling: {
    enabled: boolean;
    interval_minutes: number;
    next_check_unix_ms: number | null;
  };
}

interface CheckResult {
  active: boolean;
  discovered: number;
  summarized: number;
  fallback_notified: number;
  purged: number;
  stale_cursor_recovered: boolean;
  pending_notifications: number;
  delivered_notifications: number;
  failed_notifications: number;
  remaining_notifications: number;
}

interface WatcherSettings {
  notifications_enabled: boolean;
  poll_interval_minutes: number;
  polling_supported: boolean;
  retention_days: number;
}

interface ConfigStatus {
  present: boolean;
}

interface ConfigInitialization {
  created: boolean;
  settings: WatcherSettings;
}

function requiredElement<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (!element) throw new Error(`Required element is missing: ${selector}`);
  return element;
}

const app = requiredElement<HTMLElement>("#app");

app.innerHTML = `
  <div class="shell">
    <header class="intro">
      <p class="eyebrow">Local email watcher</p>
      <h1>Your signal inbox</h1>
      <p class="lede">Only messages from people on your watchlist appear here.</p>
    </header>

    <nav class="view-tabs" aria-label="Watcher views">
      <button id="inbox-tab" type="button" aria-controls="inbox-view" aria-pressed="true">Inbox</button>
      <button id="watchlist-tab" type="button" aria-controls="watchlist-view" aria-pressed="false">Watchlist</button>
      <button id="health-tab" type="button" aria-controls="health-view" aria-pressed="false">Health</button>
      <button id="settings-tab" type="button" aria-controls="settings-view" aria-pressed="false">Settings</button>
    </nav>

    <section id="inbox-view" class="view" aria-labelledby="inbox-tab">
      <p id="inbox-status" class="status" role="status" aria-live="polite">Loading inbox…</p>
      <ul id="inbox-list" class="inbox-list" aria-label="Recent watched messages"></ul>
    </section>

    <section id="watchlist-view" class="view" aria-labelledby="watchlist-tab" hidden>
      <h2>Watched senders</h2>
      <p class="view-lede">Only exact email addresses on this list are analyzed.</p>
      <form id="sender-form" class="sender-form">
        <label>
          <span>Name <small>optional</small></span>
          <input id="sender-name" name="name" autocomplete="name" />
        </label>
        <label>
          <span>Email address</span>
          <input id="sender-email" name="email" type="email" autocomplete="email" required />
        </label>
        <button type="submit">Add sender</button>
      </form>

      <p id="watchlist-status" class="status" role="status" aria-live="polite">Loading watchlist…</p>
      <ul id="sender-list" class="sender-list" aria-label="Watched senders"></ul>
    </section>

    <section id="health-view" class="view" aria-labelledby="health-tab" hidden>
      <div class="view-heading">
        <div>
          <h2>Watcher health</h2>
          <p class="view-lede">A live check of the local engine and its private connections.</p>
        </div>
        <button id="check-now" class="primary-action" type="button" disabled>Check now</button>
      </div>

      <p id="health-status" class="status" role="status" aria-live="polite">Loading health…</p>
      <dl class="health-grid">
        <div class="health-card">
          <dt>Gmail</dt>
          <dd id="gmail-health">Checking…</dd>
          <dd id="gmail-detail" class="health-card-detail"></dd>
          <dd class="health-card-action">
            <button id="gmail-authorize" class="gmail-action" type="button" disabled>Connect Gmail</button>
          </dd>
        </div>
        <div class="health-card">
          <dt>Local AI</dt>
          <dd id="model-health">Checking…</dd>
          <dd id="model-detail" class="health-card-detail"></dd>
        </div>
        <div class="health-card">
          <dt>Database</dt>
          <dd id="database-health">Checking…</dd>
          <dd id="database-detail" class="health-card-detail"></dd>
        </div>
        <div class="health-card">
          <dt>Notifications</dt>
          <dd id="notification-health">Checking…</dd>
          <dd id="notification-detail" class="health-card-detail"></dd>
        </div>
      </dl>

      <dl class="health-details">
        <div><dt>Mailbox cursor updated</dt><dd id="last-check">Not initialized</dd></div>
        <div><dt>Watched senders</dt><dd id="watchlist-count">0</dd></div>
        <div><dt>Automatic polling</dt><dd id="polling-cadence">Loading…</dd></div>
        <div><dt>Next check</dt><dd id="next-check">Loading…</dd></div>
      </dl>
    </section>

    <section id="settings-view" class="view" aria-labelledby="settings-tab" hidden>
      <h2>Watcher settings</h2>
      <p class="view-lede">Change the everyday controls that are safe to manage from this app.</p>
      <form id="config-initialize-form" class="settings-form" hidden>
        <label>
          <span>Time zone</span>
          <input id="initial-timezone" name="timezone" autocomplete="off" required />
        </label>
        <label>
          <span>Local AI endpoint</span>
          <input id="initial-model-endpoint" name="modelBaseUrl" type="url" placeholder="http://127.0.0.1:8080/v1" autocomplete="url" required />
        </label>
        <label>
          <span>Model identifier</span>
          <input id="initial-model-name" name="modelName" autocomplete="off" required />
        </label>
        <p class="settings-note">The AI endpoint must be an HTTP service on this computer. Gmail connection comes next.</p>
        <button type="submit">Create watcher configuration</button>
      </form>
      <form id="settings-form" class="settings-form" hidden>
        <label>
          <span>Polling cadence <small>minutes</small></span>
          <input id="poll-interval" name="pollIntervalMinutes" type="number" min="1" max="1440" step="1" required />
        </label>
        <label>
          <span>Message retention <small>days</small></span>
          <input id="retention-days" name="retentionDays" type="number" min="1" max="3650" step="1" required />
        </label>
        <label class="settings-toggle">
          <input id="notifications-enabled" name="notificationsEnabled" type="checkbox" />
          <span>Deliver native notifications</span>
        </label>
        <p class="settings-note">Polling cadence changes apply after the app restarts. Retention and notification changes apply on later watcher operations.</p>
        <button type="submit">Save settings</button>
      </form>
      <p id="settings-status" class="status" role="status" aria-live="polite">Loading settings…</p>
    </section>
  </div>
`;

const inboxTab = requiredElement<HTMLButtonElement>("#inbox-tab");
const watchlistTab = requiredElement<HTMLButtonElement>("#watchlist-tab");
const healthTab = requiredElement<HTMLButtonElement>("#health-tab");
const settingsTab = requiredElement<HTMLButtonElement>("#settings-tab");
const inboxView = requiredElement<HTMLElement>("#inbox-view");
const watchlistView = requiredElement<HTMLElement>("#watchlist-view");
const healthView = requiredElement<HTMLElement>("#health-view");
const settingsView = requiredElement<HTMLElement>("#settings-view");
const inboxList = requiredElement<HTMLUListElement>("#inbox-list");
const inboxStatus = requiredElement<HTMLParagraphElement>("#inbox-status");
const form = requiredElement<HTMLFormElement>("#sender-form");
const emailInput = requiredElement<HTMLInputElement>("#sender-email");
const nameInput = requiredElement<HTMLInputElement>("#sender-name");
const list = requiredElement<HTMLUListElement>("#sender-list");
const watchlistStatus = requiredElement<HTMLParagraphElement>("#watchlist-status");
const healthStatus = requiredElement<HTMLParagraphElement>("#health-status");
const checkNow = requiredElement<HTMLButtonElement>("#check-now");
const gmailHealth = requiredElement<HTMLElement>("#gmail-health");
const gmailDetail = requiredElement<HTMLElement>("#gmail-detail");
const gmailAuthorize = requiredElement<HTMLButtonElement>("#gmail-authorize");
const modelHealth = requiredElement<HTMLElement>("#model-health");
const modelDetail = requiredElement<HTMLElement>("#model-detail");
const databaseHealth = requiredElement<HTMLElement>("#database-health");
const databaseDetail = requiredElement<HTMLElement>("#database-detail");
const notificationHealth = requiredElement<HTMLElement>("#notification-health");
const notificationDetail = requiredElement<HTMLElement>("#notification-detail");
const lastCheck = requiredElement<HTMLElement>("#last-check");
const watchlistCount = requiredElement<HTMLElement>("#watchlist-count");
const pollingCadence = requiredElement<HTMLElement>("#polling-cadence");
const nextCheck = requiredElement<HTMLElement>("#next-check");
const configInitializeForm = requiredElement<HTMLFormElement>("#config-initialize-form");
const initialTimezoneInput = requiredElement<HTMLInputElement>("#initial-timezone");
const initialModelEndpointInput = requiredElement<HTMLInputElement>(
  "#initial-model-endpoint",
);
const initialModelNameInput = requiredElement<HTMLInputElement>("#initial-model-name");
const settingsForm = requiredElement<HTMLFormElement>("#settings-form");
const pollIntervalInput = requiredElement<HTMLInputElement>("#poll-interval");
const retentionDaysInput = requiredElement<HTMLInputElement>("#retention-days");
const notificationsEnabledInput = requiredElement<HTMLInputElement>(
  "#notifications-enabled",
);
const settingsStatus = requiredElement<HTMLParagraphElement>("#settings-status");
let watchedSenders: WatchedSender[] = [];
let operationInFlight = true;
let checkInFlight = false;
let checkSupported = false;
let gmailAuthorizationInFlight = false;
let gmailConnected = false;
let gmailCredentialsConfigured = false;
let healthRequestGeneration = 0;
const attachmentCapabilities = new Map<string, ConnectCapability[]>();
const attachmentInvocationsInFlight = new Set<string>();
const attachmentRequestIds = new Map<string, string>();
let inboxRequestGeneration = 0;
let settingsInFlight = false;
let configurationReady = false;
let configInitializationInFlight = false;

function errorMessage(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    const message = (error as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  return "The watcher engine could not complete that request.";
}

function showView(view: "inbox" | "watchlist" | "health" | "settings"): void {
  const inboxSelected = view === "inbox";
  const watchlistSelected = view === "watchlist";
  const healthSelected = view === "health";
  const settingsSelected = view === "settings";
  inboxView.hidden = !inboxSelected;
  watchlistView.hidden = !watchlistSelected;
  healthView.hidden = !healthSelected;
  settingsView.hidden = !settingsSelected;
  inboxTab.setAttribute("aria-pressed", String(inboxSelected));
  watchlistTab.setAttribute("aria-pressed", String(watchlistSelected));
  healthTab.setAttribute("aria-pressed", String(healthSelected));
  settingsTab.setAttribute("aria-pressed", String(settingsSelected));
}

function receivedLabel(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function intervalLabel(minutes: number): string {
  if (minutes % 60 === 0) {
    const hours = minutes / 60;
    return `Every ${hours} hour${hours === 1 ? "" : "s"}`;
  }
  return `Every ${minutes} minute${minutes === 1 ? "" : "s"}`;
}

function stateLabel(item: InboxItem): string {
  if (item.status === "summarized") {
    return item.notified_at ? "Notification delivered" : "Analysis complete";
  }
  if (item.status === "skipped") return "Message unavailable";
  if (item.last_error) {
    if (item.status === "analyzed") return "Notification retry queued";
    if (item.analysis_retryable === false) {
      const reasons: Record<string, string> = {
        unauthenticated: "inference sign-in required",
        forbidden: "inference permission required",
        unsupported_task: "email analysis unavailable",
        invalid_request: "inference configuration needs repair",
        invalid_error_envelope: "inference service response incompatible",
        invalid_worker_output: "inference service output invalid",
      };
      const reason = item.analysis_error_code
        ? reasons[item.analysis_error_code] ?? `inference error: ${item.analysis_error_code}`
        : "inference configuration needs repair";
      return `Analysis paused · ${reason}`;
    }
    return "Analysis retry queued";
  }
  if (item.status === "analyzed") return "Ready to notify";
  return "Waiting for analysis";
}

function attachmentKey(messageId: string, partId: string): string {
  return JSON.stringify([messageId, partId]);
}

function canonicalCapabilityParameters(
  parameters: Record<string, string | number | boolean>,
): string {
  return JSON.stringify(
    Object.entries(parameters).sort(([left], [right]) => left.localeCompare(right)),
  );
}

function capabilityInvocationKey(
  messageId: string,
  partId: string,
  provider: ConnectProviderIdentity,
  capability: ConnectCapabilityRef,
  parameters: Record<string, string | number | boolean>,
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
  ]);
}

function capabilityGroups(
  messageId: string,
  attachment: InboxAttachment,
): ConnectCapability[][] {
  const groups = new Map<string, ConnectCapability[]>();
  for (const capability of attachmentCapabilities.get(
    attachmentKey(messageId, attachment.part_id),
  ) ?? []) {
    const key = JSON.stringify([
      capability.capability.id,
      capability.capability.version,
    ]);
    const providers = groups.get(key) ?? [];
    providers.push(capability);
    groups.set(key, providers);
  }
  return [...groups.values()];
}

function matchingCapabilityResult(
  attachment: InboxAttachment,
  selected: ConnectCapability,
  parameters?: Record<string, string | number | boolean>,
): AttachmentCapabilityResult | undefined {
  const matches = (attachment.capability_results ?? []).filter(
    (result) =>
      result.protocol_version === 2 &&
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

function activeCapabilityResult(result: AttachmentCapabilityResult | undefined): boolean {
  return ["requested", "accepted", "processing"].includes(result?.status ?? "");
}

function collectCapabilityParameters(
  capability: ConnectCapabilityDeclaration,
  initial: Record<string, string | number | boolean> = {},
): Record<string, string | number | boolean> | null {
  const values: Record<string, string | number | boolean> = {};
  for (const parameter of capability.parameters) {
    const omission = parameter.required
      ? "Cancel stops this action."
      : "Cancel leaves this optional value unset.";
    const raw = window.prompt(
      `${parameter.label}\n${parameter.description}\nExpected ${parameter.value_type}. ${omission}`,
      parameter.name in initial ? String(initial[parameter.name]) : "",
    );
    if (raw === null) {
      if (parameter.required) return null;
      continue;
    }
    if (parameter.value_type === "string") {
      values[parameter.name] = raw;
      continue;
    }
    if (parameter.value_type === "integer") {
      if (!/^-?(0|[1-9]\d*)$/.test(raw)) {
        throw new Error(`${parameter.label} must be a whole number.`);
      }
      const value = Number(raw);
      if (!Number.isSafeInteger(value)) {
        throw new Error(`${parameter.label} is outside the supported number range.`);
      }
      values[parameter.name] = value;
      continue;
    }
    const normalized = raw.trim().toLowerCase();
    if (normalized !== "true" && normalized !== "false") {
      throw new Error(`${parameter.label} must be true or false.`);
    }
    values[parameter.name] = normalized === "true";
  }
  return values;
}

function renderOutputPresentation(
  container: HTMLDivElement,
  presentation: ConnectOutputPresentation,
): void {
  container.replaceChildren();
  if (presentation.kind === "document_summary") {
    const label = document.createElement("strong");
    label.textContent = "Document summary";
    const text = document.createElement("p");
    text.textContent = presentation.summary.text;
    container.append(label, text);
    if (presentation.summary.warnings.length) {
      const warnings = document.createElement("p");
      warnings.className = "attachment-summary-warnings";
      warnings.textContent = presentation.summary.warnings
        .map((warning) => warning.message)
        .join(" ");
      container.append(warnings);
    }
    return;
  }
  if (presentation.kind === "text") {
    const text = document.createElement("pre");
    text.className = "capability-output-text";
    text.textContent = presentation.text;
    container.append(text);
    return;
  }
  const unavailable = document.createElement("p");
  unavailable.textContent = "This output type has no native preview. Use the safe export.";
  container.append(unavailable);
}

function renderCapabilityResult(
  row: HTMLLIElement,
  result: AttachmentCapabilityResult,
  messageId: string,
  partId: string,
): void {
  if (result.summary) {
    const summary = document.createElement("div");
    summary.className = "attachment-summary";
    const label = document.createElement("strong");
    label.textContent = "Document summary";
    const text = document.createElement("p");
    text.textContent = result.summary.text;
    summary.append(label, text);
    if (result.summary.warnings.length) {
      const warnings = document.createElement("p");
      warnings.className = "attachment-summary-warnings";
      warnings.textContent = result.summary.warnings
        .map((warning) => warning.message)
        .join(" ");
      summary.append(warnings);
    }
    row.append(summary);
    return;
  }

  const presentation = document.createElement("div");
  presentation.className =
    result.status === "failed" ? "attachment-summary-error" : "attachment-summary";
  const label = document.createElement("strong");
  label.textContent = result.provider
    ? `${result.capability_id} · ${result.provider.app_id}`
    : result.capability_id;
  presentation.append(label);
  if (result.status === "completed") {
    for (const output of result.outputs ?? []) {
      const outputRow = document.createElement("div");
      outputRow.className = "capability-output";
      const detail = document.createElement("p");
      detail.textContent = `${output.display_name} · ${output.media_type} · ${output.byte_size.toLocaleString()} bytes`;
      outputRow.append(detail);
      if (result.job_id) {
        const jobId = result.job_id;
        const controls = document.createElement("div");
        controls.className = "capability-output-actions";
        const preview = document.createElement("div");
        preview.className = "capability-output-preview";
        if (
          output.media_type === "application/vnd.local-connect.document-summary+json" ||
          output.media_type === "text/plain"
        ) {
          const view = document.createElement("button");
          view.type = "button";
          view.textContent = "View";
          view.addEventListener("click", async () => {
            view.disabled = true;
            view.textContent = "Loading…";
            try {
              const outputView = await invoke<ConnectOutputView>("capability_output_present", {
                messageId,
                partId,
                jobId,
                artifactId: output.artifact_id,
              });
              renderOutputPresentation(preview, outputView.presentation);
              inboxStatus.textContent = `Showing ${output.display_name}.`;
              inboxStatus.dataset.kind = "success";
            } catch (error) {
              inboxStatus.textContent = errorMessage(error);
              inboxStatus.dataset.kind = "error";
            } finally {
              view.disabled = false;
              view.textContent = "View";
            }
          });
          controls.append(view);
        }
        const exportButton = document.createElement("button");
        exportButton.type = "button";
        exportButton.textContent = "Safe export";
        exportButton.addEventListener("click", async () => {
          exportButton.disabled = true;
          exportButton.textContent = "Exporting…";
          try {
            const exported = await invoke<RevealedCapabilityOutput>(
              "capability_output_export",
              {
                messageId,
                partId,
                jobId,
                artifactId: output.artifact_id,
              },
            );
            inboxStatus.textContent = `Prepared ${exported.display_name} as ${exported.filename} and opened its export folder.`;
            inboxStatus.dataset.kind = "success";
          } catch (error) {
            inboxStatus.textContent = errorMessage(error);
            inboxStatus.dataset.kind = "error";
          } finally {
            exportButton.disabled = false;
            exportButton.textContent = "Safe export";
          }
        });
        controls.append(exportButton);
        outputRow.append(controls, preview);
      }
      presentation.append(outputRow);
    }
  } else {
    const detail = document.createElement("p");
    detail.textContent =
      result.status === "failed" && result.error
        ? result.error.message
        : `Local capability status: ${result.status}`;
    presentation.append(detail);
  }
  row.append(presentation);
}

function renderInbox(items: InboxItem[]): void {
  inboxList.replaceChildren();
  if (items.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty-state";
    empty.textContent = "No watched messages yet. Add a sender in Watchlist, then run the watcher.";
    inboxList.append(empty);
    return;
  }

  for (const item of items) {
    const card = document.createElement("li");
    card.className = "inbox-card";
    const priority = item.priority?.toLowerCase() ?? "untriaged";
    card.dataset.priority = ["urgent", "high", "normal", "low"].includes(priority)
      ? priority
      : "untriaged";

    const meta = document.createElement("div");
    meta.className = "message-meta";
    const senderIdentity = document.createElement("div");
    senderIdentity.className = "message-sender";
    const sender = document.createElement("strong");
    sender.textContent = item.sender_name || item.sender;
    senderIdentity.append(sender);
    if (item.sender_name) {
      const senderAddress = document.createElement("span");
      senderAddress.textContent = item.sender;
      senderIdentity.append(senderAddress);
    }
    const received = document.createElement("time");
    received.dateTime = item.received_at;
    received.textContent = receivedLabel(item.received_at);
    meta.append(senderIdentity, received);

    const subject = document.createElement("h3");
    subject.textContent = item.subject;
    const summary = document.createElement("p");
    summary.className = "message-summary";
    summary.textContent = item.summary || "Local analysis has not completed yet.";

    const details = document.createElement("div");
    details.className = "message-details";
    if (item.action_required !== null) {
      const action = document.createElement("p");
      action.textContent =
        item.action_required === 1
          ? item.suggested_action || "Review this message."
          : "No action required.";
      action.dataset.label = "Action required";
      details.append(action);
    }
    const deadlineValue = item.deadline_text || item.deadline_iso;
    if (deadlineValue) {
      const deadline = document.createElement("p");
      deadline.textContent = deadlineValue;
      deadline.dataset.label = "Deadline";
      details.append(deadline);
    }

    const attachments = document.createElement("ul");
    attachments.className = "attachment-list";
    for (const attachment of item.attachments) {
      const row = document.createElement("li");
      const attachmentDetails = document.createElement("div");
      attachmentDetails.className = "attachment-details";
      const filename = document.createElement("strong");
      filename.textContent = attachment.filename;
      const metadata = document.createElement("span");
      const type = attachment.media_type || "Unknown type";
      const size = new Intl.NumberFormat(undefined, {
        style: "unit",
        unit: "byte",
        unitDisplay: "narrow",
        notation: "compact",
      }).format(attachment.byte_size);
      metadata.textContent = `${type} · ${size}`;
      attachmentDetails.append(filename, metadata);
      const openButton = document.createElement("button");
      openButton.type = "button";
      openButton.textContent = "Open";
      openButton.addEventListener("click", async () => {
        openButton.disabled = true;
        openButton.textContent = "Opening…";
        try {
          const opened = await invoke<OpenedAttachment>("attachment_open", {
            messageId: item.message_id,
            partId: attachment.part_id,
          });
          inboxStatus.textContent = `Opened ${opened.filename} in the default app.`;
          inboxStatus.dataset.kind = "success";
        } catch (error) {
          inboxStatus.textContent = errorMessage(error);
          inboxStatus.dataset.kind = "error";
        } finally {
          openButton.disabled = false;
          openButton.textContent = "Open";
        }
      });
      const actions = document.createElement("div");
      actions.className = "attachment-actions";
      actions.append(openButton);

      for (const result of attachment.capability_results ?? []) {
        if (result.protocol_version !== 2 || !result.provider || !result.job_id) continue;
        const key = capabilityInvocationKey(
          item.message_id,
          attachment.part_id,
          result.provider,
          { id: result.capability_id, version: result.capability_version },
          result.parameters ?? {},
        );
        if (activeCapabilityResult(result)) attachmentRequestIds.set(key, result.job_id);
        else attachmentRequestIds.delete(key);
      }

      for (const providers of capabilityGroups(item.message_id, attachment)) {
        let selected = providers.length === 1 ? providers[0] : undefined;
        let providerSelect: HTMLSelectElement | undefined;
        if (providers.length > 1) {
          providerSelect = document.createElement("select");
          providerSelect.className = "capability-provider";
          providerSelect.setAttribute(
            "aria-label",
            `Choose a provider for ${providers[0].capability.id}`,
          );
          const placeholder = document.createElement("option");
          placeholder.value = "";
          placeholder.textContent = "Choose provider…";
          providerSelect.append(placeholder);
          providers.forEach((provider, index) => {
            const option = document.createElement("option");
            option.value = String(index);
            option.textContent = `${provider.provider.name} (${provider.provider.version}) · ${provider.provider.instance_id.slice(0, 8)}`;
            providerSelect?.append(option);
          });
          actions.append(providerSelect);
        }

        const invokeButton = document.createElement("button");
        invokeButton.type = "button";
        const syncButton = (): void => {
          if (!selected) {
            invokeButton.disabled = true;
            invokeButton.textContent = "Choose provider";
            invokeButton.removeAttribute("title");
            return;
          }
          const existing = matchingCapabilityResult(attachment, selected);
          const key = capabilityInvocationKey(
            item.message_id,
            attachment.part_id,
            selected.provider,
            selected.capability,
            existing?.parameters ?? {},
          );
          const active = activeCapabilityResult(existing);
          const inFlight = attachmentInvocationsInFlight.has(key);
          invokeButton.disabled = inFlight;
          invokeButton.textContent = inFlight
            ? `Running ${selected.capability.action.label}…`
            : active
              ? `Resume ${selected.capability.action.label}`
              : selected.capability.action.label;
          invokeButton.title = selected.capability.action.description;
        };
        providerSelect?.addEventListener("change", () => {
          selected =
            providerSelect?.value === ""
              ? undefined
              : providers[Number(providerSelect?.value)];
          syncButton();
        });
        syncButton();
        invokeButton.addEventListener("click", async () => {
          const capability = selected;
          if (!capability) return;
          let parameters: Record<string, string | number | boolean> | null;
          try {
            parameters = collectCapabilityParameters(
              capability.capability,
              matchingCapabilityResult(attachment, capability)?.parameters,
            );
          } catch (error) {
            inboxStatus.textContent = errorMessage(error);
            inboxStatus.dataset.kind = "error";
            return;
          }
          if (parameters === null) return;
          const requiresConfirmation =
            capability.capability.effects.external ||
            capability.capability.effects.confirmation_required;
          const confirmed = requiresConfirmation
            ? window.confirm(
                `${capability.capability.action.label}\n${capability.capability.action.description}\n\nContinue with ${capability.provider.name}?`,
              )
            : false;
          if (requiresConfirmation && !confirmed) return;

          const key = capabilityInvocationKey(
            item.message_id,
            attachment.part_id,
            capability.provider,
            capability.capability,
            parameters,
          );
          const active = matchingCapabilityResult(attachment, capability, parameters);
          const requestId =
            (activeCapabilityResult(active) ? active?.job_id : undefined) ??
            attachmentRequestIds.get(key) ??
            crypto.randomUUID();
          attachmentRequestIds.set(key, requestId);
          attachmentInvocationsInFlight.add(key);
          invokeButton.disabled = true;
          invokeButton.textContent = `Running ${capability.capability.action.label}…`;
          inboxStatus.textContent = `Running ${capability.capability.action.label} for ${attachment.filename}…`;
          delete inboxStatus.dataset.kind;
          let result: ConnectInvocationResult | undefined;
          let invocationError: unknown;
          try {
            result = await invoke<ConnectInvocationResult>(
              "attachment_capability_invoke",
              {
                requestId,
                messageId: item.message_id,
                partId: attachment.part_id,
                provider: {
                  app_id: capability.provider.app_id,
                  version: capability.provider.version,
                  instance_id: capability.provider.instance_id,
                },
                capability: {
                  id: capability.capability.id,
                  version: capability.capability.version,
                },
                parameters,
                confirmed,
              },
            );
            attachmentRequestIds.delete(key);
          } catch (error) {
            invocationError = error;
          } finally {
            attachmentInvocationsInFlight.delete(key);
          }
          await loadInbox();
          if (result) {
            inboxStatus.textContent = `${capability.capability.action.label} completed for ${attachment.filename} with ${result.outputs.length} output${result.outputs.length === 1 ? "" : "s"}.`;
            inboxStatus.dataset.kind = "success";
          } else {
            inboxStatus.textContent = errorMessage(invocationError);
            inboxStatus.dataset.kind = "error";
          }
        });
        actions.append(invokeButton);
      }
      row.append(attachmentDetails, actions);
      for (const result of attachment.capability_results ?? []) {
        renderCapabilityResult(row, result, item.message_id, attachment.part_id);
      }
      attachments.append(row);
    }

    const footer = document.createElement("div");
    footer.className = "message-footer";
    const badge = document.createElement("span");
    badge.className = "priority-badge";
    badge.textContent = item.priority || "Untriaged";
    const state = document.createElement("span");
    state.textContent = stateLabel(item);
    footer.append(badge, state);
    if (item.status === "pending" && item.analysis_retryable === false) {
      const retryButton = document.createElement("button");
      retryButton.type = "button";
      retryButton.textContent = "Retry analysis";
      retryButton.addEventListener("click", async () => {
        retryButton.disabled = true;
        retryButton.textContent = "Retrying…";
        delete inboxStatus.dataset.kind;
        inboxStatus.textContent = `Requeueing analysis for ${item.subject}…`;
        try {
          await invoke("analysis_requeue", { messageId: item.message_id });
          await loadInbox();
          inboxStatus.textContent = `Analysis requeued for ${item.subject}.`;
          inboxStatus.dataset.kind = "success";
        } catch (error) {
          retryButton.disabled = false;
          retryButton.textContent = "Retry analysis";
          inboxStatus.textContent = errorMessage(error);
          inboxStatus.dataset.kind = "error";
        }
      });
      footer.append(retryButton);
    }

    card.append(meta, subject, summary);
    if (details.childElementCount) card.append(details);
    if (attachments.childElementCount) card.append(attachments);
    card.append(footer);
    inboxList.append(card);
  }
}

async function loadAttachmentCapabilities(
  items: InboxItem[],
): Promise<{ capabilities: Map<string, ConnectCapability[]>; unavailable: number }> {
  const attachments = items.flatMap((item) =>
    item.attachments.map((attachment) => ({
      attachment,
      messageId: item.message_id,
    })),
  );
  const capabilities = new Map<string, ConnectCapability[]>();
  const discoveries: PromiseSettledResult<ConnectCapabilities>[] = [];
  const batchSize = 4;
  for (let offset = 0; offset < attachments.length; offset += batchSize) {
    const batch = attachments.slice(offset, offset + batchSize);
    discoveries.push(
      ...(await Promise.allSettled(
        batch.map(({ attachment, messageId }) =>
          invoke<ConnectCapabilities>("attachment_capabilities", {
            messageId,
            partId: attachment.part_id,
          }),
        ),
      )),
    );
  }
  let unavailable = 0;
  discoveries.forEach((discovery, index) => {
    const target = attachments[index];
    const key = attachmentKey(target.messageId, target.attachment.part_id);
    if (discovery.status === "fulfilled") {
      capabilities.set(key, discovery.value.items);
      if (discovery.value.diagnostic) unavailable += 1;
    } else {
      capabilities.set(key, []);
      unavailable += 1;
    }
  });
  return { capabilities, unavailable };
}

async function loadInbox(): Promise<void> {
  const generation = ++inboxRequestGeneration;
  let items: InboxItem[];
  try {
    items = await invoke<InboxItem[]>("inbox_recent");
  } catch (error) {
    if (generation !== inboxRequestGeneration) return;
    attachmentCapabilities.clear();
    inboxStatus.textContent = errorMessage(error);
    inboxStatus.dataset.kind = "error";
    return;
  }
  if (generation !== inboxRequestGeneration) return;

  attachmentCapabilities.clear();
  renderInbox(items);
  inboxStatus.textContent = "Showing recent messages while local capabilities refresh.";
  delete inboxStatus.dataset.kind;
  try {
    const discovery = await loadAttachmentCapabilities(items);
    if (generation !== inboxRequestGeneration) return;
    for (const [key, capabilities] of discovery.capabilities) {
      attachmentCapabilities.set(key, capabilities);
    }
    renderInbox(items);
    inboxStatus.textContent = discovery.unavailable
      ? "Showing recent messages. Some local capability providers are unavailable."
      : "Showing the most recent watched messages.";
    inboxStatus.dataset.kind = "success";
  } catch (error) {
    if (generation !== inboxRequestGeneration) return;
    attachmentCapabilities.clear();
    renderInbox(items);
    inboxStatus.textContent = `Showing recent messages. Local capabilities could not refresh: ${errorMessage(error)}`;
    inboxStatus.dataset.kind = "warning";
  }
}

function setHealthValue(element: HTMLElement, ready: boolean, text: string): void {
  element.textContent = text;
  element.dataset.ready = String(ready);
}

function syncGmailAuthorizationAction(): void {
  gmailAuthorize.textContent = gmailAuthorizationInFlight
    ? "Connecting…"
    : gmailConnected
      ? "Reconnect Gmail"
      : "Connect Gmail";
  gmailAuthorize.disabled = gmailAuthorizationInFlight || !gmailCredentialsConfigured;
}

function renderHealthUnknown(): void {
  const detail = "Health refresh failed; current status is unknown.";
  for (const [value, description] of [
    [gmailHealth, gmailDetail],
    [modelHealth, modelDetail],
    [databaseHealth, databaseDetail],
    [notificationHealth, notificationDetail],
  ]) {
    setHealthValue(value, false, "Unknown");
    description.textContent = detail;
  }
  lastCheck.textContent = "Unknown";
  watchlistCount.textContent = "Unknown";
  pollingCadence.textContent = "Unknown";
  nextCheck.textContent = "Unknown";
  gmailConnected = false;
  gmailCredentialsConfigured = false;
  syncGmailAuthorizationAction();
}

function renderHealth(health: HealthStatus): void {
  gmailConnected = health.gmail.connected;
  gmailCredentialsConfigured = health.gmail.credentials_configured;
  syncGmailAuthorizationAction();
  const gmailReady = health.gmail.connected && health.gmail.credentials_configured;
  setHealthValue(gmailHealth, gmailReady, gmailReady ? "Configured" : "Needs attention");
  gmailDetail.textContent = gmailReady
    ? "A read-only watcher token is present."
    : health.gmail.credentials_configured
      ? "Finish Gmail authorization to start watching."
      : "Gmail credentials have not been configured.";

  setHealthValue(modelHealth, health.local_model.ok, health.local_model.ok ? "Ready" : "Unavailable");
  modelDetail.textContent = `${health.local_model.model} · ${health.local_model.endpoint} · ${health.local_model.detail}`;

  const databaseReady = health.database.ok && health.database.initialized;
  setHealthValue(databaseHealth, databaseReady, databaseReady ? "Ready" : "Needs setup");
  databaseDetail.textContent = health.database.initialized
    ? "The local inbox ledger is initialized."
    : "Run watcher setup to initialize the mailbox cursor.";

  const notificationsReady =
    !health.notifications.enabled || health.notifications.host_delivery_ready;
  setHealthValue(
    notificationHealth,
    notificationsReady,
    health.notifications.enabled ? (notificationsReady ? "Queue ready" : "Not ready") : "Disabled",
  );
  notificationDetail.textContent = health.notifications.ntfy_configured
    ? "The desktop host cannot take delivery while ntfy is configured."
    : health.notifications.enabled
      ? notificationsReady
        ? "Native desktop delivery is active; failed notifications remain durably queued."
        : "Native desktop delivery is unavailable; queued notifications remain durable."
      : "Analysis will still appear in the local inbox.";

  lastCheck.textContent = health.last_check ? receivedLabel(health.last_check) : "Not initialized";
  watchlistCount.textContent = String(health.watchlist_count);
  if (health.polling.enabled && health.polling.next_check_unix_ms !== null) {
    pollingCadence.textContent = intervalLabel(health.polling.interval_minutes);
    nextCheck.textContent = receivedLabel(
      new Date(health.polling.next_check_unix_ms).toISOString(),
    );
  } else {
    pollingCadence.textContent = "Disabled for current configuration";
    nextCheck.textContent = "Not scheduled";
  }
  const watcherPrerequisitesReady =
    health.watchlist_count === 0 || (gmailReady && databaseReady);
  checkSupported =
    health.production_check_supported &&
    health.notifications.host_delivery_ready &&
    watcherPrerequisitesReady;
  checkNow.disabled = checkInFlight || !checkSupported;
}

async function authorizeGmail(): Promise<void> {
  if (gmailAuthorizationInFlight || !gmailCredentialsConfigured) return;
  gmailAuthorizationInFlight = true;
  syncGmailAuthorizationAction();
  healthStatus.textContent = "Complete Gmail authorization in your browser…";
  delete healthStatus.dataset.kind;
  try {
    const result = await invoke<GmailAuthorization>("gmail_authorize");
    const message = result.baseline_initialized
      ? "Gmail connected. Watching begins from the current mailbox state."
      : "Gmail connection verified. The existing mailbox position was preserved.";
    await loadHealth(message);
  } catch (error) {
    await loadHealth(errorMessage(error), "error");
  } finally {
    gmailAuthorizationInFlight = false;
    syncGmailAuthorizationAction();
  }
}

async function loadHealth(
  message = "Health is up to date.",
  kind: "success" | "error" = "success",
): Promise<boolean> {
  const requestGeneration = ++healthRequestGeneration;
  checkSupported = false;
  checkNow.disabled = true;
  healthStatus.textContent = "Refreshing health…";
  try {
    const health = await invoke<HealthStatus>("health_get");
    if (requestGeneration !== healthRequestGeneration) return false;
    renderHealth(health);
    healthStatus.textContent = message;
    healthStatus.dataset.kind = kind;
    return true;
  } catch (error) {
    if (requestGeneration !== healthRequestGeneration) return false;
    checkSupported = false;
    checkNow.disabled = true;
    renderHealthUnknown();
    healthStatus.textContent = errorMessage(error);
    healthStatus.dataset.kind = "error";
    return false;
  }
}

function checkResultMessage(result: CheckResult): string {
  const delivered = result.delivered_notifications;
  const deliveryMessage = delivered
    ? ` ${delivered} notification${delivered === 1 ? " was" : "s were"} delivered.`
    : "";
  const failed = result.failed_notifications;
  const failureMessage = failed
    ? ` ${failed} delivery attempt${failed === 1 ? " failed" : "s failed"}.`
    : "";
  const remaining = result.remaining_notifications;
  const queueMessage = remaining
    ? ` ${remaining} notification${remaining === 1 ? " remains" : "s remain"} queued.`
    : "";
  if (!result.active) {
    return `Add a watched sender before running a check.${deliveryMessage}${failureMessage}${queueMessage}`;
  }
  const summary = `Check complete: ${result.discovered} found, ${result.summarized} analyzed.`;
  return `${summary}${deliveryMessage}${failureMessage}${queueMessage}`;
}

async function runCheck(): Promise<void> {
  if (checkInFlight) return;
  checkInFlight = true;
  checkNow.disabled = true;
  healthStatus.textContent = "Checking watched mail…";
  try {
    const result = await invoke<CheckResult>("watcher_check");
    const message = checkResultMessage(result);
    await Promise.all([loadInbox(), loadHealth(message)]);
  } catch (error) {
    await Promise.all([loadInbox(), loadHealth(errorMessage(error), "error")]);
  } finally {
    checkInFlight = false;
    checkNow.disabled = !checkSupported;
  }
}

function renderSettings(settings: WatcherSettings): void {
  pollIntervalInput.value = String(settings.poll_interval_minutes);
  retentionDaysInput.value = String(settings.retention_days);
  notificationsEnabledInput.checked = settings.notifications_enabled;
}

function setConfigInitializationBusy(busy: boolean): void {
  configInitializationInFlight = busy;
  for (const control of configInitializeForm.elements) {
    if (control instanceof HTMLInputElement || control instanceof HTMLButtonElement) {
      control.disabled = busy;
    }
  }
}

function setConfiguredNavigation(enabled: boolean): void {
  inboxTab.disabled = !enabled;
  watchlistTab.disabled = !enabled;
  healthTab.disabled = !enabled;
}

function startConfiguredDesktop(): void {
  configurationReady = true;
  setConfiguredNavigation(true);
  configInitializeForm.hidden = true;
  settingsForm.hidden = false;
  void loadInbox();
  void loadHealth();
  void loadSenders().then((loaded) => {
    if (loaded) finishOperation();
  });
}

async function initializeDesktop(): Promise<void> {
  setConfiguredNavigation(false);
  settingsForm.hidden = true;
  configInitializeForm.hidden = true;
  try {
    const status = await invoke<ConfigStatus>("config_status");
    if (status.present) {
      startConfiguredDesktop();
      return;
    }
    initialTimezoneInput.value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    showView("settings");
    configInitializeForm.hidden = false;
    settingsStatus.textContent = "Set the local essentials to create this watcher's configuration.";
    delete settingsStatus.dataset.kind;
    initialModelEndpointInput.focus();
  } catch (error) {
    showView("settings");
    settingsStatus.textContent = errorMessage(error);
    settingsStatus.dataset.kind = "error";
  }
}

configInitializeForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void (async () => {
    if (configInitializationInFlight) return;
    setConfigInitializationBusy(true);
    settingsStatus.textContent = "Creating watcher configuration…";
    delete settingsStatus.dataset.kind;
    try {
      const result = await invoke<ConfigInitialization>("config_initialize", {
        modelBaseUrl: initialModelEndpointInput.value,
        modelName: initialModelNameInput.value,
        timezone: initialTimezoneInput.value,
      });
      if (!result.created) throw new Error("Watcher configuration was not created.");
      renderSettings(result.settings);
      startConfiguredDesktop();
      settingsStatus.textContent =
        "Configuration created. Restart the app once to enable automatic polling.";
      settingsStatus.dataset.kind = "success";
    } catch (error) {
      settingsStatus.textContent = errorMessage(error);
      settingsStatus.dataset.kind = "error";
    } finally {
      setConfigInitializationBusy(false);
    }
  })();
});

function setSettingsBusy(busy: boolean): void {
  settingsInFlight = busy;
  for (const control of settingsForm.elements) {
    if (control instanceof HTMLInputElement || control instanceof HTMLButtonElement) {
      control.disabled = busy;
    }
  }
}

async function loadSettings(): Promise<void> {
  if (settingsInFlight) return;
  setSettingsBusy(true);
  settingsStatus.textContent = "Loading settings…";
  delete settingsStatus.dataset.kind;
  try {
    const settings = await invoke<WatcherSettings>("settings_get");
    renderSettings(settings);
    settingsStatus.textContent = "Settings are up to date.";
    settingsStatus.dataset.kind = "success";
  } catch (error) {
    settingsStatus.textContent = errorMessage(error);
    settingsStatus.dataset.kind = "error";
  } finally {
    setSettingsBusy(false);
  }
}

settingsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void (async () => {
    if (settingsInFlight) return;
    setSettingsBusy(true);
    settingsStatus.textContent = "Saving settings…";
    delete settingsStatus.dataset.kind;
    try {
      const settings = await invoke<WatcherSettings>("settings_update", {
        notificationsEnabled: notificationsEnabledInput.checked,
        pollIntervalMinutes: Number(pollIntervalInput.value),
        retentionDays: Number(retentionDaysInput.value),
      });
      renderSettings(settings);
      settingsStatus.textContent =
        "Settings saved. Restart the app to use the new polling cadence.";
      settingsStatus.dataset.kind = "success";
    } catch (error) {
      settingsStatus.textContent = errorMessage(error);
      settingsStatus.dataset.kind = "error";
    } finally {
      setSettingsBusy(false);
    }
  })();
});

function setBusy(busy: boolean): void {
  for (const control of form.elements) {
    if (control instanceof HTMLInputElement || control instanceof HTMLButtonElement) {
      control.disabled = busy;
    }
  }
  for (const button of list.querySelectorAll<HTMLButtonElement>("button")) {
    button.disabled = busy;
  }
}

function beginOperation(): boolean {
  if (operationInFlight) return false;
  operationInFlight = true;
  setBusy(true);
  return true;
}

function finishOperation(): void {
  operationInFlight = false;
  setBusy(false);
}

function renderSenders(senders: WatchedSender[]): void {
  watchedSenders = senders;
  list.replaceChildren();
  if (senders.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty-state";
    empty.textContent = "No watched senders yet. Add the first exact address above.";
    list.append(empty);
    return;
  }

  for (const sender of senders) {
    const item = document.createElement("li");
    item.className = "sender-card";

    const identity = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = sender.name || sender.email;
    identity.append(title);
    if (sender.name) {
      const email = document.createElement("span");
      email.textContent = sender.email;
      identity.append(email);
    }

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-button";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", `Remove ${sender.name || sender.email}`);
    remove.addEventListener("click", () => void removeSender(sender.email));

    item.append(identity, remove);
    list.append(item);
  }
}

async function loadSenders(message = "Watchlist is up to date."): Promise<boolean> {
  try {
    const senders = await invoke<WatchedSender[]>("watchlist_list");
    renderSenders(senders);
    watchlistStatus.textContent = message;
    watchlistStatus.dataset.kind = "success";
    return true;
  } catch (error) {
    watchlistStatus.textContent = errorMessage(error);
    watchlistStatus.dataset.kind = "error";
    return false;
  }
}

async function removeSender(email: string): Promise<void> {
  if (!beginOperation()) return;
  watchlistStatus.textContent = `Removing ${email}…`;
  try {
    const removed = await invoke<WatchedSender>("watchlist_remove", { email });
    renderSenders(watchedSenders.filter((sender) => sender.email !== removed.email));
    watchlistStatus.textContent = `${removed.email} is no longer watched.`;
    watchlistStatus.dataset.kind = "success";
  } catch (error) {
    watchlistStatus.textContent = errorMessage(error);
    watchlistStatus.dataset.kind = "error";
  } finally {
    finishOperation();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void (async () => {
    if (!beginOperation()) return;
    watchlistStatus.textContent = "Adding sender…";
    let addedSuccessfully = false;
    try {
      const sender = await invoke<WatchedSender>("watchlist_add", {
        email: emailInput.value,
        name: nameInput.value.trim() || null,
      });
      form.reset();
      renderSenders([...watchedSenders, sender]);
      watchlistStatus.textContent = `${sender.email} is now watched.`;
      watchlistStatus.dataset.kind = "success";
      addedSuccessfully = true;
    } catch (error) {
      watchlistStatus.textContent = errorMessage(error);
      watchlistStatus.dataset.kind = "error";
    } finally {
      finishOperation();
      if (addedSuccessfully) emailInput.focus();
    }
  })();
});

setBusy(true);
inboxTab.addEventListener("click", () => showView("inbox"));
watchlistTab.addEventListener("click", () => showView("watchlist"));
healthTab.addEventListener("click", () => {
  showView("health");
  void loadHealth();
});
settingsTab.addEventListener("click", () => {
  showView("settings");
  if (configurationReady) void loadSettings();
});
checkNow.addEventListener("click", () => void runCheck());
gmailAuthorize.addEventListener("click", () => void authorizeGmail());
void listen<{
  status: "complete" | "delivery_failed" | "check_failed";
  failed_notifications: number;
}>("watcher://scheduled-check", (event) => {
  void loadInbox();
  if (!healthView.hidden) {
    if (event.payload.status === "complete") {
      void loadHealth("Automatic check complete.", "success");
    } else if (event.payload.status === "delivery_failed") {
      const count = event.payload.failed_notifications;
      void loadHealth(
        `Automatic check complete, but ${count} notification${count === 1 ? "" : "s"} remain queued.`,
        "error",
      );
    } else {
      void loadHealth("Automatic check failed; it will retry on schedule.", "error");
    }
  }
});
window.addEventListener("focus", () => {
  if (configurationReady) void loadInbox();
});
void initializeDesktop();
