import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { configAdmissionView } from "../src/configAdmissionView.ts";

const libSource = await readFile(new URL("../src-tauri/src/lib.rs", import.meta.url), "utf8");
const engineSource = await readFile(
  new URL("../src-tauri/src/engine.rs", import.meta.url),
  "utf8",
);
const deliverySource = await readFile(
  new URL("../src-tauri/src/delivery.rs", import.meta.url),
  "utf8",
);
const schedulerSource = await readFile(
  new URL("../src-tauri/src/scheduler.rs", import.meta.url),
  "utf8",
);
const tauriConfig = JSON.parse(
  await readFile(new URL("../src-tauri/tauri.conf.json", import.meta.url), "utf8"),
);
const uiSource = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");

const disclosureCopy =
  "Phone notification privacy " +
  "Email Watcher sends the configured ntfy service the notification topic; the watched sender's configured label, or the message-supplied display name or email address; the email subject; and either the local-model summary with any suggested action and deadline, fixed fallback text, or scheduling review text that may contain an email-derived summary. " +
  "Email Watcher does not redact or encrypt these fields at the application layer. HTTPS protects them while they travel to the service, but the configured ntfy service can read and may retain or log them. " +
  "A long random topic limits who can subscribe or publish; it does not hide the content from that service. " +
  "For confidentiality-sensitive mail, close Email Watcher and remove the topic from the private configuration before continuing. " +
  "I understand and allow this email-derived content to be sent to the configured ntfy service";

test("ntfy disclosure native startup gates every config-dependent worker", () => {
  assert.match(engineSource, /"config\.ntfy_disclosure\.status"/);
  assert.match(engineSource, /"config\.ntfy_disclosure\.acknowledge"/);
  assert.match(libSource, /fn config_admission_status/);
  assert.match(libSource, /fn config_ntfy_disclosure_acknowledge/);

  const setup = libSource.slice(libSource.indexOf(".setup(move |app|"));
  const inspect = setup.indexOf("refresh_admission");
  assert.ok(inspect >= 0, "native setup must invoke the admission coordinator");
  assert.ok(inspect < setup.indexOf("app.manage(engine.clone())"));

  const stagedWorkers = libSource.slice(
    libSource.indexOf("fn stage_admitted_workers"),
    libSource.indexOf("impl AdmissionCoordinator<AdmissionWorkers>"),
  );
  const settings = stagedWorkers.indexOf("settings_with_timeout");
  const queue = stagedWorkers.indexOf("ConnectQueueScheduler::stage_with_cancellation");
  const poller = stagedWorkers.indexOf("scheduler\n                .stage");
  assert.ok(settings >= 0, "worker startup must begin with normal settings admission");
  assert.ok(settings < queue, "settings admission must precede staged Connect queue startup");
  assert.ok(queue < poller, "Connect queue staging must precede poll staging");
  assert.match(stagedWorkers, /StartupDeliveryWorker::stage/);
  assert.match(libSource, /startup_delivery: StartupDeliveryWorker/);

  const installAttempt = libSource.slice(
    libSource.indexOf("fn install_attempt"),
    libSource.indexOf("fn refresh_with"),
  );
  assert.match(installAttempt, /workers\.activate\(\)/);
  assert.doesNotMatch(libSource, /fn start_startup_delivery/);
  assert.match(libSource, /delivery\.deliver_with_cancellation/);
  assert.match(
    libSource,
    /self\.cancellation\.cancel\(\);[\s\S]*self\.startup_delivery\.signal_stop\(\)[\s\S]*self\.scheduler\.signal_stop\(\)[\s\S]*self\.connect_queue\.signal_stop\(\)[\s\S]*self\.startup_delivery\.join\(\)[\s\S]*self\.scheduler\.join\(\)[\s\S]*self\.connect_queue\.join\(\)/,
  );
  assert.match(deliverySource, /deadline\.cancel\(\);[\s\S]*worker\.join\(\)/);
  assert.match(deliverySource, /Command::new\(&self\.program\)/);
  assert.match(deliverySource, /child\.kill\(\)/);
  assert.match(deliverySource, /child\.wait\(\)/);
  assert.match(deliverySource, /run_notification_helper/);
  assert.match(deliverySource, /notify_rust::Notification::new\(\)/);
  assert.match(
    deliverySource,
    /tauri_winrt_notification::Toast::new\(WINDOWS_NOTIFICATION_APP_ID\)[\s\S]*\.show\(\)/,
  );
  assert.equal(
    deliverySource.match(/WINDOWS_NOTIFICATION_APP_ID: &str = "([^"]+)"/)?.[1],
    tauriConfig.identifier,
  );
  assert.match(
    deliverySource,
    /cfg\(not\(any\(target_os = "linux", windows\)\)\)[\s\S]*PlatformNotificationError::Unsupported/,
  );
  assert.doesNotMatch(deliverySource, /tauri_plugin_notification::NotificationExt/);
  assert.match(deliverySource, /self\.lock\.try_lock\(\)/);
  assert.match(deliverySource, /deadline\.bounded_engine\(engine\)/);
  assert.match(schedulerSource, /fn wait_for_activation/);
  assert.match(schedulerSource, /pub fn shutdown\(&self\) -> io::Result<\(\)>/);
  assert.match(libSource, /configuration_not_admitted/);

  const guardedCommands = [
    "analysis_requeue",
    "attachment_capabilities",
    "attachment_capability_invoke",
    "attachment_open",
    "calendar_consent_connect",
    "calendar_consent_disconnect",
    "calendar_consent_status",
    "calendar_proposal_decide",
    "capability_output_export",
    "capability_output_present",
    "connect_entitlement_install",
    "connect_entitlement_status",
    "gmail_authorize",
    "health_get",
    "inbox_clear",
    "inbox_delete",
    "inbox_query",
    "mail_account_activate",
    "mail_account_connect",
    "mail_account_disconnect",
    "mail_account_reconnect",
    "mail_accounts_list",
    "settings_get",
    "settings_update",
    "watcher_check",
    "watchlist_add",
    "watchlist_list",
    "watchlist_remove",
  ];
  for (const command of guardedCommands) {
    const start = libSource.indexOf(`async fn ${command}(`);
    assert.ok(start >= 0, `missing config-dependent command ${command}`);
    const next = libSource.indexOf("#[tauri::command]", start);
    const block = libSource.slice(start, next < 0 ? undefined : next);
    assert.match(block, /admission: State<'_, AdmissionCoordinator>/, `${command} lacks gate state`);
    assert.match(block, /admission\.require_admitted\(\)\?;/, `${command} bypasses admission`);
  }
});

