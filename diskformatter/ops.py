"""Planen und Ausführen: Formatieren, Volume mit Nullen füllen, ganzen Datenträger nullen.

Jede Ausführung baut den Plan unmittelbar vorher aus einer frischen Laufwerksabfrage neu auf,
prüft alle Sperren erneut, vergleicht die Kennung mit dem bestätigten Datenträger und verlangt
die exakte Bestätigung (Laufwerksbuchstabe bzw. Datenträgernummer).
"""
from __future__ import annotations

import os
import random
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field

from . import disks, ps
from .disks import Disk, Snapshot, Volume, fmt_size
from .settings import Settings

FILESYSTEMS = ("NTFS", "exFAT", "FAT32")
LABEL_MAX = {"NTFS": 32, "exFAT": 15, "FAT32": 11}
FAT32_MAX = 32 * 1024 ** 3
# Grobe Schreibraten für die erste Schätzung (Bytes/s); im Lauf wird aus dem Fortschritt neu geschätzt.
SPEED = {"USB": 25e6, "SD": 15e6, "MMC": 15e6, "File Backed Virtual": 300e6, "NVMe": 1000e6,
         "SATA": 300e6}
DEFAULT_SPEED = 150e6


class OpError(RuntimeError):
    pass


class Cancelled(OpError):
    pass


@dataclass
class Plan:
    action: str                 # format | wipe
    kind: str                   # volume | disk
    ident: str | int            # 'E' bzw. 2
    disk: Disk | None
    volume: Volume | None
    filesystem: str
    label: str
    full: bool
    partition_style: str
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    estimate_s: float = 0.0

    @property
    def allowed(self) -> bool:
        return not self.blocked

    @property
    def confirm_text(self) -> str:
        return f"{self.ident}:" if self.kind == "volume" else str(self.ident)

    @property
    def mode_name(self) -> str:
        if self.action == "format":
            return "Vollständig formatieren" if self.full else "Schnellformatierung"
        return "Volume mit Nullen füllen" if self.kind == "volume" else "Ganzen Datenträger mit Nullen füllen"

    def summary_lines(self) -> list[str]:
        d, v = self.disk, self.volume
        out = []
        if v is not None:
            out.append(f"Laufwerk:        {v.letter}:  „{v.label or '(ohne Namen)'}“  {v.filesystem or 'RAW'}, {fmt_size(v.size)}")
        if d is not None:
            out += [f"Datenträger:     Nr. {d.number}  ({fmt_size(d.size)}, {d.bus_type}, "
                    f"{'Wechseldatenträger' if d.removable else 'intern'})",
                    f"Modell:          {d.model or '?'}",
                    f"Seriennummer:    {d.serial or '?'}"]
            others = [x for x in d.volumes if v is None or x.path != v.path]
            if self.kind == "disk" and d.volumes:
                out.append("Betroffene Volumes: " + ", ".join(
                    f"{x.name} ({x.label or '-'}, {fmt_size(x.size)})" for x in d.volumes))
            elif others:
                out.append("Weitere Volumes (bleiben unberührt): " + ", ".join(x.name for x in others))
        out.append(f"Aktion:          {self.mode_name}")
        out.append(f"Danach:          {self.filesystem}, Name „{self.label}“"
                   + (f", Partitionsstil {self.partition_style}" if self.kind == "disk" else ""))
        out.append(f"Geschätzte Dauer: {fmt_duration(self.estimate_s)}")
        return out

    def to_json(self) -> dict:
        return {"action": self.action, "target_kind": self.kind,
                "target": f"{self.ident}:" if self.kind == "volume" else self.ident,
                "mode": self.mode_name, "filesystem": self.filesystem, "label": self.label,
                "full": self.full, "partition_style": self.partition_style if self.kind == "disk" else None,
                "disk": self.disk.to_json(Settings(allow_internal=True)) if self.disk else None,
                "volume": self.volume.to_json() if self.volume else None,
                "steps": self.steps, "warnings": self.warnings,
                "allowed": self.allowed, "blocked_reasons": self.blocked,
                "confirm_target_required": self.confirm_text,
                "estimate_seconds": round(self.estimate_s), "estimate": fmt_duration(self.estimate_s),
                "summary": self.summary_lines()}


