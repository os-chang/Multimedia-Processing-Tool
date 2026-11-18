import os
import shutil
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, scrolledtext
import multiprocessing as mp
import queue
import uuid
import argparse


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
    def __init__(self, root, src=None, dst=None, ext=None, mode="copy"):
        self.root = root
        self.initial_mode = mode if mode in ("copy", "move") else "copy"
        self.current_mode = self.initial_mode
        self.root.title("Multimedia Processing Tool")
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
        root = self.root
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
        root.rowconfigure(9, weight=3)   # 日志（可伸缩，占比更高）

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
                                 show="headings", height=4)
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
        self.root.title("Multimedia Processing Tool")
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


def main():
    parser = argparse.ArgumentParser(description="Multimedia Processing Tool")
    parser.add_argument("--src", help="源文件夹路径")
    parser.add_argument("--dst", help="目标文件夹路径")
    parser.add_argument("--ext", help="文件后缀，如 .txt,.py")
    parser.add_argument("--mode", choices=("copy", "move"), default="copy",
                        help="操作模式：copy=复制（默认），move=移动")
    args = parser.parse_args()

    root = tk.Tk()
    app = FileScannerApp(root, src=args.src, dst=args.dst, ext=args.ext, mode=args.mode)
    root.mainloop()


if __name__ == '__main__':
    mp.freeze_support()
    main()