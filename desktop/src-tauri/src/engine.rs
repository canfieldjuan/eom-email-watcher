use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::fs::{File, TryLockError};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager};
use tauri_plugin_shell::ShellExt;

#[cfg(unix)]
use std::os::unix::process::CommandExt;
#[cfg(windows)]
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
use windows_sys::Win32::{
    Foundation::INVALID_HANDLE_VALUE,
    System::{
        Diagnostics::ToolHelp::{
            CreateToolhelp32Snapshot, TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First, Thread32Next,
        },
        JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
            SetInformationJobObject, TerminateJobObject,
        },
        Threading::{
            CREATE_NO_WINDOW, CREATE_SUSPENDED, OpenThread, ResumeThread, THREAD_SUSPEND_RESUME,
        },
    },
};

const PROTOCOL_VERSION: u8 = 1;

fn default_config_path(home_dir: &Path) -> PathBuf {
    home_dir.join(".config/eom-email-watcher/config.toml")
}

#[derive(Debug)]
struct HostOperationLock {
    file: File,
}

impl HostOperationLock {
    fn acquire(path: &Path) -> Result<Self, EngineError> {
        let mut options = File::options();
        options.read(true).write(true).create(true);
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
        #[cfg(windows)]
        // Match Python filelock's read/write sharing while denying deletion so
        // every contender continues to coordinate through the same pathname.
        std::os::windows::fs::OpenOptionsExt::share_mode(&mut options, 0x0000_0003);
        let file = options.open(path).map_err(|_| {
            EngineError::host(
                "operation_lock_unavailable",
                "Desktop operation locking is unavailable",
            )
        })?;
        match file.try_lock() {
            Ok(()) => Ok(Self { file }),
            Err(TryLockError::WouldBlock) => Err(EngineError::host(
                "operation_busy",
                "Another watcher operation is already running",
            )),
            Err(TryLockError::Error(_)) => Err(EngineError::host(
                "operation_lock_unavailable",
                "Desktop operation locking is unavailable",
            )),
        }
    }
}

impl Drop for HostOperationLock {
    fn drop(&mut self) {
        if let Err(error) = self.file.unlock() {
            eprintln!("desktop operation lock could not be released: {error}");
        }
    }
}

#[cfg(windows)]
struct WindowsJob {
    handle: OwnedHandle,
}

#[cfg(windows)]
impl WindowsJob {
    fn new() -> io::Result<Self> {
        // SAFETY: null security attributes and name request a private job with
        // default security. The returned owned handle is closed on every path.
        let raw_handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if raw_handle.is_null() {
            return Err(io::Error::last_os_error());
        }

        // SAFETY: CreateJobObjectW returned a non-null, newly owned handle.
        let handle = unsafe { OwnedHandle::from_raw_handle(raw_handle) };
        let job = Self { handle };
        job.set_kill_on_close(true)?;
        Ok(job)
    }

    fn set_kill_on_close(&self, enabled: bool) -> io::Result<()> {
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        if enabled {
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        }
        let limits_size = u32::try_from(std::mem::size_of_val(&limits))
            .expect("Windows job limit structure size fits in u32");
        // SAFETY: the handle is a live job object and `limits` remains valid for
        // the duration of this synchronous call.
        if unsafe {
            SetInformationJobObject(
                self.handle.as_raw_handle(),
                JobObjectExtendedLimitInformation,
                std::ptr::from_ref(&limits).cast(),
                limits_size,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    fn assign(&self, child: &Child) -> io::Result<()> {
        // SAFETY: both handles remain live for the duration of this call. A
        // successful assignment causes future descendants to inherit the job.
        if unsafe { AssignProcessToJobObject(self.handle.as_raw_handle(), child.as_raw_handle()) }
            == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    fn terminate(&self) {
        // SAFETY: the owned handle remains live. Closing it later is a second
        // fail-safe because the job was configured with KILL_ON_JOB_CLOSE.
        let _ = unsafe { TerminateJobObject(self.handle.as_raw_handle(), 1) };
    }

    fn release_descendants(&self) -> io::Result<()> {
        // The request completed normally. Clear KILL_ON_JOB_CLOSE before the
        // last job handle closes so user-facing descendants such as the OAuth
        // browser remain open.
        self.set_kill_on_close(false)
    }
}

#[cfg(windows)]
fn resume_suspended_process(process_id: u32) -> io::Result<()> {
    // SAFETY: this creates an owned system snapshot handle; the process id is
    // the child returned by Command::spawn and is used only for matching.
    let raw_snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if raw_snapshot == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: CreateToolhelp32Snapshot returned a valid, newly owned handle.
    let snapshot = unsafe { OwnedHandle::from_raw_handle(raw_snapshot) };
    let mut entry = THREADENTRY32 {
        dwSize: u32::try_from(std::mem::size_of::<THREADENTRY32>())
            .expect("Windows thread entry size fits in u32"),
        ..THREADENTRY32::default()
    };
    // SAFETY: `entry` has the required size and remains live for enumeration.
    if unsafe { Thread32First(snapshot.as_raw_handle(), &mut entry) } == 0 {
        return Err(io::Error::last_os_error());
    }

    loop {
        if entry.th32OwnerProcessID == process_id {
            // SAFETY: the enumerated thread belongs to our suspended child and
            // the returned owned handle is closed on every path.
            let raw_thread = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) };
            if raw_thread.is_null() {
                return Err(io::Error::last_os_error());
            }
            // SAFETY: OpenThread returned a non-null, newly owned handle.
            let thread = unsafe { OwnedHandle::from_raw_handle(raw_thread) };
            // SAFETY: this is the primary thread of the child created with
            // CREATE_SUSPENDED; u32::MAX is the documented failure sentinel.
            if unsafe { ResumeThread(thread.as_raw_handle()) } == u32::MAX {
                return Err(io::Error::last_os_error());
            }
            return Ok(());
        }
        // SAFETY: `entry` and the snapshot remain valid for enumeration.
        if unsafe { Thread32Next(snapshot.as_raw_handle(), &mut entry) } == 0 {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "suspended engine primary thread was unavailable",
            ));
        }
    }
}

struct EngineChild {
    process: Child,
    #[cfg(windows)]
    job: WindowsJob,
}

impl EngineChild {
    fn spawn(command: &mut Command) -> io::Result<Self> {
        #[cfg(windows)]
        let job = WindowsJob::new()?;
        #[cfg(windows)]
        // CREATE_SUSPENDED closes the spawn-before-assignment race: the engine
        // cannot create descendants until it belongs to the terminating job.
        // CREATE_NO_WINDOW preserves the sidecar's piped JSON protocol without
        // flashing a console window.
        command.creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED);
        let process = command.spawn()?;
        #[cfg(windows)]
        let process = match job
            .assign(&process)
            .and_then(|()| resume_suspended_process(process.id()))
        {
            Ok(()) => process,
            Err(error) => {
                let mut process = process;
                job.terminate();
                let _ = process.kill();
                let _ = process.wait();
                return Err(error);
            }
        };
        Ok(Self {
            process,
            #[cfg(windows)]
            job,
        })
    }

    fn terminate(&mut self) {
        #[cfg(unix)]
        if let Ok(group_id) = i32::try_from(self.process.id()) {
            // The child starts a dedicated process group, so this also terminates
            // uv-launched Python descendants that would otherwise retain locks.
            // SAFETY: the negative id targets only the process group created for
            // this child; it is not derived from frontend or engine input.
            unsafe {
                libc::kill(-group_id, libc::SIGKILL);
            }
        }
        #[cfg(windows)]
        self.job.terminate();
        let _ = self.process.kill();
        let _ = self.process.wait();
    }

    fn wait_with_output(self) -> io::Result<Output> {
        #[cfg(windows)]
        {
            let Self { process, job } = self;
            let output = process.wait_with_output()?;
            if output.status.success() {
                job.release_descendants()?;
            } else {
                job.terminate();
            }
            Ok(output)
        }
        #[cfg(not(windows))]
        {
            self.process.wait_with_output()
        }
    }
}

