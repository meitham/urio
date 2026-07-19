"""An async ``pathlib.Path``.

Pure-path operations (``name``, ``suffix``, ``parent``, ``/`` joining,
``with_suffix`` ...) involve no I/O and delegate straight to a wrapped
:class:`pathlib.Path`. Operations that touch the filesystem (``stat``,
``read_bytes``, ``mkdir``, ``rename`` ...) are ``async`` and go through the
active backend -- so on Linux ``stat``/``mkdir``/``unlink``/``rename``/symlink
and the read/write helpers are genuine io_uring submissions, while directory
listing and the metadata tweaks fall back to the executor.
"""

from __future__ import annotations

import locale
import os
import pathlib
import stat as stat_module

from .backend import get_backend
from .file import open as _async_open


class AsyncPath:
    __slots__ = ('_path',)

    def __init__(self, *segments):
        if len(segments) == 1 and isinstance(segments[0], AsyncPath):
            self._path = segments[0]._path
        else:
            parts = [s._path if isinstance(s, AsyncPath) else s for s in segments]
            self._path = pathlib.Path(*parts) if parts else pathlib.Path()

    # -- construction / dunders ------------------------------------------

    @classmethod
    def _wrap(cls, path: pathlib.Path) -> AsyncPath:
        obj = cls.__new__(cls)
        obj._path = path
        return obj

    @classmethod
    def cwd(cls) -> AsyncPath:
        return cls._wrap(pathlib.Path.cwd())

    @classmethod
    def home(cls) -> AsyncPath:
        return cls._wrap(pathlib.Path.home())

    def __fspath__(self):
        return str(self._path)

    def __str__(self):
        return str(self._path)

    def __repr__(self):
        return f'{type(self).__name__}({str(self._path)!r})'

    def __eq__(self, other):
        if isinstance(other, AsyncPath):
            return self._path == other._path
        if isinstance(other, pathlib.PurePath):
            return self._path == other
        return NotImplemented

    def __hash__(self):
        return hash(self._path)

    def __truediv__(self, other):
        other = other._path if isinstance(other, AsyncPath) else other
        return self._wrap(self._path / other)

    def __rtruediv__(self, other):
        other = other._path if isinstance(other, AsyncPath) else other
        return self._wrap(other / self._path)

    # -- pure path properties (no I/O) -----------------------------------

    @property
    def name(self):
        return self._path.name

    @property
    def stem(self):
        return self._path.stem

    @property
    def suffix(self):
        return self._path.suffix

    @property
    def suffixes(self):
        return self._path.suffixes

    @property
    def parent(self):
        return self._wrap(self._path.parent)

    @property
    def parents(self):
        return [self._wrap(p) for p in self._path.parents]

    @property
    def parts(self):
        return self._path.parts

    @property
    def anchor(self):
        return self._path.anchor

    @property
    def drive(self):
        return self._path.drive

    @property
    def root(self):
        return self._path.root

    def is_absolute(self):
        return self._path.is_absolute()

    def as_posix(self):
        return self._path.as_posix()

    def as_uri(self):
        return self._path.as_uri()

    def match(self, pattern):
        return self._path.match(pattern)

    def with_name(self, name):
        return self._wrap(self._path.with_name(name))

    def with_suffix(self, suffix):
        return self._wrap(self._path.with_suffix(suffix))

    def with_stem(self, stem):
        return self._wrap(self._path.with_stem(stem))

    def joinpath(self, *other):
        other = [o._path if isinstance(o, AsyncPath) else o for o in other]
        return self._wrap(self._path.joinpath(*other))

    def relative_to(self, *other):
        other = [o._path if isinstance(o, AsyncPath) else o for o in other]
        return self._wrap(self._path.relative_to(*other))

    def is_relative_to(self, other) -> bool:
        other = other._path if isinstance(other, AsyncPath) else other
        return self._path.is_relative_to(other)

    def expanduser(self) -> AsyncPath:
        return self._wrap(self._path.expanduser())

    # -- filesystem I/O (async) ------------------------------------------

    async def stat(self):
        return await get_backend().statx(str(self._path), follow=True)

    async def lstat(self):
        return await get_backend().statx(str(self._path), follow=False)

    async def exists(self):
        try:
            await self.stat()
            return True
        except (FileNotFoundError, NotADirectoryError):
            return False

    async def is_file(self):
        try:
            return stat_module.S_ISREG((await self.stat()).st_mode)
        except (FileNotFoundError, NotADirectoryError):
            return False

    async def is_dir(self):
        try:
            return stat_module.S_ISDIR((await self.stat()).st_mode)
        except (FileNotFoundError, NotADirectoryError):
            return False

    async def is_symlink(self):
        try:
            return stat_module.S_ISLNK((await self.lstat()).st_mode)
        except (FileNotFoundError, NotADirectoryError):
            return False

    def open(self, mode='r', *, encoding=None, errors=None, newline=None):
        return _async_open(
            self._path,
            mode,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    async def read_bytes(self):
        # Backends with linked open→read chains (io_uring) read a whole small
        # file in a single kernel round-trip; others take the classic path.
        backend = get_backend()
        fast = getattr(backend, 'read_file', None)
        if fast is not None:
            return await fast(str(self._path))
        async with _async_open(self._path, 'rb') as f:
            return await f.read()

    async def write_bytes(self, data):
        backend = get_backend()
        fast = getattr(backend, 'write_file', None)
        if fast is not None:
            return await fast(str(self._path), bytes(data))
        async with _async_open(self._path, 'wb') as f:
            return await f.write(data)

    async def read_text(self, encoding=None, errors=None):
        backend = get_backend()
        fast = getattr(backend, 'read_file', None)
        if fast is not None:
            data = await fast(str(self._path))
            if encoding is None:
                encoding = locale.getpreferredencoding(False)
            text = data.decode(encoding, errors or 'strict')
            # Universal newlines, matching open(mode='r', newline=None): a
            # whole-file decode + translate equals the incremental path.
            if '\r' in text:
                text = text.replace('\r\n', '\n').replace('\r', '\n')
            return text
        async with _async_open(self._path, 'r', encoding=encoding, errors=errors) as f:
            return await f.read()

    async def write_text(self, data, encoding=None, errors=None, newline=None):
        backend = get_backend()
        fast = getattr(backend, 'write_file', None)
        if fast is not None:
            if not isinstance(data, str):
                raise TypeError(f'data must be str, not {type(data).__name__}')
            nchars = len(data)
            if newline is None:
                nl = os.linesep
            elif newline == '':
                nl = ''
            else:
                nl = newline
            if nl and nl != '\n':
                data = data.replace('\n', nl)
            if encoding is None:
                encoding = locale.getpreferredencoding(False)
            await fast(str(self._path), data.encode(encoding, errors or 'strict'))
            return nchars
        async with _async_open(
            self._path, 'w', encoding=encoding, errors=errors, newline=newline
        ) as f:
            return await f.write(data)

    async def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        backend = get_backend()
        try:
            await backend.mkdir(str(self._path), mode)
        except FileNotFoundError:
            if not parents or self.parent == self:
                raise
            await self.parent.mkdir(mode=mode, parents=True, exist_ok=True)
            await backend.mkdir(str(self._path), mode)
        except FileExistsError:
            if not exist_ok or not await self.is_dir():
                raise

    async def rmdir(self):
        await get_backend().unlink(str(self._path), remove_dir=True)

    async def unlink(self, missing_ok=False):
        try:
            await get_backend().unlink(str(self._path), remove_dir=False)
        except FileNotFoundError:
            if not missing_ok:
                raise

    async def rename(self, target):
        target = target._path if isinstance(target, AsyncPath) else target
        await get_backend().rename(str(self._path), os.fspath(target))
        return self._wrap(pathlib.Path(target))

    async def replace(self, target):
        # Atomically replace an existing destination. POSIX rename(2) overwrites,
        # but Windows MoveFile (os.rename) fails if the target exists — os.replace
        # does the atomic overwrite on every platform, so route through it.
        target = target._path if isinstance(target, AsyncPath) else target
        await get_backend().replace(str(self._path), os.fspath(target))
        return self._wrap(pathlib.Path(target))

    async def symlink_to(self, target, target_is_directory=False):
        target = target._path if isinstance(target, AsyncPath) else target
        await get_backend().symlink(os.fspath(target), str(self._path))

    async def hardlink_to(self, target):
        target = target._path if isinstance(target, AsyncPath) else target
        await get_backend().link(os.fspath(target), str(self._path))

    async def touch(self, mode=0o666, exist_ok=True):
        flags = (
            os.O_CREAT
            | os.O_WRONLY
            | getattr(os, 'O_CLOEXEC', 0)
            | getattr(os, 'O_BINARY', 0)
        )
        if not exist_ok:
            flags |= os.O_EXCL
        backend = get_backend()
        fd = await backend.open(str(self._path), flags, mode)
        await backend.close(fd)
        if exist_ok:
            try:
                await backend.utime(str(self._path), None)
            except OSError:
                pass

    async def chmod(self, mode):
        await get_backend().chmod(str(self._path), mode)

    async def readlink(self):
        return self._wrap(pathlib.Path(await get_backend().readlink(str(self._path))))

    async def resolve(self, strict=False):
        return self._wrap(pathlib.Path(await get_backend().realpath(str(self._path))))

    def absolute(self):
        return self._wrap(self._path.absolute())

    async def samefile(self, other):
        other = other._path if isinstance(other, AsyncPath) else other
        a = await self.stat()
        b = await get_backend().statx(os.fspath(other), follow=True)
        return (a.st_ino, a.st_dev) == (b.st_ino, b.st_dev)

    async def iterdir(self):
        names = await get_backend().scandir(str(self._path))
        for entry in names:
            yield self._wrap(self._path / entry)

    async def glob(self, pattern):
        matches = await _to_thread_glob(self._path, pattern, recursive=False)
        for m in matches:
            yield self._wrap(m)

    async def rglob(self, pattern):
        matches = await _to_thread_glob(self._path, pattern, recursive=True)
        for m in matches:
            yield self._wrap(m)

    async def walk(self, top_down=True, on_error=None, follow_symlinks=False):
        """Async ``pathlib.Path.walk``: yields ``(dirpath, dirnames, filenames)``
        with ``dirpath`` as an :class:`AsyncPath`. The whole tree is walked in
        the executor up front, so editing ``dirnames`` in place does NOT prune
        the walk (unlike the stdlib); ``on_error`` (if given) is called in the
        executor thread."""
        import asyncio

        loop = asyncio.get_running_loop()

        def _walk():
            return list(
                self._path.walk(
                    top_down=top_down,
                    on_error=on_error,
                    follow_symlinks=follow_symlinks,
                )
            )

        for dirpath, dirnames, filenames in await loop.run_in_executor(None, _walk):
            yield self._wrap(dirpath), dirnames, filenames


async def _to_thread_glob(path: pathlib.Path, pattern: str, recursive: bool):
    import asyncio

    loop = asyncio.get_running_loop()

    def _glob():
        it = path.rglob(pattern) if recursive else path.glob(pattern)
        return list(it)

    return await loop.run_in_executor(None, _glob)
