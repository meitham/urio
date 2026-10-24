#!/usr/bin/env python3
"""Render SVG charts from the raw samples in benchmarks/data.

The selected JSON files below provide every plotted median. Run this script
after adding new samples to update the charts used by README and the docs.
"""

from __future__ import annotations

import pathlib

OUT = pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'assets'

DATA = pathlib.Path(__file__).resolve().parent.parent / 'benchmarks' / 'data'
SIZES = [
    ('4 KB', 4 * 1024),
    ('64 KB', 64 * 1024),
    ('1 MB', 1024 * 1024),
    ('8 MB', 8 * 1024 * 1024),
]


def _load_rows(paths):
    """Index raw benchmark rows; reject duplicate workload cells."""
    import json

    rows = {}
    for path in paths:
        data = json.loads(path.read_text())
        for row in data['matrix']:
            key = (row['mode'], row['size'], row['op'])
            if key in rows:
                raise ValueError(f'duplicate benchmark cell {key}: {path}')
            rows[key] = row
    return rows


def _median(row, contender):
    import statistics

    return statistics.median(row['samples'][contender])


def _ratio(row, native):
    """Ratio of contender medians, matching the benchmark report."""
    return _median(row, 'aiofiles') / _median(row, native)


def _operation_rows(rows, operation, native):
    result = []
    for mode in ('binary', 'text'):
        for label, size in SIZES:
            row = rows[(mode, size, operation)]
            result.append(
                (
                    f'{mode} {label}',
                    *(
                        _median(row, name) * 1000
                        for name in (native, 'urio[thread]', 'aiofiles')
                    ),
                )
            )
    return result


LINUX = _load_rows([DATA / 'linux-x86_64-3.14-uring.json'])
LINUX_NOGIL = _load_rows([DATA / 'linux-x86_64-3.14t-uring.json'])
ZFS = _load_rows([DATA / 'linux-x86_64-3.14-uring-zfs.json'])
WINDOWS = _load_rows(sorted(DATA.glob('windows-x86_64-3.14-iocp-thread-*files.json')))
WINDOWS_NOGIL = _load_rows(
    sorted(DATA.glob('windows-x86_64-3.14t-iocp-thread-*files.json'))
)

READS = _operation_rows(LINUX, 'read', 'urio[uring]')
WRITES = _operation_rows(LINUX, 'write', 'urio[uring]')
FSYNC = _operation_rows(LINUX, 'write+fsync', 'urio[uring]')
WIN_READS = _operation_rows(WINDOWS, 'read', 'urio[iocp]')
WIN_WRITES = _operation_rows(WINDOWS, 'write', 'urio[iocp]')
WIN_FSYNC = _operation_rows(WINDOWS, 'write+fsync', 'urio[iocp]')

NOGIL_URING_READS = [
    (
        f'binary {label}',
        _ratio(LINUX[('binary', size, 'read')], 'urio[uring]'),
        _ratio(LINUX_NOGIL[('binary', size, 'read')], 'urio[uring]'),
    )
    for label, size in SIZES
]

WIN_NOGIL_CELLS = [
    ('4 KB write+fsync', 'binary', 4 * 1024, 'write+fsync'),
    ('4 KB read (binary)', 'binary', 4 * 1024, 'read'),
    ('1 MB read (text)', 'text', 1024 * 1024, 'read'),
    ('8 MB read (text)', 'text', 8 * 1024 * 1024, 'read'),
    ('8 MB read (binary)', 'binary', 8 * 1024 * 1024, 'read'),
]
NOGIL_WIN_IOCP = [
    (
        label,
        _ratio(WINDOWS[(mode, size, operation)], 'urio[iocp]'),
        _ratio(WINDOWS_NOGIL[(mode, size, operation)], 'urio[iocp]'),
    )
    for label, mode, size, operation in WIN_NOGIL_CELLS
]

ZFS_FSYNC = [
    (
        label,
        _median(LINUX[('binary', size, 'write+fsync')], 'urio[uring]') * 1000,
        _median(ZFS[('binary', size, 'write+fsync')], 'urio[uring]') * 1000,
    )
    for label, size in SIZES
]
EXT4_C = '#3b6ea5'
ZFS_C = '#e08a3c'

FASTER = '#2a9d4a'
SLOWER = '#d9772b'
URING = '#3b6ea5'
THREAD = '#8fb3d9'
AIOFILES = '#b0b0b0'
GIL_C = '#3b6ea5'
NOGIL_C = '#e08a3c'
TEXT = '#222222'
GRID = '#cccccc'


