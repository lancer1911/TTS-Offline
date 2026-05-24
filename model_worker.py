"""
Lancer1911 TTS Offline v0.4w — 模型工作进程
使用 mlx_audio (https://github.com/Blaizzy/mlx-audio) 驱动 Qwen3-TTS MLX 推理。
"""
from __future__ import annotations
import os, time, traceback, tempfile, wave, struct, inspect, re
import numpy as np
from pathlib import Path


# ── 工作进程主函数 ────────────────────────────────────────────
def _cleanup_temp_files():
    """清理上次运行残留的 ttso_* 临时文件。"""
    import tempfile, glob
    pattern = os.path.join(tempfile.gettempdir(), "ttso_*.wav")
    for f in glob.glob(pattern):
        try:
            os.unlink(f)
            print(f"[worker] cleaned up: {f}", flush=True)
        except Exception:
            pass


class _QueueTee:
    """Mirror worker stdout/stderr to terminal and to the UI log via result_q."""
    def __init__(self, stream, result_q, name: str):
        self.stream = stream
        self.result_q = result_q
        self.name = name
        self._buf = ""

    def write(self, data):
        try:
            self.stream.write(data)
            self.stream.flush()
        except Exception:
            pass
        if not data:
            return 0
        self._buf += str(data)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                try:
                    self.result_q.put({
                        "type": "terminal_log",
                        "stream": self.name,
                        "message": line,
                        "ts": time.time(),
                    })
                except Exception:
                    pass
        return len(data)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self.stream.isatty()
        except Exception:
            return False


def worker_main(task_q, result_q, model_repo: str, device: str = "mlx"):
    import sys
    # Keep terminal output unchanged, but also forward every worker print() line to the UI log.
    sys.stdout = _QueueTee(sys.stdout, result_q, "stdout")
    sys.stderr = _QueueTee(sys.stderr, result_q, "stderr")
    _cleanup_temp_files()
    try:
        _init_model(model_repo)
        result_q.put({"type": "ready", "ok": True})
    except Exception as e:
        result_q.put({"type": "ready", "ok": False, "error": str(e),
                      "trace": traceback.format_exc()})
        return

    while True:
        try:
            task = task_q.get(timeout=60)
        except Exception:
            continue
        if task is None:
            break
        t   = task.get("type", "")
        tid = task.get("id", "")
        try:
            if t == "ping":
                result_q.put({"id": tid, "ok": True, "type": "pong"})
            elif t == "tts":
                _handle_tts(task, result_q)
            elif t == "list_voices":
                result_q.put({"id": tid, "ok": True, "type": "voices",
                               "voices": _list_voices()})
            elif t == "clone_voice":
                _handle_clone(task, result_q)
            elif t == "create_anchor":
                _handle_create_anchor(task, result_q)
            elif t == "delete_clone":
                name = task.get("name", "")
                _cloned_voices.pop(name, None)
                result_q.put({"id": tid, "ok": True, "type": "clone_deleted", "name": name})
            else:
                result_q.put({"id": tid, "ok": False,
                               "error": f"Unknown task: {t}"})
        except Exception as e:
            result_q.put({"id": tid, "ok": False, "error": str(e),
                           "trace": traceback.format_exc()})


# ── 模型状态 ──────────────────────────────────────────────────
_model      = None
_model_repo = ""
_model_type = "base"
_cloned_voices: dict = {}

_TYPE_MAP = {
    "base":         "base",
    "customvoice":  "custom_voice",
    "voicedesign":  "voice_design",
}


# ── 彩色终端日志与坏音频恢复 ─────────────────────────────────────
_ANSI = {
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "dim": "\033[2m", "reset": "\033[0m",
}

def _col(text: str, color: str) -> str:
    # macOS Terminal / iTerm / VS Code terminal all support ANSI.  If output is
    # redirected to a file, the escape codes are still harmless and make warnings
    # easy to grep.
    return f"{_ANSI.get(color, '')}{text}{_ANSI['reset'] if color in _ANSI else ''}"


def _log(level: str, msg: str):
    color = {"OK":"green", "WARN":"yellow", "BAD":"red", "RETRY":"cyan", "INFO":"blue"}.get(level, "reset")
    print(_col(msg, color), flush=True)


class BadAudioError(RuntimeError):
    """Raised when generated audio looks like low-frequency whine/drone."""
    pass


def _expected_duration_s(text: str) -> float:
    t = str(text or "")
    zh_ratio = sum(1 for c in t if '一' <= c <= '鿿') / max(len(t), 1)
    ms_per_char = 400 if zh_ratio > 0.2 else 220
    return max(1.5, len(t) * ms_per_char / 1000)


