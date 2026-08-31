mod delivery;
mod engine;
mod scheduler;

use delivery::NotificationDelivery;
use engine::{
    CheckResult, ConfigInitialization, ConnectCapabilities, ConnectCapabilityRef,
    ConnectEntitlementStatus, ConnectInvocationResult, ConnectOutputView, ConnectProviderIdentity,
    Engine, EngineError, EngineSettings, GmailAuthorization, HealthStatus, InboxItem,
    WatchedSender,
};
use scheduler::{PollScheduler, PollingStatus};
use serde::Serialize;
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::Duration;
use tauri::{AppHandle, Manager, State};
use tauri_plugin_opener::OpenerExt;

const INBOX_LIMIT: u16 = 50;
const DEFAULT_POLL_INTERVAL_MINUTES: u64 = 120;
const STARTUP_SETTINGS_TIMEOUT: Duration = Duration::from_secs(5);
#[cfg(desktop)]
const TRAY_OPEN_ID: &str = "open";
#[cfg(desktop)]
const TRAY_QUIT_ID: &str = "quit";

#[cfg(desktop)]
#[derive(Debug, PartialEq, Eq)]
enum TrayMenuAction {
    Open,
    Quit,
    Ignore,
}

#[cfg(desktop)]
fn tray_menu_action(id: &str) -> TrayMenuAction {
    match id {
        TRAY_OPEN_ID => TrayMenuAction::Open,
        TRAY_QUIT_ID => TrayMenuAction::Quit,
        _ => TrayMenuAction::Ignore,
    }
}

#[cfg(desktop)]
fn show_main_window(app: &AppHandle) {
    let Some(window) = app.get_webview_window("main") else {
        eprintln!("watcher main window is unavailable");
        return;
    };
    if let Err(error) = window.unminimize() {
        eprintln!("watcher main window could not be unminimized: {error}");
    }
    if let Err(error) = window.show() {
        eprintln!("watcher main window could not be shown: {error}");
    }
    if let Err(error) = window.set_focus() {
        eprintln!("watcher main window could not be focused: {error}");
    }
}

