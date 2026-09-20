mod delivery;
mod engine;
mod scheduler;

use delivery::NotificationDelivery;
use engine::{
    AdmissionErrorObserver, AdmissionToken, CalendarConsentProfile, CalendarConsentStatus,
    CalendarDecisionResult, CancellationToken, CheckResult, ConnectCapabilities,
    ConnectCapabilityRef, ConnectEntitlementStatus, ConnectInvocationResult, ConnectOutputView,
    ConnectProviderIdentity, Engine, EngineError, EngineSettings, GmailAuthorization, HealthStatus,
    InboxPage, InboxQuery, MailAccountResult, MailAccounts, MailServerConnection,
    NtfyDisclosureStatus, WatchedSender,
};
use scheduler::{ConnectQueueScheduler, OwnedWorker, PollScheduler, PollingStatus, WorkerGate};
use serde::Serialize;
use serde_json::Value;
use std::collections::BTreeMap;
#[cfg(desktop)]
use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex, mpsc};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_opener::OpenerExt;

const STARTUP_DELIVERY_TIMEOUT: Duration = Duration::from_secs(30);
const CONFIG_ADMISSION_EVENT: &str = "watcher://config-admission";
#[cfg(desktop)]
const TRAY_OPEN_ID: &str = "open";
#[cfg(desktop)]
const TRAY_QUIT_ID: &str = "quit";
#[cfg(desktop)]
const AUTOSTART_BACKGROUND_ARG: &str = "--background";

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
fn starts_in_background<I, S>(args: I) -> bool
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    args.into_iter()
        .any(|arg| arg.as_ref() == OsStr::new(AUTOSTART_BACKGROUND_ARG))
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
struct AutostartStatus {
    available: bool,
    enabled: bool,
}

#[cfg(desktop)]
#[derive(Debug, PartialEq, Eq)]
enum AutostartUpdateError {
    Write,
    ReadBack,
    NotApplied,
}

#[cfg(desktop)]
fn apply_autostart_update<E>(
    enabled: bool,
    update: impl FnOnce(bool) -> Result<(), E>,
    inspect: impl FnOnce() -> Result<bool, E>,
) -> Result<bool, AutostartUpdateError> {
    update(enabled).map_err(|_| AutostartUpdateError::Write)?;
    let actual = inspect().map_err(|_| AutostartUpdateError::ReadBack)?;
    if actual != enabled {
        return Err(AutostartUpdateError::NotApplied);
    }
    Ok(actual)
}

#[tauri::command]
fn autostart_get(app: AppHandle) -> Result<AutostartStatus, EngineError> {
    #[cfg(desktop)]
    {
        let Some(manager) = app.try_state::<tauri_plugin_autostart::AutoLaunchManager>() else {
            return Ok(AutostartStatus {
                available: false,
                enabled: false,
            });
        };
        let enabled = manager.is_enabled().map_err(|_| {
            EngineError::host(
                "autostart_unavailable",
                "Start-on-login status could not be read",
            )
        })?;
        Ok(AutostartStatus {
            available: true,
            enabled,
        })
    }
    #[cfg(not(desktop))]
    {
        let _ = app;
        Ok(AutostartStatus {
            available: false,
            enabled: false,
        })
    }
}

#[tauri::command]
fn autostart_set(app: AppHandle, enabled: bool) -> Result<AutostartStatus, EngineError> {
    #[cfg(desktop)]
    {
        let manager = app
            .try_state::<tauri_plugin_autostart::AutoLaunchManager>()
            .ok_or_else(|| {
                EngineError::host(
                    "autostart_unavailable",
                    "Start on login is unavailable on this installation",
                )
            })?;
        let actual = apply_autostart_update(
            enabled,
            |requested| {
                if requested {
                    manager.enable()
                } else {
                    manager.disable()
                }
            },
            || manager.is_enabled(),
        )
        .map_err(|error| match error {
            AutostartUpdateError::Write => EngineError::host(
                "autostart_update_failed",
                "Start-on-login setting could not be updated",
            ),
            AutostartUpdateError::ReadBack => EngineError::host(
                "autostart_unavailable",
                "Start-on-login status could not be verified",
            ),
            AutostartUpdateError::NotApplied => EngineError::host(
                "autostart_update_failed",
                "Start-on-login setting did not reach the requested state",
            ),
        })?;
        Ok(AutostartStatus {
            available: true,
            enabled: actual,
        })
    }
    #[cfg(not(desktop))]
    {
        let _ = (app, enabled);
        Err(EngineError::host(
            "autostart_unsupported",
            "Start on login is not supported on this platform",
        ))
    }
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

#[derive(Clone, Debug, PartialEq, Eq)]
enum AdmissionState {
    Inspecting,
    Mutating,
    Missing,
    AwaitingAcknowledgement { expected_revision: String },
    ManualRepairRequired,
    Admitted,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
enum ConfigAdmissionStatus {
    Missing,
    AcknowledgementRequired { expected_revision: String },
    ManualRepairRequired,
    Admitted,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
struct ConfigAdmissionUpdate {
    generation: u64,
    #[serde(flatten)]
    status: ConfigAdmissionStatus,
}

impl ConfigAdmissionUpdate {
    fn is_admitted(&self) -> bool {
        self.status == ConfigAdmissionStatus::Admitted
    }
}

type AdmissionEventSink = Arc<dyn Fn(ConfigAdmissionUpdate) + Send + Sync + 'static>;

impl AdmissionState {
    fn public_status(&self) -> ConfigAdmissionStatus {
        match self {
            Self::Inspecting | Self::Mutating | Self::ManualRepairRequired => {
                ConfigAdmissionStatus::ManualRepairRequired
            }
            Self::Missing => ConfigAdmissionStatus::Missing,
            Self::AwaitingAcknowledgement { expected_revision } => {
                ConfigAdmissionStatus::AcknowledgementRequired {
                    expected_revision: expected_revision.clone(),
                }
            }
            Self::Admitted => ConfigAdmissionStatus::Admitted,
        }
    }
}

struct AdmissionAttempt<W> {
    state: AdmissionState,
    workers: Option<W>,
}

fn run_admission_attempt<W>(
    inspect: impl FnOnce() -> Result<NtfyDisclosureStatus, EngineError>,
    admit: impl FnOnce() -> Result<W, EngineError>,
) -> AdmissionAttempt<W> {
    match inspect() {
        Ok(NtfyDisclosureStatus::Missing) => AdmissionAttempt {
            state: AdmissionState::Missing,
            workers: None,
        },
        Ok(NtfyDisclosureStatus::AcknowledgementRequired { expected_revision }) => {
            AdmissionAttempt {
                state: AdmissionState::AwaitingAcknowledgement { expected_revision },
                workers: None,
            }
        }
        Ok(NtfyDisclosureStatus::ManualRepairRequired) | Err(_) => AdmissionAttempt {
            state: AdmissionState::ManualRepairRequired,
            workers: None,
        },
        Ok(NtfyDisclosureStatus::NormalAdmission) => match admit() {
            Ok(workers) => AdmissionAttempt {
                state: AdmissionState::Admitted,
                workers: Some(workers),
            },
            Err(_) => AdmissionAttempt {
                state: AdmissionState::ManualRepairRequired,
                workers: None,
            },
        },
    }
}

fn run_acknowledgement_attempt<W>(
    acknowledge: impl FnOnce() -> Result<(), EngineError>,
    inspect: impl FnOnce() -> Result<NtfyDisclosureStatus, EngineError>,
    admit: impl FnOnce() -> Result<W, EngineError>,
) -> AdmissionAttempt<W> {
    let _outcome = acknowledge();
    run_admission_attempt(inspect, admit)
}

struct AdmissionWorkers {
    admission_engine: Engine,
    admission_token: AdmissionToken,
    observer: AdmissionErrorObserver,
    cancellation: CancellationToken,
    connect_queue: ConnectQueueScheduler,
    scheduler: PollScheduler,
    startup_delivery: StartupDeliveryWorker,
}

#[derive(Clone)]
struct StartupDeliveryWorker {
    gate: Arc<WorkerGate>,
    worker: OwnedWorker,
}

impl StartupDeliveryWorker {
    fn stage(
        engine: Engine,
        delivery: NotificationDelivery,
        cancellation: CancellationToken,
    ) -> std::io::Result<Self> {
        let gate = Arc::new(WorkerGate::with_cancellation(cancellation));
        let worker = OwnedWorker::default();
        let staged = Self {
            gate: Arc::clone(&gate),
            worker: worker.clone(),
        };
        worker.spawn("email-watcher-startup-delivery", move || {
            if !gate.wait_for_activation() {
                return;
            }
            let cancellation = gate.cancellation();
            match delivery.deliver_with_cancellation(
                &engine,
                STARTUP_DELIVERY_TIMEOUT,
                cancellation.clone(),
            ) {
                Ok(outcome) if outcome.failed > 0 => eprintln!(
                    "{} watcher startup notification deliveries failed; {} remain queued",
                    outcome.failed, outcome.remaining
                ),
                Ok(_) => {}
                Err(_) if cancellation.is_cancelled() => {}
                Err(error) => eprintln!(
                    "watcher startup notification delivery failed ({}): {}",
                    error.code, error.message
                ),
            }
        })?;
        Ok(staged)
    }

    #[cfg(all(test, unix))]
    fn stage_probe(
        cancellation: CancellationToken,
        operation: impl FnOnce(CancellationToken) + Send + 'static,
    ) -> std::io::Result<Self> {
        let gate = Arc::new(WorkerGate::with_cancellation(cancellation));
        let worker = OwnedWorker::default();
        let staged = Self {
            gate: Arc::clone(&gate),
            worker: worker.clone(),
        };
        worker.spawn("email-watcher-startup-delivery-probe", move || {
            if gate.wait_for_activation() {
                operation(gate.cancellation());
            }
        })?;
        Ok(staged)
    }

    fn activate(&self) {
        self.gate.activate();
    }

    fn signal_stop(&self) {
        self.gate.signal_stop();
    }

    fn join(&self) -> std::io::Result<()> {
        self.worker.join("Startup notification delivery worker")
    }

    fn shutdown(&self) -> std::io::Result<()> {
        let cancellation = self.gate.cancellation();
        cancellation.cancel();
        self.signal_stop();
        cancellation.wait_for_registrations();
        self.join()
    }

    #[cfg(all(test, unix))]
    fn is_joined(&self) -> bool {
        self.worker.is_joined()
    }
}

trait StagedWorker {
    fn shutdown(&self);
}

impl StagedWorker for ConnectQueueScheduler {
    fn shutdown(&self) {
        if let Err(error) = ConnectQueueScheduler::shutdown(self) {
            eprintln!("Connect queue scheduler could not stop cleanly: {error}");
        }
    }
}

impl StagedWorker for PollScheduler {
    fn shutdown(&self) {
        if let Err(error) = PollScheduler::shutdown(self) {
            eprintln!("Polling scheduler could not stop cleanly: {error}");
        }
    }
}

impl StagedWorker for StartupDeliveryWorker {
    fn shutdown(&self) {
        if let Err(error) = StartupDeliveryWorker::shutdown(self) {
            eprintln!("Startup notification delivery worker could not stop cleanly: {error}");
        }
    }
}

fn stage_worker_pair<Q, P, E>(
    stage_queue: impl FnOnce() -> Result<Q, E>,
    stage_poll: impl FnOnce(&Q) -> Result<P, E>,
) -> Result<(Q, P), E>
where
    Q: StagedWorker,
{
    let queue = stage_queue()?;
    match stage_poll(&queue) {
        Ok(poll) => Ok((queue, poll)),
        Err(error) => {
            queue.shutdown();
            Err(error)
        }
    }
}

trait AdmissionWorkerSet: Send + 'static {
    fn revalidate(&self) -> Result<(), EngineError> {
        Ok(())
    }

    fn admission_token(&self) -> Option<&AdmissionToken> {
        None
    }

    fn begin_revocation(&self) {}

    fn activate(&self);
}

impl AdmissionWorkerSet for AdmissionWorkers {
    fn revalidate(&self) -> Result<(), EngineError> {
        self.admission_engine
            .compare_admission(&self.admission_token)
    }

    fn admission_token(&self) -> Option<&AdmissionToken> {
        Some(&self.admission_token)
    }

    fn begin_revocation(&self) {
        self.admission_engine
            .clear_admission_binding(&self.admission_token);
        self.cancellation.cancel();
        self.startup_delivery.signal_stop();
        self.scheduler.signal_stop();
        self.connect_queue.signal_stop();
    }

    fn activate(&self) {
        self.admission_engine
            .install_admission_binding(self.admission_token.clone(), Arc::clone(&self.observer));
        self.connect_queue.activate();
        self.scheduler.activate();
        self.startup_delivery.activate();
    }
}

impl AdmissionWorkerSet for () {
    fn activate(&self) {}
}

impl Drop for AdmissionWorkers {
    fn drop(&mut self) {
        self.begin_revocation();
        self.cancellation.wait_for_registrations();
        if let Err(error) = self.startup_delivery.join() {
            eprintln!("Startup notification delivery worker could not stop cleanly: {error}");
        }
        if let Err(error) = self.scheduler.join() {
            eprintln!("Polling scheduler could not stop cleanly: {error}");
        }
        if let Err(error) = self.connect_queue.join() {
            eprintln!("Connect queue scheduler could not stop cleanly: {error}");
        }
    }
}

struct AdmissionInner<W> {
    state: AdmissionState,
    workers: Option<W>,
    generation: u64,
    active_effects: usize,
}

struct AdmissionPermit<W: AdmissionWorkerSet> {
    inner: Arc<Mutex<AdmissionInner<W>>>,
    effects_quiesced: Arc<Condvar>,
}

impl<W: AdmissionWorkerSet> std::fmt::Debug for AdmissionPermit<W> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("AdmissionPermit")
    }
}

