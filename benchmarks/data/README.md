# Raw benchmark samples

Each JSON file contains wall-clock samples from `bench_vs_aiofiles.py`.
Each sample times a concurrent workload, including file opens and closes;
it is measured in seconds. The charts in `docs/benchmarks.md` are generated
from these files by `scripts/plot_benchmarks.py`. A speedup is the aiofiles
median divided by the urio median.

The historical files record Python version, GIL state, platform, and backend.
They do not record the exact source commit, dependency versions, or command
line. The current runner records dependency versions, options, and installed
code hashes; it records the commit when run from a Git checkout. The size
and file count change together across the matrix. The archived runs always
measured contenders in the same order. The hardware descriptions below
come from the original run notes; they are not fields in the historical JSON.
Separate file sets prevent direct reuse but cannot remove system-wide cache
or writeback effects. Use `--reverse-order` in a second run to check how much
the order matters.

The charts use CPython 3.14 and 3.14t data. The ext4 and ZFS results come
from different Linux machines, so they cannot isolate filesystem effects.
The 3.15t file is from a **3.15.0b3t** prerelease; it does not measure the
final 3.15 interpreter. The 3.12 and armv7l files are retained for reference.

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

The ARM files contain three repetitions per workload and have wide sample
ranges. Several Windows and ZFS rows also vary substantially. Differences
close to a ratio of 1 are unresolved by these runs.

[Validation runs from 23 October 2026](validation-2026-10-23/README.md)
include forward and reverse order samples on Linux/ZFS, macOS/APFS,
a hosted ARM64 runner, and a hosted Windows runner.
They use the current 0.2.0 source and show where older conclusions do
and do not repeat. The raw samples are a linked release asset. The old chart
files remain historical snapshots.

[Hosted validation runs from 24 October 2026](validation-2026-10-24/README.md)
cover Linux, macOS, and Windows on x64 and ARM64 with CPython 3.15.1. The
linked release asset includes two independent workflow runs and every timed
sample.

Recompute a statistic, e.g. the mean instead of the median:

```python
import json, statistics

with open('linux-x86_64-3.12-uring.json') as source:
    d = json.load(source)
for cell in d['matrix']:
    s = cell['samples']
    print(
        cell['scenario'],
        cell['op'],
        {k: round(statistics.fmean(v) * 1000, 1) for k, v in s.items()},
    )
```
