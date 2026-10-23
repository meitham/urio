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

Prebuilt `abi3` wheels cover regular CPython 3.12 and newer, including 3.15.
Free-threaded CPython 3.14t and 3.15t have separate wheels:

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

The [benchmark](benchmarks/bench_vs_aiofiles.py) compares concurrent file
workloads with aiofiles. Each timing includes opening, operating on, and
closing a batch of files. Results are batch wall-clock times, not latency
per file.

In [October 2026 validation runs](benchmarks/data/validation-2026-10-23/README.md)
using the current source, io_uring was faster than aiofiles for 4 KB and
64 KB binary batches on one Linux/ZFS host in both contender orders. It was
slower for 8 MB warm binary reads. On a Mac, the thread backend and
aiofiles were close for small reads; large buffered writes varied widely
between runs. These are observations on those machines, not expected
speedups on other storage.

The [benchmark documentation](docs/benchmarks.md) explains the workloads,
shows the archived Linux and Windows results, and describes their limits.
All [raw samples](benchmarks/data/README.md) are available for inspection.

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
