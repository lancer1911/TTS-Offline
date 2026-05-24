"""
Lancer1911 TTS Offline v0.5k — 主入口
基于 MLX Qwen3-TTS，支持 docx/txt/md/srt/pdf/epub 等文本格式转语音
"""
import sys, threading, time, urllib.request, queue, os

PORT = 17435

_dialog_request_q: queue.Queue = queue.Queue()


def _dialog_kind(webview_module, name: str):
    file_dialog = getattr(webview_module, "FileDialog", None)
    if file_dialog is not None and hasattr(file_dialog, name):
        return getattr(file_dialog, name)
    legacy_name = f"{name}_DIALOG"
    if hasattr(webview_module, legacy_name):
        return getattr(webview_module, legacy_name)
    # pywebview 版本差异兜底：常见值为 OPEN_DIALOG / SAVE_DIALOG 字符串常量
    return legacy_name


def _cleanup():
    import subprocess
    my = os.getpid()
    try:
        r = subprocess.run(["lsof", "-ti", f":{PORT}"],
                           capture_output=True, text=True)
        for p in r.stdout.strip().split():
            pid = int(p)
            if pid != my:
                subprocess.run(["kill", "-9", str(pid)], check=False)
        time.sleep(0.3)
    except Exception:
        pass


def _start_server(dialog_q):
    import uvicorn
    from server import create_app
    import server as _srv
    _srv._DIALOG_Q = dialog_q
    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning", ws_ping_interval=None)


