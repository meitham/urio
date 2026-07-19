#!/usr/bin/env python3
"""Benchmark urio (io_uring and thread backends) against aiofiles.

`aiofiles` pioneered ergonomic async file access for asyncio, and urio's
thread-pool backend deliberately mirrors its approach for platforms without a
native async filesystem interface (for example macOS). On Linux, urio adds an
io_uring backend that submits real asynchronous I/O instead of parking a thread
per operation -- this benchmark shows what that buys across file sizes.

Usage::

    python benchmarks/bench_vs_aiofiles.py                 # default matrix
    python benchmarks/bench_vs_aiofiles.py --markdown       # tables for docs
    python benchmarks/bench_vs_aiofiles.py --files 500 --size 65536

Each scenario runs many file operations concurrently -- the regime where a real
async backend should pull ahead, since it does not need a thread per in-flight
operation. Reads are served warm from the page cache, so they measure the
async machinery overhead rather than disk latency.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import statistics
import tempfile
import time
from typing import NamedTuple

import aiofiles

import urio

# (label, file count, size in bytes) -- chosen so total bytes per scenario stay
# modest while spanning four orders of magnitude in file size.
DEFAULT_MATRIX = [
    ('small', 2000, 4 * 1024),
    ('medium', 500, 64 * 1024),
    ('large', 100, 1 * 1024 * 1024),
    ('xlarge', 20, 8 * 1024 * 1024),
]

# Single large file streamed in chunks.
STREAM_SIZE = 128 * 1024 * 1024
STREAM_CHUNK = 1024 * 1024


class Result(NamedTuple):
    """Every per-iteration wall-clock sample for one workload.

    We keep *all* samples (not a single collapsed number) so any statistic can
    be derived later — median, mean, best, percentiles — without re-running, and
    so ``--json`` can persist the raw data. Reporting defaults to the **median**
    plus the ``[lo, hi]`` range: the minimum ("best of N") is the single
    luckiest, least-contended run and hides variance. For rigorous A/B work
    raise ``--repeat`` (and ideally run several processes — see pyperf).
    """

    samples: tuple[float, ...]

    @property
    def median(self) -> float:
        return statistics.median(self.samples)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.samples)

    @property
    def best(self) -> float:
        return min(self.samples)

    @property
    def lo(self) -> float:
        return min(self.samples)

    @property
    def hi(self) -> float:
        return max(self.samples)

    @property
    def spread(self) -> float:
        """Full min..max range as a fraction of the median."""
        m = self.median
        return (self.hi - self.lo) / m if m else 0.0


async def _timeit(coro_factory, repeat, warmup, setup=None):
    # Untimed warm-up runs so every contender enters timing from an identical
    # state. `setup` (if given) runs untimed before EACH timed iteration -- used
    # in cold mode to evict the page cache so every read actually hits storage.
    for _ in range(warmup):
        await coro_factory()
    samples = []
    for _ in range(repeat):
        if setup is not None:
            setup()
        start = time.perf_counter()
        await coro_factory()
        samples.append(time.perf_counter() - start)
    return Result(tuple(samples))


def _warm_cache(paths):
    """Pull every file into the page cache with a neutral, synchronous reader,
    so the read phase starts warm for all contenders regardless of order."""
    for p in paths:
        try:
            with open(p, 'rb') as f:
                while f.read(1 << 20):
                    pass
        except OSError:
            pass


def _evict_cache(paths):
    """Drop each file from the page cache so the next read hits storage. We
    fsync first (DONTNEED only evicts clean pages) then advise the kernel to
    discard the file's cached pages. This measures io_uring's real target:
    latency-bound, cold-cache I/O rather than a warm memcpy."""
    for p in paths:
        try:
            fd = os.open(p, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass
        finally:
            os.close(fd)


# -- concurrent workloads -------------------------------------------------
#
# Each workload runs in both binary ('b') and text mode so we exercise the raw
# positional path and the text codec / newline-translation layer alike.


async def urio_write(opener, paths, payload, mode, syncer=None):
    async def one(p):
        async with opener(p, mode) as f:
            await f.write(payload)
            if syncer is not None:
                # Durable write: force the data to disk before closing, so the
                # timing reflects real I/O cost rather than the page-cache burst.
                await syncer(f)

    await asyncio.gather(*(one(p) for p in paths))


async def urio_read(opener, paths, mode):
    async def one(p):
        async with opener(p, mode) as f:
            return await f.read()

    await asyncio.gather(*(one(p) for p in paths))


# -- per-contender fsync ---------------------------------------------------
#
# Each contender flushes through its own natural path: urio via its async
# fsync() (io_uring IORING_OP_FSYNC on the uring backend, os.fsync in the
# executor on the thread backend); aiofiles by flushing and fsync'ing its fd in
# the executor, the idiomatic aiofiles way.


async def urio_sync(f):
    await f.fsync()


async def aiofiles_sync(f):
    await f.flush()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, os.fsync, f.fileno())


# -- streaming workloads --------------------------------------------------


async def stream_read(opener, path, mode):
    async with opener(path, mode) as f:
        while True:
            chunk = await f.read(STREAM_CHUNK)
            if not chunk:
                break


# -- runners --------------------------------------------------------------

# (display label, urio backend name or None for aiofiles, opener, syncer)
# Unavailable backends (e.g. uring off Linux, iocp off Windows) are skipped by
# _available().
CONTENDERS = [
    ('urio[uring]', 'uring', urio.open, urio_sync),
    ('urio[iocp]', 'iocp', urio.open, urio_sync),
    ('urio[thread]', 'thread', urio.open, urio_sync),
    ('aiofiles', None, aiofiles.open, aiofiles_sync),
]


def _available():
    """Filter contenders to those usable here (skip uring if unavailable)."""
    out = []
    for label, name, opener, syncer in CONTENDERS:
        if name is not None:
            try:
                urio.set_backend(name)
            except RuntimeError:
                continue
        out.append((label, name, opener, syncer))
    urio.set_backend('auto')
    return out


def _select(name):
    if name is not None:
        urio.set_backend(name)


async def _measure(factory, repeat, warmup, setup=None):
    return await _timeit(factory, repeat, warmup, setup)


def _make_payload(size, binary):
    if binary:
        return os.urandom(size)
    # Deterministic ASCII (with newlines) so the text file is exactly `size`
    # bytes in UTF-8 and the newline-translation path is exercised.
    return ''.join('\n' if i % 64 == 63 else chr(32 + (i % 94)) for i in range(size))


def _safe(label):
    return label.replace('[', '_').replace(']', '')


async def run_matrix(matrix, modes, repeat, warmup, workdir, markdown, cold=False):
    rows = []
    for label, n_files, size in matrix:
        for mode_name, binary in modes:
            wmode = 'wb' if binary else 'w'
            rmode = 'rb' if binary else 'r'
            payload = _make_payload(size, binary)

            write_times = {}
            fsync_times = {}
            read_times = {}
            # Each contender gets its OWN file set so it never inherits another
            # contender's dirty-page / writeback pressure -- the ordering of
            # contenders cannot advantage or penalise any one of them.
            for blabel, name, opener, syncer in _available():
                _select(name)
                cdir = os.path.join(workdir, f'{label}_{mode_name}_{_safe(blabel)}')
                os.makedirs(cdir, exist_ok=True)
                paths = [os.path.join(cdir, str(i)) for i in range(n_files)]
                dpaths = [p + '.d' for p in paths]

                write_times[blabel] = await _measure(
                    lambda o=opener, p=paths, d=payload, m=wmode: urio_write(
                        o, p, d, m
                    ),
                    repeat,
                    warmup,
                )
                # Durable write: same write, then fsync each file to disk. Far
                # more stable than buffered writes, which only hit the cache.
                fsync_times[blabel] = await _measure(
                    lambda o=opener, p=dpaths, d=payload, m=wmode, s=syncer: urio_write(
                        o, p, d, m, s
                    ),
                    repeat,
                    warmup,
                )
                # Cold: evict before each timed read so it hits storage.
                # Warm: prime the cache once so the read is measured warm.
                if cold:
                    read_setup = lambda p=paths: _evict_cache(p)  # noqa: E731
                    read_warmup = 0
                else:
                    _warm_cache(paths)
                    read_setup = None
                    read_warmup = warmup
                read_times[blabel] = await _measure(
                    lambda o=opener, p=paths, m=rmode: urio_read(o, p, m),
                    repeat,
                    read_warmup,
                    read_setup,
                )

            rows.append((label, mode_name, n_files, size, 'write', write_times))
            rows.append((label, mode_name, n_files, size, 'write+fsync', fsync_times))
            rows.append((label, mode_name, n_files, size, 'read', read_times))
            urio.set_backend('auto')

    _report(rows, markdown)
    return rows


async def run_stream(modes, repeat, warmup, workdir, markdown, cold=False):
    stream_rows = []
    for mode_name, binary in modes:
        path = os.path.join(workdir, f'stream_{mode_name}.bin')
        with open(path, 'wb') as f:
            f.write(_payload_bytes(STREAM_SIZE, binary))
        rmode = 'rb' if binary else 'r'
        if cold:
            setup = lambda p=path: _evict_cache([p])  # noqa: E731
            s_warmup = 0
        else:
            _warm_cache([path])
            setup = None
            s_warmup = warmup

        times = {}
        for blabel, name, opener, _syncer in _available():
            _select(name)
            times[blabel] = await _measure(
                lambda o=opener, p=path, m=rmode: stream_read(o, p, m),
                repeat,
                s_warmup,
                setup,
            )
        urio.set_backend('auto')
        stream_rows.append((mode_name, times))
    _report_stream(stream_rows, markdown)
    return stream_rows


def _dump_json(path, rows, stream_rows):
    """Persist every raw sample so any statistic (mean/median/best/percentile)
    can be computed later without re-running the benchmark."""
    import json
    import sys

    data = {
        'meta': {
            'python': sys.version.split()[0],
            'gil': getattr(sys, '_is_gil_enabled', lambda: True)(),
            'platform': sys.platform,
            'backend': urio.get_backend().name,
            'stream_bytes': STREAM_SIZE,
            'stream_chunk': STREAM_CHUNK,
        },
        'matrix': [
            {
                'scenario': label,
                'mode': mode,
                'files': n,
                'size': size,
                'op': op,
                'samples': {k: list(v.samples) for k, v in times.items()},
            }
            for (label, mode, n, size, op, times) in rows
        ],
        'stream': [
            {'mode': mode, 'samples': {k: list(v.samples) for k, v in times.items()}}
            for (mode, times) in stream_rows
        ],
    }
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f'\nraw samples written to {path}')


def _payload_bytes(size, binary):
    p = _make_payload(size, binary)
    return p if isinstance(p, bytes) else p.encode('utf-8')


def _native(times):
    """The active urio native backend label present in a result set."""
    for k in ('urio[uring]', 'urio[iocp]'):
        if k in times:
            return k
    return None


def _speedup(times):
    base = times.get('aiofiles')
    nk = _native(times)
    nat = times.get(nk) if nk else None
    return (base.median / nat.median) if (base and nat) else None


def _report(rows, markdown):
    if markdown:
        nk = _native(rows[0][5]) if rows else 'urio[native]'
        nk = nk or 'urio[native]'
        print(
            f'| Scenario | Mode | Files | Size | Op | {nk} | urio[thread] '
            '| aiofiles | speedup |'
        )
        print('|---|---|--:|--:|---|--:|--:|--:|--:|')
        for label, mode, n, size, op, times in rows:
            speed = _speedup(times)
            speed_s = f'{speed:.2f}x' if speed else 'n/a'
            print(
                f'| {label} | {mode} | {n} | {_fmt_size(size)} | {op} '
                f'| {_ms(times.get(nk))} '
                f'| {_ms(times.get("urio[thread]"))} '
                f'| {_ms(times.get("aiofiles"))} '
                f'| {speed_s} |'
            )
    else:
        for label, mode, n, size, op, times in rows:
            print(f'\n{label} [{mode}] ({n} x {_fmt_size(size)}) {op.upper()}:')
            base = times.get('aiofiles')
            for blabel, r in times.items():
                tag = ''
                if base and blabel != 'aiofiles':
                    tag = f'  ({base.median / r.median:.2f}x)'
                print(
                    f'  {blabel:<22} {r.median * 1000:8.2f} ms '
                    f'[{r.lo * 1000:.1f}-{r.hi * 1000:.1f}]{tag}'
                )


def _report_stream(stream_rows, markdown):
    mb = STREAM_SIZE / (1024 * 1024)
    print()
    if markdown:
        print(
            f'### Streaming read ({mb:.0f} MB file, {STREAM_CHUNK // 1024} KB chunks)\n'
        )
        print('| Mode | Backend | Time (ms) | Throughput (MB/s) | vs aiofiles |')
        print('|---|---|--:|--:|--:|')
        for mode, times in stream_rows:
            base = times.get('aiofiles')
            for label, r in times.items():
                speed = base.median / r.median if (base and r) else None
                tag = '' if label == 'aiofiles' else (f'{speed:.2f}x' if speed else '')
                print(
                    f'| {mode} | {label} | {r.median * 1000:.1f} '
                    f'| {mb / r.median:.0f} | {tag} |'
                )
    else:
        for mode, times in stream_rows:
            print(
                f'Streaming read [{mode}] ({mb:.0f} MB, {STREAM_CHUNK // 1024} KB chunks):'
            )
            base = times.get('aiofiles')
            for label, r in times.items():
                speed = base.median / r.median if (base and r) else None
                tag = (
                    ''
                    if label == 'aiofiles'
                    else (f'  ({speed:.2f}x)' if speed else '')
                )
                print(
                    f'  {label:<22} {r.median * 1000:8.1f} ms '
                    f'  {mb / r.median:6.0f} MB/s{tag}'
                )


def _ms(r):
    return f'{r.median * 1000:.1f}' if r is not None else 'n/a'


def _fmt_size(size):
    if size >= 1024 * 1024:
        return f'{size // (1024 * 1024)} MB'
    return f'{size // 1024} KB'


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--files', type=int, help='override file count')
    parser.add_argument('--size', type=int, help='override file size (bytes)')
    parser.add_argument(
        '--repeat', type=int, default=5, help='timed runs (median reported)'
    )
    parser.add_argument(
        '--json', metavar='PATH', help='write every raw sample to PATH as JSON'
    )
    parser.add_argument(
        '--warmup', type=int, default=1, help='untimed warm-up runs per contender'
    )
    parser.add_argument(
        '--mode',
        choices=('binary', 'text', 'both'),
        default='both',
        help='file mode(s) to benchmark',
    )
    parser.add_argument('--markdown', action='store_true')
    parser.add_argument('--no-stream', action='store_true')
    parser.add_argument(
        '--cold',
        action='store_true',
        help='evict the page cache before each read (latency-bound, not warm)',
    )
    args = parser.parse_args()

    if args.files and args.size:
        matrix = [('custom', args.files, args.size)]
    else:
        matrix = DEFAULT_MATRIX

    all_modes = [('binary', True), ('text', False)]
    if args.mode == 'binary':
        modes = [all_modes[0]]
    elif args.mode == 'text':
        modes = [all_modes[1]]
    else:
        modes = all_modes

    workdir = tempfile.mkdtemp(prefix='urio-bench-')
    try:
        if not args.markdown:
            cache = 'COLD cache' if args.cold else 'warm cache'
            print(
                f'Concurrent file I/O ({cache}, {args.warmup} warm-up + '
                f'median of {args.repeat}, [min-max] shown)\n'
            )
        rows = await run_matrix(
            matrix, modes, args.repeat, args.warmup, workdir, args.markdown, args.cold
        )
        stream_rows = []
        if not args.no_stream:
            stream_rows = await run_stream(
                modes, args.repeat, args.warmup, workdir, args.markdown, args.cold
            )
        if args.json:
            _dump_json(args.json, rows, stream_rows)
    finally:
        urio.set_backend('auto')
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == '__main__':
    asyncio.run(main())
