import { confirm } from "@tauri-apps/plugin-dialog";

let pending = false;

// The native dialog is asynchronous. Only its explicit affirmative result
// authorizes an action; denial, dismissal and dialog failures leave it untouched.
export async function confirmAction(message: string, actionLabel = "Continue"): Promise<boolean> {
  if (pending) return false;
  pending = true;
  try {
    return await confirm(message, {
      title: "Email Watcher",
      kind: "warning",
      okLabel: actionLabel,
      cancelLabel: "Cancel",
    }) === true;
  } catch {
    console.error("Confirmation dialog unavailable; action cancelled.");
    return false;
  } finally {
    pending = false;
  }
}