def _audio_zcr_stats(arr: np.ndarray, sr: int) -> dict:
    arr = np.asarray(arr, dtype=np.float32).flatten()
    if arr.size < int(sr * 0.05):
        return {"windows": 0, "speech": 0, "above_sil": 0, "min": 0.0, "max": 0.0, "mean": 0.0}
    hop = max(1, int(sr * 0.05))
    win = max(1, int(sr * 0.10))
    n = max(1, (arr.size - win) // hop + 1)
    vals = []
    for wi in range(n):
        st = wi * hop
        en = min(arr.size, st + win)
        w = arr[st:en]
        if w.size < 2:
            vals.append(0.0)
            continue
        signs = np.sign(w); signs[signs == 0] = 1
        vals.append(float(np.sum(np.abs(np.diff(signs))) / 2) / (len(w) / sr))
    z = np.array(vals, dtype=np.float32)
    return {
        "windows": int(z.size),
        "speech": int(np.sum(z >= 200.0)),
        "above_sil": int(np.sum(z >= 5.0)),
        "min": float(z.min()) if z.size else 0.0,
        "max": float(z.max()) if z.size else 0.0,
        "mean": float(z.mean()) if z.size else 0.0,
    }


def _audio_metrics(arr: np.ndarray, text: str, sr: int | None = None) -> dict:
    sr = int(sr or _get_sample_rate())
    arr = np.asarray(arr, dtype=np.float32).flatten()
    dur = arr.size / sr if sr and arr.size else 0.0
    peak = float(np.abs(arr).max()) if arr.size else 0.0
    z = _audio_zcr_stats(arr, sr) if arr.size else {"windows":0,"speech":0,"above_sil":0,"min":0.0,"max":0.0,"mean":0.0}
    expected = _expected_duration_s(text)
    return {"duration": dur, "peak": peak, "expected": expected, **z}


def _bad_audio_reason(metrics: dict, text: str = "") -> str:
    dur = float(metrics.get("duration") or 0)
    peak = float(metrics.get("peak") or 0)
    expected = float(metrics.get("expected") or _expected_duration_s(text))
    zmean = float(metrics.get("mean") or 0)
    speech = int(metrics.get("speech") or 0)
    above = int(metrics.get("above_sil") or 0)
    text_len = len(str(text or ""))
    # Strong low-energy failure: looks like silence/drone but lasts too long.
    if peak < 0.020 and dur > max(2.0, expected * 1.2):
        return f"low_peak_long_output peak={peak:.4f} dur={dur:.2f}s expected={expected:.2f}s"
    # Long output with low ZCR is the typical low-frequency whine / drone case.
    if dur > expected * 2.5 and zmean < 850:
        return f"long_low_zcr dur={dur:.2f}s expected={expected:.2f}s zcr_mean={zmean:.0f}/s"
    if text_len <= 20 and dur > 10.0 and zmean < 900:
        return f"short_text_long_low_zcr len={text_len} dur={dur:.2f}s zcr_mean={zmean:.0f}/s"
    if dur > 25.0 and zmean < 700:
        return f"very_long_low_zcr dur={dur:.2f}s zcr_mean={zmean:.0f}/s"
    if dur > expected * 2.2 and speech <= max(1, above // 4) and zmean < 1000:
        return f"speech_sparse_drone dur={dur:.2f}s speech={speech} above_sil={above} zcr_mean={zmean:.0f}/s"
    return ""


def _sanitize_retry_text(text: str) -> str:
    t = str(text or "").strip()
    # Do not rewrite semantic content; only remove terminal symbols that often
    # make Qwen3-TTS continue with a drone/unfinished utterance.
    t = t.replace("……", "。").replace("...", "。")
    t = t.rstrip("—-–")
    # Retry text should be TTS-friendly.  Quotation marks are useful for
    # reading but often destabilize short cloned-voice Base generations,
    # especially when only one side of the quote remains after splitting.
    for _q in "「」『』“”\"'":
        t = t.replace(_q, "")
    t = t.replace("（", "，").replace("）", "，")
    t = t.replace("(", ",").replace(")", ",")
    return t.strip() or str(text or "").strip()


def _repair_split_level_count() -> int:
    # 0: sentence punctuation; 1: comma/soft punctuation; 2-4: increasingly
    # shorter phrase groups; 5-8: near word/character-level fallback;
    # 9+: final one-character fallback repeated with safer parameters until
    # the 20-attempt budget is consumed.
    return 12


def _split_text_for_repair(text: str, max_chars: int = 45, level: int = 0) -> list[str]:
    """Progressively split a bad chunk for repair.

    The earlier levels preserve natural phrasing.  Later levels are deliberately
    aggressive so a pathological sentence can still be synthesized in small,
    controllable pieces before we give up and replace the whole chunk with
    short silence.
    """
    t = _sanitize_retry_text(text)
    if not t:
        return []
    level = max(0, int(level or 0))

    # Level 0: only hard sentence punctuation.
    if level == 0:
        separators = r'(?<=[。！？!?；;])'
        limit = max_chars
    # Level 1: include comma-like punctuation.
    elif level == 1:
        separators = r'(?<=[。！？!?；;，,、：:])'
        limit = min(max_chars, 32)
    # Level 2: phrase-level, still readable.
    elif level == 2:
        separators = r'(?<=[。！？!?；;，,、：:———\-])'
        limit = 22
    # Level 3: short phrase-level.
    elif level == 3:
        separators = r'(?<=[。！？!?；;，,、：:———\-])'
        limit = 14
    # Level 4: very short phrase-level.
    elif level == 4:
        separators = r'(?<=[。！？!?；;，,、：:———\-])'
        limit = 8
    else:
        # Level 5+: approximate word/character-level.  For Chinese we cannot
        # rely on a tokenizer.  The chunk size is gradually reduced from
        # 4 Chinese characters down to 1 character.  This prevents a sentence
        # such as "我的天哪！我的粉盒在哪儿？" from staying stuck as the same
        # two punctuation-based subchunks; later levels become "我的天哪 /
        # 我的粉盒 / 在哪儿", then "我的 / 天哪 / 我的 / 粉盒 / 在哪 / 儿",
        # and finally single characters.  Punctuation is removed in these
        # emergency levels because exclamation/question marks are a common
        # trigger for cloned-voice Base-model drone tails.
        if level == 5:
            token_limit = 4
        elif level == 6:
            token_limit = 3
        elif level == 7:
            token_limit = 2
        else:
            token_limit = 1

        # Remove punctuation and brackets for the most aggressive retries.
        # Keep Latin words/numbers intact; split CJK text by character count.
        clean = re.sub(r'''[。！？!?；;，,、：:、\.．…—\-「」『』“”"'（）()\[\]【】《》<>\s]+''', '', t)
        if not clean:
            clean = re.sub(r'\s+', '', t) or t
        pieces = re.findall(r'[\u4e00-\u9fff]|[A-Za-z0-9]+(?:[\'’][A-Za-z0-9]+)?|.', clean)
        out = []
        buf = ''
        for piece in pieces:
            # Keep non-CJK word tokens intact where possible, but still flush
            # the buffer first to avoid making mixed chunks too long.
            if re.fullmatch(r'[A-Za-z0-9]+(?:[\'’][A-Za-z0-9]+)?', piece):
                if buf.strip():
                    out.append(buf.strip())
                    buf = ''
                out.append(piece)
                continue
            if buf and len(buf) + len(piece) > token_limit:
                out.append(buf.strip())
                buf = piece
            else:
                buf += piece
        if buf.strip():
            out.append(buf.strip())
        return out or [t]

    raw = re.split(separators, t)
    parts = []
    for piece in raw:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) <= limit:
            parts.append(piece)
            continue

        # Secondary forced split for overlong phrases.  Prefer natural breaks;
        # if none exist, cut by character length as a last resort for this level.
        soft = re.split(r'(?<=[，,、：:])', piece)
        buf = ''
        for sp in soft:
            sp = sp.strip()
            if not sp:
                continue
            if len(sp) > limit:
                if buf.strip():
                    parts.append(buf.strip())
                    buf = ''
                parts.extend([sp[i:i+limit] for i in range(0, len(sp), limit) if sp[i:i+limit].strip()])
            elif buf and len(buf) + len(sp) > limit:
                parts.append(buf.strip())
                buf = sp
            else:
                buf += sp
        if buf.strip():
            parts.append(buf.strip())
    return parts or [t]

def _safer_retry_advanced(advanced: dict, text: str, attempt: int = 1) -> dict:
    adv = dict(advanced or {})
    a = max(1, int(attempt or 1))
    # Gradually reduce sampling freedom as repair becomes more aggressive.
    # This helps short sub-chunks terminate cleanly instead of drifting into
    # low-frequency drone or unfinished speech.
    temp_cap = max(0.06, 0.14 - 0.015 * a)
    top_p_cap = max(0.60, 0.78 - 0.035 * a)
    top_k_cap = max(4, 12 - a)
    adv["temperature"] = min(float(adv.get("temperature") or 0.2), temp_cap)
    adv["top_p"] = min(float(adv.get("top_p") or 0.85), top_p_cap)
    adv["top_k"] = min(int(float(adv.get("top_k") or 20)), top_k_cap)
    # Shorter sub-chunks should not get the same token budget as whole lines.
    safe_tokens = max(96, min(512, int(len(str(text or "")) * 7 + 96)))
    adv["max_tokens"] = min(int(float(adv.get("max_tokens") or safe_tokens)), safe_tokens)
    return adv

def _write_wav_array(audio_np: np.ndarray, idx: int, tag: str = "") -> str:
    fd, tmp_wav = tempfile.mkstemp(prefix=f"ttso_{os.getpid()}_{idx}_{tag}", suffix=".wav")
    os.close(fd)
    sr = _get_sample_rate()
    pcm = (np.clip(audio_np, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(tmp_wav, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return tmp_wav


def _make_silence_wav(duration_s: float, idx: int, tag: str = "silence") -> str:
    sr = _get_sample_rate()
    dur = min(max(float(duration_s or 0.8), 0.6), 2.0)
    return _write_wav_array(np.zeros(int(sr * dur), dtype=np.float32), idx, tag=tag)


def _make_timeline_silence_wav(duration_s: float, idx: int, tag: str = "timeline_gap") -> str:
    """Create arbitrary-length silence for SRT timeline alignment.

    Unlike _make_silence_wav(), this must not cap at 2 seconds because
    timestamped files may start late or contain long blank intervals.
    """
    sr = _get_sample_rate()
    dur = max(0.001, float(duration_s or 0))
    return _write_wav_array(np.zeros(int(sr * dur), dtype=np.float32), idx, tag=tag)

def _detect_type(repo: str) -> str:
    r = repo.lower().replace("-", "").replace("_", "")
    for k, v in _TYPE_MAP.items():
        if k in r:
            return v
    return "base"

def _init_model(repo: str):
    global _model, _model_repo, _model_type, _VOICES_CUSTOM
    import os as _os
    # Enforce offline mode — prevent any HuggingFace network requests at runtime.
    # Models must be downloaded in advance via `hf download` or the app UI.
    _os.environ.setdefault("HF_HUB_OFFLINE", "1")
    _os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    from mlx_audio.tts.utils import load_model
    _model_type = _detect_type(repo)
    try:
        _model = load_model(repo)
    except Exception as e:
        err_str = str(e)
        if "offline" in err_str.lower() or "connection" in err_str.lower() or "404" in err_str or "not found" in err_str.lower():
            raise RuntimeError(
                f"Model not found in local cache: {repo}\n"
                f"Please download it first with:\n"
                f"  hf download {repo}\n"
                f"Original error: {e}"
            ) from e
        raise
    _model_repo = repo
    # 动态读取模型实际支持的音色列表
    if _model_type == "custom_voice" and hasattr(_model, "supported_speakers"):
        _gender_hint = {"serena":"female","vivian":"female","ono_anna":"female",
                        "sohee":"female","ryan":"male","aiden":"male","eric":"male",
                        "dylan":"male","uncle_fu":"male","chelsie":"female",
                        "cherry":"female","ethan":"male","leo":"male","daniel":"male",
                        "aura":"female","brenda":"female"}
        _VOICES_CUSTOM = [
            {"id": spk, "name": spk.replace("_"," ").title(),
             "lang": "zh/en",
             "gender": _gender_hint.get(spk.lower(), "neutral"),
             "desc": ""}
            for spk in _model.supported_speakers
        ]
        print(f"[worker] CustomVoice speakers: {[v['id'] for v in _VOICES_CUSTOM]}", flush=True)


# ── 内置音色 ──────────────────────────────────────────────────
# mlx-audio 8bit CustomVoice 实际支持的小写音色 ID（运行时从模型读取会更准确）
# 以下为已知列表，_list_voices() 会在模型加载后动态更新
_VOICES_BASE = [
    {"id": "base_default", "name": "Default (Voice Clone)", "lang": "zh/en/ja/ko",
     "gender": "neutral", "desc": "Upload reference audio in the Clone tab"},
]

# mlx-community 8bit CustomVoice 实际音色（小写）
_VOICES_CUSTOM_FALLBACK = [
    {"id": "serena",    "name": "Serena",    "lang": "zh/en", "gender": "female", "desc": "温柔知性女声"},
    {"id": "vivian",    "name": "Vivian",    "lang": "zh/en", "gender": "female", "desc": "俏皮可爱女声"},
    {"id": "ono_anna",  "name": "Ono Anna",  "lang": "ja/zh", "gender": "female", "desc": "日系女声"},
    {"id": "sohee",     "name": "Sohee",     "lang": "ko/zh", "gender": "female", "desc": "韩系女声"},
    {"id": "ryan",      "name": "Ryan",      "lang": "zh/en", "gender": "male",   "desc": "标准英语男声"},
    {"id": "aiden",     "name": "Aiden",     "lang": "zh/en", "gender": "male",   "desc": "沉稳男声"},
    {"id": "eric",      "name": "Eric",      "lang": "zh/en", "gender": "male",   "desc": "活力男声"},
    {"id": "dylan",     "name": "Dylan",     "lang": "zh/en", "gender": "male",   "desc": "磁性男声（北京腔）"},
    {"id": "uncle_fu",  "name": "Uncle Fu",  "lang": "zh",    "gender": "male",   "desc": "成熟男声"},
]
_VOICES_CUSTOM = list(_VOICES_CUSTOM_FALLBACK)  # 运行时从模型动态更新

# VoiceDesign 模型：用自然语言描述音色
_VOICES_DESIGN = [
    {"id": "vd_female_warm_zh",  "name": "温暖中文女声",   "lang": "zh",    "gender": "female", "desc": "温柔自然的中文女声叙述者"},
    {"id": "vd_female_british",  "name": "英式女声叙述者", "lang": "en",    "gender": "female", "desc": "冷静专业的英式女声"},
    {"id": "vd_male_deep_en",    "name": "沉稳英语男声",   "lang": "en",    "gender": "male",   "desc": "深沉平稳的中年男声"},
    {"id": "vd_male_energetic",  "name": "活力男声",       "lang": "zh/en", "gender": "male",   "desc": "充满活力、清晰有力"},
    {"id": "vd_female_anime",    "name": "元气动漫女声",   "lang": "zh",    "gender": "female", "desc": "活泼可爱的动漫风格"},
    {"id": "vd_male_anchor_zh",  "name": "播音男声",       "lang": "zh",    "gender": "male",   "desc": "专业播音腔，标准普通话"},
]
_VD_PROMPTS = {
    "vd_female_warm_zh":  "female, warm Chinese narrator, gentle and clear",
    "vd_female_british":  "female, British narrator, calm and professional",
    "vd_male_deep_en":    "male, deep and calm voice, mid-30s, natural pacing",
    "vd_male_energetic":  "male, energetic and clear, friendly tone",
    "vd_female_anime":    "female, anime style, cheerful and cute, young voice",
    "vd_male_anchor_zh":  "male, professional Chinese news anchor, authoritative and clear, standard Mandarin",
}

def _list_voices() -> list:
    base = {
        "base":         _VOICES_BASE,
        "custom_voice": _VOICES_CUSTOM,
        "voice_design": _VOICES_DESIGN,
    }.get(_model_type, _VOICES_BASE)
    cloned = [
        {"id": f"__clone__{k}", "name": k, "lang": "any",
         "gender": "clone", "desc": "声音克隆（Base 模型）"}
        for k in _cloned_voices
    ]
    return base + cloned


# ── TTS 推理 ──────────────────────────────────────────────────

def _sanitize_dialog_text(text: str) -> str:
    """Keep a dialog-row payload as literal text, not an instruction."""
    s = str(text or "").strip()
    # Remove common accidental wrapping quotes but do not otherwise rewrite content.
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        s = s[1:-1].strip()
    return s


def _safe_dialog_instruction(raw: str) -> str:
    """Convert short emotion labels into a safe CustomVoice instruction.

    Qwen3-TTS CustomVoice treats `instruct` as a style/control prompt.  If the UI
    passes only "悲伤" or a dialogue-like sentence, the model may interpret it as a
    performance prompt and add lines before the target text.  This wrapper keeps
    the style request but explicitly forbids continuation, explanation or extra
    dialogue.
    """
    raw = str(raw or "").strip()
    emotion_map = {
        "平静": "平静自然，语速适中，发音清晰。",
        "旁白": "平静自然，像有声书旁白一样朗读。",
        "高兴": "语气轻快明亮，带有自然的高兴感。",
        "开心": "语气轻快明亮，带有自然的高兴感。",
        "悲伤": "语气低沉、缓慢，带有克制的悲伤和失落感。",
        "低落": "语气低沉、缓慢，带有克制的悲伤和失落感。",
        "亢奋": "语气更有能量，节奏略快，但保持清晰。",
        "激动": "语气更有能量，节奏略快，但保持清晰。",
        "愤怒": "语气严厉、克制，带有压抑的愤怒感。",
        "紧张": "语气紧张、压低，节奏略快。",
        "恐惧": "语气不安，略带恐惧感，但不要尖叫。",
        "惊讶": "语气带有明显惊讶和疑问，但保持自然。",
        "严肃": "语气严肃、稳重、冷静，停顿明确。",
        "温柔": "语气柔和、温暖、安抚，语速稍慢。",
        "担忧": "语气带有担忧和犹豫，停顿略多。",
        "疲惫": "语气疲惫、略低、力量不足，但吐字清楚。",
        "neutral": "平静自然，语速适中，发音清晰。",
        "happy": "语气轻快明亮，带有自然的高兴感。",
        "sad": "语气低沉、缓慢，带有克制的悲伤和失落感。",
        "excited": "语气更有能量，节奏略快，但保持清晰。",
        "angry": "语气严厉、克制，带有压抑的愤怒感。",
        "tense": "语气紧张、压低，节奏略快。",
        "fear": "语气不安，略带恐惧感，但不要尖叫。",
        "surprised": "语气带有明显惊讶和疑问，但保持自然。",
        "serious": "语气严肃、稳重、冷静，停顿明确。",
        "gentle": "语气柔和、温暖、安抚，语速稍慢。",
        "worried": "语气带有担忧和犹豫，停顿略多。",
        "tired": "语气疲惫、略低、力量不足，但吐字清楚。",
    }
    base = emotion_map.get(raw, raw or "平静自然，语速适中，发音清晰。")
    guard = (
        "严格只朗读输入的 text 字段原文；不要添加任何开场白、口头禅、解释、续写、"
        "补充台词或角色反应；不要说“让我把话说完”等未出现在 text 中的内容。"
    )
    return f"{base} {guard}"


def _safe_base_advanced(advanced: dict, text: str = "") -> dict:
    """Apply conservative defaults for Base/clone mode, but respect user overrides.

    max_tokens is computed dynamically from text length to prevent the model from
    hitting a fixed ceiling and filling the remainder with noise/continuation:
      - English TTS: ~80 codec tokens/second, ~4 chars/token
      - Safety multiplier ×2.5 accounts for slow speech, pauses, punctuation
      - Floor of 256 tokens for very short strings
      - Hard ceiling of 4096 (never needed in practice, guards against bugs)
    """
    adv = dict(advanced or {})
    adv.setdefault("temperature", 0.20)
    adv.setdefault("top_p",       0.85)
    adv.setdefault("top_k",       20)
    # Dynamic max_tokens based on text length.
    # If the user has explicitly set max_tokens in Advanced Settings, honour it
    # only when it is LOWER than our computed value (they want tighter control).
    # If they set a very high value (e.g. 4096 default), use our tighter limit.
    text_len = len(str(text or ""))
    # ~4 chars per codec token, ×2.5 safety margin, +64 overhead
    dynamic = max(256, int(text_len * 4 * 2.5) + 64)
    dynamic = min(dynamic, 4096)
    user_val = adv.get("max_tokens")
    try:
        user_val = int(float(user_val)) if user_val is not None else None
    except Exception:
        user_val = None
    if user_val and user_val < dynamic:
        adv["max_tokens"] = user_val   # user explicitly tightened → respect it
    else:
        adv["max_tokens"] = dynamic    # use dynamic limit
    return adv




def _handle_tts(task: dict, result_q):
    tid        = task["id"]
    text       = task["text"]
    voice_id   = task.get("voice_id", "")
    speed      = float(task.get("speed", 1.0))
    out_path   = task.get("output_path", "")
    advanced   = task.get("advanced", {})
    dialog_rows = task.get("dialog_rows") or []
    srt_segments = task.get("srt_segments") or []
    chunk_size = int(task.get("chunk_size", 250))

    if not out_path:
        fd, out_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

    # 根据文本语言动态决定分段上限
    # 中文：100 字（≈100 tokens），英文：450 字符（≈60-80 词）
    # Base 模型对长文本不稳定，额外限制
    zh_ratio = sum(1 for c in text if '\u4e00' <= c <= '\u9fff') / max(len(text), 1)
    is_mainly_chinese = zh_ratio > 0.2
    if is_mainly_chinese:
        lang_limit = 100
    else:
        lang_limit = 600
    if _model_type == "base":
        lang_limit = min(lang_limit, 100 if is_mainly_chinese else 500)
    chunk_size = min(chunk_size, lang_limit)

    # 普通文本：整体分段；多人对话 CSV：按每行 speaker/instruction/text 分段；
    # SRT：按字幕时间轴处理，第一次文字前的空白也用静音补齐。
    chunk_items = []
    is_srt_timeline = False
    if isinstance(dialog_rows, list) and dialog_rows:
        if _model_type not in ("custom_voice", "base"):
            result_q.put({"id": tid, "type": "done", "ok": False,
                           "error": "多人对话 CSV 仅支持 CustomVoice 或 Base 模型"})
            return
        for row_idx, row in enumerate(dialog_rows):
            if not isinstance(row, dict):
                continue
            row_text = str(row.get("text") or "").strip()
            if not row_text:
                continue
            row_speaker = str(row.get("speaker") or voice_id or "").strip()
            row_instruction = "" if _model_type == "base" else _safe_dialog_instruction(str(row.get("instruction") or "").strip())
            row_text_san = _sanitize_dialog_text(row_text)
            # 每行独立合成以防止跨行上下文泄漏；但若单行超过 chunk_size，
            # 仍需拆分——超长文本会导致模型在后半段产生质量退化的音频（低频啸叫）。
            # 拆分后各子段共享同一 speaker/instruction，合并时当作同一段落处理。
            if len(row_text_san) > chunk_size:
                sub_chunks = _split_text(row_text_san, chunk_size)
            else:
                sub_chunks = [(row_text_san, True)]
            for sub_i, (sub_text, _) in enumerate(sub_chunks):
                if not sub_text.strip():
                    continue
                chunk_items.append({
                    "text": sub_text,
                    # 只有该行最后一个子段才算 para_end，以控制停顿插入
                    "is_para_end": (sub_i == len(sub_chunks) - 1),
                    "speaker": row_speaker,
                    "instruction": row_instruction,
                    "row_index": row_idx,
                    "dialog_row": True,
                })
    elif isinstance(srt_segments, list) and srt_segments:
        is_srt_timeline = True
        for seg_idx, seg in enumerate(srt_segments):
            if not isinstance(seg, dict):
                continue
            seg_text = str(seg.get("text") or "").strip()
            if not seg_text:
                continue
            try:
                target_start_ms = max(0, int(float(seg.get("start_ms") or 0)))
            except Exception:
                target_start_ms = 0
            try:
                target_end_ms = max(target_start_ms, int(float(seg.get("end_ms") or target_start_ms)))
            except Exception:
                target_end_ms = target_start_ms
            if len(seg_text) > chunk_size:
                sub_chunks = _split_text(seg_text, chunk_size)
            else:
                sub_chunks = [(seg_text, True)]
            for sub_i, (sub_text, _) in enumerate(sub_chunks):
                if not sub_text.strip():
                    continue
                chunk_items.append({
                    "text": sub_text,
                    "is_para_end": False,
                    "speaker": "",
                    "instruction": "",
                    "row_index": None,
                    "dialog_row": False,
                    "srt_timeline": True,
                    "srt_index": seg_idx,
                    # 只把原始字幕开始时间绑定到该字幕的第一个子段；
                    # 同一字幕拆出的后续子段顺序紧接，不再强制二次对齐。
                    "target_start_ms": target_start_ms if sub_i == 0 else None,
                    "target_end_ms": target_end_ms if sub_i == len(sub_chunks) - 1 else None,
                    "source_start_time": str(seg.get("start_time") or ""),
                    "source_end_time": str(seg.get("end_time") or ""),
                })
    else:
        for chunk_text, is_para_end in _split_text(text, chunk_size):
            if chunk_text.strip():
                chunk_items.append({
                    "text": chunk_text,
                    "is_para_end": is_para_end,
                    "speaker": "",
                    "instruction": "",
                    "row_index": None,
                    "dialog_row": False,
                })

    base_gap = int(advanced.get("silence_gap_ms", 300))
    total  = len(chunk_items)
    result_q.put({"id": tid, "type": "progress", "stage": "start",
                  "total_chunks": total})

    chunk_wavs = []  # list of (wav_path, pause_ms)
    timestamp_entries = []  # final concat timestamps; also sent in done for reliable follow
    _audio_cursor_ms = 0  # 用于时间戳累计，必须与最终合并后的音频一致
    timeline_overrun_count = 0
    timeline_max_lag_ms = 0
    timeline_gap_count = 0

    for i, item in enumerate(chunk_items):
        chunk = item["text"]
        is_para_end = item.get("is_para_end", False)
        row_speaker = item.get("speaker") or voice_id
        row_instruction = item.get("instruction") or ""
        row_idx = item.get("row_index")
        t0 = time.time()
        try:
            row_adv = dict(advanced or {})
            if _model_type == "base" and item.get("dialog_row"):
                row_adv = _safe_base_advanced(row_adv, text=chunk)
            if row_instruction:
                row_adv["style_instruct"] = row_instruction
            wav_path, repair_info = _synth_chunk_with_repair(chunk, row_speaker, speed, row_adv, i)
            # SRT 时间轴模式由原始时间戳控制空白，不再叠加标点停顿。
            pause_ms = 0 if item.get("srt_timeline") else _pause_for_ending(chunk, base_gap, is_paragraph_end=is_para_end)

            target_start = item.get("target_start_ms")
            if item.get("srt_timeline") and target_start is not None:
                try:
                    target_start = int(target_start)
                except Exception:
                    target_start = None
                if target_start is not None:
                    if _audio_cursor_ms < target_start:
                        gap_ms = target_start - _audio_cursor_ms
                        timeline_gap_count += 1
                        gap_wav = _make_timeline_silence_wav(gap_ms / 1000.0, i, tag="srt_gap")
                        chunk_wavs.append((gap_wav, 0))
                        print(f"[srt-timeline] insert silence before chunk={i}: {gap_ms}ms target={target_start}ms cursor={_audio_cursor_ms}ms", flush=True)
                        result_q.put({"id": tid, "type": "progress", "stage": "timeline_gap",
                                      "chunk_idx": i, "gap_ms": gap_ms,
                                      "target_start_ms": target_start,
                                      "audio_cursor_ms": _audio_cursor_ms})
                        _audio_cursor_ms = target_start
                    elif _audio_cursor_ms > target_start:
                        lag_ms = _audio_cursor_ms - target_start
                        timeline_overrun_count += 1
                        timeline_max_lag_ms = max(timeline_max_lag_ms, lag_ms)
                        print(f"[srt-timeline] overrun before chunk={i}: lag={lag_ms}ms target={target_start}ms cursor={_audio_cursor_ms}ms", flush=True)

            chunk_wavs.append((wav_path, pause_ms))
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[worker] chunk {i} error: {e}\n{tb}", flush=True)
            result_q.put({"id": tid, "type": "progress", "stage": "chunk_error",
                           "chunk_idx": i, "error": str(e),
                           "speaker": row_speaker, "instruction": row_instruction,
                           "row_index": row_idx, "trace": tb})
            continue

        elapsed = round(time.time() - t0, 2)
        dur_ms = _chunk_duration_ms(wav_path, trimmed=True)
        # 最后一段在 _merge_wavs_variable() 中不会追加人工停顿；时间戳也必须保持一致。
        effective_pause_ms = pause_ms if i < total - 1 else 0
        audio_start_ms = _audio_cursor_ms
        audio_end_ms = _audio_cursor_ms + dur_ms + effective_pause_ms
        ts_entry = {
            "chunk_idx": i,
            "total_chunks": total,
            "row_index": row_idx,
            "speaker": row_speaker if row_idx is not None else "",
            "instruction": row_instruction if row_idx is not None else "",
            "text": chunk,
            "text_preview": chunk,
            "pause_ms": effective_pause_ms,
            "audio_start_ms": audio_start_ms,
            "audio_end_ms": audio_end_ms,
            "srt_timeline": bool(item.get("srt_timeline")),
            "srt_index": item.get("srt_index"),
            "target_start_ms": item.get("target_start_ms"),
            "target_end_ms": item.get("target_end_ms"),
            "source_start_time": item.get("source_start_time", ""),
            "source_end_time": item.get("source_end_time", ""),
            "bad_audio_status": repair_info.get("bad_audio_status", "ok") if isinstance(repair_info, dict) else "ok",
            "bad_audio_attempts": repair_info.get("bad_audio_attempts", 0) if isinstance(repair_info, dict) else 0,
            "bad_audio_reason": repair_info.get("bad_audio_reason", "") if isinstance(repair_info, dict) else "",
        }
        timestamp_entries.append(ts_entry)
        result_q.put({
            "id": tid, "type": "progress", "stage": "chunk",
            "elapsed": elapsed,
            **ts_entry,
        })
        _audio_cursor_ms = audio_end_ms

    if not chunk_wavs:
        result_q.put({"id": tid, "type": "done", "ok": False,
                       "error": "No audio generated"})
        return

    # 合并所有分段 wav，每段间使用对应的停顿时长。
    # 关键：合并必须显式校验输出文件是否真正落盘；否则前端会收到 done，
    # 但 /api/download 播放时 404。
    try:
        _merge_wavs_variable(chunk_wavs, out_path)
        if not os.path.exists(out_path) or os.path.getsize(out_path) <= 44:
            raise RuntimeError(f"Merged output was not created or is empty: {out_path}")
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[worker] merge error: {e}\n{tb}", flush=True)
        # 合并失败时清理已生成的分段临时文件，避免磁盘泄漏
        for _p, _ in chunk_wavs:
            try:
                if _p != out_path:
                    os.unlink(_p)
            except Exception:
                pass
        result_q.put({"id": tid, "type": "done", "ok": False,
                      "error": f"Failed to merge output audio: {e}",
                      "output_path": out_path, "trace": tb})
        return

    # 清理临时文件
    for path, _ in chunk_wavs:
        try:
            if path != out_path:
                os.unlink(path)
        except Exception:
            pass

    sr       = _get_sample_rate()
    duration = _wav_duration(out_path, sr)
    timeline_warning = ""
    if is_srt_timeline and timeline_overrun_count:
        timeline_warning = (
            f"SRT timeline overrun: {timeline_overrun_count} segment(s) started later than their original timestamps; "
            f"maximum lag {timeline_max_lag_ms/1000:.2f}s. Consider increasing speed or shortening text."
        )
        print(f"[srt-timeline] warning: {timeline_warning}", flush=True)

    result_q.put({
        "id": tid, "type": "done", "ok": True,
        "output_path": out_path,
        "duration": round(duration, 2),
        "sample_rate": sr,
        "srt_timeline": bool(is_srt_timeline),
        "timeline_gap_count": timeline_gap_count,
        "timeline_overrun_count": timeline_overrun_count,
        "timeline_max_lag_ms": timeline_max_lag_ms,
        "timeline_warning": timeline_warning,
        # 关键：WebSocket 或轮询可能丢掉中间 progress。done 中携带完整时间戳，
        # 确保播放器跟随/字幕导出与最终裁剪后的音频一致。
        "timestamps": timestamp_entries,
    })


def _generation_kwargs(base_kwargs: dict, advanced: dict) -> dict:
    """Attach sampling parameters when the loaded mlx-audio method supports them.

    Different mlx-audio / Qwen3-TTS versions expose these knobs in different
    places. Passing only recognized kwargs keeps older versions compatible.
    """
    out = dict(base_kwargs)
    for key in ("temperature", "top_p", "top_k", "max_tokens"):
        if key in (advanced or {}) and advanced.get(key) is not None:
            val = advanced.get(key)
            try:
                if key in ("top_k", "max_tokens"):
                    val = int(val)
                else:
                    val = float(val)
            except Exception:
                continue
            out[key] = val
    return out

def _call_tts_generator(method, kwargs: dict):
    """Call a model generator while filtering unsupported sampling kwargs."""
    try:
        sig = inspect.signature(method)
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if not accepts_var_kw:
            kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    except Exception:
        # If signature inspection fails, try full kwargs first.
        pass
    try:
        return list(method(**kwargs))
    except TypeError as e:
        # Older wrappers may reject sampling kwargs. Retry without them.
        retry = {k: v for k, v in kwargs.items() if k not in {"temperature", "top_p", "top_k", "max_tokens"}}
        if retry != kwargs:
            return list(method(**retry))
        raise e

def _synth_chunk_to_file(text: str, voice_id: str, speed: float,
                          advanced: dict, idx: int) -> str:
    """合成一段，返回 wav 文件路径。
    mlx-audio 的 generate / generate_custom_voice / generate_voice_design
    都返回生成器，必须用 list() 或 for 循环完整消费后才能拿到音频。
    """
    import mlx.core as mx
    from mlx_audio.tts.utils import load_model as _lm  # noqa — already loaded

    lang = "Chinese" if _is_chinese(text) else "English"
    print(f"[synth] chunk={idx} model={_model_type} voice={voice_id} "
          f"lang={lang} len={len(text)} text={text!r}", flush=True)
    print(f"[synth] chunk={idx} advanced={advanced}", flush=True)
    _CUSTOM_VOICES = {"Chelsie","Cherry","Serena","Vivian","Aura","Ethan","Ryan","Leo","Daniel"}

    # ── Base 模型 ────────────────────────────────────────────
    if _model_type == "base":
        ref_audio = ""
        if voice_id == "base_default":
            raise RuntimeError(
                "Please clone a voice first: go to the Clone tab, upload 5–30 s of "
                "reference audio, then select the cloned voice here.\n"
                "（请先克隆音色：在「克隆」标签页上传 5~30 秒参考音频，完成后选择克隆音色再合成。）"
            )
        # 支持两种格式：__clone__Name 或直接 Name（来自对话表格 speaker 列）
        if voice_id.startswith("__clone__"):
            clone_name = voice_id[len("__clone__"):]
        else:
            clone_name = voice_id  # dialog 模式下 speaker 列直接是克隆名
        info = _cloned_voices.get(clone_name, {})
        ref_audio = info.get("audio_path", "")

        if not ref_audio:
            raise RuntimeError(
                f"Clone voice '{clone_name}' not found or has no reference audio. "
                "Please re-clone or re-import in the Clone tab.\n"
                f"（克隆音色「{clone_name}」未找到或无参考音频，请在「克隆」标签页重新克隆或导入。）"
            )

        # Base mode has no instruction field.  Keep decoding conservative to
        # reduce filler phrases / continuation hallucinations.
        advanced = _safe_base_advanced(advanced, text=text)

        kwargs = dict(
            text=text,
            lang_code=lang,
            speed=speed,
            ref_audio=ref_audio,
        )

        # generate() 返回生成器 → list() 完整消费
        _gen_kwargs = _generation_kwargs(kwargs, advanced)
        print(f"[synth] chunk={idx} Base generate kwargs={_gen_kwargs}", flush=True)
        results = _call_tts_generator(_model.generate, _gen_kwargs)
        print(f"[synth] chunk={idx} generate() returned {len(results)} result(s)", flush=True)

    # ── CustomVoice 模型 ─────────────────────────────────────
    elif _model_type == "custom_voice":
        # 获取模型实际支持的音色集合（小写匹配）
        _avail = {v["id"].lower() for v in _VOICES_CUSTOM}
        _avail_orig = {v["id"] for v in _VOICES_CUSTOM}
        # 优先精确匹配，再小写匹配，最后 fallback 到第一个
        if voice_id in _avail_orig:
            speaker = voice_id
        elif voice_id.lower() in _avail:
            speaker = voice_id.lower()
        else:
            speaker = _VOICES_CUSTOM[0]["id"] if _VOICES_CUSTOM else "serena"
        instruct = advanced.get("style_instruct", "") or "平静自然地朗读输入文本；严格只朗读 text 原文，不添加任何额外内容。"

        # ── 稳定性默认值（方案一）──────────────────────────────
        # 用户未显式传入采样参数时，使用更保守的默认值以减少 identity drift。
        # setdefault 保证用户通过高级设置面板手动调整的值不被覆盖。
        _STABLE_DEFAULTS = {"temperature": 0.2, "top_k": 20, "top_p": 0.85}
        for _k, _v in _STABLE_DEFAULTS.items():
            advanced.setdefault(_k, _v)

        # generate_custom_voice() 也是生成器
        kwargs = _generation_kwargs({
            "text": text,
            "speaker": speaker,
            "language": lang,
            "instruct": instruct,
        }, advanced)
        results = _call_tts_generator(_model.generate_custom_voice, kwargs)

    # ── VoiceDesign 模型 ──────────────────────────────────────
    elif _model_type == "voice_design":
        prompt  = _VD_PROMPTS.get(voice_id,
                    advanced.get("voice_design_prompt", "female, warm narrator"))
        kwargs = _generation_kwargs({
            "text": text,
            "language": lang,
            "instruct": prompt,
        }, advanced)
        results = _call_tts_generator(_model.generate_voice_design, kwargs)

    else:
        raise RuntimeError(f"Unknown model type: {_model_type}")

    # 拼接所有 audio 片段
    segments = [r.audio for r in results if hasattr(r, "audio") and r.audio is not None]
    print(f"[synth] chunk={idx} raw segments={len(segments)}", flush=True)
    if not segments:
        raise RuntimeError("generate() returned no audio segments")
    import numpy as np
    arrays = []
    for seg_i, seg in enumerate(segments):
        arr = np.array(seg, dtype=np.float32).flatten()
        sr_est = _get_sample_rate()
        dur = arr.size / sr_est if arr.size > 0 else 0
        raw_peak = float(np.abs(arr).max()) if arr.size > 0 else 0
        print(f"[synth] chunk={idx} seg={seg_i} samples={arr.size} dur={dur:.3f}s peak={raw_peak:.4f}", flush=True)
        if arr.size == 0:
            print(f"[synth] chunk={idx} seg={seg_i} → SKIP (empty)", flush=True)
            continue
        # ── 归一化 ──────────────────────────────────────────────
        peak = float(np.abs(arr).max())
        if peak > 1.5:
            arr = arr / peak           # 整数 PCM 编码，除以峰值归一化
        elif peak < 1e-4:
            print(f"[worker] chunk {idx} seg {seg_i}: peak={peak:.2e} too low, skipping", flush=True)
            continue

        raw_arr_size = arr.size   # 保存原始大小，供后面的 DURATION CEILING 使用

        # ── 分窗 ZCR 检测 ──────────────────────────────────────
        # 使用两档阈值：
        #   ZCR >= 200/s：「确定是语音」（real_speech）
        #   ZCR >=   5/s：「至少不是纯静音/直流」（above_silence）
        #
        # 经日志校准：
        #   正常语音：ZCR mean 1000-4000/s，real_speech windows 占绝大多数
        #   幻觉续写（drone）：ZCR mean 200-600/s，above_silence 通过但 real_speech 极少
        #   低频啸叫：ZCR < 5/s，above_silence 不通过
        #
        # above_silence 用于识别整段噪声（丢弃）；
        # real_speech 用于找语音结束点（截断 drone 尾巴）。
        sr_est = _get_sample_rate()
        min_check_samples = int(sr_est * 0.05)
        zcr_arr = None   # 供后面的 DURATION CEILING 使用
        hop_samples = max(1, int(sr_est * 0.05))
        win_samples = max(1, int(sr_est * 0.10))
        if arr.size >= min_check_samples and peak > 0.01:
            n_windows = max(1, (arr.size - win_samples) // hop_samples + 1)
            zcr_windows = []
            for wi in range(n_windows):
                st = wi * hop_samples
                en = min(arr.size, st + win_samples)
                w = arr[st:en]
                s = np.sign(w); s[s == 0] = 1
                zcr = float(np.sum(np.abs(np.diff(s))) / 2) / (len(w) / sr_est)
                zcr_windows.append(zcr)
            zcr_arr = np.array(zcr_windows)
            above_silence = zcr_arr >= 5.0     # 不是纯噪声/直流
            real_speech   = zcr_arr >= 200.0   # 确定是语音（drone 无法通过）
            print(f"[synth] chunk={idx} seg={seg_i} ZCR windows={len(zcr_windows)} "
                  f"speech={int(np.sum(real_speech))} above_sil={int(np.sum(above_silence))} "
                  f"min={float(zcr_arr.min()):.0f} max={float(zcr_arr.max()):.0f} "
                  f"mean={float(zcr_arr.mean()):.0f}/s", flush=True)
            if not np.any(above_silence):
                # 整段都是低频噪声或直流
                print(f"[synth] chunk={idx} seg={seg_i} → SKIP all-noise ZCR", flush=True)
                continue
            if not np.any(real_speech):
                # 全段 ZCR 都很低（drone）：幻觉续写的典型特征，整段丢弃
                print(f"[synth] chunk={idx} seg={seg_i} → SKIP drone (no real speech ZCR)", flush=True)
                continue
            # 找最后一个「确定是语音」的窗口。旧版在这里直接截断 drone/噪声尾巴；
            # 但实践中发现：某些句子虽然前段有语音、后面是 drone，直接截断会
            # 造成“句子没读完但被当作 OK”。因此只要原始音频出现显著长尾，
            # 一律作为可恢复坏音频处理，让上层改用清洗/重新切分后完整重生。
            last_speech_win = int(np.where(real_speech)[0][-1])
            cut_sample = min(arr.size, (last_speech_win + 1) * hop_samples + win_samples)
            if cut_sample < arr.size * 0.85:
                raw_metrics = _audio_metrics(arr, text, sr_est)
                raw_reason = _bad_audio_reason(raw_metrics, text)
                speech_ratio = float(np.sum(real_speech)) / max(1, len(real_speech))
                truncated_fraction = 1.0 - (cut_sample / max(1, arr.size))
                msg = (f"TRUNCATE would cut {truncated_fraction*100:.0f}% "
                       f"({cut_sample/sr_est:.2f}s / {arr.size/sr_est:.2f}s), "
                       f"speech_ratio={speech_ratio:.2f}")
                if raw_reason or truncated_fraction > 0.25 or speech_ratio < 0.65:
                    raise BadAudioError(f"raw_tail_before_complete_repair: {msg}; {raw_reason or 'significant_tail'}")
                print(f"[synth] chunk={idx} seg={seg_i} → TRUNCATE at "
                      f"{cut_sample/sr_est:.2f}s / {arr.size/sr_est:.2f}s "
                      f"(last real speech win={last_speech_win})", flush=True)
                arr = arr[:cut_sample]
            else:
                print(f"[synth] chunk={idx} seg={seg_i} → OK", flush=True)

        # ── 时长上限检测 ──────────────────────────────────────────
        # 触发条件：raw_actual > expected * 2.0
        # 三个 Bug 修复：
        #   Bug1: arr.size < raw_arr_size 的判断太严格——ZCR 可能只裁了极少量
        #         改为：ZCR 裁掉超过 10% 才认为"已处理"
        #   Bug2: last speech win 可能是 drone 中零星的高 ZCR 窗口
        #         改为：找最后一个"持续语音块"（连续 ≥3 个 >=200/s 窗口）的末尾
        #   Bug3: 截断后若 peak 极低，用 expected_s 兜底
        if raw_arr_size > 0:
            zh_ratio = sum(1 for c in text if '一' <= c <= '鿿') / max(len(text), 1)
            ms_per_char = 400 if zh_ratio > 0.2 else 220
            expected_s = max(1.5, len(text) * ms_per_char / 1000)
            raw_actual_s = raw_arr_size / sr_est
            if raw_actual_s > expected_s * 2.0:
                # Do not silently accept a duration-ceiling crop.  Cropping often
                # removes the drone tail, but in user testing it also masked cases
                # where the sentence had not been fully spoken.  Treat the raw long
                # output as bad audio so the repair path re-generates the whole
                # sentence or smaller sub-sentences.
                zcr_already_handled = (arr.size < raw_arr_size * 0.9)
                if zcr_already_handled:
                    raise BadAudioError(f"raw_long_output_after_zcr_cut raw={raw_actual_s:.1f}s expected={expected_s:.1f}s")
                else:
                    raw_metrics = _audio_metrics(arr, text, sr_est)
                    raw_reason = _bad_audio_reason(raw_metrics, text) or f"duration_ceiling_raw={raw_actual_s:.1f}s expected={expected_s:.1f}s"
                    raise BadAudioError(raw_reason)


        arrays.append(arr)
    if not arrays:
        raise BadAudioError("All audio segments were empty after ZCR/drone filtering")
    audio_np = np.concatenate(arrays)
    sr = _get_sample_rate()
    metrics = _audio_metrics(audio_np, text, sr)
    print(f"[synth] chunk={idx} final audio: {len(arrays)} array(s) "
          f"total_samples={audio_np.size} dur={metrics['duration']:.3f}s "
          f"peak={metrics['peak']:.4f} zcr_mean={metrics['mean']:.0f}/s", flush=True)

    reason = _bad_audio_reason(metrics, text)
    if reason:
        _log("BAD", f"[synth] chunk={idx} BAD_AUDIO detected: {reason}")
        raise BadAudioError(reason)

    # 用 wave 模块写 PCM WAV，绕过 audio_write 的值域问题
    tmp_wav = _write_wav_array(audio_np, idx)
    _log("OK", f"[worker] chunk {idx}: {len(audio_np)/sr:.2f}s  peak={metrics['peak']:.3f}")
    return tmp_wav


def _synth_chunk_with_repair(text: str, voice_id: str, speed: float,
                             advanced: dict, idx: int) -> tuple[str, dict]:
    """Generate one chunk with automatic bad-audio repair.

    Flow:
      1) original text
      2) sanitized text with safer sampling
      3) progressively re-split and regenerate: hard punctuation -> soft
         punctuation -> short phrases -> approximate word units
      4) after 20 bad-audio events, replace the whole chunk with short silence
         and mark it in the UI.
    """
    MAX_BAD_AUDIO_RETRIES = 20
    failures = 0
    last_error = ''

    def _silence_result(status: str = 'silence') -> tuple[str, dict]:
        wav = _make_silence_wav(_expected_duration_s(text), idx)
        return wav, {
            'bad_audio_status': status,
            'bad_audio_attempts': failures,
            'bad_audio_reason': last_error or 'bad_audio_retry_limit_reached',
        }

    def _try_once(t: str, adv: dict, label: str) -> str:
        nonlocal failures, last_error
        if failures >= MAX_BAD_AUDIO_RETRIES:
            raise BadAudioError('bad_audio_retry_limit_reached')
        try:
            return _synth_chunk_to_file(t, voice_id, speed, adv, idx)
        except BadAudioError as e:
            failures += 1
            last_error = str(e)
            _log('BAD', f"[synth] chunk={idx} {label} bad audio ({failures}/{MAX_BAD_AUDIO_RETRIES}): {e}")
            raise

    # 1) Original attempt.
    try:
        return _try_once(text, advanced, 'original'), {'bad_audio_status': 'ok', 'bad_audio_attempts': 0}
    except BadAudioError:
        pass

    if failures >= MAX_BAD_AUDIO_RETRIES:
        _log('WARN', f"[synth] chunk={idx} reached {MAX_BAD_AUDIO_RETRIES} bad-audio events; use short silence")
        return _silence_result()

    # 2) Sanitized whole-text retry.
    retry_text = _sanitize_retry_text(text)
    retry_adv = _safer_retry_advanced(advanced, retry_text, attempt=1)
    if retry_text != text:
        _log('RETRY', f"[synth] chunk={idx} retry sanitized whole text={retry_text!r}")
    try:
        wav = _try_once(retry_text, retry_adv, 'sanitized-whole')
        return wav, {'bad_audio_status': 'repaired', 'bad_audio_attempts': failures, 'bad_audio_reason': last_error}
    except BadAudioError:
        pass

    if failures >= MAX_BAD_AUDIO_RETRIES:
        _log('WARN', f"[synth] chunk={idx} reached {MAX_BAD_AUDIO_RETRIES} bad-audio events; use short silence")
        return _silence_result()

    # 3) Progressive re-splitting.  Each level must fully succeed.  If any
    # sub-chunk fails, discard that level's partial output and try a more
    # aggressive split.  This avoids silently dropping the end of a sentence.
    # Do not stop merely because all predefined split levels were tried once:
    # if the failure budget has not reached 20, keep retrying the most
    # aggressive one-character split with increasingly conservative sampling.
    level = 0
    while failures < MAX_BAD_AUDIO_RETRIES:
        split_level = min(level, _repair_split_level_count() - 1)
        sub_texts = _split_text_for_repair(text, level=split_level)
        if not sub_texts:
            level += 1
            continue
        _log('RETRY', f"[synth] chunk={idx} repair level={split_level} round={level} into {len(sub_texts)} sub-chunk(s): {sub_texts}")
        sub_wavs: list[tuple[str, int]] = []
        level_failed = False
        try:
            for sub_i, sub in enumerate(sub_texts):
                if failures >= MAX_BAD_AUDIO_RETRIES:
                    _log('WARN', f"[synth] chunk={idx} reached {MAX_BAD_AUDIO_RETRIES} bad-audio events during level={split_level}; use short silence")
                    return _silence_result()
                sub_adv = _safer_retry_advanced(advanced, sub, attempt=min(10, level + 2))
                try:
                    sub_wav = _try_once(sub, sub_adv, f"split-L{split_level}[{sub_i+1}/{len(sub_texts)}]")
                    # In emergency character-level mode, the gap should be very
                    # small, otherwise the final audio becomes unnaturally slow.
                    gap_ms = 80 if split_level >= 8 else 160
                    sub_wavs.append((sub_wav, gap_ms if sub_i < len(sub_texts) - 1 else 0))
                except BadAudioError:
                    level_failed = True
                    _log('RETRY', f"[synth] chunk={idx} split level={split_level} failed at sub {sub_i+1}/{len(sub_texts)}; escalate split/retry")
                    break

            if level_failed:
                level += 1
                continue
            if not sub_wavs:
                level += 1
                continue
            fd, merged = tempfile.mkstemp(prefix=f"ttso_{os.getpid()}_{idx}_repairL{split_level}_", suffix='.wav')
            os.close(fd)
            _merge_wavs_variable(sub_wavs, merged)
            dur_ms = _chunk_duration_ms(merged, trimmed=True)
            if dur_ms <= 0:
                last_error = 'repair merge produced empty wav'
                failures += 1
                _log('BAD', f"[synth] chunk={idx} repair level={split_level} merge empty ({failures}/{MAX_BAD_AUDIO_RETRIES})")
                level += 1
                continue
            _log('OK', f"[synth] chunk={idx} repaired by split level={split_level}; failures={failures}")
            return merged, {'bad_audio_status': 'repaired', 'bad_audio_attempts': failures, 'bad_audio_reason': last_error}
        finally:
            # If this level failed, remove its temporary sub-wavs.  On success,
            # the merged file already contains their audio, so they can also be
            # removed safely.
            for p, _gap in sub_wavs:
                try:
                    os.unlink(p)
                except Exception:
                    pass

    # 4) Final fallback only after the full 20 bad-audio budget is exhausted.
    # Do not put known-bad audio into the final audiobook.
    _log('WARN', f"[synth] chunk={idx} reached {MAX_BAD_AUDIO_RETRIES} bad-audio events; use short silence; failures={failures}/{MAX_BAD_AUDIO_RETRIES}")
    return _silence_result()



def _find_output_wav_in_dir(tmp_dir: str, prefix: str) -> str:
    """在 tmp_dir 里找到 generate_audio 实际写出的 wav 文件。
    generate_audio 的命名规则：
      join_audio=True  → {prefix}_joined.wav 或 {prefix}.wav
      join_audio=False → {prefix}_0.wav, {prefix}_1.wav ...
    """
    d = Path(tmp_dir)
    # 优先找 joined 文件
    for pattern in (f"{prefix}_joined.wav", f"{prefix}.wav", "*.wav"):
        hits = sorted(d.glob(pattern), key=lambda p: p.stat().st_size, reverse=True)
        if hits and hits[0].stat().st_size > 44:
            return str(hits[0])
    return str(d / f"{prefix}.wav")  # fallback，让调用方报错


def _save_results_to_wav(results, path: str):
    """把 generate_custom_voice / generate_voice_design 的结果写成 wav"""
    import mlx.core as mx
    from mlx_audio.audio_io import write as audio_write
    segments = []
    for r in results:
        a = r.audio if hasattr(r, "audio") else r
        if hasattr(a, "flatten"):
            a = a.flatten()
        segments.append(a)
    if segments:
        audio = mx.concatenate(segments, axis=0)
        audio_write(path, audio, _get_sample_rate())


def _get_sample_rate() -> int:
    if _model and hasattr(_model, "sample_rate"):
        return _model.sample_rate
    return 24000


def _wav_duration(path: str, sr: int) -> float:
    try:
        with wave.open(path) as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:
        return 0.0


def _chunk_duration_ms(wav_path: str, *, trimmed: bool = False) -> int:
    """返回 wav 文件时长（毫秒）。

    trimmed=True 时，按最终合并时的首尾静音裁剪逻辑估算有效时长，
    使字幕时间戳尽量与最终音频保持一致。
    """
    try:
        import wave as wv
        with wv.open(wav_path) as wf:
            sr = wf.getframerate()
            if not trimmed:
                return int(wf.getnframes() / sr * 1000)
            sampwidth = wf.getsampwidth()
            channels = wf.getnchannels()
            raw = wf.readframes(wf.getnframes())
            raw2 = _trim_edge_silence(raw, sampwidth, sr, channels=channels)
            frame_bytes = max(1, sampwidth * max(1, channels))
            return int((len(raw2) // frame_bytes) / sr * 1000)
    except Exception:
        return 0


def _merge_wavs(paths: list, out_path: str, gap_ms: int = 300):
    """合并多个 wav 文件，中间插入固定静音（兼容旧调用）"""
    _merge_wavs_variable([(p, gap_ms) for p in paths], out_path)


def _trim_edge_silence(frames: bytes, sampwidth: int, sr: int, *, channels: int = 1,
                       threshold: float = 0.010, keep_head_ms: int = 8,
                       keep_tail_ms: int = 60) -> bytes:
    """裁掉 PCM 帧开头和末尾的长静音，并保留少量自然呼吸空间。

    v0.4w: 早期版本用逐采样 peak > 0.003 判断“有声”。Qwen3-TTS 某些 chunk
    开头会带低频噪声、点击声或非常轻的气口，虽然听感上仍是长空白，但会
    被误判为 active，导致开头空白裁不掉。本版改为 20ms 窗口 RMS + 动态阈值，
    并要求连续约 40ms 有效声音才认定为真正开始；尾部同理。

    注意：如果整段都是静音或极低幅度内容，本函数原样返回，避免把 chunk 整段删除。
    """
    if sampwidth not in (2, 4) or not frames:
        return frames
    channels = max(1, int(channels or 1))
    dtype = np.int16 if sampwidth == 2 else np.int32
    maxval = float(np.iinfo(dtype).max)
    arr = np.frombuffer(frames, dtype=dtype)
    if arr.size == 0:
        return frames

    # 按“音频帧”判断静音，避免多声道交错采样导致切点不对齐。
    usable = (arr.size // channels) * channels
    if usable <= 0:
        return frames
    arr2 = arr[:usable].reshape(-1, channels)
    mono = np.max(np.abs(arr2).astype(np.float32) / maxval, axis=1)
    if mono.size == 0:
        return frames

    peak = float(np.max(mono))
    if peak < 0.02:
        return frames          # 全静音/极低幅度，原样保留，避免整段消失

    # 20ms RMS 窗口，比逐采样 peak 更能忽略开头小噪声/轻微点击。
    win = max(1, int(sr * 0.020))
    hop = max(1, int(sr * 0.010))
    if mono.size < win:
        return frames
    starts = np.arange(0, mono.size - win + 1, hop, dtype=np.int64)
    # RMS on absolute signal; mean square of mono amplitude.
    rms = np.empty(starts.size, dtype=np.float32)
    for i, st in enumerate(starts):
        seg = mono[int(st):int(st) + win]
        rms[i] = float(np.sqrt(np.mean(seg * seg)))

    # 动态阈值：绝对阈值负责砍掉低噪声；相对阈值适配整体音量偏小的 chunk。
    # 对峰值很低但实际有声的短句，0.012 仍通常低于语音 RMS；若不满足则回退到较低阈值。
    thr = max(float(threshold), peak * 0.045)
    active = rms > thr
    if not np.any(active):
        thr = max(0.006, peak * 0.025)
        active = rms > thr
    if not np.any(active):
        return frames

    # 要求连续两个窗口（约 40ms 覆盖）有效，避免开头孤立噪点阻止裁剪。
    min_run = 2
    idx = np.where(active)[0]
    first_win = int(idx[0])
    last_win = int(idx[-1])
    if idx.size >= min_run:
        for j in range(0, idx.size - min_run + 1):
            run = idx[j:j + min_run]
            if int(run[-1] - run[0]) == min_run - 1:
                first_win = int(run[0])
                break
        for j in range(idx.size - min_run, -1, -1):
            run = idx[j:j + min_run]
            if int(run[-1] - run[0]) == min_run - 1:
                last_win = int(run[-1])
                break

    first_active = int(starts[first_win])
    last_active = int(min(mono.size, starts[last_win] + win)) - 1
    keep_head_frames = max(0, int(sr * keep_head_ms / 1000))
    keep_tail_frames = max(0, int(sr * keep_tail_ms / 1000))
    start_frame = max(0, first_active - keep_head_frames)
    end_frame = min(arr2.shape[0], last_active + keep_tail_frames + 1)
    if end_frame <= start_frame:
        return frames
    return arr2[start_frame:end_frame].reshape(-1).tobytes()


def _trim_tail_silence(frames: bytes, sampwidth: int, sr: int,
                        threshold: float = 0.003, keep_ms: int = 60) -> bytes:
    """兼容旧调用：只裁尾部静音。新合并逻辑使用 _trim_edge_silence。"""
    return _trim_edge_silence(frames, sampwidth, sr, channels=1,
                              threshold=threshold, keep_head_ms=10**9,
                              keep_tail_ms=keep_ms)


def _merge_wavs_variable(chunks: list, out_path: str):
    """合并多个 wav 文件，每段后插入对应时长的静音。

    v0.4r 修复点：
    1. 不再静默吞掉所有合并异常；任何 chunk 读取失败都会进入日志。
    2. 如果没有可合并的有效音频，直接抛错，而不是返回后仍向前端广播 done。
    3. 写入前确保输出目录存在，并使用 .tmp 原子替换，避免前端读到半写入文件。
    4. 尾部静音裁剪只作用于每段末尾；全静音/低幅度段会原样保留，不会整段删除。
    """
    import wave as wv
    from pathlib import Path as _Path

    parts = []   # list of (frames_bytes, pause_ms)
    sr = 24000
    sampwidth = 2
    channels = 1
    read_errors = []

    for path, pause_ms in chunks:
        if not os.path.exists(path):
            read_errors.append(f"missing chunk: {path}")
            continue
        try:
            with wv.open(path) as wf:
                sr        = wf.getframerate()
                sampwidth = wf.getsampwidth()
                channels  = wf.getnchannels()
                raw = wf.readframes(wf.getnframes())
                if not raw:
                    read_errors.append(f"empty chunk: {path}")
                    continue
                # 裁掉模型输出的首尾长静音，只保留少量自然呼吸空间，
                # 再使用受控的人工停顿连接各段。
                # _trim_edge_silence 对“全静音/极低幅度”会原样返回，因此不会把整段删除。
                trimmed = _trim_edge_silence(raw, sampwidth, sr, channels=channels)
                if not trimmed:
                    read_errors.append(f"trimmed to empty: {path}")
                    continue
                parts.append((trimmed, pause_ms))
        except Exception as e:
            read_errors.append(f"failed to read chunk {path}: {e}")
            continue

    if read_errors:
        print("[worker] merge warnings: " + " | ".join(read_errors), flush=True)

    if not parts:
        raise RuntimeError("No valid audio chunks to merge; " + "; ".join(read_errors[:5]))

    combined = b""
    frame_bytes = max(1, sampwidth * max(1, channels))
    for i, (frames, pause_ms) in enumerate(parts):
        combined += frames
        # 最后一段不加尾部静音
        if i < len(parts) - 1:
            n_silence_frames = max(0, int(sr * pause_ms / 1000))
            combined += b"\x00" * (n_silence_frames * frame_bytes)

    out = _Path(out_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_name(out.name + ".tmp")
    try:
        with wv.open(str(tmp_out), "w") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sampwidth)
            wf.setframerate(sr)
            wf.writeframes(combined)
        if not tmp_out.exists() or tmp_out.stat().st_size <= 44:
            raise RuntimeError(f"merged tmp file is empty: {tmp_out}")
        os.replace(str(tmp_out), str(out))
    finally:
        try:
            if tmp_out.exists():
                tmp_out.unlink()
        except Exception:
            pass


# ── 声音克隆 ──────────────────────────────────────────────────
def _handle_clone(task: dict, result_q):
    tid        = task["id"]
    name       = task.get("name", "clone")
    audio_path = task.get("audio_path", "")

    if not audio_path or not Path(audio_path).exists():
        result_q.put({"id": tid, "ok": False, "error": "音频文件不存在"})
        return
    if _model_type != "base":
        result_q.put({"id": tid, "ok": False,
                       "error": "声音克隆仅支持 Base 模型"})
        return

    _cloned_voices[name] = {"audio_path": audio_path}
    result_q.put({
        "id": tid, "ok": True, "type": "clone_done",
        "voice_id": f"__clone__{name}", "name": name,
    })


def _handle_create_anchor(task: dict, result_q):
    """Create a role anchor by synthesizing one stable reference WAV.

    This is intentionally model-side only: server persists the returned WAV as a
    clone reference and the Base model later reuses it as the character anchor.
    """
    tid = task.get("id", "")
    if _model_type not in ("custom_voice", "voice_design"):
        result_q.put({
            "id": tid, "ok": False, "type": "anchor_done",
            "error": "角色锚定样本需要先加载 CustomVoice 或 VoiceDesign 模型。"
        })
        return
    name = str(task.get("name") or "角色锚点").strip() or "角色锚点"
    sample_text = str(task.get("sample_text") or "窗外的光线逐渐变亮，房间里安静而清晰。").strip()
    # Avoid leakage from old/default anchor samples.  Some TTS clone models may
    # occasionally speak part of the reference text before the target text when
    # the reference sample contains dialogue-like continuation phrases.
    _unsafe_anchor_samples = {
        "我知道很多事情已经无法挽回，但我还是想把这句话说完。": "夜色慢慢沉下来，风从很远的地方吹过。",
        "你好，这是用于固定角色音色的参考样本。": "窗外的光线逐渐变亮，房间里安静而清晰。",
        "你好，这是用于固定角色平静音色的参考样本。请保持平稳、自然、清晰的表达。": "窗外的光线逐渐变亮，房间里安静而清晰。",
    }
    sample_text = _unsafe_anchor_samples.get(sample_text, sample_text)
    source_speaker = str(task.get("speaker") or "").strip()
    instruction = str(task.get("instruction") or "").strip()
    emotion = str(task.get("emotion") or "neutral").strip()
    emotion_label = str(task.get("emotion_label") or emotion).strip()
    speed = float(task.get("speed", 1.0) or 1.0)
    advanced = dict(task.get("advanced") or {})
    if instruction:
        advanced["style_instruct"] = instruction
    # For anchors, stable sampling is more important than expressiveness.
    advanced.setdefault("temperature", 0.2)
    advanced.setdefault("top_p", 0.85)
    advanced.setdefault("top_k", 20)
    try:
        wav_path = _synth_chunk_to_file(sample_text, source_speaker, speed, advanced, int(time.time()) % 100000)
        result_q.put({
            "id": tid, "ok": True, "type": "anchor_done",
            "name": name, "audio_path": wav_path,
            "source_speaker": source_speaker, "instruction": instruction, "emotion": emotion, "emotion_label": emotion_label,
        })
    except Exception as e:
        result_q.put({
            "id": tid, "ok": False, "type": "anchor_done",
            "name": name, "error": str(e), "trace": traceback.format_exc(),
        })


# ── 文本工具 ──────────────────────────────────────────────────
def _is_chinese(text: str) -> bool:
    zh = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    return zh / max(len(text), 1) > 0.2


def _pause_for_ending(text: str, base_gap_ms: int,
                       is_paragraph_end: bool = False) -> int:
    """根据段落末尾标点及是否段落边界决定停顿时长（ms）。
    规则（优先级从高到低）：
      段落末尾（无论有无标点）   → base_gap_ms * 2（段落停顿）
      省略号 结尾                → base_gap_ms * 1.5
      句号/感叹号/问号 结尾      → base_gap_ms（标准句间停顿）
      括号闭合 结尾              → base_gap_ms * 0.6
      逗号/分号/顿号 结尾        → base_gap_ms * 0.4
      其他（含无标点句中截断）   → base_gap_ms * 0.3
    """
    t = text.rstrip()
    if not t:
        return base_gap_ms

    # 段落末尾——最高优先级，无论末尾是什么标点
    if is_paragraph_end:
        return int(base_gap_ms * 2)

    last = t[-1]
    if last in '…':
        return int(base_gap_ms * 1.5)
    if last in '。！？!?':
        return base_gap_ms
    if last in '）】》)\]」』':
        return int(base_gap_ms * 0.6)
    if last in '，,；;、':
        return int(base_gap_ms * 0.4)
    return int(base_gap_ms * 0.3)


# 英文缩写句点集合，这些 . 后面不是句子边界
_EN_ABBREVS = {
    # 称谓
    "mr","mrs","ms","miss","dr","prof","rev","sr","jr","st","gen","sgt","cpl","pvt",
    "capt","lt","col","maj","brig","adm","pres","gov","sen","rep","hon","atty","supt",
    # 地名/通用
    "ave","blvd","rd","st","dept","est","approx","misc","etc","vs","cf","seq",
    "jan","feb","mar","apr","jun","jul","aug","sep","sept","oct","nov","dec",
    # 学术/单位
    "no","vol","pp","ed","eds","fig","eq","al","ibid","op","cit","viz","ca","approx",
    "ft","lb","lbs","oz","kg","km","cm","mm","mt","pt","qt","gal",
    # 公司/组织
    "co","corp","inc","ltd","llc","assn","assoc","dept","div",
    # 其他常见
    "i.e","e.g","etc","p.s","a.m","p.m","u.s","u.k","ph.d","m.d","b.a","m.a",
}

def _is_sentence_boundary(text: str, dot_pos: int) -> bool:
    """判断 text[dot_pos] 处的 . 是否是真正的句子结束。"""
    if dot_pos < 0 or dot_pos >= len(text):
        return False
    ch = text[dot_pos]
    if ch not in '.!?':
        return True  # !? 总是句子结束

    if ch != '.':
        return True

    # 检查 . 前面的词是否是缩写
    # 找到 . 前的单词
    start = dot_pos - 1
    while start >= 0 and text[start].isalpha():
        start -= 1
    word = text[start+1:dot_pos].lower()

    if not word:
        return False  # 孤立的点，不算句子结束

    if word in _EN_ABBREVS:
        return False  # 缩写，不算句子结束

    # 单个大写字母（首字母缩写如 U.S.A.）
    if len(word) == 1 and word.isupper():
        return False

    # . 后面紧跟大写字母+点（如 U.S.A.）→ 不是句子结束
    after = dot_pos + 1
    if after < len(text) and text[after].isupper():
        # 再往后看，若还是字母+点的模式，是缩写
        j = after
        while j < len(text) and text[j].isalpha():
            j += 1
        if j < len(text) and text[j] == '.':
            return False

    # . 后面跳过空格
    after = dot_pos + 1
    while after < len(text) and text[after] == ' ':
        after += 1

    # . 后面如果是小写字母，通常不是句子结束（如 "e.g. something"）
    if after < len(text) and text[after].islower():
        return False

    # . 后面是数字（如版本号 v1.2.3）
    if after < len(text) and text[after].isdigit():
        return False

    # 单个大写字母后接 . 且后面还有大写字母（如 U.S.）
    if len(word) == 1 and after < len(text) and text[after].isupper():
        return False

    return True


def _split_english_sentences(text: str) -> list:
    """将英文文本按真实句子边界切分，返回句子列表（含末尾标点）。"""
    import re
    sentences = []
    cur = ""
    i = 0
    while i < len(text):
        ch = text[i]
        cur += ch
        if ch in '.!?' and _is_sentence_boundary(text, i):
            # 消耗连续的结束标点和空格
            j = i + 1
            while j < len(text) and text[j] in '.!? \'"）)':
                cur += text[j]
                j += 1
            sentences.append(cur.strip())
            cur = ""
            i = j
        else:
            i += 1
    if cur.strip():
        sentences.append(cur.strip())
    return [s for s in sentences if s]


def _split_long_sentence(s: str, max_chars: int) -> list:
    """将超长句子切成不超过 max_chars 的片段。
    - 中文：按字符截断
    - 英文：优先在句子边界截断，其次在单词边界截断
    """
    if len(s) <= max_chars:
        return [s]

    zh_count = sum(1 for c in s if '\u4e00' <= c <= '\u9fff')
    is_mainly_chinese = zh_count / max(len(s), 1) > 0.2

    if is_mainly_chinese:
        return [s[i:i + max_chars] for i in range(0, len(s), max_chars)]

    # 英文：先尝试在句子边界切
    sents = _split_english_sentences(s)
    if len(sents) > 1:
        # 合并规则同 _split_text：只有加进来不超限才合并，否则先断开
        pieces = []
        cur = ""
        for sent in sents:
            # 单句本身超限，先强制按词边界切
            if len(sent) > max_chars:
                if cur:
                    pieces.append(cur)
                    cur = ""
                words = sent.split(' ')
                wcur = ""
                for w in words:
                    if not wcur:
                        wcur = w
                    elif len(wcur) + 1 + len(w) <= max_chars:
                        wcur += ' ' + w
                    else:
                        pieces.append(wcur)
                        wcur = w
                if wcur:
                    pieces.append(wcur)
                continue
            if not cur:
                cur = sent
            elif len(cur) + 1 + len(sent) <= max_chars:
                cur += " " + sent
            else:
                pieces.append(cur)
                cur = sent
        if cur:
            pieces.append(cur)
        return pieces

    # 无句子边界：按单词边界截断，绝不在词中截断
    words = s.split(' ')
    pieces = []
    cur = ""
    for word in words:
        if not cur:
            cur = word  # 单词本身超限也整词保留
        elif len(cur) + 1 + len(word) <= max_chars:
            cur += ' ' + word
        else:
            pieces.append(cur)
            cur = word  # 单词本身超限也整词保留
    if cur:
        pieces.append(cur)
    return pieces


def _split_text(text: str, max_chars: int = 250) -> list:
    """
    分段规则：
    1. 先按自然段落（双换行）分成段落
    2. 每段按句子/括号边界再切分句子
    3. 将句子贪心合并：只要累计不超过 max_chars 就继续追加
    4. 单句超过 max_chars 时按 max_chars 强制截断（英文按词边界，中文按字符）
    返回 (text, is_paragraph_end) 元组列表。
    is_paragraph_end=True 表示该 chunk 是某个自然段落的最后一段，
    无论末尾有无标点都应插入双倍停顿。
    调用前已由 _handle_tts 根据语言设定合适的 max_chars：
      中文 → 100，英文 → 450，Base 模型更保守。
    """
    import re

    # 按自然段落分（双换行或更多）
    # 单个或多个连续换行都视为段落分隔
    paragraphs = re.split(r'\n+', text.strip())
    result = []  # list of (text, is_paragraph_end)

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # 段内按句子/括号边界拆分，保留标点
        # 中文用正则切句，英文用更准确的 _split_english_sentences
        zh_count_para = sum(1 for c in para if '\u4e00' <= c <= '\u9fff')
        if zh_count_para / max(len(para), 1) > 0.2:
            raw = re.split('(?<=[。！？!?…\n）)\\]】》」』])', para)
        else:
            raw = _split_english_sentences(para)
        sentences = []
        for s in raw:
            s = s.strip()
            if not s:
                continue
            if len(s) > max_chars:
                for piece in _split_long_sentence(s, max_chars):
                    if piece:
                        sentences.append(piece)
            else:
                sentences.append(s)

        # 合并规则：
        # 第一优先：在句子边界断开（每句默认独立）
        # 允许合并的唯一条件：cur + separator + 下一句 ≤ max_chars
        # 中文句子间无分隔，英文句子间加空格
        is_zh_para = zh_count_para / max(len(para), 1) > 0.2
        sep = "" if is_zh_para else " "
        chunks = []
        cur = ""
        for sent in sentences:
            if not cur:
                cur = sent
            elif len(cur) + len(sep) + len(sent) <= max_chars:
                # 只有加进来不超限，才合并
                cur += sep + sent
            else:
                # 超限：先输出当前，新句子重新开始
                chunks.append(cur)
                cur = sent
        if cur:
            chunks.append(cur)

        # 标记段落末尾
        for i, chunk in enumerate(chunks):
            is_para_end = (i == len(chunks) - 1)
            result.append((chunk, is_para_end))

    return [(t, p) for t, p in result if t.strip()]
