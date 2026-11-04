"""Synthetic-only native M3 fixture and browser assertions for prove_thread_csp."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def seed_history(profile: Path, remote: str) -> dict:
    # Reuse the public synthetic legacy-upgrade and capture fixture owners.
    sys.path.insert(0, str(ROOT / "tests"))
    from test_thread_body_reads import unresolved_thread
    from test_thread_copy_reads import attachment
    from test_thread_reconcile import KEY, NOW, seed

    from eom_email_watcher import body_state, thread_reads

    store, legacy = unresolved_thread(profile)
    store.rename_vendor(legacy, "Legacy supplier")
    store.register_mail_account("gmail", "retained", display_name="Retained account")
    store.reconcile_mailbox_identity("gmail", "retained", KEY)
    store.register_mail_account("gmail", "other", display_name="Other retained account")
    store.reconcile_mailbox_identity("gmail", "other", "b" * 64)
    vendor = store.create_vendor("Aster Supply")
    linked = store.create_vendor("Northside Repairs")
    empty = store.create_vendor("Empty supplier")
    store.add_vendor_address(vendor, "aster@example.test")
    store.add_vendor_address(linked, "northside@example.test")
    hostile = (
        f'<img src="{remote}/mail-beacon" onerror="window.__mailExecuted=true">'
        "<script>window.__mailExecuted=true</script>"
    )
    literal = (
        f"  Invoice details\n{hostile}\n> original quote\nhttps://example.test/literal\n"
        + "long" * 80
    )
    for index in range(30):
        seed(
            store,
            f"native-{index:02}",
            account_id="retained",
            thread_id="shared-native",
            sender="northside@example.test" if index == 5 else "aster@example.test",
            sender_name=hostile if index == 0 else None,
            subject=f"Retained invoice {index:02} {hostile if index == 0 else ''}".strip(),
            rfc_message_id=f"native-{index}@example.test",
            received_at=(NOW + timedelta(minutes=index)).isoformat(),
            locations=frozenset({"inbox"}),
        )
    body_state.record(store, "native-00", (literal, len(literal) + 7), fetched_at=NOW)
    body_state.record(store, "native-01", ("", 0), fetched_at=NOW)
    body_state.record(store, "native-02", None, fetched_at=NOW, reason="source_gone")
    body_state.record(store, "native-03", None, fetched_at=NOW, reason="outside_folders")
    seed(
        store,
        "location-copy",
        account_id="retained",
        thread_id="shared-native",
        sender="aster@example.test",
        rfc_message_id="native-0@example.test",
        locations=frozenset({"inbox", "sent"}),
        to=("aster@example.test",),
    )
    store.replace_attachments(
        "native-00",
        [replace(attachment(), filename=hostile)],
        source_provider_message_id="location-copy",
    )
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET summary=? WHERE message_id='native-00'",
            (f"Retained summary {hostile}",),
        )
    seed(
        store,
        "other-account",
        account_id="other",
        mailbox_identity_key="b" * 64,
        thread_id="shared-native",
        sender="aster@example.test",
        subject="Other account only",
    )
    store.set_sent_scope("gmail", "retained", "unavailable", now=NOW)
    ref = next(
        item["thread_ref"]
        for item in thread_reads.list_threads(store, vendor)["items"]
        if item["thread_ref"]["account_id"] == "retained"
    )
    selected = thread_reads.ThreadRef(**ref)
    assert (
        thread_reads.list_messages(store, vendor, selected)["items"][0]["message_id"] == "native-00"
    )
    assert thread_reads.read_body(store, vendor, selected, "native-00")["body"]["text"] == literal
    config = profile / "config.toml"
    with config.open("a") as output:
        output.write(f"database_file = {json.dumps(str(store.path))}\n")
        for email in [
            "aster@example.test",
            "northside@example.test",
            "long-" + "x" * 50 + "@example.test",
        ]:
            output.write(f"\n[[senders]]\nemail = {json.dumps(email)}\n")
    return {
        "vendor": vendor,
        "linked": linked,
        "legacy": legacy,
        "empty": empty,
        "ref": ref,
        "literal": literal,
        "hostile": hostile,
        "database": str(store.path),
    }


def exercise(script, fixture: dict, resize, screenshot, keypress) -> dict:
    def wait(code, *args):
        deadline = time.monotonic() + 30
        while not script(code, *args):
            if time.monotonic() >= deadline:
                raise AssertionError(f"Native thread UI did not satisfy: {code}")
            time.sleep(0.05)

    def click(label):
        assert script(
            """const b = [...document.querySelectorAll('#thread-view button')]
            .find(b => b.textContent === arguments[0] && !b.disabled);
            if (!b) return false; b.click(); return true;""",
            label,
        ), label

    def enter(vendor):
        wait("return !document.getElementById('vendors-tab').disabled;")
        assert script("""document.getElementById('vendors-tab').click();
            return !document.getElementById('vendors-view').hidden;""")
        wait("return !!document.querySelector('[data-thread-vendor=" + json.dumps(vendor) + "]');")
        assert script(
            """const b = [...document.querySelectorAll('[data-thread-vendor]')]
            .find(b => b.dataset.threadVendor === arguments[0]);
            if (!b || b.disabled) return false; b.click(); return true;""",
            vendor,
        )
        wait(
            """const view = document.getElementById('thread-view');
            return !view.hidden && !view.textContent.includes('Loading retained');"""
        )

    # Entry works with absent Connect entitlement and disconnected retained accounts.
    enter(fixture["vendor"])
    assert script("return document.getElementById('vendor-form').hidden;")
    assert script(
        "return document.querySelector('#thread-view').textContent.includes('Other account only');"
    )
    script("""const s = document.querySelector('#thread-view select');
        s.value = JSON.stringify(['gmail','retained']);
        s.dispatchEvent(new Event('change', {bubbles:true}));""")
    wait("return document.querySelectorAll('#thread-list > li').length === 1;")
    assert script(
        "return !document.querySelector('#thread-view').textContent.includes('Other account only');"
    )
    script("document.querySelector('#thread-list button').click();")
    wait("return document.querySelectorAll('#thread-messages > ol > li').length === 25;")
    assert script(
        "return document.querySelector('#thread-messages h3').textContent === arguments[0];",
        "Retained invoice 29",
    )
    account_context = script(
        """const done = arguments[0];
        window.__TAURI_INTERNALS__.invoke('mail_accounts_list')
          .then(value => done(value.accounts)).catch(e => done({error:String(e)}));""",
        asynchronous=True,
    )
    retained = next(a for a in account_context if a["account_id"] == "retained")
    assert retained["connected"] is False and retained["sent_scope"] == "not_polled"
    assert script(
        "return !document.querySelector('#thread-view').textContent.includes(arguments[0]);",
        "Sent mail unavailable",
    )
    click("Load more messages")
    wait("return document.querySelectorAll('#thread-messages > ol > li').length === 30;")
    assert script(
        "return document.querySelector('#thread-messages h3').textContent === arguments[0];",
        "Retained invoice 29",
    )
    # Keyboard activation of a native button, not a synthetic click handler.
    script("document.querySelector('#thread-messages > ol > li button').focus();")
    keypress("Return")
    wait("return !!document.querySelector('.thread-body-text');")
    assert (
        script("return document.querySelector('.thread-body-text').textContent; ")
        == fixture["literal"]
    )
    assert script("return document.querySelector('.thread-body-text').children.length === 0;")
    for index, expected in [
        (1, ""),
        (2, "Body not stored (source no longer available)"),
        (3, "Body not stored (outside the admitted folders)"),
        (4, "Body not stored yet"),
    ]:
        script(
            "document.querySelectorAll('#thread-messages > ol > li')[arguments[0]]"
            ".querySelector('button').click();",
            index,
        )
        wait(
            """const card = document.querySelectorAll('#thread-messages > ol > li')[arguments[0]];
            const body = card.querySelector('[id^="thread-body-"]');
            return arguments[1] === '' ? body.querySelector('.thread-body-text')?.textContent === ''
              : body.textContent === arguments[1] && !body.querySelector('.thread-body-text');""",
            index,
            expected,
        )
        script(
            "document.querySelectorAll('#thread-messages > ol > li')[arguments[0]]"
            ".querySelector('button').click();",
            index,
        )
    assert script(
        """const view = document.querySelector('#thread-view');
        const inserted = view.querySelectorAll('img,script,iframe,a,form').length;
        return !window.__mailExecuted && inserted === 0;
        """
    )
    click("Show copies (2)")
    wait("return document.querySelectorAll('.thread-copy').length === 2;")
    click("Show attachment metadata (1)")
    wait(
        """const copies = document.querySelector('.thread-copy').parentElement;
        return copies.textContent.includes(arguments[0]);""",
        "application/pdf",
    )
    assert script(
        "return !document.querySelector('#thread-view').textContent.includes(arguments[0]);",
        "private-provider-token",
    )
    # Actual IPC body results held after transport must not repaint a collapsed view.
    click("Hide body")
    assert script("""const callbacks = window.__TAURI_INTERNALS__.callbacks;
        const originalSet = callbacks.set;
        window.__m3HoldCallback = function(id, callback) {
          return originalSet.call(this, id, function(value) {
            if (value?.message_id === 'native-00' && value?.body) {
              window.__m3Release = () => callback(value);
            } else callback(value);
          });
        };
        callbacks.set = window.__m3HoldCallback;
        return callbacks.set === window.__m3HoldCallback;""")
    click("Show body")
    wait("return typeof window.__m3Release === 'function';")
    click("Hide body")
    script("delete window.__TAURI_INTERNALS__.callbacks.set; window.__m3Release();")
    time.sleep(0.1)
    assert script("return document.querySelectorAll('.thread-body-text').length === 0;")
    click("Show body")
    wait("return !!document.querySelector('.thread-body-text');")
    resize(640, 720)
    wait("return innerWidth === 640;")

    def layout():
        script(
            "const done = arguments[0]; requestAnimationFrame(() => requestAnimationFrame(done));",
            asynchronous=True,
        )
        measured = script("""const body = document.querySelector('.thread-body-text');
            const rect = body.getBoundingClientRect();
            return {inner:innerWidth, scroll:document.documentElement.scrollWidth,
              body_width:body.clientWidth, body_scroll:body.scrollWidth,
              left:rect.left, right:rect.right,
              font:getComputedStyle(document.documentElement).fontSize};""")
        assert measured["inner"] == 640 and measured["scroll"] <= 640, measured
        assert measured["body_scroll"] <= measured["body_width"], measured
        assert 0 <= measured["left"] < measured["right"] <= 640, measured
        return measured

    size = layout()
    script("document.querySelector('.thread-body-text').scrollIntoView({block:'center'});")
    screenshot("thread-640.png")
    script("document.documentElement.style.fontSize = '200%';")
    zoom = layout()
    script("document.querySelector('#thread-view').scrollIntoView({block:'start'});")
    screenshot("thread-640-text-200.png")
    script("document.querySelector('.thread-body-text').scrollIntoView({block:'center'});")
    screenshot("thread-640-text-200-body.png")
    assert zoom["font"] == "32px", zoom
    script("document.documentElement.style.removeProperty('font-size');")
    click("Back to threads")
    wait("return document.querySelectorAll('#thread-list > li').length === 1;")
    assert script(
        "return document.activeElement === document.querySelector('#thread-list button');"
    )
    assert script("return document.querySelector('#thread-view select').value;") == json.dumps(
        ["gmail", "retained"], separators=(",", ":")
    )
    click("Back to vendors")
    enter(fixture["legacy"])
    assert script(
        "return document.querySelector('#thread-view').textContent.includes(arguments[0]);",
        "Mailbox identity unavailable",
    )
    script("document.querySelector('#thread-list button').click();")
    wait("return document.querySelectorAll('#thread-messages > ol > li').length === 2;")
    click("Show body")
    wait(
        """const body = document.querySelector('#thread-messages > ol > li [id^="thread-body-"]');
        return body && body.textContent.includes(arguments[0]);""",
        "Body not stored (mailbox identity unavailable)",
    )
    assert script("return document.querySelectorAll('.thread-body-text').length === 0;")
    screenshot("thread-legacy.png")
    click("Back to vendors")
    enter(fixture["empty"])
    wait(
        "return document.querySelector('#thread-view').textContent.includes('No retained threads');"
    )
    click("Back to vendors")
    script("document.getElementById('inbox-tab').click();")
    wait(
        "return document.querySelector('#inbox-sender-nav').textContent.includes(arguments[0]);",
        "long-" + "x" * 50 + "@example.test",
    )
    screenshot("inbox-preserved.png")
    return {
        "messages": 30,
        "newest_heading_outside_first_page": True,
        "copies": 2,
        "body_literal": True,
        "late_body_fenced": True,
        "identity_unavailable": True,
        "empty_body_and_unavailable_states": True,
        "disconnected_sent_scope": retained["sent_scope"],
        "empty": True,
        "keyboard_body": True,
        "width": size,
        "text_zoom": zoom,
        "source_scope": fixture["ref"],
    }
