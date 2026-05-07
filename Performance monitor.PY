"""
Windows 성능 모니터
실행: python performance_monitor.py
필요 패키지: pip install psutil wmi
"""

import tkinter as tk
from tkinter import ttk
import psutil
import time
import threading
from collections import deque
import platform
import wmi
import pystray
from PIL import Image
import pythoncom

# ── 색상 팔레트 ──────────────────────────────────────────────
BG_MAIN    = "#1a1a2e"  # 메인 창 배경, 콘텐츠 영역 배경
BG_TITLE   = "#16213e"  # 타이틀바 배경 (아이콘, 제목, 버튼 포함)
BG_TAB     = "#0d1b2a"  # 탭바 배경 (개요, CPU, 메모리, 디스크 탭)
BG_CARD    = "#0f1f30"  # 미니 카드 배경 (CPU 사용률, 메모리 카드)
BG_GRAPH   = "#0a141e"  # 스파크라인 그래프 캔버스 배경
BG_BOTTOM  = "#0d1b2a"  # 하단 상태바 배경 (가동시간, 프로세스)

BORDER     = "#2d2d4a"  # 창 테두리
BORDER2    = "#1a3050"  # 미니 카드 테두리

TEXT_PRI   = "#c8d8e8"  # 주요 텍스트 (미사용)
TEXT_SEC   = "#7090a8"  # 타이틀 텍스트 ("성능 모니터")
TEXT_MUTED = "#EEEFF0"  # 라벨 텍스트 (CPU, 메모리, 디스크 등), 상태바 텍스트

C_CPU      = "#4fc3f7"  # CPU 스파크라인 색상 및 라벨 색 (파란색)
C_MEM      = "#69f0ae"  # 메모리 스파크라인 색상 및 라벨 색 (초록색)
C_DISK     = "#ffcc02"  # 디스크 스파크라인 색상 및 라벨 색 (노란색)
C_NET      = "#ff7043"  # 네트워크 스파크라인 색상 및 라벨 색 (주황색)
C_GREEN    = "#69f0ae"  # 미사용

HISTORY_LEN = 71
UPDATE_MS   = 1000


# ── 스파크라인 캔버스 ──────────────────────────────────────────
class SparklineCanvas(tk.Canvas):
    def __init__(self, parent, color, max_val=100, **kwargs):
        kwargs.setdefault("height", 38)
        kwargs.setdefault("bg", BG_GRAPH)
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(parent, **kwargs)
        self.color = color
        self.max_val = max_val
        self.history = deque([0] * HISTORY_LEN, maxlen=HISTORY_LEN)

    def push(self, val):
        self.history.append(val)
        self.redraw()

    def redraw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 2 or h < 2:
            return

        # 격자선
        for frac in (0.33, 0.66):
            y = int(h * frac)
            self.create_line(0, y, w, y, fill="#ffffff", dash=(3, 4))

        data = list(self.history)
        n = len(data)
        if n < 2:
            return

        step = w / (n - 1)
        pts = [(i * step, h - (data[i] / max(self.max_val, 1)) * h) for i in range(n)]

        # 채우기 폴리곤
        fill_pts = [*pts, (pts[-1][0], h), (0, h)]
        flat = []
        for x, y in fill_pts:
            flat += [x, y]
        # 반투명 채우기는 tk에서 hex+alpha 미지원 → 별도 색 사용
        fill_color = self._darken(self.color, 0.18)
        self.create_polygon(flat, fill=fill_color, outline="")

        # 선
        line_pts = []
        for x, y in pts:
            line_pts += [x, y]
        self.create_line(line_pts, fill=self.color, width=1.5, smooth=False)

    def _darken(self, hex_color, alpha):
        """hex 색에 BG_GRAPH를 알파 블렌딩"""
        def parse(h):
            h = h.lstrip("#")
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        fr, fg, fb = parse(hex_color)
        br, bg_, bb = parse(BG_GRAPH)
        r = int(fr * alpha + br * (1 - alpha))
        g = int(fg * alpha + bg_ * (1 - alpha))
        b = int(fb * alpha + bb * (1 - alpha))
        return f"#{r:02x}{g:02x}{b:02x}"