def fmt_duration(s: float | None) -> str:
    if s is None:
        return "?"
    s = int(max(0, s))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def _speed(d: Disk | None) -> float:
    return SPEED.get(d.bus_type, DEFAULT_SPEED) if d else DEFAULT_SPEED


def _own_letters() -> set[str]:
    """Laufwerke, von denen dieses Programm / Python gerade läuft."""
    out = set()
    for p in (sys.executable, os.path.abspath(__file__), os.getcwd()):
        if len(p) > 1 and p[1] == ":":
            out.add(p[0].upper())
    return out


def make_plan(snap: Snapshot, settings: Settings, action: str, target: str, filesystem: str = "NTFS",
              label: str = "", full: bool = False, partition_style: str | None = None) -> Plan:
    if action not in ("format", "wipe"):
        raise OpError(f"Unbekannte Aktion '{action}'")
    try:
        kind, ident = disks.parse_target(target)
    except ValueError as e:
        raise OpError(str(e)) from None
    fs = next((f for f in FILESYSTEMS if f.lower() == (filesystem or "").lower()), None)
    plan = Plan(action=action, kind=kind, ident=ident, disk=None, volume=None,
                filesystem=fs or str(filesystem), label=(label or "").strip(), full=bool(full),
                partition_style="")
    if fs is None:
        plan.blocked.append(f"Dateisystem '{filesystem}' nicht unterstützt (NTFS, exFAT, FAT32)")

    if kind == "volume":
        hit = snap.volume(ident)
        if hit is None:
            raise OpError(f"Laufwerk {ident}: nicht gefunden")
        plan.volume, plan.disk = hit
        if plan.disk is None:
            plan.blocked.append(f"{ident}: gehört zu keinem lokalen Datenträger (CD/DVD, Netzwerk o. Ä.)")
    else:
        if action == "format":
            raise OpError("Formatieren braucht einen Laufwerksbuchstaben (z. B. E:). "
                          "Für den ganzen Datenträger: wipe mit Datenträgernummer.")
        plan.disk = snap.disk(int(ident))
        if plan.disk is None:
            raise OpError(f"Datenträger {ident} nicht gefunden")

    d, v = plan.disk, plan.volume
    if d is not None:
        plan.blocked += d.blocks(settings)
        own = _own_letters() & {x.letter for x in d.volumes if x.letter}
        if own:
            plan.blocked.append(f"DiskFormatter/Python läuft von diesem Datenträger ({', '.join(sorted(own))}:)")
    if v is not None and v.letter in disks.system_letters():
        plan.blocked.append(f"{v.letter}: ist das System-/Windows-Laufwerk")
    if v is not None and v.drive_type not in ("Fixed", "Removable"):
        plan.blocked.append(f"Laufwerkstyp '{v.drive_type}' kann nicht formatiert werden")

    size = v.size if v is not None else (d.size if d else 0)
    if fs == "FAT32" and size > FAT32_MAX:
        plan.blocked.append(f"FAT32 geht mit Windows-Bordmitteln nur bis 32 GB (Ziel: {fmt_size(size)}) – exFAT oder NTFS wählen")
    if fs and len(plan.label) > LABEL_MAX[fs]:
        plan.blocked.append(f"Volumenname zu lang für {fs} (max. {LABEL_MAX[fs]} Zeichen)")
    if re.search(r'[*?/\\|.,;:+=\[\]<>"]', plan.label) and fs in ("FAT32", "exFAT"):
        plan.blocked.append("Volumenname enthält Zeichen, die FAT32/exFAT nicht erlaubt")
    if not plan.label:
        plan.label = "USB" if (d and d.removable) else "Daten"

    sp = _speed(d)
    if kind == "volume":
        name = f"{ident}:"
        if action == "format" and not plan.full:
            plan.steps = [f"Format-Volume {name} → {plan.filesystem} (Schnellformatierung)"]
            plan.estimate_s = 10
        elif action == "format":
            plan.steps = [f"format {name} /FS:{plan.filesystem} (vollständig, prüft jeden Sektor)",
                          "falls Windows das ablehnt (thin provisioned): Volume direkt mit Nullen beschreiben, dann Schnellformatierung"]
            plan.estimate_s = size / sp + 10
        else:
            plan.steps = [f"format {name} /FS:{plan.filesystem} /P:0 – jeden Sektor des Volumes mit Nullen überschreiben",
                          "falls Windows das ablehnt (thin provisioned): Volume sperren, direkt mit Nullen beschreiben, dann Schnellformatierung",
                          "Stichprobe: zufällige Sektoren lesen und auf 0 prüfen"]
            plan.estimate_s = size / sp * 1.1 + 10
            plan.warnings.append("Partitionstabelle und andere Volumes auf dem Datenträger bleiben erhalten.")
        plan.warnings.append(f"Alle Daten auf {name} gehen verloren.")
    else:
        style = (partition_style or "").upper()
        if style not in ("GPT", "MBR"):
            style = "MBR" if (d and d.removable and d.size < 2 * 1024 ** 4) else "GPT"
        plan.partition_style = style
        plan.steps = [f"Clear-Disk -Number {ident} -RemoveData -RemoveOEM -ZeroOutEntireDisk (alle Sektoren inkl. Partitionstabelle = 0)",
                      f"Stichprobe: zufällige Sektoren von \\\\.\\PhysicalDrive{ident} lesen und auf 0 prüfen",
                      f"Initialize-Disk -PartitionStyle {style}",
                      "New-Partition -UseMaximumSize -AssignDriveLetter",
                      f"Format-Volume -FileSystem {plan.filesystem} (Schnellformatierung)"]
        plan.estimate_s = size / sp * 1.05 + 20
        if d is not None:
            plan.warnings.append(f"ALLE Partitionen und Daten auf Datenträger {ident} gehen verloren"
                                 + (f" ({', '.join(x.name for x in d.volumes)})." if d.volumes else "."))
        plan.warnings.append("Abbrechen ist nur bedingt möglich: der Datenträger bleibt dann ohne Partition und ist teilweise genullt.")
    if d is not None and not d.removable:
        plan.warnings.append("ACHTUNG: interner Datenträger!")
    return plan


