import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  configAdmissionView,
  isCurrentConfigInitializationResult,
  reconcileConfigAdmissionRefresh,
} from "../src/configAdmissionView.ts";

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

test("engine child retains cancellation ownership through pipe drainage", () => {
  assert.doesNotMatch(engineSource, /wait_with_output/);
  assert.match(engineSource, /struct EnginePipeDrain/);
  assert.match(engineSource, /fn collect_output/);
  assert.match(
    engineSource,
    /self\.control\.terminate\(\);[\s\S]*self\.join_drains/,
  );
});

test("ntfy disclosure native startup gates every config-dependent worker", () => {
  assert.match(engineSource, /"config\.ntfy_disclosure\.status"/);
  assert.match(engineSource, /"config\.ntfy_disclosure\.acknowledge"/);
  assert.match(libSource, /fn config_admission_status/);
  assert.match(libSource, /fn config_ntfy_disclosure_acknowledge/);
  assert.match(libSource, /watcher:\/\/config-admission/);
  assert.match(libSource, /fn engine_error_observer/);
  assert.match(engineSource, /binding\.observer/);

  const setup = libSource.slice(libSource.indexOf(".setup(move |app|"));
  const inspect = setup.indexOf("refresh_admission");
  assert.ok(inspect >= 0, "native setup must invoke the admission coordinator");
  assert.ok(inspect < setup.indexOf("app.manage(engine.clone())"));

  const stagedWorkers = libSource.slice(
    libSource.indexOf("fn stage_admitted_workers"),
    libSource.indexOf("impl AdmissionCoordinator<AdmissionWorkers>"),
  );
  const snapshot = stagedWorkers.indexOf("admission_snapshot");
  const queue = stagedWorkers.indexOf("ConnectQueueScheduler::stage_with_cancellation");
  const poller = stagedWorkers.indexOf("scheduler\n                .stage");
  assert.ok(snapshot >= 0, "worker startup must begin with one atomic admission snapshot");
  assert.ok(snapshot < queue, "admission snapshot must precede staged Connect queue startup");
  assert.doesNotMatch(stagedWorkers, /settings_with_timeout|\.settings\(\)/);
  assert.ok(queue < poller, "Connect queue staging must precede poll staging");
  assert.match(stagedWorkers, /StartupDeliveryWorker::stage/);
  assert.match(libSource, /startup_delivery: StartupDeliveryWorker/);

  const installAttempt = libSource.slice(
    libSource.indexOf("fn install_attempt"),
    libSource.indexOf("fn refresh_with"),
  );
  const compare = installAttempt.indexOf("workers.revalidate()");
  const activate = installAttempt.indexOf("workers.activate()");
  assert.ok(compare >= 0, "staged workers must revalidate their admission token");
  assert.ok(compare < activate, "admission token compare must precede worker activation");
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
    "certificate_expiry_ledger_list",
    "gmail_authorize",
    "gmail_labels_catalog",
    "gmail_label_selectors_list",
    "gmail_label_selector_add",
    "gmail_label_selector_remove",
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
    "watcher_check",
    "watchlist_list",
  ];
  for (const command of guardedCommands) {
    const start = libSource.indexOf(`async fn ${command}(`);
    assert.ok(start >= 0, `missing config-dependent command ${command}`);
    const next = libSource.indexOf("#[tauri::command]", start);
    const block = libSource.slice(start, next < 0 ? undefined : next);
    assert.match(block, /admission: State<'_, AdmissionCoordinator>/, `${command} lacks gate state`);
    assert.match(
      block,
      /let _admission_permit = admission\.require_admitted\(\)\?;/,
      `${command} does not retain its admission permit through the effect`,
    );
  }

  for (const command of ["settings_update", "watchlist_add", "watchlist_remove"]) {
    const start = libSource.indexOf(`async fn ${command}(`);
    assert.ok(start >= 0, `missing config mutation command ${command}`);
    const next = libSource.indexOf("#[tauri::command]", start);
    const block = libSource.slice(start, next < 0 ? undefined : next);
    assert.match(block, /app: AppHandle/, `${command} cannot restage native workers`);
    assert.match(
      block,
      /delivery: State<'_, NotificationDelivery>/,
      `${command} cannot restage notification delivery`,
    );
    assert.match(block, /admission\.mutate_config\(/, `${command} bypasses mutation transaction`);
    assert.doesNotMatch(block, /renew_after_config_mutation/, `${command} retains split renewal`);
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
  assert.match(uiSource, /listen<ConfigAdmissionStatus>\("watcher:\/\/config-admission"/);
  assert.match(uiSource, /status\.generation <= configAdmissionGeneration/);
  assert.match(uiSource, /const startupEpoch = \+\+configuredStartupEpoch/);
  assert.match(uiSource, /startConfiguredDesktop\(configAdmissionGeneration, startupEpoch\)/);
  assert.doesNotMatch(uiSource, /status\.state === "admitted" && configurationReady/);
  assert.match(uiSource, /renderConfigAdmission\(event\.payload\)/);
  assert.match(
    uiSource,
    /async function initializeDesktop[\s\S]*await configAdmissionListenerReady;[\s\S]*await refreshConfigAdmission\(\)/,
  );

  const acknowledgementCalls = uiSource.match(/"config_ntfy_disclosure_acknowledge"/g) ?? [];
  assert.equal(acknowledgementCalls.length, 1, "render, focus, and reconciliation must not acknowledge");
  assert.doesNotMatch(uiSource, /invoke<ConfigStatus>\("config_status"\)/);
});

test("newer admission generation suppresses a stale refresh failure", async () => {
  let generation = 4;
  let configurationReady = false;
  let renderedState = "manual_repair_required";
  let rejectRequest: ((reason?: unknown) => void) | undefined;
  const request = new Promise<never>((_resolve, reject) => {
    rejectRequest = reject;
  });
  const refresh = reconcileConfigAdmissionRefresh({
    currentGeneration: () => generation,
    request: () => request,
    renderStatus: () => assert.fail("rejected request rendered a status"),
    renderFailure: () => {
      configurationReady = false;
      renderedState = "manual_repair_required";
    },
  });

  generation = 5;
  configurationReady = true;
  renderedState = "admitted";
  rejectRequest?.(new Error("older status request failed"));
  await refresh;
  assert.equal(configurationReady, true);
  assert.equal(renderedState, "admitted");

  configurationReady = true;
  renderedState = "admitted";
  await reconcileConfigAdmissionRefresh({
    currentGeneration: () => generation,
    request: () => Promise.reject(new Error("current status request failed")),
    renderStatus: () => assert.fail("rejected request rendered a status"),
    renderFailure: () => {
      configurationReady = false;
      renderedState = "manual_repair_required";
    },
  });
  assert.equal(configurationReady, false);
  assert.equal(renderedState, "manual_repair_required");

  assert.match(
    uiSource,
    /reconcileConfigAdmissionRefresh\(\{[\s\S]*currentGeneration: \(\) => configAdmissionGeneration,[\s\S]*request: \(\) => invoke<ConfigAdmissionStatus>\("config_admission_status"\),[\s\S]*renderStatus: renderConfigAdmission,[\s\S]*renderFailure:[\s\S]*renderConfigAdmissionState\(\{ state: "manual_repair_required" \}\)/,
  );
});

test("initialization success copy requires the exact current admission generation", () => {
  assert.equal(isCurrentConfigInitializationResult(7, 7), true);
  assert.equal(isCurrentConfigInitializationResult(6, 7), false);
  assert.equal(isCurrentConfigInitializationResult(8, 7), false);
  assert.match(
    uiSource,
    /status\.state === "admitted"[\s\S]*isCurrentConfigInitializationResult\(status\.generation, configAdmissionGeneration\)[\s\S]*Configuration created\./,
  );
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
  assert.match(engineSource, /struct CancellationRegistration/);
  assert.match(engineSource, /wait_for_registrations/);
  assert.match(engineSource, /with_cancellation/);
  assert.match(
    engineSource,
    /fn cancellation_requested[\s\S]*iter\(\)\.any\(CancellationToken::is_cancelled\)/,
  );
  assert.match(
    engineSource,
    /cancellation_requested\(&self\.cancellations\)[\s\S]*child\.terminate\(\)/,
  );
  assert.match(libSource, /self\.cancellation\.cancel\(\)/);
  assert.match(schedulerSource, /pump_connect_queue/);
  assert.match(schedulerSource, /check_and_deliver_with_cancellation/);
  assert.match(deliverySource, /check_and_deliver_with_cancellation/);
  assert.doesNotMatch(schedulerSource, /process::abort/);
});