impl<W: AdmissionWorkerSet> Drop for AdmissionPermit<W> {
    fn drop(&mut self) {
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        debug_assert!(inner.active_effects > 0);
        inner.active_effects = inner.active_effects.saturating_sub(1);
        if inner.active_effects == 0 {
            self.effects_quiesced.notify_all();
        }
    }
}

#[derive(Clone)]
struct AdmissionTransition {
    status: ConfigAdmissionStatus,
    generation: u64,
}

impl From<&AdmissionTransition> for ConfigAdmissionUpdate {
    fn from(transition: &AdmissionTransition) -> Self {
        Self {
            generation: transition.generation,
            status: transition.status.clone(),
        }
    }
}

struct AdmissionCleanup<W> {
    workers: W,
    update: ConfigAdmissionUpdate,
}

struct AdmissionRevocationSupervisor<W: AdmissionWorkerSet> {
    sender: Mutex<Option<mpsc::Sender<AdmissionCleanup<W>>>>,
    worker: OwnedWorker,
    event_sink: AdmissionEventSink,
}

impl<W: AdmissionWorkerSet> AdmissionRevocationSupervisor<W> {
    fn start(event_sink: AdmissionEventSink) -> std::io::Result<Self> {
        let (sender, receiver) = mpsc::channel::<AdmissionCleanup<W>>();
        let worker = OwnedWorker::default();
        let supervisor_sink = Arc::clone(&event_sink);
        worker.spawn("email-watcher-admission-revocation", move || {
            while let Ok(cleanup) = receiver.recv() {
                let update = cleanup.update;
                drop(cleanup.workers);
                supervisor_sink(update);
            }
        })?;
        Ok(Self {
            sender: Mutex::new(Some(sender)),
            worker,
            event_sink,
        })
    }

    fn enqueue(&self, cleanup: AdmissionCleanup<W>) -> Result<(), AdmissionCleanup<W>> {
        let sender = self
            .sender
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let Some(sender) = sender.as_ref() else {
            return Err(cleanup);
        };
        sender.send(cleanup).map_err(|error| error.0)
    }

    fn sender(&self) -> Option<mpsc::Sender<AdmissionCleanup<W>>> {
        self.sender
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .cloned()
    }

    fn emit(&self, update: ConfigAdmissionUpdate) {
        (self.event_sink)(update);
    }
}

impl<W: AdmissionWorkerSet> Drop for AdmissionRevocationSupervisor<W> {
    fn drop(&mut self) {
        self.sender
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        if let Err(error) = self
            .worker
            .join("Configuration admission revocation supervisor")
        {
            eprintln!("Configuration admission revocation supervisor could not stop: {error}");
        }
    }
}

struct AdmissionCoordinator<W: AdmissionWorkerSet = AdmissionWorkers> {
    inner: Arc<Mutex<AdmissionInner<W>>>,
    supervisor: Arc<AdmissionRevocationSupervisor<W>>,
    effects_quiesced: Arc<Condvar>,
}

impl<W: AdmissionWorkerSet> Clone for AdmissionCoordinator<W> {
    fn clone(&self) -> Self {
        Self {
            inner: Arc::clone(&self.inner),
            supervisor: Arc::clone(&self.supervisor),
            effects_quiesced: Arc::clone(&self.effects_quiesced),
        }
    }
}

impl<W: AdmissionWorkerSet> AdmissionCoordinator<W> {
    #[cfg(test)]
    fn new() -> Self {
        Self::with_event_sink(Arc::new(|_| {}))
            .expect("configuration admission revocation supervisor must start")
    }

    fn with_event_sink(event_sink: AdmissionEventSink) -> std::io::Result<Self> {
        let supervisor = Arc::new(AdmissionRevocationSupervisor::start(event_sink)?);
        Ok(Self {
            inner: Arc::new(Mutex::new(AdmissionInner {
                state: AdmissionState::Inspecting,
                workers: None,
                generation: 0,
                active_effects: 0,
            })),
            supervisor,
            effects_quiesced: Arc::new(Condvar::new()),
        })
    }

    fn require_admitted(&self) -> Result<AdmissionPermit<W>, EngineError> {
        let mut inner = self.inner.lock().map_err(|_| {
            EngineError::host(
                "configuration_not_admitted",
                "Watcher configuration is not admitted",
            )
        })?;
        if inner.workers.is_some() && inner.state == AdmissionState::Admitted {
            inner.active_effects = inner.active_effects.checked_add(1).ok_or_else(|| {
                EngineError::host("host_error", "Watcher command admission is unavailable")
            })?;
            Ok(AdmissionPermit {
                inner: Arc::clone(&self.inner),
                effects_quiesced: Arc::clone(&self.effects_quiesced),
            })
        } else {
            Err(EngineError::host(
                "configuration_not_admitted",
                "Watcher configuration is not admitted",
            ))
        }
    }

    fn next_generation(inner: &mut AdmissionInner<W>) -> Result<u64, EngineError> {
        inner.generation = inner.generation.checked_add(1).ok_or_else(|| {
            EngineError::host(
                "host_error",
                "Configuration admission generation is unavailable",
            )
        })?;
        Ok(inner.generation)
    }

    fn take_for_revocation(inner: &mut AdmissionInner<W>) -> Option<AdmissionCleanup<W>> {
        inner.state = AdmissionState::ManualRepairRequired;
        let workers = inner.workers.as_ref()?;
        workers.begin_revocation();
        inner.generation = inner.generation.saturating_add(1);
        Some(AdmissionCleanup {
            workers: inner.workers.take().expect("workers checked above"),
            update: ConfigAdmissionUpdate {
                generation: inner.generation,
                status: ConfigAdmissionStatus::ManualRepairRequired,
            },
        })
    }

    fn enqueue_cleanup(
        inner: &Arc<Mutex<AdmissionInner<W>>>,
        supervisor: &AdmissionRevocationSupervisor<W>,
        cleanup: AdmissionCleanup<W>,
    ) {
        if let Err(cleanup) = supervisor.enqueue(cleanup) {
            Self::restore_cleanup(inner, cleanup);
        }
    }

    fn restore_cleanup(inner: &Arc<Mutex<AdmissionInner<W>>>, cleanup: AdmissionCleanup<W>) {
        let mut state = inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if state.generation == cleanup.update.generation && state.workers.is_none() {
            state.workers = Some(cleanup.workers);
        } else {
            std::mem::forget(cleanup.workers);
        }
    }

    fn engine_error_observer(&self) -> AdmissionErrorObserver {
        let inner = Arc::downgrade(&self.inner);
        let sender = self
            .supervisor
            .sender()
            .expect("configuration admission revocation supervisor is running");
        Arc::new(move |token, error| {
            if !matches!(error.code.as_str(), "conflict" | "configuration_error") {
                return;
            }
            let Some(inner) = inner.upgrade() else {
                return;
            };
            let cleanup = {
                let mut state = inner
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                let token_is_current = state.state == AdmissionState::Admitted
                    && state
                        .workers
                        .as_ref()
                        .and_then(AdmissionWorkerSet::admission_token)
                        == Some(token);
                token_is_current
                    .then(|| Self::take_for_revocation(&mut state))
                    .flatten()
            };
            if let Some(cleanup) = cleanup
                && let Err(error) = sender.send(cleanup)
            {
                Self::restore_cleanup(&inner, error.0);
            }
        })
    }

    fn fail_config_mutation(&self, expected_generation: u64) -> Result<(), EngineError> {
        let update = {
            let mut inner = self.inner.lock().map_err(|_| {
                EngineError::host("host_error", "Configuration admission coordinator stopped")
            })?;
            if inner.generation != expected_generation
                || inner.state != AdmissionState::Mutating
                || inner.workers.is_some()
            {
                return Err(EngineError::host(
                    "configuration_not_admitted",
                    "Watcher configuration is not admitted",
                ));
            }
            Self::next_generation(&mut inner)?;
            inner.state = AdmissionState::ManualRepairRequired;
            ConfigAdmissionUpdate {
                generation: inner.generation,
                status: ConfigAdmissionStatus::ManualRepairRequired,
            }
        };
        self.supervisor.emit(update);
        Ok(())
    }

    fn activate_mutation_workers(
        &self,
        expected_generation: u64,
        staged: W,
    ) -> Result<(), EngineError> {
        staged.revalidate()?;
        let mut inner = self.inner.lock().map_err(|_| {
            EngineError::host("host_error", "Configuration admission coordinator stopped")
        })?;
        if inner.generation != expected_generation
            || inner.state != AdmissionState::Mutating
            || inner.workers.is_some()
        {
            return Err(EngineError::host(
                "configuration_not_admitted",
                "Watcher configuration is not admitted",
            ));
        }
        Self::next_generation(&mut inner)?;
        inner.state = AdmissionState::Admitted;
        inner.workers = Some(staged);
        inner
            .workers
            .as_ref()
            .expect("new admission workers installed")
            .activate();
        let update = ConfigAdmissionUpdate {
            generation: inner.generation,
            status: ConfigAdmissionStatus::Admitted,
        };
        drop(inner);
        self.supervisor.emit(update);
        Ok(())
    }

    fn mutate_with<T>(
        &self,
        mutate: impl FnOnce() -> Result<T, EngineError>,
        restage: impl FnOnce() -> Result<W, EngineError>,
    ) -> Result<T, EngineError> {
        let generation = {
            let mut inner = self.inner.lock().map_err(|_| {
                EngineError::host("host_error", "Configuration admission coordinator stopped")
            })?;
            if inner.state != AdmissionState::Admitted || inner.workers.is_none() {
                return Err(EngineError::host(
                    "configuration_not_admitted",
                    "Watcher configuration is not admitted",
                ));
            }
            inner.state = AdmissionState::Mutating;
            inner.generation
        };

        let mut inner = self.inner.lock().map_err(|_| {
            EngineError::host("host_error", "Configuration admission coordinator stopped")
        })?;
        while inner.active_effects != 0 {
            inner = self.effects_quiesced.wait(inner).map_err(|_| {
                EngineError::host("host_error", "Configuration admission coordinator stopped")
            })?;
        }
        if inner.generation != generation
            || inner.state != AdmissionState::Mutating
            || inner.workers.is_none()
        {
            return Err(EngineError::host(
                "configuration_not_admitted",
                "Watcher configuration is not admitted",
            ));
        }
        let workers = inner
            .workers
            .take()
            .expect("mutating workers checked above");
        let prior_token = workers.admission_token().cloned();
        workers.begin_revocation();
        drop(inner);
        drop(workers);
        let value = match mutate() {
            Ok(value) => value,
            Err(error) => {
                let recovery = restage().and_then(|staged| {
                    if prior_token.as_ref() != staged.admission_token() {
                        return Err(EngineError::host(
                            "conflict",
                            "Watcher configuration changed during config mutation",
                        ));
                    }
                    self.activate_mutation_workers(generation, staged)
                });
                return match recovery {
                    Ok(()) => Err(error),
                    Err(recovery_error) => {
                        self.fail_config_mutation(generation)?;
                        Err(recovery_error)
                    }
                };
            }
        };
        let staged = match restage() {
            Ok(staged) => staged,
            Err(error) => {
                self.fail_config_mutation(generation)?;
                return Err(error);
            }
        };
        if let Err(error) = self.activate_mutation_workers(generation, staged) {
            self.fail_config_mutation(generation)?;
            return Err(error);
        }
        Ok(value)
    }

    fn install_attempt(
        inner: &mut AdmissionInner<W>,
        mut attempt: AdmissionAttempt<W>,
    ) -> AdmissionTransition
    where
        W: AdmissionWorkerSet,
    {
        if attempt.state == AdmissionState::Admitted
            && attempt
                .workers
                .as_ref()
                .is_none_or(|workers| workers.revalidate().is_err())
        {
            attempt.state = AdmissionState::ManualRepairRequired;
            attempt.workers = None;
        }
        inner.state = attempt.state;
        inner.workers = attempt.workers;
        if inner.state == AdmissionState::Admitted
            && let Some(workers) = inner.workers.as_ref()
        {
            workers.activate();
        }
        AdmissionTransition {
            status: inner.state.public_status(),
            generation: inner.generation,
        }
    }

    fn refresh_with(
        &self,
        inspect: impl FnOnce() -> Result<NtfyDisclosureStatus, EngineError>,
        admit: impl FnOnce() -> Result<W, EngineError>,
    ) -> Result<AdmissionTransition, EngineError>
    where
        W: AdmissionWorkerSet,
    {
        let (transition, cleanup) = {
            let mut inner = self.inner.lock().map_err(|_| {
                EngineError::host("host_error", "Configuration admission coordinator stopped")
            })?;
            if inner.state == AdmissionState::Mutating {
                return Ok(AdmissionTransition {
                    status: ConfigAdmissionStatus::ManualRepairRequired,
                    generation: inner.generation,
                });
            }
            if inner.state == AdmissionState::Admitted
                && let Some(workers) = inner.workers.as_ref()
            {
                if workers.revalidate().is_ok() {
                    (
                        AdmissionTransition {
                            status: ConfigAdmissionStatus::Admitted,
                            generation: inner.generation,
                        },
                        None,
                    )
                } else {
                    let cleanup = Self::take_for_revocation(&mut inner);
                    (
                        AdmissionTransition {
                            status: ConfigAdmissionStatus::ManualRepairRequired,
                            generation: inner.generation,
                        },
                        cleanup,
                    )
                }
            } else {
                Self::next_generation(&mut inner)?;
                inner.state = AdmissionState::Inspecting;
                let attempt = run_admission_attempt(inspect, admit);
                (Self::install_attempt(&mut inner, attempt), None)
            }
        };
        if let Some(cleanup) = cleanup {
            Self::enqueue_cleanup(&self.inner, &self.supervisor, cleanup);
        } else {
            self.supervisor.emit((&transition).into());
        }
        Ok(transition)
    }

