# -*- coding: utf-8 -*-
"""
ATcith 一体化工具（由 main.py / avi_lossless_gui.py / del.py / Copy.py 合并）

标签页：
  1. AVI 转 MP4      —— 多进程动态领取，H.264 + AAC
  2. 无损压缩 (FFV1) —— 逐文件预编码，确认后应用；PCM 原样复制
  3. 解码 / 转码     —— 格式/像素格式/编码器/音频/画质下拉
  4. 复制 / 移动     —— 多进程分块传输，冲突询问，移动支持同盘改名与跨盘搬运
  5. 批量删除        —— 后缀筛选、二次确认、单线程后台删除

原四个脚本保留不变。本文件为单文件交付，可直接运行：
  py -3.15 AllInOne.py
"""

from __future__ import annotations

import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import Manager
from tkinter import ttk, filedialog, messagebox, scrolledtext
import tkinter as tk


# ============================================================
# 来自 main.py：AVI 批量转 MP4
# ============================================================

# ==================== 子进程任务函数 ====================
def convert_single_file(input_path, output_path, crf, audio_br, overwrite,
                        progress_queue, task_id, stop_event):
    """
    子进程任务：
    - 先写临时文件，成功后重命名
    - 被中断或失败时自动删除临时文件
    - 每行读取都检查 stop_event
    - 启动后主动上报 started/error，错误不再被吞
    """
    tmp_path = output_path + ".tmp.mp4"
    process = None
    success = False
    base = os.path.basename(input_path)

    try:
        if stop_event.is_set():
            progress_queue.put((task_id, "skip", 0, base))
            return False

        # 获取时长
        duration = 0.0
        try:
            probe_cmd = [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", input_path
            ]
            result = subprocess.run(probe_cmd, capture_output=True, text=True,
                                    encoding="utf-8", errors="ignore", timeout=10,
                                    env=os.environ.copy())
            duration = float(result.stdout.strip())
        except Exception:
            pass

        # 检查覆盖策略（针对最终输出文件）
        if os.path.exists(output_path) and not overwrite:
            progress_queue.put((task_id, "skip", 100, base))
            return True

        # 命令先处理（构建列表）再执行：不经 shell、不经 PowerShell、无通配符展开风险
        cmd = build_convert_cmd(input_path, tmp_path, crf, audio_br)

        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

        # 关键修复点：显式复制环境变量，避免 spawn 子进程 PATH 丢失导致 ffmpeg 找不到
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="ignore",
                env=os.environ.copy(),
                shell=False,  # 关键：直接执行列表，不走 shell，避免通配符/PowerShell 问题
                creationflags=creation_flags
            )
        except FileNotFoundError:
            _safe_remove(tmp_path)
            progress_queue.put((task_id, "error", 0,
                                f"[找不到ffmpeg] {base} 请确认 ffmpeg 在 PATH"))
            return False

        # 启动后立即检测是否意外秒退（参数错误/无法打开输入等）
        retcode = process.poll()
        if retcode is not None:
            try:
                out = process.stdout.read() if process.stdout else ""
            except Exception:
                out = ""
            _safe_remove(tmp_path)
            snippet = (out or "").strip().replace("\n", " | ")[:200]
            progress_queue.put((task_id, "error", 0,
                                f"{base} ffmpeg立即退出(code={retcode}) {snippet}"))
            return False

        # ffmpeg 已真正启动：上报 started，进度条从"空闲"切换为"启动中"
        progress_queue.put((task_id, "started", 0, base))

        time_pattern = re.compile(r'time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})')
        last_report_time = 0

        for line in process.stdout:
            # 逐行检查停止信号
            if stop_event.is_set():
                process.kill()
                try:
                    process.wait(timeout=3)
                except Exception:
                    pass
                _safe_remove(tmp_path)
                progress_queue.put((task_id, "skip", 0, f"[已中断] {base}"))
                return False

            match = time_pattern.search(line)
            if match and duration > 0:
                h, m, s, cs = map(int, match.groups())
                current_sec = h * 3600 + m * 60 + s + cs / 100.0
                pct = min(99.9, (current_sec / duration) * 100)
                now = time.time()
                if now - last_report_time > 0.3:
                    progress_queue.put((task_id, "running", pct, base))
                    last_report_time = now
            else:
                # 非进度行：转发有信息量的 FFmpeg 输出（版本/流信息/错误/警告）
                stripped = line.strip()
                if stripped and any(kw in stripped.lower() for kw in (
                        "error", "invalid", "warning", "unable", "failed",
                        "input #", "output #", "stream #", "duration:", "video:", "audio:")):
                    progress_queue.put((task_id, "log", 0, f"[{base}] {stripped[:160]}"))

        process.wait()
        success = (process.returncode == 0)

        if success:
            try:
                os.replace(tmp_path, output_path)
            except OSError as e:
                success = False
                progress_queue.put((task_id, "error", 0,
                                    f"{base}: 重命名失败 {e}"))
                _safe_remove(tmp_path)
                return False
            progress_queue.put((task_id, "done", 100, base))
        else:
            _safe_remove(tmp_path)
            progress_queue.put((task_id, "error", 100,
                                f"{base} (code={process.returncode})"))

        return success

    except Exception as e:
        _safe_remove(tmp_path)
        progress_queue.put((task_id, "error", 0,
                            f"{base}: {type(e).__name__}: {e}"))
        return False
    finally:
        # 兜底：确保子进程和临时文件都被清理
        if process and process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass
        if not success:
            _safe_remove(tmp_path)


def _safe_remove(path):
    """安全删除文件，忽略不存在或权限错误"""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def build_convert_cmd(input_path, output_path, crf, audio_br):
    """
    命令先处理阶段：按固定模板构建 ffmpeg 命令列表。
    模板: ffmpeg -i input.avi -c:v libx264 -crf 23 -c:a aac -b:a 128k -pix_fmt yuv420p output.mp4
    返回 list（供 shell=False 直接执行），通配符/特殊字符不会被 shell 解析。
    """
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",
        "-y",                      # 临时文件始终覆盖
        "-i", input_path,
        "-c:v", "libx264",
        "-crf", str(crf),
        "-c:a", "aac",
        "-b:a", audio_br,
        "-pix_fmt", "yuv420p",
        output_path,
    ]


def worker_loop(worker_id, task_queue, processed_counter, counter_lock, crf, audio_br,
                overwrite, progress_queue, stop_event):
    """
    顶层 worker 函数（在独立子进程中长期运行）。
    动态分配模型：
    - 每个 worker 从共享 task_queue 中领取 1 个文件任务；
    - 完成（成功/失败/跳过）后立即回到队首再领下一个；
    - 队列被取空（Empty）即自然结束 -> "任一子进程结束/任务结束就领取新任务"由队列天然保证。
    返回该 worker 累计处理的文件数。
    """
    handled = 0
    try:
        while not stop_event.is_set():
            try:
                # 从共享清单领取下一个任务；取不到说明清单已空 -> 退出
                input_path = task_queue.get_nowait()
            except queue.Empty:
                # 清单已耗尽，本 worker 结束
                progress_queue.put((worker_id, "idle", 0, ""))
                break

            output_path = os.path.splitext(input_path)[0] + ".mp4"
            try:
                convert_single_file(
                    input_path, output_path, crf, audio_br, overwrite,
                    progress_queue, worker_id, stop_event
                )
            except Exception as e:
                # 单文件级兜底：不让一个文件异常导致 worker 退出
                progress_queue.put((worker_id, "error", 0,
                                    f"{os.path.basename(input_path)}: worker内异常 {e}"))
            finally:
                # 无论成功/失败/跳过，都计为"已从清单领取并处理完毕"
                handled += 1
                # 关键修复：Manager().Value 的 ValueProxy 没有 get_lock()，
                # 改用显式的 Manager().Lock() 加锁，避免 AttributeError。
                counter_lock.acquire()
                try:
                    processed_counter.value += 1
                finally:
                    counter_lock.release()
        return handled
    except Exception as e:
        tb = traceback.format_exc()
        progress_queue.put((worker_id, "error", 0,
                            f"[worker异常] {type(e).__name__}: {e} | {''.join(tb.splitlines(True)[-4:])}"))
        return handled


