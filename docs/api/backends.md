# Backend control

The public API for selecting and inspecting backends. See
[Backends](../getting-started/backends.md) for the conceptual overview.

## `urio.set_backend(name)`

```python
urio.set_backend('auto' | 'thread' | 'uring' | 'iocp')
```

Force the backend for subsequently created files and paths.

| `name` | Effect |
|---|---|
| `'auto'` | Use the fastest available backend: io_uring, else IOCP (if opted in), else the thread pool (default). |
| `'thread'` | Always use the thread-pool fallback. |
| `'uring'` | Require io_uring; raises `RuntimeError` if unavailable. |
| `'iocp'` | Require the Windows IOCP backend; raises `RuntimeError` unless on Windows with `URIO_WINDOWS_IOCP=1` set. |

Raises `ValueError` for any other name. The selection is process-wide and takes
precedence over the `URIO_BACKEND` environment variable.

```python
urio.set_backend('thread')   # e.g. to mimic a thread-only platform locally
```

## `urio.get_backend()`

```python
backend = urio.get_backend()
print(backend.name)   # 'uring', 'iocp', or 'thread'
```

Return the active backend instance, selecting one on first use. Selection order:

1. an explicit `set_backend(...)` override,
2. the `URIO_BACKEND` environment variable,
3. auto-detection: io_uring if a ring can be created, else IOCP if
   `URIO_WINDOWS_IOCP=1` is set on Windows, else the thread pool.

## `urio.aclose()`

```python
await urio.aclose()
```

Release the running loop's native I/O resources (the io_uring ring + eventfd on
Linux, the IOCP driver on Windows) deterministically, cancelling any still-
pending operations. Optional — a driver is cached on the loop object and is
reclaimed when the loop is garbage collected — but useful for embedders that
create many loops or want a clean teardown point. urio works again on the same
loop afterwards: the next operation lazily creates a fresh driver.

## Environment variables

```bash
URIO_BACKEND=thread python app.py
URIO_BACKEND=uring  python app.py   # error at first use if unavailable
```

On Windows, `URIO_WINDOWS_IOCP=1` opts into the IOCP backend — with it set,
auto-detection selects IOCP; without it, `'iocp'` is refused even when
requested explicitly.

## Backend classes

You normally interact with backends only through `set_backend`/`get_backend`,
but the classes are public:

- **`urio.backend.UringBackend`** — io_uring submissions for read, write,
  fsync, open, close, statx, ftruncate, mkdir, unlink, rename, symlink, and
  link; everything else inherited from the thread backend.
- **`urio.backend.IocpBackend`** — Windows overlapped I/O + completion port
  for read, write, open, and close; everything else inherited from the thread
  backend.
- **`urio.backend.ThreadBackend`** — blocking syscalls dispatched to the event
  loop's default executor (the aiofiles approach).

All expose the same coroutine surface (`open`, `read`, `write`, `fsync`,
`close`, `statx`, `mkdir`, `unlink`, `rename`, `symlink`, `link`, `ftruncate`,
`chmod`, `readlink`, `realpath`, `scandir`, `utime`), so they are fully
interchangeable.
