#[cfg(unix)]
use super::thread_reads_tests::{captured_payload, process_fixture};
use super::*;

fn origin() -> Value {
    json!({"provider":"imap", "account_id":"retained", "mailbox_identity_key":null, "message_id":"seed"})
}
fn reference() -> Value {
    json!({"provider":"imap", "account_id":"retained", "mailbox_identity_key":null, "thread_key":"alias"})
}
fn vendor() -> Value {
    json!({"vendor_id":"v", "display_name":"Vendor"})
}
fn resolved(vendor_mode: bool) -> Value {
    let thread = json!({"thread_ref":reference(), "relation":if vendor_mode {"owned"} else {"non_vendor"},
        "owner_vendor":if vendor_mode {vendor()} else {Value::Null}, "subject":"Newest", "latest_received_at":null, "message_count":1});
    json!({"state":"resolved", "message_ref":origin(), "context":if vendor_mode {
        json!({"kind":"vendor", "vendor":vendor(), "thread":thread})
    } else { json!({"kind":"non_vendor", "thread":thread}) }})
}
fn chooser() -> Value {
    json!({"state":"choose_vendor", "message_ref":origin(), "thread_ref":reference(),
        "owner_vendor":vendor(), "items":[vendor()], "next_cursor":"next"})
}
fn selector(vendor_mode: bool) -> Value {
    if vendor_mode {
        json!({"vendor_id":"v", "thread_ref":reference(), "inbox_message":origin()})
    } else {
        json!({"inbox_thread":{"thread_ref":reference(), "message_ref":origin()}})
    }
}
fn message_page(vendor_mode: bool) -> Value {
    json!({"thread":{"thread_ref":reference(), "relation":if vendor_mode {"owned"} else {"non_vendor"},
        "owner_vendor":if vendor_mode {vendor()} else {Value::Null}, "subject":"Newest", "history_notice":"Partial"},
        "items":[], "next_cursor":null})
}
fn inbox_row() -> Value {
    json!({"provider":"imap", "account_id":"retained", "message_id":"seed", "thread_entry_ref":origin(),
        "received_at":"2026-11-05T00:00:00Z", "sender":"sender@example.test", "subject":"Subject", "status":"pending", "attempts":0})
}

fn inbox_action_fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../../tests/data/inbox_thread_action_page.json"
    ))
    .unwrap()
}

#[test]
fn action_opt_in_is_a_strict_message_query_boolean() {
    let fixture = inbox_action_fixture();
    let mut query = fixture["query"].clone();
    query["cursor"] = Value::Null;
    for flag in [json!(true), json!(false)] {
        query["include_inbox_actions"] = flag;
        let decoded: ThreadMessagesQuery = serde_json::from_value(query.clone()).unwrap();
        assert_eq!(serde_json::to_value(decoded).unwrap(), query);
    }
    for flag in [Value::Null, json!(0), json!("true"), json!([]), json!({})] {
        query["include_inbox_actions"] = flag;
        assert!(serde_json::from_value::<ThreadMessagesQuery>(query.clone()).is_err());
    }
    query["include_inbox_actions"] = json!(true);
    query["message_id"] = json!("not a message-page selector");
    assert!(serde_json::from_value::<ThreadMessagesQuery>(query).is_err());
}

