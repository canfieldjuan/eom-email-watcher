use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::ffi::OsString;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager};
use tauri_plugin_shell::ShellExt;

#[cfg(unix)]
use std::os::unix::process::CommandExt;

const PROTOCOL_VERSION: u8 = 1;

fn default_config_path(home_dir: &Path) -> PathBuf {
    home_dir.join(".config/eom-email-watcher/config.toml")
}

fn terminate_child(child: &mut Child) {
    #[cfg(unix)]
    if let Ok(group_id) = i32::try_from(child.id()) {
        // The child starts a dedicated process group, so this also terminates
        // uv-launched Python descendants that would otherwise retain locks.
        // SAFETY: the negative id targets only the process group created for
        // this child; it is not derived from frontend or engine input.
        unsafe {
            libc::kill(-group_id, libc::SIGKILL);
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

#[derive(Clone)]
pub struct Engine {
    program: OsString,
    args: Vec<OsString>,
    config_path: PathBuf,
    gmail_check_gate: Arc<Mutex<()>>,
    request_timeout: Option<Duration>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct WatchedSender {
    pub email: String,
    pub name: Option<String>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
pub struct InboxAttachment {
    pub part_id: String,
    pub attachment_id: Option<String>,
    pub filename: String,
    pub media_type: String,
    pub byte_size: u64,
    #[serde(default)]
    pub capability_results: Vec<AttachmentCapabilityResult>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct AttachmentCapabilityResult {
    pub capability_id: String,
    pub capability_version: String,
    pub status: String,
    pub updated_at: String,
    pub summary: Option<ConnectSummary>,
    pub error: Option<EngineError>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectSummary {
    pub summary_version: String,
    pub text: String,
    pub warnings: Vec<ConnectWarning>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectWarning {
    pub code: String,
    pub message: String,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectCapability {
    pub id: String,
    pub version: String,
    pub accepts: Vec<String>,
    pub max_input_bytes: u64,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectDiagnostic {
    pub code: String,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectCapabilities {
    pub items: Vec<ConnectCapability>,
    pub diagnostic: Option<ConnectDiagnostic>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectSummaryResult {
    pub job_id: String,
    pub capability_id: String,
    pub capability_version: String,
    pub status: String,
    pub summary: ConnectSummary,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
pub struct ExportedAttachment {
    pub filename: String,
    pub path: PathBuf,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
pub struct InboxItem {
    pub message_id: String,
    pub received_at: String,
    pub sender: String,
    pub sender_name: Option<String>,
    pub subject: String,
    pub status: String,
    pub analysis_at: Option<String>,
    pub priority: Option<String>,
    pub summary: Option<String>,
    pub action_required: Option<i64>,
    pub suggested_action: Option<String>,
    pub deadline_text: Option<String>,
    pub deadline_iso: Option<String>,
    pub confidence: Option<f64>,
    pub attempts: i64,
    pub next_retry_at: Option<String>,
    pub analysis_retryable: Option<bool>,
    pub analysis_error_code: Option<String>,
    pub analysis_retry_after_seconds: Option<u64>,
    pub fallback_notified_at: Option<String>,
    pub notified_at: Option<String>,
    pub last_error: Option<String>,
    #[serde(default)]
    pub attachments: Vec<InboxAttachment>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct HealthStatus {
    pub database: DatabaseHealth,
    pub gmail: GmailHealth,
    pub last_check: Option<String>,
    pub local_model: LocalModelHealth,
    pub notifications: NotificationHealth,
    pub production_check_supported: bool,
    pub watchlist_count: u64,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct DatabaseHealth {
    pub ok: bool,
    pub initialized: bool,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct GmailHealth {
    pub credentials_configured: bool,
    pub connected: bool,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct GmailAuthorization {
    pub baseline_initialized: bool,
    pub connected: bool,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct LocalModelHealth {
    pub authentication_required: bool,
    pub detail: String,
    pub endpoint: String,
    pub model: String,
    pub ok: bool,
    pub token_configured: bool,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct NotificationHealth {
    pub delivery: String,
    pub enabled: bool,
    pub host_delivery_ready: bool,
    pub ntfy_configured: bool,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct CheckResult {
    pub active: bool,
    pub discovered: u64,
    pub summarized: u64,
    pub fallback_notified: u64,
    pub purged: u64,
    pub stale_cursor_recovered: bool,
    pub pending_notifications: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct NotificationIntent {
    pub analysis_at: Option<String>,
    pub body: String,
    pub kind: String,
    pub message_id: String,
    pub priority: String,
    pub title: String,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct EngineSettings {
    pub notifications_enabled: bool,
    pub poll_interval_minutes: u64,
    pub polling_supported: bool,
    pub retention_days: u64,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct EngineError {
    pub code: String,
    pub message: String,
}

#[derive(Serialize)]
struct EngineRequest<'a> {
    protocol: u8,
    operation: &'a str,
    config_path: String,
    payload: Value,
}

#[derive(Deserialize)]
struct EngineEnvelope<T> {
    protocol: u8,
    ok: bool,
    operation: Option<String>,
    data: Option<T>,
    error: Option<EngineError>,
}

#[derive(Deserialize)]
struct SenderItems {
    items: Vec<WatchedSender>,
}

#[derive(Deserialize)]
struct SenderItem {
    item: WatchedSender,
}

#[derive(Deserialize)]
struct InboxItems {
    items: Vec<InboxItem>,
}

#[derive(Deserialize)]
struct NotificationItems {
    items: Vec<NotificationIntent>,
}

#[derive(Deserialize)]
struct NotificationAcknowledgement {
    status: String,
}

#[derive(Deserialize)]
struct AnalysisRequeue {
    status: String,
}

impl EngineError {
    pub(crate) fn host(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_owned(),
            message: message.into(),
        }
    }

    fn for_frontend(self) -> Self {
        if self.code == "configuration_error" {
            return Self::host(
                "configuration_error",
                "Watcher configuration is missing or invalid; inspect desktop logs",
            );
        }
        self
    }
}

impl Engine {
    pub fn for_app(app: &AppHandle) -> Result<Self, Box<dyn std::error::Error>> {
        let config_path = std::env::var_os("EOM_EMAIL_WATCHER_CONFIG")
            .map(PathBuf::from)
            .unwrap_or(default_config_path(&app.path().home_dir()?));

        if let Some(program) = std::env::var_os("EOM_EMAIL_ENGINE_BIN") {
            return Ok(Self {
                program,
                args: Vec::new(),
                config_path,
                gmail_check_gate: Arc::new(Mutex::new(())),
                request_timeout: None,
            });
        }

        let sidecar: Command = app.shell().sidecar("eom-mail-engine")?.into();
        let packaged_program = sidecar.get_program().to_os_string();
        if Path::new(&packaged_program).is_file() {
            return Ok(Self {
                program: packaged_program,
                args: sidecar.get_args().map(OsString::from).collect(),
                config_path,
                gmail_check_gate: Arc::new(Mutex::new(())),
                request_timeout: None,
            });
        }

        let project_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .ok_or("desktop project must remain inside the watcher repository")?;
        Ok(Self {
            program: OsString::from("uv"),
            args: vec![
                OsString::from("run"),
                OsString::from("--project"),
                project_root.as_os_str().to_owned(),
                OsString::from("eom-mail-engine"),
            ],
            config_path,
            gmail_check_gate: Arc::new(Mutex::new(())),
            request_timeout: None,
        })
    }

    #[cfg(test)]
    fn with_command(
        program: impl Into<OsString>,
        args: Vec<OsString>,
        config_path: PathBuf,
    ) -> Self {
        Self {
            program: program.into(),
            args,
            config_path,
            gmail_check_gate: Arc::new(Mutex::new(())),
            request_timeout: None,
        }
    }

    pub fn list(&self) -> Result<Vec<WatchedSender>, EngineError> {
        self.request::<SenderItems>("watchlist.list", json!({}))
            .map(|data| data.items)
    }

    pub fn recent(&self, limit: u16) -> Result<Vec<InboxItem>, EngineError> {
        self.request::<InboxItems>("inbox.recent", json!({"limit": limit}))
            .map(|data| data.items)
    }

    pub fn requeue_analysis(&self, message_id: String) -> Result<(), EngineError> {
        let response =
            self.request::<AnalysisRequeue>("analysis.requeue", json!({"message_id": message_id}))?;
        if response.status == "requeued" {
            return Ok(());
        }
        Err(EngineError::host(
            "engine_protocol_error",
            "Watcher engine returned an invalid analysis requeue result",
        ))
    }

    pub fn export_attachment(
        &self,
        message_id: String,
        part_id: String,
        destination_dir: PathBuf,
    ) -> Result<ExportedAttachment, EngineError> {
        self.request(
            "attachment.export",
            json!({
                "destination_dir": destination_dir,
                "message_id": message_id,
                "part_id": part_id,
            }),
        )
    }

    pub fn connect_capabilities(&self) -> Result<ConnectCapabilities, EngineError> {
        self.request("connect.capabilities", json!({}))
    }

    pub fn summarize_attachment(
        &self,
        message_id: String,
        part_id: String,
    ) -> Result<ConnectSummaryResult, EngineError> {
        self.request(
            "connect.attachment.summarize",
            json!({"message_id": message_id, "part_id": part_id}),
        )
    }

    pub fn health(&self) -> Result<HealthStatus, EngineError> {
        self.request("health.get", json!({}))
    }

    pub fn authorize_gmail(&self) -> Result<GmailAuthorization, EngineError> {
        let _guard = self
            .gmail_check_gate
            .lock()
            .map_err(|_| EngineError::host("host_error", "Gmail operation coordinator stopped"))?;
        self.request("gmail.authorize", json!({}))
    }

    pub fn settings_with_timeout(&self, timeout: Duration) -> Result<EngineSettings, EngineError> {
        self.request_with_timeout("settings.get", json!({}), timeout)
    }

    pub fn settings(&self) -> Result<EngineSettings, EngineError> {
        self.request("settings.get", json!({}))
    }

    pub fn update_settings(
        &self,
        poll_interval_minutes: u64,
        retention_days: u64,
        notifications_enabled: bool,
    ) -> Result<EngineSettings, EngineError> {
        self.request(
            "settings.update",
            json!({
                "notifications_enabled": notifications_enabled,
                "poll_interval_minutes": poll_interval_minutes,
                "retention_days": retention_days,
            }),
        )
    }

    pub fn with_request_timeout(&self, timeout: Duration) -> Self {
        let mut engine = self.clone();
        engine.request_timeout = Some(timeout);
        engine
    }

    pub fn check(&self) -> Result<CheckResult, EngineError> {
        let _guard = self
            .gmail_check_gate
            .lock()
            .map_err(|_| EngineError::host("host_error", "Gmail operation coordinator stopped"))?;
        self.request("watcher.check", json!({"dry_run": false}))
    }

    pub fn pending_notifications(
        &self,
        limit: u16,
    ) -> Result<Vec<NotificationIntent>, EngineError> {
        self.request::<NotificationItems>("notifications.pending", json!({"limit": limit}))
            .map(|data| data.items)
    }

    pub fn acknowledge_notification(&self, intent: &NotificationIntent) -> Result<(), EngineError> {
        let acknowledgement = self.request::<NotificationAcknowledgement>(
            "notifications.ack",
            json!({
                "analysis_at": &intent.analysis_at,
                "kind": &intent.kind,
                "message_id": &intent.message_id,
            }),
        )?;
        if matches!(
            acknowledgement.status.as_str(),
            "acknowledged" | "already_acknowledged"
        ) {
            return Ok(());
        }
        Err(EngineError::host(
            "engine_protocol_error",
            "Watcher engine returned an invalid notification acknowledgement",
        ))
    }

    pub fn add(&self, email: String, name: Option<String>) -> Result<WatchedSender, EngineError> {
        self.request::<SenderItem>("watchlist.add", json!({"email": email, "name": name}))
            .map(|data| data.item)
    }

    pub fn remove(&self, email: String) -> Result<WatchedSender, EngineError> {
        self.request::<SenderItem>("watchlist.remove", json!({"email": email}))
            .map(|data| data.item)
    }

    fn request<T: DeserializeOwned>(
        &self,
        operation: &str,
        payload: Value,
    ) -> Result<T, EngineError> {
        self.request_inner(operation, payload, self.request_timeout)
    }

    fn request_with_timeout<T: DeserializeOwned>(
        &self,
        operation: &str,
        payload: Value,
        timeout: Duration,
    ) -> Result<T, EngineError> {
        self.request_inner(operation, payload, Some(timeout))
    }

    fn request_inner<T: DeserializeOwned>(
        &self,
        operation: &str,
        payload: Value,
        timeout: Option<Duration>,
    ) -> Result<T, EngineError> {
        let request = EngineRequest {
            protocol: PROTOCOL_VERSION,
            operation,
            config_path: self.config_path.to_string_lossy().into_owned(),
            payload,
        };
        let encoded = serde_json::to_vec(&request).map_err(|_| {
            EngineError::host(
                "host_error",
                "Desktop host could not encode the engine request",
            )
        })?;

        let mut command = Command::new(&self.program);
        command
            .args(&self.args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(unix)]
        command.process_group(0);
        let mut child = command.spawn().map_err(|_| {
            EngineError::host(
                "engine_unavailable",
                "Watcher engine is unavailable; reinstall it or inspect desktop logs",
            )
        })?;

        let write_result = child
            .stdin
            .take()
            .ok_or_else(|| EngineError::host("host_error", "Engine stdin was unavailable"))
            .and_then(|mut stdin| {
                stdin.write_all(&encoded).map_err(|_| {
                    EngineError::host(
                        "engine_unavailable",
                        "Watcher engine stopped before startup",
                    )
                })
            });
        if let Err(error) = write_result {
            terminate_child(&mut child);
            return Err(error);
        }

        if let Some(timeout) = timeout {
            let started = Instant::now();
            loop {
                match child.try_wait() {
                    Ok(Some(_)) => break,
                    Ok(None) if started.elapsed() < timeout => {
                        std::thread::sleep(Duration::from_millis(10));
                    }
                    Ok(None) => {
                        terminate_child(&mut child);
                        return Err(EngineError::host(
                            "engine_timeout",
                            "Watcher engine did not respond before its timeout",
                        ));
                    }
                    Err(_) => {
                        terminate_child(&mut child);
                        return Err(EngineError::host(
                            "engine_unavailable",
                            "Watcher engine status could not be inspected",
                        ));
                    }
                }
            }
        }

        let output = child.wait_with_output().map_err(|_| {
            EngineError::host(
                "engine_unavailable",
                "Watcher engine did not return a result",
            )
        })?;
        let stderr = String::from_utf8_lossy(&output.stderr);
        if !stderr.trim().is_empty() {
            eprintln!("watcher engine {operation} stderr: {}", stderr.trim());
        }
        let envelope: EngineEnvelope<T> = serde_json::from_slice(&output.stdout).map_err(|_| {
            EngineError::host(
                "engine_protocol_error",
                "Watcher engine returned an invalid response; inspect desktop logs",
            )
        })?;

        if envelope.protocol != PROTOCOL_VERSION || envelope.operation.as_deref() != Some(operation)
        {
            return Err(EngineError::host(
                "engine_protocol_error",
                "Watcher engine returned a mismatched response",
            ));
        }
        if !envelope.ok {
            let error = envelope.error.unwrap_or_else(|| {
                EngineError::host(
                    "engine_protocol_error",
                    "Watcher engine omitted error details",
                )
            });
            eprintln!(
                "watcher engine {operation} failed ({}): {}",
                error.code, error.message
            );
            return Err(error.for_frontend());
        }
        if !output.status.success() {
            return Err(EngineError::host(
                "engine_protocol_error",
                "Watcher engine reported success with a failing exit status",
            ));
        }
        envelope.data.ok_or_else(|| {
            EngineError::host(
                "engine_protocol_error",
                "Watcher engine omitted response data",
            )
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn default_config_matches_python_watcher_location() {
        assert_eq!(
            default_config_path(Path::new("/home/watcher")),
            PathBuf::from("/home/watcher/.config/eom-email-watcher/config.toml")
        );
    }

    #[test]
    fn configuration_errors_do_not_expose_paths_to_frontend() {
        let error = EngineError {
            code: "configuration_error".into(),
            message: "Configuration not found: /home/private/config.toml".into(),
        };

        assert_eq!(
            error.for_frontend(),
            EngineError {
                code: "configuration_error".into(),
                message: "Watcher configuration is missing or invalid; inspect desktop logs".into(),
            }
        );
    }

    #[test]
    fn protocol_v1_inbox_defaults_attachments_from_older_engines() {
        let item: InboxItem = serde_json::from_value(json!({
            "message_id": "message-1",
            "received_at": "2026-08-29T12:00:00+00:00",
            "sender": "sender@example.com",
            "sender_name": null,
            "subject": "Subject",
            "status": "pending",
            "analysis_at": null,
            "priority": null,
            "summary": null,
            "action_required": null,
            "suggested_action": null,
            "deadline_text": null,
            "deadline_iso": null,
            "confidence": null,
            "attempts": 0,
            "next_retry_at": null,
            "analysis_retryable": null,
            "analysis_error_code": null,
            "analysis_retry_after_seconds": null,
            "fallback_notified_at": null,
            "notified_at": null,
            "last_error": null
        }))
        .expect("protocol-v1 inbox row without attachments must remain valid");

        assert!(item.attachments.is_empty());
    }

    #[test]
    fn protocol_v1_gmail_authorization_result_is_typed() {
        let result: GmailAuthorization = serde_json::from_value(json!({
            "baseline_initialized": true,
            "connected": true
        }))
        .expect("deserialize Gmail authorization result");

        assert_eq!(
            result,
            GmailAuthorization {
                baseline_initialized: true,
                connected: true,
            }
        );
    }

    #[cfg(unix)]
    #[test]
    fn bounded_settings_request_terminates_a_stalled_engine() {
        let engine = Engine::with_command(
            "sh",
            vec![OsString::from("-c"), OsString::from("exec sleep 5")],
            PathBuf::from("unused.toml"),
        );
        let started = Instant::now();

        let error = engine
            .settings_with_timeout(Duration::from_millis(20))
            .expect_err("stalled settings request must time out");

        assert_eq!(error.code, "engine_timeout");
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[cfg(unix)]
    #[test]
    fn bounded_engine_terminates_a_stalled_scheduled_operation() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let descendant_pid_file = directory.path().join("descendant.pid");
        let engine = Engine::with_command(
            "sh",
            vec![
                OsString::from("-c"),
                OsString::from("sleep 30 & echo $! > \"$1\"; wait"),
                OsString::from("engine-timeout-probe"),
                descendant_pid_file.as_os_str().to_owned(),
            ],
            PathBuf::from("unused.toml"),
        )
        .with_request_timeout(Duration::from_millis(500));

        assert_eq!(
            engine
                .check()
                .expect_err("stalled check must time out")
                .code,
            "engine_timeout"
        );
        let descendant_pid: i32 = fs::read_to_string(descendant_pid_file)
            .expect("read descendant pid")
            .trim()
            .parse()
            .expect("parse descendant pid");
        for _ in 0..100 {
            if unsafe { libc::kill(descendant_pid, 0) } != 0 {
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        panic!("timed-out engine descendant {descendant_pid} is still running");
    }

    #[cfg(unix)]
    #[test]
    fn gmail_authorization_serializes_watcher_checks() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let authorization_started = directory.path().join("authorization-started");
        let check_started = directory.path().join("check-started");
        let engine = Engine::with_command(
            "sh",
            vec![
                OsString::from("-c"),
                OsString::from(
                    r#"request=$(cat)
case "$request" in
  *gmail.authorize*)
    : > "$1"
    sleep 0.2
    printf '%s\n' '{"protocol":1,"ok":true,"operation":"gmail.authorize","data":{"baseline_initialized":true,"connected":true}}'
    ;;
  *)
    : > "$2"
    printf '%s\n' '{"protocol":1,"ok":true,"operation":"watcher.check","data":{"active":true,"discovered":0,"summarized":0,"fallback_notified":0,"purged":0,"stale_cursor_recovered":false,"pending_notifications":0}}'
    ;;
esac"#,
                ),
                OsString::from("engine-gmail-gate-probe"),
                authorization_started.as_os_str().to_owned(),
                check_started.as_os_str().to_owned(),
            ],
            PathBuf::from("unused.toml"),
        );

        let authorization_engine = engine.clone();
        let authorization = std::thread::spawn(move || authorization_engine.authorize_gmail());
        for _ in 0..100 {
            if authorization_started.exists() {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(
            authorization_started.exists(),
            "authorization probe did not start"
        );

        let check_engine = engine.clone();
        let check = std::thread::spawn(move || check_engine.check());
        std::thread::sleep(Duration::from_millis(50));
        assert!(
            !check_started.exists(),
            "watcher check started before authorization completed"
        );

        assert!(
            authorization
                .join()
                .expect("authorization thread")
                .expect("authorization result")
                .connected
        );
        assert!(
            check
                .join()
                .expect("check thread")
                .expect("check result")
                .active
        );
        assert!(check_started.exists());
    }

    fn real_engine(config_path: PathBuf) -> Engine {
        let project_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .expect("repository root");
        Engine::with_command(
            "uv",
            vec![
                OsString::from("run"),
                OsString::from("--project"),
                project_root.as_os_str().to_owned(),
                OsString::from("eom-mail-engine"),
            ],
            config_path,
        )
    }

    #[test]
    fn desktop_operations_use_real_engine_contract() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let config_path = directory.path().join("config.toml");
        let database_path = directory.path().join("watcher.sqlite3");
        let gmail_credentials_path = directory.path().join("gmail-credentials.json");
        let gmail_token_path = directory.path().join("gmail-token.json");
        fs::write(
            &config_path,
            format!(
                r#"database_file = "{}"
gmail_credentials_file = "{}"
gmail_token_file = "{}"
model_base_url = "http://127.0.0.1:9/v1"
model_name = "local-model"
model_require_auth = false
notifications_enabled = true
"#,
                database_path.display(),
                gmail_credentials_path.display(),
                gmail_token_path.display()
            ),
        )
        .expect("write config");
        let engine = real_engine(config_path);

        let health = engine.health().expect("read engine health");
        assert_eq!(
            health.database,
            DatabaseHealth {
                ok: true,
                initialized: false
            }
        );
        assert!(!health.gmail.credentials_configured);
        assert!(!health.gmail.connected);
        assert_eq!(
            engine
                .authorize_gmail()
                .expect_err("missing credentials must prevent Gmail authorization")
                .code,
            "gmail_error"
        );
        assert_eq!(health.local_model.endpoint, "http://127.0.0.1:9/v1");
        assert_eq!(health.local_model.model, "local-model");
        assert_eq!(health.watchlist_count, 0);
        assert_eq!(
            engine
                .settings_with_timeout(Duration::from_secs(5))
                .expect("read engine settings"),
            EngineSettings {
                notifications_enabled: true,
                poll_interval_minutes: 120,
                polling_supported: true,
                retention_days: 180,
            }
        );
        assert_eq!(
            engine
                .update_settings(0, 180, true)
                .expect_err("invalid polling cadence must fail")
                .code,
            "invalid_request"
        );
        assert_eq!(
            engine
                .update_settings(45, 365, false)
                .expect("update safe desktop settings"),
            EngineSettings {
                notifications_enabled: false,
                poll_interval_minutes: 45,
                polling_supported: true,
                retention_days: 365,
            }
        );
        assert_eq!(
            engine.settings().expect("read updated desktop settings"),
            EngineSettings {
                notifications_enabled: false,
                poll_interval_minutes: 45,
                polling_supported: true,
                retention_days: 365,
            }
        );

        assert_eq!(
            engine.check().expect("inactive check without Gmail"),
            CheckResult {
                active: false,
                discovered: 0,
                summarized: 0,
                fallback_notified: 0,
                purged: 0,
                stale_cursor_recovered: false,
                pending_notifications: 0,
            }
        );
        assert_eq!(engine.list().expect("list empty watchlist"), vec![]);
        assert_eq!(engine.recent(20).expect("list empty inbox"), vec![]);
        assert_eq!(
            engine
                .requeue_analysis("missing-message".into())
                .expect_err("missing analysis must not be requeued")
                .code,
            "not_found"
        );
        assert_eq!(
            engine
                .pending_notifications(25)
                .expect("list pending notifications"),
            vec![]
        );
        let missing_notification = NotificationIntent {
            analysis_at: Some("2026-08-28T12:00:00+00:00".into()),
            body: "Missing notification".into(),
            kind: "analysis".into(),
            message_id: "missing-message".into(),
            priority: "normal".into(),
            title: "Missing notification".into(),
        };
        assert_eq!(
            engine
                .acknowledge_notification(&missing_notification)
                .expect_err("missing notification must fail")
                .code,
            "not_found"
        );
        let added = engine
            .add(
                "Person <WATCHED@Example.com>".into(),
                Some("Watched".into()),
            )
            .expect("add sender");
        assert_eq!(
            added,
            WatchedSender {
                email: "watched@example.com".into(),
                name: Some("Watched".into()),
            }
        );
        assert_eq!(engine.list().expect("list sender"), vec![added]);
        assert_eq!(
            engine
                .remove("WATCHED@example.com".into())
                .expect("remove sender")
                .email,
            "watched@example.com"
        );
        assert_eq!(engine.list().expect("list final watchlist"), vec![]);

        let missing = engine
            .remove("missing@example.com".into())
            .expect_err("missing sender must fail");
        assert_eq!(missing.code, "not_found");
    }
}
