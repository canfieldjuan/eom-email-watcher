import json

import pytest
from connect_automate import connect

from eom_email_watcher import engine_api


def test_setup_catalog_uses_shared_discovery_without_attachment(monkeypatch):
    capability = connect.DiscoveredCapability(
        protocol_version=2,
        base_url="http://127.0.0.1:12345",
        token="private-token",
        app_id="invoice-processor",
        app_name="Invoice Processor",
        app_version="0.1.0",
        instance_id="11111111-1111-4111-8111-111111111111",
        capability_id="certificate.extract",
        capability_version="1.0",
        action_label="Extract",
        action_description="Extract certificate",
        accepts=(connect.AcceptedArtifactType("application/pdf", 33554432),),
        produces=("application/vnd.local-connect.certificate+json",),
        parameters=(),
        external_effects=False,
        confirmation_required=False,
    )
    catalog = connect.CapabilityCatalog((capability,))
    monkeypatch.setattr(connect, "discover_capabilities", lambda: catalog)
    result = engine_api._response({"protocol": 1, "operation": "connect.catalog", "payload": {}})
    assert result["ok"], result
    assert result["data"] == catalog.public_result()
    assert "private-token" not in json.dumps(result)
    assert "127.0.0.1" not in json.dumps(result)


@pytest.mark.parametrize("diagnostic", [None, "ENTITLEMENT_REQUIRED", "NO_PROVIDERS"])
def test_setup_catalog_preserves_empty_and_diagnostic(monkeypatch, diagnostic):
    catalog = connect.CapabilityCatalog((), diagnostic)
    monkeypatch.setattr(connect, "discover_capabilities", lambda: catalog)
    result = engine_api._response({"protocol": 1, "operation": "connect.catalog", "payload": {}})
    assert result["ok"], result
    assert result["data"] == catalog.public_result()


@pytest.mark.parametrize("payload", [{"provider": "other"}, {"message_id": "x"}, [], False, ""])
def test_setup_catalog_rejects_nonempty_or_malformed_payload(monkeypatch, payload):
    def unexpected():
        pytest.fail("invalid request must not start discovery")

    monkeypatch.setattr(connect, "discover_capabilities", unexpected)
    result = engine_api._response(
        {"protocol": 1, "operation": "connect.catalog", "payload": payload}
    )
    assert result["error"]["code"] == "invalid_request"