#[cfg(unix)]
#[test]
fn action_page_round_trips_own_records_and_explicit_content_only_slots() {
    let fixture = inbox_action_fixture();
    let dir = tempfile::tempdir().unwrap();
    let mut query = fixture["query"].clone();
    query["cursor"] = Value::Null;
    let page = fixture["action_page"].clone();
    let engine = |data| {
        process_fixture(
            dir.path(),
            json!({
                "protocol":1, "ok":true, "operation":"threads.messages", "data":data,
            }),
        )
    };
    let decoded = engine(page.clone())
        .thread_messages(serde_json::from_value(query.clone()).unwrap())
        .unwrap();
    let result = serde_json::to_value(decoded).unwrap();
    assert_eq!(captured_payload(dir.path(), "threads.messages"), query);
    for (index, (message, part)) in [("newer", "second-part"), ("m", "first-part")]
        .into_iter()
        .enumerate()
    {
        let item = &result["items"][index];
        assert_eq!(item["message_id"], message);
        assert_eq!(
            item["inbox_action"]["thread_entry_ref"]["message_id"],
            message
        );
        assert_eq!(item["inbox_action"]["attachments"][0]["part_id"], part);
    }
    assert_eq!(result["items"][0]["direction"], "outbound");
    assert_eq!(
        result["items"][0]["inbox_action"]["admission"]["kind"],
        "sent_to_vendor"
    );
    assert_eq!(result["items"][1]["inbox_action"]["action_required"], 0);
    assert_eq!(
        fixture["copies"]["items"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|item| item["copy_ref"] == "sent-copy" && item.get("inbox_action").is_none())
            .count(),
        1
    );

    let mut legacy = message_page(false);
    legacy["thread"]["thread_ref"]["thread_key"] = json!("survivor");
    legacy["items"] = page["items"].clone();
    let record = &mut legacy["items"][0]["inbox_action"];
    record["provider"] = json!("imap");
    record["account_id"] = json!("retained");
    record["thread_entry_ref"] = json!({
        "provider":"imap", "account_id":"retained", "mailbox_identity_key":null, "message_id":"newer",
    });
    legacy["items"][1]["inbox_action"] = Value::Null;
    let mut query = selector(false);
    query["include_inbox_actions"] = json!(true);
    let result = serde_json::to_value(
        engine(legacy)
            .thread_messages(serde_json::from_value(query).unwrap())
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        result["items"][0]["inbox_action"]["thread_entry_ref"].get("mailbox_identity_key"),
        Some(&Value::Null)
    );
    assert_eq!(result["items"][1].get("inbox_action"), Some(&Value::Null));
    assert_eq!(result["thread"]["thread_ref"]["thread_key"], "survivor");
}

#[cfg(unix)]
#[test]
fn action_page_rejects_unrequested_missing_duplicate_and_foreign_records() {
    let fixture = inbox_action_fixture();
    let dir = tempfile::tempdir().unwrap();
    let mut query = fixture["query"].clone();
    query["cursor"] = Value::Null;
    let page = fixture["action_page"].clone();
    let engine = |data| {
        process_fixture(
            dir.path(),
            json!({
                "protocol":1, "ok":true, "operation":"threads.messages", "data":data,
            }),
        )
    };
    let mut plain_query = query.clone();
    plain_query
        .as_object_mut()
        .unwrap()
        .remove("include_inbox_actions");
    let plain = fixture["plain_page"].clone();
    assert_eq!(
        serde_json::to_value(
            engine(plain.clone())
                .thread_messages(serde_json::from_value(plain_query.clone()).unwrap())
                .unwrap()
        )
        .unwrap(),
        plain,
    );
    for flag in [None, Some(json!(false))] {
        if let Some(value) = flag {
            plain_query["include_inbox_actions"] = value;
        }
        assert_eq!(
            engine(page.clone())
                .thread_messages(serde_json::from_value(plain_query.clone()).unwrap())
                .unwrap_err()
                .code,
            "engine_protocol_error"
        );
    }
    let mut vendor_only = query.clone();
    vendor_only.as_object_mut().unwrap().remove("inbox_message");
    assert_eq!(
        engine(plain.clone())
            .thread_messages(serde_json::from_value(vendor_only).unwrap())
            .unwrap_err()
            .code,
        "invalid_request"
    );

    let mut missing = page.clone();
    missing["items"][0]
        .as_object_mut()
        .unwrap()
        .remove("inbox_action");
    let mut duplicate = page.clone();
    duplicate["items"][1] = duplicate["items"][0].clone();
    let mut borrowed = page.clone();
    borrowed["items"][0]["inbox_action"] = page["items"][1]["inbox_action"].clone();
    let mut missing_identity = page.clone();
    missing_identity["items"][0]["inbox_action"]["thread_entry_ref"]
        .as_object_mut()
        .unwrap()
        .remove("mailbox_identity_key");
    let mut wrong_local_origin = page.clone();
    wrong_local_origin["items"][0]["inbox_action"]["thread_entry_ref"]["message_id"] =
        json!("foreign");
    let mut wrong_type = page.clone();
    wrong_type["items"][0]["inbox_action"]["attachments"] = json!({"part_id":"first-part"});
    let mut invalid_pages = vec![
        missing,
        duplicate,
        borrowed,
        missing_identity,
        wrong_local_origin,
        wrong_type,
    ];
    for field in ["provider", "account_id", "mailbox_identity_key"] {
        let mut invalid = page.clone();
        invalid["items"][0]["inbox_action"]["thread_entry_ref"][field] = json!("foreign");
        if field != "mailbox_identity_key" {
            invalid["items"][0]["inbox_action"][field] = json!("foreign");
        }
        invalid_pages.push(invalid);
    }
    let mut foreign_thread = page.clone();
    foreign_thread["thread"]["thread_ref"]["thread_key"] = json!("foreign");
    invalid_pages.push(foreign_thread);
    let mut foreign_mailbox = page.clone();
    foreign_mailbox["thread"]["thread_ref"]["mailbox_identity_key"] = Value::Null;
    for item in foreign_mailbox["items"].as_array_mut().unwrap() {
        item["inbox_action"]["thread_entry_ref"]["mailbox_identity_key"] = Value::Null;
    }
    invalid_pages.push(foreign_mailbox);
    for (index, invalid) in invalid_pages.into_iter().enumerate() {
        assert_eq!(
            engine(invalid)
                .thread_messages(serde_json::from_value(query.clone()).unwrap())
                .unwrap_err()
                .code,
            "engine_protocol_error",
            "invalid page {index}"
        );
    }
}

