use super::{present_nullable, Engine, EngineError, InboxMessageRef, ThreadRef};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::num::NonZeroU64;

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(try_from = "u16", into = "u16")]
pub struct ConversationLimit(u16);
impl TryFrom<u16> for ConversationLimit {
    type Error = &'static str;
    fn try_from(value: u16) -> Result<Self, Self::Error> {
        if (1..=100).contains(&value) {
            Ok(Self(value))
        } else {
            Err("conversation limit must be between 1 and 100")
        }
    }
}
impl From<ConversationLimit> for u16 {
    fn from(value: ConversationLimit) -> Self {
        value.0
    }
}
fn default_limit() -> ConversationLimit {
    ConversationLimit(25)
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Priority {
    Urgent,
    High,
    Normal,
    Low,
    Untriaged,
}
#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Category {
    Invoice,
    Scheduling,
    CustomerRequest,
    AutomatedNotice,
    Informational,
    Other,
    Unclassified,
}
#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Pending,
    Analyzed,
    Summarized,
    Skipped,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct InboxConversationQuery {
    #[serde(default = "default_limit")]
    pub limit: ConversationLimit,
    pub cursor: Option<String>,
    pub provider: Option<String>,
    pub account_id: Option<String>,
    pub sender: Option<String>,
    pub sender_query: Option<String>,
    pub keyword: Option<String>,
    pub priority: Option<Priority>,
    pub category: Option<Category>,
    pub status: Option<Status>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    InboxConversationsV1,
}
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Header {
    pub message_ref: InboxMessageRef,
    pub subject: String,
    pub sender: String,
    #[serde(deserialize_with = "present_nullable")]
    pub sender_name: Option<String>,
    #[serde(deserialize_with = "present_nullable")]
    pub received_at: Option<String>,
}
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Sender {
    pub address: String,
    #[serde(deserialize_with = "present_nullable")]
    pub name: Option<String>,
}
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Counts {
    pub messages: NonZeroU64,
    pub matching_messages: NonZeroU64,
    pub pending_messages: u64,
    pub analysis_errors: u64,
    pub action_required: u64,
    pub attachments: u64,
    pub truncated_messages: u64,
    pub unknown_truncation_messages: u64,
}
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Conversation {
    pub thread_ref: ThreadRef,
    pub latest: Header,
    pub matching_message_ref: InboxMessageRef,
    pub senders: Vec<Sender>,
    pub counts: Counts,
}
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Totals {
    pub conversations: u64,
    pub messages: u64,
    pub matching_messages: u64,
}
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct InboxConversationPage {
    pub kind: Kind,
    pub items: Vec<Conversation>,
    #[serde(deserialize_with = "present_nullable")]
    pub next_cursor: Option<String>,
    pub totals: Totals,
}

fn protocol_error() -> EngineError {
    EngineError::host(
        "engine_protocol_error",
        "Invalid Inbox conversation projection",
    )
}

