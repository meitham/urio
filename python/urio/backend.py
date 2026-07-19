"""Pluggable I/O backends.

``UringBackend`` performs reads, writes, opens, and several path mutations as
genuine io_uring submissions on the event loop thread. ``ThreadBackend`` runs the
blocking syscalls in the loop's default executor -- the same strategy ``aiofiles``
uses -- and serves as the portable fallback for non-Linux platforms, kernels
without io_uring, or locked-down environments where the syscalls are blocked.

Operations the io_uring crate does not cover (``ftruncate``, ``scandir``,
``chmod``, ``readlink`` ...) live on ``ThreadBackend`` and are inherited by
``UringBackend`` unchanged, so they transparently fall back to the executor.
"""

from __future__ import annotations

import asyncio
import errno
import functools
import io
import os
import sys

# Resolved lazily so importing urio never fails on a platform without the
# native module.
_uring = None
_NATIVE_CHECKED = False
_NATIVE_OK = False


def _native_ok() -> bool:
    """Return whether a real io_uring ring can be created here."""
    global _uring, _NATIVE_CHECKED, _NATIVE_OK
    if _NATIVE_CHECKED:
        return _NATIVE_OK
    _NATIVE_CHECKED = True
    if sys.platform != 'linux':
        _NATIVE_OK = False
        return False
    try:
        from . import _uring as native
    except ImportError:
        _NATIVE_OK = False
        return False
    try:
        # On non-Linux builds the module exists but has no Ring (AttributeError).
        # On Linux, actually exercise the syscall: seccomp or a kernel without
        # io_uring (pre-5.1) fails here (OSError).
        ring = native.Ring(8)
        del ring
    except (OSError, AttributeError):
        _NATIVE_OK = False
        return False
    _uring = native
    _NATIVE_OK = True
    return True


async def _to_thread(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


_READ_CHUNK = 1 << 20
# io_uring read/write lengths are u32; keep a single submission well under 4 GiB.
_READ_MAX = 1 << 30
# First read-all chunk: sized so small/medium files complete in one op (and skip
# the fstatx). The in-flight gate bounds concurrent allocations of this size.
_READ_FIRST = 128 * 1024


# os.pread/os.pwrite are POSIX-only. On Windows (where the thread backend is the
# default) emulate them with lseek + read/write. Each file object serialises its
# own operations and concurrent ops use distinct fds, so the seek is safe.
if hasattr(os, 'pread'):
    _pread = os.pread
    _pwrite = os.pwrite

    def _append_write(fd: int, data: bytes) -> int:
        # os.write honours O_APPEND (atomic write at end); fast on POSIX.
        return os.write(fd, data)
else:

    def _pread(fd: int, n: int, offset: int) -> bytes:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, n)

    def _fileio_write(fd: int, data: bytes) -> int:
        # Windows os.write() is ~7x slower than io.FileIO.write() for large
        # buffers (a slow CRT path), so route writes through an unbuffered
        # FileIO — the same object the read-all path uses — which matches
        # aiofiles' buffered writer instead of trailing it. 'w' does not
        # truncate when wrapping an existing fd, so bytes before the seek
        # position survive. closefd=False leaves the fd for the backend to close.
        f = io.FileIO(fd, 'w', closefd=False)
        try:
            return f.write(data)
        finally:
            f.close()

    def _pwrite(fd: int, data: bytes, offset: int) -> int:
        os.lseek(fd, offset, os.SEEK_SET)
        return _fileio_write(fd, data)

    def _append_write(fd: int, data: bytes) -> int:
        # fd carries O_APPEND, so FileIO.write appends atomically at EOF.
        return _fileio_write(fd, data)


# os.fdatasync exists only on platforms with the fdatasync(2) syscall (Linux and
# some Unixes); macOS and Windows have os.fsync only.
_fdatasync = getattr(os, 'fdatasync', os.fsync)


def _read_all(fd: int, offset: int) -> bytes:
    """Read from ``offset`` to EOF inside the calling thread.

    Wraps the fd in an unbuffered ``io.FileIO`` and uses its C-level ``readall``
    rather than a Python ``lseek``/``os.read`` loop: that is markedly faster
    (on Windows ~4x, enough to beat aiofiles' buffered ``read()``), correctly
    handles short reads, and avoids the extra EOF read the loop paid per call.
    ``closefd=False`` leaves the fd open for the backend to close as usual."""
    os.lseek(fd, offset, os.SEEK_SET)
    reader = io.FileIO(fd, mode='r', closefd=False)
    try:
        return reader.readall()
    finally:
        reader.close()