# ==================== GUI 主类 ====================
class AVIConverterMulti:
    CLEANUP_TIMEOUT = 5

    def __init__(self, root, parent=None):
        self.root = root
        self.parent = parent if parent is not None else root
        if parent is None:
            self.root.title("AVI批量转MP4 (多进程·动态分配)")
            self.root.geometry("1080x840")
            self.root.minsize(920, 620)

        self.is_running = False
        self.stop_event = None
        self.progress_queue = None
        self.task_queue = None
        self.processed_counter = None
        self.counter_lock = None
        self.manager = None
        self.executor = None
        self.max_workers = max(1, (os.cpu_count() or 2) - 1)
        self._cleanup_done = False

        self._build_ui()
        if parent is None:
            self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

    def _build_ui(self):
        root = self.parent
        # 顶层 grid 权重：清单与日志共享剩余空间，互不挤占
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=0)   # 路径
        root.rowconfigure(1, weight=0)   # 设置
        root.rowconfigure(2, weight=0)   # 按钮
        root.rowconfigure(3, weight=3)   # 任务清单（可伸缩）
        root.rowconfigure(4, weight=0)   # 各进程进度
        root.rowconfigure(5, weight=0)   # 总进度+状态
        root.rowconfigure(6, weight=2)   # 日志（可伸缩）

        main_frame = ttk.Frame(root, padding=10)
        main_frame.grid(row=0, column=0, rowspan=7, sticky="nsew")
        main_frame.columnconfigure(0, weight=1)
        for r, w in ((0, 0), (1, 0), (2, 0), (3, 3), (4, 0), (5, 0), (6, 2)):
            main_frame.rowconfigure(r, weight=w)

        # 路径选择
        path_frame = ttk.LabelFrame(main_frame, text="源文件夹 (递归扫描)", padding=5)
        path_frame.grid(row=0, column=0, sticky="ew", pady=5)
        self.path_var = tk.StringVar()
        ttk.Entry(path_frame, textvariable=self.path_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(path_frame, text="浏览...", command=self._browse_folder).pack(side=tk.RIGHT)

        # 编码设置
        settings_frame = ttk.LabelFrame(main_frame, text="FFmpeg 编码设置", padding=5)
        settings_frame.grid(row=1, column=0, sticky="ew", pady=5)
        row1 = ttk.Frame(settings_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="CRF (画质):").pack(side=tk.LEFT)
        self.crf_var = tk.IntVar(value=23)
        ttk.Spinbox(row1, from_=0, to=51, textvariable=self.crf_var, width=5).pack(
            side=tk.LEFT, padx=5)
        ttk.Label(row1, text="音频码率:").pack(side=tk.LEFT, padx=(15, 0))
        self.audio_br_var = tk.StringVar(value="128k")
        ttk.Combobox(row1, textvariable=self.audio_br_var,
                     values=["96k", "128k", "192k", "320k"], width=8).pack(
            side=tk.LEFT, padx=5)
        ttk.Label(row1, text=f"进程数: {self.max_workers} (动态领取)").pack(side=tk.RIGHT)

        row2 = ttk.Frame(settings_frame)
        row2.pack(fill=tk.X, pady=2)
        self.overwrite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="覆盖已存在的 MP4 (-y)，否则跳过 (-n)",
                        variable=self.overwrite_var).pack(anchor=tk.W)

        # 控制按钮
        ctrl_frame = ttk.Frame(main_frame)
        ctrl_frame.grid(row=2, column=0, sticky="ew", pady=5)
        self.start_btn = ttk.Button(ctrl_frame, text="开始转换", command=self._start_conversion)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = ttk.Button(ctrl_frame, text="停止所有任务",
                                   command=self._user_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.delete_mp4_btn = ttk.Button(ctrl_frame, text="删除所有MP4",
                                         command=self._scan_and_delete_mp4)
        self.delete_mp4_btn.pack(side=tk.LEFT, padx=5)

        # ---- 任务清单（含进度列）----
        list_frame = ttk.LabelFrame(main_frame, text="任务清单", padding=5)
        list_frame.grid(row=3, column=0, sticky="nsew", pady=5)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        cols = ("no", "status", "file", "progress")
        self.file_tree = ttk.Treeview(list_frame, columns=cols, show="headings", height=6)
        for c, txt, w in (("no", "#", 44), ("status", "状态", 80),
                          ("file", "文件路径", 640), ("progress", "进度", 80)):
            self.file_tree.heading(c, text=txt)
            self.file_tree.column(c, width=w, anchor="w", stretch=(c == "file"))
        lvsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=lvsb.set)
        self.file_tree.grid(row=0, column=0, sticky="nsew")
        lvsb.grid(row=0, column=1, sticky="ns")
        self._file_rows = {}   # 路径 -> iid

        # 各进程进度条（横排）
        progress_container = ttk.LabelFrame(main_frame, text="各进程实时进度", padding=5)
        progress_container.grid(row=4, column=0, sticky="ew", pady=5)
        self.progress_bars_frame = ttk.Frame(progress_container)
        self.progress_bars_frame.pack(fill=tk.X)

        # 总进度 + 状态（带百分比）
        total_frame = ttk.Frame(main_frame)
        total_frame.grid(row=5, column=0, sticky="ew", pady=(5, 0))
        total_frame.columnconfigure(0, weight=1)
        self.total_progress = ttk.Progressbar(total_frame, orient=tk.HORIZONTAL, mode='determinate')
        self.total_progress.grid(row=0, column=0, sticky="ew")
        self.status_label = ttk.Label(total_frame, text="就绪", anchor=tk.W)
        self.status_label.grid(row=1, column=0, sticky="w", pady=(2, 0))

        # 日志（只读：state=disabled，程序写入时临时解锁）
        log_frame = ttk.LabelFrame(main_frame, text="运行日志 (只读)", padding=5)
        log_frame.grid(row=6, column=0, sticky="nsew", pady=5)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=6, font=("Consolas", 9),
                                                  state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.grid(row=0, column=0, sticky="nsew")

        self._bar_widgets = {}
        self._total_expected = 0

    def _browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.path_var.set(folder)

    def _log(self, message):
        try:
            self.log_text.config(state=tk.NORMAL)   # 临时解锁
            self.log_text.insert(tk.END, f"{message}\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)  # 立即锁定为只读
            self.root.update_idletasks()
        except Exception:
            pass

    def _get_avi_files(self, root_dir):
        avi_files = []
        for dirpath, _, filenames in os.walk(root_dir):
            for fn in filenames:
                if fn.lower().endswith(".avi"):
                    avi_files.append(os.path.join(dirpath, fn))
        return sorted(avi_files)

    def _create_progress_bars(self, count):
        for w in self.progress_bars_frame.winfo_children():
            w.destroy()
        self._bar_widgets.clear()
        for i in range(count):
            self.progress_bars_frame.columnconfigure(i, weight=1)
            cell = ttk.Frame(self.progress_bars_frame, padding=(0, 0, 8, 0))
            cell.grid(row=0, column=i, sticky="ew")
            label = ttk.Label(cell, text=f"进程{i+1}: 空闲", anchor=tk.W)
            label.pack(fill=tk.X)
            bar = ttk.Progressbar(cell, orient=tk.HORIZONTAL, mode='determinate', maximum=100)
            bar.pack(fill=tk.X, pady=(2, 0))
            self._bar_widgets[i] = {"bar": bar, "label": label}

    def _update_file_row(self, filename, status, pct):
        """同步任务清单中某文件的状态与进度。filename 是 basename。"""
        iid = self._file_rows.get(filename)
        if iid is None:
            return
        try:
            self.file_tree.item(
                iid,
                values=(self.file_tree.item(iid, "values")[0], status,
                        self.file_tree.item(iid, "values")[2], f"{min(100, pct):.0f}%"),
            )
        except (tk.TclError, IndexError):
            pass

    def _update_bar(self, task_id, status, pct, filename):
        # 同步任务清单
        if filename:
            row_status = {
                "started": "转换中", "running": "转换中", "done": "完成",
                "error": "失败", "skip": "跳过", "idle": "空闲",
            }.get(status, status)
            self._update_file_row(filename, row_status, pct)
        if task_id not in self._bar_widgets:
            return
        widgets = self._bar_widgets[task_id]
        widgets["bar"]["value"] = pct
        short = filename if len(filename) <= 28 else filename[:25] + "..."
        if not filename:
            widgets["label"].config(text=f"进程{task_id+1}: 已完成(空闲)")
            widgets["bar"]["value"] = 100
            return
        status_map = {
            "started": f"进程{task_id+1}: [启动中] {short}",
            "running": f"进程{task_id+1}: {short} ({pct:.1f}%)",
            "done":    f"进程{task_id+1}: [OK] {short}",
            "error":   f"进程{task_id+1}: [ERR] {short}",
            "skip":    f"进程{task_id+1}: [SKIP] {short}",
            "idle":    f"进程{task_id+1}: 已完成(空闲)",
        }
        widgets["label"].config(text=status_map.get(status, f"进程{task_id+1}: {short}"))
        if status in ("done", "skip"):
            widgets["bar"]["value"] = 100

    def _poll_progress_queue(self):
        if self.progress_queue is None:
            return
        try:
            while True:
                task_id, status, pct, filename = self.progress_queue.get_nowait()
                if status == "log":
                    # 子进程转发的 FFmpeg 实时输出
                    self._log(filename)
                    continue
                self._update_bar(task_id, status, pct, filename)
                # 错误与完成都写一份日志，便于诊断
                if status == "error":
                    self._log(f"[ERR] {filename}")
                elif status == "done":
                    self._log(f"[OK] {filename}")
        except queue.Empty:
            pass
        # 同步总进度：读取共享 processed_counter
        try:
            if self.processed_counter is not None:
                done = self.processed_counter.value
                self.total_progress.configure(value=done)
                total = max(1, self._total_expected)
                self.status_label.config(
                    text=f"总进度：{done}/{total}（{done / total * 100:.0f}%）")
        except Exception:
            pass
        if self.is_running:
            self.root.after(100, self._poll_progress_queue)

    # ==================== 统一清理机制 ====================
    def _cleanup_and_exit(self, force_destroy=True):
        if self._cleanup_done:
            if force_destroy:
                try:
                    self.root.destroy()
                except Exception:
                    pass
            return

        self._cleanup_done = True
        self.is_running = False

        if self.stop_event is not None:
            try:
                self.stop_event.set()
            except Exception:
                pass

        if self.executor is not None:
            try:
                self.executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

        def _wait_then_destroy():
            if self.executor is not None:
                try:
                    self.executor.shutdown(wait=True, cancel_futures=True)
                except Exception:
                    pass
            if force_destroy:
                try:
                    self.root.after(0, self.root.destroy)
                except Exception:
                    os._exit(0)

        threading.Thread(target=_wait_then_destroy, daemon=True).start()

    def _on_window_close(self):
        if self.is_running:
            if messagebox.askyesno("确认退出", "转换正在进行中，确定要退出吗？\n所有子进程将被终止。"):
                self._log("用户关闭窗口，正在终止所有子进程...")
                self._cleanup_and_exit(force_destroy=True)
        else:
            self.root.destroy()

    def _user_stop(self):
        """Stop 按钮：停止所有任务但不关闭窗口"""
        self._log("用户点击停止，正在终止所有子进程...")
        self._cleanup_and_exit(force_destroy=False)
        self._reset_ui()

    # ==================== 删除 MP4 功能 ====================
    def _scan_and_delete_mp4(self):
        folder = self.path_var.get()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("错误", "请先选择有效的文件夹！")
            return

        mp4_files = []
        for dirpath, _, filenames in os.walk(folder):
            for fn in filenames:
                if fn.lower().endswith(".mp4"):
                    mp4_files.append(os.path.join(dirpath, fn))

        if not mp4_files:
            messagebox.showinfo("提示", "未找到任何 .mp4 文件。")
            return

        if not self._show_delete_confirm_dialog(mp4_files):
            return

        failed_files = []
        for f in mp4_files:
            try:
                os.remove(f)
            except Exception as e:
                failed_files.append((f, str(e)))

        if failed_files:
            err_msg = f"有 {len(failed_files)} 个文件删除失败（可能被占用）：\n\n"
            for f, e in failed_files:
                err_msg += f"{os.path.basename(f)}: {e}\n"
            if len(err_msg) > 2000:
                err_msg = err_msg[:2000] + "\n...(信息过长已截断)"
            messagebox.showerror("删除完成（有错误）", err_msg)
        else:
            messagebox.showinfo("完成", f"成功删除 {len(mp4_files)} 个 MP4 文件。")

    def _show_delete_confirm_dialog(self, files):
        """自定义弹窗显示文件列表并等待确认"""
        dialog = tk.Toplevel(self.root)
        dialog.title("确认删除 MP4 文件")
        dialog.geometry("650x450")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text=f"共扫描到 {len(files)} 个 MP4 文件，确认要全部删除吗？",
                  font=("Microsoft YaHei", 10, "bold")).pack(pady=10)

        text_frame = ttk.Frame(dialog)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        scrollbar = ttk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        listbox = tk.Listbox(text_frame, yscrollcommand=scrollbar.set, font=("Consolas", 9))
        listbox.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=listbox.yview)

        for f in files:
            listbox.insert(tk.END, f)

        result = {"confirmed": False}

        def on_confirm():
            result["confirmed"] = True
            dialog.destroy()

        def on_cancel():
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="确认删除", command=on_confirm, width=15).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="取消", command=on_cancel, width=15).pack(side=tk.LEFT, padx=10)

        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        self.root.wait_window(dialog)
        return result["confirmed"]

    # ==================== 启动转换（动态分配） ====================
    def _start_conversion(self):
        folder = self.path_var.get()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("错误", "请选择有效的文件夹！")
            return

        files = self._get_avi_files(folder)
        if not files:
            messagebox.showinfo("提示", "未找到任何 .avi 文件。")
            return

        self.is_running = True
        self._cleanup_done = False
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.delete_mp4_btn.config(state=tk.DISABLED)

        self._total_expected = len(files)
        self.total_progress["maximum"] = len(files)
        self.total_progress["value"] = 0
        self.status_label.config(text=f"总进度：0/{len(files)}（0%）")
        self._create_progress_bars(self.max_workers)

        # 填充任务清单
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        self._file_rows.clear()
        for i, fp in enumerate(files, start=1):
            iid = self.file_tree.insert("", "end", values=(i, "等待", fp, "-"))
            self._file_rows[os.path.basename(fp)] = iid

        self.manager = Manager()
        self.progress_queue = self.manager.Queue()
        self.stop_event = self.manager.Event()
        self.task_queue = self.manager.Queue()
        self.processed_counter = self.manager.Value('i', 0)
        self.counter_lock = self.manager.Lock()

        # 把所有任务放进共享清单（动态分配来源）
        for fp in files:
            self.task_queue.put(fp)

        # 入队守恒校验：清单数量必须等于扫描到的文件数
        if self.task_queue.qsize() != len(files):
            self._log(f"任务入队校验失败：清单={self.task_queue.qsize()}，文件={len(files)}")
            messagebox.showerror("任务分配错误",
                                 f"入队数 {self.task_queue.qsize()} != 文件数 {len(files)}")
            self._reset_ui()
            return

        self._log(f"找到 {len(files)} 个文件，{self.max_workers} 个进程，"
                  f"采用动态清单分配（空闲即领取下一个任务）")

        thread = threading.Thread(target=self._conversion_thread, daemon=True)
        thread.start()
        self._poll_progress_queue()

    def _conversion_thread(self):
        try:
            self.executor = ProcessPoolExecutor(max_workers=self.max_workers)
            future_map = {}

            # 每个 worker 提交为一个"长期领取"任务，内部自取 task_queue
            for slot_id in range(self.max_workers):
                fut = self.executor.submit(
                    worker_loop,
                    slot_id,
                    self.task_queue, self.processed_counter, self.counter_lock,
                    self.crf_var.get(), self.audio_br_var.get(),
                    self.overwrite_var.get(),
                    self.progress_queue, self.stop_event
                )
                future_map[fut] = slot_id

            for future in as_completed(future_map):
                slot_id = future_map[future]
                try:
                    future.result()
                except Exception as e:
                    tb = traceback.format_exc()
                    self._log(f"[严重错误] 进程{slot_id+1}异常: {e}")
                    self._log("".join(tb.splitlines(True)[-6:]))
                    self.root.after(0, lambda s=slot_id: self._update_bar(
                        s, "error", 0, "worker异常(详见日志)"))

            # 完成守恒校验：处理总数应等于文件总数
            try:
                processed = self.processed_counter.value
            except Exception:
                processed = -1
            if processed != self._total_expected:
                self._log(f"[分配守恒校验失败] 已处理={processed}，"
                          f"应为={self._total_expected}，差={self._total_expected - processed}")
                self.root.after(0, lambda p=processed, t=self._total_expected:
                                messagebox.showerror(
                                    "任务守恒校验失败",
                                    f"已处理 {p} 个文件，应处理 {t} 个。\n"
                                    f"差额 {t - p}（可能因 Stop 提前中断或异常）。"))
            else:
                self._log(f"[守恒校验通过] 全部 {processed} 个文件处理完毕")

        except Exception as e:
            tb = traceback.format_exc()
            self._log(f"严重错误: {e}")
            self._log("".join(tb.splitlines(True)[-6:]))
        finally:
            if self.executor is not None:
                try:
                    self.executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
            self.executor = None
            self.root.after(0, self._reset_ui)

    def _reset_ui(self):
        self.is_running = False
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.delete_mp4_btn.config(state=tk.NORMAL)
        self.status_label.config(text="就绪")
        self._log("任务结束。")


