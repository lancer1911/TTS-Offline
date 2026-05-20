/**
 * i18n.js — TTS Offline 国际化模块
 * 支持语言：zh（中文）/ en（English）
 * 用法：
 *   <script src="i18n.js"></script>
 *   在 HTML 元素上添加 data-i18n="key"、data-i18n-title="key"、data-i18n-ph="key"
 *   调用 I18n.apply('zh') 或 I18n.apply('en') 切换语言
 *   调用 I18n.t('key') 获取当前语言的翻译文本
 *   调用 I18n.toggle() 在中英文之间切换
 */

const I18n = (() => {

  // ── 翻译字典 ─────────────────────────────────────────────────────────────

  const DICT = {

    zh: {
      // ── 顶栏按钮 ──────────────────────────────────────────────────────────
      save:                 "保存",
      load:                 "加载",
      adv_settings:         "高级设置",
      save_session_tip:     "保存当前会话为 .ttso 文件",
      load_session_tip:     "加载 .ttso 会话文件",

      // ── 状态栏 ────────────────────────────────────────────────────────────
      status_connecting:    "正在连接…",
      status_ready:         "就绪",
      status_loading:       "正在加载…",
      status_no_model:      "模型未加载 — 请在「模型」标签页点击「加载模型」",

      // ── 侧边栏标签 ────────────────────────────────────────────────────────
      tab_voice:            "音色",
      tab_model:            "模型",
      tab_clone:            "克隆",

      // ── 音色面板 ──────────────────────────────────────────────────────────
      speed:                "语速",
      select_voice:         "选择音色",
      voice_desc_clone:     "声音克隆",

      // ── 模型面板 ──────────────────────────────────────────────────────────
      tts_model:            "TTS 模型",
      select_model:         "选择模型",
      load_model:           "加载模型",
      model_desc_cv:        "内置 9 种音色（serena / vivian / ryan / aiden 等），支持情感指令控制",
      output_settings:      "输出设置",
      format:               "格式",
      output_dir:           "输出目录",
      save_settings:        "保存设置",

      // ── 克隆面板 ──────────────────────────────────────────────────────────
      voice_clone:          "声音克隆",
      clone_intro:          "上传 5~30 秒的参考音频（WAV/MP3），模型将学习该音色特征用于合成。",
      clone_name:           "克隆名称",
      ref_audio:            "参考音频",
      ref_text:             "参考音频文字（可选，填写可提升克隆质量）",
      base_voice:           "基础音色（风格参考）",
      start_clone:          "开始克隆",
      import:               "导入",
      cloned_voices:        "已克隆音色",
      cloned_none:          "暂无克隆音色",
      clone_hint:           "提示：参考音频越清晰、越纯净（无背景噪声），克隆效果越好。",
      clone_warn:           "声音克隆仅支持 Base 模型（Qwen3-TTS-12Hz-1.7B/0.6B-Base-8bit）。",
      clone_warn2:          "请在「模型」标签页切换到 Base 模型后再使用克隆功能。",

      // ── 主区域 ────────────────────────────────────────────────────────────
      upload_click:         "点击上传",
      upload_or:            "或拖放文本文件",
      upload_fmts:          "TXT · MD · DOCX · SRT · PDF · EPUB",
      text_content:         "文本内容",
      clear:                "清空",

      // ── 对话表格工具栏 ────────────────────────────────────────────────────
      dialog_mode:          "三列表格",
      normal_text:          "普通文本",
      load_csv:             "加载 CSV",
      add_row:              "+ 行",

      // ── 表格列标题 ────────────────────────────────────────────────────────
      th_speaker:           "Speaker",
      th_instruction:       "Control Instruction",
      th_text:              "Text",
      th_start:             "Start",
      th_end:               "End",
      action:               "操作",

      // ── 合成按钮 & 输出面板 ───────────────────────────────────────────────
      start_synth:          "开始合成",
      synth_cancel:         "■ 取消合成",
      synth_progress:       "合成进度",
      subtitles:            "字幕",
      download_audio:       "⬇ 下载音频",
      expand_log:           "展开日志",
      collapse_log:         "收起日志",
      follow:               "跟随",

      // ── 字幕导出格式 ──────────────────────────────────────────────────────
      sub_srt:              "SRT 字幕",
      sub_txt:              "TXT 纯文本",
      sub_md:               "Markdown",
      sub_json:             "JSON",
      sub_csv:              "Excel (CSV)",

      // ── 高级设置弹窗 ──────────────────────────────────────────────────────
      adv_title:            "高级 TTS 参数",
      close:                "关闭",
      adv_intro:            "以下参数影响 TTS 模型推理行为。建议普通用户仅调整左侧栏中的语速和音色；高级参数适合需要精细控制输出风格的用户。修改后点击\"保存并关闭\"生效。",
      presets:              "参数预设",
      apply_preset:         "套用预设",
      save_preset:          "保存为预设",
      del_preset:           "删除预设",
      adv_preset_help:      "预设会覆盖当前高级参数。自定义预设保存在本机设置文件中。",
      sampling_params:      "采样参数",
      text_proc:            "文本处理",
      audio_post:           "音频后处理",
      reset_defaults:       "恢复默认值",
      apply_settings:       "应用到设置",
      save_close:           "保存并关闭",

      // ── 高级参数标签 & 说明 ───────────────────────────────────────────────
      adv_desc_temp:        "采样温度，越高越多样",
      adv_desc_topp:        "核采样概率阈值",
      adv_desc_topk:        "采样候选数",
      adv_desc_maxtok:      "每段最大生成 token 数",
      adv_label_chunk:      "分段字数",
      adv_desc_chunk:       "每段文本最大字符数",
      adv_label_skip:       "跳过括号注释",
      adv_desc_skip:        "忽略（）内的文字",
      adv_label_norm:       "数字转中文",
      adv_desc_norm:        "将阿拉伯数字转换为中文读法",
      adv_label_gap:        "段落静音 (ms)",
      adv_desc_gap:         "段落间插入的静音时长",
      adv_label_fade:       "淡入淡出 (ms)",
      adv_desc_fade:        "音频首尾淡化时长",
      adv_label_pitch:      "音调偏移",
      adv_desc_pitch:       "音调调整（半音，0=原始）",

      // ── 内置预设名称 ──────────────────────────────────────────────────────
      preset_audiobook_en:  "Audiobook（稳定长文本）",
      preset_audiobook_zh:  "有声书（沉稳）",
      preset_podcast:       "播客（自然）",
      preset_dubbing:       "配音（快）",
      preset_reading:       "朗读（慢清晰）",
      custom_preset_suffix: " [自定义]",
      preset_applied:       "已套用预设",

      // ── 下载向导弹窗 ──────────────────────────────────────────────────────
      dl_title:             "未检测到 Qwen3-TTS 模型",
      dl_body:              "请先下载至少一个模型才能使用合成功能。",
      dl_mirror_label:      "下载源",
      dl_mirror_official:   "官方 HuggingFace",
      dl_mirror_cn:         "中国镜像 (hf-mirror.com)",
      dl_recommended:       "推荐（首次安装）",
      dl_cmd_label:         "在终端执行以下命令：",
      dl_copy:              "复制命令",
      dl_copied:            "已复制",
      dl_dismiss:           "稍后再说",
      dl_downloading:       "正在下载…",
      dl_progress:          "下载进度",
      dl_done:              "下载完成",
      dl_err:               "下载失败",
      dl_open_terminal:     "打开终端",
      dl_install_hint:      "如未安装 hf 命令：pip install -U huggingface_hub",

      // ── Placeholder 文本 ──────────────────────────────────────────────────
      ph_custom_repo:       "输入自定义 HuggingFace repo",
      ph_clone_name:        "我的声音",
      ph_ref_text:          "参考音频中说的内容，例如：你好，这是我的声音。",
      ph_text_editor:       "在此输入或粘贴文本，或上传文件…",
      ph_instruction:       "例如：用平静但略带担忧的语气说",
      ph_dialog_text:       "需要转换的文字内容",

      // ── 计数单位 ──────────────────────────────────────────────────────────
      unit_char:            "字",
      unit_rows:            "行",
    },

    en: {
      // ── Top bar buttons ───────────────────────────────────────────────────
      save:                 "Save",
      load:                 "Load",
      adv_settings:         "Advanced",
      save_session_tip:     "Save session as .ttso file",
      load_session_tip:     "Load .ttso session file",

      // ── Status bar ────────────────────────────────────────────────────────
      status_connecting:    "Connecting…",
      status_ready:         "Ready",
      status_loading:       "Loading…",
      status_no_model:      "No model — go to Model tab and click Load Model",

      // ── Sidebar tabs ──────────────────────────────────────────────────────
      tab_voice:            "Voice",
      tab_model:            "Model",
      tab_clone:            "Clone",

      // ── Voice pane ────────────────────────────────────────────────────────
      speed:                "Speed",
      select_voice:         "Select Voice",
      voice_desc_clone:     "Cloned Voice",

      // ── Model pane ────────────────────────────────────────────────────────
      tts_model:            "TTS Model",
      select_model:         "Select Model",
      load_model:           "Load Model",
      model_desc_cv:        "9 built-in voices (serena / vivian / ryan / aiden …), supports emotion instructions",
      output_settings:      "Output",
      format:               "Format",
      output_dir:           "Output Dir",
      save_settings:        "Save Settings",

      // ── Clone pane ────────────────────────────────────────────────────────
      voice_clone:          "Voice Clone",
      clone_intro:          "Upload 5–30 s of reference audio (WAV/MP3); the model will learn its voice characteristics.",
      clone_name:           "Clone Name",
      ref_audio:            "Reference Audio",
      ref_text:             "Reference Text (optional, improves clone quality)",
      base_voice:           "Base Voice (style reference)",
      start_clone:          "Start Clone",
      import:               "Import",
      cloned_voices:        "Cloned Voices",
      cloned_none:          "No cloned voices",
      clone_hint:           "Tip: cleaner reference audio (no background noise) yields better clones.",
      clone_warn:           "Voice cloning only works with Base models (Qwen3-TTS-12Hz-1.7B/0.6B-Base-8bit).",
      clone_warn2:          "Switch to a Base model in the Model tab before using clone.",

      // ── Main area ─────────────────────────────────────────────────────────
      upload_click:         "Click to upload",
      upload_or:            "or drag & drop a text file",
      upload_fmts:          "TXT · MD · DOCX · SRT · PDF · EPUB",
      text_content:         "Text",
      clear:                "Clear",

      // ── Dialog table toolbar ──────────────────────────────────────────────
      dialog_mode:          "Dialog Table",
      normal_text:          "Plain Text",
      load_csv:             "Load CSV",
      add_row:              "+ Row",

      // ── Table column headers ──────────────────────────────────────────────
      th_speaker:           "Speaker",
      th_instruction:       "Control Instruction",
      th_text:              "Text",
      th_start:             "Start",
      th_end:               "End",
      action:               "Actions",

      // ── Synth button & output panel ───────────────────────────────────────
      start_synth:          "Synthesize",
      synth_cancel:         "■ Stop",
      synth_progress:       "Progress",
      subtitles:            "Subtitles",
      download_audio:       "⬇ Download",
      expand_log:           "Show Log",
      collapse_log:         "Hide Log",
      follow:               "Follow",

      // ── Subtitle export formats ───────────────────────────────────────────
      sub_srt:              "SRT Subtitles",
      sub_txt:              "Plain Text",
      sub_md:               "Markdown",
      sub_json:             "JSON",
      sub_csv:              "Excel (CSV)",

      // ── Advanced settings modal ───────────────────────────────────────────
      adv_title:            "Advanced TTS Parameters",
      close:                "Close",
      adv_intro:            "The following parameters affect TTS inference. Regular users should only adjust speed and voice in the sidebar; these advanced options are for fine-grained control. Click \"Save & Close\" to apply.",
      presets:              "Presets",
      apply_preset:         "Apply",
      save_preset:          "Save Preset",
      del_preset:           "Delete",
      adv_preset_help:      "Presets override all current advanced parameters. Custom presets are saved in local settings.",
      sampling_params:      "Sampling",
      text_proc:            "Text Processing",
      audio_post:           "Audio Post",
      reset_defaults:       "Reset Defaults",
      apply_settings:       "Apply",
      save_close:           "Save & Close",

      // ── Advanced param labels & descriptions ──────────────────────────────
      adv_desc_temp:        "Sampling temperature — higher = more varied",
      adv_desc_topp:        "Nucleus sampling threshold",
      adv_desc_topk:        "Top-K candidates",
      adv_desc_maxtok:      "Max tokens generated per chunk",
      adv_label_chunk:      "Chunk Size",
      adv_desc_chunk:       "Max characters per text chunk",
      adv_label_skip:       "Skip Brackets",
      adv_desc_skip:        "Ignore text inside （） brackets",
      adv_label_norm:       "Numbers to Words",
      adv_desc_norm:        "Convert Arabic numerals to spoken form",
      adv_label_gap:        "Paragraph Gap (ms)",
      adv_desc_gap:         "Silence inserted between paragraphs",
      adv_label_fade:       "Fade (ms)",
      adv_desc_fade:        "Fade-in/out duration at audio edges",
      adv_label_pitch:      "Pitch Shift",
      adv_desc_pitch:       "Pitch adjustment in semitones (0 = original)",

      // ── Built-in preset names ─────────────────────────────────────────────
      preset_audiobook_en:  "Audiobook (stable long text)",
      preset_audiobook_zh:  "Audiobook (mellow)",
      preset_podcast:       "Podcast (natural)",
      preset_dubbing:       "Dubbing (fast)",
      preset_reading:       "Reading (slow & clear)",
      custom_preset_suffix: " [custom]",
      preset_applied:       "Preset applied",

      // ── Download wizard modal ─────────────────────────────────────────────
      dl_title:             "No Qwen3-TTS models found",
      dl_body:              "Please download at least one model before synthesizing.",
      dl_mirror_label:      "Download source",
      dl_mirror_official:   "Official HuggingFace",
      dl_mirror_cn:         "China mirror (hf-mirror.com)",
      dl_recommended:       "Recommended (first install)",
      dl_cmd_label:         "Run the following command in Terminal:",
      dl_copy:              "Copy command",
      dl_copied:            "Copied!",
      dl_dismiss:           "Later",
      dl_downloading:       "Downloading…",
      dl_progress:          "Download progress",
      dl_done:              "Download complete",
      dl_err:               "Download failed",
      dl_open_terminal:     "Open Terminal",
      dl_install_hint:      "If hf is not installed: pip install -U huggingface_hub",

      // ── Placeholder text ──────────────────────────────────────────────────
      ph_custom_repo:       "Enter custom HuggingFace repo",
      ph_clone_name:        "My Voice",
      ph_ref_text:          "What is said in the reference audio, e.g.: Hello, this is my voice.",
      ph_text_editor:       "Type or paste text here, or upload a file…",
      ph_instruction:       "e.g.: Speak in a calm but slightly worried tone",
      ph_dialog_text:       "Text to synthesize",

      // ── Count units ───────────────────────────────────────────────────────
      unit_char:            "chars",
      unit_rows:            "rows",
    },
  };

  // ── 内部状态 ────────────────────────────────────────────────────────────

  let _lang = (typeof localStorage !== "undefined" && localStorage.getItem("lang")) || "zh";

  // ── 核心 API ────────────────────────────────────────────────────────────

  /**
   * 获取指定 key 在当前语言下的翻译。
   * 若当前语言缺少该 key，自动回退到中文；仍找不到则返回 key 本身。
   */
  function t(key) {
    return (DICT[_lang] || DICT.zh)[key]
        ?? DICT.zh[key]
        ?? key;
  }

  /**
   * 切换到指定语言并刷新页面所有 data-i18n 元素。
   * @param {string} lang  "zh" 或 "en"
   */
  function apply(lang) {
    if (!DICT[lang]) { console.warn(`[i18n] unknown lang: ${lang}`); return; }
    _lang = lang;
    if (typeof localStorage !== "undefined") localStorage.setItem("lang", lang);
    if (typeof document === "undefined") return;

    document.documentElement.lang = lang;

    // 更新语言切换按钮文字
    const btn = document.getElementById("LANG_BTN");
    if (btn) btn.textContent = lang === "zh" ? "EN" : "中";

    // data-i18n：纯文本节点（兼容含子元素的按钮，只替换最后一个文本节点）
    document.querySelectorAll("[data-i18n]").forEach(el => {
      const val = t(el.dataset.i18n);
      if (val === undefined) return;
      const childEls = Array.from(el.childNodes).some(n => n.nodeType === 1);
      if (childEls) {
        for (let i = el.childNodes.length - 1; i >= 0; i--) {
          const n = el.childNodes[i];
          if (n.nodeType === 3) { n.textContent = val; break; }
        }
      } else {
        el.textContent = val;
      }
    });

    // data-i18n-title：title 属性
    document.querySelectorAll("[data-i18n-title]").forEach(el => {
      const val = t(el.dataset.i18nTitle);
      if (val !== undefined) el.title = val;
    });

    // data-i18n-ph：placeholder 属性
    document.querySelectorAll("[data-i18n-ph]").forEach(el => {
      el.placeholder = t(el.dataset.i18nPh);
    });

    // 上传区标签（含 innerHTML 内嵌 <strong>，特殊处理）
    const ulbl = document.getElementById("UPLOAD_LABEL");
    if (ulbl && !ulbl.dataset.uploadFile) {
      ulbl.innerHTML = `<strong data-i18n="upload_click">${t("upload_click")}</strong> `
                     + `<span data-i18n="upload_or">${t("upload_or")}</span>`;
    }

    // 字幕菜单选项
    document.querySelectorAll(".subtitle-opt[data-i18n]").forEach(el => {
      el.textContent = t(el.dataset.i18n);
    });

    // 表头
    document.querySelectorAll("th[data-i18n]").forEach(el => {
      el.textContent = t(el.dataset.i18n);
    });

    // 通知外部回调（页面主逻辑可注册此钩子刷新动态内容）
    _hooks.forEach(fn => { try { fn(lang, t); } catch (e) {} });
  }

  /**
   * 在中英文之间切换。
   */
  function toggle() {
    apply(_lang === "zh" ? "en" : "zh");
  }

  /**
   * 返回当前激活的语言代码（"zh" 或 "en"）。
   */
  function current() {
    return _lang;
  }

  // ── 钩子机制 ────────────────────────────────────────────────────────────
  // 页面主逻辑可通过 I18n.onApply(fn) 注册回调，语言切换后自动调用。
  // 回调签名：fn(lang: string, t: (key: string) => string)

  const _hooks = [];

  function onApply(fn) {
    if (typeof fn === "function") _hooks.push(fn);
  }

  // ── 暴露公共接口 ────────────────────────────────────────────────────────

  return { t, apply, toggle, current, onApply, DICT };

})();
