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
      text: `${result.completed} completed; ${result.pending} pending. ${result.message}`,
      kind: "pending",
    };
  }
  return { text: "", kind: "success" };
}

export function inboxClearView<T extends { message_id: string }>(
  _items: T[], result: InboxClearResult,
): { retained: T[]; text: string; kind: "pending" | "success" } {
  const status = inboxRemovalStatus(result);
  return { retained: [], kind: status.kind,
    text: result.outcome === "deferred" ? status.text
      : `Cleared ${result.completed} local message${result.completed === 1 ? "" : "s"}. Source email was not changed.` };
}
