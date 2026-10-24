#!/usr/bin/env python3
import os

import pytest

import urio
from urio.backend import ThreadBackend, UringBackend, _native_ok, get_backend


def test_set_backend_invalid_name():

    with pytest.raises(ValueError):
        urio.set_backend('nonsense')

    urio.set_backend('auto')


def test_force_thread_backend():

    urio.set_backend('thread')
    try:
        backend = get_backend()
        assert isinstance(backend, ThreadBackend)
        assert not isinstance(backend, UringBackend)
        assert backend.name == 'thread'
    finally:
        urio.set_backend('auto')


@pytest.mark.skipif(not _native_ok(), reason='io_uring not available')
def test_force_uring_backend():
    urio.set_backend('uring')
    try:
        backend = get_backend()
        assert isinstance(backend, UringBackend)
        assert backend.name == 'uring'
    finally:
        urio.set_backend('auto')


def test_env_var_selects_thread(monkeypatch):
    monkeypatch.setenv('URIO_BACKEND', 'thread')
    urio.set_backend('auto')
    import urio.backend as b

    b._backend = None

    assert get_backend().name == 'thread'
    b._backend = None


async def test_stat_result_fields_match_os(backend, tmp_path):
    p = tmp_path / 'cmp'
    p.write_bytes(b'abcdef')

    st = await get_backend().statx(str(p))
    ref = os.stat(str(p))

    assert st.st_size == ref.st_size == 6
    assert st.st_mode == ref.st_mode
    assert st.st_ino == ref.st_ino
    assert st.st_mtime_ns == ref.st_mtime_ns


async def test_iocp_handle_errors_are_reported(backend):
    if backend != 'iocp':
        pytest.skip('Windows IOCP only')
    active = get_backend()
    with pytest.raises(OSError):
        await active.fsync(-1)
    with pytest.raises(OSError):
        await active.filesize(-1)
    with pytest.raises(OSError):
        await active.ftruncate(-1, 0)
