# File objects

`urio.open()` returns a text or binary file object. I/O methods are
coroutines; call them with `await`. Properties and methods that inspect
the object, including `tell()` and `fileno()`, are synchronous.

Both file types support `async with` and asynchronous iteration over lines.

## Binary files

`AsyncBufferedFile` is returned when the mode contains `b`. It reads and
writes bytes and keeps its own file position.

### Reading

- `await read(size=-1)` returns up to `size` bytes. With no size, it reads
  to the end of the file. It returns `b''` at end of file.
- `await readall()` is equivalent to `await read(-1)`.
- `await readinto(buffer)` fills a writable buffer and returns the number
  of bytes read.
- `await readline(size=-1)` returns one line, including its newline if
  present. A nonnegative size limits the number of bytes.
- `await readlines(hint=-1)` returns a list of lines. A nonnegative hint
  stops the read after approximately that many bytes.

```python
async with urio.open('data.bin', 'rb') as file:
    header = await file.read(16)
    remainder = await file.read()
```

### Writing

`await write(data)` writes a bytes-like object and returns the number of
bytes written. `await writelines(lines)` writes an iterable of bytes-like
objects in order. Neither method adds line separators.

### Position and size

`tell()` returns the current byte offset. `await seek(offset,
whence=os.SEEK_SET)` changes the position and returns the new offset.
`whence` may be `os.SEEK_SET`, `os.SEEK_CUR`, or `os.SEEK_END`.
A negative resulting position raises `OSError`.

`await truncate(size=None)` changes the file size and returns the new size.
If `size` is omitted, the current position is used.

### Flushing and closing

`await flush()` does nothing: urio has no userspace write buffer.
`await fsync()` requests that file data and metadata be flushed to storage.
`await fdatasync()` requests a data flush; where `fdatasync` is unavailable,
it uses `fsync`. `await close()` closes the file and may be called more than
once.

The following are synchronous: `name`, `mode`, `closed`, `fileno()`,
`readable()`, `writable()`, and `seekable()`.

## Text files

`AsyncTextFile` is returned unless the mode contains `b`. It reads and
writes `str`, using an incremental codec so characters split across
internal reads are decoded correctly. It supports `read()`,
`readline()`, `readlines()`, `write()`, `writelines()`, `seek()`,
`tell()`, `flush()`, `fsync()`, `fdatasync()`, and `close()`.

```python
async with urio.open('notes.txt', encoding='utf-8') as file:
    async for line in file:
        print(line.rstrip())
```

`write()` returns the number of characters in the input, before encoding
or newline translation. `writelines()` does not insert separators.

### Newlines

The `newline` argument to `urio.open()` controls line handling:

| Value | Reading | Writing |
|---|---|---|
| `None` | Recognize `\n`, `\r`, and `\r\n` and return `\n`. | Translate `\n` to `os.linesep`. |
| `''` | Recognize each ending without translating it. | Do not translate. |
| `'\n'` | Recognize `\n` only. | Do not translate. |
| `'\r'` or `'\r\n'` | Recognize only the specified ending. | Translate `\n` to that ending. |

### Seeking in text files

`await seek()` accepts byte offsets and resets the decoder and newline
state. `tell()` returns an exact byte offset when no decoded characters
are buffered. With buffered text, the result can be approximate, especially
after newline translation. Seeking into the middle of a multibyte character
or newline sequence can cause the following read to decode incorrectly.

`seekable()` returns `True`. Text files also expose `encoding` and `errors`
properties.
