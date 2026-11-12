"""Neustart mit Administratorrechten über die UAC-Abfrage (ShellExecuteEx „runas“)."""
from __future__ import annotations

import ctypes
import subprocess
from ctypes import wintypes

SEE_MASK_NOCLOSEPROCESS = 0x40
INFINITE = 0xFFFFFFFF


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
                ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def run_elevated(exe: str, args: list[str], cwd: str | None = None, wait: bool = True,
                 show: int = 0) -> int | None:
    """Startet ``exe args`` als Administrator. Gibt bei wait=True den Exit-Code zurück.

    Wirft OSError, wenn der Nutzer die UAC-Abfrage ablehnt.
    """
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = subprocess.list2cmdline(args)
    info.lpDirectory = cwd
    info.nShow = show
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.windll.kernel32.GetLastError()
        if err == 1223:
            raise OSError("Administratorrechte wurden abgelehnt (UAC)")
        raise ctypes.WinError(err)
    if not wait or not info.hProcess:
        return None
    ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, INFINITE)
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    return code.value
