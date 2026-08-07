# Architecture

How urio turns a Python `await f.read(...)` into a real asynchronous kernel I/O
operation and back — without blocking the event loop, without a thread per
operation, and without ever letting the kernel touch memory Python could free.

## The shape of the problem

`asyncio` has no native asynchronous file I/O. The kernel's blocking
`read()`/`write()` syscalls would stall the event loop, so libraries like
`aiofiles` hand each call to a thread pool. That works, but caps out: under
heavy concurrency the pool saturates, and every operation pays a thread
round-trip.

Linux `io_uring` offers true async file I/O — many operations submitted to the
kernel and reaped with a single `io_uring_enter` syscall. urio bridges that to
`asyncio`. The hard parts are (1) delivering completions back onto the event
loop thread without a polling thread, and (2) guaranteeing every buffer the
kernel touches outlives the operation. Both are addressed below.

## Layers

```
python/urio/file.py · path.py        the public API
async file objects (read-ahead buffering, text codecs) and the
async pathlib; both talk only to a backend
                    |
python/urio/backend.py               pluggable backends
UringBackend (Linux io_uring) · IocpBackend (Windows, opt-in)
· ThreadBackend (portable executor fallback, aiofiles-style)
                    |
python/urio/driver.py                the asyncio bridge (Linux)
one Driver per event loop: futures keyed by user_data, batched
submission flush, in-flight gate, chain/cancel bookkeeping
                    |
src/lib.rs (PyO3)                    the native ring
Ring: io_uring SQ/CQ + eventfd + the pending map that owns every
buffer the kernel may touch (an `iocp` module mirrors this on
Windows: ProactorIo / Iocp)
```

## The engine: one ring per event loop

A `Ring` (and its `Driver`) is created lazily per event loop and cached **as an
attribute of the loop object**, so its lifetime is exactly the loop's — a
finished loop releases its ring, eventfd, and any fixed-file table with it.
(`await urio.aclose()` tears it down deterministically; the next operation
creates a fresh one.) Rings are never shared across threads — one driver per
loop, and everything below happens on the loop thread. The class is still
`Send`, though: a loop created inside a worker thread is often garbage-collected
later on a *different* thread, and dropping the ring there must be (and is)
safe and silent.

The completion path is eventfd-driven, no polling thread anywhere:

1. The ring registers an **eventfd** with io_uring; the kernel bumps it whenever
   completions post.
2. That fd is handed to `loop.add_reader`, so the loop wakes on completions like
   on any readable descriptor.
3. On wake the driver drains the eventfd, **reaps every completion**, and
   resolves the matching `asyncio.Future` per `user_data` — a result, or an
   `OSError` carrying the real `-errno`.

Submissions batch per event-loop tick: the first submission schedules one flush
via `call_soon`, later submissions in the same tick just append SQEs, and the
flush enters the kernel once for the whole batch. If the enter fails, only the
SQEs queued since the last successful enter are failed — ops already in the
kernel keep their accounting and complete normally; `EAGAIN`/`EBUSY` retry on a
later tick. In-flight operations are capped at the ring depth by a hand-rolled
O(1) gate (an `asyncio.Semaphore` profiled as O(n²) under hundreds of waiters),
so the completion queue can never overflow.

When an awaiting task is cancelled, the driver fires a fire-and-forget
`ASYNC_CANCEL` at the in-flight op so its gate slot frees promptly; the op's own
completion still arrives and does the accounting.

## Memory safety: the pending map owns what the kernel touches

io_uring takes raw pointers and returns immediately; the kernel reads or writes
that memory asynchronously. Whatever the kernel touches is therefore stored in
the ring's `pending` map, keyed by `user_data`, until the completion is reaped
— and `Ring`'s `Drop` refuses to free anything until every outstanding
completion has been drained.

Three forms are in use, and both data directions are **zero-copy**:

- **Writes** pin the caller's Python `bytes` itself in the pending map and point
  the kernel at its buffer. Safe because `bytes` is immutable, CPython never
  relocates it, and the held reference prevents its free. `writelines` submits
  one vectored `writev` (the iovec array lives in the pending entry too). Do
  not extend this to mutable or relocatable buffers — the file layer coerces to
  `bytes` before anything reaches the ring.