def _stat_from_tuple(t: tuple) -> os.stat_result:
    """Build an :class:`os.stat_result` from the native statx tuple."""
    (
        mode,
        ino,
        dev,
        nlink,
        uid,
        gid,
        size,
        atime_ns,
        mtime_ns,
        ctime_ns,
        blksize,
        blocks,
        rdev,
    ) = t
    return os.stat_result(
        (
            mode,
            ino,
            dev,
            nlink,
            uid,
            gid,
            size,
            atime_ns / 1e9,
            mtime_ns / 1e9,
            ctime_ns / 1e9,
        ),
        {
            'st_atime_ns': atime_ns,
            'st_mtime_ns': mtime_ns,
            'st_ctime_ns': ctime_ns,
            'st_blksize': blksize,
            'st_blocks': blocks,
            'st_rdev': rdev,
        },
    )


class ThreadBackend:
    """Blocking syscalls dispatched to the loop's default executor."""

    name = 'thread'

    async def open(self, path: str, flags: int, mode: int) -> int:
        return await _to_thread(os.open, path, flags, mode)

    async def read(self, fd: int, offset: int, length: int) -> bytes:
        return await _to_thread(_pread, fd, length, offset)

    async def readall(self, fd: int, offset: int) -> bytes:
        # Read from `offset` to EOF in a single executor round-trip (the C-level
        # loop runs inside the worker thread), matching aiofiles' behaviour.
        return await _to_thread(_read_all, fd, offset)

    async def write(
        self, fd: int, offset: int, data: bytes, append: bool = False
    ) -> int:
        if append:
            # O_APPEND writes atomically at end; pwrite would ignore it and
            # write at the given offset. Uses the fast FileIO path on Windows.
            return await _to_thread(_append_write, fd, data)
        return await _to_thread(_pwrite, fd, data, offset)

    async def writev(
        self, fd: int, offset: int, bufs: list[bytes], append: bool = False
    ) -> int:
        # Vectored write where the OS supports it; otherwise join and fall
        # back to the plain write path (Windows has no writev).
        if append:
            if hasattr(os, 'writev'):
                return await _to_thread(os.writev, fd, bufs)
            return await self.write(fd, offset, b''.join(bufs), True)
        if hasattr(os, 'pwritev'):
            return await _to_thread(os.pwritev, fd, bufs, offset)
        return await self.write(fd, offset, b''.join(bufs))

    async def fsync(self, fd: int, datasync: bool = False) -> None:
        # os.fdatasync is Linux-only; on macOS/Windows fall back to os.fsync
        # (which flushes data to disk just the same, only with full metadata).
        fn = _fdatasync if datasync else os.fsync
        await _to_thread(fn, fd)

    async def close(self, fd: int) -> None:
        await _to_thread(os.close, fd)

    async def statx(self, path: str, follow: bool = True) -> os.stat_result:
        return await _to_thread(os.stat if follow else os.lstat, path)

    async def mkdir(self, path: str, mode: int = 0o777) -> None:
        await _to_thread(os.mkdir, path, mode)

    async def unlink(self, path: str, remove_dir: bool = False) -> None:
        await _to_thread(os.rmdir if remove_dir else os.unlink, path)

    async def rename(self, src: str, dst: str) -> None:
        await _to_thread(os.rename, src, dst)

    async def replace(self, src: str, dst: str) -> None:
        # os.replace overwrites an existing dst atomically on every platform
        # (os.rename does not on Windows).
        await _to_thread(os.replace, src, dst)

    async def symlink(self, target: str, linkpath: str) -> None:
        await _to_thread(os.symlink, target, linkpath)

    async def link(self, src: str, dst: str) -> None:
        await _to_thread(os.link, src, dst)

    async def filesize(self, fd: int) -> int:
        return (await _to_thread(os.fstat, fd)).st_size

    # -- always executor-backed (no io_uring opcode in the crate) ---------

    async def ftruncate(self, fd: int, length: int) -> None:
        await _to_thread(os.ftruncate, fd, length)

    async def chmod(self, path: str, mode: int) -> None:
        await _to_thread(os.chmod, path, mode)

    async def readlink(self, path: str) -> str:
        return await _to_thread(os.readlink, path)

    async def realpath(self, path: str) -> str:
        return await _to_thread(os.path.realpath, path)

    async def scandir(self, path: str) -> list[str]:
        return await _to_thread(os.listdir, path)

    async def utime(self, path: str, times) -> None:
        await _to_thread(functools.partial(os.utime, path, times))