class FileDialogAPI:
    def __init__(self, window_ref):
        self._win = window_ref

    def set_window(self, win):
        self._win = win

    def save_file(self, suggested: str, content_b64: str = "", is_binary: bool = False) -> dict:
        """兼容旧前端调用：不弹出 pywebview 保存窗口，直接写入默认输出目录。"""
        try:
            import base64, json, re, uuid
            from pathlib import Path
            settings_file = Path.home() / ".tts_offline_settings.json"
            out_dir = Path.home() / "Downloads"
            if settings_file.exists():
                try:
                    settings = json.loads(settings_file.read_text(encoding="utf-8"))
                    if settings.get("output_dir"):
                        out_dir = Path(str(settings["output_dir"])).expanduser()
                except Exception:
                    pass
            out_dir.mkdir(parents=True, exist_ok=True)
            name = os.path.basename(str(suggested or "output.dat")).strip() or "output.dat"
            name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .") or "output.dat"
            dst = out_dir / name
            if dst.exists():
                for i in range(1, 1000):
                    cand = out_dir / f"{dst.stem}_{i}{dst.suffix}"
                    if not cand.exists():
                        dst = cand
                        break
                else:
                    dst = out_dir / f"{dst.stem}_{uuid.uuid4().hex[:8]}{dst.suffix}"
            if content_b64:
                dst.write_bytes(base64.b64decode(content_b64))
            else:
                dst.write_bytes(b"")
            return {"ok": True, "path": str(dst)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _read_file_b64(self, path: str) -> dict:
        """读取本地文件为 base64，供前端继续调用现有上传/导入接口。"""
        import base64
        filename = os.path.basename(path)
        ext = os.path.splitext(filename)[1].lower()
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return {"ok": True, "path": path, "filename": filename, "ext": ext, "b64": b64}

    def _read_file_text(self, path: str) -> dict:
        filename = os.path.basename(path)
        ext = os.path.splitext(filename)[1].lower()
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"ok": True, "path": path, "filename": filename, "ext": ext, "content": content}

    def open_text_file(self) -> dict:
        """打开待转换文本/文档文件：只显示 txt/md/srt/docx/pdf/epub。"""
        try:
            import webview
            win = self._win
            if win is None:
                return {"ok": False, "error": "no window"}
            result = win.create_file_dialog(
                _dialog_kind(webview, "OPEN"),
                file_types=(
                    # pywebview/macOS 对 filter 描述部分较严格，避免使用 “/” 等符号；
                    # 否则会报 “is not a valid file filter”。
                    "Supported files (*.txt;*.md;*.docx;*.srt;*.pdf;*.epub)",
                    "TXT files (*.txt)",
                    "Markdown files (*.md)",
                    "DOCX files (*.docx)",
                    "SRT files (*.srt)",
                    "PDF files (*.pdf)",
                    "EPUB files (*.epub)",
                ),
                allow_multiple=False,
            )
            if not result:
                return {"ok": False, "cancelled": True}
            path = result[0] if isinstance(result, (list, tuple)) else result
            if not path:
                return {"ok": False, "cancelled": True}
            ext = os.path.splitext(str(path))[1].lower()
            if ext not in {".txt", ".md", ".srt", ".docx", ".pdf", ".epub"}:
                return {"ok": False, "error": "不支持的文件类型，请选择 TXT / MD / SRT / DOCX / PDF / EPUB 文件"}
            return self._read_file_b64(path)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_session_file(self) -> dict:
        """打开 .ttso 会话文件；兼容旧 JSON 会话。"""
        try:
            import webview
            win = self._win
            if win is None:
                return {"ok": False, "error": "no window"}
            result = win.create_file_dialog(
                _dialog_kind(webview, "OPEN"),
                file_types=(
                    "TTS clone files (*.ttsc;*.ttscx)",
                    "All files (*)",
                ),
                allow_multiple=False,
            )
            if not result:
                return {"ok": False, "cancelled": True}
            path = result[0] if isinstance(result, (list, tuple)) else result
            if not path:
                return {"ok": False, "cancelled": True}
            ext = os.path.splitext(str(path))[1].lower()
            if ext not in {".ttso", ".json"}:
                return {"ok": False, "error": "请选择 .ttso 或 .json 会话文件"}
            return self._read_file_text(path)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_clone_file(self) -> dict:
        """打开 .ttsc / .ttscx 克隆音色文件。"""
        try:
            import webview
            win = self._win
            if win is None:
                return {"ok": False, "error": "no window"}
            result = win.create_file_dialog(
                _dialog_kind(webview, "OPEN"),
                file_types=(
                    "TTS clone files (*.ttsc;*.ttscx)",
                    "All files (*)",
                ),
                allow_multiple=False,
            )
            if not result:
                return {"ok": False, "cancelled": True}
            path = result[0] if isinstance(result, (list, tuple)) else result
            if not path:
                return {"ok": False, "cancelled": True}
            ext = os.path.splitext(str(path))[1].lower()
            if ext not in {".ttsc", ".ttscx"}:
                return {"ok": False, "error": "请选择 .ttsc 或 .ttscx 文件"}
            return self._read_file_text(path)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_anchor_pack_file(self) -> dict:
        """打开 .ttscx 锚定音色包文件；兼容 JSON 格式。"""
        try:
            import webview
            win = self._win
            if win is None:
                return {"ok": False, "error": "no window"}
            result = win.create_file_dialog(
                _dialog_kind(webview, "OPEN"),
                file_types=(
                    "TTS clone files (*.ttsc;*.ttscx)",
                    "All files (*)",
                ),
                allow_multiple=False,
            )
            if not result:
                return {"ok": False, "cancelled": True}
            path = result[0] if isinstance(result, (list, tuple)) else result
            if not path:
                return {"ok": False, "cancelled": True}
            ext = os.path.splitext(str(path))[1].lower()
            if ext not in {".ttscx", ".json"}:
                return {"ok": False, "error": "请选择 .ttscx 锚定包文件"}
            return self._read_file_text(path)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_anchor_pack_path(self) -> dict:
        """打开 .ttscx 锚定包，只返回路径（不读内容）；供桌面端大文件导入使用。
        前端拿到路径后调 /api/import_anchor_pack_from_path，由服务端直接从磁盘读取，
        避免通过 pywebview JS bridge 传输数百 MB 数据导致卡死。
        """
        try:
            import webview
            win = self._win
            if win is None:
                return {"ok": False, "error": "no window"}
            result = win.create_file_dialog(
                _dialog_kind(webview, "OPEN"),
                file_types=(
                    "TTS clone files (*.ttsc;*.ttscx)",
                    "All files (*)",
                ),
                allow_multiple=False,
            )
            if not result:
                return {"ok": False, "cancelled": True}
            path = result[0] if isinstance(result, (list, tuple)) else result
            if not path:
                return {"ok": False, "cancelled": True}
            ext = os.path.splitext(str(path))[1].lower()
            if ext not in {".ttscx", ".json"}:
                return {"ok": False, "error": "请选择 .ttscx 锚定包文件"}
            return {"ok": True, "path": str(path), "filename": os.path.basename(str(path))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_file(self) -> dict:
        """兼容旧前端调用：等同于 open_text_file。"""
        return self.open_text_file()

    def open_audio_file(self) -> dict:
        """打开声音克隆参考音频：只显示 wav/mp3/m4a/flac/ogg。"""
        try:
            import webview
            win = self._win
            if win is None:
                return {"ok": False, "error": "no window"}
            result = win.create_file_dialog(
                _dialog_kind(webview, "OPEN"),
                file_types=(
                    "WAV 音频 (*.wav)",
                    "MP3 音频 (*.mp3)",
                    "M4A 音频 (*.m4a)",
                    "FLAC 音频 (*.flac)",
                    "OGG 音频 (*.ogg)",
                ),
                allow_multiple=False,
            )
            if not result:
                return {"ok": False, "cancelled": True}
            path = result[0] if isinstance(result, (list, tuple)) else result
            if not path:
                return {"ok": False, "cancelled": True}
            return self._read_file_b64(path)
        except Exception as e:
            return {"ok": False, "error": str(e)}


def main():
    _cleanup()

    threading.Thread(target=_start_server, args=(_dialog_request_q,), daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/ping", timeout=1)
            break
        except Exception:
            time.sleep(0.15)

    if "--browser" in sys.argv:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{PORT}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        import webview

        api = FileDialogAPI(None)

        win = webview.create_window(
            title="Lancer1911 TTS Offline",
            url=f"http://127.0.0.1:{PORT}",
            width=1200, height=820, min_size=(900, 600),
            background_color="#0d0f14", text_select=True,
            js_api=api,
        )
        api.set_window(win)

        webview.start(debug="--debug" in sys.argv, private_mode=False)


if __name__ == "__main__":
    main()
