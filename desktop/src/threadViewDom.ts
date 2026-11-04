import type { ThreadReadClient, ThreadVendor, ThreadRef, ThreadMessage, ThreadCopy } from "./threadReads.ts";
import { ThreadViewState, ThreadReadState, threadBodyDisplay, threadBodyStateLabel } from "./threadView.ts";

export interface RetainedThreadAccount {
  provider: string;
  account_id: string;
  display_name: string;
  connected: boolean;
  sent_scope?: string | null;
}

const threadKey = (ref: ThreadRef) => `thread:${JSON.stringify(ref)}`;
const accountKey = (account: { provider: string; account_id: string }) => JSON.stringify([account.provider, account.account_id]);
const regionId = (kind: string, ...keys: string[]) => `thread-${kind}-${encodeURIComponent(JSON.stringify(keys))}`;
const dateLabel = (value: string | null) => value && Number.isFinite(Date.parse(value))
  ? new Date(value).toLocaleString() : "Time not recorded";

/** Plain DOM consumer of local read state. No mail/provider text enters an HTML sink. */
export class ThreadViewDom {
  readonly state: ThreadViewState;
  private readonly root: HTMLElement;
  private readonly doc: Document;
  private readonly options: { accounts: () => readonly RetainedThreadAccount[]; onExit: () => void };
  private pendingFocus: string | null = null;

  constructor(root: HTMLElement, client: ThreadReadClient, options: ThreadViewDom["options"]) {
    this.root = root;
    this.doc = root.ownerDocument;
    this.options = options;
    this.state = new ThreadViewState(client, () => this.render());
    root.className = "thread-view";
    root.setAttribute("aria-labelledby", "thread-view-heading");
    this.render();
  }

  open(vendor: ThreadVendor): Promise<void> {
    this.pendingFocus = "heading";
    return this.state.selectVendor(vendor);
  }

  private node<K extends keyof HTMLElementTagNameMap>(tag: K, text = "", className = ""): HTMLElementTagNameMap[K] {
    const element = this.doc.createElement(tag);
    element.textContent = text;
    element.className = className;
    return element;
  }

  private button(label: string, key: string, action: () => void | Promise<void>, expanded?: boolean, controls?: string): HTMLButtonElement {
    const button = this.node("button", label, "secondary-action");
    button.type = "button";
    button.dataset.threadFocus = key;
    if (expanded !== undefined) button.setAttribute("aria-expanded", String(expanded));
    if (controls) button.setAttribute("aria-controls", controls);
    button.addEventListener("click", () => { void action(); });
    return button;
  }

  private scope(): string {
    const account = this.state.account;
    return `${this.state.vendor!.display_name} · ${account ? `${account.provider} / ${account.account_id}` : "All retained accounts"}`;
  }

  private refLabel(ref: ThreadRef): string {
    return `${ref.provider} / ${ref.account_id} · ${ref.mailbox_identity_key === null
      ? "Mailbox identity unavailable" : `Mailbox identity ${ref.mailbox_identity_key}`}`;
  }

  private status<T>(container: HTMLElement, state: ThreadReadState<T>, kind: string, retry: () => Promise<void>, empty: boolean, key = kind): void {
    const scope = this.scope();
    const status = this.node("p", state.status === "loading" ? `Loading retained ${kind} for ${scope}…`
      : state.status === "error" ? `Could not load retained ${kind} for ${scope}. Try again.`
      : state.status === "idle" ? `Local history for ${scope} needs revalidation.`
      : empty ? `No retained ${kind} for ${scope}.` : `Showing retained ${kind} for ${scope}.`, "status");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    container.append(status);
    if (state.changed) container.append(this.node("p", "Local history changed. This list restarted at the first page.", "status"));
    if (state.status === "error") container.append(this.button(`Retry ${kind}`, `retry:${key}`, retry));
  }

  private accountSelect(): HTMLElement {
    const label = this.node("label", "Retained account");
    const select = this.node("select");
    select.dataset.threadFocus = "account";
    const all = this.node("option", "All retained accounts"); all.value = ""; select.append(all);
    const accounts = new Map(this.options.accounts().map(account => [accountKey(account), account]));
    for (const item of this.state.threads.value?.items ?? []) {
      const ref = item.thread_ref, key = accountKey(ref);
      if (!accounts.has(key)) accounts.set(key, { ...ref, display_name: ref.account_id, connected: false });
    }
    const selected = this.state.account;
    if (selected && !accounts.has(accountKey(selected))) accounts.set(accountKey(selected), { ...selected, display_name: selected.account_id, connected: false });
    for (const [key, account] of accounts) {
      const option = this.node("option", `${account.display_name} · ${account.provider}${account.connected ? "" : " · Disconnected"}`);
      option.value = key; select.append(option);
    }
    select.value = selected ? accountKey(selected) : "";
    select.addEventListener("change", () => {
      const account = accounts.get(select.value);
      this.pendingFocus = "account";
      void this.state.selectAccount(account ? { provider: account.provider, account_id: account.account_id } : null);
    });
    label.append(select);
    return label;
  }

