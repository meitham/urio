# Backends

urio ships interchangeable backends behind a single API.

## `uring` — io_uring (Linux)

Submits reads, writes, opens, closes, `fsync`, `statx`, `ftruncate`, and
several path mutations (`mkdir`, `unlink`, `rename`, `symlink`, `link`) to
**io_uring**, and resolves completions on the event loop via an eventfd. No
thread is occupied per in-flight operation. This is the default whenever a
working io_uring instance can be created.

Operations without an in-scope io_uring opcode (directory listing, `chmod`,
`readlink`, `realpath`, `utime`) use the thread-pool path even under the
`uring` backend.

## `iocp` — I/O completion ports (Windows, opt-in)

The Windows analogue of the io_uring backend: files are opened with
overlapped I/O and associated with an **I/O completion port**; reads, writes,
opens, and closes are submitted asynchronously, and both directions are
zero-copy (the kernel reads from the caller's Python `bytes` on writes, and
fills the returned `bytes` on reads). Completions are reaped on the
event-loop thread through the IOCP that asyncio's `ProactorEventLoop` already
owns, so no poller thread and no cross-thread wakeup are involved. On a
non-`ProactorEventLoop` loop, a single poller thread reaps completions in
batches instead. One port serves any number of in-flight operations.
Everything else (stat, mkdir, glob, and so on) uses the thread-pool path.

