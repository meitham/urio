# Backends

urio ships interchangeable backends behind a single API.

## `uring` — genuine async I/O (Linux)

Submits reads, writes, opens, closes, `fsync`, `statx`, `ftruncate`, and
several path mutations (`mkdir`, `unlink`, `rename`, `symlink`, `link`) to
**io_uring**, and resolves completions on the event loop via an eventfd. No
thread is parked per in-flight operation. This is the default whenever a
working io_uring instance can be created.

Operations without an in-scope io_uring opcode (directory listing, `chmod`,
`readlink`, `realpath`, `utime`) transparently use the thread-pool path even
under the `uring` backend.

## `iocp` — genuine async I/O (Windows, opt-in)

The Windows analogue of the io_uring backend: files are opened with overlapped
I/O and associated with an **I/O completion port**; reads, writes, opens, and
closes are submitted asynchronously, and both directions are zero-copy — the
kernel reads straight from the caller's Python `bytes` on writes, and fills
the returned `bytes` directly on reads. Completions are reaped **on the
event-loop thread** through asyncio's `ProactorEventLoop` own IOCP — no poller
thread, no cross-thread wakeup, the Windows analogue of the Linux eventfd
design. (On a non-`ProactorEventLoop` loop it falls back to a single poller
thread that batches completions.) One port serves any number of in-flight
operations, unlike a thread-per-op pool. Everything else (stat, mkdir, glob,
…) inherits the thread-pool path.