fn valid_identity(value: &Option<String>) -> bool {
    value.as_ref().is_none_or(|s| {
        s.len() == 64
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
impl InboxConversationPage {
    pub(super) fn validate(&self) -> Result<(), EngineError> {
        const MAX: u64 = (1 << 53) - 1;
        let t = &self.totals;
        if self.items.len() > 100
            || self.items.len() as u64 > t.conversations
            || t.conversations > t.matching_messages
            || t.matching_messages > t.messages
            || t.messages > MAX
            || t.conversations == 0 && t.messages != 0
            || self
                .next_cursor
                .as_ref()
                .is_some_and(|s| s.is_empty() || s.len() > 4096)
            || self.items.is_empty() && self.next_cursor.is_some()
        {
            return Err(protocol_error());
        }
        let mut keys = BTreeSet::new();
        let (mut messages, mut matching) = (0, 0);
        for row in &self.items {
            let c = &row.counts;
            let key = serde_json::to_string(&row.thread_ref).map_err(|_| protocol_error())?;
            let senders: BTreeSet<_> = row.senders.iter().map(|s| (&s.address, &s.name)).collect();
            if !keys.insert(key)
                || !valid_identity(&row.thread_ref.mailbox_identity_key)
                || senders.len() != row.senders.len()
                || !senders.contains(&(&row.latest.sender, &row.latest.sender_name))
                || row.thread_ref.thread_key.is_empty()
                || row.senders.is_empty()
                || !row.latest.message_ref.matches_scope(&row.thread_ref)
                || !row.matching_message_ref.matches_scope(&row.thread_ref)
                || row.latest.message_ref.message_id.is_empty()
                || row.matching_message_ref.message_id.is_empty()
                || row.thread_ref.provider.is_empty()
                || row.thread_ref.account_id.is_empty()
                || c.messages.get() > MAX
                || c.attachments > MAX
                || [
                    c.matching_messages.get(),
                    c.pending_messages,
                    c.analysis_errors,
                    c.action_required,
                    c.truncated_messages,
                    c.unknown_truncation_messages,
                ]
                .iter()
                .any(|n| *n > c.messages.get())
                || c.truncated_messages + c.unknown_truncation_messages > c.messages.get()
            {
                return Err(protocol_error());
            }
            messages += c.messages.get();
            matching += c.matching_messages.get();
        }
        if messages > t.messages || matching > t.matching_messages {
            return Err(protocol_error());
        }
        Ok(())
    }
}
impl Engine {
    pub fn query_inbox_conversations(
        &self,
        query: InboxConversationQuery,
    ) -> Result<InboxConversationPage, EngineError> {
        let limit = query.limit.0;
        let page: InboxConversationPage = self.request(
            "inbox.conversations.query",
            serde_json::to_value(query).map_err(|_| protocol_error())?,
        )?;
        page.validate()?;
        if page.items.len() > usize::from(limit) {
            return Err(protocol_error());
        }
        Ok(page)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};

    fn fixture() -> Value {
        serde_json::from_str(include_str!(
            "../../../../tests/data/inbox_conversation_page.json"
        ))
        .unwrap()
    }
    fn admitted(value: Value) -> bool {
        serde_json::from_value::<InboxConversationPage>(value)
            .is_ok_and(|page| page.validate().is_ok())
    }
    #[test]
    fn conversation_wire_accepts_real_python_fixture_and_refuses_partial_legacy_and_mixed_pages() {
        assert!(admitted(fixture()));
        for pointer in [
            "",
            "/items/0",
            "/items/0/thread_ref",
            "/items/0/latest",
            "/items/0/latest/message_ref",
            "/items/0/matching_message_ref",
            "/items/0/senders/0",
            "/items/0/counts",
            "/totals",
        ] {
            for extra in [false, true] {
                let mut bad = fixture();
                let object = bad.pointer_mut(pointer).unwrap().as_object_mut().unwrap();
                if extra {
                    object.insert("extra".into(), json!(true));
                } else {
                    let key = object.keys().next().unwrap().clone();
                    object.remove(&key);
                }
                assert!(!admitted(bad), "{pointer} extra={extra}");
            }
        }
        assert!(!admitted(json!({"items":[],"next_cursor":null})));
        let mut mixed = fixture();
        mixed["items"].as_array_mut().unwrap().push(json!({}));
        assert!(!admitted(mixed));
    }
    #[test]
    fn conversation_counts_and_complete_scopes_are_checked() {
        for bad in [
            json!(-1),
            json!(0),
            json!(false),
            json!(""),
            Value::Null,
            json!(1.5),
            json!(1u64 << 53),
        ] {
            let mut page = fixture();
            page["items"][0]["counts"]["messages"] = bad;
            assert!(!admitted(page));
        }
        for (path, bad) in [
            ("/items/0/latest/message_ref/account_id", json!("other")),
            (
                "/items/0/matching_message_ref/mailbox_identity_key",
                Value::Null,
            ),
            ("/items/0/counts/matching_messages", json!(3)),
            ("/totals/conversations", json!(0)),
            ("/items/0/latest/sender", json!("absent@example.test")),
        ] {
            let mut page = fixture();
            *page.pointer_mut(path).unwrap() = bad;
            assert!(!admitted(page));
        }
        let mut legacy = fixture();
        for path in [
            "/items/0/thread_ref/mailbox_identity_key",
            "/items/0/latest/message_ref/mailbox_identity_key",
            "/items/0/matching_message_ref/mailbox_identity_key",
        ] {
            *legacy.pointer_mut(path).unwrap() = Value::Null;
        }
        assert!(admitted(legacy));
        let mut malformed = fixture();
        for path in [
            "/items/0/thread_ref/mailbox_identity_key",
            "/items/0/latest/message_ref/mailbox_identity_key",
            "/items/0/matching_message_ref/mailbox_identity_key",
        ] {
            *malformed.pointer_mut(path).unwrap() = json!("invalid");
        }
        assert!(!admitted(malformed));
        let mut duplicate_sender = fixture();
        let sender = duplicate_sender["items"][0]["senders"][0].clone();
        duplicate_sender["items"][0]["senders"]
            .as_array_mut()
            .unwrap()
            .push(sender);
        assert!(!admitted(duplicate_sender));
    }
    #[test]
    fn conversation_query_is_closed_and_limit_cannot_represent_out_of_range_values() {
        let query: InboxConversationQuery = serde_json::from_value(json!({})).unwrap();
        assert_eq!(u16::from(query.limit), 25);
        for bad in [
            json!(0),
            json!(101),
            json!(-1),
            json!(false),
            json!(""),
            Value::Null,
        ] {
            assert!(
                serde_json::from_value::<InboxConversationQuery>(json!({"limit":bad})).is_err()
            );
        }
        assert!(serde_json::from_value::<InboxConversationQuery>(json!({"extra":true})).is_err());
        assert!(
            serde_json::from_value::<InboxConversationQuery>(json!({"priority":"wrong"})).is_err()
        );
        assert!(serde_json::from_value::<InboxConversationQuery>(json!({"limit":100})).is_ok());
    }
    #[cfg(unix)]
    #[test]
    fn conversation_request_preserves_query_and_host_rejects_cross_scope_engine_data() {
        use std::{ffi::OsString, path::PathBuf};
        let dir = tempfile::tempdir().unwrap();
        let request = dir.path().join("request.json");
        let response = dir.path().join("response.json");
        let run = |page: Value| {
            std::fs::write(
                &response,
                serde_json::to_vec(&json!({"protocol":1,"ok":true,
                "operation":"inbox.conversations.query","data":page}))
                .unwrap(),
            )
            .unwrap();
            let engine = Engine::with_command(
                "sh",
                vec![
                    OsString::from("-c"),
                    OsString::from("cat > \"$1\"; cat \"$2\""),
                    OsString::from("conversation-test"),
                    request.as_os_str().to_owned(),
                    response.as_os_str().to_owned(),
                ],
                PathBuf::from("unused.toml"),
            );
            engine.query_inbox_conversations(
                serde_json::from_value(json!({"limit":1,
                "sender":"older@example.test","account_id":"mailbox","provider":"gmail"}))
                .unwrap(),
            )
        };
        run(fixture()).unwrap();
        let captured: Value = serde_json::from_slice(&std::fs::read(&request).unwrap()).unwrap();
        assert_eq!(captured["operation"], "inbox.conversations.query");
        assert_eq!(captured["payload"]["sender"], "older@example.test");
        assert_eq!(captured["payload"]["account_id"], "mailbox");
        let mut bad = fixture();
        bad["items"][0]["latest"]["message_ref"]["account_id"] = json!("other");
        assert_eq!(run(bad).unwrap_err().code, "engine_protocol_error");
    }
}
