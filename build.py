"""Build dist/KitsuIko.exe: one self-contained file, no Python needed.

    .venv\\Scripts\\python build.py

Steps: write Windows version info from kitsu_iko.__version__, run
PyInstaller, then launch the new exe with --selftest and fail loudly if the
packaged app is missing anything (Qt plugins, icon, keychain, certificates).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from kitsu_iko import __version__  # noqa: E402

NAME = "KitsuIko"
ICON = os.path.join(ROOT, "Iko_ico.ico")
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")


def version_file() -> str:
    parts = [int(p) for p in __version__.split(".")] + [0, 0, 0]
    v = tuple(parts[:4])
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'Kitsu Iko - bulk upload/download for Kitsu'),
      StringStruct('ProductName', 'Kitsu Iko'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('ProductVersion', '{__version__}'),
      StringStruct('OriginalFilename', '{NAME}.exe'),
      StringStruct('InternalName', '{NAME}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    os.makedirs(BUILD, exist_ok=True)
    path = os.path.join(BUILD, "version_info.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def build() -> str:
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile", "--windowed",
        "--name", NAME,
        "--workpath", BUILD, "--distpath", DIST, "--specpath", BUILD,
        "--version-file", version_file(),
        # Qt parts the app doesn't use: smaller exe, faster start.
        *[x for m in ("PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtPdf", "PyQt6.QtNetwork",
                      "PyQt6.QtMultimedia", "PyQt6.QtWebEngineCore", "PyQt6.QtSvg",
                      "PyQt6.QtOpenGL", "tkinter", "unittest.mock")
          for x in ("--exclude-module", m)],
    ]
    if os.path.exists(ICON):
        args += ["--icon", ICON, "--add-data", f"{ICON}{os.pathsep}."]
    args.append(os.path.join(ROOT, "kitsu_iko_main.py"))
    subprocess.run(args, check=True, cwd=ROOT)
    return os.path.join(DIST, f"{NAME}.exe")


def selftest(exe: str) -> bool:
    report = os.path.join(tempfile.mkdtemp(), "selftest.json")
    proc = subprocess.run([exe, "--selftest", report], timeout=180)
    if not os.path.exists(report):
        print(f"SELFTEST FAILED: no report (exit code {proc.returncode})")
        return False
    with open(report, encoding="utf-8") as fh:
        result = json.load(fh)
    print("Selftest:", json.dumps(result, indent=1))
    return bool(result.get("ok"))


if __name__ == "__main__":
    if shutil.which("upx"):
        print("note: UPX found on PATH; PyInstaller may use it (can trigger antivirus false positives)")
    exe = build()
    size_mb = os.path.getsize(exe) / 1e6
    ok = selftest(exe)
    print(f"\n{'OK' if ok else 'PROBLEM'}: {exe}  ({size_mb:.0f} MB, version {__version__})")
    sys.exit(0 if ok else 1)
