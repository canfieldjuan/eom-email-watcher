use super::*;
#[cfg(unix)]
use std::fs;

fn thread_ref(identity: Value) -> Value {
    json!({"provider": "imap", "account_id": "retained-account",
        "mailbox_identity_key": identity, "thread_key": "alias:thread/β"})
}

fn list_query() -> Value {
    json!({"vendor_id": "11111111-1111-4111-8111-111111111111", "provider": "imap",
        "account_id": "retained-account", "limit": 7, "cursor": "list-cursor"})
}

fn copies_query(copy: Value, identity: Value) -> Value {
    json!({"vendor_id": "11111111-1111-4111-8111-111111111111",
        "thread_ref": thread_ref(identity), "message_id": "canonical-message",
        "copy_ref": copy, "limit": 3, "cursor": "copy-cursor"})
}

fn list_page() -> Value {
    json!({"items": [
        {"thread_ref": thread_ref(json!("a".repeat(64))), "relation": "owned",
            "owner_vendor": {"vendor_id": "owner", "display_name": "Owner"},
            "subject": "<literal subject>", "latest_received_at": "2026-01-02T00:00:00+00:00",
            "message_count": 2},
        {"thread_ref": thread_ref(Value::Null), "relation": "linked",
            "owner_vendor": {"vendor_id": "other", "display_name": "Other owner"},
            "subject": "No subject", "latest_received_at": null, "message_count": 1}
    ], "next_cursor": "next-list-page"})
}

fn copies_page(inventory: bool) -> Value {
    let items = if inventory {
        json!([
            {"part_id": "part-1", "filename": "invoice.pdf", "media_type": "application/pdf",
                "byte_size": 123},
            {"part_id": "part-2", "filename": "unknown.txt", "media_type": "text/plain",
                "byte_size": null}
        ])
    } else {
        json!([
            {"copy_ref": "source-1", "locations": ["INBOX", "Sent"], "summary": "Saved summary",
                "attachment_count": 2},
            {"copy_ref": "location-only", "locations": ["Archive"], "summary": null,
                "attachment_count": 0}
        ])
    };
    let mut resolved = thread_ref(Value::Null);
    resolved["thread_key"] = json!("resolved-survivor");
    json!({"thread_ref": resolved, "message_id": "canonical-message",
        "items": items, "next_cursor": null})
}

#[test]
fn thread_reads_requests_preserve_complete_scope_and_defaults() {
    let input = list_query();
    let query: ThreadListQuery = serde_json::from_value(input.clone()).unwrap();
    assert_eq!(serde_json::to_value(query).unwrap(), input);
    let query: ThreadListQuery = serde_json::from_value(json!({"vendor_id": "vendor"})).unwrap();
    assert_eq!(
        serde_json::to_value(query).unwrap(),
        json!({"vendor_id": "vendor",
        "provider": null, "account_id": null, "limit": 25, "cursor": null})
    );
    for identity in [Value::Null, json!("b".repeat(64))] {
        let input = copies_query(json!("source-1"), identity);
        let query: ThreadCopiesQuery = serde_json::from_value(input.clone()).unwrap();
        assert_eq!(serde_json::to_value(query).unwrap(), input);
    }
    let mut input = copies_query(Value::Null, Value::Null);
    for key in ["copy_ref", "limit", "cursor"] {
        input.as_object_mut().unwrap().remove(key);
    }
    let query: ThreadCopiesQuery = serde_json::from_value(input).unwrap();
    assert_eq!(query.limit, 25);
    assert_eq!(query.copy_ref, None);
    assert_eq!(query.cursor, None);
}

#[test]
fn thread_reads_requests_reject_omitted_scope_unknown_fields_and_wrong_types() {
    for field in [
        "provider",
        "account_id",
        "mailbox_identity_key",
        "thread_key",
    ] {
        let mut input = copies_query(Value::Null, Value::Null);
        input["thread_ref"].as_object_mut().unwrap().remove(field);
        assert!(
            serde_json::from_value::<ThreadCopiesQuery>(input).is_err(),
            "missing {field}"
        );
    }
    for pointer in ["/unexpected", "/thread_ref/unexpected"] {
        let mut input = copies_query(Value::Null, Value::Null);
        let (parent, field) = pointer.rsplit_once('/').unwrap();
        input.pointer_mut(parent).unwrap()[field] = json!("unrecognized scope");
        assert!(serde_json::from_value::<ThreadCopiesQuery>(input).is_err());
    }
    let mut input = list_query();
    input["mailbox_identity_key"] = json!("not a supported list selector");
    assert!(serde_json::from_value::<ThreadListQuery>(input).is_err());
    for wrong in [json!(true), json!(1.5), json!("25"), Value::Null] {
        let mut input = list_query();
        input["limit"] = wrong.clone();
        assert!(serde_json::from_value::<ThreadListQuery>(input).is_err());
        let mut input = copies_query(Value::Null, Value::Null);
        input["limit"] = wrong;
        assert!(serde_json::from_value::<ThreadCopiesQuery>(input).is_err());
    }
}

