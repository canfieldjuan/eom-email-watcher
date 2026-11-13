import type { InboxMessageRef, ThreadReadClient, ThreadRef } from "./threadReads.ts";
import { ThreadViewDom, type RetainedThreadAccount, type ThreadReadingPosition } from "./threadViewDom.ts";
import { sameMailbox, sameThread, type ThreadReadingIdentity, type ReaderMessage } from "./threadView.ts";
import type { InboxItem } from "./inboxItem.ts";

export interface InboxThreadRow {
  provider: string;
  account_id: string;
  message_id: string;
  thread_entry_ref: InboxMessageRef;
  subject?: string;
  sender?: string;
  received_at?: string | null;
  thread_ref?: ThreadRef;
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
export type InboxActionContext = Readonly<{
  messageId: string;
  subject: string;
  current: () => boolean;
  afterApproval: () => Promise<boolean>;
}>;

export const inboxConversationKey = (ref: ThreadRef): string =>
  JSON.stringify([ref.provider, ref.account_id, ref.mailbox_identity_key, ref.thread_key]);

export function inboxThreadOriginKey(ref: InboxMessageRef | null | undefined): string | null {
  if (!ref || ![ref.provider, ref.account_id, ref.message_id]
    .every(value => typeof value === "string" && value.length > 0)
    || !(ref.mailbox_identity_key === null
      || typeof ref.mailbox_identity_key === "string" && ref.mailbox_identity_key.length > 0)) return null;
  return JSON.stringify([ref.provider, ref.account_id, ref.mailbox_identity_key, ref.message_id]);
}
const sameScope = (a: InboxThreadScope | null, b: InboxThreadScope | null): boolean => !!a && !!b
  && a.mutation_epoch === b.mutation_epoch && a.mailbox_operation_revision === b.mailbox_operation_revision;
type Entry = { origin: InboxMessageRef; thread: ThreadRef | null; epoch: number; scope: InboxThreadScope;
  scroll: { element: Element; top: number; left: number }[] };
type ActionRefresh = { done: Promise<void>; finish: () => void };
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
  private actionLifetime = 0;
  private readonly actionMembers = new Map<string, symbol>();
  private actionRefresh: ActionRefresh | null = null;
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
    renderAction?: (row: HTMLElement, record: InboxItem, context: InboxActionContext) => void;
    actionNotSent?: (subject: string, reason: string) => void;
    beforeRender?: () => void;
    afterRender?: () => void;
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
      renderAction: options.renderAction ? (row, message) => {
        const context = this.actionContext(message);
        if (context && message.inbox_action) options.renderAction?.(row, message.inbox_action, context);
      } : undefined,
      beforeRender: () => {
        if (!this.actionRefresh) this.pruneActionMembers();
        options.beforeRender?.();
      },
      afterRender: options.afterRender,
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

  private actionOrigin(message: ReaderMessage, entry: Entry): string | null {
    const record = message.inbox_action;
    if (!record) return null;
    const origin = record.thread_entry_ref;
    const key = inboxThreadOriginKey(origin);
    return key !== null && message.message_id === record.message_id && record.message_id === origin.message_id
      && record.provider === origin.provider && record.account_id === origin.account_id
      && sameMailbox(origin, entry.origin) ? key : null;
  }

  private actionContext(message: ReaderMessage): InboxActionContext | null {
    const entry = this.entry, record = message.inbox_action;
    if (!entry || !record || this.actionRefresh) return null;
    const key = this.actionOrigin(message, entry);
    if (key === null) return null;
    const { message_id: messageId, subject } = record;
    const lifetime = this.actionLifetime;
    if (!this.actionMembers.has(key)) this.actionMembers.set(key, Symbol());
    const member = this.actionMembers.get(key);
    const current = () => this.actionLifetime === lifetime && this.entry === entry
      && this.actionRefresh === null && this.actionMembers.get(key) === member && this.current(entry)
      && this.state.messages.value?.items.some(item => item.message_id === messageId
        && this.actionOrigin(item, entry) === key) === true;
    if (!current()) return null;
    return Object.freeze({ messageId, subject, current, afterApproval: async () => {
      if (this.actionRefresh && this.actionLifetime === lifetime && this.entry === entry) {
        this.options.actionNotSent?.(subject, "Waiting for the Inbox refresh. Action not sent yet.");
        while (this.actionRefresh && this.actionLifetime === lifetime && this.entry === entry) {
          await this.actionRefresh.done;
        }
      }
      if (current()) return true;
      const reason = this.actionLifetime !== lifetime || this.entry !== entry
        ? "The reader selection or mailbox scope changed, or the refresh failed."
        : "The email no longer has current Inbox actions.";
      this.options.actionNotSent?.(subject, "Action not sent. " + reason);
      return false;
    } });
  }

