#!/usr/bin/env python3
import asyncio
import io
import os

import pytest

import urio


async def test_invalid_text_encoding_closes_opened_file(backend, tmpfile, monkeypatch):
    tmpfile.write_bytes(b'x')
    active = urio.get_backend()
    original_open = active.open
    original_close = active.close
    opened = []
    closed = []

    async def tracked_open(*args):
        fd = await original_open(*args)
        opened.append(fd)
        return fd

    async def tracked_close(fd):
        closed.append(fd)
        await original_close(fd)

    monkeypatch.setattr(active, 'open', tracked_open)
    monkeypatch.setattr(active, 'close', tracked_close)
    with pytest.raises(LookupError):
        await urio.open(tmpfile, 'r', encoding='not-a-real-codec')
    assert len(opened) == 1
    assert closed == opened


async def test_text_crlf_across_chunk_boundary(backend, tmpfile):
    # given: a CRLF that straddles the 128 KB internal read-chunk boundary
    from urio.file import DEFAULT_CHUNK

    head = b'a' * (DEFAULT_CHUNK - 1)
    tmpfile.write_bytes(head + b'\r\n' + b'b')  # \r ends chunk1, \n starts chunk2

    # when: reading as text in sized chunks (forces the incremental decoder)
    out = []
    async with urio.open(tmpfile) as f:
        while True:
            piece = await f.read(40000)
            if not piece:
                break
            out.append(piece)
    data = ''.join(out)

    # then: the \r\n collapses to exactly one \n (not two)
    assert data == 'a' * (DEFAULT_CHUNK - 1) + '\n' + 'b'


async def test_truncate_discards_read_ahead(backend, tmpfile):
    # given: a file read far enough to fill the read-ahead buffer
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'0123456789' * 100)
    async with urio.open(tmpfile, 'rb+') as f:
        await f.readline()  # over-reads into the internal buffer

        # when: truncating below the buffered region, then reading from start
        await f.truncate(4)
        await f.seek(0)
        data = await f.read()

    # then: no stale bytes from beyond the new EOF leak through
    assert data == b'0123'


async def test_cancel_inflight_reads_keeps_driver_healthy(backend, tmpfile):
    # given: a file to read concurrently
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'payload' * 1000)

    async def read_once():
        async with urio.open(tmpfile, 'rb') as f:
            return await f.read()

    # when: a batch of reads is cancelled mid-flight
    tasks = [asyncio.ensure_future(read_once()) for _ in range(50)]
    await asyncio.sleep(0)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # then: the driver still works for subsequent reads (gate not corrupted)
    results = await asyncio.gather(*(read_once() for _ in range(50)))
    assert all(r == b'payload' * 1000 for r in results)


async def test_write_then_read_text(backend, tmpfile):
    # given: a backend and a target file

    # when: we write text and read it back
    async with urio.open(tmpfile, 'w') as f:
        await f.write('hello world')
    async with urio.open(tmpfile) as f:
        data = await f.read()

    # then: the round trip preserves the content
    assert data == 'hello world'


async def test_write_then_read_binary(backend, tmpfile):
    # given: binary payload with non-utf8 bytes
    payload = bytes(range(256))

    # when: written and read back in binary mode
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(payload)
    async with urio.open(tmpfile, 'rb') as f:
        data = await f.read()

    # then: bytes match exactly
    assert data == payload


@pytest.mark.parametrize(
    'binary',
    [
        pytest.param(True, id='binary'),
        pytest.param(False, id='text'),
    ],
)
async def test_large_roundtrip_both_modes(backend, tmpfile, binary):
    # given: a multi-chunk payload in either binary or text mode
    if binary:
        payload = os.urandom(500 * 1024)
        wmode, rmode = 'wb', 'rb'
    else:
        payload = ''.join(f'line {i}\n' for i in range(40000))
        wmode, rmode = 'w', 'r'

    # when: writing it and reading it all back
    async with urio.open(tmpfile, wmode) as f:
        await f.write(payload)
    async with urio.open(tmpfile, rmode) as f:
        data = await f.read()

    # then: the content round-trips exactly through the read-all path
    assert data == payload