# ── 메인 앱 ───────────────────────────────────────────────────
class PerfMonitor(tk.Tk):
    def __init__(self, start_minimized=False):
        super().__init__()
        self.title("성능 모니터")
        self.geometry("300x310")
        self.resizable(False, False)
        self.configure(bg=BG_MAIN)
        
        # 우측 하단에 위치 설정
        self.update_idletasks()
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        window_width = 300
        window_height = 310
        x = screen_width - window_width - 5  # 우측에서 10px 여백
        y = screen_height - window_height - 50  # 하단에서 50px 여백 (작업표시줄 고려)
        self.geometry(f"300x310+{x}+{y}")
        self.overrideredirect(True)   # 타이틀바 숨김 → 직접 그림

        self._tray_icon = None
        self._start_minimized = start_minimized

        # Always on top 상태
        self._always_on_top = False

        # 드래그용
        self._drag_x = 0
        self._drag_y = 0

        # 네트워크 이전 값
        net = psutil.net_io_counters()
        self._prev_net_bytes = net.bytes_sent + net.bytes_recv
        self._prev_net_time = time.time()

        # 시작 시각
        self._start_time = time.time()

        # 디스크 이름 (첫 번째 파티션)
        try:
            parts = psutil.disk_partitions()
            self._disk_dev = parts[0].device.replace("\\", "") if parts else "C:"
            disk_label = " ".join(p.device.replace("\\", "") for p in parts[:2])
        except Exception:
            disk_label = "C: D:"
            self._disk_dev = "C:"

        self._disk_label = disk_label

        self._build_ui()
        self._running = True
        
        # 트레이 아이콘 생성
        self._create_tray_icon()
        
        # 시작 시 창 숨기기
        if self._start_minimized:
            self.withdraw()
        
        self._update()

    # ── UI 구성 ──────────────────────────────────────────────
    def _build_ui(self):
        # 타이틀바
        bar = tk.Frame(self, bg=BG_TITLE, height=28)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        bar.bind("<ButtonPress-1>", self._drag_start)
        bar.bind("<B1-Motion>", self._drag_move)

        icon_label = tk.Label(bar, text="⬛", fg=C_CPU, bg=BG_TITLE, font=("Pretendard", 9))
        icon_label.pack(side="left", padx=(8, 4))
        icon_label.bind("<ButtonPress-1>", self._drag_start)
        icon_label.bind("<B1-Motion>", self._drag_move)

        title_lbl = tk.Label(bar, text="성능 모니터", fg=TEXT_SEC,
                             bg=BG_TITLE, font=("Pretendard", 9, "bold"))
        title_lbl.pack(side="left")
        title_lbl.bind("<ButtonPress-1>", self._drag_start)
        title_lbl.bind("<B1-Motion>", self._drag_move)

        # 컨트롤 버튼
        btn_frame = tk.Frame(bar, bg=BG_TITLE)
        btn_frame.pack(side="right", padx=8)

        close_btn = tk.Label(btn_frame, text="●", fg="#e74c3c", bg=BG_TITLE,
                             font=("Pretendard", 10), cursor="hand2")
        close_btn.pack(side="right", padx=2)
        close_btn.bind("<Button-1>", lambda e: self._quit())

        topmost_btn = tk.Label(btn_frame, text="●", fg="#27ae60", bg=BG_TITLE,
                               font=("Pretendard", 10), cursor="hand2")
        topmost_btn.pack(side="right", padx=2)
        topmost_btn.bind("<Button-1>", lambda e: self._toggle_topmost())
        self._topmost_btn = topmost_btn

        # 탭바 (장식용)
        tab_bar = tk.Frame(self, bg=BG_TAB, height=22)
        tab_bar.pack(fill="x")
        tab_bar.pack_propagate(False)
        for i, name in enumerate(["개요", "CPU", "메모리", "디스크"]):
            fg = C_CPU if i == 0 else TEXT_MUTED
            lbl = tk.Label(tab_bar, text=name, fg=fg, bg=BG_TAB,
                           font=("Pretendard", 8))
            lbl.pack(side="left", padx=8)

        sep = tk.Frame(self, bg=BORDER2, height=1)
        sep.pack(fill="x")

        # 콘텐츠 영역
        content = tk.Frame(self, bg=BG_MAIN)
        content.pack(fill="both", expand=True, padx=6, pady=4)

        # ─ 미니 카드 2개 ─
        card_row = tk.Frame(content, bg=BG_MAIN)
        card_row.pack(fill="x", pady=(0, 4))

        self.cpu_card_val = self._mini_card(card_row, "CPU 사용률", C_CPU, "Intel Core")
        self.mem_card_val = self._mini_card(card_row, "메모리", C_MEM, "16 GB")

        # ─ 스파크라인 4개 ─
        self.cpu_pct_lbl,  self.cpu_spark  = self._metric_row(content, "CPU",  C_CPU)
        self.mem_pct_lbl,  self.mem_spark  = self._metric_row(content, "메모리", C_MEM)
        self.disk_pct_lbl, self.disk_spark = self._metric_row(
            content, f"디스크 I/O  {self._disk_label}", C_DISK)
        self.net_pct_lbl,  self.net_spark  = self._metric_row(
            content, "네트워크", C_NET, max_val=200)

        # ─ 하단 상태바 ─
        bottom = tk.Frame(self, bg=BG_BOTTOM, height=18)
        bottom.pack(fill="x")
        bottom.pack_propagate(False)

        self.uptime_lbl = tk.Label(bottom, text="가동: 0:00:00",
                                   fg=TEXT_MUTED, bg=BG_BOTTOM,
                                   font=("Pretendard", 8))
        self.uptime_lbl.pack(side="left", padx=8)

        self.proc_lbl = tk.Label(bottom, text="프로세스: --",
                                 fg=TEXT_MUTED, bg=BG_BOTTOM,
                                 font=("Pretendard", 8))
        self.proc_lbl.pack(side="right", padx=8)

        # 창 테두리
        self.configure(highlightbackground=BORDER, highlightthickness=1)

    def _mini_card(self, parent, label_text, color, sub_text):
        card = tk.Frame(parent, bg=BG_CARD, highlightbackground=BORDER2,
                        highlightthickness=1)
        card.pack(side="left", fill="both", expand=True, padx=(0, 3))

        tk.Label(card, text=label_text, fg=TEXT_MUTED, bg=BG_CARD,
                 font=("Pretendard", 7)).pack(anchor="w", padx=6, pady=(4, 0))

        val_lbl = tk.Label(card, text="--", fg=color, bg=BG_CARD,
                           font=("Pretendard", 13, "bold"))
        val_lbl.pack(anchor="w", padx=6)

        sub_lbl = tk.Label(card, text=sub_text, fg=TEXT_MUTED, bg=BG_CARD,
                           font=("Pretendard", 8))
        sub_lbl.pack(anchor="w", padx=6, pady=(0, 4))

        return val_lbl, sub_lbl

    def _metric_row(self, parent, label_text, color, max_val=100):
        row = tk.Frame(parent, bg=BG_MAIN)
        row.pack(fill="x", pady=1)

        header = tk.Frame(row, bg=BG_MAIN)
        header.pack(fill="x")

        tk.Label(header, text=label_text, fg=TEXT_MUTED, bg=BG_MAIN,
                 font=("Pretendard", 8, "bold")).pack(side="left")

        pct_lbl = tk.Label(header, text="0%", fg=color, bg=BG_MAIN,
                           font=("Pretendard", 9, "bold"))
        pct_lbl.pack(side="right")

        spark = SparklineCanvas(row, color=color, max_val=max_val)
        spark.pack(fill="x")

        return pct_lbl, spark

    # ── 드래그 이동 ──────────────────────────────────────────
    def _drag_start(self, event):
        self._drag_x = event.x_root - self.winfo_x()
        self._drag_y = event.y_root - self.winfo_y()

    def _drag_move(self, event):
        self.geometry(f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}")

    def _toggle_topmost(self):
        """Always on top 토글"""
        self._always_on_top = not self._always_on_top
        self.attributes('-topmost', self._always_on_top)
        color = "#3498db" if self._always_on_top else "#27ae60"
        self._topmost_btn.config(fg=color)

    def _create_tray_icon(self):
        """트레이 아이콘 생성"""
        if self._tray_icon is None:
            # 간단한 아이콘 이미지 생성
            image = Image.new('RGB', (64, 64), color=(79, 195, 247))  # 파란색
            self._tray_icon = pystray.Icon("Performance Monitor", image, "성능 모니터")
            self._tray_icon.menu = pystray.Menu(
                pystray.MenuItem("보이기", lambda icon, item: self.after(0, self._show_window)),
                pystray.MenuItem("종료", self._quit)
            )
            threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _minimize(self):
        """창을 숨기고 트레이 아이콘 표시"""
        self.withdraw()
        self._create_tray_icon()

    def _show_window(self):
        """창을 다시 보임"""
        self.deiconify()
        self.lift()
        self.focus_force()
        if not getattr(self, '_always_on_top', False):
            self.attributes('-topmost', True)
            self.after(100, lambda: self.attributes('-topmost', False))
        # 트레이 아이콘은 앱 실행 동안 계속 유지합니다.

    # ── 데이터 업데이트 ──────────────────────────────────────
    def _collect(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        now = time.time()

        # 디스크 I/O 이용율 (활성 시간 %)
        try:
            pythoncom.CoInitialize()
            c = wmi.WMI()
            disks = c.Win32_PerfFormattedData_PerfDisk_LogicalDisk()
            for disk_info in disks:
                if disk_info.Name == '_Total':
                    disk = float(disk_info.PercentDiskTime)
                    break
            else:
                disk = 0.0
            pythoncom.CoUninitialize()
        except Exception:
            disk = 0.0

        net_now = psutil.net_io_counters()
        net_bytes = net_now.bytes_sent + net_now.bytes_recv
        dt = max(now - self._prev_net_time, 0.001)
        net_mbps = (net_bytes - self._prev_net_bytes) / dt / 1e6
        self._prev_net_bytes = net_bytes
        self._prev_net_time = now

        procs = len(psutil.pids())
        mem_info = psutil.virtual_memory()
        mem_used_gb = mem_info.used / 1e9
        mem_total_gb = mem_info.total / 1e9

        elapsed = int(now - self._start_time)
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        uptime = f"가동: {h}:{m:02d}:{s:02d}"

        return dict(cpu=cpu, mem=mem, disk=disk,
                    net_mbps=net_mbps,
                    procs=procs,
                    mem_used_gb=mem_used_gb,
                    mem_total_gb=mem_total_gb,
                    uptime=uptime)

    def _update(self):
        if not self._running:
            return

        d = self._collect()

        # 스파크라인
        self.cpu_spark.push(d["cpu"])
        self.mem_spark.push(d["mem"])
        self.disk_spark.push(d["disk"])
        net_clamped = min(d["net_mbps"], 200)
        self.net_spark.push(net_clamped)

        # 퍼센트 라벨
        self.cpu_pct_lbl.config(text=f"{d['cpu']:.0f}%")
        self.mem_pct_lbl.config(text=f"{d['mem']:.0f}%")
        self.disk_pct_lbl.config(text=f"{d['disk']:.0f}%")
        self.net_pct_lbl.config(text=f"{d['net_mbps']:.1f} Mbps")

        # 미니 카드
        self.cpu_card_val[0].config(text=f"{d['cpu']:.0f}%")
        self.mem_card_val[0].config(text=f"{d['mem']:.0f}%")
        self.mem_card_val[1].config(
            text=f"{d['mem_used_gb']:.1f} / {d['mem_total_gb']:.0f} GB")

        # 하단
        self.uptime_lbl.config(text=d["uptime"])
        self.proc_lbl.config(text=f"프로세스: {d['procs']}")

        self.after(UPDATE_MS, self._update)

    def _quit(self):
        self._running = False
        self.destroy()


if __name__ == "__main__":
    # psutil CPU 퍼센트 첫 호출 워밍업
    psutil.cpu_percent(interval=None)
    app = PerfMonitor()
    app.mainloop()