# ============================================================
# 来自 Copy.py：多进程复制 / 移动
# ============================================================

# ================= 子进程工作函数 =================
def worker_process(worker_id, task_queue, ask_queue, response_queue,
                   progress_val, success_count, skip_count,
                   cancel_event, lock, worker_status, log_queue, mode="copy"):
    """
    子进程：通过 Queue.get() 安全顺序领取任务（Queue 本身已保证原子性，
    无需额外加锁），分块复制或移动，实时更新自身状态到共享字典。

    mode="copy"：复制，源文件保留。
    mode="move"：移动；同盘用 os.replace 原子改名，跨盘先写临时文件，
                 成功后原子替换目标，最后删除源文件。
    """
    pid = os.getpid()
    action = "移动" if mode == "move" else "复制"
    tmp_prefix = ".__mv_" if mode == "move" else ".__cp_"
    log_queue.put(f"[W{worker_id}] 进程启动 (PID {pid})，模式：{action}")
    with lock:
        worker_status[worker_id] = {"pid": pid, "file": "等待任务...", "bytes": 0, "status": "空闲"}

    while not cancel_event.is_set():
        try:
            task = task_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        if task is None:  # 毒丸信号
            break

        src_path, dst_dir, filename = task
        dst_path = os.path.join(dst_dir, filename)

        # 源与目标完全相同：无论复制还是移动都应跳过，避免自我覆盖
        if os.path.normcase(os.path.abspath(src_path)) == os.path.normcase(os.path.abspath(dst_path)):
            log_queue.put(f"[W{worker_id}] 跳过（源与目标相同）：{filename}")
            with lock:
                skip_count.value += 1
                worker_status[worker_id] = {"pid": pid, "file": filename, "bytes": 0, "status": "已跳过"}
            continue

        log_queue.put(f"[W{worker_id}] 开始{action}：{filename}")
        with lock:
            worker_status[worker_id] = {"pid": pid, "file": filename, "bytes": 0, "status": f"{action}中"}

        # 处理同名冲突 (跨进程 IPC 弹窗)
        if os.path.exists(dst_path):
            with lock:
                info = dict(worker_status[worker_id])
                info["status"] = "等待用户确认"
                worker_status[worker_id] = info

            req_id = str(uuid.uuid4())
            # 带上 worker_id，GUI 会把答复投回该 worker 专属队列，避免多 worker 串话
            ask_queue.put((req_id, filename, action, worker_id))

            choice = None
            while choice is None and not cancel_event.is_set():
                try:
                    res_id, res_choice = response_queue.get(timeout=0.5)
                    if res_id == req_id:
                        choice = res_choice
                    # 其他 req_id 属于历史残留，直接丢弃；专属队列不会串走本 worker 的答复
                except queue.Empty:
                    continue

            if cancel_event.is_set():
                break
            if choice == 'cancel':
                cancel_event.set()
                break
            elif choice == 'skip':
                log_queue.put(f"[W{worker_id}] 跳过（用户选择）：{filename}")
                with lock:
                    skip_count.value += 1
                    worker_status[worker_id] = {"pid": pid, "file": filename, "bytes": 0, "status": "已跳过"}
                continue
            else:
                log_queue.put(f"[W{worker_id}] 覆盖（用户选择）：{filename}")
            # overwrite: 继续执行下方传输逻辑

        if cancel_event.is_set():
            break

        # 传输：先写临时文件，成功后再原子替换目标，避免取消/失败留下半成品
        try:
            if not os.path.isfile(src_path):
                raise FileNotFoundError("源文件不存在")
            file_size = os.path.getsize(src_path)

            # 同盘移动可以直接原子改名，无需复制数据，源文件会被 rename 掉
            if (mode == "move" and os.path.exists(dst_dir)
                    and os.stat(src_path).st_dev == os.stat(dst_dir).st_dev):
                os.replace(src_path, dst_path)
                with lock:
                    progress_val.value += file_size
                    success_count.value += 1
                    worker_status[worker_id] = {
                        "pid": pid, "file": filename, "bytes": file_size, "status": "完成"
                    }
                log_queue.put(f"[W{worker_id}] 完成：{filename}"
                              f"（{file_size / (1024 * 1024):.1f} MB，同盘移动）")
                continue

            tmp_path = os.path.join(
                dst_dir,
                f"{tmp_prefix}{worker_id}_{uuid.uuid4().hex}{os.path.splitext(filename)[1]}"
            )
            copied = 0
            try:
                with open(src_path, 'rb') as fsrc, open(tmp_path, 'wb') as fdst:
                    while not cancel_event.is_set():
                        chunk = fsrc.read(2 * 1024 * 1024)  # 2MB
                        if not chunk:
                            break
                        fdst.write(chunk)
                        copied += len(chunk)

                        with lock:
                            progress_val.value += len(chunk)
                            pct = (copied * 100 // file_size) if file_size > 0 else 100
                            worker_status[worker_id] = {
                                "pid": pid,
                                "file": filename,
                                "bytes": copied,
                                "status": f"{action}中 ({pct}%)"
                            }

                if cancel_event.is_set():
                    raise RuntimeError("用户取消")

                # copystat 单独保护，避免权限问题导致整个任务失败
                try:
                    shutil.copystat(src_path, tmp_path)
                except (OSError, PermissionError):
                    pass

                os.replace(tmp_path, dst_path)
                tmp_path = None   # 已被 rename，finally 不再清理

                delete_error = None
                if mode == "move":
                    try:
                        os.remove(src_path)
                    except OSError as e:
                        delete_error = e

                with lock:
                    if delete_error:
                        worker_status[worker_id] = {
                            "pid": pid, "file": filename, "bytes": copied,
                            "status": "错误: 源删除失败"
                        }
                    else:
                        success_count.value += 1
                        worker_status[worker_id] = {
                            "pid": pid, "file": filename, "bytes": copied, "status": "完成"
                        }

                if delete_error:
                    log_queue.put(f"[W{worker_id}] 错误：{filename} -> 源文件删除失败：{delete_error}")
                else:
                    log_queue.put(f"[W{worker_id}] 完成：{filename}"
                                  f"（{copied / (1024 * 1024):.1f} MB）")
            finally:
                if tmp_path is not None:
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except OSError:
                        pass

        except Exception as e:
            if cancel_event.is_set():
                log_queue.put(f"[W{worker_id}] 已取消：{filename}")
                with lock:
                    worker_status[worker_id] = {"pid": pid, "file": filename, "bytes": 0, "status": "已取消"}
            else:
                log_queue.put(f"[W{worker_id}] 错误：{filename} -> {e}")
                with lock:
                    worker_status[worker_id] = {
                        "pid": pid, "file": filename, "bytes": 0, "status": f"错误: {str(e)[:30]}"
                    }

    # 进程正常退出时标记
    with lock:
        worker_status[worker_id] = {"pid": pid, "file": "-", "bytes": 0, "status": "已退出"}
    log_queue.put(f"[W{worker_id}] 进程退出")


# ================= GUI 主类 =================
class FileScannerApp:
    def __init__(self, root, src=None, dst=None, ext=None, mode="copy", parent=None):
        self.root = root
        self.parent = parent if parent is not None else root
        self.initial_mode = mode if mode in ("copy", "move") else "copy"
        self.current_mode = self.initial_mode
        if parent is None:
            self.root.title("多进程文件扫描与复制工具")
            self.root.geometry("1060x960")
            self.root.minsize(920, 720)

        self.found_files = []
        self.total_bytes = 0
        self.processes = []

        # 多进程共享状态
        self.manager = mp.Manager()
        self.lock = mp.Lock()
        self.progress_val = mp.Value('q', 0)
        self.success_count = mp.Value('i', 0)
        self.skip_count = mp.Value('i', 0)
        self.cancel_event = mp.Event()
        self.task_queue = mp.Queue()
        self.ask_queue = mp.Queue()
        self.response_queue = mp.Queue()    # 兼容旧调用的兜底队列
        self.response_queues = {}           # wid -> 该 worker 专属答复队列
        self.log_queue = mp.Queue()          # 子进程实时日志
        self.worker_status = self.manager.dict()

        self._build_ui(src, dst, ext, self.initial_mode)

    def _build_ui(self, src, dst, ext, mode):
        root = self.parent
        # 顶层 grid 权重：扫描结果与日志共享剩余空间
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=0)   # 源
        root.rowconfigure(1, weight=0)   # 操作模式
        root.rowconfigure(2, weight=0)   # 后缀
        root.rowconfigure(3, weight=0)   # 扫描按钮
        root.rowconfigure(4, weight=2)   # 扫描结果（可伸缩）
        root.rowconfigure(5, weight=0)   # 总进度
        root.rowconfigure(6, weight=0)   # 子进程状态
        root.rowconfigure(7, weight=0)   # 目标
        root.rowconfigure(8, weight=0)   # 按钮
        root.rowconfigure(9, weight=4)   # 日志（可伸缩，占比更高）

        # ---- 源文件夹 ----
        frame_src = tk.LabelFrame(root, text="源文件夹", padx=5, pady=5)
        frame_src.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        self.var_src = tk.StringVar(value=src or "")
        tk.Entry(frame_src, textvariable=self.var_src).pack(side="left", fill="x", expand=True)
        tk.Button(frame_src, text="浏览…", command=lambda: self.var_src.set(filedialog.askdirectory())).pack(side="right")

        # ---- 操作模式 ----
        frame_mode = tk.LabelFrame(root, text="操作模式", padx=5, pady=5)
        frame_mode.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        self.frame_mode = frame_mode
        self.var_mode = tk.StringVar(value=mode)
        tk.Radiobutton(frame_mode, text="复制（保留源文件）", variable=self.var_mode,
                       value="copy", command=self._on_mode_changed).pack(side="left", padx=16)
        tk.Radiobutton(frame_mode, text="移动（完成后删除源文件）", variable=self.var_mode,
                       value="move", command=self._on_mode_changed).pack(side="left", padx=16)

        # ---- 后缀名 ----
        frame_ext = tk.LabelFrame(root, text="文件后缀（如 .txt，多个用逗号分隔）", padx=5, pady=5)
        frame_ext.grid(row=2, column=0, sticky="ew", padx=10, pady=5)
        self.var_ext = tk.StringVar(value=ext or ".txt")
        tk.Entry(frame_ext, textvariable=self.var_ext).pack(fill="x")

        tk.Button(root, text="🔍 开始扫描", command=self.scan_files, height=2).grid(
            row=3, column=0, pady=5)

        # ---- 文件列表（含进度列）----
        frame_list = tk.LabelFrame(root, text="扫描结果", padx=5, pady=5)
        frame_list.grid(row=4, column=0, sticky="nsew", padx=10, pady=5)
        frame_list.columnconfigure(0, weight=1)
        frame_list.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame_list, columns=("index", "path", "size", "progress"),
                                 show="headings", height=3)
        for c, txt, w, anc in (("index", "#", 46, "center"), ("path", "文件绝对路径", 640, "w"),
                               ("size", "大小(KB)", 100, "e"), ("progress", "进度", 90, "center")):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor=anc, stretch=(c == "path"))
        vsb = ttk.Scrollbar(frame_list, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self._path_rows = {}   # 绝对路径 -> iid

        # ---- 总进度条（带百分比）----
        frame_prog = ttk.Frame(root)
        frame_prog.grid(row=5, column=0, sticky="ew", padx=10, pady=(5, 2))
        frame_prog.columnconfigure(0, weight=1)
        self.progress_bar = ttk.Progressbar(frame_prog, orient="horizontal", mode="determinate")
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        self.var_status = tk.StringVar(value="就绪")
        tk.Label(frame_prog, textvariable=self.var_status, anchor="w").grid(
            row=1, column=0, sticky="w", pady=(2, 0))

        # ---- 子进程实时监控面板 ----
        frame_workers = tk.LabelFrame(root, text="子进程实时状态", padx=5, pady=5)
        frame_workers.grid(row=6, column=0, sticky="ew", padx=10, pady=5)
        self.worker_tree = ttk.Treeview(frame_workers, columns=("id", "pid", "file", "bytes", "status"),
                                         show="headings", height=2)
        self.worker_tree.heading("id", text="Worker")
        self.worker_tree.heading("pid", text="PID")
        self.worker_tree.heading("file", text="当前文件")
        self.worker_tree.heading("bytes", text="已传输(MB)")
        self.worker_tree.heading("status", text="状态")
        self.worker_tree.column("id", width=60, anchor="center")
        self.worker_tree.column("pid", width=70, anchor="center")
        self.worker_tree.column("file", width=400)
        self.worker_tree.column("bytes", width=100, anchor="e")
        self.worker_tree.column("status", width=200)
        self.worker_tree.pack(fill="x")

        # ---- 目标文件夹 ----
        frame_dst = tk.LabelFrame(root, text="目标文件夹", padx=5, pady=5)
        frame_dst.grid(row=7, column=0, sticky="ew", padx=10, pady=(5, 5))
        self.var_dst = tk.StringVar(value=dst or "")
        tk.Entry(frame_dst, textvariable=self.var_dst).pack(side="left", fill="x", expand=True)
        tk.Button(frame_dst, text="浏览…", command=lambda: self.var_dst.set(filedialog.askdirectory())).pack(side="right")

        frame_btn = tk.Frame(root)
        frame_btn.grid(row=8, column=0, sticky="ew", padx=10, pady=(0, 5))
        self.btn_copy = tk.Button(frame_btn, text="✅ 确定复制", width=15, command=self.start_copy)
        self.btn_copy.pack(side="left", padx=5)
        self.btn_cancel = tk.Button(frame_btn, text="❌ 取消/终止", width=15, command=self.cancel_operation, state="disabled")
        self.btn_cancel.pack(side="right", padx=5)

        # ---- 实时日志 ----
        frame_log = tk.LabelFrame(root, text="实时日志", padx=5, pady=5)
        frame_log.grid(row=9, column=0, sticky="nsew", padx=10, pady=(5, 10))
        frame_log.columnconfigure(0, weight=1)
        frame_log.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(frame_log, height=8, wrap="word")
        self.log_text.configure(state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self._on_mode_changed()

    def _set_mode_controls(self, state):
        """运行时锁定/解锁操作模式，避免中途切换造成统计口径混乱。"""
        if hasattr(self, "frame_mode"):
            for widget in self.frame_mode.winfo_children():
                try:
                    widget.config(state=state)
                except tk.TclError:
                    pass

    def _on_mode_changed(self):
        move = self.var_mode.get() == "move"
        self.current_mode = self.var_mode.get()
        if self.parent is self.root:
            self.root.title("多进程文件扫描与移动工具" if move else "多进程文件扫描与复制工具")
        if hasattr(self, "btn_copy"):
            self.btn_copy.config(text="确定移动" if move else "✅ 确定复制")

    def _log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{msg}\n")
        try:
            line_no = int(self.log_text.index("end-1c").split(".")[0])
            if line_no > 600:
                self.log_text.delete("1.0", f"{line_no - 500}.0")
        except Exception:
            pass
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def scan_files(self):
        src_dir = self.var_src.get().strip()
        ext_raw = self.var_ext.get().strip()
        if not src_dir or not os.path.isdir(src_dir):
            messagebox.showerror("错误", "请选择有效的源文件夹。")
            return

        extensions = set()
        for ext in ext_raw.replace("，", ",").split(","):
            ext = ext.strip().lower()
            if ext and not ext.startswith("."):
                ext = "." + ext
            if ext:
                extensions.add(ext)

        for item in self.tree.get_children():
            self.tree.delete(item)
        self.found_files.clear()
        self.total_bytes = 0

        self.var_status.set("正在扫描…")
        self.root.update_idletasks()

        for dirpath, _, filenames in os.walk(src_dir):
            for name in filenames:
                _, file_ext = os.path.splitext(name)
                if file_ext.lower() in extensions:
                    full_path = os.path.abspath(os.path.join(dirpath, name))
                    size = os.path.getsize(full_path)
                    self.found_files.append((full_path, name, size))
                    self.total_bytes += size

        for item in self.tree.get_children():
            self.tree.delete(item)
        self._path_rows.clear()
        for i, (path, _, size) in enumerate(self.found_files, start=1):
            iid = self.tree.insert("", "end", values=(i, path, f"{size / 1024:.1f}", "-"))
            self._path_rows[path] = iid

        self.progress_bar["maximum"] = self.total_bytes if self.total_bytes > 0 else 100
        self.progress_bar["value"] = 0
        self.var_status.set(f"扫描完成：{len(self.found_files)} 个文件，总计 {self.total_bytes / (1024 * 1024):.2f} MB。")

    def start_copy(self):
        if not self.found_files:
            messagebox.showwarning("提示", "请先扫描文件。")
            return
        mode = self.var_mode.get()
        action = "移动" if mode == "move" else "复制"
        self.current_mode = mode
        dst_dir = self.var_dst.get().strip()
        if not dst_dir:
            messagebox.showerror("错误", "请选择目标文件夹。")
            return
        if not os.path.isdir(dst_dir):
            if messagebox.askyesno("确认", f"目标文件夹不存在，是否创建？\n{dst_dir}"):
                os.makedirs(dst_dir, exist_ok=True)
            else:
                return

        # 重置共享状态
        self.progress_val.value = 0
        self.success_count.value = 0
        self.skip_count.value = 0
        self.cancel_event.clear()
        self.worker_status.clear()

        while not self.task_queue.empty():
            self.task_queue.get_nowait()
        while not self.ask_queue.empty():
            self.ask_queue.get_nowait()
        while not self.response_queue.empty():
            self.response_queue.get_nowait()
        for q in self.response_queues.values():
            while not q.empty():
                q.get_nowait()

        for src_path, filename, _ in self.found_files:
            self.task_queue.put((src_path, dst_dir, filename))

        num_workers = max(1, os.cpu_count() - 1)
        self.response_queues = {wid: mp.Queue() for wid in range(num_workers)}

        self.processes = []
        for wid in range(num_workers):
            p = mp.Process(target=worker_process, args=(
                wid, self.task_queue, self.ask_queue, self.response_queues[wid],
                self.progress_val, self.success_count, self.skip_count,
                self.cancel_event, self.lock, self.worker_status, self.log_queue,
                mode
            ))
            p.start()
            self.processes.append(p)

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._log(f"启动 {num_workers} 个子进程开始{action}。")

        for _ in range(num_workers):
            self.task_queue.put(None)

        self.btn_copy.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self._set_mode_controls("disabled")
        self.var_status.set(f"{action}中... 启动了 {num_workers} 个子进程")

        self._poll_progress()

    def _poll_progress(self):
        # 0. 消费子进程实时日志
        try:
            while True:
                self._log(self.log_queue.get_nowait())
        except queue.Empty:
            pass

        # 1. 更新总进度条
        current_bytes = self.progress_val.value
        self.progress_bar["value"] = current_bytes
        pct = (current_bytes / self.total_bytes * 100) if self.total_bytes > 0 else 0
        self.var_status.set(f"总进度: {pct:.1f}% ({current_bytes / (1024 * 1024):.1f} / {self.total_bytes / (1024 * 1024):.1f} MB)")

        # 2. 刷新子进程监控面板（含 BUG#4 僵尸状态检测）
        for item in self.worker_tree.get_children():
            self.worker_tree.delete(item)
        try:
            status_snapshot = dict(self.worker_status)
            for wid in sorted(status_snapshot.keys()):
                info = status_snapshot[wid]
                # 若进程已死但状态未正常更新，强制标记为异常终止
                if wid < len(self.processes) and not self.processes[wid].is_alive():
                    if info["status"] not in ("完成", "已跳过", "已退出"):
                        info = dict(info)  # 避免修改快照中的 Manager proxy
                        info["status"] = "⚠️ 异常终止"
                mb = f"{info['bytes'] / (1024 * 1024):.2f}"
                self.worker_tree.insert("", "end", values=(
                    f"W-{wid}", info["pid"], info["file"], mb, info["status"]
                ))
                # 同步到扫描结果清单的进度列
                fname = info["file"]
                if fname in (".", "-") or not fname:
                    continue
                iid = self._path_rows.get(os.path.abspath(os.path.join(
                    self.var_dst.get().strip(), fname)))
                if iid is None:
                    # 退而求其次按文件名匹配
                    for p, row_iid in self._path_rows.items():
                        if os.path.basename(p) == fname:
                            iid = row_iid
                            break
                if iid is not None:
                    sp = info["status"]
                    if "复制中" in sp or "移动中" in sp:
                        import re as _re
                        m = _re.search(r"\((\d+)%\)", sp)
                        prog = f"{m.group(1)}%" if m else sp
                    elif sp == "完成":
                        prog = "100%"
                    elif sp == "已跳过":
                        prog = "跳过"
                    elif sp == "已取消":
                        prog = "取消"
                    elif "错误" in sp:
                        prog = "错误"
                    else:
                        prog = "-"
                    try:
                        v = self.tree.item(iid, "values")
                        self.tree.item(iid, values=(v[0], v[1], v[2], prog))
                    except (tk.TclError, IndexError):
                        pass
        except Exception:
            pass  # Manager dict 在进程退出瞬间可能短暂异常

        # 3. 处理同名弹窗请求
        try:
            while True:
                item = self.ask_queue.get_nowait()
                if len(item) == 4:
                    req_id, filename, action, wid = item
                elif len(item) == 3:
                    req_id, filename, action = item
                    wid = 0
                else:
                    req_id, filename = item
                    action = "复制"
                    wid = 0
                choice = messagebox.askyesnocancel(
                    "文件已存在",
                    f"目标文件夹中已存在：\n{filename}\n\n"
                    f"「是」覆盖 | 「否」跳过 | 「取消」终止{action}"
                )
                res_choice = 'overwrite' if choice is True else ('skip' if choice is False else 'cancel')
                target_q = self.response_queues.get(wid, self.response_queue)
                target_q.put((req_id, res_choice))
                if res_choice == 'cancel':
                    self.cancel_event.set()
        except queue.Empty:
            pass

        # 4. 检查是否全部结束
        all_dead = all(not p.is_alive() for p in self.processes)
        if all_dead:
            self._finish_copy()
        else:
            self.root.after(100, self._poll_progress)

    def _finish_copy(self):
        for p in self.processes:
            p.join()

        expected = len(self.found_files)
        success = self.success_count.value
        skipped = self.skip_count.value
        cancelled = self.cancel_event.is_set()

        msg = f"预期: {expected}\n成功: {success}\n跳过: {skipped}"
        if cancelled:
            msg += "\n\n⚠️ 任务被用户取消。"
        if success + skipped == expected and not cancelled:
            msg += "\n\n✅ 校验通过：处理文件数与预期完全一致。"
        else:
            msg += f"\n\n⚠️ 校验警告：实际处理 {success + skipped}，与预期 {expected} 不符。"

        action = "移动" if self.current_mode == "move" else "复制"
        messagebox.showinfo(f"{action}结束", msg)
        self.btn_copy.config(state="normal")
        self.btn_cancel.config(state="disabled")
        self._set_mode_controls("normal")
        self.var_status.set(f"{action}结束。成功:{success} 跳过:{skipped}")
        self._log(f"{action}任务结束：成功 {success}，跳过 {skipped}。")

    def cancel_operation(self):
        self.cancel_event.set()
        self.var_status.set("正在终止子进程...")


# ============================================================
# 来自 del.py：批量删除
# ============================================================

class FileDeleterApp:
    def __init__(self, root, parent=None):
        self.root = root
        self.parent = parent if parent is not None else root
        if parent is None:
            self.root.title("文件批量删除工具 (单线程版)")
            self.root.geometry("1040x800")
            self.root.minsize(900, 600)

        self.found_files = []
        self.is_running = False
        self.cancel_flag = False
        self.deleted_count = 0
        self.failed_count = 0
        self.current_file = ""

        self._build_ui()

    # ---------------------------------------------------------------- UI ----
    def _build_ui(self):
        root = self.parent
        # 顶层 grid 权重：结果与日志共享剩余空间
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=0)   # 目标文件夹
        root.rowconfigure(1, weight=0)   # 后缀
        root.rowconfigure(2, weight=0)   # 扫描按钮
        root.rowconfigure(3, weight=2)   # 扫描结果（可伸缩）
        root.rowconfigure(4, weight=0)   # 进度
        root.rowconfigure(5, weight=0)   # 按钮
        root.rowconfigure(6, weight=3)   # 日志（可伸缩）

        # ---- 目标文件夹 ----
        frame_src = tk.LabelFrame(root, text="目标文件夹（将扫描并删除其中的文件）", padx=5, pady=5)
        frame_src.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        self.var_src = tk.StringVar()
        tk.Entry(frame_src, textvariable=self.var_src).pack(side="left", fill="x", expand=True)
        tk.Button(frame_src, text="浏览…", command=lambda: self.var_src.set(filedialog.askdirectory())).pack(side="right")

        # ---- 后缀名 ----
        frame_ext = tk.LabelFrame(root, text="文件后缀（如 .tmp,.log，多个用逗号分隔；留空表示所有文件）", padx=5, pady=5)
        frame_ext.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        self.var_ext = tk.StringVar(value=".tmp,.log,.bak")
        tk.Entry(frame_ext, textvariable=self.var_ext).pack(fill="x")

        tk.Button(root, text="🔍 扫描文件", command=self.scan_files, height=2).grid(
            row=2, column=0, pady=5)

        # ---- 扫描结果（含状态列）----
        frame_list = tk.LabelFrame(root, text="扫描结果", padx=5, pady=5)
        frame_list.grid(row=3, column=0, sticky="nsew", padx=10, pady=5)
        frame_list.columnconfigure(0, weight=1)
        frame_list.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame_list, columns=("index", "path", "size", "status"),
                                 show="headings", height=6)
        for c, txt, w, anc in (("index", "#", 46, "center"), ("path", "文件绝对路径", 640, "w"),
                               ("size", "大小(KB)", 100, "e"), ("status", "状态", 90, "center")):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor=anc, stretch=(c == "path"))
        vsb = ttk.Scrollbar(frame_list, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self._path_rows = {}   # 路径 -> iid

        # ---- 进度（带百分比）----
        frame_prog = tk.Frame(root)
        frame_prog.grid(row=4, column=0, sticky="ew", padx=10, pady=(5, 2))
        frame_prog.columnconfigure(0, weight=1)
        self.progress_bar = ttk.Progressbar(frame_prog, orient="horizontal", mode="determinate")
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        self.var_status = tk.StringVar(value="就绪")
        tk.Label(frame_prog, textvariable=self.var_status, anchor="w").grid(
            row=1, column=0, sticky="w", pady=(2, 0))

        # ---- 按钮 ----
        frame_btn = tk.Frame(root)
        frame_btn.grid(row=5, column=0, sticky="ew", padx=10, pady=(5, 10))
        self.btn_delete = tk.Button(frame_btn, text="🗑️ 确认删除", width=16,
                                    command=self.start_delete, bg="#ffcccc")
        self.btn_delete.pack(side="left", padx=5)
        self.btn_cancel = tk.Button(frame_btn, text="❌ 取消", width=16,
                                    command=self.cancel_operation, state="disabled")
        self.btn_cancel.pack(side="right", padx=5)

        # ---- 实时日志 ----
        frame_log = tk.LabelFrame(root, text="实时日志", padx=5, pady=5)
        frame_log.grid(row=6, column=0, sticky="nsew", padx=10, pady=(5, 10))
        frame_log.columnconfigure(0, weight=1)
        frame_log.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(frame_log, height=7, wrap="word")
        self.log_text.configure(state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")

    # ------------------------------------------------------------- 工具 ----
    def _log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        try:
            line_no = int(self.log_text.index("end-1c").split(".")[0])
            if line_no > 600:
                self.log_text.delete("1.0", f"{line_no - 500}.0")
        except Exception:
            pass
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_row_status(self, path, status):
        iid = self._path_rows.get(path)
        if iid is None:
            return
        try:
            v = self.tree.item(iid, "values")
            self.tree.item(iid, values=(v[0], v[1], v[2], status))
        except (tk.TclError, IndexError):
            pass

    # ------------------------------------------------------------- 扫描 ----
    def scan_files(self):
        src_dir = self.var_src.get().strip()
        ext_raw = self.var_ext.get().strip()

        if not src_dir or not os.path.isdir(src_dir):
            messagebox.showerror("错误", "请选择有效的文件夹。")
            return

        extensions = set()
        if ext_raw:
            for ext in ext_raw.replace("，", ",").split(","):
                ext = ext.strip().lower()
                if ext and not ext.startswith("."):
                    ext = "." + ext
                if ext:
                    extensions.add(ext)

        for item in self.tree.get_children():
            self.tree.delete(item)
        self._path_rows.clear()
        self.found_files = []

        self.var_status.set("正在扫描…")
        self.root.update_idletasks()

        count = 0
        for dirpath, _, filenames in os.walk(src_dir):
            for name in filenames:
                _, file_ext = os.path.splitext(name)
                if extensions and file_ext.lower() not in extensions:
                    continue
                full_path = os.path.abspath(os.path.join(dirpath, name))
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    continue
                self.found_files.append((full_path, size))
                count += 1

        for i, (path, size) in enumerate(self.found_files, start=1):
            iid = self.tree.insert("", "end", values=(i, path, f"{size / 1024:.1f}", "待删除"))
            self._path_rows[path] = iid

        self.progress_bar["maximum"] = max(1, len(self.found_files))
        self.progress_bar["value"] = 0
        self.var_status.set(f"扫描完成：{count} 个文件，等待确认删除。")
        self._log(f"扫描完成：找到 {count} 个文件。")
        if count:
            total_mb = sum(s for _, s in self.found_files) / (1024 * 1024)
            self._log(f"合计约 {total_mb:.1f} MB。删除不可恢复，请确认后再执行。")

    # ------------------------------------------------------------- 删除 ----
    def start_delete(self):
        if self.is_running:
            return
        if not self.found_files:
            messagebox.showwarning("提示", "请先扫描文件。")
            return
        if not messagebox.askyesno(
            "不可逆操作确认",
            f"即将删除 {len(self.found_files)} 个文件。\n\n"
            "此操作不可恢复，确定继续吗？",
        ):
            return

        self.is_running = True
        self.cancel_flag = False
        self.deleted_count = 0
        self.failed_count = 0
        self.btn_delete.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.progress_bar["maximum"] = max(1, len(self.found_files))
        self.progress_bar["value"] = 0
        self._log(f"开始删除 {len(self.found_files)} 个文件…")

        threading.Thread(target=self._delete_worker, daemon=True).start()

    def _delete_worker(self):
        total = len(self.found_files)
        for idx, (path, size) in enumerate(self.found_files, start=1):
            if self.cancel_flag:
                self.root.after(0, lambda: self._log("用户取消，停止删除。"))
                break
            self.current_file = path
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    self.deleted_count += 1
                    status, msg = "已删除", f"已删除：{path}"
                else:
                    self.failed_count += 1
                    status, msg = "不存在", f"文件不存在（跳过）：{path}"
            except Exception as e:
                self.failed_count += 1
                status, msg = "失败", f"删除失败：{path} -> {e}"

            self.root.after(0, lambda p=path, s=status, m=msg, i=idx, t=total: (
                self._set_row_status(p, s),
                self._log(m),
                self.progress_bar.configure(value=i),
                self.var_status.set(
                    f"删除进度：{i}/{t}（{i / t * 100:.0f}%）  已删 {self.deleted_count} / 失败 {self.failed_count}")
            ))

        self.root.after(0, self._finish_delete)

    def _finish_delete(self):
        self.is_running = False
        self.btn_delete.config(state="normal")
        self.btn_cancel.config(state="disabled")
        self.var_status.set(f"结束：已删除 {self.deleted_count}，失败 {self.failed_count}。")
        self._log(f"删除结束：成功 {self.deleted_count}，失败 {self.failed_count}。")
        messagebox.showinfo("删除完成",
                            f"已删除：{self.deleted_count}\n失败：{self.failed_count}")

    def cancel_operation(self):
        if self.is_running:
            self.cancel_flag = True
            self.var_status.set("正在停止…")
            self._log("收到取消请求，正在停止…")


# ============================================================
# 来自 avi_lossless_gui.py：无损压缩 / 解码转码
# ============================================================

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
        if self.parent is self.root:
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


# ============================================================
# 统一入口
# ============================================================
def build_unified_ui():
    root = tk.Tk()
    root.title("ATcith 一体化工具")
    root.geometry("1180x900")
    root.minsize(1000, 700)

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)

    tab_specs = (
        ("convert", "AVI 转 MP4"),
        ("compress", "无损压缩 (FFV1)"),
        ("decode", "解码 / 转码"),
        ("transfer", "复制 / 移动"),
        ("delete", "批量删除"),
    )
    tabs = {}
    for key, label in tab_specs:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=label)
        tabs[key] = frame

    apps = {
        "convert": AVIConverterMulti(root, parent=tabs["convert"]),
        "compress": AviLosslessApp(root, parent=tabs["compress"]),
        "decode": DecodeApp(root, parent=tabs["decode"]),
        "transfer": FileScannerApp(root, parent=tabs["transfer"]),
        "delete": FileDeleterApp(root, parent=tabs["delete"]),
    }

    def on_close():
        busy = []
        if apps["convert"].is_running:
            busy.append("AVI 转 MP4")
        if apps["compress"].running:
            busy.append("无损压缩")
        if apps["decode"].running:
            busy.append("解码 / 转码")
        if apps["delete"].is_running:
            busy.append("批量删除")
        if any(p.is_alive() for p in apps["transfer"].processes):
            busy.append("复制 / 移动")
        if busy:
            detail = "\n".join("  - " + name for name in busy)
            if not messagebox.askyesno(
                    "确认退出",
                    "以下任务仍在进行：\n" + detail + "\n\n确定要退出吗？"):
                return

        for app in (
            apps["convert"], apps["compress"], apps["decode"],
            apps["delete"], apps["transfer"],
        ):
            try:
                if hasattr(app, "_cleanup_and_exit"):
                    app._cleanup_and_exit(force_destroy=False)
                elif hasattr(app, "_stop"):
                    app._stop()
                if hasattr(app, "cancel_operation"):
                    app.cancel_operation()
            except Exception:
                pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    return root, notebook, apps


def main():
    root, _notebook, _apps = build_unified_ui()
    root.mainloop()


if __name__ == "__main__":
    mp.freeze_support()
    main()
