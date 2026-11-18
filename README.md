<div align="center">

# Multimedia Processing Tool

**Windows 桌面端音视频批处理工具箱**  
  
**A Windows desktop toolbox for batch audio/video processing**

![Version](https://img.shields.io/badge/version-0.1.0--alpha-orange)

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11%20x64-blue)

![Python](https://img.shields.io/badge/python-3.15-3776ab)

![License](https://img.shields.io/badge/license-Apache--2.0-green)

[简体中文](#简体中文) · [English](#english)

</div>

---

## 简体中文

把「转码、无损压缩、解码、复制/移动、批量删除」五件事装进同一个窗口，并附带两种不需要安装 Python 的运行方式。

项目最初只是「把一堆 AVI 批量转成 MP4」的小脚本，后来陆续长出了无损压缩、解码转码、复制/移动、批量删除，最终合并成五个标签页的统一界面。底层全部调用 FFmpeg，界面用 tkinter 编写，主程序是单文件。

设计上优先照顾三件事：**能看见进度**（每个子进程都有独立进度条，FFmpeg 日志实时回显）、**不会误伤文件**（先算体积再确认、原子改名、中断不留半成品）、**开箱即用**（离线版内置 Python 与 FFmpeg，双击就跑）。

> ⚠️ 当前为 `0.1.0-alpha` 预览版本，界面与参数仍可能调整，不建议用于无人值守的生产流水线。

### 功能一览

| 标签页             | 做什么             | 输入 → 输出         | 关键点                               |
| --------------- | --------------- | --------------- | --------------------------------- |
| **AVI 转 MP4**   | 批量转码为 H.264 MP4 | `.avi` → `.mp4` | 多进程动态领任务，CRF 与音频码率可调，可选覆盖或跳过已存在文件 |
| **无损压缩 (FFV1)** | 视频无损重编码         | `.avi` → `.avi` | 先真实预编码算体积再确认，PCM 音频流原样复制          |
| **解码 / 转码**     | 按需选容器与编码器       | `.avi` → 任意格式   | 五组下拉配置，画质默认 CRF 23                |
| **复制 / 移动**     | 按后缀批量搬运         | 任意文件            | 同盘原子改名，跨盘分块复制，中断不丢源文件             |
| **批量删除**        | 按后缀清理文件         | —               | 先扫描出清单，确认后才动手                     |

五个标签页共用同一套进度条、日志面板与退出保护：有任务在跑时关闭窗口会先询问，并逐个停止子进程。

### 快速开始

**方式一：离线版（推荐，无需联网）**

下载 `Multimedia-Processing-Tool.exe`（约 265 MiB），双击即用。内置 Python 3.15 与 `ffmpeg.exe` / `ffprobe.exe` / `ffplay.exe`，运行时解压到内存，全程不弹控制台窗口。

**方式二：在线启动器（体积小，首次需联网）**

下载 `Multimedia-Processing-Tool-Online.exe`（约 12.3 MiB），双击后按以下顺序探测环境：

1. 系统已有 Python（含 tkinter）与 FFmpeg → 直接启动主程序；
2. `%APPDATA%\AllInOne` 已有运行时 → 直接复用；
3. 两者都没有 → 弹出进度窗口，把运行时下载到 `%APPDATA%\AllInOne`，uv 安装包在内存中解压，只把 `uv.exe` 落盘。

运行时只需下载一次，之后启动秒开。若网络异常，进度窗口会报错退出，此时请改用离线版。

**方式三：源码运行**

```bash
# 需要 Python 3.10+（含 tkinter）与 PATH 中的 ffmpeg / ffprobe
python Multimedia-Processing-Tool.py
```

### 各标签页说明

**AVI 转 MP4** — 递归扫描源目录下所有 `.avi`，按「CPU 核心数 − 1」启动进程，每个进程从队列动态领取任务，避免多个进程抢同一个文件。CRF 默认 23，音频码率默认 192k；可选「覆盖已存在的 MP4」，未勾选时对已存在的目标文件跳过。

**无损压缩 (FFV1)** — 对 AVI 做视频无损重编码：

```bash
ffmpeg -i input.avi -c:v ffv1 -level 3 -coder 1 -context 1 -c:a copy output.avi
```

`-coder 1 -context 1` 让 FFV1 使用范围编码与更大的上下文，压缩率更高；`-c:a copy` 保证 PCM 音频流逐字节不变。由于输出体积无法从输入稳定估算（实测在 0.55～2.59 倍之间波动），采用两段式流程：先编码到临时文件拿到**确切**体积，再弹窗展示「原文件大小 / 预计输出大小 / 变化比例」与三种处置方式（保留原文件、替换原文件、跳过），确认后用 `os.replace` 原子改名落盘。

**解码 / 转码** — 同一套流程换个出口，五组下拉决定命令：

| 选项   | 默认值               | 说明                    |
| ---- | ----------------- | --------------------- |
| 容器格式 | `mp4`             | 决定输出扩展名               |
| 像素格式 | `yuv420p`         | 兼容性最好的选择              |
| 编码器  | H.264 (`libx264`) | 可选 H.265 / VP9 等      |
| 音频流  | 与格式最兼容者           | 自动挑选 AAC / Opus / PCM |
| 画质   | `23`              | CRF 值，越小越清晰           |

不注入任何控制编码速度的参数，保证同一份文件在不同机器上产出接近的结果。输出扩展名在任务开始时快照，预编码期间改下拉不会导致最终文件名错乱。

**复制 / 移动** — 扫描 → 预览 → 执行三段式，子进程表实时显示每个 worker 正在搬运的文件与已传输字节。复制保留源文件，目标重名时逐个询问；移动在同盘走 `os.replace` 原子改名（几乎零耗时），跨盘走「临时文件 → 分块复制 → 删源」，中断不丢原文件。每个 worker 独占一个应答队列，多个冲突询问同时弹出也不会互相抢答卡死。

**批量删除** — 输入后缀（如 `.tmp,.log`，留空表示所有文件），先扫描出完整清单，确认后才执行删除，日志逐条回显。

### 实现要点

- **进度采集**：`-loglevel info -nostats -progress pipe:1`，stdout 解析 `out_time_ms` 计算进度，stderr 由后台线程逐行转发到日志面板。
- **进程模型**：转换/复制走 `multiprocessing`，压缩/解码走线程池 + 子进程，删除为单线程后台任务；共享状态通过 `Manager().dict()`，写入时整项回写而非改内嵌字段。
- **布局**：清单区与日志区使用 `grid` 权重布局，窗口缩放时按比例伸缩，日志区不会被挤没。
- **运行时注入**：启动时把 `sys._MEIPASS` 与 `%APPDATA%\AllInOne` 依次插入 `PATH`，打包后无需手动配置 FFmpeg。
- **可嵌入设计**：每个功能类都接受 `parent` 参数，既能独立运行（自己建 `Tk` 并设标题/尺寸），也能被 `ttk.Notebook` 直接托管。

### 目录结构

```
.
├── Multimedia-Processing-Tool.py   # 主程序：五合一 GUI（推荐入口）
├── avi_lossless_gui.py             # 单功能版：FFV1 压缩 + 解码/转码
├── main.py                         # 单功能版：AVI 批量转 MP4
├── Copy.py                         # 单功能版：复制 / 移动（带命令行参数）
├── del.py                          # 单功能版：批量删除
├── _online_launcher_src.py         # 在线启动器源码
├── AllInOne.spec                   # 离线版 PyInstaller 配置
├── OnlineLauncher.spec             # 在线版 PyInstaller 配置
├── LICENSE                         # Apache License 2.0
└── README.md
```

四个单功能脚本保留下来，方便只想用某一项功能、或想读更短代码的人。

> 注：仓库根目录的 `AllInOne.py` 是改名前的旧副本（117,209 字节），与 `Multimedia-Processing-Tool.py`（117,869 字节）内容不一致，请以后者为准。

### 命令行用法

`Copy.py` 支持不开界面直接跑：

```bash
python Copy.py --src "D:\素材" --dst "E:\备份" --ext .avi,.mp4 --mode move
```

| 参数       | 说明                        |
| -------- | ------------------------- |
| `--src`  | 源文件夹路径                    |
| `--dst`  | 目标文件夹路径                   |
| `--ext`  | 文件后缀，多个用逗号分隔，如 `.txt,.py` |
| `--mode` | `copy`（默认）或 `move`        |

### 从源码构建

```bash
pip install pyinstaller

# 离线版：内置 FFmpeg 三件套
pyinstaller AllInOne.spec

# 在线版：内嵌主程序，运行时按需下载
pyinstaller OnlineLauncher.spec
```

`AllInOne.spec` 的 `binaries` 指向本机 FFmpeg 的 `bin` 目录，只打包 `ffmpeg.exe` / `ffprobe.exe` / `ffplay.exe` 三个文件即可，不要把整个 `bin` 目录塞进去。两个 spec 均为 `console=False`，运行时不会弹出控制台窗口。

> 注意：两个 spec 的入口名目前仍写作 `AllInOne.py`。由于主程序已改名为 `Multimedia-Processing-Tool.py`，重新打包前需同步修改 `AllInOne.spec` 的 `Analysis(['...'])`、`OnlineLauncher.spec` 的 `datas`，以及 `_online_launcher_src.py` 中 `resource_path("AllInOne.py")` 与 `target` 两处文件名。

### 已知限制

- 仅支持 Windows 10 / 11 x64，未提供 Linux / macOS 构建。
- FFV1 是无损编码，输出体积常与源文件同量级甚至更大，这是预期行为而非缺陷。
- 原生 FFmpeg 只提供 Lagarith 解码器、没有编码器，所以无损压缩固定使用 FFV1。
- 在线版首次启动依赖网络；离线版体积较大（约 265 MiB）。
- Alpha 阶段没有自动更新，升级需手动替换 exe。
- 暂无单元测试与 CI，回归依赖人工验证。

### 路线图

- [ ] 内置 `__version__` 常量与「关于」对话框，版本号可在界面内查看
- [ ] 体积预测改为抽样估算，减少预编码的时间开销
- [ ] 补充回归测试，覆盖跨盘移动、冲突处理、扩展名快照三条路径
- [ ] 界面截图与使用动图

### 许可与反馈

本项目采用 [Apache License 2.0](LICENSE) 授权。遇到问题请在 [Issues](https://github.com/os-chang/Multimedia-Processing-Tool/issues) 反馈，并附上 Windows 版本、使用的是离线版还是在线版、复现步骤，以及日志面板中的相关输出。

---

## English

Five batch jobs — transcoding, lossless compression, decoding, copy/move, and bulk deletion — packed into a single window, with two ways to run it without installing Python.

The project started as a small script that converted a folder of AVI files to MP4. Lossless compression, decode/transcode, copy/move and bulk deletion were added over time, and everything was eventually merged into one five-tab interface. FFmpeg does all the media work, tkinter draws the UI, and the main program is a single file.

Three things drive the design: **you can see progress** (every worker gets its own progress bar and FFmpeg log lines stream back live), **files are never damaged** (size is measured before anything is replaced, renames are atomic, an interrupted job leaves no half-written output), and **it just runs** (the offline build bundles Python and FFmpeg, so a double-click is enough).

> ⚠️ This is the `0.1.0-alpha` preview. The UI and parameters may still change; do not use it in an unattended production pipeline.

### Features

| Tab                    | What it does                    | Input → Output      | Highlights                                                                     |
| ---------------------- | ------------------------------- | ------------------- | ------------------------------------------------------------------------------ |
| **AVI → MP4**          | Batch transcode to H.264 MP4    | `.avi` → `.mp4`     | Multi-process work queue, adjustable CRF and audio bitrate, optional overwrite |
| **Lossless (FFV1)**    | Lossless video re-encode        | `.avi` → `.avi`     | Real pre-encode to measure size, PCM audio copied bit-for-bit                  |
| **Decode / Transcode** | Pick container and codec freely | `.avi` → any format | Five dropdowns, CRF 23 by default                                              |
| **Copy / Move**        | Batch transfer by extension     | Any file            | Atomic rename on the same volume, chunked copy across volumes                  |
| **Bulk Delete**        | Clean up files by extension     | —                   | Scan and preview first, delete only after confirmation                         |

All five tabs share one progress area, one log pane, and one exit guard: closing the window while jobs are running asks for confirmation and then stops each worker in turn.

### Getting started

**Option 1 — Offline build (recommended, no network needed)**

Download `Multimedia-Processing-Tool.exe` (~265 MiB) and run it. Python 3.15 plus `ffmpeg.exe` / `ffprobe.exe` / `ffplay.exe` are bundled and unpacked into memory at launch; no console window ever appears.

**Option 2 — Online launcher (small download, network needed on first run)**

Download `Multimedia-Processing-Tool-Online.exe` (~12.3 MiB). On launch it probes the environment in this order:

1. System Python (with tkinter) and FFmpeg are usable → start the main program directly;
2. A runtime already exists under `%APPDATA%\AllInOne` → reuse it;
3. Neither is available → show a progress window, download the runtime into `%APPDATA%\AllInOne`, unpack the uv wheel in memory, and write only `uv.exe` to disk.

The runtime is downloaded once and reused afterwards, so later launches are instant. If the network fails, the progress window reports the error and exits — use the offline build instead.

**Option 3 — Run from source**

```bash
# Requires Python 3.10+ with tkinter, and ffmpeg / ffprobe on PATH
python Multimedia-Processing-Tool.py
```

### Tab details

**AVI → MP4** — Recursively scans the source folder for `.avi` files and starts *CPU cores − 1* processes, each pulling tasks from a shared queue so no two processes ever grab the same file. CRF defaults to 23 and audio bitrate to 192k. The overwrite checkbox controls whether existing MP4s are replaced or skipped.

**Lossless (FFV1)** — Re-encodes AVI video losslessly:

```bash
ffmpeg -i input.avi -c:v ffv1 -level 3 -coder 1 -context 1 -c:a copy output.avi
```

`-coder 1 -context 1` switches FFV1 to range coding with a larger context for better compression, while `-c:a copy` keeps the PCM audio stream byte-identical. Because the output size cannot be predicted reliably from the input (measured spread: 0.55× to 2.59×), the job runs in two stages: encode to a temporary file to obtain the **exact** size, then show a dialog with the source size, projected output size, ratio, and three choices (keep both, replace the original, skip). Only after confirmation is the temporary file promoted with an atomic `os.replace`.

**Decode / Transcode** — The same pipeline with a different output. Five dropdowns build the command:

| Option       | Default                           | Notes                                |
| ------------ | --------------------------------- | ------------------------------------ |
| Container    | `mp4`                             | Determines the output extension      |
| Pixel format | `yuv420p`                         | Widest compatibility                 |
| Encoder      | H.264 (`libx264`)                 | H.265 / VP9 also available           |
| Audio stream | Most compatible for the container | Picks AAC / Opus / PCM automatically |
| Quality      | `23`                              | CRF; lower means better quality      |

No speed-control flags are injected, so the same input produces comparable results on different machines. The output extension is snapshotted when the job starts, so changing a dropdown mid-encode cannot scramble the final filename.


**Copy / Move** — Scan, preview, then execute; the worker table shows which file each process is moving and how many bytes have been transferred. Copy keeps the source and asks about each name collision. Move uses an atomic `os.replace` on the same volume (effectively instant) and falls back to *temp file → chunked copy → delete source* across volumes, so an interruption never loses the original. Each worker owns a dedicated reply queue, so several collision prompts can be open at once without deadlocking the UI.

**Bulk Delete** — Enter extensions (for example `.tmp,.log`; leave empty to match every file), scan to build the full list, then confirm to delete. Every removal is echoed to the log.

### Implementation notes

- **Progress** — `-loglevel info -nostats -progress pipe:1`; `out_time_ms` is parsed from stdout, while stderr is forwarded line by line to the log pane by a background thread.
- **Process model** — Convert and copy use `multiprocessing`; compression and decoding use a thread pool driving child processes; deletion is a single background thread. Shared state lives in a `Manager().dict()` and is always written back as a whole entry rather than mutated in place.
- **Layout** — The task list and log pane use weighted `grid` rows, so resizing the window scales both instead of squeezing the log out of view.
- **Runtime injection** — At startup `sys._MEIPASS` and `%APPDATA%\AllInOne` are prepended to `PATH`, so a packaged build needs no manual FFmpeg setup.
- **Embeddable classes** — Every feature class accepts a `parent` argument: it can run standalone (creating its own `Tk` root, title and geometry) or be hosted directly by a `ttk.Notebook`.

### Repository layout

```
.
├── Multimedia-Processing-Tool.py   # Main program: five-tab GUI (recommended entry point)
├── avi_lossless_gui.py             # Standalone: FFV1 compression + decode/transcode
├── main.py                         # Standalone: batch AVI → MP4
├── Copy.py                         # Standalone: copy / move (with CLI flags)
├── del.py                          # Standalone: bulk delete
├── _online_launcher_src.py         # Source of the online launcher
├── AllInOne.spec                   # PyInstaller spec for the offline build
├── OnlineLauncher.spec             # PyInstaller spec for the online build
├── LICENSE                         # Apache License 2.0
└── README.md
```

The four standalone scripts are kept for anyone who wants a single feature or a shorter read.

> Note: the `AllInOne.py` at the repository root is a stale copy from before the rename (117,209 bytes) and does not match `Multimedia-Processing-Tool.py` (117,869 bytes). Treat the latter as the source of truth.

### Command line

`Copy.py` also runs headless:

```bash
python Copy.py --src "D:\media" --dst "E:\backup" --ext .avi,.mp4 --mode move
```

| Flag | Description |
| --- | --- |
| `--src` | Source folder |
| `--dst` | Destination folder |
| `--ext` | Extensions, comma separated (e.g. `.txt,.py`) |
| `--mode` | `copy` (default) or `move` |

### Building from source

```bash
pip install pyinstaller

# Offline build: bundles the three FFmpeg binaries
pyinstaller AllInOne.spec

# Online build: embeds the main program, downloads a runtime on demand
pyinstaller OnlineLauncher.spec
```

`binaries` in `AllInOne.spec` points at the local FFmpeg `bin` folder. Only `ffmpeg.exe`, `ffprobe.exe` and `ffplay.exe` need to be bundled — do not ship the whole directory. Both specs use `console=False`, so no console window appears at runtime.

> Note: both specs still reference the entry file as `AllInOne.py`. Since the main program was renamed to `Multimedia-Processing-Tool.py`, update `Analysis(['...'])` in `AllInOne.spec`, the `datas` entry in `OnlineLauncher.spec`, and the two filename references in `_online_launcher_src.py` (`resource_path("AllInOne.py")` and its `target`) before rebuilding.

### Known limitations

- Windows 10 / 11 x64 only; no Linux or macOS build.
- FFV1 is lossless, so output is often as large as the source or larger. That is expected, not a bug.
- Native FFmpeg ships a Lagarith *decoder* but no encoder, which is why lossless compression is fixed to FFV1.
- The online build needs a network connection on first launch; the offline build is large (~265 MiB).
- No auto-update during the alpha phase — replace the exe manually.
- No unit tests or CI yet; regressions are caught by hand.

### Roadmap

- [ ] Add a `__version__` constant and an About dialog so the version is visible in the UI
- [ ] Replace the pre-encode size probe with sampling to cut the time overhead
- [ ] Add regression tests covering cross-volume moves, collision handling and extension snapshots
- [ ] Screenshots and a short usage animation

### License and feedback

Released under the [Apache License 2.0](LICENSE). For bugs, please open an [issue](https://github.com/os-chang/Multimedia-Processing-Tool/issues) and include your Windows version, whether you used the offline or online build, the steps to reproduce, and the relevant log output.
