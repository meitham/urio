# Validation runs, 23 October 2026

These JSON files are raw samples from the current local urio 0.2.0 source
tree and aiofiles 25.1.0. They were collected to check whether changing
contender order changed the interpretation of the archived benchmarks.
Each ratio below is the aiofiles median divided by the urio median for a
single process. No ratio is a confidence interval.

The Linux runs used CPython 3.14.6 on an 8-thread Xeon E5-1620 host,
kernel 6.8.12, with the temporary files on ZFS. The local source snapshot
was built as a wheel with SHA-256
`8d17439089d167a67122873e1e230df8cdaa512937d9f7378ffbdb1a1c3d1019`.
The source snapshot did not contain `.git`, so its JSON reports no commit.
There were five timed samples and one warm-up per contender for 4 KB and
64 KB, and seven timed samples and two warm-ups for 8 MB. Only binary
mode was tested. File counts were 2,000, 500, and 20 respectively.

| Linux workload | Forward order | Reverse order |
|---|--:|--:|
| 4 KB write | 2.80× | 2.55× |
| 4 KB write + fsync | 2.90× | 3.01× |
| 4 KB read | 3.16× | 2.10× |
| 64 KB write | 3.50× | 2.83× |
| 64 KB write + fsync | 1.41× | 1.45× |
| 64 KB read | 2.48× | 3.01× |
| 8 MB write | 1.24× | 1.37× |
| 8 MB write + fsync | 1.03× | 1.03× |
| 8 MB read | 0.82× | 0.90× |

The macOS runs used CPython 3.14.7 on an Apple M4 Pro, with temporary
files on APFS. The wheel SHA-256 was
`5139ef9bf7e54a4e9868b2c68834eccaa828c277e186326c251165315e93a8b7`.
There were seven timed samples and two warm-ups per contender. The full
binary matrix ran in four separate processes, in forward, reverse,
forward, reverse order. The soft file descriptor limit was raised to 4,096.
The urio contender here was the thread backend.

| macOS workload | Forward 1 | Reverse 1 | Forward 2 | Reverse 2 |
|---|--:|--:|--:|--:|
| 4 KB read | 0.93× | 0.99× | 1.01× | 1.00× |
| 64 KB read | 0.90× | 0.87× | 0.88× | 0.92× |
| 1 MB write | 0.46× | 1.01× | 0.97× | 1.00× |
| 8 MB write | 0.77× | 1.76× | 0.92× | 2.10× |

Large buffered writes varied enough on macOS that these runs do not
establish which library is faster for that workload. The Linux 4 KB and
64 KB io_uring advantage appears in both orders on this ZFS host. The
8 MB `write + fsync` result is near parity, while 8 MB io_uring reads
were slower in both orders. These results do not validate exact ratios
from the older ext4, Windows, or ARM runs.

For a matched pair, use the same command twice, adding `--reverse-order`
to the second run. For example:

```console
ulimit -n 4096
python benchmarks/bench_vs_aiofiles.py --files 500 --size 65536 --mode binary --no-stream --repeat 5 --warmup 1 --json forward.json
python benchmarks/bench_vs_aiofiles.py --files 500 --size 65536 --mode binary --no-stream --repeat 5 --warmup 1 --reverse-order --json reverse.json
```

The samples come from processes on machines running other services.
They cannot establish a universal speedup, physical disk latency, or a
crash durability guarantee.

The benchmark runner subsequently gained argument checks and more JSON
provenance fields. Its timed workload functions did not change between
these runs and that update.
