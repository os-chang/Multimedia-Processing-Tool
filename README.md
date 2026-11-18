# Multimedia Processing Tool v0.1.0-alpha

首个公开预览版本：四个独立脚本合并为单一 GUI，并提供「免 Python」的离线版与在线版。
⚠️ Alpha 阶段，界面与参数仍可能调整，请勿用于无人值守的生产流水线。

## 新增

- **五合一界面**：AVI 转换、无损压缩、解码/转码、复制/移动、批量删除合并进单窗口，统一标题 `Multimedia Processing Tool`。
- **AVI 批量转换**：递归扫描 `.avi`，多进程动态领取任务，进程数默认「CPU 核心数 − 1」，单文件/总体/日志三路可见。
- **FFV1 无损压缩**：`-c:v ffv1 -level 3 -coder 1 -context 1 -c:a copy`，视频零损失、PCM 音频原样保留。
- **先算体积再动手**：真实预编码到临时文件 → 输出确切体积 → 弹窗确认 → `os.replace` 原子改名。
- **解码/转码页**：格式（默认 mp4）、像素格式（默认 yuv420p）、编码器（默认 H.264）、音频流（默认最兼容）、画质（默认 23）五项下拉。
- **复制/移动**：同盘走 `os.replace` 原子改名，跨盘走临时文件 + 分块复制 + 删源，中断不丢原文件。
- **离线版 exe**：约 265 MiB，内置 Python 3.15 与 ffmpeg/ffprobe/ffplay，解压到内存运行，无控制台窗口。
- **在线启动器 exe**：约 12.3 MiB，先查系统 Python+FFmpeg，再查 `%APPDATA%\AllInOne`，都缺则弹 Win32 进度窗口下载，uv 包在内存解压。

## 改进

- 日志采集改为 `-loglevel info -nostats -progress pipe:1`，stderr 后台线程实时转发，进度与日志不再被吞。
- 清单区与日志区改用权重布局，缩放窗口时日志区不再被挤没。
- 运行时自动注入 PATH：优先 `sys._MEIPASS`，其次 `%APPDATA%\AllInOne`。
- 脚本迁入 `Python/` 目录，仓库根目录保持干净。

## 修复

- 修复 `pack(expand=True)` 抢占空间导致日志区消失。
- 修复多 worker 抢同一应答队列导致界面卡死（改为每 worker 独立应答队列）。
- 修复 `Manager().dict()` 内嵌字段原地修改不生效（改为整项回写）。
- 修复解码页删原文件后输出仍沿用 `.avi` 扩展名（改为任务开始快照扩展名）。
- 修复 `del.py` 残缺 `for ext` 引发的 `SyntaxError`。

## 下载

| 文件 | 大小 | 说明 |
| --- | --- | --- |
| `AllInOne.exe`（离线版） | 约 265 MiB | 内置 Python 与 FFmpeg，断网可用 |
| `在线启动器.exe`（在线版） | 约 12.3 MiB | 首次运行需联网下载运行时，之后复用 |

## 已知限制

- 仅支持 Windows 10/11 x64。
- 原生 FFmpeg 无损压缩固定用 FFV1。
- 在线版首次启动依赖网络；Alpha 阶段无自动更新；暂无单元测试与 CI。

**完整版**：`0.1.0` 之前的所有改动均可能不兼容，升级请手动替换 exe。
