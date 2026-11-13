import argparse
import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path

import psutil

import urio


async def probe(args, process):
    opened = []
    close_errors = []
    reason = 'target'
    first_error = None
    peak_rss = process.memory_info().rss
    peak_handles = process.num_handles()
    started = time.monotonic()
    active_elapsed = 0.0

    with tempfile.TemporaryDirectory() as directory:
        if args.distinct:
            paths = None
        else:
            paths = [Path(directory) / f'source-{index:04d}' for index in range(1024)]
            for path in paths:
                path.write_bytes(b'x')

        urio.set_backend(args.backend)
        try:
            while len(opened) < args.target:
                previous = len(opened)
                end = min(len(opened) + args.batch_size, args.target)
                while len(opened) < end:
                    index = len(opened)
                    if args.distinct:
                        shard = Path(directory) / f'{index // 1000:04d}'
                        try:
                            if index % 1000 == 0:
                                shard.mkdir()
                            path = shard / f'{index % 1000:04d}'
                            path.write_bytes(b'x')
                        except OSError as error:
                            reason = 'create_os_error'
                            first_error = {
                                'type': type(error).__name__,
                                'errno': error.errno,
                                'winerror': getattr(error, 'winerror', None),
                                'message': str(error),
                            }
                            break
                    else:
                        path = paths[index % len(paths)]
                    try:
                        file = await urio.open(path, 'rb')
                    except OSError as error:
                        reason = 'open_os_error'
                        first_error = {
                            'type': type(error).__name__,
                            'errno': error.errno,
                            'winerror': getattr(error, 'winerror', None),
                            'message': str(error),
                        }
                        break
                    opened.append(file)

                if len(opened) > previous:
                    sample = opened[-1]
                    if await asyncio.wait_for(sample.read(1), 10) != b'x':
                        raise AssertionError('sampled read returned unexpected data')

                peak_rss = max(peak_rss, process.memory_info().rss)
                peak_handles = max(peak_handles, process.num_handles())
                elapsed = time.monotonic() - started
                if len(opened) % 25_000 == 0 or reason != 'target':
                    print(
                        f'opened={len(opened)} handles={peak_handles} '
                        f'rss_mib={peak_rss // (1024 * 1024)} '
                        f'elapsed_seconds={elapsed:.1f}',
                        flush=True,
                    )
                if reason != 'target':
                    break
                if peak_rss >= args.max_rss_gib * 1024**3:
                    reason = 'memory_guard'
                    break
                if shutil.disk_usage(directory).free < args.min_free_gib * 1024**3:
                    reason = 'disk_guard'
                    break
                if elapsed >= args.max_seconds:
                    reason = 'time_guard'
                    break

            for index in {0, len(opened) // 2, len(opened) - 1} if opened else ():
                file = opened[index]
                await file.seek(0)
                if await asyncio.wait_for(file.read(1), 10) != b'x':
                    raise AssertionError(f'read failed for open file {index}')
            active_elapsed = time.monotonic() - started
        finally:
            for file in reversed(opened):
                try:
                    await file.close()
                except OSError as error:
                    if len(close_errors) < 5:
                        close_errors.append(str(error))
            await urio.aclose()
            urio.set_backend('auto')

    return {
        'backend': args.backend,
        'path_mode': 'distinct' if args.distinct else 'shared_pool',
        'opened': len(opened),
        'target': args.target,
        'reason': reason,
        'first_error': first_error,
        'peak_rss_mib': peak_rss // (1024 * 1024),
        'peak_handles': peak_handles,
        'active_seconds': round(active_elapsed, 1),
        'total_seconds': round(time.monotonic() - started, 1),
        'close_errors': close_errors,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', choices=('thread', 'iocp'), required=True)
    parser.add_argument('--target', type=int, required=True)
    parser.add_argument('--batch-size', type=int, default=5000)
    parser.add_argument('--max-rss-gib', type=int, default=3)
    parser.add_argument('--max-seconds', type=int, default=360)
    parser.add_argument('--min-free-gib', type=int, default=3)
    parser.add_argument('--distinct', action='store_true')
    args = parser.parse_args()

    process = psutil.Process()
    baseline_handles = process.num_handles()
    result = asyncio.run(probe(args, process))
    result['handles_after_cleanup'] = process.num_handles()
    result['baseline_handles'] = baseline_handles
    summary = json.dumps(result, separators=(',', ':'))
    print(summary, flush=True)
    print(
        f'::notice title=Windows {args.backend} handle probe::'
        f'{summary.replace("%", "%25")}',
        flush=True,
    )
    if result['close_errors']:
        raise SystemExit('close errors during cleanup')
    if args.backend == 'iocp' and result['opened'] != args.target:
        raise SystemExit('IOCP target was not reached')
    if result['handles_after_cleanup'] > baseline_handles + 64:
        raise SystemExit('handle count did not return to baseline')


if __name__ == '__main__':
    main()
