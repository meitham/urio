//! Native io_uring backend for urio.
//!
//! The `Ring` class below is **Linux-only** (io_uring is a Linux interface). On
//! macOS, Windows, and other platforms this module still compiles — so a wheel
//! exists everywhere — but exposes no `Ring`, and urio falls back to its
//! thread-pool backend. The Linux implementation owns an eventfd registered
//! with io_uring so completions wake the asyncio loop via `loop.add_reader`;
//! every submission stores what the kernel touches in a pending map keyed by
//! `user_data` so it outlives the submission: a Rust-owned `Vec` for reads and
//! paths, and — for writes — the caller's Python `bytes` object itself (held by
//! reference), so the kernel writes from that buffer with no per-write copy.

// Needed to define the extension module on every platform.
use pyo3::prelude::*;

#[cfg(target_os = "linux")]
use std::collections::HashMap;
#[cfg(target_os = "linux")]
use std::ffi::CString;
#[cfg(target_os = "linux")]
use std::io;
#[cfg(target_os = "linux")]
use std::os::unix::io::{AsRawFd, RawFd};

#[cfg(target_os = "linux")]
use io_uring::{opcode, squeue, types, IoUring};
#[cfg(target_os = "linux")]
use pyo3::exceptions::PyOSError;
#[cfg(target_os = "linux")]
use pyo3::types::{PyBytes, PyTuple};

/// Owned state that must stay alive until a submission completes.
#[cfg(target_os = "linux")]
enum Pending {
    // Reads are zero-(extra-)copy: the kernel writes directly into a
    // preallocated, uninitialised Python `bytes` object that only `pending`
    // references. A full read hands that very object back to Python (no
    // memcpy); only a short read pays a copy of the prefix. Mutating the
    // buffer is safe because nothing has observed the object yet — the same
    // reasoning as PyBytes_FromStringAndSize(NULL, n) + fill inside CPython.
    Read {
        buf: Py<PyBytes>,
        cap: usize,
    },
    // Buffers/paths are kept solely so the kernel can read from / write into
    // them while the operation is in flight. For writes we keep the Python
    // `bytes` object itself alive (rather than copying it into a Vec): `bytes`
    // is immutable and CPython never relocates it, and holding this reference in
    // `pending` prevents Python from freeing it — so the kernel can read from
    // the object's own buffer with no per-write copy.
    Write {
        _buf: Py<PyBytes>,
    },
    // Vectored write: the caller's `bytes` objects plus the iovec array the
    // kernel walks — both must outlive the submission.
    Writev {
        _bufs: Vec<Py<PyBytes>>,
        _iov: IovBox,
    },
    Statx {
        _path: CString,
        stx: Box<statx_abi::statx>,
    },
    Open {
        _path: CString,
        direct: bool,
    },
    Paths {
        _paths: Vec<CString>,
    },
    Plain,
}

