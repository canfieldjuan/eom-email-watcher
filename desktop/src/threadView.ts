import type { InboxItem } from "./inboxItem.ts";
import type {
  ThreadReadClient, ThreadVendor, ThreadRef, ThreadItem, ThreadPage, ThreadMessagePage,
  ThreadBodyResponse, ThreadCopy, ThreadInventory, ThreadCopyPage, ThreadListQuery,
  InboxMessageRef, InboxActionMessagesQuery, ThreadMessage, NonVendorThreadItem, ThreadResolveResult, ThreadResolveQuery, ThreadSelector,
} from "./threadReads.ts";

const BODY_LABELS = {
  pending: "Body not stored yet",
  source_gone: "Body not stored (source no longer available)",
  outside_folders: "Body not stored (outside the admitted folders)",
  removal_pending: "Body removal pending until the local upgrade completes",
  mailbox_identity_unavailable: "Body not stored (mailbox identity unavailable)",
  not_followed: "Body not stored (thread not followed)",
};

export function threadBodyStateLabel(value: unknown): string {
  const body = value as Record<string, unknown> | null;
  if (body?.state !== "stored") return threadBodyDisplay(value).label!;
  const { stored_chars: count, source_chars: source } = body;
  if (Object.keys(body).length !== 3 || typeof count !== "number" || typeof source !== "number"
    || !Number.isSafeInteger(count) || !Number.isSafeInteger(source)
    || count < 0 || count > 200_000 || source < count) throw new Error("Invalid local body metadata");
  return count < source ? `Partial body: first ${count} of ${source} characters` : "Body stored locally";
}

// The host validates the wire union. Keep the final text sink fail-closed too.
export function threadBodyDisplay(value: unknown): { label: string | null; text: string | null } {
  const invalid = () => new Error("Invalid local body response");
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw invalid();
  const body = value as Record<string, unknown>;
  if (body.state !== "stored") {
    if (Object.keys(body).length !== 1 || typeof body.state !== "string"
      || !Object.hasOwn(BODY_LABELS, body.state)) throw invalid();
    return { label: BODY_LABELS[body.state as keyof typeof BODY_LABELS], text: null };
  }
  const { text, stored_chars: count, source_chars: source } = body;
  if (Object.keys(body).length !== 4 || typeof text !== "string"
    || typeof count !== "number" || typeof source !== "number"
    || !Number.isSafeInteger(count) || !Number.isSafeInteger(source)
    || count < 0 || count > 200_000 || source < count || text.length > 400_000
    || [...text].length !== count) throw invalid();
  return { text, label: count < source ? `Partial body: first ${count} of ${source} characters` : null };
}

export class ThreadReadState<T> {
  status: "idle" | "loading" | "ready" | "error" = "idle";
  value: T | null = null;
  changed = false;
  pages = 0;
  private generation = 0;

  // Silent reset lets the selection owner clear all old content before notifying.
  reset(): void {
    this.generation += 1;
    this.status = "idle";
    this.value = null;
    this.changed = false;
    this.pages = 0;
  }

  async read(load: () => Promise<T>, notify: () => void, merge?: (old: T, fresh: T) => T,
    staleRestart?: () => Promise<T>, clearChildren: () => void = () => {},
    selectionCurrent: () => boolean = () => true): Promise<void> {
    if (merge && this.status === "loading") return;
    const previous = merge ? this.value : null;
    const previousPages = this.pages;
    const generation = ++this.generation;
    const current = () => generation === this.generation && selectionCurrent();
    this.status = "loading";
    if (!merge) { this.value = null; this.changed = false; this.pages = 0; }
    notify();
    if (!current()) return;
    try {
      let fresh: T;
      let restarted = false;
      try { fresh = await load(); }
      catch (error) {
        if (!current()) return;
        if (!staleRestart || (error as { code?: unknown } | null)?.code !== "stale_cursor") throw error;
        this.value = null;
        this.changed = true;
        restarted = true;
        clearChildren();
        notify();
        if (!current()) return;
        fresh = await staleRestart(); // A single restart, using the identical selector.
      }
      if (!current()) return;
      this.value = previous && merge && !restarted ? merge(previous, fresh) : fresh;
      this.pages = previous && merge && !restarted ? previousPages + 1 : 1;
      this.status = "ready";
    } catch {
      if (!current()) return;
      this.value = null;
      this.pages = 0;
      this.status = "error";
      clearChildren();
    }
    if (current()) notify();
  }
}

