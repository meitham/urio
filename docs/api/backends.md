# Backend control

The default backend is selected when urio is first used. See
[backends](../getting-started/backends.md) for the available implementations.

## `urio.set_backend(name)`

Select the backend for later operations. `name` may be `'auto'`,
`'thread'`, `'uring'`, or `'iocp'`. `'auto'` restores automatic
selection. The native names raise `RuntimeError` if unavailable; any
other name raises `ValueError`.

```python
import urio

urio.set_backend('thread')
```

This setting takes precedence over `URIO_BACKEND` and applies to the
process. It does not change files that have already been opened.

## `urio.get_backend()`

Return the active backend object, selecting one if needed. Its `name`
is `'uring'`, `'iocp'`, or `'thread'`.

```python
print(urio.get_backend().name)
```

Automatic selection tries io_uring on Linux, then IOCP on Windows if it
has been enabled, then the thread backend.

## `await urio.aclose()`

Release native I/O resources associated with the running event loop and
cancel pending operations. The next operation on that loop creates a new
driver. Call this before closing an event loop running in a worker thread
with the Windows IOCP backend.

## Environment variables

| Variable | Effect |
|---|---|
| `URIO_BACKEND` | Request `auto`, `thread`, `uring`, or `iocp`. An explicit call to `set_backend()` takes precedence. |
| `URIO_WINDOWS_IOCP=1` | Enable the Windows IOCP backend. It is required even when `URIO_BACKEND=iocp`. |
| `URIO_RING_ENTRIES` | Set the io_uring queue depth (default 512, maximum 32768). |

A larger ring can admit more operations at once. Read buffers are allocated
when operations are submitted, so limiting concurrency or lowering the
depth can reduce peak memory use during many large reads.
