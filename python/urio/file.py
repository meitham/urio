"""Async file objects backing :func:`urio.open`.

io_uring reads and writes are positional, so the file objects here track a
logical offset themselves and add a read-ahead buffer to give ordinary
``read``/``readline`` semantics on top of positioned I/O. Binary mode is fully
positional (``seek``/``tell``/``truncate``). Text mode layers incremental
codecs and universal-newline translation over the binary object for forward
reading and writing.

``urio.open`` returns an object that is both awaitable and an async context
manager, mirroring ``aiofiles``::

    f = await urio.open('x')          # then await f.close()
    async with urio.open('x') as f:   # closed automatically
        ...
"""

from __future__ import annotations

import asyncio as _asyncio
import errno as _errno
import io as _io
import locale as _locale
import os as _os
import sys as _sys

from .backend import get_backend as _get_backend

# Chunk used for line-oriented reads and the text decoder.
DEFAULT_CHUNK = 128 * 1024
# Largest single read/write submitted to the ring: io_uring lengths are u32, so
# cap well below 4 GiB and let the buffered layers loop for anything bigger.
MAX_IO = 1 << 30
# Sentinel offset meaning "append at the current end of file" (io_uring treats
# an offset of -1 as the file's current position; combined with O_APPEND the
# kernel appends atomically).
APPEND_OFFSET = -1

# The text codec (encode/decode + newline translation) is CPU-bound and runs on
# the event-loop thread. On a free-threaded (no-GIL) interpreter that serialises
# concurrent files' codecs on one core, while a thread pool (aiofiles) spreads
# them across cores; offloading to the executor once a payload is big enough to
# outweigh the hop recovers that parallelism. Under the GIL the work serialises
# regardless, so the hop would only add latency — decode/encode inline there.
# 32 KiB was the empirical crossover on an 8-core Windows box (64 KiB text reads
# went from ~0.35x to ~0.83x vs aiofiles; smaller reads unaffected).
_CODEC_OFFLOAD_MIN = 32 * 1024
_gil_enabled = getattr(_sys, '_is_gil_enabled', None)