type Account = { provider: string; account_id: string } | null;
type Page = { items: unknown[]; next_cursor: string | null };
const inventoryKey = (message: string, copy: string) => JSON.stringify([message, copy]);

export interface ThreadReadingIdentity { resolution: string; messages: string }
const contextKey = (thread: Pick<ThreadItem | NonVendorThreadItem, "thread_ref" | "relation" | "owner_vendor">, vendor: string | null) => {
  const ref = thread.thread_ref;
  return JSON.stringify([ref.provider, ref.account_id, ref.mailbox_identity_key, ref.thread_key,
    vendor, thread.relation, thread.owner_vendor?.vendor_id ?? null]);
};

export type ReaderMessage = ThreadMessage & { inbox_action?: InboxItem | null };
type ReaderPage = Omit<ThreadMessagePage, "items"> & { items: ReaderMessage[] };

export function sameMailbox(a: Pick<ThreadRef, "provider" | "account_id" | "mailbox_identity_key">,
  b: Pick<ThreadRef, "provider" | "account_id" | "mailbox_identity_key">): boolean {
  return a.provider === b.provider && a.account_id === b.account_id
    && a.mailbox_identity_key === b.mailbox_identity_key;
}

export function sameThread(a: ThreadRef, b: ThreadRef): boolean {
  return sameMailbox(a, b) && a.thread_key === b.thread_key;
}

export class ThreadViewState {
  vendor: ThreadVendor | null = null;
  account: Account = null;
  selectedThread: ThreadItem | NonVendorThreadItem | null = null;
  readonly resolution = new ThreadReadState<ThreadResolveResult>();
  readonly threads = new ThreadReadState<ThreadPage>();
  readonly messages = new ThreadReadState<ReaderPage>();
  readonly bodies = new Map<string, ThreadReadState<ThreadBodyResponse>>();
  readonly copies = new Map<string, ThreadReadState<ThreadCopyPage<ThreadCopy>>>();
  readonly inventory = new Map<string, ThreadReadState<ThreadCopyPage<ThreadInventory>>>();
  private visible = true;
  private mutations = 0;
  private selectionGeneration = 0;
  private inbox: { origin: InboxMessageRef; current: () => boolean; thread: ThreadRef | null } | null = null;
  // Only a user choice persists; automatic resolution must reconsider the vendor set.
  private inboxVendor: string | undefined;
  private readonly client: ThreadReadClient;
  private readonly notify: () => void;

  private readonly inboxActions: boolean;

  constructor(client: ThreadReadClient, notify: () => void, inboxActions = false) {
    this.inboxActions = inboxActions;
    this.client = client;
    this.notify = notify;
  }

  private clearChildren(): void {
    for (const map of [this.bodies, this.copies, this.inventory]) {
      for (const state of map.values()) state.reset();
      map.clear();
    }
  }

  invalidate(): void {
    this.selectionGeneration += 1;
    this.resolution.reset();
    this.threads.reset();
    this.messages.reset();
    this.clearChildren();
    if (this.inbox) { this.vendor = null; this.selectedThread = null; }
    this.notify();
  }

  get inboxOrigin(): InboxMessageRef | null {
    return this.inbox ? { ...this.inbox.origin } : null;
  }

