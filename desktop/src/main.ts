import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import {
  CALENDAR_CONSENT_PROFILES,
  calendarConsentControls,
  calendarConsentStateLabel,
  calendarConsentVisible,
  type CalendarConsentProfile,
  type CalendarConsentStatus,
} from "./calendarConsent";
import { classifyCapabilityDiagnostic } from "./connectAvailability";
import {
  buildMailServerConnection,
  type MailServerConnection,
  type MailServerSecurity,
} from "./mailServerConnection";
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
  provider: string;
  account_id: string;
  received_at: string;
  sender: string;
  sender_name: string | null;
  subject: string;
  status: string;
  category: string | null;
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
  calendar_proposal: CalendarProposalPreview | null;
}

interface CalendarProposalPreview {
  run_id: string;
  state:
    | "awaiting_confirmation"
    | "manual_review"
    | "declined"
    | "write_authorized"
    | "writing"
    | "unresolved"
    | "reconciling"
    | "completed"
    | "failed";
  state_version: number;
  proposal_version: number;
  proposal_sha256: string;
  status: "accepted" | "no_suggestions";
  provider: string;
  account_id: string;
  account_display_name: string;
  account_address: string | null;
  subject: string;
  attendees: string[];
  start: string | null;
  end: string | null;
  timezone: string | null;
  suggestion_reason: string | null;
  empty_reason: string | null;
  observed_at: string;
  expires_at: string | null;
  write_status: string | null;
  graph_event_id: string | null;
}

interface CalendarDecisionResult {
  run_id: string;
  state: string;
  state_version: number;
  failure_code: string | null;
  graph_event_id: string | null;
}

interface InboxQuery {
  limit: number;
  cursor: string | null;
  provider: string | null;
  account_id: string | null;
  sender_query: string | null;
  priority: string | null;
  category: string | null;
  status: string | null;
  keyword: string | null;
}

interface InboxPage {
  items: InboxItem[];
  next_cursor: string | null;
}

interface OpenedAttachment {
  filename: string;
}

type ConnectEntitlementState =
  | "active"
  | "authority_unavailable"
  | "missing"
  | "invalid"
  | "not_yet_valid"
  | "expired"
  | "feature_missing";

interface ConnectEntitlementStatus {
  state: ConnectEntitlementState;
  active: boolean;
}

interface MailProviderStatus {
  provider: string;
  display_name: string;
  connection_available: boolean;
  connection_method: "browser_oauth" | "server_credentials";
  multiple_accounts: boolean;
}

interface MailAccountStatus {
  provider: string;
  account_id: string;
  display_name: string;
  address: string | null;
  connected: boolean;
  active: boolean;
  last_check: string | null;
}

interface MailAccounts {
  providers: MailProviderStatus[];
  accounts: MailAccountStatus[];
}

interface MailAccountResult {
  account: MailAccountStatus;
  baseline_initialized?: boolean;
}

