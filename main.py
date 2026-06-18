import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import time
import csv
import json
import urllib.request
from datetime import datetime
from detector import collect_hardware, get_realtime_stats, get_system_info

COLORS = {
    "bg": "#1e1e2e",
    "card": "#2a2a3d",
    "accent": "#7c3aed",
    "text": "#e0e0e0",
    "dim": "#8888aa",
    "green": "#4ade80",
    "yellow": "#facc15",
    "red": "#f87171",
    "bar_bg": "#3a3a50",
    "section": "#c084fc",
    "tab_active": "#7c3aed",
    "tab_inactive": "#3a3a50",
}


class ScrollFrame(tk.Frame):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=COLORS["bg"], **kw)
        self.canvas = tk.Canvas(self, bg=COLORS["bg"], highlightthickness=0, bd=0)
        self.vscroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=COLORS["bg"])
        self.inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor=tk.NW)
        self.canvas.configure(yscrollcommand=self.vscroll.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.inner_id, width=e.width))
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))


def card(parent, title):
    outer = tk.Frame(parent, bg=COLORS["card"])
    outer.pack(fill=tk.X, padx=6, pady=4)
    tk.Label(outer, text=title, font=("Microsoft YaHei", 11, "bold"),
             fg=COLORS["section"], bg=COLORS["card"]).pack(anchor=tk.W, padx=12, pady=(8, 2))
    tk.Frame(outer, bg=COLORS["dim"], height=1).pack(fill=tk.X, padx=12, pady=(0, 6))
    body = tk.Frame(outer, bg=COLORS["card"])
    body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))
    return body


def info_row(parent, label, value=""):
    row = tk.Frame(parent, bg=COLORS["card"])
    row.pack(fill=tk.X, pady=1)
    tk.Label(row, text=label, font=("Microsoft YaHei", 10), fg=COLORS["dim"],
             bg=COLORS["card"], width=14, anchor=tk.W).pack(side=tk.LEFT)
    val = tk.Label(row, text=value, font=("Microsoft YaHei", 10), fg=COLORS["text"],
                   bg=COLORS["card"], anchor=tk.W)
    val.pack(side=tk.LEFT, fill=tk.X, expand=True)
    return val


def usage_bar(parent, label):
    row = tk.Frame(parent, bg=COLORS["card"])
    row.pack(fill=tk.X, pady=2)
    tk.Label(row, text=label, font=("Microsoft YaHei", 10), fg=COLORS["dim"],
             bg=COLORS["card"], width=14, anchor=tk.W).pack(side=tk.LEFT)
    bar_canvas = tk.Canvas(row, height=16, bg=COLORS["bar_bg"], highlightthickness=0, bd=0)
    bar_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
    pct_lbl = tk.Label(row, text="0%", font=("Microsoft YaHei", 10), fg=COLORS["text"],
                       bg=COLORS["card"], width=6)
    pct_lbl.pack(side=tk.RIGHT)
    state = {"pct": 0}

    def _redraw(event=None):
        pct = state["pct"]
        bar_canvas.delete("bar")
        w = bar_canvas.winfo_width()
        if w < 2:
            return
        bw = int(w * pct / 100)
        color = COLORS["green"] if pct < 60 else COLORS["yellow"] if pct < 85 else COLORS["red"]
        bar_canvas.create_rectangle(0, 0, bw, 16, fill=color, outline="", tags="bar")

    bar_canvas.bind("<Configure>", _redraw)

    def set_pct(pct):
        pct = max(0, min(100, pct))
        state["pct"] = pct
        pct_lbl.config(text=f"{pct}%")
        bar_canvas.after_idle(_redraw)

    return row, set_pct