  render(): void {
    const active = this.doc.activeElement as HTMLElement | null;
    const focus = active && this.root.contains(active) ? active.dataset.threadFocus : undefined;
    const scroll = new Map(Array.from(this.root.querySelectorAll<HTMLElement>("[data-thread-focus]"),
      element => [element.dataset.threadFocus, element.scrollTop]));
    this.root.hidden = this.state.vendor === null;
    this.root.replaceChildren();
    if (!this.state.vendor) return;
    const header = this.node("div", "", "thread-toolbar");
    header.append(this.button("Back to vendors", "back-vendors", () => {
      this.state.close(); this.options.onExit();
    }), this.button("Refresh local history", "refresh", () => this.state.refresh()));
    const heading = this.node("h2", `${this.state.vendor.display_name} — Threads`);
    heading.id = "thread-view-heading"; heading.tabIndex = -1; heading.dataset.threadFocus = "heading";
    this.root.append(header, heading, this.accountSelect(),
      this.node("p", "History partial — only locally retained messages are shown", "thread-history-notice"));
    const selected = this.state.selectedThread?.thread_ref ?? this.state.account;
    for (const account of this.options.accounts()) {
      if (account.sent_scope === "unavailable" && (!selected
        || (account.provider === selected.provider && account.account_id === selected.account_id))) {
        this.root.append(this.node("p", `Sent mail unavailable — ${account.display_name}`, "thread-history-notice"));
      }
    }
    if (this.state.selectedThread) this.renderMessages(); else this.renderThreads();
    const targets = Array.from(this.root.querySelectorAll<HTMLElement>("[data-thread-focus]"));
    for (const element of targets) element.scrollTop = scroll.get(element.dataset.threadFocus) ?? 0;
    const target = targets.find(element => element.dataset.threadFocus === (this.pendingFocus ?? focus));
    if (target) { target.focus(); this.pendingFocus = null; }
    else if (this.pendingFocus && ["ready", "error"].includes(
      this.state.selectedThread ? this.state.messages.status : this.state.threads.status)) {
      heading.focus(); this.pendingFocus = null;
    }
  }

  private renderThreads(): void {
    const state = this.state.threads;
    this.status(this.root, state, "threads", () => this.state.loadThreads(), state.value?.items.length === 0);
    const list = this.node("ul", "", "thread-list"); list.id = "thread-list";
    list.setAttribute("aria-label", `Retained threads for ${this.scope()}`);
    for (const item of state.value?.items ?? []) {
      const row = this.node("li", "", "thread-card");
      const open = this.button(item.subject.trim() ? item.subject : "No subject", threadKey(item.thread_ref), () => {
        this.pendingFocus = "message-heading";
        return this.state.openThread(item);
      }, false, "thread-messages");
      row.append(open, this.node("p", this.refLabel(item.thread_ref), "thread-meta"),
        this.node("p", `${dateLabel(item.latest_received_at)} · ${item.message_count} logical message${item.message_count === 1 ? "" : "s"}`, "thread-meta"));
      if (item.relation === "linked") row.append(this.node("p", `Linked · Owner: ${item.owner_vendor.display_name}`, "thread-meta"));
      list.append(row);
    }
    const messages = this.node("section"); messages.id = "thread-messages"; messages.hidden = true;
    this.root.append(list, messages);
    if (state.value?.next_cursor) {
      const more = this.button("Load more threads", "more-threads", () => this.state.loadThreads(true));
      more.disabled = state.status === "loading"; this.root.append(more);
    }
  }

  private renderMessages(): void {
    const selected = this.state.selectedThread!, state = this.state.messages;
    this.root.append(this.button("Back to threads", "back-threads", () => {
      this.pendingFocus = threadKey(selected.thread_ref);
      return this.state.backToThreads();
    }, true, "thread-messages"));
    // After invalidation, a cached list subject is no longer retained evidence.
    // Render a subject only from the current successful message projection.
    const subject = state.value ? state.value.items[0]?.subject || "No subject" : "Retained thread";
    const heading = this.node("h3", subject.trim() ? subject : "No subject");
    heading.tabIndex = -1; heading.dataset.threadFocus = "message-heading";
    const section = this.node("section"); section.id = "thread-messages";
    section.setAttribute("aria-label", `Retained messages in ${subject}`);
    section.append(heading, this.node("p", this.refLabel(state.value?.thread.thread_ref ?? selected.thread_ref), "thread-meta"));
    if (state.value?.thread.relation === "linked") section.append(this.node("p", `Linked · Owner: ${state.value.thread.owner_vendor.display_name}`, "thread-meta"));
    this.status(section, state, "messages", () => this.state.loadMessages(), state.value?.items.length === 0);
    const list = this.node("ol", "", "thread-list");
    for (const message of state.value?.items ?? []) list.append(this.messageCard(message));
    section.append(list);
    if (state.value?.next_cursor) {
      const more = this.button("Load more messages", "more-messages", () => this.state.loadMessages(true));
      more.disabled = state.status === "loading"; section.append(more);
    }
    this.root.append(section);
  }

