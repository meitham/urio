"""asyncio integration for the native io_uring ring.

A :class:`Driver` owns one :class:`urio._uring.Ring` and bridges its completions
into the running event loop. The ring registers an eventfd with io_uring; we hand
that fd to ``loop.add_reader`` so the loop wakes whenever completions are ready.
On wake we drain the eventfd, reap every completion, and resolve the matching
future. This keeps everything on the loop thread -- no executor, no extra thread,
genuinely async I/O.

One driver is created lazily per event loop and cached, so a ring is shared by
every coroutine running on that loop.
"""

from __future__ import annotations

import asyncio as _asyncio
import collections as _collections
import errno as _errno
import os as _os
import weakref as _weakref
from typing import Any as _Any

from . import _uring

# The driver is cached as an attribute on the loop object itself, so its
# lifetime is exactly the loop's and a finished loop never pins its ring or
# eventfd. (A WeakKeyDictionary keyed by loop does NOT work here: the driver
# holds a strong reference to the loop, so the dict's strong value reference
# would keep the key alive forever and every closed loop would leak a ring.)
_DRIVER_ATTR = '_urio_uring_driver'
# Fallback for loop implementations that reject attribute assignment: weak
# *values*, so the only strong holder of the driver is the loop's own reader
# registration and everything dies with the loop.
_drivers: _weakref.WeakKeyDictionary = _weakref.WeakKeyDictionary()

# Transient submit errors: completions already in flight will drain the CQ and
# a retry next tick can succeed; anything else is treated as permanent.
_RETRYABLE = (_errno.EAGAIN, _errno.EBUSY)
_MAX_FLUSH_RETRIES = 16


class _Chain:
    """Bookkeeping for one linked open→I/O pair on a fixed-file slot.

    Tracks how many of the two completions are still outstanding so an
    abandoned chain (cancelled or failed awaiter) can close whatever landed in
    the slot and recycle it only once the kernel is done with both ops.
    """

    __slots__ = ('slot', 'open_id', 'open_ok', 'remaining', 'abandoned')

    def __init__(self, slot: int, open_id: int):
        self.slot = slot
        self.open_id = open_id
        self.open_ok = False
        self.remaining = 2
        self.abandoned = False