# ---------------------------------------------------------------- Ausführung

class Progress:
    """Rechnet Prozent + vergangene Zeit in Restzeit um und meldet an den Callback."""

    def __init__(self, cb, estimate_s: float):
        self.cb = cb or (lambda pct, msg: None)
        self.t0 = time.monotonic()
        self.estimate = estimate_s
        self.pct: float | None = None

    def eta(self) -> float | None:
        el = time.monotonic() - self.t0
        if self.pct and self.pct >= 1:
            return el * (100 - self.pct) / self.pct
        return max(0.0, self.estimate - el)

    def __call__(self, pct: float | None, msg: str) -> None:
        if pct is not None:
            self.pct = max(0.0, min(100.0, pct))
        self.cb(self.pct, msg)


@dataclass
class Job:
    progress: object = None             # callable(pct|None, msg)
    cancel: threading.Event = field(default_factory=threading.Event)
    log: list[str] = field(default_factory=list)
    _proc: ps.Streamed | None = None
    _cancel_file: str | None = None

    def emit(self, pct, msg):
        self.log.append(msg)
        if self.progress:
            self.progress(pct, msg)

    def request_cancel(self) -> None:
        self.cancel.set()
        if self._cancel_file:          # Storage-Job: sauber über RequestStateChange beenden
            try:
                open(self._cancel_file, "w").close()
            except OSError:
                pass
        elif self._proc is not None:
            self._proc.kill()

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("Vom Nutzer abgebrochen")


