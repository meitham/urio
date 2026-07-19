# File objects

[`urio.open`](open.md) returns one of two file objects depending on mode. Both
are async context managers, support async iteration, and expose a
file-object-like surface. Every I/O method is a coroutine and must be `await`ed;
introspection methods (`name`, `mode`, `closed`, `fileno`, `readable`,
`writable`, `seekable`, `tell`) are synchronous.

---

## Binary mode — `AsyncBufferedFile`

Returned for modes containing `b`. Fully positional: `seek`, `tell`, and
`truncate` all work. A read-ahead buffer gives ordinary `read`/`readline`
semantics on top of positioned io_uring reads.

### Reading

#### `await read(size=-1)`
Read and return up to `size` bytes. With `size` omitted or negative, read until
EOF. Returns `b''` at end of file.

```python
async with urio.open('f', 'rb') as f:
    head = await f.read(16)
    rest = await f.read()
```

#### `await readall()`
Read and return all remaining bytes. Equivalent to `read(-1)`.

#### `await readinto(b)`
Read into the pre-allocated writable buffer `b` (e.g. a `bytearray`). Returns the
number of bytes read.

```python
buf = bytearray(64)
n = await f.readinto(buf)
```

#### `await readline(size=-1)`
Read and return one line including the trailing `\n` (the last line may have
none). If `size >= 0`, return at most `size` bytes.

#### `await readlines(hint=-1)`
Read and return a list of all lines. If `hint >= 0`, stop once roughly `hint`
bytes have been read.

#### Async iteration
Iterate line by line:

```python
async with urio.open('f', 'rb') as f:
    async for line in f:
        process(line)
```

### Writing

#### `await write(data)`
Write the bytes-like `data` at the current position; return the number of bytes
written. Passing a `str` raises `TypeError`.

#### `await writelines(lines)`
Write an iterable of bytes-like objects in order.

### Positioning

#### `tell()` *(sync)*
Return the current byte offset.

#### `await seek(offset, whence=os.SEEK_SET)`
Move the position. `whence` is `SEEK_SET` (absolute), `SEEK_CUR` (relative), or
`SEEK_END` (from end, which statx's the file for its size). Returns the new
absolute position. A resulting negative position raises `OSError`.

#### `await truncate(size=None)`
Truncate the file to `size` bytes (default: the current position). Returns the
new size.

### Durability

#### `await flush()`
No-op for compatibility — writes go straight to the kernel, so there is no
userspace buffer to flush.

#### `await fsync()`
Flush file data **and** metadata to disk (`fsync(2)`).

#### `await fdatasync()`
Flush file data to disk, skipping non-essential metadata (`fdatasync(2)`).

### Lifecycle & introspection

| Member | Kind | Description |
|---|---|---|
| `await close()` | coroutine | Close the file. Idempotent. |
| `name` | property | The path the file was opened with. |
| `mode` | property | The mode string. |
| `closed` | property | `True` once closed. |
| `fileno()` | sync | Underlying OS file descriptor. |
| `readable()` | sync | Whether reads are allowed. |
| `writable()` | sync | Whether writes are allowed. |
| `seekable()` | sync | Always `True` for binary files. |

---

## Text mode — `AsyncTextFile`

Returned for text modes (the default). Wraps a binary file with an incremental
codec and universal-newline handling. Forward reading and writing are fully
supported.

It offers the same reading and writing coroutines as the binary file —
`read`, `readline`, `readlines`, async iteration, `write`, `writelines` — but
operating on `str`:

```python
async with urio.open('f.txt', encoding='utf-8') as f:
    async for line in f:        # str lines
        print(line.rstrip())
```

### Newline handling

`newline` follows the semantics of the builtin `open`:

| `newline` | On read | On write |
|---|---|---|
| `None` (default) | `\r\n`, `\r`, `\n` are all translated to `\n` | `\n` is translated to `os.linesep` |
| `''` | line endings recognised but **not** translated | no translation |
| `'\n'` | no translation | no translation |
| `'\r'` / `'\r\n'` | that terminator is recognised | `\n` is translated to it |

### Encoding

`encoding` defaults to the locale preferred encoding; pass an explicit codec for
portable behaviour. `errors` controls the codec error policy (`'strict'` by
default). Multibyte sequences that straddle internal read boundaries are decoded
correctly via an incremental decoder.

!!! note "Performance"
    Reading a whole text file (`read()` with no size) uses a fast path — one
    read-to-EOF round-trip plus a single bulk decode — and performs at parity
    with aiofiles. **Sized** and **streaming** text reads (`read(n)` and chunked
    loops) still decode incrementally in Python, so they trail aiofiles' C
    `TextIOWrapper`; for maximum text throughput read in binary and decode in
    bulk. See the [benchmarks](../benchmarks.md).

### Positioning

Text streams are seekable, in **byte offsets** (like `io.TextIOWrapper`):

- `await seek(offset, whence=SEEK_SET)` — `SEEK_SET`/`SEEK_CUR`/`SEEK_END` are all
  supported; the incremental decoder and newline state are reset. Returns the new
  byte position.
- `tell()` — returns a byte offset: **exact** at a boundary (nothing buffered),
  best-effort mid-buffer under universal newlines (the `\r\n`→`\n` mapping is
  lossy). Seek to a value `tell()` returned to restore an exact position.

Seeking to an arbitrary byte that splits a code point or a `\r\n` pair is allowed
but may mis-decode the following characters — the same caveat as CPython's
`TextIOWrapper` on a non-cookie target.

### Durability & lifecycle

`flush`, `fsync`, `fdatasync`, and `close` delegate to the underlying binary
file. Introspection adds `encoding` and `errors` properties; `seekable()`
returns `False`.