  private messageCard(message: ThreadMessage): HTMLElement {
    const row = this.node("li", "", "thread-card");
    row.append(this.node("h4", message.subject.trim() ? message.subject : "No subject"),
      this.node("p", `${message.direction === "outbound" ? "Outbound" : "Inbound"} · ${dateLabel(message.received_at)}`, "thread-meta"),
      this.node("p", message.sender_name ? `${message.sender_name} (${message.sender})` : message.sender),
      this.node("p", message.attributed_vendor?.display_name ?? "No attributed vendor", "thread-meta"));
    const id = message.message_id, body = this.state.bodies.get(id), bodyId = regionId("body", id);
    row.append(this.button(body ? "Hide body" : "Show body", `body:${id}`, () => body
      ? this.state.closeBody(id) : this.state.openBody(id), !!body, bodyId));
    const container = this.node("div"); container.id = bodyId; container.hidden = !body;
    if (body) {
      if (body.value) {
        const display = threadBodyDisplay(body.value.body);
        if (display.label !== null) container.append(this.node("p", display.label, "thread-meta"));
        if (display.text !== null) {
          const text = this.node("div", display.text, "thread-body-text");
          text.dataset.threadFocus = `body-text:${id}`;
          text.tabIndex = 0; text.setAttribute("role", "region"); text.setAttribute("aria-label", `Stored body: ${message.subject || "No subject"}`);
          container.append(text);
        }
      } else this.status(container, body, "body", () => this.state.openBody(id), false, bodyId);
    } else row.append(this.node("p", threadBodyStateLabel(message.body_state), "thread-meta"));
    row.append(container);
    const copies = this.state.copies.get(id), copyId = regionId("copies", id);
    row.append(this.button(copies ? "Hide copies" : `Show copies (${message.copy_count})`, `copies:${id}`,
      () => copies ? this.state.closeCopies(id) : this.state.openCopies(id), !!copies, copyId));
    const copyContainer = this.node("div"); copyContainer.id = copyId; copyContainer.hidden = !copies;
    if (copies) {
      this.status(copyContainer, copies, "copies", () => this.state.loadCopies(id), copies.value?.items.length === 0, copyId);
      for (const [index, copy] of (copies.value?.items ?? []).entries()) copyContainer.append(this.copyCard(id, copy, index));
      if (copies.value?.next_cursor) {
        const more = this.button("Load more copies", `more-copies:${id}`, () => this.state.loadCopies(id, true));
        more.disabled = copies.status === "loading"; copyContainer.append(more);
      }
    }
    row.append(copyContainer);
    return row;
  }

  private copyCard(message: string, copy: ThreadCopy, index: number): HTMLElement {
    const card = this.node("section", "", "thread-copy");
    card.append(this.node("h5", `Retained copy ${index + 1}`),
      this.node("p", copy.locations.length ? copy.locations.join(" · ") : "Location not recorded", "thread-meta"),
      this.node("p", copy.summary ?? "No summary retained for this copy", "thread-copy-summary"));
    const state = this.state.inventoryState(message, copy.copy_ref), id = regionId("inventory", message, copy.copy_ref);
    card.append(this.button(state ? "Hide attachment metadata" : `Show attachment metadata (${copy.attachment_count})`,
      id, () => state
        ? this.state.closeInventory(message, copy.copy_ref) : this.state.openInventory(message, copy.copy_ref), !!state, id));
    const container = this.node("div"); container.id = id; container.hidden = !state;
    if (state) {
      this.status(container, state, "attachment metadata", () => this.state.loadInventory(message, copy.copy_ref), state.value?.items.length === 0, id);
      const list = this.node("ul");
      for (const attachment of state.value?.items ?? []) list.append(this.node("li",
        `${attachment.filename || "Unnamed attachment"} · ${attachment.media_type} · ${attachment.byte_size === null ? "Size not recorded" : `${attachment.byte_size} bytes`}`));
      container.append(list);
      if (state.value?.next_cursor) {
        const more = this.button("Load more attachment metadata", `more:${id}`, () => this.state.loadInventory(message, copy.copy_ref, true));
        more.disabled = state.status === "loading"; container.append(more);
      }
    }
    card.append(container);
    return card;
  }
}
