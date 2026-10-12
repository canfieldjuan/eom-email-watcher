"""Local vendor attribution shared by capture and derived thread state."""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class VendorIndex:
    addresses: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    domains: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    identities: frozenset[str] = frozenset()

    def vendor_of(self, address: str) -> tuple[str, str, str, str] | None:
        if address in self.identities:
            return None
        exact = self.addresses.get(address)
        if exact is not None:
            return (*exact, "exact", address)
        domain = self.domains.get(address.rpartition("@")[2])
        return (*domain, "domain", address) if domain is not None else None

    def attribute(
        self, sender: str, *, outbound: bool, to: tuple[str, ...] = (), cc: tuple[str, ...] = ()
    ) -> tuple[str, str, str, str] | None:
        if not outbound:
            return self.vendor_of(sender)
        matches = [self.vendor_of(address) for address in (*to, *cc)]
        for kind in ("exact", "domain"):
            for match in matches:
                if match is not None and match[2] == kind:
                    return match
        return None


def vendor_index(db: sqlite3.Connection) -> VendorIndex:
    addresses = {
        str(row["address"]): (str(row["vendor_id"]), str(row["display_name"]))
        for row in db.execute(
            "SELECT a.address, a.vendor_id, v.display_name FROM vendor_addresses a JOIN "
            "vendors v ON v.vendor_id = a.vendor_id"
        )
    }
    identities = frozenset(
        str(row[0])
        for row in db.execute("SELECT address FROM mail_accounts WHERE address IS NOT NULL")
    )
    # Domain selection is introduced by M5; no implicit domain inference here.
    domains = {}
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vendor_domains'"
    ).fetchone():
        domains = {
            str(row["domain"]): (str(row["vendor_id"]), str(row["display_name"]))
            for row in db.execute(
                "SELECT d.domain, d.vendor_id, v.display_name FROM vendor_domains d JOIN "
                "vendors v ON v.vendor_id=d.vendor_id"
            )
        }
    return VendorIndex(addresses=addresses, domains=domains, identities=identities)
