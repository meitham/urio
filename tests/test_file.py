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
    from urio.file import DEFAULT_CHUNK

    head = b'a' * (DEFAULT_CHUNK - 1)
    tmpfile.write_bytes(head + b'\r\n' + b'b')

    out = []
    async with urio.open(tmpfile) as f:
        while True:
            piece = await f.read(40000)
            if not piece:
                break
            out.append(piece)
    data = ''.join(out)

    assert data == 'a' * (DEFAULT_CHUNK - 1) + '\n' + 'b'


async def test_truncate_discards_read_ahead(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'0123456789' * 100)
    async with urio.open(tmpfile, 'rb+') as f:
        await f.readline()

        await f.truncate(4)
        await f.seek(0)
        data = await f.read()

    assert data == b'0123'


async def test_cancel_inflight_reads_keeps_driver_healthy(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'payload' * 1000)

    async def read_once():
        async with urio.open(tmpfile, 'rb') as f:
            return await f.read()

    tasks = [asyncio.ensure_future(read_once()) for _ in range(50)]
    await asyncio.sleep(0)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    results = await asyncio.gather(*(read_once() for _ in range(50)))
    assert all(r == b'payload' * 1000 for r in results)


async def test_write_then_read_text(backend, tmpfile):

    async with urio.open(tmpfile, 'w') as f:
        await f.write('hello world')
    async with urio.open(tmpfile) as f:
        data = await f.read()

    assert data == 'hello world'


async def test_write_then_read_binary(backend, tmpfile):
    payload = bytes(range(256))

    async with urio.open(tmpfile, 'wb') as f:
        await f.write(payload)
    async with urio.open(tmpfile, 'rb') as f:
        data = await f.read()

    assert data == payload


@pytest.mark.parametrize(
    'binary',
    [
        pytest.param(True, id='binary'),
        pytest.param(False, id='text'),
    ],
)
async def test_large_roundtrip_both_modes(backend, tmpfile, binary):
    if binary:
        payload = os.urandom(500 * 1024)
        wmode, rmode = 'wb', 'rb'
    else:
        payload = ''.join(f'line {i}\n' for i in range(40000))
        wmode, rmode = 'w', 'r'

    async with urio.open(tmpfile, wmode) as f:
        await f.write(payload)
    async with urio.open(tmpfile, rmode) as f:
        data = await f.read()

    assert data == payload


async def test_await_form_without_context_manager(backend, tmpfile):

    f = await urio.open(tmpfile, 'wb')
    await f.write(b'abc')
    await f.close()

    assert f.closed is True
    async with urio.open(tmpfile, 'rb') as g:
        assert await g.read() == b'abc'


async def test_read_with_size_spanning_chunks(backend, tmpfile):
    payload = os.urandom(300 * 1024)
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(payload)

    out = bytearray()
    async with urio.open(tmpfile, 'rb') as f:
        while True:
            chunk = await f.read(7000)
            if not chunk:
                break
            out += chunk

    assert bytes(out) == payload


async def test_read_all_large_file(backend, tmpfile):
    payload = os.urandom(2 * 1024 * 1024 + 123)
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(payload)

    async with urio.open(tmpfile, 'rb') as f:
        data = await f.read()

    assert data == payload


async def test_readinto(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'0123456789')

    buf = bytearray(4)
    async with urio.open(tmpfile, 'rb') as f:
        n = await f.readinto(buf)

    assert n == 4
    assert bytes(buf) == b'0123'


async def test_seek_and_tell_binary(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'abcdefghij')

    async with urio.open(tmpfile, 'rb') as f:
        await f.seek(3)
        assert f.tell() == 3
        assert await f.read(2) == b'de'
        assert f.tell() == 5
        assert await f.seek(-1, os.SEEK_END) == 9
        assert await f.read() == b'j'
        await f.seek(2, os.SEEK_CUR)
        assert f.tell() == 12


async def test_seek_negative_raises(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'x')

    async with urio.open(tmpfile, 'rb') as f:
        with pytest.raises(OSError):
            await f.seek(-5)


async def test_append_mode(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'first')

    async with urio.open(tmpfile, 'ab') as f:
        assert f.tell() == 5
        await f.write(b'second')

    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b'firstsecond'


