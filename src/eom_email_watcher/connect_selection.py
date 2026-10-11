"""Durable, credential-free selection snapshots; binding stays in the shared client."""

import json
from dataclasses import asdict

from connect_automate import connect


def encode_selection(capability: connect.DiscoveredCapability) -> str:
    data = asdict(capability)
    del data["base_url"]
    del data["token"]
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def decode_selection(encoded: str) -> connect.DiscoveredCapability:
    data = json.loads(encoded)
    data["acknowledged_manifest_features"] = connect.validate_manifest_features(
        data["acknowledged_manifest_features"]
    )
    data["accepts"] = tuple(connect.AcceptedArtifactType(**item) for item in data["accepts"])
    data["parameters"] = tuple(connect.CapabilityParameter(**item) for item in data["parameters"])
    data["produces"] = tuple(data["produces"])
    return connect.DiscoveredCapability(base_url="", token="", **data)
