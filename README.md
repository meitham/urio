# urio

Async file I/O for Python that is actually asynchronous.

On Linux, urio submits reads and writes through
[io_uring](https://en.wikipedia.org/wiki/Io_uring) and resolves completions on
the event loop, with no thread parked per operation. On Windows it can do the
same through overlapped I/O and an I/O completion port (opt-in, see below).
Everywhere else — macOS, older kernels, sandboxes that block the syscalls — it
falls back to a thread pool behind the same API, so one code path works on
every platform.

The API follows [aiofiles](https://github.com/Tinche/aiofiles), which
pioneered ergonomic async file access for asyncio by running the blocking
syscalls in a thread pool — the only portable option at the time, and still
the right fallback. urio keeps that familiar interface (which in turn mirrors
the builtin `open()`), so switching is mostly changing the import.

## What you get

- Real async reads and writes: io_uring on Linux, IOCP on Windows.
- The familiar file API: `read`, `readline`, `readlines`, `write`,
  `writelines`, `seek`, `tell`, `truncate`, `flush`, async iteration, text and
  binary modes.
- An async `pathlib`: `urio.Path` with async `stat`, `mkdir`, `unlink`,
  `rename`, `symlink_to`, `read_text`/`write_text`/`read_bytes`/`write_bytes`,
  `iterdir`, `glob`, `walk`, and the full set of pure-path helpers.
- A thread-pool fallback wherever native async I/O is unavailable, same API.
- Runs GIL-free on free-threaded CPython (3.14t and later).

## Requirements

- Python 3.12+, any OS.
- Linux 5.6+ for the io_uring backend.
- Windows 10/11 for the IOCP backend (opt-in via `URIO_WINDOWS_IOCP=1`;
  Windows otherwise uses the thread backend).

Anything else — macOS, older kernels, locked-down sandboxes — runs the
thread backend automatically.

## Installation

```bash
pip install urio
```

Prebuilt abi3 wheels (one per platform covers every CPython ≥ 3.12):

| Platform | Arch | Backend |
|---|---|---|
| Linux (manylinux + musllinux) | x86_64, aarch64, armv7 | io_uring |
| Windows | x64, x86 | thread pool by default, [IOCP](docs/getting-started/backends.md) opt-in |
| macOS | x86_64, arm64 | thread pool |

Building from source needs a Rust toolchain and
[maturin](https://www.maturin.rs/); the crate compiles on every OS:

```bash
pip install -e ".[dev]"
maturin develop
```

## Usage

```python
import asyncio
import urio


async def main():
    # Files — binary and text modes, just like the builtin open().
    async with urio.open('greeting.txt', 'w') as f:
        await f.write('hello\nworld\n')

    async with urio.open('greeting.txt') as f:
        async for line in f:
            print(line.rstrip())

    # Async pathlib.
    p = urio.Path('greeting.txt')
    print(await p.exists())
    print((await p.stat()).st_size)
    print(await p.read_text())


asyncio.run(main())
```

### Async `pathlib`

`urio.Path` is an async `pathlib.Path`. Pure-path operations (`/` joining,
`name`, `suffix`, `parent`, `with_suffix`, …) are ordinary synchronous
properties; everything that touches the filesystem is `async` and goes through
the active backend:

```python
base = urio.Path('/tmp/project')
await base.mkdir(parents=True, exist_ok=True)

cfg = base / 'config.toml'
await cfg.write_text('name = "urio"\n')
print(await cfg.read_text())
print(await cfg.exists(), await cfg.is_file(), (await cfg.stat()).st_size)

async for child in base.iterdir():
    print(child.name)
```

On Linux, `stat`/`mkdir`/`unlink`/`rename`/`symlink_to` and the read/write
helpers are genuine io_uring submissions; directory listing and metadata tweaks
use the thread pool.

### Choosing a backend

urio picks the fastest available backend at runtime. You can force one:

```python
urio.set_backend('auto')  # default
urio.set_backend('thread')  # force the thread pool
urio.set_backend('uring')  # require io_uring (raises if unavailable)
urio.set_backend('iocp')  # require IOCP (Windows, needs URIO_WINDOWS_IOCP=1)
```

or via the environment: `URIO_BACKEND=thread python app.py`. On Windows,
setting `URIO_WINDOWS_IOCP=1` is enough on its own — auto-detection then
selects IOCP.

## How it works

On Linux, a single ring is created per event loop. Its io_uring instance is
registered with an eventfd, and that fd is handed to `loop.add_reader`, so the
loop wakes whenever completions are ready. Each submission maps a `user_data`
id to an `asyncio.Future`; on wake-up the driver drains the eventfd, reaps
every completion, and resolves the matching futures. Everything runs on the
loop thread — no executor, no extra threads.

On Windows with IOCP enabled, files are opened for overlapped I/O and
associated with the completion port that asyncio's `ProactorEventLoop` already
owns, so completions are again reaped on the loop thread. Same shape, native
Windows machinery.

Whatever the kernel touches is kept alive until the completion is reaped:
reads land in a buffer the kernel fills directly, and writes submit against
the caller's immutable Python `bytes` object (held by reference), so neither
direction copies. This also side-steps the hardest io_uring footgun: the
kernel using a buffer that Python has freed or moved.

## Benchmarks

`benchmarks/bench_vs_aiofiles.py` runs the full cartesian product of
{buffered write, write+fsync, read} × {binary, text} × {2000×4 KB, 500×64 KB,
100×1 MB, 20×8 MB}, plus a streaming read of a 128 MB file. It tries hard to
be fair: the cache is warmed for every contender, each contender writes to its
own files (so nobody inherits another's writeback pressure), and it reports
the median of N runs with the [min–max] range. The Linux charts below are
medians on CPython 3.14.6, measured on a 4-core Intel i5-6500, 32 GB RAM, ext4
on a SATA HDD (the Windows numbers are from a separate 8-core box; full specs
in [the benchmarks doc](docs/benchmarks.md)). Treat them as indicative and run
it on your hardware. Each bar is one `operation × mode × size`; colour is the
file size, and a bar past the 1.0× line means urio beat aiofiles.

**Linux, io_uring.** Buffered writes win across sizes: submission batching for
small files (one `io_uring_enter` per loop tick instead of one syscall per
op), zero-copy submission for large ones. Reads win small/medium and hold up
at large sizes too — reads are zero-copy (the kernel fills the returned
`bytes` directly) and big reads fan out to io-wq workers so their copies run
on multiple cores. Durable (`fsync`) writes converge on disk bandwidth and
stay competitive:

![Linux io_uring speedup vs aiofiles](docs/assets/bench-linux.svg)

**Windows, IOCP.** Real async I/O via overlapped I/O and a completion port,
reaped on the event-loop thread, with zero-copy writes and reads. Buffered
writes are at parity to winning, durable writes win, large text wins outright
(1.3–1.9×). Binary reads still lose: a warm `ReadFile` completes
synchronously, so the kernel copies inline on the loop thread. The full test
suite and a concurrency/cancellation stress test pass on Windows CPython 3.14,
both GIL and free-threaded builds:

![Windows IOCP speedup vs aiofiles](docs/assets/bench-windows.svg)

**Free-threaded Python (no-GIL).** The native module declares
`gil_used = false`, and urio has the same thread-pool mode aiofiles does — so
on no-GIL builds you lose nothing: use the thread backend for warm reads
(parity with aiofiles), and io_uring/IOCP for writes, `fsync`, and latency,
where the batching wins persist. The chart isolates the native backends'
warm-read edge as the now-unshackled thread pools catch up: on Linux the
small-read edge narrows but stays a win at every size, while on Windows the
IOCP large-text-read wins drop below parity without the GIL:

![Free-threading GIL vs no-GIL — io_uring and IOCP](docs/assets/bench-nogil.svg)

Summary vs aiofiles (medians, CPython 3.14):

- Linux writes win at small/medium sizes through submission batching
  (3.4–5.2×); large binary writes ride zero-copy submission (1.7× at 1 MB,
  parity at 8 MB). Large text writes still trail (0.6–0.7×), though offloading
  multi-megabyte encodes off the loop roughly halved the gap in 0.2.0.
- Linux reads win small/medium (2.6–3.4×); with zero-copy reads plus the
  io-wq punt, large binary sits at 0.8× (1 MB) and 0.5× (8 MB) under the GIL —
  and flips to a 1.2× win at 8 MB without it — while large text reads win
  outright (1.7–2.0×). Warm streaming is a clear win — binary 6.3 GB/s vs
  aiofiles's 3.1 (2.0×).
- Durable (`fsync`) writes converge on disk bandwidth and are competitive on
  both io_uring and IOCP. On ZFS every `fsync` forces a ZIL/txg commit, so
  large durable writes get ~24–71× slower — but aiofiles pays the same tax
  (speedup ~1.0×) and urio's batched `fsync` still wins on many small files.
  See [the benchmarks doc](docs/benchmarks.md#filesystem-impact-ext4-vs-zfs).
- Windows IOCP: writes parity to winning, durable writes win (1.1–1.7×), large
  text wins (1.3–1.9×); binary reads lose (the warm `ReadFile` copy runs
  inline on the loop thread).
- The thread backend tracks aiofiles on both reads and writes, on every OS and
  in both GIL states — same strategy, used wherever io_uring or IOCP is
  unavailable or not selected.
- Network filesystems (measured on loopback NFSv4 and SMB 3.1.1 mounts): the
  small-file fan-out read win survives — 2–4.5× vs aiofiles, warm and cold —
  because batching still hides per-file round-trips even though the kernel
  punts network-FS I/O to worker threads; large-file reads drop to parity
  (0.9–1.1×). A 9p mount was reported slower (~0.7×), so benchmark unusual
  transports before adopting.

Run it yourself: `python benchmarks/bench_vs_aiofiles.py --markdown --json out.json`
(medians + raw samples). Methodology and full tables are in the
[benchmarks docs](docs/benchmarks.md); raw sample data lives in `benchmarks/data/`.

## Documentation

[ARCHITECTURE.md](ARCHITECTURE.md) is the code-reader's tour: the ring/eventfd
bridge, the zero-copy rules, linked chains, and the backend stack. The full
site is built with MkDocs Material:

```bash
pip install -e ".[docs]"
mkdocs serve
```

## Scope & design notes

- Metadata ops without an io_uring opcode use the thread pool. io_uring covers
  the hot path — read, write, fsync/fdatasync, openat, close, statx, mkdirat,
  unlinkat, renameat, symlinkat, linkat — while a few rarer operations
  (directory listing, `chmod`, `readlink`, `realpath`) have no opcode and run
  on the thread pool even under the io_uring backend, behind the same async
  API.
- Text decoding is incremental. Reading a whole text file is one round-trip
  plus a single bulk decode, and sized `read(n)` fetches the whole request in
  one round-trip. Line-by-line iteration decodes incrementally (128 KB per
  refill), so it trails a C `TextIOWrapper` on pure line-streaming throughput;
  read binary and bulk-decode if that is your bottleneck. On free-threaded
  builds the codec runs in the executor so it parallelises across cores. Text
  streams are seekable in byte offsets, like `io.TextIOWrapper`.

## License

MIT