async def test_await_form_without_context_manager(backend, tmpfile):
    # given: open used directly as an awaitable

    # when: awaiting open, writing, and closing manually
    f = await urio.open(tmpfile, 'wb')
    await f.write(b'abc')
    await f.close()

    # then: the file holds the data and reports closed
    assert f.closed is True
    async with urio.open(tmpfile, 'rb') as g:
        assert await g.read() == b'abc'


async def test_read_with_size_spanning_chunks(backend, tmpfile):
    # given: a payload larger than the internal chunk size
    payload = os.urandom(300 * 1024)
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(payload)

    # when: reading the whole file via repeated sized reads
    out = bytearray()
    async with urio.open(tmpfile, 'rb') as f:
        while True:
            chunk = await f.read(7000)
            if not chunk:
                break
            out += chunk

    # then: the reassembled bytes equal the original
    assert bytes(out) == payload


async def test_read_all_large_file(backend, tmpfile):
    # given: a multi-megabyte file
    payload = os.urandom(2 * 1024 * 1024 + 123)
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(payload)

    # when: reading it all at once
    async with urio.open(tmpfile, 'rb') as f:
        data = await f.read()

    # then: the full content comes back
    assert data == payload


async def test_readinto(backend, tmpfile):
    # given: a file with known content
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'0123456789')

    # when: reading into a preallocated buffer
    buf = bytearray(4)
    async with urio.open(tmpfile, 'rb') as f:
        n = await f.readinto(buf)

    # then: the buffer is filled and the count returned
    assert n == 4
    assert bytes(buf) == b'0123'


async def test_seek_and_tell_binary(backend, tmpfile):
    # given: a file with sequential content
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'abcdefghij')

    # when: seeking around and reading
    async with urio.open(tmpfile, 'rb') as f:
        await f.seek(3)
        assert f.tell() == 3
        assert await f.read(2) == b'de'
        assert f.tell() == 5
        assert await f.seek(-1, os.SEEK_END) == 9
        assert await f.read() == b'j'
        await f.seek(2, os.SEEK_CUR)
        # then: SEEK_CUR is relative to the current position
        assert f.tell() == 12


async def test_seek_negative_raises(backend, tmpfile):
    # given: an open file
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'x')

    # when/then: seeking before the start raises
    async with urio.open(tmpfile, 'rb') as f:
        with pytest.raises(OSError):
            await f.seek(-5)


async def test_append_mode(backend, tmpfile):
    # given: an existing file
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'first')

    # when: opening in append mode and writing more
    async with urio.open(tmpfile, 'ab') as f:
        assert f.tell() == 5
        await f.write(b'second')

    # then: content is appended, not truncated
    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b'firstsecond'


async def test_append_writes_at_true_end_after_external_growth(backend, tmpfile):
    # given: a file opened in append mode
    tmpfile.write_bytes(b'AAAA')
    async with urio.open(tmpfile, 'ab') as f:
        # and: the file grows underneath us from another writer
        with open(tmpfile, 'ab') as ext:
            ext.write(b'BBBB')

        # when: we append
        await f.write(b'CC')

    # then: O_APPEND placed our bytes at the real end, not a stale offset
    assert tmpfile.read_bytes() == b'AAAABBBBCC'


async def test_write_returns_full_count(backend, tmpfile):
    # given: a payload
    payload = b'x' * 5000

    # when: writing it
    async with urio.open(tmpfile, 'wb') as f:
        n = await f.write(payload)

    # then: the full byte count is reported
    assert n == 5000


async def test_text_write_returns_char_count(backend, tmpfile):
    # given: text with multibyte characters
    text = 'héllo'

    # when: writing it
    async with urio.open(tmpfile, 'w', encoding='utf-8') as f:
        n = await f.write(text)

    # then: the character count is reported (not the byte count)
    assert n == len(text)


async def test_text_readline_size_cap(backend, tmpfile):
    # given: a file whose first line is long
    async with urio.open(tmpfile, 'w') as f:
        await f.write('a' * 100 + '\n')

    # when: reading a line with a size cap
    async with urio.open(tmpfile) as f:
        chunk = await f.readline(10)

    # then: at most `size` characters are returned
    assert chunk == 'a' * 10