def _should_offload_codec(n: int) -> bool:
    return n >= _CODEC_OFFLOAD_MIN and _gil_enabled is not None and not _gil_enabled()


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
    # O_CLOEXEC is POSIX-only; harmless to omit where it doesn't exist (Windows).
    # O_BINARY is Windows-only and essential there: without it the CRT opens the
    # fd in *text* mode and os.read/os.write silently translate CRLF<->LF, which
    # corrupts binary data and desyncs byte offsets. urio does all newline
    # handling itself at the Python layer, so every fd must be binary.
    flags = getattr(_os, 'O_CLOEXEC', 0) | getattr(_os, 'O_BINARY', 0)
    readable = writable = appending = False

    if 'r' in chars:
        flags |= _os.O_RDWR if plus else _os.O_RDONLY
        readable = True
        writable = plus
    elif 'w' in chars:
        flags |= _os.O_RDWR if plus else _os.O_WRONLY
        flags |= _os.O_CREAT | _os.O_TRUNC
        writable = True
        readable = plus
    elif 'a' in chars:
        flags |= _os.O_RDWR if plus else _os.O_WRONLY
        flags |= _os.O_CREAT | _os.O_APPEND
        writable = True
        readable = plus
        appending = True
    elif 'x' in chars:
        flags |= _os.O_RDWR if plus else _os.O_WRONLY
        flags |= _os.O_CREAT | _os.O_EXCL
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
        self._backend = _get_backend()
        self._closed = False
        self._appending = appending
        # Logical position of the next byte to read/write. _buf holds read-ahead
        # bytes that begin exactly at _offset.
        self._offset: int = size if appending else 0
        self._buf: bytes = b''

    # -- introspection ----------------------------------------------------

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

    # -- reading ----------------------------------------------------------

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
            # Only an empty read is EOF. A short (but non-empty) read does NOT
            # imply EOF — a single io_uring read can legitimately return fewer
            # bytes than requested — so keep going until we fill or hit EOF.
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
            raise _io.UnsupportedOperation('not readable')
        if size is None or size < 0:
            await self._readall()
            data = self._buf
            self._buf = b''
            self._offset += len(data)
            return data
        if not self._buf:
            # Fast path: nothing buffered. Satisfy the request from a single
            # backend read and return its bytes *directly* — no `_buf +=` and no
            # slice, so the only copy is the unavoidable one into `bytes`. A
            # regular-file read normally returns the whole request; only a short
            # (non-EOF) read, or a request larger than one submission, needs the
            # buffered path below.
            want = min(size, MAX_IO)
            chunk = await self._backend.read(self._fd, self._offset, want)
            if not chunk or (len(chunk) == want == size):
                self._offset += len(chunk)
                return chunk
            self._buf = chunk  # short read: keep it (still begins at _offset)
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
            raise _io.UnsupportedOperation('not readable')
        n = len(b)
        await self._fill(n)
        avail = min(n, len(self._buf))
        # Copy straight from the read-ahead buffer into the caller's buffer,
        # skipping the intermediate bytes object that read() would allocate.
        mv = b if isinstance(b, memoryview) else memoryview(b)
        mv[:avail] = self._buf[:avail]
        self._buf = self._buf[avail:]
        self._offset += avail
        return avail

    async def readline(self, size: int | None = -1) -> bytes:
        self._check_open()
        if not self._readable:
            raise _io.UnsupportedOperation('not readable')
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
            # Need more data: pull another chunk past the consumed line so far.
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

    # -- writing ----------------------------------------------------------

    async def write(self, data) -> int:
        self._check_open()
        if not self._writable:
            raise _io.UnsupportedOperation('not writable')
        if isinstance(data, str):
            raise TypeError('a bytes-like object is required, not str')
        data = bytes(data)
        # Writing invalidates read-ahead at the current position.
        self._buf = b''
        total = len(data)
        n = 0
        while n < total:
            # Avoid copying when the whole payload fits one submission (the
            # common case): slicing here would duplicate multi-MB buffers.
            chunk = data if (n == 0 and total <= MAX_IO) else data[n : n + MAX_IO]
            if self._appending:
                # O_APPEND + offset -1: the kernel writes at the true end of the
                # file atomically, so concurrent appends never overwrite.
                written = await self._backend.write(
                    self._fd, APPEND_OFFSET, chunk, append=True
                )
            else:
                written = await self._backend.write(self._fd, self._offset, chunk)
            if written <= 0:
                # No progress: fail loudly rather than silently reporting a
                # short or complete write. The kernel gave no errno (a genuine
                # failure raises OSError upstream), so report the stall as EIO
                # instead of guessing a cause.
                raise OSError(
                    _errno.EIO, f'write returned {written} of {total - n} bytes'
                )
            n += written
            self._offset += written
        return n

    # writev is capped by the kernel at IOV_MAX iovecs per call.
    _IOV_MAX = 1024

    async def writelines(self, lines) -> None:
        # Vectored write: one submission with an iovec per line, so the lines
        # go to the kernel from their own buffers with no join copy. Empty
        # input stays a no-op so it never clears read-ahead or touches the
        # ring; a single line is a plain write.
        self._check_open()
        if not self._writable:
            raise _io.UnsupportedOperation('not writable')
        bufs = [b if type(b) is bytes else bytes(b) for b in lines]
        bufs = [b for b in bufs if b]
        if not bufs:
            return
        if len(bufs) == 1:
            await self.write(bufs[0])
            return
        self._buf = b''
        # Pack lines into writev batches bounded by IOV_MAX and MAX_IO; a
        # single oversized line goes through write(), which chunks it.
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
                raise OSError(_errno.EIO, f'writev returned {written}')
            self._offset += written
            if written < total:
                # Short vectored write (rare): push the unwritten tail through
                # the plain write path, which loops until done or fails loudly.
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

    # -- positioning ------------------------------------------------------

    def tell(self) -> int:
        self._check_open()
        return self._offset

    async def seek(self, offset: int, whence: int = _os.SEEK_SET) -> int:
        self._check_open()
        self._buf = b''
        if whence == _os.SEEK_SET:
            new = offset
        elif whence == _os.SEEK_CUR:
            new = self._offset + offset
        elif whence == _os.SEEK_END:
            # Size the open fd, not the path — the name may have been renamed,
            # replaced, or unlinked since open (log rotation, atomic replace).
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
            raise _io.UnsupportedOperation('not writable')
        if size is None:
            size = self._offset
        await self._backend.ftruncate(self._fd, size)
        # Drop read-ahead: it may hold bytes from beyond the new EOF.
        self._buf = b''
        return size

    # -- durability -------------------------------------------------------

    async def flush(self) -> None:
        # Writes go straight to the kernel; there is no userspace buffer to
        # flush. Provided for file-object compatibility.
        self._check_open()

    async def fsync(self) -> None:
        self._check_open()
        await self._backend.fsync(self._fd, datasync=False)

    async def fdatasync(self) -> None:
        self._check_open()
        await self._backend.fsync(self._fd, datasync=True)

    # -- lifecycle --------------------------------------------------------

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
        self._cr_pending = ''  # CR held across a chunk boundary (universal NL)
        self._at_eof = False

        import codecs

        self._decoder = codecs.getincrementaldecoder(encoding)(errors)
        self._encoder = codecs.getincrementalencoder(encoding)(errors)

        # Read translation: only universal mode (newline=None) rewrites endings.
        self._read_translate = newline is None
        self._read_universal = newline in (None, '')
        self._read_terminator: str | None
        if newline in (None, ''):
            self._read_terminator = None  # any of \r, \n, \r\n
        else:
            self._read_terminator = newline
        # Write translation.
        if newline is None:
            self._write_newline = _os.linesep
        elif newline == '':
            self._write_newline = ''
        else:
            self._write_newline = newline

    # -- introspection ----------------------------------------------------

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

    # -- decoding helpers -------------------------------------------------

    def _translate(self, text, final):
        # Universal-newline translation, but only pay for the two full-string
        # passes when a CR is actually present.
        if not self._read_translate:
            return text
        # Carry a CR pending from the previous chunk so a '\r\n' split across a
        # read boundary is not mistranslated into two newlines.
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
        codecs run on separate cores (see :func:`_should_offload_codec`)."""
        if _should_offload_codec(len(chunk)):
            loop = _asyncio.get_running_loop()
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
            # Bulk path: pull all remaining bytes in one read-all round-trip and
            # decode them in a single pass, rather than looping 128 KB chunks
            # through the incremental decoder with O(n^2) string growth.
            if not self._at_eof:
                raw = await self._raw.read(-1)
                text = await self._decode(raw, True)
                self._at_eof = True
                self._tbuf += text
            data = self._tbuf
            self._tbuf = ''
            return data
        while len(self._tbuf) < size and not self._at_eof:
            # Ask the raw layer for the whole remaining deficit (bytes >= chars
            # for any encoding, so this normally satisfies the request in one
            # round-trip), but never below DEFAULT_CHUNK so small reads still
            # buffer ahead. The binary sized read already does this; matching it
            # here removes the per-128 KB round-trip on large text reads.
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
                # Endings already normalised to \n.
                return -1 if i_n == -1 else i_n + 1
            # newline='' : recognise \r, \n, \r\n without translation.
            if i_r == -1:
                return i_n + 1
            if i_n == -1:
                return i_r + 1 if i_r + 1 < len(self._tbuf) or self._at_eof else -2
            if i_r < i_n:
                if i_r + 1 == i_n:
                    return i_n + 1  # \r\n
                return i_r + 1
            return i_n + 1
        else:
            terminator = self._read_terminator
            assert terminator is not None  # non-universal mode always sets it
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
                # Ambiguous trailing \r; need one more char to decide \r\n.
                if not await self._decode_more():
                    return _take(self._tbuf.find('\r') + 1)
                continue
            if end != -1:
                return _take(end)
            # No terminator yet. Honour the size cap so a single huge line does
            # not buffer unboundedly.
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
        # Offload the encode (+ newline translation) for large text on a
        # free-threaded interpreter, so concurrent writers encode on separate
        # cores instead of serialising on the loop thread; inline otherwise.
        if _should_offload_codec(len(text)):
            loop = _asyncio.get_running_loop()
            data = await loop.run_in_executor(None, self._encode, text)
        else:
            data = self._encode(text)
        await self._raw.write(data)
        return len(text)

    async def writelines(self, lines) -> None:
        # Coalesce into a single encode + submission rather than one round-trip
        # per line; newline translation and encoding are per-character, so the
        # joined result is identical to writing each line in turn.
        text = ''.join(lines)
        if text:
            await self.write(text)

    def tell(self) -> int:
        self._check_open()
        if not self._tbuf:
            # Exact: nothing buffered, so the raw read pointer is the position.
            # This is the common case (e.g. tell() after read()).
            return self._raw.tell()
        # Best-effort while data is still buffered: under universal-newline
        # translation the byte<->char mapping is lossy (\r\n collapses to \n),
        # so subtracting the re-encoded buffer is approximate. Calling tell() at
        # a boundary (after reading the buffer out) yields an exact value.
        pending = len(self._tbuf.encode(self._encoding, self._errors))
        return max(0, self._raw.tell() - pending)

    async def seek(self, offset: int, whence: int = _os.SEEK_SET) -> int:
        self._check_open()
        # Positions are byte offsets, matching tell(). Seek to a value tell()
        # returned (call tell() with nothing buffered for an exact cookie) to
        # restore an exact location; the reset below re-primes the incremental
        # decoder and newline state. Seeking to an arbitrary byte that splits a
        # code point or a `\r\n` pair is allowed but — as with CPython's
        # TextIOWrapper on a non-cookie target — may mis-decode the next chars.
        if whence == _os.SEEK_CUR:
            if offset == 0:
                return self.tell()
            offset = self.tell() + offset
            whence = _os.SEEK_SET
        if whence == _os.SEEK_SET:
            new = await self._raw.seek(offset)
        elif whence == _os.SEEK_END:
            new = await self._raw.seek(offset, _os.SEEK_END)
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
    backend = _get_backend()
    flags, binary, readable, writable, appending = _parse_mode(mode)
    fspath = _os.fspath(path)
    name = fspath if isinstance(fspath, str) else _os.fsdecode(fspath)

    fd = await backend.open(name, flags, 0o666)
    size = 0
    if appending:
        # Size the fd we actually opened, not the path: the name may already
        # point elsewhere (renamed/replaced concurrently), and the fd stat is
        # one path resolution cheaper.
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
        encoding = _locale.getpreferredencoding(False)
    if errors is None:
        errors = 'strict'
    return AsyncTextFile(raw, encoding=encoding, errors=errors, newline=newline)


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
