# urio

Asynchronous file I/O for Python.

On Linux, urio submits reads and writes through
[io_uring](https://en.wikipedia.org/wiki/Io_uring) and resolves completions on
the event loop, with no thread occupied per operation. On Windows it can do
the same through overlapped I/O and an I/O completion port (opt-in, see
below). Everywhere else (macOS, older kernels, environments that block the
system calls) it falls back to a thread pool behind the same API, so one code
path works on every platform.

The API follows [aiofiles](https://github.com/Tinche/aiofiles), which
established async file access for asyncio by running the blocking system
calls in a thread pool; that was the only portable option at the time and
remains the correct fallback. urio keeps that interface (which in turn
mirrors the built-in `open()`), so switching is mostly a change of import.

## Features

- Native asynchronous reads and writes: io_uring on Linux, IOCP on Windows.
- The standard file API: `read`, `readline`, `readlines`, `write`,
  `writelines`, `seek`, `tell`, `truncate`, `flush`, async iteration, text
  and binary modes.
- An async `pathlib`: `urio.Path` with async `stat`, `mkdir`, `unlink`,
  `rename`, `symlink_to`, `read_text`/`write_text`/`read_bytes`/`write_bytes`,
  `iterdir`, `glob`, `walk`, and the pure-path helpers.
- A thread-pool fallback with the same API wherever native async I/O is
  unavailable.
- Runs GIL-free on free-threaded CPython (3.14t and later).

## Requirements

- Python 3.12 or later, any OS.
- Linux 5.6 or later for the io_uring backend.
- Windows 10/11 for the IOCP backend (opt-in via `URIO_WINDOWS_IOCP=1`;
  Windows otherwise uses the thread backend).

Anything else, such as macOS or older kernels, runs the thread backend
automatically.

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
    # Files, in binary and text modes, as with the builtin open().
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
`name`, `suffix`, `parent`, `with_suffix`, and so on) are ordinary synchronous
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
helpers are io_uring submissions; directory listing and metadata changes
use the thread pool.

### Choosing a backend

urio picks the fastest available backend at runtime. To force one:

```python
urio.set_backend('auto')  # default
urio.set_backend('thread')  # force the thread pool
urio.set_backend('uring')  # require io_uring (raises if unavailable)
urio.set_backend('iocp')  # require IOCP (Windows, needs URIO_WINDOWS_IOCP=1)
```

or via the environment: `URIO_BACKEND=thread python app.py`. On Windows,
setting `URIO_WINDOWS_IOCP=1` is sufficient on its own; auto-detection then
selects IOCP.

## How it works

On Linux, a single ring is created per event loop. Its io_uring instance is
registered with an eventfd, and that descriptor is handed to
`loop.add_reader`, so the loop wakes whenever completions are ready. Each
submission maps a `user_data` id to an `asyncio.Future`; on wake-up the driver
drains the eventfd, reaps every completion, and resolves the matching
futures. Everything runs on the loop thread, with no executor and no extra
threads.

On Windows with IOCP enabled, files are opened for overlapped I/O and
associated with the completion port that asyncio's `ProactorEventLoop`
already owns, so completions are again reaped on the loop thread.

Whatever the kernel touches is kept alive until the completion is reaped:
reads land in a buffer the kernel fills directly, and writes submit against
the caller's immutable Python `bytes` object (held by reference), so neither
direction copies. This also avoids the main io_uring hazard: the kernel
using a buffer that Python has freed or moved.

## Benchmarks

`benchmarks/bench_vs_aiofiles.py` runs the full cartesian product of
{buffered write, write+fsync, read} × {binary, text} × {2000×4 KB, 500×64 KB,
100×1 MB, 20×8 MB}, plus a streaming read of a 128 MB file. The cache is
warmed for every contender, each contender writes to its own files (so none
inherits another's writeback pressure), and the median of N runs is reported
with the [min–max] range. The Linux charts below are medians on CPython
3.14.6, measured on a 4-core Intel i5-6500, 32 GB RAM, ext4 on a SATA HDD
(the Windows numbers are from a separate 8-core machine; full specifications
in [the benchmarks doc](docs/benchmarks.md)). Treat the numbers as indicative
and run the benchmark on your own hardware. Each bar is one
`operation × mode × size`; colour is the file size, and a bar past the 1.0×
line means urio was faster than aiofiles.

**Linux, io_uring.** Buffered writes are faster across sizes: submission
batching for small files (one `io_uring_enter` per loop tick instead of one
system call per operation), zero-copy submission for large ones. Reads are
faster at small and medium sizes; reads are zero-copy (the kernel fills the
returned `bytes` directly) and large reads fan out to io-wq workers so their
copies run on multiple cores. Durable (`fsync`) writes converge on disk
bandwidth and remain competitive:

![Linux io_uring speedup vs aiofiles](docs/assets/bench-linux.svg)

**Windows, IOCP.** Asynchronous I/O via overlapped I/O and a completion port,
reaped on the event-loop thread, with zero-copy writes and reads. Buffered
writes are at parity or faster, durable writes and large text are faster;
binary reads remain slower, because a warm `ReadFile` completes synchronously
and the kernel copies inline on the loop thread. The full test suite and a
concurrency/cancellation stress test pass on Windows CPython 3.14, both GIL
and free-threaded builds:

![Windows IOCP speedup vs aiofiles](docs/assets/bench-windows.svg)

**Free-threaded Python (no-GIL).** The native module declares
`gil_used = false`, and urio has the same thread-pool mode aiofiles does, so
nothing is lost on no-GIL builds: the thread backend matches aiofiles for
warm reads, and io_uring/IOCP keep their advantage for writes, `fsync`, and
latency-bound I/O. The chart isolates how the native backends' warm-read
margin changes as the thread pools gain real parallelism: on Linux the
small-read margin narrows but remains ahead at every size, while on Windows
the IOCP large-text-read advantage drops below parity without the GIL:

![Free-threading GIL vs no-GIL — io_uring and IOCP](docs/assets/bench-nogil.svg)

Summary against aiofiles (medians, CPython 3.14):

- Linux writes are 3.4–5.2× faster at small/medium sizes through submission
  batching; large binary writes use zero-copy submission (1.7× at 1 MB,
  parity at 8 MB). Large text writes remain slower (0.6–0.7×); offloading
  multi-megabyte encodes off the event loop roughly halved that gap in 0.2.0.
- Linux reads are 2.6–3.4× faster at small/medium sizes. With zero-copy reads
  and the io-wq punt, large binary reads reach 0.8× (1 MB) and 0.5× (8 MB)
  under the GIL, and 1.2× at 8 MB without it; large text reads are 1.7–2.0×
  faster. Streaming a warm 128 MB file runs at 6.3 GB/s against aiofiles's
  3.1 (2.0×).
- Durable (`fsync`) writes converge on disk bandwidth and are competitive on
  both io_uring and IOCP. On ZFS every `fsync` forces a ZIL/txg commit, so
  large durable writes are roughly 24–71× slower there; aiofiles pays the
  same cost (speedup stays near 1.0×) and urio's batched `fsync` remains
  faster for many small files. See
  [the benchmarks doc](docs/benchmarks.md#filesystem-impact-ext4-vs-zfs).
- Windows IOCP: buffered writes at parity or faster, durable writes 1.1–1.7×
  faster, large text 1.3–1.9× faster; binary reads are slower (the warm
  `ReadFile` copy runs inline on the loop thread).
- The thread backend matches aiofiles for reads and writes on every OS and in
  both GIL states; it is the same strategy, used wherever io_uring or IOCP is
  unavailable or not selected.
- Network filesystems (measured on loopback NFSv4 and SMB 3.1.1 mounts):
  concurrent small-file reads remain 2–4.5× faster, warm and cold, because
  batched submission hides the per-file round-trip even though the kernel
  hands network-filesystem I/O to worker threads; large-file reads are at
  parity (0.9–1.1×). One report measured a 9p mount slower (about 0.7×), so
  benchmark unusual transports before adopting.

To reproduce: `python benchmarks/bench_vs_aiofiles.py --markdown --json out.json`
(medians and raw samples). Methodology and full tables are in the
[benchmarks docs](docs/benchmarks.md); raw sample data is in `benchmarks/data/`.

## Documentation

[ARCHITECTURE.md](ARCHITECTURE.md) describes the implementation: the
ring/eventfd bridge, the zero-copy rules, linked chains, and the backend
stack. The full site is built with MkDocs Material:

```bash
pip install -e ".[docs]"
mkdocs serve
```

## Scope and design notes

- Metadata operations without an io_uring opcode use the thread pool.
  io_uring covers read, write, fsync/fdatasync, openat, close, statx,
  mkdirat, unlinkat, renameat, symlinkat, and linkat; a few rarer operations
  (directory listing, `chmod`, `readlink`, `realpath`) have no opcode and run
  on the thread pool even under the io_uring backend, behind the same async
  API.
- Text decoding is incremental. Reading a whole text file is one round-trip
  plus a single bulk decode, and sized `read(n)` fetches the whole request in
  one round-trip. Line-by-line iteration decodes incrementally (128 KB per
  refill), so it trails a C `TextIOWrapper` on pure line-streaming
  throughput; read binary and bulk-decode if that is the bottleneck. On
  free-threaded builds the codec runs in the executor and parallelises
  across cores. Text streams are seekable in byte offsets, like
  `io.TextIOWrapper`.

## License

MIT
