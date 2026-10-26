import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { runInNewContext } from "node:vm";
import { mockIPC, clearMocks } from "@tauri-apps/api/mocks";
import { confirmAction } from "../src/confirmation.ts";

// Exercise the installed JS plugin's real confirm -> message IPC mapping.
test("native confirmation waits, cancels and refuses invalid responses or IPC errors", async () => {
  const previousWindow = (globalThis as any).window;
  (globalThis as any).window = {};
  try {
    const capability = JSON.parse(readFileSync(new URL("../src-tauri/capabilities/default.json", import.meta.url), "utf8"));
    assert.ok(capability.permissions.includes("dialog:allow-message"), "native confirmation requires message permission");
    for (const response of ["Delete rule", "Cancel", "", false, true, null, undefined]) {
      let release!: (value: any) => void;
      let calls = 0;
      mockIPC((command, args) => {
        calls++;
        assert.equal(command, "plugin:dialog|message");
        assert.equal(args?.message, "Delete this rule?");
        assert.deepEqual(args?.buttons, { OkCancelCustom: ["Delete rule", "Cancel"] });
        return new Promise((resolve) => { release = resolve; });
      });
      let settled = false;
      const pending = confirmAction("Delete this rule?", "Delete rule").then((value) => { settled = true; return value; });
      await new Promise((resolve) => setImmediate(resolve));
      assert.equal(settled, false);
      assert.equal(await confirmAction("Duplicate"), false);
      assert.equal(calls, 1, "only one native confirmation may be open");
      release(response);
      assert.equal(await pending, response === "Delete rule");
    }
    mockIPC(() => { throw new Error("permission denied"); });
    assert.equal(await confirmAction("Delete this rule?"), false, "IPC rejection must fail closed");
    mockIPC(() => "Continue");
    assert.equal(await confirmAction("Try again"), true, "failure releases the pending dialog fence");
  } finally {
    clearMocks();
    (globalThis as any).window = previousWindow;
  }
});

test("all action confirmations await the shared native owner", () => {
  for (const file of ["main.ts", "coiSetup.ts"]) {
    const source = readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
    assert.doesNotMatch(source, /window\.confirm\s*\(/);
    assert.match(source, /import \{ confirmAction \} from "\.\/confirmation"/);
    for (const match of source.matchAll(/confirmAction\(/g)) {
      assert.match(source.slice(0, match.index), /await\s+$/, `${file} must await each confirmation`);
    }
  }
});

test("destructive action handlers do no work while confirmation is pending or cancelled", async () => {
  const source = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
  for (const [name, args] of [
    ["deleteInboxItem", [{ subject: "Saved mail" }]],
    ["clearInboxHistory", []],
    ["disconnectMailAccount", [{ display_name: "Mailbox" }]],
    ["mutateCalendarConsent", ["disconnect", { display_name: "Mailbox" }, "write"]],
    ["removeVendorAddress", ["vendor", "Vendor", "sender@example.com", true]],
    ["deleteVendor", ["vendor", "Vendor", 1, true]],
  ] as const) {
    const handler = source.match(new RegExp(`async function ${name}\\([\\s\\S]*?\\n\\}`, "u"))![0];
    let release!: (value: boolean) => void;
    let dialogs = 0;
    const pending = new Promise<boolean>((resolve) => { release = resolve; });
    // Mutation dependencies intentionally do not exist: crossing the confirmation
    // boundary before approval makes this actual production handler throw.
    const invokeHandler = runInNewContext(`${stripTypeScriptTypes(handler)}; ${name}`, {
      confirmAction: () => { dialogs++; return pending; },
      inboxMutationInFlight: () => false, mailOperationInFlight: false,
      calendarConsentOperationInFlight: null, vendorsBusy: () => false,
      CALENDAR_CONSENT_PROFILES: [{ profile: "write", actionLabel: "Calendar access" }],
      removeAddressConfirmText: () => "Remove address?", deleteVendorConfirmText: () => "Delete vendor?",
    });
    let settled = false;
    const result = invokeHandler(...args).then(() => { settled = true; });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(dialogs, 1, name);
    assert.equal(settled, false, `${name} must await the confirmation`);
    release(false);
    await result;
  }
});