#[cfg(desktop)]
fn install_tray(app: &tauri::App) -> tauri::Result<()> {
    use tauri::menu::{Menu, MenuItem};
    use tauri::tray::TrayIconBuilder;

    let open = MenuItem::with_id(app, TRAY_OPEN_ID, "Open Email Watcher", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, TRAY_QUIT_ID, "Quit Email Watcher", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &quit])?;
    let mut tray = TrayIconBuilder::new()
        .tooltip("Email Watcher")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match tray_menu_action(event.id().as_ref()) {
            TrayMenuAction::Open => show_main_window(app),
            TrayMenuAction::Quit => app.exit(0),
            TrayMenuAction::Ignore => {}
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

#[derive(Serialize)]
struct DesktopCheckResult {
    #[serde(flatten)]
    check: CheckResult,
    delivered_notifications: u64,
    failed_notifications: u64,
    remaining_notifications: u64,
}

#[derive(Serialize)]
struct DesktopHealthStatus {
    #[serde(flatten)]
    health: HealthStatus,
    polling: PollingStatus,
}

struct AttachmentExports {
    directory: tempfile::TempDir,
}

impl AttachmentExports {
    fn path(&self) -> &Path {
        self.directory.path()
    }
}

#[derive(Serialize)]
struct OpenedAttachment {
    filename: String,
}

#[derive(Serialize)]
struct RevealedCapabilityOutput {
    display_name: String,
    filename: String,
}

#[derive(Serialize)]
struct ConfigStatus {
    present: bool,
}

#[tauri::command]
fn config_status(engine: State<'_, Engine>) -> Result<ConfigStatus, EngineError> {
    engine
        .config_present()
        .map(|present| ConfigStatus { present })
}

#[tauri::command]
async fn config_initialize(
    engine: State<'_, Engine>,
    timezone: String,
    model_base_url: String,
    model_name: String,
) -> Result<ConfigInitialization, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        engine.initialize_config(timezone, model_base_url, model_name)
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn inbox_recent(engine: State<'_, Engine>) -> Result<Vec<InboxItem>, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.recent(INBOX_LIMIT))
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn analysis_requeue(
    engine: State<'_, Engine>,
    message_id: String,
) -> Result<(), EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.requeue_analysis(message_id))
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn attachment_open(
    app: AppHandle,
    engine: State<'_, Engine>,
    exports: State<'_, AttachmentExports>,
    message_id: String,
    part_id: String,
) -> Result<OpenedAttachment, EngineError> {
    let engine = engine.inner().clone();
    let destination = exports.path().to_path_buf();
    let exported = tauri::async_runtime::spawn_blocking(move || {
        engine.export_attachment(message_id, part_id, destination)
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))??;
    let open_path = exported.path.to_str().ok_or_else(|| {
        EngineError::host(
            "open_failed",
            "Attachment path is not supported by this host",
        )
    })?;
    app.opener()
        .open_path(open_path, None::<&str>)
        .map_err(|_| EngineError::host("open_failed", "Desktop could not open the attachment"))?;
    Ok(OpenedAttachment {
        filename: exported.filename,
    })
}

#[tauri::command]
async fn attachment_capabilities(
    engine: State<'_, Engine>,
    message_id: String,
    part_id: String,
) -> Result<ConnectCapabilities, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        engine.attachment_capabilities(message_id, part_id)
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
// Tauri deserializes these named fields directly from the frozen frontend command.
#[allow(clippy::too_many_arguments)]
async fn attachment_capability_invoke(
    engine: State<'_, Engine>,
    request_id: String,
    message_id: String,
    part_id: String,
    provider: ConnectProviderIdentity,
    capability: ConnectCapabilityRef,
    parameters: BTreeMap<String, Value>,
    confirmed: bool,
) -> Result<ConnectInvocationResult, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        engine.invoke_attachment_capability(
            request_id, message_id, part_id, provider, capability, parameters, confirmed,
        )
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn capability_output_present(
    engine: State<'_, Engine>,
    message_id: String,
    part_id: String,
    job_id: String,
    artifact_id: String,
) -> Result<ConnectOutputView, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        engine.present_capability_output(message_id, part_id, job_id, artifact_id)
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn capability_output_export(
    app: AppHandle,
    engine: State<'_, Engine>,
    exports: State<'_, AttachmentExports>,
    message_id: String,
    part_id: String,
    job_id: String,
    artifact_id: String,
) -> Result<RevealedCapabilityOutput, EngineError> {
    let engine = engine.inner().clone();
    let destination = std::fs::canonicalize(exports.path()).map_err(|_| {
        EngineError::host("export_failed", "Desktop export directory is unavailable")
    })?;
    let engine_destination = destination.clone();
    let exported = tauri::async_runtime::spawn_blocking(move || {
        engine.export_capability_output(
            message_id,
            part_id,
            job_id,
            artifact_id,
            engine_destination,
        )
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))??;
    if exported.path.parent() != Some(destination.as_path()) {
        return Err(EngineError::host(
            "export_failed",
            "Watcher engine returned an invalid export path",
        ));
    }
    let filename = exported
        .path
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| EngineError::host("export_failed", "Export filename is unsupported"))?;
    let open_path = destination.to_str().ok_or_else(|| {
        EngineError::host(
            "open_failed",
            "Export directory path is not supported by this host",
        )
    })?;
    app.opener()
        .open_path(open_path, None::<&str>)
        .map_err(|_| EngineError::host("open_failed", "Desktop could not reveal the export"))?;
    Ok(RevealedCapabilityOutput {
        display_name: exported.output.display_name,
        filename: filename.to_owned(),
    })
}

