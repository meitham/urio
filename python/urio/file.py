"""Async file objects backing :func:`urio.open`.

io_uring reads and writes are positional, so the file objects here track a
logical offset themselves and add a read-ahead buffer to give ordinary
``read``/``readline`` semantics on top of positioned I/O. Binary mode is fully
positional (``seek``/``tell``/``truncate``). Text mode layers incremental
codecs and universal-newline translation over the binary object for forward
reading and writing.

``urio.open`` returns an object that is both awaitable and an async context
manager, mirroring ``aiofiles``::

    f = await urio.open('x')
    await f.close()
    async with urio.open('x') as f:
        ...
"""

from __future__ import annotations

import asyncio
import errno
import io
import locale
import os
import sys

from .backend import get_backend

DEFAULT_CHUNK = 128 * 1024
MAX_IO = 1 << 30
APPEND_OFFSET = -1

_CODEC_OFFLOAD_MIN = 32 * 1024
_ENCODE_OFFLOAD_MIN_GIL = 4 << 20
_gil_enabled = getattr(sys, '_is_gil_enabled', None)


def _no_gil() -> bool:
    return _gil_enabled is not None and not _gil_enabled()


def _should_offload_encode(n: int) -> bool:
    return n >= (_CODEC_OFFLOAD_MIN if _no_gil() else _ENCODE_OFFLOAD_MIN_GIL)


def _should_offload_decode(n: int) -> bool:
    return n >= _CODEC_OFFLOAD_MIN and _no_gil()


def _parse_mode(mode: str):
    """Translate a mode string into open(2) flags and behaviour flags.

    Returns ``(flags, binary, readable, writable, appending)``.
    """
    if not mode:
        raise ValueError('empty mode string')
    chars = set(mode)
    if chars - set('rwxab+t'):
        raise ValueError(f'invalid mode: {mode!r}')
    if len(mode) != len(chars):
        raise ValueError(f'invalid mode: {mode!r}')

    if 't' in chars and 'b' in chars:
        raise ValueError("can't have text and binary mode at once")
    binary = 'b' in chars

    primaries = chars & set('rwxa')
    if len(primaries) != 1:
        raise ValueError('must have exactly one of create/read/write/append mode')

    plus = '+' in chars
    flags = getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_BINARY', 0)
    readable = writable = appending = False

    if 'r' in chars:
        flags |= os.O_RDWR if plus else os.O_RDONLY
        readable = True
        writable = plus
    elif 'w' in chars:
        flags |= os.O_RDWR if plus else os.O_WRONLY
        flags |= os.O_CREAT | os.O_TRUNC
        writable = True
        readable = plus
    elif 'a' in chars:
        flags |= os.O_RDWR if plus else os.O_WRONLY
        flags |= os.O_CREAT | os.O_APPEND
        writable = True
        readable = plus
        appending = True
    elif 'x' in chars:
        flags |= os.O_RDWR if plus else os.O_WRONLY
        flags |= os.O_CREAT | os.O_EXCL
        writable = True
        readable = plus

    return flags, binary, readable, writable, appending


