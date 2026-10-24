# Hosted benchmarks, 24 October 2026

Two [Benchmark workflow](https://github.com/meitham/urio/actions/workflows/bench.yml)
runs completed on Linux x64 and ARM64, macOS Intel and ARM64, and Windows x64
and ARM64. The [first run](https://github.com/meitham/urio/actions/runs/40077576945)
and [final run](https://github.com/meitham/urio/actions/runs/40078800225)
each passed all six jobs. Both used CPython 3.15.1, urio 0.2.0, and aiofiles
25.1.0. The first run used PR merge commit
`1c1d861ea3bec67c83283a07274c12e2948d6d43`; the final run used
`8d9eddddf2e26f0b3fcc4ade43f24ec2a8b2fdcf`.

The [benchmark archive](https://github.com/meitham/urio/releases/download/benchmarks-2026-10-24/urio-benchmarks-2026-10-24.zip)
contains `first/` and `final/` directories with one JSON file per runner.
The archive is 231,747 bytes and its SHA-256 digest is
`a612d0d91dc333e9b6115c0363da39ca1eecc347e0ff4b8afc43b63aa36bdc7c`.
Its `SHA256SUMS.txt` lists each file digest. Each JSON file contains the 20
original process records, keyed by their artifact filenames. The records
retain every timed sample and environment field. The `artifact_sha256` field
is the digest of the original GitHub ZIP, checked after download. The JSON
groups the records for easier inspection; it is not a byte-for-byte copy of
the ZIP.

Each size class ran in four separate processes, with contender order forward,
reverse, reverse, forward. Each contender had two warmups and seven timed
samples per process. The classes were 300 files of 4 KB, 150 of 64 KB, 40 of
1 MB, and 10 of 8 MB. The runner also read one 128 MB file in 1 MB chunks.
Reads used warm cached data. The samples are wall-clock times for whole
batches, including opens and closes.

The table uses the final run's binary reads. Each cell is the range of four
process ratios, where a ratio is the aiofiles median divided by the urio
median. Values above 1 mean urio was faster in that process. Linux used
io_uring, macOS used the thread backend, and Windows used opt-in IOCP.

| Runner | 4 KB read | 64 KB read | 8 MB read | 128 MB stream |
|---|--:|--:|--:|--:|
| Linux x64 | 4.85–5.19× | 2.19–3.61× | 0.39–0.44× | 0.98–1.11× |
| Linux ARM64 | 3.95–4.06× | 2.38–2.56× | 0.27–0.35× | 1.30–1.47× |
| macOS Intel | 0.92–1.07× | 0.83–0.98× | 0.89–1.06× | 0.78–0.83× |
| macOS ARM64 | 0.76–1.19× | 0.60–1.44× | 0.87–1.07× | 0.73–0.90× |
| Windows x64 | 0.61–0.64× | 0.66–0.75× | 0.29–0.31× | 1.25–1.31× |
| Windows ARM64 | 0.53–0.58× | 0.59–0.62× | 0.32–0.51× | 1.64–1.79× |

Small Linux reads were faster in both workflow runs. The 8 MB Linux reads
were slower in both. IOCP's 4 KB buffered writes were faster than aiofiles
in all four final processes: 1.73–1.98× on Windows x64 and 1.21–1.42× on
Windows ARM64. Its concurrent 4 KB binary reads were slower. These results
describe the measured workloads, not a general ranking of the libraries.

The two runs also show why small differences should be treated cautiously.
On Windows x64, the 4 KB read ratio moved from 0.36–0.38× in the first run
to 0.61–0.64× in the final run. The 4 KB buffered-write ratio moved from
3.24–3.33× to 1.73–1.98×. Both runs agree on the direction, but not on a
precise multiplier. The ranges in the table are not confidence intervals.
The workflow uses different hosted machines for each operating system and
architecture; it cannot isolate a hardware or filesystem effect.

The first run's later JSON records report `git_dirty: true` because earlier
processes had written untracked files to `benchmark-results/`. The first
process records `false`; the source commit and benchmark hash stayed the
same throughout that run. The output directory is ignored in the final
revision, and all final records report `git_dirty: false`.
