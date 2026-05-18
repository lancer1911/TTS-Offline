"""
Lancer1911 TTS Offline v0.2q — FastAPI 后端
支持: docx / txt / md / srt / pdf / epub 文本提取 → 分段 TTS → WAV/MP3 输出
"""
import asyncio, json, os, re, time, threading, tempfile, queue as _queue, uuid, hashlib, shutil, base64, mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles

try:
    import multipart  # noqa
except ImportError:
    import sys, subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "python-multipart", "-q"])

_DIALOG_Q: Optional[_queue.Queue] = None

# ── 全局状态 ──────────────────────────────────────────────────
class State:
    def __init__(self):
        self.worker           = None
        self.worker_ready     = False
        self._worker_lock     = threading.Lock()
        self.ws_clients: list = []
        self.settings: dict   = {}
        # 当前 job
        self.job_status: str  = "idle"   # idle|loading_model|ready|extracting|synthesizing|done|error
        self.job_id: str      = ""
        self.job_text: str    = ""
        self.job_chunks: list = []
        self.job_output_path: str = ""
        self.job_progress: dict   = {}
        self.job_source_file: str = ""

G = State()
_main_loop: Optional[asyncio.AbstractEventLoop] = None

# ── 默认设置 ──────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    # 模型
    "model_repo":       "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
    # 主界面
    "voice_id":         "serena",
    "speed":            1.0,
    "theme":            "dark",
    # 文本处理
    "chunk_size":       250,
    "skip_brackets":    True,   # 跳过括号内注释
    "normalize_numbers": True,  # 数字转中文（可选）
    # 输出
    "output_format":    "wav",  # wav | mp3
    "output_dir":       str(Path.home() / "Downloads"),
    # 克隆音色列表（保存名称→路径映射）
    "cloned_voices":    {},
    # 高级
    "advanced_params": {
        "temperature":   0.9,
        "top_p":         0.9,
        "top_k":         50,
        "max_tokens":    4096,
        "pitch":         0,
        "silence_gap_ms": 300,   # 段落间静音 ms
        "fade_ms":        10,    # 淡入淡出 ms
    },
    "advanced_presets": [],
    "debug_output":     False,
}

SETTINGS_FILE = Path.home() / ".tts_offline_settings.json"
CLONE_DIR = Path.home() / ".tts_offline_clone_voices"

def _safe_clone_filename(name: str, ext: str = ".wav") -> str:
    base = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "clone")).strip("._") or "clone"
    ext = ext if str(ext).startswith(".") else "." + str(ext or "wav")
    return f"{base}_{uuid.uuid4().hex[:8]}{ext}"

def _persist_clone_audio(src_path: str, name: str, ext: str = ".wav") -> str:
    CLONE_DIR.mkdir(parents=True, exist_ok=True)
    src = Path(src_path)
    suffix = src.suffix or ext or ".wav"
    dst = CLONE_DIR / _safe_clone_filename(name, suffix)
    dst.write_bytes(src.read_bytes())
    return str(dst)

# 旧版本中可能保存的错误 repo 名，自动修正
_LEGACY_WRONG_REPOS = {
    "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
}

def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text())
            if isinstance(saved.get("advanced_params"), dict):
                adv = dict(s.get("advanced_params", {}))
                adv.update(saved["advanced_params"])
                saved["advanced_params"] = adv
            # 修正历史遗留的错误 repo 名
            if saved.get("model_repo") in _LEGACY_WRONG_REPOS:
                saved["model_repo"] = DEFAULT_SETTINGS["model_repo"]
            s.update(saved)
        except Exception:
            pass
    return s


