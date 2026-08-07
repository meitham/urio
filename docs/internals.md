# How it works

urio is a bridge between Python's `asyncio` event loop and Linux io_uring.
The native ring lives in a small Rust extension (PyO3); the async integration
and the public API are Python.

## The completion bridge

io_uring is *completion-based*: operations are submitted to a submission
queue, and results are later reaped from a completion queue. The problem to
solve is waking the event loop exactly when results are ready, without a
polling thread.

1. A single ring is created **per event loop** (lazily, then cached as an
   attribute of the loop object, so it is reclaimed with the loop; call
   [`await urio.aclose()`](api/backends.md#urioaclose) to release it
   deterministically).
2. The ring registers an **eventfd** with io_uring (`register_eventfd`). The
   kernel writes to that descriptor whenever completions are posted.
3. That descriptor is handed to **`loop.add_reader(efd, ...)`**, so the event
   loop wakes on it like any other readable descriptor.
4. Each submission maps a `user_data` id to an `asyncio.Future`. The
   coroutine awaits that future.
5. On wake-up the driver **drains the eventfd**, **reaps** every completion,
   and resolves the matching futures, setting a result or an `OSError` built
   from `-errno` for failures.

Everything runs on the loop thread. There is no executor and no background
thread on the io_uring path; the I/O itself is asynchronous, rather than
concurrency through threads.

### Submission batching

Submissions are *queued* in the ring but not entered into the kernel one at
a time. The first submission in an event-loop tick schedules a single flush
via `loop.call_soon`; every other submission in that tick appends a
submission-queue entry. When the flush runs, one `io_uring_enter` carries the
whole batch to the kernel. Many operations submitted with a single system
call is io_uring's defining feature, and batching makes it the common case:
a workload of 500 concurrent open+write+close operations issues about 15
`io_uring_enter` calls instead of 1500. In-flight operations are bounded to
the ring depth by an O(1) gate so the completion queue cannot overflow.

```
coroutine ── submit ──▶ io_uring SQ ──▶ kernel
    ▲                                      │
    │                                  completion
 Future.set_result                         │
    │                                       ▼
 driver.reap ◀── add_reader wakes ◀── eventfd
```

## Memory safety

io_uring takes a pointer from the caller and returns immediately; the kernel
reads from (write operations) or writes into (read operations) that buffer
**asynchronously**. If the buffer were freed or moved before completion, the
result would be corruption or a use-after-free. This is the main io_uring
hazard.

urio avoids it by keeping whatever the kernel touches alive in the ring's
pending map, keyed by `user_data`, until the completion is reaped:

- **Writes** submit against the caller's Python `bytes` object directly,
  without a copy, holding a reference to it in the pending map. This is safe
  because `bytes` is immutable and CPython never relocates it, so the pointer
  stays valid and the held reference prevents deallocation. `writelines`
  submits one vectored `writev` with an iovec per line, so a whole batch
  reaches the kernel from the lines' own buffers with no join copy.
- **Reads are zero-copy as well**: the kernel writes directly into a freshly
  allocated, uninitialised Python `bytes` that only the pending map
  references. A full read returns that object to the caller with no second
  `memcpy`; a short read copies only the bytes actually read. Mutating the
  buffer is safe because nothing has observed the object before completion.
- Paths for `openat`/`statx`/`mkdirat` and similar are kept as owned
  `CString`s.

The kernel therefore only touches memory that outlives the operation,
whether Rust-owned or a reference-pinned Python `bytes`.

Two further read-path details. Reads of **1 MB or more** are submitted with
`IOSQE_ASYNC`, handing them to kernel io-wq workers, so warm page-cache
copies run on multiple cores instead of serialising inline on the loop
thread (the same parallelism a thread pool gets, without the threads). When
an awaiting task is **cancelled**, the driver submits `ASYNC_CANCEL` for the
in-flight operation so its ring slot frees promptly rather than whenever the
I/O would have finished.

## Linked open-then-I/O chains

The whole-file helpers (`Path.read_bytes`, `write_bytes`, `read_text`,
`write_text`) go one step beyond batching: the ring registers a sparse
**fixed-file table** at creation, and the helper submits `OPENAT` (directly
into a table slot) linked with `IOSQE_IO_LINK` to the first `READ`/`WRITE`
on that slot, so *open + I/O* reach the kernel as **one submission** and
complete in one wakeup instead of three round-trips. The close is folded
into completion handling (fire-and-forget for reads; awaited for writes so
close errors surface), and the slot returns to the pool only after its close
completes. If the table is exhausted or the kernel lacks sparse
registration, the helpers fall back to the ordinary open/read/close path.

## Positional I/O and the file objects

io_uring reads and writes are *positional* (offset-based). The file objects
track a logical offset themselves and add a small read-ahead buffer, which
provides ordinary `read`/`readline` semantics on top of positioned I/O. A
short read on a regular file is treated as EOF, avoiding a redundant system
call to confirm it.

## Windows and the fallback

If a ring cannot be created (non-Linux, an old kernel, or an environment
that blocks the system calls), urio uses the
[thread-pool backend](getting-started/backends.md) instead, the same
approach aiofiles takes. Detection happens once, by attempting to create a
ring, so callers never branch on platform.

Windows has its own native path: the opt-in
[IOCP backend](getting-started/backends.md#iocp-io-completion-ports-windows-opt-in)
mirrors the Linux design with overlapped I/O and an I/O completion port,
reaping completions on the event-loop thread through the port that asyncio's
`ProactorEventLoop` already owns, the Windows analogue of the eventfd
bridge. It is enabled with `URIO_WINDOWS_IOCP=1`; otherwise Windows uses the
thread backend.

## Scope of io_uring opcodes

The `uring` backend submits: read, write, writev, fsync/fdatasync, openat,
close, statx, mkdirat, unlinkat, renameat, symlinkat, linkat, async-cancel,
and ftruncate (an opcode since kernel 6.9; on older kernels the completion
reports it unsupported and urio routes that call to the executor for the
rest of the process). Operations without an in-scope opcode (directory
listing, `chmod`, `readlink`, `realpath`, `utime`) run on the thread-pool
path even under the `uring` backend.
