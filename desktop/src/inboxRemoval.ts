export interface InboxClearResult {
  deleted: number;
  refused: number;
  held_message_ids: string[];
  outcome: "completed" | "partial" | "refused";
  message: string | null;
}

export function inboxClearView<T extends { message_id: string }>(
  items: T[], result: InboxClearResult,
): { retained: T[]; text: string; kind: "error" | "success" } {
  if (result.outcome !== "completed") {
    if (!result.message || result.refused < 1) {
      throw new Error("Watcher engine returned an invalid held deletion result");
    }
    const held = new Set(result.held_message_ids);
    const prefix = result.deleted > 0
      ? `Deleted ${result.deleted} local message${result.deleted === 1 ? "" : "s"}; ${result.refused} refused. `
      : "";
    return { retained: items.filter(item => held.has(item.message_id)),
      text: prefix + result.message, kind: "error" };
  }
  return { retained: [], kind: "success",
    text: `Cleared ${result.deleted} local message${result.deleted === 1 ? "" : "s"}. Source email was not changed.` };
}
