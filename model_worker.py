"""
Lancer1911 TTS Offline v0.1 — 模型工作进程
使用 mlx_audio (https://github.com/Blaizzy/mlx-audio) 驱动 Qwen3-TTS MLX 推理。
"""
from __future__ import annotations
import os, time, traceback, tempfile, wave, struct
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


def worker_main(task_q, result_q, model_repo: str, device: str = "mlx"):
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
def _handle_tts(task: dict, result_q):
    tid        = task["id"]
    text       = task["text"]
    voice_id   = task.get("voice_id", "")
    speed      = float(task.get("speed", 1.0))
    out_path   = task.get("output_path", "")
    advanced   = task.get("advanced", {})
    dialog_rows = task.get("dialog_rows") or []
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

    # 普通文本：整体分段；多人对话 CSV：按每行 speaker/instruction/text 分段。
    chunk_items = []
    if isinstance(dialog_rows, list) and dialog_rows:
        if _model_type != "custom_voice":
            result_q.put({"id": tid, "type": "done", "ok": False,
                           "error": "多人对话 CSV 仅支持 CustomVoice 模型"})
            return
        for row_idx, row in enumerate(dialog_rows):
            if not isinstance(row, dict):
                continue
            row_text = str(row.get("text") or "").strip()
            if not row_text:
                continue
            row_speaker = str(row.get("speaker") or voice_id or "").strip()
            row_instruction = str(row.get("instruction") or "").strip()
            for chunk_text, is_para_end in _split_text(row_text, chunk_size):
                if chunk_text.strip():
                    chunk_items.append({
                        "text": chunk_text,
                        "is_para_end": is_para_end,
                        "speaker": row_speaker,
                        "instruction": row_instruction,
                        "row_index": row_idx,
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
                })

    base_gap = int(advanced.get("silence_gap_ms", 300))
    total  = len(chunk_items)
    result_q.put({"id": tid, "type": "progress", "stage": "start",
                  "total_chunks": total})

    chunk_wavs = []  # list of (wav_path, pause_ms)
    _audio_cursor_ms = 0  # 用于时间戳累计

    for i, item in enumerate(chunk_items):
        chunk = item["text"]
        is_para_end = item.get("is_para_end", False)
        row_speaker = item.get("speaker") or voice_id
        row_instruction = item.get("instruction") or ""
        row_idx = item.get("row_index")
        t0 = time.time()
        try:
            row_adv = dict(advanced or {})
            if row_instruction:
                row_adv["style_instruct"] = row_instruction
            wav_path = _synth_chunk_to_file(chunk, row_speaker, speed, row_adv, i)
            pause_ms = _pause_for_ending(chunk, base_gap, is_paragraph_end=is_para_end)
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
        dur_ms = _chunk_duration_ms(wav_path)
        result_q.put({
            "id": tid, "type": "progress", "stage": "chunk",
            "chunk_idx": i, "total_chunks": total,
            "row_index": row_idx,
            "speaker": row_speaker if row_idx is not None else "",
            "instruction": row_instruction if row_idx is not None else "",
            "text": chunk,
            "text_preview": chunk,
            "pause_ms": pause_ms,
            "elapsed": elapsed,
            "audio_start_ms": _audio_cursor_ms,   # 该段在最终音频中的起始时间
            "audio_end_ms":   _audio_cursor_ms + dur_ms + pause_ms,
        })
        _audio_cursor_ms += dur_ms + pause_ms

    if not chunk_wavs:
        result_q.put({"id": tid, "type": "done", "ok": False,
                       "error": "No audio generated"})
        return

    # 合并所有分段 wav，每段间使用对应的停顿时长
    _merge_wavs_variable(chunk_wavs, out_path)

    # 清理临时文件
    for path, _ in chunk_wavs:
        try:
            if path != out_path:
                os.unlink(path)
        except Exception:
            pass

    sr       = _get_sample_rate()
    duration = _wav_duration(out_path, sr)
    result_q.put({
        "id": tid, "type": "done", "ok": True,
        "output_path": out_path,
        "duration": round(duration, 2),
        "sample_rate": sr,
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
    _CUSTOM_VOICES = {"Chelsie","Cherry","Serena","Vivian","Aura","Ethan","Ryan","Leo","Daniel"}

    # ── Base 模型 ────────────────────────────────────────────
    if _model_type == "base":
        ref_audio = ""
        ref_text  = ""
        if voice_id == "base_default":
            raise RuntimeError(
                "Please clone a voice first: go to the Clone tab, upload 5–30 s of "
                "reference audio, then select the cloned voice here.\n"
                "（请先克隆音色：在「克隆」标签页上传 5~30 秒参考音频，完成后选择克隆音色再合成。）"
            )
        if voice_id.startswith("__clone__"):
            clone_name = voice_id[len("__clone__"):]
            info = _cloned_voices.get(clone_name, {})
            ref_audio = info.get("audio_path", "")
            ref_text  = info.get("ref_text", "")

        if not ref_audio:
            raise RuntimeError(
                "Base model requires reference audio. "
                "Please upload reference audio in the Clone tab, or switch to a CustomVoice model.\n"
                "（Base 模型需要参考音频才能合成。请在「克隆」标签页上传参考音频，或切换到 CustomVoice 模型。）"
            )

        # ref_text 为空时不传，mlx-audio 会自动用 ASR 推断
        kwargs = dict(
            text=text,
            lang_code=lang,
            speed=speed,
            ref_audio=ref_audio,
        )
        if ref_text.strip():
            kwargs["ref_text"] = ref_text

        # generate() 返回生成器 → list() 完整消费
        results = _call_tts_generator(_model.generate, _generation_kwargs(kwargs, advanced))

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
        instruct = advanced.get("style_instruct", "") or "Speak naturally and clearly."
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
    if not segments:
        raise RuntimeError("generate() returned no audio segments")
    import numpy as np
    arrays = []
    for seg in segments:
        arr = np.array(seg, dtype=np.float32).flatten()
        if arr.size == 0:
            continue
        # 归一化：如果峰值超出 [-1,1] 说明是整数编码
        peak = float(np.abs(arr).max())
        if peak > 1.5:
            arr = arr / peak
        arrays.append(arr)
    if not arrays:
        raise RuntimeError("All audio segments were empty")
    audio_np = np.concatenate(arrays)

    # 用 wave 模块写 PCM WAV，绕过 audio_write 的值域问题
    fd, tmp_wav = tempfile.mkstemp(
        prefix=f"ttso_{os.getpid()}_{idx}_",
        suffix=".wav"
    )
    os.close(fd)
    sr = _get_sample_rate()
    pcm = (np.clip(audio_np, -1.0, 1.0) * 32767).astype(np.int16)
    import wave as _wave
    with _wave.open(tmp_wav, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    print(f"[worker] chunk {idx}: {len(audio_np)/sr:.2f}s  peak={float(np.abs(audio_np).max()):.3f}", flush=True)
    return tmp_wav


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


def _chunk_duration_ms(wav_path: str) -> int:
    """返回 wav 文件时长（毫秒）"""
    try:
        import wave as wv
        with wv.open(wav_path) as wf:
            return int(wf.getnframes() / wf.getframerate() * 1000)
    except Exception:
        return 0


def _merge_wavs(paths: list, out_path: str, gap_ms: int = 300):
    """合并多个 wav 文件，中间插入固定静音（兼容旧调用）"""
    _merge_wavs_variable([(p, gap_ms) for p in paths], out_path)


def _merge_wavs_variable(chunks: list, out_path: str):
    """合并多个 wav 文件，每段后插入对应时长的静音。
    chunks: list of (wav_path, pause_ms_after)
    最后一段不插入静音。
    """
    import wave as wv
    parts = []   # list of (frames_bytes, pause_frames * sampwidth)
    sr = 24000
    sampwidth = 2

    for path, pause_ms in chunks:
        if not os.path.exists(path):
            continue
        try:
            with wv.open(path) as wf:
                sr        = wf.getframerate()
                sampwidth = wf.getsampwidth()
                parts.append((wf.readframes(wf.getnframes()), pause_ms))
        except Exception:
            continue

    if not parts:
        return

    combined = b""
    for i, (frames, pause_ms) in enumerate(parts):
        combined += frames
        # 最后一段不加尾部静音
        if i < len(parts) - 1:
            n_silence = max(0, int(sr * pause_ms / 1000)) * sampwidth
            combined += b"\x00" * n_silence

    with wv.open(out_path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sr)
        wf.writeframes(combined)


# ── 声音克隆 ──────────────────────────────────────────────────
def _handle_clone(task: dict, result_q):
    tid        = task["id"]
    name       = task.get("name", "clone")
    audio_path = task.get("audio_path", "")
    ref_text   = task.get("ref_text", "")

    if not audio_path or not Path(audio_path).exists():
        result_q.put({"id": tid, "ok": False, "error": "音频文件不存在"})
        return
    if _model_type != "base":
        result_q.put({"id": tid, "ok": False,
                       "error": "声音克隆仅支持 Base 模型"})
        return

    _cloned_voices[name] = {"audio_path": audio_path, "ref_text": ref_text}
    result_q.put({
        "id": tid, "ok": True, "type": "clone_done",
        "voice_id": f"__clone__{name}", "name": name,
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