class UringBackend(ThreadBackend):
    """Genuine io_uring submissions, falling back to the executor for ops the
    ring does not implement (inherited from :class:`ThreadBackend`)."""

    name = 'uring'

    def _driver(self):
        from .driver import get_driver

        return get_driver()

    async def open(self, path: str, flags: int, mode: int) -> int:
        return await self._driver().open(path, flags, mode)

    async def read(self, fd: int, offset: int, length: int) -> bytes:
        return await self._driver().read(fd, offset, length)

    async def readall(self, fd: int, offset: int) -> bytes:
        driver = self._driver()
        # First read covers small/medium files in a SINGLE op: a short read means
        # we already have the whole file, so we skip the fstatx entirely (it was
        # profiled as a needless second round-trip that dominated small reads).
        first = await driver.read(fd, offset, _READ_FIRST)
        if len(first) < _READ_FIRST:
            return first
        # Larger file: size it, then re-read the WHOLE span in one exact-size
        # read. The probe bytes are discarded, but re-reading 128 KiB of warm
        # page cache costs far less than the b''.join that stitching
        # probe+bulk would pay (a full copy of the file), and the single read
        # comes back as one zero-copy bytes object.
        try:
            st = _stat_from_tuple(await driver.fstatx(fd))
            size = st.st_size
        except OSError:
            size = 0
        total = max(0, size - offset)
        if _READ_FIRST < total <= _READ_MAX:
            parts = [await driver.read(fd, offset, total)]
        else:
            # Huge (> _READ_MAX) or unsizable file: keep the probe and pull
            # the remainder in ring-sized chunks.
            parts = [first]
            remaining = total - _READ_FIRST
            while remaining > 0:
                data = await driver.read(
                    fd, offset + sum(map(len, parts)), min(remaining, _READ_MAX)
                )
                if not data:
                    break
                parts.append(data)
                remaining -= len(data)
        # Drain to a genuinely empty read: short reads are not EOF and the
        # file may have grown since the fstatx.
        pos = offset + sum(map(len, parts))
        while True:
            chunk = await driver.read(fd, pos, _READ_CHUNK)
            if not chunk:
                break  # empty read == EOF
            parts.append(chunk)
            pos += len(chunk)
        if len(parts) == 1:
            return parts[0]
        return b''.join(parts)

    async def write(
        self, fd: int, offset: int, data: bytes, append: bool = False
    ) -> int:
        return await self._driver().write(fd, offset, data, append)

    async def writev(
        self, fd: int, offset: int, bufs: list[bytes], append: bool = False
    ) -> int:
        return await self._driver().writev(fd, offset, bufs, append)

    async def fsync(self, fd: int, datasync: bool = False) -> None:
        await self._driver().fsync(fd, datasync)

    # ftruncate became a real io_uring opcode in kernel 6.9; on older kernels
    # the completion reports it unsupported and we drop to the executor for
    # the rest of the process.
    _ftruncate_uring = True

    async def ftruncate(self, fd: int, length: int) -> None:
        if type(self)._ftruncate_uring:
            try:
                await self._driver().ftruncate(fd, length)
                return
            except OSError as e:
                if e.errno not in (errno.EINVAL, errno.EOPNOTSUPP, errno.ENOSYS):
                    raise
                type(self)._ftruncate_uring = False
        await super().ftruncate(fd, length)

    async def close(self, fd: int) -> None:
        await self._driver().close(fd)

    async def statx(self, path: str, follow: bool = True) -> os.stat_result:
        return _stat_from_tuple(await self._driver().statx(path, follow))

    async def filesize(self, fd: int) -> int:
        return _stat_from_tuple(await self._driver().fstatx(fd)).st_size

    # -- whole-file helpers on linked open→I/O chains ----------------------
    #
    # Path.read_bytes()/write_bytes() class helpers: open+first-I/O go to the
    # kernel as ONE linked submission on a direct descriptor, so a small-file
    # read or write costs a single submit/wakeup round-trip instead of three
    # (open, I/O, close). The close is folded into completion handling: reads
    # fire-and-forget it; writes await it so close errors surface.

    async def read_file(self, path: str) -> bytes:
        driver = self._driver()
        # No O_CLOEXEC on the chain: direct descriptors are not real fds (they
        # cannot leak across exec) and the kernel rejects the flag with EINVAL.
        chain = await driver.open_read(path, os.O_RDONLY, 0o666, _READ_FIRST)
        if chain is None:
            # No fixed slot free (or kernel too old): classic three-step path.
            fd = await self.open(path, os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0), 0o666)
            try:
                return await self.readall(fd, 0)
            finally:
                await self.close(fd)
        slot, first = chain
        try:
            if len(first) < _READ_FIRST:
                return first
            # Bigger than the probe: size the file and re-read the whole span
            # in one exact read on the SAME direct descriptor (same strategy
            # and rationale as readall). The path statx is only a size hint —
            # if it races a rename the drain loop below still reads the fd to
            # true EOF, at worst in chunks.
            try:
                size = (await self.statx(path)).st_size
            except OSError:
                size = 0
            if _READ_FIRST < size <= _READ_MAX:
                parts = [await driver.read_fixed(slot, 0, size)]
            else:
                parts = [first]
                remaining = size - _READ_FIRST
                while remaining > 0:
                    data = await driver.read_fixed(
                        slot, sum(map(len, parts)), min(remaining, _READ_MAX)
                    )
                    if not data:
                        break
                    parts.append(data)
                    remaining -= len(data)
            pos = sum(map(len, parts))
            while True:
                chunk = await driver.read_fixed(slot, pos, _READ_CHUNK)
                if not chunk:
                    break
                parts.append(chunk)
                pos += len(chunk)
            return parts[0] if len(parts) == 1 else b''.join(parts)
        finally:
            driver.close_fixed_nowait(slot)

    async def write_file(self, path: str, data: bytes) -> int:
        driver = self._driver()
        # As in read_file: no O_CLOEXEC on a direct-descriptor open (EINVAL).
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        chain = await driver.open_write(path, flags, 0o666, data)
        if chain is None:
            fd = await self.open(path, flags | getattr(os, 'O_CLOEXEC', 0), 0o666)
            try:
                written = 0
                while written < len(data):
                    n = await self.write(fd, written, data[written:])
                    if n <= 0:
                        raise OSError(errno.EIO, f'write returned {n}')
                    written += n
                return written
            finally:
                await self.close(fd)
        slot, written = chain
        try:
            while written < len(data):
                n = await driver.write_fixed(slot, written, data[written:])
                if n <= 0:
                    raise OSError(errno.EIO, f'write returned {n}')
                written += n
            return written
        finally:
            await driver.close_fixed(slot)

    async def mkdir(self, path: str, mode: int = 0o777) -> None:
        await self._driver().mkdir(path, mode)

    async def unlink(self, path: str, remove_dir: bool = False) -> None:
        await self._driver().unlink(path, remove_dir)

    async def rename(self, src: str, dst: str) -> None:
        await self._driver().rename(src, dst)

    async def symlink(self, target: str, linkpath: str) -> None:
        await self._driver().symlink(target, linkpath)

    async def link(self, src: str, dst: str) -> None:
        await self._driver().link(src, dst)


