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
