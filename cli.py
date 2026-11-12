"""DiskFormatter-Kommandozeile für Claude/ToolHub.

Jeder Befehl schreibt genau ein JSON-Objekt auf stdout. Fehler: Meldung auf stderr, Exit-Code ≠ 0
(2 = gesperrt/Bestätigung falsch). Es gelten dieselben Sperren wie in der GUI: System-/Boot-/
Windows-Datenträger nie, interne Platten nur, wenn sie in der GUI freigeschaltet wurden.

    python cli.py drives
    python cli.py plan --action wipe --target E: --fs exFAT --label STICK
    python cli.py format --target E: --fs exFAT --label STICK --confirm-target E: --dry-run
    python cli.py wipe --target 3 --fs NTFS --confirm-target 3

``format``/``wipe`` ohne --dry-run brauchen Administratorrechte; fehlen sie, fragt Windows per
UAC nach (der Vorgang läuft dann in einem versteckten Admin-Prozess, das Ergebnis kommt hierher).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from diskformatter import disks, elevate, ops, ps  # noqa: E402
from diskformatter.settings import Settings, settings_path  # noqa: E402


class CliError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def cmd_drives(a) -> dict:
    st = Settings.load()
    snap = disks.query()
    ds = [d.to_json(st) for d in snap.disks]
    if a.selectable:
        ds = [d for d in ds if d["selectable"]]
    return {"admin": ps.is_admin(), "allow_internal": st.allow_internal,
            "settings_file": str(settings_path()), "disks": ds,
            "other_volumes": [v.to_json() for v in snap.orphans]}


def _plan(a, action: str) -> ops.Plan:
    try:
        return ops.make_plan(disks.query(), Settings.load(), action, a.target, a.fs, a.label or "",
                             bool(getattr(a, "full", False)), getattr(a, "style", None))
    except ops.OpError as e:
        raise CliError(str(e)) from None


def cmd_plan(a) -> dict:
    p = _plan(a, a.action)
    return {"dry_run": True, **p.to_json()}


def _change(a, action: str) -> dict:
    p = _plan(a, action)
    d = p.to_json()
    d["confirm_target_ok"] = disks.confirm_matches(p.kind, p.ident, a.confirm_target)
    if a.dry_run:
        return {"dry_run": True, **d}
    if not p.allowed:
        raise CliError("Gesperrt: " + "; ".join(p.blocked), 2)
    if not a.confirm_target:
        raise CliError(f"--confirm-target fehlt (muss '{p.confirm_text}' sein)", 2)
    if not d["confirm_target_ok"]:
        raise CliError(f"--confirm-target '{a.confirm_target}' passt nicht zum Ziel '{p.confirm_text}'", 2)
    if not ps.is_admin():
        return _run_elevated(a)
    res = ops.execute(action, a.target, a.confirm_target, filesystem=a.fs, label=a.label or "",
                      full=bool(getattr(a, "full", False)), partition_style=getattr(a, "style", None),
                      expect_uid=ops.disk_uid(p.disk) if p.disk else None,
                      verify_samples=0 if getattr(a, "no_verify", False) else None)
    return {"dry_run": False, **res}


def _run_elevated(a) -> dict:
    fd, out = tempfile.mkstemp(prefix="diskformatter_", suffix=".json")
    os.close(fd)
    args = [str(HERE / "cli.py")] + [x for x in sys.argv[1:]] + ["--result-file", out]
    try:
        code = elevate.run_elevated(sys.executable, args, cwd=str(HERE), wait=True, show=0)
    except OSError as e:
        raise CliError(str(e)) from None
    try:
        data = json.loads(Path(out).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        data = {}
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    if "error" in data:
        raise CliError("(Admin-Prozess) " + data["error"], data.get("code", 1))
    if code != 0 or not data:
        raise CliError(f"Admin-Prozess endete mit Exit {code} ohne Ergebnis")
    data["elevated"] = True
    return data


def cmd_format(a) -> dict:
    return _change(a, "format")


def cmd_wipe(a) -> dict:
    return _change(a, "wipe")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli.py", description="DiskFormatter (JSON-Ausgabe)")
    p.add_argument("--result-file", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("drives", help="Datenträger und Volumes mit Sperrstatus")
    s.add_argument("--selectable", action="store_true", help="Nur auswählbare Datenträger")
    s.set_defaults(fn=cmd_drives)

    def common(s, disk_ok: bool):
        s.add_argument("--target", required=True,
                       help="Laufwerksbuchstabe (E:)" + (" oder Datenträgernummer (3)" if disk_ok else ""))
        s.add_argument("--fs", default="NTFS", help="NTFS, exFAT oder FAT32")
        s.add_argument("--label", default="", help="Volumenname")
        s.add_argument("--result-file", help=argparse.SUPPRESS)

    s = sub.add_parser("plan", help="Vorschau einer Aktion (ändert nichts)")
    s.add_argument("--action", required=True, choices=["format", "wipe"])
    common(s, True)
    s.add_argument("--full", action="store_true")
    s.add_argument("--style", choices=["GPT", "MBR"])
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("format", help="Volume formatieren")
    common(s, False)
    s.add_argument("--full", action="store_true", help="Vollständig statt Schnellformatierung")
    s.add_argument("--confirm-target", dest="confirm_target")
    s.add_argument("--dry-run", dest="dry_run", action="store_true")
    s.set_defaults(fn=cmd_format)

    s = sub.add_parser("wipe", help="Mit Nullen füllen (Volume: Buchstabe, ganzer Datenträger: Nummer)")
    common(s, True)
    s.add_argument("--style", choices=["GPT", "MBR"], help="Partitionsstil nach dem Nullen (nur Datenträger)")
    s.add_argument("--no-verify", dest="no_verify", action="store_true", help="Keine Stichprobe")
    s.add_argument("--confirm-target", dest="confirm_target")
    s.add_argument("--dry-run", dest="dry_run", action="store_true")
    s.set_defaults(fn=cmd_wipe)
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    a = build_parser().parse_args(argv)
    try:
        res, code = a.fn(a), 0
    except CliError as e:
        res, code = {"error": str(e), "code": e.code}, e.code
    except (ops.OpError, ps.PSError) as e:
        res, code = {"error": str(e), "code": 1}, 1
    if a.result_file:   # versteckter Admin-Prozess: Ergebnis an den Aufrufer zurückgeben
        Path(a.result_file).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        return code
    if "error" in res and set(res) <= {"error", "code"}:
        print(res["error"], file=sys.stderr)
        return code
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