def save_settings(settings: dict) -> None:
    """Persist user settings to ~/.tts_offline_settings.json.

    v0.2p still called save_settings() from WebSocket/API handlers,
    but the helper itself was accidentally missing, causing NameError and
    closing the WebSocket when model voices were returned.
    """
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(SETTINGS_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(settings or {}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)
    except Exception as e:
        print(f"[settings] save failed: {e}", flush=True)

def _get_output_dir() -> Path:
    """返回用户指定输出目录；为空或不可用时退回 Downloads。"""
    out_dir = G.settings.get("output_dir") or DEFAULT_SETTINGS.get("output_dir") or str(Path.home() / "Downloads")
    p = Path(str(out_dir)).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_output_filename(filename: str, default: str = "output.dat") -> str:
    """清理保存文件名，避免路径穿越和非法字符。"""
    name = os.path.basename(str(filename or default)).strip() or default
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = name.strip(" .") or default
    return name


def _unique_output_path(filename: str) -> Path:
    """在输出目录内生成不覆盖已有文件的路径。"""
    out_dir = _get_output_dir()
    safe = _safe_output_filename(filename)
    p = out_dir / safe
    if not p.exists():
        return p
    stem = p.stem
    suffix = p.suffix
    for i in range(1, 1000):
        cand = out_dir / f"{stem}_{i}{suffix}"
        if not cand.exists():
            return cand
    return out_dir / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


# ── WebSocket 广播 ────────────────────────────────────────────
async def _broadcast(msg: dict):
    dead = []
    for ws in list(G.ws_clients):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            G.ws_clients.remove(ws)
        except ValueError:
            pass

def broadcast_sync(msg: dict):
    print(f"[broadcast] type={msg.get('type')} loop={_main_loop is not None}", flush=True)
    if _main_loop and not _main_loop.is_closed():
        asyncio.run_coroutine_threadsafe(_broadcast(msg), _main_loop)
    else:
        print("[broadcast] WARNING: no event loop!", flush=True)


# ── ModelWorker ───────────────────────────────────────────────
from multiprocessing import Queue, Process

class ModelWorker:
    def __init__(self, model_repo: str):
        from model_worker import worker_main
        self.task_q   = Queue(maxsize=4)
        self.result_q = Queue()
        self.proc = Process(
            target=worker_main,
            args=(self.task_q, self.result_q, model_repo),
            daemon=True,
        )
        self.proc.start()
        self._listener = threading.Thread(
            target=self._listen, daemon=True
        )
        self._listener.start()
        self._callbacks: dict = {}
        self._cb_lock = threading.Lock()

    def _listen(self):
        import sys
        print("[listener] started", flush=True)
        while True:
            try:
                msg = self.result_q.get(timeout=5)
            except Exception:
                if not self.proc.is_alive():
                    print("[listener] worker process died, exiting", flush=True)
                    break
                continue
            if msg is None:
                break
            t = msg.get("type", "")
            tid = msg.get("id", "")
            print(f"[listener] got msg type={t} stage={msg.get('stage','')} chunk={msg.get('chunk_idx','')}", flush=True)

            if t == "ready":
                G.worker_ready = msg.get("ok", False)
                broadcast_sync({"type": "model_ready", "ok": G.worker_ready,
                                 "error": msg.get("error", "")})
                if G.worker_ready:
                    _register_saved_clones_to_worker()
            elif t == "progress":
                broadcast_sync({**msg, "type": "tts_progress"})
                G.job_progress = msg
            elif t == "done":
                G.job_status = "done" if msg.get("ok") else "error"
                broadcast_sync({**msg, "type": "tts_done"})
            elif t == "voices":
                with self._cb_lock:
                    cb = self._callbacks.pop(tid, None)
                if cb:
                    cb(msg)
            elif t == "clone_done":
                broadcast_sync({"type": "clone_done", **msg})
            else:
                with self._cb_lock:
                    cb = self._callbacks.pop(tid, None)
                if cb:
                    cb(msg)

    def send(self, task: dict, callback=None):
        tid = task.setdefault("id", str(uuid.uuid4())[:8])
        if callback:
            with self._cb_lock:
                self._callbacks[tid] = callback
        self.task_q.put(task)
        return tid

    def alive(self):
        return self.proc.is_alive()


def _ensure_worker():
    """确保 worker 已启动（懒加载）"""
    with G._worker_lock:
        if G.worker is None or not G.worker.alive():
            repo = G.settings.get("model_repo", DEFAULT_SETTINGS["model_repo"])
            G.worker_ready = False
            G.job_status = "loading_model"
            broadcast_sync({"type": "status", "status": "loading_model",
                             "model": repo})
            G.worker = ModelWorker(repo)


def _register_saved_clones_to_worker():
    """把 settings 中已保存的克隆音色重新注册到当前 worker。

    这样即使用户先克隆/导入，后加载 Base 模型，或者重启程序后再加载模型，
    已保存的 .ttsc/克隆音色仍能在 Base 模型下使用。CustomVoice 模型不支持
    clone，worker 会忽略并返回错误；这里不向前端弹错。
    """
    if not (G.worker and G.worker_ready):
        return
    for name, clone in (G.settings.get("cloned_voices") or {}).items():
        audio_path = clone.get("audio_path", "")
        if not audio_path or not Path(audio_path).exists():
            continue
        try:
            G.worker.send({
                "type": "clone_voice",
                "name": name,
                "audio_path": audio_path,
                "base_voice": clone.get("base_voice", ""),
                "ref_text": clone.get("ref_text", ""),
            })
        except Exception as e:
            print(f"[clone] register saved clone failed: {name}: {e}", flush=True)


# ── 文本提取 ──────────────────────────────────────────────────
def extract_text_from_file(path: str, ext: str) -> str:
    ext = ext.lower().lstrip(".")
    if ext == "txt":
        return Path(path).read_text(encoding="utf-8", errors="replace")
    elif ext == "md":
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        # 去掉 markdown 语法符号
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'\*(.+?)\*', r'\1', text)
        text = re.sub(r'`{1,3}[^`]*`{1,3}', '', text)
        text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
        text = re.sub(r'\[(.+?)\]\(.*?\)', r'\1', text)
        text = re.sub(r'^[-*+]\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
        return text
    elif ext == "srt":
        return _parse_srt(Path(path).read_text(encoding="utf-8", errors="replace"))
    elif ext == "docx":
        return _parse_docx(path)
    elif ext == "pdf":
        return _parse_pdf(path)
    elif ext == "epub":
        return _parse_epub(path)
    else:
        raise RuntimeError("不支持的文件类型。请使用 TXT / MD / SRT / DOCX / PDF / EPUB 文件。")


def _parse_srt(content: str) -> str:
    """提取 SRT 字幕文本（去掉时间戳和序号）"""
    lines = []
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.isdigit():
            continue
        if re.match(r'\d{2}:\d{2}:\d{2}', line):
            continue
        lines.append(line)
    return "\n".join(lines)


def _parse_docx(path: str) -> str:
    try:
        from docx import Document
        doc = Document(path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        raise RuntimeError("请安装 python-docx: pip install python-docx")


def _parse_pdf(path: str) -> str:
    try:
        import pdfminer.high_level
        return pdfminer.high_level.extract_text(path)
    except ImportError:
        pass
    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    except ImportError:
        raise RuntimeError("请安装 pdfminer.six: pip install pdfminer.six")


def _parse_epub(path: str) -> str:
    """提取 EPUB 中的 XHTML/HTML 文本。

    这里不依赖 ebooklib，直接读取 EPUB(zip) 内的 .xhtml/.html/.htm 文件，
    去掉标签后按阅读顺序尽量拼接。对于常见无 DRM EPUB 足够使用；
    加密/DRM EPUB 无法解析。
    """
    import zipfile, html as _html
    from html.parser import HTMLParser

    class _TextExtractor(HTMLParser):
        block_tags = {
            "p", "div", "br", "li", "section", "article", "chapter",
            "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote"
        }
        skip_tags = {"script", "style", "svg", "math", "head", "metadata"}

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
            self._skip = 0

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            if tag in self.skip_tags:
                self._skip += 1
            elif tag in self.block_tags:
                self.parts.append("\n")

        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag in self.skip_tags and self._skip:
                self._skip -= 1
            elif tag in self.block_tags:
                self.parts.append("\n")

        def handle_data(self, data):
            if self._skip:
                return
            data = (data or "").strip()
            if data:
                self.parts.append(data + " ")

        def text(self):
            txt = _html.unescape("".join(self.parts))
            txt = re.sub(r"[ \t\r\f\v]+", " ", txt)
            txt = re.sub(r"\n\s*\n+", "\n\n", txt)
            txt = re.sub(r" *\n *", "\n", txt)
            return txt.strip()

    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
            # 跳过目录/导航文件，正文文件优先；仍保留兜底，避免空结果。
            body_names = [n for n in names if not any(x in n.lower() for x in ("nav", "toc", "cover"))]
            ordered = body_names or names
            chunks = []
            for name in ordered:
                try:
                    raw = z.read(name)
                    text = raw.decode("utf-8", errors="replace")
                    parser = _TextExtractor()
                    parser.feed(text)
                    t = parser.text()
                    if t:
                        chunks.append(t)
                except Exception:
                    continue
            if not chunks:
                raise RuntimeError("未能从 EPUB 中提取文本。加密/DRM 或图片型 EPUB 暂不支持。")
            return "\n\n".join(chunks)
    except zipfile.BadZipFile:
        raise RuntimeError("EPUB 文件格式无效或已损坏。")


# ── FastAPI App ───────────────────────────────────────────────
def _cleanup_ttso_files():
    """启动时清理上次残留的 ttso_* 临时文件。"""
    import glob, tempfile
    pattern = os.path.join(tempfile.gettempdir(), "ttso_*.wav")
    removed = 0
    for f in glob.glob(pattern):
        try:
            os.unlink(f)
            removed += 1
        except Exception:
            pass
    if removed:
        print(f"[startup] cleaned up {removed} leftover ttso_* temp file(s)", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _main_loop
    _main_loop = asyncio.get_event_loop()
    _cleanup_ttso_files()
    G.settings = load_settings()
    # 启动时自动加载上次使用的模型
    threading.Thread(target=_ensure_worker, daemon=True).start()
    yield
    if G.worker:
        try:
            G.worker.task_q.put(None)
        except Exception:
            pass


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    _H1 = hashlib.sha256("Lancer1911".encode()).hexdigest()

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    @app.get("/api/author")
    async def author_token():
        return {"token": _H1}

    @app.get("/", response_class=HTMLResponse)
    async def index():
        html_file = static_dir / "index.html"
        if html_file.exists():
            return HTMLResponse(html_file.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>TTS Offline</h1>")

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        G.ws_clients.append(ws)
        try:
            # 发送初始状态
            await ws.send_json({
                "type": "init",
                "status": G.job_status if G.worker_ready else "loading_model",
                "worker_ready": G.worker_ready,
                "settings": G.settings,
                "model": G.settings.get("model_repo", ""),
            })
            while True:
                try:
                    data = await asyncio.wait_for(ws.receive_text(), timeout=30)
                    msg = json.loads(data)
                    await _handle_ws_msg(msg, ws)
                except asyncio.TimeoutError:
                    await ws.send_json({"type": "ping"})
        except WebSocketDisconnect:
            pass
        finally:
            try:
                G.ws_clients.remove(ws)
            except ValueError:
                pass

    async def _handle_ws_msg(msg: dict, ws: WebSocket):
        t = msg.get("type", "")
        if t == "pong":
            pass
        elif t == "load_model":
            repo = msg.get("repo", G.settings.get("model_repo"))
            G.settings["model_repo"] = repo
            save_settings(G.settings)
            with G._worker_lock:
                G.worker = None
                G.worker_ready = False
            _ensure_worker()
        elif t == "get_voices":
            if G.worker and G.worker_ready:
                evt = threading.Event()
                voices_result = {}
                def cb(r):
                    voices_result.update(r)
                    evt.set()
                G.worker.send({"type": "list_voices"}, callback=cb)
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: evt.wait(5)
                )
                await ws.send_json({"type": "voices",
                                    "voices": voices_result.get("voices", [])})
            else:
                # worker 未就绪时返回静态列表（CustomVoice 官方 9 个音色）
                static_voices = [
                    {"id": "serena",   "name": "Serena",   "lang": "zh/en", "gender": "female", "desc": "温柔知性女声"},
                    {"id": "vivian",   "name": "Vivian",   "lang": "zh/en", "gender": "female", "desc": "俏皮可爱女声"},
                    {"id": "ono_anna", "name": "Ono Anna", "lang": "ja/zh", "gender": "female", "desc": "日系女声"},
                    {"id": "sohee",    "name": "Sohee",    "lang": "ko/zh", "gender": "female", "desc": "韩系女声"},
                    {"id": "ryan",     "name": "Ryan",     "lang": "zh/en", "gender": "male",   "desc": "标准英语男声"},
                    {"id": "aiden",    "name": "Aiden",    "lang": "zh/en", "gender": "male",   "desc": "沉稳男声"},
                    {"id": "eric",     "name": "Eric",     "lang": "zh/en", "gender": "male",   "desc": "活力男声"},
                    {"id": "dylan",    "name": "Dylan",    "lang": "zh/en", "gender": "male",   "desc": "磁性男声"},
                    {"id": "uncle_fu", "name": "Uncle Fu", "lang": "zh",    "gender": "male",   "desc": "成熟男声"},
                ]
                await ws.send_json({"type": "voices", "voices": static_voices})

    @app.get("/api/installed_models")
    async def installed_models():
        """扫描 HF 缓存，返回已下载的 Qwen3-TTS 模型列表"""
        import os
        from pathlib import Path
        cache_dir = Path(os.environ.get("HF_HUB_CACHE",
                         Path.home() / ".cache" / "huggingface" / "hub"))
        known = [
            "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
            "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit",
            "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16",
            "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit",
            "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
            "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit",
            "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
            "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16",
            "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit",
            "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16",
        ]
        installed = []
        for repo in known:
            # HF 缓存目录名：把 / 和 - 转成 --，org/name → models--org--name
            folder = "models--" + repo.replace("/", "--")
            model_dir = cache_dir / folder
            # 认为已安装：目录存在且 snapshots 子目录非空
            if model_dir.exists():
                snapshots = list((model_dir / "snapshots").glob("*")) if (model_dir / "snapshots").exists() else []
                if snapshots:
                    installed.append(repo)
        return JSONResponse({"installed": installed})

    @app.post("/api/open_terminal")
    async def open_terminal_route(req: Request):
        """Open Terminal.app with a command pre-filled (macOS only)."""
        import subprocess, shlex
        body = await req.json()
        cmd = str(body.get("cmd", "")).strip()
        if not cmd:
            return JSONResponse({"ok": False, "error": "no command"})
        try:
            # Escape for AppleScript double-quoted string
            safe = cmd.replace("\\", "\\\\").replace('"', '\\"')
            apple = (
                f'tell application "Terminal"\n'
                f'    activate\n'
                f'    do script "{safe}"\n'
                f'end tell'
            )
            subprocess.Popen(["osascript", "-e", apple])
            return JSONResponse({"ok": True})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})

    @app.get("/api/settings")
    async def get_settings():
        return JSONResponse(G.settings)

    @app.post("/api/settings")
    async def post_settings(req: Request):
        body = await req.json()
        G.settings.update(body)
        save_settings(G.settings)
        # 如果模型 repo 变了，重启 worker
        if "model_repo" in body and G.worker:
            with G._worker_lock:
                G.worker = None
                G.worker_ready = False
        return JSONResponse({"ok": True})

    @app.post("/api/upload_text")
    async def upload_text(req: Request):
        """上传文本文件（JSON base64），提取纯文本返回"""
        import base64
        body = await req.json()
        ext  = body.get("ext", "txt").lower().lstrip(".")
        allowed_exts = {"txt", "md", "srt", "docx", "pdf", "epub"}
        if ext not in allowed_exts:
            return JSONResponse({"ok": False, "error": "不支持的文件类型。请使用 TXT / MD / SRT / DOCX / PDF / EPUB 文件。"}, status_code=400)
        b64  = body.get("b64", "")
        filename = body.get("filename", f"file.{ext}")
        if not b64:
            return JSONResponse({"ok": False, "error": "未收到文件数据"}, status_code=400)
        fd, tmp = tempfile.mkstemp(suffix=f".{ext}")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode(b64))
            text = extract_text_from_file(tmp, ext)
            char_count = len(text)
            preview = text[:200]
            return JSONResponse({
                "ok": True, "text": text,
                "char_count": char_count, "preview": preview,
                "filename": filename,
            })
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    @app.post("/api/synthesize")
    async def synthesize(req: Request):
        """启动 TTS 合成任务"""
        body = await req.json()
        dialog_rows = body.get("dialog_rows") or []
        import sys; print(f"[synthesize] text={len(body.get('text',''))}chars rows={len(dialog_rows) if isinstance(dialog_rows, list) else 0} voice={body.get('voice_id')} worker_ready={G.worker_ready}", flush=True)
        text     = body.get("text", "").strip()
        voice_id = body.get("voice_id", G.settings.get("voice_id", "Chelsie"))
        speed    = float(body.get("speed", G.settings.get("speed", 1.0)))
        advanced = body.get("advanced", G.settings.get("advanced_params", {}))
        chunk_size = 9999  # 由 model_worker 根据语言自动限制，此处传大值不干预

        if isinstance(dialog_rows, list) and dialog_rows:
            cleaned = []
            for row in dialog_rows:
                if not isinstance(row, dict):
                    continue
                r_text = str(row.get("text") or "").strip()
                if not r_text:
                    continue
                cleaned.append({
                    "speaker": str(row.get("speaker") or "").strip(),
                    "instruction": str(row.get("instruction") or "").strip(),
                    "text": r_text,
                })
            dialog_rows = cleaned
            if dialog_rows:
                text = "\n".join(r["text"] for r in dialog_rows)
        else:
            dialog_rows = []

        if not text and not dialog_rows:
            return JSONResponse({"ok": False, "error": "文本为空"}, status_code=400)

        # 确保 worker 在线
        _ensure_worker()
        if not G.worker_ready:
            return JSONResponse({"ok": False, "error": "模型正在加载中，请稍候"},
                                status_code=503)

        # 输出路径
        out_dir = G.settings.get("output_dir", str(Path.home() / "Downloads"))
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = str(Path(out_dir) / f"tts_{ts}.wav")

        G.job_status = "synthesizing"
        G.job_text   = text
        G.job_output_path = out_path

        task = {
            "type":       "tts",
            "text":       text,
            "dialog_rows": dialog_rows,
            "input_mode": body.get("input_mode", "text"),
            "voice_id":   voice_id,
            "speed":      speed,
            "output_path": out_path,
            "chunk_size": 9999,  # 由 model_worker 按语言自动计算
            "advanced":   advanced,
        }
        G.worker.send(task)
        return JSONResponse({"ok": True, "output_path": out_path})

    @app.get("/api/download")
    async def download_audio(path: str):
        """下载/播放生成的音频文件。"""
        p = Path(path).expanduser()
        if not p.exists() or not p.is_file():
            return JSONResponse({"error": "文件不存在"}, status_code=404)
        media_type = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        return FileResponse(str(p), media_type=media_type, filename=p.name)

    @app.get("/api/file_info")
    async def file_info(path: str):
        """返回音频文件元数据，用于写入 .ttso 会话。"""
        p = Path(path).expanduser()
        if not p.exists() or not p.is_file():
            return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)
        st = p.stat()
        return JSONResponse({"ok": True, "info": {
            "path": str(p),
            "filename": p.name,
            "ext": p.suffix.lower(),
            "mime": mimetypes.guess_type(str(p))[0] or "",
            "size": st.st_size,
            "mtime": st.st_mtime,
            "exists": True,
        }})

    @app.post("/api/resolve_audio_file")
    async def resolve_audio_file(req: Request):
        """根据 .ttso 中记录的信息寻找对应 wav/mp3 音频。

        查找顺序：原绝对路径 → 会话文件同目录 → 默认输出目录。
        这样 .ttso 加载后可以自动恢复 playback/follow。
        """
        body = await req.json()
        raw_path = str(body.get("path") or "").strip()
        filename = _safe_output_filename(body.get("filename") or (Path(raw_path).name if raw_path else ""), "")
        session_path = str(body.get("session_path") or "").strip()
        candidates = []
        if raw_path:
            candidates.append(Path(raw_path).expanduser())
        if session_path and filename:
            candidates.append(Path(session_path).expanduser().parent / filename)
        if filename:
            candidates.append(_get_output_dir() / filename)
        seen = set()
        for cand in candidates:
            try:
                key = str(cand.expanduser())
                if key in seen:
                    continue
                seen.add(key)
                if cand.exists() and cand.is_file():
                    st = cand.stat()
                    return JSONResponse({"ok": True, "path": str(cand), "filename": cand.name,
                                         "ext": cand.suffix.lower(),
                                         "mime": mimetypes.guess_type(str(cand))[0] or "",
                                         "size": st.st_size, "mtime": st.st_mtime})
            except Exception:
                continue
        return JSONResponse({"ok": False, "error": "未找到音频文件"}, status_code=404)

    @app.post("/api/save_text_file")
    async def save_text_file(req: Request):
        """将前端生成的文本/JSON/字幕等直接保存到默认输出目录，不弹出 pywebview 保存窗口。"""
        body = await req.json()
        filename = _safe_output_filename(body.get("filename", "output.txt"), "output.txt")
        content = body.get("content")
        content_b64 = body.get("content_b64")
        is_binary = bool(body.get("is_binary", False))
        dst = _unique_output_path(filename)
        try:
            if content_b64 is not None:
                dst.write_bytes(base64.b64decode(content_b64))
            elif is_binary:
                dst.write_bytes(bytes(content or b""))
            else:
                dst.write_text(str(content or ""), encoding="utf-8")
            return JSONResponse({"ok": True, "path": str(dst), "filename": dst.name, "output_dir": str(dst.parent)})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.post("/api/save_existing_file")
    async def save_existing_file(req: Request):
        """将已有文件复制/确认保存到默认输出目录，不触发浏览器下载或 pywebview 对话框。"""
        body = await req.json()
        src = Path(str(body.get("path", ""))).expanduser()
        if not src.exists() or not src.is_file():
            return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)
        filename = body.get("filename") or src.name
        dst = _unique_output_path(filename)
        try:
            # 如果文件已经在输出目录下，直接返回原路径，避免无意义复制。
            try:
                if src.resolve().parent == _get_output_dir().resolve():
                    return JSONResponse({"ok": True, "path": str(src), "filename": src.name, "output_dir": str(src.parent), "already_in_output_dir": True})
            except Exception:
                pass
            shutil.copy2(str(src), str(dst))
            return JSONResponse({"ok": True, "path": str(dst), "filename": dst.name, "output_dir": str(dst.parent)})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.post("/api/clone_voice")
    async def clone_voice(req: Request):
        """注册/保存声音克隆（接收 JSON base64 音频）。

        注意：保存 .ttsc 所需的信息不应依赖模型是否已经加载成功。
        旧版本在 G.worker_ready=False 时直接 503，导致前端已经显示卡片但后端没有保存，
        后续导出 /api/export_clone 自然 404。这里改为：先把参考音频持久化并写入 settings；
        如果 worker 已就绪，则再尝试注册给 worker；如果 worker 未就绪，仍返回 ok，待模型加载
        完成后由 _register_saved_clones_to_worker() 自动注册。
        """
        import base64
        body = await req.json()
        name = str(body.get("name", "我的声音") or "我的声音").strip() or "我的声音"
        base_voice = body.get("base_voice", "Chelsie")
        ref_text = body.get("ref_text", "")
        audio_b64 = body.get("audio_b64", "")
        ext = body.get("ext", ".wav") or ".wav"
        if not str(ext).startswith("."):
            ext = "." + str(ext)

        if not audio_b64:
            return JSONResponse({"ok": False, "error": "未收到音频数据"}, status_code=400)

        fd, tmp = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64decode(audio_b64))

        # 先持久化，保证 .ttsc 导出与删除不依赖 worker/model 状态。
        try:
            saved_audio = _persist_clone_audio(tmp, name, ext)
        except Exception:
            saved_audio = tmp
        G.settings.setdefault("cloned_voices", {})[name] = {
            "audio_path": saved_audio,
            "base_voice": base_voice,
            "ref_text": ref_text,
        }
        save_settings(G.settings)

        result = {"ok": True, "type": "clone_done", "name": name, "voice_id": f"__clone__{name}"}

        # worker 已就绪时，尝试立即注册；未就绪时不报 503。
        if G.worker and G.worker_ready:
            evt = threading.Event()
            worker_result = {}
            def cb(r):
                worker_result.update(r)
                evt.set()
            try:
                G.worker.send({
                    "type": "clone_voice",
                    "name": name,
                    "audio_path": saved_audio,
                    "base_voice": base_voice,
                    "ref_text": ref_text,
                }, callback=cb)
                await asyncio.get_event_loop().run_in_executor(None, lambda: evt.wait(10))
                if worker_result and not worker_result.get("ok"):
                    # CustomVoice 模型等可能不支持 clone。保存仍成功，只提示切换 Base 模型后可用。
                    result["warning"] = worker_result.get("error", "克隆音色已保存，但当前模型未完成注册")
            except Exception as e:
                result["warning"] = f"克隆音色已保存，但当前模型未完成注册：{e}"
        else:
            result["warning"] = "克隆音色已保存；当前模型未就绪，加载 Base 模型后会自动注册。"

        broadcast_sync(result)
        return JSONResponse(result)

    @app.delete("/api/clone_voice")
    async def delete_clone_voice(name: str):
        """删除指定克隆音色"""
        # 从设置文件移除
        removed = G.settings.get("cloned_voices", {}).pop(name, None)
        save_settings(G.settings)
        # 通知 worker 进程清除
        if G.worker and G.worker_ready:
            G.worker.send({"type": "delete_clone", "name": name})
        broadcast_sync({"type": "clone_deleted", "name": name})
        return JSONResponse({"ok": True, "removed": removed is not None})

    @app.post("/api/delete_clone")
    async def delete_clone_voice_post(req: Request):
        """兼容某些 WebView/代理不稳定处理 DELETE 的情况。"""
        data = await req.json()
        name = str(data.get("name", "")).strip()
        if not name:
            return JSONResponse({"ok": False, "error": "缺少克隆音色名称"}, status_code=400)
        removed = G.settings.get("cloned_voices", {}).pop(name, None)
        save_settings(G.settings)
        if G.worker and G.worker_ready:
            G.worker.send({"type": "delete_clone", "name": name})
        broadcast_sync({"type": "clone_deleted", "name": name})
        return JSONResponse({"ok": True, "removed": removed is not None})

    @app.get("/api/export_clone")
    async def export_clone(name: str):
        """导出克隆音色为 base64 JSON，供移植使用"""
        import base64
        clone = G.settings.get("cloned_voices", {}).get(name)
        if not clone:
            return JSONResponse({"ok": False, "error": "未找到该克隆音色"}, status_code=404)
        audio_path = clone.get("audio_path", "")
        audio_b64 = ""
        ext = ".wav"
        if audio_path and Path(audio_path).exists():
            ext = Path(audio_path).suffix or ".wav"
            audio_b64 = base64.b64encode(Path(audio_path).read_bytes()).decode()
        return JSONResponse({
            "ok": True, "name": name,
            "base_voice": clone.get("base_voice", ""),
            "ref_text":   clone.get("ref_text", ""),
            "audio_b64":  audio_b64,
            "ext":        ext,
        })

    @app.get("/api/export_clone_file")
    async def export_clone_file(name: str):
        """直接以附件形式下载 .ttsc，兼容 pywebview/浏览器下载。"""
        import base64
        clone = G.settings.get("cloned_voices", {}).get(name)
        if not clone:
            return JSONResponse({"ok": False, "error": "未找到该克隆音色"}, status_code=404)
        audio_path = clone.get("audio_path", "")
        if not audio_path or not Path(audio_path).exists():
            return JSONResponse({"ok": False, "error": "克隆音色音频文件不存在，可能已被系统清理，请重新导入或重新克隆"}, status_code=404)
        ext = Path(audio_path).suffix or ".wav"
        data = {
            "file_type": "ttsc",
            "version": "0.2",
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "name": name,
            "base_voice": clone.get("base_voice", ""),
            "ref_text": clone.get("ref_text", ""),
            "audio_b64": base64.b64encode(Path(audio_path).read_bytes()).decode(),
            "ext": ext,
        }
        safe = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "clone"
        body = json.dumps(data, indent=2, ensure_ascii=False)
        return Response(
            content=body,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{safe}.ttsc"},
        )

    @app.post("/api/import_clone")
    async def import_clone(req: Request):
        """导入克隆音色（从导出的 JSON 恢复）"""
        import base64
        body = await req.json()
        name      = body.get("name", "导入音色")
        audio_b64 = body.get("audio_b64", "")
        ext       = body.get("ext", ".wav")
        base_voice = body.get("base_voice", "")
        ref_text  = body.get("ref_text", "")
        if not audio_b64:
            return JSONResponse({"ok": False, "error": "无音频数据"}, status_code=400)
        fd, tmp = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64decode(audio_b64))
        try:
            saved_audio = _persist_clone_audio(tmp, name, ext)
        except Exception:
            saved_audio = tmp
        G.settings.setdefault("cloned_voices", {})[name] = {
            "audio_path": saved_audio, "base_voice": base_voice, "ref_text": ref_text
        }
        save_settings(G.settings)
        if G.worker and G.worker_ready:
            G.worker.send({"type": "clone_voice", "name": name,
                           "audio_path": tmp, "base_voice": base_voice,
                           "ref_text": ref_text})
        broadcast_sync({"type": "clone_done", "ok": True, "name": name,
                        "voice_id": f"__clone__{name}"})
        return JSONResponse({"ok": True, "voice_id": f"__clone__{name}"})

    @app.get("/api/status")
    async def status():
        return JSONResponse({
            "status": G.job_status,
            "worker_ready": G.worker_ready,
            "output_path": G.job_output_path,
            "progress": G.job_progress,
        })

    @app.post("/api/stop")
    async def stop_synthesis():
        """中止当前合成（重启 worker）"""
        with G._worker_lock:
            if G.worker:
                try:
                    G.worker.proc.terminate()
                except Exception:
                    pass
            G.worker = None
            G.worker_ready = False
        G.job_status = "idle"
        broadcast_sync({"type": "status", "status": "idle"})
        return JSONResponse({"ok": True})

    return app