  get readingIdentity(): ThreadReadingIdentity | null {
    const resolved = this.resolution.value, messages = this.messages.value;
    if (resolved?.state !== "resolved" || this.messages.status !== "ready" || !messages) return null;
    const vendor = resolved.context.kind === "vendor" ? resolved.context.vendor.vendor_id : null;
    return { resolution: contextKey(resolved.context.thread, vendor), messages: contextKey(messages.thread, vendor) };
  }

  openInbox(origin: InboxMessageRef, current: () => boolean, thread: ThreadRef | null = null): Promise<void> {
    this.inbox = { origin: structuredClone(origin), current, thread };
    this.inboxVendor = undefined;
    this.vendor = null;
    this.account = { provider: origin.provider, account_id: origin.account_id };
    this.selectedThread = null;
    this.visible = true;
    return this.refresh();
  }

  renewInboxOrigin(origin: InboxMessageRef): void {
    if (this.inbox) this.inbox.origin = { ...origin };
  }

  async resolveInbox(more = false, expected?: ThreadReadingIdentity): Promise<void> {
    const entry = this.inbox;
    if (!entry || !this.allowed()) return;
    const previous = this.resolution.value;
    if (more && (previous?.state !== "choose_vendor" || !previous.next_cursor
      || this.resolution.status === "loading")) return;
    const generation = ++this.selectionGeneration;
    if (!more) {
      this.vendor = null;
      this.selectedThread = null;
      this.messages.reset();
      this.clearChildren();
    }
    const current = () => this.inbox === entry && generation === this.selectionGeneration && this.allowed();
    const cursor = more && previous?.state === "choose_vendor" ? previous.next_cursor : null;
    const vendor = this.inboxVendor;
    const load = (cursor: string | null) => {
      const query: ThreadResolveQuery = vendor === undefined
        ? { message_ref: entry.origin, cursor }
        : { message_ref: entry.origin, vendor_id: vendor };
      return this.client.resolve(query).then(result => {
        if (entry.thread && !sameThread(entry.thread, result.state === "resolved"
          ? result.context.thread.thread_ref : result.thread_ref)) throw new Error("Inbox conversation changed");
        return result;
      });
    };
    await this.resolution.read(() => load(cursor), this.notify,
      more ? (old, fresh) => old.state === "choose_vendor" && fresh.state === "choose_vendor"
        ? { ...fresh, items: [...old.items, ...fresh.items] } : fresh : undefined,
      cursor ? () => load(null) : undefined, () => this.clearChildren(), current);
    if (!current() || this.resolution.status !== "ready") return;
    const resolved = this.resolution.value;
    if (resolved?.state !== "resolved") return;
    const vendorId = resolved.context.kind === "vendor" ? resolved.context.vendor.vendor_id : null;
    if (expected && contextKey(resolved.context.thread, vendorId) !== expected.resolution) {
      this.resolution.reset(); this.resolution.status = "error"; this.notify(); return;
    }
    this.selectedThread = structuredClone(resolved.context.thread);
    this.vendor = resolved.context.kind === "vendor" ? { ...resolved.context.vendor } : null;
    this.messages.reset();
    this.clearChildren();
    this.notify();
    if (current()) await this.loadMessages(false, expected);
  }

  chooseVendor(vendorId: string): Promise<void> {
    const choices = this.resolution.value;
    if (!this.inbox || !this.allowed() || choices?.state !== "choose_vendor"
      || !choices.items.some(vendor => vendor.vendor_id === vendorId)) return Promise.resolve();
    this.inboxVendor = vendorId;
    this.invalidate();
    return this.resolveInbox();
  }

  selectVendor(vendor: ThreadVendor): Promise<void> {
    this.inbox = null;
    this.inboxVendor = undefined;
    this.vendor = { ...vendor };
    this.account = null;
    this.selectedThread = null;
    this.visible = true;
    return this.refresh();
  }

  selectAccount(account: Account): Promise<void> {
    if (this.inbox) return Promise.resolve();
    this.account = account ? { ...account } : null;
    this.selectedThread = null;
    return this.refresh();
  }