def execute(action: str, target: str, confirm: str | None, *, filesystem="NTFS", label="",
            full=False, partition_style=None, expect_uid: str | None = None,
            settings: Settings | None = None, job: Job | None = None, verify_samples: int | None = None) -> dict:
    """Plan neu aufbauen, alle Sperren und die Bestätigung prüfen, dann ausführen."""
    settings = settings or Settings.load()
    job = job or Job()
    if not ps.is_admin():
        raise OpError("Administratorrechte nötig")
    snap = disks.query()
    plan = make_plan(snap, settings, action, target, filesystem, label, full, partition_style)
    if not plan.allowed:
        raise OpError("Gesperrt: " + "; ".join(plan.blocked))
    if not disks.confirm_matches(plan.kind, plan.ident, confirm):
        raise OpError(f"Bestätigung passt nicht zum Ziel: erwartet '{plan.confirm_text}', erhalten '{confirm or ''}'")
    if expect_uid is not None and plan.disk is not None and _uid(plan.disk) != expect_uid:
        raise OpError("Der Datenträger hat sich seit der Bestätigung geändert – bitte neu auswählen")
    samples = settings.verify_samples if verify_samples is None else verify_samples

    prog = Progress(job.emit, plan.estimate_s)
    t0 = time.monotonic()
    result = {"action": plan.action, "target": plan.to_json()["target"], "mode": plan.mode_name,
              "disk_number": plan.disk.number if plan.disk else None}
    if plan.kind == "volume":
        if plan.action == "format" and not plan.full:
            _quick_format(plan, prog)
        else:
            _volume_zero_or_full(plan, prog, job, result)
        letter = plan.volume.letter
        if plan.action == "wipe" and samples:
            prog(None, "Stichprobe: lese zufällige Sektoren …")
            result["verify"] = sample_zero(f"\\\\.\\{letter}:", plan.volume.size,
                                           plan.disk.sector_size, samples)
    else:
        letter = _wipe_disk(plan, prog, job, samples, result)
    result.update({"ok": True, "letter": f"{letter}:" if letter else None,
                   "filesystem": plan.filesystem, "label": plan.label,
                   "seconds": round(time.monotonic() - t0, 1)})
    prog(100, "Fertig.")
    return result


def _uid(d: Disk) -> str:
    return f"{d.unique_id}|{d.serial}|{d.size}"


def disk_uid(d: Disk) -> str:
    return _uid(d)


def _quick_format(plan: Plan, prog: Progress) -> None:
    prog(None, f"Schnellformatierung {plan.volume.letter}: als {plan.filesystem} …")
    ps.run(f"Format-Volume -DriveLetter {plan.volume.letter} -FileSystem {plan.filesystem} "
           f"-NewFileSystemLabel {ps.quote(plan.label)} -Force -Confirm:$false | Out-Null", timeout=600)
    prog(100, "Formatierung abgeschlossen.")


_PCT_RE = re.compile(r"(\d{1,3})(?:[.,]\d+)?\s*(?:%|percent|prozent|pour|por)", re.I)


def _format_com(plan: Plan, prog: Progress, job: Job, zero: bool) -> None:
    v = plan.volume
    args = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "format.com"),
            f"{v.letter}:", f"/FS:{plan.filesystem}", f"/V:{plan.label}", "/Y", "/X"]
    if zero:
        args.append("/P:0")
    enc = "mbcs"   # format.com schreibt in eine Pipe im ANSI-Zeichensatz
    # Falls format nach dem aktuellen Namen fragt: den bekannten Namen eingeben.
    stdin = f"{v.label}\r\n" if v.label else "\r\n"
    prog(0, ("Fülle " if zero else "Formatiere ") + f"{v.letter}: (" + " ".join(args[1:]) + ")")
    job.check()
    proc = ps.Streamed(args, encoding=enc, stdin_text=stdin)
    job._proc = proc
    tail: list[str] = []
    try:
        for line in proc.lines():
            m = _PCT_RE.search(line)
            if m:
                prog(float(m.group(1)), f"{m.group(1)} % – Rest ca. {fmt_duration(prog.eta())}")
            else:
                tail.append(line)
                job.log.append(line)
        rc = proc.wait()
    finally:
        job._proc = None
    if job.cancel.is_set():
        raise Cancelled(f"Abgebrochen – {v.letter}: ist jetzt evtl. unformatiert (RAW) und muss neu formatiert werden")
    if rc != 0:
        msg = " | ".join(tail[-4:])
        if re.search(r"spärlich|thin|sparse", msg, re.I):
            raise ThinVolume(msg)
        raise OpError(f"format.com Exit {rc}: " + msg)


