#!/usr/bin/env python3
import os
import pathlib
import sys

import pytest

import urio

# Symlink creation needs a privilege Windows withholds by default, and chmod on
# Windows only toggles the read-only bit (not POSIX mode bits) — so these tests
# assert POSIX semantics that do not hold there.
skip_on_windows = pytest.mark.skipif(
    sys.platform == 'win32', reason='POSIX-only filesystem semantics'
)

# -- pure path behaviour (no I/O, no backend needed) ----------------------


def test_pure_path_properties():
    # given: an AsyncPath and the equivalent pathlib.Path (platform-native)
    p = urio.Path('/tmp/foo/bar.txt')
    ref = pathlib.Path('/tmp/foo/bar.txt')

    # when/then: pure properties delegate to pathlib (compare to it so the
    # assertions hold on both POSIX and Windows separators)
    assert p.name == 'bar.txt'
    assert p.stem == 'bar'
    assert p.suffix == '.txt'
    assert p.parent == urio.Path('/tmp/foo')
    assert p.parts == ref.parts
    assert p.is_absolute() == ref.is_absolute()


def test_truediv_joins():
    # given: a base path

    # when: using the / operator
    p = urio.Path('/tmp') / 'a' / 'b'

    # then: segments are joined (native separator, so compare to pathlib)
    assert str(p) == str(pathlib.Path('/tmp') / 'a' / 'b')


def test_with_suffix_and_name():
    # given: a path
    p = urio.Path('doc.txt')

    # when/then: pure transforms return new AsyncPaths
    assert p.with_suffix('.md') == urio.Path('doc.md')
    assert p.with_name('other.txt') == urio.Path('other.txt')
    assert p.with_stem('renamed') == urio.Path('renamed.txt')


def test_fspath_and_str_and_repr():
    # given: a path
    p = urio.Path('/tmp/x')

    # when/then: it interoperates with os.fspath and str/repr (native separator)
    native = str(pathlib.Path('/tmp/x'))
    assert os.fspath(p) == native
    assert str(p) == native
    assert repr(p) == f'AsyncPath({native!r})'


def test_equality_with_pathlib():
    # given: an AsyncPath and an equivalent pathlib.Path

    # when/then: they compare equal and hash together
    assert urio.Path('/a/b') == pathlib.Path('/a/b')
    assert hash(urio.Path('/a/b')) == hash(pathlib.Path('/a/b'))


# -- filesystem behaviour (runs against both backends) --------------------


async def test_write_and_read_text(backend, tmp_path):
    # given: a path under a temp dir
    p = urio.Path(tmp_path) / 'note.txt'

    # when: writing and reading text
    await p.write_text('content')
    out = await p.read_text()

    # then: it round-trips
    assert out == 'content'


async def test_write_and_read_bytes(backend, tmp_path):
    # given: a path
    p = urio.Path(tmp_path) / 'blob.bin'

    # when: writing and reading bytes
    await p.write_bytes(b'\x00\xff\x10')
    out = await p.read_bytes()

    # then: bytes match
    assert out == b'\x00\xff\x10'


async def test_stat_size(backend, tmp_path):
    # given: a file of known size
    p = urio.Path(tmp_path) / 'sized'
    await p.write_bytes(b'12345')

    # when: statting it
    st = await p.stat()

    # then: st_size and st_mtime_ns are populated
    assert st.st_size == 5
    assert st.st_mtime_ns > 0


async def test_exists_and_type_predicates(backend, tmp_path):
    # given: a file and a directory
    base = urio.Path(tmp_path)
    f = base / 'file'
    await f.write_bytes(b'x')
    d = base / 'dir'
    await d.mkdir()

    # when/then: predicates report correctly
    assert await f.exists() is True
    assert await f.is_file() is True
    assert await f.is_dir() is False
    assert await d.is_dir() is True
    assert await (base / 'missing').exists() is False
    assert await (base / 'missing').is_file() is False


async def test_mkdir_parents_and_exist_ok(backend, tmp_path):
    # given: a deep path that does not exist
    deep = urio.Path(tmp_path) / 'a' / 'b' / 'c'

    # when: creating without parents
    with pytest.raises(FileNotFoundError):
        await deep.mkdir()

    # then: parents=True creates the chain, exist_ok tolerates re-create
    await deep.mkdir(parents=True)
    assert await deep.is_dir()
    await deep.mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileExistsError):
        await deep.mkdir()


