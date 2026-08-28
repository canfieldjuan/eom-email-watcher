import { invoke } from "@tauri-apps/api/core";
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
  </div>
`;

const inboxTab = requiredElement<HTMLButtonElement>("#inbox-tab");
const watchlistTab = requiredElement<HTMLButtonElement>("#watchlist-tab");
const inboxView = requiredElement<HTMLElement>("#inbox-view");
const watchlistView = requiredElement<HTMLElement>("#watchlist-view");
const inboxList = requiredElement<HTMLUListElement>("#inbox-list");
const inboxStatus = requiredElement<HTMLParagraphElement>("#inbox-status");
const form = requiredElement<HTMLFormElement>("#sender-form");
const emailInput = requiredElement<HTMLInputElement>("#sender-email");
const nameInput = requiredElement<HTMLInputElement>("#sender-name");
const list = requiredElement<HTMLUListElement>("#sender-list");
const watchlistStatus = requiredElement<HTMLParagraphElement>("#watchlist-status");
let watchedSenders: WatchedSender[] = [];
let operationInFlight = true;

function errorMessage(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    const message = (error as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  return "The watcher engine could not complete that request.";
}

function showView(view: "inbox" | "watchlist"): void {
  const inboxSelected = view === "inbox";
  inboxView.hidden = !inboxSelected;
  watchlistView.hidden = inboxSelected;
  inboxTab.setAttribute("aria-pressed", String(inboxSelected));
  watchlistTab.setAttribute("aria-pressed", String(!inboxSelected));
}

function receivedLabel(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
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
void loadInbox();
void loadSenders().then((loaded) => {
  if (loaded) finishOperation();
});
