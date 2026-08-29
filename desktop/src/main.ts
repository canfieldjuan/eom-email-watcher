import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import "./styles.css";

interface WatchedSender {
  email: string;
  name: string | null;
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
    interval_minutes: number;
    next_check_unix_ms: number;
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
  </div>
`;

const inboxTab = requiredElement<HTMLButtonElement>("#inbox-tab");
const watchlistTab = requiredElement<HTMLButtonElement>("#watchlist-tab");
const healthTab = requiredElement<HTMLButtonElement>("#health-tab");
const inboxView = requiredElement<HTMLElement>("#inbox-view");
const watchlistView = requiredElement<HTMLElement>("#watchlist-view");
const healthView = requiredElement<HTMLElement>("#health-view");
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
let watchedSenders: WatchedSender[] = [];
let operationInFlight = true;
let checkInFlight = false;
let checkSupported = false;
let healthRequestGeneration = 0;

function errorMessage(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    const message = (error as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  return "The watcher engine could not complete that request.";
}

function showView(view: "inbox" | "watchlist" | "health"): void {
  const inboxSelected = view === "inbox";
  const watchlistSelected = view === "watchlist";
  const healthSelected = view === "health";
  inboxView.hidden = !inboxSelected;
  watchlistView.hidden = !watchlistSelected;
  healthView.hidden = !healthSelected;
  inboxTab.setAttribute("aria-pressed", String(inboxSelected));
  watchlistTab.setAttribute("aria-pressed", String(watchlistSelected));
  healthTab.setAttribute("aria-pressed", String(healthSelected));
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
    return "Analysis retry queued";
  }
  if (item.status === "analyzed") return "Ready to notify";
  return "Waiting for analysis";
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

    const footer = document.createElement("div");
    footer.className = "message-footer";
    const badge = document.createElement("span");
    badge.className = "priority-badge";
    badge.textContent = item.priority || "Untriaged";
    const state = document.createElement("span");
    state.textContent = stateLabel(item);
    footer.append(badge, state);

    card.append(meta, subject, summary);
    if (details.childElementCount) card.append(details);
    card.append(footer);
    inboxList.append(card);
  }
}

async function loadInbox(): Promise<void> {
  try {
    renderInbox(await invoke<InboxItem[]>("inbox_recent"));
    inboxStatus.textContent = "Showing the most recent watched messages.";
    inboxStatus.dataset.kind = "success";
  } catch (error) {
    inboxStatus.textContent = errorMessage(error);
    inboxStatus.dataset.kind = "error";
  }
}

function setHealthValue(element: HTMLElement, ready: boolean, text: string): void {
  element.textContent = text;
  element.dataset.ready = String(ready);
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
}

function renderHealth(health: HealthStatus): void {
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
  pollingCadence.textContent = intervalLabel(health.polling.interval_minutes);
  nextCheck.textContent = receivedLabel(new Date(health.polling.next_check_unix_ms).toISOString());
  const watcherPrerequisitesReady =
    health.watchlist_count === 0 || (gmailReady && databaseReady);
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
checkNow.addEventListener("click", () => void runCheck());
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
void loadInbox();
void loadHealth();
void loadSenders().then((loaded) => {
  if (loaded) finishOperation();
});