class AsyncBufferedFile:
    """Binary async file with a read-ahead buffer over positional I/O."""

    def __init__(
        self,
        fd: int,
        *,
        name: str,
        mode: str,
        readable: bool,
        writable: bool,
        appending: bool,
        size: int,
    ):
        self._fd = fd
        self._name = name
        self._mode = mode
        self._readable = readable
        self._writable = writable
        self._backend = get_backend()
        self._closed = False
        self._appending = appending
        self._offset: int = size if appending else 0
        self._buf: bytes = b''

    @property
    def name(self):
        return self._name

    @property
    def mode(self):
        return self._mode

    @property
    def closed(self):
        return self._closed

    def fileno(self):
        self._check_open()
        return self._fd

    def readable(self) -> bool:
        return self._readable

    def writable(self) -> bool:
        return self._writable

    def seekable(self) -> bool:
        return True

    def _check_open(self):
        if self._closed:
            raise ValueError('I/O operation on closed file')

    async def _fill(self, want):
        """Ensure ``_buf`` holds at least ``want`` bytes (``want >= 0``), or EOF.

        Each underlying read requests exactly the still-missing amount, so a
        sized read normally costs a single round-trip instead of many fixed-size
        chunks. Returns False when EOF was hit.
        """
        while len(self._buf) < want:
            need = min(want - len(self._buf), MAX_IO)
            fetch_at = self._offset + len(self._buf)
            chunk = await self._backend.read(self._fd, fetch_at, need)
            if not chunk:
                return False
            self._buf += chunk
        return True

    async def _readall(self):
        """Read to EOF in a single backend round-trip where possible (the
        backend sizes the file and pulls the remainder in one read), appending
        to any data already buffered."""
        fetch_at = self._offset + len(self._buf)
        rest = await self._backend.readall(self._fd, fetch_at)
        self._buf += rest

    async def read(self, size: int | None = -1) -> bytes:
        self._check_open()
        if not self._readable:
            raise io.UnsupportedOperation('not readable')
        if size is None or size < 0:
            await self._readall()
            data = self._buf
            self._buf = b''
            self._offset += len(data)
            return data
        if not self._buf:
            want = min(size, MAX_IO)
            chunk = await self._backend.read(self._fd, self._offset, want)
            if not chunk or (len(chunk) == want == size):
                self._offset += len(chunk)
                return chunk
            self._buf = chunk
        await self._fill(size)
        data = self._buf[:size]
        self._buf = self._buf[len(data) :]
        self._offset += len(data)
        return data

    async def readall(self) -> bytes:
        return await self.read(-1)

    async def readinto(self, b) -> int:
        self._check_open()
        if not self._readable:
            raise io.UnsupportedOperation('not readable')
        n = len(b)
        await self._fill(n)
        avail = min(n, len(self._buf))
        mv = b if isinstance(b, memoryview) else memoryview(b)
        mv[:avail] = self._buf[:avail]
        self._buf = self._buf[avail:]
        self._offset += avail
        return avail

    async def readline(self, size: int | None = -1) -> bytes:
        self._check_open()
        if not self._readable:
            raise io.UnsupportedOperation('not readable')
        parts = []
        total = 0
        while True:
            nl = self._buf.find(b'\n')
            if nl != -1:
                end = nl + 1
                if size is not None and size >= 0 and total + end > size:
                    end = size - total
                line = self._buf[:end]
                self._buf = self._buf[end:]
                self._offset += len(line)
                parts.append(line)
                return b''.join(parts)
            if size is not None and size >= 0 and total + len(self._buf) >= size:
                take = size - total
                line = self._buf[:take]
                self._buf = self._buf[take:]
                self._offset += len(line)
                parts.append(line)
                return b''.join(parts)
            parts.append(self._buf)
            total += len(self._buf)
            self._offset += len(self._buf)
            self._buf = b''
            fetch_at = self._offset
            chunk = await self._backend.read(self._fd, fetch_at, DEFAULT_CHUNK)
            if not chunk:
                return b''.join(parts)
            self._buf = chunk

    async def readlines(self, hint: int | None = -1) -> list[bytes]:
        lines = []
        total = 0
        while True:
            line = await self.readline()
            if not line:
                break
            lines.append(line)
            total += len(line)
            if hint is not None and hint >= 0 and total >= hint:
                break
        return lines

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        line = await self.readline()
        if not line:
            raise StopAsyncIteration
        return line

    async def write(self, data) -> int:
        self._check_open()
        if not self._writable:
            raise io.UnsupportedOperation('not writable')
        if isinstance(data, str):
            raise TypeError('a bytes-like object is required, not str')
        data = bytes(data)
        self._buf = b''
        total = len(data)
        n = 0
        while n < total:
            chunk = data if (n == 0 and total <= MAX_IO) else data[n : n + MAX_IO]
            if self._appending:
                written = await self._backend.write(
                    self._fd, APPEND_OFFSET, chunk, append=True
                )
            else:
                written = await self._backend.write(self._fd, self._offset, chunk)
            if written <= 0:
                raise OSError(
                    errno.EIO, f'write returned {written} of {total - n} bytes'
                )
            n += written
            self._offset += written
        return n

    _IOV_MAX = 1024

    async def writelines(self, lines) -> None:
        self._check_open()
        if not self._writable:
            raise io.UnsupportedOperation('not writable')
        bufs = [b if type(b) is bytes else bytes(b) for b in lines]
        bufs = [b for b in bufs if b]
        if not bufs:
            return
        if len(bufs) == 1:
            await self.write(bufs[0])
            return
        self._buf = b''
        group: list[bytes] = []
        group_len = 0

        async def _flush_group():
            nonlocal group, group_len
            if not group:
                return
            total, batch = group_len, group
            group, group_len = [], 0
            written = await self._backend.writev(
                self._fd,
                APPEND_OFFSET if self._appending else self._offset,
                batch,
                append=self._appending,
            )
            if written < 0:
                raise OSError(errno.EIO, f'writev returned {written}')
            self._offset += written
            if written < total:
                tail = b''.join(batch)[written:]
                await self.write(tail)

        for b in bufs:
            if len(b) > MAX_IO:
                await _flush_group()
                await self.write(b)
                continue
            if len(group) >= self._IOV_MAX or group_len + len(b) > MAX_IO:
                await _flush_group()
            group.append(b)
            group_len += len(b)
        await _flush_group()

    def tell(self) -> int:
        self._check_open()
        return self._offset

    async def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._check_open()
        self._buf = b''
        if whence == os.SEEK_SET:
            new = offset
        elif whence == os.SEEK_CUR:
            new = self._offset + offset
        elif whence == os.SEEK_END:
            new = await self._backend.filesize(self._fd) + offset
        else:
            raise ValueError(f'invalid whence: {whence}')
        if new < 0:
            raise OSError(22, 'Invalid argument')
        self._offset = new
        return new

    async def truncate(self, size: int | None = None) -> int:
        self._check_open()
        if not self._writable:
            raise io.UnsupportedOperation('not writable')
        if size is None:
            size = self._offset
        await self._backend.ftruncate(self._fd, size)
        self._buf = b''
        return size

    async def flush(self) -> None:
        self._check_open()

    async def fsync(self) -> None:
        self._check_open()
        await self._backend.fsync(self._fd, datasync=False)

    async def fdatasync(self) -> None:
        self._check_open()
        await self._backend.fsync(self._fd, datasync=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._backend.close(self._fd)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


class AsyncTextFile:
    """Text wrapper adding incremental codecs and newline handling.

    Reading, writing, and positioning are all supported: ``seek``/``tell`` work
    in **byte offsets** (like ``io.TextIOWrapper`` cookies). ``tell`` is exact at
    a boundary (nothing buffered) and best-effort mid-buffer under universal
    newlines, where the byte<->char mapping is lossy; seek to a value ``tell``
    returned to restore an exact position.
    """

    def __init__(
        self,
        raw: AsyncBufferedFile,
        *,
        encoding: str,
        errors: str,
        newline: str | None,
    ):
        self._raw = raw
        self._encoding = encoding
        self._errors = errors
        self._newline = newline
        self._closed = False
        self._tbuf: str = ''
        self._cr_pending = ''
        self._at_eof = False

        import codecs

        self._decoder = codecs.getincrementaldecoder(encoding)(errors)
        self._encoder = codecs.getincrementalencoder(encoding)(errors)

        self._read_translate = newline is None
        self._read_universal = newline in (None, '')
        self._read_terminator: str | None
        if newline in (None, ''):
            self._read_terminator = None
        else:
            self._read_terminator = newline
        if newline is None:
            self._write_newline = os.linesep
        elif newline == '':
            self._write_newline = ''
        else:
            self._write_newline = newline

    @property
    def name(self):
        return self._raw.name

    @property
    def mode(self):
        return self._raw.mode

    @property
    def encoding(self):
        return self._encoding

    @property
    def errors(self):
        return self._errors

    @property
    def newlines(self):
        return None

    @property
    def closed(self):
        return self._closed

    def fileno(self):
        return self._raw.fileno()

    def readable(self):
        return self._raw.readable()

    def writable(self):
        return self._raw.writable()

    def seekable(self):
        return self._raw.seekable()

    def _check_open(self):
        if self._closed:
            raise ValueError('I/O operation on closed file')

    def _translate(self, text, final):
        if not self._read_translate:
            return text
        text = self._cr_pending + text
        self._cr_pending = ''
        if not final and text.endswith('\r'):
            self._cr_pending = '\r'
            text = text[:-1]
        if '\r' in text:
            return text.replace('\r\n', '\n').replace('\r', '\n')
        return text

    def _decode_and_translate(self, chunk, final):
        return self._translate(self._decoder.decode(chunk, final), final)

    async def _decode(self, chunk, final):
        """Decode + newline-translate ``chunk``, offloading to the executor for
        large payloads on a free-threaded interpreter so concurrent files'
        codecs run on separate cores (see :func:`_should_offload_decode`)."""
        if _should_offload_decode(len(chunk)):
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, self._decode_and_translate, chunk, final
            )
        return self._decode_and_translate(chunk, final)

    async def _decode_more(self, want=DEFAULT_CHUNK):
        """Pull ``want`` bytes from the raw file and append decoded text to
        _tbuf. Returns False at EOF."""
        if self._at_eof:
            return False
        chunk = await self._raw.read(want)
        final = not chunk
        text = await self._decode(chunk, final)
        if final:
            self._at_eof = True
        self._tbuf += text
        return not final

    async def read(self, size: int | None = -1) -> str:
        self._check_open()
        if size is None or size < 0:
            if not self._at_eof:
                raw = await self._raw.read(-1)
                text = await self._decode(raw, True)
                self._at_eof = True
                self._tbuf += text
            data = self._tbuf
            self._tbuf = ''
            return data
        while len(self._tbuf) < size and not self._at_eof:
            want = max(DEFAULT_CHUNK, size - len(self._tbuf))
            await self._decode_more(want)
        data = self._tbuf[:size]
        self._tbuf = self._tbuf[len(data) :]
        return data

    def _find_line_end(self):
        """Index just past the next line terminator in _tbuf, or -1."""
        if self._read_universal:
            i_n = self._tbuf.find('\n')
            i_r = self._tbuf.find('\r')
            if i_n == -1 and i_r == -1:
                return -1
            if self._read_translate:
                return -1 if i_n == -1 else i_n + 1
            if i_r == -1:
                return i_n + 1
            if i_n == -1:
                return i_r + 1 if i_r + 1 < len(self._tbuf) or self._at_eof else -2
            if i_r < i_n:
                if i_r + 1 == i_n:
                    return i_n + 1
                return i_r + 1
            return i_n + 1
        else:
            terminator = self._read_terminator
            assert terminator is not None
            idx = self._tbuf.find(terminator)
            if idx == -1:
                return -1
            return idx + len(terminator)

    async def readline(self, size: int | None = -1) -> str:
        self._check_open()
        cap = size if size is not None and size >= 0 else None

        def _take(end: int) -> str:
            if cap is not None and end > cap:
                end = cap
            line = self._tbuf[:end]
            self._tbuf = self._tbuf[end:]
            return line

        while True:
            end = self._find_line_end()
            if end == -2:
                if not await self._decode_more():
                    return _take(self._tbuf.find('\r') + 1)
                continue
            if end != -1:
                return _take(end)
            if cap is not None and len(self._tbuf) >= cap:
                return _take(cap)
            if not await self._decode_more():
                line = self._tbuf
                self._tbuf = ''
                return line

    async def readlines(self, hint: int | None = -1) -> list[str]:
        lines = []
        total = 0
        while True:
            line = await self.readline()
            if not line:
                break
            lines.append(line)
            total += len(line)
            if hint is not None and hint >= 0 and total >= hint:
                break
        return lines

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        line = await self.readline()
        if not line:
            raise StopAsyncIteration
        return line

    def _encode(self, text: str) -> bytes:
        out = text
        if self._write_newline and self._write_newline != '\n':
            out = out.replace('\n', self._write_newline)
        return self._encoder.encode(out)

    async def write(self, text: str) -> int:
        self._check_open()
        if not isinstance(text, str):
            raise TypeError(f'write() argument must be str, not {type(text).__name__}')
        if _should_offload_encode(len(text)):
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, self._encode, text)
        else:
            data = self._encode(text)
        await self._raw.write(data)
        return len(text)

    async def writelines(self, lines) -> None:
        text = ''.join(lines)
        if text:
            await self.write(text)

    def tell(self) -> int:
        self._check_open()
        if not self._tbuf:
            return self._raw.tell()
        pending = len(self._tbuf.encode(self._encoding, self._errors))
        return max(0, self._raw.tell() - pending)

    async def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._check_open()
        if whence == os.SEEK_CUR:
            if offset == 0:
                return self.tell()
            offset = self.tell() + offset
            whence = os.SEEK_SET
        if whence == os.SEEK_SET:
            new = await self._raw.seek(offset)
        elif whence == os.SEEK_END:
            new = await self._raw.seek(offset, os.SEEK_END)
        else:
            raise ValueError(f'invalid whence: {whence}')
        self._tbuf = ''
        self._cr_pending = ''
        self._at_eof = False
        self._decoder.reset()
        return new

    async def flush(self) -> None:
        await self._raw.flush()

    async def fsync(self) -> None:
        await self._raw.fsync()

    async def fdatasync(self) -> None:
        await self._raw.fdatasync()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._raw.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


