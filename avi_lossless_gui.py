# -*- coding: utf-8 -*-
"""
AVI 批量无损压缩工具 (tkinter + FFmpeg)

功能：
  1. 递归扫描指定目录下的 *.avi 文件并加入处理清单；
  2. 使用 FFmpeg 进行无损压缩：视频 FFV1（-coder 1 -context 1，像素级无损），
     音频 -c:a copy 原样保留（PCM 流不变），输出保持 AVI 容器与 .avi 扩展名；
  3. 多线程处理，线程数 = CPU 核心数 - 1；
  4. 工作线程从任务队列中动态领取任务；
  5. 两段式处理：先"预编码"到独立临时文件，得到真实输出体积并向用户展示预测
     汇总，用户确认后才改名/删原文件（不重复编码）；取消则清理临时文件、原文件不动；
  6. 可勾选"删除原文件"：确认应用后删除原文件，并把结果改名为该任务自己的原文件名。

说明：脚本可直接双击/命令行运行，无需 bat。tkinter 与 queue 均为标准库。
关于体积：体积比随画面内容在很大范围波动，无法从输入体积估算，故采用"真实预编码"
作为预测依据——预测值即真实输出值，稳定可靠。FFmpeg 无 Lagarith 编码器，
故采用同为无损、FFmpeg 原生支持的 FFV1 替代。
输入：假设待处理文件均为 AVI 无损编码；如需处理有损 AVI 也可，同为无损转码。
"""

from __future__ import annotations

import os
import queue
import shlex
import subprocess
import threading
import time
import uuid
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
CPU_COUNT = os.cpu_count() or 1
WORKER_COUNT = max(1, CPU_COUNT - 1)          # 线程数 = CPU 核心数 - 1，至少 1

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows 下不弹黑框
TMP_PREFIX = ".__avic_"                        # 临时文件前缀，扫描时会跳过

# 默认无损参数：FFV1 像素级无损视频 + 音频原样复制(PCM 等流不变)，保持 AVI 容器
# -map 0:v:0 取第一条视频流；-map 0:a? 有音频才映射（兼容无音轨的 AVI）
DEFAULT_PARAMS = "-map 0:v:0 -map 0:a? -c:v ffv1 -level 3 -coder 1 -context 1 -c:a copy"

POLL_MS = 100                                  # UI 轮询间隔


# ----------------------------------------------------------------------------
# 任务对象：每个任务随身携带自己的源路径、文件名等信息
# ----------------------------------------------------------------------------
class Task:
    def __init__(self, path: str, idx: int = 0):
        self.path = path
        self.idx = idx
        self.dir = os.path.dirname(path)
        self.name = os.path.basename(path)
        self.stem, self.ext = os.path.splitext(self.name)
        self.size = os.path.getsize(path)
        self.status = "等待"
        self.message = ""
        self.elapsed = 0.0


def fmt_size(n: int) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return "-"


