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

#[derive(Clone, Debug, Serialize)]
struct ScheduledCheckEvent {
    ok: bool,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct PollingStatus {
    pub interval_minutes: u64,
    pub next_check_unix_ms: u64,
}

#[derive(Clone)]
pub struct PollScheduler {
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

impl PollScheduler {
    pub fn new(interval_minutes: u64) -> Self {
        Self {
            interval_minutes,
            next_check_unix_ms: Arc::new(AtomicU64::new(next_check_unix_ms(
                SystemTime::now(),
                interval_minutes,
            ))),
        }
    }

    pub fn status(&self) -> PollingStatus {
        PollingStatus {
            interval_minutes: self.interval_minutes,
            next_check_unix_ms: self.next_check_unix_ms.load(Ordering::Relaxed),
        }
    }

    pub fn start(
        &self,
        app: AppHandle,
        engine: Engine,
        delivery: NotificationDelivery,
    ) -> io::Result<()> {
        let scheduler = self.clone();
        let interval = Duration::from_secs(self.interval_minutes.saturating_mul(60));
        thread::Builder::new()
            .name("email-watcher-poll".into())
            .spawn(move || loop {
                thread::sleep(interval);
                let ok = match delivery.check_and_deliver(&app, &engine) {
                    Ok(outcome) => {
                        if outcome.delivery.failed > 0 {
                            eprintln!(
                                "{} scheduled notifications remain queued after delivery errors",
                                outcome.delivery.failed
                            );
                        }
                        true
                    }
                    Err(error) => {
                        eprintln!(
                            "scheduled watcher check failed ({}): {}",
                            error.code, error.message
                        );
                        false
                    }
                };
                scheduler.next_check_unix_ms.store(
                    next_check_unix_ms(SystemTime::now(), scheduler.interval_minutes),
                    Ordering::Relaxed,
                );
                if app.emit(SCHEDULED_CHECK_EVENT, ScheduledCheckEvent { ok }).is_err() {
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
        let scheduler = PollScheduler::new(120);
        let status = scheduler.status();
        assert_eq!(status.interval_minutes, 120);
        assert!(status.next_check_unix_ms > 0);
    }
}
