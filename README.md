🌐 [中文说明](README-ZH.md)

# Lancer1911 TTS Offline

> Fully offline, multi-voice, emotion-controllable Text-to-Speech synthesis  
> Built for Apple Silicon — designed for audiobooks, multi-character dialogue, narration, and long-form local batch synthesis

![Platform](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon%20only-black?logo=apple)
![RAM](https://img.shields.io/badge/RAM-16%20GB%20minimum-red)
![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python)
![MLX](https://img.shields.io/badge/MLX-local%20inference-orange)
![Version](https://img.shields.io/badge/version-0.5w-informational)
![License](https://img.shields.io/badge/license-Apache--2.0-green)

---

## 🎬 Demo Video

<p align="center">
  <a href="https://lancer1911.github.io/videos/lancer1911-tts-offline.mp4" target="_blank">
    <img src="https://lancer1911.github.io/images/tts-offline-demo-cover.png"
         alt="TTS-Offline Demo Video"
         width="800">
  </a>
  <br>
  <em>TTS-Offline demo — local text-to-speech, multi-character dialog, and audiobook production workflow</em>
  <br>
  <a href="https://lancer1911.github.io/videos/lancer1911-tts-offline.mp4">
    ▶ Open demo video
  </a>
</p>

---

## ⚠️ Hardware Requirements

Lancer1911 TTS Offline runs Qwen3-TTS entirely on your Mac via the MLX framework. Audio synthesis data never leaves your machine, but all model weights, KV cache, and intermediate audio buffers must fit in local unified memory.

|  | Minimum | Recommended |
|---|---|---|
| **Chip** | Apple M1 | M2 Pro / M3 / M4 or later |
| **Unified Memory** | **16 GB** | **32 GB** |
| **Storage** | 5 GB free | 15 GB free (multiple models) |
| **macOS** | 13 Ventura | 14 Sonoma or later |

> **Why 16 GB?** The default model is `Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit` (~1.7 GB weights). At runtime, MLX allocates additional memory for the KV cache, audio buffers, output WAV data, and the pywebview / FastAPI stack. A 16 GB Mac can handle most synthesis tasks comfortably. The lightweight 0.6B variant requires even less. Voice cloning adds a small overhead for the reference audio embedding. Long documents or very large dialog tables with many speakers may require more headroom.

---

## Screenshots

<p align="center">
  <img src="images/screenshot_main.png" alt="Main interface — plain text synthesis" width="800">
  <br><em>Main interface — plain text synthesis with voice selection and playback</em>
</p>
<p align="center">
  <img src="images/screenshot_main_en.png" alt="Dialog Table mode — multi-character audiobook production" width="800">
  <br><em>Dialog Table mode — per-line voice, emotion instruction, and text for audiobook production</em>
</p>
<p align="center">
  <img src="images/screenshot_base_csv.png" alt="Base model CSV dialog mode — multi-character audiobook with cloned voices" width="800">
  <br><em>Base model CSV dialog mode — four-column script (character / voice / emotion / text) with cloned and anchored voices for multi-character audiobook production</em>
</p>

### 🎧 Sample: *The Murder on the Links* Chapter 1 (Base model, multi-character)

The following sample was produced entirely with the Base model using a four-column CSV script. Each character is voiced by a different cloned or anchored voice; emotion instructions are applied per line.

<p align="center">
  <audio controls style="width:680px">
    <source src="samples/sample-Murder_on_the_Links_Ch1.m4a" type="audio/mp4">
    Your browser does not support the audio element.
    <a href="samples/sample-Murder_on_the_Links_Ch1.m4a">⬇ Download sample audio</a>
  </audio>
  <br>
  <a href="samples/sample-Murder_on_the_Links_Ch1.csv">📄 View CSV script</a>
</p>

<p align="center">
  <img src="images/screenshot_playback.png" alt="Playback bar with follow and reverse-follow" width="800">
  <br><em>Playback bar — follow mode highlights the current sentence; click any line to seek</em>
</p>
<p align="center">
  <img src="images/screenshot_settings.png" alt="Model tab and advanced settings" width="480">
  <br><em>Model tab and Advanced Settings — model selection, output format, and TTS parameters</em>
</p>
<p align="center">
  <img src="images/installation.png" alt="Installation in macOS" width="600">
  <br><em>Installation in macOS</em>
</p>

---

## What Can It Do?

Lancer1911 TTS Offline is more than a text-to-speech converter. Its dialog table mode enables **sentence-level, per-character, per-emotion production** of long-form audio — a capability that most commercial TTS tools charge premium rates for:

- Assign a different **voice** to each line of dialogue
- Provide a free-form **emotion or direction instruction** per line (e.g. "speak with quiet urgency", "cheerful but restrained", "authoritative and slow")
- Load the script as a **CSV**, synthesize the full audiobook or drama, then export with per-segment timestamps as SRT / JSON
- **Click any line** in the playback timeline to jump to the exact position — or click any sentence in the text to seek the player
- **Follow mode** keeps the highlighted sentence in view while the audio plays; reverse follow mode works in both directions
- Save the entire session (script + voices + settings + timestamps) as a `.ttso` file and reload it later

This makes the app well suited for:

| Use Case | Details |
|---|---|
| Audiobooks | Full multi-character cast, per-sentence pacing and emotion |
| Drama / podcast scripts | Each speaker line gets its own voice and director note |
| Narration with character voices | Mix narrator voice with character voices in one pass |
| Long-form content creation | Process entire books or scripts in a single session |
| Voice prototyping | Test how different voices and emotions sound before recording |

---

## Recent Updates (v0.5w)

Compared with the older README, the current version adds major stability and workflow improvements for long-form, multi-character, and English Base-model synthesis:

- **Bad-audio detection and repair** — Detects low-frequency whine, drone tails, suspicious duration, peak and ZCR patterns. Failed chunks are retried and progressively re-split by sentence, phrase, word group, or smaller emergency units before final merge.
- **Improved English Base-mode reliability** — English synthesis now uses word/duration-based estimation, shorter English chunks, tighter sampling parameters, guarded tail-cropping for cloned voices, and faster failure handling.
- **Persistent cloned and anchored voices** — Clone and anchor metadata is saved to `~/.tts_offline_clone_voices/init.json`, so voices are restored immediately after restart without repeated imports or waiting for worker registration.
- **Python-side microphone recording** — The Clone tab can record reference audio through the system microphone using Python `sounddevice`, avoiding WKWebView restrictions on `navigator.mediaDevices`. Device selection and up to 30 seconds of recording are supported.
- **Four-column CSV dialog scripts** — Base mode supports Character / Voice / Emotion / Text scripts, making multi-character audiobook and drama production more stable and explicit.
- **Role Anchor voices** — Generate reusable anchors from CustomVoice / VoiceDesign voices; create Chinese anchors by default and optional English anchors; import/export `.ttscx` anchor packages.
- **Direct English anchor generation** — English anchors are generated from the selected original speaker and emotion instead of one shared neutral reference, preserving speaker and emotion differences.
- **Automatic language-aware anchor selection in Base CSV mode** — English lines prefer matching `- English` anchors for the same character/emotion; otherwise the app falls back to `- Chinese` or another related source voice.
- **SRT timeline synthesis** — Imported SRT files are aligned to their original timestamps by inserting silence. If generated audio overruns the next subtitle, content is not truncated; the app continues and warns the user to adjust speed if needed.
- **Stricter file-entry filtering** — The main text upload accepts only TXT / MD / DOCX / SRT / PDF / EPUB; CSV is loaded only from Dialog Table; clone/anchor package importers accept only their intended formats.

## Features

- **Fully offline** — Synthesis runs locally on MLX. Your text and audio never leave your Mac.
- **9 built-in voices** — Serena, Vivian, Uncle Fu, Ryan, Alden, Ono Anna, Sohee, Eric, Dylan — covering multiple genders, accents, and styles.
- **CustomVoice / Base / VoiceDesign model families** — Supports built-in voices, reference-audio cloning, role anchors, and experimental voice-design workflows.
- **Voice cloning** — Upload or record 5–30 seconds of reference audio. Base models synthesize with the cloned speaker. New clones are saved as `- Chinese` by default and can derive a matching `- English` voice.
- **Python-side microphone recording** — The Clone tab can select an input device and record reference audio directly, including in packaged macOS apps.
- **Role Anchor voices** — Generate a stable sample from the current CustomVoice / VoiceDesign speaker and save it as a reusable Base-mode anchor. Batch anchoring, Chinese anchors, optional English anchors, and `.ttscx` import/export are supported.
- **Differentiated English anchor generation** — English anchors are generated per speaker and per one of 12 emotions, avoiding the “all English anchors sound the same” problem.
- **Four-column dialog table mode** — Supports Character / Voice / Emotion / Text CSV scripts. Character binds the role, Voice controls the actual synthesis voice, and Emotion gives per-line direction.
- **Language-aware Base CSV voice selection** — English lines prefer a matching `- English` anchor for the same character/emotion; Chinese and Japanese lines prefer `- Chinese` anchors.
- **Plain text mode** — Paste or upload text for single-voice synthesis with automatic chunking and paragraph handling.
- **Wide input format support** — TXT, Markdown, DOCX, SRT, PDF, EPUB. The main upload entry rejects CSV, JSON, PPTX, TTSCX, and other non-text workflow files.
- **SRT timeline synthesis** — Imported SRT files can preserve their original timing by inserting silence. If generated audio exceeds the next subtitle timestamp, content is not truncated.
- **Bad-audio detection and automatic repair** — Detects low-frequency whine, drone tails, truncation, and failed chunks; retries, progressively re-splits, and only falls back to silence when repair fails.
- **WAV and MP3 output** — Choose the output format; MP3 requires `ffmpeg`.
- **Per-segment timestamps** — Every synthesized chunk is tagged with its start and end time in the final audio. Exportable as SRT, TXT, Markdown, JSON, or CSV.
- **Playback with dual follow modes** — During or after synthesis, play back the result. Follow mode highlights the current sentence; reverse follow mode lets you click any sentence or dialog row to seek the player.
- **Session files (.ttso)** — Save and restore the full state: script, voice selection, settings, timestamps, and playback state.
- **Advanced TTS parameters** — Temperature, Top-P, Top-K, max tokens, chunk size, silence gap, fade duration, and pitch shift — all accessible from the Advanced Settings panel with built-in and custom presets.
- **Live log panel** — Expand logs to inspect chunk progress, bad-audio detection, retry/repair events, and model-worker output.
- **Model selector** — Switch between CustomVoice, Base, and VoiceDesign model families; the UI detects which models are installed locally.
- **Subtitle export** — Export SRT, TXT, Markdown, JSON, or Excel-compatible CSV.
- **Bilingual UI** — Switch between Chinese and English with the top-bar language button.
- **Dark / light theme** — Toggle appearance from the top bar.

---

## Quick Start

### 1. Clone or unpack the project

```bash
git clone https://github.com/lancer1911/TTS-Offline.git
cd TTS-Offline
```

If you are using a ZIP distribution, unzip it and enter the project folder.

### 2. Install system dependencies

```bash
brew install ffmpeg
```

`ffmpeg` is required for MP3 encoding. WAV output works without it.

### 3. Create the runtime environment

```bash
python3 -m venv ~/tts-offline-env
source ~/tts-offline-env/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

### 4. Download a TTS model

All models are from the `mlx-community` organization on HuggingFace. Use `hf download` (requires `huggingface_hub`):

```bash
# Recommended default — 9 built-in voices, emotion instructions
hf download mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit

# Lightweight alternative — faster, lower memory
# hf download mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit

# Voice cloning — no built-in voices; requires reference audio
# hf download mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit
```

> **China users:** prepend `HF_ENDPOINT=https://hf-mirror.com` to use the mirror:
> ```bash
> HF_ENDPOINT=https://hf-mirror.com hf download mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit
> ```

Models are cached under `~/.cache/huggingface/hub/` and work fully offline after download.

> If `hf` is not found: `pip install -U huggingface_hub`

### 5. Launch

```bash
source ~/tts-offline-env/bin/activate
python main.py
```

The app starts a local FastAPI service and opens a pywebview desktop window at:

```
http://127.0.0.1:17435
```

First launch may take 10–30 seconds while the model loads into memory. The status bar will show "Ready" when synthesis is available.

---

## Basic Workflow

### Plain text synthesis

1. Select a voice in the **Voice** tab (left sidebar).
2. Paste or type text in the main editor, or drag and drop a TXT / MD / DOCX / SRT / PDF / EPUB file.
3. Adjust speed in the sidebar slider.
4. Click **▶ Synthesize**.
5. When synthesis completes, the playback bar appears. Click **⬇ Download** to save the audio.

### Multi-character audiobook (four-column Dialog Table)

1. Click **Dialog Table** to switch to the table view.
2. Each row represents one speech segment. The recommended format is now four columns:
   - **Character** — binds the role identity, such as `Narrator`, `Ye Wenjie`, or `Poirot`.
   - **Voice** — the actual synthesis voice: built-in, cloned, or anchored.
   - **Emotion** — optional emotion or direction instruction, such as `sad and slow`, `whispering`, `happy`, or `angry`.
   - **Text** — the line to synthesize.

3. Add rows with **+ Row**, or import a CSV with **Load CSV**.

   Recommended CSV format (header optional):
   ```csv
   Character,Voice,Emotion,Text
   Narrator,Uncle Fu,calm,Once upon a time in a quiet village…
   Ryan,Ryan,whispering,We need to leave. Now.
   Vivian,Vivian,warm,Everything will be fine.
   ```

4. Click **▶ Synthesize**. Each row is synthesized in order and concatenated into a single audio file with per-segment timestamps.
5. After synthesis, click any row to jump to that segment in playback (**reverse follow**). Enable **Follow** to scroll and highlight the current row while audio plays.
6. In Base mode, if the same character has both `- English` and `- Chinese` anchors, the app automatically prefers the anchor that better matches the current line language.

### SRT timeline synthesis

1. Upload an `.srt` file from the main text upload entry.
2. The app reads each subtitle start time and inserts silence where needed to preserve the original timeline.
3. If a generated segment is longer than the original subtitle interval, the audio is not truncated; synthesis continues in order. If the timeline catches up later, silence insertion resumes.
4. If the final audio significantly overruns the original timeline, the app warns the user to increase speed, shorten text, or adjust the subtitles.

### Voice cloning and Role Anchors

#### Voice cloning

1. Go to the **Clone** tab.
2. Enter a **Clone Name**. Reference text can usually be left blank, especially when the reference audio itself is clear.
3. Upload a WAV / MP3 reference file, or use the microphone button to record 5–30 seconds of reference audio.
4. Click **Start Clone**. The app saves a Chinese clone card, named `Name - Chinese` by default, and may derive a matching `Name - English` card.
5. Cloned voices are persisted locally and restored automatically after restart.

> Voice cloning requires a **Base model** (e.g. `Qwen3-TTS-12Hz-1.7B-Base-8bit`). Switch to a Base model in the **Model** tab first.

#### Role Anchors

1. Switch to a CustomVoice or VoiceDesign model, then select an original speaker and emotion.
2. Use **Role Anchor** to generate a stable sample and save it as a reusable Base-mode voice.
3. A `- Chinese` anchor is created by default. If **also create English anchor** is checked, a separate `- English` anchor is generated.
4. Batch anchoring also respects this checkbox, making it useful for preparing multiple characters and emotions for audiobook work.
5. Use **Export Anchors / Import Anchors** to move `.ttscx` anchor packages between machines or projects.

## Voice Reference

| Voice | Gender | Language | Style |
|---|---|---|---|
| Serena | Female | zh/en | Warm, professional |
| Vivian | Female | zh/en | Bright, expressive |
| Uncle Fu | Male | zh/en | Deep, authoritative |
| Ryan | Male | zh/en | Calm, clear |
| Alden | Male | zh/en | Casual, conversational |
| Ono Anna | Female | zh/en | Gentle, measured |
| Sohee | Female | zh/en | Energetic, youthful |
| Eric | Male | zh/en | Neutral, versatile |
| Dylan | Male | zh/en | Rich, narrative |

All built-in voices support both Chinese and English and respond to emotion / style instructions.

---

## Emotion and Style Instructions

The **Control Instruction** field accepts free-form natural language. The model interprets these as speaker-direction notes. Examples:

| Instruction | Effect |
|---|---|
| `speak slowly and clearly` | Measured pace, enunciation |
| `whispering, tense` | Hushed delivery |
| `cheerful and upbeat` | Lighter, faster cadence |
| `calm but slightly worried` | Restrained affect with underlying tension |
| `authoritative, formal` | Deliberate, composed delivery |
| `excited, breathless` | Faster pace, higher energy |
| `melancholy, distant` | Slower, softer, trailing |
| `warm storytelling tone` | Relaxed, narrative register |

Instructions are per-line in dialog mode, giving you sentence-level expressive control across an entire production.

---

## Session Files (.ttso)

`.ttso` is the complete session format. It contains:

- All dialog rows (speaker, instruction, text) or plain text
- Voice selection and speed setting
- Advanced parameter values
- Per-segment timestamps (start/end ms for each synthesized chunk)
- Output audio path

Recommended folder layout:

```
ProjectFolder/
├── my_audiobook.ttso
└── my_audiobook.wav
```

When loading a `.ttso`, the app attempts to locate the original audio in the same folder and restore playback automatically.

---

## Settings

### Model Tab

| Setting | Notes |
|---|---|
| **Select Model** | Choose from locally installed Qwen3-TTS variants (CustomVoice / Base / VoiceDesign). Models not in local cache are shown as unavailable. |
| **Load Model** | Loads the selected model into memory. Required after switching models. |
| **Output Format** | WAV (no dependency) or MP3 (requires ffmpeg). |
| **Output Directory** | Where synthesized audio files are saved. Defaults to `~/Downloads`. |

### Voice Tab

| Setting | Notes |
|---|---|
| **Speed** | Playback rate multiplier (0.5×–2.0×). Applied after synthesis via resampling. |
| **Select Voice** | Grid of built-in and cloned voices. The selected voice is used for plain text synthesis and as the default for new dialog rows. |

---

## Advanced TTS Parameters

Click **Advanced** in the top bar to open the Advanced Settings panel.

### Sampling Parameters

| Parameter | Default | Meaning |
|---|---:|---|
| `Temperature` | `0.9` | Sampling randomness. Lower for more stable output; higher for more expressive variation. |
| `Top-P` | `0.9` | Nucleus sampling threshold. |
| `Top-K` | `50` | Number of candidate tokens. |
| `Max Tokens` | `4096` | Maximum tokens generated per chunk. Increase for very long sentences. |

### Text Processing

| Parameter | Default | Meaning |
|---|---:|---|
| `Chunk Size` | `250` | Maximum characters per synthesis chunk. Smaller = more chunks; larger may affect stability on long sentences. |
| `Skip Brackets` | `true` | Ignores text inside （）brackets (stage directions, annotations). |
| `Numbers to Words` | `true` | Converts Arabic numerals to spoken form before synthesis. |

### Audio Post-processing

| Parameter | Default | Meaning |
|---|---:|---|
| `Paragraph Gap (ms)` | `300` | Silence inserted between synthesized chunks. |
| `Fade (ms)` | `10` | Fade-in / fade-out duration at audio edges. |
| `Pitch Shift` | `0` | Pitch adjustment in semitones (0 = original). |

### Built-in Presets

| Preset | Best for |
|---|---|
| Audiobook (stable long text) | Long documents, chapter-length synthesis, stable output |
| Audiobook (mellow) | Deeper, more deliberate pacing for audiobooks |
| Podcast (natural) | Conversational narration, varied cadence |
| Dubbing (fast) | Short-form content, voice-over, faster delivery |
| Reading (slow & clear) | Clear enunciation, educational content |

Custom presets are saved to `~/.tts_offline_settings.json`.

---

## Export

Use the **Subtitles** menu to export timing data alongside the audio:

| Format | Description |
|---|---|
| SRT | Standard subtitle file — import into video editors or media players |
| TXT | Plain text with timestamps — easy to read and diff |
| Markdown | Suitable for Obsidian, Notion, documentation |
| JSON | Full structured data: text, speaker, start_ms, end_ms per segment |
| Excel (CSV) | Tab-separated for spreadsheet import |

---

## Packaging as a macOS .app

The project uses a lightweight shell-app design. The `.app` contains project code and static assets but does not embed Python, MLX, or model weights. At launch it locates an external Python environment and runs the backend there.

### 1. Prepare the runtime environment

```bash
python3 -m venv ~/tts-offline-env
source ~/tts-offline-env/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

### 2. Prepare the build environment

```bash
python3 -m venv ~/tts-offline-build-env
source ~/tts-offline-build-env/bin/activate
pip install --upgrade pip setuptools wheel py2app
```

### 3. Build

```bash
rm -rf build dist
python build_mac.py py2app
```

The generated application is:

```
dist/Lancer1911 TTS Offline.app
```

### 4. First-launch Gatekeeper warning

If macOS blocks the app:

```bash
xattr -cr "/Applications/Lancer1911 TTS Offline.app"
```

Or: right-click the app → Open → click Open in the dialog.

Application logs:

```
~/Library/Logs/TTSOffline.log
```

---

## FAQ

**The status bar shows "Connecting" for a long time.**  
The model is loading into memory. Wait until the status bar shows "Ready". First load may take 20–60 seconds depending on model size and storage speed.

**The port is already in use.**  
Default port is 17435. Check with:

```bash
lsof -i :17435
```

Terminate if needed:

```bash
lsof -ti :17435 | xargs kill -9
```

**No models are detected.**  
The app will show a download wizard on first launch. Follow the instructions to download a model using `hf download`. If `hf` is not found, run `pip install -U huggingface_hub` first.

**Synthesis sounds unstable or cuts off.**  
Try reducing `Temperature` (e.g. to 0.7) and `Chunk Size` (e.g. to 150). Very long sentences may also benefit from adding punctuation to trigger natural chunk boundaries.

**Voice cloning does not work.**  
Voice cloning requires a Base model. Switch to `Qwen3-TTS-12Hz-1.7B-Base-8bit` or `0.6B-Base-8bit` in the Model tab, load it, then retry cloning.

**MP3 output fails.**  
Install ffmpeg: `brew install ffmpeg`. WAV output works without any additional dependencies.

**Dialog table CSV import fails.**  
Ensure the CSV uses UTF-8 encoding. The recommended format is four columns: Character / Voice / Emotion / Text. Older three-column scripts (Speaker / Instruction / Text) can still be used for simple dialog, but Base-mode multi-character production should use the four-column format.

**Session loads but audio is missing.**  
Keep the `.ttso` file and the `.wav` / `.mp3` output in the same folder with the original filename. The app attempts to auto-pair them on load.

---

## Dependencies

| Project | Purpose |
|---|---|
| [mlx-audio](https://github.com/Blaizzy/mlx-audio) | Qwen3-TTS MLX inference backend |
| [FastAPI](https://fastapi.tiangolo.com) | Local backend API and WebSocket service |
| [uvicorn](https://www.uvicorn.org) | ASGI server |
| [pywebview](https://pywebview.flowrl.com) | macOS desktop window |
| [ffmpeg](https://ffmpeg.org) | MP3 encoding and audio processing |
| [Qwen3-TTS](https://huggingface.co/Qwen) | TTS model family (CustomVoice / Base / VoiceDesign) |
| [python-docx](https://python-docx.readthedocs.io) | DOCX text extraction |
| [pdfminer.six](https://pdfminer.six.readthedocs.io) | PDF text extraction |
| [numpy](https://numpy.org) | Audio array processing |
| [sounddevice](https://python-sounddevice.readthedocs.io) | Python-side microphone recording |
| [PyObjC AVFoundation](https://pyobjc.readthedocs.io) | macOS microphone permission trigger for packaged apps |

---

## License

This project is licensed under the **Apache License 2.0**.

You may use, copy, modify, distribute, and commercialize this software, including as part of proprietary products, provided that you comply with the terms of the Apache License 2.0.

When redistributing this project or derivative works, please retain the copyright notice, the license text, and the `NOTICE` file. If you distribute a modified version, please clearly indicate that changes have been made. Third-party dependencies remain subject to their respective license terms.
