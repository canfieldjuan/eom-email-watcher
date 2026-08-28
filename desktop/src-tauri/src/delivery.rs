use crate::engine::{Engine, EngineError, NotificationIntent};
use std::sync::{Arc, Mutex};
use tauri::AppHandle;
use tauri_plugin_notification::NotificationExt;

const DELIVERY_BATCH_LIMIT: u16 = 25;

trait NotificationQueue {
    fn pending(&self, limit: u16) -> Result<Vec<NotificationIntent>, EngineError>;
    fn acknowledge(&self, intent: &NotificationIntent) -> Result<(), EngineError>;
}

impl NotificationQueue for Engine {
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

fn deliver_batch(
    queue: &impl NotificationQueue,
    sink: &impl NotificationSink,
) -> Result<u64, EngineError> {
    let intents = queue.pending(DELIVERY_BATCH_LIMIT)?;
    let mut delivered = 0;
    for intent in intents {
        sink.show(&intent)?;
        queue.acknowledge(&intent)?;
        delivered += 1;
    }
    Ok(delivered)
}

#[derive(Clone, Default)]
pub struct NotificationDelivery {
    lock: Arc<Mutex<()>>,
}

impl NotificationDelivery {
    pub fn deliver(&self, app: &AppHandle, engine: &Engine) -> Result<u64, EngineError> {
        let _guard = self.lock.lock().map_err(|_| {
            EngineError::host(
                "host_error",
                "Desktop notification delivery lock is unavailable",
            )
        })?;
        deliver_batch(engine, &TauriNotificationSink { app })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FakeQueue {
        events: Arc<Mutex<Vec<&'static str>>>,
        intents: Vec<NotificationIntent>,
    }

    impl NotificationQueue for FakeQueue {
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
        fail: bool,
    }

    impl NotificationSink for FakeSink {
        fn show(&self, _intent: &NotificationIntent) -> Result<(), EngineError> {
            self.events.lock().expect("events lock").push("show");
            if self.fail {
                return Err(EngineError::host(
                    "notification_error",
                    "simulated delivery failure",
                ));
            }
            Ok(())
        }
    }

    fn intent() -> NotificationIntent {
        NotificationIntent {
            analysis_at: Some("2026-08-28T12:00:00+00:00".into()),
            body: "Private local summary".into(),
            kind: "analysis".into(),
            message_id: "message-1".into(),
            priority: "high".into(),
            title: "Watched sender: Action needed".into(),
        }
    }

    #[test]
    fn acknowledges_only_after_platform_acceptance() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let queue = FakeQueue {
            events: events.clone(),
            intents: vec![intent()],
        };
        let sink = FakeSink {
            events: events.clone(),
            fail: false,
        };

        assert_eq!(deliver_batch(&queue, &sink).expect("delivery succeeds"), 1);
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
            intents: vec![intent()],
        };
        let sink = FakeSink {
            events: events.clone(),
            fail: true,
        };

        let error = deliver_batch(&queue, &sink).expect_err("delivery must fail");
        assert_eq!(error.code, "notification_error");
        assert_eq!(*events.lock().expect("events lock"), ["show"]);
    }
}