class ThinVolume(OpError):
    """format.com verweigert die vollständige Formatierung (thin provisioned / TRIM-fähig)."""


def _zero_volume(plan: Plan, prog: Progress, job: Job) -> None:
    """Ersatzweg für format /P:0: Volume sperren + aushängen, Nullen schreiben, dann schnell formatieren."""
    from . import rawio
    v = plan.volume
    prog(0, f"Schreibe Nullen direkt auf {v.letter}: (Volume gesperrt und ausgehängt) …")
    job.check()
    with rawio.VolumeWriter(v.letter) as w:
        total = w.length - w.length % max(512, plan.disk.sector_size)
        last = [0.0]

        def on_progress(done):
            now = time.monotonic()
            if now - last[0] > 0.5 or done >= total:
                last[0] = now
                pct = done * 100 / total
                prog(pct, f"{pct:.0f} % ({fmt_size(done)} von {fmt_size(total)}) – Rest ca. {fmt_duration(prog.eta())}")
        w.write_zeros(total, 4 * 1024 * 1024, on_progress, job.cancel.is_set)
    if job.cancel.is_set():
        raise Cancelled(f"Abgebrochen – {v.letter}: ist jetzt unformatiert (RAW) und nur teilweise genullt")
    time.sleep(1)
    _quick_format(plan, prog)


def _volume_zero_or_full(plan: Plan, prog: Progress, job: Job, result: dict) -> None:
    zero = plan.action == "wipe"
    method = os.environ.get("DISKFORMATTER_ZERO_METHOD", "auto")
    if method != "python":
        try:
            _format_com(plan, prog, job, zero=zero)
            result["zero_method"] = "format /P:0" if zero else "format (vollständig)"
            return
        except ThinVolume as e:
            job.emit(None, "format.com lehnt ab (thin provisioned): " + str(e)[:160])
            job.emit(None, "→ Ersatzweg: Nullen direkt schreiben, danach Schnellformatierung")
    _zero_volume(plan, prog, job)
    result["zero_method"] = "direktes Schreiben (Volume) + Schnellformatierung"


_CLEAR_SCRIPT = r"""
$n = {n}
$cancel = {cancel}
$disk = Get-CimInstance -Namespace root/Microsoft/Windows/Storage -ClassName MSFT_Disk -Filter "Number=$n"
if (-not $disk) {{ [Console]::WriteLine("ERROR Datenträger $n nicht gefunden"); exit 1 }}
$r = Invoke-CimMethod -InputObject $disk -MethodName Clear -Arguments @{{RemoveData=$true; RemoveOEM=$true; ZeroOutEntireDisk=$true; RunAsJob=$true}}
if ($r.ReturnValue -ne 0 -and $r.ReturnValue -ne 4096) {{ [Console]::WriteLine("UNSUPPORTED $($r.ReturnValue)"); exit 3 }}
$job = $r.CreatedStorageJob
if ($job) {{
  $asked = $false
  while ($true) {{
    $job = Get-CimInstance -InputObject $job
    [Console]::WriteLine("PROGRESS $($job.PercentComplete) $($job.BytesProcessed) $($job.BytesTotal)")
    if ($job.JobState -ge 7) {{ break }}
    if (-not $asked -and (Test-Path -LiteralPath $cancel)) {{
      $asked = $true
      try {{ Invoke-CimMethod -InputObject $job -MethodName RequestStateChange -Arguments @{{RequestedState=[uint16]4}} | Out-Null }} catch {{ }}
      [Console]::WriteLine("CANCELREQ")
    }}
    Start-Sleep -Milliseconds 700
  }}
  [Console]::WriteLine("FINALSTATE $($job.JobState)")
  if ($job.JobState -ne 7) {{ [Console]::WriteLine("ERROR Storage-Job Status $($job.JobState) $($job.ErrorDescription)"); exit 1 }}
}}
[Console]::WriteLine("DONE")
"""


