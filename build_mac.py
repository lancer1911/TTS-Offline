"""
Build Lancer1911 TTS Offline as a lightweight macOS .app using py2app.

Recommended build steps:
  python3 -m venv ~/tts-offline-build-env
  source ~/tts-offline-build-env/bin/activate
  pip install --upgrade pip setuptools wheel py2app
  cd /path/to/tts_offline
  python build_mac.py py2app

Output:
  ~/Playground/tts_offline/dist/Lancer1911 TTS Offline.app

Runtime note:
  The generated .app is intentionally lightweight. It launches main.py with an
  external Python environment, preferably ~/tts-env, so MLX / TTS dependencies
  do not need to be bundled into the app.
"""
import sys
from pathlib import Path
from setuptools import setup

APP_NAME = "Lancer1911 TTS Offline"
VERSION = "0.5a"
BUNDLE_ID = "com.lancer1911.ttsoffline"

ROOT = Path(__file__).resolve().parent
OUT = Path.home() / "Playground" / "tts_offline"
OUT.mkdir(parents=True, exist_ok=True)

dist_dir = str(OUT / "dist")
if "py2app" in sys.argv and "--dist-dir" not in sys.argv:
    sys.argv += ["--dist-dir", dist_dir]

static_files = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "static").glob("*"))
root_files = [
    "main.py",
    "server.py",
    "model_worker.py",
    "requirements.txt",
]
for optional in ["README.md", "README-ZH.md", "安装与使用说明.md", "打包说明.md"]:
    if (ROOT / optional).exists():
        root_files.append(optional)

py2app_options = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
        "LSMinimumSystemVersion": "13.0",
        "NSLocalNetworkUsageDescription": "The app runs a local server for communication between the desktop window and the local TTS backend.",
        "NSDocumentsFolderUsageDescription": "The app lets you open text/audio files and save generated audio, subtitles, and clone voice packages selected by you.",
        "NSDownloadsFolderUsageDescription": "The app may save exported audio, subtitles, TTSC clone packages, or debug files to Downloads when requested.",
    },
    "packages": ["encodings"],
    "includes": [
        "encodings", "encodings.utf_8", "encodings.ascii", "encodings.latin_1",
        "os", "sys", "subprocess", "pathlib", "json", "time",
    ],
    "excludes": [
        "tkinter", "matplotlib", "test", "unittest",
        "PyQt5", "PyQt6", "wx", "PIL",
        "mlx", "mlx_audio", "torch", "numpy", "scipy",
        "fastapi", "uvicorn", "starlette", "webview", "pywebview",
    ],
    "semi_standalone": False,
    "strip": True,
}

icon_path = ROOT / "icon.icns"
if icon_path.exists():
    py2app_options["iconfile"] = str(icon_path)

setup(
    app=["launcher.py"],
    name=APP_NAME,
    data_files=[
        ("static", static_files),
        ("", root_files),
    ],
    options={"py2app": py2app_options},
    setup_requires=["py2app"],
)
