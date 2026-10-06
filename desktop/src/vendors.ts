import { CONNECT_ENTITLEMENT_REQUIRED } from "./connectAvailability.ts";

// Vendor records (docs/THREAD_VIEW_CONTRACT.md, D-vendor and D-ops).
export interface VendorAddress {
  address: string;
  watched: boolean;
}

export interface Vendor {
  vendor_id: string;
  display_name: string;
  addresses: VendorAddress[];
}

export const VENDORS_LOCKED_NOTICE = "Connect required to update";
export const MAX_VENDOR_NAME_BYTES = 200;
// Mirrors the engine's printable_display_name (Python str.isprintable): Unicode
// "Other" and "Separator" characters are refused, except the ASCII space. The
// engine stays the authority; this only explains the refusal before a request.
const NON_PRINTABLE = /(?! )[\p{C}\p{Z}]/u;

export interface VendorAddressView {
  address: string;
  status: string | null;
  watchAgain: boolean;
}

export interface VendorCardView {
  vendorId: string;
  name: string;
  addresses: VendorAddressView[];
  noAddressesText: string | null;
}

export interface VendorsView {
  // Gated controls (create, rename, add address) need an active entitlement;
  // read and removal controls always show (D-ops).
  canUpdate: boolean;
  lockedNotice: string | null;
  emptyText: string | null;
  vendors: VendorCardView[];
}

export function vendorsView(
  vendors: readonly Vendor[],
  connectActive: boolean | null,
): VendorsView {
  const canUpdate = connectActive === true;
  return {
    canUpdate,
    lockedNotice: connectActive === false ? VENDORS_LOCKED_NOTICE : null,
    emptyText: vendors.length > 0 ? null
      : canUpdate ? "No vendors yet. Add one to group the addresses a vendor writes from."
      : "No vendors yet.",
    vendors: vendors.map((vendor) => ({
      vendorId: vendor.vendor_id,
      name: vendor.display_name,
      addresses: vendor.addresses.map((item) => ({
        address: item.address,
        status: item.watched ? null : "Not watched",
        watchAgain: !item.watched,
      })),
      noAddressesText: vendor.addresses.length > 0 ? null : "No addresses yet.",
    })),
  };
}

export function vendorNameError(name: string): string | null {
  const trimmed = name.trim();
  if (trimmed === "") return "Vendor name is required.";
  if (new TextEncoder().encode(trimmed).length > MAX_VENDOR_NAME_BYTES) {
    return `Vendor name must be at most ${MAX_VENDOR_NAME_BYTES} UTF-8 bytes.`;
  }
  if (NON_PRINTABLE.test(trimmed)) {
    return "Vendor name must not contain line breaks or invisible characters.";
  }
  return null;
}

// The engine's conflict message names the other vendor or the mailbox, so it
// is shown as is; a lapsed entitlement gets the locked copy.
export function vendorErrorText(code: string | null, message: string): string {
  if (code === CONNECT_ENTITLEMENT_REQUIRED) return `${VENDORS_LOCKED_NOTICE} vendors.`;
  return message;
}

export function removeAddressConfirmText(
  vendorName: string,
  address: string,
  unwatch: boolean,
): string {
  return unwatch
    ? `Remove ${address} from ${vendorName} and stop watching it?`
    : `Remove ${address} from ${vendorName}? It stays on your watchlist.`;
}

export function deleteVendorConfirmText(
  vendorName: string,
  addressCount: number,
  unwatch: boolean,
): string {
  if (addressCount === 0) return `Delete ${vendorName}?`;
  const addresses = addressCount === 1 ? "its address" : `its ${addressCount} addresses`;
  return unwatch
    ? `Delete ${vendorName} and stop watching ${addresses}?`
    : `Delete ${vendorName}? ${addressCount === 1 ? "Its address stays" : "Its addresses stay"} on your watchlist.`;
}

// Only the latest of overlapping requests may commit its result, so an older
// vendor list that finishes last cannot overwrite a fresher one.
export function latestRequestFence() {
  let current = 0;
  return {
    begin(): number {
      current += 1;
      return current;
    },
    isLatest(request: number): boolean {
      return request === current;
    },
  };
}

// After a vendor operation, what the view reloads (D-ops, two stores). A
// success reloads with its message. A failure of an operation that can change
// the watchlist may follow a committed watchlist write, so it reloads too but
// keeps the error showing; any other failure changed nothing.
export type VendorReload = "with_message" | "keep_error" | "none";

export function vendorReloadAfter(succeeded: boolean, changesWatchlist: boolean): VendorReload {
  if (succeeded) return "with_message";
  return changesWatchlist ? "keep_error" : "none";
}

export interface VendorDraft {
  address: string;
  stopWatching: boolean;
}

// In-progress input on each vendor card, kept across re-renders, so a list
// reload or an entitlement change never wipes a half-typed address.
export function vendorDrafts() {
  const drafts = new Map<string, VendorDraft>();
  return {
    get(vendorId: string): VendorDraft {
      let draft = drafts.get(vendorId);
      if (draft === undefined) {
        draft = { address: "", stopWatching: false };
        drafts.set(vendorId, draft);
      }
      return draft;
    },
    // Drafts of vendors that no longer exist are dropped.
    keepOnly(vendorIds: Iterable<string>): void {
      const keep = new Set(vendorIds);
      for (const vendorId of [...drafts.keys()]) {
        if (!keep.has(vendorId)) drafts.delete(vendorId);
      }
    },
  };
}