test("ntfy disclosure UI requires one explicit click and reconciles every outcome", () => {
  const panelSource = uiSource.match(
    /<section id="ntfy-disclosure-panel"[\s\S]*?<\/section>/,
  )?.[0];
  assert.ok(panelSource, "disclosure panel is missing");
  const panelText = panelSource.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
  assert.equal(panelText, disclosureCopy);
  assert.match(uiSource, /invoke<ConfigAdmissionStatus>\("config_admission_status"\)/);
  assert.match(
    uiSource,
    /ntfyDisclosureAcknowledge\.addEventListener\("click",[\s\S]*invoke<ConfigAdmissionStatus>\(\s*"config_ntfy_disclosure_acknowledge"/,
  );
  assert.match(uiSource, /ntfyDisclosureAcknowledge\.disabled = true/);
  assert.match(uiSource, /await refreshConfigAdmission\(\)/);

  const acknowledgementCalls = uiSource.match(/"config_ntfy_disclosure_acknowledge"/g) ?? [];
  assert.equal(acknowledgementCalls.length, 1, "render, focus, and reconciliation must not acknowledge");
  assert.doesNotMatch(uiSource, /invoke<ConfigStatus>\("config_status"\)/);
});

test("admitted startup opens the populated application while held states alone force settings", () => {
  assert.equal(configAdmissionView({ state: "admitted" }), "inbox");
  assert.equal(configAdmissionView({ state: "missing" }), "settings");
  assert.equal(configAdmissionView({ state: "acknowledgement_required" }), "settings");
  assert.equal(configAdmissionView({ state: "manual_repair_required" }), "settings");

  const renderAdmission = uiSource.slice(
    uiSource.indexOf("function renderConfigAdmission"),
    uiSource.indexOf("async function refreshConfigAdmission"),
  );
  assert.match(renderAdmission, /showView\(configAdmissionView\(status\)\)/);
  assert.doesNotMatch(renderAdmission, /showView\("settings"\)/);
});

test("scheduler shutdown cancellation reaches queue, delivery, and engine children", () => {
  assert.match(engineSource, /struct CancellationToken/);
  assert.match(engineSource, /with_cancellation/);
  assert.match(engineSource, /cancellation\.is_cancelled\(\)[\s\S]*child\.terminate\(\)/);
  assert.match(libSource, /self\.cancellation\.cancel\(\)/);
  assert.match(schedulerSource, /pump_connect_queue/);
  assert.match(schedulerSource, /check_and_deliver_with_cancellation/);
  assert.match(deliverySource, /check_and_deliver_with_cancellation/);
});