async def test_text_tell_exact_after_full_read(backend, tmpfile):
    # given: a CRLF text file (translation shrinks bytes->chars)
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'a\r\nb\r\nc\r\n')
    size = (await urio.Path(tmpfile).stat()).st_size

    # when: reading everything then asking the position
    async with urio.open(tmpfile) as f:
        data = await f.read()
        pos = f.tell()

    # then: universal newlines were applied and tell is the exact byte size
    assert data == 'a\nb\nc\n'
    assert pos == size


async def test_write_truncates_existing(backend, tmpfile):
    # given: a file with content
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'aaaaaaaaaa')

    # when: reopening with 'w'
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'bb')

    # then: the old content is gone
    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b'bb'


async def test_truncate(backend, tmpfile):
    # given: a 10-byte file
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'0123456789')

    # when: truncating to 4 bytes
    async with urio.open(tmpfile, 'rb+') as f:
        await f.truncate(4)

    # then: only the first 4 bytes remain
    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b'0123'


async def test_exclusive_create(backend, tmpfile):
    # given: a file that does not exist yet

    # when: creating it with 'x'
    async with urio.open(tmpfile, 'xb') as f:
        await f.write(b'new')

    # then: a second exclusive create fails
    with pytest.raises(FileExistsError):
        async with urio.open(tmpfile, 'xb') as f:
            await f.write(b'again')


async def test_open_missing_file_raises(backend, tmpfile):
    # given: a path with no file

    # when/then: opening for read raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        await urio.open(tmpfile, 'rb')


async def test_readline_and_iteration(backend, tmpfile):
    # given: a multi-line text file
    async with urio.open(tmpfile, 'w') as f:
        await f.write('one\ntwo\nthree')

    # when: reading line by line
    async with urio.open(tmpfile) as f:
        first = await f.readline()
        rest = [line async for line in f]

    # then: lines keep their terminators; last line has none
    assert first == 'one\n'
    assert rest == ['two\n', 'three']


async def test_readlines(backend, tmpfile):
    # given: a file with three lines
    async with urio.open(tmpfile, 'w') as f:
        await f.write('a\nb\nc\n')

    # when: reading all lines
    async with urio.open(tmpfile) as f:
        lines = await f.readlines()

    # then: every line is returned with its newline
    assert lines == ['a\n', 'b\n', 'c\n']


async def test_binary_readline(backend, tmpfile):
    # given: binary content with newlines
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'x\ny\n')

    # when: reading lines in binary mode
    async with urio.open(tmpfile, 'rb') as f:
        lines = await f.readlines()

    # then: bytes lines come back with terminators
    assert lines == [b'x\n', b'y\n']


async def test_writelines(backend, tmpfile):
    # given: a list of lines

    # when: writing them with writelines
    async with urio.open(tmpfile, 'w') as f:
        await f.writelines(['a\n', 'b\n'])

    # then: they are concatenated in the file
    async with urio.open(tmpfile) as f:
        assert await f.read() == 'a\nb\n'


async def test_writelines_single_submission(backend, tmpfile, monkeypatch):
    # given: a binary file with both backend write paths wrapped in a counter.
    # Only top-level calls count: on platforms without a real writev the
    # backend's writev delegates to write internally, which is still a single
    # submission from the file layer's point of view.
    async with urio.open(tmpfile, 'wb') as f:
        calls = []
        depth = 0
        orig_write = f._backend.write
        orig_writev = f._backend.writev

        def counting(orig):
            async def wrapper(*a, **k):
                nonlocal depth
                if depth == 0:
                    calls.append(1)
                depth += 1
                try:
                    return await orig(*a, **k)
                finally:
                    depth -= 1

            return wrapper

        monkeypatch.setattr(f._backend, 'write', counting(orig_write))
        monkeypatch.setattr(f._backend, 'writev', counting(orig_writev))

        # when: writing many lines in one writelines call
        await f.writelines([b'a\n', b'b\n', b'c\n', b'd\n'])

        # then: the whole batch reaches the kernel as one vectored submission
        assert len(calls) == 1

    # and: the bytes land on disk concatenated
    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b'a\nb\nc\nd\n'


async def test_writelines_many_lines_beyond_iov_max(backend, tmpfile):
    # given: more lines than a single writev can carry (IOV_MAX = 1024)
    lines = [f'line-{i}\n'.encode() for i in range(3000)]

    # when: writing them in one writelines call
    async with urio.open(tmpfile, 'wb') as f:
        await f.writelines(lines)

    # then: the file holds every line in order
    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b''.join(lines)


