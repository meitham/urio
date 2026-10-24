#!/usr/bin/env python3
import os
import pathlib
import sys

import pytest

import urio

skip_on_windows = pytest.mark.skipif(
    sys.platform == 'win32', reason='POSIX-only filesystem semantics'
)


async def test_resolve_strict_missing_path(backend, tmp_path):
    missing = tmp_path / 'missing'
    with pytest.raises(FileNotFoundError):
        await urio.Path(missing).resolve(strict=True)
    assert await urio.Path(missing).resolve(strict=False) == urio.Path(missing)


def test_pure_path_properties():
    p = urio.Path('/tmp/foo/bar.txt')
    ref = pathlib.Path('/tmp/foo/bar.txt')

    assert p.name == 'bar.txt'
    assert p.stem == 'bar'
    assert p.suffix == '.txt'
    assert p.parent == urio.Path('/tmp/foo')
    assert p.parts == ref.parts
    assert p.is_absolute() == ref.is_absolute()


def test_truediv_joins():

    p = urio.Path('/tmp') / 'a' / 'b'

    assert str(p) == str(pathlib.Path('/tmp') / 'a' / 'b')


def test_with_suffix_and_name():
    p = urio.Path('doc.txt')

    assert p.with_suffix('.md') == urio.Path('doc.md')
    assert p.with_name('other.txt') == urio.Path('other.txt')
    assert p.with_stem('renamed') == urio.Path('renamed.txt')


def test_fspath_and_str_and_repr():
    p = urio.Path('/tmp/x')

    native = str(pathlib.Path('/tmp/x'))
    assert os.fspath(p) == native
    assert str(p) == native
    assert repr(p) == f'AsyncPath({native!r})'


def test_equality_with_pathlib():

    assert urio.Path('/a/b') == pathlib.Path('/a/b')
    assert hash(urio.Path('/a/b')) == hash(pathlib.Path('/a/b'))


async def test_write_and_read_text(backend, tmp_path):
    p = urio.Path(tmp_path) / 'note.txt'

    await p.write_text('content')
    out = await p.read_text()

    assert out == 'content'


async def test_write_and_read_bytes(backend, tmp_path):
    p = urio.Path(tmp_path) / 'blob.bin'

    await p.write_bytes(b'\x00\xff\x10')
    out = await p.read_bytes()

    assert out == b'\x00\xff\x10'


async def test_stat_size(backend, tmp_path):
    p = urio.Path(tmp_path) / 'sized'
    await p.write_bytes(b'12345')

    st = await p.stat()

    assert st.st_size == 5
    assert st.st_mtime_ns > 0


async def test_exists_and_type_predicates(backend, tmp_path):
    base = urio.Path(tmp_path)
    f = base / 'file'
    await f.write_bytes(b'x')
    d = base / 'dir'
    await d.mkdir()

    assert await f.exists() is True
    assert await f.is_file() is True
    assert await f.is_dir() is False
    assert await d.is_dir() is True
    assert await (base / 'missing').exists() is False
    assert await (base / 'missing').is_file() is False


async def test_mkdir_parents_and_exist_ok(backend, tmp_path):
    deep = urio.Path(tmp_path) / 'a' / 'b' / 'c'

    with pytest.raises(FileNotFoundError):
        await deep.mkdir()

    await deep.mkdir(parents=True)
    assert await deep.is_dir()
    await deep.mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileExistsError):
        await deep.mkdir()


async def test_unlink_and_missing_ok(backend, tmp_path):
    p = urio.Path(tmp_path) / 'gone'
    await p.write_bytes(b'x')

    await p.unlink()

    assert not await p.exists()
    with pytest.raises(FileNotFoundError):
        await p.unlink()
    await p.unlink(missing_ok=True)


async def test_rmdir(backend, tmp_path):
    d = urio.Path(tmp_path) / 'empty'
    await d.mkdir()

    await d.rmdir()

    assert not await d.exists()


async def test_rename(backend, tmp_path):
    base = urio.Path(tmp_path)
    src = base / 'src'
    dst = base / 'dst'
    await src.write_bytes(b'data')

    returned = await src.rename(dst)

    assert not await src.exists()
    assert await dst.read_bytes() == b'data'
    assert returned == dst


async def test_replace_overwrites(backend, tmp_path):
    base = urio.Path(tmp_path)
    src = base / 'src'
    dst = base / 'dst'
    await src.write_bytes(b'new')
    await dst.write_bytes(b'old')

    await src.replace(dst)

    assert await dst.read_bytes() == b'new'


@skip_on_windows
async def test_symlink_to(backend, tmp_path):
    base = urio.Path(tmp_path)
    target = base / 'target'
    await target.write_bytes(b'real')
    link = base / 'link'

    await link.symlink_to(target)

    assert await link.is_symlink() is True
    assert await link.read_bytes() == b'real'
    assert await link.readlink() == target


async def test_hardlink_to(backend, tmp_path):
    base = urio.Path(tmp_path)
    target = base / 'target'
    await target.write_bytes(b'shared')
    link = base / 'hard'

    await link.hardlink_to(target)

    assert await link.read_bytes() == b'shared'
    assert await link.samefile(target) is True


