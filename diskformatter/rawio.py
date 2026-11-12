"""Direkter Schreibzugriff auf ein Volume (\\\\.\\E:): sperren, aushängen, Nullen schreiben."""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
OPEN_EXISTING = 3
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020
FSCTL_ALLOW_EXTENDED_DASD_IO = 0x00090083
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
MEM_COMMIT_RESERVE, MEM_RELEASE, PAGE_READWRITE = 0x3000, 0x8000, 0x04
INVALID = ctypes.c_void_p(-1).value

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.VirtualAlloc.restype = ctypes.c_void_p
k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _err(what: str) -> OSError:
    e = ctypes.get_last_error()
    return OSError(e, f"{what}: {ctypes.FormatError(e).strip()} (Fehler {e})")


class VolumeWriter:
    """Öffnet \\\\.\\X: exklusiv (Lock + Dismount). Mit ``with`` benutzen."""

    def __init__(self, letter: str):
        self.path = f"\\\\.\\{letter.upper()}:"
        self.h = None
        self.length = 0
        self.locked = False

    def __enter__(self):
        h = k32.CreateFileW(self.path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                            None, OPEN_EXISTING, 0, None)
        if h in (None, INVALID):
            raise _err(f"{self.path} öffnen")
        self.h = h
        ret = wintypes.DWORD()
        for _ in range(10):          # Explorer/Indexer halten evtl. kurz Dateien offen
            if k32.DeviceIoControl(h, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(ret), None):
                self.locked = True
                break
            time.sleep(0.5)
        if not k32.DeviceIoControl(h, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0, ctypes.byref(ret), None):
            err = _err("Volume aushängen")
            self.close()
            raise err
        # Auch die Sektoren hinter dem Dateisystem (z. B. NTFS-Bootsektor-Kopie) beschreiben dürfen.
        k32.DeviceIoControl(h, FSCTL_ALLOW_EXTENDED_DASD_IO, None, 0, None, 0, ctypes.byref(ret), None)
        length = ctypes.c_longlong()
        if not k32.DeviceIoControl(h, IOCTL_DISK_GET_LENGTH_INFO, None, 0, ctypes.byref(length),
                                   ctypes.sizeof(length), ctypes.byref(ret), None):
            err = _err("Volumegröße lesen")
            self.close()
            raise err
        self.length = length.value
        return self

    def write_zeros(self, total: int, chunk: int, on_progress, should_stop) -> int:
        buf = k32.VirtualAlloc(None, chunk, MEM_COMMIT_RESERVE, PAGE_READWRITE)   # seitenausgerichtet, genullt
        if not buf:
            raise _err("Speicher anfordern")
        done = 0
        try:
            written = wintypes.DWORD()
            while done < total:
                if should_stop():
                    return done
                k = min(chunk, total - done)
                if not k32.WriteFile(self.h, buf, k, ctypes.byref(written), None) or written.value != k:
                    raise _err(f"Schreiben bei Byte {done}")
                done += k
                on_progress(done)
        finally:
            k32.VirtualFree(buf, 0, MEM_RELEASE)
        return done

    def close(self):
        if self.h is not None:
            k32.CloseHandle(self.h)    # Volume wird danach von Windows neu (als RAW) eingehängt
            self.h = None

    def __exit__(self, *exc):
        self.close()
