export const admissionLabels = {
  exact_sender: "watched sender",
  vendor_address: "vendor address",
  vendor_domain: "vendor domain",
  sent_to_vendor: "vendor recipient",
  thread_follow: "followed thread",
  gmail_user_label: "Gmail label",
} as const;

export type InboxAdmissionKind = keyof typeof admissionLabels;

export function inboxAdmissionLabel(kind: InboxAdmissionKind, displayName: string | null, sender: string): string {
  const fallback = kind === "gmail_user_label" ? "selected label" : sender;
  return `Admitted by ${admissionLabels[kind]}: ${displayName || fallback}`;
}
