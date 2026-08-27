use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::ffi::OsString;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use tauri::{AppHandle, Manager};

const PROTOCOL_VERSION: u8 = 1;

fn default_config_path(home_dir: &Path) -> PathBuf {
    home_dir.join(".config/eom-email-watcher/config.toml")
}

#[derive(Clone)]
pub struct Engine {
    program: OsString,
    args: Vec<OsString>,
    config_path: PathBuf,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct WatchedSender {
    pub email: String,
    pub name: Option<String>,
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
        }
    }

    pub fn list(&self) -> Result<Vec<WatchedSender>, EngineError> {
        self.request::<SenderItems>("watchlist.list", json!({}))
            .map(|data| data.items)
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

        let mut child = Command::new(&self.program)
            .args(&self.args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| {
                EngineError::host(
                    "engine_unavailable",
                    "Watcher engine is unavailable; verify uv and the project environment",
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
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
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
    fn watchlist_round_trip_uses_real_engine_contract() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let config_path = directory.path().join("config.toml");
        fs::write(
            &config_path,
            r#"model_base_url = "http://127.0.0.1:1234/v1"
model_name = "local-model"
model_require_auth = false
notifications_enabled = true
"#,
        )
        .expect("write config");
        let engine = real_engine(config_path);

        assert_eq!(engine.list().expect("list empty watchlist"), vec![]);
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
