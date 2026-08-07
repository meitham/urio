# Quickstart

Everything in urio is `async`. Run the examples below inside an event loop
(`asyncio.run(...)`).

## Writing and reading text

```python
import urio


async def main():
    async with urio.open('notes.txt', 'w') as f:
        await f.write('first line\n')
        await f.write('second line\n')

    async with urio.open('notes.txt') as f:
        content = await f.read()
    print(content)
```

## Iterating over lines

```python
async with urio.open('notes.txt') as f:
    async for line in f:
        print(line.rstrip())
```

## Binary mode

```python
async with urio.open('data.bin', 'wb') as f:
    await f.write(b'\x00\x01\x02\x03')

async with urio.open('data.bin', 'rb') as f:
    await f.seek(2)
    print(await f.read(2))  # b'\x02\x03'
```

## Awaiting open instead of `async with`

Like aiofiles, `urio.open` is both an async context manager and directly
awaitable:

```python
f = await urio.open('notes.txt', 'a')
try:
    await f.write('appended\n')
finally:
    await f.close()
```

## Async pathlib

```python
from urio import Path


async def main():
    p = Path('project') / 'README.md'
    await p.parent.mkdir(parents=True, exist_ok=True)
    await p.write_text('# Project\n')

    print(await p.exists())  # True
    print((await p.stat()).st_size)  # 10
    async for child in p.parent.iterdir():
        print(child.name)
```

## Concurrency — where urio shines

Because the native backends (io_uring, IOCP) don't tie up a thread per
operation, large fan-out workloads scale well:

```python
import asyncio
import urio


async def save(i):
    async with urio.open(f'out/{i}.txt', 'w') as f:
        await f.write(f'record {i}\n')


async def main():
    await asyncio.gather(*(save(i) for i in range(1000)))
```

See the [benchmarks](../benchmarks.md) for what that concurrency buys.