    fn acknowledge_with(
        &self,
        expected_revision: &str,
        acknowledge: impl FnOnce() -> Result<(), EngineError>,
        inspect: impl FnOnce() -> Result<NtfyDisclosureStatus, EngineError>,
        admit: impl FnOnce() -> Result<W, EngineError>,
    ) -> Result<AdmissionTransition, EngineError> {
        let (transition, cleanup) = {
            let mut inner = self.inner.lock().map_err(|_| {
                EngineError::host("host_error", "Configuration admission coordinator stopped")
            })?;
            if inner.state == AdmissionState::Mutating {
                return Ok(AdmissionTransition {
                    status: ConfigAdmissionStatus::ManualRepairRequired,
                    generation: inner.generation,
                });
            }
            if inner.state == AdmissionState::Admitted
                && let Some(workers) = inner.workers.as_ref()
            {
                if workers.revalidate().is_ok() {
                    (
                        AdmissionTransition {
                            status: ConfigAdmissionStatus::Admitted,
                            generation: inner.generation,
                        },
                        None,
                    )
                } else {
                    let cleanup = Self::take_for_revocation(&mut inner);
                    (
                        AdmissionTransition {
                            status: ConfigAdmissionStatus::ManualRepairRequired,
                            generation: inner.generation,
                        },
                        cleanup,
                    )
                }
            } else {
                let revision_matches = match &inner.state {
                    AdmissionState::AwaitingAcknowledgement {
                        expected_revision: current,
                    } => current == expected_revision,
                    _ => false,
                };
                Self::next_generation(&mut inner)?;
                inner.state = AdmissionState::Inspecting;
                let attempt = if revision_matches {
                    run_acknowledgement_attempt(acknowledge, inspect, admit)
                } else {
                    run_admission_attempt(inspect, admit)
                };
                (Self::install_attempt(&mut inner, attempt), None)
            }
        };
        if let Some(cleanup) = cleanup {
            Self::enqueue_cleanup(&self.inner, &self.supervisor, cleanup);
        } else {
            self.supervisor.emit((&transition).into());
        }
        Ok(transition)
    }
}

fn stage_admitted_workers(
    app: &AppHandle,
    engine: &Engine,
    startup_delivery: &NotificationDelivery,
    observer: AdmissionErrorObserver,
) -> Result<AdmissionWorkers, EngineError> {
    let snapshot = engine.admission_snapshot()?;
    let settings = snapshot.settings;
    let cancellation = CancellationToken::new();
    let scheduler = PollScheduler::with_cancellation(
        settings.poll_interval_minutes,
        settings.polling_supported,
        cancellation.clone(),
    );
    if !settings.polling_supported {
        eprintln!("watcher automatic polling is disabled for the current host configuration");
    }
    let (connect_queue, scheduler) = stage_worker_pair(
        || {
            ConnectQueueScheduler::stage_with_cancellation(
                app.clone(),
                engine.clone(),
                cancellation.clone(),
            )
            .map_err(|_| {
                EngineError::host("host_error", "Connect queue scheduler could not be started")
            })
        },
        |connect_queue| {
            scheduler
                .stage(
                    app.clone(),
                    engine.clone(),
                    startup_delivery.clone(),
                    connect_queue.clone(),
                )
                .map_err(|_| {
                    EngineError::host("host_error", "Polling scheduler could not be started")
                })?;
            Ok(scheduler)
        },
    )?;
    let startup_delivery = match StartupDeliveryWorker::stage(
        engine.clone(),
        startup_delivery.clone(),
        cancellation.clone(),
    ) {
        Ok(worker) => worker,
        Err(_) => {
            StagedWorker::shutdown(&scheduler);
            StagedWorker::shutdown(&connect_queue);
            return Err(EngineError::host(
                "host_error",
                "Startup notification delivery worker could not be started",
            ));
        }
    };
    Ok(AdmissionWorkers {
        admission_engine: engine.clone(),
        admission_token: snapshot.token,
        observer,
        cancellation,
        connect_queue,
        scheduler,
        startup_delivery,
    })
}

impl AdmissionCoordinator<AdmissionWorkers> {
    fn refresh_admission(
        &self,
        app: &AppHandle,
        engine: &Engine,
        delivery: &NotificationDelivery,
    ) -> Result<ConfigAdmissionUpdate, EngineError> {
        let observer = self.engine_error_observer();
        let transition = self.refresh_with(
            || engine.ntfy_disclosure_status(),
            || stage_admitted_workers(app, engine, delivery, observer),
        )?;
        Ok((&transition).into())
    }

    fn initialize(
        &self,
        app: &AppHandle,
        engine: &Engine,
        delivery: &NotificationDelivery,
        timezone: String,
        model_base_url: String,
        model_name: String,
    ) -> Result<ConfigAdmissionUpdate, EngineError> {
        let observer = self.engine_error_observer();
        let (initialization, transition) = {
            let mut inner = self.inner.lock().map_err(|_| {
                EngineError::host("host_error", "Configuration admission coordinator stopped")
            })?;
            if inner.state != AdmissionState::Missing || inner.workers.is_some() {
                return Err(EngineError::host(
                    "configuration_not_admitted",
                    "Watcher configuration cannot be initialized in its current state",
                ));
            }
            Self::next_generation(&mut inner)?;
            let initialization = engine.initialize_config(timezone, model_base_url, model_name);
            let attempt = run_admission_attempt(
                || engine.ntfy_disclosure_status(),
                || stage_admitted_workers(app, engine, delivery, observer),
            );
            (initialization, Self::install_attempt(&mut inner, attempt))
        };
        let update: ConfigAdmissionUpdate = (&transition).into();
        self.supervisor.emit(update.clone());
        match initialization {
            Ok(_) => Ok(update),
            Err(_) if update.is_admitted() => Ok(update),
            Err(error) => Err(error),
        }
    }

    fn acknowledge(
        &self,
        app: &AppHandle,
        engine: &Engine,
        delivery: &NotificationDelivery,
        expected_revision: String,
    ) -> Result<ConfigAdmissionUpdate, EngineError> {
        let observer = self.engine_error_observer();
        let acknowledgement_revision = expected_revision.clone();
        let transition = self.acknowledge_with(
            &expected_revision,
            || engine.acknowledge_ntfy_disclosure(acknowledgement_revision),
            || engine.ntfy_disclosure_status(),
            || stage_admitted_workers(app, engine, delivery, observer),
        )?;
        Ok((&transition).into())
    }

    fn mutate_config<T>(
        &self,
        app: &AppHandle,
        engine: &Engine,
        delivery: &NotificationDelivery,
        mutate: impl FnOnce(&Engine) -> Result<T, EngineError>,
    ) -> Result<T, EngineError> {
        let observer = self.engine_error_observer();
        self.mutate_with(
            || mutate(engine),
            || stage_admitted_workers(app, engine, delivery, observer),
        )
    }

    fn wake_connect_queue(&self) -> Result<(), EngineError> {
        let inner = self.inner.lock().map_err(|_| {
            EngineError::host("host_error", "Configuration admission coordinator stopped")
        })?;
        let workers = inner.workers.as_ref().ok_or_else(|| {
            EngineError::host(
                "configuration_not_admitted",
                "Watcher configuration is not admitted",
            )
        })?;
        workers.connect_queue.wake();
        Ok(())
    }

    fn polling_status(&self) -> Result<PollingStatus, EngineError> {
        let inner = self.inner.lock().map_err(|_| {
            EngineError::host("host_error", "Configuration admission coordinator stopped")
        })?;
        let workers = inner.workers.as_ref().ok_or_else(|| {
            EngineError::host(
                "configuration_not_admitted",
                "Watcher configuration is not admitted",
            )
        })?;
        Ok(workers.scheduler.status())
    }
}

#[tauri::command]
async fn config_initialize(
    app: AppHandle,
    engine: State<'_, Engine>,
    delivery: State<'_, NotificationDelivery>,
    admission: State<'_, AdmissionCoordinator>,
    timezone: String,
    model_base_url: String,
    model_name: String,
) -> Result<ConfigAdmissionUpdate, EngineError> {
    let engine = engine.inner().clone();
    let delivery = delivery.inner().clone();
    let admission = AdmissionCoordinator::clone(&*admission);
    tauri::async_runtime::spawn_blocking(move || {
        admission.initialize(
            &app,
            &engine,
            &delivery,
            timezone,
            model_base_url,
            model_name,
        )
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn config_admission_status(
    app: AppHandle,
    engine: State<'_, Engine>,
    delivery: State<'_, NotificationDelivery>,
    admission: State<'_, AdmissionCoordinator>,
) -> Result<ConfigAdmissionUpdate, EngineError> {
    let engine = engine.inner().clone();
    let delivery = delivery.inner().clone();
    let admission = AdmissionCoordinator::clone(&*admission);
    tauri::async_runtime::spawn_blocking(move || {
        admission.refresh_admission(&app, &engine, &delivery)
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn config_ntfy_disclosure_acknowledge(
    app: AppHandle,
    engine: State<'_, Engine>,
    delivery: State<'_, NotificationDelivery>,
    admission: State<'_, AdmissionCoordinator>,
    expected_revision: String,
) -> Result<ConfigAdmissionUpdate, EngineError> {
    let engine = engine.inner().clone();
    let delivery = delivery.inner().clone();
    let admission = AdmissionCoordinator::clone(&*admission);
    tauri::async_runtime::spawn_blocking(move || {
        admission.acknowledge(&app, &engine, &delivery, expected_revision)
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn inbox_query(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
    query: InboxQuery,
) -> Result<InboxPage, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.query_inbox(query))
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn inbox_delete(
    engine: State<'_, Engine>,
    delivery: State<'_, NotificationDelivery>,
    admission: State<'_, AdmissionCoordinator>,
    message_id: String,
) -> Result<(), EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    let delivery = delivery.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        delivery.run_engine_exclusive(&engine, |engine| engine.delete_inbox_item(message_id))
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn inbox_clear(
    engine: State<'_, Engine>,
    delivery: State<'_, NotificationDelivery>,
    admission: State<'_, AdmissionCoordinator>,
) -> Result<u64, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    let delivery = delivery.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        delivery.run_engine_exclusive(&engine, Engine::clear_inbox)
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn analysis_requeue(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
    message_id: String,
) -> Result<(), EngineError> {
    let _admission_permit = admission.require_admitted()?;
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
    admission: State<'_, AdmissionCoordinator>,
    message_id: String,
    part_id: String,
) -> Result<OpenedAttachment, EngineError> {
    let _admission_permit = admission.require_admitted()?;
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
    admission: State<'_, AdmissionCoordinator>,
    message_id: String,
    part_id: String,
) -> Result<ConnectCapabilities, EngineError> {
    let _admission_permit = admission.require_admitted()?;
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
    admission: State<'_, AdmissionCoordinator>,
    request_id: String,
    message_id: String,
    part_id: String,
    provider: ConnectProviderIdentity,
    capability: ConnectCapabilityRef,
    parameters: BTreeMap<String, Value>,
    confirmed: bool,
) -> Result<ConnectInvocationResult, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        engine.invoke_attachment_capability(
            request_id, message_id, part_id, provider, capability, parameters, confirmed,
        )
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"));
    admission.wake_connect_queue()?;
    result?
}

#[tauri::command]
async fn capability_output_present(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
    message_id: String,
    part_id: String,
    job_id: String,
    artifact_id: String,
) -> Result<ConnectOutputView, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        engine.present_capability_output(message_id, part_id, job_id, artifact_id)
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
// Tauri deserializes these named fields directly from the frozen frontend command.
#[allow(clippy::too_many_arguments)]
async fn capability_output_export(
    app: AppHandle,
    engine: State<'_, Engine>,
    exports: State<'_, AttachmentExports>,
    admission: State<'_, AdmissionCoordinator>,
    message_id: String,
    part_id: String,
    job_id: String,
    artifact_id: String,
) -> Result<RevealedCapabilityOutput, EngineError> {
    let _admission_permit = admission.require_admitted()?;
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
    admission: State<'_, AdmissionCoordinator>,
) -> Result<DesktopHealthStatus, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    let polling = admission.polling_status()?;
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
    admission: State<'_, AdmissionCoordinator>,
) -> Result<ConnectEntitlementStatus, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.connect_entitlement_status())
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

fn wake_connect_queue_after_entitlement_install<T, E>(
    result: Result<T, E>,
    wake: impl FnOnce(),
) -> Result<T, E> {
    if result.is_ok() {
        wake();
    }
    result
}

#[tauri::command]
async fn connect_entitlement_install(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
    source_path: String,
) -> Result<ConnectEntitlementStatus, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        engine.install_connect_entitlement(PathBuf::from(source_path))
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?;
    wake_connect_queue_after_entitlement_install(result, || {
        let _ = admission.wake_connect_queue();
    })
}

#[tauri::command]
async fn calendar_consent_status(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
    profile: CalendarConsentProfile,
    provider: String,
    account_id: String,
) -> Result<CalendarConsentStatus, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        engine.calendar_consent_status(profile, provider, account_id)
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn calendar_consent_connect(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
    profile: CalendarConsentProfile,
    provider: String,
    account_id: String,
) -> Result<CalendarConsentStatus, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        engine.connect_calendar_consent(profile, provider, account_id)
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn calendar_consent_disconnect(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
    profile: CalendarConsentProfile,
    provider: String,
    account_id: String,
) -> Result<CalendarConsentStatus, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        engine.disconnect_calendar_consent(profile, provider, account_id)
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
#[allow(clippy::too_many_arguments)]
async fn calendar_proposal_decide(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
    message_id: String,
    run_id: String,
    state_version: i64,
    proposal_version: i64,
    proposal_sha256: String,
    decision: String,
) -> Result<CalendarDecisionResult, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        engine.decide_calendar_proposal(
            message_id,
            run_id,
            state_version,
            proposal_version,
            proposal_sha256,
            decision,
        )
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn gmail_authorize(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
) -> Result<GmailAuthorization, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.authorize_gmail())
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn mail_accounts_list(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
) -> Result<MailAccounts, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.mail_accounts())
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn mail_account_connect(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
    provider: String,
    connection: Option<MailServerConnection>,
) -> Result<MailAccountResult, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.connect_mail_provider(provider, connection))
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn mail_account_reconnect(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
    provider: String,
    account_id: String,
    connection: Option<MailServerConnection>,
) -> Result<MailAccountResult, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        engine.reconnect_mail_account(provider, account_id, connection)
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn mail_account_disconnect(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
    provider: String,
    account_id: String,
) -> Result<MailAccountResult, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        engine.disconnect_mail_account(provider, account_id)
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn mail_account_activate(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
    provider: String,
    account_id: String,
) -> Result<MailAccountResult, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.activate_mail_account(provider, account_id))
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn settings_get(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
) -> Result<EngineSettings, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.settings())
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
// Tauri deserializes these named fields directly from the frozen frontend command.
#[allow(clippy::too_many_arguments)]
async fn settings_update(
    app: AppHandle,
    engine: State<'_, Engine>,
    delivery: State<'_, NotificationDelivery>,
    admission: State<'_, AdmissionCoordinator>,
    poll_interval_minutes: u64,
    retention_days: u64,
    notifications_enabled: bool,
    model_base_url: Option<String>,
    model_name: Option<String>,
) -> Result<EngineSettings, EngineError> {
    let admission = AdmissionCoordinator::clone(&*admission);
    let engine = engine.inner().clone();
    let delivery = delivery.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        admission.mutate_config(&app, &engine, &delivery, |engine| {
            delivery.run_engine_exclusive(engine, |engine| {
                engine.update_settings(
                    poll_interval_minutes,
                    retention_days,
                    notifications_enabled,
                    model_base_url,
                    model_name,
                )
            })
        })
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn watcher_check(
    engine: State<'_, Engine>,
    delivery: State<'_, NotificationDelivery>,
    admission: State<'_, AdmissionCoordinator>,
) -> Result<DesktopCheckResult, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    let delivery = delivery.inner().clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        let outcome = delivery.check_and_deliver(&engine)?;
        Ok(DesktopCheckResult {
            check: outcome.check,
            delivered_notifications: outcome.delivery.delivered,
            failed_notifications: outcome.delivery.failed,
            remaining_notifications: outcome.delivery.remaining,
        })
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"));
    admission.wake_connect_queue()?;
    result?
}

