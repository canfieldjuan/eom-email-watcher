import type { ThreadViewState } from "./threadView.ts";

/** One lifecycle dispatch for the independent Vendors and Inbox readers. */
export class ThreadHistory {
  private readonly vendor: ThreadViewState;
  private readonly inbox: ThreadViewState;
  private holds = 0;
  private inboxSpanCommitted = false;

  constructor(vendor: ThreadViewState, inbox: ThreadViewState) {
    this.vendor = vendor;
    this.inbox = inbox;
  }

  setView(view: string, configured: boolean): void {
    if (configured && view === "vendors") void this.vendor.resume();
    else this.vendor.suspend();
    if (configured && view === "inbox") void this.inbox.resume();
    else this.inbox.suspend();
  }

  refresh(): void {
    void this.vendor.refresh();
    void this.inbox.refresh();
  }

  clearInboxSelection(): void {
    this.inboxSpanCommitted = false;
    this.inbox.close();
  }

  prepareInboxRefresh(): void {
    this.inboxSpanCommitted = false;
    this.inbox.invalidate();
    void this.vendor.refresh();
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
  }

  async endMutation(): Promise<void> {
    this.holds = Math.max(0, this.holds - 1);
    const vendor = this.vendor.endMutation();
    await this.inbox.endMutation(false);
    await Promise.all([vendor, this.refreshCommittedInbox()]);
  }
}
