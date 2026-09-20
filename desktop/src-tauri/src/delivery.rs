use crate::engine::{CheckResult, Engine, EngineError, NotificationIntent};
use std::sync::{
    Arc, Mutex, MutexGuard, TryLockError,
    atomic::{AtomicBool, Ordering},
    mpsc,
};
use std::thread;
use std::time::{Duration, Instant};
use tauri::AppHandle;
use tauri_plugin_notification::NotificationExt;

const DELIVERY_BATCH_LIMIT: u16 = 25;
const DEFAULT_DELIVERY_OPERATION_TIMEOUT: Duration = Duration::from_secs(30 * 60);
const DELIVERY_LOCK_RETRY: Duration = Duration::from_millis(5);

fn delivery_timeout() -> EngineError {
    EngineError::host("engine_timeout", "Desktop notification delivery timed out")
}

#[derive(Clone)]
pub(crate) struct DeliveryDeadline {
    deadline: Instant,
    cancelled: Arc<AtomicBool>,
}

impl DeliveryDeadline {
    fn new(timeout: Duration) -> Self {
        let now = Instant::now();
        Self {
            deadline: now.checked_add(timeout).unwrap_or(now),
            cancelled: Arc::new(AtomicBool::new(false)),
        }
    }

    fn cancel(&self) {
        self.cancelled.store(true, Ordering::SeqCst);
    }

    pub(crate) fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::SeqCst)
    }

    pub(crate) fn remaining(&self) -> Result<Duration, EngineError> {
        if self.is_cancelled() {
            return Err(delivery_timeout());
        }
        self.deadline
            .checked_duration_since(Instant::now())
            .filter(|remaining| !remaining.is_zero())
            .ok_or_else(delivery_timeout)
    }

    fn check(&self) -> Result<(), EngineError> {
        self.remaining().map(|_| ())
    }

    fn bounded_engine(&self, engine: &Engine) -> Result<Engine, EngineError> {
        Ok(engine.with_request_timeout(self.remaining()?))
    }
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum BoundedOperation<T> {
    Completed(T),
    TimedOut,
    WorkerStopped,
}

pub(crate) fn run_bounded_operation<T: Send + 'static>(
    timeout: Duration,
    operation: impl FnOnce(DeliveryDeadline) -> T + Send + 'static,
) -> BoundedOperation<T> {
    let deadline = DeliveryDeadline::new(timeout);
    let worker_deadline = deadline.clone();
    let (sender, receiver) = mpsc::sync_channel(1);
    let worker = thread::Builder::new()
        .name("email-watcher-bounded-delivery".into())
        .spawn(move || {
            let outcome = operation(worker_deadline);
            let _ = sender.send(outcome);
        });
    let Ok(worker) = worker else {
        return BoundedOperation::WorkerStopped;
    };

    let remaining = deadline.remaining().unwrap_or(Duration::ZERO);
    match receiver.recv_timeout(remaining) {
        Ok(outcome) => match worker.join() {
            Ok(()) => BoundedOperation::Completed(outcome),
            Err(_) => BoundedOperation::WorkerStopped,
        },
        Err(mpsc::RecvTimeoutError::Timeout) => {
            deadline.cancel();
            match worker.join() {
                Ok(()) => BoundedOperation::TimedOut,
                Err(_) => BoundedOperation::WorkerStopped,
            }
        }
        Err(mpsc::RecvTimeoutError::Disconnected) => {
            let _ = worker.join();
            BoundedOperation::WorkerStopped
        }
    }
}

trait NotificationQueue {
    fn check(&self) -> Result<CheckResult, EngineError>;
    fn pending(&self, limit: u16) -> Result<Vec<NotificationIntent>, EngineError>;
    fn pending_count(&self) -> Result<u64, EngineError>;
    fn acknowledge(&self, intent: &NotificationIntent) -> Result<(), EngineError>;
}

impl NotificationQueue for Engine {
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

struct DeadlineQueue<'a> {
    engine: &'a Engine,
    deadline: &'a DeliveryDeadline,
}