class SysInfoApp:
    def __init__(self, root):
        self.root = root
        self.root.title("SysInfo Tool - 硬件信息检测工具")
        self.root.geometry("780x860")
        self.root.configure(bg=COLORS["bg"])
        self.root.resizable(True, True)
        self._running = True
        self._prev_disk_io = None
        self._prev_net_io = None
        self._prev_time = None
        self._gpu_widgets = []
        self._disk_widgets = []
        self._records = []
        self._recording = False
        self._rec_interval = 2

        self._build_ui()
        self._load_hardware()
        self._load_system()
        self._start_monitoring()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # Toolbar
        toolbar = tk.Frame(self.root, bg=COLORS["bg"])
        toolbar.pack(fill=tk.X, padx=10, pady=(8, 0))
        tk.Label(toolbar, text="SysInfo Tool", font=("Microsoft YaHei", 14, "bold"),
                 fg=COLORS["text"], bg=COLORS["bg"]).pack(side=tk.LEFT)

        # Tab buttons
        self.tab_btn_frame = tk.Frame(toolbar, bg=COLORS["bg"])
        self.tab_btn_frame.pack(side=tk.LEFT, padx=(20, 0))
        self._tab_btns = {}
        for name in ("实时监控", "监控记录"):
            btn = tk.Label(self.tab_btn_frame, text=name, font=("Microsoft YaHei", 10),
                           fg=COLORS["text"], bg=COLORS["tab_inactive"],
                           padx=12, pady=4, cursor="hand2")
            btn.pack(side=tk.LEFT, padx=2)
            btn.bind("<Button-1>", lambda e, n=name: self._switch_tab(n))
            self._tab_btns[name] = btn

        tk.Button(toolbar, text="刷新", command=self._refresh_all,
                  font=("Microsoft YaHei", 10)).pack(side=tk.RIGHT, padx=4)
        tk.Button(toolbar, text="复制全部", command=self._copy_all,
                  font=("Microsoft YaHei", 10)).pack(side=tk.RIGHT, padx=4)

        # ---- Page: Monitor ----
        self.page_monitor = tk.Frame(self.root, bg=COLORS["bg"])
        self.sf = ScrollFrame(self.page_monitor)
        self.sf.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)
        content = self.sf.inner

        self.hw_body = card(content, "硬件信息")
        self.hw_status = tk.Label(self.hw_body, text="正在检测硬件...",
                                  font=("Microsoft YaHei", 10), fg=COLORS["dim"], bg=COLORS["card"])
        self.hw_status.pack(anchor=tk.W, pady=4)

        self.rt_body = card(content, "实时监控")
        self.cpu_bar_row, self.cpu_bar_set = usage_bar(self.rt_body, "CPU 使用率")
        self.cpu_freq_lbl = info_row(self.rt_body, "CPU温度/频率")
        self._gpu_area = tk.Frame(self.rt_body, bg=COLORS["card"])
        self._gpu_area.pack(fill=tk.X, pady=0)
        self.mem_bar_row, self.mem_bar_set = usage_bar(self.rt_body, "内存使用率")
        self.mem_detail_lbl = info_row(self.rt_body, "内存详情")
        self.mem_freq_lbl = info_row(self.rt_body, "内存频率")
        self.swap_bar_row, self.swap_bar_set = usage_bar(self.rt_body, "交换分区")
        self._disk_area = tk.Frame(self.rt_body, bg=COLORS["card"])
        self._disk_area.pack(fill=tk.X, pady=0)
        self.disk_io_lbl = info_row(self.rt_body, "磁盘 I/O")
        self.net_lbl = info_row(self.rt_body, "网络流量")

        self.sys_body = card(content, "系统信息")

        # ---- Page: Record ----
        self.page_record = tk.Frame(self.root, bg=COLORS["bg"])

        # Record toolbar
        rec_toolbar = tk.Frame(self.page_record, bg=COLORS["card"])
        rec_toolbar.pack(fill=tk.X, padx=6, pady=(6, 2))

        self.rec_btn = tk.Button(rec_toolbar, text="开始记录", font=("Microsoft YaHei", 10),
                                 command=self._toggle_recording, bg=COLORS["green"],
                                 fg="#000000", relief=tk.FLAT, padx=12)
        self.rec_btn.pack(side=tk.LEFT, padx=8, pady=8)

        tk.Label(rec_toolbar, text="采样间隔:", font=("Microsoft YaHei", 10),
                 fg=COLORS["text"], bg=COLORS["card"]).pack(side=tk.LEFT, padx=(16, 4))
        self.interval_var = tk.StringVar(value="2秒")
        interval_cb = ttk.Combobox(rec_toolbar, textvariable=self.interval_var,
                                   values=["1秒", "2秒", "5秒"], width=5, state="readonly")
        interval_cb.pack(side=tk.LEFT, padx=4)
        interval_cb.bind("<<ComboboxSelected>>", self._on_interval_change)

        self.rec_count_lbl = tk.Label(rec_toolbar, text="记录: 0 条",
                                      font=("Microsoft YaHei", 10), fg=COLORS["dim"], bg=COLORS["card"])
        self.rec_count_lbl.pack(side=tk.LEFT, padx=16)

        tk.Button(rec_toolbar, text="清空", font=("Microsoft YaHei", 10),
                  command=self._clear_records).pack(side=tk.RIGHT, padx=8, pady=8)
        tk.Button(rec_toolbar, text="导出CSV", font=("Microsoft YaHei", 10),
                  command=self._export_csv).pack(side=tk.RIGHT, padx=4, pady=8)

        # Record table
        table_frame = tk.Frame(self.page_record, bg=COLORS["bg"])
        table_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(2, 6))

        columns = ("time", "cpu_pct", "cpu_temp", "cpu_freq",
                   "gpu_pct", "gpu_temp", "gpu_mem", "mem_pct", "mem_freq")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=20)

        headers = {
            "time": ("时间", 140), "cpu_pct": ("CPU%", 55), "cpu_temp": ("CPU温度", 65),
            "cpu_freq": ("CPU频率", 70), "gpu_pct": ("GPU%", 55), "gpu_temp": ("GPU温度", 65),
            "gpu_mem": ("显存%", 55), "mem_pct": ("内存%", 55), "mem_freq": ("内存频率", 70),
        }
        for col, (text, width) in headers.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=tk.CENTER)

        style = ttk.Style()
        style.configure("Treeview", background=COLORS["card"], foreground=COLORS["text"],
                        fieldbackground=COLORS["card"], font=("Microsoft YaHei", 9))
        style.configure("Treeview.Heading", background=COLORS["tab_inactive"],
                        foreground=COLORS["text"], font=("Microsoft YaHei", 9, "bold"))

        vscroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vscroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)

        # ---- AI Analysis section ----
        ai_card = tk.Frame(self.page_record, bg=COLORS["card"])
        ai_card.pack(fill=tk.X, padx=6, pady=(0, 4))
        tk.Label(ai_card, text="AI 分析", font=("Microsoft YaHei", 11, "bold"),
                 fg=COLORS["section"], bg=COLORS["card"]).pack(anchor=tk.W, padx=12, pady=(8, 2))
        tk.Frame(ai_card, bg=COLORS["dim"], height=1).pack(fill=tk.X, padx=12, pady=(0, 6))

        ai_cfg = tk.Frame(ai_card, bg=COLORS["card"])
        ai_cfg.pack(fill=tk.X, padx=12, pady=(0, 6))

        tk.Label(ai_cfg, text="API地址:", font=("Microsoft YaHei", 9),
                 fg=COLORS["dim"], bg=COLORS["card"]).pack(side=tk.LEFT)
        self.ai_url_entry = tk.Entry(ai_cfg, font=("Microsoft YaHei", 9), width=30,
                                     bg=COLORS["bar_bg"], fg=COLORS["text"], insertbackground=COLORS["text"])
        self.ai_url_entry.pack(side=tk.LEFT, padx=(2, 12))
        self.ai_url_entry.insert(0, "https://api.openai.com")

        tk.Label(ai_cfg, text="API Key:", font=("Microsoft YaHei", 9),
                 fg=COLORS["dim"], bg=COLORS["card"]).pack(side=tk.LEFT)
        self.ai_key_entry = tk.Entry(ai_cfg, font=("Microsoft YaHei", 9), width=24,
                                     bg=COLORS["bar_bg"], fg=COLORS["text"], insertbackground=COLORS["text"],
                                     show="*")
        self.ai_key_entry.pack(side=tk.LEFT, padx=(2, 12))

        tk.Label(ai_cfg, text="模型:", font=("Microsoft YaHei", 9),
                 fg=COLORS["dim"], bg=COLORS["card"]).pack(side=tk.LEFT)
        self.ai_model_entry = tk.Entry(ai_cfg, font=("Microsoft YaHei", 9), width=16,
                                       bg=COLORS["bar_bg"], fg=COLORS["text"], insertbackground=COLORS["text"])
        self.ai_model_entry.pack(side=tk.LEFT, padx=(2, 12))
        self.ai_model_entry.insert(0, "gpt-4o")

        ai_btn_frame = tk.Frame(ai_card, bg=COLORS["card"])
        ai_btn_frame.pack(fill=tk.X, padx=12, pady=(0, 6))

        self.ai_btn = tk.Button(ai_btn_frame, text="开始分析", font=("Microsoft YaHei", 10),
                                command=self._run_ai_analysis, bg=COLORS["accent"],
                                fg="#ffffff", relief=tk.FLAT, padx=12)
        self.ai_btn.pack(side=tk.LEFT, padx=0)

        self.ai_status_lbl = tk.Label(ai_btn_frame, text="", font=("Microsoft YaHei", 9),
                                      fg=COLORS["dim"], bg=COLORS["card"])
        self.ai_status_lbl.pack(side=tk.LEFT, padx=12)

        # AI response area
        ai_resp_frame = tk.Frame(ai_card, bg=COLORS["card"])
        ai_resp_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))

        self.ai_response = tk.Text(ai_resp_frame, font=("Microsoft YaHei", 10),
                                   bg=COLORS["bar_bg"], fg=COLORS["text"],
                                   wrap=tk.WORD, height=10, state=tk.DISABLED,
                                   insertbackground=COLORS["text"])
        ai_resp_scroll = tk.Scrollbar(ai_resp_frame, command=self.ai_response.yview)
        self.ai_response.configure(yscrollcommand=ai_resp_scroll.set)
        self.ai_response.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ai_resp_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Status bar
        self.status = tk.Label(self.root, text="就绪", font=("Microsoft YaHei", 9),
                               fg=COLORS["dim"], bg=COLORS["bg"])
        self.status.pack(fill=tk.X, padx=10, pady=(0, 6))

        # Default tab
        self._current_tab = None
        self._switch_tab("实时监控")

    # ==================== Tab switching ====================
    def _switch_tab(self, name):
        if self._current_tab == name:
            return
        self._current_tab = name
        for n, btn in self._tab_btns.items():
            btn.config(bg=COLORS["tab_active"] if n == name else COLORS["tab_inactive"])

        self.page_monitor.pack_forget()
        self.page_record.pack_forget()
        if name == "实时监控":
            self.page_monitor.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
        else:
            self.page_record.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

    # ==================== Hardware ====================
    def _load_hardware(self):
        threading.Thread(target=self._fetch_hardware, daemon=True).start()

    def _fetch_hardware(self):
        try:
            data = collect_hardware()
            self.root.after(0, self._display_hardware, data)
        except Exception as e:
            err = str(e)
            self.root.after(0, lambda msg=err: self.hw_status.config(text=f"检测失败: {msg}"))

    def _display_hardware(self, data):
        self.hw_status.destroy()
        for cpu in data["cpu"]:
            tk.Label(self.hw_body, text="CPU", font=("Microsoft YaHei", 11, "bold"),
                     fg=COLORS["section"], bg=COLORS["card"]).pack(anchor=tk.W, pady=(4, 2))
            info_row(self.hw_body, "名称", cpu["name"])
            info_row(self.hw_body, "制造商", cpu["manufacturer"])
            info_row(self.hw_body, "核心/线程", f"{cpu['cores']} 核 / {cpu['threads']} 线程")
            info_row(self.hw_body, "最大频率", f"{cpu['max_clock_mhz']} MHz")

        tk.Label(self.hw_body, text="GPU", font=("Microsoft YaHei", 11, "bold"),
                 fg=COLORS["section"], bg=COLORS["card"]).pack(anchor=tk.W, pady=(10, 2))
        multi_gpu = len(data["gpu"]) > 1
        for i, gpu in enumerate(data["gpu"], 1):
            sfx = str(i) if multi_gpu else ""
            info_row(self.hw_body, f"名称{sfx}", gpu["name"])
            vram = f"{gpu['vram_mb'] / 1024:.0f} GB" if gpu['vram_mb'] >= 1024 else f"{gpu['vram_mb']} MB"
            info_row(self.hw_body, f"显存{sfx}", vram)
            info_row(self.hw_body, f"驱动版本{sfx}", gpu["driver_version"])

        tk.Label(self.hw_body, text="内存", font=("Microsoft YaHei", 11, "bold"),
                 fg=COLORS["section"], bg=COLORS["card"]).pack(anchor=tk.W, pady=(10, 2))
        total_mem = sum(s["capacity_gb"] for s in data["memory"])
        info_row(self.hw_body, "总容量", f"{total_mem} GB")
        multi_mem = len(data["memory"]) > 1
        for i, stick in enumerate(data["memory"], 1):
            sfx = str(i) if multi_mem else ""
            info_row(self.hw_body, f"型号{sfx}", f"{stick['manufacturer']} {stick['part_number']}")
            info_row(self.hw_body, f"容量/频率{sfx}", f"{stick['capacity_gb']} GB / {stick['speed_mhz']} MHz")

        tk.Label(self.hw_body, text="硬盘", font=("Microsoft YaHei", 11, "bold"),
                 fg=COLORS["section"], bg=COLORS["card"]).pack(anchor=tk.W, pady=(10, 2))
        multi_disk = len(data["disk"]) > 1
        for i, disk in enumerate(data["disk"], 1):
            sfx = str(i) if multi_disk else ""
            info_row(self.hw_body, f"型号{sfx}", disk["model"])
            info_row(self.hw_body, f"制造商{sfx}", disk["manufacturer"])
            info_row(self.hw_body, f"容量{sfx}", f"{disk['size_gb']} GB")

        self.status.config(text="硬件检测完成")

    # ==================== System ====================
    def _load_system(self):
        threading.Thread(target=self._fetch_system, daemon=True).start()

    def _fetch_system(self):
        try:
            data = get_system_info()
            self.root.after(0, self._display_system, data)
        except Exception as e:
            err = str(e)
            self.root.after(0, lambda msg=err: self.status.config(text=f"系统信息获取失败: {msg}"))

    def _display_system(self, data):
        info_row(self.sys_body, "操作系统", data["os"])
        info_row(self.sys_body, "计算机名", data["hostname"])
        info_row(self.sys_body, "架构", data["machine_arch"])
        info_row(self.sys_body, "Python", data["python_version"])
        info_row(self.sys_body, "启动时间", data["boot_time"])
        tk.Label(self.sys_body, text="网络适配器", font=("Microsoft YaHei", 11, "bold"),
                 fg=COLORS["section"], bg=COLORS["card"]).pack(anchor=tk.W, pady=(8, 2))
        for adapter in data["adapters"]:
            if adapter["name"].startswith("Loopback"):
                continue
            ip_str = ", ".join(adapter["ips"]) if adapter["ips"] else "无 IP"
            info_row(self.sys_body, adapter["name"], ip_str)

    # ==================== Realtime ====================
    def _start_monitoring(self):
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    def _monitor_loop(self):
        while self._running:
            try:
                stats = get_realtime_stats()
                self.root.after(0, self._update_monitor, stats)
                # Recording
                if self._recording:
                    self.root.after(0, self._record_snapshot, stats)
            except Exception:
                pass
            time.sleep(2)

    def _update_monitor(self, stats):
        # Save scroll position to prevent jumping after widget rebuild
        scroll_pos = self.sf.canvas.yview()

        self.cpu_bar_set(stats["cpu_percent"])
        self.cpu_freq_lbl.config(text=f"{stats['cpu_temp_c']} °C / {stats['cpu_freq_mhz']} MHz")

        for w in self._gpu_widgets:
            w.destroy()
        self._gpu_widgets.clear()
        for gpu in stats.get("gpu", []):
            gpu_na = gpu["gpu_percent"] < 0
            row, set_fn = usage_bar(self._gpu_area, "GPU 使用率")
            if gpu_na:
                set_fn(0); row.winfo_children()[-1].config(text="N/A")
            else:
                set_fn(gpu["gpu_percent"])
            self._gpu_widgets.append(row)

            row2, set_fn2 = usage_bar(self._gpu_area, "显存使用率")
            if gpu_na:
                set_fn2(0); row2.winfo_children()[-1].config(text="N/A")
            else:
                set_fn2(gpu["mem_percent"])
            self._gpu_widgets.append(row2)

            if gpu_na:
                lbl2 = info_row(self._gpu_area, "GPU温度/频率", "N/A / N/A")
            else:
                lbl2 = info_row(self._gpu_area, "GPU温度/频率",
                                f"{gpu['temp_c']} °C / {gpu['clock_mhz']} MHz")
            self._gpu_widgets.append(lbl2.master)

            total_gb = gpu['mem_total_mb'] / 1024
            if gpu_na:
                lbl3 = info_row(self._gpu_area, "显存容量", f"{total_gb:.1f} GB")
            else:
                used_gb = gpu['mem_used_mb'] / 1024
                lbl3 = info_row(self._gpu_area, "显存用量",
                                f"{used_gb:.1f} GB / {total_gb:.1f} GB")
            self._gpu_widgets.append(lbl3.master)

        self.mem_bar_set(stats["mem_percent"])
        self.mem_detail_lbl.config(text=f"{stats['mem_used_gb']} GB / {stats['mem_total_gb']} GB")
        self.mem_freq_lbl.config(text=f"{stats['mem_freq_mhz']} MHz")
        self.swap_bar_set(stats["swap_percent"])

        for w in self._disk_widgets:
            w.destroy()
        self._disk_widgets.clear()
        for part in stats["disk_partitions"]:
            row, set_fn = usage_bar(self._disk_area, part["device"])
            set_fn(part["percent"])
            self._disk_widgets.append(row)

        now = time.time()
        if self._prev_time and self._prev_disk_io:
            dt = now - self._prev_time
            if dt > 0:
                rs = (stats["disk_read_mb"] - self._prev_disk_io[0]) / dt
                ws = (stats["disk_write_mb"] - self._prev_disk_io[1]) / dt
                self.disk_io_lbl.config(text=f"读: {rs:.1f} MB/s | 写: {ws:.1f} MB/s")
        self._prev_disk_io = (stats["disk_read_mb"], stats["disk_write_mb"])
        self._prev_time = now

        if self._prev_net_io:
            ns = stats["net_sent_mb"] - self._prev_net_io[0]
            nr = stats["net_recv_mb"] - self._prev_net_io[1]
            self.net_lbl.config(text=f"发送: {ns} MB | 接收: {nr} MB")
        self._prev_net_io = (stats["net_sent_mb"], stats["net_recv_mb"])

        # Restore scroll position
        self.sf.canvas.yview_moveto(scroll_pos[0])

    # ==================== Recording ====================
    def _toggle_recording(self):
        self._recording = not self._recording
        if self._recording:
            self.rec_btn.config(text="停止记录", bg=COLORS["red"])
            self.status.config(text="正在记录...")
        else:
            self.rec_btn.config(text="开始记录", bg=COLORS["green"])
            self.status.config(text=f"记录已停止，共 {len(self._records)} 条")

    def _on_interval_change(self, event=None):
        val = self.interval_var.get().replace("秒", "")
        self._rec_interval = int(val)

    def _record_snapshot(self, stats):
        gpu = stats.get("gpu", [{}])[0] if stats.get("gpu") else {}
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cpu_pct": stats["cpu_percent"],
            "cpu_temp": stats["cpu_temp_c"],
            "cpu_freq": stats["cpu_freq_mhz"],
            "gpu_pct": gpu.get("gpu_percent", "N/A"),
            "gpu_temp": gpu.get("temp_c", "N/A"),
            "gpu_mem": gpu.get("mem_percent", "N/A"),
            "mem_pct": stats["mem_percent"],
            "mem_freq": stats["mem_freq_mhz"],
        }
        self._records.append(record)
        self.tree.insert("", tk.END, values=(
            record["time"], record["cpu_pct"], f"{record['cpu_temp']} °C",
            f"{record['cpu_freq']} MHz",
            "N/A" if record["gpu_pct"] < 0 else record["gpu_pct"],
            "N/A" if record["gpu_temp"] < 0 else f"{record['gpu_temp']} °C",
            "N/A" if record["gpu_mem"] < 0 else record["gpu_mem"],
            record["mem_pct"], f"{record['mem_freq']} MHz",
        ))
        # Auto-scroll to bottom
        children = self.tree.get_children()
        if children:
            self.tree.see(children[-1])
        self.rec_count_lbl.config(text=f"记录: {len(self._records)} 条")

    def _clear_records(self):
        self._records.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.rec_count_lbl.config(text="记录: 0 条")

    def _export_csv(self):
        if not self._records:
            messagebox.showwarning("提示", "没有可导出的记录")
            return
        filename = f"SysInfo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        filepath = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=filename,
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
        if not filepath:
            return
        try:
            with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["时间", "CPU%", "CPU温度(°C)", "CPU频率(MHz)",
                                 "GPU%", "GPU温度(°C)", "显存%", "内存%", "内存频率(MHz)"])
                for r in self._records:
                    writer.writerow([
                        r["time"], r["cpu_pct"], r["cpu_temp"], r["cpu_freq"],
                        "N/A" if r["gpu_pct"] < 0 else r["gpu_pct"],
                        "N/A" if r["gpu_temp"] < 0 else r["gpu_temp"],
                        "N/A" if r["gpu_mem"] < 0 else r["gpu_mem"],
                        r["mem_pct"], r["mem_freq"],
                    ])
            messagebox.showinfo("提示", f"已导出到:\n{filepath}")
        except Exception as e:
            messagebox.showerror("错误", f"导出失败: {e}")

    # ==================== AI Analysis ====================
    def _collect_labels(self, frame):
        """Recursively collect all label texts from a frame."""
        texts = []
        for child in frame.winfo_children():
            if isinstance(child, tk.Label):
                text = child.cget("text")
                if text and text != "正在检测硬件...":
                    texts.append((child.cget("font").find("bold") >= 0, text))
            elif isinstance(child, tk.Frame):
                texts.extend(self._collect_labels(child))
        return texts

    def _build_hw_summary(self):
        """Build a text summary of hardware info for AI prompt."""
        parts = []
        for is_bold, text in self._collect_labels(self.hw_body):
            if is_bold:
                parts.append(f"\n【{text}】")
            else:
                parts.append(text)
        return "\n".join(parts)

    def _build_csv_text(self):
        """Build CSV text from records for AI prompt."""
        lines = ["时间,CPU%,CPU温度(°C),CPU频率(MHz),GPU%,GPU温度(°C),显存%,内存%,内存频率(MHz)"]
        for r in self._records:
            lines.append(",".join([
                r["time"], str(r["cpu_pct"]), str(r["cpu_temp"]), str(r["cpu_freq"]),
                "N/A" if r["gpu_pct"] < 0 else str(r["gpu_pct"]),
                "N/A" if r["gpu_temp"] < 0 else str(r["gpu_temp"]),
                "N/A" if r["gpu_mem"] < 0 else str(r["gpu_mem"]),
                str(r["mem_pct"]), str(r["mem_freq"]),
            ]))
        return "\n".join(lines)

    def _normalize_api_url(self, url):
        """Auto-append /v1/chat/completions if user only provides base URL."""
        url = url.rstrip("/")
        # Already has the full endpoint
        if url.endswith("/chat/completions"):
            return url
        # Has /v1 but missing chat/completions
        if url.endswith("/v1"):
            return url + "/chat/completions"
        # Just base URL, append full path
        return url + "/v1/chat/completions"

    def _run_ai_analysis(self):
        url = self._normalize_api_url(self.ai_url_entry.get().strip())
        api_key = self.ai_key_entry.get().strip()
        model = self.ai_model_entry.get().strip()

        if not url or not api_key or not model:
            messagebox.showwarning("提示", "请填写 API 地址、API Key 和模型名称")
            return
        if not self._records:
            messagebox.showwarning("提示", "没有记录数据，请先录制一段时间的监控数据")
            return

        self.ai_btn.config(state=tk.DISABLED)
        self.ai_status_lbl.config(text="正在分析，请稍候...")
        self.ai_response.config(state=tk.NORMAL)
        self.ai_response.delete("1.0", tk.END)
        self.ai_response.config(state=tk.DISABLED)

        threading.Thread(target=self._do_ai_request, args=(url, api_key, model),
                         daemon=True).start()

    def _do_ai_request(self, url, api_key, model):
        hw_summary = self._build_hw_summary()
        csv_text = self._build_csv_text()

        system_prompt = (
            "你是一个专业的电脑硬件分析师。用户会提供电脑的硬件配置信息和一段时间内的硬件监控数据（CPU使用率、温度、频率，"
            "GPU使用率、温度、显存占用，内存使用率、频率等）。请分析这些数据，找出可能的性能瓶颈或异常，"
            "例如：CPU/GPU是否过热导致降频、内存是否不足、显存是否溢出等。"
            "如果用户提到具体问题（如游戏掉帧），请结合数据给出可能的原因和优化建议。"
            "请用中文回答，条理清晰。"
        )

        user_msg = (
            f"以下是电脑的硬件配置信息：\n{hw_summary}\n\n"
            f"以下是一段时间内的硬件监控数据（每2秒采集一次）：\n{csv_text}\n\n"
            "请分析这些数据，找出可能的性能瓶颈或异常，并给出优化建议。"
        )

        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": 4096,
            "temperature": 0.7,
        }).encode("utf-8")

        try:
            req = urllib.request.Request(url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", f"Bearer {api_key}")
            # Debug: show URL being called
            print(f"[AI Request] URL: {url}")

            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            reply = result["choices"][0]["message"]["content"]
            self.root.after(0, self._show_ai_response, reply)
        except Exception as e:
            err = f"{e}\n请求地址: {url}"
            self.root.after(0, lambda msg=err: self._show_ai_error(msg))

    def _show_ai_response(self, text):
        self.ai_response.config(state=tk.NORMAL)
        self.ai_response.delete("1.0", tk.END)
        self.ai_response.insert(tk.END, text)
        self.ai_response.config(state=tk.DISABLED)
        self.ai_btn.config(state=tk.NORMAL)
        self.ai_status_lbl.config(text="分析完成")

    def _show_ai_error(self, msg):
        self.ai_response.config(state=tk.NORMAL)
        self.ai_response.delete("1.0", tk.END)
        self.ai_response.insert(tk.END, f"请求失败: {msg}\n\n常见解决方法:\n"
                                "1. 检查 API 地址是否正确（如 https://api.deepseek.com）\n"
                                "2. 检查 API Key 是否有效\n"
                                "3. 检查模型名称是否正确（如 deepseek-chat、gpt-4o）")
        self.ai_response.config(state=tk.DISABLED)
        self.ai_btn.config(state=tk.NORMAL)
        self.ai_status_lbl.config(text="分析失败")

    # ==================== Utils ====================
    def _refresh_all(self):
        for w in self.hw_body.winfo_children():
            if w != self.hw_status:
                w.destroy()
        self._load_hardware()
        self._load_system()

    def _copy_all(self):
        parts = []
        for child in self.hw_body.winfo_children():
            if isinstance(child, tk.Label):
                text = child.cget("text")
                if text and text != "正在检测硬件...":
                    parts.append(f"\n【{text}】" if child.cget("font").find("bold") >= 0 else text)
        parts.append("")
        for child in self.sys_body.winfo_children():
            if isinstance(child, tk.Label):
                parts.append(child.cget("text"))
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(parts))
        messagebox.showinfo("提示", "已复制到剪贴板")

    def _on_close(self):
        self._running = False
        self.root.destroy()


def main():
    root = tk.Tk()
    SysInfoApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