- **Reads** allocate an *uninitialised* Python `bytes` up front and let the
  kernel fill it. A full read hands that very object back (no second `memcpy`);
  a short read copies only the prefix actually read. Mutating the buffer is
  sound because only the pending map references it until completion — the
  mirror image of the write trick. Reads ≥ 1 MB additionally carry
  `IOSQE_ASYNC`, punting them to io-wq workers so concurrent warm-cache copies
  run on multiple cores instead of serialising inline on the loop thread.
- **Paths** for `openat`/`statx`/`mkdirat`/… are owned `CString`s in the entry.

## Linked open→I/O chains

The whole-file helpers (`Path.read_bytes` / `write_bytes` / `read_text` /
`write_text`) skip the open→I/O→close round-trips. The ring registers a sparse
**fixed-file table** at creation; a helper submits `OPENAT` (directly into a
table slot, no real fd) linked via `IOSQE_IO_LINK` to the first `READ`/`WRITE`
on `Fixed(slot)` — one submission, one wakeup, for open *and* I/O. Files larger
than the first read continue on the same slot; the close is fire-and-forget for
reads and awaited for writes (so close errors surface), and a slot returns to
the pool only after its close completes, so a new chain can never race the old
fd. The driver tracks each chain so a cancelled or half-failed pair still
closes whatever landed in the slot. No free slot, or a kernel without sparse
registration → transparent fallback to the classic three-step path.

## The file objects

io_uring I/O is positional, so `AsyncBufferedFile` tracks a logical offset and
keeps a read-ahead buffer to provide ordinary `read`/`readline` semantics; a
short non-empty read is *not* treated as EOF (only an empty read is).
`read(-1)` probes 128 KiB first — small files complete in a single op with no
stat — then sizes the file and re-reads the whole span in one exact read: the
re-read of a warm 128 KiB probe costs far less than a join copy of the file,
and the result is the single `bytes` the kernel filled. `AsyncTextFile` layers
incremental codecs and universal newlines on top; on free-threaded (no-GIL)
builds, codec work ≥ 32 KiB is offloaded to the executor so concurrent files
decode on separate cores.

## Backends and selection

`get_backend()` picks once per process: an explicit `set_backend()` override,
then `URIO_BACKEND`, then auto-detection — which actually creates a ring, so
seccomp'd sandboxes and pre-5.1 kernels fall back cleanly. `ThreadBackend` runs
blocking syscalls in the loop's executor (the aiofiles strategy) and is the
base class; `UringBackend` overrides the ops with io_uring opcodes (including
`ftruncate` on kernels ≥ 6.9, with a runtime fallback) and inherits the rest
(`scandir`, `chmod`, `realpath`, …) so unsupported ops transparently use the
executor.

## Windows: IOCP (opt-in)

`URIO_WINDOWS_IOCP=1` enables real async file I/O via overlapped I/O + an I/O
completion port, mirroring the Linux design: per-op state owns its buffers
until reaped, writes and reads are zero-copy. On a `ProactorEventLoop` (the
Windows default) completions are reaped **on the loop thread** through the
loop's *own* IOCP — the Windows analogue of the eventfd, no poller thread; a
single-poller-thread driver covers other loops. The known limitation: a
warm-cache overlapped `ReadFile` completes synchronously, so the kernel copies
inline on the submitting thread — binary reads stay single-core, which is why
Windows defaults to the thread backend and IOCP remains opt-in.

## Testing

Every I/O test runs against **both** the io_uring and thread backends (and IOCP
on Windows) via one parametrised fixture. A differential suite drives
seeded-random op sequences against stdlib `io` oracles — a real file for binary
semantics (truncate-beyond-EOF zero-extends a file but not a `BytesIO`), and
`TextIOWrapper` for universal/kept newline modes — so the buffered and text
layers are checked against CPython's behaviour, not our reading of it.
