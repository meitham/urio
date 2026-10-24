# `urio.Path`

`urio.Path` represents a filesystem path. Methods that only manipulate the
path, such as `parent` and `with_suffix()`, are synchronous. Methods that
access the filesystem, such as `stat()` and `read_text()`, are asynchronous.

```python
from urio import Path

path = Path('project') / 'README.md'
await path.parent.mkdir(parents=True, exist_ok=True)
await path.write_text('# Project\n', encoding='utf-8')
```

`Path` is an alias for `urio.AsyncPath`. It accepts the same path segments
as `pathlib.Path`. `str(path)` and `os.fspath(path)` return the path string,
and a `urio.Path` compares equal to a `pathlib.Path` for the same path.

## Path operations

The `/` operator and `joinpath()` join segments. `name`, `stem`, `suffix`,
`suffixes`, `parent`, `parents`, `parts`, `anchor`, `drive`, and `root`
inspect the path. `with_name()`, `with_stem()`, `with_suffix()`,
`relative_to()`, `expanduser()`, and `absolute()` return new paths.
`is_absolute()`, `is_relative_to()`, `match()`, `as_posix()`, and
`as_uri()` return plain values. `Path.cwd()` and `Path.home()` construct
paths for the current working directory and home directory.

These operations do not access the filesystem. In particular,
`absolute()` does not resolve symbolic links; use `await resolve()`
for that.

## Filesystem operations

### Reading and writing files

`await read_bytes()` returns the file's contents as bytes.
`await write_bytes(data)` replaces the file and returns the number of bytes
written. `await read_text(encoding=None, errors=None)` and
`await write_text(data, encoding=None, errors=None, newline=None)`
provide the corresponding text operations. `write_text()` returns the
number of input characters.

`open(mode='r', *, encoding=None, errors=None, newline=None)` returns the
same awaitable context manager as [`urio.open()`](open.md).

### File status

`await stat()` follows symbolic links; `await lstat()` does not. Both
return an `os.stat_result`. `await exists()`, `await is_file()`,
`await is_dir()`, and `await is_symlink()` return booleans.
`await samefile(other)` reports whether two paths identify the same file.

`await resolve(strict=False)` returns an absolute path with symbolic links
resolved. If `strict` is true, a missing path raises `FileNotFoundError`.
`await readlink()` returns the target of a symbolic link as a `urio.Path`.

### Creating and removing paths

`await mkdir(mode=0o777, parents=False, exist_ok=False)` creates a
directory. `parents=True` creates missing parents. `exist_ok=True`
allows an existing directory.

`await touch(mode=0o666, exist_ok=True)` creates a file or updates its
modification time. `await unlink(missing_ok=False)` removes a file;
`missing_ok=True` ignores a missing file. `await rmdir()` removes an
empty directory. `await chmod(mode)` changes its permission bits.

`await rename(target)` and `await replace(target)` return the path at the
new location. `replace()` overwrites an existing destination.
`await symlink_to(target, target_is_directory=False)` creates a symbolic
link; `await hardlink_to(target)` creates a hard link.

### Directory iteration

`iterdir()`, `glob(pattern)`, and `rglob(pattern)` return asynchronous
iterators of `urio.Path` objects:

```python
async for path in Path('src').rglob('*.py'):
    print(path)
```

`walk(top_down=True, on_error=None, follow_symlinks=False)` yields
`(dirpath, dirnames, filenames)` tuples. Unlike `pathlib.Path.walk()`,
it collects the walk in an executor before yielding, so modifying
`dirnames` does not prune the traversal. If supplied, `on_error` runs in
the executor thread.

Directory iteration and several metadata operations use the thread backend
even when io_uring handles other operations. See [backends](../getting-started/backends.md).