  private pruneActionMembers(): void {
    const entry = this.entry;
    const keys = new Set(entry ? this.state.messages.value?.items.map(item => this.actionOrigin(item, entry)) : []);
    for (const key of this.actionMembers.keys()) if (!keys.has(key)) this.actionMembers.delete(key);
  }

  private finishActionRefresh(invalidate: boolean): void {
    if (invalidate) {
      this.actionLifetime++;
      this.actionMembers.clear();
    } else this.pruneActionMembers();
    const refresh = this.actionRefresh;
    this.actionRefresh = null;
    refresh?.finish();
  }

  /** Preserve identifiers and positions before clearing every retained payload. */
  invalidate(): void {
    const snapshot = this.options.snapshot(), entry = this.entry, identity = this.state.readingIdentity;
    const origin = entry ? this.entryKey(entry) : null;
    if (!entry || origin === null || snapshot.blocked || !snapshot.visible || entry.epoch !== snapshot.queryEpoch
      || !sameScope(entry.scope, snapshot.scope)) {
      this.intent = null;
      this.finishActionRefresh(true);
    } else if (!this.intent && identity && this.state.messages.value) {
      const position = this.reader.readingPosition();
      this.intent = { origin, epoch: entry.epoch, identity,
        count: this.state.messages.value.items.length, pages: this.state.messages.pages,
        bodies: [...this.state.bodies.keys()], position, focus: position.focus !== null };
      if (!this.actionRefresh) {
        let finish!: () => void;
        const done = new Promise<void>(resolve => { finish = resolve; });
        this.actionRefresh = { done, finish };
      }
    }
    if (this.options.root.ownerDocument.visibilityState === "hidden") this.finishActionRefresh(true);
    this.generation++;
    this.state.invalidate();
  }

  failRefresh(generation: number): void {
    if (generation !== this.generation) return;
    this.intent = null; this.generation++;
    this.finishActionRefresh(true);
    this.state.invalidate();
    if (this.entry) { this.state.resolution.status = "error"; this.reader.render(); }
  }

  async refresh(): Promise<void> {
    const entry = this.entry, intent = this.intent, generation = ++this.generation;
    if (this.options.root.ownerDocument.visibilityState === "hidden") {
      this.finishActionRefresh(true); this.state.invalidate(); return;
    }
    if (!entry || !this.current(entry)) {
      this.intent = null; this.finishActionRefresh(true); this.state.invalidate(); return;
    }
    const current = () => {
      const valid = generation === this.generation && this.entry === entry && this.current(entry);
      if (!valid) this.failRefresh(generation);
      return valid;
    };
    if (intent && (intent.epoch !== entry.epoch || intent.origin !== this.entryKey(entry))) {
      this.failRefresh(generation); return;
    }
    await this.state.refresh(intent?.identity);
    if (!current()) return;
    if (!intent) { this.finishActionRefresh(this.state.messages.status !== "ready"); return; }
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
    this.finishActionRefresh(false);
    this.reader.render();
    this.reader.restoreReadingPosition(intent.position, intent.focus);
  }

  resume(): Promise<void> { return this.entry ? this.options.refreshInbox() : this.state.resume(); }
  close(): void { this.intent = null; this.generation++; this.finishActionRefresh(true); this.state.close(); }
  beginMutation(): void { this.intent = null; this.generation++; this.finishActionRefresh(true); this.state.beginMutation(); }
  async endMutation(refresh = true): Promise<void> {
    await this.state.endMutation(false);
    if (refresh) await this.refresh();
  }

  isSelected(row: InboxThreadRow): boolean {
    const key = row.thread_ref ? inboxConversationKey(row.thread_ref) : inboxThreadOriginKey(row.thread_entry_ref);
    return key !== null && this.entry !== null && key === this.entryKey(this.entry);
  }