!!! note "Opt-in — validated, and binary reads are the trade-off"
    The IOCP backend is validated on Windows 10/11 (CPython 3.14, GIL and
    free-threaded): the full test suite — 190 tests, including the
    differential suite against the stdlib `io` oracles — and a
    concurrency/cancellation stress test pass. Against aiofiles it is at
    parity to winning on buffered writes, wins on durable writes (1.1–1.7×),
    and wins large text outright (1.3–1.9×). The remaining loss is **binary
    reads** (down to ~0.25× at 8 MB): a warm overlapped `ReadFile` completes
    synchronously, so the kernel copies inline on the single loop thread,
    while a thread pool spreads that copy across cores. Because binary reads
    decide many warm workloads, IOCP stays **off by default**: enable it with
    `URIO_WINDOWS_IOCP=1`, and Windows otherwise uses the thread-pool backend.
    Full numbers in [the benchmarks](../benchmarks.md#windows-iocp-backend).

## `thread` — the portable fallback

Runs the blocking syscalls in the event loop's default executor — exactly the
strategy [aiofiles](https://github.com/Tinche/aiofiles) pioneered. This is
what urio uses on macOS, older Linux kernels, locked-down sandboxes where
io_uring is unavailable, and on Windows unless IOCP is opted into. In those
environments urio behaves just like aiofiles, so your code keeps working
unchanged.

## Selecting a backend

urio auto-detects at runtime by actually trying to create a ring. You rarely
need to intervene, but you can:

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

On Windows, setting `URIO_WINDOWS_IOCP=1` is enough on its own: auto-detection
then selects the IOCP backend. Without it, `'iocp'` is refused even when asked
for explicitly — the variable is the opt-in.

| Source | Precedence |
|---|---|
| `urio.set_backend(...)` | highest |
| `URIO_BACKEND` env var | middle |
| auto-detection | default |

!!! tip
    Forcing `thread` is handy for A/B benchmarking and for reproducing the
    behaviour your code will have on a deployment target without native async
    I/O.

## FAQ: which backend, when?

### When does io_uring actually beat the thread pool?

When the cost is per-operation or waiting, not copying bytes:

- **Many small/medium files at once.** The flagship case. Concurrent 4–64 KB
  reads run 2.6–3.4× and buffered writes 3.4–5.2× faster than a thread pool: a
  pool pays a thread round-trip per op and starves under high fan-out, while
  urio batches everything queued in an event-loop tick into a single
  `io_uring_enter` — 500 concurrent open+write+close ops issue ~15 syscalls,
  not 1500. Think cache shards, log segments, thumbnails, per-message spools.
- **fsync-heavy durability.** One executor `fsync` per file versus a batched
  `IORING_OP_FSYNC` sweep: 3.9–5.8× on many small files, and the one win that
  survives ZFS (per-commit cost there *widens* the batching gap). Think
  write-ahead logs and message queues.
- **Few or weak cores.** A thread pool's advantage is spreading copies across
  cores; on a 1–4 core VM or an SBC there is little to spread and you still
  pay the contention. On a 4-core Raspberry Pi the read win *grew* to
  2.2–2.9×. The GIL has the same effect: a GIL-bound pool cannot parallelise
  its copies anyway.
- **Latency-bound local storage.** io_uring hides latency, not bandwidth —
  cold caches, disks busy with other traffic.

Numbers and caveats: [the benchmarks](../benchmarks.md).

### When is the thread pool the better choice?

A few big files, warm cache, plenty of cores. A warm multi-megabyte read is
just a `memcpy` into Python `bytes`, and a pool runs N of those on N cores
while one loop thread cannot — concurrent 8 MB warm reads sit around 0.5–0.8×
even with zero-copy reads. The effect is strongest on free-threaded (no-GIL)
Python, where the unshackled pool catches up on all warm reads. The thread
backend tracks aiofiles within noise everywhere, so falling back to it never
costs you anything.

### What about network filesystems (NFS, SMB/CIFS, 9p, FUSE)?

Kernels mostly lack an asynchronous submission path for network filesystems,
so io_uring punts each op to kernel worker threads (io-wq) — a thread pool
again, just inside the kernel. Measured, that does *not* erase the fan-out
win, it relocates it: on loopback NFSv4.2 and SMB 3.1.1 mounts, concurrent
small-file reads (200 × 64 KB) still run **2–4.5× faster** than aiofiles —
warm and cold, GIL and free-threaded, on 4- and 8-core hosts — because
batched submission still hides the per-file network round-trip. Large-file
reads (20 × 8 MB) sit at 0.9–1.1× (parity within noise), and durable writes
keep the batching win (cold `write+fsync` 1.5–3.1× on SMB). The one negative
report is a 9p mount (~0.7× reads, ~0.5× `exists()`) — exotic transports
vary, so if your data lives on a network mount, run
`benchmarks/bench_vs_aiofiles.py` there; the thread backend is one
`set_backend('thread')` away if it loses.

### Do I actually have to choose?

Usually not. `auto` picks io_uring when it can and the thread pool otherwise,
and the thread pool is at aiofiles parity — so the default is safe on every
platform. Reach for `set_backend()` when you know your workload sits squarely
in one of the cases above (e.g. force `thread` for a warm-read-heavy service
on a many-core no-GIL build).

### Does io_uring use more memory than the thread pool?

Per operation, no — a read allocates exactly one `bytes` of the requested
size on either backend (io_uring's is filled by the kernel directly), and
io_uring writes hold only a reference to your existing `bytes`. Fixed costs
favour io_uring: the ring is tens of kilobytes, while an executor pool
carries up to ~32 OS threads and their stacks.

The one case where io_uring's high-water mark is higher: **large concurrent
reads**. Read buffers are allocated at submission, and the ring admits up to
its depth (512 by default, tunable via `URIO_RING_ENTRIES`) ops in flight,
where a thread pool implicitly throttles at ~32 running ops — so 1000
concurrent 8 MB reads can hold ~4 GB of live buffers under io_uring versus
~256 MB under the pool. Those in-flight ops are also exactly what makes
io_uring fast, so this is a queue-depth trade, not waste. If it exceeds your
RAM budget, bound the fan-out yourself:

```python
sem = asyncio.Semaphore(32)


async def read_one(path):
    async with sem:
        return await urio.Path(path).read_bytes()
```

### What about Windows?

Windows defaults to the thread pool; set `URIO_WINDOWS_IOCP=1` to opt into
the [IOCP backend](#iocp-genuine-async-io-windows-opt-in). Worth it for
write-heavy, fsync-heavy, or large-text workloads (durable writes 1.1–1.7×,
large text 1.3–1.9×); skip it if concurrent binary reads dominate — those
stay on one core under IOCP and lose to the pool.