/// An iovec array for a pending vectored write. `libc::iovec` holds raw
/// pointers, which strip the auto `Send`/`Sync` impls from anything containing
/// them — and through `Pending` from `Ring` itself. The pointers here aim into
/// the immutable `bytes` buffers held alive in the same `Pending::Writev`
/// entry and are only ever dereferenced by the kernel, so moving or sharing
/// the array across threads is sound.
#[cfg(target_os = "linux")]
struct IovBox(#[allow(dead_code)] Box<[libc::iovec]>);

#[cfg(target_os = "linux")]
unsafe impl Send for IovBox {}
#[cfg(target_os = "linux")]
unsafe impl Sync for IovBox {}

/// statx ABI. The layout and flag values come from the kernel's
/// include/uapi/linux/stat.h and are stable kernel ABI, but the libc crate
/// only exposes bindings for them on glibc — musl builds must vendor them.
/// The glibc arm re-exports libc's definitions so the two arms can never
/// drift apart in meaning, only in provenance.
#[cfg(all(target_os = "linux", not(target_env = "musl")))]
mod statx_abi {
    pub use libc::{statx, statx_timestamp, AT_STATX_SYNC_AS_STAT, STATX_BASIC_STATS};
}

#[cfg(all(target_os = "linux", target_env = "musl"))]
mod statx_abi {
    pub const STATX_BASIC_STATS: libc::c_uint = 0x07ff;
    pub const AT_STATX_SYNC_AS_STAT: libc::c_int = 0;

    #[repr(C)]
    pub struct statx_timestamp {
        pub tv_sec: i64,
        pub tv_nsec: u32,
        pub __reserved: i32,
    }

    // include/uapi/linux/stat.h `struct statx` — 256 bytes.
    #[repr(C)]
    pub struct statx {
        pub stx_mask: u32,
        pub stx_blksize: u32,
        pub stx_attributes: u64,
        pub stx_nlink: u32,
        pub stx_uid: u32,
        pub stx_gid: u32,
        pub stx_mode: u16,
        pub __statx_pad1: [u16; 1],
        pub stx_ino: u64,
        pub stx_size: u64,
        pub stx_blocks: u64,
        pub stx_attributes_mask: u64,
        pub stx_atime: statx_timestamp,
        pub stx_btime: statx_timestamp,
        pub stx_ctime: statx_timestamp,
        pub stx_mtime: statx_timestamp,
        pub stx_rdev_major: u32,
        pub stx_rdev_minor: u32,
        pub stx_dev_major: u32,
        pub stx_dev_minor: u32,
        pub stx_mnt_id: u64,
        pub stx_dio_mem_align: u32,
        pub stx_dio_offset_align: u32,
        pub __statx_pad3: [u64; 12],
    }
}

/// Reads at least this large are submitted with IOSQE_ASYNC so their
/// page-cache copies run on io-wq workers (in parallel across cores) instead
/// of inline on the loop thread.
#[cfg(target_os = "linux")]
const ASYNC_READ_MIN: usize = 1 << 20;

/// Size of the sparse fixed-file table registered at ring creation. Linked
/// open→I/O chains install their direct descriptor into one of these slots;
/// the Python driver owns slot allocation.
#[cfg(target_os = "linux")]
const FIXED_SLOTS: u32 = 512;

// Deliberately NOT `unsendable`: the driver confines each ring to its loop
// thread, but the *last reference* can be dropped by GC on any thread — e.g.
// an event loop from `asyncio.run` in a worker thread collected later on the
// main thread. An unsendable class turns that drop into an unraisable
// RuntimeError and leaks the ring. Every field is Send + Sync (see `IovBox`),
// and both `io_uring_enter` and the fd closes in `Drop` are thread-agnostic,
// so a cross-thread drop is sound.
#[cfg(target_os = "linux")]
#[pyclass]
struct Ring {
    ring: IoUring,
    eventfd: RawFd,
    pending: HashMap<u64, Pending>,
    next_id: u64,
    fixed_slots: u32,
}

#[cfg(target_os = "linux")]
fn oserr<E: std::fmt::Display>(e: E) -> PyErr {
    PyOSError::new_err(e.to_string())
}

/// Like `oserr`, but preserves the OS error code so the raised OSError has a
/// real `errno` (matchable by `except` handlers and `e.errno` checks).
#[cfg(target_os = "linux")]
fn os_ioerr(e: io::Error) -> PyErr {
    match e.raw_os_error() {
        Some(code) => PyOSError::new_err((code, e.to_string())),
        None => PyOSError::new_err(e.to_string()),
    }
}

/// Allocate an uninitialised Python `bytes` of exactly `len` bytes and return
/// it with a raw pointer to its internal buffer. The caller must fill the
/// buffer (here: the kernel does) before the object is exposed to Python.
/// `len == 0` returns the shared empty-bytes singleton, which is fine: nothing
/// is ever written through the pointer for a zero-length operation.
#[cfg(target_os = "linux")]
fn alloc_pybytes(py: Python<'_>, len: usize) -> PyResult<(Py<PyBytes>, *mut u8)> {
    unsafe {
        let raw =
            pyo3::ffi::PyBytes_FromStringAndSize(std::ptr::null(), len as pyo3::ffi::Py_ssize_t);
        if raw.is_null() {
            return Err(PyErr::fetch(py));
        }
        let ptr = pyo3::ffi::PyBytes_AsString(raw) as *mut u8;
        let obj: Bound<'_, PyBytes> = Bound::from_owned_ptr(py, raw).cast_into_unchecked();
        Ok((obj.unbind(), ptr))
    }
}

#[cfg(target_os = "linux")]
impl Ring {
    /// Queue an entry in the submission ring **without** entering the kernel.
    /// The driver batches many entries and flushes them with a single
    /// `submit()` (one `io_uring_enter`) per event-loop tick. Only if the
    /// submission queue is momentarily full do we flush early to make room.
    fn push_entry(&mut self, entry: &io_uring::squeue::Entry) -> io::Result<()> {
        // SAFETY: the buffers referenced by `entry` are stored in `pending`
        // before this call and live until the matching completion is reaped.
        let first = unsafe { self.ring.submission().push(entry) };
        if first.is_err() {
            self.ring.submit()?;
            unsafe { self.ring.submission().push(entry) }
                .map_err(|_| io::Error::other("submission queue full"))?;
        }
        Ok(())
    }

    /// Queue a linked pair. The two SQEs must land contiguously in one
    /// submission — the kernel severs an IO_LINK chain at a submit boundary —
    /// so make room for both up front and never flush between them.
    fn push_pair(
        &mut self,
        first: &io_uring::squeue::Entry,
        second: &io_uring::squeue::Entry,
    ) -> io::Result<()> {
        let need_flush = {
            let sq = self.ring.submission();
            sq.capacity() - sq.len() < 2
        };
        if need_flush {
            self.ring.submit()?;
        }
        // SAFETY: as in push_entry — everything the entries reference lives in
        // `pending` until the completions are reaped.
        unsafe {
            let mut sq = self.ring.submission();
            sq.push(first)
                .and_then(|_| sq.push(second))
                .map_err(|_| io::Error::other("submission queue full"))?;
        }
        Ok(())
    }

    fn alloc_id(&mut self) -> u64 {
        let id = self.next_id;
        self.next_id = self.next_id.wrapping_add(1);
        id
    }

    /// Queue `entry` and register `id` as in-flight. On submission-queue
    /// failure, roll back the pending entry so it is not orphaned (which would
    /// leak its owned buffer and permanently inflate `inflight`).
    fn finish(&mut self, id: u64, entry: io_uring::squeue::Entry) -> PyResult<u64> {
        if let Err(e) = self.push_entry(&entry) {
            self.pending.remove(&id);
            return Err(os_ioerr(e));
        }
        Ok(id)
    }
}

#[cfg(target_os = "linux")]
fn cstring(path: &str) -> PyResult<CString> {
    CString::new(path.as_bytes()).map_err(oserr)
}

#[cfg(target_os = "linux")]
#[pymethods]
impl Ring {
    #[new]
    #[pyo3(signature = (entries = 256))]
    fn new(entries: u32) -> PyResult<Self> {
        // Keep the default builder. Cross-thread drop (see `Drop`) relies on
        // the ring being usable from whatever thread GC runs on; opting into
        // IORING_SETUP_SINGLE_ISSUER or SQPOLL would silently break that
        // (enter/reap become issuer-thread-only) — revisit `Drop` first.
        let ring = IoUring::new(entries).map_err(os_ioerr)?;
        // EFD_NONBLOCK so draining never blocks the loop thread; CLOEXEC so the
        // fd does not leak across exec.
        let efd = unsafe { libc::eventfd(0, libc::EFD_NONBLOCK | libc::EFD_CLOEXEC) };
        if efd < 0 {
            return Err(os_ioerr(io::Error::last_os_error()));
        }
        ring.submitter().register_eventfd(efd).map_err(|e| {
            unsafe { libc::close(efd) };
            os_ioerr(e)
        })?;
        // A sparse direct-descriptor table enables linked open→I/O chains
        // (one submission for open+read / open+write). Optional: on kernels
        // without sparse registration the chains are simply not offered.
        let fixed_slots = match ring.submitter().register_files_sparse(FIXED_SLOTS) {
            Ok(()) => FIXED_SLOTS,
            Err(_) => 0,
        };
        Ok(Ring {
            ring,
            eventfd: efd,
            pending: HashMap::new(),
            next_id: 1,
            fixed_slots,
        })
    }

    /// The eventfd to hand to `loop.add_reader`.
    #[getter]
    fn eventfd(&self) -> RawFd {
        self.eventfd
    }

    /// Number of direct-descriptor slots available for linked chains (0 when
    /// the kernel lacks sparse fixed-file registration).
    #[getter]
    fn fixed_slots(&self) -> u32 {
        self.fixed_slots
    }

    /// Number of submissions still awaiting completion.
    #[getter]
    fn inflight(&self) -> usize {
        self.pending.len()
    }

    /// Flush all queued submissions to the kernel in one `io_uring_enter`.
    /// Called once per event-loop tick by the driver after a batch of pushes.
    fn submit(&mut self) -> PyResult<()> {
        self.ring.submit().map_err(os_ioerr)?;
        Ok(())
    }

    /// Clear the eventfd counter. Reads until EAGAIN so the level-triggered
    /// reader does not spin.
    fn drain_eventfd(&self) {
        let mut buf = [0u8; 8];
        loop {
            let n = unsafe { libc::read(self.eventfd, buf.as_mut_ptr() as *mut libc::c_void, 8) };
            if n != 8 {
                break;
            }
        }
    }

    fn submit_read(&mut self, py: Python<'_>, fd: RawFd, offset: u64, len: u32) -> PyResult<u64> {
        let id = self.alloc_id();
        // Zero-copy: the kernel writes directly into a fresh uninitialised
        // `bytes` object. A full read hands the object to Python as-is (the
        // old Vec path paid a second full memcpy in `reap`); only a short read
        // copies, and then only the bytes actually read. No zero-fill either —
        // the kernel overwrites the buffer and the uninitialised tail of a
        // short read is never exposed.
        let (buf, ptr) = alloc_pybytes(py, len as usize)?;
        self.pending.insert(
            id,
            Pending::Read {
                buf,
                cap: len as usize,
            },
        );
        let mut entry = opcode::Read::new(types::Fd(fd), ptr, len)
            .offset(offset)
            .build()
            .user_data(id);
        // Large warm reads complete inline at submit time, serialising every
        // page-cache memcpy on the loop thread. IOSQE_ASYNC punts them to
        // io-wq workers so concurrent big reads copy on multiple cores (the
        // same parallelism a thread pool gets). Small reads stay inline —
        // the punt costs more than the copy there.
        if len as usize >= ASYNC_READ_MIN {
            entry = entry.flags(squeue::Flags::ASYNC);
        }
        self.finish(id, entry)
    }

    fn submit_write(&mut self, fd: RawFd, offset: u64, data: Bound<'_, PyBytes>) -> PyResult<u64> {
        let id = self.alloc_id();
        // Zero-copy: point the kernel at the Python bytes' own buffer and keep
        // the object alive in `pending` until the write completes. No to_vec().
        // The raw pointer stays valid because `bytes` is immutable, never
        // relocated by CPython, and the stored reference prevents its free.
        let (ptr, len) = {
            let b = data.as_bytes();
            (b.as_ptr(), b.len() as u32)
        };
        self.pending.insert(
            id,
            Pending::Write {
                _buf: data.unbind(),
            },
        );
        let entry = opcode::Write::new(types::Fd(fd), ptr, len)
            .offset(offset)
            .build()
            .user_data(id);
        self.finish(id, entry)
    }

    /// Vectored write: one submission, one iovec per buffer — no join copy.
    /// Zero-copy like `submit_write`: the kernel reads each `bytes` object's
    /// own buffer, all held alive in `pending` until completion.
    fn submit_writev(
        &mut self,
        fd: RawFd,
        offset: u64,
        bufs: Vec<Bound<'_, PyBytes>>,
    ) -> PyResult<u64> {
        let id = self.alloc_id();
        let iov: Box<[libc::iovec]> = bufs
            .iter()
            .map(|b| {
                let s = b.as_bytes();
                libc::iovec {
                    iov_base: s.as_ptr() as *mut libc::c_void,
                    iov_len: s.len(),
                }
            })
            .collect();
        let iov_ptr = iov.as_ptr();
        let cnt = iov.len() as u32;
        let owned: Vec<Py<PyBytes>> = bufs.into_iter().map(|b| b.unbind()).collect();
        self.pending.insert(
            id,
            Pending::Writev {
                _bufs: owned,
                _iov: IovBox(iov),
            },
        );
        let entry = opcode::Writev::new(types::Fd(fd), iov_ptr, cnt)
            .offset(offset)
            .build()
            .user_data(id);
        self.finish(id, entry)
    }

    /// Ask the kernel to abort the in-flight op submitted with `user_data`.
    /// Fire-and-forget: the target's own completion still arrives (with
    /// -ECANCELED or its real result); this op's completion reports whether
    /// the cancellation found anything, which the driver ignores.
    fn submit_cancel(&mut self, user_data: u64) -> PyResult<u64> {
        let id = self.alloc_id();
        self.pending.insert(id, Pending::Plain);
        let entry = opcode::AsyncCancel::new(user_data).build().user_data(id);
        self.finish(id, entry)
    }

    /// ftruncate(2) as an io_uring op (kernel 6.9+). On older kernels the
    /// completion carries -EINVAL/-EOPNOTSUPP and the backend falls back to
    /// the executor.
    fn submit_ftruncate(&mut self, fd: RawFd, len: u64) -> PyResult<u64> {
        let id = self.alloc_id();
        self.pending.insert(id, Pending::Plain);
        let entry = opcode::Ftruncate::new(types::Fd(fd), len)
            .build()
            .user_data(id);
        self.finish(id, entry)
    }

    #[pyo3(signature = (fd, datasync = false))]
    fn submit_fsync(&mut self, fd: RawFd, datasync: bool) -> PyResult<u64> {
        let id = self.alloc_id();
        let mut op = opcode::Fsync::new(types::Fd(fd));
        if datasync {
            op = op.flags(types::FsyncFlags::DATASYNC);
        }
        self.pending.insert(id, Pending::Plain);
        let entry = op.build().user_data(id);
        self.finish(id, entry)
    }

    fn submit_open(&mut self, path: &str, flags: i32, mode: u32) -> PyResult<u64> {
        let id = self.alloc_id();
        let cpath = cstring(path)?;
        let ptr = cpath.as_ptr();
        self.pending.insert(
            id,
            Pending::Open {
                _path: cpath,
                direct: false,
            },
        );
        let entry = opcode::OpenAt::new(types::Fd(libc::AT_FDCWD), ptr)
            .flags(flags)
            .mode(mode)
            .build()
            .user_data(id);
        self.finish(id, entry)
    }

    fn submit_close(&mut self, fd: RawFd) -> PyResult<u64> {
        let id = self.alloc_id();
        self.pending.insert(id, Pending::Plain);
        let entry = opcode::Close::new(types::Fd(fd)).build().user_data(id);
        self.finish(id, entry)
    }

    // -- linked open→I/O chains on direct descriptors ----------------------
    //
    // One submission opens the path straight into fixed-file slot `slot` and
    // (IO_LINK) performs the first I/O on it — a whole small-file read or
    // write costs a single submit/wakeup round-trip instead of three. The
    // Python driver owns slot allocation and closes the slot afterwards. If
    // the open fails, the linked op completes with -ECANCELED.

    /// Open `path` into `slot` and read `len` bytes from offset 0, linked.
    /// Returns `(open_id, read_id)`.
    fn submit_open_read(
        &mut self,
        py: Python<'_>,
        path: &str,
        flags: i32,
        mode: u32,
        len: u32,
        slot: u32,
    ) -> PyResult<(u64, u64)> {
        let open_id = self.alloc_id();
        let read_id = self.alloc_id();
        let cpath = cstring(path)?;
        let path_ptr = cpath.as_ptr();
        let dest = types::DestinationSlot::try_from_slot_target(slot)
            .map_err(|_| oserr("invalid fixed-file slot"))?;
        let (buf, buf_ptr) = alloc_pybytes(py, len as usize)?;
        let open_entry = opcode::OpenAt::new(types::Fd(libc::AT_FDCWD), path_ptr)
            .flags(flags)
            .mode(mode)
            .file_index(Some(dest))
            .build()
            .flags(squeue::Flags::IO_LINK)
            .user_data(open_id);
        let read_entry = opcode::Read::new(types::Fixed(slot), buf_ptr, len)
            .offset(0)
            .build()
            .user_data(read_id);
        self.pending.insert(
            open_id,
            Pending::Open {
                _path: cpath,
                direct: true,
            },
        );
        self.pending.insert(
            read_id,
            Pending::Read {
                buf,
                cap: len as usize,
            },
        );
        if let Err(e) = self.push_pair(&open_entry, &read_entry) {
            self.pending.remove(&open_id);
            self.pending.remove(&read_id);
            return Err(os_ioerr(e));
        }
        Ok((open_id, read_id))
    }

    /// Open `path` into `slot` and write `data` at offset 0, linked (zero-copy
    /// from the caller's `bytes`). Returns `(open_id, write_id)`.
    fn submit_open_write(
        &mut self,
        path: &str,
        flags: i32,
        mode: u32,
        data: Bound<'_, PyBytes>,
        slot: u32,
    ) -> PyResult<(u64, u64)> {
        let open_id = self.alloc_id();
        let write_id = self.alloc_id();
        let cpath = cstring(path)?;
        let path_ptr = cpath.as_ptr();
        let dest = types::DestinationSlot::try_from_slot_target(slot)
            .map_err(|_| oserr("invalid fixed-file slot"))?;
        let (data_ptr, data_len) = {
            let b = data.as_bytes();
            (b.as_ptr(), b.len() as u32)
        };
        let open_entry = opcode::OpenAt::new(types::Fd(libc::AT_FDCWD), path_ptr)
            .flags(flags)
            .mode(mode)
            .file_index(Some(dest))
            .build()
            .flags(squeue::Flags::IO_LINK)
            .user_data(open_id);
        let write_entry = opcode::Write::new(types::Fixed(slot), data_ptr, data_len)
            .offset(0)
            .build()
            .user_data(write_id);
        self.pending.insert(
            open_id,
            Pending::Open {
                _path: cpath,
                direct: true,
            },
        );
        self.pending.insert(
            write_id,
            Pending::Write {
                _buf: data.unbind(),
            },
        );
        if let Err(e) = self.push_pair(&open_entry, &write_entry) {
            self.pending.remove(&open_id);
            self.pending.remove(&write_id);
            return Err(os_ioerr(e));
        }
        Ok((open_id, write_id))
    }

    /// Read from the direct descriptor in `slot` (continuation reads for files
    /// larger than the chain's first read).
    fn submit_read_fixed(
        &mut self,
        py: Python<'_>,
        slot: u32,
        offset: u64,
        len: u32,
    ) -> PyResult<u64> {
        let id = self.alloc_id();
        let (buf, ptr) = alloc_pybytes(py, len as usize)?;
        self.pending.insert(
            id,
            Pending::Read {
                buf,
                cap: len as usize,
            },
        );
        let mut entry = opcode::Read::new(types::Fixed(slot), ptr, len)
            .offset(offset)
            .build()
            .user_data(id);
        // Same io-wq punt as submit_read: parallelise big warm-read copies.
        if len as usize >= ASYNC_READ_MIN {
            entry = entry.flags(squeue::Flags::ASYNC);
        }
        self.finish(id, entry)
    }

    /// Write to the direct descriptor in `slot` (continuation writes after a
    /// short chained write).
    fn submit_write_fixed(
        &mut self,
        slot: u32,
        offset: u64,
        data: Bound<'_, PyBytes>,
    ) -> PyResult<u64> {
        let id = self.alloc_id();
        let (ptr, len) = {
            let b = data.as_bytes();
            (b.as_ptr(), b.len() as u32)
        };
        self.pending.insert(
            id,
            Pending::Write {
                _buf: data.unbind(),
            },
        );
        let entry = opcode::Write::new(types::Fixed(slot), ptr, len)
            .offset(offset)
            .build()
            .user_data(id);
        self.finish(id, entry)
    }

    /// Close the direct descriptor in `slot`, freeing it for reuse.
    fn submit_close_fixed(&mut self, slot: u32) -> PyResult<u64> {
        let id = self.alloc_id();
        self.pending.insert(id, Pending::Plain);
        let entry = opcode::Close::new(types::Fixed(slot)).build().user_data(id);
        self.finish(id, entry)
    }

    /// statx on a path. `follow` controls symlink resolution.
    #[pyo3(signature = (path, follow = true))]
    fn submit_statx(&mut self, path: &str, follow: bool) -> PyResult<u64> {
        let id = self.alloc_id();
        let cpath = cstring(path)?;
        let path_ptr = cpath.as_ptr();
        let stx: Box<statx_abi::statx> = Box::new(unsafe { std::mem::zeroed() });
        let stx_ptr = Box::as_ref(&stx) as *const statx_abi::statx as *mut types::statx;
        let mut flags = if follow { 0 } else { libc::AT_SYMLINK_NOFOLLOW };
        flags |= statx_abi::AT_STATX_SYNC_AS_STAT;
        let entry = opcode::Statx::new(types::Fd(libc::AT_FDCWD), path_ptr, stx_ptr)
            .flags(flags)
            .mask(statx_abi::STATX_BASIC_STATS)
            .build()
            .user_data(id);
        self.pending
            .insert(id, Pending::Statx { _path: cpath, stx });
        self.finish(id, entry)
    }

    /// statx on an already-open fd (AT_EMPTY_PATH). Used to size read-all in a
    /// single extra round-trip.
    fn submit_fstatx(&mut self, fd: RawFd) -> PyResult<u64> {
        let id = self.alloc_id();
        let empty = cstring("")?;
        let path_ptr = empty.as_ptr();
        let stx: Box<statx_abi::statx> = Box::new(unsafe { std::mem::zeroed() });
        let stx_ptr = Box::as_ref(&stx) as *const statx_abi::statx as *mut types::statx;
        let flags = libc::AT_EMPTY_PATH | statx_abi::AT_STATX_SYNC_AS_STAT;
        let entry = opcode::Statx::new(types::Fd(fd), path_ptr, stx_ptr)
            .flags(flags)
            .mask(statx_abi::STATX_BASIC_STATS)
            .build()
            .user_data(id);
        self.pending
            .insert(id, Pending::Statx { _path: empty, stx });
        self.finish(id, entry)
    }

    #[pyo3(signature = (path, mode = 0o777))]
    fn submit_mkdir(&mut self, path: &str, mode: u32) -> PyResult<u64> {
        let id = self.alloc_id();
        let cpath = cstring(path)?;
        let ptr = cpath.as_ptr();
        self.pending.insert(
            id,
            Pending::Paths {
                _paths: vec![cpath],
            },
        );
        let entry = opcode::MkDirAt::new(types::Fd(libc::AT_FDCWD), ptr)
            .mode(mode)
            .build()
            .user_data(id);
        self.finish(id, entry)
    }

    /// unlink (file) or rmdir (when `remove_dir` is true).
    #[pyo3(signature = (path, remove_dir = false))]
    fn submit_unlink(&mut self, path: &str, remove_dir: bool) -> PyResult<u64> {
        let id = self.alloc_id();
        let cpath = cstring(path)?;
        let ptr = cpath.as_ptr();
        let flags = if remove_dir { libc::AT_REMOVEDIR } else { 0 };
        self.pending.insert(
            id,
            Pending::Paths {
                _paths: vec![cpath],
            },
        );
        let entry = opcode::UnlinkAt::new(types::Fd(libc::AT_FDCWD), ptr)
            .flags(flags)
            .build()
            .user_data(id);
        self.finish(id, entry)
    }

    fn submit_rename(&mut self, src: &str, dst: &str) -> PyResult<u64> {
        let id = self.alloc_id();
        let csrc = cstring(src)?;
        let cdst = cstring(dst)?;
        let sptr = csrc.as_ptr();
        let dptr = cdst.as_ptr();
        self.pending.insert(
            id,
            Pending::Paths {
                _paths: vec![csrc, cdst],
            },
        );
        let entry = opcode::RenameAt::new(
            types::Fd(libc::AT_FDCWD),
            sptr,
            types::Fd(libc::AT_FDCWD),
            dptr,
        )
        .build()
        .user_data(id);
        self.finish(id, entry)
    }

    fn submit_symlink(&mut self, target: &str, linkpath: &str) -> PyResult<u64> {
        let id = self.alloc_id();
        let ctarget = cstring(target)?;
        let clink = cstring(linkpath)?;
        let tptr = ctarget.as_ptr();
        let lptr = clink.as_ptr();
        self.pending.insert(
            id,
            Pending::Paths {
                _paths: vec![ctarget, clink],
            },
        );
        let entry = opcode::SymlinkAt::new(types::Fd(libc::AT_FDCWD), tptr, lptr)
            .build()
            .user_data(id);
        self.finish(id, entry)
    }

    fn submit_link(&mut self, src: &str, dst: &str) -> PyResult<u64> {
        let id = self.alloc_id();
        let csrc = cstring(src)?;
        let cdst = cstring(dst)?;
        let sptr = csrc.as_ptr();
        let dptr = cdst.as_ptr();
        self.pending.insert(
            id,
            Pending::Paths {
                _paths: vec![csrc, cdst],
            },
        );
        let entry = opcode::LinkAt::new(
            types::Fd(libc::AT_FDCWD),
            sptr,
            types::Fd(libc::AT_FDCWD),
            dptr,
        )
        .build()
        .user_data(id);
        self.finish(id, entry)
    }

    /// Drain all available completions. Returns a list of
    /// `(user_data, result, payload)`. `result` is the raw kernel return value
    /// (negative is `-errno`). `payload` is bytes for reads, a statx tuple for
    /// statx, otherwise `None`.
    fn reap(&mut self, py: Python<'_>) -> PyResult<Vec<Py<PyAny>>> {
        let mut raw: Vec<(u64, i32)> = Vec::new();
        {
            let mut cq = self.ring.completion();
            cq.sync();
            for cqe in &mut cq {
                raw.push((cqe.user_data(), cqe.result()));
            }
        }
        let mut out: Vec<Py<PyAny>> = Vec::with_capacity(raw.len());
        for (id, res) in raw {
            let pending = self.pending.remove(&id);
            let payload: Py<PyAny> = if res < 0 {
                py.None()
            } else {
                match pending {
                    Some(Pending::Read { buf, cap }) => {
                        let n = res as usize;
                        if n == cap {
                            // Full read: hand Python the very buffer the kernel
                            // filled — no copy. Safe: `pending` held the only
                            // reference, so nothing observed it uninitialised.
                            buf.into_any()
                        } else {
                            // Short read: expose only the bytes actually read.
                            let p =
                                unsafe { pyo3::ffi::PyBytes_AsString(buf.as_ptr()) as *const u8 };
                            let s = unsafe { std::slice::from_raw_parts(p, n) };
                            PyBytes::new(py, s).into()
                        }
                    }
                    Some(Pending::Statx { stx, .. }) => statx_tuple(py, &stx)?,
                    _ => py.None(),
                }
            };
            let tup = (id, res, payload).into_pyobject(py)?.unbind().into();
            out.push(tup);
        }
        Ok(out)
    }
}

/// `io_uring_enter(2)` flag: wait for / flush completions without submitting.
/// Kernel-ABI constant (the crate does not re-export it).
#[cfg(target_os = "linux")]
const IORING_ENTER_GETEVENTS: u32 = 1;

/// How long `Drop` waits through complete silence before giving up on the
/// remaining completions and leaking their buffers instead.
#[cfg(target_os = "linux")]
const DRAIN_POLL_MS: i32 = 100;
#[cfg(target_os = "linux")]
const DRAIN_MAX_SILENT_POLLS: u32 = 50; // ~5 s without a single completion

#[cfg(target_os = "linux")]
impl Drop for Ring {
    fn drop(&mut self) {
        // The kernel may still be reading from / writing into buffers held in
        // `pending`. Dropping the ring would free those buffers (and unmap the
        // rings) out from under in-flight operations — a use-after-free. Wait
        // for the outstanding completions before letting the buffers drop.
        //
        // Two things this drain must never do:
        //
        //  * Submit. SQEs still sitting in the SQ ring were never consumed by
        //    the kernel — their `io_uring_enter` failed and the driver already
        //    failed their futures — so entering with `to_submit > 0` here
        //    would execute ops the application was told had failed, e.g.
        //    writing a stale payload into an fd number the application closed
        //    and the OS has since recycled. They die with the ring instead,
        //    and their buffers are safe to free: the kernel never saw them.
        //
        //  * Wait forever. An op stuck on a hung mount (NFS, 9p) would
        //    otherwise block whichever thread runs GC — possibly the main
        //    thread at interpreter exit. After ~5 s with no completion at all
        //    the remaining buffers are deliberately leaked: the kernel may
        //    still write into them, so leaking is the only safe way out.
        let queued = {
            let mut sq = self.ring.submission();
            sq.sync();
            sq.len()
        };
        let mut remaining = self.pending.len().saturating_sub(queued);
        let mut silent_polls = 0u32;
        while remaining > 0 {
            // GETEVENTS with to_submit == 0 and min_complete == 0: flush any
            // overflowed CQEs into the ring without submitting or blocking.
            // Best-effort — if it fails, the poll below still sees ordinary
            // completions and the silence cap bounds the wait.
            let _ = unsafe {
                self.ring
                    .submitter()
                    .enter::<libc::sigset_t>(0, 0, IORING_ENTER_GETEVENTS, None)
            };
            let mut completed = 0usize;
            {
                let mut cq = self.ring.completion();
                cq.sync();
                for cqe in &mut cq {
                    // A normal open completed while the driver was shutting
                    // down: no Python caller can receive its fd now. Direct
                    // opens live in the ring's fixed table and are released
                    // when the ring is destroyed instead.
                    if let Some(Pending::Open { direct: false, .. }) =
                        self.pending.remove(&cqe.user_data())
                    {
                        if cqe.result() >= 0 {
                            unsafe { libc::close(cqe.result()) };
                        }
                    }
                    completed += 1;
                }
            }
            if completed > 0 {
                remaining = remaining.saturating_sub(completed);
                silent_polls = 0;
                continue;
            }
            if silent_polls >= DRAIN_MAX_SILENT_POLLS {
                std::mem::forget(std::mem::take(&mut self.pending));
                break;
            }
            // The ring fd polls readable when the CQ is non-empty.
            let mut pfd = libc::pollfd {
                fd: self.ring.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            };
            let rc = unsafe { libc::poll(&mut pfd, 1, DRAIN_POLL_MS) };
            if rc == 0 {
                silent_polls += 1;
            } else if rc < 0 && io::Error::last_os_error().raw_os_error() != Some(libc::EINTR) {
                // Cannot even wait any more; leaking beats a use-after-free.
                std::mem::forget(std::mem::take(&mut self.pending));
                break;
            }
        }
        unsafe { libc::close(self.eventfd) };
    }
}

#[cfg(target_os = "linux")]
fn ts_ns(t: &statx_abi::statx_timestamp) -> i64 {
    t.tv_sec * 1_000_000_000 + t.tv_nsec as i64
}

/// Build the statx payload tuple consumed by `urio.backend`.
#[cfg(target_os = "linux")]
fn statx_tuple(py: Python<'_>, s: &statx_abi::statx) -> PyResult<Py<PyAny>> {
    let dev = libc::makedev(s.stx_dev_major, s.stx_dev_minor);
    let rdev = libc::makedev(s.stx_rdev_major, s.stx_rdev_minor);
    // 13 fields exceeds PyO3's tuple-conversion arity, and the values are mixed
    // signedness, so widen everything to i128 and build the tuple explicitly.
    let elems: [i128; 13] = [
        s.stx_mode as i128,          // 0 mode
        s.stx_ino as i128,           // 1 inode
        dev as i128,                 // 2 device
        s.stx_nlink as i128,         // 3 nlink
        s.stx_uid as i128,           // 4 uid
        s.stx_gid as i128,           // 5 gid
        s.stx_size as i128,          // 6 size
        ts_ns(&s.stx_atime) as i128, // 7 atime ns
        ts_ns(&s.stx_mtime) as i128, // 8 mtime ns
        ts_ns(&s.stx_ctime) as i128, // 9 ctime ns
        s.stx_blksize as i128,       // 10 blksize
        s.stx_blocks as i128,        // 11 blocks (512B units)
        rdev as i128,                // 12 rdev
    ];
    Ok(PyTuple::new(py, elems)?.into_any().unbind())
}

// ===========================================================================
// Windows: real async file I/O via overlapped I/O + I/O completion ports.
//
// Disabled unless URIO_WINDOWS_IOCP=1; Windows otherwise uses the thread-pool
// backend. See docs/benchmarks.md for the measured workload trade-offs. Mirrors
// the Linux design: each op carries owned buffers that outlive the kernel
// operation. Two reaping strategies:
//   * `ProactorIo` (primary): submit overlapped ops onto asyncio's
//     ProactorEventLoop IOCP and let the loop reap them on the loop thread — no
//     poller thread, no cross-thread hop, the Windows analogue of Linux eventfd.
//   * `Iocp` (fallback for non-Proactor loops): a single poller thread reaps
//     completions in batches and hands each batch to the loop with one
//     `call_soon_threadsafe` (one thread for any number of in-flight ops).
// See driver_iocp_proactor.py and driver_iocp.py.
// ===========================================================================
#[cfg(windows)]
mod iocp {
    use std::cell::RefCell;
    use std::collections::HashMap;
    use std::ffi::c_void;
    use std::os::windows::ffi::OsStrExt;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::thread::JoinHandle;

    use pyo3::prelude::*;
    use pyo3::types::{PyBytes, PyList};
    use windows_sys::Win32::Foundation::{
        CloseHandle, GetLastError, ERROR_IO_PENDING, INVALID_HANDLE_VALUE,
    };
    use windows_sys::Win32::Storage::FileSystem::{CreateFileW, ReadFile, WriteFile};
    use windows_sys::Win32::System::IO::{
        CreateIoCompletionPort, GetQueuedCompletionStatusEx, PostQueuedCompletionStatus,
        OVERLAPPED, OVERLAPPED_ENTRY,
    };

    // NTSTATUS reported in OVERLAPPED.Internal for a read that hit end-of-file.
    const STATUS_END_OF_FILE: u32 = 0xC000_0011;
    // Reap up to this many completions per GetQueuedCompletionStatusEx call so a
    // single cross-thread wakeup + GIL acquire serves a whole burst of ops.
    const BATCH: usize = 64;

    // ntdll is always loaded; map an I/O NTSTATUS to a Win32 error for reporting.
    extern "system" {
        fn RtlNtStatusToDosError(status: i32) -> u32;
    }

    // Win32 constants (declared locally to avoid version-specific import paths).
    const GENERIC_READ: u32 = 0x8000_0000;
    const GENERIC_WRITE: u32 = 0x4000_0000;
    const FILE_SHARE_READ: u32 = 0x1;
    const FILE_SHARE_WRITE: u32 = 0x2;
    const CREATE_NEW: u32 = 1;
    const CREATE_ALWAYS: u32 = 2;
    const OPEN_EXISTING: u32 = 3;
    const OPEN_ALWAYS: u32 = 4;
    const FILE_FLAG_OVERLAPPED: u32 = 0x4000_0000;
    const FILE_ATTRIBUTE_NORMAL: u32 = 0x80;
    const INFINITE: u32 = 0xFFFF_FFFF;
    const ERROR_HANDLE_EOF: u32 = 38;
    const SHUTDOWN_KEY: usize = usize::MAX;

    const OP_READ: u8 = 0;
    const OP_WRITE: u8 = 1;
    const OP_OPEN: u8 = 2;
    const OP_CLOSE: u8 = 3;

    /// Per-operation state. `ov` MUST be the first field so the OVERLAPPED
    /// pointer the kernel returns can be cast back to `*mut Op`.
    #[repr(C)]
    struct Op {
        ov: OVERLAPPED,
        id: u64,
        kind: u8,
        // Reads: the kernel writes directly into this preallocated Python
        // `bytes` (only `pending`/the Op reference it, so nothing observes it
        // uninitialised). A full read hands the object back with no copy —
        // the memcpy that used to dominate large warm reads; a short read
        // copies just the prefix. `rptr`/`rcap` cache its buffer and size.
        rbuf: Option<Py<PyBytes>>,
        rptr: *mut u8,
        rcap: usize,
        // Writes only: the caller's `bytes`, held alive until the completion is
        // reaped so the pointer handed to WriteFile stays valid (bytes are
        // immutable and never relocated by CPython).
        wbuf: Option<Py<PyBytes>>,
        handle: isize, // open: resulting handle
        status: i32,   // non-zero = synthetic error (open/close/sync failure)
    }

    /// Allocate an uninitialised Python `bytes` of exactly `len` bytes (the
    /// kernel fills it before it is ever exposed). Mirrors the Linux helper;
    /// kept local so the module stays self-contained.
    fn alloc_pybytes(py: Python<'_>, len: usize) -> PyResult<(Py<PyBytes>, *mut u8)> {
        unsafe {
            let raw = pyo3::ffi::PyBytes_FromStringAndSize(
                std::ptr::null(),
                len as pyo3::ffi::Py_ssize_t,
            );
            if raw.is_null() {
                return Err(PyErr::fetch(py));
            }
            let ptr = pyo3::ffi::PyBytes_AsString(raw) as *mut u8;
            let obj: Bound<'_, PyBytes> = Bound::from_owned_ptr(py, raw).cast_into_unchecked();
            Ok((obj.unbind(), ptr))
        }
    }

    /// Consume a completed read's buffer: the whole object on a full read,
    /// a prefix copy on a short one.
    fn take_read_payload(py: Python<'_>, op: &mut Op, n: usize) -> Py<PyAny> {
        match op.rbuf.take() {
            Some(b) if n == op.rcap => b.into_any(),
            Some(b) => {
                let p = unsafe { pyo3::ffi::PyBytes_AsString(b.as_ptr()) as *const u8 };
                let s = unsafe { std::slice::from_raw_parts(p, n) };
                PyBytes::new(py, s).into()
            }
            None => py.None(),
        }
    }

    fn wide(path: &str) -> Vec<u16> {
        std::ffi::OsStr::new(path)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect()
    }

    #[pyclass(unsendable)]
    pub struct Iocp {
        port: isize,
        next_id: AtomicU64,
        poller: Option<JoinHandle<()>>,
    }

    impl Iocp {
        fn post(&self, op: *mut Op) {
            unsafe {
                PostQueuedCompletionStatus(self.port as *mut c_void, 0, 0, op as *const OVERLAPPED);
            }
        }

        /// Signal the poller thread to stop and wait for it, then close the
        /// port. The join MUST run with the GIL released: this thread usually
        /// holds the GIL (shutdown is driven from the loop thread), while the
        /// poller thread needs the GIL to unwind — it drops its `Py<PyAny>`
        /// handles and may be mid `call_soon_threadsafe`. Joining while holding
        /// the GIL therefore deadlocks (only under a real GIL — free-threaded
        /// builds are unaffected, which is why this hid on GIL Windows alone).
        fn stop_poller(&mut self) {
            if self.poller.is_none() {
                return;
            }
            unsafe {
                PostQueuedCompletionStatus(
                    self.port as *mut c_void,
                    0,
                    SHUTDOWN_KEY,
                    std::ptr::null(),
                );
            }
            if let Some(j) = self.poller.take() {
                Python::attach(|py| {
                    py.detach(|| {
                        let _ = j.join();
                    })
                });
            }
            unsafe {
                CloseHandle(self.port as *mut c_void);
            }
        }
    }

    #[pymethods]
    impl Iocp {
        #[new]
        fn new(loop_obj: Py<PyAny>, on_complete: Py<PyAny>) -> PyResult<Self> {
            let port =
                unsafe { CreateIoCompletionPort(INVALID_HANDLE_VALUE, std::ptr::null_mut(), 0, 0) };
            if port.is_null() {
                return Err(pyo3::exceptions::PyOSError::new_err(
                    "CreateIoCompletionPort failed",
                ));
            }
            let port_i = port as isize;
            let poller = std::thread::spawn(move || poll_loop(port_i, loop_obj, on_complete));
            Ok(Iocp {
                port: port_i,
                next_id: AtomicU64::new(1),
                poller: Some(poller),
            })
        }

        fn submit_open(
            &self,
            path: &str,
            write: bool,
            create: bool,
            truncate: bool,
            exclusive: bool,
        ) -> u64 {
            let id = self.next_id.fetch_add(1, Ordering::Relaxed);
            let w = wide(path);
            let access = if write {
                GENERIC_READ | GENERIC_WRITE
            } else {
                GENERIC_READ
            };
            let disp = if create && exclusive {
                CREATE_NEW // O_CREAT|O_EXCL: fail (ERROR_FILE_EXISTS) if it exists
            } else if truncate {
                CREATE_ALWAYS
            } else if create {
                OPEN_ALWAYS
            } else {
                OPEN_EXISTING
            };
            let handle = unsafe {
                CreateFileW(
                    w.as_ptr(),
                    access,
                    FILE_SHARE_READ | FILE_SHARE_WRITE,
                    std::ptr::null(),
                    disp,
                    FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED,
                    std::ptr::null_mut(),
                )
            };
            let status = if handle == INVALID_HANDLE_VALUE {
                unsafe { GetLastError() as i32 }
            } else {
                // Associate the new handle with our completion port.
                let port =
                    unsafe { CreateIoCompletionPort(handle, self.port as *mut c_void, 0, 0) };
                if port.is_null() {
                    let error = unsafe { GetLastError() as i32 };
                    unsafe { CloseHandle(handle) };
                    error
                } else {
                    0
                }
            };
            let op = Box::new(Op {
                ov: unsafe { std::mem::zeroed() },
                id,
                kind: OP_OPEN,
                rbuf: None,
                rptr: std::ptr::null_mut(),
                rcap: 0,
                wbuf: None,
                handle: handle as isize,
                status,
            });
            self.post(Box::into_raw(op));
            id
        }

        fn submit_read(
            &self,
            py: Python<'_>,
            handle: isize,
            offset: u64,
            len: u32,
        ) -> PyResult<u64> {
            let id = self.next_id.fetch_add(1, Ordering::Relaxed);
            // Zero-copy: the kernel writes straight into a preallocated bytes
            // object (no zero-fill — the uninitialised tail of a short read is
            // never exposed; see Op.rbuf).
            let (rbuf, rptr) = alloc_pybytes(py, len as usize)?;
            let mut op = Box::new(Op {
                ov: unsafe { std::mem::zeroed() },
                id,
                kind: OP_READ,
                rbuf: Some(rbuf),
                rptr,
                rcap: len as usize,
                wbuf: None,
                handle: 0,
                status: 0,
            });
            op.ov.Anonymous.Anonymous.Offset = (offset & 0xFFFF_FFFF) as u32;
            op.ov.Anonymous.Anonymous.OffsetHigh = (offset >> 32) as u32;
            let ptr = Box::into_raw(op);
            unsafe {
                // lpNumberOfBytesRead is only read in the synchronous EOF branch
                // below (valid then); the async path uses the completion's count.
                let mut nread: u32 = 0;
                let ok = ReadFile(
                    handle as *mut c_void,
                    (*ptr).rptr,
                    len,
                    &mut nread,
                    &mut (*ptr).ov,
                );
                if ok == 0 {
                    let e = GetLastError();
                    if e == ERROR_IO_PENDING {
                        // async — the kernel will post the completion.
                    } else if e == ERROR_HANDLE_EOF {
                        // Reading at/over EOF: deliver the bytes read so far
                        // (possibly 0) as a successful short read = EOF.
                        PostQueuedCompletionStatus(
                            self.port as *mut c_void,
                            nread,
                            0,
                            ptr as *const OVERLAPPED,
                        );
                    } else {
                        (*ptr).status = e as i32;
                        self.post(ptr);
                    }
                }
            }
            Ok(id)
        }

        fn submit_write(&self, handle: isize, offset: u64, data: Bound<'_, PyBytes>) -> u64 {
            let id = self.next_id.fetch_add(1, Ordering::Relaxed);
            // Zero-copy: point WriteFile at the Python bytes' own buffer and keep
            // the object alive in the Op (dropped when the completion is reaped).
            let (dptr, len) = {
                let b = data.as_bytes();
                (b.as_ptr(), b.len() as u32)
            };
            let mut op = Box::new(Op {
                ov: unsafe { std::mem::zeroed() },
                id,
                kind: OP_WRITE,
                rbuf: None,
                rptr: std::ptr::null_mut(),
                rcap: 0,
                wbuf: Some(data.unbind()),
                handle: 0,
                status: 0,
            });
            op.ov.Anonymous.Anonymous.Offset = (offset & 0xFFFF_FFFF) as u32;
            op.ov.Anonymous.Anonymous.OffsetHigh = (offset >> 32) as u32;
            let ptr = Box::into_raw(op);
            unsafe {
                let ok = WriteFile(
                    handle as *mut c_void,
                    dptr,
                    len,
                    std::ptr::null_mut(),
                    &mut (*ptr).ov,
                );
                if ok == 0 {
                    let e = GetLastError();
                    if e != ERROR_IO_PENDING {
                        (*ptr).status = e as i32;
                        self.post(ptr);
                    }
                }
            }
            id
        }

        fn submit_close(&self, handle: isize) -> u64 {
            let id = self.next_id.fetch_add(1, Ordering::Relaxed);
            let status = unsafe {
                if CloseHandle(handle as *mut c_void) == 0 {
                    GetLastError() as i32
                } else {
                    0
                }
            };
            let op = Box::new(Op {
                ov: unsafe { std::mem::zeroed() },
                id,
                kind: OP_CLOSE,
                rbuf: None,
                rptr: std::ptr::null_mut(),
                rcap: 0,
                wbuf: None,
                handle: 0,
                status,
            });
            self.post(Box::into_raw(op));
            id
        }

        /// Release a successful open whose Python awaiter was cancelled.
        fn close_now(&self, handle: isize) -> PyResult<()> {
            if unsafe { CloseHandle(handle as *mut c_void) } == 0 {
                let error = unsafe { GetLastError() };
                return Err(pyo3::exceptions::PyOSError::new_err((
                    0i32,
                    format!("CloseHandle failed (winerror {error})"),
                    Option::<String>::None,
                    error as i32,
                )));
            }
            Ok(())
        }

        fn shutdown(&mut self) {
            self.stop_poller();
        }
    }

    impl Drop for Iocp {
        fn drop(&mut self) {
            self.stop_poller();
        }
    }

    /// Poller thread: reap completions in batches and resolve the matching
    /// futures on the loop thread. Each `GetQueuedCompletionStatusEx` call drains
    /// up to `BATCH` completions, and the whole batch is handed to the loop with a
    /// single `call_soon_threadsafe` under one GIL acquire — so the per-op
    /// cross-thread wakeup and GIL hand-off are amortised across the burst rather
    /// than paid once per operation.
    fn poll_loop(port_i: isize, loop_obj: Py<PyAny>, on_complete: Py<PyAny>) {
        let port = port_i as *mut c_void;
        let mut entries: [OVERLAPPED_ENTRY; BATCH] = unsafe { std::mem::zeroed() };
        loop {
            let mut removed: u32 = 0;
            let ok = unsafe {
                GetQueuedCompletionStatusEx(
                    port,
                    entries.as_mut_ptr(),
                    BATCH as u32,
                    &mut removed,
                    INFINITE,
                    0, // not alertable
                )
            };
            if ok == 0 {
                // INFINITE wait on a live port does not fail; treat any failure
                // (e.g. the port closed during shutdown) as a stop.
                break;
            }
            let mut shutdown = false;
            Python::attach(|py| {
                let batch = PyList::empty(py);
                for entry in &entries[..removed as usize] {
                    if entry.lpOverlapped.is_null() {
                        // The shutdown sentinel is posted with a null overlapped.
                        if entry.lpCompletionKey == SHUTDOWN_KEY {
                            shutdown = true;
                        }
                        continue;
                    }
                    let mut op = unsafe { Box::from_raw(entry.lpOverlapped as *mut Op) };
                    let nbytes = entry.dwNumberOfBytesTransferred;
                    let status = if op.status != 0 {
                        // Synthetic failure recorded at submit time (open/close).
                        op.status
                    } else {
                        // Real I/O status lives in OVERLAPPED.Internal as an
                        // NTSTATUS. EOF on a read is a successful short read.
                        let nt = op.ov.Internal as u32;
                        if nt == 0 || nt == STATUS_END_OF_FILE {
                            0
                        } else {
                            unsafe { RtlNtStatusToDosError(nt as i32) as i32 }
                        }
                    };
                    let value: i64 = match op.kind {
                        OP_OPEN => op.handle as i64,
                        OP_READ | OP_WRITE => nbytes as i64,
                        _ => 0,
                    };
                    let data: Py<PyAny> = if op.kind == OP_READ && status == 0 {
                        take_read_payload(py, &mut op, nbytes as usize)
                    } else {
                        py.None()
                    };
                    let _ = batch.append((op.id, status, value, data));
                    // op is dropped here; a fully-read buffer has already been
                    // handed off to the batch without a copy.
                }
                if batch.len() > 0 {
                    let cb = loop_obj.bind(py);
                    let _ = cb.call_method1("call_soon_threadsafe", (on_complete.bind(py), &batch));
                }
            });
            if shutdown {
                break;
            }
        }
    }

    // =======================================================================
    // ProactorIo: reap completions on the *loop thread* via asyncio's own
    // ProactorEventLoop IOCP, with no poller thread and no call_soon_threadsafe.
    //
    // We submit overlapped reads/writes (with offsets — which `_overlapped`
    // cannot express) onto the loop's completion port. The loop already blocks
    // on that port in `IocpProactor._poll`, so our completions wake it directly
    // and it resolves the matching future on the loop thread. Each op's owned
    // buffer lives in `pending` until the completion is reaped (take) or the op
    // is abandoned (discard) — never freed while the kernel may still touch it.
    // =======================================================================
    #[pyclass(unsendable)]
    pub struct ProactorIo {
        iocp: isize, // the loop's IOCP — borrowed, never closed here
        next_id: AtomicU64,
        pending: RefCell<HashMap<usize, Box<Op>>>,
    }

    impl ProactorIo {
        // Insert the op, then issue the overlapped call against its stable heap
        // address. Every path posts exactly one completion to the loop's IOCP:
        // the kernel posts for IO_PENDING and for synchronous success, and we
        // post manually for a synchronous EOF/error (which the kernel does not).
        fn launch_read(&self, handle: isize, mut op: Box<Op>, len: u32) -> usize {
            let op_ptr = op.as_mut() as *mut Op;
            let addr = op_ptr as usize;
            self.pending.borrow_mut().insert(addr, op);
            unsafe {
                let mut nread: u32 = 0;
                let ok = ReadFile(
                    handle as *mut c_void,
                    (*op_ptr).rptr,
                    len,
                    &mut nread,
                    &mut (*op_ptr).ov,
                );
                if ok == 0 {
                    let e = GetLastError();
                    if e == ERROR_IO_PENDING {
                        // kernel will post the completion
                    } else if e == ERROR_HANDLE_EOF {
                        PostQueuedCompletionStatus(
                            self.iocp as *mut c_void,
                            nread,
                            0,
                            &mut (*op_ptr).ov,
                        );
                    } else {
                        (*op_ptr).status = e as i32;
                        PostQueuedCompletionStatus(
                            self.iocp as *mut c_void,
                            0,
                            0,
                            &mut (*op_ptr).ov,
                        );
                    }
                }
            }
            addr
        }

        // `dptr` points into the op's `wbuf` (the caller's Python bytes), which
        // is kept alive in `pending`, so the pointer stays valid for the write.
        fn launch_write(&self, handle: isize, mut op: Box<Op>, dptr: *const u8, len: u32) -> usize {
            let op_ptr = op.as_mut() as *mut Op;
            let addr = op_ptr as usize;
            self.pending.borrow_mut().insert(addr, op);
            unsafe {
                let ok = WriteFile(
                    handle as *mut c_void,
                    dptr,
                    len,
                    std::ptr::null_mut(),
                    &mut (*op_ptr).ov,
                );
                if ok == 0 {
                    let e = GetLastError();
                    if e != ERROR_IO_PENDING {
                        (*op_ptr).status = e as i32;
                        PostQueuedCompletionStatus(
                            self.iocp as *mut c_void,
                            0,
                            0,
                            &mut (*op_ptr).ov,
                        );
                    }
                }
            }
            addr
        }
    }

    #[pymethods]
    impl ProactorIo {
        #[new]
        fn new(iocp: isize) -> Self {
            ProactorIo {
                iocp,
                next_id: AtomicU64::new(1),
                pending: RefCell::new(HashMap::new()),
            }
        }

        /// Open a file with overlapped I/O and associate it with the loop's IOCP.
        /// Synchronous (CreateFileW is not an overlapped operation).
        fn open(
            &self,
            path: &str,
            write: bool,
            create: bool,
            truncate: bool,
            exclusive: bool,
        ) -> PyResult<isize> {
            let w = wide(path);
            let access = if write {
                GENERIC_READ | GENERIC_WRITE
            } else {
                GENERIC_READ
            };
            let disp = if create && exclusive {
                CREATE_NEW // O_CREAT|O_EXCL: fail (ERROR_FILE_EXISTS) if it exists
            } else if truncate {
                CREATE_ALWAYS
            } else if create {
                OPEN_ALWAYS
            } else {
                OPEN_EXISTING
            };
            let handle = unsafe {
                CreateFileW(
                    w.as_ptr(),
                    access,
                    FILE_SHARE_READ | FILE_SHARE_WRITE,
                    std::ptr::null(),
                    disp,
                    FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED,
                    std::ptr::null_mut(),
                )
            };
            if handle == INVALID_HANDLE_VALUE {
                let e = unsafe { GetLastError() };
                return Err(pyo3::exceptions::PyOSError::new_err((
                    0i32,
                    format!("CreateFileW failed (winerror {e})"),
                    Option::<String>::None,
                    e as i32,
                )));
            }
            let port = unsafe { CreateIoCompletionPort(handle, self.iocp as *mut c_void, 0, 0) };
            if port.is_null() {
                let error = unsafe { GetLastError() };
                unsafe { CloseHandle(handle) };
                return Err(pyo3::exceptions::PyOSError::new_err((
                    0i32,
                    format!("CreateIoCompletionPort failed (winerror {error})"),
                    Option::<String>::None,
                    error as i32,
                )));
            }
            Ok(handle as isize)
        }

        fn close(&self, handle: isize) {
            unsafe {
                CloseHandle(handle as *mut c_void);
            }
        }

        fn submit_read(
            &self,
            py: Python<'_>,
            handle: isize,
            offset: u64,
            len: u32,
        ) -> PyResult<usize> {
            let id = self.next_id.fetch_add(1, Ordering::Relaxed);
            // Zero-copy: kernel writes straight into a preallocated bytes
            // object (see Op.rbuf); `take` hands it back without the copy.
            let (rbuf, rptr) = alloc_pybytes(py, len as usize)?;
            let mut op = Box::new(Op {
                ov: unsafe { std::mem::zeroed() },
                id,
                kind: OP_READ,
                rbuf: Some(rbuf),
                rptr,
                rcap: len as usize,
                wbuf: None,
                handle: 0,
                status: 0,
            });
            op.ov.Anonymous.Anonymous.Offset = (offset & 0xFFFF_FFFF) as u32;
            op.ov.Anonymous.Anonymous.OffsetHigh = (offset >> 32) as u32;
            Ok(self.launch_read(handle, op, len))
        }

        fn submit_write(&self, handle: isize, offset: u64, data: Bound<'_, PyBytes>) -> usize {
            let id = self.next_id.fetch_add(1, Ordering::Relaxed);
            // Zero-copy: hand WriteFile the Python bytes' buffer and keep the
            // object alive in the Op (pinned in `pending` until reaped).
            let (dptr, len) = {
                let b = data.as_bytes();
                (b.as_ptr(), b.len() as u32)
            };
            let mut op = Box::new(Op {
                ov: unsafe { std::mem::zeroed() },
                id,
                kind: OP_WRITE,
                rbuf: None,
                rptr: std::ptr::null_mut(),
                rcap: 0,
                wbuf: Some(data.unbind()),
                handle: 0,
                status: 0,
            });
            op.ov.Anonymous.Anonymous.Offset = (offset & 0xFFFF_FFFF) as u32;
            op.ov.Anonymous.Anonymous.OffsetHigh = (offset >> 32) as u32;
            self.launch_write(handle, op, dptr, len)
        }

        /// Reap a completed op: return its bytes (read) or transferred count
        /// (write), or raise OSError. Frees the op. Runs on the loop thread.
        fn take(&self, py: Python<'_>, addr: usize, transferred: u32) -> PyResult<Py<PyAny>> {
            let mut op = match self.pending.borrow_mut().remove(&addr) {
                Some(o) => o,
                None => return Ok(py.None()),
            };
            let status = if op.status != 0 {
                op.status
            } else {
                let nt = op.ov.Internal as u32;
                if nt == 0 || nt == STATUS_END_OF_FILE {
                    0
                } else {
                    unsafe { RtlNtStatusToDosError(nt as i32) as i32 }
                }
            };
            if status != 0 {
                return Err(pyo3::exceptions::PyOSError::new_err((
                    0i32,
                    format!("I/O failed (winerror {status})"),
                    Option::<String>::None,
                    status,
                )));
            }
            match op.kind {
                OP_READ => Ok(take_read_payload(py, &mut op, transferred as usize)),
                _ => Ok(transferred.into_pyobject(py).unwrap().into_any().unbind()),
            }
        }

        /// Free an op whose future was cancelled and whose completion the loop
        /// reaped but did not hand to `take` (the future was already done). Safe
        /// because the shim that calls this is only dropped after `_poll` has
        /// popped the completion, so the kernel is finished with the buffer.
        fn discard(&self, addr: usize) {
            self.pending.borrow_mut().remove(&addr);
        }
    }
}

// gil_used = false: the module is safe on free-threaded (no-GIL) CPython.
// urio creates one driver per event loop, so no ring/port is ever *used* from
// two threads. `Ring` is Send + Sync (not unsendable) purely so the loop's GC
// finalisation can drop it from a different thread; concurrent method calls
// would hit PyO3's runtime borrow check, not native state. The two Windows
// classes remain `#[pyclass(unsendable)]` (their state holds a RefCell and
// port handles reaped by a specific thread), so on Windows a driver must be
// torn down on its own thread — `urio.aclose()` before the loop dies.
#[pymodule(gil_used = false)]
fn _uring(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // The Ring class only exists on Linux; the Iocp class only on Windows.
    // Elsewhere the module is an empty marker and urio's backend selection
    // falls back to the thread pool.
    #[cfg(target_os = "linux")]
    m.add_class::<Ring>()?;
    #[cfg(windows)]
    m.add_class::<iocp::Iocp>()?;
    #[cfg(windows)]
    m.add_class::<iocp::ProactorIo>()?;
    m.add(
        "__doc__",
        "Native async backend for urio (io_uring on Linux, IOCP on Windows).",
    )?;
    Ok(())
}
