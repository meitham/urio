# Raw benchmark samples

Every file here is the `--json` output of `bench_vs_aiofiles.py` — the **raw
per-iteration wall-clock samples** (not just a summary), plus a `meta` block
(Python version, GIL state, platform, backend). Keeping the raw samples means any
statistic — median, mean, best, percentiles — can be recomputed later without
re-running, and the charts in `docs/benchmarks.md` (which report **medians**)
stay auditable.

CPython **3.14** is the primary version — the charts in `docs/benchmarks.md` are
its medians. The same Linux benchmark is run on **two boxes** to show filesystem
impact: **ext4 on a SATA HDD** and **ZFS** (the `-zfs` files). Free-threading is
validated on **3.14t and 3.15.0b3t** (PyO3 0.29, urio's binding, does not support
the free-threaded build below 3.14). The 3.12 and armv7l files are kept for
reference.

| File | Hardware | Python | Backend |
|---|---|---|---|
| `linux-x86_64-3.14-uring.json` | 4-core i5-6500 (Skylake), 32 GB, ext4 on SATA HDD, kernel 7.0 | 3.14.6 (GIL) | io_uring |
| `linux-x86_64-3.14t-uring.json` | ↑ ext4/HDD box | 3.14.6t (no-GIL) | io_uring |
| `linux-x86_64-3.14-uring-cold.json` | ↑ ext4/HDD box | 3.14.6 (GIL) | io_uring, `--cold` |
| `linux-x86_64-3.15t-uring.json` | ↑ ext4/HDD box | 3.15.0b3t (no-GIL) | io_uring |
| `linux-x86_64-3.14-uring-zfs.json` | 8-thread Xeon E5-1620, 64 GB, ZFS, kernel 6.8 | 3.14.6 (GIL) | io_uring |
| `linux-x86_64-3.14t-uring-zfs.json` | ↑ ZFS box | 3.14.6t (no-GIL) | io_uring |
| `linux-x86_64-3.14-uring-zfs-cold.json` | ↑ ZFS box | 3.14.6 (GIL) | io_uring, `--cold` |
| `windows-x86_64-3.14-iocp-thread-*files.json` | Windows 10, 8-core | 3.14 (GIL) | IOCP (zero-copy) + thread |
| `windows-x86_64-3.14t-iocp-thread-*files.json` | Windows 10, 8-core | 3.14t (no-GIL) | IOCP (zero-copy) + thread |
| `macos-arm64-3.14-thread.json` | Apple M4 Pro | 3.14 (GIL) | thread |
| `linux-x86_64-3.12-uring.json` | 8-thread Xeon E5-1620, ZFS, kernel 6.8 | 3.12 (GIL) | io_uring |
| `linux-x86_64-3.12-uring-cold.json` | ↑ Xeon/ZFS box | 3.12 (GIL) | io_uring, `--cold` |
| `windows-x86_64-3.12-iocp-thread-*files.json` | Windows 10, 8-core | 3.12 (GIL) | IOCP + thread |
| `linux-armv7l-3.12-uring-*files.json` | Raspberry Pi (armv7l, kernel 5.15) | 3.12 (GIL) | io_uring |

The Windows and armv7 files are split per file-count because their light matrices
ran each size class separately (the full 2000-file matrix saturates those small
boxes). x86_64 Linux/macOS files each hold the full default matrix.

Recompute a statistic, e.g. the mean instead of the median:

```python
import json, statistics
d = json.load(open('linux-x86_64-3.12-uring.json'))
for cell in d['matrix']:
    s = cell['samples']
    print(cell['scenario'], cell['op'],
          {k: round(statistics.fmean(v) * 1000, 1) for k, v in s.items()})
```
