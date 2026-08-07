"""urio -- real async file I/O for Python, powered by io_uring.

Unlike ``aiofiles``, which dispatches blocking syscalls to a thread pool, urio
submits reads and writes to the Linux ``io_uring`` interface and resolves them
on the event loop via an eventfd -- genuinely asynchronous I/O with no thread
per operation. On platforms or kernels without io_uring it transparently falls
back to a thread-pool backend, so the API works everywhere.

    import urio

    async def main():
        async with urio.open('hello.txt', 'w') as f:
            await f.write('hello')
        data = await urio.Path('hello.txt').read_text()

It also ships :class:`urio.Path`, an async ``pathlib.Path`` with truly-async
``stat``/``mkdir``/``unlink``/``rename`` and read/write helpers.
"""

from __future__ import annotations

import asyncio as _asyncio
import sys as _sys

from ._version import __version__
from .backend import get_backend, set_backend
from .file import (
    AsyncBufferedFile,
    AsyncTextFile,
    open,
)
from .path import AsyncPath
from .path import AsyncPath as Path

__all__ = (
    '__version__',
    'open',
    'Path',
    'AsyncPath',
    'AsyncBufferedFile',
    'AsyncTextFile',
    'aclose',
    'get_backend',
    'set_backend',
)


async def aclose() -> None:
    """Release the running loop's native I/O resources deterministically.

    Shuts down whichever driver (io_uring ring + eventfd, or IOCP poller) this
    loop lazily created, cancelling any still-pending operations. On Linux this
    is optional — drivers are also reclaimed when the loop is garbage
    collected, from any thread — but useful for embedders that create many
    loops or want a clean teardown point. With the Windows IOCP backend it is
    the supported teardown for loops running on worker threads: the native port
    must be released on the thread that created it, which a later GC pass may
    not honour. urio works again on the same loop afterwards (a fresh driver is
    created on the next operation).
    """
    loop = _asyncio.get_running_loop()
    for name in ('urio.driver', 'urio.driver_iocp', 'urio.driver_iocp_proactor'):
        mod = _sys.modules.get(name)
        if mod is not None:
            mod.close_driver(loop)
