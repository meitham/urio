# Opening files

## `urio.open(path, mode='r', *, encoding=None, errors=None, newline=None)`

Open a file and return an object that can be used as an asynchronous context
manager or awaited directly. It yields an [`AsyncTextFile`](files.md#text-files)
in text mode and an [`AsyncBufferedFile`](files.md#binary-files) in binary mode.

```python
async with urio.open('notes.txt', 'w', encoding='utf-8') as file:
    await file.write('hello\n')
```

The context manager closes the file when the block exits. If you await
`urio.open()` directly, call `await file.close()` when finished:

```python
file = await urio.open('notes.txt')
try:
    text = await file.read()
finally:
    await file.close()
```

`path` may be a string, bytes, or an object implementing `os.PathLike`,
including `urio.Path`. The default mode, `'r'`, opens an existing file for
text reading.

### Modes

| Character | Effect |
|---|---|
| `r` | Read an existing file. |
| `w` | Write, creating the file if needed and truncating it otherwise. |
| `a` | Append, creating the file if needed. |
| `x` | Create a new file; raise `FileExistsError` if it exists. |
| `b` | Return bytes. |
| `t` | Return text; this is the default. |
| `+` | Allow both reading and writing. |

A mode must contain exactly one of `r`, `w`, `a`, or `x`. `b` and `t`
cannot be combined. Invalid modes raise `ValueError`.

### Text options

`encoding` names the text codec. It defaults to the locale's preferred
encoding; specify it when files need a known encoding. `errors` is the codec
error handler and defaults to `'strict'`. `newline` controls line ending
recognition and translation; see [text files](files.md#text-files).
These options are ignored in binary mode.

Errors from opening the path, such as `FileNotFoundError` or
`PermissionError`, are passed through from the operating system.
