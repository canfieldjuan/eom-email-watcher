mod delivery;
mod engine;
mod scheduler;

use delivery::NotificationDelivery;
use engine::{
    CheckResult, ConnectCapabilities, ConnectSummaryResult, Engine, EngineError, HealthStatus,
    InboxItem, WatchedSender,
};
use scheduler::{PollScheduler, PollingStatus};
use serde::Serialize;
use std::path::Path;
use std::time::Duration;
use tauri::{AppHandle, Manager, State};
use tauri_plugin_opener::OpenerExt;

const INBOX_LIMIT: u16 = 50;
const DEFAULT_POLL_INTERVAL_MINUTES: u64 = 120;
const STARTUP_SETTINGS_TIMEOUT: Duration = Duration::from_secs(5);

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
async fn connect_capabilities(
    engine: State<'_, Engine>,
) -> Result<ConnectCapabilities, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.connect_capabilities())
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn attachment_summarize(
    engine: State<'_, Engine>,
    message_id: String,
    part_id: String,
) -> Result<ConnectSummaryResult, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.summarize_attachment(message_id, part_id))
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
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
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.show();
            let _ = window.set_focus();
        }
    }));
    builder
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
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
            attachment_open,
            attachment_summarize,
            connect_capabilities,
            health_get,
            inbox_recent,
            watcher_check,
            watchlist_list,
            watchlist_add,
            watchlist_remove
        ])
        .run(tauri::generate_context!())
        .expect("error while running the desktop host");
}