#[test]
fn thread_reads_response_types_reject_silent_shape_loss() {
    for pointer in [
        "/items/0/thread_ref/mailbox_identity_key",
        "/items/0/latest_received_at",
        "/next_cursor",
    ] {
        let mut input = list_page();
        let (parent, field) = pointer.rsplit_once('/').unwrap();
        input
            .pointer_mut(parent)
            .unwrap()
            .as_object_mut()
            .unwrap()
            .remove(field);
        assert!(
            serde_json::from_value::<ThreadPage>(input).is_err(),
            "missing {pointer}"
        );
    }
    for pointer in [
        "",
        "/items/0",
        "/items/0/thread_ref",
        "/items/0/owner_vendor",
    ] {
        let mut input = list_page();
        input.pointer_mut(pointer).unwrap()["body"] = json!("unexpected content");
        assert!(serde_json::from_value::<ThreadPage>(input).is_err());
    }
    let mut input = list_page();
    input["items"][0]["relation"] = json!("unrelated");
    assert!(serde_json::from_value::<ThreadPage>(input).is_err());
    assert!(serde_json::from_value::<ThreadCopyPage<ThreadCopy>>(copies_page(true)).is_err());
    assert!(serde_json::from_value::<ThreadCopyPage<ThreadInventory>>(copies_page(false)).is_err());
    for inventory in [false, true] {
        let mut input = copies_page(inventory);
        let nullable = if inventory { "byte_size" } else { "summary" };
        input["items"][0].as_object_mut().unwrap().remove(nullable);
        let invalid = if inventory {
            serde_json::from_value::<ThreadCopyPage<ThreadInventory>>(input).is_err()
        } else {
            serde_json::from_value::<ThreadCopyPage<ThreadCopy>>(input).is_err()
        };
        assert!(invalid, "missing {nullable}");
    }
}

#[cfg(unix)]
fn process_fixture(directory: &Path, response: Value) -> Engine {
    let response_path = directory.join("response.json");
    fs::write(&response_path, serde_json::to_vec(&response).unwrap()).unwrap();
    Engine::with_command(
        "sh",
        vec![
            "-c".into(),
            "cat > \"$1\"; cat \"$2\"".into(),
            "thread-read-fixture".into(),
            directory.join("request.json").into_os_string(),
            response_path.into_os_string(),
        ],
        directory.join("unused.toml"),
    )
}

#[cfg(unix)]
fn captured_payload(directory: &Path, operation: &str) -> Value {
    let request: Value =
        serde_json::from_slice(&fs::read(directory.join("request.json")).unwrap()).unwrap();
    assert_eq!(request["protocol"], 1);
    assert_eq!(request["operation"], operation);
    request["payload"].clone()
}

#[cfg(unix)]
#[test]
fn thread_reads_process_roundtrip_preserves_requests_and_frontend_data() {
    let dir = tempfile::tempdir().unwrap();
    let response = list_page();
    let engine = process_fixture(
        dir.path(),
        json!({"protocol": 1, "ok": true,
        "operation": "threads.list", "data": response}),
    );
    let actual = engine
        .list_threads(serde_json::from_value(list_query()).unwrap())
        .unwrap();
    assert_eq!(serde_json::to_value(actual).unwrap(), response);
    assert_eq!(captured_payload(dir.path(), "threads.list"), list_query());
    for (inventory, identity) in [
        (false, Value::Null),
        (true, Value::Null),
        (false, json!("b".repeat(64))),
        (true, json!("b".repeat(64))),
    ] {
        for empty in [false, true] {
            let mut response = copies_page(inventory);
            response["thread_ref"]["mailbox_identity_key"] = identity.clone();
            if empty {
                response["items"] = json!([]);
            }
            let engine = process_fixture(
                dir.path(),
                json!({"protocol": 1, "ok": true,
                "operation": "threads.copies", "data": response}),
            );
            let query = copies_query(
                if inventory {
                    json!("source-1")
                } else {
                    Value::Null
                },
                identity.clone(),
            );
            let actual = engine
                .thread_copies(serde_json::from_value(query.clone()).unwrap())
                .unwrap();
            assert_eq!(serde_json::to_value(actual).unwrap(), response);
            assert_eq!(captured_payload(dir.path(), "threads.copies"), query);
        }
    }
}

