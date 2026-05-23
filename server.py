"""
Lancer1911 TTS Offline v0.5a — FastAPI 后端
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
        "temperature":   0.20,   # 默认：稳定自然（与 preset_stable_bal 一致）
        "top_p":         0.85,
        "top_k":         20,
        "max_tokens":    4096,
        "pitch":         0,
        "silence_gap_ms": 400,   # 段落间静音 ms（有声书默认值）
        "fade_ms":        10,    # 淡入淡出 ms
        "chunk_size":    100,    # 中文分段上限（字）
    },
    "advanced_presets": [],
    "debug_output":     False,
}

SETTINGS_FILE = Path.home() / ".tts_offline_settings.json"
CLONE_DIR     = Path.home() / ".tts_offline_clone_voices"
CLONE_INIT_FILE = CLONE_DIR / "init.json"

def _safe_clone_filename(name: str, ext: str = ".wav") -> str:
    base = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "clone")).strip("._") or "clone"
    ext = ext if str(ext).startswith(".") else "." + str(ext or "wav")
    return f"{base}_{uuid.uuid4().hex[:8]}{ext}"

def _persist_clone_audio(src_path: str, name: str, ext: str = ".wav") -> str:
    """将克隆参考音频持久化到 CLONE_DIR。

    如果同名音色在 settings 中已有有效的 audio_path（文件存在且位于
    CLONE_DIR 内），则原地覆盖该文件，避免每次加载 .ttsc/.ttscx 都生成
    带随机 UUID 的新文件、造成磁盘堆积。
    只有首次保存或文件丢失时才创建新文件名。
    """
    CLONE_DIR.mkdir(parents=True, exist_ok=True)
    src = Path(src_path)
    suffix = src.suffix or ext or ".wav"

    # 尝试复用 settings 中已记录的路径
    existing_meta = (G.settings.get("cloned_voices") or {}).get(name)
    if isinstance(existing_meta, dict):
        existing_path = existing_meta.get("audio_path", "")
        if existing_path:
            ep = Path(existing_path)
            try:
                # 只复用位于 CLONE_DIR 内的文件（防止复用用户自定义路径）
                if CLONE_DIR in ep.resolve().parents or ep.resolve().parent == CLONE_DIR.resolve():
                    ep.write_bytes(src.read_bytes())
                    print(f"[clone] reused existing audio file for '{name}': {ep}", flush=True)
                    return str(ep)
            except Exception as e:
                print(f"[clone] could not reuse {existing_path}: {e}, creating new file", flush=True)

    # 首次保存或文件丢失：创建新文件名
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
    """Persist user settings to ~/.tts_offline_settings.json."""
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(SETTINGS_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(settings or {}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)
    except Exception as e:
        print(f"[settings] save failed: {e}", flush=True)

def _sync_clone_init(settings: dict) -> None:
    """把 settings['cloned_voices'] 同步写入 CLONE_DIR/init.json。

    以 settings 为准直接写入，删除操作会正确反映到 init.json。
    启动时由 _restore_from_clone_init() 负责从 init.json 补全 settings，
    两者分工明确，不在写入时做合并。
    """
    try:
        CLONE_DIR.mkdir(parents=True, exist_ok=True)
        all_cv = settings.get("cloned_voices") or {}
        cloned  = {k: v for k, v in all_cv.items()
                   if isinstance(v, dict) and v.get("audio_path") and not v.get("anchor")}
        anchors = {k: v for k, v in all_cv.items()
                   if isinstance(v, dict) and v.get("audio_path") and v.get("anchor")}
        payload = {"cloned_voices": cloned, "anchor_voices": anchors}
        tmp = CLONE_INIT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CLONE_INIT_FILE)
    except Exception as e:
        print(f"[clone_init] sync failed: {e}", flush=True)


def load_clone_init() -> dict:
    """从 CLONE_DIR/init.json 读取克隆/锚定音色记录。

    返回格式与 _sync_clone_init 写入的一致：
    {"cloned_voices": {...}, "anchor_voices": {...}}
    文件不存在或损坏时返回空字典，不影响启动。
    """
    try:
        if CLONE_INIT_FILE.exists():
            data = json.loads(CLONE_INIT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[clone_init] load failed: {e}", flush=True)
    return {"cloned_voices": {}, "anchor_voices": {}}


def _restore_from_clone_init() -> None:
    """启动时把 init.json 中的克隆/锚定音色合并回 G.settings['cloned_voices']。

    init.json 采用只增不减策略，即使 settings.json 中的 cloned_voices 因意外
    被清空，也能从 init.json 恢复。只补全缺失的条目，不覆盖 settings 中已有的值。
    合并完成后把 settings 持久化，保证两份文件同步。
    """
    ci = load_clone_init()
    all_from_init = {}
    all_from_init.update(ci.get("cloned_voices") or {})
    all_from_init.update(ci.get("anchor_voices") or {})
    if not all_from_init:
        return
    cv = G.settings.setdefault("cloned_voices", {})
    restored = 0
    for name, meta in all_from_init.items():
        if not isinstance(meta, dict) or not meta.get("audio_path"):
            continue
        if not Path(meta["audio_path"]).exists():
            print(f"[clone_init] restore skip '{name}': audio file missing", flush=True)
            continue
        if name not in cv:
            cv[name] = meta
            restored += 1
        elif not cv[name].get("audio_path") and meta.get("audio_path"):
            cv[name]["audio_path"] = meta["audio_path"]
            restored += 1
    if restored:
        print(f"[clone_init] restored {restored} entries from init.json into settings", flush=True)
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = SETTINGS_FILE.with_suffix(SETTINGS_FILE.suffix + ".tmp")
            tmp.write_text(json.dumps(G.settings, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(SETTINGS_FILE)
        except Exception as e:
            print(f"[clone_init] failed to persist restored settings: {e}", flush=True)


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
                                 "error": msg.get("error", ""),
                                 "clone_init": load_clone_init()})
                if G.worker_ready:
                    _register_saved_clones_to_worker()
            elif t == "progress":
                G.job_progress = {**msg, "type": "tts_progress"}
                broadcast_sync({**msg, "type": "tts_progress"})
            elif t == "done":
                if msg.get("ok") and msg.get("output_path"):
                    op = str(msg.get("output_path") or "")
                    try:
                        exists = bool(op) and Path(op).expanduser().exists() and Path(op).expanduser().is_file() and Path(op).expanduser().stat().st_size > 44
                    except Exception:
                        exists = False
                    if exists:
                        G.job_status = "done"
                        G.job_output_path = op
                        broadcast_sync({**msg, "type": "tts_done", "output_path": op})
                    else:
                        G.job_status = "error"
                        G.job_output_path = ""
                        err = f"Output audio was reported as done but file is missing or empty: {op}"
                        print(f"[listener] {err}", flush=True)
                        broadcast_sync({**msg, "type": "tts_done", "ok": False, "error": err, "output_path": ""})
                else:
                    G.job_status = "error"
                    G.job_output_path = ""
                    broadcast_sync({**msg, "type": "tts_done", "ok": False})
            elif t == "voices":
                with self._cb_lock:
                    cb = self._callbacks.pop(tid, None)
                if cb:
                    cb(msg)
            elif t == "clone_done":
                with self._cb_lock:
                    cb = self._callbacks.pop(tid, None)
                if cb:
                    cb(msg)
                else:
                    broadcast_sync({"type": "clone_done", **msg})
            elif t == "anchor_done":
                with self._cb_lock:
                    cb = self._callbacks.pop(tid, None)
                if cb:
                    cb(msg)
                else:
                    broadcast_sync({"type": "anchor_done", **msg})
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
    # Re-registering saved clone/anchor voices may involve dozens or hundreds of
    # entries (for example 9 speakers × 12 emotions).  These are internal restore
    # operations, not user-facing clone completions.  Consume their worker replies
    # with callbacks so they do NOT broadcast clone_done messages, otherwise the
    # browser receives a storm of clone_done/get_voices/voices messages and the
    # synthesis progress UI can be starved at 2/142 even though the worker keeps
    # generating audio normally.
    entries = list((G.settings.get("cloned_voices") or {}).items())
    print(f"[clone] restoring {len(entries)} saved clone/anchor voices to worker", flush=True)
    for name, clone in entries:
        audio_path = clone.get("audio_path", "")
        if not audio_path:
            print(f"[clone] skip '{name}': no audio_path in settings", flush=True)
            continue
        if not Path(audio_path).exists():
            # 音频文件丢失（可能被系统清理或路径变化）；打印警告但不崩溃。
            # 前端仍可看到卡片，但合成时会报错提示用户重新生成。
            print(f"[clone] skip '{name}': audio file missing at {audio_path}", flush=True)
            continue
        try:
            def _silent_cb(msg, _name=name):
                if not msg.get("ok"):
                    print(f"[clone] silent restore skipped: {_name}: {msg.get('error','')}", flush=True)
                else:
                    print(f"[clone] restored: {_name}", flush=True)
            G.worker.send({
                "type": "clone_voice",
                "name": name,
                "audio_path": audio_path,
            }, callback=_silent_cb)
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
    # 启动时将 init.json 里的克隆/锚定音色合并回 settings，
    # 防止 settings.json 中的 cloned_voices 因意外丢失而造成数据丢失。
    # init.json 是独立的、只增不减的持久化文件，以它为准来补全 settings。
    _restore_from_clone_init()
    # 启动时立即从 settings 生成/更新 init.json
    _sync_clone_init(G.settings)
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
            # 发送初始状态，包含独立的克隆/锚定音色记录（不依赖 worker 注册完成）
            clone_init = load_clone_init()
            await ws.send_json({
                "type": "init",
                "status": G.job_status if G.worker_ready else "loading_model",
                "worker_ready": G.worker_ready,
                "settings": G.settings,
                "model": G.settings.get("model_repo", ""),
                "clone_init": clone_init,
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
                voices = voices_result.get("voices", [])
                # 补全：把 settings 里已持久化的克隆/锚定音色合并进列表。
                # worker 的 _cloned_voices 可能因为 register 尚未完成（竞态窗口）而缺少部分条目；
                # 直接从 settings 读取可保证切换模型后克隆/锚定音色立即显示，不随模型切换消失。
                existing_ids = {v["id"] for v in voices}
                for clone_name, clone_meta in (G.settings.get("cloned_voices") or {}).items():
                    cid = f"__clone__{clone_name}"
                    if cid not in existing_ids and clone_meta.get("audio_path"):
                        voices.append({
                            "id": cid, "name": clone_name,
                            "lang": "any", "gender": "clone",
                            "desc": "声音克隆（Base 模型）",
                        })
                        existing_ids.add(cid)
                await ws.send_json({"type": "voices", "voices": voices})
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
        # 前端的 cloned_voices 只用于 UI 展示，可能不包含 audio_path。
        # 直接覆盖会导致已批量锚定的内部音频路径丢失，下一次启动无法默认调入。
        # 因此这里做保护性合并：保留服务端已有的持久化字段，只更新前端传来的显示 metadata。
        if isinstance(body.get("cloned_voices"), dict):
            merged = dict(G.settings.get("cloned_voices") or {})
            for name, incoming in (body.get("cloned_voices") or {}).items():
                if isinstance(incoming, dict):
                    old_meta = dict(merged.get(name) or {})
                    # 保护服务端持久化字段：前端传来的空值不得覆盖服务端已存的路径。
                    # audio_path 是锚定音色持久化的唯一依据，前端 cloned_voices 通常
                    # 不携带该字段（只含 UI 元数据），如果直接 update 会把路径清空，
                    # 导致重启后 _register_saved_clones_to_worker 跳过这些条目。
                    for _server_only_key in ("audio_path",):
                        if not incoming.get(_server_only_key) and old_meta.get(_server_only_key):
                            incoming = {k: v for k, v in incoming.items() if k != _server_only_key}
                    old_meta.update(incoming)
                    merged[name] = old_meta
                else:
                    merged[name] = incoming
            body["cloned_voices"] = merged
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
                    "person": str(row.get("person") or "").strip(),
                    "voice": str(row.get("voice") or "").strip(),
                    "emotion": str(row.get("emotion") or "").strip(),
                    "emotion_label": str(row.get("emotion_label") or "").strip(),
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

        # 合成前确保所有已保存的克隆音色已注册到 worker（防止模型刚加载完的竞态窗口）
        _register_saved_clones_to_worker()

        # Base 模型 dialog 模式：将不认识的 speaker 名自动替换为第一个可用克隆音色
        model_repo = G.settings.get("model_repo", "")
        is_base = ("base" in model_repo.lower() and "customvoice" not in model_repo.lower())
        if is_base and dialog_rows:
            known_clones = list(G.settings.get("cloned_voices", {}).keys())
            if known_clones:
                fallback = known_clones[0]
                for r in dialog_rows:
                    if r["speaker"] not in known_clones:
                        print(f"[synthesize] Base dialog: unknown speaker '{r['speaker']}' → fallback to '{fallback}'", flush=True)
                        r["speaker"] = fallback
            else:
                return JSONResponse({
                    "ok": False,
                    "error": (
                        "No cloned voices available. Please clone a voice in the Clone tab first.\n"
                        "（没有可用的克隆音色，请先在「克隆」标签页完成克隆。）"
                    )
                }, status_code=400)

        # 输出路径
        out_dir = G.settings.get("output_dir", str(Path.home() / "Downloads"))
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = str(Path(out_dir) / f"tts_{ts}.wav")

        G.job_status = "synthesizing"
        G.job_text   = text
        G.job_output_path = ""
        G.job_progress = {}   # 留空直到 worker 确认文件已写出

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
        """下载/播放生成的音频文件。

        若文件尚不存在（合并写入未完成的竞态窗口），最多等待 4 秒再判定 404，
        避免轮询/WS 消息略早于文件落盘时出现的误报 404。
        """
        import asyncio
        p = Path(path).expanduser()
        if not p.exists() or not p.is_file():
            # 短暂轮询等待文件落盘（最多 4 秒，每 200ms 检查一次）
            for _ in range(20):
                await asyncio.sleep(0.2)
                if p.exists() and p.is_file() and p.stat().st_size > 44:
                    break
            else:
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


    @app.post("/api/create_role_anchor")
    async def create_role_anchor(req: Request):
        """Create a role anchor from the currently loaded CustomVoice/VoiceDesign model.

        The worker synthesizes a short reference WAV from the selected built-in voice
        and instruction. The server then persists that WAV as a cloned voice entry.
        Use the saved anchor after switching to a Base model for more stable multi-line
        synthesis.
        """
        body = await req.json()
        name = str(body.get("name") or "角色锚点").strip() or "角色锚点"
        speaker = str(body.get("speaker") or G.settings.get("voice_id") or "serena").strip()
        instruction = str(body.get("instruction") or "平静自然，语速适中，发音清晰，保持同一角色音色，不夸张表演。").strip()
        emotion = str(body.get("emotion") or "neutral").strip()
        emotion_label = str(body.get("emotion_label") or emotion).strip()
        sample_text = str(body.get("sample_text") or "你好，这是用于固定角色音色的参考样本。请保持平稳、自然、清晰的表达。").strip()
        speed = float(body.get("speed", G.settings.get("speed", 1.0)) or 1.0)
        advanced = dict(body.get("advanced") or G.settings.get("advanced_params", {}) or {})
        # Anchor defaults: conservative unless user explicitly overrides.
        advanced.setdefault("temperature", 0.2)
        advanced.setdefault("top_p", 0.85)
        advanced.setdefault("top_k", 20)

        _ensure_worker()
        if not G.worker_ready:
            return JSONResponse({"ok": False, "error": "模型正在加载中，请稍候"}, status_code=503)

        evt = threading.Event()
        worker_result = {}
        def cb(r):
            worker_result.update(r or {})
            evt.set()
        try:
            G.worker.send({
                "type": "create_anchor",
                "name": name,
                "speaker": speaker,
                "instruction": instruction,
                "sample_text": sample_text,
                "speed": speed,
                "advanced": advanced,
                "emotion": emotion,
                "emotion_label": emotion_label,
            }, callback=cb)
            await asyncio.get_event_loop().run_in_executor(None, lambda: evt.wait(120))
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

        if not worker_result:
            return JSONResponse({"ok": False, "error": "创建角色锚点超时"}, status_code=504)
        if not worker_result.get("ok"):
            return JSONResponse({"ok": False, "error": worker_result.get("error", "创建角色锚点失败")}, status_code=400)

        anchor_audio = worker_result.get("audio_path", "")
        if not anchor_audio or not Path(anchor_audio).exists():
            return JSONResponse({"ok": False, "error": "未生成角色锚点音频"}, status_code=500)
        saved_audio = ""
        try:
            saved_audio = _persist_clone_audio(anchor_audio, name, ".wav")
        finally:
            # worker 生成的临时音频已持久化（或失败），无论如何清理原始临时文件
            if anchor_audio != saved_audio:  # 持久化成功时两者不同，失败时 saved_audio=""
                try:
                    os.unlink(anchor_audio)
                except Exception:
                    pass
        if not saved_audio:
            return JSONResponse({"ok": False, "error": "角色锚点音频持久化失败"}, status_code=500)

        anchor_meta = {
            "audio_path": saved_audio,
            "anchor": True,
            "source_speaker": speaker,
            "instruction": instruction,
            "emotion": emotion,
            "emotion_label": emotion_label,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        G.settings.setdefault("cloned_voices", {})[name] = anchor_meta
        save_settings(G.settings)
        _sync_clone_init(G.settings)
        result = {
            "ok": True, "type": "clone_done", "anchor": True,
            "name": name, "voice_id": f"__clone__{name}",
            "audio_path": saved_audio,   # 让前端 _settings.cloned_voices 能存住路径，防止 saveBasicSettings 时丢失
            "source_speaker": speaker,
            "emotion": emotion,
            "emotion_label": emotion_label,
            "instruction": instruction,
            "meta": anchor_meta,
            "warning": "角色锚点已保存；在 CustomVoice 模式下会立即显示锚定卡片，切换到 Base 模型后可用于合成长文本。"
        }
        broadcast_sync(result)
        return JSONResponse(result)

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
        finally:
            # 持久化成功后清理上传临时文件，避免内存/磁盘泄漏。
            # 若 _persist_clone_audio 将 tmp 直接作为 saved_audio 返回（异常路径），则不删除。
            if saved_audio != tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        G.settings.setdefault("cloned_voices", {})[name] = {
            "audio_path": saved_audio,
        }
        save_settings(G.settings)
        _sync_clone_init(G.settings)

        result = {"ok": True, "type": "clone_done", "name": name, "voice_id": f"__clone__{name}",
                  "audio_path": saved_audio}   # 让前端能存住路径，避免 saveBasicSettings 时被清空

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
        _sync_clone_init(G.settings)
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
        _sync_clone_init(G.settings)
        if G.worker and G.worker_ready:
            G.worker.send({"type": "delete_clone", "name": name})
        broadcast_sync({"type": "clone_deleted", "name": name})
        return JSONResponse({"ok": True, "removed": removed is not None})

    @app.post("/api/clear_anchor_voices")
    async def clear_anchor_voices(req: Request):
        """一键清空所有角色锚定音色。

        只删除 metadata 中 anchor=True 的条目；普通 Base 克隆音色保留。
        同时删除内部保存的参考音频文件，并通知 worker 移除已注册的 clone prompt。
        """
        cloned = G.settings.setdefault("cloned_voices", {})
        names = [name for name, meta in list(cloned.items()) if isinstance(meta, dict) and bool(meta.get("anchor"))]
        removed = []
        for name in names:
            meta = cloned.pop(name, None) or {}
            audio_path = meta.get("audio_path", "") if isinstance(meta, dict) else ""
            if audio_path:
                try:
                    ap = Path(audio_path)
                    # 只清理由本程序内部 clone 目录保存的文件，避免误删用户原始音频。
                    if ap.exists() and CLONE_DIR in ap.resolve().parents:
                        ap.unlink()
                except Exception as e:
                    print(f"[anchor] failed to remove audio {audio_path}: {e}", flush=True)
            removed.append(name)
            if G.worker and G.worker_ready:
                try:
                    G.worker.send({"type": "delete_clone", "name": name})
                except Exception:
                    pass
        save_settings(G.settings)
        _sync_clone_init(G.settings)
        broadcast_sync({"type": "anchors_cleared", "names": removed, "count": len(removed)})
        return JSONResponse({"ok": True, "names": removed, "count": len(removed)})

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
            "audio_b64":  audio_b64,
            "ext":        ext,
            "anchor":     bool(clone.get("anchor")),
            "source_speaker": clone.get("source_speaker", ""),
            "instruction": clone.get("instruction", ""),
        })

    @app.get("/api/clone_audio")
    async def clone_audio(name: str):
        """在线试听已保存的克隆/锚定参考音频。"""
        clone = G.settings.get("cloned_voices", {}).get(name)
        if not clone:
            return JSONResponse({"ok": False, "error": "未找到该克隆音色"}, status_code=404)
        audio_path = clone.get("audio_path", "")
        if not audio_path or not Path(audio_path).exists():
            return JSONResponse({"ok": False, "error": "音色音频文件不存在，可能已被系统清理，请重新导入或重新生成"}, status_code=404)
        media_type = mimetypes.guess_type(str(audio_path))[0] or "audio/wav"
        return FileResponse(str(audio_path), media_type=media_type, filename=Path(audio_path).name)

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
            "anchor": bool(clone.get("anchor")),
            "source_speaker": clone.get("source_speaker", ""),
            "instruction": clone.get("instruction", ""),
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
        is_anchor = bool(body.get("anchor"))
        source_speaker = body.get("source_speaker", "")
        instruction = body.get("instruction", "")
        if not audio_b64:
            return JSONResponse({"ok": False, "error": "无音频数据"}, status_code=400)
        fd, tmp = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64decode(audio_b64))
        try:
            saved_audio = _persist_clone_audio(tmp, name, ext)
        except Exception:
            saved_audio = tmp
        finally:
            # 持久化成功后清理导入临时文件，避免磁盘泄漏。
            if saved_audio != tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        G.settings.setdefault("cloned_voices", {})[name] = {
            "audio_path": saved_audio,
            "anchor": is_anchor,
            "source_speaker": source_speaker,
            "instruction": instruction,
        }
        save_settings(G.settings)
        _sync_clone_init(G.settings)
        if G.worker and G.worker_ready:
            G.worker.send({"type": "clone_voice", "name": name,
                           "audio_path": saved_audio})
        broadcast_sync({"type": "clone_done", "ok": True, "name": name,
                        "anchor": is_anchor, "source_speaker": source_speaker,
                        "audio_path": saved_audio,   # 让前端能存住路径
                        "voice_id": f"__clone__{name}"})
        return JSONResponse({"ok": True, "voice_id": f"__clone__{name}"})

    @app.get("/api/export_anchor_pack")
    async def export_anchor_pack():
        """把所有 anchor=True 的克隆音色打包为 .ttscx（JSON，包含每个锚点的音频 base64）。
        用于一次性保存 108 个锚点组合，下次在 Base 模型下一键导入恢复。
        """
        import base64, zipfile, io
        cloned = G.settings.get("cloned_voices") or {}
        anchors = {name: meta for name, meta in cloned.items()
                   if isinstance(meta, dict) and meta.get("anchor")}
        if not anchors:
            return JSONResponse({"ok": False, "error": "没有已锚定的音色可导出"}, status_code=404)

        entries = []
        missing = []
        for name, meta in anchors.items():
            audio_path = meta.get("audio_path", "")
            if not audio_path or not Path(audio_path).exists():
                missing.append(name)
                continue
            ext = Path(audio_path).suffix or ".wav"
            audio_b64 = base64.b64encode(Path(audio_path).read_bytes()).decode()
            entries.append({
                "name": name,
                "anchor": True,
                "source_speaker": meta.get("source_speaker", ""),
                "emotion": meta.get("emotion", ""),
                "emotion_label": meta.get("emotion_label", ""),
                "instruction": meta.get("instruction", ""),
                "created_at": meta.get("created_at", ""),
                "audio_b64": audio_b64,
                "ext": ext,
            })

        pack = {
            "file_type": "ttscx",
            "version": "1.0",
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "count": len(entries),
            "missing": missing,
            "entries": entries,
        }
        body = json.dumps(pack, ensure_ascii=False, separators=(",", ":"))
        ts = time.strftime("%Y%m%d_%H%M%S")
        return Response(
            content=body,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=\"anchor_pack_{ts}.ttscx\""},
        )

    @app.post("/api/import_anchor_pack_from_path")
    async def import_anchor_pack_from_path(req: Request):
        """从本地磁盘路径直接读取 .ttscx 并导入，供 pywebview 桌面端使用。
        避免通过 JS bridge 传输大文件内容（可达数百 MB）导致 bridge 卡死或超时。
        """
        body = await req.json()
        file_path = str(body.get("path", "")).strip()
        if not file_path:
            return JSONResponse({"ok": False, "error": "未提供路径"}, status_code=400)
        p = Path(file_path).expanduser()
        if not p.exists() or not p.is_file():
            return JSONResponse({"ok": False, "error": f"文件不存在: {file_path}"}, status_code=404)
        try:
            pack = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"读取失败: {e}"}, status_code=400)
        return await _do_import_anchor_pack(pack)

    @app.post("/api/import_anchor_pack")
    async def import_anchor_pack(req: Request):
        body = await req.json()
        return await _do_import_anchor_pack(body)

    async def _do_import_anchor_pack(body: dict):
        """从 .ttscx 批量导入锚定音色包。已存在同名条目时跳过（不覆盖）。"""
        import base64 as _b64
        if body.get("file_type") != "ttscx":
            return JSONResponse({"ok": False, "error": "不是有效的 .ttscx 文件"}, status_code=400)
        entries = body.get("entries") or []
        if not entries:
            return JSONResponse({"ok": False, "error": ".ttscx 中没有锚点数据"}, status_code=400)

        existing = G.settings.setdefault("cloned_voices", {})
        imported, skipped, failed = 0, 0, []
        for entry in entries:
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            if name in existing:
                skipped += 1
                continue
            audio_b64 = entry.get("audio_b64", "")
            ext = entry.get("ext", ".wav") or ".wav"
            if not str(ext).startswith("."):
                ext = "." + ext
            if not audio_b64:
                failed.append(name)
                continue
            try:
                fd, tmp = tempfile.mkstemp(suffix=ext)
                with os.fdopen(fd, "wb") as f:
                    f.write(_b64.b64decode(audio_b64))
                try:
                    saved_audio = _persist_clone_audio(tmp, name, ext)
                except Exception:
                    saved_audio = tmp
                finally:
                    if saved_audio != tmp:
                        try:
                            os.unlink(tmp)
                        except Exception:
                            pass
                existing[name] = {
                    "audio_path": saved_audio,
                    "anchor": True,
                    "source_speaker": entry.get("source_speaker", ""),
                    "emotion": entry.get("emotion", ""),
                    "emotion_label": entry.get("emotion_label", ""),
                    "instruction": entry.get("instruction", ""),
                    "created_at": entry.get("created_at", ""),
                }
                if G.worker and G.worker_ready:
                    G.worker.send({"type": "clone_voice", "name": name,
                                   "audio_path": saved_audio})
                imported += 1
            except Exception as e:
                failed.append(f"{name}: {e}")

        save_settings(G.settings)
        _sync_clone_init(G.settings)
        if imported:
            broadcast_sync({"type": "anchor_pack_imported",
                             "imported": imported, "skipped": skipped,
                             "failed": len(failed)})
        return JSONResponse({
            "ok": True,
            "imported": imported,
            "skipped": skipped,
            "failed": failed,
            "total": len(entries),
        })

    @app.get("/api/clone_init")
    async def get_clone_init():
        """返回 init.json 中记录的克隆/锚定音色列表，供前端随时刷新。"""
        return JSONResponse(load_clone_init())

    @app.get("/api/status")
    async def status():
        # 只有文件真正落盘后才把 output_path 暴露给轮询方，
        # 避免前端在合并写入未完成时就发起 /api/download 请求。
        op = G.job_output_path
        safe_op = op if (op and Path(op).exists() and Path(op).stat().st_size > 44) else ""
        return JSONResponse({
            "status": G.job_status,
            "worker_ready": G.worker_ready,
            "output_path": safe_op,
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
