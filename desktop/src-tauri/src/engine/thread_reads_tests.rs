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
