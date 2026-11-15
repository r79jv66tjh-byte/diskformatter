"""Systemlaufwerk-Erkennung: lieber abbrechen als ein Laufwerk raten (Grundlage der Sperre)."""
import ctypes
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diskformatter import ps  # noqa: E402


def windows_api(path):
    """Simulates GetWindowsDirectoryW: writes the path and returns its length (0 = failure)."""
    def call(buf, size):
        buf.value = path
        return len(path)
    return mock.Mock(kernel32=mock.Mock(GetWindowsDirectoryW=call))


class SystemDrive(unittest.TestCase):
    def test_reads_system_drive(self):
        with mock.patch.dict(os.environ, {"SystemDrive": "d:"}):
            self.assertEqual(ps.system_drive(), "D")

    def test_missing_system_drive_stops(self):
        with mock.patch.dict(os.environ, clear=True):
            with self.assertRaises(RuntimeError):
                ps.system_drive()


class WindowsDrive(unittest.TestCase):
    def test_reads_windows_folder(self):
        with mock.patch.object(ctypes, "windll", windows_api("W:\\Windows"), create=True):
            self.assertEqual(ps.windows_drive(), "W")

    def test_failed_api_stops_even_with_environment(self):
        with mock.patch.object(ctypes, "windll", windows_api(""), create=True), \
                mock.patch.dict(os.environ, {"SystemRoot": "C:\\Windows"}):
            with self.assertRaises(RuntimeError):
                ps.windows_drive()


if __name__ == "__main__":
    unittest.main()