async def test_unlink_and_missing_ok(backend, tmp_path):
    # given: an existing file
    p = urio.Path(tmp_path) / 'gone'
    await p.write_bytes(b'x')

    # when: unlinking it
    await p.unlink()

    # then: it is gone and missing_ok controls re-unlink
    assert not await p.exists()
    with pytest.raises(FileNotFoundError):
        await p.unlink()
    await p.unlink(missing_ok=True)


async def test_rmdir(backend, tmp_path):
    # given: an empty directory
    d = urio.Path(tmp_path) / 'empty'
    await d.mkdir()

    # when: removing it
    await d.rmdir()

    # then: it no longer exists
    assert not await d.exists()


async def test_rename(backend, tmp_path):
    # given: a source file
    base = urio.Path(tmp_path)
    src = base / 'src'
    dst = base / 'dst'
    await src.write_bytes(b'data')

    # when: renaming it
    returned = await src.rename(dst)

    # then: the destination has the content and the return value points to it
    assert not await src.exists()
    assert await dst.read_bytes() == b'data'
    assert returned == dst


async def test_replace_overwrites(backend, tmp_path):
    # given: source and an existing destination
    base = urio.Path(tmp_path)
    src = base / 'src'
    dst = base / 'dst'
    await src.write_bytes(b'new')
    await dst.write_bytes(b'old')

    # when: replacing
    await src.replace(dst)

    # then: the destination is overwritten
    assert await dst.read_bytes() == b'new'


@skip_on_windows
async def test_symlink_to(backend, tmp_path):
    # given: a real target file
    base = urio.Path(tmp_path)
    target = base / 'target'
    await target.write_bytes(b'real')
    link = base / 'link'

    # when: creating a symlink to it
    await link.symlink_to(target)

    # then: the link resolves and reports as a symlink
    assert await link.is_symlink() is True
    assert await link.read_bytes() == b'real'
    assert await link.readlink() == target


async def test_hardlink_to(backend, tmp_path):
    # given: a real file
    base = urio.Path(tmp_path)
    target = base / 'target'
    await target.write_bytes(b'shared')
    link = base / 'hard'

    # when: creating a hard link
    await link.hardlink_to(target)

    # then: both names see the same inode
    assert await link.read_bytes() == b'shared'
    assert await link.samefile(target) is True


async def test_touch(backend, tmp_path):
    # given: a path with no file
    p = urio.Path(tmp_path) / 'touched'

    # when: touching it
    await p.touch()

    # then: an empty file exists
    assert await p.is_file()
    assert (await p.stat()).st_size == 0


async def test_touch_exclusive(backend, tmp_path):
    # given: an existing file
    p = urio.Path(tmp_path) / 'exists'
    await p.touch()

    # when/then: exclusive touch fails
    with pytest.raises(FileExistsError):
        await p.touch(exist_ok=False)


@skip_on_windows
async def test_chmod(backend, tmp_path):
    # given: a file
    p = urio.Path(tmp_path) / 'perm'
    await p.write_bytes(b'x')

    # when: changing its mode
    await p.chmod(0o600)

    # then: the new permission bits are set
    st = await p.stat()
    assert (st.st_mode & 0o777) == 0o600


async def test_iterdir(backend, tmp_path):
    # given: a directory with several entries
    base = urio.Path(tmp_path) / 'listing'
    await base.mkdir()
    for n in ('a', 'b', 'c'):
        await (base / n).write_bytes(b'')

    # when: iterating its contents
    names = sorted([entry.name async for entry in base.iterdir()])

    # then: every entry is yielded as an AsyncPath
    assert names == ['a', 'b', 'c']


async def test_glob(backend, tmp_path):
    # given: a directory with mixed file types
    base = urio.Path(tmp_path) / 'g'
    await base.mkdir()
    await (base / 'one.txt').write_bytes(b'')
    await (base / 'two.txt').write_bytes(b'')
    await (base / 'three.dat').write_bytes(b'')

    # when: globbing for *.txt
    matches = sorted([p.name async for p in base.glob('*.txt')])

    # then: only matching files are returned
    assert matches == ['one.txt', 'two.txt']


