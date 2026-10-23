# Quickstart

File operations are coroutines. Use `await` for reads, writes, and closing
files. An `async with` block closes its file when the block exits:

```python
import asyncio
import urio


async def main():
    async with urio.open('notes.txt', 'w', encoding='utf-8') as file:
        await file.write('first line\n')
        await file.write('second line\n')

    async with urio.open('notes.txt', encoding='utf-8') as file:
        async for line in file:
            print(line.rstrip())


asyncio.run(main())
```

The remaining snippets belong inside an asynchronous function unless
they define one themselves.

Pass `'rb'` or `'wb'` for bytes. The file position can be changed with
`await file.seek(...)`; `file.tell()` is synchronous.

```python
async with urio.open('data.bin', 'wb') as file:
    await file.write(b'\x00\x01\x02\x03')

async with urio.open('data.bin', 'rb') as file:
    await file.seek(2)
    assert await file.read(2) == b'\x02\x03'
```

`urio.open()` can also be awaited directly. In that case, close the file
yourself:

```python
file = await urio.open('notes.txt', 'a', encoding='utf-8')
try:
    await file.write('another line\n')
finally:
    await file.close()
```

## Working with paths

`urio.Path` has synchronous methods for path manipulation and asynchronous
methods for filesystem access:

```python
from urio import Path

path = Path('project') / 'README.md'  # no I/O
await path.parent.mkdir(parents=True, exist_ok=True)
await path.write_text('# Project\n', encoding='utf-8')

print(await path.exists())
print((await path.stat()).st_size)

async for child in path.parent.iterdir():
    print(child.name)
```

## Running operations concurrently

Ordinary `asyncio` tools work with urio's coroutines:

```python
import asyncio
from urio import Path


async def save(number):
    await Path('out', f'{number}.txt').write_text(f'record {number}\n')


async def main():
    await Path('out').mkdir(exist_ok=True)
    await asyncio.gather(*(save(number) for number in range(100)))


asyncio.run(main())
```

Use a semaphore to limit concurrency when many large reads would otherwise
hold too much data in memory. See [backends](backends.md) for how operations
are carried out on each platform.