async def test_touch(backend, tmp_path):
    p = urio.Path(tmp_path) / 'touched'

    await p.touch()

    assert await p.is_file()
    assert (await p.stat()).st_size == 0


async def test_touch_exclusive(backend, tmp_path):
    p = urio.Path(tmp_path) / 'exists'
    await p.touch()

    with pytest.raises(FileExistsError):
        await p.touch(exist_ok=False)


@skip_on_windows
async def test_chmod(backend, tmp_path):
    p = urio.Path(tmp_path) / 'perm'
    await p.write_bytes(b'x')

    await p.chmod(0o600)

    st = await p.stat()
    assert (st.st_mode & 0o777) == 0o600


async def test_iterdir(backend, tmp_path):
    base = urio.Path(tmp_path) / 'listing'
    await base.mkdir()
    for n in ('a', 'b', 'c'):
        await (base / n).write_bytes(b'')

    names = sorted([entry.name async for entry in base.iterdir()])

    assert names == ['a', 'b', 'c']


async def test_glob(backend, tmp_path):
    base = urio.Path(tmp_path) / 'g'
    await base.mkdir()
    await (base / 'one.txt').write_bytes(b'')
    await (base / 'two.txt').write_bytes(b'')
    await (base / 'three.dat').write_bytes(b'')

    matches = sorted([p.name async for p in base.glob('*.txt')])

    assert matches == ['one.txt', 'two.txt']


async def test_rglob(backend, tmp_path):
    base = urio.Path(tmp_path) / 'r'
    await (base / 'sub').mkdir(parents=True)
    await (base / 'top.txt').write_bytes(b'')
    await (base / 'sub' / 'deep.txt').write_bytes(b'')

    matches = sorted([p.name async for p in base.rglob('*.txt')])

    assert matches == ['deep.txt', 'top.txt']


async def test_open_via_path(backend, tmp_path):
    p = urio.Path(tmp_path) / 'viapath'

    async with p.open('w') as f:
        await f.write('through path')

    assert await p.read_text() == 'through path'


@skip_on_windows
async def test_lstat_vs_stat_on_symlink(backend, tmp_path):
    base = urio.Path(tmp_path)
    target = base / 't'
    await target.write_bytes(b'12345')
    link = base / 'l'
    await link.symlink_to(target)

    st = await link.stat()
    lst = await link.lstat()

    assert st.st_size == 5
    assert lst.st_ino != st.st_ino


async def test_read_bytes_large_file(backend, tmp_path):
    p = urio.Path(tmp_path) / 'large.bin'
    payload = os.urandom(3 * 1024 * 1024)
    await p.write_bytes(payload)

    out = await p.read_bytes()

    assert out == payload


async def test_read_bytes_missing_file(backend, tmp_path):
    p = urio.Path(tmp_path) / 'nope.bin'

    with pytest.raises(FileNotFoundError):
        await p.read_bytes()


async def test_write_bytes_overwrites_longer_content(backend, tmp_path):
    p = urio.Path(tmp_path) / 'trunc.bin'
    await p.write_bytes(b'long old content here')

    await p.write_bytes(b'new')

    assert await p.read_bytes() == b'new'


async def test_chain_slots_recycled(backend, tmp_path):
    if backend != 'uring':
        return
    import asyncio

    from urio.driver import get_driver

    driver = get_driver()
    before = len(driver._slots) + len(driver._recycle_on_done)

    base = urio.Path(tmp_path)

    async def roundtrip(i):
        p = base / f'c{i}'
        await p.write_bytes(f'payload-{i}'.encode())
        return await p.read_bytes()

    results = await asyncio.gather(*(roundtrip(i) for i in range(64)))
    assert results == [f'payload-{i}'.encode() for i in range(64)]

    for _ in range(100):
        if len(driver._slots) >= before and not driver._recycle_on_done:
            break
        await asyncio.sleep(0.01)
    assert len(driver._slots) >= before
    assert not driver._recycle_on_done


async def test_pure_path_additions(backend):
    p = urio.Path('/tmp/claude/a/b.txt')

    assert p.is_relative_to('/tmp/claude')
    assert not p.is_relative_to('/var')
    assert urio.Path.cwd() == pathlib.Path.cwd()
    assert urio.Path.home() == pathlib.Path.home()
    assert urio.Path('~').expanduser() == pathlib.Path('~').expanduser()


async def test_walk(backend, tmp_path):
    (tmp_path / 'sub').mkdir()
    (tmp_path / 'sub' / 'f1').write_bytes(b'1')
    (tmp_path / 'top').write_bytes(b't')

    seen = {}
    async for dirpath, dirnames, filenames in urio.Path(tmp_path).walk():
        seen[str(dirpath)] = (sorted(dirnames), sorted(filenames))

    assert seen[str(tmp_path)] == (['sub'], ['top'])
    assert seen[str(tmp_path / 'sub')] == ([], ['f1'])