interface CalendarConsentEntry {
  account: MailAccountStatus;
  profile: CalendarConsentProfile;
  status: CalendarConsentStatus | null;
  error: string | null;
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
  mail: MailAccounts;
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
  local_model: {
    editable: boolean;
    endpoint: string;
    model: string;
  };
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

interface AutostartStatus {
  available: boolean;
  enabled: boolean;
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
      <details class="inbox-filter-panel" open>
        <summary>
          <span>Inbox filters</span>
          <span class="inbox-filter-summary">Sender, priority, topic and more</span>
        </summary>
        <form id="inbox-filter-form" class="inbox-filter-form">
          <label>
            <span>Subject or summary</span>
            <input id="inbox-keyword" name="keyword" maxlength="200" />
          </label>
          <label>
            <span>Sender</span>
            <input id="inbox-sender" name="sender" maxlength="320" />
          </label>
          <label>
            <span>Email account</span>
            <select id="inbox-account" name="account">
              <option value="active">Active account</option>
              <option value="all">All retained accounts</option>
            </select>
          </label>
          <label>
            <span>Priority</span>
            <select id="inbox-priority" name="priority">
              <option value="">Any priority</option>
              <option value="urgent">Urgent</option>
              <option value="high">High</option>
              <option value="normal">Normal</option>
              <option value="low">Low</option>
              <option value="untriaged">Untriaged</option>
            </select>
          </label>
          <label>
            <span>Topic</span>
            <select id="inbox-category" name="category">
              <option value="">Any topic</option>
              <option value="invoice">Invoice</option>
              <option value="scheduling">Scheduling</option>
              <option value="customer_request">Customer request</option>
              <option value="automated_notice">Automated notice</option>
              <option value="informational">Informational</option>
              <option value="other">Other</option>
              <option value="unclassified">Unclassified</option>
            </select>
          </label>
          <label>
            <span>Status</span>
            <select id="inbox-state" name="status">
              <option value="">Any status</option>
              <option value="pending">Pending</option>
              <option value="analyzed">Analyzed</option>
              <option value="summarized">Notified</option>
              <option value="skipped">Unavailable</option>
            </select>
          </label>
          <label>
            <span>Page size</span>
            <select id="inbox-page-size" name="pageSize">
              <option value="10">10</option>
              <option value="25" selected>25</option>
              <option value="50">50</option>
              <option value="100">100</option>
            </select>
          </label>
          <div class="inbox-filter-actions">
            <button type="submit">Apply filters</button>
            <button id="inbox-reset" class="secondary-action" type="button">Reset</button>
          </div>
        </form>
      </details>
      <p id="inbox-status" class="status" role="status" aria-live="polite">Loading inbox…</p>
      <ul id="inbox-list" class="inbox-list" aria-label="Recent watched messages"></ul>
      <div class="inbox-page-actions">
        <button id="inbox-load-more" type="button" hidden>Load more</button>
        <button id="inbox-clear" class="danger-action" type="button">Clear local history</button>
      </div>
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
        <div class="health-card mail-health-card">
          <dt>Email accounts</dt>
          <dd id="mail-health">Checking…</dd>
          <dd id="mail-detail" class="health-card-detail"></dd>
          <dd><ul id="mail-account-list" class="mail-account-list" aria-label="Email accounts"></ul></dd>
          <dd id="mail-provider-actions" class="health-card-action"></dd>
          <dd id="mail-server-panel" class="mail-server-panel" hidden>
            <form id="mail-server-form" class="mail-server-form">
              <div class="mail-server-heading">
                <div>
                  <h3 id="mail-server-title">Connect another mail server</h3>
                  <p>Use the read-only IMAP details supplied by your mail administrator.</p>
                </div>
                <button id="mail-server-cancel" class="secondary-action" type="button">Cancel</button>
              </div>
              <label>
                <span>Mailbox email address</span>
                <input id="mail-server-email" name="emailAddress" type="email" maxlength="320" autocomplete="email" required />
              </label>
              <label>
                <span>Incoming mail server</span>
                <input id="mail-server-host" name="host" maxlength="253" placeholder="mail.example.com" autocomplete="off" required />
              </label>
              <div class="mail-server-row">
                <label>
                  <span>Security</span>
                  <select id="mail-server-security" name="security">
                    <option value="tls" selected>TLS</option>
                    <option value="starttls">STARTTLS</option>
                  </select>
                </label>
                <label>
                  <span>Port</span>
                  <input id="mail-server-port" name="port" type="number" min="1" max="65535" step="1" value="993" required />
                </label>
              </div>
              <label>
                <span>Username</span>
                <input id="mail-server-username" name="username" maxlength="320" autocomplete="username" required />
              </label>
              <label>
                <span>Password or app password</span>
                <input id="mail-server-password" name="password" type="password" maxlength="4096" autocomplete="current-password" required />
              </label>
              <div class="mail-server-ca">
                <button id="mail-server-ca-choose" class="secondary-action" type="button">Choose private CA</button>
                <button id="mail-server-ca-clear" class="secondary-action" type="button" hidden>Clear CA</button>
                <span id="mail-server-ca-label">System trust store</span>
              </div>
              <p class="mail-server-note">Passwords stay in the local engine credential store. Source email remains unchanged.</p>
              <button id="mail-server-submit" type="submit">Connect account</button>
            </form>
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
        <div class="health-card connect-health-card">
          <dt>Connect</dt>
          <dd id="connect-health">Checking…</dd>
          <dd id="connect-detail" class="health-card-detail">Looking for your license…</dd>
          <dd class="health-card-action">
            <button id="connect-activate" class="connect-action" type="button" hidden>Activate</button>
          </dd>
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
          <span>Local AI endpoint</span>
          <input id="model-endpoint" name="modelBaseUrl" type="url" autocomplete="url" required />
        </label>
        <label>
          <span>Model identifier</span>
          <input id="model-name" name="modelName" autocomplete="off" required />
        </label>
        <p id="model-settings-note" class="settings-note"></p>
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
        <label class="settings-toggle">
          <input id="start-at-login" name="startAtLogin" type="checkbox" disabled />
          <span>Start Email Watcher when I sign in</span>
        </label>
        <p id="autostart-settings-note" class="settings-note">Checking start-on-login status…</p>
        <p class="settings-note">Polling cadence changes apply after the app restarts. Retention changes remove expired local history immediately. Source email is never deleted.</p>
        <button type="submit">Save settings</button>
      </form>
      <p id="settings-status" class="status" role="status" aria-live="polite">Loading settings…</p>
      <section id="calendar-consent-settings" class="calendar-consent-settings" hidden>
        <div class="calendar-consent-heading">
          <h3>Microsoft calendar access</h3>
          <p>Each permission is separate from mailbox reading. Email Watcher never creates an event without a later explicit confirmation.</p>
        </div>
        <p id="calendar-consent-status" class="status" role="status" aria-live="polite"></p>
        <div id="calendar-consent-list" class="calendar-consent-list"></div>
      </section>
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
const inboxFilterForm = requiredElement<HTMLFormElement>("#inbox-filter-form");
const inboxKeywordInput = requiredElement<HTMLInputElement>("#inbox-keyword");
const inboxSenderInput = requiredElement<HTMLInputElement>("#inbox-sender");
const inboxAccountSelect = requiredElement<HTMLSelectElement>("#inbox-account");
const inboxPrioritySelect = requiredElement<HTMLSelectElement>("#inbox-priority");
const inboxCategorySelect = requiredElement<HTMLSelectElement>("#inbox-category");
const inboxStateSelect = requiredElement<HTMLSelectElement>("#inbox-state");
const inboxPageSizeSelect = requiredElement<HTMLSelectElement>("#inbox-page-size");
const inboxReset = requiredElement<HTMLButtonElement>("#inbox-reset");
const inboxLoadMore = requiredElement<HTMLButtonElement>("#inbox-load-more");
const inboxClear = requiredElement<HTMLButtonElement>("#inbox-clear");
const form = requiredElement<HTMLFormElement>("#sender-form");
const emailInput = requiredElement<HTMLInputElement>("#sender-email");
const nameInput = requiredElement<HTMLInputElement>("#sender-name");
const list = requiredElement<HTMLUListElement>("#sender-list");
const watchlistStatus = requiredElement<HTMLParagraphElement>("#watchlist-status");
const healthStatus = requiredElement<HTMLParagraphElement>("#health-status");
const checkNow = requiredElement<HTMLButtonElement>("#check-now");
const mailHealth = requiredElement<HTMLElement>("#mail-health");
const mailDetail = requiredElement<HTMLElement>("#mail-detail");
const mailAccountList = requiredElement<HTMLUListElement>("#mail-account-list");
const mailProviderActions = requiredElement<HTMLElement>("#mail-provider-actions");
const mailServerPanel = requiredElement<HTMLElement>("#mail-server-panel");
const mailServerForm = requiredElement<HTMLFormElement>("#mail-server-form");
const mailServerTitle = requiredElement<HTMLElement>("#mail-server-title");
const mailServerCancel = requiredElement<HTMLButtonElement>("#mail-server-cancel");
const mailServerEmail = requiredElement<HTMLInputElement>("#mail-server-email");
const mailServerHost = requiredElement<HTMLInputElement>("#mail-server-host");
const mailServerSecurity = requiredElement<HTMLSelectElement>("#mail-server-security");
const mailServerPort = requiredElement<HTMLInputElement>("#mail-server-port");
const mailServerUsername = requiredElement<HTMLInputElement>("#mail-server-username");
const mailServerPassword = requiredElement<HTMLInputElement>("#mail-server-password");
const mailServerCaChoose = requiredElement<HTMLButtonElement>("#mail-server-ca-choose");
const mailServerCaClear = requiredElement<HTMLButtonElement>("#mail-server-ca-clear");
const mailServerCaLabel = requiredElement<HTMLElement>("#mail-server-ca-label");
const mailServerSubmit = requiredElement<HTMLButtonElement>("#mail-server-submit");
const modelHealth = requiredElement<HTMLElement>("#model-health");
const modelDetail = requiredElement<HTMLElement>("#model-detail");
const databaseHealth = requiredElement<HTMLElement>("#database-health");
const databaseDetail = requiredElement<HTMLElement>("#database-detail");
const notificationHealth = requiredElement<HTMLElement>("#notification-health");
const notificationDetail = requiredElement<HTMLElement>("#notification-detail");
const connectHealth = requiredElement<HTMLElement>("#connect-health");
const connectDetail = requiredElement<HTMLElement>("#connect-detail");
const connectActivate = requiredElement<HTMLButtonElement>("#connect-activate");
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
const modelEndpointInput = requiredElement<HTMLInputElement>("#model-endpoint");
const modelNameInput = requiredElement<HTMLInputElement>("#model-name");
const modelSettingsNote = requiredElement<HTMLParagraphElement>("#model-settings-note");
const pollIntervalInput = requiredElement<HTMLInputElement>("#poll-interval");
const retentionDaysInput = requiredElement<HTMLInputElement>("#retention-days");
const notificationsEnabledInput = requiredElement<HTMLInputElement>(
  "#notifications-enabled",
);
const autostartEnabledInput = requiredElement<HTMLInputElement>("#start-at-login");
const autostartSettingsNote = requiredElement<HTMLParagraphElement>(
  "#autostart-settings-note",
);
const settingsStatus = requiredElement<HTMLParagraphElement>("#settings-status");
const calendarConsentSettings = requiredElement<HTMLElement>("#calendar-consent-settings");
const calendarConsentStatus = requiredElement<HTMLParagraphElement>("#calendar-consent-status");
const calendarConsentList = requiredElement<HTMLElement>("#calendar-consent-list");
let watchedSenders: WatchedSender[] = [];
let operationInFlight = true;
let checkInFlight = false;
let checkSupported = false;
let mailOperationInFlight = false;
let mailProviders: MailProviderStatus[] = [];
let mailAccounts: MailAccountStatus[] = [];
let mailServerProvider: MailProviderStatus | null = null;
let mailServerAccount: MailAccountStatus | null = null;
let mailServerCaFile: string | null = null;
let healthRequestGeneration = 0;
let mailAccountsRequestGeneration = 0;
let mailAccountCatalogRevision = 0;
let connectInstalling = false;
let connectStatusRefreshInFlight = false;
let connectEntitlementActive: boolean | null = null;
const attachmentCapabilities = new Map<string, ConnectCapability[]>();
const attachmentCapabilityDiagnostics = new Map<string, string>();
const attachmentInvocationsInFlight = new Set<string>();
const attachmentRequestIds = new Map<string, string>();
const capabilityOutputPresentations = new Map<string, ConnectOutputPresentation>();
const capabilityOutputPresentationsInFlight = new Set<string>();
const capabilityOutputPreviews = new Map<string, HTMLDivElement>();
const capabilityOutputViewButtons = new Map<string, HTMLButtonElement>();
let inboxRequestGeneration = 0;
let inboxItems: InboxItem[] = [];
let inboxNextCursor: string | null = null;
let inboxCapabilityUnavailableCount = 0;
const inboxDeletionsInFlight = new Set<string>();
let inboxClearInFlight = false;
let activeInboxAccountSelection = "active";
let activeInboxQuery: Omit<InboxQuery, "cursor"> = {
  limit: 25,
  provider: "__no_active_account__",
  account_id: "__no_active_account__",
  sender_query: null,
  priority: null,
  category: null,
  status: null,
  keyword: null,
};
let settingsInFlight = false;
let autostartInFlight = false;
let autostartAvailable = false;
let localModelSettingsEditable = false;
let configurationReady = false;
let configInitializationInFlight = false;
let calendarConsentOperationInFlight: string | null = null;
let calendarConsentRequestGeneration = 0;

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

function capabilityOutputKey(
  messageId: string,
  partId: string,
  jobId: string,
  artifactId: string,
): string {
  return JSON.stringify([messageId, partId, jobId, artifactId]);
}

function clearMessageOwnedUiState(messageId?: string): void {
  const collections = [
    attachmentCapabilities,
    attachmentCapabilityDiagnostics,
    attachmentInvocationsInFlight,
    attachmentRequestIds,
    capabilityOutputPresentations,
    capabilityOutputPresentationsInFlight,
    capabilityOutputPreviews,
    capabilityOutputViewButtons,
  ];
  for (const collection of collections) {
    if (messageId === undefined) {
      collection.clear();
      continue;
    }
    for (const key of collection.keys()) {
      try {
        const parts: unknown = JSON.parse(key);
        if (Array.isArray(parts) && parts[0] === messageId) collection.delete(key);
      } catch {
        // Keys are constructed locally; an unrecognized key cannot be assigned
        // to a message safely, so leave it for a full-history clear.
      }
    }
  }
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
        const outputKey = capabilityOutputKey(
          messageId,
          partId,
          jobId,
          output.artifact_id,
        );
        const controls = document.createElement("div");
        controls.className = "capability-output-actions";
        const preview = document.createElement("div");
        preview.className = "capability-output-preview";
        capabilityOutputPreviews.set(outputKey, preview);
        const cachedPresentation = capabilityOutputPresentations.get(outputKey);
        if (cachedPresentation) renderOutputPresentation(preview, cachedPresentation);
        if (
          output.media_type === "application/vnd.local-connect.document-summary+json" ||
          output.media_type === "text/plain"
        ) {
          const view = document.createElement("button");
          view.type = "button";
          const syncViewButton = (button: HTMLButtonElement): void => {
            const loading = capabilityOutputPresentationsInFlight.has(outputKey);
            button.disabled = loading;
            button.textContent = loading ? "Loading…" : "View";
          };
          capabilityOutputViewButtons.set(outputKey, view);
          syncViewButton(view);
          view.addEventListener("click", async () => {
            if (capabilityOutputPresentationsInFlight.has(outputKey)) return;
            capabilityOutputPresentationsInFlight.add(outputKey);
            syncViewButton(view);
            let presentationError: unknown;
            try {
              const outputView = await invoke<ConnectOutputView>("capability_output_present", {
                messageId,
                partId,
                jobId,
                artifactId: output.artifact_id,
              });
              const currentPreview = capabilityOutputPreviews.get(outputKey);
              if (currentPreview?.isConnected) {
                capabilityOutputPresentations.set(outputKey, outputView.presentation);
                renderOutputPresentation(currentPreview, outputView.presentation);
              }
            } catch (error) {
              presentationError = error;
            } finally {
              capabilityOutputPresentationsInFlight.delete(outputKey);
              const currentButton = capabilityOutputViewButtons.get(outputKey);
              if (currentButton) syncViewButton(currentButton);
            }
            if (presentationError) {
              inboxStatus.textContent = errorMessage(presentationError);
              inboxStatus.dataset.kind = "error";
            } else if (capabilityOutputPreviews.get(outputKey)?.isConnected) {
              inboxStatus.textContent = `Showing ${output.display_name}.`;
              inboxStatus.dataset.kind = "success";
            } else {
              inboxStatus.textContent = `${output.display_name} is no longer in the current inbox.`;
              inboxStatus.dataset.kind = "warning";
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
  capabilityOutputPreviews.clear();
  capabilityOutputViewButtons.clear();
  inboxList.replaceChildren();
  if (items.length === 0) {
    capabilityOutputPresentations.clear();
    const empty = document.createElement("li");
    empty.className = "empty-state";
    const filtered = Object.entries(activeInboxQuery).some(
      ([key, value]) => key !== "limit" && value !== null,
    );
    empty.textContent = filtered
      ? "No watched messages match these filters."
      : "No watched messages yet. Add a sender in Watchlist, then run the watcher.";
    inboxList.append(empty);
    return;
  }

  for (const item of items) {
    const card = document.createElement("li");
    card.className = "inbox-card";
    card.inert = inboxMutationInFlight();
    if (card.inert) card.setAttribute("aria-busy", "true");
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
    if (activeInboxQuery.provider === null && activeInboxQuery.account_id === null) {
      const account = mailAccounts.find(
        (candidate) =>
          candidate.provider === item.provider && candidate.account_id === item.account_id,
      );
      const provider = mailProviders.find((candidate) => candidate.provider === item.provider);
      const sourceAccount = document.createElement("span");
      sourceAccount.textContent = `Mailbox: ${
        account?.address ||
        (account ? `${account.display_name} · ${account.account_id}` : undefined) ||
        `${provider?.display_name || item.provider} · ${item.account_id}`
      }`;
      senderIdentity.append(sourceAccount);
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

    let calendarProposal: HTMLElement | null = null;
    if (item.calendar_proposal) {
      const proposal = item.calendar_proposal;
      calendarProposal = document.createElement("section");
      calendarProposal.className = "calendar-proposal";
      calendarProposal.setAttribute("aria-label", "Calendar proposal");
      const heading = document.createElement("div");
      heading.className = "calendar-proposal-heading";
      const title = document.createElement("strong");
      title.textContent = "Calendar proposal";
      const state = document.createElement("span");
      const hasSuggestion = proposal.status === "accepted";
      const expired = Boolean(
        hasSuggestion && proposal.expires_at && Date.parse(proposal.expires_at) <= Date.now(),
      );
      const proposalStateLabels: Record<CalendarProposalPreview["state"], string> = {
        awaiting_confirmation: expired ? "Expired" : "Not confirmed",
        completed: "Created",
        declined: "Declined",
        failed: "Not created",
        manual_review: "Needs review",
        reconciling: "Reconciling",
        unresolved: "Needs reconciliation",
        write_authorized: "Authorized",
        writing: "Creating event",
      };
      state.textContent = !hasSuggestion ? "Needs review" : proposalStateLabels[proposal.state];
      state.dataset.expired = String(expired);
      heading.append(title, state);
      const subject = document.createElement("p");
      subject.textContent = `Event title: ${proposal.subject}`;
      const timing = document.createElement("p");
      if (!hasSuggestion) {
        timing.textContent = proposal.empty_reason || "No meeting time satisfied the request.";
      } else if (proposal.start && proposal.end && proposal.timezone) {
        try {
          const formatter = new Intl.DateTimeFormat(undefined, {
            weekday: "short",
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
            timeZone: proposal.timezone,
            timeZoneName: "short",
          });
          timing.textContent = `${formatter.format(new Date(proposal.start))} – ${formatter.format(new Date(proposal.end))} (${proposal.timezone}) · Exact interval: ${proposal.start} – ${proposal.end}`;
        } catch {
          timing.textContent = `${proposal.start} – ${proposal.end} (${proposal.timezone})`;
        }
      } else {
        timing.textContent = "The calendar proposal is incomplete and needs review.";
      }
      const attendees = document.createElement("p");
      attendees.textContent = proposal.attendees.length
        ? `Attendees: ${proposal.attendees.join(", ")}`
        : "No additional attendees";
      const calendar = document.createElement("p");
      calendar.textContent = `Calendar owner: ${proposal.account_address || proposal.account_display_name} · Account identity: ${proposal.account_id}`;
      const location = document.createElement("p");
      location.textContent = "Location: Not specified";
      const onlineMeeting = document.createElement("p");
      onlineMeeting.textContent = "Teams link: No";
      const note = document.createElement("p");
      note.className = "calendar-proposal-note";
      if (proposal.state === "completed") {
        note.textContent = "The calendar event was created.";
      } else if (proposal.state === "unresolved" || proposal.state === "reconciling") {
        note.textContent =
          "The write result is uncertain. Email Watcher will reconcile it without creating a second event.";
      } else if (proposal.state === "failed") {
        note.textContent = "Microsoft definitively rejected the event creation request.";
      } else if (proposal.state === "declined") {
        note.textContent = "You declined this calendar proposal. No event was created.";
      } else {
        note.textContent = "No calendar event has been created.";
      }
      calendarProposal.append(
        heading,
        subject,
        timing,
        attendees,
        calendar,
        location,
        onlineMeeting,
        note,
      );
      if (proposal.state === "awaiting_confirmation" && hasSuggestion) {
        const actions = document.createElement("div");
        actions.className = "calendar-proposal-actions";
        const decline = document.createElement("button");
        decline.type = "button";
        decline.className = "secondary";
        decline.textContent = "Decline";
        const confirm = document.createElement("button");
        confirm.type = "button";
        confirm.textContent = expired ? "Recheck proposal" : "Create event";
        const decide = async (decision: "confirm" | "decline"): Promise<void> => {
          if (decision === "confirm") {
            const invitationWarning = proposal.attendees.length
              ? ` This will send meeting invitations from ${proposal.account_address || proposal.account_display_name} to ${proposal.attendees.join(", ")}.`
              : "";
            const confirmationMessage = expired
              ? `Recheck the expired proposal “${proposal.subject}” before creating it? If Email Watcher still considers it valid, this confirmation will create the event on ${proposal.account_address || proposal.account_display_name}; otherwise it will refresh the proposal without creating an event.${invitationWarning}`
              : `Create “${proposal.subject}” on ${proposal.account_address || proposal.account_display_name}?${invitationWarning}`;
            if (
              !window.confirm(confirmationMessage)
            ) {
              return;
            }
          }
          confirm.disabled = true;
          decline.disabled = true;
          try {
            const result = await invoke<CalendarDecisionResult>("calendar_proposal_decide", {
              decision,
              messageId: item.message_id,
              proposalSha256: proposal.proposal_sha256,
              proposalVersion: proposal.proposal_version,
              runId: proposal.run_id,
              stateVersion: proposal.state_version,
            });
            if (result.state === "completed") {
              inboxStatus.textContent = "Calendar event created.";
              inboxStatus.dataset.kind = "success";
            } else if (result.state === "declined") {
              inboxStatus.textContent = "Calendar proposal declined.";
              inboxStatus.dataset.kind = "success";
            } else if (result.state === "failed") {
              inboxStatus.textContent = "Microsoft rejected the calendar event creation.";
              inboxStatus.dataset.kind = "error";
            } else {
              inboxStatus.textContent =
                "Calendar decision saved; Email Watcher will reconcile the result.";
              inboxStatus.dataset.kind = "warning";
            }
            await loadInbox();
          } catch (error) {
            inboxStatus.textContent = errorMessage(error);
            inboxStatus.dataset.kind = "error";
            await loadInbox();
          } finally {
            confirm.disabled = false;
            decline.disabled = false;
          }
        };
        decline.addEventListener("click", () => void decide("decline"));
        confirm.addEventListener("click", () => void decide("confirm"));
        actions.append(decline, confirm);
        calendarProposal.append(actions);
      }
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

      const diagnostic = attachmentCapabilityDiagnostics.get(
        attachmentKey(item.message_id, attachment.part_id),
      );
      if (classifyCapabilityDiagnostic(diagnostic ?? null) === "locked") {
        const locked = document.createElement("span");
        locked.className = "capability-locked";
        locked.textContent = "Connect actions locked";
        locked.title = "Activate Connect to discover compatible actions from local apps.";

        const viewConnect = document.createElement("button");
        viewConnect.type = "button";
        viewConnect.textContent = "View Connect";
        viewConnect.addEventListener("click", () => {
          showView("health");
          connectHealth.scrollIntoView({ block: "center" });
          if (!connectActivate.hidden) connectActivate.focus();
        });
        actions.append(locked, viewConnect);
      }

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
    const badges = document.createElement("div");
    badges.className = "message-badges";
    const badge = document.createElement("span");
    badge.className = "priority-badge";
    badge.textContent = item.priority || "Untriaged";
    const category = document.createElement("span");
    category.className = "category-badge";
    category.textContent = (item.category || "Unclassified").replace(/_/g, " ");
    badges.append(badge, category);
    const state = document.createElement("span");
    state.textContent = stateLabel(item);
    const footerActions = document.createElement("div");
    footerActions.className = "message-footer-actions";
    footerActions.append(state);
    footer.append(badges, footerActions);
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
      footerActions.append(retryButton);
    }
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "remove-button";
    deleteButton.textContent = inboxDeletionsInFlight.has(item.message_id)
      ? "Deleting…"
      : "Delete locally";
    deleteButton.disabled = inboxMutationInFlight();
    deleteButton.addEventListener("click", () => void deleteInboxItem(item));
    footerActions.append(deleteButton);

    card.append(meta, subject, summary);
    if (details.childElementCount) card.append(details);
    if (calendarProposal) card.append(calendarProposal);
    if (attachments.childElementCount) card.append(attachments);
    card.append(footer);
    inboxList.append(card);
  }
  for (const key of capabilityOutputPresentations.keys()) {
    if (!capabilityOutputPreviews.has(key)) capabilityOutputPresentations.delete(key);
  }
}

async function loadAttachmentCapabilities(
  items: InboxItem[],
): Promise<{
  capabilities: Map<string, ConnectCapability[]>;
  diagnostics: Map<string, string>;
  unavailable: number;
}> {
  const attachments = items.flatMap((item) =>
    item.attachments.map((attachment) => ({
      attachment,
      messageId: item.message_id,
    })),
  );
  const capabilities = new Map<string, ConnectCapability[]>();
  const diagnostics = new Map<string, string>();
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
      const diagnostic = discovery.value.diagnostic?.code ?? null;
      if (diagnostic !== null) diagnostics.set(key, diagnostic);
      if (classifyCapabilityDiagnostic(diagnostic) === "unavailable") unavailable += 1;
    } else {
      capabilities.set(key, []);
      unavailable += 1;
    }
  });
  return { capabilities, diagnostics, unavailable };
}

function optionalFilterValue(value: string): string | null {
  const trimmed = value.trim();
  return trimmed || null;
}

function accountOptionValue(account: MailAccountStatus): string {
  return JSON.stringify([account.provider, account.account_id]);
}

const unavailableAccountFilter = {
  provider: "__no_active_account__",
  account_id: "__no_active_account__",
};

function inboxAccountForSelection(
  selection: string,
): Pick<InboxQuery, "provider" | "account_id"> {
  if (selection === "all") return { provider: null, account_id: null };
  if (selection === "active") {
    const account = mailAccounts.find((candidate) => candidate.active);
    return account
      ? { provider: account.provider, account_id: account.account_id }
      : unavailableAccountFilter;
  }
  try {
    const parsed: unknown = JSON.parse(selection);
    if (
      Array.isArray(parsed) &&
      parsed.length === 2 &&
      parsed.every((value) => typeof value === "string")
    ) {
      return { provider: parsed[0], account_id: parsed[1] };
    }
  } catch {
    // A stale option falls back to the current active account.
  }
  const active = mailAccounts.find((candidate) => candidate.active);
  return active
    ? { provider: active.provider, account_id: active.account_id }
    : unavailableAccountFilter;
}

function queryFromInboxControls(): Omit<InboxQuery, "cursor"> {
  const account = inboxAccountForSelection(inboxAccountSelect.value);
  return {
    limit: Number(inboxPageSizeSelect.value),
    provider: account.provider,
    account_id: account.account_id,
    sender_query: optionalFilterValue(inboxSenderInput.value),
    priority: optionalFilterValue(inboxPrioritySelect.value),
    category: optionalFilterValue(inboxCategorySelect.value),
    status: optionalFilterValue(inboxStateSelect.value),
    keyword: optionalFilterValue(inboxKeywordInput.value),
  };
}

function clearInboxPageForAccountChange(message: string): void {
  inboxRequestGeneration += 1;
  inboxItems = [];
  inboxNextCursor = null;
  inboxCapabilityUnavailableCount = 0;
  clearMessageOwnedUiState();
  inboxLoadMore.hidden = true;
  renderInbox(inboxItems);
  inboxStatus.textContent = message;
  delete inboxStatus.dataset.kind;
}

function commitInboxQueryFromControls(): void {
  const nextQuery = queryFromInboxControls();
  const accountScopeChanged =
    activeInboxQuery.provider !== nextQuery.provider ||
    activeInboxQuery.account_id !== nextQuery.account_id;
  activeInboxAccountSelection = inboxAccountSelect.value;
  activeInboxQuery = nextQuery;
  if (accountScopeChanged) {
    clearInboxPageForAccountChange("Email account filter changed. Refreshing local history…");
  }
}

function inboxMutationInFlight(): boolean {
  return inboxClearInFlight || inboxDeletionsInFlight.size > 0;
}

function setInboxControlsBusy(busy: boolean): void {
  for (const control of inboxFilterForm.elements) {
    if (
      control instanceof HTMLInputElement ||
      control instanceof HTMLSelectElement ||
      control instanceof HTMLButtonElement
    ) {
      control.disabled = busy;
    }
  }
  inboxLoadMore.disabled = busy;
  inboxClear.disabled = busy || inboxMutationInFlight();
}

async function deleteInboxItem(item: InboxItem): Promise<void> {
  if (inboxMutationInFlight()) return;
  const confirmed = window.confirm(
    `Delete "${item.subject}" from Email Watcher's local history? ` +
      "Its local analysis, attachment metadata, notifications, and capability results will be removed. The source email will stay in your mailbox.",
  );
  if (!confirmed) return;

  inboxDeletionsInFlight.add(item.message_id);
  inboxRequestGeneration += 1;
  setInboxControlsBusy(true);
  renderInbox(inboxItems);
  try {
    await invoke<void>("inbox_delete", { messageId: item.message_id });
    clearMessageOwnedUiState(item.message_id);
    inboxItems = inboxItems.filter((candidate) => candidate.message_id !== item.message_id);
    renderInbox(inboxItems);
    inboxStatus.textContent = `Deleted "${item.subject}" from local history. The source email was not changed.`;
    inboxStatus.dataset.kind = "success";
  } catch (error) {
    inboxStatus.textContent = errorMessage(error);
    inboxStatus.dataset.kind = "error";
  } finally {
    inboxDeletionsInFlight.delete(item.message_id);
    setInboxControlsBusy(false);
    renderInbox(inboxItems);
  }
}

async function clearInboxHistory(): Promise<void> {
  if (inboxMutationInFlight()) return;
  const confirmed = window.confirm(
    "Clear all local Email Watcher history? This removes local analyses, attachment metadata, notifications, and capability results. Source email will stay in your mailbox.",
  );
  if (!confirmed) return;

  inboxClearInFlight = true;
  inboxRequestGeneration += 1;
  setInboxControlsBusy(true);
  renderInbox(inboxItems);
  try {
    const deleted = await invoke<number>("inbox_clear");
    inboxItems = [];
    inboxNextCursor = null;
    inboxCapabilityUnavailableCount = 0;
    clearMessageOwnedUiState();
    inboxLoadMore.hidden = true;
    renderInbox(inboxItems);
    inboxStatus.textContent = `Cleared ${deleted} local message${deleted === 1 ? "" : "s"}. Source email was not changed.`;
    inboxStatus.dataset.kind = "success";
  } catch (error) {
    inboxStatus.textContent = errorMessage(error);
    inboxStatus.dataset.kind = "error";
  } finally {
    inboxClearInFlight = false;
    setInboxControlsBusy(false);
    renderInbox(inboxItems);
  }
}

function inboxStatusLabel(): string {
  const count = inboxItems.length;
  const availability = inboxCapabilityUnavailableCount
    ? " Some local capability providers are unavailable."
    : "";
  const more = inboxNextCursor ? " More matching messages are available." : "";
  return `Showing ${count} matching message${count === 1 ? "" : "s"}.${more}${availability}`;
}

async function loadInbox(append = false): Promise<void> {
  if (inboxMutationInFlight()) return;
  if (append && !inboxNextCursor) return;
  const generation = ++inboxRequestGeneration;
  const cursor = append ? inboxNextCursor : null;
  setInboxControlsBusy(true);
  let page: InboxPage;
  try {
    page = await invoke<InboxPage>("inbox_query", {
      query: { ...activeInboxQuery, cursor },
    });
  } catch (error) {
    if (generation !== inboxRequestGeneration) return;
    if (!append) {
      inboxNextCursor = null;
      inboxLoadMore.hidden = true;
    }
    inboxStatus.textContent = errorMessage(error);
    inboxStatus.dataset.kind = "error";
    setInboxControlsBusy(false);
    return;
  }
  if (generation !== inboxRequestGeneration) return;

  if (!append) {
    attachmentCapabilities.clear();
    attachmentCapabilityDiagnostics.clear();
    inboxCapabilityUnavailableCount = 0;
    inboxItems = page.items;
  } else {
    const known = new Set(inboxItems.map((item) => item.message_id));
    inboxItems = [...inboxItems, ...page.items.filter((item) => !known.has(item.message_id))];
  }
  inboxNextCursor = page.next_cursor;
  inboxLoadMore.hidden = inboxNextCursor === null;
  renderInbox(inboxItems);
  inboxStatus.textContent = `${inboxStatusLabel()} Local capabilities are refreshing.`;
  delete inboxStatus.dataset.kind;
  try {
    const discovery = await loadAttachmentCapabilities(page.items);
    if (generation !== inboxRequestGeneration) return;
    for (const [key, capabilities] of discovery.capabilities) {
      attachmentCapabilities.set(key, capabilities);
    }
    for (const [key, diagnostic] of discovery.diagnostics) {
      attachmentCapabilityDiagnostics.set(key, diagnostic);
    }
    inboxCapabilityUnavailableCount += discovery.unavailable;
    renderInbox(inboxItems);
    inboxStatus.textContent = inboxStatusLabel();
    inboxStatus.dataset.kind = "success";
  } catch (error) {
    if (generation !== inboxRequestGeneration) return;
    renderInbox(inboxItems);
    inboxStatus.textContent = `${inboxStatusLabel()} Local capabilities could not refresh: ${errorMessage(error)}`;
    inboxStatus.dataset.kind = "warning";
  }
  if (generation === inboxRequestGeneration) setInboxControlsBusy(false);
}

function setHealthValue(element: HTMLElement, ready: boolean, text: string): void {
  element.textContent = text;
  element.dataset.ready = String(ready);
}

function providerFor(account: MailAccountStatus): MailProviderStatus | undefined {
  return mailProviders.find((provider) => provider.provider === account.provider);
}

function mailServerCaName(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).at(-1) || "Selected certificate";
}

function closeMailServerForm(): void {
  mailServerForm.reset();
  mailServerPassword.value = "";
  mailServerProvider = null;
  mailServerAccount = null;
  mailServerCaFile = null;
  mailServerCaLabel.textContent = "System trust store";
  mailServerCaClear.hidden = true;
  mailServerPanel.hidden = true;
}

function showMailServerForm(
  provider: MailProviderStatus,
  account: MailAccountStatus | null = null,
): void {
  if (mailOperationInFlight) return;
  closeMailServerForm();
  mailServerProvider = provider;
  mailServerAccount = account;
  const address = account?.address ?? "";
  mailServerEmail.value = address;
  mailServerUsername.value = address;
  mailServerSecurity.value = "tls";
  mailServerPort.value = "993";
  mailServerTitle.textContent = account
    ? `Reconnect ${address || provider.display_name}`
    : `Connect ${provider.display_name}`;
  mailServerSubmit.textContent = account ? "Reconnect account" : "Connect account";
  mailServerPanel.hidden = false;
  (address ? mailServerHost : mailServerEmail).focus();
}

async function chooseMailServerCa(): Promise<void> {
  try {
    const selected = await open({
      multiple: false,
      directory: false,
      filters: [{ name: "PEM certificates", extensions: ["pem", "crt", "cer"] }],
    });
    if (selected === null || Array.isArray(selected)) return;
    mailServerCaFile = selected;
    mailServerCaLabel.textContent = mailServerCaName(selected);
    mailServerCaClear.hidden = false;
  } catch (error) {
    healthStatus.textContent = errorMessage(error);
    healthStatus.dataset.kind = "error";
  }
}

function currentMailServerConnection(): MailServerConnection {
  return buildMailServerConnection({
    emailAddress: mailServerEmail.value,
    host: mailServerHost.value,
    port: mailServerPort.value,
    security: mailServerSecurity.value as MailServerSecurity,
    username: mailServerUsername.value,
    password: mailServerPassword.value,
    caFile: mailServerCaFile,
  });
}

function renderInboxAccountOptions(): void {
  const previous = inboxAccountSelect.value || "active";
  const active = mailAccounts.find((account) => account.active);
  const activeOption = document.createElement("option");
  activeOption.value = "active";
  activeOption.textContent = active
    ? `Active — ${active.address || active.display_name}`
    : "Active account";
  const allOption = document.createElement("option");
  allOption.value = "all";
  allOption.textContent = "All retained accounts";
  inboxAccountSelect.replaceChildren(activeOption, allOption);

  for (const account of mailAccounts) {
    const option = document.createElement("option");
    option.value = accountOptionValue(account);
    option.textContent = `${account.address || account.display_name} · ${account.display_name}`;
    inboxAccountSelect.append(option);
  }
  const values = new Set(
    Array.from(inboxAccountSelect.options, (option) => option.value),
  );
  inboxAccountSelect.value = values.has(previous) ? previous : "active";
}

function reconcileInboxAccountScope(): boolean {
  const selected = inboxAccountForSelection(activeInboxAccountSelection);
  if (
    activeInboxQuery.provider === selected.provider &&
    activeInboxQuery.account_id === selected.account_id
  ) {
    return false;
  }

  activeInboxQuery = {
    ...activeInboxQuery,
    provider: selected.provider,
    account_id: selected.account_id,
  };
  clearInboxPageForAccountChange("Active email account changed. Refreshing local history…");
  return true;
}

function renderMailAccounts(data: MailAccounts): boolean {
  mailAccountCatalogRevision += 1;
  mailProviders = data.providers;
  mailAccounts = data.accounts;
  renderInboxAccountOptions();
  const inboxScopeChanged = reconcileInboxAccountScope();
  mailAccountList.replaceChildren();
  mailProviderActions.replaceChildren();

  for (const account of mailAccounts) {
    const item = document.createElement("li");
    item.className = "mail-account-item";
    const identity = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = account.address || account.display_name;
    const detail = document.createElement("span");
    detail.textContent = `${account.display_name} · ${account.active ? "Active" : "Retained"} · ${account.connected ? "Connected" : "Disconnected"}`;
    identity.append(title, detail);

    const actions = document.createElement("div");
    actions.className = "mail-account-actions";
    const provider = providerFor(account);
    if (account.connected) {
      const disconnect = document.createElement("button");
      disconnect.type = "button";
      disconnect.textContent = "Disconnect";
      disconnect.className = "danger-action";
      disconnect.disabled = mailOperationInFlight;
      disconnect.addEventListener("click", () => void disconnectMailAccount(account));
      if (provider) {
        const reconnect = document.createElement("button");
        reconnect.type = "button";
        reconnect.textContent = "Reconnect";
        reconnect.disabled = mailOperationInFlight || !provider.connection_available;
        reconnect.addEventListener("click", () => {
          if (provider.connection_method === "server_credentials") {
            showMailServerForm(provider, account);
          } else {
            void reconnectMailAccount(account);
          }
        });
        actions.append(reconnect);
      }
      actions.append(disconnect);
      if (!account.active) {
        const activate = document.createElement("button");
        activate.type = "button";
        activate.textContent = "Use this account";
        activate.disabled = mailOperationInFlight;
        activate.addEventListener("click", () => void activateMailAccount(account));
        actions.prepend(activate);
      }
    } else if (provider) {
      const connect = document.createElement("button");
      connect.type = "button";
      connect.textContent = account.address ? "Reconnect" : "Connect";
      connect.disabled = mailOperationInFlight || !provider.connection_available;
      connect.addEventListener("click", () => {
        if (provider.connection_method === "server_credentials") {
          showMailServerForm(provider, account);
        } else if (account.address) {
          void reconnectMailAccount(account);
        } else {
          void connectMailProvider(account.provider);
        }
      });
      actions.append(connect);
    }
    item.append(identity, actions);
    mailAccountList.append(item);
  }

  for (const provider of mailProviders) {
    if (
      !provider.multiple_accounts &&
      mailAccounts.some((account) => account.provider === provider.provider)
    ) {
      continue;
    }
    const connect = document.createElement("button");
    connect.type = "button";
    connect.textContent = mailAccounts.some(
      (account) => account.provider === provider.provider,
    )
      ? `Add ${provider.display_name} account`
      : `Connect ${provider.display_name}`;
    connect.disabled = mailOperationInFlight || !provider.connection_available;
    connect.addEventListener("click", () => {
      if (provider.connection_method === "server_credentials") {
        showMailServerForm(provider);
      } else {
        void connectMailProvider(provider.provider);
      }
    });
    mailProviderActions.append(connect);
  }
  return inboxScopeChanged;
}

async function refreshAfterMailMutation(message: string): Promise<void> {
  await loadHealth(message);
  await loadInbox();
}

async function connectMailProvider(provider: string): Promise<void> {
  if (mailOperationInFlight) return;
  closeMailServerForm();
  mailOperationInFlight = true;
  renderMailAccounts({ providers: mailProviders, accounts: mailAccounts });
  healthStatus.textContent = "Complete email authorization in your browser…";
  delete healthStatus.dataset.kind;
  try {
    const result = await invoke<MailAccountResult>("mail_account_connect", { provider });
    const message = result.baseline_initialized
      ? "Email account connected. Watching begins from its current mailbox state."
      : "Email account connected. Its saved mailbox position was preserved.";
    await refreshAfterMailMutation(message);
  } catch (error) {
    await loadHealth(errorMessage(error), "error");
  } finally {
    mailOperationInFlight = false;
    renderMailAccounts({ providers: mailProviders, accounts: mailAccounts });
  }
}

async function submitMailServerConnection(): Promise<void> {
  if (mailOperationInFlight || !mailServerProvider) return;
  let connection: MailServerConnection;
  try {
    connection = currentMailServerConnection();
  } catch (error) {
    healthStatus.textContent = errorMessage(error);
    healthStatus.dataset.kind = "error";
    return;
  }
  const provider = mailServerProvider;
  const account = mailServerAccount;
  closeMailServerForm();
  mailOperationInFlight = true;
  renderMailAccounts({ providers: mailProviders, accounts: mailAccounts });
  healthStatus.textContent = account
    ? "Verifying the replacement mail server credentials…"
    : "Verifying the mail server credentials…";
  delete healthStatus.dataset.kind;
  try {
    const result = account
      ? await invoke<MailAccountResult>("mail_account_reconnect", {
          provider: provider.provider,
          accountId: account.account_id,
          connection,
        })
      : await invoke<MailAccountResult>("mail_account_connect", {
          provider: provider.provider,
          connection,
        });
    const message = result.baseline_initialized
      ? "Mail server connected. Watching begins from its current mailbox state."
      : "Mail server connected. Its saved mailbox position was preserved.";
    await refreshAfterMailMutation(message);
  } catch (error) {
    await loadHealth(errorMessage(error), "error");
  } finally {
    mailOperationInFlight = false;
    renderMailAccounts({ providers: mailProviders, accounts: mailAccounts });
  }
}

async function reconnectMailAccount(account: MailAccountStatus): Promise<void> {
  if (mailOperationInFlight) return;
  closeMailServerForm();
  mailOperationInFlight = true;
  renderMailAccounts({ providers: mailProviders, accounts: mailAccounts });
  healthStatus.textContent = "Complete email authorization in your browser…";
  delete healthStatus.dataset.kind;
  try {
    await invoke<MailAccountResult>("mail_account_reconnect", {
      provider: account.provider,
      accountId: account.account_id,
    });
    await refreshAfterMailMutation("Email account reconnected. Its saved mailbox position was preserved.");
  } catch (error) {
    await loadHealth(errorMessage(error), "error");
  } finally {
    mailOperationInFlight = false;
    renderMailAccounts({ providers: mailProviders, accounts: mailAccounts });
  }
}

async function disconnectMailAccount(account: MailAccountStatus): Promise<void> {
  if (mailOperationInFlight) return;
  const confirmed = window.confirm(
    `Disconnect ${account.address || account.display_name}? Local history remains available and source email is not changed.`,
  );
  if (!confirmed) return;
  closeMailServerForm();
  mailOperationInFlight = true;
  renderMailAccounts({ providers: mailProviders, accounts: mailAccounts });
  try {
    await invoke<MailAccountResult>("mail_account_disconnect", {
      provider: account.provider,
      accountId: account.account_id,
    });
    await refreshAfterMailMutation("Email account disconnected. Its local history was retained.");
  } catch (error) {
    await loadHealth(errorMessage(error), "error");
  } finally {
    mailOperationInFlight = false;
    renderMailAccounts({ providers: mailProviders, accounts: mailAccounts });
  }
}

async function activateMailAccount(account: MailAccountStatus): Promise<void> {
  if (mailOperationInFlight) return;
  closeMailServerForm();
  mailOperationInFlight = true;
  renderMailAccounts({ providers: mailProviders, accounts: mailAccounts });
  try {
    await invoke<MailAccountResult>("mail_account_activate", {
      provider: account.provider,
      accountId: account.account_id,
    });
    await refreshAfterMailMutation("Active email account changed.");
  } catch (error) {
    await loadHealth(errorMessage(error), "error");
  } finally {
    mailOperationInFlight = false;
    renderMailAccounts({ providers: mailProviders, accounts: mailAccounts });
  }
}

async function loadMailAccounts(): Promise<boolean> {
  const requestGeneration = ++mailAccountsRequestGeneration;
  const catalogRevision = mailAccountCatalogRevision;
  try {
    const accounts = await invoke<MailAccounts>("mail_accounts_list");
    if (
      requestGeneration !== mailAccountsRequestGeneration ||
      catalogRevision !== mailAccountCatalogRevision
    ) {
      return false;
    }
    renderMailAccounts(accounts);
    return true;
  } catch (error) {
    if (
      requestGeneration !== mailAccountsRequestGeneration ||
      catalogRevision !== mailAccountCatalogRevision
    ) {
      return false;
    }
    setHealthValue(mailHealth, false, "Unknown");
    mailDetail.textContent = errorMessage(error);
    return false;
  }
}

function renderConnectStatus(status: ConnectEntitlementStatus): void {
  connectActivate.disabled = connectInstalling;
  connectActivate.hidden = status.state === "authority_unavailable";
  connectActivate.textContent = status.active ? "Replace license" : "Activate";

  const content: Record<ConnectEntitlementState, [string, string]> = {
    active: ["Active", "Compatible installed apps can add actions to email attachments."],
    authority_unavailable: [
      "Unavailable in this build",
      "Install an official Connect-enabled build to activate a license.",
    ],
    missing: ["Not activated", "Install your Connect license to enable app-to-app capabilities."],
    invalid: ["License invalid", "Choose a valid signed Connect license to restore capabilities."],
    not_yet_valid: [
      "Not active yet",
      "This license cannot be used before its signed start time.",
    ],
    expired: ["License expired", "Install a current signed license to restore capabilities."],
    feature_missing: [
      "Access not included",
      "This license does not include app-to-app capability exchange.",
    ],
  };
  const [title, detail] = content[status.state];
  setHealthValue(connectHealth, status.active, title);
  connectDetail.textContent = detail;
}

function applyConnectStatus(status: ConnectEntitlementStatus, forceCapabilityRefresh = false): void {
  const activeChanged =
    connectEntitlementActive !== null && connectEntitlementActive !== status.active;
  connectEntitlementActive = status.active;
  renderConnectStatus(status);
  if ((activeChanged || forceCapabilityRefresh) && configurationReady) void loadInbox();
}

async function refreshConnectStatus(): Promise<void> {
  if (connectInstalling || connectStatusRefreshInFlight) return;
  connectStatusRefreshInFlight = true;
  setHealthValue(connectHealth, false, "Checking…");
  connectDetail.textContent = "Looking for your license…";
  connectActivate.hidden = true;
  try {
    const status = await invoke<ConnectEntitlementStatus>("connect_entitlement_status");
    if (!connectInstalling) applyConnectStatus(status);
  } catch (error) {
    if (!connectInstalling) {
      setHealthValue(connectHealth, false, "Unknown");
      connectDetail.textContent = errorMessage(error);
    }
  } finally {
    connectStatusRefreshInFlight = false;
  }
}

async function selectAndInstallConnectEntitlement(): Promise<void> {
  if (connectInstalling) return;
  connectInstalling = true;
  connectActivate.disabled = true;
  try {
    let selected: string | null;
    try {
      selected = await open({
        multiple: false,
        filters: [{ name: "Connect license", extensions: ["json"] }],
      });
    } catch (error) {
      setHealthValue(connectHealth, false, "Selection failed");
      connectDetail.textContent = errorMessage(error);
      return;
    }
    if (selected === null) return;

    setHealthValue(connectHealth, false, "Activating…");
    connectDetail.textContent = "Verifying and installing your signed license…";
    try {
      const status = await invoke<ConnectEntitlementStatus>("connect_entitlement_install", {
        sourcePath: selected,
      });
      applyConnectStatus(status, status.active);
    } catch (error) {
      const failure = errorMessage(error);
      try {
        const current = await invoke<ConnectEntitlementStatus>("connect_entitlement_status");
        applyConnectStatus(current);
        setHealthValue(
          connectHealth,
          current.active,
          current.active ? "Active — replacement failed" : "Activation failed",
        );
        connectDetail.textContent = failure;
      } catch {
        setHealthValue(connectHealth, false, "Activation failed");
        connectDetail.textContent = failure;
        connectActivate.hidden = false;
      }
    }
  } finally {
    connectInstalling = false;
    connectActivate.disabled = false;
  }
}

function calendarConsentDetail(status: CalendarConsentStatus): string {
  if (!status.entitlement_active) {
    return "Connect access is inactive. Saved authorization can still be removed.";
  }
  if (status.state === "consent_pending") {
    return "Microsoft or your administrator still needs to complete consent.";
  }
  if (status.state === "rejected") return "Microsoft did not grant this permission.";
  if (status.state === "revoked") return "Microsoft revoked or invalidated this permission.";
  if (status.state === "ready" && !status.available) {
    return "Consent is saved, but its account identity or token could not be verified.";
  }
  return status.available
    ? "Authorized and available."
    : "This permission has not been authorized.";
}

function setCalendarConsentButtonsBusy(): void {
  for (const button of calendarConsentList.querySelectorAll("button")) {
    button.disabled = true;
  }
}

function renderCalendarConsents(
  entitlementActive: boolean,
  accounts: MailAccountStatus[],
  entries: CalendarConsentEntry[],
): void {
  const visibleEntries = entries.filter(
    (entry) =>
      (entry.status !== null && calendarConsentVisible(entry.status)) ||
      (entitlementActive && entry.error !== null),
  );
  const visible = entitlementActive || visibleEntries.length > 0;
  calendarConsentSettings.hidden = !visible;
  calendarConsentList.replaceChildren();
  if (!visible) {
    calendarConsentStatus.textContent = "";
    delete calendarConsentStatus.dataset.kind;
    return;
  }

  if (accounts.length === 0) {
    calendarConsentStatus.textContent =
      "Connect a Microsoft 365 email account before authorizing calendar access.";
    delete calendarConsentStatus.dataset.kind;
    return;
  }

  for (const account of accounts) {
    const accountEntries = visibleEntries.filter(
      (entry) => entry.account.account_id === account.account_id,
    );
    if (accountEntries.length === 0) continue;

    const accountSection = document.createElement("article");
    accountSection.className = "calendar-consent-account";
    const accountHeading = document.createElement("div");
    accountHeading.className = "calendar-consent-account-heading";
    const accountTitle = document.createElement("h4");
    accountTitle.textContent = account.address || account.display_name;
    const accountState = document.createElement("p");
    accountState.textContent = account.connected
      ? "Microsoft 365 mailbox connected"
      : "Mailbox disconnected — reconnect it before adding or renewing calendar access";
    accountHeading.append(accountTitle, accountState);

    const profileList = document.createElement("div");
    profileList.className = "calendar-consent-profiles";
    for (const entry of accountEntries) {
      const definition = CALENDAR_CONSENT_PROFILES.find(
        (candidate) => candidate.profile === entry.profile,
      );
      if (!definition) continue;
      const card = document.createElement("section");
      card.className = `calendar-consent-card calendar-consent-${entry.profile}`;
      const title = document.createElement("h5");
      title.textContent = definition.title;
      const description = document.createElement("p");
      description.textContent = definition.description;
      const effect = document.createElement("p");
      effect.className = "calendar-consent-effect";
      effect.textContent = definition.effectNote;
      card.append(title, description, effect);

      if (entry.status === null) {
        const failure = document.createElement("p");
        failure.className = "calendar-consent-state calendar-consent-error";
        failure.textContent = entry.error || "Calendar permission status is unavailable.";
        card.append(failure);
        profileList.append(card);
        continue;
      }

      const status = entry.status;
      const state = document.createElement("p");
      state.className = "calendar-consent-state";
      state.textContent = `${calendarConsentStateLabel(status)} · Scope: ${status.scope}`;
      const detail = document.createElement("p");
      detail.className = "calendar-consent-detail";
      detail.textContent = calendarConsentDetail(status);
      const actions = document.createElement("div");
      actions.className = "calendar-consent-actions";
      const controls = calendarConsentControls(status, account.connected);
      if (controls.connectVisible) {
        const connect = document.createElement("button");
        connect.type = "button";
        connect.textContent = `${controls.connectLabel} ${definition.actionLabel}`;
        connect.disabled = !controls.connectEnabled || calendarConsentOperationInFlight !== null;
        if (!account.connected) connect.title = "Reconnect this Microsoft 365 mailbox first";
        connect.addEventListener("click", () => {
          void mutateCalendarConsent("connect", account, entry.profile);
        });
        actions.append(connect);
      }
      if (controls.disconnectVisible) {
        const disconnect = document.createElement("button");
        disconnect.type = "button";
        disconnect.className = "danger-action";
        disconnect.textContent = `Remove ${definition.actionLabel}`;
        disconnect.disabled = calendarConsentOperationInFlight !== null;
        disconnect.addEventListener("click", () => {
          void mutateCalendarConsent("disconnect", account, entry.profile);
        });
        actions.append(disconnect);
      }
      card.append(state, detail, actions);
      profileList.append(card);
    }
    accountSection.append(accountHeading, profileList);
    calendarConsentList.append(accountSection);
  }
}

async function loadCalendarConsents(message?: string): Promise<void> {
  if (calendarConsentOperationInFlight !== null) return;
  const requestGeneration = ++calendarConsentRequestGeneration;
  calendarConsentStatus.textContent = "Checking Microsoft calendar permissions…";
  delete calendarConsentStatus.dataset.kind;
  try {
    const [entitlement, catalog] = await Promise.all([
      invoke<ConnectEntitlementStatus>("connect_entitlement_status"),
      invoke<MailAccounts>("mail_accounts_list"),
    ]);
    if (requestGeneration !== calendarConsentRequestGeneration) return;
    applyConnectStatus(entitlement);
    const accounts = catalog.accounts.filter((account) => account.provider === "microsoft365");
    const entries = await Promise.all(
      accounts.flatMap((account) =>
        CALENDAR_CONSENT_PROFILES.map(async ({ profile }): Promise<CalendarConsentEntry> => {
          try {
            const status = await invoke<CalendarConsentStatus>("calendar_consent_status", {
              profile,
              provider: account.provider,
              accountId: account.account_id,
            });
            return { account, profile, status, error: null };
          } catch (error) {
            return { account, profile, status: null, error: errorMessage(error) };
          }
        }),
      ),
    );
    if (requestGeneration !== calendarConsentRequestGeneration) return;
    renderCalendarConsents(entitlement.active, accounts, entries);
    if (!calendarConsentSettings.hidden && accounts.length > 0) {
      const failureCount = entries.filter((entry) => entry.error !== null).length;
      if (failureCount > 0) {
        calendarConsentStatus.textContent = `${failureCount} calendar permission status ${failureCount === 1 ? "is" : "are"} unavailable.`;
        calendarConsentStatus.dataset.kind = "error";
      } else {
        calendarConsentStatus.textContent = message || "Calendar permissions are up to date.";
        calendarConsentStatus.dataset.kind = "success";
      }
    }
  } catch (error) {
    if (requestGeneration !== calendarConsentRequestGeneration) return;
    if (connectEntitlementActive) {
      calendarConsentSettings.hidden = false;
      calendarConsentList.replaceChildren();
      calendarConsentStatus.textContent = errorMessage(error);
      calendarConsentStatus.dataset.kind = "error";
    } else {
      calendarConsentSettings.hidden = true;
    }
  }
}

async function mutateCalendarConsent(
  action: "connect" | "disconnect",
  account: MailAccountStatus,
  profile: CalendarConsentProfile,
): Promise<void> {
  if (calendarConsentOperationInFlight !== null) return;
  const definition = CALENDAR_CONSENT_PROFILES.find(
    (candidate) => candidate.profile === profile,
  );
  if (!definition) return;
  if (
    action === "disconnect" &&
    !window.confirm(
      `Remove ${definition.actionLabel} from ${account.address || account.display_name}? This removes only that calendar permission; mailbox reading remains connected.`,
    )
  ) {
    return;
  }

  calendarConsentOperationInFlight = `${account.account_id}:${profile}:${action}`;
  setCalendarConsentButtonsBusy();
  calendarConsentStatus.textContent =
    action === "connect"
      ? `Complete ${definition.actionLabel} authorization in your browser…`
      : `Removing ${definition.actionLabel}…`;
  delete calendarConsentStatus.dataset.kind;
  try {
    const status = await invoke<CalendarConsentStatus>(`calendar_consent_${action}`, {
      profile,
      provider: account.provider,
      accountId: account.account_id,
    });
    calendarConsentOperationInFlight = null;
    const message =
      action === "disconnect"
        ? `${definition.title} permission removed. Mailbox reading was not changed.`
        : status.state === "consent_pending"
          ? `${definition.title} consent is pending Microsoft or administrator approval.`
          : `${definition.title} permission authorized.`;
    await loadCalendarConsents(message);
  } catch (error) {
    calendarConsentOperationInFlight = null;
    await loadCalendarConsents();
    calendarConsentStatus.textContent = errorMessage(error);
    calendarConsentStatus.dataset.kind = "error";
  }
}

function renderHealthUnknown(): void {
  const detail = "Health refresh failed; current status is unknown.";
  for (const [value, description] of [
    [mailHealth, mailDetail],
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
}

function renderHealth(health: HealthStatus): void {
  const inboxScopeChanged = renderMailAccounts(health.mail);
  if (inboxScopeChanged && configurationReady && !mailOperationInFlight) void loadInbox();
  const activeAccount = health.mail.accounts.find((account) => account.active);
  const activeProvider = activeAccount
    ? health.mail.providers.find((provider) => provider.provider === activeAccount.provider)
    : undefined;
  const mailReady = Boolean(
    activeAccount?.connected && activeProvider?.connection_available,
  );
  setHealthValue(mailHealth, mailReady, mailReady ? "Ready" : "Needs attention");
  mailDetail.textContent = activeAccount
    ? mailReady
      ? `${activeAccount.address || activeAccount.display_name} is the active read-only account.`
      : activeProvider?.connection_available
        ? "Connect the active email account to resume watching."
        : `${activeAccount.display_name} connection support is not configured in this build.`
    : "Choose a connected email account to start watching.";

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
    health.watchlist_count === 0 || (mailReady && databaseReady);
  checkSupported =
    health.production_check_supported &&
    health.notifications.host_delivery_ready &&
    watcherPrerequisitesReady;
  checkNow.disabled = checkInFlight || !checkSupported;
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
  localModelSettingsEditable = settings.local_model.editable;
  modelEndpointInput.value = settings.local_model.endpoint;
  modelNameInput.value = settings.local_model.model;
  modelSettingsNote.textContent = localModelSettingsEditable
    ? "Use an OpenAI-compatible HTTP endpoint on localhost or 127.0.0.1. Model changes apply to the next analysis."
    : "Inference settings are managed by your administrator and are read-only here.";
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
  void loadMailAccounts().then((loaded) => {
    if (loaded) return loadInbox();
  });
  void loadHealth();
  void loadAutostart();
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

function refreshSettingsControls(): void {
  const busy = settingsInFlight || autostartInFlight;
  for (const control of settingsForm.elements) {
    if (control instanceof HTMLInputElement || control instanceof HTMLButtonElement) {
      const managedModelSetting =
        control === modelEndpointInput || control === modelNameInput;
      const managedAutostartSetting = control === autostartEnabledInput;
      control.disabled =
        busy ||
        (managedModelSetting && !localModelSettingsEditable) ||
        (managedAutostartSetting && !autostartAvailable);
    }
  }
}

function setSettingsBusy(busy: boolean): void {
  settingsInFlight = busy;
  refreshSettingsControls();
}

function setAutostartBusy(busy: boolean): void {
  autostartInFlight = busy;
  refreshSettingsControls();
}

function renderAutostart(status: AutostartStatus): void {
  autostartAvailable = status.available;
  autostartEnabledInput.checked = status.enabled;
  if (!status.available) {
    autostartSettingsNote.textContent = "Start on login is unavailable on this installation.";
  } else {
    autostartSettingsNote.textContent = status.enabled
      ? "Email Watcher will start in the tray after you sign in."
      : "Start on login is off.";
  }
}

async function loadAutostart(): Promise<void> {
  if (autostartInFlight) return;
  setAutostartBusy(true);
  autostartSettingsNote.textContent = "Checking start-on-login status…";
  try {
    renderAutostart(await invoke<AutostartStatus>("autostart_get"));
  } catch (error) {
    autostartAvailable = false;
    autostartSettingsNote.textContent = errorMessage(error);
  } finally {
    setAutostartBusy(false);
  }
}

async function updateAutostart(enabled: boolean): Promise<void> {
  if (!autostartAvailable || autostartInFlight || settingsInFlight) return;
  setAutostartBusy(true);
  autostartSettingsNote.textContent = enabled
    ? "Enabling start on login…"
    : "Disabling start on login…";
  try {
    renderAutostart(await invoke<AutostartStatus>("autostart_set", { enabled }));
  } catch (error) {
    autostartEnabledInput.checked = !enabled;
    autostartSettingsNote.textContent = errorMessage(error);
  } finally {
    setAutostartBusy(false);
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
      const updates: {
        modelBaseUrl?: string;
        modelName?: string;
        notificationsEnabled: boolean;
        pollIntervalMinutes: number;
        retentionDays: number;
      } = {
        notificationsEnabled: notificationsEnabledInput.checked,
        pollIntervalMinutes: Number(pollIntervalInput.value),
        retentionDays: Number(retentionDaysInput.value),
      };
      if (localModelSettingsEditable) {
        updates.modelBaseUrl = modelEndpointInput.value;
        updates.modelName = modelNameInput.value;
      }
      const settings = await invoke<WatcherSettings>("settings_update", updates);
      renderSettings(settings);
      void loadInbox();
      settingsStatus.textContent = settings.local_model.editable
        ? "Settings saved. Model changes apply to the next analysis; restart the app to use the new polling cadence."
        : "Settings saved. Restart the app to use the new polling cadence.";
      settingsStatus.dataset.kind = "success";
    } catch (error) {
      settingsStatus.textContent = errorMessage(error);
      settingsStatus.dataset.kind = "error";
    } finally {
      setSettingsBusy(false);
    }
  })();
});

mailServerForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!mailServerForm.reportValidity()) return;
  void submitMailServerConnection();
});