def probe_duration(path: str) -> float:
    """用 ffprobe 读取媒体时长（秒）；失败返回 0。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=60)
        return float(r.stdout.strip())
    except (ValueError, subprocess.SubprocessError, OSError):
        return 0.0


def parse_progress_line(line: str):
    """解析 ffmpeg -progress 输出的一行，返回 (key, value) 或 None。"""
    line = line.strip()
    if "=" not in line:
        return None
    k, _, v = line.partition("=")
    return k.strip(), v.strip()


# ----------------------------------------------------------------------------
# 主程序
# ----------------------------------------------------------------------------
class AviLosslessApp:
    # 可被子类覆写的配置钩子
    PAGE_NAME = "压缩"          # 用于日志措辞
    SCAN_EXTS = (".avi",)       # 扫描的扩展名
    OUTPUT_SUFFIX = "_lossless" # 不删原文件时的输出后缀
    ACTION_KEEP = "保留原文件，另存为 *_lossless.avi"
    OPT_TITLE = "压缩设置"
    START_TEXT = "开始压缩"

    def __init__(self, root: tk.Tk, parent=None, page_title=None):
        # parent 不为空时表示嵌入 Notebook 页；自绘控件挂在 parent 上
        self.root = root                      # 顶层窗口，仅用于 after()/协议
        self.parent = parent if parent is not None else root
        if page_title:
            self.root.title(page_title)
        self.root.geometry("1040x820")
        self.root.minsize(920, 640)

        # ---- 运行时状态 ----
        self.tasks: list[Task] = []
        self.task_queue: queue.Queue = queue.Queue()   # 待处理任务（动态领取）
        self.ui_queue: queue.Queue = queue.Queue()     # 子线程 -> 主线程的消息
        self.stop_event = threading.Event()
        self.procs: dict[int, subprocess.Popen] = {}
        self.procs_lock = threading.Lock()
        self.counter_lock = threading.Lock()
        self.active_count = 0
        self.done_count = 0
        self.pending_total = 0
        self.running = False
        self.snapshot: dict = {}                        # 开始时的配置快照
        self.stat = {"ok": 0, "fail": 0, "cancel": 0}
        self.pending: list[dict] = []                   # 预编码完成、待确认应用的结果
        self.pending_lock = threading.Lock()
        self.phase = "idle"                             # idle / encode / confirm / apply
        self.task_progress: dict[int, float] = {}       # idx -> 预编码进度 0..100
        self.worker_labels: list[tk.StringVar] = []
        self.worker_bars: list[ttk.Progressbar] = []

        self._build_ui()
        self.root.after(POLL_MS, self._poll_ui)

    # ------------------------------------------------------- 可覆写钩子 ----
    def _build_options(self, frm_opt):
        """压缩页的选项区。解码页覆写此方法提供下拉控件。"""
        self.delete_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frm_opt,
            text="删除原文件（删除后把压缩结果改名为该文件的原名）",
            variable=self.delete_var,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))

        ttk.Label(frm_opt, text="FFmpeg 输出参数：").grid(row=1, column=0, sticky="w")
        self.param_var = tk.StringVar(value=DEFAULT_PARAMS)
        ttk.Entry(frm_opt, textvariable=self.param_var).grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=6)
        frm_opt.columnconfigure(1, weight=1)

        ttk.Label(
            frm_opt,
            foreground="#555",
            text=("默认方案：FFV1 无损视频（-coder 1 -context 1）+ 音频原样复制（PCM 流不变），"
                  "保持 AVI 容器与 .avi 扩展名。FFmpeg 无 Lagarith 编码器，故用同为无损的 FFV1 替代。"
                  "参数可自行修改。"),
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        ttk.Label(
            frm_opt,
            foreground="#555",
            text=(f"线程数：{WORKER_COUNT}（= CPU 核心 {CPU_COUNT} - 1）。"
                  "两段式：先预编码得到真实体积→弹窗展示预测→你确认后才改名/删原文件，取消则原文件不动。"),
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(2, 0))

    def _get_output_ext(self) -> str:
        """输出文件扩展名（含点）。压缩页固定 .avi；解码页随格式下拉变化。"""
        return ".avi"

    def _build_params(self) -> list[str]:
        """生成 FFmpeg 输出参数。压缩页取参数输入框；解码页由下拉组合生成。"""
        return shlex.split(self.param_var.get())

    def _final_name(self, task: Task, delete_original: bool) -> str:
        """生成最终文件名（不含目录）。输出扩展名优先取本次任务快照。"""
        out_ext = self.snapshot.get("output_ext") or self._get_output_ext()
        if delete_original:
            return task.name                       # 原名（同扩展名）
        return f"{task.stem}{self.OUTPUT_SUFFIX}{out_ext}"

    # ---------------------------------------------------------------- UI ----
    def _build_ui(self):
        root = self.parent
        # 根窗口按行分配：目录/选项/按钮固定，清单与日志按比例共享剩余空间
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=0)   # 目录
        root.rowconfigure(1, weight=0)   # 选项
        root.rowconfigure(2, weight=3)   # 处理清单（可伸缩）
        root.rowconfigure(3, weight=0)   # 按钮
        root.rowconfigure(4, weight=3)   # 进度与日志（可伸缩，与清单同等占比）

        # ---- 目录选择 ----
        frm_dir = ttk.LabelFrame(root, text="扫描目录（递归）", padding=8)
        frm_dir.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))

        self.dir_var = tk.StringVar(value=os.getcwd())
        ttk.Entry(frm_dir, textvariable=self.dir_var).pack(
            side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(frm_dir, text="浏览...", command=self._browse, width=10).pack(side="left")
        ttk.Button(frm_dir, text="扫描 AVI", command=self._scan, width=12).pack(
            side="left", padx=6)

        # ---- 选项（由子类覆写以提供不同控件）----
        frm_opt = ttk.LabelFrame(root, text=self.OPT_TITLE, padding=8)
        frm_opt.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        self._build_options(frm_opt)

        # ---- 清单 ----
        frm_list = ttk.LabelFrame(root, text="处理清单", padding=6)
        frm_list.grid(row=2, column=0, sticky="nsew", padx=10, pady=4)

        cols = ("no", "status", "file", "size", "progress", "elapsed", "message")
        self.tree = ttk.Treeview(frm_list, columns=cols, show="headings", height=6)
        heads = {
            "no": ("#", 40),
            "status": ("状态", 68),
            "file": ("文件路径", 470),
            "size": ("大小", 80),
            "progress": ("进度", 76),
            "elapsed": ("耗时", 70),
            "message": ("备注", 170),
        }
        for c in cols:
            self.tree.heading(c, text=heads[c][0])
            self.tree.column(c, width=heads[c][1], anchor="w", stretch=(c == "file"))
        vsb = ttk.Scrollbar(frm_list, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # ---- 操作按钮 ----
        frm_btn = ttk.Frame(root, padding=(10, 4))
        frm_btn.grid(row=3, column=0, sticky="ew")
        self.btn_start = ttk.Button(frm_btn, text=self.START_TEXT, command=self._start, width=12)
        self.btn_stop = ttk.Button(frm_btn, text="停止", command=self._stop, width=10,
                                   state="disabled")
        self.btn_clear = ttk.Button(frm_btn, text="清空清单", command=self._clear, width=12)
        self.btn_start.pack(side="left")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_clear.pack(side="left")
        ttk.Button(frm_btn, text="打开所在目录", command=self._open_dir, width=14).pack(
            side="right")

        # ---- 进度与日志 ----
        frm_bottom = ttk.LabelFrame(root, text="进度与日志", padding=6)
        frm_bottom.grid(row=4, column=0, sticky="nsew", padx=10, pady=(4, 10))
        frm_bottom.columnconfigure(0, weight=1)
        frm_bottom.rowconfigure(3, weight=1)   # 日志文本区可伸缩

        # 总进度
        self.pb = ttk.Progressbar(frm_bottom, mode="determinate", value=0)
        self.pb.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        self.stat_var = tk.StringVar(value="等待扫描…")
        ttk.Label(frm_bottom, textvariable=self.stat_var).grid(row=1, column=0, sticky="w")

        # 每个工作线程的进度条（横向排列，节省垂直空间）
        frm_workers = ttk.Frame(frm_bottom)
        frm_workers.grid(row=2, column=0, sticky="ew", pady=(4, 2))
        for wid in range(WORKER_COUNT):
            frm_workers.columnconfigure(wid, weight=1)
            cell = ttk.Frame(frm_workers, padding=(0, 0, 8, 0))
            cell.grid(row=0, column=wid, sticky="ew")
            lab = tk.StringVar(value=f"线程 {wid}：空闲")
            ttk.Label(cell, textvariable=lab, anchor="w").pack(fill="x")
            bar = ttk.Progressbar(cell, mode="determinate", value=0)
            bar.pack(fill="x", pady=(2, 0))
            self.worker_labels.append(lab)
            self.worker_bars.append(bar)

        self.txt = scrolledtext.ScrolledText(frm_bottom, height=6, wrap="word")
        self.txt.configure(state="disabled")
        self.txt.grid(row=3, column=0, sticky="nsew", pady=(4, 0))

    # ------------------------------------------------------------- 工具 ----
    def _row_values(self, i: int, status: str = None, message: str = None,
                    elapsed: float = None):
        """统一生成清单行数据（含进度列），供各 UI 更新处调用。"""
        t = self.tasks[i]
        pct = self.task_progress.get(i, 0)
        prog = f"{pct:.0f}%" if pct > 0 else "-"
        return (
            i + 1,
            status if status is not None else t.status,
            t.path,
            fmt_size(t.size),
            prog,
            f"{elapsed:.1f}s" if elapsed else "-",
            message if message is not None else t.message,
        )

    def _log(self, text: str):
        self.txt.configure(state="normal")
        self.txt.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        # 限制日志行数，避免长时间运行内存增长
        try:
            line_no = int(self.txt.index("end-1c").split(".")[0])
            if line_no > 600:
                self.txt.delete("1.0", f"{line_no - 500}.0")
        except Exception:
            pass
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _ui(self, *msg):
        """子线程调用：把 UI 更新请求放入队列"""
        self.ui_queue.put(msg)

    def _poll_ui(self):
        """主线程轮询队列并更新控件（tkinter 非线程安全）"""
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                self._handle_ui(msg)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll_ui)

    def _handle_ui(self, msg):
        kind = msg[0]
        if kind == "log":
            self._log(msg[1])

        elif kind == "add":
            task: Task = msg[1]
            i = task.idx
            self.tree.insert(
                "", "end", iid=str(i), values=self._row_values(i),
            )

        elif kind == "update":
            i, status, message, elapsed = msg[1], msg[2], msg[3], msg[4]
            try:
                self.tree.item(str(i), values=self._row_values(
                    i, status=status, message=message, elapsed=elapsed))
            except tk.TclError:
                pass

        elif kind == "scan_result":
            self._handle_scan_result(msg[1])

        elif kind == "scan_done":
            count = msg[1]
            self._log(f"扫描完成，共找到 {count} 个 AVI 文件。")
            self.stat_var.set(f"清单：{count} 个文件，等待开始")

        elif kind == "progress":
            done, total = msg[1], msg[2]
            self.pb["maximum"] = max(1, total)
            self.pb["value"] = done
            self.stat_var.set(
                f"总进度：{done}/{total}（{done / max(1, total) * 100:.0f}%） | "
                f"成功 {self.stat['ok']} | 失败 {self.stat['fail']} | 取消 {self.stat['cancel']}"
            )

        elif kind == "task_progress":
            # (idx, wid, pct, detail) —— 单个任务/线程进度
            i, wid, pct, detail = msg[1], msg[2], msg[3], msg[4]
            self.task_progress[i] = pct
            if 0 <= wid < len(self.worker_bars):
                self.worker_bars[wid]["value"] = pct
                name = self.tasks[i].name if 0 <= i < len(self.tasks) else "?"
                self.worker_labels[wid].set(f"线程 {wid}：{pct:.0f}% {name}")
            try:
                self.tree.item(str(i), values=self._row_values(i))
            except tk.TclError:
                pass

        elif kind == "worker_idle":
            wid = msg[1]
            if 0 <= wid < len(self.worker_bars):
                self.worker_bars[wid]["value"] = 0
                self.worker_labels[wid].set(f"线程 {wid}：空闲")

        elif kind == "ffmpeg_log":
            # FFmpeg 实时输出（去掉时间戳前缀由 _log 统一加）
            self._log(msg[1])

        elif kind == "stat":
            key = msg[1]
            self.stat[key] += 1

        elif kind == "all_done":
            self.running = False
            self.btn_stop.configure(state="disabled")
            # 停止时尚未领取的任务标记为已取消，避免停留在"等待"
            for i, t in enumerate(self.tasks):
                if t.status == "等待":
                    t.status = "已取消"
                    self._handle_ui(("update", i, "已取消", "未处理", 0.0))
            if self.phase == "encode" and not self.stop_event.is_set():
                # 预编码正常完成 -> 弹出体积预测汇总让用户确认
                self.phase = "confirm"
                self.btn_start.configure(state="normal")
                self.btn_clear.configure(state="normal")
                self._show_prediction_dialog()
            elif self.phase == "encode" and self.stop_event.is_set():
                # 用户中途停止 -> 放弃已编码结果，清理临时文件
                self._discard_pending()
                self._log("已停止，临时结果已清理，原文件未改动。")
            else:
                self.phase = "idle"
                self.btn_start.configure(state="normal")
                self.btn_clear.configure(state="normal")
                self._log("全部任务处理结束。")

    # ------------------------------------------------------------- 动作 ----
    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or os.getcwd())
        if d:
            self.dir_var.set(os.path.normpath(d))

    def _clear(self):
        if self.running:
            messagebox.showinfo("提示", "任务运行中，不能清空清单。")
            return
        # 若处于预测确认阶段，先清理遗留的临时文件
        if self.phase == "confirm" and self.pending:
            with self.pending_lock:
                pending = list(self.pending)
                self.pending = []
            for p in pending:
                self._cleanup(p["tmp_path"])
            self._log(f"清空清单：已清理 {len(pending)} 个临时文件。")
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.tasks.clear()
        with self.task_queue.mutex:
            self.task_queue.queue.clear()
        self.stat = {"ok": 0, "fail": 0, "cancel": 0}
        self.pb["value"] = 0
        self.phase = "idle"
        self.stat_var.set("清单已清空")

    def _open_dir(self):
        d = self.dir_var.get().strip()
        if os.path.isdir(d):
            try:
                os.startfile(d)  # Windows
            except Exception:
                self._log(f"无法打开目录：{d}")

    def _scan(self):
        if self.running:
            messagebox.showinfo("提示", "任务运行中，不能重新扫描。")
            return
        root = self.dir_var.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showwarning("目录无效", "请选择一个有效的扫描目录。")
            return

        self._log(f"开始扫描：{root}")

        def _do_scan():
            found: list[Task] = []
            for dp, _dns, fns in os.walk(root):
                for fn in fns:
                    if fn.startswith(TMP_PREFIX):      # 跳过本工具临时文件
                        continue
                    if fn.lower().endswith(self.SCAN_EXTS):
                        p = os.path.join(dp, fn)
                        try:
                            found.append(Task(p))
                        except OSError as e:
                            self._ui("log", f"无法读取 {p}：{e}")
            found.sort(key=lambda t: t.path.lower())
            self.ui_queue.put(("scan_result", found))

        threading.Thread(target=_do_scan, daemon=True).start()

    def _handle_scan_result(self, found: list[Task]):
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.tasks = found
        with self.task_queue.mutex:
            self.task_queue.queue.clear()
        for i, t in enumerate(found):
            t.idx = i
            self.task_queue.put(t)
            self.tree.insert(
                "", "end", iid=str(i), values=self._row_values(i),
            )
        self.stat = {"ok": 0, "fail": 0, "cancel": 0}
        self.task_progress = {}
        self.pb["value"] = 0
        self.ui_queue.put(("scan_done", len(found)))

    def _start(self):
        if self.running:
            return
        if not self.tasks:
            messagebox.showinfo("提示", "清单为空，请先扫描。")
            return

        delete_original = self.delete_var.get()

        try:
            params = self._build_params()
        except ValueError as e:
            messagebox.showerror("参数错误", f"FFmpeg 参数解析失败：{e}")
            return

        # 配置快照：子线程只读快照，避免跨线程访问 tkinter 变量
        self.snapshot = {
            "delete_original": delete_original,
            "params": params,
            "output_ext": self._get_output_ext(),
        }

        # 重建任务队列：已完成的跳过，其余重置为等待（上次可能被停止过）
        with self.task_queue.mutex:
            self.task_queue.queue.clear()
        pending = 0
        for i, t in enumerate(self.tasks):
            if t.status == "完成":
                continue
            t.status = "等待"
            t.message = ""
            t.elapsed = 0.0
            self.task_queue.put(t)
            self._handle_ui(("update", i, "等待", "", 0.0))
            pending += 1
        if pending == 0:
            messagebox.showinfo("提示", "清单中没有待处理的任务（已全部完成）。")
            return

        self.stat = {"ok": 0, "fail": 0, "cancel": 0}
        self.task_progress = {}
        self.done_count = 0
        self.pending_total = pending
        with self.pending_lock:
            self.pending = []
        self.stop_event.clear()
        self.running = True
        self.phase = "encode"
        self.active_count = WORKER_COUNT
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_clear.configure(state="disabled")

        self._log(f"预编码阶段：{pending} 个文件，{WORKER_COUNT} 个工作线程（不改动原文件）。")
        self.ui_queue.put(("progress", 0, pending))

        for wid in range(WORKER_COUNT):
            threading.Thread(target=self._worker, args=(wid,), daemon=True).start()

    def _stop(self):
        if not self.running:
            return
        self.stop_event.set()
        with self.procs_lock:
            procs = list(self.procs.values())
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        self._log("已发出停止请求，正在运行的 FFmpeg 将被终止…")

    # ------------------------------------------------------------- 核心 ----
    def _worker(self, wid: int):
        """工作线程：动态从队列领取任务，直到队列空或收到停止信号"""
        total = self.pending_total
        while not self.stop_event.is_set():
            try:
                task = self.task_queue.get_nowait()
            except queue.Empty:
                break

            try:
                self._process_task(task, wid)
            except Exception as e:                       # 兜底，避免线程静默崩溃
                self._ui("update", task.idx, "失败", str(e), 0.0)
                self._ui("stat", "fail")
                self._ui("log", f"[{task.name}] 未预期错误：{e}")
            finally:
                self._ui("worker_idle", wid)
                self.task_queue.task_done()
                with self.counter_lock:
                    self.done_count += 1
                    done = self.done_count
                self.ui_queue.put(("progress", done, total))

        with self.counter_lock:
            self.active_count -= 1
            if self.active_count <= 0:
                self.ui_queue.put(("all_done",))

    def _process_task(self, task: Task, wid: int):
        i = task.idx
        out_ext = self.snapshot["output_ext"]
        # 临时文件与目标文件都在源目录（同一磁盘，rename 为原子操作）
        tmp_name = f"{TMP_PREFIX}{wid}_{uuid.uuid4().hex[:10]}{out_ext}"
        tmp_path = os.path.join(task.dir, tmp_name)
        delete_original = self.snapshot["delete_original"]

        # 目标文件名：删除原文件 -> 用该任务自己的原文件名（若扩展名变化则改用新扩展名）；
        # 否则加后缀避免覆盖
        if delete_original:
            final_path = os.path.join(task.dir, self._final_name(task, True))
        else:
            final_path = self._unique_path(
                os.path.join(task.dir, self._final_name(task, False)))

        # -progress pipe:1 输出结构化进度到 stdout；-nostats 关闭刷屏的 stats；
        # loglevel info 让 stderr 输出可读的 FFmpeg 日志
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info",
               "-nostats", "-progress", "pipe:1",
               "-i", task.path] + self.snapshot["params"] + [tmp_path]

        self._ui("update", i, "预编码", f"线程 {wid}", 0.0)
        self._ui("task_progress", i, wid, 0.0, "")
        self._ui("log", f"[线程 {wid}] 预编码：{task.name}")
        t0 = time.time()

        duration = probe_duration(task.path)   # 用于把 out_time 换算成百分比
        err_lines: list[str] = []

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        with self.procs_lock:
            self.procs[threading.get_ident()] = proc

        # 后台线程读 stderr 日志（实时转发到 GUI），避免管道塞满导致 FFmpeg 阻塞
        def _read_stderr():
            assert proc.stderr is not None
            for line in proc.stderr:
                line = line.rstrip()
                if not line:
                    continue
                err_lines.append(line)
                if len(err_lines) > 200:
                    del err_lines[0]
                # 只转发有信息量的行，过滤空行
                self._ui("ffmpeg_log", f"[线程 {wid}] {line}")

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        # 主线程读 stdout 的 -progress 流
        last_pct = -1.0
        assert proc.stdout is not None
        for raw in proc.stdout:
            parsed = parse_progress_line(raw)
            if not parsed:
                continue
            key, val = parsed
            if key == "out_time":
                # HH:MM:SS.ffffff -> 秒
                try:
                    parts = val.split(":")
                    secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                except (ValueError, IndexError):
                    secs = 0.0
                if duration > 0:
                    pct = min(100.0, secs / duration * 100.0)
                    if pct - last_pct >= 1.0 or pct >= 100.0:  # 每 1% 更新一次，避免刷屏
                        last_pct = pct
                        self._ui("task_progress", i, wid, pct, "")
            elif key == "progress" and val == "end":
                self._ui("task_progress", i, wid, 100.0, "")

        proc.wait()
        stderr_thread.join(timeout=2)
        with self.procs_lock:
            self.procs.pop(threading.get_ident(), None)

        elapsed = time.time() - t0

        # 被用户停止 => 取消（清理临时文件）
        if self.stop_event.is_set():
            self._cleanup(tmp_path)
            self._ui("update", i, "已取消", "用户停止", elapsed)
            self._ui("stat", "cancel")
            self._ui("log", f"[线程 {wid}] 已取消：{task.name}")
            return

        # FFmpeg 失败
        if proc.returncode != 0:
            self._cleanup(tmp_path)
            detail = err_lines[-1][:120] if err_lines else f"退出码 {proc.returncode}"
            self._ui("task_progress", i, wid, 0.0, "")
            self._ui("update", i, "失败", detail, elapsed)
            self._ui("stat", "fail")
            self._ui("log", f"[线程 {wid}] 失败：{task.name} -> {detail}")
            return

        # 输出文件无效
        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            self._cleanup(tmp_path)
            self._ui("task_progress", i, wid, 0.0, "")
            self._ui("update", i, "失败", "输出为空", elapsed)
            self._ui("stat", "fail")
            self._ui("log", f"[线程 {wid}] 失败：{task.name} -> 输出为空")
            return

        # 预编码成功：真实输出体积已知，记录为待确认项，不改动原文件
        out_size = os.path.getsize(tmp_path)
        with self.pending_lock:
            self.pending.append({
                "task": task,
                "tmp_path": tmp_path,
                "final_path": final_path,
                "delete_original": delete_original,
                "orig_size": task.size,
                "out_size": out_size,
                "elapsed": elapsed,
            })
        self._ui("stat", "ok")
        self._ui("update", i, "待确认",
                 f"{fmt_size(task.size)} → {fmt_size(out_size)}", elapsed)
        self._ui("log", f"[线程 {wid}] 预编码完成：{task.name} "
                        f"{fmt_size(task.size)} → {fmt_size(out_size)}")

    # -------------------------------------------------- 体积预测与确认 ----
    def _show_prediction_dialog(self):
        """预编码完成后，汇总真实输出体积，弹窗让用户确认是否应用。"""
        with self.pending_lock:
            pending = list(self.pending)
        ok_items = [p for p in pending]
        if not ok_items:
            messagebox.showinfo("预测结果", "没有成功预编码的文件，无需确认。")
            self.phase = "idle"
            self.btn_start.configure(state="normal")
            self.btn_clear.configure(state="normal")
            return

        total_orig = sum(p["orig_size"] for p in ok_items)
        total_out = sum(p["out_size"] for p in ok_items)
        diff = total_out - total_orig
        pct = (diff / total_orig * 100) if total_orig else 0.0
        verb = "增大" if diff > 0 else ("减小" if diff < 0 else "不变")

        # 明细（最多显示 40 条，避免对话框过大）
        lines = []
        for p in ok_items[:40]:
            lines.append(f"  {p['task'].name}: {fmt_size(p['orig_size'])} → "
                         f"{fmt_size(p['out_size'])}")
        if len(ok_items) > 40:
            lines.append(f"  ... 其余 {len(ok_items) - 40} 个文件省略")
        detail = "\n".join(lines)

        out_ext = self.snapshot.get("output_ext") or self._get_output_ext()
        action = (f"删除原文件，结果改名为原名（{out_ext}）"
                  if self.snapshot.get("delete_original") else self.ACTION_KEEP)
        msg = (f"预编码完成（尚未改动任何原文件）。\n\n"
               f"成功：{len(ok_items)} 个文件\n"
               f"原体积合计：{fmt_size(total_orig)}\n"
               f"预测输出合计：{fmt_size(total_out)}\n"
               f"变化：{verb} {fmt_size(abs(diff))}（{pct:+.1f}%）\n\n"
               f"确认后将执行：{action}\n\n"
               f"逐文件体积：\n{detail}\n\n"
               f"确定应用这些结果吗？")

        self._log(f"体积预测：{fmt_size(total_orig)} → {fmt_size(total_out)} "
                  f"（{verb} {abs(pct):.1f}%），等待用户确认。")
        if messagebox.askyesno("输出体积预测 - 确认", msg):
            self._apply_pending()
        else:
            self._discard_pending()

    def _apply_pending(self):
        """应用预编码结果：删除原文件(可选) + 原子改名。不重复编码。"""
        self.phase = "apply"
        self.running = True
        self.btn_start.configure(state="disabled")
        self.btn_clear.configure(state="disabled")
        self._log("用户确认，开始应用结果…")

        with self.pending_lock:
            pending = list(self.pending)
        applied = 0
        failed = 0
        for p in pending:
            task = p["task"]
            tmp_path = p["tmp_path"]
            final_path = p["final_path"]
            try:
                if not os.path.exists(tmp_path):
                    raise FileNotFoundError("临时文件已丢失")
                # 删除原文件：仅当目标与原文件不同名时显式删除（同名时 os.replace 直接覆盖）
                if p["delete_original"] and os.path.normcase(final_path) != os.path.normcase(task.path):
                    os.remove(task.path)
                os.replace(tmp_path, final_path)   # 同名时原子覆盖原文件
                self._handle_ui(("update", task.idx, "完成",
                                 os.path.basename(final_path), p["elapsed"]))
                applied += 1
            except OSError as e:
                self._cleanup(tmp_path)
                self._handle_ui(("update", task.idx, "失败", f"应用失败：{e}", p["elapsed"]))
                failed += 1
                self._log(f"应用失败：{task.name} -> {e}")

        with self.pending_lock:
            self.pending = []
        self.running = False
        self.phase = "idle"
        self.btn_start.configure(state="normal")
        self.btn_clear.configure(state="normal")
        self._log(f"应用完成：成功 {applied}，失败 {failed}。")

    def _discard_pending(self):
        """取消：清理所有临时文件，原文件保持不变。"""
        with self.pending_lock:
            pending = list(self.pending)
            self.pending = []
        for p in pending:
            self._cleanup(p["tmp_path"])
            task = p["task"]
            self._handle_ui(("update", task.idx, "已取消", "用户放弃", 0.0))
        self.phase = "idle"
        self.running = False
        self.btn_start.configure(state="normal")
        self.btn_clear.configure(state="normal")
        self._log(f"用户取消，已清理 {len(pending)} 个临时文件，原文件未改动。")

    @staticmethod
    def _cleanup(path: str):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    @staticmethod
    def _unique_path(path: str) -> str:
        """若目标已存在，追加 _1 / _2 ... 避免覆盖"""
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        n = 1
        while True:
            cand = f"{base}_{n}{ext}"
            if not os.path.exists(cand):
                return cand
            n += 1


# ----------------------------------------------------------------------------
# 解码/转码页：把无损 AVI 解码成通用格式（默认 mp4 + H.264 + yuv420p + aac + CRF23）
# 复用 AviLosslessApp 的全部引擎，仅替换选项区与 FFmpeg 参数生成
# ----------------------------------------------------------------------------
class DecodeApp(AviLosslessApp):
    PAGE_NAME = "解码"
    OUTPUT_SUFFIX = "_decoded"
    OPT_TITLE = "解码 / 转码设置"
    START_TEXT = "开始解码"

    # 格式 -> (扩展名, 首选音频编码器)
    FORMATS = {
        "mp4": (".mp4", "AAC"),
        "mkv": (".mkv", "AAC"),
        "mov": (".mov", "AAC"),
        "avi": (".avi", "MP3"),
        "webm": (".webm", "Opus"),
        "flv": (".flv", "AAC"),
    }
    # 编码器显示名 -> ffmpeg -c:v 值
    ENCODERS = {
        "H.264": "libx264",
        "H.265": "libx265",
        "VP9": "libvpx-vp9",
        "AV1": "libaom-av1",
        "MPEG-4": "mpeg4",
        "FFV1 (无损)": "ffv1",
    }
    PIX_FMTS = ["yuv420p", "yuv422p", "yuv444p", "yuv420p10le", "yuv444p10le", "rgb24"]
    # 音频显示名 -> (ffmpeg -c:a 值, 额外参数)
    AUDIO_CODECS = {
        "自动（与格式兼容）": None,           # 由格式决定
        "AAC": ("aac", []),
        "MP3": ("libmp3lame", []),
        "FLAC": ("flac", []),
        "Opus": ("libopus", []),
        "AC3": ("ac3", []),
        "PCM s16le": ("pcm_s16le", []),
        "不保留音频": ("NONE", []),
    }
    QUALITIES = ["18", "20", "21", "23", "25", "28", "32"]  # CRF，越小质量越高

    def _build_options(self, frm_opt):
        self.delete_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frm_opt,
            text="删除原文件（解码后删除原 AVI，结果用原名 + 新格式扩展名）",
            variable=self.delete_var,
        ).grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 6))

        self.var_format = tk.StringVar(value="mp4")
        self.var_pixfmt = tk.StringVar(value="yuv420p")
        self.var_encoder = tk.StringVar(value="H.264")
        self.var_audio = tk.StringVar(value="自动（与格式兼容）")
        self.var_quality = tk.StringVar(value="23")

        specs = [
            ("格式：", self.var_format, list(self.FORMATS.keys()), 0, 0),
            ("像素格式：", self.var_pixfmt, self.PIX_FMTS, 0, 2),
            ("编码器：", self.var_encoder, list(self.ENCODERS.keys()), 0, 4),
            ("音频流：", self.var_audio, list(self.AUDIO_CODECS.keys()), 1, 0),
            ("画质(CRF)：", self.var_quality, self.QUALITIES, 1, 2),
        ]
        for label, var, values, row, col in specs:
            ttk.Label(frm_opt, text=label).grid(row=row, column=col, sticky="w", pady=2)
            cb = ttk.Combobox(frm_opt, textvariable=var, values=values,
                              state="readonly", width=18)
            cb.grid(row=row, column=col + 1, sticky="ew", padx=(4, 10), pady=2)
            cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_cmd_preview())
        frm_opt.columnconfigure(1, weight=1)
        frm_opt.columnconfigure(3, weight=1)
        frm_opt.columnconfigure(5, weight=1)

        # 命令预览（只读）
        self.cmd_preview = tk.StringVar(value="")
        ttk.Label(frm_opt, text="生成命令：", foreground="#555").grid(
            row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(frm_opt, textvariable=self.cmd_preview, state="readonly",
                  foreground="#444").grid(row=2, column=1, columnspan=5,
                                          sticky="ew", padx=(4, 0), pady=(4, 0))
        ttk.Label(
            frm_opt, foreground="#555",
            text=("默认：mp4 + H.264 + yuv420p + AAC + CRF23；音频默认取与格式最兼容者。"
                  "画质对 FFV1 无效、对 MPEG-4 自动换算。命令中不含编码速度参数（如 -preset）。"),
        ).grid(row=3, column=0, columnspan=6, sticky="w", pady=(4, 0))
        self._refresh_cmd_preview()

    def _refresh_cmd_preview(self):
        try:
            params = self._build_params()
            self.cmd_preview.set("ffmpeg -i <input> " + " ".join(params) + " <output>")
        except Exception as e:
            self.cmd_preview.set(f"参数生成失败：{e}")

    def _get_output_ext(self) -> str:
        return self.FORMATS[self.var_format.get()][0]

    def _final_name(self, task: Task, delete_original: bool) -> str:
        """删除原文件时用原文件名 + 新格式扩展名；否则加 _decoded 后缀。"""
        out_ext = self.snapshot.get("output_ext") or self._get_output_ext()
        if delete_original:
            return f"{task.stem}{out_ext}"
        return f"{task.stem}{self.OUTPUT_SUFFIX}{out_ext}"

    def _audio_codec(self, fmt_key: str):
        """返回 (c:a 值, 额外参数)；自动模式按格式选最兼容音频。"""
        choice = self.var_audio.get()
        if choice == "不保留音频":
            return ("NONE", [])
        if choice == "自动（与格式兼容）":
            name = self.FORMATS[fmt_key][1]
            return self.AUDIO_CODECS[name]
        return self.AUDIO_CODECS[choice]

    def _build_params(self) -> list[str]:
        fmt_key = self.var_format.get()
        enc = self.ENCODERS[self.var_encoder.get()]
        pixfmt = self.var_pixfmt.get()
        quality = int(self.var_quality.get())
        a_codec, a_extra = self._audio_codec(fmt_key)

        params = ["-map", "0:v:0"]
        if a_codec != "NONE":
            params += ["-map", "0:a?"]

        # 视频编码与画质
        params += ["-c:v", enc]
        if enc in ("libx264", "libx265"):
            params += ["-crf", str(quality)]
        elif enc == "libvpx-vp9":
            params += ["-b:v", "0", "-crf", str(quality)]
        elif enc == "libaom-av1":
            params += ["-crf", str(quality)]
        elif enc == "mpeg4":
            # CRF(0 best..51 worst) -> q:v(1 best..31 worst)，线性反向映射
            q = max(1, min(31, round(quality / 51 * 30) + 1))
            params += ["-q:v", str(q)]
        # ffv1 无损：无画质参数
        params += ["-pix_fmt", pixfmt]

        # 音频
        if a_codec == "NONE":
            params += ["-an"]
        else:
            params += ["-c:a", a_codec] + list(a_extra)
        return params

    def _show_prediction_dialog(self):
        """解码页沿用体积预测，但动作文案使用新格式扩展名。"""
        self.ACTION_KEEP = (
            f"保留原文件，另存为 *{self.snapshot.get('output_ext') or self._get_output_ext()}"
            f"（后缀 {self.OUTPUT_SUFFIX}）"
        )
        super()._show_prediction_dialog()


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    root = tk.Tk()
    root.title("Multimedia Processing Tool")
    root.geometry("1040x820")
    root.minsize(920, 640)

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)

    tab_compress = ttk.Frame(notebook)
    tab_decode = ttk.Frame(notebook)
    notebook.add(tab_compress, text="无损压缩 (FFV1)")
    notebook.add(tab_decode, text="解码 / 转码")

    compress_app = AviLosslessApp(root, parent=tab_compress)
    decode_app = DecodeApp(root, parent=tab_decode)

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
