import os
import re
import sys
import time
import queue
import threading
import traceback
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import Manager
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, scrolledtext
import tkinter as tk


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

    def __init__(self, root):
        self.root = root
        self.root.title("Multimedia Processing Tool")
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
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

    def _build_ui(self):
        root = self.root
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


if __name__ == "__main__":
    root = tk.Tk()
    app = AVIConverterMulti(root)
    root.mainloop()
