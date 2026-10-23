# Backends

urio selects a backend when it is first used. The public file and path APIs
are the same with each backend.

| Backend | Used when | File I/O |
|---|---|---|
| `uring` | Linux, when a ring can be created | io_uring submissions |
| `iocp` | Windows with `URIO_WINDOWS_IOCP=1` | overlapped I/O and a completion port |
| `thread` | Otherwise | blocking calls in the event loop's executor |

The Linux backend submits file reads, writes, opens, closes, and several
filesystem operations through io_uring. Directory listing, `chmod`,
`readlink`, `realpath`, and `utime` still use the executor. On kernels that
do not support io_uring `ftruncate`, that operation also uses the executor.

The Windows IOCP backend handles file opens, reads, writes, and closes.
Other operations use the executor. IOCP is optional because concurrent,
warm-cache binary reads were slower than the thread backend in the
[Windows benchmarks](../benchmarks.md#windows-iocp). Windows uses `thread`
unless `URIO_WINDOWS_IOCP=1` is set.

The thread backend uses the event loop's default executor. It is selected on
macOS, when io_uring cannot be used, and by default on Windows. It is also
available explicitly on any platform.

## Selecting a backend

```python
import urio

urio.set_backend('thread')
print(urio.get_backend().name)
```

`set_backend()` accepts `'auto'`, `'uring'`, `'iocp'`, or `'thread'`.
`'auto'` restores automatic selection. Requesting `'uring'` or `'iocp'`
raises `RuntimeError` if that backend is unavailable. Selection set in code
takes precedence over `URIO_BACKEND`:

```console
URIO_BACKEND=thread python app.py
```

See [backend control](../api/backends.md) for the full API and environment
variables.

## Choosing for a workload

The native backends avoid an executor round trip for each supported operation.
That can help when many small files are accessed concurrently or many files
need `fsync`. A thread pool can do better for concurrent, warm-cache reads of
large files, where copying bytes across several cores matters more than
submission overhead.

These are workload effects, not guarantees. The [benchmark results](../benchmarks.md)
include the test setup and cases where a native backend was slower. Run the
benchmark on the storage and Python build used by your application before
choosing a backend for performance reasons.