#[test]
fn inbox_reference_requires_all_fields_and_never_defaults_an_origin() {
    for identity in [Value::Null, json!("a".repeat(64))] {
        let mut valid = origin();
        valid["mailbox_identity_key"] = identity;
        let typed: InboxMessageRef = serde_json::from_value(valid.clone()).unwrap();
        assert_eq!(serde_json::to_value(typed).unwrap(), valid);
        for field in [
            "provider",
            "account_id",
            "mailbox_identity_key",
            "message_id",
        ] {
            let mut missing = valid.clone();
            missing.as_object_mut().unwrap().remove(field);
            assert!(
                serde_json::from_value::<InboxMessageRef>(missing).is_err(),
                "missing {field}"
            );
            for wrong in [json!(1), json!(true), json!({}), json!([])] {
                let mut invalid = valid.clone();
                invalid[field] = wrong;
                assert!(
                    serde_json::from_value::<InboxMessageRef>(invalid).is_err(),
                    "wrong {field}"
                );
            }
        }
    }
    let valid = inbox_row();
    assert!(serde_json::from_value::<InboxItem>(valid.clone()).is_ok());
    let mut missing = valid.clone();
    missing.as_object_mut().unwrap().remove("thread_entry_ref");
    assert!(serde_json::from_value::<InboxItem>(missing).is_err());
    for value in [Value::Null, json!({}), json!({"provider":"gmail"})] {
        let mut invalid = valid.clone();
        invalid["thread_entry_ref"] = value;
        assert!(serde_json::from_value::<InboxItem>(invalid).is_err());
    }
    let mut extra = origin();
    extra["unexpected"] = json!(true);
    assert!(serde_json::from_value::<InboxMessageRef>(extra).is_err());
}