#[tauri::command]
async fn health_get(
    engine: State<'_, Engine>,
    scheduler: State<'_, PollScheduler>,
) -> Result<DesktopHealthStatus, EngineError> {
    let engine = engine.inner().clone();
    let polling = scheduler.status();
    tauri::async_runtime::spawn_blocking(move || {
        engine
            .health()
            .map(|health| DesktopHealthStatus { health, polling })
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn connect_entitlement_status(
    engine: State<'_, Engine>,
) -> Result<ConnectEntitlementStatus, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.connect_entitlement_status())
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn connect_entitlement_install(
    engine: State<'_, Engine>,
    source_path: String,
) -> Result<ConnectEntitlementStatus, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        engine.install_connect_entitlement(PathBuf::from(source_path))
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn gmail_authorize(engine: State<'_, Engine>) -> Result<GmailAuthorization, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.authorize_gmail())
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn settings_get(engine: State<'_, Engine>) -> Result<EngineSettings, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.settings())
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn settings_update(
    engine: State<'_, Engine>,
    poll_interval_minutes: u64,
    retention_days: u64,
    notifications_enabled: bool,
    model_base_url: Option<String>,
    model_name: Option<String>,
) -> Result<EngineSettings, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        engine.update_settings(
            poll_interval_minutes,
            retention_days,
            notifications_enabled,
            model_base_url,
            model_name,
        )
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn watcher_check(
    app: AppHandle,
    engine: State<'_, Engine>,
    delivery: State<'_, NotificationDelivery>,
) -> Result<DesktopCheckResult, EngineError> {
    let engine = engine.inner().clone();
    let delivery = delivery.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let outcome = delivery.check_and_deliver(&app, &engine)?;
        let remaining_notifications = outcome
            .check
            .pending_notifications
            .saturating_sub(outcome.delivery.delivered);
        Ok(DesktopCheckResult {
            check: outcome.check,
            delivered_notifications: outcome.delivery.delivered,
            failed_notifications: outcome.delivery.failed,
            remaining_notifications,
        })
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn watchlist_list(engine: State<'_, Engine>) -> Result<Vec<WatchedSender>, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.list())
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn watchlist_add(
    engine: State<'_, Engine>,
    email: String,
    name: Option<String>,
) -> Result<WatchedSender, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.add(email, name))
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn watchlist_remove(
    engine: State<'_, Engine>,
    email: String,
) -> Result<WatchedSender, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.remove(email))
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default();
    #[cfg(desktop)]
    let builder = builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
        show_main_window(app);
    }));
    #[cfg(desktop)]
    let builder = builder.on_window_event(|window, event| {
        if window.label() == "main"
            && let tauri::WindowEvent::CloseRequested { api, .. } = event
        {
            api.prevent_close();
            if let Err(error) = window.hide() {
                eprintln!("watcher main window could not be hidden: {error}");
            }
        }
    });
    builder
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            #[cfg(desktop)]
            install_tray(app)?;
            let engine = Engine::for_app(app.handle())?;
            let delivery = NotificationDelivery::default();
            let exports = AttachmentExports {
                directory: tempfile::Builder::new()
                    .prefix("email-watcher-attachments-")
                    .tempdir()?,
            };
            let (poll_interval_minutes, polling_supported) =
                match engine.settings_with_timeout(STARTUP_SETTINGS_TIMEOUT) {
                    Ok(settings) => (
                        settings.poll_interval_minutes,
                        settings.polling_supported,
                    ),
                    Err(error) => {
                        eprintln!(
                            "watcher polling settings unavailable ({}): {}; polling disabled with {}-minute default",
                            error.code, error.message, DEFAULT_POLL_INTERVAL_MINUTES
                        );
                        (DEFAULT_POLL_INTERVAL_MINUTES, false)
                    }
                };
            if !polling_supported {
                eprintln!(
                    "watcher automatic polling is disabled for the current host configuration"
                );
            }
            let scheduler = PollScheduler::new(poll_interval_minutes, polling_supported);
            app.manage(engine.clone());
            app.manage(delivery.clone());
            app.manage(exports);
            app.manage(scheduler.clone());
            let startup_app = app.handle().clone();
            let startup_engine = engine.clone();
            let startup_delivery = delivery.clone();
            tauri::async_runtime::spawn_blocking(move || {
                match startup_delivery.deliver(&startup_app, &startup_engine) {
                    Ok(outcome) if outcome.failed > 0 => eprintln!(
                        "{} watcher startup notifications remain queued after delivery errors",
                        outcome.failed
                    ),
                    Ok(_) => {}
                    Err(error) => {
                        eprintln!(
                            "watcher startup notification delivery failed ({}): {}",
                            error.code, error.message
                        );
                    }
                }
            });
            scheduler.start(app.handle().clone(), engine, delivery)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            analysis_requeue,
            attachment_capabilities,
            attachment_capability_invoke,
            attachment_open,
            capability_output_export,
            capability_output_present,
            connect_entitlement_install,
            connect_entitlement_status,
            config_initialize,
            config_status,
            gmail_authorize,
            health_get,
            inbox_recent,
            settings_get,
            settings_update,
            watcher_check,
            watchlist_list,
            watchlist_add,
            watchlist_remove
        ])
        .run(tauri::generate_context!())
        .expect("error while running the desktop host");
}

#[cfg(all(test, desktop))]
mod tests {
    use super::*;

    #[test]
    fn tray_menu_routing_is_explicit() {
        assert_eq!(tray_menu_action(TRAY_OPEN_ID), TrayMenuAction::Open);
        assert_eq!(tray_menu_action(TRAY_QUIT_ID), TrayMenuAction::Quit);
        assert_eq!(tray_menu_action("unexpected"), TrayMenuAction::Ignore);
    }
}
