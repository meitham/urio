#!/usr/bin/env python3
"""Render the benchmark results as SVG bar charts (no dependencies).

Run after updating the numbers below from a `bench_vs_aiofiles.py --markdown`
run; it writes the charts referenced by the README and the docs into
docs/assets/. SVG is used so the charts render crisply in GitHub/Gitea markdown
and MkDocs alike, and stay readable on light and dark themes.
"""

from __future__ import annotations

import pathlib

OUT = pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'assets'

# (scenario label, urio[uring] ms, urio[thread] ms, aiofiles ms)
# Warm cache, MEDIAN of 7 runs on a Linux 4-core i5-6500, kernel 7.0, ext4-on-HDD
# box, CPython 3.14.6; reproduce with `bench_vs_aiofiles.py --markdown --json out.json`.
READS = [
    ('binary 4 KB', 81, 307, 287),
    ('binary 64 KB', 22, 71, 72),
    ('binary 1 MB', 26, 25, 26),
    ('binary 8 MB', 36, 23, 25),
    ('text 4 KB', 100, 324, 288),
    ('text 64 KB', 33, 81, 85),
    ('text 1 MB', 56, 82, 77),
    ('text 8 MB', 106, 149, 111),
]
WRITES = [
    ('binary 4 KB', 76, 271, 280),
    ('binary 64 KB', 14, 56, 74),
    ('binary 1 MB', 17, 25, 25),
    ('binary 8 MB', 23, 23, 23),
    ('text 4 KB', 108, 279, 314),
    ('text 64 KB', 21, 65, 78),
    ('text 1 MB', 50, 62, 35),
    ('text 8 MB', 136, 110, 38),
]
FSYNC = [
    ('binary 4 KB', 94, 323, 423),
    ('binary 64 KB', 17, 70, 99),
    ('binary 1 MB', 16, 28, 32),
    ('binary 8 MB', 24, 25, 25),
    ('text 4 KB', 114, 332, 441),
    ('text 64 KB', 23, 79, 106),
    ('text 1 MB', 48, 65, 42),
    ('text 8 MB', 115, 110, 40),
]
# Cold cache (page cache evicted before each read), binary, median of 5.
COLD_READS = [
    ('binary 4 KB', 92, 269, 283),
    ('binary 64 KB', 32, 72, 72),
    ('binary 1 MB', 37, 31, 28),
    ('binary 8 MB', 39, 21, 25),
    ('128 MB stream', 22, 44, 43),
]
# (mode, urio[uring] MB/s, urio[thread] MB/s, aiofiles MB/s), warm, median of 7.
STREAM = [
    ('binary', 6447, 3471, 3329),
    ('text', 2249, 1814, 1783),
]

# Windows IOCP: (label, urio[iocp] ms, thread ms, aiofiles ms). MEDIAN of 5 on
# a Win10 8-core box, CPython 3.14, GIL. IOCP reaps completions on the loop
# thread via asyncio's ProactorEventLoop IOCP with zero-copy writes AND
# zero-copy reads (the kernel fills the returned bytes). Durable writes and
# large text win; binary reads still lose — the warm ReadFile copy completes
# inline on the loop thread, while aiofiles spreads it across the pool.
WIN_WRITES = [
    ('binary 4 KB', 113, 113, 113),
    ('binary 64 KB', 57, 66, 76),
    ('binary 1 MB', 28, 20, 23),
    ('binary 8 MB', 33, 21, 16),
    ('text 4 KB', 124, 122, 120),
    ('text 64 KB', 118, 170, 157),
    ('text 1 MB', 259, 406, 490),
    ('text 8 MB', 553, 562, 849),
]
WIN_FSYNC = [
    ('binary 4 KB', 207, 221, 230),
    ('binary 64 KB', 144, 166, 173),
    ('binary 1 MB', 132, 136, 128),
    ('binary 8 MB', 217, 184, 194),
    ('text 4 KB', 228, 244, 260),
    ('text 64 KB', 206, 236, 280),
    ('text 1 MB', 383, 550, 653),
    ('text 8 MB', 778, 786, 1104),
]
WIN_READS = [
    ('binary 4 KB', 190, 80, 88),
    ('binary 64 KB', 97, 47, 50),
    ('binary 1 MB', 30, 17, 18),
    ('binary 8 MB', 72, 20, 18),
    ('text 4 KB', 187, 91, 101),
    ('text 64 KB', 141, 105, 120),
    ('text 1 MB', 290, 364, 503),
    ('text 8 MB', 547, 613, 730),
]

# Free-threading: GIL vs no-GIL on the SAME interpreter version. Each row is
# (workload, GIL speedup, no-GIL speedup); speedup = aiofiles / urio (uring on
# Linux, iocp on Windows). The unifying story: urio's warm-read edge is
# memcpy/codec-bound, so removing the GIL lets aiofiles (and urio's own thread
# backend) copy+decode across all cores and the edge erodes — on both backends.

