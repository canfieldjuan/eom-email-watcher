use crate::delivery::NotificationDelivery;
use crate::engine::Engine;
use serde::Serialize;
use std::io;
use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter};

pub const SCHEDULED_CHECK_EVENT: &str = "watcher://scheduled-check";
const MAX_SLEEP_SLICE: Duration = Duration::from_secs(30);
const SCHEDULED_ENGINE_TIMEOUT: Duration = Duration::from_secs(30 * 60);

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
}
