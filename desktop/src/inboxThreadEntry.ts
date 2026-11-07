import type { InboxMessageRef, ThreadReadClient } from "./threadReads.ts";
import { ThreadViewDom, type RetainedThreadAccount, type ThreadReadingPosition } from "./threadViewDom.ts";
import type { ThreadReadingIdentity } from "./threadView.ts";

export interface InboxThreadRow {
  provider: string;
  account_id: string;
  message_id: string;
  thread_entry_ref: InboxMessageRef;
  subject?: string;
  sender?: string;
  received_at?: string;
}
export interface InboxThreadScope { mutation_epoch: number; mailbox_operation_revision: number }
export interface InboxThreadSnapshot {
  configured: boolean;
  visible: boolean;
  blocked: boolean;
  queryEpoch: number;
  itemsEpoch: number | null;
  items: readonly InboxThreadRow[];
  scope: InboxThreadScope | null;
  spanScope: InboxThreadScope | null;
}
export const inboxThreadOriginKey = (ref: InboxMessageRef): string => JSON.stringify([
  ref.provider, ref.account_id, ref.mailbox_identity_key, ref.message_id,
]);
const sameScope = (a: InboxThreadScope | null, b: InboxThreadScope | null): boolean => !!a && !!b
  && a.mutation_epoch === b.mutation_epoch && a.mailbox_operation_revision === b.mailbox_operation_revision;
type Entry = { origin: InboxMessageRef; epoch: number; scope: InboxThreadScope;
  scroll: { element: Element; top: number; left: number }[] };
type ReadingIntent = { origin: string; epoch: number; identity: ThreadReadingIdentity;
  count: number; pages: number; bodies: string[]; position: ThreadReadingPosition; focus: boolean };

/** Binds the reader to a current retained row, never to a sender/subject or default account. */
export class InboxThreadEntry {
  readonly reader: ThreadViewDom;
  private entry: Entry | null = null;
  private retryGeneration = 0;
  private mode: "wide" | "split" | "single" = "single";
  private sendersExpanded = false;
  private generation = 0;
  private intent: ReadingIntent | null = null;
  private readonly options: {
    root: HTMLElement;
    columns: HTMLElement;
    heading: HTMLElement;
    client: ThreadReadClient;
    accounts: () => readonly RetainedThreadAccount[];
    snapshot: () => InboxThreadSnapshot;
    refreshInbox: () => Promise<void>;
    scrollElements: () => readonly Element[];
    onSelectionChange?: () => void;
    layout?: {
      grid: HTMLElement; readerPane: HTMLElement; placeholder: HTMLElement; source: HTMLElement;
      senders: HTMLElement; senderToggle: HTMLButtonElement; senderContent: HTMLElement; footer: HTMLElement;
    };
  };

  constructor(options: InboxThreadEntry["options"]) {
    this.options = options;
    this.reader = new ThreadViewDom(options.root, options.client, {
      idPrefix: "inbox-thread", accounts: options.accounts, onExit: () => this.back(),
      scrollRoot: options.layout?.readerPane,
      restoring: () => this.intent !== null,
      onInboxRefresh: () => { this.retryGeneration++; return options.refreshInbox(); },
      onInboxRetry: () => this.retry(), onInboxChoice: () => { this.retryGeneration++; },
      inboxSourceNotice: () => this.entry && !this.current(this.entry)
        ? "The originating message is unavailable in the current Inbox results. Refresh to try again." : null,
      onStateChange: () => {
        if (!this.reader.state.inboxOrigin) this.entry = null;
        this.present();
      },
    });
    const layout = options.layout;
    if (layout) {
      const movedOutside = (event: Event) => {
        if (this.intent && event.target && (event.type === "pointerdown" || event.target !== options.root.ownerDocument.body)
          && !layout.readerPane.contains(event.target as Node)) this.intent.focus = false;
      };
      options.root.ownerDocument.addEventListener("focusin", movedOutside);
      options.root.ownerDocument.addEventListener("pointerdown", movedOutside);
      layout.senderToggle.addEventListener("click", () => {
        this.sendersExpanded = !this.sendersExpanded; this.present(); this.measure();
      });
      if (typeof ResizeObserver !== "undefined") {
        new ResizeObserver(() => this.measure()).observe(layout.grid.parentElement!);
        options.root.ownerDocument.defaultView?.addEventListener("resize", () => this.measure());
      }
    }
  }

  get state() { return this.reader.state; }
  get selected(): boolean { return this.entry !== null; }
  get refreshGeneration(): number { return this.generation; }

  /** Preserve identifiers and positions before clearing every retained payload. */
  invalidate(): void {
    const snapshot = this.options.snapshot(), entry = this.entry, identity = this.state.readingIdentity;
    if (!entry || snapshot.blocked || !snapshot.visible || entry.epoch !== snapshot.queryEpoch
      || !sameScope(entry.scope, snapshot.scope)) this.intent = null;
    else if (!this.intent && identity && this.state.messages.value) {
      const position = this.reader.readingPosition();
      this.intent = { origin: inboxThreadOriginKey(entry.origin), epoch: entry.epoch, identity,
        count: this.state.messages.value.items.length, pages: this.state.messages.pages,
        bodies: [...this.state.bodies.keys()], position, focus: position.focus !== null };
    }
    this.generation++;
    this.state.invalidate();
  }

