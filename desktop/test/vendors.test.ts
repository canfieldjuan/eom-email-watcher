import assert from "node:assert/strict";
import test from "node:test";

const acme = {
  vendor_id: "1b4e28ba-2fa1-4d2e-8e7a-2f5d3c1f0a11",
  display_name: "Acme Supply",
  addresses: [
    { address: "billing@acme.com", watched: true },
    { address: "rep@gmail.com", watched: false },
  ],
};

async function vendors() {
  const module = await import("../src/vendors.ts").catch(() => ({}));
  assert.equal(typeof module.vendorsView, "function", "vendorsView export is required");
  return module;
}

test("an active entitlement shows gated controls and no notice", async () => {
  const { vendorsView } = await vendors();
  const view = vendorsView([acme], true);
  assert.equal(view.canUpdate, true);
  assert.equal(view.lockedNotice, null);
  assert.equal(view.emptyText, null);
  assert.deepEqual(view.vendors[0].addresses.map((item) => item.address), [
    "billing@acme.com",
    "rep@gmail.com",
  ]);
});

test("a locked entitlement hides gated controls but keeps the vendors readable", async () => {
  const { vendorsView, VENDORS_LOCKED_NOTICE } = await vendors();
  const view = vendorsView([acme], false);
  assert.equal(view.canUpdate, false);
  assert.equal(view.lockedNotice, VENDORS_LOCKED_NOTICE);
  assert.equal(VENDORS_LOCKED_NOTICE, "Connect required to update");
  assert.equal(view.vendors.length, 1);
});

test("an unknown entitlement fails closed without claiming it is locked", async () => {
  const { vendorsView } = await vendors();
  const view = vendorsView([acme], null);
  assert.equal(view.canUpdate, false);
  assert.equal(view.lockedNotice, null);
});

test("empty states depend on whether vendors can be added", async () => {
  const { vendorsView } = await vendors();
  assert.match(vendorsView([], true).emptyText, /Add one/);
  assert.equal(vendorsView([], false).emptyText, "No vendors yet.");
  const bare = vendorsView([{ ...acme, addresses: [] }], true);
  assert.equal(bare.vendors[0].noAddressesText, "No addresses yet.");
});

test("an unwatched vendor address offers Watch again", async () => {
  const { vendorsView } = await vendors();
  const [watched, unwatched] = vendorsView([acme], true).vendors[0].addresses;
  assert.deepEqual(watched, { address: "billing@acme.com", status: null, watchAgain: false });
  assert.deepEqual(unwatched, { address: "rep@gmail.com", status: "Not watched", watchAgain: true });
});

test("conflicts keep the engine's message and a lapsed entitlement reads as locked", async () => {
  const { vendorErrorText } = await vendors();
  assert.equal(
    vendorErrorText("conflict", "billing@acme.com already belongs to vendor Other Co"),
    "billing@acme.com already belongs to vendor Other Co",
  );
  assert.equal(
    vendorErrorText("connect_entitlement_required", "An active Connect entitlement is required."),
    "Connect required to update vendors.",
  );
});

test("vendor names are trimmed, required, and bounded in UTF-8 bytes", async () => {
  const { vendorNameError } = await vendors();
  assert.equal(vendorNameError("   "), "Vendor name is required.");
  assert.equal(vendorNameError("x".repeat(200)), null);
  assert.equal(vendorNameError(` ${"x".repeat(200)} `), null);
  assert.match(vendorNameError("x".repeat(201)), /at most 200 UTF-8 bytes/);
  assert.match(vendorNameError("é".repeat(101)), /at most 200 UTF-8 bytes/);
});

test("confirmations say whether addresses stay watched", async () => {
  const { removeAddressConfirmText, deleteVendorConfirmText } = await vendors();
  assert.match(removeAddressConfirmText("Acme", "a@acme.com", false), /stays on your watchlist/);
  assert.match(removeAddressConfirmText("Acme", "a@acme.com", true), /stop watching it/);
  assert.equal(deleteVendorConfirmText("Acme", 0, true), "Delete Acme?");
  assert.match(deleteVendorConfirmText("Acme", 1, false), /Its address stays/);
  assert.match(deleteVendorConfirmText("Acme", 2, true), /stop watching its 2 addresses/);
});

test("vendor names refuse the characters the watchlist refuses in sender names", async () => {
  const { vendorNameError } = await vendors();
  for (const name of ["Acme Corp", "Acme\tCorp", "Soft­Hyphen", "Line\nBreak", "Zero‍Join"]) {
    assert.match(vendorNameError(name), /invisible characters/, JSON.stringify(name));
  }
  assert.equal(vendorNameError("Café Ñandú 株式会社"), null);
  assert.equal(vendorNameError("  Acme Supply  "), null);
});

test("only the latest of overlapping vendor list requests commits", async () => {
  const { latestRequestFence } = await vendors();
  const fence = latestRequestFence();
  const tabOpened = fence.begin();
  const afterMutation = fence.begin();
  // The mutation's reload finishes first, then the stale tab-open request.
  assert.equal(fence.isLatest(afterMutation), true);
  assert.equal(fence.isLatest(tabOpened), false);
});

test("a failed watchlist-changing operation still reloads, keeping its error", async () => {
  const { vendorReloadAfter } = await vendors();
  assert.equal(vendorReloadAfter(true, true), "with_message");
  assert.equal(vendorReloadAfter(true, false), "with_message");
  // The watchlist write may have committed before the database step failed.
  assert.equal(vendorReloadAfter(false, true), "keep_error");
  assert.equal(vendorReloadAfter(false, false), "none");
});
