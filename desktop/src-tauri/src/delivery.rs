use crate::engine::{CheckResult, Engine, EngineError, NotificationIntent};
use std::sync::{Arc, Mutex};
use tauri::AppHandle;
use tauri_plugin_notification::NotificationExt;

const DELIVERY_BATCH_LIMIT: u16 = 25;

trait NotificationQueue {
    #[cfg(test)]
    fn check(&self) -> Result<CheckResult, EngineError>;
    fn pending(&self, limit: u16) -> Result<Vec<NotificationIntent>, EngineError>;
    fn pending_count(&self) -> Result<u64, EngineError>;
    fn acknowledge(&self, intent: &NotificationIntent) -> Result<(), EngineError>;
}

impl NotificationQueue for Engine {
    #[cfg(test)]
    fn check(&self) -> Result<CheckResult, EngineError> {
        self.check()
    }

    fn pending(&self, limit: u16) -> Result<Vec<NotificationIntent>, EngineError> {
        self.pending_notifications_under_host_lock(limit)
    }

    fn pending_count(&self) -> Result<u64, EngineError> {
        self.pending_notification_count_under_host_lock()
    }

    fn acknowledge(&self, intent: &NotificationIntent) -> Result<(), EngineError> {
        self.acknowledge_notification(intent)
    }
}

trait NotificationSink {
    fn show(&self, intent: &NotificationIntent) -> Result<(), EngineError>;
}

struct TauriNotificationSink<'a> {
    app: &'a AppHandle,
}