  failRefresh(generation: number): void {
    if (generation !== this.generation) return;
    this.intent = null; this.generation++;
    this.state.invalidate();
    if (this.entry) { this.state.resolution.status = "error"; this.reader.render(); }
  }

  async refresh(): Promise<void> {
    const entry = this.entry, intent = this.intent, generation = ++this.generation;
    if (this.options.root.ownerDocument.visibilityState === "hidden") { this.state.invalidate(); return; }
    if (!entry || !this.current(entry)) { this.intent = null; this.state.invalidate(); return; }
    const current = () => {
      const valid = generation === this.generation && this.entry === entry && this.current(entry);
      if (!valid) this.failRefresh(generation);
      return valid;
    };
    if (intent && (intent.epoch !== entry.epoch || intent.origin !== inboxThreadOriginKey(entry.origin))) {
      this.failRefresh(generation); return;
    }
    await this.state.refresh(intent?.identity);
    if (!current() || !intent) return;
    // Replay only the user's old page budget at the unchanged cursor-bound limit.
    // A growing final page stays fresh but closed; missing body IDs never cause a hunt.
    while (this.state.messages.status === "ready" && this.state.messages.value?.next_cursor
      && this.state.messages.pages < intent.pages && this.state.messages.value.items.length < intent.count) {
      await this.state.loadMessages(true, intent.identity);
      if (!current()) return;
    }
    if (this.state.messages.status !== "ready" || !this.state.messages.value) { this.failRefresh(generation); return; }
    const eligible = new Set(this.state.messages.value.items.map(message => message.message_id));
    const bodies = intent.bodies.filter(id => eligible.has(id));
    await Promise.all(bodies.map(id => this.state.openBody(id)));
    if (!current()) return;
    if (bodies.some(id => this.state.bodies.get(id)?.status !== "ready")) { this.failRefresh(generation); return; }
    this.intent = null;
    this.reader.render();
    this.reader.restoreReadingPosition(intent.position, intent.focus);
  }

  resume(): Promise<void> { return this.entry ? this.options.refreshInbox() : this.state.resume(); }
  close(): void { this.intent = null; this.generation++; this.state.close(); }
  beginMutation(): void { this.intent = null; this.generation++; this.state.beginMutation(); }
  async endMutation(refresh = true): Promise<void> {
    await this.state.endMutation(false);
    if (refresh) await this.refresh();
  }

  isSelected(row: InboxThreadRow): boolean {
    return this.entry !== null && inboxThreadOriginKey(row.thread_entry_ref) === inboxThreadOriginKey(this.entry.origin);
  }

  private saveListScroll(): Entry["scroll"] {
    return this.options.scrollElements().map(element => ({ element, top: element.scrollTop, left: element.scrollLeft }));
  }

  private restoreListScroll(): void {
    for (const saved of this.entry?.scroll ?? []) { saved.element.scrollTop = saved.top; saved.element.scrollLeft = saved.left; }
  }

  /** Content-width changes alter presentation only; they never query or reopen a reader. */
  updateLayout(width: number, rem = 16, availableHeight = 0): void {
    const oldMode = this.mode;
    this.mode = width >= 72 * rem ? "wide" : width >= 52 * rem ? "split" : "single";
    if (this.entry && oldMode !== "single" && this.mode === "single") this.entry.scroll = this.saveListScroll();
    this.present();
    if (this.entry && oldMode === "single" && this.mode !== "single") this.restoreListScroll();
    const layout = this.options.layout;
    if (layout) {
      layout.grid.dataset.layout = this.mode;
      layout.grid.dataset.fit = String(this.mode !== "single" && availableHeight >= 240);
      layout.grid.style.setProperty("--inbox-pane-height", `${Math.max(0, availableHeight)}px`);
    }
  }

  private measure(): void {
    const layout = this.options.layout, win = this.options.root.ownerDocument.defaultView;
    if (!layout || !win || !layout.grid.getClientRects().length) return;
    const bounds = layout.grid.getBoundingClientRect();
    const rem = Number.parseFloat(win.getComputedStyle(this.options.root.ownerDocument.documentElement).fontSize);
    this.updateLayout(bounds.width, rem, win.innerHeight - bounds.top - layout.footer.getBoundingClientRect().height - 40);
  }