  private entryKey(entry: Entry): string | null {
    return entry.thread ? inboxConversationKey(entry.thread) : inboxThreadOriginKey(entry.origin);
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
        ? this.row(this.entry.origin, this.options.snapshot(), this.entry.thread) : null;
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

  private row(origin: InboxMessageRef, snapshot: InboxThreadSnapshot, thread: ThreadRef | null): InboxThreadRow | undefined {
    const key = inboxThreadOriginKey(origin);
    if (key === null) return undefined;
    return snapshot.items.find(row => row.provider === origin.provider && row.account_id === origin.account_id
      && row.message_id === row.thread_entry_ref?.message_id && (thread
        ? row.thread_ref && sameThread(thread, row.thread_ref) && sameMailbox(thread, row.thread_entry_ref)
          && inboxThreadOriginKey(row.thread_entry_ref) !== null
        : inboxThreadOriginKey(row.thread_entry_ref) === key));
  }

  canOpen(row: InboxThreadRow): boolean {
    const snapshot = this.options.snapshot();
    if (!this.ready(snapshot) || inboxThreadOriginKey(row.thread_entry_ref) === null) return false;
    const fresh = this.row(row.thread_entry_ref, snapshot, row.thread_ref ?? null);
    return row.message_id === row.thread_entry_ref.message_id && row.provider === row.thread_entry_ref.provider
      && row.account_id === row.thread_entry_ref.account_id && !!fresh && inboxThreadOriginKey(fresh.thread_entry_ref) === inboxThreadOriginKey(row.thread_entry_ref);
  }

  private current(entry: Entry): boolean {
    const snapshot = this.options.snapshot();
    return this.entry === entry && this.ready(snapshot) && entry.epoch === snapshot.queryEpoch
      && sameScope(entry.scope, snapshot.scope) && !!this.row(entry.origin, snapshot, entry.thread);
  }

  async open(row: InboxThreadRow): Promise<void> {
    if (!this.canOpen(row)) return;
    this.retryGeneration++;
    if (this.entry && this.isSelected(row) && this.current(this.entry)) { this.reader.focusHeading(); return; }
    this.intent = null; this.generation++;
    this.finishActionRefresh(true);
    const snapshot = this.options.snapshot();
    const entry: Entry = { origin: { ...row.thread_entry_ref }, thread: row.thread_ref ? { ...row.thread_ref } : null, epoch: snapshot.queryEpoch,
      scope: { ...snapshot.scope! }, scroll: this.saveListScroll() };
    this.entry = entry;
    if (this.options.layout) this.options.layout.readerPane.scrollTop = 0;
    this.present();
    await this.reader.openInbox(entry.origin, () => this.current(entry), entry.thread);
  }

  /** Called only after the complete requested Inbox span commits, before reader refresh. */
  revalidateSource(): void {
    const entry = this.entry;
    if (!entry) return;
    // An old read must not become current again when the source binding is renewed.
    this.state.invalidate();
    const snapshot = this.options.snapshot();
    // A committed span can renew the binding while held; current() still forbids reads until release.
    const fresh = this.row(entry.origin, snapshot, entry.thread);
    if (this.committed(snapshot) && entry.epoch === snapshot.queryEpoch && fresh) {
      if (!sameScope(entry.scope, snapshot.scope)) this.finishActionRefresh(true);
      entry.scope = { ...snapshot.scope! };
      entry.origin = { ...fresh.thread_entry_ref };
      this.state.renewInboxOrigin(entry.origin);
    } else if (this.committed(snapshot) && entry.thread) {
      this.back();
      return;
    } else {
      this.intent = null;
      this.finishActionRefresh(true);
    }
    this.reader.render();
  }

  async retry(): Promise<void> {
    const entry = this.entry;
    if (!entry) return;
    this.intent = null; this.generation++;
    this.finishActionRefresh(true);
    const generation = ++this.retryGeneration;
    await this.options.refreshInbox();
    if (generation !== this.retryGeneration || !this.current(entry)) return;
    // A disappeared explicit vendor stays an error until this explicit fresh-origin retry.
    await this.reader.openInbox(entry.origin, () => this.current(entry), entry.thread);
  }

  back(): void {
    const entry = this.entry;
    this.intent = null; this.generation++;
    this.entry = null;
    this.finishActionRefresh(true);
    this.state.close();
    this.options.columns.hidden = false;
    if (!entry) return;
    if (this.mode === "single") {
      for (const saved of entry.scroll) { saved.element.scrollTop = saved.top; saved.element.scrollLeft = saved.left; }
    }
    const key = this.entryKey(entry);
    const trigger = Array.from(this.options.columns.querySelectorAll<HTMLButtonElement>("[data-inbox-thread-origin]"))
      .find(button => key !== null && button.dataset.inboxThreadOrigin === key && !button.disabled);
    (trigger ?? this.options.heading).focus({ preventScroll: true });
  }
}
