export interface InboxRemovalResult {
  request_id: string;
  intent_ids: string[];
  outcome: "completed" | "deferred";
  requested: number;
  completed: number;
  pending: number;
  message: string | null;
}

export interface InboxClearResult extends InboxRemovalResult {
  deleted: number;
}

export interface InboxDeletionResult extends InboxRemovalResult {
  deleted: boolean;
  message_id: string;
}

export function inboxRemovalStatus(result: InboxRemovalResult): {
  text: string; kind: "pending" | "success";
} {
  if (result.outcome === "deferred") {
    return {
      text: `${result.completed} completed; ${result.pending} pending.${result.message ? ` ${result.message}` : ""}`,
      kind: "pending",
    };
  }
  return { text: `${result.completed} completed; ${result.pending} pending.`, kind: "success" };
}

export function inboxClearStatus(result: InboxRemovalResult): {
  text: string; kind: "pending" | "success";
} {
  return result.outcome === "deferred" ? inboxRemovalStatus(result) : {
    text: `Cleared ${result.completed} local message${result.completed === 1 ? "" : "s"}. Source email was not changed.`,
    kind: "success",
  };
}

export function inboxClearView<T extends { message_id: string }>(
  _items: T[], result: InboxRemovalResult,
): { retained: T[]; text: string; kind: "pending" | "success" } {
  return { retained: [], ...inboxClearStatus(result) };
}

export type RemovalInvoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;

export interface SavedRemoval {
  request_id: string;
  kind: "clear" | "delete";
  message_id: string | null;
}

export type InboxRemovalRecord = {
  request: SavedRemoval;
} & ({ state: "found"; receipt: InboxRemovalResult }
  | { state: "missing" | "error"; error: unknown });

export function inboxRemovalRecordStatus(
  record: InboxRemovalRecord, errorMessage: (error: unknown) => string,
): { text: string; kind: "pending" | "success" | "error" } {
  if (record.state === "found") {
    return record.request.kind === "clear"
      ? inboxClearStatus(record.receipt) : inboxRemovalStatus(record.receipt);
  }
  if (record.state === "missing" && record.request.kind === "clear") {
    return { text: "Your last Clear didn't go through; nothing was deleted.", kind: "error" };
  }
  return { text: errorMessage(record.error), kind: "error" };
}

export class InboxRemovalClient {
  private invoke: RemovalInvoke;
  private onChange: (records: readonly InboxRemovalRecord[]) => void | Promise<void>;
  private errorCode: (error: unknown) => string | null;
  private records = new Map<string, InboxRemovalRecord>();
  private tail: Promise<unknown> = Promise.resolve();
  private restoring: Promise<void> | null = null;
  private latestClear: string | null = null;

  constructor(
    invoke: RemovalInvoke,
    onChange: (records: readonly InboxRemovalRecord[]) => void | Promise<void> = () => {},
    errorCode: (error: unknown) => string | null = () => null,
  ) {
    this.invoke = invoke;
    this.onChange = onChange;
    this.errorCode = errorCode;
  }

  statuses(): readonly InboxRemovalRecord[] { return [...this.records.values()]; }

  private serialize<T>(operation: () => Promise<T>): Promise<T> {
    const next = this.tail.then(operation, operation);
    this.tail = next.then(() => undefined, () => undefined);
    return next;
  }

  private async publish(record: InboxRemovalRecord): Promise<void> {
    this.records.set(record.request.request_id, record);
    await this.onChange(this.statuses());
    if (record.state === "found" && record.receipt.outcome === "completed") {
      try {
        await this.invoke<void>("inbox_removal_acknowledge", { requestId: record.request.request_id });
      } catch (error) {
        // Keep the rendered outcome and recovery identity; never retry a
        // mutation or classify acknowledgement failure as a lost reply.
        console.warn("Local removal acknowledgement unavailable", this.errorCode(error));
      }
    }
  }

  private async readStatus(request: SavedRemoval): Promise<InboxRemovalResult | undefined> {
    let receipt: InboxRemovalResult;
    try {
      receipt = await this.invoke<InboxRemovalResult>("inbox_removal_status", {
        requestId: request.request_id, kind: request.kind, messageId: request.message_id,
      });
    } catch (error) {
      const missing = this.errorCode(error) === "not_found";
      if (missing && request.kind === "clear" && request.request_id !== this.latestClear) {
        this.records.delete(request.request_id);
        await this.onChange(this.statuses());
      } else {
        await this.publish({ request, state: missing ? "missing" : "error", error });
      }
      return undefined;
    }
    await this.publish({ request, state: "found", receipt });
    return receipt;
  }

  private perform(kind: SavedRemoval["kind"], messageId: string | null): Promise<InboxRemovalResult | undefined> {
    return this.serialize(async () => {
      const prepared = await this.invoke<{ request_id: string }>("inbox_removal_prepare", { kind, messageId });
      if (kind === "clear") {
        this.latestClear = prepared.request_id;
        for (const [key, record] of this.records) {
          if (record.request.kind === "clear" && record.state === "missing") this.records.delete(key);
        }
        await this.onChange(this.statuses());
      }
      const request: SavedRemoval = { ...prepared, kind, message_id: messageId };
      let receipt: InboxRemovalResult;
      try {
        receipt = await this.invoke<InboxRemovalResult>(kind === "clear" ? "inbox_clear" : "inbox_delete", {
          requestId: request.request_id, ...(kind === "delete" ? { messageId } : {}),
        });
      } catch {
        // An indeterminate reply is reconciled by this key only, never by a second mutation.
        return this.readStatus(request);
      }
      await this.publish({ request, state: "found", receipt });
      return receipt;
    });
  }

  clear(): Promise<InboxRemovalResult | undefined> { return this.perform("clear", null); }
  delete(messageId: string): Promise<InboxRemovalResult | undefined> { return this.perform("delete", messageId); }

  restore(): Promise<void> {
    if (this.restoring) return this.restoring;
    // Each read joins the same queue, so an explicit action can run between old receipts,
    // but recovery can never inspect this client's freshly prepared, undispatched key.
    this.restoring = (async () => {
      const saved = await this.serialize(async () => {
        const requests = await this.invoke<SavedRemoval[]>("inbox_removal_saved");
        if (this.latestClear === null) {
          this.latestClear = requests.filter(request => request.kind === "clear").at(-1)?.request_id ?? null;
        }
        return requests;
      });
      for (const request of saved) await this.serialize(() => this.readStatus(request));
    })().finally(() => { this.restoring = null; });
    return this.restoring;
  }
}
