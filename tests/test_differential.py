#!/usr/bin/env python3
"""Differential tests: random op sequences vs the stdlib ``io`` oracle.

The buffered and text layers re-implement subtle ``io`` semantics (positional
buffering, universal newlines, size-capped readline, truncate). These tests
drive urio and the equivalent ``io`` object with the same seeded-random op
sequence and require identical observable behaviour at every step.
"""

import io
import random

import pytest

import urio

# Alphabet biased towards newlines so readline paths get real coverage.
_ALPHABET = b'ab\ncd\ne\n\nf'


def _payload(rng, n):
    return bytes(rng.choices(_ALPHABET, k=n))


@pytest.mark.parametrize('seed', [1, 2, 3])
async def test_binary_ops_match_stdlib_file(backend, tmp_path, seed):
    # given: an empty urio file and a real stdlib file as the oracle (a real
    # file, not BytesIO: ftruncate beyond EOF zero-extends a file but does not
    # extend a BytesIO buffer)
    rng = random.Random(seed)
    oracle = open(tmp_path / 'oracle.bin', 'w+b')  # noqa: SIM115
    f = await urio.open(tmp_path / 'subject.bin', 'w+b')
    try:
        # when: applying the same random op sequence to both
        for _ in range(300):
            op = rng.choice(('write', 'read', 'readline', 'seek', 'tell', 'truncate'))
            if op == 'write':
                data = _payload(rng, rng.randrange(0, 4000))
                assert await f.write(data) == oracle.write(data)
            elif op == 'read':
                n = rng.randrange(0, 5000)
                assert await f.read(n) == oracle.read(n)
            elif op == 'readline':
                n = rng.choice((-1, rng.randrange(0, 40)))
                assert await f.readline(n) == oracle.readline(n)
            elif op == 'seek':
                pos = rng.randrange(0, 20000)
                assert await f.seek(pos) == oracle.seek(pos)
            elif op == 'tell':
                assert f.tell() == oracle.tell()
            else:
                size = rng.choice((None, rng.randrange(0, 3000)))
                assert await f.truncate(size) == oracle.truncate(size)

        # then: the full final contents match byte for byte
        await f.seek(0)
        oracle.seek(0)
        assert await f.read() == oracle.read()
    finally:
        await f.close()
        oracle.close()


@pytest.mark.parametrize('seed', [11, 12, 13])
async def test_text_reads_match_textiowrapper(backend, tmp_path, seed):
    # given: a file with messy mixed newlines and multi-byte characters, plus
    # a TextIOWrapper oracle in universal-newline mode over the same bytes
    rng = random.Random(seed)
    pieces = rng.choices(
        ['abc', 'x\n', 'y\r\n', 'z\r', 'äöü', '', 'long' * 40, '\r', '\n\n'],
        k=300,
    )
    payload = ''.join(pieces)
    raw = payload.encode('utf-8')
    p = tmp_path / 'mixed.txt'
    p.write_bytes(raw)
    oracle = io.TextIOWrapper(io.BytesIO(raw), encoding='utf-8')

    # when: driving both with the same random read/readline sequence
    f = await urio.open(p, 'r', encoding='utf-8')
    try:
        for _ in range(150):
            if rng.random() < 0.5:
                n = rng.randrange(0, 60)
                assert await f.read(n) == oracle.read(n)
            else:
                assert await f.readline() == oracle.readline()

        # then: the remainder matches too
        assert await f.read() == oracle.read()
    finally:
        await f.close()


@pytest.mark.parametrize(
    'newline',
    [pytest.param(None, id='universal'), pytest.param('', id='keep')],
)
async def test_readline_newline_modes_match(backend, tmp_path, newline):
    # given: every newline style, including a trailing bare CR
    raw = b'one\ntwo\r\nthree\rfour\n\r\nlast\r'
    p = tmp_path / 'nl.txt'
    p.write_bytes(raw)
    oracle = io.TextIOWrapper(io.BytesIO(raw), encoding='ascii', newline=newline)

    # when: reading line by line in the same newline mode
    f = await urio.open(p, 'r', encoding='ascii', newline=newline)
    try:
        # then: every line (and the EOF sentinel) matches the oracle
        while True:
            expected = oracle.readline()
            assert await f.readline() == expected
            if not expected:
                break
    finally:
        await f.close()
