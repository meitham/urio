# Installation

Install urio with pip:

```console
python -m pip install urio
```

urio requires CPython 3.12 or newer. Regular CPython 3.12–3.15 uses wheels
built against Python's stable ABI. Free-threaded Python requires a separate
wheel for each interpreter version. The 0.3.0 release provides 3.14t and
3.15t wheels.

On Linux, urio uses io_uring if the kernel supports the required operations
and permits a ring to be created. Linux 5.6 or newer is recommended. On
macOS, Windows, older Linux kernels, and restricted Linux environments, urio
uses a thread pool. The [Windows IOCP backend](backends.md) is available by
opt-in.

The project builds wheels for Linux (x86_64, aarch64, armv7), Windows
(x64, x86), and macOS (x86_64, arm64). If pip builds from source, a Rust
toolchain is required.

## Check the installation

```python
import urio

print(urio.__version__)
print(urio.get_backend().name)
```

The backend name is `uring`, `iocp`, or `thread`.
Backend selection happens at runtime. An installed Linux wheel can therefore
use the thread backend if io_uring is unavailable.

## Install from a checkout

```console
git clone https://github.com/meitham/urio
cd urio
python -m pip install -e '.[dev]'
```

The editable install builds the Rust extension with maturin. Reinstall after
changing Rust code. For release builds, use `maturin build --release`.