#[cfg(unix)]
#[test]
fn thread_reads_process_rejects_wrong_copy_variant_and_mixed_items() {
    let dir = tempfile::tempdir().unwrap();
    for inventory in [false, true] {
        for mixed in [false, true] {
            let mut response = copies_page(!inventory);
            if mixed {
                response["items"]
                    .as_array_mut()
                    .unwrap()
                    .push(copies_page(inventory)["items"][0].clone());
            }
            let engine = process_fixture(
                dir.path(),
                json!({"protocol": 1, "ok": true,
                "operation": "threads.copies", "data": response}),
            );
            let query = copies_query(
                if inventory {
                    json!("source-1")
                } else {
                    Value::Null
                },
                Value::Null,
            );
            let error = engine
                .thread_copies(serde_json::from_value(query).unwrap())
                .unwrap_err();
            assert_eq!(error.code, "engine_protocol_error");
        }
    }
}

#[cfg(unix)]
#[test]
fn thread_reads_process_preserves_local_read_errors() {
    let dir = tempfile::tempdir().unwrap();
    for operation in ["threads.list", "threads.copies"] {
        for code in ["stale_cursor", "not_found", "invalid_request"] {
            let engine = process_fixture(
                dir.path(),
                json!({"protocol": 1, "ok": false,
                "operation": operation, "error": {"code": code, "message": "Local history changed"}}),
            );
            let error = if operation == "threads.list" {
                engine
                    .list_threads(serde_json::from_value(list_query()).unwrap())
                    .unwrap_err()
            } else {
                engine
                    .thread_copies(
                        serde_json::from_value(copies_query(Value::Null, Value::Null)).unwrap(),
                    )
                    .unwrap_err()
            };
            assert_eq!(error.code, code);
            assert_eq!(error.message, "Local history changed");
        }
    }
}

fn message_query() -> Value {
    json!({"vendor_id": "vendor", "thread_ref": thread_ref(json!("a".repeat(64))),
        "limit": 7, "cursor": "message-cursor"})
}

fn body_query() -> Value {
    json!({"vendor_id": "vendor", "thread_ref": thread_ref(Value::Null),
        "message_id": "canonical-message"})
}

fn message_page(state: Value) -> Value {
    json!({"thread": {"thread_ref": thread_ref(Value::Null), "relation": "linked",
        "owner_vendor": {"vendor_id": "owner", "display_name": "Owner"},
        "subject": "Newest visible subject <literal> β",
        "history_notice": "History partial — only locally retained messages are shown"},
        "items": [{"message_id": "canonical-message", "subject": "<literal>",
            "direction": "outbound", "sender": "me@example.com", "sender_name": null,
            "attributed_vendor": null, "received_at": null, "body_state": state, "copy_count": 2}],
        "next_cursor": "next-message-page"})
}

fn body_response(body: Value) -> Value {
    let mut resolved = thread_ref(Value::Null);
    resolved["thread_key"] = json!("resolved-survivor");
    json!({"thread_ref": resolved, "message_id": "canonical-message", "body": body})
}

const UNSTORED_STATES: [&str; 5] = [
    "pending",
    "source_gone",
    "outside_folders",
    "removal_pending",
    "mailbox_identity_unavailable",
];

#[test]
fn thread_reads_message_body_responses_reject_silent_shape_loss() {
    let mut valid = message_page(json!({"state": "pending"}));
    valid["next_cursor"] = Value::Null;
    assert!(serde_json::from_value::<ThreadMessagePage>(valid.clone()).is_ok());
    for pointer in ["", "/thread", "/thread/owner_vendor", "/items/0"] {
        let mut input = valid.clone();
        input.pointer_mut(pointer).unwrap()["body"] = json!("unexpected content");
        assert!(
            serde_json::from_value::<ThreadMessagePage>(input).is_err(),
            "unknown field at {pointer}"
        );
    }
    valid.as_object_mut().unwrap().remove("next_cursor");
    assert!(serde_json::from_value::<ThreadMessagePage>(valid).is_err());
    let mut response = body_response(json!({"state": "pending"}));
    assert!(serde_json::from_value::<ThreadBodyResponse>(response.clone()).is_ok());
    response["unexpected"] = json!("unexpected content");
    assert!(serde_json::from_value::<ThreadBodyResponse>(response).is_err());
}