def _wipe_disk(plan: Plan, prog: Progress, job: Job, samples: int, result: dict) -> str:
    d, n = plan.disk, int(plan.ident)
    if d.partition_style.upper() == "RAW":
        prog(None, "Datenträger ist nicht initialisiert – initialisiere kurz für Clear …")
        ps.run(f"Initialize-Disk -Number {n} -PartitionStyle GPT", timeout=120)
    job.check()
    method = os.environ.get("DISKFORMATTER_ZERO_METHOD", "auto")
    used = "python"
    if method != "python":
        used = _zero_clear_disk(plan, prog, job)
    if used == "python":
        prog(None, "Entferne Partitionen (Clear-Disk -RemoveData) …")
        ps.run(f"Clear-Disk -Number {n} -RemoveData -RemoveOEM -Confirm:$false", timeout=300)
        _zero_python(n, d.size, d.sector_size, prog, job)
    result["zero_method"] = ("MSFT_Disk.Clear(ZeroOutEntireDisk)" if used == "clear"
                             else "direktes Schreiben (PhysicalDrive)")
    result["zeroed_bytes"] = d.size
    if samples:
        prog(None, "Stichprobe: lese zufällige Sektoren des ganzen Datenträgers …")
        v = sample_zero(f"\\\\.\\PhysicalDrive{n}", d.size, d.sector_size, samples)
        result["verify"] = v
        if v["zero_samples"] != v["samples"]:
            raise OpError(f"Stichprobe: {v['samples'] - v['zero_samples']} gelesene Blöcke sind nicht 0 "
                          "– Nullen fehlgeschlagen")
    job.check()
    prog(None, f"Initialisiere ({plan.partition_style}), lege Partition an und formatiere als {plan.filesystem} …")
    out = ps.run_json(
        f"Update-Disk -Number {n}; Initialize-Disk -Number {n} -PartitionStyle {plan.partition_style};"
        f"$p = New-Partition -DiskNumber {n} -UseMaximumSize -AssignDriveLetter;"
        f"$v = $p | Format-Volume -FileSystem {plan.filesystem} -NewFileSystemLabel {ps.quote(plan.label)} -Force -Confirm:$false;"
        "$p = Get-Partition -DiskNumber $p.DiskNumber -PartitionNumber $p.PartitionNumber;"
        "[pscustomobject]@{Letter=[string]$p.DriveLetter; Size=$v.Size} | ConvertTo-Json -Compress",
        timeout=900) or {}
    return disks._letter(out.get("Letter"))