#[tauri::command]
async fn watchlist_list(
    engine: State<'_, Engine>,
    admission: State<'_, AdmissionCoordinator>,
) -> Result<Vec<WatchedSender>, EngineError> {
    let _admission_permit = admission.require_admitted()?;
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || engine.list())
        .await
        .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn watchlist_add(
    app: AppHandle,
    engine: State<'_, Engine>,
    delivery: State<'_, NotificationDelivery>,
    admission: State<'_, AdmissionCoordinator>,
    email: String,
    name: Option<String>,
) -> Result<WatchedSender, EngineError> {
    let admission = AdmissionCoordinator::clone(&*admission);
    let engine = engine.inner().clone();
    let delivery = delivery.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        admission.mutate_config(&app, &engine, &delivery, |engine| engine.add(email, name))
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[tauri::command]
async fn watchlist_remove(
    app: AppHandle,
    engine: State<'_, Engine>,
    delivery: State<'_, NotificationDelivery>,
    admission: State<'_, AdmissionCoordinator>,
    email: String,
) -> Result<WatchedSender, EngineError> {
    let admission = AdmissionCoordinator::clone(&*admission);
    let engine = engine.inner().clone();
    let delivery = delivery.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        admission.mutate_config(&app, &engine, &delivery, |engine| engine.remove(email))
    })
    .await
    .map_err(|_| EngineError::host("host_error", "Watcher engine worker stopped"))?
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    #[cfg(desktop)]
    if delivery::notification_helper_requested(std::env::args_os()) {
        if delivery::run_notification_helper().is_err() {
            eprintln!("watcher notification helper failed");
            std::process::exit(2);
        }
        return;
    }
    #[cfg(desktop)]
    let launch_in_background = starts_in_background(std::env::args_os());
    let builder = tauri::Builder::default();
    #[cfg(desktop)]
    let builder = builder.plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
        if !starts_in_background(args.iter()) {
            show_main_window(app);
        }
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
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .setup(move |app| {
            #[cfg(desktop)]
            install_tray(app)?;
            #[cfg(desktop)]
            if let Err(error) = app.handle().plugin(tauri_plugin_autostart::init(
                tauri_plugin_autostart::MacosLauncher::LaunchAgent,
                Some(vec![AUTOSTART_BACKGROUND_ARG]),
            )) {
                eprintln!("watcher start-on-login support is unavailable: {error}");
            }
            #[cfg(desktop)]
            if launch_in_background
                && let Some(window) = app.get_webview_window("main")
                && let Err(error) = window.hide()
            {
                eprintln!("watcher main window could not start hidden: {error}");
            }
            let engine = Engine::for_app(app.handle())?;
            let delivery = NotificationDelivery::default();
            let exports = AttachmentExports {
                directory: tempfile::Builder::new()
                    .prefix("email-watcher-attachments-")
                    .tempdir()?,
            };
            let admission_events = app.handle().clone();
            let admission = AdmissionCoordinator::with_event_sink(Arc::new(move |update| {
                if admission_events
                    .emit(CONFIG_ADMISSION_EVENT, update)
                    .is_err()
                {
                    eprintln!(
                        "Configuration admission change could not refresh the desktop window"
                    );
                }
            }))?;
            let admission_status = admission
                .refresh_admission(app.handle(), &engine, &delivery)
                .map_err(|_| std::io::Error::other("configuration admission could not start"))?;
            #[cfg(desktop)]
            if launch_in_background && !admission_status.is_admitted() {
                show_main_window(app.handle());
            }
            app.manage(engine.clone());
            app.manage(delivery.clone());
            app.manage(exports);
            app.manage(admission);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            analysis_requeue,
            attachment_capabilities,
            attachment_capability_invoke,
            attachment_open,
            autostart_get,
            autostart_set,
            calendar_consent_connect,
            calendar_consent_disconnect,
            calendar_consent_status,
            calendar_proposal_decide,
            capability_output_export,
            capability_output_present,
            connect_entitlement_install,
            connect_entitlement_status,
            config_admission_status,
            config_initialize,
            config_ntfy_disclosure_acknowledge,
            gmail_authorize,
            health_get,
            inbox_clear,
            inbox_delete,
            inbox_query,
            mail_account_activate,
            mail_account_connect,
            mail_account_disconnect,
            mail_account_reconnect,
            mail_accounts_list,
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
    use std::cell::RefCell;
    #[cfg(unix)]
    use std::ffi::OsString;
    #[cfg(unix)]
    use std::fs;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::mpsc;
    use std::thread;
    #[cfg(unix)]
    use std::time::Instant;

    #[cfg(unix)]
    fn wait_for_process_id(path: &Path) -> i32 {
        for _ in 0..100 {
            if let Ok(value) = fs::read_to_string(path)
                && let Ok(process_id) = value.trim().parse()
            {
                return process_id;
            }
            thread::sleep(Duration::from_millis(10));
        }
        panic!("startup delivery probe did not record its process id");
    }

    #[cfg(unix)]
    fn assert_process_stopped(process_id: i32) {
        for _ in 0..100 {
            // SAFETY: signal 0 only inspects the disposable child PID recorded by
            // this test and does not signal an unrelated process.
            if unsafe { libc::kill(process_id, 0) } != 0 {
                return;
            }
            thread::sleep(Duration::from_millis(10));
        }
        panic!("cancelled startup engine process {process_id} is still running");
    }

    #[cfg(unix)]
    fn stalled_startup_engine(primary_id_path: &Path, descendant_id_path: &Path) -> Engine {
        Engine::with_command(
            "sh",
            vec![
                OsString::from("-c"),
                OsString::from(
                    r#"cat >/dev/null
echo $$ > "$1"
sleep 30 &
echo $! > "$2"
printf '%s\n' '{"protocol":1,"ok":true,"operation":"host.operation_lock","data":{"path":"/tmp/unused-operation.lock"}}'"#,
                ),
                OsString::from("startup-delivery-shutdown-probe"),
                primary_id_path.as_os_str().to_owned(),
                descendant_id_path.as_os_str().to_owned(),
            ],
            "unused.toml".into(),
        )
    }

    #[cfg(unix)]
    fn successful_delivery_engine(lock_path: &Path, ack_path: &Path, log_path: &Path) -> Engine {
        Engine::with_command(
            "sh",
            vec![
                OsString::from("-c"),
                OsString::from(
                    r#"request=$(cat)
case "$request" in
  *host.operation_lock*)
    printf '%s\n' 'host.operation_lock' >> "$3"
    printf '%s\n' "{\"protocol\":1,\"ok\":true,\"operation\":\"host.operation_lock\",\"data\":{\"path\":\"$1\"}}"
    ;;
  *notifications.pending_under_host_lock*)
    printf '%s\n' 'notifications.pending_under_host_lock' >> "$3"
    if [ -f "$2" ]; then
      printf '%s\n' '{"protocol":1,"ok":true,"operation":"notifications.pending_under_host_lock","data":{"items":[]}}'
    else
      printf '%s\n' '{"protocol":1,"ok":true,"operation":"notifications.pending_under_host_lock","data":{"items":[{"analysis_at":null,"body":"Private local summary","kind":"watched_sender","message_id":"message-1","priority":"default","revision":null,"subject_id":null,"subject_type":null,"title":"Watched sender"}]}}'
    fi
    ;;
  *notifications.ack*)
    printf '%s\n' 'notifications.ack' >> "$3"
    : > "$2"
    printf '%s\n' '{"protocol":1,"ok":true,"operation":"notifications.ack","data":{"status":"acknowledged"}}'
    ;;
  *notifications.count_under_host_lock*)
    printf '%s\n' 'notifications.count_under_host_lock' >> "$3"
    if [ -f "$2" ]; then count=0; else count=1; fi
    printf '%s\n' "{\"protocol\":1,\"ok\":true,\"operation\":\"notifications.count_under_host_lock\",\"data\":{\"count\":$count}}"
    ;;
  *) exit 2 ;;
esac"#,
                ),
                OsString::from("successful-startup-delivery-fixture"),
                lock_path.as_os_str().to_owned(),
                ack_path.as_os_str().to_owned(),
                log_path.as_os_str().to_owned(),
            ],
            "unused.toml".into(),
        )
    }

    #[derive(Clone)]
    struct ProbeStagedWorker {
        shutdowns: Arc<AtomicUsize>,
    }

    impl StagedWorker for ProbeStagedWorker {
        fn shutdown(&self) {
            self.shutdowns.fetch_add(1, Ordering::SeqCst);
        }
    }

    struct ProbeAdmissionWorkers {
        queue_activations: Arc<AtomicUsize>,
        poll_activations: Arc<AtomicUsize>,
        startup_delivery_activations: Arc<AtomicUsize>,
    }

    struct TokenProbeWorkers {
        token: AdmissionToken,
        held: Arc<AtomicUsize>,
        drops: Arc<AtomicUsize>,
    }

    fn probe_admission_token(revision: char, identity: char) -> AdmissionToken {
        AdmissionToken {
            version: 1,
            revision: format!("sha256:{}", revision.to_string().repeat(64)),
            identity: format!("sha256:{}", identity.to_string().repeat(64)),
        }
    }

    impl AdmissionWorkerSet for TokenProbeWorkers {
        fn admission_token(&self) -> Option<&AdmissionToken> {
            Some(&self.token)
        }

        fn begin_revocation(&self) {
            self.held.fetch_add(1, Ordering::SeqCst);
        }

        fn activate(&self) {}
    }

    impl Drop for TokenProbeWorkers {
        fn drop(&mut self) {
            self.drops.fetch_add(1, Ordering::SeqCst);
        }
    }

    #[cfg(unix)]
    fn inert_admission_engine() -> Engine {
        Engine::with_command("unused", Vec::new(), "unused.toml".into())
    }

    #[cfg(unix)]
    fn inert_admission_token() -> AdmissionToken {
        AdmissionToken {
            version: 1,
            revision: "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                .into(),
            identity: "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                .into(),
        }
    }

    #[cfg(unix)]
    fn inert_admission_observer() -> AdmissionErrorObserver {
        Arc::new(|_, _| {})
    }

    impl AdmissionWorkerSet for ProbeAdmissionWorkers {
        fn activate(&self) {
            self.queue_activations.fetch_add(1, Ordering::SeqCst);
            self.poll_activations.fetch_add(1, Ordering::SeqCst);
            self.startup_delivery_activations
                .fetch_add(1, Ordering::SeqCst);
        }
    }

    #[test]
    fn tray_menu_routing_is_explicit() {
        assert_eq!(tray_menu_action(TRAY_OPEN_ID), TrayMenuAction::Open);
        assert_eq!(tray_menu_action(TRAY_QUIT_ID), TrayMenuAction::Quit);
        assert_eq!(tray_menu_action("unexpected"), TrayMenuAction::Ignore);
    }

    #[test]
    fn background_launch_requires_the_exact_argument() {
        assert!(starts_in_background(["email-watcher", "--background"]));
        assert!(!starts_in_background(["email-watcher"]));
        assert!(!starts_in_background([
            "email-watcher",
            "--background=true"
        ]));
        assert!(!starts_in_background([
            "email-watcher",
            "prefix--background"
        ]));
    }

    #[test]
    fn disclosure_status_is_always_the_first_admission_operation() {
        let held_states = [
            (NtfyDisclosureStatus::Missing, AdmissionState::Missing),
            (
                NtfyDisclosureStatus::AcknowledgementRequired {
                    expected_revision:
                        "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                            .into(),
                },
                AdmissionState::AwaitingAcknowledgement {
                    expected_revision:
                        "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                            .into(),
                },
            ),
            (
                NtfyDisclosureStatus::ManualRepairRequired,
                AdmissionState::ManualRepairRequired,
            ),
        ];
        for (status, expected) in held_states {
            let events = RefCell::new(Vec::new());
            let attempt = run_admission_attempt(
                || {
                    events.borrow_mut().push("status");
                    Ok(status)
                },
                || {
                    events.borrow_mut().push("settings/workers");
                    Ok(())
                },
            );
            assert_eq!(attempt.state, expected);
            assert!(attempt.workers.is_none());
            assert_eq!(*events.borrow(), ["status"]);
        }

        let events = RefCell::new(Vec::new());
        let admitted = run_admission_attempt(
            || {
                events.borrow_mut().push("status");
                Ok(NtfyDisclosureStatus::NormalAdmission)
            },
            || {
                events.borrow_mut().push("settings/workers");
                Ok(())
            },
        );
        assert_eq!(admitted.state, AdmissionState::Admitted);
        assert!(admitted.workers.is_some());
        assert_eq!(*events.borrow(), ["status", "settings/workers"]);
    }

    #[test]
    fn admission_errors_hold_workers_and_expose_only_manual_repair() {
        let status_error = run_admission_attempt::<()>(
            || Err(EngineError::host("engine_timeout", "private diagnostic")),
            || panic!("workers must remain held after a status error"),
        );
        assert_eq!(status_error.state, AdmissionState::ManualRepairRequired);
        assert!(status_error.workers.is_none());
        assert_eq!(
            status_error.state.public_status(),
            ConfigAdmissionStatus::ManualRepairRequired
        );

        let admission_error = run_admission_attempt::<()>(
            || Ok(NtfyDisclosureStatus::NormalAdmission),
            || {
                Err(EngineError::host(
                    "configuration_error",
                    "private diagnostic",
                ))
            },
        );
        assert_eq!(admission_error.state, AdmissionState::ManualRepairRequired);
        assert!(admission_error.workers.is_none());
    }

    #[test]
    fn poll_spawn_failure_rolls_back_staged_queue_without_work() {
        let queue_shutdowns = Arc::new(AtomicUsize::new(0));
        let poll_stage_attempts = Arc::new(AtomicUsize::new(0));

        let result = stage_worker_pair(
            || {
                Ok::<_, &'static str>(ProbeStagedWorker {
                    shutdowns: Arc::clone(&queue_shutdowns),
                })
            },
            |_| {
                poll_stage_attempts.fetch_add(1, Ordering::SeqCst);
                Err::<(), _>("poll spawn failed")
            },
        );

        assert!(result.is_err());
        assert_eq!(poll_stage_attempts.load(Ordering::SeqCst), 1);
        assert_eq!(queue_shutdowns.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn queue_spawn_failure_never_stages_poll() {
        let poll_stage_attempts = Arc::new(AtomicUsize::new(0));
        let result = stage_worker_pair::<ProbeStagedWorker, (), _>(
            || Err("queue spawn failed"),
            |_| {
                poll_stage_attempts.fetch_add(1, Ordering::SeqCst);
                Ok(())
            },
        );

        assert!(result.is_err());
        assert_eq!(poll_stage_attempts.load(Ordering::SeqCst), 0);
    }

    #[cfg(unix)]
    #[test]
    fn quit_during_startup_delivery_kills_child_releases_lock_and_allows_restart() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let primary_id_path = directory.path().join("startup-delivery-primary.pid");
        let descendant_id_path = directory.path().join("startup-delivery-descendant.pid");
        let delivery = NotificationDelivery::default();
        let cancellation = CancellationToken::new();
        let connect_queue =
            ConnectQueueScheduler::stage_probe_with_cancellation(cancellation.clone(), |token| {
                while !token.is_cancelled() {
                    thread::yield_now();
                }
            })
            .expect("stage Connect queue");
        let scheduler = PollScheduler::with_cancellation(1, true, cancellation.clone());
        scheduler
            .stage_probe(|token| {
                while !token.is_cancelled() {
                    thread::yield_now();
                }
            })
            .expect("stage polling");
        let startup_delivery = StartupDeliveryWorker::stage(
            stalled_startup_engine(&primary_id_path, &descendant_id_path),
            delivery.clone(),
            cancellation.clone(),
        )
        .expect("stage startup delivery");
        let queue_probe = connect_queue.clone();
        let poll_probe = scheduler.clone();
        let startup_probe = startup_delivery.clone();
        let workers = AdmissionWorkers {
            admission_engine: inert_admission_engine(),
            admission_token: inert_admission_token(),
            observer: inert_admission_observer(),
            cancellation,
            connect_queue,
            scheduler,
            startup_delivery,
        };
        workers.activate();
        let primary_id = wait_for_process_id(&primary_id_path);
        let descendant_id = wait_for_process_id(&descendant_id_path);
        assert_process_stopped(primary_id);
        // SAFETY: signal 0 only inspects the disposable pipe-holding descendant.
        assert_eq!(unsafe { libc::kill(descendant_id, 0) }, 0);

        let started = Instant::now();
        drop(workers);

        assert!(started.elapsed() < Duration::from_secs(1));
        assert_process_stopped(descendant_id);
        assert!(startup_probe.is_joined());
        assert!(poll_probe.is_joined());
        assert!(queue_probe.is_joined());
        delivery
            .run_exclusive_with_timeout(Duration::from_millis(100), |_| Ok(()))
            .expect("delivery lock is released after startup shutdown");

        let lock_path = directory.path().join("operation.lock");
        let ack_path = directory.path().join("acknowledged");
        let operation_log = directory.path().join("operations.log");
        let accepted_log = directory.path().join("accepted.log");
        let fixture_delivery = NotificationDelivery::with_notification_command(
            "sh",
            vec![
                OsString::from("-c"),
                OsString::from("cat >/dev/null; printf '%s\\n' accepted >> \"$1\""),
                OsString::from("successful-notification-fixture"),
                accepted_log.as_os_str().to_owned(),
            ],
        );
        let restarted = StartupDeliveryWorker::stage(
            successful_delivery_engine(&lock_path, &ack_path, &operation_log),
            fixture_delivery.clone(),
            CancellationToken::new(),
        )
        .expect("stage fresh startup delivery");
        restarted.activate();
        restarted.join().expect("fresh startup delivery joins");
        assert!(restarted.is_joined());
        assert!(ack_path.is_file(), "accepted notification is acknowledged");
        assert_eq!(
            fs::read_to_string(&accepted_log).expect("read platform acceptance log"),
            "accepted\n"
        );

        let subsequent = StartupDeliveryWorker::stage(
            successful_delivery_engine(&lock_path, &ack_path, &operation_log),
            fixture_delivery,
            CancellationToken::new(),
        )
        .expect("stage subsequent startup delivery");
        subsequent.activate();
        subsequent
            .join()
            .expect("subsequent startup delivery joins");
        assert!(subsequent.is_joined());
        assert_eq!(
            fs::read_to_string(&accepted_log).expect("read deduplicated acceptance log"),
            "accepted\n"
        );
        let operations = fs::read_to_string(&operation_log).expect("read engine operation log");
        assert_eq!(operations.matches("host.operation_lock").count(), 2);
        assert_eq!(
            operations
                .matches("notifications.pending_under_host_lock")
                .count(),
            2
        );
        assert_eq!(operations.matches("notifications.ack").count(), 1);
        assert_eq!(
            operations
                .matches("notifications.count_under_host_lock")
                .count(),
            2
        );
    }

    #[cfg(unix)]
    #[test]
    fn admission_drop_signals_all_three_workers_before_deterministic_join() {
        let cancellation = CancellationToken::new();
        let (started_sender, started_receiver) = mpsc::channel();
        let (stopped_sender, stopped_receiver) = mpsc::channel();

        let queue_started = started_sender.clone();
        let queue_stopped = stopped_sender.clone();
        let connect_queue = ConnectQueueScheduler::stage_probe_with_cancellation(
            cancellation.clone(),
            move |token| {
                queue_started.send("queue").expect("queue started");
                while !token.is_cancelled() {
                    thread::yield_now();
                }
                queue_stopped.send("queue").expect("queue stopped");
            },
        )
        .expect("stage queue");

        let scheduler = PollScheduler::with_cancellation(1, true, cancellation.clone());
        let poll_started = started_sender.clone();
        let poll_stopped = stopped_sender.clone();
        scheduler
            .stage_probe(move |token| {
                poll_started.send("poll").expect("poll started");
                while !token.is_cancelled() {
                    thread::yield_now();
                }
                poll_stopped.send("poll").expect("poll stopped");
            })
            .expect("stage poll");

        let startup_delivery =
            StartupDeliveryWorker::stage_probe(cancellation.clone(), move |token| {
                started_sender.send("startup").expect("startup started");
                while !token.is_cancelled() {
                    thread::yield_now();
                }
                stopped_sender.send("startup").expect("startup stopped");
            })
            .expect("stage startup delivery");

        let queue_probe = connect_queue.clone();
        let poll_probe = scheduler.clone();
        let startup_probe = startup_delivery.clone();
        let workers = AdmissionWorkers {
            admission_engine: inert_admission_engine(),
            admission_token: inert_admission_token(),
            observer: inert_admission_observer(),
            cancellation,
            connect_queue,
            scheduler,
            startup_delivery,
        };
        workers.activate();
        let mut started = [
            started_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("first worker starts"),
            started_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("second worker starts"),
            started_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("third worker starts"),
        ];
        started.sort_unstable();
        assert_eq!(started, ["poll", "queue", "startup"]);

        let stopped_at = Instant::now();
        drop(workers);
        assert!(stopped_at.elapsed() < Duration::from_secs(1));
        let mut stopped = [
            stopped_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("first worker stops"),
            stopped_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("second worker stops"),
            stopped_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("third worker stops"),
        ];
        stopped.sort_unstable();
        assert_eq!(stopped, ["poll", "queue", "startup"]);
        assert!(startup_probe.is_joined());
        assert!(poll_probe.is_joined());
        assert!(queue_probe.is_joined());
    }

    #[cfg(unix)]
    #[test]
    fn cancellation_racing_activation_runs_no_worker_and_leaves_no_handle() {
        let cancellation = CancellationToken::new();
        let work = Arc::new(AtomicUsize::new(0));
        let queue_work = Arc::clone(&work);
        let connect_queue =
            ConnectQueueScheduler::stage_probe_with_cancellation(cancellation.clone(), move |_| {
                queue_work.fetch_add(1, Ordering::SeqCst);
            })
            .expect("stage queue");
        let scheduler = PollScheduler::with_cancellation(1, true, cancellation.clone());
        let poll_work = Arc::clone(&work);
        scheduler
            .stage_probe(move |_| {
                poll_work.fetch_add(1, Ordering::SeqCst);
            })
            .expect("stage poll");
        let startup_work = Arc::clone(&work);
        let startup_delivery =
            StartupDeliveryWorker::stage_probe(cancellation.clone(), move |_| {
                startup_work.fetch_add(1, Ordering::SeqCst);
            })
            .expect("stage startup delivery");
        let queue_probe = connect_queue.clone();
        let poll_probe = scheduler.clone();
        let startup_probe = startup_delivery.clone();
        let workers = AdmissionWorkers {
            admission_engine: inert_admission_engine(),
            admission_token: inert_admission_token(),
            observer: inert_admission_observer(),
            cancellation: cancellation.clone(),
            connect_queue,
            scheduler,
            startup_delivery,
        };

        cancellation.cancel();
        workers.activate();
        drop(workers);

        assert_eq!(work.load(Ordering::SeqCst), 0);
        assert!(startup_probe.is_joined());
        assert!(poll_probe.is_joined());
        assert!(queue_probe.is_joined());
    }

    #[test]
    fn repeated_admission_refresh_activates_one_worker_pair_and_one_delivery() {
        let queue_activations = Arc::new(AtomicUsize::new(0));
        let poll_activations = Arc::new(AtomicUsize::new(0));
        let startup_delivery_activations = Arc::new(AtomicUsize::new(0));
        let admission = AdmissionCoordinator::<ProbeAdmissionWorkers>::new();

        let first = admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(ProbeAdmissionWorkers {
                        queue_activations: Arc::clone(&queue_activations),
                        poll_activations: Arc::clone(&poll_activations),
                        startup_delivery_activations: Arc::clone(&startup_delivery_activations),
                    })
                },
            )
            .expect("first admission succeeds");
        let second = admission
            .refresh_with(
                || panic!("admitted refresh must not inspect again"),
                || panic!("admitted refresh must not stage workers again"),
            )
            .expect("repeated admission succeeds");

        assert_eq!(first.status, ConfigAdmissionStatus::Admitted);
        assert_eq!(second.status, ConfigAdmissionStatus::Admitted);
        assert_eq!(queue_activations.load(Ordering::SeqCst), 1);
        assert_eq!(poll_activations.load(Ordering::SeqCst), 1);
        assert_eq!(startup_delivery_activations.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn stale_snapshot_drops_staged_workers_before_any_activation() {
        struct StaleWorkers {
            activations: Arc<AtomicUsize>,
            drops: Arc<AtomicUsize>,
        }

        impl AdmissionWorkerSet for StaleWorkers {
            fn revalidate(&self) -> Result<(), EngineError> {
                Err(EngineError::host(
                    "conflict",
                    "Configuration admission snapshot changed",
                ))
            }

            fn activate(&self) {
                self.activations.fetch_add(1, Ordering::SeqCst);
            }
        }

        impl Drop for StaleWorkers {
            fn drop(&mut self) {
                self.drops.fetch_add(1, Ordering::SeqCst);
            }
        }

        let activations = Arc::new(AtomicUsize::new(0));
        let drops = Arc::new(AtomicUsize::new(0));
        let admission = AdmissionCoordinator::<StaleWorkers>::new();

        let transition = admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(StaleWorkers {
                        activations: Arc::clone(&activations),
                        drops: Arc::clone(&drops),
                    })
                },
            )
            .expect("stale snapshot must reconcile to repair state");

        assert_eq!(
            transition.status,
            ConfigAdmissionStatus::ManualRepairRequired
        );
        assert_eq!(activations.load(Ordering::SeqCst), 0);
        assert_eq!(drops.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn normal_admission_compares_snapshot_before_worker_activation() {
        struct OrderedWorkers {
            events: Arc<Mutex<Vec<&'static str>>>,
        }

        impl AdmissionWorkerSet for OrderedWorkers {
            fn revalidate(&self) -> Result<(), EngineError> {
                self.events.lock().expect("events").push("compare");
                Ok(())
            }

            fn activate(&self) {
                self.events.lock().expect("events").push("activate");
            }
        }

        let events = Arc::new(Mutex::new(Vec::new()));
        let admission = AdmissionCoordinator::<OrderedWorkers>::new();
        let inspect_events = Arc::clone(&events);
        let stage_events = Arc::clone(&events);

        let transition = admission
            .refresh_with(
                move || {
                    inspect_events.lock().expect("events").push("status");
                    Ok(NtfyDisclosureStatus::NormalAdmission)
                },
                move || {
                    stage_events.lock().expect("events").push("snapshot/stage");
                    Ok(OrderedWorkers {
                        events: Arc::clone(&stage_events),
                    })
                },
            )
            .expect("unchanged atomic snapshot admits");

        assert_eq!(transition.status, ConfigAdmissionStatus::Admitted);
        assert_eq!(
            *events.lock().expect("events"),
            ["status", "snapshot/stage", "compare", "activate"]
        );
    }

    #[test]
    fn admitted_refresh_revokes_workers_when_retained_token_is_stale() {
        struct ExpiringWorkers {
            comparisons: AtomicUsize,
            activations: Arc<AtomicUsize>,
            drops: Arc<AtomicUsize>,
        }

        impl AdmissionWorkerSet for ExpiringWorkers {
            fn revalidate(&self) -> Result<(), EngineError> {
                if self.comparisons.fetch_add(1, Ordering::SeqCst) == 0 {
                    Ok(())
                } else {
                    Err(EngineError::host(
                        "conflict",
                        "Configuration admission snapshot changed",
                    ))
                }
            }

            fn activate(&self) {
                self.activations.fetch_add(1, Ordering::SeqCst);
            }
        }

        impl Drop for ExpiringWorkers {
            fn drop(&mut self) {
                self.drops.fetch_add(1, Ordering::SeqCst);
            }
        }

        let activations = Arc::new(AtomicUsize::new(0));
        let drops = Arc::new(AtomicUsize::new(0));
        let (event_sender, event_receiver) = mpsc::channel();
        let admission =
            AdmissionCoordinator::<ExpiringWorkers>::with_event_sink(Arc::new(move |event| {
                event_sender.send(event).expect("admission event")
            }))
            .expect("start revocation supervisor");
        let admitted = admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(ExpiringWorkers {
                        comparisons: AtomicUsize::new(0),
                        activations: Arc::clone(&activations),
                        drops: Arc::clone(&drops),
                    })
                },
            )
            .expect("initial current snapshot admits");
        event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("initial admitted event");
        let repaired = admission
            .refresh_with(
                || panic!("stale admitted refresh must not perform loose inspection"),
                || panic!("stale admitted refresh must not stage new workers"),
            )
            .expect("stale retained token reconciles");
        event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("manual repair event after cleanup");

        assert_eq!(admitted.status, ConfigAdmissionStatus::Admitted);
        assert_eq!(repaired.status, ConfigAdmissionStatus::ManualRepairRequired);
        assert_eq!(activations.load(Ordering::SeqCst), 1);
        assert_eq!(drops.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn worker_admission_conflict_holds_commands_before_supervised_cleanup() {
        struct HeldWorkers {
            token: AdmissionToken,
            held: Arc<AtomicUsize>,
            cleanup_started: mpsc::Sender<String>,
            cleanup_release: mpsc::Receiver<()>,
        }

        impl AdmissionWorkerSet for HeldWorkers {
            fn admission_token(&self) -> Option<&AdmissionToken> {
                Some(&self.token)
            }

            fn begin_revocation(&self) {
                self.held.fetch_add(1, Ordering::SeqCst);
            }

            fn activate(&self) {}
        }

        impl Drop for HeldWorkers {
            fn drop(&mut self) {
                self.cleanup_started
                    .send(thread::current().name().unwrap_or("unnamed").to_owned())
                    .expect("cleanup started");
                self.cleanup_release.recv().expect("cleanup released");
            }
        }

        let token = AdmissionToken {
            version: 1,
            revision: "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                .into(),
            identity: "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                .into(),
        };
        let held = Arc::new(AtomicUsize::new(0));
        let (cleanup_started_sender, cleanup_started_receiver) = mpsc::channel();
        let (cleanup_release_sender, cleanup_release_receiver) = mpsc::channel();
        let (event_sender, event_receiver) = mpsc::channel();
        let admission =
            AdmissionCoordinator::<HeldWorkers>::with_event_sink(Arc::new(move |event| {
                event_sender.send(event).expect("admission event");
            }))
            .expect("start revocation supervisor");
        let installed_token = token.clone();
        let transition = admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(HeldWorkers {
                        token: installed_token,
                        held: Arc::clone(&held),
                        cleanup_started: cleanup_started_sender,
                        cleanup_release: cleanup_release_receiver,
                    })
                },
            )
            .expect("admit probe workers");
        let admitted_event = event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("admitted event");
        assert_eq!(admitted_event.generation, transition.generation);
        assert_eq!(admitted_event.status, ConfigAdmissionStatus::Admitted);

        let observer = admission.engine_error_observer();
        observer(
            &token,
            &EngineError::host("conflict", "Configuration admission snapshot changed"),
        );

        assert_eq!(held.load(Ordering::SeqCst), 1);
        assert_eq!(
            admission.require_admitted().unwrap_err().code,
            "configuration_not_admitted"
        );
        let blocked_effects = AtomicUsize::new(0);
        let result = admission.require_admitted().map(|_permit| {
            blocked_effects.fetch_add(1, Ordering::SeqCst);
        });
        assert!(result.is_err());
        assert_eq!(blocked_effects.load(Ordering::SeqCst), 0);
        assert_eq!(
            cleanup_started_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("supervisor owns cleanup"),
            "email-watcher-admission-revocation"
        );
        observer(
            &token,
            &EngineError::host("configuration_error", "Configuration is unsafe"),
        );
        assert_eq!(held.load(Ordering::SeqCst), 1);
        drop(observer);
        let coordinator_shutdown = thread::spawn(move || drop(admission));
        cleanup_release_sender.send(()).expect("release cleanup");
        let event = event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("manual repair event");
        assert!(event.generation > transition.generation);
        assert_eq!(event.status, ConfigAdmissionStatus::ManualRepairRequired);
        coordinator_shutdown
            .join()
            .expect("coordinator shuts down without supervisor self-join");
    }

    #[cfg(unix)]
    #[test]
    fn guarded_engine_conflict_revokes_before_a_followup_command_can_run() {
        let token = probe_admission_token('5', '6');
        let held = Arc::new(AtomicUsize::new(0));
        let drops = Arc::new(AtomicUsize::new(0));
        let (event_sender, event_receiver) = mpsc::channel();
        let admission =
            AdmissionCoordinator::<TokenProbeWorkers>::with_event_sink(Arc::new(move |event| {
                event_sender.send(event).expect("admission event")
            }))
            .expect("start revocation supervisor");
        let worker_token = token.clone();
        admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(TokenProbeWorkers {
                        token: worker_token,
                        held: Arc::clone(&held),
                        drops: Arc::clone(&drops),
                    })
                },
            )
            .expect("admit probe workers");
        event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("admitted event");
        let engine = Engine::with_command(
            "sh",
            vec![
                OsString::from("-c"),
                OsString::from(
                    r#"cat >/dev/null
printf '%s\n' '{"protocol":1,"ok":false,"operation":"connect.queue.pump","error":{"code":"conflict","message":"Configuration admission snapshot changed"}}'"#,
                ),
            ],
            "unused.toml".into(),
        );
        engine.install_admission_binding(token, admission.engine_error_observer());

        assert_eq!(
            engine
                .pump_connect_queue()
                .expect_err("guarded request must report stale admission")
                .code,
            "conflict"
        );
        let followup_effects = AtomicUsize::new(0);
        assert!(
            admission
                .require_admitted()
                .map(|_permit| followup_effects.fetch_add(1, Ordering::SeqCst))
                .is_err()
        );
        assert_eq!(followup_effects.load(Ordering::SeqCst), 0);
        assert_eq!(
            event_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("manual repair event")
                .status,
            ConfigAdmissionStatus::ManualRepairRequired
        );
        assert_eq!(held.load(Ordering::SeqCst), 1);
        assert_eq!(drops.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn simultaneous_worker_conflicts_revoke_once_and_transient_errors_do_not_revoke() {
        let token = probe_admission_token('c', 'd');
        let held = Arc::new(AtomicUsize::new(0));
        let drops = Arc::new(AtomicUsize::new(0));
        let (event_sender, event_receiver) = mpsc::channel();
        let admission =
            AdmissionCoordinator::<TokenProbeWorkers>::with_event_sink(Arc::new(move |event| {
                event_sender.send(event).expect("admission event")
            }))
            .expect("start revocation supervisor");
        let worker_token = token.clone();
        admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(TokenProbeWorkers {
                        token: worker_token,
                        held: Arc::clone(&held),
                        drops: Arc::clone(&drops),
                    })
                },
            )
            .expect("admit probe workers");
        assert_eq!(
            event_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("admitted event")
                .status,
            ConfigAdmissionStatus::Admitted
        );
        let observer = admission.engine_error_observer();
        observer(
            &token,
            &EngineError::host(
                "provider_unavailable",
                "Provider is temporarily unavailable",
            ),
        );
        admission
            .require_admitted()
            .expect("transient provider failure remains admitted");
        assert!(event_receiver.try_recv().is_err());

        let barrier = Arc::new(std::sync::Barrier::new(4));
        let mut reporters = Vec::new();
        for code in ["conflict", "configuration_error", "conflict"] {
            let observer = Arc::clone(&observer);
            let token = token.clone();
            let barrier = Arc::clone(&barrier);
            reporters.push(thread::spawn(move || {
                barrier.wait();
                observer(
                    &token,
                    &EngineError::host(code, "Configuration admission changed"),
                );
            }));
        }
        barrier.wait();
        for reporter in reporters {
            reporter.join().expect("conflict reporter joins");
        }
        let repaired = event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("manual repair event");

        assert_eq!(repaired.status, ConfigAdmissionStatus::ManualRepairRequired);
        assert_eq!(held.load(Ordering::SeqCst), 1);
        assert_eq!(drops.load(Ordering::SeqCst), 1);
        assert!(event_receiver.try_recv().is_err());
    }

    #[test]
    fn conflict_racing_worker_activation_revokes_without_deadlock() {
        struct ActivationFaultWorkers {
            token: AdmissionToken,
            observer: AdmissionErrorObserver,
            held: Arc<AtomicUsize>,
            drops: Arc<AtomicUsize>,
        }

        impl AdmissionWorkerSet for ActivationFaultWorkers {
            fn admission_token(&self) -> Option<&AdmissionToken> {
                Some(&self.token)
            }

            fn begin_revocation(&self) {
                self.held.fetch_add(1, Ordering::SeqCst);
            }

            fn activate(&self) {
                let observer = Arc::clone(&self.observer);
                let token = self.token.clone();
                thread::Builder::new()
                    .name("activation-admission-conflict".into())
                    .spawn(move || {
                        observer(
                            &token,
                            &EngineError::host(
                                "conflict",
                                "Configuration admission changed during activation",
                            ),
                        );
                    })
                    .expect("spawn activation conflict");
            }
        }

        impl Drop for ActivationFaultWorkers {
            fn drop(&mut self) {
                self.drops.fetch_add(1, Ordering::SeqCst);
            }
        }

        let token = probe_admission_token('3', '4');
        let held = Arc::new(AtomicUsize::new(0));
        let drops = Arc::new(AtomicUsize::new(0));
        let (event_sender, event_receiver) = mpsc::channel();
        let admission = AdmissionCoordinator::<ActivationFaultWorkers>::with_event_sink(Arc::new(
            move |event| event_sender.send(event).expect("admission event"),
        ))
        .expect("start revocation supervisor");
        let observer = admission.engine_error_observer();
        let worker_token = token;
        let worker_observer = Arc::clone(&observer);
        let transition = admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(ActivationFaultWorkers {
                        token: worker_token,
                        observer: worker_observer,
                        held: Arc::clone(&held),
                        drops: Arc::clone(&drops),
                    })
                },
            )
            .expect("activation attempt returns");
        let first = event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("first activation event");
        let second = event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("second activation event");
        let repaired = [&first, &second]
            .into_iter()
            .find(|event| event.status == ConfigAdmissionStatus::ManualRepairRequired)
            .expect("manual repair event");

        assert_eq!(transition.status, ConfigAdmissionStatus::Admitted);
        assert!(repaired.generation > transition.generation);
        assert_eq!(held.load(Ordering::SeqCst), 1);
        assert_eq!(drops.load(Ordering::SeqCst), 1);
        assert_eq!(
            admission.require_admitted().unwrap_err().code,
            "configuration_not_admitted"
        );
    }

    #[test]
    fn explicit_readmission_ignores_stale_token_fault_and_installs_new_workers() {
        let old_token = probe_admission_token('e', 'f');
        let new_token = probe_admission_token('1', '2');
        let held = Arc::new(AtomicUsize::new(0));
        let drops = Arc::new(AtomicUsize::new(0));
        let (event_sender, event_receiver) = mpsc::channel();
        let admission =
            AdmissionCoordinator::<TokenProbeWorkers>::with_event_sink(Arc::new(move |event| {
                event_sender.send(event).expect("admission event")
            }))
            .expect("start revocation supervisor");
        let first_worker_token = old_token.clone();
        let first = admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(TokenProbeWorkers {
                        token: first_worker_token,
                        held: Arc::clone(&held),
                        drops: Arc::clone(&drops),
                    })
                },
            )
            .expect("admit first workers");
        event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("first admitted event");
        let observer = admission.engine_error_observer();
        observer(
            &old_token,
            &EngineError::host("conflict", "Configuration admission changed"),
        );
        event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("first repair event");

        let second_worker_token = new_token.clone();
        let second = admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(TokenProbeWorkers {
                        token: second_worker_token,
                        held: Arc::clone(&held),
                        drops: Arc::clone(&drops),
                    })
                },
            )
            .expect("admit replacement workers");
        let readmitted = event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("replacement admitted event");
        assert!(second.generation > first.generation);
        assert_eq!(readmitted.generation, second.generation);
        admission
            .require_admitted()
            .expect("replacement workers are admitted");

        observer(
            &old_token,
            &EngineError::host("configuration_error", "Old configuration is unsafe"),
        );

        admission
            .require_admitted()
            .expect("stale token fault cannot revoke replacement workers");
        assert!(event_receiver.try_recv().is_err());
        assert_eq!(held.load(Ordering::SeqCst), 1);
        assert_eq!(drops.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn config_mutation_quiesces_old_workers_before_commit_and_activates_new_token() {
        struct RacingMutationWorkers {
            token: AdmissionToken,
            stop: mpsc::Sender<()>,
            worker: Option<thread::JoinHandle<()>>,
            joined: Arc<AtomicUsize>,
        }

        impl AdmissionWorkerSet for RacingMutationWorkers {
            fn admission_token(&self) -> Option<&AdmissionToken> {
                Some(&self.token)
            }

            fn begin_revocation(&self) {
                let _ = self.stop.send(());
            }

            fn activate(&self) {}
        }

        impl Drop for RacingMutationWorkers {
            fn drop(&mut self) {
                self.begin_revocation();
                self.worker
                    .take()
                    .expect("mutation probe worker handle")
                    .join()
                    .expect("mutation probe worker joins");
                self.joined.fetch_add(1, Ordering::SeqCst);
            }
        }

        fn mutation_worker(
            token: AdmissionToken,
            observer: Option<AdmissionErrorObserver>,
            joined: Arc<AtomicUsize>,
            faults: Arc<AtomicUsize>,
        ) -> RacingMutationWorkers {
            let (stop, stopped) = mpsc::channel();
            let worker_token = token.clone();
            let worker = thread::Builder::new()
                .name("old-admission-mutation-race".into())
                .spawn(move || {
                    stopped.recv().expect("mutation worker stop");
                    if let Some(observer) = observer {
                        observer(
                            &worker_token,
                            &EngineError::host(
                                "conflict",
                                "Old admission token observed config mutation",
                            ),
                        );
                        faults.fetch_add(1, Ordering::SeqCst);
                    }
                })
                .expect("spawn mutation probe worker");
            RacingMutationWorkers {
                token,
                stop,
                worker: Some(worker),
                joined,
            }
        }

        let old_token = probe_admission_token('5', '6');
        let new_token = probe_admission_token('7', '8');
        let old_joined = Arc::new(AtomicUsize::new(0));
        let new_joined = Arc::new(AtomicUsize::new(0));
        let faults = Arc::new(AtomicUsize::new(0));
        let (event_sender, event_receiver) = mpsc::channel();
        let admission = AdmissionCoordinator::<RacingMutationWorkers>::with_event_sink(Arc::new(
            move |event| event_sender.send(event).expect("admission event"),
        ))
        .expect("start revocation supervisor");
        let observer = admission.engine_error_observer();
        let admitted_old_token = old_token.clone();
        let old_observer = Arc::clone(&observer);
        admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(mutation_worker(
                        admitted_old_token,
                        Some(old_observer),
                        Arc::clone(&old_joined),
                        Arc::clone(&faults),
                    ))
                },
            )
            .expect("admit old workers");
        let admitted = event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("initial admitted event");

        let replacement_token = new_token.clone();
        let result = admission.mutate_with(
            || {
                assert_eq!(old_joined.load(Ordering::SeqCst), 1);
                assert_eq!(faults.load(Ordering::SeqCst), 1);
                Ok("mutation committed")
            },
            || {
                Ok(mutation_worker(
                    replacement_token,
                    None,
                    Arc::clone(&new_joined),
                    Arc::new(AtomicUsize::new(0)),
                ))
            },
        );

        assert_eq!(
            result.expect("serialized mutation succeeds"),
            "mutation committed"
        );
        admission
            .require_admitted()
            .expect("new worker generation is admitted");
        let inner = admission.inner.lock().expect("admission state");
        assert_eq!(inner.state, AdmissionState::Admitted);
        assert_eq!(
            inner
                .workers
                .as_ref()
                .and_then(AdmissionWorkerSet::admission_token),
            Some(&new_token)
        );
        let replacement_admitted = event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("replacement admitted event follows worker activation");
        assert_eq!(replacement_admitted.status, ConfigAdmissionStatus::Admitted);
        assert!(replacement_admitted.generation > admitted.generation);
        assert_eq!(inner.generation, replacement_admitted.generation);
        drop(inner);
        assert!(event_receiver.try_recv().is_err());
        drop(observer);
        drop(admission);
        assert_eq!(new_joined.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn config_mutation_rejection_reactivates_only_the_unchanged_token() {
        let old_token = probe_admission_token('1', '2');
        let held = Arc::new(AtomicUsize::new(0));
        let drops = Arc::new(AtomicUsize::new(0));
        let restaged = Arc::new(AtomicUsize::new(0));
        let (event_sender, event_receiver) = mpsc::channel();
        let admission =
            AdmissionCoordinator::<TokenProbeWorkers>::with_event_sink(Arc::new(move |event| {
                event_sender.send(event).expect("admission event")
            }))
            .expect("start revocation supervisor");
        let admitted_old_token = old_token.clone();
        admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(TokenProbeWorkers {
                        token: admitted_old_token,
                        held: Arc::clone(&held),
                        drops: Arc::clone(&drops),
                    })
                },
            )
            .expect("admit old workers");
        let admitted = event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("initial admitted event");

        let restaged_on_failure = Arc::clone(&restaged);
        let recovery_token = old_token.clone();
        let result: Result<(), EngineError> = admission.mutate_with(
            || {
                Err(EngineError::host(
                    "invalid_request",
                    "Config mutation was rejected",
                ))
            },
            || {
                restaged_on_failure.fetch_add(1, Ordering::SeqCst);
                Ok(TokenProbeWorkers {
                    token: recovery_token,
                    held: Arc::clone(&held),
                    drops: Arc::clone(&drops),
                })
            },
        );

        assert_eq!(
            result
                .expect_err("rejected mutation preserves its error")
                .code,
            "invalid_request"
        );
        assert_eq!(restaged.load(Ordering::SeqCst), 1);
        assert_eq!(held.load(Ordering::SeqCst), 1);
        assert_eq!(drops.load(Ordering::SeqCst), 1);
        admission
            .require_admitted()
            .expect("unchanged token restores command admission");
        let recovered = event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("recovered admission event follows worker activation");
        assert_eq!(recovered.status, ConfigAdmissionStatus::Admitted);
        assert!(recovered.generation > admitted.generation);
        let inner = admission.inner.lock().expect("admission state");
        assert_eq!(
            inner
                .workers
                .as_ref()
                .and_then(AdmissionWorkerSet::admission_token),
            Some(&old_token)
        );
    }

    #[test]
    fn config_mutation_rejection_with_changed_identity_requires_manual_repair() {
        let old_token = probe_admission_token('5', '6');
        let changed_token = probe_admission_token('7', '8');
        let held = Arc::new(AtomicUsize::new(0));
        let drops = Arc::new(AtomicUsize::new(0));
        let (event_sender, event_receiver) = mpsc::channel();
        let admission =
            AdmissionCoordinator::<TokenProbeWorkers>::with_event_sink(Arc::new(move |event| {
                event_sender.send(event).expect("admission event")
            }))
            .expect("start revocation supervisor");
        let admitted_old_token = old_token;
        admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(TokenProbeWorkers {
                        token: admitted_old_token,
                        held: Arc::clone(&held),
                        drops: Arc::clone(&drops),
                    })
                },
            )
            .expect("admit old workers");
        event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("initial admitted event");

        let result: Result<(), EngineError> = admission.mutate_with(
            || {
                Err(EngineError::host(
                    "invalid_request",
                    "Config mutation was rejected",
                ))
            },
            || {
                Ok(TokenProbeWorkers {
                    token: changed_token,
                    held: Arc::clone(&held),
                    drops: Arc::clone(&drops),
                })
            },
        );

        assert_eq!(
            result
                .expect_err("changed identity cannot recover rejected mutation")
                .code,
            "conflict"
        );
        assert_eq!(held.load(Ordering::SeqCst), 1);
        assert_eq!(drops.load(Ordering::SeqCst), 2);
        assert_eq!(
            admission.require_admitted().unwrap_err().code,
            "configuration_not_admitted"
        );
        assert_eq!(
            event_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("repair event")
                .status,
            ConfigAdmissionStatus::ManualRepairRequired
        );
    }

    #[test]
    fn config_mutation_snapshot_failure_keeps_committed_change_fail_closed() {
        let token = probe_admission_token('9', 'a');
        let held = Arc::new(AtomicUsize::new(0));
        let drops = Arc::new(AtomicUsize::new(0));
        let committed = Arc::new(AtomicUsize::new(0));
        let (event_sender, event_receiver) = mpsc::channel();
        let admission =
            AdmissionCoordinator::<TokenProbeWorkers>::with_event_sink(Arc::new(move |event| {
                event_sender.send(event).expect("admission event")
            }))
            .expect("start revocation supervisor");
        admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(TokenProbeWorkers {
                        token,
                        held: Arc::clone(&held),
                        drops: Arc::clone(&drops),
                    })
                },
            )
            .expect("admit old workers");
        event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("initial admitted event");

        let mutation_committed = Arc::clone(&committed);
        let result = admission.mutate_with(
            || {
                mutation_committed.fetch_add(1, Ordering::SeqCst);
                Ok("config committed")
            },
            || {
                Err(EngineError::host(
                    "configuration_error",
                    "Configuration admission snapshot is unavailable",
                ))
            },
        );

        assert_eq!(committed.load(Ordering::SeqCst), 1);
        assert_eq!(
            result
                .expect_err("snapshot failure rejects committed mutation")
                .code,
            "configuration_error"
        );
        assert_eq!(held.load(Ordering::SeqCst), 1);
        assert_eq!(drops.load(Ordering::SeqCst), 1);
        assert_eq!(
            admission.require_admitted().unwrap_err().code,
            "configuration_not_admitted"
        );
        assert_eq!(
            event_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("repair event")
                .status,
            ConfigAdmissionStatus::ManualRepairRequired
        );
    }

    #[test]
    fn config_mutation_waits_for_active_effect_and_blocks_new_effects() {
        struct EffectGateWorkers {
            token: AdmissionToken,
            binding: Arc<Mutex<Option<AdmissionToken>>>,
            held: Mutex<Option<mpsc::Sender<()>>>,
            dropped: mpsc::Sender<String>,
        }

        impl AdmissionWorkerSet for EffectGateWorkers {
            fn admission_token(&self) -> Option<&AdmissionToken> {
                Some(&self.token)
            }

            fn begin_revocation(&self) {
                self.binding
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .take();
                if let Some(held) = self
                    .held
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .take()
                {
                    let _ = held.send(());
                }
            }

            fn activate(&self) {
                *self
                    .binding
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(self.token.clone());
            }
        }

        impl Drop for EffectGateWorkers {
            fn drop(&mut self) {
                self.begin_revocation();
                let _ = self
                    .dropped
                    .send(thread::current().name().unwrap_or("unnamed").to_owned());
            }
        }

        let old_token = probe_admission_token('b', 'c');
        let new_token = probe_admission_token('d', 'e');
        let binding = Arc::new(Mutex::new(None));
        let (held_sender, held_receiver) = mpsc::channel();
        let (drop_sender, drop_receiver) = mpsc::channel();
        let (event_sender, event_receiver) = mpsc::channel();
        let admission =
            AdmissionCoordinator::<EffectGateWorkers>::with_event_sink(Arc::new(move |event| {
                event_sender.send(event).expect("admission event")
            }))
            .expect("start revocation supervisor");
        let initial_drop_sender = drop_sender.clone();
        admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(EffectGateWorkers {
                        token: old_token.clone(),
                        binding: Arc::clone(&binding),
                        held: Mutex::new(Some(held_sender)),
                        dropped: initial_drop_sender,
                    })
                },
            )
            .expect("admit old workers");
        event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("initial admitted event");
        let active_effect = admission
            .require_admitted()
            .expect("admitted effect starts before mutation");
        let (commit_sender, commit_receiver) = mpsc::channel();
        let transaction = AdmissionCoordinator::clone(&admission);
        let replacement_drop_sender = drop_sender;
        let replacement_binding = Arc::clone(&binding);
        let transaction = thread::Builder::new()
            .name("config-mutation-transaction".into())
            .spawn(move || {
                transaction.mutate_with(
                    || {
                        commit_sender.send(()).expect("record config mutation");
                        Ok(())
                    },
                    || {
                        let (held, _held_receiver) = mpsc::channel();
                        Ok(EffectGateWorkers {
                            token: new_token,
                            binding: replacement_binding,
                            held: Mutex::new(Some(held)),
                            dropped: replacement_drop_sender,
                        })
                    },
                )
            })
            .expect("spawn config mutation transaction");

        for _ in 0..100 {
            if admission.inner.lock().expect("admission state").state == AdmissionState::Mutating {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(
            admission.inner.lock().expect("admission state").state,
            AdmissionState::Mutating
        );
        assert!(commit_receiver.try_recv().is_err());
        assert_eq!(
            admission.require_admitted().unwrap_err().code,
            "configuration_not_admitted"
        );
        assert!(
            held_receiver.try_recv().is_err(),
            "old admission binding must remain installed for permitted effects"
        );
        assert!(
            drop_receiver.try_recv().is_err(),
            "old workers must remain alive for permitted effects"
        );
        assert_eq!(
            binding.lock().expect("engine admission binding").as_ref(),
            Some(&old_token),
            "the in-flight command sends the exact token it was admitted under"
        );

        drop(active_effect);
        held_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("old admission binding revoked after permitted effects finish");
        assert_eq!(
            drop_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("old workers dropped after permitted effects finish"),
            "config-mutation-transaction"
        );
        commit_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("mutation begins only after old admitted effect ends");
        transaction
            .join()
            .expect("mutation transaction joins")
            .expect("mutation transaction succeeds");
        assert_eq!(
            event_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("replacement admitted event")
                .status,
            ConfigAdmissionStatus::Admitted
        );
        admission
            .require_admitted()
            .expect("new effects use the replacement generation");
        assert_eq!(
            binding.lock().expect("engine admission binding").as_ref(),
            Some(&probe_admission_token('d', 'e'))
        );
    }

    #[test]
    fn admitted_acknowledgement_revalidates_retained_binding_and_fails_closed_when_stale() {
        struct ExpiringAcknowledgementWorkers {
            token: AdmissionToken,
            comparisons: Arc<AtomicUsize>,
            revocations: Arc<AtomicUsize>,
        }

        impl AdmissionWorkerSet for ExpiringAcknowledgementWorkers {
            fn revalidate(&self) -> Result<(), EngineError> {
                if self.comparisons.fetch_add(1, Ordering::SeqCst) == 0 {
                    Ok(())
                } else {
                    Err(EngineError::host(
                        "conflict",
                        "Configuration admission snapshot changed",
                    ))
                }
            }

            fn admission_token(&self) -> Option<&AdmissionToken> {
                Some(&self.token)
            }

            fn begin_revocation(&self) {
                self.revocations.fetch_add(1, Ordering::SeqCst);
            }

            fn activate(&self) {}
        }

        let comparisons = Arc::new(AtomicUsize::new(0));
        let revocations = Arc::new(AtomicUsize::new(0));
        let (event_sender, event_receiver) = mpsc::channel();
        let admission = AdmissionCoordinator::<ExpiringAcknowledgementWorkers>::with_event_sink(
            Arc::new(move |event| event_sender.send(event).expect("admission event")),
        )
        .expect("start revocation supervisor");
        admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(ExpiringAcknowledgementWorkers {
                        token: probe_admission_token('a', 'b'),
                        comparisons: Arc::clone(&comparisons),
                        revocations: Arc::clone(&revocations),
                    })
                },
            )
            .expect("initial snapshot admits");
        event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("initial admitted event");

        let transition = admission
            .acknowledge_with(
                "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                || panic!("delayed acknowledgement must not mutate an admitted config"),
                || panic!("retained admission must use its atomic snapshot comparator"),
                || panic!("stale retained admission must fail closed before restaging"),
            )
            .expect("delayed acknowledgement reconciles");

        assert_eq!(
            transition.status,
            ConfigAdmissionStatus::ManualRepairRequired
        );
        assert_eq!(comparisons.load(Ordering::SeqCst), 2);
        assert_eq!(revocations.load(Ordering::SeqCst), 1);
        assert_eq!(
            admission.require_admitted().unwrap_err().code,
            "configuration_not_admitted"
        );
        assert_eq!(
            event_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("manual repair event")
                .status,
            ConfigAdmissionStatus::ManualRepairRequired
        );
    }

    #[test]
    fn admitted_acknowledgement_revalidates_current_binding_without_replaying_acknowledgement() {
        struct CurrentAcknowledgementWorkers {
            comparisons: Arc<AtomicUsize>,
        }

        impl AdmissionWorkerSet for CurrentAcknowledgementWorkers {
            fn revalidate(&self) -> Result<(), EngineError> {
                self.comparisons.fetch_add(1, Ordering::SeqCst);
                Ok(())
            }

            fn activate(&self) {}
        }

        let comparisons = Arc::new(AtomicUsize::new(0));
        let (event_sender, event_receiver) = mpsc::channel();
        let admission = AdmissionCoordinator::<CurrentAcknowledgementWorkers>::with_event_sink(
            Arc::new(move |event| event_sender.send(event).expect("admission event")),
        )
        .expect("start revocation supervisor");
        admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(CurrentAcknowledgementWorkers {
                        comparisons: Arc::clone(&comparisons),
                    })
                },
            )
            .expect("initial snapshot admits");
        event_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("initial admitted event");

        let transition = admission
            .acknowledge_with(
                "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                || panic!("admitted configuration must not replay acknowledgement"),
                || panic!("retained admission must compare its atomic snapshot"),
                || panic!("current retained admission must not restage workers"),
            )
            .expect("current retained admission reconciles");

        assert_eq!(transition.status, ConfigAdmissionStatus::Admitted);
        assert_eq!(comparisons.load(Ordering::SeqCst), 2);
        admission
            .require_admitted()
            .expect("current retained admission remains available");
        assert_eq!(
            event_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("reconciled admitted event")
                .status,
            ConfigAdmissionStatus::Admitted
        );
    }

    #[test]
    fn acknowledgement_racing_config_mutation_observes_fail_closed_state_without_deadlock() {
        let admission = AdmissionCoordinator::<TokenProbeWorkers>::new();
        let token = probe_admission_token('e', 'f');
        let held = Arc::new(AtomicUsize::new(0));
        let drops = Arc::new(AtomicUsize::new(0));
        admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(TokenProbeWorkers {
                        token: token.clone(),
                        held: Arc::clone(&held),
                        drops: Arc::clone(&drops),
                    })
                },
            )
            .expect("initial snapshot admits");
        let active_effect = admission
            .require_admitted()
            .expect("effect starts before config mutation");
        let transaction = AdmissionCoordinator::clone(&admission);
        let replacement_token = probe_admission_token('1', '2');
        let replacement_held = Arc::clone(&held);
        let replacement_drops = Arc::clone(&drops);
        let transaction = thread::spawn(move || {
            transaction.mutate_with(
                || Ok(()),
                || {
                    Ok(TokenProbeWorkers {
                        token: replacement_token,
                        held: replacement_held,
                        drops: replacement_drops,
                    })
                },
            )
        });
        for _ in 0..100 {
            if admission.inner.lock().expect("admission state").state == AdmissionState::Mutating {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        let generation = admission.inner.lock().expect("admission state").generation;

        let transition = admission
            .acknowledge_with(
                "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                || panic!("mutation race must not acknowledge"),
                || panic!("mutation race must not inspect outside its transaction"),
                || panic!("mutation race must not stage competing workers"),
            )
            .expect("mutation race reports fail-closed state");
        assert_eq!(
            transition.status,
            ConfigAdmissionStatus::ManualRepairRequired
        );
        assert_eq!(transition.generation, generation);
        assert_eq!(
            admission.require_admitted().unwrap_err().code,
            "configuration_not_admitted"
        );

        drop(active_effect);
        transaction
            .join()
            .expect("mutation thread joins")
            .expect("mutation completes after effect release");
        admission
            .require_admitted()
            .expect("completed mutation installs replacement admission");
    }

    #[test]
    fn timed_out_delivery_does_not_hold_admission_commands() {
        let admission = AdmissionCoordinator::<ProbeAdmissionWorkers>::new();
        let transition = admission
            .refresh_with(
                || Ok(NtfyDisclosureStatus::NormalAdmission),
                || {
                    Ok(ProbeAdmissionWorkers {
                        queue_activations: Arc::new(AtomicUsize::new(0)),
                        poll_activations: Arc::new(AtomicUsize::new(0)),
                        startup_delivery_activations: Arc::new(AtomicUsize::new(0)),
                    })
                },
            )
            .expect("admission succeeds");
        assert_eq!(transition.status, ConfigAdmissionStatus::Admitted);

        let (started_sender, started_receiver) = mpsc::sync_channel(1);
        let delivery = thread::spawn(move || {
            delivery::run_bounded_operation(Duration::from_millis(10), move |deadline| {
                started_sender.send(()).expect("signal delivery start");
                while !deadline.is_cancelled() {
                    thread::yield_now();
                }
            })
        });
        started_receiver.recv().expect("delivery started");

        admission
            .require_admitted()
            .expect("commands remain admitted during delivery");
        let repeated = admission
            .refresh_with(
                || panic!("admitted refresh must not inspect"),
                || panic!("admitted refresh must not stage"),
            )
            .expect("repair UI remains usable during delivery");
        assert_eq!(repeated.status, ConfigAdmissionStatus::Admitted);
        assert_eq!(
            delivery.join().expect("delivery waiter joins"),
            delivery::BoundedOperation::TimedOut
        );
    }

    #[test]
    fn every_acknowledgement_outcome_reinspects_without_retrying() {
        let outcomes = [
            None,
            Some("conflict"),
            Some("outcome_unknown"),
            Some("engine_timeout"),
        ];
        for outcome in outcomes {
            let events = RefCell::new(Vec::new());
            let attempt = run_acknowledgement_attempt(
                || {
                    events.borrow_mut().push("acknowledge");
                    match outcome {
                        None => Ok(()),
                        Some(code) => Err(EngineError::host(code, "fixed failure")),
                    }
                },
                || {
                    events.borrow_mut().push("status");
                    Ok(NtfyDisclosureStatus::AcknowledgementRequired {
                        expected_revision: "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                            .into(),
                    })
                },
                || {
                    events.borrow_mut().push("settings/workers");
                    Ok(())
                },
            );
            assert_eq!(
                attempt.state,
                AdmissionState::AwaitingAcknowledgement {
                    expected_revision:
                        "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                            .into(),
                }
            );
            assert!(attempt.workers.is_none());
            assert_eq!(*events.borrow(), ["acknowledge", "status"]);
        }

        let events = RefCell::new(Vec::new());
        let admitted = run_acknowledgement_attempt(
            || {
                events.borrow_mut().push("acknowledge");
                Err(EngineError::host("outcome_unknown", "fixed failure"))
            },
            || {
                events.borrow_mut().push("status");
                Ok(NtfyDisclosureStatus::NormalAdmission)
            },
            || {
                events.borrow_mut().push("settings/workers");
                Ok(())
            },
        );
        assert_eq!(admitted.state, AdmissionState::Admitted);
        assert_eq!(
            *events.borrow(),
            ["acknowledge", "status", "settings/workers"]
        );
    }

    #[test]
    fn commands_require_an_installed_admitted_worker_set() {
        let admission = AdmissionCoordinator::<()>::new();
        assert_eq!(
            admission.require_admitted().unwrap_err().code,
            "configuration_not_admitted"
        );
        let mut inner = admission.inner.lock().expect("admission lock");
        AdmissionCoordinator::install_attempt(
            &mut inner,
            AdmissionAttempt {
                state: AdmissionState::Admitted,
                workers: Some(()),
            },
        );
        drop(inner);
        admission
            .require_admitted()
            .expect("admitted commands allowed");
    }

    #[test]
    fn entitlement_install_wakes_queue_only_after_success() {
        let mut success_wakes = 0;
        let success = wake_connect_queue_after_entitlement_install(Ok::<_, ()>("active"), || {
            success_wakes += 1
        });
        let mut failure_wakes = 0;
        let failure = wake_connect_queue_after_entitlement_install(Err::<(), _>("invalid"), || {
            failure_wakes += 1
        });

        assert_eq!(success, Ok("active"));
        assert_eq!(success_wakes, 1);
        assert_eq!(failure, Err("invalid"));
        assert_eq!(failure_wakes, 0);
    }

    #[test]
    fn autostart_update_verifies_the_persisted_state() {
        assert_eq!(
            apply_autostart_update(true, |_| Ok::<_, ()>(()), || Ok(true)),
            Ok(true)
        );
        assert_eq!(
            apply_autostart_update(false, |_| Ok::<_, ()>(()), || Ok(false)),
            Ok(false)
        );
        assert_eq!(
            apply_autostart_update(true, |_| Ok::<_, ()>(()), || Ok(false)),
            Err(AutostartUpdateError::NotApplied)
        );
        assert_eq!(
            apply_autostart_update(true, |_| Err::<(), _>(()), || Ok(true)),
            Err(AutostartUpdateError::Write)
        );
        assert_eq!(
            apply_autostart_update(true, |_| Ok::<_, ()>(()), || Err(())),
            Err(AutostartUpdateError::ReadBack)
        );
    }
}