SIZE_COLORS = [
    ('4 KB', '#3b6ea5'),
    ('64 KB', '#2a9d4a'),
    ('1 MB', '#e08a3c'),
    ('8 MB', '#b5539c'),
]


def _esc(s: str) -> str:
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _speedups_by_size(rows):
    """From an 8-row (binary 4/64/1M/8M, text 4/64/1M/8M) table of
    (label, native ms, thread ms, aiofiles ms), return (binary, text) lists of
    speedup = aiofiles / native."""
    sp = [aio / native for (_l, native, _t, aio) in rows]
    return sp[:4], sp[4:]


def grouped_speedup_chart(title, platform, subtitle, groups, matrix, filename):
    """One dense chart: horizontal bars grouped by operation×mode, with a colour
    per file size (SIZE_COLORS). `groups` are the row labels; `matrix[i]` is the
    list of speedups for group i, aligned with SIZE_COLORS. Dashed line at 1.0×."""
    xmax = max([v for row in matrix for v in row] + [1.0]) * 1.12
    Lm, Rm, Tm, Bm = 120, 58, 104, 30
    bar_h, bar_gap, group_gap = 13, 2, 15
    per_group = len(SIZE_COLORS) * (bar_h + bar_gap) + group_gap
    plot_w = 600
    W = Lm + plot_w + Rm
    H = Tm + per_group * len(groups) + Bm

    def x(v):
        return Lm + (v / xmax) * plot_w

    p = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="sans-serif">',
        f'<rect width="{W}" height="{H}" fill="white"/>',
        f'<rect x="0" y="0" width="{W}" height="6" fill="{URING}"/>',
        f'<text x="{Lm}" y="30" font-size="17" font-weight="bold" '
        f'fill="{TEXT}">{_esc(title)}</text>',
        f'<text x="{Lm}" y="50" font-size="12.5" font-weight="bold" '
        f'fill="{URING}">{_esc(platform)}</text>',
        f'<text x="{Lm}" y="68" font-size="12" fill="#666">{_esc(subtitle)}</text>',
    ]
    lx = Lm
    for slabel, color in SIZE_COLORS:
        p.append(f'<rect x="{lx}" y="80" width="12" height="12" fill="{color}"/>')
        p.append(
            f'<text x="{lx + 16}" y="90" font-size="11" fill="{TEXT}">{slabel}</text>'
        )
        lx += 66
    x1 = x(1.0)
    p.append(
        f'<line x1="{x1:.1f}" y1="{Tm - 8}" x2="{x1:.1f}" y2="{H - Bm + 2}" '
        f'stroke="{GRID}" stroke-width="1.5" stroke-dasharray="4 3"/>'
    )
    p.append(
        f'<text x="{x1:.1f}" y="{H - Bm + 16}" font-size="11" fill="#888" '
        f'text-anchor="middle">1.0x</text>'
    )
    for gi, (glabel, row) in enumerate(zip(groups, matrix, strict=True)):
        gy = Tm + gi * per_group
        p.append(
            f'<text x="{Lm - 8}" y="{gy + (per_group - group_gap) / 2 + 4:.0f}" '
            f'font-size="11" fill="{TEXT}" text-anchor="end">{_esc(glabel)}</text>'
        )
        for si, (val, (_sl, color)) in enumerate(zip(row, SIZE_COLORS, strict=True)):
            by = gy + si * (bar_h + bar_gap)
            bw = x(val) - Lm
            p.append(
                f'<rect x="{Lm}" y="{by}" width="{bw:.1f}" height="{bar_h}" '
                f'fill="{color}" rx="2"/>'
            )
            p.append(
                f'<text x="{x(val) + 5:.1f}" y="{by + bar_h - 3}" font-size="10.5" '
                f'fill="{color}">{val:.2f}x</text>'
            )
    p.append('</svg>')
    (OUT / filename).write_text('\n'.join(p))
    print('wrote', OUT / filename)


