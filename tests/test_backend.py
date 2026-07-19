#!/usr/bin/env python3
import os

import pytest

import urio
from urio.backend import ThreadBackend, UringBackend, _native_ok, get_backend


def test_set_backend_invalid_name():
    # given: urio's backend selector

    # when/then: an unknown backend name is rejected
    with pytest.raises(ValueError):
        urio.set_backend('nonsense')

    urio.set_backend('auto')


def test_force_thread_backend():
    # given: the thread backend forced

    # when: requesting the active backend
    urio.set_backend('thread')
    try:
        backend = get_backend()
        # then: the thread implementation is selected
        assert isinstance(backend, ThreadBackend)
        assert not isinstance(backend, UringBackend)
        assert backend.name == 'thread'
    finally:
        urio.set_backend('auto')


@pytest.mark.skipif(not _native_ok(), reason='io_uring not available')
def test_force_uring_backend():
    # given: io_uring is available and forced
    urio.set_backend('uring')
    try:
        backend = get_backend()
        # then: the uring implementation is selected
        assert isinstance(backend, UringBackend)
        assert backend.name == 'uring'
    finally:
        urio.set_backend('auto')


def test_env_var_selects_thread(monkeypatch):
    # given: URIO_BACKEND=thread in the environment
    monkeypatch.setenv('URIO_BACKEND', 'thread')
    urio.set_backend('auto')
    import urio.backend as b

    # when: forcing reselection
    b._backend = None

    # then: the thread backend is chosen
    assert get_backend().name == 'thread'
    b._backend = None


async def test_stat_result_fields_match_os(backend, tmp_path):
    # given: a file inspected by both os.stat and the backend
    p = tmp_path / 'cmp'
    p.write_bytes(b'abcdef')

    # when: statting via the active backend
    st = await get_backend().statx(str(p))
    ref = os.stat(str(p))

    # then: the core fields agree
    assert st.st_size == ref.st_size == 6
    assert st.st_mode == ref.st_mode
    assert st.st_ino == ref.st_ino
    assert st.st_mtime_ns == ref.st_mtime_ns