async def test_append_writes_at_true_end_after_external_growth(backend, tmpfile):
    tmpfile.write_bytes(b'AAAA')
    async with urio.open(tmpfile, 'ab') as f:
        with open(tmpfile, 'ab') as ext:
            ext.write(b'BBBB')

        await f.write(b'CC')

    assert tmpfile.read_bytes() == b'AAAABBBBCC'


async def test_write_returns_full_count(backend, tmpfile):
    payload = b'x' * 5000

    async with urio.open(tmpfile, 'wb') as f:
        n = await f.write(payload)

    assert n == 5000


async def test_text_write_returns_char_count(backend, tmpfile):
    text = 'héllo'

    async with urio.open(tmpfile, 'w', encoding='utf-8') as f:
        n = await f.write(text)

    assert n == len(text)


async def test_text_readline_size_cap(backend, tmpfile):
    async with urio.open(tmpfile, 'w') as f:
        await f.write('a' * 100 + '\n')

    async with urio.open(tmpfile) as f:
        chunk = await f.readline(10)

    assert chunk == 'a' * 10


async def test_text_tell_exact_after_full_read(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'a\r\nb\r\nc\r\n')
    size = (await urio.Path(tmpfile).stat()).st_size

    async with urio.open(tmpfile) as f:
        data = await f.read()
        pos = f.tell()

    assert data == 'a\nb\nc\n'
    assert pos == size


async def test_write_truncates_existing(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'aaaaaaaaaa')

    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'bb')

    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b'bb'


async def test_truncate(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'0123456789')

    async with urio.open(tmpfile, 'rb+') as f:
        await f.truncate(4)

    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b'0123'


async def test_exclusive_create(backend, tmpfile):

    async with urio.open(tmpfile, 'xb') as f:
        await f.write(b'new')

    with pytest.raises(FileExistsError):
        async with urio.open(tmpfile, 'xb') as f:
            await f.write(b'again')


async def test_open_missing_file_raises(backend, tmpfile):

    with pytest.raises(FileNotFoundError):
        await urio.open(tmpfile, 'rb')


async def test_readline_and_iteration(backend, tmpfile):
    async with urio.open(tmpfile, 'w') as f:
        await f.write('one\ntwo\nthree')

    async with urio.open(tmpfile) as f:
        first = await f.readline()
        rest = [line async for line in f]

    assert first == 'one\n'
    assert rest == ['two\n', 'three']


async def test_readlines(backend, tmpfile):
    async with urio.open(tmpfile, 'w') as f:
        await f.write('a\nb\nc\n')

    async with urio.open(tmpfile) as f:
        lines = await f.readlines()

    assert lines == ['a\n', 'b\n', 'c\n']


async def test_binary_readline(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'x\ny\n')

    async with urio.open(tmpfile, 'rb') as f:
        lines = await f.readlines()

    assert lines == [b'x\n', b'y\n']


async def test_writelines(backend, tmpfile):

    async with urio.open(tmpfile, 'w') as f:
        await f.writelines(['a\n', 'b\n'])

    async with urio.open(tmpfile) as f:
        assert await f.read() == 'a\nb\n'


async def test_writelines_single_submission(backend, tmpfile, monkeypatch):
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

        await f.writelines([b'a\n', b'b\n', b'c\n', b'd\n'])

        assert len(calls) == 1

    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b'a\nb\nc\nd\n'


async def test_writelines_many_lines_beyond_iov_max(backend, tmpfile):
    lines = [f'line-{i}\n'.encode() for i in range(3000)]

    async with urio.open(tmpfile, 'wb') as f:
        await f.writelines(lines)

    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b''.join(lines)


async def test_writelines_append_mode(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'head:')

    async with urio.open(tmpfile, 'ab') as f:
        await f.writelines([b'a\n', b'b\n', b'c\n'])

    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b'head:a\nb\nc\n'


