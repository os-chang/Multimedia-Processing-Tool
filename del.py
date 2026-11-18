import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, scrolledtext
import threading
import time


class FileDeleterApp:
    def __init__(self, root):
        self.root = root
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
        root = self.root
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


if __name__ == "__main__":
    root = tk.Tk()
    app = FileDeleterApp(root)
    root.mainloop()
