# urio

urio provides asynchronous file I/O for `asyncio`. Its file objects have
methods like those returned by Python's built-in `open()`, and `urio.Path`
provides asynchronous filesystem operations alongside the usual path methods.

```python
import asyncio
import urio


async def main():
    async with urio.open('notes.txt', 'w', encoding='utf-8') as file:
        await file.write('A note\n')

    text = await urio.Path('notes.txt').read_text(encoding='utf-8')
    print(text)


asyncio.run(main())
```

On Linux, urio uses io_uring when the kernel permits it. On other systems it
uses the event loop's thread pool. Windows also has an optional IOCP backend.
The [backend guide](getting-started/backends.md) describes the selection and
its limitations.

Start with [installation](getting-started/installation.md) and the
[quickstart](getting-started/quickstart.md). The [API reference](api/open.md)
covers file objects, paths, and backend control. See [benchmarks](benchmarks.md)
for measurements and [how it works](internals.md) for implementation details.
