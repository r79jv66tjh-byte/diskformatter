"""Sperren und Bestätigung – mit nachgebauten Laufwerksdaten, ohne echte Datenträger anzufassen."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diskformatter import disks, ops  # noqa: E402
from diskformatter.settings import Settings  # noqa: E402

GB = 1000 ** 3


def raw():
    return {
        "disks": [
            {"Number": 0, "Model": "SYS-SSD", "Serial": "S0", "BusType": "NVMe", "Size": 1000 * GB,
             "IsSystem": True, "IsBoot": True, "PartitionStyle": "GPT", "UniqueId": "u0"},
            {"Number": 1, "Model": "DATA-HDD", "Serial": "S1", "BusType": "SATA", "Size": 4000 * GB,
             "PartitionStyle": "GPT", "UniqueId": "u1"},
            {"Number": 2, "Model": "USB-STICK", "Serial": "S2", "BusType": "USB", "Size": 16 * GB,
             "PartitionStyle": "MBR", "UniqueId": "u2"},
            {"Number": 3, "Model": "Hidden-Sys", "Serial": "S3", "BusType": "SATA", "Size": 500 * GB,
             "PartitionStyle": "GPT", "UniqueId": "u3"},           # Windows liegt hier (W:)
            {"Number": 4, "Model": "PAGE", "Serial": "S4", "BusType": "USB", "Size": 64 * GB,
             "PartitionStyle": "GPT", "UniqueId": "u4"},           # Auslagerungsdatei auf P:
            {"Number": 5, "Model": "BigStick", "Serial": "S5", "BusType": "7", "Size": 128 * GB,
             "PartitionStyle": "GPT", "UniqueId": "u5", "IsReadOnly": True},
            {"Number": 6, "Model": "EFI-Disk", "Serial": "S6", "BusType": "SATA", "Size": 100 * GB,
             "PartitionStyle": "GPT", "UniqueId": "u6"},           # trägt nur die EFI-Partition
        ],
        "partitions": [
            {"DiskNumber": 0, "PartitionNumber": 3, "DriveLetter": "C", "IsBoot": True,
             "AccessPaths": ["C:\\", "\\\\?\\Volume{c}\\"]},
            {"DiskNumber": 1, "PartitionNumber": 2, "DriveLetter": "D", "AccessPaths": ["D:\\", "\\\\?\\Volume{d}\\"]},
            {"DiskNumber": 2, "PartitionNumber": 1, "DriveLetter": "E", "AccessPaths": ["E:\\", "\\\\?\\Volume{e}\\"]},
            {"DiskNumber": 3, "PartitionNumber": 1, "DriveLetter": "W", "AccessPaths": ["W:\\", "\\\\?\\Volume{w}\\"]},
            {"DiskNumber": 4, "PartitionNumber": 1, "DriveLetter": "P", "AccessPaths": ["P:\\", "\\\\?\\Volume{p}\\"]},
            {"DiskNumber": 5, "PartitionNumber": 1, "DriveLetter": "F", "AccessPaths": ["F:\\", "\\\\?\\Volume{f}\\"]},
            {"DiskNumber": 6, "PartitionNumber": 1, "DriveLetter": "", "IsSystem": True,
             "AccessPaths": ["\\\\?\\Volume{efi}\\"]},
        ],
        "volumes": [
            {"DriveLetter": "C", "FileSystem": "NTFS", "Size": 999 * GB, "DriveType": "Fixed", "Path": "\\\\?\\Volume{c}\\"},
            {"DriveLetter": "D", "Label": "Daten", "FileSystem": "NTFS", "Size": 4000 * GB, "DriveType": "Fixed", "Path": "\\\\?\\Volume{d}\\"},
            {"DriveLetter": "E", "Label": "STICK", "FileSystem": "FAT32", "Size": 16 * GB, "DriveType": "Removable", "Path": "\\\\?\\Volume{e}\\"},
            {"DriveLetter": "W", "FileSystem": "NTFS", "Size": 500 * GB, "DriveType": "Fixed", "Path": "\\\\?\\Volume{w}\\"},
            {"DriveLetter": "P", "FileSystem": "NTFS", "Size": 64 * GB, "DriveType": "Fixed", "Path": "\\\\?\\Volume{p}\\"},
            {"DriveLetter": "F", "FileSystem": "exFAT", "Size": 128 * GB, "DriveType": "Removable", "Path": "\\\\?\\Volume{f}\\"},
            {"DriveLetter": "", "FileSystem": "FAT32", "Size": 100 * 1024 ** 2, "DriveType": "Fixed", "Path": "\\\\?\\Volume{efi}\\"},
            {"DriveLetter": "Z", "FileSystem": "UDF", "Size": 4 * GB, "DriveType": "CD-ROM", "Path": "\\\\?\\Volume{cd}\\"},
        ],
        "pagefiles": ["P:\\pagefile.sys"],
    }


def snap():
    return disks.build_snapshot(raw(), {"C", "W"})


SAFE = Settings(allow_internal=False)
UNLOCKED = Settings(allow_internal=True)


def plan(action, target, settings=SAFE, **kw):
    with mock.patch.object(ops, "_own_letters", return_value=set()):
        return ops.make_plan(snap(), settings, action, target, kw.pop("fs", "exFAT"), kw.pop("label", "X"), **kw)


class Locks(unittest.TestCase):
    def test_system_disk_always_locked(self):
        for st in (SAFE, UNLOCKED):
            for action, tgt in (("format", "C:"), ("wipe", "C"), ("wipe", "0")):
                p = plan(action, tgt, st, fs="NTFS")
                self.assertFalse(p.allowed, (action, tgt))
                self.assertTrue(any("System" in b or "Boot" in b for b in p.blocked))

    def test_windows_drive_disk_locked(self):
        p = plan("wipe", "3", UNLOCKED, fs="NTFS")
        self.assertFalse(p.allowed)
        self.assertIn("Windows", " ".join(p.blocked))

    def test_efi_partition_disk_locked(self):
        p = plan("wipe", "6", UNLOCKED, fs="NTFS")
        self.assertFalse(p.allowed)
        self.assertIn("System-/Boot-Partition", " ".join(p.blocked))

    def test_pagefile_disk_locked_even_if_usb(self):
        p = plan("wipe", "P:", UNLOCKED, fs="NTFS")
        self.assertFalse(p.allowed)
        self.assertIn("Auslagerungsdatei", " ".join(p.blocked))

    def test_internal_needs_unlock(self):
        self.assertFalse(plan("format", "D:", SAFE, fs="NTFS").allowed)
        self.assertFalse(plan("wipe", "1", SAFE, fs="NTFS").allowed)
        self.assertTrue(plan("format", "D:", UNLOCKED, fs="NTFS").allowed)
        self.assertTrue(plan("wipe", "1", UNLOCKED, fs="NTFS").allowed)
        self.assertIn("interner", " ".join(plan("wipe", "1", UNLOCKED, fs="NTFS").warnings).lower())

    def test_usb_allowed_by_default(self):
        self.assertTrue(plan("format", "E:").allowed)
        self.assertTrue(plan("wipe", "E").allowed)
        p = plan("wipe", "2")
        self.assertTrue(p.allowed)
        self.assertEqual(p.partition_style, "MBR")

    def test_readonly_and_numeric_bus(self):
        p = plan("wipe", "5")
        self.assertEqual(p.disk.bus_type, "USB")
        self.assertFalse(p.allowed)
        self.assertIn("schreibgeschützt", " ".join(p.blocked))

    def test_cdrom_orphan_locked(self):
        p = plan("format", "Z:")
        self.assertFalse(p.allowed)

    def test_own_drive_locked(self):
        with mock.patch.object(ops, "_own_letters", return_value={"E"}):
            p = ops.make_plan(snap(), SAFE, "wipe", "2", "exFAT", "X")
        self.assertFalse(p.allowed)

    def test_fat32_limit_and_label(self):
        self.assertFalse(plan("format", "D:", UNLOCKED, fs="FAT32").allowed)
        self.assertFalse(plan("format", "E:", fs="FAT32", label="VIEL_ZU_LANGER_NAME").allowed)
        self.assertFalse(plan("format", "E:", fs="ext4").allowed)

    def test_format_needs_letter(self):
        with self.assertRaises(ops.OpError):
            plan("format", "2")
        with self.assertRaises(ops.OpError):
            plan("format", "Q:")          # gibt es nicht

    def test_summary_contains_serial_and_model(self):
        p = plan("wipe", "2")
        s = "\n".join(p.summary_lines())
        self.assertIn("USB-STICK", s)
        self.assertIn("S2", s)
        self.assertEqual(p.confirm_text, "2")


class Confirm(unittest.TestCase):
    def test_volume(self):
        for ok in ("E", "E:", "e", "e:"):
            self.assertTrue(disks.confirm_matches("volume", "E", ok), ok)
        for bad in ("", "F", "E:\\x", "EE", "2", None, " "):
            self.assertFalse(disks.confirm_matches("volume", "E", bad), bad)

    def test_disk(self):
        self.assertTrue(disks.confirm_matches("disk", 2, "2"))
        self.assertTrue(disks.confirm_matches("disk", 2, " 2 "))
        for bad in ("02", "E", "3", "disk2", "", None):
            self.assertFalse(disks.confirm_matches("disk", 2, bad), bad)

    def test_parse_target(self):
        self.assertEqual(disks.parse_target("e:\\"), ("volume", "E"))
        self.assertEqual(disks.parse_target("disk:4"), ("disk", 4))
        with self.assertRaises(ValueError):
            disks.parse_target("C:\\Windows")


class ExecuteGuards(unittest.TestCase):
    """execute() prüft Admin, Sperren, Bestätigung und Kennung, bevor irgendetwas läuft."""

    def run_exec(self, target, confirm, settings=SAFE, admin=True, uid=None):
        called = []
        with mock.patch.object(ops.ps, "is_admin", return_value=admin), \
             mock.patch.object(ops.disks, "query", side_effect=snap), \
             mock.patch.object(ops, "_own_letters", return_value=set()), \
             mock.patch.object(ops.ps, "run", side_effect=lambda *a, **k: called.append(a)), \
             mock.patch.object(ops.ps, "Streamed", side_effect=lambda *a, **k: called.append(a)), \
             mock.patch.object(ops.ps, "streamed_ps", side_effect=lambda *a, **k: called.append(a)):
            try:
                ops.execute("wipe", target, confirm, filesystem="exFAT", label="X",
                            settings=settings, expect_uid=uid)
            except ops.OpError as e:
                return str(e), called
        return None, called

    def test_blocked_targets_never_run(self):
        for tgt in ("C:", "0", "3", "6", "P:", "D:", "1"):
            err, called = self.run_exec(tgt, tgt.rstrip(":"))
            self.assertIsNotNone(err, tgt)
            self.assertEqual(called, [], tgt)

    def test_wrong_confirm_never_runs(self):
        for c in ("", "3", "E", "02"):
            err, called = self.run_exec("2", c)
            self.assertIn("Bestätigung", err)
            self.assertEqual(called, [])

    def test_not_admin(self):
        err, called = self.run_exec("2", "2", admin=False)
        self.assertIn("Administrator", err)
        self.assertEqual(called, [])

    def test_changed_disk(self):
        err, called = self.run_exec("2", "2", uid="anderer|datenträger|1")
        self.assertIn("geändert", err)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
