#!/bin/bash
# macOS 内置 bash 是 3.2（不支持 ${var,,} 等 bash4 语法）
# 本脚本已改写为兼容 bash 3.2 的写法，无需 Homebrew bash
# =============================================================================
#  Lancer1911 TTS Offline — 安装脚本
#  Install Script
#
#  用法 / Usage:
#    bash install.sh                    # 标准安装（推荐，下载默认 TTS 模型）
#    bash install.sh --no-model         # 跳过模型下载（已下载或手动管理）
#    bash install.sh --cn               # 使用 hf-mirror.com 镜像（中国用户）
#    bash install.sh --no-model --cn    # 组合选项
# =============================================================================

set -euo pipefail

# ── 颜色输出 ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; }
section() { echo -e "\n${BOLD}━━━  $*  ━━━${NC}"; }

# ── 参数解析 ─────────────────────────────────────────────────────────────────
DOWNLOAD_MODEL=true
USE_CN_MIRROR=false
for arg in "$@"; do
    [[ "$arg" == "--no-model" ]] && DOWNLOAD_MODEL=false
    [[ "$arg" == "--cn"       ]] && USE_CN_MIRROR=true
done

# ── 脚本所在目录（即 app 根目录） ────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# launcher.py 按此顺序查找：~/tts-env, ~/tts-offline-env, ~/tts_offline_env
# 使用 tts-offline-env 以便与其他项目环境隔离
VENV_DIR="$HOME/tts-offline-env"
PYTHON_MIN="3.11"
PORT=17435

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  Lancer1911 TTS Offline — Installer      ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}"
echo ""
[[ "$DOWNLOAD_MODEL" == false ]] && info "Mode: skip model download (--no-model)"
[[ "$USE_CN_MIRROR"  == true  ]] && info "Mirror: hf-mirror.com (--cn)"
echo ""

# ═══════════════════════════════════════════════════════════════════════════════
section "1 / 5  Hardware & OS Check"
# ═══════════════════════════════════════════════════════════════════════════════

# Apple Silicon
ARCH=$(uname -m)
if [[ "$ARCH" != "arm64" ]]; then
    error "This app requires Apple Silicon (M-series chip). Detected: $ARCH"
    exit 1
fi
ok "Apple Silicon detected"

# macOS version ≥ 13
OS_VER=$(sw_vers -productVersion)
OS_MAJOR=$(echo "$OS_VER" | cut -d. -f1)
if [[ "$OS_MAJOR" -lt 13 ]]; then
    error "macOS 13 Ventura or later required. Current: $OS_VER"
    exit 1
fi
ok "macOS $OS_VER"

# RAM — TTS Offline 最低 16 GB（Qwen3-TTS-1.7B 约 1.7 GB 权重）
RAM_GB=$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 ))
if [[ "$RAM_GB" -lt 16 ]]; then
    warn "Only ${RAM_GB} GB RAM detected. 16 GB minimum recommended."
    warn "The app may run slowly or fail on long documents."
    read -r -p "Continue anyway? [y/N] " ans
    ans_lower=$(echo "$ans" | tr "[:upper:]" "[:lower:]")
    [[ "$ans_lower" == "y" ]] || exit 1
else
    ok "${RAM_GB} GB unified memory"
fi

# Disk space ≥ 5 GB free（1.7B 8bit 模型约 1.7 GB；多模型建议 10 GB）
FREE_GB=$(( $(df -k "$HOME" | tail -1 | awk '{print $4}') / 1024 / 1024 ))
if [[ "$FREE_GB" -lt 5 ]]; then
    error "Less than 5 GB free disk space (${FREE_GB} GB). TTS models require at least 2 GB."
    exit 1
fi
ok "${FREE_GB} GB free disk space"

# ═══════════════════════════════════════════════════════════════════════════════
section "2 / 5  Homebrew & System Dependencies"
# ═══════════════════════════════════════════════════════════════════════════════

# Homebrew
if ! command -v brew &>/dev/null; then
    info "Homebrew not found — installing..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
fi
ok "Homebrew $(brew --version | head -1)"

# ffmpeg — MP3 输出需要；WAV 输出不依赖 ffmpeg
# TTS 合成结果默认为 WAV，但用户可在设置中选择 MP3 输出
if ! command -v ffmpeg &>/dev/null; then
    info "Installing ffmpeg (required for MP3 output; WAV works without it)..."
    brew install ffmpeg
