mod engine;

use engine::{CheckResult, Engine, EngineError, HealthStatus, InboxItem, WatchedSender};
use tauri::{Manager, State};

const INBOX_LIMIT: u16 = 50;

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
async fn watcher_check(engine: State<'_, Engine>) -> Result<CheckResult, EngineError> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.check())
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
        .setup(|app| {
            let engine = Engine::for_app(app.handle())?;
            app.manage(engine);
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
