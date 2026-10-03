const labels = [
  "Needs your confirmation", "Action failed", "Needs review",
  "Action paused", "Action running", "Completed",
] as const;

const fireRanks: Record<string, number> = {
  awaiting_confirmation: 0, failed: 1, manual_review: 2, source_unavailable: 2,
  entitlement_paused: 3, pending_dispatch: 4, submitted: 4, completed: 5, declined: 6,
};
const resultRanks: Record<string, number> = {
  failed: 1, requested: 4, accepted: 4, processing: 4, completed: 5,
};
const proposalRanks: Record<string, number> = {
  awaiting_confirmation: 0, failed: 1, manual_review: 2, unresolved: 2,
  write_authorized: 4, writing: 4, reconciling: 4, completed: 5, declined: 6,
};

function stateRank(states: Record<string, number>, value: unknown): number {
  return typeof value === "string" && Object.hasOwn(states, value) ? states[value] : labels.length;
}

export function inboxActionState(
  fires: readonly unknown[],
  results: readonly unknown[],
  proposal: unknown,
  now: number,
): string | null {
  let rank: number = labels.length;
  for (const state of fires) rank = Math.min(rank, stateRank(fireRanks, state));
  for (const status of results) rank = Math.min(rank, stateRank(resultRanks, status));
  if (proposal !== null && typeof proposal === "object" && !Array.isArray(proposal)) {
    const { state, status, expires_at } = proposal as Record<string, unknown>;
    const proposalRank = stateRank(proposalRanks, state);
    if (proposalRank < labels.length) {
      if (status === "no_suggestions") {
        rank = Math.min(rank, 2);
      } else if (status === "accepted") {
        const expiresAt = typeof expires_at === "string" ? Date.parse(expires_at) : Number.NaN;
        const expired = Number.isFinite(expiresAt) && expiresAt <= now;
        if (state !== "awaiting_confirmation" || !expired) rank = Math.min(rank, proposalRank);
      }
    }
  }
  return labels[rank] ?? null;
}