#[test]
fn resolver_and_all_child_queries_are_closed_alternatives_with_full_guard_forwarding() {
    for valid in [
        json!({"message_ref":origin(), "limit":3, "cursor":"page"}),
        json!({"message_ref":origin(), "vendor_id":"v"}),
    ] {
        let typed: ThreadResolveQuery = serde_json::from_value(valid.clone()).unwrap();
        assert_eq!(serde_json::to_value(typed).unwrap(), valid);
    }
    for invalid in [
        json!({}),
        json!({"message_ref":null}),
        json!({"message_ref":origin(), "vendor_id":null}),
        json!({"message_ref":origin(), "vendor_id":"v", "limit":3}),
        json!({"message_ref":origin(), "vendor_id":"v", "cursor":null}),
        json!({"message_ref":origin(), "extra":true}),
    ] {
        assert!(
            serde_json::from_value::<ThreadResolveQuery>(invalid.clone()).is_err(),
            "{invalid}"
        );
    }
    for vendor_mode in [false, true] {
        for operation in ["messages", "body", "copies", "inventory"] {
            let mut valid = selector(vendor_mode);
            if operation != "messages" {
                valid["message_id"] = json!("target");
            }
            if operation != "body" {
                valid["limit"] = json!(3);
                valid["cursor"] = json!("page");
            }
            if operation == "copies" || operation == "inventory" {
                valid["copy_ref"] = if operation == "inventory" {
                    json!("copy")
                } else {
                    Value::Null
                };
            }
            let decode = |input: Value| -> Result<Value, serde_json::Error> {
                match operation {
                    "messages" => serde_json::from_value::<ThreadMessagesQuery>(input)
                        .and_then(serde_json::to_value),
                    "body" => serde_json::from_value::<ThreadBodyQuery>(input)
                        .and_then(serde_json::to_value),
                    _ => serde_json::from_value::<ThreadCopiesQuery>(input)
                        .and_then(serde_json::to_value),
                }
            };
            assert_eq!(decode(valid.clone()).unwrap(), valid, "{operation}");
            let mut invalids = Vec::new();
            for field in [
                "unexpected",
                if vendor_mode {
                    "inbox_thread"
                } else {
                    "vendor_id"
                },
                if vendor_mode {
                    "inbox_message"
                } else {
                    "thread_ref"
                },
            ] {
                let mut input = valid.clone();
                input[field] = Value::Null;
                invalids.push(input);
            }
            let prefix = if vendor_mode {
                "/inbox_message"
            } else {
                "/inbox_thread/message_ref"
            };
            for field in [
                "provider",
                "account_id",
                "mailbox_identity_key",
                "message_id",
            ] {
                let mut input = valid.clone();
                input
                    .pointer_mut(prefix)
                    .unwrap()
                    .as_object_mut()
                    .unwrap()
                    .remove(field);
                invalids.push(input);
            }
            if !vendor_mode {
                for field in ["message_ref", "thread_ref"] {
                    let mut input = valid.clone();
                    input["inbox_thread"].as_object_mut().unwrap().remove(field);
                    invalids.push(input);
                }
                let mut input = valid.clone();
                input["inbox_thread"]["extra"] = json!(true);
                invalids.push(input);
            }
            for invalid in invalids {
                assert!(decode(invalid.clone()).is_err(), "{operation}: {invalid}");
            }
        }
    }
}

#[test]
fn resolver_and_message_response_modes_require_consistent_relation_owner_and_origin() {
    for vendor_mode in [false, true] {
        let valid = resolved(vendor_mode);
        let query = serde_json::from_value(json!({"message_ref":origin()})).unwrap();
        assert!(
            serde_json::from_value::<ThreadResolveResult>(valid.clone())
                .unwrap()
                .valid_for(&query)
        );
        for owner in [Value::Null, vendor()] {
            let expected = vendor_mode != owner.is_null();
            let mut input = valid.clone();
            input["context"]["thread"]["owner_vendor"] = owner.clone();
            assert_eq!(
                serde_json::from_value::<ThreadResolveResult>(input).is_ok(),
                expected
            );
            let mut page = message_page(vendor_mode);
            page["thread"]["owner_vendor"] = owner;
            assert_eq!(
                serde_json::from_value::<ThreadMessagePage>(page).is_ok(),
                expected
            );
        }
        for pointer in [
            "/context/thread/owner_vendor",
            "/context/thread/latest_received_at",
            "/message_ref/mailbox_identity_key",
        ] {
            let mut missing = valid.clone();
            let (parent, field) = pointer.rsplit_once('/').unwrap();
            missing
                .pointer_mut(parent)
                .unwrap()
                .as_object_mut()
                .unwrap()
                .remove(field);
            assert!(
                serde_json::from_value::<ThreadResolveResult>(missing).is_err(),
                "{pointer}"
            );
        }
        for (pointer, value) in [
            ("/message_ref/message_id", json!("other")),
            ("/context/thread/thread_ref/provider", json!("gmail")),
            ("/context/thread/thread_ref/account_id", json!("other")),
            (
                "/context/thread/thread_ref/mailbox_identity_key",
                json!("a".repeat(64)),
            ),
        ] {
            let mut wrong = valid.clone();
            *wrong.pointer_mut(pointer).unwrap() = value;
            assert!(
                !serde_json::from_value::<ThreadResolveResult>(wrong)
                    .unwrap()
                    .valid_for(&query),
                "{pointer}"
            );
        }
        let page: ThreadMessagePage = serde_json::from_value(message_page(vendor_mode)).unwrap();
        for query_vendor in [false, true] {
            let request: ThreadMessagesQuery =
                serde_json::from_value(selector(query_vendor)).unwrap();
            assert_eq!(page.thread.valid_for(&request), query_vendor == vendor_mode);
        }
    }
    let explicit: ThreadResolveQuery =
        serde_json::from_value(json!({"message_ref":origin(), "vendor_id":"v"})).unwrap();
    for invalid in [resolved(false), chooser()] {
        assert!(
            !serde_json::from_value::<ThreadResolveResult>(invalid)
                .unwrap()
                .valid_for(&explicit)
        );
    }
    let mut wrong = resolved(true);
    wrong["context"]["vendor"]["vendor_id"] = json!("other");
    assert!(
        !serde_json::from_value::<ThreadResolveResult>(wrong)
            .unwrap()
            .valid_for(&explicit)
    );
}

