use crate::engine::{CheckResult, Engine, EngineError, NotificationIntent};
use std::sync::{Arc, Mutex};
use tauri::AppHandle;
use tauri_plugin_notification::NotificationExt;

const DELIVERY_BATCH_LIMIT: u16 = 25;

trait NotificationQueue {
    fn check(&self) -> Result<CheckResult, EngineError>;
    fn pending(&self, limit: u16) -> Result<Vec<NotificationIntent>, EngineError>;
    fn acknowledge(&self, intent: &NotificationIntent) -> Result<(), EngineError>;
}

impl NotificationQueue for Engine {
    fn check(&self) -> Result<CheckResult, EngineError> {
        self.check()
    }

    fn pending(&self, limit: u16) -> Result<Vec<NotificationIntent>, EngineError> {
        self.pending_notifications(limit)
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
    Ok(DeliveryOutcome { delivered, failed })
}

fn check_and_deliver(
    queue: &impl NotificationQueue,
    sink: &impl NotificationSink,
) -> Result<CoordinatedCheck, EngineError> {
    let check = queue.check();
    let delivery = deliver_batch(queue, sink);
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

    pub fn deliver(
        &self,
        app: &AppHandle,
        engine: &Engine,
    ) -> Result<DeliveryOutcome, EngineError> {
        let _guard = self.lock()?;
        deliver_batch(engine, &TauriNotificationSink { app })
    }

    pub fn check_and_deliver(
        &self,
        app: &AppHandle,
        engine: &Engine,
    ) -> Result<CoordinatedCheck, EngineError> {
        let _guard = self.lock()?;
        check_and_deliver(engine, &TauriNotificationSink { app })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FakeQueue {
        events: Arc<Mutex<Vec<&'static str>>>,
        intents: Vec<NotificationIntent>,
        check_error: bool,
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
                pending_notifications: self.intents.len() as u64,
            })
        }

        fn pending(&self, limit: u16) -> Result<Vec<NotificationIntent>, EngineError> {
            assert_eq!(limit, DELIVERY_BATCH_LIMIT);
            Ok(self.intents.clone())
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
        };
        let sink = FakeSink {
            events: events.clone(),
            failed_message: None,
        };

        assert_eq!(
            deliver_batch(&queue, &sink).expect("delivery succeeds"),
            DeliveryOutcome {
                delivered: 1,
                failed: 0
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
        };
        let sink = FakeSink {
            events: events.clone(),
            failed_message: Some("message-1".into()),
        };

        assert_eq!(
            deliver_batch(&queue, &sink).expect("batch remains available"),
            DeliveryOutcome {
                delivered: 0,
                failed: 1
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
        };
        let sink = FakeSink {
            events: events.clone(),
            failed_message: Some("blocked".into()),
        };

        assert_eq!(
            deliver_batch(&queue, &sink).expect("batch remains available"),
            DeliveryOutcome {
                delivered: 1,
                failed: 1
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
}
