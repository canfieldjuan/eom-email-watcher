import { invoke } from "@tauri-apps/api/core";
import "./styles.css";

interface WatchedSender {
  email: string;
  name: string | null;
}

function requiredElement<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (!element) throw new Error(`Required element is missing: ${selector}`);
  return element;
}

const app = requiredElement<HTMLElement>("#app");

app.innerHTML = `
  <section class="shell" aria-labelledby="watchlist-title">
    <header class="intro">
      <p class="eyebrow">Local email watcher</p>
      <h1 id="watchlist-title">Watched senders</h1>
      <p class="lede">Only exact email addresses on this list are analyzed.</p>
    </header>

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

    <p id="status" class="status" role="status" aria-live="polite">Loading watchlist…</p>
    <ul id="sender-list" class="sender-list" aria-label="Watched senders"></ul>
  </section>
`;

const form = requiredElement<HTMLFormElement>("#sender-form");
const emailInput = requiredElement<HTMLInputElement>("#sender-email");
const nameInput = requiredElement<HTMLInputElement>("#sender-name");
const list = requiredElement<HTMLUListElement>("#sender-list");
const status = requiredElement<HTMLParagraphElement>("#status");

function errorMessage(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    const message = (error as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  return "The watcher engine could not complete that request.";
}

function setBusy(busy: boolean): void {
  for (const control of form.elements) {
    if (control instanceof HTMLInputElement || control instanceof HTMLButtonElement) {
      control.disabled = busy;
    }
  }
}

function renderSenders(senders: WatchedSender[]): void {
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
    remove.addEventListener("click", () => void removeSender(sender.email, remove));

    item.append(identity, remove);
    list.append(item);
  }
}

async function loadSenders(message = "Watchlist is up to date."): Promise<void> {
  try {
    const senders = await invoke<WatchedSender[]>("watchlist_list");
    renderSenders(senders);
    status.textContent = message;
    status.dataset.kind = "success";
  } catch (error) {
    status.textContent = errorMessage(error);
    status.dataset.kind = "error";
  }
}

async function removeSender(email: string, button: HTMLButtonElement): Promise<void> {
  button.disabled = true;
  status.textContent = `Removing ${email}…`;
  try {
    await invoke<WatchedSender>("watchlist_remove", { email });
    await loadSenders(`${email} is no longer watched.`);
  } catch (error) {
    button.disabled = false;
    status.textContent = errorMessage(error);
    status.dataset.kind = "error";
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void (async () => {
    setBusy(true);
    status.textContent = "Adding sender…";
    try {
      const sender = await invoke<WatchedSender>("watchlist_add", {
        email: emailInput.value,
        name: nameInput.value.trim() || null,
      });
      form.reset();
      await loadSenders(`${sender.email} is now watched.`);
      emailInput.focus();
    } catch (error) {
      status.textContent = errorMessage(error);
      status.dataset.kind = "error";
    } finally {
      setBusy(false);
    }
  })();
});

void loadSenders();