def speedup_chart(
    title: str, rows, filename: str, platform: str = 'Linux · io_uring backend'
) -> None:
    """Horizontal bars of speedup = aiofiles / urio[uring] (>1 = urio faster)."""
    speeds = [(label, aio / uring) for (label, uring, _t, aio) in rows]
    xmax = max(max(v for _, v in speeds), 1.0) * 1.15

    Lm, Rm, Tm, Bm = 150, 70, 72, 30
    row_h, bar_h = 30, 18
    plot_w = 640
    W = Lm + plot_w + Rm
    H = Tm + row_h * len(speeds) + Bm

    def x(v):
        return Lm + (v / xmax) * plot_w

    p = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="sans-serif">',
        f'<rect width="{W}" height="{H}" fill="white"/>',
        f'<rect x="0" y="0" width="{W}" height="6" fill="{URING}"/>',
        f'<text x="{Lm}" y="30" font-size="17" font-weight="bold" '
        f'fill="{TEXT}">{_esc(title)}</text>',
        f'<text x="{W - Rm}" y="30" font-size="12" font-weight="bold" '
        f'fill="{URING}" text-anchor="end">{_esc(platform)}</text>',
        f'<text x="{Lm}" y="50" font-size="12" fill="#666">'
        f'speedup vs aiofiles — bars right of the 1.0 line mean urio is faster</text>',
    ]
    x1 = x(1.0)
    p.append(
        f'<line x1="{x1:.1f}" y1="{Tm - 6}" x2="{x1:.1f}" y2="{H - Bm + 4}" '
        f'stroke="{GRID}" stroke-width="1.5" stroke-dasharray="4 3"/>'
    )
    p.append(
        f'<text x="{x1:.1f}" y="{H - Bm + 18}" font-size="11" fill="#888" '
        f'text-anchor="middle">1.0x</text>'
    )
    for i, (label, v) in enumerate(speeds):
        y = Tm + i * row_h
        bw = x(v) - Lm
        color = FASTER if v >= 1.0 else SLOWER
        p.append(
            f'<text x="{Lm - 8}" y="{y + bar_h - 4}" font-size="12" '
            f'fill="{TEXT}" text-anchor="end">{_esc(label)}</text>'
        )
        p.append(
            f'<rect x="{Lm}" y="{y}" width="{bw:.1f}" height="{bar_h}" '
            f'fill="{color}" rx="2"/>'
        )
        p.append(
            f'<text x="{x(v) + 6:.1f}" y="{y + bar_h - 4}" font-size="12" '
            f'font-weight="bold" fill="{color}">{v:.2f}x</text>'
        )
    p.append('</svg>')
    (OUT / filename).write_text('\n'.join(p))
    print('wrote', OUT / filename)


def gil_speedup_chart(
    title: str, rows, filename: str, platform: str, subtitle: str
) -> None:
    """Two bars per workload — GIL vs no-GIL speedup (aiofiles / urio).

    Colour encodes the interpreter (GIL vs no-GIL, the dimension of interest);
    the dashed 1.0x line shows the outcome (right = urio faster). This is how the
    free-threading effect is made visible per workload and per OS/backend.
    """
    xmax = max([max(g, n) for _, g, n in rows] + [1.0]) * 1.18

    Lm, Rm, Tm, Bm = 170, 40, 116, 34
    row_h, bar_h, bar_gap = 46, 16, 4
    plot_w = 610
    W = Lm + plot_w + Rm
    H = Tm + row_h * len(rows) + Bm

    def x(v):
        return Lm + (v / xmax) * plot_w

    p = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="sans-serif">',
        f'<rect width="{W}" height="{H}" fill="white"/>',
        f'<rect x="0" y="0" width="{W}" height="6" fill="{NOGIL_C}"/>',
        f'<text x="{Lm}" y="30" font-size="17" font-weight="bold" '
        f'fill="{TEXT}">{_esc(title)}</text>',
        f'<text x="{Lm}" y="50" font-size="12.5" font-weight="bold" '
        f'fill="{NOGIL_C}">{_esc(platform)}</text>',
        f'<text x="{Lm}" y="68" font-size="12" fill="#666">{_esc(subtitle)}</text>',
    ]
    ly = 88
    p.append(f'<rect x="{Lm}" y="{ly}" width="12" height="12" fill="{GIL_C}"/>')
    p.append(
        f'<text x="{Lm + 16}" y="{ly + 10}" font-size="11" fill="{TEXT}">GIL</text>'
    )
    p.append(f'<rect x="{Lm + 70}" y="{ly}" width="12" height="12" fill="{NOGIL_C}"/>')
    p.append(
        f'<text x="{Lm + 86}" y="{ly + 10}" font-size="11" fill="{TEXT}">no-GIL '
        '(free-threaded)</text>'
    )
    x1 = x(1.0)
    p.append(
        f'<line x1="{x1:.1f}" y1="{Tm - 8}" x2="{x1:.1f}" y2="{H - Bm + 4}" '
        f'stroke="{GRID}" stroke-width="1.5" stroke-dasharray="4 3"/>'
    )
    p.append(
        f'<text x="{x1:.1f}" y="{H - Bm + 18}" font-size="11" fill="#888" '
        f'text-anchor="middle">1.0x (parity)</text>'
    )
    for i, (label, g, n) in enumerate(rows):
        y0 = Tm + i * row_h
        p.append(
            f'<text x="{Lm - 8}" y="{y0 + bar_h}" font-size="12" '
            f'fill="{TEXT}" text-anchor="end">{_esc(label)}</text>'
        )
        for j, (val, color) in enumerate(((g, GIL_C), (n, NOGIL_C))):
            by = y0 + j * (bar_h + bar_gap)
            bw = x(val) - Lm
            p.append(
                f'<rect x="{Lm}" y="{by}" width="{bw:.1f}" height="{bar_h}" '
                f'fill="{color}" rx="2"/>'
            )
            p.append(
                f'<text x="{x(val) + 6:.1f}" y="{by + bar_h - 3}" font-size="11" '
                f'font-weight="bold" fill="{color}">{val:.2f}x</text>'
            )
    p.append('</svg>')
    (OUT / filename).write_text('\n'.join(p))
    print('wrote', OUT / filename)