#[derive(Clone)]
pub struct Engine {
    program: OsString,
    args: Vec<OsString>,
    config_path: PathBuf,
    mailbox_operation_gate: Arc<Mutex<()>>,
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
    #[serde(default)]
    pub job_id: Option<String>,
    pub capability_id: String,
    pub capability_version: String,
    #[serde(default)]
    pub protocol_version: Option<u32>,
    #[serde(default)]
    pub provider: Option<ConnectProviderIdentity>,
    #[serde(default)]
    pub parameters: BTreeMap<String, Value>,
    pub status: String,
    #[serde(default)]
    pub dispatch_state: Option<String>,
    #[serde(default)]
    pub queue_ahead: Option<u64>,
    #[serde(default)]
    pub next_attempt_at: Option<String>,
    #[serde(default)]
    pub dispatch_error: Option<EngineError>,
    pub updated_at: String,
    pub summary: Option<ConnectSummary>,
    #[serde(default)]
    pub outputs: Vec<ConnectOutputMetadata>,
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
pub struct ConnectProviderIdentity {
    pub app_id: String,
    pub version: String,
    pub instance_id: String,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectProvider {
    pub app_id: String,
    pub name: String,
    pub version: String,
    pub instance_id: String,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectCapabilityRef {
    pub id: String,
    pub version: String,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectAction {
    pub label: String,
    pub description: String,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectAcceptedArtifact {
    pub media_type: String,
    pub max_bytes: u64,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectParameter {
    pub name: String,
    pub value_type: String,
    pub required: bool,
    pub label: String,
    pub description: String,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectEffects {
    pub external: bool,
    pub confirmation_required: bool,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectCapabilityDeclaration {
    pub id: String,
    pub version: String,
    pub action: ConnectAction,
    pub accepts: Vec<ConnectAcceptedArtifact>,
    pub produces: Vec<String>,
    pub parameters: Vec<ConnectParameter>,
    pub effects: ConnectEffects,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectCapability {
    pub protocol_version: u32,
    pub provider: ConnectProvider,
    pub capability: ConnectCapabilityDeclaration,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectOutputMetadata {
    pub artifact_id: String,
    pub media_type: String,
    pub display_name: String,
    pub byte_size: u64,
    pub sha256: String,
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
#[serde(rename_all = "snake_case")]
pub enum ConnectEntitlementState {
    Active,
    AuthorityUnavailable,
    Missing,
    Invalid,
    NotYetValid,
    Expired,
    FeatureMissing,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectEntitlementStatus {
    pub state: ConnectEntitlementState,
    pub active: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CalendarConsentProfile {
    Read,
    Proposal,
    Write,
}

impl CalendarConsentProfile {
    fn as_str(self) -> &'static str {
        match self {
            Self::Read => "read",
            Self::Proposal => "proposal",
            Self::Write => "write",
        }
    }

    fn expected_scope(self) -> &'static str {
        match self {
            Self::Read => "Calendars.Read",
            Self::Proposal => "Calendars.Read.Shared",
            Self::Write => "Calendars.ReadWrite",
        }
    }
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CalendarConsentState {
    NotRequested,
    ConsentPending,
    Ready,
    Rejected,
    Revoked,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct CalendarConsentStatus {
    pub account_id: String,
    pub available: bool,
    pub entitlement_active: bool,
    pub profile: CalendarConsentProfile,
    pub scope: String,
    pub state: CalendarConsentState,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectInvocationResult {
    pub protocol_version: u32,
    pub job_id: String,
    pub provider: ConnectProviderIdentity,
    pub capability: ConnectCapabilityRef,
    pub status: String,
    pub outputs: Vec<ConnectOutputMetadata>,
    #[serde(default)]
    pub dispatch_state: Option<String>,
    #[serde(default)]
    pub queue_ahead: Option<u64>,
    #[serde(default)]
    pub next_attempt_at: Option<String>,
    #[serde(default)]
    pub dispatch_error: Option<EngineError>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectQueueItem {
    pub job_id: String,
    pub job_status: String,
    pub dispatch_state: String,
    pub outcome: String,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectQueuePump {
    pub items: Vec<ConnectQueueItem>,
    pub next_wake_unix_ms: Option<u64>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConnectOutputView {
    pub job_id: String,
    pub output: ConnectOutputMetadata,
    pub presentation: ConnectOutputPresentation,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ConnectOutputPresentation {
    DocumentSummary { summary: ConnectSummary },
    Text { text: String },
    Opaque,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
pub struct ExportedCapabilityOutput {
    pub job_id: String,
    pub output: ConnectOutputMetadata,
    pub path: PathBuf,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
pub struct ExportedAttachment {
    pub filename: String,
    pub path: PathBuf,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
pub struct CalendarProposalPreview {
    pub run_id: String,
    pub state: String,
    pub state_version: i64,
    pub proposal_version: i64,
    pub proposal_sha256: String,
    pub status: String,
    pub provider: String,
    pub account_id: String,
    pub account_display_name: String,
    pub account_address: Option<String>,
    pub subject: String,
    pub attendees: Vec<String>,
    pub start: Option<String>,
    pub end: Option<String>,
    pub timezone: Option<String>,
    pub suggestion_reason: Option<String>,
    pub empty_reason: Option<String>,
    pub observed_at: String,
    pub expires_at: Option<String>,
    pub write_status: Option<String>,
    pub graph_event_id: Option<String>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct CalendarDecisionResult {
    pub run_id: String,
    pub state: String,
    pub state_version: i64,
    pub failure_code: Option<String>,
    pub graph_event_id: Option<String>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
pub struct InboxItem {
    pub message_id: String,
    #[serde(default = "default_mail_provider")]
    pub provider: String,
    #[serde(default = "default_mail_account_id")]
    pub account_id: String,
    pub received_at: String,
    pub sender: String,
    pub sender_name: Option<String>,
    pub subject: String,
    pub status: String,
    pub analysis_at: Option<String>,
    #[serde(default)]
    pub category: Option<String>,
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
    #[serde(default)]
    pub calendar_proposal: Option<CalendarProposalPreview>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct InboxQuery {
    pub limit: u16,
    pub cursor: Option<String>,
    pub provider: Option<String>,
    pub account_id: Option<String>,
    pub sender_query: Option<String>,
    pub priority: Option<String>,
    pub category: Option<String>,
    pub status: Option<String>,
    pub keyword: Option<String>,
}

fn default_mail_provider() -> String {
    "gmail".into()
}

fn default_mail_account_id() -> String {
    "gmail-default".into()
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
pub struct InboxPage {
    pub items: Vec<InboxItem>,
    pub next_cursor: Option<String>,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
struct InboxDeletion {
    deleted: bool,
    message_id: String,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
struct InboxClear {
    deleted: u64,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct HealthStatus {
    pub database: DatabaseHealth,
    pub gmail: GmailHealth,
    pub last_check: Option<String>,
    pub local_model: LocalModelHealth,
    pub mail: MailAccounts,
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
pub struct MailProviderStatus {
    pub provider: String,
    pub display_name: String,
    pub connection_available: bool,
    #[serde(default = "browser_oauth_connection_method")]
    pub connection_method: String,
    #[serde(default)]
    pub multiple_accounts: bool,
}

fn browser_oauth_connection_method() -> String {
    "browser_oauth".to_owned()
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct MailAccountStatus {
    pub provider: String,
    pub account_id: String,
    pub display_name: String,
    pub address: Option<String>,
    pub connected: bool,
    pub active: bool,
    pub last_check: Option<String>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct MailAccounts {
    pub providers: Vec<MailProviderStatus>,
    pub accounts: Vec<MailAccountStatus>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum MailServerSecurity {
    Tls,
    Starttls,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct MailServerConnection {
    pub email_address: String,
    pub host: String,
    pub port: u16,
    pub security: MailServerSecurity,
    pub username: String,
    pub password: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ca_file: Option<String>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct MailAccountResult {
    pub account: MailAccountStatus,
    #[serde(default)]
    pub baseline_initialized: Option<bool>,
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
    #[serde(default)]
    pub automation_processed: u64,
    #[serde(default)]
    pub automation_review_required: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct NotificationIntent {
    pub analysis_at: Option<String>,
    pub body: String,
    pub kind: String,
    pub message_id: String,
    pub priority: String,
    #[serde(default)]
    pub revision: Option<String>,
    #[serde(default)]
    pub subject_id: Option<String>,
    #[serde(default)]
    pub subject_type: Option<String>,
    pub title: String,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct LocalModelSettings {
    #[serde(default)]
    pub editable: bool,
    pub endpoint: String,
    pub model: String,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct EngineSettings {
    pub local_model: LocalModelSettings,
    pub notifications_enabled: bool,
    pub poll_interval_minutes: u64,
    pub polling_supported: bool,
    pub retention_days: u64,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConfigInitialization {
    pub created: bool,
    pub settings: EngineSettings,
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
struct NotificationItems {
    items: Vec<NotificationIntent>,
}

#[derive(Deserialize)]
struct NotificationCount {
    count: u64,
}

#[derive(Deserialize)]
struct NotificationAcknowledgement {
    status: String,
}

#[derive(Deserialize)]
struct OperationLockLocation {
    path: PathBuf,
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
                mailbox_operation_gate: Arc::new(Mutex::new(())),
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
                mailbox_operation_gate: Arc::new(Mutex::new(())),
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
            mailbox_operation_gate: Arc::new(Mutex::new(())),
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
            mailbox_operation_gate: Arc::new(Mutex::new(())),
            request_timeout: None,
        }
    }

    pub fn list(&self) -> Result<Vec<WatchedSender>, EngineError> {
        self.request::<SenderItems>("watchlist.list", json!({}))
            .map(|data| data.items)
    }

    pub fn query_inbox(&self, query: InboxQuery) -> Result<InboxPage, EngineError> {
        self.request(
            "inbox.query",
            json!({
                "limit": query.limit,
                "cursor": query.cursor,
                "provider": query.provider,
                "account_id": query.account_id,
                "sender_query": query.sender_query,
                "priority": query.priority,
                "category": query.category,
                "status": query.status,
                "keyword": query.keyword,
            }),
        )
    }

    pub fn delete_inbox_item(&self, message_id: String) -> Result<(), EngineError> {
        let response = self
            .request::<InboxDeletion>("inbox.delete", json!({"message_id": message_id.clone()}))?;
        if response.deleted && response.message_id == message_id {
            return Ok(());
        }
        Err(EngineError::host(
            "engine_protocol_error",
            "Watcher engine returned an invalid inbox deletion result",
        ))
    }

    pub fn clear_inbox(&self) -> Result<u64, EngineError> {
        self.request::<InboxClear>("inbox.clear", json!({}))
            .map(|response| response.deleted)
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

    pub fn attachment_capabilities(
        &self,
        message_id: String,
        part_id: String,
    ) -> Result<ConnectCapabilities, EngineError> {
        self.request(
            "connect.attachment.capabilities",
            json!({"message_id": message_id, "part_id": part_id}),
        )
    }

    // These fields mirror the explicit, versioned engine request instead of hiding
    // provider or effect-confirmation identity in a loosely typed object.
    #[allow(clippy::too_many_arguments)]
    pub fn invoke_attachment_capability(
        &self,
        request_id: String,
        message_id: String,
        part_id: String,
        provider: ConnectProviderIdentity,
        capability: ConnectCapabilityRef,
        parameters: BTreeMap<String, Value>,
        confirmed: bool,
    ) -> Result<ConnectInvocationResult, EngineError> {
        self.request(
            "connect.attachment.invoke",
            json!({
                "request_id": request_id,
                "message_id": message_id,
                "part_id": part_id,
                "provider": provider,
                "capability": capability,
                "parameters": parameters,
                "confirmed": confirmed,
            }),
        )
    }

    pub fn pump_connect_queue(&self) -> Result<ConnectQueuePump, EngineError> {
        self.request("connect.queue.pump", json!({"limit": 25}))
    }

    pub fn present_capability_output(
        &self,
        message_id: String,
        part_id: String,
        job_id: String,
        artifact_id: String,
    ) -> Result<ConnectOutputView, EngineError> {
        self.request(
            "connect.output.present",
            json!({
                "message_id": message_id,
                "part_id": part_id,
                "job_id": job_id,
                "artifact_id": artifact_id,
            }),
        )
    }

    pub fn export_capability_output(
        &self,
        message_id: String,
        part_id: String,
        job_id: String,
        artifact_id: String,
        destination_dir: PathBuf,
    ) -> Result<ExportedCapabilityOutput, EngineError> {
        self.request(
            "connect.output.export",
            json!({
                "message_id": message_id,
                "part_id": part_id,
                "job_id": job_id,
                "artifact_id": artifact_id,
                "destination_dir": destination_dir,
            }),
        )
    }

    pub fn health(&self) -> Result<HealthStatus, EngineError> {
        self.request("health.get", json!({}))
    }

    pub fn connect_entitlement_status(&self) -> Result<ConnectEntitlementStatus, EngineError> {
        self.request("connect.entitlement.status", json!({}))
    }

    pub fn install_connect_entitlement(
        &self,
        source_path: PathBuf,
    ) -> Result<ConnectEntitlementStatus, EngineError> {
        self.request(
            "connect.entitlement.install",
            json!({"source_path": source_path}),
        )
    }

    pub fn authorize_gmail(&self) -> Result<GmailAuthorization, EngineError> {
        let _guard = self
            .mailbox_operation_gate
            .lock()
            .map_err(|_| EngineError::host("host_error", "Email account coordinator stopped"))?;
        self.request("gmail.authorize", json!({}))
    }

    pub fn mail_accounts(&self) -> Result<MailAccounts, EngineError> {
        self.request("mail.accounts.list", json!({}))
    }

    fn calendar_consent_request(
        &self,
        action: &str,
        profile: CalendarConsentProfile,
        provider: String,
        account_id: String,
    ) -> Result<CalendarConsentStatus, EngineError> {
        let operation = format!("calendar.{}.{}", profile.as_str(), action);
        let status: CalendarConsentStatus = self.request(
            &operation,
            json!({"provider": provider, "account_id": account_id}),
        )?;
        if status.account_id != account_id
            || status.profile != profile
            || status.scope != profile.expected_scope()
        {
            return Err(EngineError::host(
                "engine_protocol_error",
                "Watcher engine returned a mismatched calendar consent status",
            ));
        }
        Ok(status)
    }

    pub fn calendar_consent_status(
        &self,
        profile: CalendarConsentProfile,
        provider: String,
        account_id: String,
    ) -> Result<CalendarConsentStatus, EngineError> {
        self.calendar_consent_request("status", profile, provider, account_id)
    }

    pub fn connect_calendar_consent(
        &self,
        profile: CalendarConsentProfile,
        provider: String,
        account_id: String,
    ) -> Result<CalendarConsentStatus, EngineError> {
        let _guard = self
            .mailbox_operation_gate
            .lock()
            .map_err(|_| EngineError::host("host_error", "Email account coordinator stopped"))?;
        self.calendar_consent_request("connect", profile, provider, account_id)
    }

    pub fn disconnect_calendar_consent(
        &self,
        profile: CalendarConsentProfile,
        provider: String,
        account_id: String,
    ) -> Result<CalendarConsentStatus, EngineError> {
        let _guard = self
            .mailbox_operation_gate
            .lock()
            .map_err(|_| EngineError::host("host_error", "Email account coordinator stopped"))?;
        self.calendar_consent_request("disconnect", profile, provider, account_id)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn decide_calendar_proposal(
        &self,
        message_id: String,
        run_id: String,
        state_version: i64,
        proposal_version: i64,
        proposal_sha256: String,
        decision: String,
    ) -> Result<CalendarDecisionResult, EngineError> {
        let _guard = self
            .mailbox_operation_gate
            .lock()
            .map_err(|_| EngineError::host("host_error", "Email account coordinator stopped"))?;
        self.request(
            "calendar.automation.decide",
            json!({
                "decision": decision,
                "message_id": message_id,
                "proposal_sha256": proposal_sha256,
                "proposal_version": proposal_version,
                "run_id": run_id,
                "state_version": state_version,
            }),
        )
    }

    pub fn connect_mail_provider(
        &self,
        provider: String,
        connection: Option<MailServerConnection>,
    ) -> Result<MailAccountResult, EngineError> {
        let _guard = self
            .mailbox_operation_gate
            .lock()
            .map_err(|_| EngineError::host("host_error", "Email account coordinator stopped"))?;
        let payload = match connection {
            Some(connection) => json!({"provider": provider, "connection": connection}),
            None => json!({"provider": provider}),
        };
        self.request("mail.accounts.connect", payload)
    }

    pub fn reconnect_mail_account(
        &self,
        provider: String,
        account_id: String,
        connection: Option<MailServerConnection>,
    ) -> Result<MailAccountResult, EngineError> {
        let _guard = self
            .mailbox_operation_gate
            .lock()
            .map_err(|_| EngineError::host("host_error", "Email account coordinator stopped"))?;
        let payload = match connection {
            Some(connection) => json!({
                "provider": provider,
                "account_id": account_id,
                "connection": connection,
            }),
            None => json!({"provider": provider, "account_id": account_id}),
        };
        self.request("mail.accounts.reconnect", payload)
    }

    pub fn disconnect_mail_account(
        &self,
        provider: String,
        account_id: String,
    ) -> Result<MailAccountResult, EngineError> {
        let _guard = self
            .mailbox_operation_gate
            .lock()
            .map_err(|_| EngineError::host("host_error", "Email account coordinator stopped"))?;
        self.request(
            "mail.accounts.disconnect",
            json!({"provider": provider, "account_id": account_id}),
        )
    }

    pub fn activate_mail_account(
        &self,
        provider: String,
        account_id: String,
    ) -> Result<MailAccountResult, EngineError> {
        let _guard = self
            .mailbox_operation_gate
            .lock()
            .map_err(|_| EngineError::host("host_error", "Email account coordinator stopped"))?;
        self.request(
            "mail.accounts.activate",
            json!({"provider": provider, "account_id": account_id}),
        )
    }

    pub fn settings_with_timeout(&self, timeout: Duration) -> Result<EngineSettings, EngineError> {
        self.request_with_timeout("settings.get", json!({}), timeout)
    }

    pub fn settings(&self) -> Result<EngineSettings, EngineError> {
        self.request("settings.get", json!({}))
    }

    pub fn config_present(&self) -> Result<bool, EngineError> {
        match std::fs::symlink_metadata(&self.config_path) {
            Ok(_) => Ok(true),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(_) => Err(EngineError::host(
                "host_error",
                "Desktop could not inspect watcher configuration",
            )),
        }
    }

    pub fn initialize_config(
        &self,
        timezone: String,
        model_base_url: String,
        model_name: String,
    ) -> Result<ConfigInitialization, EngineError> {
        self.request(
            "config.initialize",
            json!({
                "model_base_url": model_base_url,
                "model_name": model_name,
                "timezone": timezone,
            }),
        )
    }

    pub fn update_settings(
        &self,
        poll_interval_minutes: u64,
        retention_days: u64,
        notifications_enabled: bool,
        model_base_url: Option<String>,
        model_name: Option<String>,
    ) -> Result<EngineSettings, EngineError> {
        let mut payload = json!({
            "notifications_enabled": notifications_enabled,
            "poll_interval_minutes": poll_interval_minutes,
            "retention_days": retention_days,
        });
        if let Some(value) = model_base_url {
            payload["model_base_url"] = Value::String(value);
        }
        if let Some(value) = model_name {
            payload["model_name"] = Value::String(value);
        }
        self.request("settings.update", payload)
    }

    pub fn with_request_timeout(&self, timeout: Duration) -> Self {
        let mut engine = self.clone();
        engine.request_timeout = Some(timeout);
        engine
    }

    pub fn check(&self) -> Result<CheckResult, EngineError> {
        let _guard = self
            .mailbox_operation_gate
            .lock()
            .map_err(|_| EngineError::host("host_error", "Email account coordinator stopped"))?;
        self.request("watcher.check", json!({"dry_run": false}))
    }

    pub(crate) fn run_with_operation_lock<T>(
        &self,
        operation: impl FnOnce() -> Result<T, EngineError>,
    ) -> Result<T, EngineError> {
        let location = self.request::<OperationLockLocation>("host.operation_lock", json!({}))?;
        let _lock = HostOperationLock::acquire(&location.path)?;
        operation()
    }

    #[cfg(test)]
    pub fn pending_notifications(
        &self,
        limit: u16,
    ) -> Result<Vec<NotificationIntent>, EngineError> {
        self.request::<NotificationItems>("notifications.pending", json!({"limit": limit}))
            .map(|data| data.items)
    }

    pub(crate) fn pending_notifications_under_host_lock(
        &self,
        limit: u16,
    ) -> Result<Vec<NotificationIntent>, EngineError> {
        self.request::<NotificationItems>(
            "notifications.pending_under_host_lock",
            json!({"limit": limit}),
        )
        .map(|data| data.items)
    }

    pub(crate) fn pending_notification_count_under_host_lock(&self) -> Result<u64, EngineError> {
        self.request::<NotificationCount>("notifications.count_under_host_lock", json!({}))
            .map(|data| data.count)
    }

    pub fn acknowledge_notification(&self, intent: &NotificationIntent) -> Result<(), EngineError> {
        let acknowledgement = self.request::<NotificationAcknowledgement>(
            "notifications.ack",
            json!({
                "analysis_at": &intent.analysis_at,
                "kind": &intent.kind,
                "message_id": &intent.message_id,
                "revision": &intent.revision,
                "subject_id": &intent.subject_id,
                "subject_type": &intent.subject_type,
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
        let mut child = EngineChild::spawn(&mut command).map_err(|_| {
            EngineError::host(
                "engine_unavailable",
                "Watcher engine is unavailable; reinstall it or inspect desktop logs",
            )
        })?;

        let write_result = child
            .process
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
            child.terminate();
            return Err(error);
        }

        if let Some(timeout) = timeout {
            let started = Instant::now();
            loop {
                match child.process.try_wait() {
                    Ok(Some(_)) => break,
                    Ok(None) if started.elapsed() < timeout => {
                        std::thread::sleep(Duration::from_millis(10));
                    }
                    Ok(None) => {
                        child.terminate();
                        return Err(EngineError::host(
                            "engine_timeout",
                            "Watcher engine did not respond before its timeout",
                        ));
                    }
                    Err(_) => {
                        child.terminate();
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
    #[cfg(windows)]
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    #[cfg(windows)]
    use windows_sys::Win32::{
        Foundation::WAIT_TIMEOUT,
        System::Threading::{
            OpenProcess, PROCESS_SYNCHRONIZE, PROCESS_TERMINATE, TerminateProcess,
            WaitForSingleObject,
        },
    };

    #[cfg(windows)]
    // This bounds a hung probe, not product latency. Hosted Windows runners
    // can spend more than ten seconds starting the nested PowerShell process.
    const WINDOWS_PROCESS_PROBE_TIMEOUT: Duration = Duration::from_secs(30);

    #[cfg(windows)]
    struct WindowsTestProcess(u32);

    #[cfg(windows)]
    impl Drop for WindowsTestProcess {
        fn drop(&mut self) {
            // SAFETY: the PID came from the test child. If it is still live,
            // terminate only that disposable probe and wait for handle signal.
            let raw_handle =
                unsafe { OpenProcess(PROCESS_SYNCHRONIZE | PROCESS_TERMINATE, 0, self.0) };
            if raw_handle.is_null() {
                return;
            }
            // SAFETY: OpenProcess returned a non-null, newly owned handle.
            let handle = unsafe { OwnedHandle::from_raw_handle(raw_handle) };
            // SAFETY: the handle grants PROCESS_TERMINATE for this test probe.
            let _ = unsafe { TerminateProcess(handle.as_raw_handle(), 1) };
            // SAFETY: the process handle remains live for this bounded wait.
            let _ = unsafe { WaitForSingleObject(handle.as_raw_handle(), 5_000) };
        }
    }

    #[test]
    fn default_config_matches_python_watcher_location() {
        assert_eq!(
            default_config_path(Path::new("/home/watcher")),
            PathBuf::from("/home/watcher/.config/eom-email-watcher/config.toml")
        );
    }

    #[test]
    fn host_operation_lock_is_exclusive_and_reusable() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let path = directory.path().join("watcher.check.lock");
        let active = HostOperationLock::acquire(&path).expect("first lock acquisition");

        let blocked = HostOperationLock::acquire(&path).expect_err("second lock must contend");
        assert_eq!(blocked.code, "operation_busy");

        drop(active);
        HostOperationLock::acquire(&path).expect("released lock is reusable");
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
    fn config_presence_distinguishes_missing_from_existing_paths() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let config_path = directory.path().join("config.toml");
        let engine = Engine::with_command("unused", Vec::new(), config_path.clone());

        assert!(!engine.config_present().expect("inspect missing config"));
        fs::write(&config_path, "invalid but present").expect("write config marker");
        assert!(engine.config_present().expect("inspect present config"));
    }

    #[cfg(unix)]
    #[test]
    fn broken_config_symlink_is_present_and_never_treated_as_first_run() {
        use std::os::unix::fs::symlink;

        let directory = tempfile::tempdir().expect("temporary directory");
        let config_path = directory.path().join("config.toml");
        symlink(directory.path().join("missing-target"), &config_path)
            .expect("create broken config symlink");
        let engine = Engine::with_command("unused", Vec::new(), config_path);

        assert!(engine.config_present().expect("inspect broken symlink"));
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
        assert_eq!(item.calendar_proposal, None);
        assert_eq!(item.category, None);
        assert_eq!(item.provider, "gmail");
        assert_eq!(item.account_id, "gmail-default");
    }

    #[test]
    fn inbox_calendar_proposal_contract_is_typed_without_private_identity() {
        let item: InboxItem = serde_json::from_str(r#"{
            "message_id": "message-1",
            "provider": "microsoft365",
            "account_id": "microsoft365-account",
            "received_at": "2026-09-07T12:00:00+00:00",
            "sender": "sender@example.com",
            "sender_name": "Sender",
            "subject": "Meeting request",
            "status": "analyzed",
            "analysis_at": "2026-09-07T12:01:00+00:00",
            "category": "scheduling",
            "priority": "normal",
            "summary": "A meeting was requested.",
            "action_required": 1,
            "suggested_action": "Review the meeting proposal.",
            "deadline_text": null,
            "deadline_iso": null,
            "confidence": 0.9,
            "attempts": 0,
            "next_retry_at": null,
            "analysis_retryable": null,
            "analysis_error_code": null,
            "analysis_retry_after_seconds": null,
            "fallback_notified_at": null,
            "notified_at": null,
            "last_error": null,
            "attachments": [],
            "calendar_proposal": {
                "run_id": "run-1",
                "state": "awaiting_confirmation",
                "state_version": 4,
                "proposal_version": 1,
                "proposal_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "status": "accepted",
                "provider": "microsoft365",
                "account_id": "microsoft365-account",
                "account_display_name": "Microsoft 365",
                "account_address": "owner@example.com",
                "subject": "Meeting request",
                "attendees": ["jane@example.com"],
                "start": "2026-09-08T10:00:00-05:00",
                "end": "2026-09-08T10:30:00-05:00",
                "timezone": "America/Chicago",
                "suggestion_reason": "All attendees are available.",
                "empty_reason": null,
                "observed_at": "2026-09-07T13:00:00+00:00",
                "expires_at": "2026-09-07T13:15:00+00:00"
            }
        }"#)
        .expect("calendar proposal preview must cross the typed desktop boundary");

        let proposal = item.calendar_proposal.expect("calendar proposal");
        assert_eq!(proposal.state, "awaiting_confirmation");
        assert_eq!(proposal.status, "accepted");
        assert_eq!(proposal.attendees, vec!["jane@example.com"]);
        assert_eq!(proposal.timezone.as_deref(), Some("America/Chicago"));
        assert_eq!(
            proposal.account_address.as_deref(),
            Some("owner@example.com")
        );
    }

    #[test]
    fn inbox_calendar_no_suggestions_contract_is_typed() {
        let proposal: CalendarProposalPreview = serde_json::from_str(
            r#"{
            "run_id": "run-1",
            "state": "manual_review",
            "state_version": 5,
            "proposal_version": 1,
            "proposal_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "status": "no_suggestions",
            "provider": "microsoft365",
            "account_id": "microsoft365-account",
            "account_display_name": "Microsoft 365",
            "account_address": "owner@example.com",
            "subject": "Meeting request",
            "attendees": ["jane@example.com"],
            "start": null,
            "end": null,
            "timezone": null,
            "suggestion_reason": null,
            "empty_reason": "No common time.",
            "observed_at": "2026-09-07T13:00:00+00:00",
            "expires_at": null
        }"#,
        )
        .expect("no-suggestions review must cross the typed desktop boundary");

        assert_eq!(proposal.state, "manual_review");
        assert_eq!(proposal.empty_reason.as_deref(), Some("No common time."));
        assert_eq!(proposal.start, None);
    }

    #[test]
    fn inbox_query_and_page_contract_are_typed() {
        let query = InboxQuery {
            limit: 25,
            cursor: Some("opaque-cursor".into()),
            provider: Some("gmail".into()),
            account_id: Some("gmail-default".into()),
            sender_query: Some("billing".into()),
            priority: Some("high".into()),
            category: Some("invoice".into()),
            status: Some("analyzed".into()),
            keyword: Some("overdue".into()),
        };
        assert_eq!(
            serde_json::to_value(query).expect("serialize inbox query"),
            json!({
                "limit": 25,
                "cursor": "opaque-cursor",
                "provider": "gmail",
                "account_id": "gmail-default",
                "sender_query": "billing",
                "priority": "high",
                "category": "invoice",
                "status": "analyzed",
                "keyword": "overdue"
            })
        );

        let page: InboxPage = serde_json::from_value(json!({
            "items": [],
            "next_cursor": "next-page"
        }))
        .expect("deserialize inbox page");
        assert_eq!(page.items, vec![]);
        assert_eq!(page.next_cursor.as_deref(), Some("next-page"));

        let deletion: InboxDeletion = serde_json::from_value(json!({
            "deleted": true,
            "message_id": "message-1"
        }))
        .expect("deserialize inbox deletion");
        assert!(deletion.deleted);
        assert_eq!(deletion.message_id, "message-1");

        let cleared: InboxClear =
            serde_json::from_value(json!({"deleted": 3})).expect("deserialize inbox clear result");
        assert_eq!(cleared.deleted, 3);
    }

    #[test]
    fn protocol_v2_capabilities_and_durable_results_are_typed() {
        let capabilities: ConnectCapabilities = serde_json::from_value(json!({
            "items": [{
                "protocol_version": 2,
                "provider": {
                    "app_id": "document-summarizer",
                    "name": "Document Summarizer",
                    "version": "0.1.0",
                    "instance_id": "11111111-1111-4111-8111-111111111111"
                },
                "capability": {
                    "id": "document.summarize",
                    "version": "1.0",
                    "action": {
                        "label": "Summarize",
                        "description": "Create a local summary."
                    },
                    "accepts": [{"media_type": "application/pdf", "max_bytes": 1024}],
                    "produces": ["application/vnd.local-connect.document-summary+json"],
                    "parameters": [],
                    "effects": {"external": false, "confirmation_required": false}
                }
            }],
            "diagnostic": null
        }))
        .expect("deserialize generic capability catalog");
        assert_eq!(capabilities.items[0].protocol_version, 2);
        assert_eq!(capabilities.items[0].provider.name, "Document Summarizer");

        let result: AttachmentCapabilityResult = serde_json::from_value(json!({
            "job_id": "22222222-2222-4222-8222-222222222222",
            "protocol_version": 2,
            "capability_id": "document.summarize",
            "capability_version": "1.0",
            "provider": {
                "app_id": "document-summarizer",
                "version": "0.1.0",
                "instance_id": "11111111-1111-4111-8111-111111111111"
            },
            "parameters": {},
            "status": "completed",
            "updated_at": "2026-08-30T12:00:00+00:00",
            "outputs": [{
                "artifact_id": "33333333-3333-4333-8333-333333333333",
                "media_type": "application/vnd.local-connect.document-summary+json",
                "display_name": "summary.json",
                "byte_size": 25,
                "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            }]
        }))
        .expect("deserialize durable generic result");
        assert_eq!(
            result.job_id.as_deref(),
            Some("22222222-2222-4222-8222-222222222222")
        );
        assert_eq!(result.outputs[0].display_name, "summary.json");

        let active: ConnectInvocationResult = serde_json::from_value(json!({
            "protocol_version": 2,
            "job_id": "22222222-2222-4222-8222-222222222222",
            "provider": {
                "app_id": "document-summarizer",
                "version": "0.1.0",
                "instance_id": "11111111-1111-4111-8111-111111111111"
            },
            "capability": {"id": "document.summarize", "version": "1.0"},
            "status": "requested",
            "outputs": [],
            "dispatch_state": "waiting",
            "queue_ahead": 1,
            "next_attempt_at": "2026-09-09T12:00:02+00:00",
            "dispatch_error": {
                "code": "PROVIDER_BUSY",
                "message": "Another job is running."
            }
        }))
        .expect("deserialize active queue result");
        assert_eq!(active.dispatch_state.as_deref(), Some("waiting"));
        assert_eq!(active.queue_ahead, Some(1));

        let pump: ConnectQueuePump = serde_json::from_value(json!({
            "items": [{
                "job_id": "22222222-2222-4222-8222-222222222222",
                "job_status": "requested",
                "dispatch_state": "waiting",
                "outcome": "deferred"
            }],
            "next_wake_unix_ms": 1_788_955_202_000_u64
        }))
        .expect("deserialize queue pump result");
        assert_eq!(pump.items[0].outcome, "deferred");
        assert_eq!(pump.next_wake_unix_ms, Some(1_788_955_202_000));

        let view: ConnectOutputView = serde_json::from_value(json!({
            "job_id": "22222222-2222-4222-8222-222222222222",
            "output": {
                "artifact_id": "33333333-3333-4333-8333-333333333333",
                "media_type": "text/plain",
                "display_name": "translation.txt",
                "byte_size": 7,
                "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            },
            "presentation": {"kind": "text", "text": "bonjour"}
        }))
        .expect("deserialize trusted output presentation");
        assert_eq!(
            view.presentation,
            ConnectOutputPresentation::Text {
                text: "bonjour".into()
            }
        );
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

    #[test]
    fn protocol_v1_mail_account_contract_is_typed_and_secret_free() {
        let accounts: MailAccounts = serde_json::from_value(json!({
            "providers": [{
                "provider": "gmail",
                "display_name": "Gmail",
                "connection_available": true,
                "connection_method": "browser_oauth",
                "multiple_accounts": true
            }],
            "accounts": [{
                "provider": "gmail",
                "account_id": "gmail-default",
                "display_name": "Gmail",
                "address": "owner@example.com",
                "connected": true,
                "active": true,
                "last_check": "2026-09-01T12:00:00+00:00"
            }]
        }))
        .expect("deserialize generic email account catalog");

        assert_eq!(
            accounts.accounts[0].address.as_deref(),
            Some("owner@example.com")
        );
        assert!(accounts.accounts[0].active);
        assert_eq!(accounts.providers[0].provider, "gmail");

        let result: MailAccountResult = serde_json::from_value(json!({
            "account": accounts.accounts[0],
            "baseline_initialized": false
        }))
        .expect("deserialize email account mutation result");
        assert_eq!(result.baseline_initialized, Some(false));
        let encoded = serde_json::to_string(&result).expect("serialize account result");
        assert!(!encoded.contains("token"));
    }

    #[cfg(unix)]
    #[test]
    fn mail_server_connection_is_forwarded_through_the_typed_engine_request() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let request_path = directory.path().join("request.json");
        let engine = Engine::with_command(
            "sh",
            vec![
                OsString::from("-c"),
                OsString::from(
                    r#"request=$(cat)
printf '%s' "$request" > "$1"
printf '%s\n' '{"protocol":1,"ok":true,"operation":"mail.accounts.connect","data":{"account":{"provider":"imap","account_id":"imap-test","display_name":"Other mail server","address":"owner@example.com","connected":true,"active":true,"last_check":null},"baseline_initialized":true}}'"#,
                ),
                OsString::from("engine-imap-connection-probe"),
                request_path.as_os_str().to_owned(),
            ],
            PathBuf::from("unused.toml"),
        );
        let connection = MailServerConnection {
            email_address: "owner@example.com".into(),
            host: "mail.example.com".into(),
            port: 993,
            security: MailServerSecurity::Tls,
            username: "owner".into(),
            password: "private password".into(),
            ca_file: Some("/private/root.pem".into()),
        };

        let result = engine
            .connect_mail_provider("imap".into(), Some(connection))
            .expect("connect mail server through engine request");
        let request: Value =
            serde_json::from_slice(&fs::read(&request_path).expect("read captured engine request"))
                .expect("decode captured engine request");

        assert_eq!(result.account.account_id, "imap-test");
        assert_eq!(
            request["payload"],
            json!({
                "provider": "imap",
                "connection": {
                    "email_address": "owner@example.com",
                    "host": "mail.example.com",
                    "port": 993,
                    "security": "tls",
                    "username": "owner",
                    "password": "private password",
                    "ca_file": "/private/root.pem"
                }
            })
        );

        engine
            .connect_mail_provider("gmail".into(), None)
            .expect("connect OAuth provider without server credentials");
        let oauth_request: Value =
            serde_json::from_slice(&fs::read(&request_path).expect("read captured OAuth request"))
                .expect("decode captured OAuth request");
        assert_eq!(oauth_request["payload"], json!({"provider": "gmail"}));
    }

    #[test]
    fn protocol_v1_entitlement_status_is_typed_and_claim_free() {
        let status: ConnectEntitlementStatus = serde_json::from_value(json!({
            "state": "expired",
            "active": false
        }))
        .expect("deserialize claim-free entitlement status");

        assert_eq!(
            status,
            ConnectEntitlementStatus {
                state: ConnectEntitlementState::Expired,
                active: false,
            }
        );
    }

    #[test]
    fn protocol_v1_calendar_consent_status_is_typed_and_secret_free() {
        let status: CalendarConsentStatus = serde_json::from_value(json!({
            "account_id": "microsoft365-account",
            "available": false,
            "entitlement_active": true,
            "profile": "proposal",
            "scope": "Calendars.Read.Shared",
            "state": "consent_pending"
        }))
        .expect("deserialize calendar consent status");

        assert_eq!(status.profile, CalendarConsentProfile::Proposal);
        assert_eq!(status.state, CalendarConsentState::ConsentPending);
        assert_eq!(status.scope, "Calendars.Read.Shared");
        let encoded = serde_json::to_string(&status).expect("serialize calendar consent status");
        assert!(!encoded.contains("token"));
        assert!(!encoded.contains("cache"));
    }

    #[cfg(unix)]
    #[test]
    fn calendar_consent_mutation_uses_closed_profile_operation_and_selected_account() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let request_path = directory.path().join("request.json");
        let engine = Engine::with_command(
            "sh",
            vec![
                OsString::from("-c"),
                OsString::from(
                    r#"request=$(cat)
printf '%s' "$request" > "$1"
printf '%s\n' '{"protocol":1,"ok":true,"operation":"calendar.write.connect","data":{"account_id":"microsoft365-account","available":true,"entitlement_active":true,"profile":"write","scope":"Calendars.ReadWrite","state":"ready"}}'"#,
                ),
                OsString::from("engine-calendar-consent-probe"),
                request_path.as_os_str().to_owned(),
            ],
            PathBuf::from("unused.toml"),
        );

        let status = engine
            .connect_calendar_consent(
                CalendarConsentProfile::Write,
                "microsoft365".into(),
                "microsoft365-account".into(),
            )
            .expect("connect calendar consent through engine request");
        let request: Value =
            serde_json::from_slice(&fs::read(&request_path).expect("read captured engine request"))
                .expect("decode captured engine request");

        assert!(status.available);
        assert_eq!(request["operation"], "calendar.write.connect");
        assert_eq!(
            request["payload"],
            json!({"provider": "microsoft365", "account_id": "microsoft365-account"})
        );
    }

    #[cfg(unix)]
    #[test]
    fn calendar_decision_bridge_forwards_exact_durable_identity() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let request_path = directory.path().join("request.json");
        let engine = Engine::with_command(
            "sh",
            vec![
                OsString::from("-c"),
                OsString::from(
                    r#"request=$(cat)
printf '%s' "$request" > "$1"
printf '%s\n' '{"protocol":1,"ok":true,"operation":"calendar.automation.decide","data":{"run_id":"77d9c691-1c91-4e23-8f03-92973e12c385","state":"completed","state_version":8,"failure_code":null,"graph_event_id":"immutable-event-id"}}'"#,
                ),
                OsString::from("engine-calendar-decision-probe"),
                request_path.as_os_str().to_owned(),
            ],
            PathBuf::from("unused.toml"),
        );

        let result = engine
            .decide_calendar_proposal(
                "message-1".into(),
                "77d9c691-1c91-4e23-8f03-92973e12c385".into(),
                7,
                2,
                "a".repeat(64),
                "confirm".into(),
            )
            .expect("decide calendar proposal through engine request");
        let request: Value =
            serde_json::from_slice(&fs::read(&request_path).expect("read captured engine request"))
                .expect("decode captured engine request");

        assert_eq!(result.state, "completed");
        assert_eq!(result.graph_event_id.as_deref(), Some("immutable-event-id"));
        assert_eq!(
            request["payload"],
            json!({
                "decision": "confirm",
                "message_id": "message-1",
                "proposal_sha256": "a".repeat(64),
                "proposal_version": 2,
                "run_id": "77d9c691-1c91-4e23-8f03-92973e12c385",
                "state_version": 7
            })
        );
    }

    #[cfg(unix)]
    #[test]
    fn calendar_consent_bridge_rejects_mismatched_engine_identity() {
        let engine = Engine::with_command(
            "sh",
            vec![
                OsString::from("-c"),
                OsString::from(
                    r#"cat >/dev/null
printf '%s\n' '{"protocol":1,"ok":true,"operation":"calendar.read.status","data":{"account_id":"other-account","available":true,"entitlement_active":true,"profile":"read","scope":"Calendars.Read","state":"ready"}}'"#,
                ),
            ],
            PathBuf::from("unused.toml"),
        );

        let error = engine
            .calendar_consent_status(
                CalendarConsentProfile::Read,
                "microsoft365".into(),
                "microsoft365-account".into(),
            )
            .expect_err("mismatched calendar consent status must fail closed");

        assert_eq!(error.code, "engine_protocol_error");
    }

    #[test]
    fn protocol_v1_settings_without_editability_default_to_read_only() {
        let settings: EngineSettings = serde_json::from_value(json!({
            "local_model": {
                "endpoint": "http://127.0.0.1:8080/v1",
                "model": "local-model"
            },
            "notifications_enabled": true,
            "poll_interval_minutes": 120,
            "polling_supported": true,
            "retention_days": 180
        }))
        .expect("deserialize settings from an older protocol-v1 engine");

        assert!(!settings.local_model.editable);
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

    #[cfg(windows)]
    fn windows_process_is_running(process_id: u32) -> bool {
        // SAFETY: OpenProcess receives a PID produced by the test child, and
        // the returned owned handle is closed before this helper returns.
        let raw_handle = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, process_id) };
        if raw_handle.is_null() {
            return false;
        }
        // SAFETY: OpenProcess returned a non-null, newly owned handle.
        let handle = unsafe { OwnedHandle::from_raw_handle(raw_handle) };
        // SAFETY: the process handle remains live for this nonblocking wait.
        unsafe { WaitForSingleObject(handle.as_raw_handle(), 0) == WAIT_TIMEOUT }
    }

    #[cfg(windows)]
    #[test]
    fn windows_job_terminates_immediate_descendant_on_timeout() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let script_path = directory.path().join("process-tree-probe.ps1");
        let descendant_pid_file = directory.path().join("descendant.pid");
        fs::write(
            &script_path,
            r#"
$descendant = Start-Process -PassThru -WindowStyle Hidden -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 30")
Set-Content -LiteralPath $args[0] -Value $descendant.Id
Wait-Process -Id $descendant.Id
"#,
        )
        .expect("write process-tree probe");
        let engine = Engine::with_command(
            "powershell.exe",
            vec![
                OsString::from("-NoProfile"),
                OsString::from("-NonInteractive"),
                OsString::from("-ExecutionPolicy"),
                OsString::from("Bypass"),
                OsString::from("-File"),
                script_path.as_os_str().to_owned(),
                descendant_pid_file.as_os_str().to_owned(),
            ],
            PathBuf::from("unused.toml"),
        )
        .with_request_timeout(WINDOWS_PROCESS_PROBE_TIMEOUT);

        assert_eq!(
            engine
                .check()
                .expect_err("stalled check must time out")
                .code,
            "engine_timeout"
        );
        let descendant_pid: u32 = fs::read_to_string(descendant_pid_file)
            .expect("read descendant pid")
            .trim()
            .parse()
            .expect("parse descendant pid");
        for _ in 0..100 {
            if !windows_process_is_running(descendant_pid) {
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        panic!("timed-out Windows engine descendant {descendant_pid} is still running");
    }

    #[cfg(windows)]
    #[test]
    fn windows_job_releases_descendant_after_successful_request() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let script_path = directory.path().join("successful-process-tree-probe.ps1");
        let descendant_pid_file = directory.path().join("descendant.pid");
        fs::write(
            &script_path,
            r#"
$null = [Console]::In.ReadToEnd()
$descendant = Start-Process -PassThru -WindowStyle Hidden -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 30")
Set-Content -LiteralPath $args[0] -Value $descendant.Id
[Console]::Out.WriteLine('{"protocol":1,"ok":true,"operation":"watcher.check","data":{"active":false,"discovered":0,"summarized":0,"fallback_notified":0,"purged":0,"stale_cursor_recovered":false,"pending_notifications":0}}')
"#,
        )
        .expect("write successful process-tree probe");
        let engine = Engine::with_command(
            "powershell.exe",
            vec![
                OsString::from("-NoProfile"),
                OsString::from("-NonInteractive"),
                OsString::from("-ExecutionPolicy"),
                OsString::from("Bypass"),
                OsString::from("-File"),
                script_path.as_os_str().to_owned(),
                descendant_pid_file.as_os_str().to_owned(),
            ],
            PathBuf::from("unused.toml"),
        )
        .with_request_timeout(WINDOWS_PROCESS_PROBE_TIMEOUT);

        let result = engine.check().expect("probe request must succeed");
        assert!(!result.active);
        let descendant_pid: u32 = fs::read_to_string(descendant_pid_file)
            .expect("read descendant pid")
            .trim()
            .parse()
            .expect("parse descendant pid");
        let _cleanup = WindowsTestProcess(descendant_pid);
        assert!(
            windows_process_is_running(descendant_pid),
            "successful request must not terminate descendant {descendant_pid}"
        );
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

    fn toml_path_literal(path: &Path) -> String {
        serde_json::to_string(&path.to_string_lossy()).expect("serialize test config path")
    }

    #[test]
    fn toml_path_literal_escapes_windows_separators() {
        assert_eq!(
            toml_path_literal(Path::new(r"C:\Users\Watcher\state.sqlite3")),
            r#""C:\\Users\\Watcher\\state.sqlite3""#
        );
    }

    #[test]
    fn real_engine_initializes_missing_config_once() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let config_path = directory.path().join("nested/config.toml");
        let engine = real_engine(config_path);

        assert!(!engine.config_present().expect("inspect missing config"));
        assert_eq!(
            engine
                .initialize_config(
                    "UTC".into(),
                    "http://127.0.0.1:8080/v1".into(),
                    "local-model".into(),
                )
                .expect("initialize first-run config"),
            ConfigInitialization {
                created: true,
                settings: EngineSettings {
                    local_model: LocalModelSettings {
                        editable: true,
                        endpoint: "http://127.0.0.1:8080/v1".into(),
                        model: "local-model".into(),
                    },
                    notifications_enabled: true,
                    poll_interval_minutes: 120,
                    polling_supported: true,
                    retention_days: 180,
                },
            }
        );
        assert!(engine.config_present().expect("inspect created config"));
        assert_eq!(
            engine
                .initialize_config(
                    "UTC".into(),
                    "http://127.0.0.1:8080/v1".into(),
                    "local-model".into(),
                )
                .expect_err("existing config must not be replaced")
                .code,
            "conflict"
        );
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
                r#"database_file = {}
gmail_credentials_file = {}
gmail_token_file = {}
model_base_url = "http://127.0.0.1:9/v1"
model_name = "local-model"
model_require_auth = false
notifications_enabled = true
"#,
                toml_path_literal(&database_path),
                toml_path_literal(&gmail_credentials_path),
                toml_path_literal(&gmail_token_path)
            ),
        )
        .expect("write config");
        let engine = real_engine(config_path);

        let health = engine.health().expect("read engine health");
        let accounts = engine.mail_accounts().expect("read email accounts");
        assert_eq!(
            engine
                .connect_entitlement_status()
                .expect("read entitlement status without watcher configuration coupling"),
            ConnectEntitlementStatus {
                state: ConnectEntitlementState::AuthorityUnavailable,
                active: false,
            }
        );
        assert_eq!(
            health.database,
            DatabaseHealth {
                ok: true,
                initialized: false
            }
        );
        assert!(!health.gmail.credentials_configured);
        assert!(!health.gmail.connected);
        assert_eq!(accounts.accounts.len(), 1);
        assert_eq!(accounts.accounts[0].provider, "gmail");
        assert_eq!(accounts.accounts[0].account_id, "gmail-default");
        assert!(accounts.accounts[0].active);
        assert!(!accounts.accounts[0].connected);
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
                .run_with_operation_lock(|| {
                    Ok((
                        engine.pending_notifications_under_host_lock(25)?,
                        engine.pending_notification_count_under_host_lock()?,
                    ))
                })
                .expect("host lock must permit lock-aware notification reads"),
            (vec![], 0)
        );
        assert_eq!(
            engine
                .run_with_operation_lock(|| engine.clear_inbox())
                .expect_err("Python mutation must contend with the Rust host lock")
                .code,
            "runtime_error"
        );
        assert_eq!(
            engine
                .clear_inbox()
                .expect("released host lock is reusable"),
            0
        );
        assert_eq!(
            engine
                .settings_with_timeout(Duration::from_secs(5))
                .expect("read engine settings"),
            EngineSettings {
                local_model: LocalModelSettings {
                    editable: true,
                    endpoint: "http://127.0.0.1:9/v1".into(),
                    model: "local-model".into(),
                },
                notifications_enabled: true,
                poll_interval_minutes: 120,
                polling_supported: true,
                retention_days: 180,
            }
        );
        assert_eq!(
            engine
                .update_settings(0, 180, true, None, None)
                .expect_err("invalid polling cadence must fail")
                .code,
            "invalid_request"
        );
        assert_eq!(
            engine
                .update_settings(
                    45,
                    365,
                    false,
                    Some("http://localhost:8080/v1/".into()),
                    Some("replacement-model".into()),
                )
                .expect("update safe desktop settings"),
            EngineSettings {
                local_model: LocalModelSettings {
                    editable: true,
                    endpoint: "http://localhost:8080/v1".into(),
                    model: "replacement-model".into(),
                },
                notifications_enabled: false,
                poll_interval_minutes: 45,
                polling_supported: true,
                retention_days: 365,
            }
        );
        assert_eq!(
            engine.settings().expect("read updated desktop settings"),
            EngineSettings {
                local_model: LocalModelSettings {
                    editable: true,
                    endpoint: "http://localhost:8080/v1".into(),
                    model: "replacement-model".into(),
                },
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
                automation_processed: 0,
                automation_review_required: 0,
            }
        );
        assert_eq!(engine.list().expect("list empty watchlist"), vec![]);
        assert_eq!(
            engine
                .query_inbox(InboxQuery {
                    limit: 20,
                    cursor: None,
                    provider: None,
                    account_id: None,
                    sender_query: None,
                    priority: None,
                    category: None,
                    status: None,
                    keyword: None,
                })
                .expect("list empty inbox")
                .items,
            vec![]
        );
        assert_eq!(engine.clear_inbox().expect("clear empty inbox"), 0);
        assert_eq!(
            engine
                .delete_inbox_item("missing-message".into())
                .expect_err("missing inbox item must not be deleted")
                .code,
            "not_found"
        );
        assert_eq!(
            engine
                .attachment_capabilities("missing-message".into(), "2".into())
                .expect_err("missing attachment must not discover capabilities")
                .code,
            "not_found"
        );
        assert_eq!(
            engine
                .invoke_attachment_capability(
                    "22222222-2222-4222-8222-222222222222".into(),
                    "missing-message".into(),
                    "2".into(),
                    ConnectProviderIdentity {
                        app_id: "document-summarizer".into(),
                        version: "0.1.0".into(),
                        instance_id: "11111111-1111-4111-8111-111111111111".into(),
                    },
                    ConnectCapabilityRef {
                        id: "document.summarize".into(),
                        version: "1.0".into(),
                    },
                    BTreeMap::new(),
                    false,
                )
                .expect_err("missing attachment must not invoke a capability")
                .code,
            "not_found"
        );
        assert_eq!(
            engine
                .present_capability_output(
                    "missing-message".into(),
                    "2".into(),
                    "22222222-2222-4222-8222-222222222222".into(),
                    "33333333-3333-4333-8333-333333333333".into(),
                )
                .expect_err("missing capability output must not be presented")
                .code,
            "not_found"
        );
        assert_eq!(
            engine
                .export_capability_output(
                    "missing-message".into(),
                    "2".into(),
                    "22222222-2222-4222-8222-222222222222".into(),
                    "33333333-3333-4333-8333-333333333333".into(),
                    directory.path().to_path_buf(),
                )
                .expect_err("missing capability output must not be exported")
                .code,
            "not_found"
        );
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
            revision: Some("2026-08-28T12:00:00+00:00".into()),
            subject_id: Some("missing-message".into()),
            subject_type: Some("message".into()),
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
