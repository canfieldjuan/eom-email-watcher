export type ConfigAdmissionViewStatus =
  | "admitted"
  | "missing"
  | "acknowledgement_required"
  | "manual_repair_required";

export function configAdmissionView(status: {
  state: ConfigAdmissionViewStatus;
}): "inbox" | "settings" {
  return status.state === "admitted" ? "inbox" : "settings";
}

export async function reconcileConfigAdmissionRefresh<T>(options: {
  currentGeneration: () => number;
  request: () => Promise<T>;
  renderFailure: () => void;
  renderStatus: (status: T) => void;
}): Promise<void> {
  const requestGeneration = options.currentGeneration();
  try {
    options.renderStatus(await options.request());
  } catch {
    if (requestGeneration === options.currentGeneration()) options.renderFailure();
  }
}
