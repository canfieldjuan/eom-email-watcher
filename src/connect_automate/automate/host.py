"""The Automate host skeleton and its startup license gate.

Running unattended automations requires the base capability-exchange entitlement plus the
automations feature: the same pair the scheduling automation already enforces in
``service.py``. The automations feature is additive to capability exchange and grants no
discovery or invocation on its own (ADR-0006), so both must be active.

This module is the seam through which the host reuses the licensed-consumer substrate.
Slice 0 wires only the entitlement gate. Later slices attach, behind the same
``require_license`` gate: the rule control plane (``engine_api``), the durable dispatch
lane (``db``), the v2 Connect client (``connect``), and the workflow record and ledger.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..entitlement import (
    AUTOMATIONS_FEATURE_ID,
    CONNECT_FEATURE_ID,
    EntitlementGate,
)

# The Connect features the host must hold, in the same order and combination that
# service.py requires for the scheduling automation. Kept as a tuple so callers cannot
# mutate the required set.
REQUIRED_FEATURES: tuple[str, ...] = (CONNECT_FEATURE_ID, AUTOMATIONS_FEATURE_ID)


class AutomateLicenseError(RuntimeError):
    """Raised when the host is asked to run automations without an active license."""


@dataclass(frozen=True)
class AutomateHost:
    """Skeleton of the licensed Automate host.

    The host holds an :class:`~connect_automate.entitlement.EntitlementGate` rather than
    reaching for global entitlement state, so tests can drive it with a fixed clock and a
    test keyring, and so a future host process can hold a single gate for its lifetime.
    """

    entitlement: EntitlementGate

    @classmethod
    def from_installation(cls) -> AutomateHost:
        """Build a host bound to this machine's installed entitlement and keyring."""
        return cls(entitlement=EntitlementGate.from_installation())

    def licensed(self) -> bool:
        """Return True only when every required Connect feature is active right now.

        Re-evaluated on each call rather than cached, so a host that keeps running past
        its entitlement's expiry stops being licensed the moment the clock crosses it.
        """
        return self.entitlement.features_active(REQUIRED_FEATURES)

    def require_license(self) -> None:
        """Refuse to proceed unless the automations license is active.

        This is the single choke point every later slice calls before admitting work, so
        the license is revalidated at each admission boundary, not only at startup.
        """
        if not self.licensed():
            raise AutomateLicenseError(
                "Automate host requires an active Connect automations entitlement "
                f"({', '.join(REQUIRED_FEATURES)})."
            )

    def start(self) -> AutomateHost:
        """Admit the host to run and return it. Slice 0 performs the license gate only."""
        self.require_license()
        return self


def main() -> int:
    """Report whether this machine is licensed to run the Automate host.

    Exit code 0 when the automations entitlement is active, 1 otherwise. This is the
    skeleton entry point; later slices load and run workflow packs after this gate.
    """
    host = AutomateHost.from_installation()
    try:
        host.require_license()
    except AutomateLicenseError as exc:
        print(str(exc))
        return 1
    print("Automate host licensed: the Connect automations entitlement is active.")
    return 0