def zfs_fsync_chart(title, subtitle, rows, filename) -> None:
    """Show fsync-workload times on two machines; hardware also differs."""
    xmax = max(v for _, _e, z in rows for v in (_e, z)) * 1.22
    Lm, Rm, Tm, Bm = 90, 60, 108, 26
    row_h, bar_h, bar_gap = 46, 16, 4
    plot_w = 600
    W = Lm + plot_w + Rm
    H = Tm + row_h * len(rows) + Bm

    def x(v):
        return Lm + (v / xmax) * plot_w

    p = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="sans-serif">',
        f'<rect width="{W}" height="{H}" fill="white"/>',
        f'<rect x="0" y="0" width="{W}" height="6" fill="{ZFS_C}"/>',
        f'<text x="{Lm}" y="30" font-size="17" font-weight="bold" '
        f'fill="{TEXT}">{_esc(title)}</text>',
        f'<text x="{Lm}" y="50" font-size="12.5" font-weight="bold" '
        f'fill="{ZFS_C}">binary write+fsync · urio[uring] median ms · lower is faster</text>',
        f'<text x="{Lm}" y="68" font-size="12" fill="#666">{_esc(subtitle)}</text>',
    ]
    ly = 88
    p.append(f'<rect x="{Lm}" y="{ly}" width="12" height="12" fill="{EXT4_C}"/>')
    p.append(
        f'<text x="{Lm + 16}" y="{ly + 10}" font-size="11" fill="{TEXT}">ext4 on '
        'SATA HDD</text>'
    )
    p.append(f'<rect x="{Lm + 150}" y="{ly}" width="12" height="12" fill="{ZFS_C}"/>')
    p.append(
        f'<text x="{Lm + 166}" y="{ly + 10}" font-size="11" fill="{TEXT}">ZFS</text>'
    )
    for i, (label, e, z) in enumerate(rows):
        y0 = Tm + i * row_h
        p.append(
            f'<text x="{Lm - 8}" y="{y0 + bar_h}" font-size="12" '
            f'fill="{TEXT}" text-anchor="end">{_esc(label)}</text>'
        )
        for j, (val, color, extra) in enumerate(
            (
                (e, EXT4_C, f'{e:.1f} ms'),
                (z, ZFS_C, f'{z:.1f} ms ({z / e:.1f}×)'),
            )
        ):
            by = y0 + j * (bar_h + bar_gap)
            bw = max(x(val) - Lm, 1.0)
            p.append(
                f'<rect x="{Lm}" y="{by}" width="{bw:.1f}" height="{bar_h}" '
                f'fill="{color}" rx="2"/>'
            )
            p.append(
                f'<text x="{x(val) + 6:.1f}" y="{by + bar_h - 3}" font-size="11" '
                f'font-weight="bold" fill="{color}">{extra}</text>'
            )
    p.append('</svg>')
    (OUT / filename).write_text('\n'.join(p))
    print('wrote', OUT / filename)