async def test_rglob(backend, tmp_path):
    # given: a nested tree
    base = urio.Path(tmp_path) / 'r'
    await (base / 'sub').mkdir(parents=True)
    await (base / 'top.txt').write_bytes(b'')
    await (base / 'sub' / 'deep.txt').write_bytes(b'')

    # when: recursively globbing
    matches = sorted([p.name async for p in base.rglob('*.txt')])

    # then: matches at every depth are found
    assert matches == ['deep.txt', 'top.txt']


async def test_open_via_path(backend, tmp_path):
    # given: an AsyncPath
    p = urio.Path(tmp_path) / 'viapath'

    # when: opening it through Path.open
    async with p.open('w') as f:
        await f.write('through path')

    # then: the content is written
    assert await p.read_text() == 'through path'


@skip_on_windows
async def test_lstat_vs_stat_on_symlink(backend, tmp_path):
    # given: a symlink to a file
    base = urio.Path(tmp_path)
    target = base / 't'
    await target.write_bytes(b'12345')
    link = base / 'l'
    await link.symlink_to(target)

    # when: comparing stat (follows) and lstat (does not)
    st = await link.stat()
    lst = await link.lstat()

    # then: stat sees the target, lstat sees the link itself
    assert st.st_size == 5
    assert lst.st_ino != st.st_ino


async def test_read_bytes_large_file(backend, tmp_path):
    # given: a file bigger than the linked chain's first read (128 KiB)
    p = urio.Path(tmp_path) / 'large.bin'
    payload = os.urandom(3 * 1024 * 1024)
    await p.write_bytes(payload)

    # when: reading it back whole
    out = await p.read_bytes()

    # then: content matches exactly
    assert out == payload


async def test_read_bytes_missing_file(backend, tmp_path):
    # given: a path that does not exist
    p = urio.Path(tmp_path) / 'nope.bin'

    # when/then: reading raises FileNotFoundError (from the chained open too)
    with pytest.raises(FileNotFoundError):
        await p.read_bytes()


async def test_write_bytes_overwrites_longer_content(backend, tmp_path):
    # given: a file holding long content
    p = urio.Path(tmp_path) / 'trunc.bin'
    await p.write_bytes(b'long old content here')

    # when: writing shorter content
    await p.write_bytes(b'new')

    # then: the file was truncated to exactly the new content
    assert await p.read_bytes() == b'new'


async def test_chain_slots_recycled(backend, tmp_path):
    # given: the uring driver's fixed-slot pool at its full size
    if backend != 'uring':
        return
    import asyncio

    from urio.driver import get_driver

    driver = get_driver()
    before = len(driver._slots) + len(driver._recycle_on_done)

    # when: many concurrent whole-file reads and writes via the chain path
    base = urio.Path(tmp_path)

    async def roundtrip(i):
        p = base / f'c{i}'
        await p.write_bytes(f'payload-{i}'.encode())
        return await p.read_bytes()

    results = await asyncio.gather(*(roundtrip(i) for i in range(64)))
    assert results == [f'payload-{i}'.encode() for i in range(64)]

    # then: every fixed slot returns to the pool once closes complete
    for _ in range(100):
        if len(driver._slots) >= before and not driver._recycle_on_done:
            break
        await asyncio.sleep(0.01)
    assert len(driver._slots) >= before
    assert not driver._recycle_on_done


async def test_pure_path_additions(backend):
    # given: paths built from parts
    p = urio.Path('/tmp/proj/a/b.txt')

    # then: the pathlib mirrors behave like their pathlib counterparts
    assert p.is_relative_to('/tmp/proj')
    assert not p.is_relative_to('/var')
    assert urio.Path.cwd() == pathlib.Path.cwd()
    assert urio.Path.home() == pathlib.Path.home()
    assert urio.Path('~').expanduser() == pathlib.Path('~').expanduser()


async def test_walk(backend, tmp_path):
    # given: a small tree
    (tmp_path / 'sub').mkdir()
    (tmp_path / 'sub' / 'f1').write_bytes(b'1')
    (tmp_path / 'top').write_bytes(b't')

    # when: walking it asynchronously
    seen = {}
    async for dirpath, dirnames, filenames in urio.Path(tmp_path).walk():
        seen[str(dirpath)] = (sorted(dirnames), sorted(filenames))

    # then: both levels are reported with their entries
    assert seen[str(tmp_path)] == (['sub'], ['top'])
    assert seen[str(tmp_path / 'sub')] == ([], ['f1'])
