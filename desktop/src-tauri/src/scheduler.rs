use crate::delivery::NotificationDelivery;
use crate::engine::{CancellationToken, Engine};
use serde::Serialize;
use std::io;
use std::sync::{
    Arc, Condvar, Mutex,
    atomic::{AtomicU64, Ordering},
    mpsc::{self, Receiver, RecvTimeoutError, Sender, SyncSender, TrySendError},
};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter};

pub const SCHEDULED_CHECK_EVENT: &str = "watcher://scheduled-check";
pub const CONNECT_QUEUE_EVENT: &str = "watcher://connect-queue";
const MAX_SLEEP_SLICE: Duration = Duration::from_secs(30);
const SCHEDULED_ENGINE_TIMEOUT: Duration = Duration::from_secs(30 * 60);
const CONNECT_QUEUE_ERROR_RETRY: Duration = Duration::from_secs(30);
const WORKER_SHUTDOWN_GRACE: Duration = Duration::from_secs(2);

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum ScheduledCheckStatus {
    Complete,
    DeliveryFailed,
    CheckFailed,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
struct ScheduledCheckEvent {
    status: ScheduledCheckStatus,
    failed_notifications: u64,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
struct ConnectQueueEvent {
    attempted: usize,
}

#[derive(Clone)]
pub struct ConnectQueueScheduler {
    wake_sender: SyncSender<()>,
    gate: Arc<WorkerGate>,
    worker: OwnedWorker,
}

#[derive(Default)]
struct WorkerGateState {
    activated: bool,
    stopped: bool,
}

pub(crate) struct WorkerGate {
    state: Mutex<WorkerGateState>,
    changed: Condvar,
    cancellation: CancellationToken,
}

impl Default for WorkerGate {
    fn default() -> Self {
        Self::with_cancellation(CancellationToken::new())
    }
}

impl WorkerGate {
    pub(crate) fn with_cancellation(cancellation: CancellationToken) -> Self {
        Self {
            state: Mutex::new(WorkerGateState::default()),
            changed: Condvar::new(),
            cancellation,
        }
    }

    pub(crate) fn cancellation(&self) -> CancellationToken {
        self.cancellation.clone()
    }

    pub(crate) fn activate(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if !state.stopped {
            state.activated = true;
        }
        self.changed.notify_all();
    }

    pub(crate) fn signal_stop(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.stopped = true;
        self.changed.notify_all();
    }

    fn is_stopped(&self) -> bool {
        self.cancellation.is_cancelled()
            || self
                .state
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .stopped
    }

    pub(crate) fn wait_for_activation(&self) -> bool {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        while !state.activated && !state.stopped && !self.cancellation.is_cancelled() {
            state = self
                .changed
                .wait(state)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
        }
        state.activated && !state.stopped && !self.cancellation.is_cancelled()
    }

    fn wait_for(&self, duration: Duration) -> bool {
        let state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if state.stopped || self.cancellation.is_cancelled() {
            return false;
        }
        let (state, _) = self
            .changed
            .wait_timeout(state, duration)
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        !state.stopped && !self.cancellation.is_cancelled()
    }
}

struct CompletionSignal(Option<Sender<()>>);

impl Drop for CompletionSignal {
    fn drop(&mut self) {
        if let Some(sender) = self.0.take() {
            let _ = sender.send(());
        }
    }
}

struct WorkerRegistration {
    handle: thread::JoinHandle<()>,
    completed: Receiver<()>,
}

#[derive(Clone, Default)]
pub(crate) struct OwnedWorker {
    registration: Arc<Mutex<Option<WorkerRegistration>>>,
}

impl OwnedWorker {
    pub(crate) fn spawn(
        &self,
        name: &str,
        operation: impl FnOnce() + Send + 'static,
    ) -> io::Result<()> {
        let mut registration = self
            .registration
            .lock()
            .map_err(|_| io::Error::other("Worker handle is unavailable"))?;
        if registration.is_some() {
            return Err(io::Error::other("Worker is already running"));
        }
        let (completed_sender, completed_receiver) = mpsc::channel();
        let handle = thread::Builder::new().name(name.into()).spawn(move || {
            let _completion = CompletionSignal(Some(completed_sender));
            operation();
        })?;
        *registration = Some(WorkerRegistration {
            handle,
            completed: completed_receiver,
        });
        Ok(())
    }

    pub(crate) fn join(&self, label: &str) -> io::Result<()> {
        let registration = self
            .registration
            .lock()
            .map_err(|_| io::Error::other("Worker handle is unavailable"))?
            .take();
        let Some(registration) = registration else {
            return Ok(());
        };
        match registration.completed.recv_timeout(WORKER_SHUTDOWN_GRACE) {
            Ok(()) | Err(RecvTimeoutError::Disconnected) => registration
                .handle
                .join()
                .map_err(|_| io::Error::other(format!("{label} stopped unexpectedly"))),
            Err(RecvTimeoutError::Timeout) => {
                eprintln!("fatal: {label} did not stop within the owned worker shutdown deadline");
                std::process::abort();
            }
        }
    }

    #[cfg(all(test, unix))]
    pub(crate) fn is_joined(&self) -> bool {
        self.registration
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .is_none()
    }
}

fn unix_ms_now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

fn queue_wait_duration(deadline_unix_ms: u64, now_unix_ms: u64) -> Duration {
    Duration::from_millis(deadline_unix_ms.saturating_sub(now_unix_ms))
}

fn wait_for_queue_wakeup(receiver: &Receiver<()>, deadline_unix_ms: Option<u64>) -> bool {
    wait_for_queue_wakeup_with_clock(receiver, deadline_unix_ms, MAX_SLEEP_SLICE, unix_ms_now)
}

fn wait_for_queue_wakeup_with_clock(
    receiver: &Receiver<()>,
    deadline_unix_ms: Option<u64>,
    max_sleep_slice: Duration,
    mut now_unix_ms: impl FnMut() -> u64,
) -> bool {
    match deadline_unix_ms {
        Some(deadline) => loop {
            let duration = queue_wait_duration(deadline, now_unix_ms()).min(max_sleep_slice);
            if duration.is_zero() {
                return true;
            }
            match receiver.recv_timeout(duration) {
                Ok(()) => return true,
                Err(RecvTimeoutError::Timeout) => continue,
                Err(RecvTimeoutError::Disconnected) => return false,
            }
        },
        None => receiver.recv().is_ok(),
    }
}

fn queue_refresh_needed(
    queue_was_active: bool,
    attempted: usize,
    next_wake_unix_ms: Option<u64>,
) -> bool {
    attempted > 0 || (queue_was_active && next_wake_unix_ms.is_none())
}

impl ConnectQueueScheduler {
    pub(crate) fn stage_with_cancellation(
        app: AppHandle,
        engine: Engine,
        cancellation: CancellationToken,
    ) -> io::Result<Self> {
        let (wake_sender, receiver) = mpsc::sync_channel(1);
        let gate = Arc::new(WorkerGate::with_cancellation(cancellation));
        let worker = OwnedWorker::default();
        let scheduler = Self {
            wake_sender,
            gate: Arc::clone(&gate),
            worker: worker.clone(),
        };
        let engine = engine
            .with_request_timeout(SCHEDULED_ENGINE_TIMEOUT)
            .with_cancellation(gate.cancellation());
        worker.spawn("email-watcher-connect-queue", move || {
            if !gate.wait_for_activation() {
                return;
            }
            let mut next_wake_unix_ms = Some(unix_ms_now());
            let mut queue_was_active = false;
            while wait_for_queue_wakeup(&receiver, next_wake_unix_ms) {
                if gate.is_stopped() {
                    break;
                }
                match engine.pump_connect_queue() {
                    Ok(outcome) => {
                        next_wake_unix_ms = outcome.next_wake_unix_ms;
                        let refresh = queue_refresh_needed(
                            queue_was_active,
                            outcome.items.len(),
                            next_wake_unix_ms,
                        );
                        queue_was_active = next_wake_unix_ms.is_some();
                        if refresh
                            && app
                                .emit(
                                    CONNECT_QUEUE_EVENT,
                                    ConnectQueueEvent {
                                        attempted: outcome.items.len(),
                                    },
                                )
                                .is_err()
                        {
                            eprintln!(
                                "Connect queue progress could not refresh the desktop window"
                            );
                        }
                    }
                    Err(error) => {
                        eprintln!(
                            "Connect queue pump failed ({}): {}",
                            error.code, error.message
                        );
                        next_wake_unix_ms = Some(
                            unix_ms_now()
                                .saturating_add(CONNECT_QUEUE_ERROR_RETRY.as_millis() as u64),
                        );
                    }
                }
            }
        })?;
        Ok(scheduler)
    }

    #[cfg(all(test, unix))]
    fn stage_probe(operation: impl FnOnce(CancellationToken) + Send + 'static) -> io::Result<Self> {
        Self::stage_probe_with_cancellation(CancellationToken::new(), operation)
    }

    #[cfg(all(test, unix))]
    pub(crate) fn stage_probe_with_cancellation(
        cancellation: CancellationToken,
        operation: impl FnOnce(CancellationToken) + Send + 'static,
    ) -> io::Result<Self> {
        let (wake_sender, _receiver) = mpsc::sync_channel(1);
        let gate = Arc::new(WorkerGate::with_cancellation(cancellation));
        let worker = OwnedWorker::default();
        let scheduler = Self {
            wake_sender,
            gate: Arc::clone(&gate),
            worker: worker.clone(),
        };
        worker.spawn("email-watcher-connect-queue-probe", move || {
            if gate.wait_for_activation() {
                operation(gate.cancellation());
            }
        })?;
        Ok(scheduler)
    }

    pub fn activate(&self) {
        self.gate.activate();
    }

    pub(crate) fn signal_stop(&self) {
        self.gate.signal_stop();
        let _ = self.wake_sender.try_send(());
    }

    pub(crate) fn join(&self) -> io::Result<()> {
        self.worker.join("Connect queue worker")
    }

    pub fn shutdown(&self) -> io::Result<()> {
        self.gate.cancellation().cancel();
        self.signal_stop();
        self.join()
    }

    #[cfg(all(test, unix))]
    pub(crate) fn is_joined(&self) -> bool {
        self.worker.is_joined()
    }

    pub fn wake(&self) {
        match self.wake_sender.try_send(()) {
            Ok(()) | Err(TrySendError::Full(())) => {}
            Err(TrySendError::Disconnected(())) => {
                eprintln!("Connect queue scheduler is unavailable");
            }
        }
    }
}

impl ScheduledCheckEvent {
    fn completed(failed_notifications: u64) -> Self {
        Self {
            status: if failed_notifications == 0 {
                ScheduledCheckStatus::Complete
            } else {
                ScheduledCheckStatus::DeliveryFailed
            },
            failed_notifications,
        }
    }

    fn check_failed() -> Self {
        Self {
            status: ScheduledCheckStatus::CheckFailed,
            failed_notifications: 0,
        }
    }
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct PollingStatus {
    pub enabled: bool,
    pub interval_minutes: u64,
    pub next_check_unix_ms: Option<u64>,
}

#[derive(Clone)]
pub struct PollScheduler {
    enabled: bool,
    interval_minutes: u64,
    next_check_unix_ms: Arc<AtomicU64>,
    gate: Arc<WorkerGate>,
    worker: OwnedWorker,
}

fn next_check_unix_ms(now: SystemTime, interval_minutes: u64) -> u64 {
    now.duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .saturating_add(Duration::from_secs(interval_minutes.saturating_mul(60)))
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

fn sleep_slice(deadline_unix_ms: u64, now_unix_ms: u64) -> Option<Duration> {
    let remaining_ms = deadline_unix_ms.saturating_sub(now_unix_ms);
    if remaining_ms == 0 {
        return None;
    }
    Some(Duration::from_millis(remaining_ms).min(MAX_SLEEP_SLICE))
}

fn wait_until(gate: &WorkerGate, deadline_unix_ms: u64) -> bool {
    loop {
        let now_unix_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis()
            .try_into()
            .unwrap_or(u64::MAX);
        let Some(duration) = sleep_slice(deadline_unix_ms, now_unix_ms) else {
            return !gate.is_stopped();
        };
        if !gate.wait_for(duration) {
            return false;
        }
    }
}

impl PollScheduler {
    #[cfg(test)]
    pub fn new(interval_minutes: u64, enabled: bool) -> Self {
        Self::with_cancellation(interval_minutes, enabled, CancellationToken::new())
    }

    pub(crate) fn with_cancellation(
        interval_minutes: u64,
        enabled: bool,
        cancellation: CancellationToken,
    ) -> Self {
        Self {
            enabled,
            interval_minutes,
            next_check_unix_ms: Arc::new(AtomicU64::new(if enabled {
                next_check_unix_ms(SystemTime::now(), interval_minutes)
            } else {
                0
            })),
            gate: Arc::new(WorkerGate::with_cancellation(cancellation)),
            worker: OwnedWorker::default(),
        }
    }

    pub fn status(&self) -> PollingStatus {
        let next_check_unix_ms = self.next_check_unix_ms.load(Ordering::Relaxed);
        PollingStatus {
            enabled: self.enabled,
            interval_minutes: self.interval_minutes,
            next_check_unix_ms: (next_check_unix_ms != 0).then_some(next_check_unix_ms),
        }
    }

    pub fn stage(
        &self,
        app: AppHandle,
        engine: Engine,
        delivery: NotificationDelivery,
        connect_queue: ConnectQueueScheduler,
    ) -> io::Result<()> {
        if !self.enabled {
            return Ok(());
        }
        let scheduler = self.clone();
        let engine = engine
            .with_request_timeout(SCHEDULED_ENGINE_TIMEOUT)
            .with_cancellation(scheduler.gate.cancellation());
        let cancellation = scheduler.gate.cancellation();
        self.worker.spawn("email-watcher-poll", move || {
            if !scheduler.gate.wait_for_activation() {
                return;
            }
            while wait_until(
                &scheduler.gate,
                scheduler.next_check_unix_ms.load(Ordering::Relaxed),
            ) {
                let event = match delivery.check_and_deliver_with_cancellation(
                    &engine,
                    SCHEDULED_ENGINE_TIMEOUT,
                    cancellation.clone(),
                ) {
                    Ok(outcome) => {
                        if outcome.delivery.failed > 0 {
                            eprintln!(
                                "{} scheduled notifications remain queued after delivery errors",
                                outcome.delivery.failed
                            );
                        }
                        ScheduledCheckEvent::completed(outcome.delivery.failed)
                    }
                    Err(error) => {
                        eprintln!(
                            "scheduled watcher check failed ({}): {}",
                            error.code, error.message
                        );
                        ScheduledCheckEvent::check_failed()
                    }
                };
                connect_queue.wake();
                scheduler.next_check_unix_ms.store(
                    next_check_unix_ms(SystemTime::now(), scheduler.interval_minutes),
                    Ordering::Relaxed,
                );
                if app.emit(SCHEDULED_CHECK_EVENT, event).is_err() {
                    eprintln!("scheduled watcher check could not refresh the desktop window");
                }
            }
        })
    }

    #[cfg(all(test, unix))]
    pub(crate) fn stage_probe(
        &self,
        operation: impl FnOnce(CancellationToken) + Send + 'static,
    ) -> io::Result<()> {
        if !self.enabled {
            return Ok(());
        }
        let scheduler = self.clone();
        self.worker.spawn("email-watcher-poll-probe", move || {
            if scheduler.gate.wait_for_activation() {
                operation(scheduler.gate.cancellation());
            }
        })
    }

    pub fn activate(&self) {
        self.gate.activate();
    }

    pub(crate) fn signal_stop(&self) {
        self.gate.signal_stop();
    }

    pub(crate) fn join(&self) -> io::Result<()> {
        self.worker.join("Polling worker")
    }

    pub fn shutdown(&self) -> io::Result<()> {
        self.gate.cancellation().cancel();
        self.signal_stop();
        self.join()
    }

    #[cfg(all(test, unix))]
    pub(crate) fn is_joined(&self) -> bool {
        self.worker.is_joined()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;
    #[cfg(unix)]
    use std::ffi::OsString;
    #[cfg(unix)]
    use std::fs;
    use std::sync::atomic::AtomicUsize;
    #[cfg(unix)]
    use std::time::Instant;

    #[cfg(unix)]
    fn wait_for_process_id(path: &std::path::Path) -> i32 {
        for _ in 0..100 {
            if let Ok(value) = fs::read_to_string(path)
                && let Ok(process_id) = value.trim().parse()
            {
                return process_id;
            }
            thread::sleep(Duration::from_millis(10));
        }
        panic!("engine probe did not record its process id");
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
        panic!("cancelled engine process {process_id} is still running");
    }

    #[cfg(unix)]
    fn stalled_engine(process_id_path: &std::path::Path) -> Engine {
        Engine::with_command(
            "sh",
            vec![
                OsString::from("-c"),
                OsString::from("cat >/dev/null; echo $$ > \"$1\"; exec sleep 30"),
                OsString::from("scheduler-shutdown-probe"),
                process_id_path.as_os_str().to_owned(),
            ],
            "unused.toml".into(),
        )
    }

    #[cfg(unix)]
    fn successful_queue_engine() -> Engine {
        Engine::with_command(
            "sh",
            vec![
                OsString::from("-c"),
                OsString::from(
                    r#"cat >/dev/null
printf '%s\n' '{"protocol":1,"ok":true,"operation":"connect.queue.pump","data":{"items":[],"next_wake_unix_ms":null}}'"#,
                ),
            ],
            "unused.toml".into(),
        )
    }

    #[test]
    fn next_check_uses_configured_interval() {
        let now = UNIX_EPOCH + Duration::from_secs(1_000);
        assert_eq!(next_check_unix_ms(now, 120), 8_200_000);
    }

    #[test]
    fn status_reports_interval_and_deadline() {
        let scheduler = PollScheduler::new(120, true);
        let status = scheduler.status();
        assert!(status.enabled);
        assert_eq!(status.interval_minutes, 120);
        assert!(status.next_check_unix_ms.is_some());
    }

    #[test]
    fn unsupported_polling_has_no_deadline() {
        let status = PollScheduler::new(120, false).status();
        assert!(!status.enabled);
        assert_eq!(status.next_check_unix_ms, None);
    }

    #[test]
    fn wall_clock_sleep_rechecks_resume_and_deadline_boundaries() {
        assert_eq!(sleep_slice(120_000, 0), Some(MAX_SLEEP_SLICE));
        assert_eq!(sleep_slice(120_000, 110_000), Some(Duration::from_secs(10)));
        assert_eq!(sleep_slice(120_000, 120_000), None);
        assert_eq!(sleep_slice(120_000, 130_000), None);
    }

    #[test]
    fn scheduled_event_distinguishes_delivery_failure_from_complete_check() {
        assert_eq!(
            ScheduledCheckEvent::completed(2),
            ScheduledCheckEvent {
                status: ScheduledCheckStatus::DeliveryFailed,
                failed_notifications: 2,
            }
        );
        assert_eq!(
            ScheduledCheckEvent::completed(0),
            ScheduledCheckEvent {
                status: ScheduledCheckStatus::Complete,
                failed_notifications: 0,
            }
        );
        assert_eq!(
            serde_json::to_value(ScheduledCheckEvent::completed(2)).expect("serialize event"),
            serde_json::json!({
                "status": "delivery_failed",
                "failed_notifications": 2,
            })
        );
    }

    #[test]
    fn queue_deadline_wait_is_due_now_or_is_bounded_by_poll_slice() {
        assert_eq!(
            queue_wait_duration(120_000, 110_000),
            Duration::from_secs(10)
        );
        assert_eq!(queue_wait_duration(120_000, 120_000), Duration::ZERO);
        assert_eq!(queue_wait_duration(120_000, 130_000), Duration::ZERO);
        assert_eq!(
            queue_wait_duration(180_000, 120_000).min(MAX_SLEEP_SLICE),
            MAX_SLEEP_SLICE
        );
    }

    #[test]
    fn repeated_queue_wakes_are_coalesced() {
        let (wake_sender, receiver) = mpsc::sync_channel(1);
        let scheduler = ConnectQueueScheduler {
            wake_sender,
            gate: Arc::new(WorkerGate::default()),
            worker: OwnedWorker::default(),
        };

        scheduler.wake();
        scheduler.wake();

        assert_eq!(receiver.try_recv(), Ok(()));
        assert!(receiver.try_recv().is_err());
    }

    #[test]
    fn staged_worker_stopped_before_activation_does_no_work() {
        let gate = Arc::new(WorkerGate::default());
        let work = Arc::new(AtomicUsize::new(0));
        let worker_gate = Arc::clone(&gate);
        let worker_work = Arc::clone(&work);
        let worker = thread::spawn(move || {
            if worker_gate.wait_for_activation() {
                worker_work.fetch_add(1, Ordering::SeqCst);
            }
        });

        gate.signal_stop();
        worker.join().expect("staged worker joins");

        assert_eq!(work.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn intermediate_queue_sleep_slice_rechecks_deadline_without_pumping() {
        let (_sender, receiver) = mpsc::sync_channel(1);
        let clock_calls = Cell::new(0);

        let should_pump = wait_for_queue_wakeup_with_clock(
            &receiver,
            Some(200),
            Duration::from_millis(1),
            || {
                let call = clock_calls.get();
                clock_calls.set(call + 1);
                if call == 0 { 100 } else { 200 }
            },
        );

        assert!(should_pump);
        assert_eq!(clock_calls.get(), 2);
    }

    #[test]
    fn queue_refresh_includes_external_terminal_transition() {
        assert!(!queue_refresh_needed(false, 0, None));
        assert!(queue_refresh_needed(false, 1, None));
        assert!(!queue_refresh_needed(false, 0, Some(100)));
        assert!(queue_refresh_needed(true, 0, None));
    }

    #[cfg(unix)]
    #[test]
    fn connect_shutdown_cancels_noncooperative_pump_and_allows_restart() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let process_id_path = directory.path().join("connect.pid");
        let engine = stalled_engine(&process_id_path);
        let (result_sender, result_receiver) = mpsc::sync_channel(1);
        let scheduler = ConnectQueueScheduler::stage_probe(move |cancellation| {
            let result = engine
                .with_request_timeout(SCHEDULED_ENGINE_TIMEOUT)
                .with_cancellation(cancellation)
                .pump_connect_queue();
            result_sender.send(result).expect("report queue result");
        })
        .expect("stage queue probe");
        scheduler.activate();
        let process_id = wait_for_process_id(&process_id_path);

        let started = Instant::now();
        scheduler.shutdown().expect("queue shutdown joins");

        assert!(started.elapsed() < Duration::from_secs(1));
        assert_eq!(
            result_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("queue result")
                .expect_err("cancelled queue pump fails")
                .code,
            "engine_cancelled"
        );
        assert_process_stopped(process_id);
        assert!(scheduler.is_joined());

        let engine = successful_queue_engine();
        let (restart_sender, restart_receiver) = mpsc::sync_channel(1);
        let restarted = ConnectQueueScheduler::stage_probe(move |cancellation| {
            restart_sender
                .send(
                    engine
                        .with_request_timeout(Duration::from_secs(1))
                        .with_cancellation(cancellation)
                        .pump_connect_queue()
                        .is_ok(),
                )
                .expect("report restarted queue");
        })
        .expect("stage restarted queue");
        restarted.activate();
        assert_eq!(
            restart_receiver.recv_timeout(Duration::from_secs(1)),
            Ok(true)
        );
        restarted.shutdown().expect("restarted queue joins");
    }

    #[cfg(unix)]
    #[test]
    fn polling_shutdown_cancels_noncooperative_delivery_and_releases_lock() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let process_id_path = directory.path().join("poll.pid");
        let engine = stalled_engine(&process_id_path);
        let delivery = NotificationDelivery::default();
        let worker_delivery = delivery.clone();
        let (result_sender, result_receiver) = mpsc::sync_channel(1);
        let scheduler = PollScheduler::new(1, true);
        scheduler
            .stage_probe(move |cancellation| {
                let result = worker_delivery.check_and_deliver_with_cancellation(
                    &engine,
                    SCHEDULED_ENGINE_TIMEOUT,
                    cancellation,
                );
                result_sender.send(result).expect("report polling result");
            })
            .expect("stage polling probe");
        scheduler.activate();
        let process_id = wait_for_process_id(&process_id_path);

        let started = Instant::now();
        scheduler.shutdown().expect("polling shutdown joins");

        assert!(started.elapsed() < Duration::from_secs(1));
        assert!(
            result_receiver
                .recv_timeout(Duration::from_secs(1))
                .expect("polling result")
                .is_err()
        );
        assert_process_stopped(process_id);
        assert!(scheduler.is_joined());
        delivery
            .run_exclusive_with_timeout(Duration::from_millis(100), |_| Ok(()))
            .expect("delivery lock is released after shutdown");

        let (restart_sender, restart_receiver) = mpsc::sync_channel(1);
        let restarted = PollScheduler::new(1, true);
        restarted
            .stage_probe(move |cancellation| {
                restart_sender
                    .send(!cancellation.is_cancelled())
                    .expect("report restarted poller");
            })
            .expect("stage restarted poller");
        restarted.activate();
        assert_eq!(
            restart_receiver.recv_timeout(Duration::from_secs(1)),
            Ok(true)
        );
        restarted.shutdown().expect("restarted poller joins");
    }
}