impl DeadlineQueue<'_> {
    fn call<T>(
        &self,
        operation: impl FnOnce(&Engine) -> Result<T, EngineError>,
    ) -> Result<T, EngineError> {
        let engine = self.deadline.bounded_engine(self.engine)?;
        let outcome = operation(&engine)?;
        self.deadline.check()?;
        Ok(outcome)
    }
}

impl NotificationQueue for DeadlineQueue<'_> {
    fn check(&self) -> Result<CheckResult, EngineError> {
        self.call(Engine::check)
    }

    fn pending(&self, limit: u16) -> Result<Vec<NotificationIntent>, EngineError> {
        self.call(|engine| engine.pending_notifications_under_host_lock(limit))
    }

    fn pending_count(&self) -> Result<u64, EngineError> {
        self.call(Engine::pending_notification_count_under_host_lock)
    }

    fn acknowledge(&self, intent: &NotificationIntent) -> Result<(), EngineError> {
        self.call(|engine| engine.acknowledge_notification(intent))
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

fn deliver_batch_until(
    queue: &impl NotificationQueue,
    sink: &impl NotificationSink,
    deadline: &DeliveryDeadline,
) -> Result<DeliveryOutcome, EngineError> {
    deadline.check()?;
    let intents = queue.pending(DELIVERY_BATCH_LIMIT)?;
    deadline.check()?;
    let mut delivered = 0;
    let mut failed = 0;
    for intent in intents {
        deadline.check()?;
        if sink.show(&intent).is_err() {
            failed += 1;
            continue;
        }
        deadline.check()?;
        if queue.acknowledge(&intent).is_err() {
            failed += 1;
            continue;
        }
        delivered += 1;
    }
    deadline.check()?;
    let remaining = queue.pending_count()?;
    deadline.check()?;
    Ok(DeliveryOutcome {
        delivered,
        failed,
        remaining,
    })
}

#[cfg(test)]
fn deliver_batch(
    queue: &impl NotificationQueue,
    sink: &impl NotificationSink,
) -> Result<DeliveryOutcome, EngineError> {
    deliver_batch_until(
        queue,
        sink,
        &DeliveryDeadline::new(DEFAULT_DELIVERY_OPERATION_TIMEOUT),
    )
}

#[cfg(test)]
fn check_and_deliver(
    queue: &impl NotificationQueue,
    sink: &impl NotificationSink,
) -> Result<CoordinatedCheck, EngineError> {
    let deadline = DeliveryDeadline::new(DEFAULT_DELIVERY_OPERATION_TIMEOUT);
    let check = queue.check();
    let delivery = deliver_batch_until(queue, sink, &deadline);
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
    fn lock_until(&self, deadline: &DeliveryDeadline) -> Result<MutexGuard<'_, ()>, EngineError> {
        loop {
            deadline.check()?;
            match self.lock.try_lock() {
                Ok(guard) => return Ok(guard),
                Err(TryLockError::Poisoned(_)) => {
                    return Err(EngineError::host(
                        "host_error",
                        "Desktop notification delivery lock is unavailable",
                    ));
                }
                Err(TryLockError::WouldBlock) => {
                    thread::sleep(deadline.remaining()?.min(DELIVERY_LOCK_RETRY));
                }
            }
        }
    }

    fn run_exclusive_until<T>(
        &self,
        deadline: &DeliveryDeadline,
        operation: impl FnOnce(&DeliveryDeadline) -> Result<T, EngineError>,
    ) -> Result<T, EngineError> {
        let _guard = self.lock_until(deadline)?;
        deadline.check()?;
        let outcome = operation(deadline)?;
        deadline.check()?;
        Ok(outcome)
    }

    #[cfg(test)]
    fn run_exclusive<T>(
        &self,
        operation: impl FnOnce() -> Result<T, EngineError>,
    ) -> Result<T, EngineError> {
        self.run_exclusive_until(
            &DeliveryDeadline::new(DEFAULT_DELIVERY_OPERATION_TIMEOUT),
            |_| operation(),
        )
    }

    #[cfg(test)]
    fn run_exclusive_with_timeout<T>(
        &self,
        timeout: Duration,
        operation: impl FnOnce(&DeliveryDeadline) -> Result<T, EngineError>,
    ) -> Result<T, EngineError> {
        self.run_exclusive_until(&DeliveryDeadline::new(timeout), operation)
    }

    pub fn run_engine_exclusive<T>(
        &self,
        engine: &Engine,
        operation: impl FnOnce(&Engine) -> Result<T, EngineError>,
    ) -> Result<T, EngineError> {
        self.run_exclusive_until(
            &DeliveryDeadline::new(DEFAULT_DELIVERY_OPERATION_TIMEOUT),
            |deadline| {
                let engine = deadline.bounded_engine(engine)?;
                operation(&engine)
            },
        )
    }

    fn deliver_until(
        &self,
        app: &AppHandle,
        engine: &Engine,
        deadline: &DeliveryDeadline,
    ) -> Result<DeliveryOutcome, EngineError> {
        let queue = DeadlineQueue { engine, deadline };
        self.run_exclusive_until(deadline, |_| {
            deadline
                .bounded_engine(engine)?
                .run_with_operation_lock(|| {
                    deliver_batch_until(&queue, &TauriNotificationSink { app }, deadline)
                })
        })
    }

    pub(crate) fn deliver_bounded(
        &self,
        app: AppHandle,
        engine: Engine,
        timeout: Duration,
    ) -> BoundedOperation<Result<DeliveryOutcome, EngineError>> {
        let delivery = self.clone();
        run_bounded_operation(timeout, move |deadline| {
            delivery.deliver_until(&app, &engine, &deadline)
        })
    }

    pub fn check_and_deliver(
        &self,
        app: &AppHandle,
        engine: &Engine,
    ) -> Result<CoordinatedCheck, EngineError> {
        let deadline = DeliveryDeadline::new(DEFAULT_DELIVERY_OPERATION_TIMEOUT);
        let queue = DeadlineQueue {
            engine,
            deadline: &deadline,
        };
        self.run_exclusive_until(&deadline, |_| {
            let check = queue.check();
            let delivery = deadline
                .bounded_engine(engine)?
                .run_with_operation_lock(|| {
                    deliver_batch_until(&queue, &TauriNotificationSink { app }, &deadline)
                });
            coordinated_result(check, delivery)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
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

    struct SlowPendingQueue {
        events: Arc<Mutex<Vec<&'static str>>>,
        delay: Duration,
    }

    impl NotificationQueue for SlowPendingQueue {
        fn check(&self) -> Result<CheckResult, EngineError> {
            panic!("check is not part of this delivery probe")
        }

        fn pending(&self, limit: u16) -> Result<Vec<NotificationIntent>, EngineError> {
            assert_eq!(limit, DELIVERY_BATCH_LIMIT);
            self.events.lock().expect("events lock").push("pending");
            thread::sleep(self.delay);
            Ok(vec![intent("slow")])
        }

        fn pending_count(&self) -> Result<u64, EngineError> {
            self.events
                .lock()
                .expect("events lock")
                .push("pending_count");
            Ok(1)
        }

        fn acknowledge(&self, _intent: &NotificationIntent) -> Result<(), EngineError> {
            self.events.lock().expect("events lock").push("acknowledge");
            Ok(())
        }
    }

    struct CancellationSink {
        events: Arc<Mutex<Vec<&'static str>>>,
        deadline: DeliveryDeadline,
    }

    impl NotificationSink for CancellationSink {
        fn show(&self, _intent: &NotificationIntent) -> Result<(), EngineError> {
            self.events.lock().expect("events lock").push("show");
            while !self.deadline.is_cancelled() {
                thread::yield_now();
            }
            Ok(())
        }
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

    #[test]
    fn bounded_timeout_cancels_and_joins_worker() {
        struct ActiveWorker(Arc<AtomicUsize>);

        impl Drop for ActiveWorker {
            fn drop(&mut self) {
                self.0.fetch_sub(1, Ordering::SeqCst);
            }
        }

        let active = Arc::new(AtomicUsize::new(0));
        let worker_active = Arc::clone(&active);
        let result = run_bounded_operation(Duration::from_millis(10), move |deadline| {
            worker_active.fetch_add(1, Ordering::SeqCst);
            let _worker = ActiveWorker(Arc::clone(&worker_active));
            while !deadline.is_cancelled() {
                thread::yield_now();
            }
        });

        assert_eq!(result, BoundedOperation::TimedOut);
        assert_eq!(active.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn held_delivery_lock_times_out_and_subsequent_manual_operation_works() {
        let delivery = NotificationDelivery::default();
        let active_delivery = delivery.clone();
        let (active_sender, active_receiver) = mpsc::sync_channel(1);
        let (release_sender, release_receiver) = mpsc::sync_channel(1);
        let active = thread::spawn(move || {
            active_delivery
                .run_exclusive(|| {
                    active_sender.send(()).expect("signal active lock");
                    release_receiver.recv().expect("release active lock");
                    Ok(())
                })
                .expect("active operation finishes");
        });
        active_receiver.recv().expect("active lock acquired");

        let ran = Arc::new(AtomicUsize::new(0));
        let timed_ran = Arc::clone(&ran);
        let error = delivery
            .run_exclusive_with_timeout(Duration::from_millis(10), |_| {
                timed_ran.fetch_add(1, Ordering::SeqCst);
                Ok(())
            })
            .expect_err("held lock must respect the operation deadline");
        assert_eq!(error.code, "engine_timeout");
        assert_eq!(ran.load(Ordering::SeqCst), 0);

        release_sender.send(()).expect("release active lock");
        active.join().expect("active operation joins");
        delivery
            .run_exclusive(|| {
                ran.fetch_add(1, Ordering::SeqCst);
                Ok(())
            })
            .expect("later manual operation acquires the released lock");
        assert_eq!(ran.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn slow_first_queue_call_prevents_later_delivery_calls_after_budget() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let queue = SlowPendingQueue {
            events: Arc::clone(&events),
            delay: Duration::from_millis(20),
        };
        let sink = FakeSink {
            events: Arc::clone(&events),
            failed_message: None,
        };

        let error = deliver_batch_until(
            &queue,
            &sink,
            &DeliveryDeadline::new(Duration::from_millis(5)),
        )
        .expect_err("expired batch must stop after the first call");

        assert_eq!(error.code, "engine_timeout");
        assert_eq!(*events.lock().expect("events lock"), ["pending"]);
    }

    #[test]
    fn cancellation_after_platform_acceptance_keeps_intent_for_one_retry() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let worker_events = Arc::clone(&events);
        let result = run_bounded_operation(Duration::from_millis(10), move |deadline| {
            let queue = FakeQueue {
                events: Arc::clone(&worker_events),
                intents: vec![intent("message-1")],
                check_error: false,
                check_pending: 1,
            };
            let sink = CancellationSink {
                events: Arc::clone(&worker_events),
                deadline: deadline.clone(),
            };
            deliver_batch_until(&queue, &sink, &deadline)
        });
        assert_eq!(result, BoundedOperation::TimedOut);
        assert_eq!(*events.lock().expect("events lock"), ["show"]);

        let queue = FakeQueue {
            events: Arc::clone(&events),
            intents: vec![intent("message-1")],
            check_error: false,
            check_pending: 1,
        };
        let sink = FakeSink {
            events: Arc::clone(&events),
            failed_message: None,
        };
        assert_eq!(
            deliver_batch(&queue, &sink).expect("queued intent retries"),
            DeliveryOutcome {
                delivered: 1,
                failed: 0,
                remaining: 0,
            }
        );
        assert_eq!(
            *events.lock().expect("events lock"),
            ["show", "show", "acknowledge"]
        );
    }
}