#[test]
fn thread_reads_message_context_requires_a_string_title() {
    let valid = message_page(json!({"state": "pending"}));
    let decoded: ThreadMessagePage = serde_json::from_value(valid.clone()).unwrap();
    assert_eq!(decoded.thread.subject, "Newest visible subject <literal> β");
    let mut missing = valid.clone();
    missing["thread"].as_object_mut().unwrap().remove("subject");
    assert!(serde_json::from_value::<ThreadMessagePage>(missing).is_err());
    for value in [Value::Null, json!(1), json!(true), json!([]), json!({})] {
        let mut invalid = valid.clone();
        invalid["thread"]["subject"] = value;
        assert!(serde_json::from_value::<ThreadMessagePage>(invalid).is_err());
    }
}

#[test]
fn thread_reads_message_body_queries_are_closed_and_preserve_scope() {
    let query: ThreadMessagesQuery = serde_json::from_value(message_query()).unwrap();
    assert_eq!(serde_json::to_value(query).unwrap(), message_query());
    let query: ThreadBodyQuery = serde_json::from_value(body_query()).unwrap();
    assert_eq!(serde_json::to_value(query).unwrap(), body_query());
    let mut input = message_query();
    input.as_object_mut().unwrap().remove("limit");
    input.as_object_mut().unwrap().remove("cursor");
    let query: ThreadMessagesQuery = serde_json::from_value(input).unwrap();
    assert_eq!(query.limit, 25);
    assert_eq!(query.cursor, None);
    for field in ["vendor_id", "thread_ref", "message_id"] {
        let mut input = body_query();
        input.as_object_mut().unwrap().remove(field);
        assert!(serde_json::from_value::<ThreadBodyQuery>(input).is_err());
    }
    for field in ["limit", "cursor", "copy_ref"] {
        let mut input = body_query();
        input[field] = json!(1);
        assert!(serde_json::from_value::<ThreadBodyQuery>(input).is_err());
    }
    let mut input = message_query();
    input["message_id"] = json!("not a message selector");
    assert!(serde_json::from_value::<ThreadMessagesQuery>(input).is_err());
}

#[test]
fn thread_reads_body_discriminators_refuse_unknown_missing_or_extra_fields() {
    for state in UNSTORED_STATES {
        let input = json!({"state": state});
        assert!(serde_json::from_value::<ThreadBody>(input.clone()).is_ok());
        assert!(serde_json::from_value::<ThreadBodyState>(input.clone()).is_ok());
        for field in ["text", "stored_chars", "source_chars", "unexpected"] {
            let mut extra = input.clone();
            extra[field] = json!("untrusted text");
            assert!(
                serde_json::from_value::<ThreadBody>(extra.clone()).is_err(),
                "{extra}"
            );
            assert!(serde_json::from_value::<ThreadBodyState>(extra).is_err());
        }
    }
    for input in [
        json!({}),
        json!({"state": "unknown"}),
        json!({"state": "stored"}),
        json!({"state": "stored", "text": null, "stored_chars": 0, "source_chars": 0}),
    ] {
        assert!(serde_json::from_value::<ThreadBody>(input).is_err());
    }
    assert!(
        serde_json::from_value::<ThreadBodyState>(json!({"state": "stored",
        "text": "not list metadata", "stored_chars": 17, "source_chars": 17}))
        .is_err()
    );
    for field in ["sender_name", "attributed_vendor", "received_at"] {
        let mut input = message_page(json!({"state": "pending"}));
        input["items"][0].as_object_mut().unwrap().remove(field);
        assert!(
            serde_json::from_value::<ThreadMessagePage>(input).is_err(),
            "missing {field}"
        );
    }
    let mut input = message_page(json!({"state": "pending"}));
    input["items"][0]["direction"] = json!("unknown");
    assert!(serde_json::from_value::<ThreadMessagePage>(input).is_err());
}