  openThread(thread: ThreadItem): Promise<void> {
    if (this.inbox) return Promise.resolve();
    this.selectionGeneration += 1;
    this.selectedThread = structuredClone(thread);
    this.threads.reset();
    this.messages.reset();
    this.clearChildren();
    this.notify();
    return this.loadMessages();
  }

  backToThreads(): Promise<void> {
    if (this.inbox) { this.close(); return Promise.resolve(); }
    this.selectedThread = null;
    return this.refresh();
  }

  close(): void {
    this.inbox = null;
    this.inboxVendor = undefined;
    this.vendor = null;
    this.selectedThread = null;
    this.account = null;
    this.suspend();
  }

  suspend(): void { this.visible = false; this.invalidate(); }
  resume(): Promise<void> { this.visible = true; return this.refresh(); }
  beginMutation(): void { this.mutations += 1; this.invalidate(); }
  endMutation(refresh = true): Promise<void> {
    this.mutations = Math.max(0, this.mutations - 1);
    return refresh ? this.refresh() : Promise.resolve();
  }

  refresh(expected?: ThreadReadingIdentity): Promise<void> {
    this.invalidate();
    if (this.inbox) return this.resolveInbox(false, expected);
    return this.selectedThread ? this.loadMessages() : this.loadThreads();
  }

  private allowed(): boolean {
    return this.visible && this.mutations === 0 && (this.inbox ? this.inbox.current() : this.vendor !== null);
  }

  private guarded<T>(load: () => Promise<T>): Promise<T> {
    const generation = this.selectionGeneration, entry = this.inbox;
    if (!entry) return load();
    return load().catch(error => {
      const code = (error as { code?: unknown } | null)?.code;
      if (entry && this.inbox === entry && generation === this.selectionGeneration && this.allowed()
        && (code === "not_found" || code === "stale_selection")) {
        this.invalidate();
        this.resolution.status = "error";
        this.notify();
      }
      throw error;
    });
  }

  private page<T extends Page>(state: ThreadReadState<T>, more: boolean,
    load: (cursor: string | null) => Promise<T>, clearChildren: () => void = () => {}, restart = true): Promise<void> {
    if (!this.allowed() || (more && (!state.value?.next_cursor || state.status === "loading"))) return Promise.resolve();
    const cursor = more ? state.value!.next_cursor : null;
    if (!more) clearChildren();
    return state.read(() => this.guarded(() => load(cursor)), this.notify,
      more ? (old, fresh) => ({ ...fresh, items: [...old.items, ...fresh.items] }) : undefined,
      cursor && restart ? () => this.guarded(() => load(null)) : undefined, clearChildren, () => this.allowed());
  }

  loadThreads(more = false): Promise<void> {
    if (!this.vendor || this.inbox) return Promise.resolve();
    const query: ThreadListQuery = this.account
      ? { vendor_id: this.vendor.vendor_id, ...this.account }
      : { vendor_id: this.vendor.vendor_id };
    return this.page(this.threads, more, cursor => this.client.list({ ...query, cursor }));
  }

  loadMessages(more = false, expected?: ThreadReadingIdentity): Promise<void> {
    if (!this.selectedThread) return Promise.resolve();
    const query = this.threadSelector(this.selectedThread.thread_ref);
    if (!query) return Promise.resolve();
    return this.page(this.messages, more, async cursor => {
      const origin = this.inbox?.origin;
      const inboxQuery: InboxActionMessagesQuery | null = this.inboxActions && origin
        ? query.inbox_thread ? { inbox_thread: query.inbox_thread, cursor, include_inbox_actions: true }
          : { vendor_id: query.vendor_id, thread_ref: query.thread_ref, inbox_message: origin, cursor, include_inbox_actions: true }
        : null;
      const result: ReaderPage = inboxQuery
        ? await this.client.messages(inboxQuery) : await this.client.messages({ ...query, cursor });
      if (this.inbox?.thread && !sameThread(result.thread.thread_ref, this.inbox.thread)) {
        throw new Error("Inbox conversation changed");
      }
      if (expected && contextKey(result.thread, this.vendor?.vendor_id ?? null) !== expected.messages) {
        throw new Error("Retained thread context changed");
      }
      for (const message of result.items) threadBodyStateLabel(message.body_state);
      return result;
    }, () => this.clearChildren(), expected === undefined);
  }