  private present(): void {
    const { columns, layout } = this.options, active = columns.ownerDocument.activeElement;
    const hideMessages = this.entry !== null && this.mode === "single";
    const hidFocusedMessages = hideMessages && !columns.hidden && !!active && columns.contains(active);
    columns.hidden = hideMessages;
    if (layout) {
      const hideSenders = hideMessages, hideSenderContent = this.mode !== "wide" && !this.sendersExpanded;
      const hidFocusedSender = !!active && layout.senderContent.contains(active) && (hideSenders || hideSenderContent);
      layout.senders.hidden = hideSenders;
      layout.senderToggle.hidden = this.mode === "wide";
      layout.senderContent.hidden = hideSenderContent;
      layout.senderToggle.setAttribute("aria-expanded", String(!hideSenderContent));
      layout.readerPane.hidden = !this.entry && this.mode === "single";
      layout.placeholder.hidden = this.entry !== null;
      const row = this.entry && this.current(this.entry) && this.state.resolution.status === "ready"
        ? this.row(this.entry.origin, this.options.snapshot()) : null;
      layout.source.textContent = row ? `Opened from Inbox: ${row.subject || "No subject"} · ${row.sender ?? "Sender unavailable"} · ${row.received_at ?? "Date unavailable"} · ${row.provider} / ${row.account_id}` : "";
      layout.source.hidden = !row;
      if (hidFocusedSender) (hideSenders ? this.options.heading : layout.senderToggle).focus({ preventScroll: true });
    }
    if (hidFocusedMessages) this.reader.focusHeading();
    this.options.onSelectionChange?.();
  }

  private committed(snapshot: InboxThreadSnapshot): boolean {
    return snapshot.configured && snapshot.visible
      && snapshot.itemsEpoch === snapshot.queryEpoch && sameScope(snapshot.scope, snapshot.spanScope);
  }

  private ready(snapshot: InboxThreadSnapshot): boolean {
    return this.options.root.ownerDocument.visibilityState !== "hidden" && !snapshot.blocked && this.committed(snapshot);
  }

  private row(origin: InboxMessageRef, snapshot: InboxThreadSnapshot): InboxThreadRow | undefined {
    return snapshot.items.find(row => row.provider === origin.provider && row.account_id === origin.account_id
      && row.message_id === origin.message_id && inboxThreadOriginKey(row.thread_entry_ref) === inboxThreadOriginKey(origin));
  }

  canOpen(row: InboxThreadRow): boolean {
    const snapshot = this.options.snapshot();
    return this.ready(snapshot) && !!this.row(row.thread_entry_ref, snapshot);
  }

  private current(entry: Entry): boolean {
    const snapshot = this.options.snapshot();
    return this.entry === entry && this.ready(snapshot) && entry.epoch === snapshot.queryEpoch
      && sameScope(entry.scope, snapshot.scope) && !!this.row(entry.origin, snapshot);
  }

  async open(row: InboxThreadRow): Promise<void> {
    if (!this.canOpen(row)) return;
    this.retryGeneration++;
    if (this.entry && this.isSelected(row) && this.current(this.entry)) { this.reader.focusHeading(); return; }
    this.intent = null; this.generation++;
    const snapshot = this.options.snapshot();
    const entry: Entry = { origin: { ...row.thread_entry_ref }, epoch: snapshot.queryEpoch,
      scope: { ...snapshot.scope! }, scroll: this.saveListScroll() };
    this.entry = entry;
    if (this.options.layout) this.options.layout.readerPane.scrollTop = 0;
    this.present();
    await this.reader.openInbox(entry.origin, () => this.current(entry));
  }

  /** Called only after the complete requested Inbox span commits, before reader refresh. */
  revalidateSource(): void {
    const entry = this.entry;
    if (!entry) return;
    // An old read must not become current again when the source binding is renewed.
    this.state.invalidate();
    const snapshot = this.options.snapshot();
    // A committed span can renew the binding while held; current() still forbids reads until release.
    if (this.committed(snapshot) && entry.epoch === snapshot.queryEpoch && this.row(entry.origin, snapshot)) {
      entry.scope = { ...snapshot.scope! };
    } else this.intent = null;
    this.reader.render();
  }

  async retry(): Promise<void> {
    const entry = this.entry;
    if (!entry) return;
    this.intent = null; this.generation++;
    const generation = ++this.retryGeneration;
    await this.options.refreshInbox();
    if (generation !== this.retryGeneration || !this.current(entry)) return;
    // A disappeared explicit vendor stays an error until this explicit fresh-origin retry.
    await this.reader.openInbox(entry.origin, () => this.current(entry));
  }

  back(): void {
    const entry = this.entry;
    this.intent = null; this.generation++;
    this.entry = null;
    this.state.close();
    this.options.columns.hidden = false;
    if (!entry) return;
    if (this.mode === "single") {
      for (const saved of entry.scroll) { saved.element.scrollTop = saved.top; saved.element.scrollLeft = saved.left; }
    }
    const trigger = Array.from(this.options.columns.querySelectorAll<HTMLButtonElement>("[data-inbox-thread-origin]"))
      .find(button => button.dataset.inboxThreadOrigin === inboxThreadOriginKey(entry.origin) && !button.disabled);
    (trigger ?? this.options.heading).focus({ preventScroll: true });
  }
}
