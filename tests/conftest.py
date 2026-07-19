#!/usr/bin/env python3
import asyncio

import pytest

import urio
from urio.backend import _iocp_ok, _native_ok

# Every I/O test runs against each real backend and the thread-pool fallback:
# io_uring (Linux), IOCP (Windows, opt-in), and the portable thread backend.
# A backend is skipped where it is unavailable, so the thread backend always
# runs and the native ones run only on their platform.
BACKENDS = ('uring', 'iocp', 'thread')


@pytest.fixture(name='backend', params=BACKENDS)
def fixture_backend(request):
    """Given: urio configured to use a specific backend."""
    name = request.param
    if name == 'uring' and not _native_ok():
        pytest.skip('io_uring not available on this system')
    if name == 'iocp' and not _iocp_ok():
        pytest.skip('IOCP not available (Windows-only, set URIO_WINDOWS_IOCP=1)')
    urio.set_backend(name)
    yield name
    urio.set_backend('auto')


@pytest.fixture(name='tmpfile')
def fixture_tmpfile(tmp_path):
    """Given: a path to a not-yet-created file in a temp directory."""
    return tmp_path / 'data'


@pytest.fixture(name='selector_loop')
def fixture_selector_loop():
    """Given: the IOCP backend on a SelectorEventLoop.

    A ProactorEventLoop (the Windows default, and what pytest-asyncio runs the
    rest of the suite on) uses the loop-thread driver; a SelectorEventLoop has
    no ``_proactor``, which forces the poller-thread fallback driver
    (``urio.driver_iocp``). Tests using this fixture drive their coroutines
    with ``selector_loop.run_until_complete(...)``.
    """
    if not _iocp_ok():
        pytest.skip('IOCP not available (Windows-only, set URIO_WINDOWS_IOCP=1)')
    loop = asyncio.SelectorEventLoop()
    urio.set_backend('iocp')
    yield loop
    loop.run_until_complete(urio.aclose())
    loop.close()
    urio.set_backend('auto')
