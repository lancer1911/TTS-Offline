# Lancer1911 TTS Offline — pywebview / py2app 打包说明

本版本采用与 ASR Offline 类似的结构：`main.py` 启动本地 FastAPI 服务，并通过 `pywebview` 打开原生桌面窗口；`--browser` 参数仍可用于浏览器调试。

## 运行源码版

```bash
python3 -m venv ~/tts-env
source ~/tts-env/bin/activate
pip install -r requirements.txt
python main.py
```

浏览器调试：

```bash
python main.py --browser
```

## 构建轻量 macOS .app

```bash
python3 -m venv ~/tts-offline-build-env
source ~/tts-offline-build-env/bin/activate
pip install --upgrade pip setuptools wheel py2app
cd /path/to/tts_offline
python build_mac.py py2app
```

输出位置：

```text
~/Playground/tts_offline/dist/Lancer1911 TTS Offline.app
```

该 `.app` 是轻量壳程序，会优先寻找 `~/tts-env/bin/python3` 等外部 Python 环境来运行真正的 TTS 后端，因此无需把 MLX / mlx-audio 等大依赖打进 app 包内。
