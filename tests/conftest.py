import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def restore_process_umask() -> Iterator[None]:
    # engine_api.main() sets a 0o077 umask for its one-shot process; in-process tests
    # must not leak it, or later tests pass only because of test order.
    previous = os.umask(0o022)
    os.umask(previous)
    yield
    os.umask(previous)
