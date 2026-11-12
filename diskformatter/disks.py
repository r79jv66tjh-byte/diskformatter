"""Datenträger und Volumes ermitteln (Get-Disk/Get-Partition/Get-Volume) und Sperren prüfen.

Harte Sperren (nie auswählbar, auch nicht mit Einstellung):
  * Datenträger mit IsSystem/IsBoot, mit System-/Boot-Partition,
  * Datenträger mit dem Systemlaufwerk (%SystemDrive%) oder dem Windows-Ordner,
  * Datenträger mit einer Auslagerungsdatei,
  * Volumes ohne zugeordneten Datenträger (CD/DVD, Netzwerk …), schreibgeschützte/offline Datenträger.
Weiche Sperre: interne Datenträger (kein USB/SD/Wechseldatenträger) nur mit
``allow_internal`` in den Einstellungen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import ps
from .settings import Settings

REMOVABLE_BUS = {"USB", "SD", "MMC"}
_BUS_NUM = {"1": "SCSI", "2": "ATAPI", "3": "ATA", "4": "1394", "5": "SSA", "6": "Fibre Channel",
            "7": "USB", "8": "RAID", "9": "iSCSI", "10": "SAS", "11": "SATA", "12": "SD",
            "13": "MMC", "14": "Virtual", "15": "File Backed Virtual", "16": "Storage Spaces",
            "17": "NVMe"}


def fmt_size(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1000
    return f"{n:.1f} TB"


@dataclass
class Volume:
    letter: str            # "" wenn ohne Buchstabe
    label: str
    filesystem: str
    size: int
    free: int
    drive_type: str
    path: str
    disk_number: int | None = None
    partition_number: int | None = None

    @property
    def name(self) -> str:
        return f"{self.letter}:" if self.letter else (self.label or self.path)

    def to_json(self) -> dict:
        return {"letter": self.letter or None, "label": self.label, "filesystem": self.filesystem,
                "size_bytes": self.size, "size": fmt_size(self.size), "free_bytes": self.free,
                "drive_type": self.drive_type, "disk_number": self.disk_number,
                "partition_number": self.partition_number}


@dataclass
class Disk:
    number: int
    model: str
    serial: str
    bus_type: str
    size: int
    is_system: bool = False
    is_boot: bool = False
    is_offline: bool = False
    is_readonly: bool = False
    partition_style: str = ""
    location: str = ""
    unique_id: str = ""
    sector_size: int = 512
    volumes: list[Volume] = field(default_factory=list)
    hard_locks: list[str] = field(default_factory=list)

    @property
    def removable(self) -> bool:
        return (self.bus_type in REMOVABLE_BUS
                or any(v.drive_type == "Removable" for v in self.volumes))

    @property
    def is_system_disk(self) -> bool:
        return bool(self.hard_locks)

    def blocks(self, settings: Settings) -> list[str]:
        """Alle Gründe, warum dieser Datenträger (und seine Volumes) nicht bearbeitet werden darf."""
        r = list(self.hard_locks)
        if self.is_offline:
            r.append("Datenträger ist offline")
        if self.is_readonly:
            r.append("Datenträger ist schreibgeschützt")
        if not self.removable and not settings.allow_internal:
            r.append("Interner Datenträger – nur nach Freischalten in den Einstellungen")
        return r

    @property
    def title(self) -> str:
        return f"Datenträger {self.number}: {self.model or '?'} ({fmt_size(self.size)}, {self.bus_type})"

    def to_json(self, settings: Settings) -> dict:
        b = self.blocks(settings)
        return {"number": self.number, "model": self.model, "serial": self.serial,
                "bus_type": self.bus_type, "size_bytes": self.size, "size": fmt_size(self.size),
                "removable": self.removable, "system": self.is_system_disk,
                "partition_style": self.partition_style, "location": self.location,
                "selectable": not b, "locked_reasons": b,
                "volumes": [v.to_json() for v in self.volumes]}


@dataclass
class Snapshot:
    disks: list[Disk]
    orphans: list[Volume]          # Volumes ohne Datenträger (CD, Netzwerk …)

    def disk(self, number: int) -> Disk | None:
        return next((d for d in self.disks if d.number == number), None)

    def volume(self, letter: str) -> tuple[Volume, Disk | None] | None:
        letter = letter.upper()
        for d in self.disks:
            for v in d.volumes:
                if v.letter == letter:
                    return v, d
        for v in self.orphans:
            if v.letter == letter:
                return v, None
        return None


# ---------------------------------------------------------------- Abfrage

_QUERY = r"""
$d = @(Get-Disk | ForEach-Object { [pscustomobject]@{
  Number=$_.Number; Model=$_.Model; FriendlyName=$_.FriendlyName; Serial=$_.SerialNumber;
  BusType=[string]$_.BusType; Size=$_.Size; IsSystem=$_.IsSystem; IsBoot=$_.IsBoot;
  IsOffline=$_.IsOffline; IsReadOnly=$_.IsReadOnly; PartitionStyle=[string]$_.PartitionStyle;
  Location=$_.Location; UniqueId=$_.UniqueId; SectorSize=$_.LogicalSectorSize } })
$p = @(Get-Partition -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{
  DiskNumber=$_.DiskNumber; PartitionNumber=$_.PartitionNumber; DriveLetter=[string]$_.DriveLetter;
  Size=$_.Size; IsBoot=$_.IsBoot; IsSystem=$_.IsSystem; Type=[string]$_.Type;
  AccessPaths=@($_.AccessPaths) } })
