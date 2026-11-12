"""Integrationstest an einer TEST-VHDX (braucht Administratorrechte).

Legt eine virtuelle Festplatte an, hängt sie an und testet daran CLI und GUI:
Formatieren, Volume nullen, ganzen Datenträger nullen, Sperren, Bestätigung, Abbrechen.
Danach wird die VHDX getrennt und gelöscht. Echte Laufwerke werden nie verändert: vor jeder
Aktion wird geprüft, dass das Ziel genau die Test-VHDX ist (BusType + Dateipfad).

    python tests\\vhd_integration.py [--out ergebnis.json] [--size-mb 2048]
"""
from __future__ import annotations

import argparse
import json
import re
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from diskformatter import disks, ops, ps  # noqa: E402
from diskformatter.settings import Settings  # noqa: E402

RESULTS: list[dict] = []


def log(name, ok, **info):
    RESULTS.append({"test": name, "ok": bool(ok), **info})
    print(("OK   " if ok else "FAIL ") + name, json.dumps(info, ensure_ascii=False, default=str)[:400], flush=True)


def diskpart(script: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="ascii") as f:
        f.write(script)
        name = f.name
    try:
        p = subprocess.run(["diskpart", "/s", name], capture_output=True, timeout=300)
        return p.stdout.decode(ps.oem_encoding(), "replace")
    finally:
        os.remove(name)


class TestDisk:
    def __init__(self, path: Path, size_mb: int):
        self.path, self.size_mb = path, size_mb
        self.number: int | None = None

    def create(self):
        if self.path.exists():
            self.path.unlink()
        out = diskpart(f'create vdisk file="{self.path}" maximum={self.size_mb} type=fixed\n'
                       f'select vdisk file="{self.path}"\nattach vdisk\n')
        for _ in range(30):
            n = self.find()
            if n is not None:
                self.number = n
                return
            time.sleep(1)
        raise RuntimeError("Test-VHDX nicht gefunden:\n" + out)

    def find(self) -> int | None:
        data = ps.as_list(ps.run_json(
            "Get-Disk | Where-Object { $_.Location -eq " + ps.quote(str(self.path)) + " } | "
            "Select-Object Number,@{n='Bus';e={[string]$_.BusType}},Location | ConvertTo-Json -Compress"))
        return int(data[0]["Number"]) if data else None

    def assert_is_test_disk(self, number: int):
        """Sicherheitsnetz: Abbruch, wenn die Nummer nicht (mehr) die Test-VHDX ist."""
        d = ps.as_list(ps.run_json(f"Get-Disk -Number {int(number)} | Select-Object Number,"
                                   "@{n='Bus';e={[string]$_.BusType}},Location,IsSystem,IsBoot | ConvertTo-Json -Compress"))
        if not d or d[0]["Bus"] != "File Backed Virtual" or str(d[0]["Location"]).lower() != str(self.path).lower() \
                or d[0]["IsSystem"] or d[0]["IsBoot"]:
            raise SystemExit(f"SICHERHEITSABBRUCH: Datenträger {number} ist nicht die Test-VHDX: {d}")

    def prepare(self, fs="NTFS", label="TESTVOL") -> str:
        self.assert_is_test_disk(self.number)
        out = ps.run_json(
            f"$d = Get-Disk -Number {self.number}; if ($d.PartitionStyle -eq 'RAW') {{ Initialize-Disk -Number {self.number} -PartitionStyle MBR }};"
            f"Get-Partition -DiskNumber {self.number} -ErrorAction SilentlyContinue | Remove-Partition -Confirm:$false;"
            f"$p = New-Partition -DiskNumber {self.number} -UseMaximumSize -AssignDriveLetter;"
            f"$p | Format-Volume -FileSystem {fs} -NewFileSystemLabel {ps.quote(label)} -Confirm:$false -Force | Out-Null;"
            "[pscustomobject]@{L=[string]$p.DriveLetter} | ConvertTo-Json -Compress")
        return disks._letter(out["L"])

    def letter(self) -> str | None:
        out = ps.as_list(ps.run_json(f"Get-Partition -DiskNumber {self.number} -ErrorAction SilentlyContinue | "
                                     "Where-Object DriveLetter | Select-Object @{n='L';e={[string]$_.DriveLetter}} | ConvertTo-Json -Compress"))
        return disks._letter(out[0]["L"]) if out else None

    def destroy(self):
        diskpart(f'select vdisk file="{self.path}"\ndetach vdisk\n')
        time.sleep(1)
        try:
            self.path.unlink()
        except OSError as e:
            print("VHDX konnte nicht gelöscht werden:", e)