async def test_cancelled_reads_release_inflight_slots(backend, tmp_path):
    p = tmp_path / 'big'
    async with urio.open(p, 'wb') as f:
        await f.write(b'x' * (4 * 1024 * 1024))

    async def read_it():
        async with urio.open(p, 'rb') as f:
            return await f.read()

    tasks = [asyncio.ensure_future(read_it()) for _ in range(20)]
    await asyncio.sleep(0)

    for t in tasks:
        t.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, (asyncio.CancelledError, bytes)) for r in results)

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
    async with urio.open(tmpfile, 'wb') as f:
        calls = []
        orig = f._backend.write

        async def counting(*a, **k):
            calls.append(1)
            return await orig(*a, **k)

        monkeypatch.setattr(f._backend, 'write', counting)

        await f.writelines([])

        assert len(calls) == 0


async def test_text_sized_read_single_round_trip(backend, tmpfile, monkeypatch):
    payload = 'x' * (300 * 1024)
    async with urio.open(tmpfile, 'w') as f:
        await f.write(payload)

    async with urio.open(tmpfile) as f:
        calls = []
        orig = f._raw._backend.read

        async def counting(*a, **k):
            calls.append(1)
            return await orig(*a, **k)

        monkeypatch.setattr(f._raw._backend, 'read', counting)
        data = await f.read(300 * 1024)

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

    async with urio.open(tmpfile, 'w', newline=newline) as f:
        await f.write(written)

    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == on_disk


async def test_universal_newline_read(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'a\r\nb\rc\nd')

    async with urio.open(tmpfile) as f:
        data = await f.read()

    assert data == 'a\nb\nc\nd'


async def test_universal_newline_readline(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'one\r\ntwo\r\n')

    async with urio.open(tmpfile) as f:
        lines = [line async for line in f]

    assert lines == ['one\n', 'two\n']


async def test_text_encoding(backend, tmpfile):
    text = 'héllo — wörld'

    async with urio.open(tmpfile, 'w', encoding='utf-8') as f:
        await f.write(text)
    async with urio.open(tmpfile, encoding='utf-8') as f:
        out = await f.read()

    assert out == text


async def test_text_multibyte_split_across_chunks(backend, tmpfile):
    text = 'あ' * 200000
    async with urio.open(tmpfile, 'w', encoding='utf-8') as f:
        await f.write(text)

    async with urio.open(tmpfile, encoding='utf-8') as f:
        out = await f.read()

    assert out == text


async def test_read_on_closed_file_raises(backend, tmpfile):
    f = await urio.open(tmpfile, 'wb')
    await f.close()

    with pytest.raises(ValueError):
        await f.write(b'x')


async def test_write_to_readonly_raises(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'x')

    async with urio.open(tmpfile, 'rb') as f:
        with pytest.raises(io.UnsupportedOperation):
            await f.write(b'y')


async def test_read_from_writeonly_raises(backend, tmpfile):

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

    with pytest.raises(ValueError):
        await urio.open(tmpfile, mode)


async def test_name_and_mode_attributes(backend, tmpfile):

    async with urio.open(tmpfile, 'wb') as f:
        assert f.name == str(tmpfile)
        assert f.mode == 'wb'
        assert isinstance(f.fileno(), int)
        assert f.writable() is True
        assert f.readable() is False
        assert f.seekable() is True


async def test_fsync_and_fdatasync(backend, tmpfile):

    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'durable')
        await f.flush()
        await f.fsync()
        await f.fdatasync()

    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b'durable'


async def test_text_seek_zero_resets(backend, tmpfile):
    async with urio.open(tmpfile, 'w') as f:
        await f.write('abcdef')

    async with urio.open(tmpfile) as f:
        assert await f.read(3) == 'abc'

        await f.seek(0)

        assert await f.read(3) == 'abc'


async def test_text_seek_arbitrary_offset(backend, tmpfile):
    async with urio.open(tmpfile, 'w') as f:
        await f.write('abcdef')

    async with urio.open(tmpfile) as f:
        assert f.seekable() is True

        assert await f.seek(3) == 3
        assert await f.read() == 'def'

        assert await f.seek(-2, os.SEEK_END) == 4
        assert await f.read() == 'ef'


async def test_text_seek_tell_roundtrip(backend, tmpfile):
    async with urio.open(tmpfile, 'w') as f:
        await f.write('one two three four')

    async with urio.open(tmpfile) as f:
        assert await f.read(8) == 'one two '
        cookie = f.tell()

        assert await f.read(5) == 'three'
        assert await f.seek(cookie) == cookie

        assert await f.read(5) == 'three'