async def _open(
    path, mode, encoding, errors, newline
) -> AsyncBufferedFile | AsyncTextFile:
    backend = get_backend()
    flags, binary, readable, writable, appending = _parse_mode(mode)
    fspath = os.fspath(path)
    name = fspath if isinstance(fspath, str) else os.fsdecode(fspath)

    fd = await backend.open(name, flags, 0o666)
    try:
        size = 0
        if appending:
            try:
                size = await backend.filesize(fd)
            except OSError:
                size = 0

        raw = AsyncBufferedFile(
            fd,
            name=name,
            mode=mode,
            readable=readable,
            writable=writable,
            appending=appending,
            size=size,
        )
        if binary:
            return raw
        if encoding is None:
            encoding = locale.getpreferredencoding(False)
        if errors is None:
            errors = 'strict'
        return AsyncTextFile(raw, encoding=encoding, errors=errors, newline=newline)
    except BaseException:
        await asyncio.shield(backend.close(fd))
        raise


class _OpenContext:
    """Awaitable + async context manager returned by :func:`open`."""

    __slots__ = ('_coro', '_file')

    def __init__(self, coro):
        self._coro = coro
        self._file = None

    def __await__(self):
        return self._coro.__await__()

    async def __aenter__(self):
        self._file = await self._coro
        return self._file

    async def __aexit__(self, *exc):
        if self._file is not None:
            await self._file.close()


def open(
    path,
    mode: str = 'r',
    *,
    encoding=None,
    errors=None,
    newline=None,
):
    """Open ``path`` and return an awaitable async file / context manager.

    Mirrors the builtin :func:`open` surface for the common cases. Binary modes
    return :class:`AsyncBufferedFile`; text modes return :class:`AsyncTextFile`.
    """
    return _OpenContext(_open(path, mode, encoding, errors, newline))