def volume_info(letter: str) -> dict:
    return ps.run_json(f"Get-Volume -DriveLetter {letter} | Select-Object @{{n='FS';e={{[string]$_.FileSystemType}}}},"
                       "@{n='Label';e={$_.FileSystemLabel}},Size | ConvertTo-Json -Compress") or {}


def fill_random(letter: str, mb: int):
    with open(f"{letter}:\\random.bin", "wb") as f:
        for _ in range(mb):
            f.write(os.urandom(1024 * 1024))
        f.flush()
        os.fsync(f.fileno())


def cancelling_job() -> ops.Job:
    """Job, der sich bei der ersten Fortschrittsmeldung des Schreibvorgangs („12 % …“) selbst abbricht."""
    job = ops.Job()
    job.at = None

    def cb(pct, msg):
        if job.at is None and pct is not None and pct < 100 and re.match(r"\d+ %", msg):
            job.at = pct
            job.request_cancel()
    job.progress = cb
    return job


def cli(*args, env=None) -> tuple[int, dict | str]:
    p = subprocess.run([sys.executable, str(ROOT / "cli.py"), *args], capture_output=True, env=env, timeout=1800)
    out = p.stdout.decode("utf-8", "replace")
    try:
        return p.returncode, json.loads(out)
    except ValueError:
        return p.returncode, (out + p.stderr.decode("utf-8", "replace")).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "diskformatter_test.json"))
    ap.add_argument("--size-mb", type=int, default=2048)
    ap.add_argument("--ext", default="vhd", choices=["vhd", "vhdx"])
    ap.add_argument("--dir", default=tempfile.gettempdir())
    a = ap.parse_args()
    if not ps.is_admin():
        raise SystemExit("Braucht Administratorrechte")

    settings_file = Path(a.dir) / "df_test_settings.json"
    env = dict(os.environ, DISKFORMATTER_SETTINGS=str(settings_file))
    os.environ["DISKFORMATTER_SETTINGS"] = str(settings_file)
    vhd = TestDisk(Path(a.dir) / f"diskformatter_test.{a.ext}", a.size_mb)
    try:
        vhd.create()
        n = vhd.number
        vhd.assert_is_test_disk(n)
        L = vhd.prepare()
        log("vhdx_attached", True, disk=n, letter=L, path=str(vhd.path))

        # ---------- Sperren
        Settings(allow_internal=False).save()
        code, out = cli("drives", env=env)
        d = next(x for x in out["disks"] if x["number"] == n)
        log("lock_vhd_internal_by_default", not d["selectable"] and "Interner" in " ".join(d["locked_reasons"]),
            reasons=d["locked_reasons"])
        sysd = [x for x in out["disks"] if x["system"]]
        log("lock_system_disk_listed_locked", bool(sysd) and all(not x["selectable"] for x in sysd),
            system_disks=[(x["number"], x["locked_reasons"][:3]) for x in sysd])
        code, out = cli("format", "--target", f"{L}:", "--fs", "exFAT", "--confirm-target", f"{L}:", env=env)
        log("lock_cli_refuses_internal_without_unlock", code == 2 and "Interner" in str(out), code=code, out=str(out)[:200])
        for tgt in ("C:", "0"):
            code, out = cli("wipe", "--target", tgt, "--confirm-target", tgt.rstrip(":"), "--dry-run", env=env)
            log(f"lock_dry_run_system_{tgt}", code == 0 and out["allowed"] is False, blocked=out["blocked_reasons"][:4])

        Settings(allow_internal=True).save()
        code, out = cli("format", "--target", f"{L}:", "--fs", "exFAT", "--label", "NOPE", "--confirm-target", "Q:", env=env)
        log("confirm_wrong_letter_refused", code == 2 and volume_info(L)["FS"] == "NTFS", out=str(out)[:200])
        code, out = cli("format", "--target", f"{L}:", "--fs", "exFAT", "--label", "NOPE", env=env)
        log("confirm_missing_refused", code == 2 and volume_info(L)["FS"] == "NTFS", out=str(out)[:200])
        code, out = cli("wipe", "--target", str(n), "--confirm-target", f"{L}:", env=env)
        log("confirm_letter_for_disk_refused", code == 2, out=str(out)[:200])
        code, out = cli("format", "--target", f"{L}:", "--fs", "exFAT", "--label", "NOPE",
                        "--confirm-target", f"{L}:", "--dry-run", env=env)
        log("dry_run_changes_nothing", code == 0 and out["dry_run"] and out["confirm_target_ok"]
            and volume_info(L)["FS"] == "NTFS", info=volume_info(L))

        # ---------- CLI: formatieren
        vhd.assert_is_test_disk(n)
        code, out = cli("format", "--target", f"{L}:", "--fs", "exFAT", "--label", "CLIFMT",
                        "--confirm-target", f"{L}:", env=env)
        vi = volume_info(L)
        log("cli_format_exfat", code == 0 and vi["FS"] == "exFAT" and vi["Label"] == "CLIFMT", out=out, vol=vi)

        # ---------- CLI: Volume nullen
        fill_random(L, 800)
        before = ops.sample_zero(f"\\\\.\\{L}:", vi["Size"], 512, 400)
        vhd.assert_is_test_disk(n)
        code, out = cli("wipe", "--target", f"{L}:", "--fs", "NTFS", "--label", "CLIWIPE",
                        "--confirm-target", f"{L}:", env=env)
        vi = volume_info(L)
        after = ops.sample_zero(f"\\\\.\\{L}:", vi["Size"], 512, 2000)
        log("cli_wipe_volume", code == 0 and out.get("verify", {}).get("zero_ratio", 0) >= 0.95 and vi["FS"] == "NTFS" and after["zero_ratio"] >= 0.95
            and before["zero_ratio"] < 0.8, before=before["zero_ratio"], after=after["zero_ratio"],
            tool_verify=out.get("verify") if isinstance(out, dict) else out,
            method=out.get("zero_method") if isinstance(out, dict) else None, seconds=out.get("seconds") if isinstance(out, dict) else None)

        # ---------- CLI: ganzen Datenträger nullen
        fill_random(L, 800)
        before = ops.sample_zero(f"\\\\.\\PhysicalDrive{n}", a.size_mb * 1024 ** 2, 512, 400)
        vhd.assert_is_test_disk(n)
        code, out = cli("wipe", "--target", str(n), "--fs", "exFAT", "--label", "CLIDISK", "--style", "GPT",
                        "--confirm-target", str(n), env=env)
        ok = code == 0 and isinstance(out, dict)
        v = out.get("verify", {}) if ok else {}
        L = vhd.letter()
        vi = volume_info(L) if L else {}
        log("cli_wipe_disk", ok and v.get("zero_samples") == v.get("samples") and vi.get("FS") == "exFAT"
            and vi.get("Label") == "CLIDISK" and before["zero_ratio"] < 0.8,
            before=before["zero_ratio"], verify=v, new_letter=L, vol=vi, out=out if not ok else out.get("seconds"))

        # ---------- Abbrechen mitten im Schreiben (Volume)
        fill_random(L, 1500)
        vhd.assert_is_test_disk(n)
        job = cancelling_job()
        try:
            ops.execute("wipe", f"{L}:", f"{L}:", filesystem="NTFS", label="CANCEL", job=job,
                        settings=Settings(allow_internal=True))
            log("cancel_volume_wipe", False, note="lief durch, bevor abgebrochen wurde")
        except ops.Cancelled as e:
            time.sleep(2)
            raw = ops.sample_zero(f"\\\\.\\{L}:", 1900 * 1024 ** 2, 512, 400)["zero_ratio"]
            log("cancel_volume_wipe", job.at is not None and raw < 0.95, msg=str(e), cancelled_at_pct=job.at,
                zero_ratio_after_cancel=raw)
        except Exception as e:  # noqa: BLE001
            log("cancel_volume_wipe", False, err=repr(e))
        time.sleep(2)
        L = vhd.prepare(label="AFTERCANCEL")

        # ---------- Ersatzweg Volume: direkt schreiben (auch wenn format.com es könnte)
        fill_random(L, 600)
        vhd.assert_is_test_disk(n)
        os.environ["DISKFORMATTER_ZERO_METHOD"] = "python"
        try:
            res = ops.execute("wipe", f"{L}:", f"{L}:", filesystem="exFAT", label="PYVOL",
                              settings=Settings(allow_internal=True), verify_samples=2000)
            vi = volume_info(L)
            log("ops_wipe_volume_direct", res["verify"]["zero_ratio"] >= 0.95 and vi["FS"] == "exFAT"
                and vi["Label"] == "PYVOL", verify=res["verify"]["zero_ratio"], method=res["zero_method"])
        except Exception as e:  # noqa: BLE001
            log("ops_wipe_volume_direct", False, err=repr(e))
        finally:
            os.environ.pop("DISKFORMATTER_ZERO_METHOD", None)
            L = vhd.prepare(label="AFTERPYVOL")

        # ---------- Ersatzweg: direktes Nullen über \.\PhysicalDriveN
        fill_random(L, 600)
        vhd.assert_is_test_disk(n)
        os.environ["DISKFORMATTER_ZERO_METHOD"] = "python"
        try:
            res = ops.execute("wipe", str(n), str(n), filesystem="NTFS", label="PYZERO",
                              settings=Settings(allow_internal=True), verify_samples=2000)
            L = vhd.letter()
            vi = volume_info(L)
            v = res["verify"]
            log("ops_wipe_disk_python_fallback", v["zero_samples"] == v["samples"] and vi["FS"] == "NTFS"
                and vi["Label"] == "PYZERO", verify=v, method=res["zero_method"], seconds=res["seconds"])
        except Exception as e:  # noqa: BLE001
            log("ops_wipe_disk_python_fallback", False, err=repr(e))
        finally:
            os.environ.pop("DISKFORMATTER_ZERO_METHOD", None)

        # ---------- Abbrechen beim Datenträger-Nullen (Storage-Job)
        L = vhd.letter()
        fill_random(L, 1500)
        vhd.assert_is_test_disk(n)
        job = cancelling_job()
        try:
            ops.execute("wipe", str(n), str(n), filesystem="NTFS", label="X", job=job,
                        settings=Settings(allow_internal=True))
            log("cancel_disk_wipe", False, note="Storage-Job war fertig, bevor der Abbruch griff", at=job.at)
        except ops.Cancelled as e:
            ratio = ops.sample_zero(f"\\\\.\\PhysicalDrive{n}", a.size_mb * 1024 ** 2, 512, 400)["zero_ratio"]
            # Ehrliche Meldung verlangt: entweder wirklich teilweise genullt, oder die Meldung sagt „vollständig“.
            honest = (ratio < 1.0 and "teilweise" in str(e)) or (ratio == 1.0 and "vollständig" in str(e))
            log("cancel_disk_wipe", job.at is not None and honest, msg=str(e), cancelled_at_pct=job.at,
                zero_ratio_after_cancel=ratio)
        except Exception as e:  # noqa: BLE001
            log("cancel_disk_wipe", False, err=repr(e))
        time.sleep(2)
        L = vhd.prepare(label="CANCELPY")
        fill_random(L, 1500)
        vhd.assert_is_test_disk(n)
        os.environ["DISKFORMATTER_ZERO_METHOD"] = "python"
        job = cancelling_job()
        try:
            ops.execute("wipe", str(n), str(n), filesystem="NTFS", label="X", job=job,
                        settings=Settings(allow_internal=True))
            log("cancel_disk_wipe_direct", False, note="lief durch")
        except ops.Cancelled as e:
            ratio = ops.sample_zero(f"\\\\.\\PhysicalDrive{n}", a.size_mb * 1024 ** 2, 512, 400)["zero_ratio"]
            log("cancel_disk_wipe_direct", job.at is not None and ratio < 0.9, msg=str(e),
                cancelled_at_pct=job.at, zero_ratio_after_cancel=ratio)
        except Exception as e:  # noqa: BLE001
            log("cancel_disk_wipe_direct", False, err=repr(e))
        finally:
            os.environ.pop("DISKFORMATTER_ZERO_METHOD", None)
        time.sleep(2)
        L = vhd.prepare(label="BEFOREGUI")

        # ---------- GUI
        gui_tests(vhd, n, L)
    except SystemExit:
        raise
    except Exception:
        log("exception", False, tb=traceback.format_exc())
    finally:
        vhd.destroy()
        try:
            settings_file.unlink()
        except OSError:
            pass
        log("vhdx_removed", not vhd.path.exists() and vhd.find() is None)
        Path(a.out).write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def gui_tests(vhd: TestDisk, n: int, L: str):
    from diskformatter import gui
    app = gui.App()
    app.quiet = True
    app.withdraw()

    def run_gui(kind, ident, action, fs, label, full=False, style="Automatisch", typed=None, timeout=900):
        vhd.assert_is_test_disk(n)
        app.refresh()
        assert app.select(kind, ident), f"Ziel {kind} {ident} nicht in der Liste"
        app.var_action.set(action)
        app.var_fs.set(fs)
        app.var_label.set(label)
        app.var_full.set(full)
        app.var_style.set(style)
        app._update_controls()
        if str(app.btn_start.cget("state")) != "normal":
            return {"started": False, "block": app.lbl_block.cget("text")}
        seen = {}

        def confirm(plan):
            seen["summary"] = plan.summary_lines()
            # Echten Bestätigungsdialog benutzen: falscher Text sperrt den Knopf, richtiger gibt frei.
            dlg = gui.ConfirmDialog(app, plan)
            dlg.var.set("Z:" if plan.kind == "disk" else "99")
            seen["wrong_disabled"] = str(dlg.ok.cget("state")) == "disabled"
            dlg.var.set(typed or plan.confirm_text)
            seen["right_enabled"] = str(dlg.ok.cget("state")) == "normal"
            dlg.ok.invoke()
            return dlg.result
        app.ask_confirmation = confirm
        app.start()
        t0 = time.time()
        while app.job is not None and time.time() - t0 < timeout:
            app.update()
            time.sleep(0.05)
        app.update()
        return {"started": True, "result": app.last_result, "error": app.last_error, **seen,
                "log": app.log.get("1.0", "end").strip().splitlines()[-4:]}

    # Sperre in der GUI: interne Platten ohne Freischalten
    app.settings.allow_internal = False
    r = run_gui("volume", L, "format", "NTFS", "X")
    log("gui_lock_internal", not r["started"] and "Interner" in r["block"], block=r.get("block"))
    sysvol = ps.system_drive()
    r = run_gui("volume", sysvol, "format", "NTFS", "X")
    log("gui_lock_system_drive", not r["started"] and "System" in r["block"], block=r.get("block"))
    app.settings.allow_internal = True

    # Vollständige Formatierung (format.com mit Fortschritt)
    r = run_gui("volume", L, "format", "FAT32", "GUIFULL", full=True)
    vi = volume_info(L)
    log("gui_format_full_fat32", r.get("result") and vi["FS"] == "FAT32" and vi["Label"] == "GUIFULL"
        and r["wrong_disabled"] and r["right_enabled"], vol=vi, err=r.get("error"), log=r.get("log"))

    # Volume nullen
    fill_random(L, 700)
    r = run_gui("volume", L, "wipe_volume", "NTFS", "GUIWIPE")
    vi = volume_info(L)
    after = ops.sample_zero(f"\\\\.\\{L}:", vi["Size"], 512, 2000)
    log("gui_wipe_volume", r.get("result") and r["result"]["verify"]["zero_ratio"] >= 0.95 and vi["FS"] == "NTFS" and after["zero_ratio"] >= 0.95,
        after=after["zero_ratio"], tool_verify=(r.get("result") or {}).get("verify"),
        method=(r.get("result") or {}).get("zero_method"), err=r.get("error"), log=r.get("log"))

    # Ganzen Datenträger nullen
    fill_random(L, 700)
    r = run_gui("disk", n, "wipe_disk", "exFAT", "GUIDISK", style="MBR")
    res = r.get("result") or {}
    v = res.get("verify", {})
    L2 = vhd.letter()
    vi = volume_info(L2) if L2 else {}
    log("gui_wipe_disk", bool(res) and v.get("zero_samples") == v.get("samples") and vi.get("FS") == "exFAT"
        and vi.get("Label") == "GUIDISK", verify=v, vol=vi, err=r.get("error"), summary=r.get("summary"))
    app.destroy()


if __name__ == "__main__":
    main()