async def test_writelines_append_mode(backend, tmpfile):
    # given: a file with existing content
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'head:')

    # when: appending several lines via writelines
    async with urio.open(tmpfile, 'ab') as f:
        await f.writelines([b'a\n', b'b\n', b'c\n'])

    # then: the lines land after the existing content
    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b'head:a\nb\nc\n'


async def test_cancelled_reads_release_inflight_slots(backend, tmp_path):
    # given: a large file and many reads started concurrently
    p = tmp_path / 'big'
    async with urio.open(p, 'wb') as f:
        await f.write(b'x' * (4 * 1024 * 1024))

    async def read_it():
        async with urio.open(p, 'rb') as f:
            return await f.read()

    tasks = [asyncio.ensure_future(read_it()) for _ in range(20)]
    await asyncio.sleep(0)

    # when: cancelling them mid-flight
    for t in tasks:
        t.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, (asyncio.CancelledError, bytes)) for r in results)

    # then: the ring drains back to zero in-flight submissions and still works
    async with urio.open(p, 'rb') as f:
        assert len(await f.read()) == 4 * 1024 * 1024
    if backend == 'uring':
        from urio.driver import get_driver

        for _ in range(50):
            if get_driver().inflight == 0:
                break
            await asyncio.sleep(0.01)
        assert get_driver().inflight == 0


async def test_writelines_empty_is_noop(backend, tmpfile, monkeypatch):
    # given: a file with the backend write wrapped in a call counter
    async with urio.open(tmpfile, 'wb') as f:
        calls = []
        orig = f._backend.write

        async def counting(*a, **k):
            calls.append(1)
            return await orig(*a, **k)

        monkeypatch.setattr(f._backend, 'write', counting)

        # when: writelines is given no lines
        await f.writelines([])

        # then: nothing is submitted to the ring
        assert len(calls) == 0


async def test_text_sized_read_single_round_trip(backend, tmpfile, monkeypatch):
    # given: a text file several DEFAULT_CHUNK-sized reads long
    payload = 'x' * (300 * 1024)
    async with urio.open(tmpfile, 'w') as f:
        await f.write(payload)

    # when: reading a size that spans several chunks, counting backend reads
    async with urio.open(tmpfile) as f:
        calls = []
        orig = f._raw._backend.read

        async def counting(*a, **k):
            calls.append(1)
            return await orig(*a, **k)

        monkeypatch.setattr(f._raw._backend, 'read', counting)
        data = await f.read(300 * 1024)

    # then: the data is correct and the request did not loop per 128 KB
    # (the old DEFAULT_CHUNK loop would have issued >= 3 reads)
    assert data == payload
    assert len(calls) <= 2


@pytest.mark.parametrize(
    'newline,written,on_disk',
    [
        pytest.param('', 'a\nb', b'a\nb', id='no-translation'),
        pytest.param('\r\n', 'a\nb', b'a\r\nb', id='crlf-translation'),
        pytest.param('\r', 'a\nb', b'a\rb', id='cr-translation'),
    ],
)
async def test_text_write_newline_translation(
    backend, tmpfile, newline, written, on_disk
):
    # given: a text file opened with a specific newline setting

    # when: writing text containing '\n'
    async with urio.open(tmpfile, 'w', newline=newline) as f:
        await f.write(written)

    # then: the bytes on disk reflect the translation
    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == on_disk


async def test_universal_newline_read(backend, tmpfile):
    # given: a file with mixed CRLF / CR / LF endings
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'a\r\nb\rc\nd')

    # when: reading in universal (default) text mode
    async with urio.open(tmpfile) as f:
        data = await f.read()

    # then: all endings are normalised to '\n'
    assert data == 'a\nb\nc\nd'


async def test_universal_newline_readline(backend, tmpfile):
    # given: CRLF-separated lines
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'one\r\ntwo\r\n')

    # when: iterating lines in universal text mode
    async with urio.open(tmpfile) as f:
        lines = [line async for line in f]

    # then: terminators are normalised
    assert lines == ['one\n', 'two\n']


async def test_text_encoding(backend, tmpfile):
    # given: text with non-ascii characters and an explicit encoding
    text = 'héllo — wörld'

    # when: writing and reading with utf-8
    async with urio.open(tmpfile, 'w', encoding='utf-8') as f:
        await f.write(text)
    async with urio.open(tmpfile, encoding='utf-8') as f:
        out = await f.read()

    # then: the text round-trips
    assert out == text


