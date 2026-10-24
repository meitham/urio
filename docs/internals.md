# How it works

The public API and its `asyncio` integration are written in Python. A
PyO3 extension implements the Linux io_uring ring and the optional Windows
IOCP backend. The [backend guide](getting-started/backends.md) describes
which implementation is selected.

## Linux: completions and the event loop

The first operation on an event loop creates a ring. The driver gives each
submission an identifier and keeps an `asyncio.Future` for its result.
The ring registers an eventfd, which the event loop watches with
`loop.add_reader()`. When the kernel posts a completion, the eventfd wakes
the loop; the driver reads the result and resolves the corresponding future.

```text
coroutine → submission queue → kernel
    ↑                         |
  future ← driver ← eventfd ← completion
```

Submissions made during one event-loop turn are flushed together. The
driver also limits the number of outstanding operations to the ring depth.
`URIO_RING_ENTRIES` changes that depth; the default is 512.

Operations without a supported io_uring path use the event loop's
executor. This includes directory listing, `chmod`, `readlink`,
`realpath`, and `utime`. `ftruncate` uses io_uring on kernels that
support it and the executor otherwise.

## Buffers and cancellation

An io_uring submission may refer to memory after the submitting function
returns. The ring therefore retains paths and buffers until their
completions have been collected. For writes it retains the caller's
`bytes` object. For reads it allocates a `bytes` object for the kernel to
fill; a full read can return that object directly. A short read copies
only the bytes received.

When a task awaiting an operation is cancelled, the driver requests
cancellation from the kernel and still collects the operation's eventual
completion. A successful open whose caller is gone is closed by the
driver. A fixed-file slot is made available again only after its close
completes.

Large reads (at least 1 MiB) are submitted with `IOSQE_ASYNC`. This
allows the kernel to carry out their page-cache copies in io-wq workers
instead of on the event-loop thread. The [benchmarks](benchmarks.md)
show how this affects warm reads.

## Whole-file operations

On the io_uring backend, `Path.read_bytes()` and `Path.write_bytes()`
can submit an open linked to the first read or write using a fixed-file
slot. If the table has no free slot, or the kernel cannot register it,
the operation uses an ordinary open/read-or-write/close sequence.
The text helpers encode or decode around the same binary operation.

## File positions

io_uring reads and writes use explicit offsets. A urio file object keeps
its own position and a read-ahead buffer for line reading. A short,
nonempty read is not assumed to be end of file; the file object reads
again if more bytes were requested. An empty read marks end of file.

Text files add an incremental codec and newline translation. Their
`tell()` value is exact at an unbuffered boundary, but can be
approximate while decoded text remains buffered.

## Windows and the thread backend

With `URIO_WINDOWS_IOCP=1`, file opens, reads, writes, and closes use
Windows overlapped I/O. On a ProactorEventLoop, completions are collected
through the loop's own completion port. Other loop types use a poller
thread that delivers batches to the loop. Other filesystem operations
use the executor.

The thread backend runs blocking file operations in the event loop's
default executor. It also waits for cancelled executor jobs that still
use a file before closing it. This prevents a later open from reusing
the descriptor while an earlier job is still running.
