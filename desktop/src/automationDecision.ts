export interface AutomationDecisionProjection {
  fire_id: string;
  state: string;
  state_version: number;
  prepared_identity_sha256: string | null;
}

export interface AutomationDecisionRequest {
  fireId: string;
  expectedVersion: number;
  preparedIdentitySha256: string;
  decision: "confirmed" | "declined";
}

export interface AutomationDecisionResult {
  fire_id: string;
  state: string;
  state_version: number;
}

type Submission =
  | { status: "submitted"; result: AutomationDecisionResult }
  | { status: "rejected"; error: unknown };

export type AutomationDecisionOutcome =
  | { status: "ignored" }
  | (Submission & ({ refreshed: true } | { refreshed: false; refreshError: unknown }));

export function releaseAutomationRefreshFences(
  required: Map<string, number>,
  committedGeneration: number,
  append: boolean,
): boolean {
  if (append) return false;
  let released = false;
  for (const [fireId, blockedThroughGeneration] of required) {
    if (committedGeneration > blockedThroughGeneration) {
      required.delete(fireId);
      released = true;
    }
  }
  return released;
}

export function canDecideAutomationFire(fire: AutomationDecisionProjection): boolean {
  return (
    fire.state === "awaiting_confirmation" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(fire.fire_id) &&
    Number.isSafeInteger(fire.state_version) &&
    fire.state_version > 0 &&
    typeof fire.prepared_identity_sha256 === "string" &&
    /^[0-9a-f]{64}$/.test(fire.prepared_identity_sha256)
  );
}

export async function runAutomationDecision(
  fire: AutomationDecisionProjection,
  decision: AutomationDecisionRequest["decision"],
  inFlight: Set<string>,
  submit: (request: AutomationDecisionRequest) => Promise<AutomationDecisionResult>,
  refresh: () => Promise<void>,
): Promise<AutomationDecisionOutcome> {
  if (!canDecideAutomationFire(fire) || inFlight.has(fire.fire_id)) return { status: "ignored" };
  inFlight.add(fire.fire_id);
  try {
    const request: AutomationDecisionRequest = {
      fireId: fire.fire_id,
      expectedVersion: fire.state_version,
      preparedIdentitySha256: fire.prepared_identity_sha256 as string,
      decision,
    };
    let submission: Submission;
    try {
      submission = { status: "submitted", result: await submit(request) };
    } catch (error) {
      submission = { status: "rejected", error };
    }
    try {
      await refresh();
      return { ...submission, refreshed: true };
    } catch (refreshError) {
      return { ...submission, refreshed: false, refreshError };
    }
  } finally {
    inFlight.delete(fire.fire_id);
  }
}