  private threadSelector(thread_ref: ThreadRef): ThreadSelector | null {
    if (!this.allowed()) return null;
    if (this.inbox && this.selectedThread?.relation === "non_vendor") {
      return { inbox_thread: { thread_ref, message_ref: this.inbox.origin } };
    }
    if (!this.vendor) return null;
    return this.inbox
      ? { vendor_id: this.vendor.vendor_id, thread_ref, inbox_message: this.inbox.origin }
      : { vendor_id: this.vendor.vendor_id, thread_ref };
  }

  private selector(message_id: string): (ThreadSelector & { message_id: string }) | null {
    if (!this.allowed() || !this.messages.value?.items.some(item => item.message_id === message_id)) return null;
    const selector = this.threadSelector(this.messages.value.thread.thread_ref);
    return selector ? { ...selector, message_id } : null;
  }

  openBody(message_id: string): Promise<void> {
    const query = this.selector(message_id);
    if (!query) return Promise.resolve();
    this.bodies.get(message_id)?.reset();
    const state = new ThreadReadState<ThreadBodyResponse>();
    this.bodies.set(message_id, state);
    return state.read(() => this.guarded(async () => {
      const result = await this.client.body(query);
      threadBodyDisplay(result.body);
      return result;
    }), this.notify, undefined, undefined, () => {}, () => this.allowed());
  }

  closeBody(message_id: string): void {
    this.bodies.get(message_id)?.reset(); this.bodies.delete(message_id); this.notify();
  }

  openCopies(message_id: string): Promise<void> {
    if (!this.selector(message_id)) return Promise.resolve();
    this.closeCopies(message_id);
    this.copies.set(message_id, new ThreadReadState());
    return this.loadCopies(message_id);
  }

  loadCopies(message_id: string, more = false): Promise<void> {
    const query = this.selector(message_id), state = this.copies.get(message_id);
    if (!query || !state) return Promise.resolve();
    return this.page(state, more, cursor => this.client.copies({ ...query, cursor }), () => this.clearInventory(message_id));
  }

  private clearInventory(message_id: string): void {
    for (const [key, state] of this.inventory) {
      if (JSON.parse(key)[0] !== message_id) continue;
      state.reset(); this.inventory.delete(key);
    }
  }

  closeCopies(message_id: string): void {
    this.copies.get(message_id)?.reset(); this.copies.delete(message_id);
    this.clearInventory(message_id); this.notify();
  }

  inventoryState(message_id: string, copy_ref: string): ThreadReadState<ThreadCopyPage<ThreadInventory>> | undefined {
    return this.inventory.get(inventoryKey(message_id, copy_ref));
  }

  openInventory(message_id: string, copy_ref: string): Promise<void> {
    if (!this.copies.get(message_id)?.value?.items.some(item => item.copy_ref === copy_ref)) return Promise.resolve();
    this.closeInventory(message_id, copy_ref);
    this.inventory.set(inventoryKey(message_id, copy_ref), new ThreadReadState());
    return this.loadInventory(message_id, copy_ref);
  }

  loadInventory(message_id: string, copy_ref: string, more = false): Promise<void> {
    const query = this.selector(message_id), state = this.inventoryState(message_id, copy_ref);
    if (!query || !state) return Promise.resolve();
    return this.page(state, more, cursor => this.client.copies({ ...query, copy_ref, cursor }));
  }

  closeInventory(message_id: string, copy_ref: string): void {
    const key = inventoryKey(message_id, copy_ref);
    this.inventory.get(key)?.reset(); this.inventory.delete(key); this.notify();
  }
}
