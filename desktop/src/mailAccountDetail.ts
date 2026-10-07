// One line per mail account in Health. The Sent folder notice belongs here
// (thread view contract, D-scope: "Sent mail unavailable").
export interface MailAccountDetailInput {
  display_name: string;
  active: boolean;
  connected: boolean;
  sent_scope?: "available" | "unavailable" | "not_polled" | null;
}

export function mailAccountDetail(account: MailAccountDetailInput): string {
  const parts = [
    account.display_name,
    account.active ? "Active" : "Retained",
    account.connected ? "Connected" : "Disconnected",
  ];
  if (account.sent_scope === "unavailable") parts.push("Sent mail unavailable");
  return parts.join(" · ");
}
