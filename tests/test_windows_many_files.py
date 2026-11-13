import asyncio
import os
import sys

import pytest

import urio

pytestmark = pytest.mark.skipif(
    sys.platform != 'win32' or os.environ.get('URIO_STRESS_TEST') != '1',
    reason='Windows stress test only',
)

OPEN_COUNT = 5_200


async def _exercise_many_open_files(paths):
    results = await asyncio.wait_for(
        asyncio.gather(
            *(urio.open(path, 'rb') for path in paths),
            return_exceptions=True,
        ),
        timeout=180,
    )
    files = [result for result in results if not isinstance(result, BaseException)]
    open_errors = [result for result in results if isinstance(result, BaseException)]
    reads = []
    try:
        if not open_errors:
            reads = await asyncio.wait_for(
                asyncio.gather(
                    *(file.read(1) for file in files), return_exceptions=True
                ),
                timeout=180,
            )
    finally:
        closes = await asyncio.wait_for(
            asyncio.gather(*(file.close() for file in files), return_exceptions=True),
            timeout=180,
        )

    assert not open_errors, (
        f'{len(files)}/{OPEN_COUNT} files opened; first error: {open_errors[0]!r}'
    )
    read_errors = [result for result in reads if isinstance(result, BaseException)]
    close_errors = [result for result in closes if isinstance(result, BaseException)]
    assert not read_errors, f'first read error: {read_errors[0]!r}'
    assert all(result == b'x' for result in reads)
    assert not close_errors, f'first close error: {close_errors[0]!r}'


@pytest.mark.asyncio
@pytest.mark.parametrize('backend_name', ('thread', 'iocp'))
async def test_many_open_files_proactor(tmp_path, backend_name):
    paths = [tmp_path / f'file-{index:05d}' for index in range(OPEN_COUNT)]
    for path in paths:
        path.write_bytes(b'x')
    urio.set_backend(backend_name)
    try:
        if backend_name == 'iocp':
            assert hasattr(asyncio.get_running_loop(), '_proactor')
        await _exercise_many_open_files(paths)
    finally:
        await urio.aclose()
        urio.set_backend('auto')


def test_many_open_files_selector(selector_loop, tmp_path):
    paths = [tmp_path / f'file-{index:05d}' for index in range(OPEN_COUNT)]
    for path in paths:
        path.write_bytes(b'x')
    assert not hasattr(selector_loop, '_proactor')
    selector_loop.run_until_complete(_exercise_many_open_files(paths))