def _zero_clear_disk(plan: Plan, prog: Progress, job: Job) -> str:
    """Nullen per MSFT_Disk.Clear(ZeroOutEntireDisk) als Storage-Job.

    Gibt 'clear' zurück oder 'python', wenn der Datenträger das nicht unterstützt.
    """
    n = int(plan.ident)
    fd, cancel_file = tempfile.mkstemp(prefix="df_cancel_")
    os.close(fd)
    os.remove(cancel_file)
    prog(0, f"Clear (ZeroOutEntireDisk) Datenträger {n}: überschreibe {fmt_size(plan.disk.size)} mit Nullen …")
    proc = ps.streamed_ps(_CLEAR_SCRIPT.format(n=n, cancel=ps.quote(cancel_file)))
    job._proc, job._cancel_file = proc, cancel_file
    errs: list[str] = []
    unsupported = False
    final_state = ""
    try:
        for line in proc.lines():
            if line.startswith("PROGRESS"):
                try:
                    pct = float(line.split()[1])
                except (IndexError, ValueError):
                    continue
                prog(pct, f"{pct:.0f} % – Rest ca. {fmt_duration(prog.eta())}")
            elif line.startswith("UNSUPPORTED"):
                unsupported = True
                job.log.append("Clear(ZeroOutEntireDisk) nicht unterstützt: " + line)
            elif line.startswith("ERROR"):
                errs.append(line[6:])
            elif line.startswith("FINALSTATE"):
                final_state = line.split()[-1]
            elif line == "CANCELREQ":
                job.log.append("Storage-Job: Abbruch angefordert")
            elif line != "DONE":
                job.log.append(line)
        rc = proc.wait()
    finally:
        job._proc, job._cancel_file = None, None
        try:
            os.remove(cancel_file)
        except OSError:
            pass
    if job.cancel.is_set():
        if final_state == "7" or (rc == 0 and not final_state):
            raise Cancelled(f"Abbruch angefordert, aber Windows hatte das Nullen schon abgeschlossen – "
                            f"Datenträger {n} ist vollständig genullt und ohne Partition (neu formatieren nötig)")
        raise Cancelled(f"Abgebrochen – Datenträger {n} ist jetzt ohne Partition und nur teilweise genullt")
    if unsupported or rc == 3:
        prog(0, "ZeroOutEntireDisk wird von diesem Datenträger nicht unterstützt – nulle direkt …")
        return "python"
    if rc != 0:
        raise OpError("Nullen (Clear) fehlgeschlagen: " + ("; ".join(errs) or f"Exit {rc}"))
    return "clear"


def _zero_python(n: int, size: int, sector: int, prog: Progress, job: Job, chunk: int = 4 * 1024 * 1024) -> None:
    """Schreibt Nullen direkt auf \\\\.\\PhysicalDriveN (nachdem alle Partitionen entfernt sind)."""
    size -= size % sector
    zeros = bytes(chunk)
    done, last = 0, 0.0
    prog(0, f"Schreibe Nullen auf \\\\.\\PhysicalDrive{n} …")
    with open(f"\\\\.\\PhysicalDrive{n}", "r+b", buffering=0) as f:
        while done < size:
            if job.cancel.is_set():
                raise Cancelled(f"Abgebrochen – Datenträger {n} ist jetzt ohne Partition und nur teilweise genullt")
            k = min(chunk, size - done)
            f.write(zeros if k == chunk else zeros[:k])
            done += k
            now = time.monotonic()
            if now - last > 0.5 or done >= size:
                last = now
                pct = done * 100 / size
                prog(pct, f"{pct:.0f} % ({fmt_size(done)} von {fmt_size(size)}) – Rest ca. {fmt_duration(prog.eta())}")


def sample_zero(device: str, size: int, sector: int, samples: int, block: int = 4096) -> dict:
    """Liest ``samples`` zufällige Blöcke (sektor-ausgerichtet) und zählt, wie viele nur Nullen enthalten."""
    block = max(block, sector)
    nblocks = max(1, size // block)
    rnd = random.Random()
    picks = sorted({0, nblocks - 1} | {rnd.randrange(nblocks) for _ in range(max(0, samples - 2))})
    zero = 0
    nonzero_at: list[int] = []
    with open(device, "rb", buffering=0) as f:
        for b in picks:
            f.seek(b * block)
            data = f.read(block)
            if data and not any(data):
                zero += 1
            elif len(nonzero_at) < 20:
                nonzero_at.append(b * block)
    return {"device": device, "samples": len(picks), "zero_samples": zero,
            "zero_ratio": round(zero / len(picks), 4), "block_bytes": block,
            "nonzero_offsets": nonzero_at}