class IocpBackend(ThreadBackend):
    """Windows backend: real async read/write/open/close via IOCP, everything
    else inherited from the thread pool. Opt-in (``URIO_WINDOWS_IOCP``),
    production-validated on Windows 10/11 (CPython 3.14, GIL and free-threaded).
    See :mod:`urio.driver_iocp`."""

    name = 'iocp'

    def _driver(self):
        loop = asyncio.get_running_loop()
        from . import _uring as native

        # On a ProactorEventLoop (asyncio's Windows default) reap completions on
        # the loop thread via its own IOCP — no poller thread, no cross-thread
        # wakeup. Fall back to the poller-thread driver on other loops.
        if hasattr(loop, '_proactor') and hasattr(native, 'ProactorIo'):
            from .driver_iocp_proactor import get_proactor_driver

            return get_proactor_driver(loop)
        from .driver_iocp import get_iocp_driver

        return get_iocp_driver(loop)

    async def open(self, path: str, flags: int, mode: int) -> int:
        write = bool(flags & (os.O_WRONLY | os.O_RDWR))
        create = bool(flags & os.O_CREAT)
        truncate = bool(flags & os.O_TRUNC)
        exclusive = bool(flags & os.O_EXCL)
        return await self._driver().open(path, write, create, truncate, exclusive)

    async def read(self, fd: int, offset: int, length: int) -> bytes:
        return await self._driver().read(fd, offset, length)

    async def readall(self, fd: int, offset: int) -> bytes:
        # fd is a Windows HANDLE, not a C fd, so the thread-pool pread path does
        # not apply: loop IOCP reads until EOF (an empty read).
        driver = self._driver()
        parts = []
        while True:
            chunk = await driver.read(fd, offset, _READ_CHUNK)
            if not chunk:
                break
            parts.append(chunk)
            offset += len(chunk)
        return b''.join(parts)

    async def write(
        self, fd: int, offset: int, data: bytes, append: bool = False
    ) -> int:
        # Windows overlapped writes append when both offset halves are 0xFFFFFFFF
        # (i.e. a u64 offset of -1), matching urio's APPEND_OFFSET sentinel.
        off = (1 << 64) - 1 if append else offset
        return await self._driver().write(fd, off, data)

    async def fsync(self, fd: int, datasync: bool = False) -> None:
        # The HANDLE isn't a C fd, so flush it with FlushFileBuffers via ctypes.
        import ctypes

        await _to_thread(ctypes.windll.kernel32.FlushFileBuffers, fd)

    async def filesize(self, fd: int) -> int:
        import ctypes

        def _size(handle):
            n = ctypes.c_longlong(0)
            ctypes.windll.kernel32.GetFileSizeEx(handle, ctypes.byref(n))
            return n.value

        return await _to_thread(_size, fd)

    async def ftruncate(self, fd: int, length: int) -> None:
        # fd is a Windows HANDLE; set the pointer and SetEndOfFile via ctypes
        # rather than os.ftruncate (which expects a C fd).
        import ctypes

        def _trunc(handle):
            ctypes.windll.kernel32.SetFilePointerEx(
                handle, ctypes.c_longlong(length), None, 0
            )
            ctypes.windll.kernel32.SetEndOfFile(handle)

        await _to_thread(_trunc, fd)

    async def close(self, fd: int) -> None:
        await self._driver().close(fd)