fi
ok "ffmpeg $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"

# ═══════════════════════════════════════════════════════════════════════════════
section "3 / 5  Python Environment"
# ═══════════════════════════════════════════════════════════════════════════════

# 查找 Python 3.11+
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        VER=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        MAJOR=$(echo "$VER" | cut -d. -f1)
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [[ "$MAJOR" -ge 3 && "$MINOR" -ge 11 ]]; then
            PYTHON_BIN="$candidate"
            ok "Found Python $VER at $(command -v $candidate)"
            break
        fi
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    info "Python $PYTHON_MIN+ not found — installing via Homebrew..."
    brew install python@3.11
    PYTHON_BIN="python3.11"
fi

# 创建虚拟环境 ~/tts-offline-env
# launcher.py 会按优先级查找此路径
if [[ -d "$VENV_DIR" ]]; then
    warn "Virtual environment already exists at $VENV_DIR"
    read -r -p "Re-use existing environment? [Y/n] " ans
    if [[ "$(echo "$ans" | tr "[:upper:]" "[:lower:]")" == "n" ]]; then
        info "Removing old environment..."
        rm -rf "$VENV_DIR"
        "$PYTHON_BIN" -m venv "$VENV_DIR"
        ok "New virtual environment created at $VENV_DIR"
    else
        ok "Using existing environment"
    fi
else
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    ok "Virtual environment created at $VENV_DIR"
fi

# 激活
source "$VENV_DIR/bin/activate"
pip install --upgrade pip --quiet

# ═══════════════════════════════════════════════════════════════════════════════
section "4 / 5  Python Packages"
# ═══════════════════════════════════════════════════════════════════════════════

info "Installing packages from requirements.txt (this may take 2–4 minutes)..."

REQS="$SCRIPT_DIR/requirements.txt"
if [[ -f "$REQS" ]]; then
    pip install --quiet -r "$REQS"
    ok "Packages installed (from requirements.txt)"
else
    # 回退：与 requirements.txt 保持同步的显式列表
    pip install --quiet \
        "fastapi>=0.111.0" \
        "uvicorn[standard]>=0.30.0" \
        "python-multipart>=0.0.9" \
        "pywebview>=5.1" \
        "mlx_audio" \
        "numpy>=1.26.0" \
        "python-docx>=1.1.0" \
        "pdfminer.six>=20221105"
    ok "Packages installed (fallback list)"
fi

# huggingface_hub CLI（hf 命令）
# mlx_audio 会拉取 huggingface_hub，但版本可能不带 CLI；确保 hf 命令可用
if ! command -v hf &>/dev/null 2>&1; then
    info "Installing huggingface_hub[cli] to enable 'hf download'..."
    pip install --quiet "huggingface_hub[cli]>=0.34.0"
fi
ok "hf CLI ready ($(hf version 2>/dev/null | head -1 || echo 'installed'))"

# ═══════════════════════════════════════════════════════════════════════════════
section "5 / 5  Model Download"
# ═══════════════════════════════════════════════════════════════════════════════

HF_CACHE="$HOME/.cache/huggingface/hub"

download_model() {
    local repo="$1"
    local label="$2"
    local size="$3"
    local dir_name
    dir_name="models--$(echo "$repo" | tr '/' '--')"
    if [[ -d "$HF_CACHE/$dir_name" ]] && \
       [[ -d "$HF_CACHE/$dir_name/snapshots" ]] && \
       [[ -n "$(ls -A "$HF_CACHE/$dir_name/snapshots" 2>/dev/null)" ]]; then
        ok "$label already cached — skipping"
    else
        info "Downloading $label ($size)..."
        if [[ "$USE_CN_MIRROR" == true ]]; then
            info "  Using mirror: hf-mirror.com"
            HF_ENDPOINT="https://hf-mirror.com" hf download "$repo" || {
                warn "Download failed for $repo (mirror)."
                warn "Try without --cn, or download manually from https://hf-mirror.com/$repo"
            }
        else
            hf download "$repo" || {
                warn "Download failed for $repo."
                warn "China users: re-run with --cn to use hf-mirror.com"
                warn "Or download manually: hf download $repo"
            }
        fi
    fi
}

