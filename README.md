<div align="center">

# Multimedia Processing Tool

**Windows 桌面端音视频批处理工具箱**

把「转码、无损压缩、解码、复制/移动、批量删除」五件事装进同一个窗口，并附带两种不需要安装 Python 的运行方式。

![Version](https://img.shields.io/badge/version-0.1.0--alpha-orange)

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11%20x64-blue)

![Python](https://img.shields.io/badge/python-3.15-3776ab)

![License](https://img.shields.io/badge/license-Apache--2.0-green)

</div>

---

## 这是什么

一个用 tkinter 写的单文件 GUI，底层全部调用 FFmpeg。最初只是「把一堆 AVI 批量转成 MP4」的小脚本，后来陆续长出了无损压缩、解码转码、复制/移动、批量删除，最后合并成五个标签页的统一界面。

设计上优先照顾三件事：**能看见进度**（每个子进程都有独立进度条，FFmpeg 日志实时回显）、**不会误伤文件**（先算体积再确认、原子改名、中断不留半成品）、**开箱即用**（离线版内置 Python 与 FFmpeg，双击就跑）。

> ⚠️ 当前为 `0.1.0-alpha` 预览版本，界面与参数仍可能调整，不建议用于无人值守的生产流水线。

## 功能一览

| 标签页             | 做什么             | 输入 → 输出         | 关键点                               |
| --------------- | --------------- | --------------- | --------------------------------- |
| **AVI 转 MP4**   | 批量转码为 H.264 MP4 | `.avi` → `.mp4` | 多进程动态领任务，CRF 与音频码率可调，可选覆盖或跳过已存在文件 |
| **无损压缩 (FFV1)** | 视频无损重编码         | `.avi` → `.avi` | 先真实预编码算体积再确认，PCM 音频流原样复制          |
| **解码 / 转码**     | 按需选容器与编码器       | `.avi` → 任意格式   | 五组下拉配置，画质默认 CRF 23                |
| **复制 / 移动**     | 按后缀批量搬运         | 任意文件            | 同盘原子改名，跨盘分块复制，中断不丢源文件             |
| **批量删除**        | 按后缀清理文件         | —               | 先扫描出清单，确认后才动手                     |

五个标签页共用同一套进度条、日志面板与退出保护：有任务在跑时关闭窗口会先询问，并逐个停止子进程。

## 快速开始

### 方式一：离线版（推荐，无需联网）

下载 `Multimedia-Processing-Tool.exe`（约 265 MiB），双击即用。

内置 Python 3.15 与 `ffmpeg.exe` / `ffprobe.exe` / `ffplay.exe`，运行时解压到内存，全程不弹控制台窗口。

### 方式二：在线启动器（体积小，首次需联网）

下载 `Multimedia-Processing-Tool-Online.exe`（约 12.3 MiB），双击后按以下顺序探测环境：

1. 系统已有 Python（含 tkinter）与 FFmpeg → 直接启动主程序；
2. `%APPDATA%\AllInOne` 已有运行时 → 直接复用；
3. 两者都没有 → 弹出进度窗口，把运行时下载到 `%APPDATA%\AllInOne`，uv 安装包在内存中解压，只把 `uv.exe` 落盘。

运行时只需下载一次，之后启动秒开。若网络异常，进度窗口会报错退出，此时请改用离线版。

### 方式三：源码运行

```bash
# 需要 Python 3.10+（含 tkinter）与 PATH 中的 ffmpeg / ffprobe
python Multimedia-Processing-Tool.py
```

## 各标签页说明

### AVI 转 MP4

递归扫描源目录下所有 `.avi`，加入任务清单后按「CPU 核心数 − 1」启动进程，每个进程从队列里动态领取任务，避免多个进程抢同一个文件。

- CRF 默认 23，音频码率默认 192k；
- 可选「覆盖已存在的 MP4」，未勾选时对已存在的目标文件跳过；
- 提供「删除所有 MP4」按钮，用于清理失败产物。

### 无损压缩 (FFV1)

对 AVI 做视频无损重编码，命令形如：

```bash
ffmpeg -i input.avi -c:v ffv1 -level 3 -coder 1 -context 1 -c:a copy output.avi
```

`-coder 1 -context 1` 让 FFV1 使用范围编码与更大的上下文，压缩率更高；`-c:a copy` 保证 PCM 音频流逐字节不变。

因为无损编码的输出体积无法从输入稳定估算（实测在 0.55～2.59 倍之间波动），所以采用两段式流程：

1. 先编码到临时文件，拿到**确切**输出体积；
2. 弹窗展示「原文件大小 / 预计输出大小 / 变化比例」与三种处置方式（保留原文件、替换原文件、跳过）；
3. 用户确认后，用 `os.replace` 原子改名落盘，中途失败不会留下半成品。

### 解码 / 转码

同一套压缩流程换了个出口，五组下拉决定命令：

| 选项   | 默认值               | 说明                    |
| ---- | ----------------- | --------------------- |
| 容器格式 | `mp4`             | 决定输出扩展名               |
| 像素格式 | `yuv420p`         | 兼容性最好的选择              |
| 编码器  | H.264 (`libx264`) | 可选 H.265 / VP9 等      |
| 音频流  | 与格式最兼容者           | 自动挑选 AAC / Opus / PCM |
| 画质   | `23`              | CRF 值，越小越清晰           |

不注入任何控制编码速度的参数，保证同一份文件在不同机器上产出接近的结果。输出扩展名在任务开始时快照，预编码期间改下拉不会导致最终文件名错乱。

### 复制 / 移动

扫描 → 预览 → 执行三段式，子进程表实时显示每个 worker 正在搬运的文件与已传输字节。

- **复制**：保留源文件，目标重名时逐个询问；
- **移动**：同盘走 `os.replace` 原子改名（几乎零耗时），跨盘走「临时文件 → 分块复制 → 删源」，中断不丢原文件；
- 每个 worker 独占一个应答队列，多个冲突询问同时弹出也不会互相抢答卡死。

### 批量删除

输入后缀（如 `.tmp,.log`，留空表示所有文件），先扫描出完整清单，确认后才执行删除，日志逐条回显。

## 实现要点

- **进度采集**：`-loglevel info -nostats -progress pipe:1`，stdout 解析 `out_time_ms` 计算进度，stderr 由后台线程逐行转发到日志面板。
- **进程模型**：转换/复制走 `multiprocessing`，压缩/解码走线程池 + 子进程，删除为单线程后台任务；共享状态通过 `Manager().dict()`，写入时整项回写而非改内嵌字段。
- **布局**：清单区与日志区使用 `grid` 权重布局，窗口缩放时两者按比例伸缩，日志区不会被挤没。
- **运行时注入**：启动时把 `sys._MEIPASS` 与 `%APPDATA%\AllInOne` 依次插入 `PATH`，打包后无需手动配置 FFmpeg。
- **可嵌入设计**：每个功能类都接受 `parent` 参数，既能独立运行（自己建 `Tk` 并设标题/尺寸），也能被 `ttk.Notebook` 直接托管。

## 目录结构

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

四个单功能脚本保留下来，方便只想用某一项功能或想读更短的代码。

## 命令行用法

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

## 从源码构建

```bash
pip install pyinstaller

# 离线版：内置 FFmpeg 三件套
pyinstaller AllInOne.spec

# 在线版：内嵌主程序，运行时按需下载
pyinstaller OnlineLauncher.spec
```

`AllInOne.spec` 中的 `binaries` 指向本机 FFmpeg 的 `bin` 目录，只打包 `ffmpeg.exe` / `ffprobe.exe` / `ffplay.exe` 三个文件即可，不要把整个 `bin` 目录塞进去。两个 spec 都是 `console=False`，因此运行时不会弹出控制台窗口。

## 已知限制

- 仅支持 Windows 10 / 11 x64，未提供 Linux / macOS 构建。
- 在线版首次启动依赖网络；离线版体积较大（约 265 MiB）。
- Alpha 阶段没有自动更新，升级需手动替换 exe。
- 暂无单元测试与 CI，回归依赖人工验证。

## 路线图

- [ ] 内置 `__version__` 常量与「关于」对话框，版本号可在界面内查看
- [ ] 体积预测改为抽样估算，减少预编码的时间开销
- [ ] 补充回归测试，覆盖跨盘移动、冲突处理、扩展名快照三条路径
- [ ] 界面截图与使用动图

## 许可

本项目采用 [Apache License 2.0](LICENSE) 授权。

## 反馈

遇到问题请在 [Issues](https://github.com/os-chang/Multimedia-Processing-Tool/issues) 反馈，并附上：Windows 版本、使用的是离线版还是在线版、复现步骤，以及日志面板中的相关输出。
