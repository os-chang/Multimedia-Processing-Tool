# -*- coding: utf-8 -*-
"""AllInOne online launcher (GUI, no console).

- Detects system Python (with tkinter) and FFmpeg/ffprobe.
- If usable, starts immediately.
- Otherwise uses/downloads runtime under %APPDATA%/AllInOne.
- uv wheel is downloaded and unpacked in memory; only uv.exe is written to disk.
"""
import io
import os
import sys
import time
import queue
import shutil
import zipfile
import threading
import subprocess
import urllib.request
import tkinter as tk
from tkinter import ttk, messagebox

UV_URL = (
    "https://pypi.tuna.tsinghua.edu.cn/packages/dd/ef/"
    "ceeabf3631117f7354787d9fb1ffe943899e59ed4b887a696ed547e47e76/"
    "uv-0.13.9-py3-none-win_amd64.whl"
)
PYTHON_VERSION = "3.15"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def appdata_dir():
    root = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(root, "AllInOne")


def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, name)
    return os.path.join(base_dir(), name)


def embedded_app_path(runtime_dir):
    source = resource_path("AllInOne.py")
    if not os.path.exists(source):
        raise FileNotFoundError("Embedded AllInOne.py is missing.")
    target = os.path.join(runtime_dir, "AllInOne.py")
    if not os.path.exists(target) or os.path.getsize(target) != os.path.getsize(source):
        os.makedirs(runtime_dir, exist_ok=True)
        shutil.copyfile(source, target)
    return target


def run_quiet(cmd, timeout=15):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
        timeout=timeout,
        check=False,
    )


def find_system_python():
    candidates = (
        ("python", ("python",)),
        ("python3", ("python3",)),
        ("py -3.15", ("py", "-3.15")),
        ("py -3", ("py", "-3")),
    )
    for _label, cmd in candidates:
        try:
            r = run_quiet(cmd + ("-c", "import sys,tkinter;print(sys.version_info[0]*10+sys.version_info[1])"), 10)
            if r.returncode == 0:
                v = int(r.stdout.decode("utf-8", "ignore").strip().splitlines()[0])
                if v >= 310:
                    return list(cmd)
        except Exception:
            continue
    return None


def find_ffmpeg():
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return True
    runtime = appdata_dir()
    if (os.path.exists(os.path.join(runtime, "ffmpeg.exe"))
            and os.path.exists(os.path.join(runtime, "ffprobe.exe"))):
        os.environ["PATH"] = runtime + os.pathsep + os.environ.get("PATH", "")
        return True
    return False


def find_runtime_python(runtime_dir):
    python_root = os.path.join(runtime_dir, "python")
    if not os.path.isdir(python_root):
        return None
    for dirpath, _dirs, files in os.walk(python_root):
        if "python.exe" in files:
            return os.path.join(dirpath, "python.exe")
    return None


def download_uv_in_memory(uv_exe, q):
    q.put(("status", "Downloading uv package..."))
    data = bytearray()
    with urllib.request.urlopen(UV_URL, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if total:
                q.put(("progress", min(99, int(len(data) * 100 / total))))

    q.put(("status", "Extracting uv.exe in memory..."))
    uv_bytes = None
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if name.lower().endswith("uv.exe"):
                uv_bytes = zf.read(name)
                break
    if uv_bytes is None:
        raise RuntimeError("uv.exe not found in downloaded package.")
    with open(uv_exe, "wb") as f:
        f.write(uv_bytes)
    q.put(("progress", 100))


def prepare_runtime(runtime_dir, q):
    os.makedirs(runtime_dir, exist_ok=True)
    python_dir = os.path.join(runtime_dir, "python")
    cache_dir = os.path.join(runtime_dir, "cache")
    os.makedirs(python_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)

    uv_exe = os.path.join(runtime_dir, "uv.exe")
    if not os.path.exists(uv_exe):
        download_uv_in_memory(uv_exe, q)

    env = os.environ.copy()
    env["UV_PYTHON_INSTALL_DIR"] = python_dir
    env["UV_CACHE_DIR"] = cache_dir

    q.put(("status", "Preparing portable Python 3.15 (first run only)..."))
    q.put(("indeterminate", True))
    r = subprocess.run(
        [uv_exe, "python", "install", PYTHON_VERSION],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=env,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError("Failed to install portable Python.")
    q.put(("indeterminate", False))

    python_exe = find_runtime_python(runtime_dir)
    if not python_exe:
        raise RuntimeError("Portable Python was installed but python.exe was not found.")
    return python_exe


class ProgressWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Multimedia Processing Tool - 运行时准备")
        self.root.geometry("460x180")
        self.root.resizable(False, False)
        self.q = queue.Queue()

        ttk.Label(self.root, text="正在准备 AllInOne 运行所需环境，请稍候…").pack(pady=12)
        self.pb = ttk.Progressbar(self.root, mode="determinate", maximum=100)
        self.pb.pack(fill="x", padx=20)
        self.var = tk.StringVar(value="准备中…")
        ttk.Label(self.root, textvariable=self.var, anchor="w").pack(fill="x", padx=20, pady=8)
        self.error = None

    def poll(self):
        while True:
            try:
                kind, value = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.var.set(value)
            elif kind == "progress":
                self.pb["value"] = value
            elif kind == "indeterminate":
                self.pb.configure(mode="indeterminate")
                if value:
                    self.pb.start(12)
                else:
                    self.pb.stop()
            elif kind == "error":
                self.error = value
        self.root.after(80, self.poll)

    def run_worker(self, worker):
        result = {}

        def target():
            try:
                result["value"] = worker(self.q)
            except Exception as exc:
                self.q.put(("error", "%s: %s" % (type(exc).__name__, exc)))

        self.root.after(80, self.poll)
        threading.Thread(target=target, daemon=True).start()
        while self.root.winfo_exists() and "value" not in result and self.error is None:
            self.root.update_idletasks()
            self.root.update()
            time.sleep(0.05)
        self.root.destroy()
        if self.error:
            raise RuntimeError(self.error)
        return result.get("value")


def start_app(python_cmd, app_path):
    if isinstance(python_cmd, str):
        python_cmd = [python_cmd]
    env = os.environ.copy()
    appdata = appdata_dir()
    env["PATH"] = appdata + os.pathsep + env.get("PATH", "")
    subprocess.Popen(
        list(python_cmd) + [app_path],
        env=env,
        creationflags=CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    runtime = appdata_dir()
    os.makedirs(runtime, exist_ok=True)
    app_path = embedded_app_path(runtime)

    system_python = find_system_python()
    ffmpeg_ok = find_ffmpeg()

    if system_python and ffmpeg_ok:
        start_app(system_python, app_path)
        return

    win = ProgressWindow()
    python_exe = win.run_worker(lambda q: prepare_runtime(runtime, q))
    start_app([python_exe], app_path)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Multimedia Processing Tool 启动失败", "%s: %s" % (type(exc).__name__, exc))
            root.destroy()
        except Exception:
            pass
        sys.exit(1)
