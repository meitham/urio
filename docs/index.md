# urio

Asynchronous file I/O for Python, backed by Linux
[io_uring](https://en.wikipedia.org/wiki/Io_uring), with a Windows IOCP
backend and a portable thread-pool fallback behind the same API.

```python
import urio


async def main():
    async with urio.open('greeting.txt', 'w') as f:
        await f.write('hello\nworld\n')

    text = await urio.Path('greeting.txt').read_text()
```

## Relationship to aiofiles

[`aiofiles`](https://github.com/Tinche/aiofiles) established the common
interface for asynchronous file access in asyncio: async versions of the
built-in `open()` and file API, with the blocking system calls executed in a
thread pool. That was the only portable implementation strategy at the time
and remains the correct fallback. urio provides the same interface
(`urio.open` mirrors `aiofiles.open`), so most code can switch by changing
the import.

The difference is the implementation. On Linux, urio submits I/O through
io_uring and resolves completions on the event loop via an eventfd; no thread
is occupied per operation. On Windows, an opt-in
[IOCP backend](getting-started/backends.md) does the same through overlapped
I/O and a completion port. Where neither is available (macOS, older kernels,
restricted environments, and Windows by default), urio uses a thread-pool
backend, the same approach as aiofiles.

## Features

- Native asynchronous I/O: io_uring on Linux, IOCP on Windows (opt-in), with
  automatic fallback to a thread pool elsewhere.
- Zero-copy in both directions: writes pass the caller's `bytes` buffer to
  the kernel without an intermediate copy, and reads return the buffer the
  kernel filled.
- The standard file API: `read`, `readline`, `readlines`, `write`,
  `writelines` (one vectored `writev` per batch), `seek`, `tell`, `truncate`,
  `flush`, async iteration, and text and binary modes.
- An async `pathlib`: [`urio.Path`](api/path.md) with async `stat`, `mkdir`,
  `unlink`, `rename`, `symlink_to`,
  `read_text`/`write_text`/`read_bytes`/`write_bytes` (the whole-file helpers
  use a linked open-then-I/O chain, one kernel submission per small file),
  `iterdir`, `glob`, `walk`, and the pure-path helpers.
- A single API on every platform; the fastest available backend is selected
  at runtime.

## Contents

- [Installation](getting-started/installation.md)
- [Quickstart](getting-started/quickstart.md)
- [Backends](getting-started/backends.md) — io_uring, IOCP, thread pool
- [API reference](api/open.md)
- [Benchmarks](benchmarks.md) — measured behaviour across file sizes
- [How it works](internals.md)
