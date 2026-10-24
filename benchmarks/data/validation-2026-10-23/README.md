# Validation runs, 23 October 2026

The [benchmark archive](https://github.com/meitham/urio/releases/download/benchmarks-2026-10-24/urio-benchmarks-2026-10-23.zip)
contains raw JSON samples from the current local urio 0.2.0 source tree and
aiofiles 25.1.0. The archive is 62,965 bytes and its SHA-256 digest is
`59ec08a28d9d091621217302b5b2fbf391cdc74722017bc6ae8d2c9b341cc2d1`.
Its `SHA256SUMS.txt` lists each file digest. The samples were collected to check whether changing
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

## Hosted ARM64 run

The archive's `arm-gh` directory contains twelve runs from a GitHub-hosted Ubuntu
ARM64 runner. The [Actions run](https://github.com/meitham/urio/actions/runs/40026586497)
built commit `b3a82e47180d44e14cb4b5c267e03da348802907`, passed the test
suite, and uploaded the raw samples as an artifact. The downloaded ZIP had
SHA-256 `4ba74ea4b684350139a94ad8d1f77dc17b85cc7e1b52845930b4a3aa5af298ea`.
The JSON records CPython 3.14.8, aiofiles 25.1.0, Linux 6.17 on AArch64,
and the io_uring backend. Temporary files were under `/tmp` on the runner.

Each row used seven timed samples and two warm-ups per contender. The four
independent processes ran in forward, reverse, reverse, forward order.
Only binary mode was tested. The ranges below are the smallest and largest
ratios of process medians, not confidence intervals.

| ARM64 workload | Files | Write | Write + fsync | Read |
|---|--:|--:|--:|--:|
| 4 KB | 300 | 3.57–3.78× | 2.29–2.71× | 4.06–4.23× |
| 64 KB | 150 | 1.53–1.64× | 1.73–1.90× | 2.28–2.49× |
| 8 MB | 10 | 0.91–1.06× | 0.98–1.05× | 0.31–0.38× |

Small io_uring operations were faster in both orders on this runner. The 8 MB
read was slower in all four processes. For comparison, the urio thread
backend's read ratios were 0.83–0.86× at 4 KB, 0.91–0.94× at 64 KB, and
1.55–1.88× at 8 MB. This is an ARM64 virtual machine, not the 32-bit
Raspberry Pi used for the earlier ARM results.

## Hosted Windows run

The archive's `windows-gh` directory contains twelve runs from a GitHub-hosted
Windows runner. The [Actions run](https://github.com/meitham/urio/actions/runs/40026077281)
built commit `ba8bf27abdffe3225cae53b8af8bd550e7abf9f0`, and the CI
[run for that commit](https://github.com/meitham/urio/actions/runs/40026077376)
passed on all configured Python versions, including Windows 3.15 and 3.15t
with IOCP enabled. The downloaded benchmark ZIP had SHA-256
`1c232507082233dc3e02c9ef333a8b7e8c26ea40024e2ad21342f382696dfd66`.
The extracted JSON line endings were changed from CRLF to LF for the
repository; no sample values were changed.
The JSON records CPython 3.14.9, aiofiles 25.1.0, Windows Server 2025,
and the IOCP backend. Temporary files were under the runner's user temp
directory.

Each workload used seven timed samples and two warm-ups per contender.
The four independent processes ran in forward, reverse, reverse, forward
order. Both binary and text modes were tested. The ranges below are the
smallest and largest ratios of process medians, not confidence intervals.

| Windows workload | Write | Write + fsync | Read |
|---|--:|--:|--:|
| 300 × 4 KB binary | 1.65–1.94× | 0.77–1.38× | 0.53–0.57× |
| 300 × 4 KB text | 1.48–1.71× | 0.76–1.14× | 0.55–0.63× |
| 150 × 64 KB binary | 1.74–1.97× | 1.11–2.09× | 0.55–0.71× |
| 150 × 64 KB text | 1.24–1.42× | 0.47–1.03× | 0.66–0.76× |
| 10 × 8 MB binary | 0.55–0.62× | 0.97–1.08× | 0.27–0.67× |
| 10 × 8 MB text | 0.95–0.97× | 0.88–1.07× | 0.81–0.82× |

Concurrent 4–64 KB IOCP reads were slower than aiofiles in every
process, while 4–64 KB buffered writes were faster. The `write + fsync`
ratios were less stable and several straddled parity. The earlier Windows
10 run reported an 8 MB text-read ratio above 1; the current hosted
runner did not repeat that result. The difference cannot be assigned to
source changes, Windows version, or hardware from these measurements.