#[cfg(unix)]
#[test]
fn thread_reads_body_and_messages_process_roundtrip_preserves_all_fields() {
    let dir = tempfile::tempdir().unwrap();
    let mut bodies: Vec<Value> = UNSTORED_STATES
        .iter()
        .map(|state| json!({"state": state}))
        .collect();
    for (text, chars, source) in [
        ("".into(), 0, 0),
        ("🙂e\u{301}".into(), 3, 8),
        ("x".repeat(200_000), 200_000, 200_009),
    ] {
        bodies.push(
            json!({"state": "stored", "text": text, "stored_chars": chars, "source_chars": source}),
        );
    }
    for body in bodies {
        let response = body_response(body.clone());
        let engine = process_fixture(
            dir.path(),
            json!({"protocol": 1, "ok": true,
            "operation": "threads.body", "data": response}),
        );
        let actual = engine
            .thread_body(serde_json::from_value(body_query()).unwrap())
            .unwrap();
        assert_eq!(serde_json::to_value(actual).unwrap(), response);
        assert_eq!(captured_payload(dir.path(), "threads.body"), body_query());
        let mut state = body;
        state.as_object_mut().unwrap().remove("text");
        let response = message_page(state);
        let engine = process_fixture(
            dir.path(),
            json!({"protocol": 1, "ok": true,
            "operation": "threads.messages", "data": response}),
        );
        let actual = engine
            .thread_messages(serde_json::from_value(message_query()).unwrap())
            .unwrap();
        assert_eq!(serde_json::to_value(actual).unwrap(), response);
        assert_eq!(
            captured_payload(dir.path(), "threads.messages"),
            message_query()
        );
    }
}

#[cfg(unix)]
#[test]
fn thread_reads_body_process_refuses_invalid_text_lengths_and_cap() {
    let dir = tempfile::tempdir().unwrap();
    for body in [
        json!({"state": "stored", "text": "🙂", "stored_chars": 4, "source_chars": 4}),
        json!({"state": "stored", "text": "🙂", "stored_chars": 2, "source_chars": 2}),
        json!({"state": "stored", "text": "body", "stored_chars": 4, "source_chars": 3}),
        json!({"state": "stored", "text": "x".repeat(200_001), "stored_chars": 200_001, "source_chars": 200_001}),
        json!({"state": "stored", "text": "x".repeat(200_001), "stored_chars": 200_000, "source_chars": 200_001}),
        json!({"state": "stored", "text": "", "stored_chars": -1, "source_chars": 0}),
        json!({"state": "stored", "text": "", "stored_chars": true, "source_chars": 0}),
        json!({"state": "stored", "text": "", "stored_chars": 0.5, "source_chars": 1}),
        json!({"state": "pending", "text": "old text"}),
        json!({"state": "unknown", "text": "old text"}),
    ] {
        let engine = process_fixture(
            dir.path(),
            json!({"protocol": 1, "ok": true,
            "operation": "threads.body", "data": body_response(body)}),
        );
        let error = engine
            .thread_body(serde_json::from_value(body_query()).unwrap())
            .unwrap_err();
        assert_eq!(error.code, "engine_protocol_error");
    }
    for state in [
        json!({"state": "stored", "stored_chars": 4, "source_chars": 3}),
        json!({"state": "stored", "stored_chars": 200_001, "source_chars": 200_001}),
    ] {
        let engine = process_fixture(
            dir.path(),
            json!({"protocol": 1, "ok": true,
            "operation": "threads.messages", "data": message_page(state)}),
        );
        let error = engine
            .thread_messages(serde_json::from_value(message_query()).unwrap())
            .unwrap_err();
        assert_eq!(error.code, "engine_protocol_error");
    }
}

#[cfg(unix)]
#[test]
fn thread_reads_message_body_errors_are_not_successful_body_states() {
    let dir = tempfile::tempdir().unwrap();
    for operation in ["threads.messages", "threads.body"] {
        for code in [
            "stale_cursor",
            "not_found",
            "invalid_request",
            "runtime_error",
        ] {
            let engine = process_fixture(
                dir.path(),
                json!({"protocol": 1, "ok": false,
                "operation": operation, "error": {"code": code, "message": "Read failed"}}),
            );
            let error = if operation == "threads.body" {
                engine
                    .thread_body(serde_json::from_value(body_query()).unwrap())
                    .unwrap_err()
            } else {
                engine
                    .thread_messages(serde_json::from_value(message_query()).unwrap())
                    .unwrap_err()
            };
            assert_eq!(error.code, code);
            assert_eq!(error.message, "Read failed");
        }
    }
}