#[cfg(unix)]
#[test]
fn inbox_origins_and_guarded_reads_cross_the_actual_engine_boundary() {
    let dir = tempfile::tempdir().unwrap();
    let engine_for = |operation: &str, data: Value| {
        process_fixture(
            dir.path(),
            json!({"protocol":1, "ok":true, "operation":operation, "data":data}),
        )
    };
    for field in [
        None,
        Some("provider"),
        Some("account_id"),
        Some("message_id"),
    ] {
        let mut row = inbox_row();
        if let Some(field) = field {
            row["thread_entry_ref"][field] = json!("wrong");
        }
        let result = engine_for("inbox.query", json!({"items":[row], "next_cursor":null}))
            .query_inbox(serde_json::from_value(json!({"limit":25})).unwrap());
        if field.is_none() {
            assert_eq!(
                serde_json::to_value(result.unwrap()).unwrap()["items"][0]["thread_entry_ref"],
                origin()
            );
        } else {
            assert_eq!(result.unwrap_err().code, "engine_protocol_error");
        }
    }
    for vendor_mode in [false, true] {
        let query = json!({"message_ref":origin(), "limit":3, "cursor":null});
        let actual = engine_for("threads.resolve", resolved(vendor_mode))
            .resolve_thread(serde_json::from_value(query.clone()).unwrap())
            .unwrap();
        assert_eq!(serde_json::to_value(actual).unwrap(), resolved(vendor_mode));
        assert_eq!(captured_payload(dir.path(), "threads.resolve"), query);
        let mut query = selector(vendor_mode);
        query["limit"] = json!(3);
        query["cursor"] = Value::Null;
        let actual = engine_for("threads.messages", message_page(vendor_mode))
            .thread_messages(serde_json::from_value(query.clone()).unwrap())
            .unwrap();
        assert_eq!(
            serde_json::to_value(actual).unwrap(),
            message_page(vendor_mode)
        );
        assert_eq!(captured_payload(dir.path(), "threads.messages"), query);
        let mut query = selector(vendor_mode);
        query["message_id"] = json!("target");
        let response = json!({"thread_ref":reference(), "message_id":"target", "body":{"state":"not_followed"}});
        let actual = engine_for("threads.body", response.clone())
            .thread_body(serde_json::from_value(query.clone()).unwrap())
            .unwrap();
        assert_eq!(serde_json::to_value(actual).unwrap(), response);
        assert_eq!(captured_payload(dir.path(), "threads.body"), query);
        for inventory in [false, true] {
            query["copy_ref"] = if inventory {
                json!("copy")
            } else {
                Value::Null
            };
            query["limit"] = json!(3);
            query["cursor"] = json!("cursor");
            let response = json!({"thread_ref":reference(), "message_id":"target", "items":[], "next_cursor":null});
            let actual = engine_for("threads.copies", response.clone())
                .thread_copies(serde_json::from_value(query.clone()).unwrap())
                .unwrap();
            assert_eq!(serde_json::to_value(actual).unwrap(), response);
            assert_eq!(captured_payload(dir.path(), "threads.copies"), query);
        }
    }
    // Same engine harness accepts valid controls above; contradictory responses must reach the host checks.
    let query = json!({"message_ref":origin()});
    let mut wrong = resolved(false);
    wrong["message_ref"]["message_id"] = json!("wrong");
    assert_eq!(
        engine_for("threads.resolve", wrong)
            .resolve_thread(serde_json::from_value(query).unwrap())
            .unwrap_err()
            .code,
        "engine_protocol_error"
    );
    assert_eq!(
        engine_for("threads.messages", message_page(true))
            .thread_messages(serde_json::from_value(selector(false)).unwrap())
            .unwrap_err()
            .code,
        "engine_protocol_error"
    );
}
