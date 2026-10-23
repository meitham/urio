# Benchmarks

The repository includes `benchmarks/bench_vs_aiofiles.py`, which compares
urio with [aiofiles](https://github.com/Tinche/aiofiles). Each timing covers
a concurrent batch of file opens, an operation on each file, and closes.
It is wall-clock time for the batch, not latency per file. The charts show
the **ratio of median times**: aiofiles median divided by urio median.
A value above 1 means urio was faster in that run.

```console
python benchmarks/bench_vs_aiofiles.py --markdown
python benchmarks/bench_vs_aiofiles.py --repeat 9 --warmup 2 --json run.json
python benchmarks/bench_vs_aiofiles.py --reverse-order --json reverse.json
python benchmarks/bench_vs_aiofiles.py --cold  # Linux: request cache eviction
python benchmarks/bench_vs_aiofiles.py --directory /path/to/test-volume
```

The 2,000-file row needs a soft file descriptor limit above 2,064 on
POSIX systems. For example, run `ulimit -n 4096` in the shell first.
The runner checks this before it starts.
Use `--directory` to select the volume used for temporary files; the
runner removes its own files when it finishes.

## What the results can show

The Linux matrix uses 2,000 files at 4 KB, 500 at 64 KB, 100 at 1 MB,
and 20 at 8 MB. The Windows matrix uses 300, 150, 40, and 10 files.
File size, file count, and total bytes therefore change together.
Comparisons between urio and aiofiles *within* a row use the same
workload; differences *between* rows cannot be attributed to file size alone.

Read tests warm the working set before timing. Each contender writes to its
own files, but all share the machine's caches, CPU, and storage. The archived
runs always measured contenders in the same order. This can bias results;
the new `--reverse-order` option permits a sensitivity check. The October
2026 runs below include both orders. Buffered writes are especially affected
by writeback.

Most results have five repetitions in one process; ZFS has seven and ARM
has three. A median is useful for describing those runs, but it is not a
confidence interval. Small differences around 1.0 are not evidence that
one library is faster. The historical JSON also lacks the exact source
commit and dependency versions. The [raw samples](https://github.com/meitham/urio/tree/main/benchmarks/data)
are retained, and `scripts/plot_benchmarks.py` now generates the charts
directly from them.

`write + fsync` includes an `fsync` call for each file. It is not a
crash-consistency test: the benchmark does not sync parent directories
or verify what reached physical storage.

## Current source check

The [October 2026 raw samples](https://github.com/meitham/urio/tree/main/benchmarks/data/validation-2026-10-23)
use the current 0.2.0 source and aiofiles 25.1.0. Each workload was run
in a separate process with the normal and reversed contender orders.
The following ratios compare the medians from each process; a value
above 1 means urio was faster in that process.

On a Linux host with ZFS, CPython 3.14.6, and the io_uring backend:

| Binary workload | Files | Normal order | Reverse order |
|---|--:|--:|--:|
| 4 KB read | 2,000 | 3.16× | 2.10× |
| 4 KB write + fsync | 2,000 | 2.90× | 3.01× |
| 64 KB read | 500 | 2.48× | 3.01× |
| 64 KB write + fsync | 500 | 1.41× | 1.45× |
| 8 MB read | 20 | 0.82× | 0.90× |
| 8 MB write + fsync | 20 | 1.03× | 1.03× |

The small-read and small-write advantages persisted when the order was
reversed. The 8 MB `write + fsync` comparison was near parity. On a Mac
with CPython 3.14.7, four separate runs of the thread backend and
aiofiles gave 4 KB read ratios of 0.93–1.01×. The 8 MB buffered-write
ratios ranged from 0.77× to 2.10×, so those runs do not establish a
winner for that operation. All measurements used warm-cache data and
machines running other services.

## Earlier Linux io_uring run

The archived chart below uses CPython 3.14.6 on an Intel Core i5-6500 (4 cores),
32 GB RAM, ext4 on a SATA hard disk, and Linux 7.0. It shows concurrent,
warm-cache operations against aiofiles. These samples were collected in
one process, always with the same contender order; the exact source
commit was not recorded. Treat the chart as a record of that run.

![Linux io_uring compared with aiofiles](assets/bench-linux.svg)

Small and medium workloads had the largest advantages in this run:
4–64 KB buffered writes were 3.4–5.2 times faster, reads were
2.6–3.4 times faster, and `write + fsync` was 4.1–5.9 times faster.
Large warm binary reads were slower: about 0.8 times at 1 MB and
0.5 times at 8 MB. Large text writes also fell below parity.

For example, these are three cells behind the chart:

| Workload | Files | urio io_uring | aiofiles | Ratio |
|---|--:|--:|--:|--:|
| 4 KB binary read | 2,000 | 83.8 ms | 285.3 ms | 3.40× |
| 64 KB binary write + fsync | 500 | 17.0 ms | 100.1 ms | 5.89× |
| 8 MB binary read | 20 | 52.1 ms | 25.5 ms | 0.49× |

These timings include the whole batch. They do not measure individual
read or `fsync` latency.

### Attempted cold-cache reads

`--cold` calls `fsync` and `posix_fadvise(DONTNEED)` before each read.
This requests eviction; the benchmark does not verify cache misses.
On this machine the results remained close to the warm-cache results.
The 128 MB stream still ran at several GB/s, much faster than the disk,
so the data was apparently served from memory despite the eviction
request. These results should not be read as a measurement of physical
disk latency.

### The older ZFS run

The [archived ZFS samples](https://github.com/meitham/urio/blob/main/benchmarks/data/linux-x86_64-3.14-uring-zfs.json)
were collected on a different machine from the ext4 samples. The two
runs cannot isolate a filesystem effect. Some ZFS results also differ
substantially from the current-source ZFS check above: the older 64 KB
binary buffered-write ratio was 0.66×, compared with 2.83–3.50× in
the new forward and reverse runs. The source commit and dependency
versions for the older run are unavailable, so the cause of that change
cannot be determined from these samples.

## Windows IOCP

The optional IOCP backend was measured on an 8-core Windows 10
machine with CPython 3.14. These are concurrent warm-cache workloads;
the figures are medians of five runs in one process. This platform has
not yet been rerun with the current source and reversed contender order.

![Windows IOCP compared with aiofiles](assets/bench-windows.svg)

| Workload | Buffered write | Write + fsync | Read |
|---|--:|--:|--:|
| Binary, 4–64 KB | 1.00–1.32× | 1.11–1.20× | 0.47–0.51× |
| Binary, 1–8 MB | 0.50–0.82× | 0.90–0.97× | 0.25–0.61× |
| Text, 4–64 KB | 0.97–1.32× | 1.14–1.36× | 0.54–0.85× |
| Text, 1–8 MB | 1.53–1.89× | 1.42–1.71× | 1.34–1.74× |

Some Windows rows have wide sample ranges. In the 4 KB binary-write row,
both medians were about 113 ms; the 1.00× ratio indicates no measured
advantage. The larger read differences are clearer in these samples.

Concurrent binary reads were slower with IOCP in these samples. The
benchmark does not isolate the cause. Windows uses the thread backend
by default; IOCP is available with `URIO_WINDOWS_IOCP=1`.

## Earlier ARM run

A Jetson Nano (aarch64, Linux 4.9, CPython 3.12) used the thread
backend because its kernel predates io_uring. The manylinux2014 wheel
ran on its glibc 2.27 system. The earlier Jetson measurements have no
saved samples in this repository, so they are omitted here.

A Raspberry Pi running 32-bit ARM, Linux 5.15, and CPython 3.12 used
io_uring. The table shows medians of three warm-cache runs:

| Scenario | Write | Write + fsync | Read |
|---|--:|--:|--:|
| 300 × 4 KB binary | 0.22× | 1.36× | 2.89× |
| 300 × 4 KB text | 1.35× | 2.07× | 2.18× |
| 150 × 64 KB binary | 2.21× | 2.43× | 2.30× |
| 150 × 64 KB text | 1.18× | 0.63× | 2.33× |
| 20 × 1 MB binary | 0.35× | 0.77× | 0.49× |

The ARM measurements have only three repetitions and many wide ranges.
The 0.22× buffered-write result varied widely, possibly because of
writeback on the SD card.
Use these rows as exploratory measurements, not expected Pi performance.

## Earlier free-threaded runs

Free-threaded Python changes how much work an executor can do in
parallel. The following Linux results compare CPython 3.14.6 and
3.14.6t on the same 4-core machine. They are concurrent, warm-cache
binary reads.

![GIL and free-threaded read results](assets/bench-nogil.svg)

| Size | 3.14.6 | 3.14.6t |
|---|--:|--:|
| 4 KB | 3.40× | 2.24× |
| 64 KB | 2.88× | 1.60× |
| 1 MB | 0.81× | 0.97× |
| 8 MB | 0.49× | 1.22× |

The ratio narrowed for small reads, and the 8 MB result moved above
parity in this run. These measurements cannot separate interpreter
effects from run-to-run variation. A separate 3.15.0b3t
run is retained in the raw results; it should not be treated as a
measurement of the final 3.15 release.

On the Windows IOCP machine, the comparison between CPython 3.14 and
3.14t showed a different effect for large text reads:

| Size | Operation | 3.14 | 3.14t |
|---|---|--:|--:|
| 4 KB | Binary write + fsync | 1.11× | 1.13× |
| 64 KB | Binary write + fsync | 1.20× | 1.08× |
| 4 KB | Binary read | 0.47× | 0.64× |
| 8 MB | Binary read | 0.25× | 0.33× |
| 1 MB | Text read | 1.74× | 0.75× |
| 8 MB | Text read | 1.34× | 0.69× |

The measurements do not isolate why the ratios changed. In particular,
the large text-read result has not been checked with independent runs
and reversed contender order. Measure your own mix of reads, writes,
and syncs before choosing IOCP or forcing the thread backend.
