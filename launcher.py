"""
launcher.py — Lancer1911 TTS Offline .app entry point for py2app

The .app itself is intentionally lightweight. It finds an external Python
environment that already has TTS dependencies installed, then runs main.py
from the app Resources directory.
"""
import os
import sys
import subprocess
from pathlib import Path

APP_NAME = "Lancer1911 TTS Offline"
LOG_NAME = "TTSOffline.log"


def _quote_for_applescript(s: str) -> str:
    return s.replace('\\', '\\\\').replace('"', '\\"')


def alert(title: str, msg: str) -> None:
    try:
        script = (
            f'display alert "{_quote_for_applescript(title)}" '
            f'message "{_quote_for_applescript(msg)}" as critical'
        )
        subprocess.run(["osascript", "-e", script], check=False)
    except Exception:
        pass


def find_python() -> str | None:
    """Find a Python executable likely to contain the TTS runtime packages."""
    candidates = [
        Path.home() / "tts-env" / "bin" / "python3",
        Path.home() / "tts-offline-env" / "bin" / "python3",
        Path.home() / "tts_offline_env" / "bin" / "python3",
        Path.home() / "tts-env" / "bin" / "python",
    ]

    here = Path(__file__).resolve().parent
    candidates += [
        here / ".venv" / "bin" / "python3",
        here / "venv" / "bin" / "python3",
        here / "venv_build" / "bin" / "python3",
    ]

    pyenv_root = Path.home() / ".pyenv" / "versions"
    if pyenv_root.exists():
        for version_dir in sorted(pyenv_root.iterdir(), reverse=True):
            candidates.append(version_dir / "bin" / "python3")

    candidates += [
        Path("/opt/homebrew/bin/python3"),
        Path("/usr/local/bin/python3"),
        Path("/usr/bin/python3"),
    ]

    for p in candidates:
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


def find_main() -> str | None:
    resource_path = os.environ.get("RESOURCEPATH")
    if resource_path:
        main_py = Path(resource_path) / "main.py"
        if main_py.exists():
            return str(main_py)

    here = Path(__file__).resolve().parent
    main_py = here / "main.py"
    if main_py.exists():
        return str(main_py)
    return None


def request_microphone_permission_at_launch() -> None:
    """Trigger macOS microphone TCC permission dialog for the bundled .app.

    This must run inside the py2app bundle process (launcher.py) — NOT in
    the external python subprocess — because TCC grants permissions per
    bundle identity.  We pump a short NSRunLoop so the AVFoundation callback
    (dispatched on the main-thread run-loop) actually fires and the dialog
    appears.
    """
    if sys.platform != "darwin":
        return
    if os.environ.get("TTS_OFFLINE_SKIP_MIC_PERMISSION") == "1":
        return
    try:
        from AVFoundation import (
            AVCaptureDevice,
            AVMediaTypeAudio,
            AVAuthorizationStatusNotDetermined,
        )
        from AppKit import NSApplication
        from Foundation import NSRunLoop, NSDate

        NSApplication.sharedApplication()  # ensure we're recognised as a GUI app

        status = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
        if status != AVAuthorizationStatusNotDetermined:
            return  # already authorised or denied

        _granted_box = [None]

        def _handler(granted):
            _granted_box[0] = granted

        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVMediaTypeAudio, _handler
        )

        # Pump the run-loop so the system dialog fires and the callback is delivered.
        run_loop = NSRunLoop.mainRunLoop()
        elapsed, deadline, tick = 0.0, 10.0, 0.1
        while _granted_box[0] is None and elapsed < deadline:
            run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(tick))
            elapsed += tick
    except Exception:
        pass


def main() -> None:
    request_microphone_permission_at_launch()

    python = find_python()
    if not python:
        alert(
            f"{APP_NAME} - Missing Python environment",
            "Cannot find a usable Python environment.\n\n"
            "Recommended setup:\n"
            "python3 -m venv ~/tts-env\n"
            "source ~/tts-env/bin/activate\n"
            "pip install -r requirements.txt\n\n"
            "Then launch the app again."
        )
        sys.exit(1)

    main_py = find_main()
    if not main_py:
        alert(f"{APP_NAME} - File missing", "Cannot find main.py. Please rebuild or reinstall the app.")
        sys.exit(1)

    work_dir = str(Path(main_py).parent)
    env = os.environ.copy()
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["HF_DATASETS_OFFLINE"] = "1"
    # Permission was already requested by the bundle process above.
    # The subprocess has no bundle identity and its AVFoundation request
    # would be silently refused by TCC — skip it entirely.
    env["TTS_OFFLINE_SKIP_MIC_PERMISSION"] = "1"
    for key in [
        "PYTHONPATH", "PYTHONHOME", "PYTHONEXECUTABLE",
        "RESOURCEPATH", "EXECUTABLEPATH", "ARGVZERO",
    ]:
        env.pop(key, None)

    path_parts = [
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
        str(Path.home() / ".local" / "bin"),
    ]
    old_path = env.get("PATH", "")
    env["PATH"] = ":".join(path_parts + ([old_path] if old_path else []))

    log_path = Path.home() / "Library" / "Logs" / LOG_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n=== {APP_NAME} launch ===\n")
        log.write(f"Python: {python}\n")
        log.write(f"main.py: {main_py}\n")
        log.flush()
        proc = subprocess.run(
            [python, main_py],
            cwd=work_dir,
            env=env,
            stdout=log,
            stderr=log,
        )
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
