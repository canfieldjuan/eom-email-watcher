import type { ThreadViewState } from "./threadView.ts";

type InboxLifecycle = Pick<ThreadViewState, "resume" | "close" | "invalidate" | "refresh" | "beginMutation" | "endMutation">;

/** One lifecycle dispatch for the independent Vendors and Inbox readers. */
export class ThreadHistory {
  private readonly vendor: ThreadViewState;
  private readonly inbox: InboxLifecycle;
  private holds = 0;
  private inboxSpanCommitted = false;
  private readonly onMutationChange: () => void;
  private readonly refreshInbox: (() => Promise<void>) | undefined;

  constructor(vendor: ThreadViewState, inbox: InboxLifecycle, onMutationChange: () => void = () => {},
    refreshInbox?: () => Promise<void>) {
    this.vendor = vendor;
    this.inbox = inbox;
    this.onMutationChange = onMutationChange;
    this.refreshInbox = refreshInbox;
  }

  get mutationHeld(): boolean { return this.holds > 0; }

  setView(view: string, configured: boolean): void {
    if (configured && view === "vendors") void this.vendor.resume();
    else this.vendor.suspend();
    if (configured && view === "inbox") void this.inbox.resume();
    else this.inbox.close();
  }

  refresh(): void {
    void this.vendor.refresh();
    void (this.refreshInbox ? this.refreshInbox() : this.inbox.refresh());
  }

  clearInboxSelection(): void {
    this.inboxSpanCommitted = false;
    this.inbox.close();
  }

  prepareInboxRefresh(refreshVendor = true): void {
    this.inboxSpanCommitted = false;
    this.inbox.invalidate();
    if (refreshVendor) void this.vendor.refresh();
  }

  commitInboxSpan(): Promise<void> {
    this.inboxSpanCommitted = true;
    return this.refreshCommittedInbox();
  }

  private refreshCommittedInbox(): Promise<void> {
    if (this.holds || !this.inboxSpanCommitted) return Promise.resolve();
    this.inboxSpanCommitted = false;
    return this.inbox.refresh();
  }

  beginMutation(): void {
    this.holds += 1;
    this.inboxSpanCommitted = false;
    this.vendor.beginMutation();
    this.inbox.beginMutation();
    this.onMutationChange();
  }

  async endMutation(): Promise<void> {
    this.holds = Math.max(0, this.holds - 1);
    const vendor = this.vendor.endMutation();
    await this.inbox.endMutation(false);
    this.onMutationChange();
    await Promise.all([vendor, this.refreshCommittedInbox()]);
  }
}