$v = @(Get-Volume | ForEach-Object { [pscustomobject]@{
  DriveLetter=[string]$_.DriveLetter; Label=$_.FileSystemLabel; FileSystem=[string]$_.FileSystemType;
  Size=$_.Size; Free=$_.SizeRemaining; DriveType=[string]$_.DriveType; Path=$_.Path } })
$pf = @(Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
[pscustomobject]@{disks=$d; partitions=$p; volumes=$v; pagefiles=$pf} | ConvertTo-Json -Depth 4 -Compress
"""


def _letter(x) -> str:
    s = str(x or "").strip().replace("\x00", "")
    return s[0].upper() if s and s[0].isalpha() else ""


def _bus(x) -> str:
    s = str(x or "").strip()
    return _BUS_NUM.get(s, s or "?")


def build_snapshot(raw: dict, system_letters: set[str]) -> Snapshot:
    """Rohdaten (JSON aus _QUERY) in Disk/Volume-Objekte übersetzen und Sperren setzen."""
    disks: dict[int, Disk] = {}
    for r in ps.as_list(raw.get("disks")):
        n = int(r["Number"])
        disks[n] = Disk(
            number=n, model=(r.get("Model") or r.get("FriendlyName") or "").strip(),
            serial=(r.get("Serial") or "").strip(), bus_type=_bus(r.get("BusType")),
            size=int(r.get("Size") or 0), is_system=bool(r.get("IsSystem")),
            is_boot=bool(r.get("IsBoot")), is_offline=bool(r.get("IsOffline")),
            is_readonly=bool(r.get("IsReadOnly")), partition_style=str(r.get("PartitionStyle") or ""),
            location=str(r.get("Location") or ""), unique_id=str(r.get("UniqueId") or ""),
            sector_size=int(r.get("SectorSize") or 512))

    # Volume-Pfad -> (Datenträger, Partition)
    by_path: dict[str, tuple[int, int]] = {}
    by_letter: dict[str, tuple[int, int]] = {}
    for p in ps.as_list(raw.get("partitions")):
        dn, pn = int(p["DiskNumber"]), int(p["PartitionNumber"])
        d = disks.get(dn)
        if d is not None and (p.get("IsBoot") or p.get("IsSystem")):
            _lock(d, "Enthält die System-/Boot-Partition")
        for ap in ps.as_list(p.get("AccessPaths")):
            if str(ap).lower().startswith("\\\\?\\volume"):
                by_path[str(ap).lower().rstrip("\\")] = (dn, pn)
        if _letter(p.get("DriveLetter")):
            by_letter[_letter(p.get("DriveLetter"))] = (dn, pn)

    orphans: list[Volume] = []
    for r in ps.as_list(raw.get("volumes")):
        v = Volume(letter=_letter(r.get("DriveLetter")), label=str(r.get("Label") or ""),
                   filesystem=str(r.get("FileSystem") or ""), size=int(r.get("Size") or 0),
                   free=int(r.get("Free") or 0), drive_type=str(r.get("DriveType") or ""),
                   path=str(r.get("Path") or ""))
        hit = by_path.get(v.path.lower().rstrip("\\")) or (by_letter.get(v.letter) if v.letter else None)
        if hit and hit[0] in disks:
            v.disk_number, v.partition_number = hit
            disks[hit[0]].volumes.append(v)
        else:
            orphans.append(v)

    page_letters = {_letter(p) for p in ps.as_list(raw.get("pagefiles")) if _letter(p)}
    for d in disks.values():
        if d.is_system:
            _lock(d, "Systemdatenträger (IsSystem)")
        if d.is_boot:
            _lock(d, "Boot-Datenträger (IsBoot)")
        letters = {v.letter for v in d.volumes if v.letter}
        for L in sorted(letters & system_letters):
            _lock(d, f"Enthält das Windows-/Systemlaufwerk {L}:")
        for L in sorted(letters & page_letters):
            _lock(d, f"Enthält eine Auslagerungsdatei ({L}:)")
        d.volumes.sort(key=lambda v: (v.partition_number or 0))
    return Snapshot(disks=sorted(disks.values(), key=lambda d: d.number), orphans=orphans)


def _lock(d: Disk, reason: str) -> None:
    if reason not in d.hard_locks:
        d.hard_locks.append(reason)


def system_letters() -> set[str]:
    return {ps.system_drive(), ps.windows_drive()}


def query() -> Snapshot:
    raw = ps.run_json(_QUERY, timeout=90) or {}
    return build_snapshot(raw, system_letters())


# ---------------------------------------------------------------- Ziele

_LETTER_RE = re.compile(r"^([A-Za-z])(:\\?)?$")
_DISK_RE = re.compile(r"^(?:disk\s*:?\s*|datenträger\s*)?(\d{1,3})$", re.I)


def parse_target(text: str) -> tuple[str, str | int]:
    """'E', 'E:', 'E:\\' -> ('volume', 'E'); '2', 'disk:2' -> ('disk', 2)."""
    t = (text or "").strip()
    m = _LETTER_RE.match(t)
    if m:
        return "volume", m.group(1).upper()
    m = _DISK_RE.match(t)
    if m:
        return "disk", int(m.group(1))
    raise ValueError(f"Ungültiges Ziel '{text}': Laufwerksbuchstabe (z. B. E:) oder Datenträgernummer (z. B. 2) angeben.")


def confirm_matches(kind: str, ident: str | int, typed: str | None) -> bool:
    """Bestätigung muss exakt zum Ziel passen: Buchstabe (E oder E:) bzw. Nummer."""
    t = (typed or "").strip()
    if kind == "volume":
        return bool(re.fullmatch(r"[A-Za-z]:?", t)) and t[0].upper() == str(ident).upper()
    return t == str(ident)
