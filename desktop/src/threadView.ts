import type {
  ThreadReadClient, ThreadVendor, ThreadRef, ThreadItem, ThreadPage, ThreadMessagePage,
  ThreadBodyResponse, ThreadCopy, ThreadInventory, ThreadCopyPage, ThreadListQuery,
} from "./threadReads.ts";

const BODY_LABELS = {
  pending: "Body not stored yet",
  source_gone: "Body not stored (source no longer available)",
  outside_folders: "Body not stored (outside the admitted folders)",
  removal_pending: "Body removal pending until the local upgrade completes",
  mailbox_identity_unavailable: "Body not stored (mailbox identity unavailable)",
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
  private generation = 0;

  // Silent reset lets the selection owner clear all old content before notifying.
  reset(): void {
    this.generation += 1;
    this.status = "idle";
    this.value = null;
    this.changed = false;
  }

  async read(load: () => Promise<T>, notify: () => void, merge?: (old: T, fresh: T) => T,
    staleRestart?: () => Promise<T>, clearChildren: () => void = () => {}): Promise<void> {
    if (merge && this.status === "loading") return;
    const previous = merge ? this.value : null;
    const generation = ++this.generation;
    const current = () => generation === this.generation;
    this.status = "loading";
    if (!merge) { this.value = null; this.changed = false; }
    notify();
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
        fresh = await staleRestart(); // A single restart, using the identical selector.
      }
      if (!current()) return;
      this.value = previous && merge && !restarted ? merge(previous, fresh) : fresh;
      this.status = "ready";
    } catch {
      if (!current()) return;
      this.value = null;
      this.status = "error";
      clearChildren();
    }
    if (current()) notify();
  }
}

type Account = { provider: string; account_id: string } | null;
type Page = { items: unknown[]; next_cursor: string | null };
const inventoryKey = (message: string, copy: string) => JSON.stringify([message, copy]);

export class ThreadViewState {
  vendor: ThreadVendor | null = null;
  account: Account = null;
  selectedThread: ThreadItem | null = null;
  readonly threads = new ThreadReadState<ThreadPage>();
  readonly messages = new ThreadReadState<ThreadMessagePage>();
  readonly bodies = new Map<string, ThreadReadState<ThreadBodyResponse>>();
  readonly copies = new Map<string, ThreadReadState<ThreadCopyPage<ThreadCopy>>>();
  readonly inventory = new Map<string, ThreadReadState<ThreadCopyPage<ThreadInventory>>>();
  private visible = true;
  private mutations = 0;
  private readonly client: ThreadReadClient;
  private readonly notify: () => void;

  constructor(client: ThreadReadClient, notify: () => void) {
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
    this.threads.reset();
    this.messages.reset();
    this.clearChildren();
    this.notify();
  }

  selectVendor(vendor: ThreadVendor): Promise<void> {
    this.vendor = { ...vendor };
    this.account = null;
    this.selectedThread = null;
    this.visible = true;
    return this.refresh();
  }

  selectAccount(account: Account): Promise<void> {
    this.account = account ? { ...account } : null;
    this.selectedThread = null;
    return this.refresh();
  }

  openThread(thread: ThreadItem): Promise<void> {
    this.selectedThread = structuredClone(thread);
    this.threads.reset();
    this.messages.reset();
    this.clearChildren();
    this.notify();
    return this.loadMessages();
  }

  backToThreads(): Promise<void> {
    this.selectedThread = null;
    return this.refresh();
  }

  close(): void {
    this.vendor = null;
    this.selectedThread = null;
    this.account = null;
    this.suspend();
  }

  suspend(): void { this.visible = false; this.invalidate(); }
  resume(): Promise<void> { this.visible = true; return this.refresh(); }
  beginMutation(): void { this.mutations += 1; this.invalidate(); }
  endMutation(): Promise<void> { this.mutations = Math.max(0, this.mutations - 1); return this.refresh(); }

  refresh(): Promise<void> {
    this.invalidate();
    return this.selectedThread ? this.loadMessages() : this.loadThreads();
  }

  private allowed(): boolean { return this.visible && this.mutations === 0 && this.vendor !== null; }

  private page<T extends Page>(state: ThreadReadState<T>, more: boolean,
    load: (cursor: string | null) => Promise<T>, clearChildren: () => void = () => {}): Promise<void> {
    if (!this.allowed() || (more && (!state.value?.next_cursor || state.status === "loading"))) return Promise.resolve();
    const cursor = more ? state.value!.next_cursor : null;
    if (!more) clearChildren();
    return state.read(() => load(cursor), this.notify,
      more ? (old, fresh) => ({ ...fresh, items: [...old.items, ...fresh.items] }) : undefined,
      cursor ? () => load(null) : undefined, clearChildren);
  }

  loadThreads(more = false): Promise<void> {
    if (!this.vendor) return Promise.resolve();
    const query: ThreadListQuery = this.account
      ? { vendor_id: this.vendor.vendor_id, ...this.account }
      : { vendor_id: this.vendor.vendor_id };
    return this.page(this.threads, more, cursor => this.client.list({ ...query, cursor }));
  }

  loadMessages(more = false): Promise<void> {
    if (!this.vendor || !this.selectedThread) return Promise.resolve();
    const query = { vendor_id: this.vendor.vendor_id, thread_ref: this.selectedThread.thread_ref };
    return this.page(this.messages, more, async cursor => {
      const result = await this.client.messages({ ...query, cursor });
      for (const message of result.items) threadBodyStateLabel(message.body_state);
      return result;
    }, () => this.clearChildren());
  }

  private selector(message_id: string): { vendor_id: string; thread_ref: ThreadRef; message_id: string } | null {
    if (!this.allowed() || !this.messages.value?.items.some(item => item.message_id === message_id)) return null;
    return { vendor_id: this.vendor!.vendor_id, thread_ref: this.messages.value.thread.thread_ref, message_id };
  }

  openBody(message_id: string): Promise<void> {
    const query = this.selector(message_id);
    if (!query) return Promise.resolve();
    this.bodies.get(message_id)?.reset();
    const state = new ThreadReadState<ThreadBodyResponse>();
    this.bodies.set(message_id, state);
    return state.read(async () => {
      const result = await this.client.body(query);
      threadBodyDisplay(result.body);
      return result;
    }, this.notify);
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