impl NotificationSink for TauriNotificationSink<'_> {
    fn show(&self, intent: &NotificationIntent) -> Result<(), EngineError> {
        self.app
            .notification()
            .builder()
            .title(&intent.title)
            .body(&intent.body)
            .show()
            .map_err(|_| {
                EngineError::host(
                    "notification_error",
                    "The desktop notification could not be delivered",
                )
            })
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct DeliveryOutcome {
    pub delivered: u64,
    pub failed: u64,
    pub remaining: u64,
}

#[derive(Debug)]
pub struct CoordinatedCheck {
    pub check: CheckResult,
    pub delivery: DeliveryOutcome,
}

fn deliver_batch(
    queue: &impl NotificationQueue,
    sink: &impl NotificationSink,
) -> Result<DeliveryOutcome, EngineError> {
    let intents = queue.pending(DELIVERY_BATCH_LIMIT)?;
    let mut delivered = 0;
    let mut failed = 0;
    for intent in intents {
        if sink.show(&intent).is_err() {
            failed += 1;
            continue;
        }
        if queue.acknowledge(&intent).is_err() {
            failed += 1;
            continue;
        }
        delivered += 1;
    }
    let remaining = queue.pending_count()?;
    Ok(DeliveryOutcome {
        delivered,
        failed,
        remaining,
    })
}

#[cfg(test)]
fn check_and_deliver(
    queue: &impl NotificationQueue,
    sink: &impl NotificationSink,
) -> Result<CoordinatedCheck, EngineError> {
    let check = queue.check();
    let delivery = deliver_batch(queue, sink);
    coordinated_result(check, delivery)
}

fn coordinated_result(
    check: Result<CheckResult, EngineError>,
    delivery: Result<DeliveryOutcome, EngineError>,
) -> Result<CoordinatedCheck, EngineError> {
    match check {
        Ok(check) => Ok(CoordinatedCheck {
            check,
            delivery: delivery?,
        }),
        Err(error) => {
            if let Err(delivery_error) = delivery {
                eprintln!(
                    "notification retry after failed check also failed ({}): {}",
                    delivery_error.code, delivery_error.message
                );
            }
            Err(error)
        }
    }
}

#[derive(Clone, Default)]
pub struct NotificationDelivery {
    lock: Arc<Mutex<()>>,
}

impl NotificationDelivery {
    fn lock(&self) -> Result<std::sync::MutexGuard<'_, ()>, EngineError> {
        self.lock.lock().map_err(|_| {
            EngineError::host(
                "host_error",
                "Desktop notification delivery lock is unavailable",
            )
        })
    }

    pub fn run_exclusive<T>(
        &self,
        operation: impl FnOnce() -> Result<T, EngineError>,
    ) -> Result<T, EngineError> {
        let _guard = self.lock()?;
        operation()
    }

    pub fn deliver(
        &self,
        app: &AppHandle,
        engine: &Engine,
    ) -> Result<DeliveryOutcome, EngineError> {
        self.run_exclusive(|| {
            engine.run_with_operation_lock(|| deliver_batch(engine, &TauriNotificationSink { app }))
        })
    }

    pub fn check_and_deliver(
        &self,
        app: &AppHandle,
        engine: &Engine,
    ) -> Result<CoordinatedCheck, EngineError> {
        self.run_exclusive(|| {
            let check = engine.check();
            let delivery = engine
                .run_with_operation_lock(|| deliver_batch(engine, &TauriNotificationSink { app }));
            coordinated_result(check, delivery)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::{self, RecvTimeoutError};
    use std::thread;
    use std::time::Duration;

    struct FakeQueue {
        events: Arc<Mutex<Vec<&'static str>>>,
        intents: Vec<NotificationIntent>,
        check_error: bool,
        check_pending: u64,
    }

    impl NotificationQueue for FakeQueue {
        fn check(&self) -> Result<CheckResult, EngineError> {
            self.events.lock().expect("events lock").push("check");
            if self.check_error {
                return Err(EngineError::host("gmail_error", "simulated check failure"));
            }
            Ok(CheckResult {
                active: true,
                discovered: 0,
                summarized: 0,
                fallback_notified: 0,
                purged: 0,
                stale_cursor_recovered: false,
                pending_notifications: self.check_pending,
                automation_processed: 0,
                automation_review_required: 0,
            })
        }

        fn pending(&self, limit: u16) -> Result<Vec<NotificationIntent>, EngineError> {
            assert_eq!(limit, DELIVERY_BATCH_LIMIT);
            Ok(self.intents.clone())
        }

        fn pending_count(&self) -> Result<u64, EngineError> {
            let acknowledged = self
                .events
                .lock()
                .expect("events lock")
                .iter()
                .filter(|event| **event == "acknowledge")
                .count();
            Ok(self.intents.len().saturating_sub(acknowledged) as u64)
        }

        fn acknowledge(&self, _intent: &NotificationIntent) -> Result<(), EngineError> {
            self.events.lock().expect("events lock").push("acknowledge");
            Ok(())
        }
    }

    struct FakeSink {
        events: Arc<Mutex<Vec<&'static str>>>,
        failed_message: Option<String>,
    }

    impl NotificationSink for FakeSink {
        fn show(&self, intent: &NotificationIntent) -> Result<(), EngineError> {
            self.events.lock().expect("events lock").push("show");
            if self.failed_message.as_deref() == Some(&intent.message_id) {
                return Err(EngineError::host(
                    "notification_error",
                    "simulated delivery failure",
                ));
            }
            Ok(())
        }
    }

    fn intent(message_id: &str) -> NotificationIntent {
        NotificationIntent {
            analysis_at: Some("2026-08-28T12:00:00+00:00".into()),
            body: "Private local summary".into(),
            kind: "analysis".into(),
            message_id: message_id.into(),
            priority: "high".into(),
            revision: Some("2026-08-28T12:00:00+00:00".into()),
            subject_id: Some(message_id.into()),
            subject_type: Some("message".into()),
            title: "Watched sender: Action needed".into(),
        }
    }

    #[test]
    fn acknowledges_only_after_platform_acceptance() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let queue = FakeQueue {
            events: events.clone(),
            intents: vec![intent("message-1")],
            check_error: false,
            check_pending: 1,
        };
        let sink = FakeSink {
            events: events.clone(),
            failed_message: None,
        };

        assert_eq!(
            deliver_batch(&queue, &sink).expect("delivery succeeds"),
            DeliveryOutcome {
                delivered: 1,
                failed: 0,
                remaining: 0,
            }
        );
        assert_eq!(
            *events.lock().expect("events lock"),
            ["show", "acknowledge"]
        );
    }

    #[test]
    fn failed_platform_delivery_remains_unacknowledged() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let queue = FakeQueue {
            events: events.clone(),
            intents: vec![intent("message-1")],
            check_error: false,
            check_pending: 1,
        };
        let sink = FakeSink {
            events: events.clone(),
            failed_message: Some("message-1".into()),
        };

        assert_eq!(
            deliver_batch(&queue, &sink).expect("batch remains available"),
            DeliveryOutcome {
                delivered: 0,
                failed: 1,
                remaining: 1,
            }
        );
        assert_eq!(*events.lock().expect("events lock"), ["show"]);
    }

    #[test]
    fn failed_intent_does_not_starve_later_intents() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let queue = FakeQueue {
            events: events.clone(),
            intents: vec![intent("blocked"), intent("deliverable")],
            check_error: false,
            check_pending: 2,
        };
        let sink = FakeSink {
            events: events.clone(),
            failed_message: Some("blocked".into()),
        };

        assert_eq!(
            deliver_batch(&queue, &sink).expect("batch remains available"),
            DeliveryOutcome {
                delivered: 1,
                failed: 1,
                remaining: 1,
            }
        );
        assert_eq!(
            *events.lock().expect("events lock"),
            ["show", "show", "acknowledge"]
        );
    }

    #[test]
    fn failed_check_still_retries_pending_delivery() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let queue = FakeQueue {
            events: events.clone(),
            intents: vec![intent("queued")],
            check_error: true,
            check_pending: 1,
        };
        let sink = FakeSink {
            events: events.clone(),
            failed_message: None,
        };

        let error = check_and_deliver(&queue, &sink).expect_err("check must fail");
        assert_eq!(error.code, "gmail_error");
        assert_eq!(
            *events.lock().expect("events lock"),
            ["check", "show", "acknowledge"]
        );
    }

    #[test]
    fn reports_post_delivery_count_instead_of_the_check_snapshot() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let queue = FakeQueue {
            events: events.clone(),
            intents: vec![intent("still-current")],
            check_error: false,
            check_pending: 2,
        };
        let sink = FakeSink {
            events,
            failed_message: None,
        };

        let outcome = check_and_deliver(&queue, &sink).expect("check and delivery succeed");

        assert_eq!(outcome.check.pending_notifications, 2);
        assert_eq!(outcome.delivery.remaining, 0);
    }

    #[test]
    fn exclusive_operations_wait_for_active_notification_delivery() {
        let delivery = NotificationDelivery::default();
        let active_delivery = delivery.clone();
        let queued_mutation = delivery.clone();
        let (active_tx, active_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let (attempt_tx, attempt_rx) = mpsc::channel();
        let (mutation_tx, mutation_rx) = mpsc::channel();

        let delivery_thread = thread::spawn(move || {
            active_delivery
                .run_exclusive(|| {
                    active_tx.send(()).expect("signal active delivery");
                    release_rx.recv().expect("release active delivery");
                    Ok(())
                })
                .expect("active delivery finishes");
        });
        active_rx.recv().expect("delivery acquired lock");
        let mutation_thread = thread::spawn(move || {
            attempt_tx.send(()).expect("signal mutation attempt");
            queued_mutation
                .run_exclusive(|| {
                    mutation_tx.send(()).expect("signal mutation");
                    Ok(())
                })
                .expect("mutation finishes");
        });
        attempt_rx.recv().expect("mutation reached delivery lock");

        assert_eq!(
            mutation_rx.recv_timeout(Duration::from_millis(50)),
            Err(RecvTimeoutError::Timeout)
        );
        release_tx.send(()).expect("release delivery");
        mutation_rx
            .recv_timeout(Duration::from_secs(1))
            .expect("mutation proceeds after delivery");
        delivery_thread.join().expect("delivery thread joins");
        mutation_thread.join().expect("mutation thread joins");
    }
}
