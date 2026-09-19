"""Connect Automate: the vendor-neutral licensed-consumer host core.

This package holds the generic Connect v2 client, the entitlement gate, the
file-locking substrate, and the Automate host runtime (rule model, stage
engine, effect executor, adapter registry, pack loader). It names only
abstract action kinds and opaque capability identifiers; it never imports a
vendor integration, a provider backend, or any application-specific package.

The boundary is enforced by tests/test_connect_automate_boundary.py: nothing
here may import ``eom_email_watcher`` or a concrete provider client. Bundled
adapters (mail, notify, calendar) register into the adapter registry at a host
composition layer that lives outside this package.
"""

__version__ = "0.1.0"