# Linux io_uring warm binary reads · CPython 3.14.6 vs 3.14.6t (matched build),
# median of 7 on the Linux 4-core i5-6500, kernel 7.0 box. speedup = aiofiles /
# urio[uring]. Small-read wins narrow under no-GIL (aiofiles copies across
# cores) but stay wins; large reads — zero-copy handoff + io-wq punt — hold
# ~parity or better in both GIL states.
NOGIL_URING_READS = [
    ('binary 4 KB', 3.53, 2.21),
    ('binary 64 KB', 3.23, 1.55),
    ('binary 1 MB', 0.99, 1.20),
    ('binary 8 MB', 0.69, 0.92),
]
# Windows IOCP (zero-copy) · CPython 3.14 vs 3.14t · speedup aiofiles / urio[iocp],
# median of 5. Binary reads lose in both GIL states (inline loop-thread copy);
# large text reads WIN under the GIL and drop below parity without it
# (aiofiles/thread parallelise the decode+copy across cores); durable small
# writes hold.
NOGIL_WIN_IOCP = [
    ('4 KB write+fsync', 1.11, 1.13),
    ('4 KB read (binary)', 0.47, 0.64),
    ('1 MB read (text)', 1.74, 0.75),
    ('8 MB read (text)', 1.34, 0.69),
    ('8 MB read (binary)', 0.25, 0.33),
]

# Filesystem impact — ext4-on-HDD vs ZFS, SAME urio build (HEAD), CPython 3.14.6
# GIL, median of 7. urio[uring] absolute median ms for binary `write+fsync`, by
# size. On ZFS every fsync forces a ZIL/txg commit, so durable-write latency
# explodes — but aiofiles pays the identical tax (speedup vs aiofiles stays
# ~1.0x), so this is the filesystem's cost, not urio's. The two boxes also differ
# in CPU/RAM/kernel, so this is a ZFS *data point*, not a clean ext4-vs-ZFS A/B.
# (ext4 ms = the FSYNC binary rows above; ZFS ms from the -zfs dataset.)
ZFS_FSYNC = [  # (size, ext4 ms, ZFS ms)
    ('4 KB', 94, 378),
    ('64 KB', 17, 416),
    ('1 MB', 16, 656),
    ('8 MB', 24, 1695),
]
EXT4_C = '#3b6ea5'
ZFS_C = '#e08a3c'

FASTER = '#2a9d4a'  # urio faster than aiofiles
SLOWER = '#d9772b'  # urio slower than aiofiles
URING = '#3b6ea5'
THREAD = '#8fb3d9'
AIOFILES = '#b0b0b0'
GIL_C = '#3b6ea5'  # GIL series
NOGIL_C = '#e08a3c'  # no-GIL series
TEXT = '#222222'
GRID = '#cccccc'


# One colour per file size — used across the consolidated grouped charts so a
# reader learns the palette once.
SIZE_COLORS = [
    ('4 KB', '#3b6ea5'),  # blue
    ('64 KB', '#2a9d4a'),  # green
    ('1 MB', '#e08a3c'),  # orange
    ('8 MB', '#b5539c'),  # purple
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
    # legend (sizes)
    lx = Lm
    for slabel, color in SIZE_COLORS:
        p.append(f'<rect x="{lx}" y="80" width="12" height="12" fill="{color}"/>')
        p.append(
            f'<text x="{lx + 16}" y="90" font-size="11" fill="{TEXT}">{slabel}</text>'
        )
        lx += 66
    # parity line
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
    # 1.0 parity line
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

    # Title, platform, and subtitle each get their own line so long labels
    # (which name OS · backend · Python build) never collide or clip.
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
    # legend
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
    """Horizontal grouped bars of absolute urio[uring] median ms, ext4 vs ZFS,
    per size. Values are labelled in ms with the ZFS/ext4 factor on the ZFS bar,
    so the durable-write filesystem tax is legible despite the wide range."""
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
                (e, EXT4_C, f'{e} ms'),
                (z, ZFS_C, f'{z} ms ({z / e:.0f}×)'),
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
    # legend
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
_SPEEDUP_SUB = 'speedup vs aiofiles — bars past the 1.0x line mean urio is faster'


def _op_matrix(writes, fsync, reads):
    wb, wt = _speedups_by_size(writes)
    fb, ft = _speedups_by_size(fsync)
    rb, rt = _speedups_by_size(reads)
    return [wb, wt, fb, ft, rb, rt]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # Three dense charts, one per platform/topic, rather than a dozen small ones.
    grouped_speedup_chart(
        'Linux · io_uring vs aiofiles',
        'Linux · io_uring · CPython 3.14 · median of 7',
        _SPEEDUP_SUB,
        OP_GROUPS,
        _op_matrix(WRITES, FSYNC, READS),
        'bench-linux.svg',
    )
    grouped_speedup_chart(
        'Windows · IOCP vs aiofiles',
        'Windows · IOCP · CPython 3.14 · median of 5',
        _SPEEDUP_SUB,
        OP_GROUPS,
        _op_matrix(WIN_WRITES, WIN_FSYNC, WIN_READS),
        'bench-windows.svg',
    )
    nogil_rows = [(f'io_uring · {lbl}', g, n) for lbl, g, n in NOGIL_URING_READS]
    nogil_rows += [(f'IOCP · {lbl}', g, n) for lbl, g, n in NOGIL_WIN_IOCP]
    gil_speedup_chart(
        'Free-threading shifts warm reads to the thread pool',
        nogil_rows,
        'bench-nogil.svg',
        'Linux io_uring & Windows IOCP · CPython 3.14 vs 3.14t',
        'urio runs GIL-free (gil_used=false); no-GIL lets urio’s thread backend, like aiofiles, copy on N cores',
    )
    zfs_fsync_chart(
        'ZFS taxes durable writes',
        'io_uring · CPython 3.14.6 GIL · median of 7 · aiofiles pays the same tax '
        '(speedup ~1.0×)',
        ZFS_FSYNC,
        'bench-zfs.svg',
    )


if __name__ == '__main__':
    main()
