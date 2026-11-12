"""PowerShell aufrufen (JSON rein/raus) und lange Vorgänge zeilenweise mitlesen."""
from __future__ import annotations

import base64
import json
import os
import subprocess
from typing import Callable, Iterator

CREATE_NO_WINDOW = 0x08000000
_PRELUDE = ("$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;")


class PSError(RuntimeError):
    pass


def _cmd(script: str) -> list[str]:
    enc = base64.b64encode((_PRELUDE + script).encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", enc]


def _clean_err(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # PowerShell hängt Positions-/Kategoriezeilen an – die erste Zeile ist die eigentliche Meldung.
    useful = [ln for ln in lines if not ln.startswith(("+", "At line", "In Zeile", "CategoryInfo",
                                                        "FullyQualifiedErrorId", "#<"))]
    return " ".join(useful[:3]) or text.strip()


def run(script: str, timeout: float = 120) -> str:
    p = subprocess.run(_cmd(script), capture_output=True, timeout=timeout,
                       creationflags=CREATE_NO_WINDOW)
    out = p.stdout.decode("utf-8", "replace")
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace") or p.stderr.decode("mbcs", "replace")
        raise PSError(_clean_err(err) or f"PowerShell-Fehler (Exit {p.returncode})")
    return out


def run_json(script: str, timeout: float = 120):
    out = run(script, timeout).strip()
    if not out:
        return None
    return json.loads(out)


def as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def quote(s: str) -> str:
    """String als PowerShell-Literal in einfachen Anführungszeichen."""
    return "'" + str(s).replace("'", "''") + "'"


class Streamed:
    """Startet einen Prozess und liefert seine Ausgabe zeilenweise (\\r und \\n trennen)."""

    def __init__(self, args: list[str], encoding: str = "utf-8", stdin_text: str | None = None):
        self.encoding = encoding
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        if stdin_text is not None:
            try:
                self.proc.stdin.write(stdin_text.encode(encoding, "replace"))
                self.proc.stdin.flush()
            except OSError:
                pass
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def lines(self) -> Iterator[str]:
        buf = b""
        while True:
            ch = self.proc.stdout.read(1)
            if not ch:
                break
            if ch in (b"\r", b"\n"):
                if buf.strip():
                    yield buf.decode(self.encoding, "replace").strip()
                buf = b""
            else:
                buf += ch
        if buf.strip():
            yield buf.decode(self.encoding, "replace").strip()

    def wait(self) -> int:
        return self.proc.wait()

    def kill(self) -> None:
        try:
            # Ganzen Prozessbaum beenden (format.com hängt ggf. an conhost).
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        except OSError:
            self.proc.kill()


def streamed_ps(script: str) -> Streamed:
    return Streamed(_cmd(script), encoding="utf-8")


def oem_encoding() -> str:
    import ctypes
    try:
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:
        return "cp850"


def is_admin() -> bool:
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def system_drive() -> str:
    return (os.environ.get("SystemDrive") or "C:")[0].upper()


def windows_drive() -> str:
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    try:
        ctypes.windll.kernel32.GetWindowsDirectoryW(buf, 260)
        if buf.value:
            return buf.value[0].upper()
    except Exception:
        pass
    return (os.environ.get("SystemRoot") or os.environ.get("windir") or "C:\\")[0].upper()


ProgressFn = Callable[[float | None, str], None]
