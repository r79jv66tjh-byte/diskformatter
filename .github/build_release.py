"""Baut die Release-Dateien eines ToolHub-Tools (lokal oder in GitHub Actions).

Liest release.json im Repo:
    {"name": "Tool", "entry": "Tool.pyw", "pyinstaller": ["--collect-all", "paket"]}
Erzeugt in release/:
    <Name>.exe            eigenständiges Programm (PyInstaller, ohne Python lauffähig)
    <Name>-source.zip     Quellcode (für die Installation „mit Python“ inkl. Claude-Schnittstelle)
    manifest.json         Version + SHA-256 aller Dateien (prüft der ToolHub-Store)
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd):
    print(">", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def main():
    with open(os.path.join(ROOT, "release.json"), encoding="utf-8-sig") as f:
        build = json.load(f)
    name = build["name"]
    entry = build.get("entry")
    if not entry:
        sys.exit("release.json: entry fehlt")
    version = os.environ.get("GITHUB_REF_NAME") or (sys.argv[1] if len(sys.argv) > 1 else "dev")

    req = os.path.join(ROOT, "requirements.txt")
    pip = [sys.executable, "-m", "pip", "install", "--quiet", "pyinstaller"]
    if os.path.exists(req):
        pip += ["-r", "requirements.txt"]
    run(pip)

    out = os.path.join(ROOT, "release")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed", "--name", name,
         "--distpath", os.path.join(ROOT, "build", "dist"), "--workpath", os.path.join(ROOT, "build", "work"),
         "--specpath", os.path.join(ROOT, "build"), "--paths", ROOT, *build.get("pyinstaller", []), entry])
    exe = os.path.join(out, f"{name}.exe")
    shutil.copy(os.path.join(ROOT, "build", "dist", f"{name}.exe"), exe)

    src = os.path.join(out, f"{name}-source.zip")
    run(["git", "archive", "--format", "zip", "--prefix", f"{name}/", "-o", src, "HEAD"])

    manifest = {"name": name, "version": version,
                "files": {os.path.basename(p): {"sha256": sha256(p), "size": os.path.getsize(p)}
                          for p in (exe, src)}}
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