!!! note "Opt-in; binary reads are the trade-off"
    The IOCP backend is validated on Windows 10/11 (CPython 3.14, GIL and
    free-threaded): the full test suite, including the differential suite
    against the stdlib `io` oracles, and a concurrency/cancellation stress
    test pass. Against aiofiles it is at parity or faster on buffered writes,
    faster on durable writes (1.1–1.7×), and faster on large text (1.3–1.9×).
    The remaining loss is **binary reads** (down to about 0.25× at 8 MB): a
    warm overlapped `ReadFile` completes synchronously, so the kernel copies
    inline on the single loop thread, while a thread pool spreads that copy
    across cores. Because binary reads decide many warm workloads, IOCP stays
    off by default: enable it with `URIO_WINDOWS_IOCP=1`; Windows otherwise
    uses the thread-pool backend. Full numbers are in
    [the benchmarks](../benchmarks.md#windows-iocp-backend).

## `thread` — the portable fallback

Runs the blocking system calls in the event loop's default executor, the
same strategy [aiofiles](https://github.com/Tinche/aiofiles) uses. This is
what urio selects on macOS, older Linux kernels, environments where io_uring
is unavailable, and on Windows unless IOCP is opted into. In those
environments urio behaves like aiofiles, so existing code keeps working
unchanged.

## Selecting a backend

urio auto-detects at runtime by attempting to create a ring. Intervening is
rarely necessary, but possible:

```python
import urio

urio.set_backend('auto')  # default: fastest available, else thread
urio.set_backend('thread')  # force the portable fallback
urio.set_backend('uring')  # require io_uring (raises if unavailable)
urio.set_backend('iocp')  # require IOCP (raises unless on Windows with
# URIO_WINDOWS_IOCP=1)

print(urio.get_backend().name)  # 'uring', 'iocp', or 'thread'
```

Or via the environment, without touching code:

```bash
URIO_BACKEND=thread python app.py
```

On Windows, setting `URIO_WINDOWS_IOCP=1` is sufficient on its own:
auto-detection then selects the IOCP backend. Without it, `'iocp'` is refused
even when requested explicitly; the variable is the opt-in.

| Source | Precedence |
|---|---|
| `urio.set_backend(...)` | highest |
| `URIO_BACKEND` env var | middle |
| auto-detection | default |

!!! tip
    Forcing `thread` is useful for A/B benchmarking and for reproducing the
    behaviour of a deployment target that lacks native async I/O.

## FAQ: which backend, when?

### When does io_uring beat the thread pool?

When the cost is per-operation or waiting, not copying bytes:

- **Many small or medium files at once.** Concurrent 4–64 KB reads run
  2.6–3.4× and buffered writes 3.4–5.2× faster than a thread pool: a pool
  pays a thread round-trip per operation and saturates under high fan-out,
  while urio batches everything queued in an event-loop tick into a single
  `io_uring_enter`, so 500 concurrent open+write+close operations issue
  roughly 15 system calls rather than 1500. Typical workloads: cache shards,
  log segments, thumbnails, per-message spools.
- **fsync-heavy durability.** One executor `fsync` per file versus a batched
  `IORING_OP_FSYNC` sweep: 4.1–5.9× on many small files. This advantage also
  survives ZFS, where the higher per-commit cost widens the batching gap.
  Typical workloads: write-ahead logs and message queues.
- **Few or slow cores.** A thread pool's advantage is spreading copies across
  cores; on a 1–4 core VM or a single-board computer there is little to
  spread, and the contention remains. On a 4-core Raspberry Pi the read
  advantage grew to 2.2–2.9×. The GIL has the same effect: a GIL-bound pool
  cannot parallelise its copies.
- **Latency-bound local storage.** io_uring hides latency, not bandwidth:
  cold caches, disks busy with other traffic.

Numbers and caveats: [the benchmarks](../benchmarks.md).

### When is the thread pool the better choice?

A few big files, a warm cache, and many cores. A warm multi-megabyte read is
a `memcpy` into Python `bytes`, and a pool runs N of those on N cores while
one loop thread cannot; concurrent 8 MB warm reads measure around 0.5–0.8×
even with zero-copy reads. The effect is strongest on free-threaded (no-GIL)
Python, where the pool gains real parallelism on all warm reads. The thread
backend matches aiofiles within noise everywhere, so falling back to it
costs nothing.

### What about network filesystems (NFS, SMB/CIFS, 9p, FUSE)?

Kernels mostly lack an asynchronous submission path for network filesystems,
so io_uring hands each operation to kernel worker threads (io-wq), which is
again a thread pool, inside the kernel. Measured, that does not remove the
fan-out advantage; it relocates it. On loopback NFSv4.2 and SMB 3.1.1
mounts, concurrent small-file reads (200 × 64 KB) still run 2–4.5× faster
than aiofiles, warm and cold, under the GIL and free-threaded, on 4- and
8-core hosts, because batched submission still hides the per-file network
round-trip. Large-file reads (20 × 8 MB) measure 0.9–1.1×, parity within
noise, and durable writes keep the batching advantage (cold `write+fsync`
1.5–3.1× on SMB). The one negative report is a 9p mount (about 0.7× on
reads, 0.5× on `exists()`); unusual transports vary, so run
`benchmarks/bench_vs_aiofiles.py` on the target mount, and use
`set_backend('thread')` if io_uring loses there.

### Do I have to choose?

Usually not. `auto` picks io_uring when it can and the thread pool otherwise,
and the thread pool is at aiofiles parity, so the default is safe on every
platform. Reach for `set_backend()` when the workload sits squarely in one of
the cases above (for example, force `thread` for a warm-read-heavy service on
a many-core no-GIL build).

### Does io_uring use more memory than the thread pool?

Per operation, no. A read allocates exactly one `bytes` of the requested size
on either backend (io_uring's is filled by the kernel directly), and io_uring
writes hold only a reference to the existing `bytes`. Fixed costs favour
io_uring: the ring is tens of kilobytes, while an executor pool carries up to
about 32 OS threads and their stacks.

The one case where io_uring's high-water mark is higher is **large concurrent
reads**. Read buffers are allocated at submission, and the ring admits up to
its depth (512 by default, tunable via `URIO_RING_ENTRIES`) operations in
flight, where a thread pool implicitly throttles at about 32 running
operations; 1000 concurrent 8 MB reads can therefore hold about 4 GB of live
buffers under io_uring versus about 256 MB under the pool. Those in-flight
operations are also what makes io_uring fast, so this is a queue-depth
trade-off, not waste. If it exceeds the memory budget, bound the fan-out:

```python
sem = asyncio.Semaphore(32)


async def read_one(path):
    async with sem:
        return await urio.Path(path).read_bytes()
```

### What about Windows?

Windows defaults to the thread pool; set `URIO_WINDOWS_IOCP=1` to opt into
the [IOCP backend](#iocp-io-completion-ports-windows-opt-in). Worth it for
write-heavy, fsync-heavy, or large-text workloads (durable writes 1.1–1.7×,
large text 1.3–1.9×); skip it if concurrent binary reads dominate, since
those stay on one core under IOCP and lose to the pool.
