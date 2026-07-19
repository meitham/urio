# Opening files

## `urio.open`

```python
urio.open(path, mode='r', *, encoding=None, errors=None, newline=None)
```

Open `path` and return an object that is both **awaitable** and an **async
context manager**. Binary modes yield an [`AsyncBufferedFile`](files.md); text
modes yield an [`AsyncTextFile`](files.md).

### Parameters

| Parameter | Description |
|---|---|
| `path` | Path to open. Accepts `str`, `bytes`, `os.PathLike`, or a [`urio.Path`](path.md). |
| `mode` | Mode string (see below). Defaults to `'r'` (text read). |
| `encoding` | Text mode only. Codec name; defaults to the locale preferred encoding. Ignored in binary mode. |
| `errors` | Text mode only. Codec error policy (e.g. `'strict'`, `'replace'`). Defaults to `'strict'`. |
| `newline` | Text mode only. Controls universal-newline behaviour (see [text mode](files.md#text-mode-asynctextfile)). |

### Modes

| Char | Meaning |
|---|---|
| `r` | Open for reading (default). |
| `w` | Open for writing, truncating first. Creates if missing. |
| `a` | Open for writing, appending to the end. Creates if missing. |
| `x` | Exclusive creation; fails if the file exists. |
| `b` | Binary mode. |
| `t` | Text mode (default). |
| `+` | Open for updating (reading **and** writing). |

Exactly one of `r`/`w`/`a`/`x` must be present. Text (`t`) is the default unless
`b` is given. Combining `t` and `b`, or supplying more than one primary mode,
raises `ValueError`.

### Two ways to use it

As an async context manager (closes automatically):

```python
async with urio.open('file.txt', 'w') as f:
    await f.write('hello')
```

As an awaitable (you close it yourself):

```python
f = await urio.open('file.txt', 'w')
try:
    await f.write('hello')
finally:
    await f.close()
```

### Return type by mode

| Mode contains | Returns |
|---|---|
| `b` | [`AsyncBufferedFile`](files.md#binary-mode-asyncbufferedfile) |
| otherwise (text) | [`AsyncTextFile`](files.md#text-mode-asynctextfile) |

### Errors

- `ValueError` — invalid or contradictory mode string.
- `FileNotFoundError` — opening a missing file for reading.
- `FileExistsError` — `x` mode when the file already exists.
- `PermissionError`, `IsADirectoryError`, etc. — surfaced from the OS as usual.

### Examples

```python
# Text, explicit encoding
async with urio.open('u.txt', 'w', encoding='utf-8') as f:
    await f.write('héllo')

# Binary append
async with urio.open('log.bin', 'ab') as f:
    await f.write(b'\x01')

# Read+write update mode
async with urio.open('data', 'rb+') as f:
    await f.seek(0)
    header = await f.read(4)
```
