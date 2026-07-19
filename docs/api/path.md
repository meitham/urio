# `urio.Path`

```python
from urio import Path        # alias of urio.AsyncPath
```

An async counterpart to `pathlib.Path`. **Pure-path** operations (those that
only manipulate the path string) are synchronous and delegate directly to
`pathlib`. **Filesystem** operations are coroutines and go through the active
[backend](backends.md) — so on Linux `stat`, `mkdir`, `unlink`, `rename`,
`symlink_to`, `hardlink_to`, and the read/write helpers are genuine io_uring
submissions, while directory listing and a few metadata tweaks use the
thread-pool path.

This is functionality aiofiles does not offer.

```python
p = Path('project') / 'src' / 'main.py'
await p.parent.mkdir(parents=True, exist_ok=True)
await p.write_text('print("hi")\n')
print(await p.exists(), (await p.stat()).st_size)
```

---

## Construction

```python
Path('a', 'b', 'c')      # joins segments, like pathlib.Path
Path(other_async_path)   # wraps/copies another urio.Path
Path('/abs') / 'rel'     # the / operator joins (sync)
```

`urio.Path` interoperates with the rest of Python: `str(p)`, `os.fspath(p)`, and
`repr(p)` all work, and a `urio.Path` compares/hashes equal to the
`pathlib.Path` for the same string, so you can pass it straight to `urio.open`
or any stdlib function expecting a path.

## Why a single class?

`pathlib` ships six classes, but they split along one line: the **pure**
classes (`PurePath`, `PurePosixPath`, `PureWindowsPath`) do no I/O — they
exist so you can manipulate foreign-flavour paths, like building a Windows
path on a Linux server — while `Path` does the I/O, instantiating as
`PosixPath` or `WindowsPath` for the running OS (pathlib refuses to create
the off-OS one, since its I/O couldn't work).

Only the I/O half has anything to make async, so urio mirrors only `Path`.
There is deliberately no `AsyncPureWindowsPath`: pure path algebra has
nothing to await, and pathlib already does it perfectly — use pathlib's pure
classes and pass the result to `urio.Path` (it accepts any `str` or
`os.PathLike`, and wraps a native `pathlib.Path` underneath, so per-OS
flavour behaviour is inherited rather than reimplemented). `trio.Path` and
`anyio.Path` make the same choice.

---

## Pure path operations (synchronous, no I/O)

These mirror `pathlib` exactly and return plain values or new `urio.Path`
objects.

| Member | Kind | Description |
|---|---|---|
| `name` | property | Final component. |
| `stem` | property | Final component without its suffix. |
| `suffix` | property | File extension of the final component. |
| `suffixes` | property | List of all suffixes. |
| `parent` | property | The logical parent (`urio.Path`). |
| `parents` | property | List of ancestors (`urio.Path`). |
| `parts` | property | Tuple of path components. |
| `anchor` | property | Drive + root. |
| `drive` | property | Drive letter/name (empty on POSIX). |
| `root` | property | Root, e.g. `'/'`. |
| `is_absolute()` | method | Whether the path is absolute. |
| `as_posix()` | method | String with forward slashes. |
| `as_uri()` | method | A `file:` URI. |
| `match(pattern)` | method | Glob-match against the path. |
| `with_name(name)` | method | New path with the final component replaced. |
| `with_suffix(suffix)` | method | New path with the suffix replaced. |
| `with_stem(stem)` | method | New path with the stem replaced. |
| `joinpath(*other)` | method | Join more segments. |
| `relative_to(*other)` | method | Path relative to another. |
| `is_relative_to(other)` | method | Whether the path is relative to another. |
| `expanduser()` | method | New path with `~`/`~user` expanded. |
| `absolute()` | method | Absolute path (no symlink resolution; no I/O). |
| `Path.cwd()` | classmethod | The current working directory. |
| `Path.home()` | classmethod | The user's home directory. |

---

## Filesystem operations (async)

### Metadata & predicates

#### `await stat()`
Return an `os.stat_result` for the path, following symlinks (io_uring `statx`
under the `uring` backend).

#### `await lstat()`
Like `stat`, but does **not** follow symlinks.

#### `await exists()`
`True` if the path exists.

#### `await is_file()` / `await is_dir()` / `await is_symlink()`
Type predicates derived from `stat`/`lstat`. Return `False` (rather than raising)
when the path does not exist.

#### `await samefile(other)`
`True` if both paths refer to the same inode/device.

### Reading & writing

#### `await read_bytes()` / `await write_bytes(data)`
Read/write the whole file in binary. `write_bytes` returns the byte count.

#### `await read_text(encoding=None, errors=None)` / `await write_text(data, encoding=None, errors=None, newline=None)`
Read/write the whole file as text. `write_text` returns the character count.

!!! tip "One-submission fast path"
    On the io_uring backend these four helpers ride a **linked open→I/O chain**
    on a fixed-file table: the open and the first read/write reach the kernel as
    a single submission, so a small file costs one round-trip instead of three
    (see [How it works](../internals.md#linked-openreadwrite-chains)). Whole-file
    reads are also zero-copy — the kernel fills the returned `bytes` directly.

#### `open(mode='r', *, encoding=None, errors=None, newline=None)`
Open this path and return the same awaitable/context-manager object as
[`urio.open`](open.md):

```python
async with Path('f.txt').open('w') as f:
    await f.write('hello')
```

### Directory & link operations

#### `await mkdir(mode=0o777, parents=False, exist_ok=False)`
Create a directory. With `parents=True`, create missing parents; with
`exist_ok=True`, tolerate an existing directory.

#### `await rmdir()`
Remove an empty directory.

#### `await unlink(missing_ok=False)`
Remove a file. With `missing_ok=True`, do nothing if it's already gone.

#### `await rename(target)` / `await replace(target)`
Rename/move to `target`, returning a `urio.Path` for the new location.
`rename(2)` replaces an existing destination atomically, so `replace` behaves the
same.

#### `await symlink_to(target, target_is_directory=False)`
Make this path a symbolic link pointing at `target`.

#### `await hardlink_to(target)`
Make this path a hard link to the existing file `target`.

#### `await touch(mode=0o666, exist_ok=True)`
Create the file if absent (updating its mtime if present). `exist_ok=False`
raises `FileExistsError` when it already exists.

#### `await chmod(mode)`
Change the permission bits.

#### `await readlink()`
Return the target of a symbolic link as a `urio.Path`.

#### `await resolve(strict=False)`
Return the canonical absolute path, resolving symlinks (`realpath`).

### Iteration

#### `iterdir()`
Async iterator yielding the directory's entries as `urio.Path` objects:

```python
async for child in Path('.').iterdir():
    print(child.name)
```

#### `glob(pattern)` / `rglob(pattern)`
Async iterators over matching paths; `rglob` recurses:

```python
async for py in Path('src').rglob('*.py'):
    print(py)
```

#### `walk(top_down=True, on_error=None, follow_symlinks=False)`
Async iterator over `(dirpath, dirnames, filenames)` triples, mirroring
`pathlib.Path.walk`, with `dirpath` as a `urio.Path`:

```python
async for dirpath, dirnames, filenames in Path('src').walk():
    ...
```

The tree is walked in the executor up front, so editing `dirnames` in place
does **not** prune the walk (unlike the stdlib).

!!! note
    `iterdir`, `glob`, `rglob`, `walk`, `chmod`, `readlink`, `resolve`, and
    `touch`'s mtime update use the thread-pool path even on the io_uring
    backend, because those operations have no in-scope io_uring opcode. They
    are still fully async from the caller's perspective.