mailServerCancel.addEventListener("click", closeMailServerForm);
mailServerCaChoose.addEventListener("click", () => void chooseMailServerCa());
mailServerCaClear.addEventListener("click", () => {
  mailServerCaFile = null;
  mailServerCaLabel.textContent = "System trust store";
  mailServerCaClear.hidden = true;
});
mailServerSecurity.addEventListener("change", () => {
  if (mailServerSecurity.value === "starttls" && mailServerPort.value === "993") {
    mailServerPort.value = "143";
  } else if (mailServerSecurity.value === "tls" && mailServerPort.value === "143") {
    mailServerPort.value = "993";
  }
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
  void Promise.all([loadHealth(), refreshConnectStatus()]);
});
settingsTab.addEventListener("click", () => {
  showView("settings");
  if (configurationReady) {
    void Promise.all([loadSettings(), loadAutostart(), loadCalendarConsents()]);
  }
});
autostartEnabledInput.addEventListener("change", () => {
  void updateAutostart(autostartEnabledInput.checked);
});
inboxFilterForm.addEventListener("submit", (event) => {
  event.preventDefault();
  commitInboxQueryFromControls();
  void loadInbox();
});
inboxReset.addEventListener("click", () => {
  inboxFilterForm.reset();
  commitInboxQueryFromControls();
  void loadInbox();
});
inboxLoadMore.addEventListener("click", () => void loadInbox(true));
inboxClear.addEventListener("click", () => void clearInboxHistory());
checkNow.addEventListener("click", () => void runCheck());
connectActivate.addEventListener("click", () => void selectAndInstallConnectEntitlement());
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
  if (configurationReady) {
    void loadMailAccounts().then((loaded) => {
      if (loaded) return loadInbox();
    });
  }
  if (!settingsView.hidden && configurationReady) {
    void loadCalendarConsents();
  } else {
    void refreshConnectStatus();
  }
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (!settingsView.hidden && configurationReady) {
    void loadCalendarConsents();
  } else {
    void refreshConnectStatus();
  }
});
window.setInterval(() => {
  if (document.visibilityState === "visible" && !healthView.hidden) void refreshConnectStatus();
}, 30_000);
void refreshConnectStatus();
void initializeDesktop();