if [[ "$DOWNLOAD_MODEL" == true ]]; then
    echo ""
    info "The default model is Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit (~1.7 GB)."
    info "It includes 9 built-in voices and supports emotion/style instructions."
    echo ""
    echo -e "  Available model variants:"
    echo -e "    ${BOLD}1.7B-CustomVoice-8bit${NC}  — 9 built-in voices, emotion control   ${GREEN}[default]${NC}"
    echo -e "    ${BOLD}0.6B-CustomVoice-8bit${NC}  — lightweight, faster, lower memory"
    echo -e "    ${BOLD}1.7B-Base-8bit${NC}         — no built-in voices, for voice cloning only"
    echo ""
    read -r -p "Download default model now (1.7B-CustomVoice-8bit, ~1.7 GB)? [Y/n] " ans
    if [[ "$(echo "$ans" | tr "[:upper:]" "[:lower:]")" != "n" ]]; then
        download_model \
            "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit" \
            "Qwen3-TTS 1.7B-CustomVoice-8bit" \
            "~1.7 GB"
    else
        info "Skipping model download."
        info "Download later from the app's startup wizard, or run:"
        if [[ "$USE_CN_MIRROR" == true ]]; then
            echo -e "    ${BOLD}HF_ENDPOINT=https://hf-mirror.com hf download mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit${NC}"
        else
            echo -e "    ${BOLD}hf download mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit${NC}"
        fi
    fi

    echo ""
    echo -e "  ${BOLD}Optional models (download later if needed):${NC}"
    if [[ "$USE_CN_MIRROR" == true ]]; then
        echo -e "    Lightweight:    ${BLUE}HF_ENDPOINT=https://hf-mirror.com hf download mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit${NC}"
        echo -e "    Voice cloning:  ${BLUE}HF_ENDPOINT=https://hf-mirror.com hf download mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit${NC}"
    else
        echo -e "    Lightweight:    ${BLUE}hf download mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit${NC}"
        echo -e "    Voice cloning:  ${BLUE}hf download mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit${NC}"
    fi
else
    info "Skipping model download (--no-model)."
    info "Download manually when ready:"
    if [[ "$USE_CN_MIRROR" == true ]]; then
        echo -e "    ${BOLD}HF_ENDPOINT=https://hf-mirror.com hf download mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit${NC}"
    else
        echo -e "    ${BOLD}hf download mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit${NC}"
    fi
    echo -e "  Or launch the app — it will show a download wizard automatically."
fi

# ── 桌面启动器（从源码运行时使用；.app 版本不需要） ─────────────────────────
LAUNCHER="$HOME/Desktop/TTS Offline.command"
cat > "$LAUNCHER" << LAUNCH
#!/bin/bash
source "$VENV_DIR/bin/activate"
cd "$SCRIPT_DIR"
python main.py
LAUNCH
chmod +x "$LAUNCHER"
ok "Launcher created: ~/Desktop/TTS Offline.command"

# ── 检查端口冲突 ──────────────────────────────────────────────────────────────
if lsof -i ":$PORT" &>/dev/null; then
    warn "Port $PORT is already in use. The app may fail to start."
    warn "Check with: lsof -i :$PORT"
    warn "Kill if needed: lsof -ti :$PORT | xargs kill -9"
fi

# ── 完成 ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║   Installation complete ✓                ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}From source:${NC}"
echo -e "    Double-click: ${BOLD}~/Desktop/TTS Offline.command${NC}"
echo -e "    Or:           ${BOLD}source $VENV_DIR/bin/activate && python $SCRIPT_DIR/main.py${NC}"
echo ""
echo -e "  ${BOLD}As .app:${NC}"
echo -e "    Open ${BOLD}Lancer1911 TTS Offline.app${NC} — it will find $VENV_DIR automatically."
echo ""
echo -e "  The app starts at: ${BOLD}http://127.0.0.1:$PORT${NC}"
echo -e "  First model load may take 10–30 seconds."
echo ""
echo -e "  ${BOLD}Voice cloning${NC} requires the Base model:"
echo -e "    Switch to Model tab → select 1.7B-Base-8bit → Load Model"
echo ""
echo -e "  ${BOLD}Settings file:${NC} ~/.tts_offline_settings.json"
echo -e "  ${BOLD}Logs:${NC}          ~/Library/Logs/TTSOffline.log"
echo ""
