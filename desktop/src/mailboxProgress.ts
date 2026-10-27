/** Last successful provider observation; never infer backlog from counts. */
export function mailboxPendingMessage(state: string | undefined): string | null {
  return state === "pending"
    ? "Check finished; more mailbox changes remain. They will be read in later checks."
    : null;
}