async def test_text_multibyte_split_across_chunks(backend, tmpfile):
    # given: many multibyte chars so utf-8 sequences straddle chunk reads
    text = 'あ' * 200000
    async with urio.open(tmpfile, 'w', encoding='utf-8') as f:
        await f.write(text)

    # when: reading it back incrementally
    async with urio.open(tmpfile, encoding='utf-8') as f:
        out = await f.read()

    # then: incremental decoding handles the boundaries correctly
    assert out == text


async def test_read_on_closed_file_raises(backend, tmpfile):
    # given: a closed file
    f = await urio.open(tmpfile, 'wb')
    await f.close()

    # when/then: operating on it raises ValueError
    with pytest.raises(ValueError):
        await f.write(b'x')


async def test_write_to_readonly_raises(backend, tmpfile):
    # given: a file opened read-only
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'x')

    # when/then: writing raises UnsupportedOperation
    async with urio.open(tmpfile, 'rb') as f:
        with pytest.raises(io.UnsupportedOperation):
            await f.write(b'y')


async def test_read_from_writeonly_raises(backend, tmpfile):
    # given: a file opened write-only

    # when/then: reading raises UnsupportedOperation
    async with urio.open(tmpfile, 'wb') as f:
        with pytest.raises(io.UnsupportedOperation):
            await f.read()


@pytest.mark.parametrize(
    'mode',
    [
        pytest.param('rw', id='read-and-write'),
        pytest.param('', id='empty'),
        pytest.param('rb t', id='garbage'),
        pytest.param('bt', id='binary-and-text'),
        pytest.param('z', id='unknown-char'),
    ],
)
async def test_invalid_mode_raises(backend, tmpfile, mode):
    # given: an invalid mode string

    # when/then: opening raises ValueError
    with pytest.raises(ValueError):
        await urio.open(tmpfile, mode)


async def test_name_and_mode_attributes(backend, tmpfile):
    # given: an open file

    # when: inspecting metadata attributes
    async with urio.open(tmpfile, 'wb') as f:
        # then: name, mode, fileno and capability flags are exposed
        assert f.name == str(tmpfile)
        assert f.mode == 'wb'
        assert isinstance(f.fileno(), int)
        assert f.writable() is True
        assert f.readable() is False
        assert f.seekable() is True


async def test_fsync_and_fdatasync(backend, tmpfile):
    # given: a file with buffered writes

    # when: explicitly syncing to disk
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'durable')
        await f.flush()
        await f.fsync()
        await f.fdatasync()

    # then: data is present
    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b'durable'


async def test_text_seek_zero_resets(backend, tmpfile):
    # given: a text file partially read
    async with urio.open(tmpfile, 'w') as f:
        await f.write('abcdef')

    async with urio.open(tmpfile) as f:
        assert await f.read(3) == 'abc'

        # when: seeking back to the start
        await f.seek(0)

        # then: reading again returns from the beginning
        assert await f.read(3) == 'abc'


async def test_text_seek_arbitrary_offset(backend, tmpfile):
    # given: a text file
    async with urio.open(tmpfile, 'w') as f:
        await f.write('abcdef')

    async with urio.open(tmpfile) as f:
        # then: streams report as seekable
        assert f.seekable() is True

        # when: seeking to an absolute byte offset
        assert await f.seek(3) == 3
        # then: reading continues from there
        assert await f.read() == 'def'

        # when: seeking relative to the end
        assert await f.seek(-2, os.SEEK_END) == 4
        assert await f.read() == 'ef'


async def test_text_seek_tell_roundtrip(backend, tmpfile):
    # given: a text file read up to a boundary where tell() is exact
    async with urio.open(tmpfile, 'w') as f:
        await f.write('one two three four')

    async with urio.open(tmpfile) as f:
        assert await f.read(8) == 'one two '
        cookie = f.tell()  # exact here: ASCII, so byte offset == char offset

        # when: reading on, then seeking back to the saved cookie
        assert await f.read(5) == 'three'
        assert await f.seek(cookie) == cookie

        # then: the same text is read again
        assert await f.read(5) == 'three'
