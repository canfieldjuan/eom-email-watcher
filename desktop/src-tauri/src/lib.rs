mod delivery;
mod engine;

use delivery::NotificationDelivery;
use engine::{CheckResult, Engine, EngineError, HealthStatus, InboxItem, WatchedSender};
use serde::Serialize;
use tauri::{AppHandle, Manager, State};

const INBOX_LIMIT: u16 = 50;

#[derive(Serialize)]
struct DesktopCheckResult {
    #[serde(flatten)]
    check: CheckResult,
    delivered_notifications: u64,
    failed_notifications: u64,
    remaining_notifications: u64,
}

#[tauri::command]
async fn inbox_recent(engine: State<'_, Engine>) -> Result<Vec<InboxItem>, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.recent(INBOX_LIMIT))
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn health_get(engine: State<'_, Engine>) -> Result<HealthStatus, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.health())
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
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .setup(|app| {
            let engine = Engine::for_app(app.handle())?;
            let delivery = NotificationDelivery::default();
            app.manage(engine.clone());
            app.manage(delivery.clone());
            let app = app.handle().clone();
            tauri::async_runtime::spawn_blocking(move || match delivery.deliver(&app, &engine) {
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
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
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
