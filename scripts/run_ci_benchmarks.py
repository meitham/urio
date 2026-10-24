#!/usr/bin/env python3
"""Run the same light benchmark matrix on each GitHub-hosted runner.

Every workload runs in a separate process, in forward/reverse/reverse/forward
contender order. The underlying benchmark writes raw samples and provenance
to one JSON file per process; the workflow uploads those files unchanged.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

CASES = (
    ('4k', 300, 4 * 1024),
    ('64k', 150, 64 * 1024),
    ('1m', 40, 1024 * 1024),
    ('8m', 10, 8 * 1024 * 1024),
)
ORDERS = ('forward', 'reverse', 'reverse', 'forward')
BENCHMARK = Path(__file__).with_name('bench_vs_aiofiles.py')


def _raise_open_file_limit(parser: argparse.ArgumentParser) -> None:
    if os.name != 'posix':
        return
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    needed = max(files for _, files, _ in CASES) + 64
    if soft >= needed or soft == resource.RLIM_INFINITY:
        return
    target = 4096 if hard == resource.RLIM_INFINITY else min(4096, hard)
    if target < needed:
        parser.error(f'the runner needs at least {needed} open file descriptors')
    resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path('benchmark-results'))
    parser.add_argument('--repeat', type=int, default=7)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--skip-stream', action='store_true')
    args = parser.parse_args()
    if args.repeat < 1 or args.warmup < 0:
        parser.error('--repeat must be positive and --warmup cannot be negative')

    _raise_open_file_limit(parser)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for label, files, size in CASES:
        for iteration, order in enumerate(ORDERS):
            command = [
                sys.executable,
                str(BENCHMARK),
                '--files',
                str(files),
                '--size',
                str(size),
                '--mode',
                'both',
                '--no-stream',
                '--repeat',
                str(args.repeat),
                '--warmup',
                str(args.warmup),
                '--json',
                str(args.output_dir / f'{label}-{iteration}-{order}.json'),
            ]
            if order == 'reverse':
                command.append('--reverse-order')
            subprocess.run(command, check=True)

    if not args.skip_stream:
        for iteration, order in enumerate(ORDERS):
            command = [
                sys.executable,
                str(BENCHMARK),
                '--stream-only',
                '--mode',
                'both',
                '--repeat',
                str(args.repeat),
                '--warmup',
                str(args.warmup),
                '--json',
                str(args.output_dir / f'stream-{iteration}-{order}.json'),
            ]
            if order == 'reverse':
                command.append('--reverse-order')
            subprocess.run(command, check=True)


if __name__ == '__main__':
    main()