class Driver:
    """Bridges one io_uring ring into a single asyncio event loop."""

    def __init__(self, loop: _asyncio.AbstractEventLoop, entries: int = 512):
        self._loop = loop
        self._ring = _uring.Ring(entries)
        self._eventfd = self._ring.eventfd
        # user_data -> Future awaiting that completion.
        self._futures: dict[int, _asyncio.Future] = {}
        self._closed = False
        # Cap simultaneously in-flight submissions to the ring depth so the
        # kernel completion queue (sized >= entries) can never overflow, no
        # matter how many coroutines submit at once. This is a hand-rolled O(1)
        # gate: asyncio.Semaphore was profiled as the dominant cost because its
        # _wake_up_next scans the whole waiter deque on every release (O(n) per
        # release -> O(n^2) under hundreds of waiters). Here a release wakes
        # exactly one parked waiter in O(1).
        self._depth = entries
        self._inflight = 0
        self._waiters: _collections.deque[_asyncio.Future] = _collections.deque()
        # Submissions are queued in the ring but not entered into the kernel
        # immediately; a single flush per event-loop tick coalesces every SQE
        # queued in that tick into one io_uring_enter.
        self._flush_scheduled = False
        # Idents queued since the last successful kernel-enter: exactly the ops
        # a failed enter must fail (ops from earlier successful flushes are in
        # the kernel and will still complete normally).
        self._unflushed: list[int] = []
        self._flush_retries = 0
        # Completions to swallow without slot/future accounting (fire-and-forget
        # AsyncCancel / fixed-close ops: no in-flight slot, no awaiter).
        self._untracked: set[int] = set()
        # Fixed-file slots for linked open→I/O chains (empty when the kernel
        # lacks sparse fixed-file registration; callers then fall back).
        self._slots: _collections.deque[int] = _collections.deque(
            range(getattr(self._ring, 'fixed_slots', 0))
        )
        # ident -> _Chain for in-flight linked pairs.
        self._chains: dict[int, _Chain] = {}
        # untracked close ident -> slot to recycle once that close completes
        # (the slot must not be reused while its old fd may still be attached).
        self._recycle_on_done: dict[int, int] = {}
        loop.add_reader(self._eventfd, self._on_ready)

    async def _acquire_slot(self) -> None:
        # Fast path: capacity available (no waiters can exist unless full).
        if self._inflight < self._depth:
            self._inflight += 1
            return
        await self._park_for_slot()

    async def _park_for_slot(self) -> None:
        # Slow path (ring full): park until a completion frees a slot. Kept
        # separate so the hot ops can inline the fast check and only pay a
        # coroutine when they actually have to wait.
        fut = self._loop.create_future()
        self._waiters.append(fut)
        try:
            await fut
            # Granted: the releasing side kept our slot counted (no increment).
        except BaseException:
            # Distinguish "granted then cancelled" (fut has a result -> we hold a
            # slot, give it back) from "parked then cancelled" (asyncio cancels
            # the awaited fut, so fut.cancelled() is True -> we never held a slot).
            if fut.cancelled():
                try:
                    self._waiters.remove(fut)
                except ValueError:
                    pass
            else:
                self._release_slot()
            raise

    def _release_slot(self) -> None:
        # Hand the freed slot to the next live waiter (O(1)), else drop the count.
        while self._waiters:
            w = self._waiters.popleft()
            if not w.done():
                w.set_result(None)
                return
        self._inflight -= 1

    def _schedule_flush(self) -> None:
        if not self._flush_scheduled:
            self._flush_scheduled = True
            self._loop.call_soon(self._flush)

    def _flush(self) -> None:
        self._flush_scheduled = False
        if self._closed:
            return
        try:
            self._ring.submit()
        except OSError as e:
            if e.errno in _RETRYABLE and self._flush_retries < _MAX_FLUSH_RETRIES:
                # CQ momentarily saturated: in-flight completions will drain it
                # via _on_ready; the queued SQEs stay in the SQ ring, so just
                # retry the enter on a later tick.
                self._flush_retries += 1
                self._schedule_flush()
                return
            # Permanent failure: only the SQEs queued since the last successful
            # enter never reached the kernel — fail exactly those (and free
            # their slots). Ops from earlier flushes are still in flight and
            # must keep their slots until their completions are reaped, or the
            # in-flight gate would double-release and over-admit.
            failed, self._unflushed = self._unflushed, []
            self._flush_retries = 0
            for ident in failed:
                fut = self._futures.pop(ident, None)
                self._release_slot()
                if fut is not None and not fut.done():
                    fut.set_exception(
                        OSError(e.errno or 0, f'io_uring submit failed: {e}')
                    )
            return
        self._unflushed.clear()
        self._flush_retries = 0

    def _on_ready(self) -> None:
        """Reader callback: drain the eventfd and resolve completed futures."""
        self._ring.drain_eventfd()
        for ident, result, payload in self._ring.reap():
            if ident in self._untracked:
                # Fire-and-forget op (AsyncCancel / fixed close): no slot was
                # taken and no future awaits it — swallow the completion, and
                # recycle the fixed-file slot if one was waiting on this close.
                self._untracked.discard(ident)
                slot = self._recycle_on_done.pop(ident, None)
                if slot is not None:
                    self._slots.append(slot)
                continue
            chain = self._chains.pop(ident, None)
            if chain is not None:
                chain.remaining -= 1
                if ident == chain.open_id:
                    chain.open_ok = result >= 0
                if chain.abandoned and chain.remaining == 0:
                    self._finish_chain(chain)
            # Every reaped completion frees one in-flight slot. Resolve each
            # future defensively: a failure on one completion must never abandon
            # the rest of the batch (which would leak their slots and hang their
            # awaiters), so the slot release and pop happen unconditionally and
            # set_result/set_exception is guarded.
            self._release_slot()
            fut = self._futures.pop(ident, None)
            if fut is None or fut.done():
                continue
            try:
                if result < 0:
                    fut.set_exception(OSError(-result, _os.strerror(-result)))
                else:
                    fut.set_result((result, payload))
            except Exception:
                if not fut.done():
                    fut.cancel()

    def _await_id(self, ident: int) -> _asyncio.Future:
        fut = self._loop.create_future()
        self._futures[ident] = fut
        self._unflushed.append(ident)
        return fut

    def _cancel_op(self, ident: int) -> None:
        # The awaiting future was cancelled. The op itself keeps running in the
        # kernel (and keeps its in-flight slot) until it completes; ask the
        # kernel to abort it so the slot frees promptly instead of whenever the
        # I/O would have finished. Fire-and-forget: the cancel's own completion
        # is swallowed via _untracked, and the target op's completion still
        # arrives (-ECANCELED or its real result) and does the accounting.
        if self._closed or ident not in self._futures:
            return
        try:
            cid = self._ring.submit_cancel(ident)
        except (OSError, AttributeError):
            return
        self._untracked.add(cid)
        self._schedule_flush()

    async def _run(self, submit) -> tuple[int, _Any]:
        """Submit via ``submit`` (returns an id) and await its completion."""
        if self._closed:
            raise RuntimeError('driver is closed')
        await self._acquire_slot()
        try:
            ident = submit(self._ring)
        except BaseException:
            # Submission never reached the kernel, so no completion will free
            # this slot; release it ourselves.
            self._release_slot()
            raise
        # Queue the kernel-enter for the end of this tick so SQEs batch.
        self._schedule_flush()
        try:
            return await self._await_id(ident)
        except _asyncio.CancelledError:
            self._cancel_op(ident)
            raise

    # -- operations -------------------------------------------------------

    # read/write are the hot path, so they are inlined rather than routed through
    # `_run`: no per-op lambda, no extra coroutine frame, and the in-flight slot
    # is taken with a plain increment when capacity is available (only parking on
    # a coroutine when the ring is actually full). The error/cancellation
    # semantics match `_run` exactly.

    async def read(self, fd: int, offset: int, length: int) -> bytes:
        if self._closed:
            raise RuntimeError('driver is closed')
        if self._inflight < self._depth:
            self._inflight += 1
        else:
            await self._park_for_slot()
        try:
            ident = self._ring.submit_read(fd, offset, length)
        except BaseException:
            self._release_slot()
            raise
        self._schedule_flush()
        try:
            _, payload = await self._await_id(ident)
        except _asyncio.CancelledError:
            self._cancel_op(ident)
            raise
        return payload if payload is not None else b''

    async def write(
        self, fd: int, offset: int, data: bytes, append: bool = False
    ) -> int:
        # io_uring uses a u64 offset of -1 to mean "the file's current offset";
        # with O_APPEND that appends atomically at end-of-file.
        off = (1 << 64) - 1 if append else offset
        if self._closed:
            raise RuntimeError('driver is closed')
        if self._inflight < self._depth:
            self._inflight += 1
        else:
            await self._park_for_slot()
        try:
            ident = self._ring.submit_write(fd, off, data)
        except BaseException:
            self._release_slot()
            raise
        self._schedule_flush()
        try:
            result, _ = await self._await_id(ident)
        except _asyncio.CancelledError:
            self._cancel_op(ident)
            raise
        return result

    async def writev(
        self, fd: int, offset: int, bufs: list[bytes], append: bool = False
    ) -> int:
        off = (1 << 64) - 1 if append else offset
        result, _ = await self._run(lambda r: r.submit_writev(fd, off, bufs))
        return result

    async def fsync(self, fd: int, datasync: bool = False) -> None:
        await self._run(lambda r: r.submit_fsync(fd, datasync))

    async def ftruncate(self, fd: int, length: int) -> None:
        await self._run(lambda r: r.submit_ftruncate(fd, length))

    async def open(self, path: str, flags: int, mode: int) -> int:
        result, _ = await self._run(lambda r: r.submit_open(path, flags, mode))
        return result

    async def close(self, fd: int) -> None:
        await self._run(lambda r: r.submit_close(fd))

    async def statx(self, path: str, follow: bool = True) -> tuple:
        _, payload = await self._run(lambda r: r.submit_statx(path, follow))
        return payload

    async def fstatx(self, fd: int) -> tuple:
        _, payload = await self._run(lambda r: r.submit_fstatx(fd))
        return payload

    async def mkdir(self, path: str, mode: int = 0o777) -> None:
        await self._run(lambda r: r.submit_mkdir(path, mode))

    async def unlink(self, path: str, remove_dir: bool = False) -> None:
        await self._run(lambda r: r.submit_unlink(path, remove_dir))

    async def rename(self, src: str, dst: str) -> None:
        await self._run(lambda r: r.submit_rename(src, dst))

    async def symlink(self, target: str, linkpath: str) -> None:
        await self._run(lambda r: r.submit_symlink(target, linkpath))

    async def link(self, src: str, dst: str) -> None:
        await self._run(lambda r: r.submit_link(src, dst))

    # -- linked open→I/O chains on fixed-file slots ------------------------

    def _finish_chain(self, chain: _Chain) -> None:
        # Both completions of an abandoned chain are reaped: close whatever
        # landed in the slot, then recycle it (immediately if the open failed
        # and the slot is empty).
        if chain.open_ok:
            self.close_fixed_nowait(chain.slot)
        else:
            self._slots.append(chain.slot)

    def _abandon_chain(self, chain: _Chain, *futs: _asyncio.Future) -> None:
        # The awaiter is bailing out (cancellation or a failed half): hand the
        # chain to completion-side cleanup and make sure no unretrieved
        # exception warning fires for the halves nobody will await.
        chain.abandoned = True

        def _swallow(f: _asyncio.Future) -> None:
            if not f.cancelled():
                f.exception()

        for f in futs:
            if f.done():
                _swallow(f)
            else:
                f.add_done_callback(_swallow)
        if chain.remaining == 0:
            self._finish_chain(chain)

    async def _open_chain(self, submit):
        """Common half of open_read/open_write: allocate a slot, take two
        in-flight slots (two completions), submit the linked pair, and await
        both halves with abandoned-chain cleanup. Returns (slot, second-op
        payload/result) or None when no fixed slot is available."""
        if self._closed:
            raise RuntimeError('driver is closed')
        if not self._slots:
            return None
        slot = self._slots.popleft()
        acquired = 0
        try:
            for _ in range(2):
                if self._inflight < self._depth:
                    self._inflight += 1
                else:
                    await self._park_for_slot()
                acquired += 1
            open_id, io_id = submit(self._ring, slot)
        except BaseException:
            for _ in range(acquired):
                self._release_slot()
            self._slots.append(slot)
            raise
        self._schedule_flush()
        ofut = self._await_id(open_id)
        iofut = self._await_id(io_id)
        chain = _Chain(slot, open_id)
        self._chains[open_id] = chain
        self._chains[io_id] = chain
        try:
            await ofut
        except _asyncio.CancelledError:
            self._abandon_chain(chain, ofut, iofut)
            raise
        except OSError:
            # Open failed; the linked op completes with -ECANCELED and the
            # abandoned-chain cleanup recycles the (empty) slot.
            self._abandon_chain(chain, iofut)
            raise
        try:
            result, payload = await iofut
        except _asyncio.CancelledError:
            self._abandon_chain(chain, iofut)
            raise
        except OSError:
            # The fd is installed but its first I/O failed: close and recycle
            # via the abandoned-chain path.
            self._abandon_chain(chain)
            raise
        return slot, (payload if payload is not None else result)

    async def open_read(
        self, path: str, flags: int, mode: int, length: int
    ) -> tuple[int, bytes] | None:
        """Linked open+read: one submission opens ``path`` into a fixed slot
        and reads the first ``length`` bytes. Returns ``(slot, data)`` — the
        caller owns the slot and must release it via ``close_fixed`` /
        ``close_fixed_nowait`` — or None when no slot is free."""
        res = await self._open_chain(
            lambda r, s: r.submit_open_read(path, flags, mode, length, s)
        )
        if res is None:
            return None
        slot, payload = res
        return slot, (payload if isinstance(payload, bytes) else b'')

    async def open_write(
        self, path: str, flags: int, mode: int, data: bytes
    ) -> tuple[int, int] | None:
        """Linked open+write of ``data`` at offset 0. Returns
        ``(slot, bytes_written)`` (caller owns the slot) or None."""
        res = await self._open_chain(
            lambda r, s: r.submit_open_write(path, flags, mode, data, s)
        )
        if res is None:
            return None
        return res

    async def read_fixed(self, slot: int, offset: int, length: int) -> bytes:
        _, payload = await self._run(
            lambda r: r.submit_read_fixed(slot, offset, length)
        )
        return payload if payload is not None else b''

    async def write_fixed(self, slot: int, offset: int, data: bytes) -> int:
        result, _ = await self._run(lambda r: r.submit_write_fixed(slot, offset, data))
        return result

    async def close_fixed(self, slot: int) -> None:
        """Close the direct descriptor in ``slot`` and recycle the slot."""
        try:
            await self._run(lambda r: r.submit_close_fixed(slot))
        finally:
            self._slots.append(slot)

    def close_fixed_nowait(self, slot: int) -> None:
        """Fire-and-forget close of ``slot``. The slot returns to the pool only
        when the close completes, so a new chain can never race the old fd."""
        if self._closed:
            return
        try:
            cid = self._ring.submit_close_fixed(slot)
        except OSError:
            # Could not even queue the close: leave the slot out of the pool
            # rather than risk reusing a slot with a live fd attached.
            return
        self._untracked.add(cid)
        self._recycle_on_done[cid] = slot
        self._schedule_flush()

    # -- lifecycle --------------------------------------------------------

    @property
    def inflight(self) -> int:
        return self._ring.inflight

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.remove_reader(self._eventfd)
        except Exception:
            pass
        for fut in self._futures.values():
            if not fut.done():
                fut.cancel()
        self._futures.clear()
        self._unflushed.clear()
        self._untracked.clear()
        self._chains.clear()
        self._recycle_on_done.clear()
        # Also wake any coroutines parked waiting for an in-flight slot, or they
        # hang forever on a closed driver.
        while self._waiters:
            w = self._waiters.popleft()
            if not w.done():
                w.cancel()


def get_driver(loop: _asyncio.AbstractEventLoop | None = None) -> Driver:
    """Return the driver for ``loop`` (default: the running loop), creating it
    on first use and caching it for the lifetime of the loop."""
    if loop is None:
        loop = _asyncio.get_running_loop()
    driver = getattr(loop, _DRIVER_ATTR, None)
    if driver is None:
        ref = _drivers.get(loop)
        driver = ref() if ref is not None else None
    if driver is None:
        driver = Driver(loop)
        try:
            setattr(loop, _DRIVER_ATTR, driver)
        except AttributeError:
            _drivers[loop] = _weakref.ref(driver)
    return driver


def close_driver(loop: _asyncio.AbstractEventLoop) -> None:
    """Shut down and discard the driver cached on ``loop``, if any."""
    driver = getattr(loop, _DRIVER_ATTR, None)
    if driver is not None:
        try:
            delattr(loop, _DRIVER_ATTR)
        except AttributeError:
            pass
    else:
        ref = _drivers.pop(loop, None)
        driver = ref() if ref is not None else None
    if driver is not None:
        driver.shutdown()
