# urio

Async file I/O for Python that is actually asynchronous, powered by Linux
[io_uring](https://en.wikipedia.org/wiki/Io_uring) and Rust — with a Windows
IOCP backend and a portable thread-pool fallback behind the same API.

```python
import urio

async def main():
    async with urio.open('greeting.txt', 'w') as f:
        await f.write('hello\nworld\n')

    text = await urio.Path('greeting.txt').read_text()
```

## A nod to aiofiles

[`aiofiles`](https://github.com/Tinche/aiofiles) pioneered ergonomic async
file access for asyncio: async versions of Python's standard `open()` and file
API, running the blocking syscalls in a thread pool — the only portable option
at the time, and still the right fallback. urio keeps the same familiar feel
(`urio.open` mirrors `aiofiles.open`), so moving across is mostly changing the
import.

Where urio differs is underneath. On Linux it submits genuinely asynchronous
I/O through io_uring and resolves completions on the event loop via an
eventfd; no thread is parked per operation. On Windows an opt-in
[IOCP backend](getting-started/backends.md) does the same through overlapped
I/O and a completion port. Where neither is available (macOS, older kernels,
locked-down sandboxes — and Windows by default), urio uses a thread-pool
backend, the same approach aiofiles takes.

## Highlights

- **True async I/O** — io_uring on Linux, IOCP on Windows (opt-in) — with
  automatic, transparent fallback to a thread pool everywhere else.
- **Zero-copy both ways**: writes point the kernel at the caller's `bytes`
  with no per-write copy, and reads hand back the very buffer the kernel
  filled.
- **The familiar file API**: `read`, `readline`, `readlines`, `write`,
  `writelines` (one vectored `writev` per batch), `seek`, `tell`, `truncate`,
  `flush`, async iteration, and both text and binary modes.
- **Async `pathlib`**: [`urio.Path`](api/path.md) with async `stat`, `mkdir`,
  `unlink`, `rename`, `symlink_to`,
  `read_text`/`write_text`/`read_bytes`/`write_bytes` (the whole-file helpers
  ride a linked open→I/O chain — one kernel submission per small file),
  `iterdir`, `glob`, `walk`, and the full set of pure-path helpers.
- **One API, everywhere**: write your code once; urio picks the fastest
  backend available at runtime.

## Where to next

- [Installation](getting-started/installation.md)
- [Quickstart](getting-started/quickstart.md)
- [Backends](getting-started/backends.md) — io_uring, IOCP, thread pool
- [API reference](api/open.md)
- [Benchmarks](benchmarks.md) — what the native backends buy you across file sizes
- [How it works](internals.md)
