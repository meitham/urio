# Installation

## From PyPI

```bash
pip install urio
```

Prebuilt wheels target Python 3.12+ via the stable ABI (`abi3`), so a single
wheel works across CPython versions.

## Requirements

- **Python** 3.12 or newer, on any operating system.
- **Linux 5.6+** for the io_uring backend.
- **Windows 10/11** for the [IOCP backend](backends.md) — opt-in via
  `URIO_WINDOWS_IOCP=1`; Windows otherwise uses the thread backend.

Everywhere else — macOS, older kernels, sandboxes where the io_uring syscalls
are blocked — urio automatically uses its [thread-pool backend](backends.md),
the same approach aiofiles takes, so the library works on any platform.

urio detects io_uring availability at runtime by actually creating a ring, so
you never have to branch on platform yourself.

## Platforms & wheels

Prebuilt `abi3` wheels (one per platform, covering CPython 3.12+) are published
for:

| Platform | Architectures | Backend |
|---|---|---|
| Linux (manylinux & musllinux) | x86_64, aarch64, armv7 | io_uring |
| Windows | x64, x86 | thread pool by default; [IOCP](backends.md) opt-in |
| macOS | x86_64, arm64 | thread pool |

The native extension carries the io_uring ring on Linux and the IOCP driver on
Windows; on macOS it compiles to an empty stub and urio runs on the thread
backend. In every case the wheel installs without a Rust toolchain.

## Building from source

Building the native extension needs a Rust toolchain and
[maturin](https://www.maturin.rs/):

```bash
git clone https://github.com/meitham/urio
cd urio
pip install -e ".[dev]"      # installs maturin, pytest, aiofiles, ruff
maturin develop --release    # compile the Rust extension into your venv
```

After any change to the Rust sources (`src/lib.rs`) re-run `maturin develop`
before the Python side sees it.

## Verifying the install

```python
import asyncio
import urio


async def main():
    print('urio', urio.__version__)
    print('backend:', urio.get_backend().name)  # 'uring', 'iocp', or 'thread'


asyncio.run(main())
```
