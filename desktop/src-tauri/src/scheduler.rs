use crate::delivery::NotificationDelivery;
use crate::engine::Engine;
use serde::Serialize;
use std::io;
use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
    mpsc::{self, Receiver, RecvTimeoutError, SyncSender, TrySendError},
};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter};

pub const SCHEDULED_CHECK_EVENT: &str = "watcher://scheduled-check";
pub const CONNECT_QUEUE_EVENT: &str = "watcher://connect-queue";
const MAX_SLEEP_SLICE: Duration = Duration::from_secs(30);
const SCHEDULED_ENGINE_TIMEOUT: Duration = Duration::from_secs(30 * 60);
const CONNECT_QUEUE_ERROR_RETRY: Duration = Duration::from_secs(30);

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
    pub fn start(app: AppHandle, engine: Engine) -> io::Result<Self> {
        let (wake_sender, receiver) = mpsc::sync_channel(1);
        let scheduler = Self { wake_sender };
        let engine = engine.with_request_timeout(SCHEDULED_ENGINE_TIMEOUT);
        thread::Builder::new()
            .name("email-watcher-connect-queue".into())
            .spawn(move || {
                let mut next_wake_unix_ms = Some(unix_ms_now());
                let mut queue_was_active = false;
                while wait_for_queue_wakeup(&receiver, next_wake_unix_ms) {
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
            })
            .map(|_| ())?;
        Ok(scheduler)
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

fn wait_until(deadline_unix_ms: u64) {
    loop {
        let now_unix_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis()
            .try_into()
            .unwrap_or(u64::MAX);
        let Some(duration) = sleep_slice(deadline_unix_ms, now_unix_ms) else {
            return;
        };
        thread::sleep(duration);
    }
}

impl PollScheduler {
    pub fn new(interval_minutes: u64, enabled: bool) -> Self {
        Self {
            enabled,
            interval_minutes,
            next_check_unix_ms: Arc::new(AtomicU64::new(if enabled {
                next_check_unix_ms(SystemTime::now(), interval_minutes)
            } else {
                0
            })),
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

    pub fn start(
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
        let engine = engine.with_request_timeout(SCHEDULED_ENGINE_TIMEOUT);
        thread::Builder::new()
            .name("email-watcher-poll".into())
            .spawn(move || loop {
                wait_until(scheduler.next_check_unix_ms.load(Ordering::Relaxed));
                let event = match delivery.check_and_deliver(&app, &engine) {
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
            })
            .map(|_| ())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

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
        let scheduler = ConnectQueueScheduler { wake_sender };

        scheduler.wake();
        scheduler.wake();

        assert_eq!(receiver.try_recv(), Ok(()));
        assert!(receiver.try_recv().is_err());
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
}