def _iocp_ok() -> bool:
    """Whether the opt-in Windows IOCP backend can be used here."""
    if sys.platform != 'win32':
        return False
    if os.environ.get('URIO_WINDOWS_IOCP', '') not in ('1', 'true', 'yes'):
        return False
    try:
        from . import _uring as native

        return hasattr(native, 'Iocp')
    except ImportError:
        return False


_backend: ThreadBackend | None = None
_override: str | None = None


def set_backend(name: str) -> None:
    """Force the backend: ``'uring'``, ``'iocp'``, ``'thread'``, or ``'auto'``.

    ``'uring'`` raises if the native ring is unavailable; ``'iocp'`` raises
    unless on Windows with ``URIO_WINDOWS_IOCP=1`` set. Primarily useful for
    tests and benchmarking.
    """
    global _backend, _override
    if name not in ('uring', 'thread', 'iocp', 'auto'):
        raise ValueError(f'unknown backend: {name!r}')
    if name == 'uring' and not _native_ok():
        raise RuntimeError('io_uring backend is not available on this system')
    if name == 'iocp' and not _iocp_ok():
        raise RuntimeError(
            'IOCP backend unavailable (Windows-only, set URIO_WINDOWS_IOCP=1)'
        )
    _override = None if name == 'auto' else name
    _backend = None


def get_backend() -> ThreadBackend:
    """Return the active backend instance, selecting one on first use.

    Selection order: an explicit :func:`set_backend` override, then the
    ``URIO_BACKEND`` environment variable, then auto-detection (io_uring when
    available, otherwise the thread pool).
    """
    global _backend
    if _backend is not None:
        return _backend
    choice = _override or os.environ.get('URIO_BACKEND', 'auto')
    if choice == 'thread':
        _backend = ThreadBackend()
    elif choice == 'uring':
        if not _native_ok():
            raise RuntimeError('io_uring backend is not available on this system')
        _backend = UringBackend()
    elif choice == 'iocp':
        if not _iocp_ok():
            raise RuntimeError('IOCP backend is not available on this system')
        _backend = IocpBackend()
    elif _native_ok():
        _backend = UringBackend()
    elif _iocp_ok():
        _backend = IocpBackend()
    else:
        _backend = ThreadBackend()
    return _backend
