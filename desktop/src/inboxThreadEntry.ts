import type { InboxMessageRef, ThreadReadClient } from "./threadReads.ts";
import { ThreadViewDom, type RetainedThreadAccount } from "./threadViewDom.ts";

export interface InboxThreadRow {
  provider: string;
  account_id: string;
  message_id: string;
  thread_entry_ref: InboxMessageRef;
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

/** Binds the reader to a current retained row, never to a sender/subject or default account. */
export class InboxThreadEntry {
  readonly reader: ThreadViewDom;
  private entry: Entry | null = null;
  private retryGeneration = 0;
  private readonly options: {
    root: HTMLElement;
    columns: HTMLElement;
    heading: HTMLElement;
    client: ThreadReadClient;
    accounts: () => readonly RetainedThreadAccount[];
    snapshot: () => InboxThreadSnapshot;
    refreshInbox: () => Promise<void>;
    scrollElements: () => readonly Element[];
  };

  constructor(options: InboxThreadEntry["options"]) {
    this.options = options;
    this.reader = new ThreadViewDom(options.root, options.client, {
      idPrefix: "inbox-thread", accounts: options.accounts, onExit: () => this.back(),
      onInboxRefresh: () => { this.retryGeneration++; return options.refreshInbox(); },
      onInboxRetry: () => this.retry(), onInboxChoice: () => { this.retryGeneration++; },
      inboxSourceNotice: () => this.entry && !this.current(this.entry)
        ? "The originating message is unavailable in the current Inbox results. Refresh to try again." : null,
      onStateChange: () => {
        if (!this.reader.state.inboxOrigin) this.entry = null;
        options.columns.hidden = this.entry !== null;
      },
    });
  }

  get state() { return this.reader.state; }

  private committed(snapshot: InboxThreadSnapshot): boolean {
    return snapshot.configured && snapshot.visible
      && snapshot.itemsEpoch === snapshot.queryEpoch && sameScope(snapshot.scope, snapshot.spanScope);
  }

  private ready(snapshot: InboxThreadSnapshot): boolean { return !snapshot.blocked && this.committed(snapshot); }

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
    const snapshot = this.options.snapshot();
    const entry: Entry = { origin: { ...row.thread_entry_ref }, epoch: snapshot.queryEpoch,
      scope: { ...snapshot.scope! }, scroll: this.options.scrollElements().map(element => ({
        element, top: element.scrollTop, left: element.scrollLeft,
      })) };
    this.entry = entry;
    this.options.columns.hidden = true;
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
    }
    this.reader.render();
  }

  async retry(): Promise<void> {
    const entry = this.entry;
    if (!entry) return;
    const generation = ++this.retryGeneration;
    await this.options.refreshInbox();
    if (generation !== this.retryGeneration || !this.current(entry)) return;
    // A disappeared explicit vendor stays an error until this explicit fresh-origin retry.
    await this.reader.openInbox(entry.origin, () => this.current(entry));
  }

  back(): void {
    const entry = this.entry;
    this.entry = null;
    this.state.close();
    this.options.columns.hidden = false;
    if (!entry) return;
    for (const saved of entry.scroll) { saved.element.scrollTop = saved.top; saved.element.scrollLeft = saved.left; }
    const trigger = Array.from(this.options.columns.querySelectorAll<HTMLButtonElement>("[data-inbox-thread-origin]"))
      .find(button => button.dataset.inboxThreadOrigin === inboxThreadOriginKey(entry.origin) && !button.disabled);
    (trigger ?? this.options.heading).focus({ preventScroll: true });
  }
}