def grouped_chart(title: str, subtitle: str, rows, filename: str) -> None:
    """Grouped vertical bars: per group, urio[uring]/thread/aiofiles (MB/s)."""
    series = [('urio[uring]', URING), ('urio[thread]', THREAD), ('aiofiles', AIOFILES)]
    ymax = max(max(r[1:]) for r in rows) * 1.18

    Lm, Rm, Tm, Bm = 60, 20, 70, 40
    group_w, bar_w, gap = 200, 50, 12
    plot_h = 240
    W = Lm + group_w * len(rows) + Rm
    H = Tm + plot_h + Bm

    def y(v):
        return Tm + plot_h - (v / ymax) * plot_h

    p = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="sans-serif">',
        f'<rect width="{W}" height="{H}" fill="white"/>',
        f'<rect x="0" y="0" width="{W}" height="6" fill="{URING}"/>',
        f'<text x="{Lm}" y="26" font-size="17" font-weight="bold" '
        f'fill="{TEXT}">{_esc(title)}</text>',
        f'<text x="{W - Rm}" y="26" font-size="12" font-weight="bold" '
        f'fill="{URING}" text-anchor="end">Linux · io_uring backend</text>',
        f'<text x="{Lm}" y="44" font-size="12" fill="#666">{_esc(subtitle)}</text>',
        f'<line x1="{Lm}" y1="{Tm + plot_h}" x2="{W - Rm}" y2="{Tm + plot_h}" '
        f'stroke="{GRID}"/>',
    ]
    lx = Lm
    for name, color in series:
        p.append(
            f'<rect x="{lx}" y="{Tm - 16}" width="12" height="12" fill="{color}"/>'
        )
        p.append(
            f'<text x="{lx + 16}" y="{Tm - 6}" font-size="11" fill="{TEXT}">'
            f'{_esc(name)}</text>'
        )
        lx += 110
    for gi, row in enumerate(rows):
        label, vals = row[0], row[1:]
        gx = Lm + gi * group_w + (group_w - (bar_w * 3 + gap * 2)) / 2
        for bi, (v, (_n, color)) in enumerate(zip(vals, series, strict=True)):
            bx = gx + bi * (bar_w + gap)
            by = y(v)
            bh = Tm + plot_h - by
            p.append(
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w}" height="{bh:.1f}" '
                f'fill="{color}" rx="2"/>'
            )
            p.append(
                f'<text x="{bx + bar_w / 2:.1f}" y="{by - 4:.1f}" font-size="11" '
                f'fill="{TEXT}" text-anchor="middle">{v}</text>'
            )
        p.append(
            f'<text x="{Lm + gi * group_w + group_w / 2:.1f}" '
            f'y="{Tm + plot_h + 20}" font-size="13" fill="{TEXT}" '
            f'text-anchor="middle">{_esc(label)}</text>'
        )
    p.append('</svg>')
    (OUT / filename).write_text('\n'.join(p))
    print('wrote', OUT / filename)


OP_GROUPS = [
    'write · binary',
    'write · text',
    'fsync · binary',
    'fsync · text',
    'read · binary',
    'read · text',
]
_SPEEDUP_SUB = 'aiofiles median / urio median · above 1.0x means urio was faster'


def _op_matrix(writes, fsync, reads):
    wb, wt = _speedups_by_size(writes)
    fb, ft = _speedups_by_size(fsync)
    rb, rt = _speedups_by_size(reads)
    return [wb, wt, fb, ft, rb, rt]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    grouped_speedup_chart(
        'Linux · io_uring vs aiofiles',
        'Archived Linux · CPython 3.14.6 · 5 repeats · fixed order',
        _SPEEDUP_SUB,
        OP_GROUPS,
        _op_matrix(WRITES, FSYNC, READS),
        'bench-linux.svg',
    )
    grouped_speedup_chart(
        'Windows · IOCP vs aiofiles',
        'Archived Windows · CPython 3.14 · 5 repeats · fixed order',
        _SPEEDUP_SUB,
        OP_GROUPS,
        _op_matrix(WIN_WRITES, WIN_FSYNC, WIN_READS),
        'bench-windows.svg',
    )
    nogil_rows = [(f'io_uring · {lbl}', g, n) for lbl, g, n in NOGIL_URING_READS]
    nogil_rows += [(f'IOCP · {lbl}', g, n) for lbl, g, n in NOGIL_WIN_IOCP]
    gil_speedup_chart(
        'Warm reads with and without the GIL',
        nogil_rows,
        'bench-nogil.svg',
        'Archived Linux and Windows · CPython 3.14 vs 3.14t',
        'aiofiles median / urio median · above 1.0x means urio was faster',
    )
    zfs_fsync_chart(
        'Write and fsync on two Linux machines',
        'Archived CPython 3.14.6 · 5/7 repeats · different hardware',
        ZFS_FSYNC,
        'bench-zfs.svg',
    )


if __name__ == '__main__':
    main()
