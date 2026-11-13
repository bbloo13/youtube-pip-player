"""유튜브 PIP 플레이어 (mpv + yt-dlp)

- 내 유튜브 계정 연동: 나중에 볼 영상 / 좋아요 표시한 동영상 / 내 재생목록
- 링크·검색으로 직접 만든 목록(재생 큐) 관리
- 다른 위젯(youtube-music-widget)과 같은 다크 위젯 디자인
"""
VERSION = "20261110"     # 날짜 버전 (youtube-music-widget 과 같은 방식)

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import urllib.request
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

from PIL import Image, ImageDraw, ImageTk

try:
    import websocket        # pip install websocket-client (로그인 자동 감지용)
except ImportError:         # 없으면 예전 방식(로그인 후 ⟳)으로 동작
    websocket = None

import resize_grips         # mpv 창 둘레의 크기 조절 띠 (같은 폴더의 resize_grips.py)

def _refresh_path():
    """시작 메뉴 등으로 켜면 winget이 추가한 PATH(yt-dlp, ffmpeg, deno)가 빠져 있을 수 있어서
    레지스트리의 최신 PATH를 합쳐 준다."""
    try:
        import winreg
        parts = os.environ.get("PATH", "").split(os.pathsep)
        for hive, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                          (winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    val, _ = winreg.QueryValueEx(k, "Path")
                for p in os.path.expandvars(val).split(os.pathsep):
                    if p and p not in parts:
                        parts.append(p)
            except OSError:
                pass
        os.environ["PATH"] = os.pathsep.join(parts)
    except Exception:
        pass


_refresh_path()

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "playlist.json")
MPV = r"C:\Program Files\MPV Player\mpv.exe"
# winget 등으로 다른 위치에 설치했으면 PATH 에서 찾는다 (콘솔용 mpv.com 이 아니라 창이 뜨는 mpv.exe)
if not os.path.exists(MPV):
    MPV = shutil.which("mpv.exe") or MPV
YTDLP = shutil.which("yt-dlp") or "yt-dlp"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 앱 전용 Edge 프로필 (여기에 구글 로그인 정보가 저장됨. 평소 쓰는 Edge와 분리)
PROFILE_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "youtube-pip-player", "edge-profile")
PROFILE = os.path.join(PROFILE_ROOT, "Default")
CACHE_FILE = os.path.join(os.path.dirname(PROFILE_ROOT), "cache.json")
AUTO_REFRESH_SEC = 300   # 자동 새로고침 주기(초)
LINKED_FLAG = os.path.join(PROFILE_ROOT, "linked.flag")  # 계정 목록을 한 번 읽어오는 데 성공하면 생김
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

SETTINGS_FILE = os.path.join(os.path.dirname(PROFILE_ROOT), "settings.json")
# 내 목록에서 뺐지만 아직 시청 기록에 못 올린 영상. 파일에 남겨 둬서 앱을 끄거나 재부팅해도 이어서 올린다
WATCH_PENDING_FILE = os.path.join(os.path.dirname(PROFILE_ROOT), "watch-pending.json")
WATCH_MAX_TRIES = 5            # 이만큼 실패하면 포기
WATCH_MAX_AGE = 30 * 86400     # 이보다 오래 못 올린 건 버린다(초)
MPV_IPC = r"\\.\pipe\yt-pip-mpv"
QUALITIES = [("360", "360p"), ("480", "480p"), ("720", "720p"), ("1080", "1080p")]
DEFAULT_QUALITY = "720"


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(d):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError:
        pass


class _PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def mpv_window_rect(pid):
    """mpv 창의 화면 위치/크기 (x, y, 너비, 높이; 물리 픽셀). 창이 없으면 None"""
    user32 = ctypes.windll.user32
    found = []
    old = None
    try:
        old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))   # 화면 배율과 상관없이 실제 픽셀로
    except Exception:
        pass

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lp):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True
    try:
        user32.EnumWindows(cb, 0)
        if not found:
            return None
        rc = wt.RECT()
        user32.GetWindowRect(found[0], ctypes.byref(rc))
        return rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top
    except Exception:
        return None
    finally:
        if old:
            try:
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))
            except Exception:
                pass


def proc_mem_mb(pid=None):
    """프로세스 램 사용량(MB). pid 없으면 이 앱 자신."""
    try:
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.GetCurrentProcess.restype = wt.HANDLE
        k32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(_PMC), wt.DWORD]
        k32.CloseHandle.argtypes = [wt.HANDLE]
        h = k32.OpenProcess(0x1000 | 0x0400, False, pid) if pid else k32.GetCurrentProcess()
        if not h:
            return None
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(pmc)
        ok = k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
        if pid:
            k32.CloseHandle(h)
        return round(pmc.WorkingSetSize / 1048576) if ok else None
    except Exception:
        return None


def mpv_ipc_many(commands):
    """mpv 에 명령 여러 개를 한 연결로 보내고 결과(data) 목록을 돌려받는다. 실패는 None."""
    res = []
    try:
        with open(MPV_IPC, "r+b", buffering=0) as f:
            for i, command in enumerate(commands):
                rid = 100 + i
                f.write((json.dumps({"command": command, "request_id": rid}) + "\n").encode())
                got = None
                for _ in range(80):
                    line = f.readline()
                    if not line:
                        break
                    msg = json.loads(line.decode("utf-8", "replace"))
                    if msg.get("request_id") == rid:
                        got = msg
                        break
                if got is None or got.get("error") != "success":
                    res.append(None)
                else:
                    res.append(got.get("data", True))
    except Exception:
        return [None] * len(commands)
    return res


def mpv_ipc(command, timeout=4):
    return mpv_ipc_many([command])[0]


STREAM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")
INPUT_CONF = os.path.join(os.path.dirname(PROFILE_ROOT), "mpv-input.conf")

# 목록용 '말랑한' 폰트 (프로젝트에 넣어 둔 Gowun Dodum 을 이 앱에서만 임시로 불러 쓴다)
FONT_FILE = os.path.join(HERE, "assets", "fonts", "GowunDodum-Regular.ttf")
LIST_FAMILY = "Segoe UI"
try:
    if os.path.exists(FONT_FILE) and ctypes.windll.gdi32.AddFontResourceExW(FONT_FILE, 0x10, 0):
        LIST_FAMILY = "Gowun Dodum"
except Exception:
    pass

MPV_ARGS = [
    "--ontop",
    "--border=no",                # 기본 윈도우 제목줄 제거 (제목줄은 Lua 컨트롤바에서 직접 그림)
    "--autofit=30%",
    "--geometry=0%:0%",                # 화면 왼쪽 위에 열기
    # 램 절약: 미리 받아 두는 버퍼를 작게 제한
    "--demuxer-max-bytes=16MiB",
    "--demuxer-max-back-bytes=4MiB",
    "--demuxer-readahead-secs=20",
    "--player-operation-mode=cplayer",   # 인자 없이 켜면 빈 창이 뜨는 pseudo-gui 모드가 되므로 끔
    "--force-window=no",
    "--vo=gpu",                   # gpu-next 보다 램을 덜 씀
    "--hwdec=auto-safe",         # 그래픽카드로 영상 풀기 (램/CPU 절약)
    "--osc=no",                   # mpv 기본 컨트롤바 대신 아래 Lua 로 만든 위젯 스타일 컨트롤바 사용
    "--osd-bar=no",
    f"--script={os.path.join(HERE, 'assets', 'mpv', 'yt-pip-osc.lua')}",
    "--idle=yes",                 # 영상이 없어도 대기 (미리 켜 두고 명령만 보내서 바로 재생)
    "--prefetch-playlist=yes",    # 다음 영상 주소를 미리 가져오기
    f"--input-ipc-server={MPV_IPC}",
]

# 디자인 토큰 (youtube-music-widget 의 style.css 와 동일)
BG = "#141418"
BG_SOFT = "#202026"      # 입력창/버튼 배경 (흰색 6% 정도)
BG_HOVER = "#2c2c33"
BG_SEG = "#1e1e23"       # 탭 토글 배경 (흰색 5%)
BG_SEG_ON = "#34343a"    # 선택된 탭 (흰색 12%)     # 호버 (흰색 8~12%)
FG = "#e8e8ea"
FG_MUTE = "#8f8fa3"
FG_DIM = "#6f6f83"
FG_BLUE = "#7a9cc9"      # 안내 문구용 부드러운 파랑
ACCENT = "#7fb5ff"
ACCENT_BG = "#233046"
BORDER = "#2a2a31"
FONT = ("Segoe UI", -12)               # 12px (검색창/기본)
FONT_B = ("Segoe UI Semibold", -12)
FONT_TITLE = ("Segoe UI Semibold", -13)  # 13px / 600 (.widget__title)
FONT_TAB = ("Segoe UI Semibold", -11)    # 11.5px (.widget__viewBtn)
FONT_LIST = (LIST_FAMILY, -12)           # 목록 행 (말랑한 폰트)
FONT_SMALL = ("Segoe UI", -11)
FONT_ICON = ("Segoe UI Symbol", -15)      # 미니 플레이어 버튼 아이콘
FONT_CHAN = (LIST_FAMILY, -11)           # 목록 행의 올린 사람 이름

TABS = [("queue", "내 목록"), ("rec", "추천"), ("subs", "구독"), ("pls", "재생목록")]
REMOTE_URL = {
    "rec": "https://www.youtube.com/feed/recommended",
    "subs": "https://www.youtube.com/feed/subscriptions",
    "wl": "https://www.youtube.com/playlist?list=WL",
    "ll": "https://www.youtube.com/playlist?list=LL",
    "pls": "https://www.youtube.com/feed/playlists",
}


def edge_path():
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    return shutil.which("msedge")


def auth_args():
    if os.path.isdir(PROFILE):
        return ["--cookies-from-browser", f"edge:{PROFILE}"]
    return []


def close_profile_edge():
    """앱 전용 Edge 창이 열려 있으면 쿠키 파일이 잠겨서 못 읽으므로, 그 창만 닫는다
    (평소 쓰는 Edge는 건드리지 않음)."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
          "Where-Object { $_.CommandLine -like '*youtube-pip-player*edge-profile*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=20, creationflags=NOWIN)
        time.sleep(1)
    except Exception:
        pass


LOGIN_URL = "https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com/"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cdp_browser_ws(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))["webSocketDebuggerUrl"]


def _cdp_call(ws_url, method, params=None, timeout=5):
    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return msg.get("result", {})
    finally:
        ws.close()


def youtube_login_cookie_present(port):
    """Edge 디버그 포트로 쿠키를 읽어 유튜브 로그인(SAPISID)이 됐는지 본다. 쿠키 값은 쓰지 않고 있는지만 확인."""
    cookies = _cdp_call(_cdp_browser_ws(port), "Storage.getCookies").get("cookies", [])
    return any(c.get("name") == "SAPISID" and c.get("value") and c.get("domain", "").endswith("youtube.com")
               for c in cookies)


def wait_for_youtube_login(port, proc, stop=lambda: False, timeout=900, connect_grace=30, settle=3.0):
    """로그인 창이 열려 있는 동안 쿠키를 지켜보다가, 유튜브 로그인이 되면 창을 정상 종료한다.
    반환: ok(로그인 확인·창 닫음) / closed(사용자가 창을 닫음) / unreachable(창에 연결 못 함)
          / timeout / stopped(새 로그인 시도가 이 감시를 대체함)"""
    start = time.time()
    connected = False
    fails = 0
    while time.time() - start < timeout:
        if stop():
            return "stopped"
        time.sleep(1.5)
        try:
            found = youtube_login_cookie_present(port)
            connected = True
            fails = 0
        except Exception:
            fails += 1
            if connected and fails >= 3:      # 연결됐던 창이 계속 응답이 없다 = 닫힘
                return "closed"
            if not connected and (proc.poll() is not None):
                return "closed"
            if not connected and time.time() - start > connect_grace:
                return "unreachable"
            continue
        if found:
            time.sleep(settle)                # 구글이 나머지 쿠키를 마저 심을 시간
            try:
                _cdp_call(_cdp_browser_ws(port), "Browser.close")   # 정상 종료여야 쿠키가 파일에 저장된다
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                close_profile_edge()
            return "ok"
    return "timeout"


def run_ytdlp(args, timeout=90, auth=False):
    cmd = [YTDLP, "--no-warnings", "--encoding", "utf-8", *(auth_args() if auth else []), *args]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, creationflags=NOWIN,
                       env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    return r.stdout, r.stderr


def mark_watched(url):
    """yt-dlp --mark-watched: 유튜브가 이 영상을 '시청 완료'로 기록하게 한다 (로그인 쿠키 필요).
    유튜브 사이트가 보내는 것과 같은 재생 시작 / 시청 완료 알림 두 개를 보낸다. 성공하면 True.
    --no-warnings 는 일부러 안 쓴다: 실패는 "Unable to mark ..." 경고로만 알려주기 때문."""
    cmd = [YTDLP, "--encoding", "utf-8", *auth_args(), "--mark-watched", "--simulate", "--no-playlist", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=90, creationflags=NOWIN,
                           env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    except Exception:
        return False
    text = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0 and "Marking fully watched" in text and "Unable to mark" not in text


def load_watch_pending():
    try:
        with open(WATCH_PENDING_FILE, encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        return []
    now = time.time()
    return [p for p in items if isinstance(p, dict) and p.get("id") and p.get("url")
            and p.get("tries", 0) < WATCH_MAX_TRIES and now - p.get("t", now) < WATCH_MAX_AGE]


def save_watch_pending(items):
    try:
        os.makedirs(os.path.dirname(WATCH_PENDING_FILE), exist_ok=True)
        tmp = WATCH_PENDING_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, WATCH_PENDING_FILE)      # 쓰다가 꺼져도 파일이 깨지지 않게
    except OSError:
        pass


CHAN_FILE = os.path.join(os.path.dirname(PROFILE_ROOT), "channels.json")
CHAN = {}          # 영상 id -> 올린 사람(채널) 이름
try:
    with open(CHAN_FILE, encoding="utf-8") as _f:
        CHAN.update(json.load(_f))
except Exception:
    pass


def save_chan():
    try:
        os.makedirs(os.path.dirname(CHAN_FILE), exist_ok=True)
        if len(CHAN) > 3000:                       # 너무 쌓이면 오래된 것부터 버림
            for k in list(CHAN)[:len(CHAN) - 3000]:
                CHAN.pop(k, None)
        with open(CHAN_FILE, "w", encoding="utf-8") as f:
            json.dump(CHAN, f, ensure_ascii=False)
    except OSError:
        pass


def clean_chan(x):
    x = (x or "").strip()
    return "" if x in ("NA", "None", "null") else x


def fetch_channels(urls):
    """채널 이름이 아직 없는 영상들의 올린 사람을 한 번에 조회해서 CHAN 에 채운다"""
    ids = [(video_id(u), u) for u in urls if video_id(u) and video_id(u) not in CHAN]
    if not ids:
        return False
    out, _ = run_ytdlp(["--skip-download", "--no-playlist", "--ignore-errors",
                        "--print", "%(id)s\t%(channel,uploader|)s", *[u for _, u in ids]],
                       timeout=180)
    got = False
    for line in out.splitlines():
        if "\t" in line:
            vid, name = line.split("\t", 1)
            name = clean_chan(name)
            if vid and name:
                CHAN[vid] = name
                got = True
    if got:
        save_chan()
    return got


def fetch_entries(query, auth=False, limit=50):
    """링크(영상/재생목록/채널) 또는 검색어 -> [(제목, URL), ...]"""
    target = query if query.startswith(("http://", "https://")) else f"ytsearch10:{query}"
    out, err = run_ytdlp(
        ["--flat-playlist", "--playlist-end", str(limit),
         "--print", "%(title)s\t%(webpage_url,url)s\t%(channel,uploader|)s", target],
        timeout=120, auth=auth)
    items = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        title, url = line.split("\t", 1)
        chan = ""
        if "\t" in url:
            url, chan = url.split("\t", 1)
        if url.startswith("http") and title.strip() not in ("[Private video]", "[Deleted video]"):
            items.append((title.strip() or url, url.strip()))
            vid, chan = video_id(url), clean_chan(chan)
            if vid and chan:
                CHAN[vid] = chan
    if not items and err.strip():
        raise RuntimeError(err.strip().splitlines()[-1][:160])
    return items


def rrect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1+r, y1, x2-r, y1, x2, y1, x2, y1+r, x2, y2-r, x2, y2, x2-r, y2,
           x1+r, y2, x1, y2, x1, y2-r, x1, y1+r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


class Flat(tk.Canvas):
    """.widget__iconBtn 처럼 평소엔 투명, 호버 시 둥글게 밝아지는 버튼"""

    def __init__(self, master, text, command, active=False, bg=BG, font=FONT, px=10, py=5, fg=None):
        f = tkfont.Font(font=font)
        w, h = f.measure(text) + px * 2, f.metrics("linespace") + py * 2
        super().__init__(master, width=w, height=h, bg=bg, highlightthickness=0,
                         bd=0, cursor="hand2")
        self._bg, self._cmd, self.active = bg, command, active
        self._fg = fg or FG_MUTE
        self._font, self._px, self._py = font, px, py
        self.shape = rrect(self, 1, 1, w - 1, h - 1, 7, fill=bg, outline=bg)
        self.txt = self.create_text(w // 2, h // 2, text=text, font=font, fill=self._fg)
        self.bind("<Enter>", lambda e: self._paint(True))
        self.bind("<Leave>", lambda e: self._paint(False))
        self.bind("<Button-1>", lambda e: self._cmd())
        self._paint(False)

    def set_text(self, text):
        f = tkfont.Font(font=self._font)
        w, h = f.measure(text) + self._px * 2, f.metrics("linespace") + self._py * 2
        self.config(width=w, height=h)
        self.delete("all")
        self.shape = rrect(self, 1, 1, w - 1, h - 1, 7, fill=self._bg, outline=self._bg)
        self.txt = self.create_text(w // 2, h // 2, text=text, font=self._font, fill=self._fg)
        self._paint(False)

    def set_active(self, v):
        self.active = v
        self._paint(False)

    def _paint(self, hover):
        if self.active:
            fill, fg = ACCENT_BG, ACCENT
        elif hover:
            fill, fg = BG_HOVER, "#ffffff"
        else:
            fill, fg = self._bg, self._fg
        self.itemconfig(self.shape, fill=fill, outline=fill)
        self.itemconfig(self.txt, fill=fg)


class Segmented(tk.Canvas):
    """.widget__viewToggle: 둥근 배경 안에 탭, 선택된 탭만 밝게"""

    def __init__(self, master, tabs, command, bg=BG):
        f = tkfont.Font(font=FONT_TAB)
        self.tabs, self.command, self.cur = tabs, command, tabs[0][0]
        pad, gap, edge = 10, 2, 3
        h = f.metrics("linespace") + 12
        widths = [f.measure(t) + pad * 2 for _, t in tabs]
        total = sum(widths) + gap * (len(tabs) - 1) + edge * 2
        super().__init__(master, width=total, height=h + edge * 2, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        rrect(self, 1, 1, total - 1, h + edge * 2 - 1, 9, fill=BG_SEG, outline=BG_SEG)
        self.pills, self.labels, self.spans = {}, {}, []
        x = edge
        for (key, text), w in zip(tabs, widths):
            self.pills[key] = rrect(self, x, edge, x + w, edge + h, 7, fill=BG_SEG, outline=BG_SEG)
            self.labels[key] = self.create_text(x + w // 2, edge + h // 2, text=text,
                                                font=FONT_TAB, fill=FG_MUTE)
            self.spans.append((x, x + w, key))
            x += w + gap
        self.bind("<Button-1>", self._click)
        self.set(self.cur)

    def _click(self, e):
        for x1, x2, key in self.spans:
            if x1 <= e.x <= x2:
                self.command(key)
                return

    def set(self, key):
        self.cur = key
        for k in self.pills:
            on = k == key
            self.itemconfig(self.pills[k], fill=BG_SEG_ON if on else BG_SEG,
                            outline=BG_SEG_ON if on else BG_SEG)
            self.itemconfig(self.labels[k], fill=ACCENT if on else FG_MUTE)


class RoundEntry(tk.Canvas):
    """.widget__searchInput: 둥근 입력창, 포커스 시 파란 테두리"""

    def __init__(self, master, bg=BG, height=30):
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0)
        self.h = height
        self.shape = rrect(self, 1, 1, 10, height - 1, 8, fill=BG_SOFT, outline=BORDER)
        self.entry = tk.Entry(self, bg=BG_SOFT, fg=FG, insertbackground=FG, relief="flat",
                              font=FONT, bd=0, highlightthickness=0)
        self.win = self.create_window(10, height // 2, window=self.entry, anchor="w")
        self.bind("<Configure>", self._resize)
        self.entry.bind("<FocusIn>", lambda e: self.itemconfig(self.shape, outline=ACCENT))
        self.entry.bind("<FocusOut>", lambda e: self.itemconfig(self.shape, outline=BORDER))

    def _resize(self, e):
        self.delete(self.shape)
        self.shape = rrect(self, 1, 1, e.width - 1, self.h - 1, 8, fill=BG_SOFT,
                           outline=ACCENT if self.focus_get() is self.entry else BORDER)
        self.tag_lower(self.shape)
        self.itemconfig(self.win, width=max(e.width - 22, 10))

    def get(self):
        return self.entry.get()

    def delete_text(self):
        self.entry.delete(0, "end")


def ytdl_format(q):
    # H.264 를 우선 (그래픽카드로 풀 수 있어 램/CPU 가 적게 듦). 없으면 그냥 가장 좋은 것
    return (f"bestvideo[height<={q}][vcodec^=avc1]+bestaudio"
            f"/bestvideo[height<={q}]+bestaudio/best[height<={q}]")


class Resolver:
    """영상 주소를 미리 가져와 두는 도우미 (클릭하면 바로 열리게).
    yt-dlp 로 스트림 주소를 뽑아 캐시해 두고, 재생할 때 mpv 에 바로 넘긴다.
    여러 개를 동시에(WORKERS) 뽑고, 캐시는 파일에도 저장해서 앱을 껐다 켜도 이어 쓴다."""
    TTL = 5400   # 유튜브 스트림 주소는 몇 시간 뒤 만료되므로 1.5시간만 사용
    MAXN = 120
    WORKERS = 4
    FILE = os.path.join(os.path.dirname(PROFILE_ROOT), "resolved.json")

    def __init__(self):
        self.cache = {}
        self.todo = []
        self.busy = set()
        self.cv = threading.Condition()
        self._load()
        for _ in range(self.WORKERS):
            threading.Thread(target=self._loop, daemon=True).start()

    def _load(self):
        try:
            with open(self.FILE, encoding="utf-8") as f:
                raw = json.load(f)
            now = time.time()
            for k, (t, urls) in raw.items():
                url, q = k.rsplit("|", 1)
                if now - t < self.TTL:
                    self.cache[(url, q)] = (t, urls)
        except Exception:
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.FILE), exist_ok=True)
            with open(self.FILE, "w", encoding="utf-8") as f:
                json.dump({f"{u}|{q}": v for (u, q), v in self.cache.items()}, f)
        except OSError:
            pass

    def get(self, url, q):
        e = self.cache.get((url, q))
        if e and time.time() - e[0] < self.TTL:
            return e[1]
        return None

    def want(self, urls, q, front=False):
        with self.cv:
            keys = []
            for u in urls:
                k = (u, q)
                if self.get(u, q) is None and k not in self.todo and k not in self.busy and k not in keys:
                    keys.append(k)
            if front:       # 방금 고른 영상이 이미 대기열에 있으면 맨 앞으로
                self.todo = keys + [k for k in self.todo if k not in keys]
            else:
                self.todo = self.todo + keys
            self.cv.notify_all()

    def resolve_now(self, url, q):
        """이미 뽑는 중이면 그걸 기다리고(같은 일을 두 번 하지 않음), 아니면 지금 바로 뽑는다"""
        k = (url, q)
        for _ in range(200):
            hit = self.get(url, q)
            if hit:
                return hit
            with self.cv:
                if k in self.busy:
                    pass
                else:
                    if k in self.todo:
                        self.todo.remove(k)
                    break
            time.sleep(0.15)
        return self.get(url, q) or self._resolve(url, q)

    def _resolve(self, url, q):
        for auth in ([False, True] if os.path.isdir(PROFILE) else [False]):
            try:
                out, _ = run_ytdlp(["-f", ytdl_format(q), "-g", "--no-playlist", url],
                                   timeout=90, auth=auth)
            except Exception:
                continue
            urls = [x for x in out.split() if x.startswith("http")][:2]
            if urls:
                self.cache[(url, q)] = (time.time(), urls)
                if len(self.cache) > self.MAXN:
                    oldest = min(self.cache, key=lambda k: self.cache[k][0])
                    self.cache.pop(oldest, None)
                self._save()
                return urls
        return None

    def _loop(self):
        while True:
            with self.cv:
                while not self.todo:
                    self.cv.wait()
                key = self.todo.pop(0)
                self.busy.add(key)
            try:
                self._resolve(*key)
            except Exception:
                pass
            finally:
                with self.cv:
                    self.busy.discard(key)


THUMB_DIR = os.path.join(os.path.dirname(PROFILE_ROOT), "thumbs")
TW, TH = 88, 50
ROW_H = 62
VID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/)([\w-]{11})")


def video_id(url):
    m = VID_RE.search(url or "")
    return m.group(1) if m else None


def round_thumb(path):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    target = TW / TH
    if w / h > target:
        nw = int(h * target)
        im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    else:
        nh = int(w / target)
        im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    im = im.resize((TW, TH), Image.LANCZOS)
    mask = Image.new("L", (TW * 4, TH * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, TW * 4 - 1, TH * 4 - 1), radius=28, fill=255)
    im.putalpha(mask.resize((TW, TH), Image.LANCZOS))
    return ImageTk.PhotoImage(im)


class ThumbList(tk.Canvas):
    """썸네일 + 제목(최대 2줄) 목록. 클릭/Ctrl/Shift 선택, 더블클릭, Delete, 휠 스크롤."""

    def __init__(self, master):
        super().__init__(master, bg=BG, highlightthickness=0, bd=0, takefocus=1)
        self.rows, self.sel, self.anchor = [], set(), None
        self.off, self.hover = 0, None
        self.imgs, self.requested = {}, set()
        self.dlq = queue.Queue()
        self.font = tkfont.Font(font=FONT_LIST)
        self.cfont = tkfont.Font(font=FONT_CHAN)
        self.wrap_cache = {}
        self.on_activate = self.on_delete = self.on_select = None
        self.row_btns = []   # [(글자, 배경색, 글자색, 콜백), ...] 오른쪽 끝부터. 마우스를 올린 행에 버튼이 나온다
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self._click)
        self.bind("<Double-Button-1>", lambda e: self.on_activate and self.on_activate())
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self.bind("<Delete>", lambda e: self.on_delete and self.on_delete())
        for _ in range(3):
            threading.Thread(target=self._dl_loop, daemon=True).start()

    # --- API (Listbox 와 비슷하게) ---
    def set_items(self, rows):
        self.rows = rows
        self.sel = {i for i in self.sel if i < len(rows)}
        self._clamp()
        self.redraw()

    def reset(self):
        self.sel, self.anchor, self.off = set(), None, 0

    def curselection(self):
        return tuple(sorted(self.sel))

    def set_selection(self, idx):
        self.sel = set(idx)
        self.redraw()

    selection_set = lambda self, i: self.set_selection(list(self.sel) + [i])

    # --- 내부 ---
    def _clamp(self):
        mx = max(0, len(self.rows) * ROW_H - self.winfo_height())
        self.off = max(0, min(self.off, mx))

    def _idx(self, y):
        i = (y + self.off) // ROW_H
        return int(i) if 0 <= i < len(self.rows) else None

    def _click(self, e):
        self.focus_set()
        i = self._idx(e.y)
        if i is None:
            return
        for k, b in enumerate(self.row_btns):
            if abs(e.x - (self.winfo_width() - 28 - 28 * k)) <= 14:
                b[3](i)
                return
        if e.state & 0x4:
            self.sel ^= {i}
            self.anchor = i
        elif e.state & 0x1 and self.anchor is not None:
            lo, hi = sorted((self.anchor, i))
            self.sel = set(range(lo, hi + 1))
        else:
            self.sel, self.anchor = {i}, i
        self.redraw()
        if self.on_select:
            self.on_select(i)

    def _wheel(self, e):
        self.off -= int(e.delta / 120 * ROW_H * 0.8)
        self._clamp()
        self.redraw()

    def _motion(self, e):
        self._set_hover(self._idx(e.y))

    def _set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self.redraw()

    def _wrap(self, text, maxw):
        key = (text, maxw)
        if key in self.wrap_cache:
            return self.wrap_cache[key]
        f, lines, cur = self.font, [], ""
        for ch in text:
            if f.measure(cur + ch) <= maxw:
                cur += ch
                continue
            if not lines:
                k = cur.rfind(" ")
                if k > len(cur) * 0.5:
                    lines.append(cur[:k])
                    cur = cur[k + 1:] + ch
                else:
                    lines.append(cur)
                    cur = ch
            else:
                while cur and f.measure(cur + "…") > maxw:
                    cur = cur[:-1]
                lines.append(cur + "…")
                cur = None
                break
        if cur is not None:
            lines.append(cur)
        if len(self.wrap_cache) > 600:
            self.wrap_cache.clear()
        self.wrap_cache[key] = lines
        return lines

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        n = len(self.rows)
        if w < 20 or h < 20:
            return
        if n == 0:
            self.create_text(w // 2, h // 2, text="목록이 비어 있어요", font=FONT_SMALL, fill=FG_DIM)
            return
        first = max(0, self.off // ROW_H)
        last = min(n, (self.off + h) // ROW_H + 2)
        lh = self.font.metrics("linespace")
        for i in range(first, last):
            y = i * ROW_H - self.off
            on = i in self.sel
            if on or i == self.hover:
                col = ACCENT_BG if on else BG_HOVER
                rrect(self, 2, y + 2, w - 10, y + ROW_H - 2, 10, fill=col, outline=col)
            r = self.rows[i]
            tx, ty = 10, y + (ROW_H - TH) // 2
            if r.get("folder"):
                rrect(self, tx, ty, tx + TW, ty + TH, 9, fill=BG_SOFT, outline=BG_SOFT)
                self.create_text(tx + TW // 2, ty + TH // 2, text="▤", font=(LIST_FAMILY, -22), fill=ACCENT)
            else:
                img = self.imgs.get(r.get("vid"))
                if img:
                    self.create_image(tx, ty, image=img, anchor="nw")
                else:
                    rrect(self, tx, ty, tx + TW, ty + TH, 9, fill=BG_SOFT, outline=BG_SOFT)
                    if r.get("vid"):
                        self._request(r["vid"])
            tw = w - (tx + TW + 10) - 34 - 28 * max(0, len(self.row_btns) - 1)
            lines = self._wrap(r["title"], tw)
            chan = r.get("chan") or ""
            clh = self.cfont.metrics("linespace") + 1 if chan else 0
            top = y + ROW_H // 2 - (len(lines) * lh + clh) // 2
            color = ACCENT if (on or r.get("folder")) else "#c9c9d3"
            for k, line in enumerate(lines):
                self.create_text(tx + TW + 10, top + k * lh, text=line, anchor="nw",
                                 font=FONT_LIST, fill=color)
            if chan:
                name = self._wrap(chan, tw)[0]
                self.create_text(tx + TW + 10, top + len(lines) * lh + 1, text=name, anchor="nw",
                                 font=FONT_CHAN, fill=FG_BLUE if not on else "#9cc4ff")
            if self.row_btns and i == self.hover and not r.get("folder"):
                for k, b in enumerate(self.row_btns):
                    cx, cy = w - 28 - 28 * k, y + ROW_H // 2
                    self.create_text(cx, cy, text=b[0], font=("Segoe UI Semibold", -18), fill=b[2])
        total = n * ROW_H
        if total > h:
            bh = max(24, h * h // total)
            by = self.off * (h - bh) // max(1, total - h)
            rrect(self, w - 6, by + 1, w - 2, by + bh - 1, 2, fill=BORDER, outline=BORDER)

    # --- 썸네일 내려받기 ---
    def _request(self, vid):
        if vid in self.requested:
            return
        self.requested.add(vid)
        self.dlq.put(vid)

    def _dl_loop(self):
        while True:
            vid = self.dlq.get()
            path = os.path.join(THUMB_DIR, vid + ".jpg")
            try:
                if not os.path.exists(path):
                    os.makedirs(THUMB_DIR, exist_ok=True)
                    req = urllib.request.Request(
                        f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
                        headers={"User-Agent": "Mozilla/5.0"})
                    data = urllib.request.urlopen(req, timeout=15).read()
                    with open(path, "wb") as f:
                        f.write(data)
                self.after(0, lambda v=vid, pth=path: self._loaded(v, pth))
            except Exception:
                self.requested.discard(vid)

    def _loaded(self, vid, path):
        try:
            self.imgs[vid] = round_thumb(path)
        except Exception:
            return
        self.redraw()


class Gauge(tk.Canvas):
    """램 사용량 게이지 바: mpv(진한 파랑) + 앱(어두운 파랑), 많이 쓰면 주황색"""
    CAP = 600  # 이 값(MB)이 가득 찬 상태

    def __init__(self, master, bg=BG, height=20):
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0)
        self.h, self.vals = height, (None, None)
        self.bind("<Configure>", lambda e: self.redraw())

    def set(self, app_mb, mpv_mb):
        self.vals = (app_mb, mpv_mb)
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.h
        if w < 40:
            return
        r = h // 2 - 1
        rrect(self, 1, 1, w - 1, h - 1, r, fill=BG_SEG, outline=BG_SEG)
        app, mpv = self.vals
        app, mpv = app or 0, mpv or 0
        inner = w - 2
        mpv_w = min(inner, inner * mpv / self.CAP)
        both_w = min(inner, inner * (mpv + app) / self.CAP)
        warn = (mpv + app) > self.CAP * 0.75
        c_app, c_mpv = ("#8a5a2b", "#d9822b") if warn else ("#2c4a7a", "#3d6fb8")
        if both_w > 2 * r:
            rrect(self, 1, 1, 1 + both_w, h - 1, r, fill=c_app, outline=c_app)
        if mpv_w > 2 * r:
            rrect(self, 1, 1, 1 + mpv_w, h - 1, r, fill=c_mpv, outline=c_mpv)
        text = f"램  영상 {mpv}MB  ·  앱 {app}MB" if mpv else f"램  영상 대기  ·  앱 {app}MB"
        self.create_text(10, h // 2, text=text, anchor="w", font=FONT_SMALL, fill="#ffffff")


def ask_dialog(parent, title, msg, ok="예", cancel="아니요"):
    """앱 디자인에 맞춘 확인 창. cancel=None 이면 확인 버튼만 있는 안내 창. 확인하면 True."""
    d = tk.Toplevel(parent)
    d.overrideredirect(True)
    d.configure(bg=BG)
    d.attributes("-topmost", True)
    result = {"v": False}

    def close(v):
        result["v"] = v
        d.destroy()

    box = tk.Frame(d, bg=BG, highlightthickness=1, highlightbackground=BORDER)
    box.pack(fill="both", expand=True)
    tk.Label(box, text=title, font=FONT_TITLE, fg=FG, bg=BG, anchor="w").pack(
        fill="x", padx=18, pady=(16, 4))
    tk.Label(box, text=msg, font=FONT, fg=FG_MUTE, bg=BG, anchor="w", justify="left",
             wraplength=300).pack(fill="x", padx=18, pady=(0, 14))
    row = tk.Frame(box, bg=BG)
    row.pack(fill="x", padx=12, pady=(0, 12))
    Flat(row, ok, lambda: close(True), active=True, bg=BG, font=FONT_B, px=16).pack(side="right")
    if cancel:
        Flat(row, cancel, lambda: close(False), bg=BG, font=FONT, px=16).pack(side="right", padx=(0, 6))
    d.bind("<Escape>", lambda e: close(False))
    d.bind("<Return>", lambda e: close(True))
    d.update_idletasks()
    w, h = max(320, d.winfo_reqwidth()), d.winfo_reqheight()
    x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
    d.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
    try:
        d.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(d.winfo_id()) or d.winfo_id()
        pref = ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass
    d.focus_force()
    d.grab_set()
    parent.wait_window(d)
    return result["v"]


class MiniPlayer(tk.Frame):
    """미니 모드: 지금 재생 중인 영상 제목 + 진행바 + 이전/재생·일시정지/다음 (youtube-music-widget 의 미니 플레이어처럼)"""

    def __init__(self, master, app):
        super().__init__(master, bg=BG)
        self.app = app
        self.font = tkfont.Font(font=FONT_B)
        self.title = tk.Label(self, text="", font=FONT_B, fg=FG, bg=BG, anchor="w")
        self.title.pack(fill="x", padx=14, pady=(2, 0))
        self.sub = tk.Label(self, text="", font=FONT_SMALL, fg=FG_BLUE, bg=BG, anchor="w")
        self.sub.pack(fill="x", padx=14)
        self.prog = tk.Canvas(self, height=14, bg=BG, highlightthickness=0, bd=0, cursor="hand2")
        self.prog.pack(fill="x", padx=14, pady=(6, 0))
        self.prog.bind("<Button-1>", self._seek)
        self.prog.bind("<Configure>", lambda e: self._draw())
        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=10, pady=(2, 10))
        self.time = tk.Label(row, text="", font=FONT_SMALL, fg=FG_MUTE, bg=BG)
        self.time.pack(side="left", padx=(4, 0))
        self.btn_next = Flat(row, "▶▶", lambda: self._cmd(["playlist-next"]), px=12, font=FONT_ICON)
        self.btn_next.pack(side="right")
        self.btn_play = Flat(row, "▶", self._toggle, active=True, px=18, font=FONT_ICON)
        self.btn_play.pack(side="right", padx=2)
        self.btn_prev = Flat(row, "◀◀", lambda: self._cmd(["playlist-prev"]), px=12, font=FONT_ICON)
        self.btn_prev.pack(side="right")
        self.frac, self.state, self.busy = 0.0, None, False

    def _cmd(self, c):
        threading.Thread(target=lambda: mpv_ipc(c), daemon=True).start()
        self.after(400, self.refresh)

    def _toggle(self):
        if self.state is None:               # 재생 중이 아니면 목록 처음부터 재생
            self.app.play_all_from_mini()
        else:
            self._cmd(["cycle", "pause"])

    def _seek(self, e):
        w = self.prog.winfo_width() - 2
        if self.state is not None and w > 0:
            pct = max(0.0, min(100.0, (e.x - 1) * 100.0 / w))
            self._cmd(["seek", pct, "absolute-percent+exact"])

    def _draw(self):
        c = self.prog
        c.delete("all")
        w = c.winfo_width()
        if w < 10:
            return
        y = 7
        rrect(c, 1, y - 3, w - 1, y + 3, 3, fill="#2a2a31", outline="#2a2a31")
        fw = int((w - 2) * self.frac)
        if fw > 6:
            rrect(c, 1, y - 3, 1 + fw, y + 3, 3, fill=ACCENT, outline=ACCENT)

    @staticmethod
    def _fmt(t):
        t = int(t or 0)
        return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"

    def refresh(self):
        """mpv 상태를 뒤에서 읽어 와서 화면에 반영 (미니 모드일 때만 1초마다)"""
        if self.busy:
            return
        self.busy = True

        def work():
            res = mpv_ipc_many([["get_property", "idle-active"], ["get_property", "time-pos"],
                                ["get_property", "duration"], ["get_property", "pause"],
                                ["get_property", "playlist-pos"]])
            self.after(0, lambda: self._apply(res))
        threading.Thread(target=work, daemon=True).start()

    def _apply(self, res):
        self.busy = False
        idle, pos, dur, paused, ppos = res
        items = self.app.playing_items
        if res[0] is None or idle is not False or pos is None:
            self.state = None
            self.title.config(text="재생 중인 영상이 없어요")
            self.sub.config(text="▶ 를 누르면 내 목록을 처음부터 재생해요")
            self.time.config(text="")
            self.frac = 0.0
            self.btn_play.set_text("▶")
        else:
            self.state = True
            name, chan = "", ""
            if isinstance(ppos, int) and 0 <= ppos < len(items):
                name = items[ppos][0]
                chan = CHAN.get(video_id(items[ppos][1]), "")
            self.title.config(text=self._ellipsize(name or "재생 중"))
            self.sub.config(text=chan)
            self.time.config(text=f"{self._fmt(pos)} / {self._fmt(dur)}")
            self.frac = max(0.0, min(1.0, (pos or 0) / dur)) if dur else 0.0
            self.btn_play.set_text("❚❚" if not paused else "▶")
        self._draw()

    def _ellipsize(self, text):
        maxw = max(100, self.winfo_width() - 32)
        if self.font.measure(text) <= maxw:
            return text
        while text and self.font.measure(text + "…") > maxw:
            text = text[:-1]
        return text + "…"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YouTube PIP")
        self.configure(bg=BG)
        self.overrideredirect(True)
        self.attributes("-alpha", 0.96)
        self.geometry("400x640+80+80")
        self.minsize(300, 200)

        self.queue = []
        self.remote = {}          # view -> [(title, url)]
        self.view = "queue"
        self.nav_stack = []       # 재생목록 탭에서 들어간 목록 (뒤로가기용)
        self.shown = []           # 지금 화면에 보이는 항목
        self.proc = None
        self.pinned = False
        self.mini = False
        self.busy = 0
        self.inflight = set()
        self.login_opened = False
        self.login_token = 0
        self.quality = load_settings().get("quality", DEFAULT_QUALITY)
        self.resolver = Resolver()
        self.mpv_lock = threading.Lock()
        self.playing_items = []
        self.mark_pending = load_watch_pending()   # 시청 기록에 올릴 영상 (재부팅해도 남음)
        self.mark_runner = False
        self.mark_lock = threading.Lock()
        self.cache = {}           # 목록 주소 -> {"t": 시각, "items": [...]}
        self.hidden = list(load_settings().get("hidden_rec", []))   # 추천에서 치운 영상
        self.load()
        self.load_cache()
        self.build()
        self.show("queue")
        self.update_login_btn()     # 시작할 때부터 '✓ 연동됨' / '로그인' 이 맞게 보이도록
        self.update_idletasks()
        self.round_corners()
        self.bind("<Configure>", self._on_resize)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(1000, self.update_mem)
        self.grips = resize_grips.Grips(self, lambda: self.proc.pid if self.mpv_alive() else None,
                                        on_resized=self.remember_mpv_window)
        self.after(1500, self.grips.start)
        self.after(2000, lambda: threading.Thread(target=self._warm_mpv, daemon=True).start())
        self.after(1500, self.prefetch)
        self.after(8000, self.kick_mark_worker)    # 지난번에 못 올린 게 있으면 이어서
        self.after(AUTO_REFRESH_SEC * 1000, self.auto_tick)

    def round_corners(self):
        """다른 위젯처럼 둥근 모서리 (Windows 11 DWM, 안 되면 영역 자르기)"""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
            pref = ctypes.c_int(2)  # DWMWCP_ROUND
            r = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
            self._dwm_ok = r == 0
            if not self._dwm_ok:
                self._clip_round(hwnd)
        except Exception:
            pass

    def _clip_round(self, hwnd=None):
        import ctypes
        hwnd = hwnd or ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
        w, h = self.winfo_width(), self.winfo_height()
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, 24, 24)
        ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)

    def _on_resize(self, e):
        if e.widget is self and not getattr(self, "_dwm_ok", True):
            self._clip_round()

    # ---------------- UI ----------------
    def build(self):
        pad = 12
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=pad, pady=(10, 6))
        title = tk.Label(head, text="YouTube", font=FONT_TITLE, fg=FG, bg=BG)
        title.pack(side="left")
        title2 = tk.Label(head, text=" PIP", font=FONT_TITLE, fg=ACCENT, bg=BG)
        title2.pack(side="left")
        for w in (head, title, title2):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
        self.btn_close = Flat(head, "✕", self.on_close)
        self.btn_close.pack(side="right")
        self.btn_mini = Flat(head, "─", self.toggle_mini)
        self.btn_mini.pack(side="right")
        self.btn_pin = Flat(head, "고정", self.toggle_pin)
        self.btn_pin.pack(side="right")
        self.btn_refresh = Flat(head, "⟳", self.refresh_remote)
        self.btn_refresh.pack(side="right")
        self.btn_login = Flat(head, "로그인", self.login, fg=ACCENT)
        self.update_login_btn()
        self.btn_login.pack(side="right")

        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.minibar = MiniPlayer(self, self)      # 미니 모드에서만 보임

        tabs = tk.Frame(self.body, bg=BG)
        tabs.pack(fill="x", padx=pad - 4)
        self.seg = Segmented(tabs, TABS, self.show)
        self.seg.pack(side="left", padx=4)

        self.nav = tk.Frame(self.body, bg=BG)
        self.btn_back = Flat(self.nav, "‹ 뒤로", self.go_back)
        self.btn_back.pack(side="left")
        self.nav_label = tk.Label(self.nav, text="", font=FONT_B, fg=FG, bg=BG)
        self.nav_label.pack(side="left", padx=4)

        self.addrow = tk.Frame(self.body, bg=BG)
        self.addrow.pack(fill="x", padx=pad, pady=(8, 0))
        box = RoundEntry(self.addrow)
        box.pack(side="left", fill="x", expand=True)
        self.entry = box.entry
        self.entry.bind("<Return>", lambda e: self.add())
        Flat(self.addrow, "추가/검색", self.add, bg=BG).pack(side="left", padx=(4, 0))

        self.status = tk.StringVar(value="링크(영상·재생목록·채널) 또는 검색어를 입력하세요")
        self.status_lbl = tk.Label(self.body, textvariable=self.status, font=FONT_SMALL,
                                   fg=FG_BLUE, bg=BG, anchor="w")
        self.status_lbl.pack(fill="x", padx=pad, pady=(4, 2))

        mid = tk.Frame(self.body, bg=BG)
        mid.pack(fill="both", expand=True, padx=pad - 4, pady=(0, 6))
        self.lb = ThumbList(mid)
        self.lb.pack(fill="both", expand=True)
        self.lb.on_activate = self.activate
        self.lb.on_delete = self.remove
        self.lb.on_select = self.on_select

        self.gauge = Gauge(self.body)
        self.gauge.pack(fill="x", padx=pad, pady=(0, 6))

        opts = tk.Frame(self.body, bg=BG)
        opts.pack(fill="x", padx=pad - 4, pady=(0, 6))
        tk.Label(opts, text="화질", font=FONT_SMALL, fg=FG_BLUE, bg=BG).pack(side="left", padx=(4, 2))
        self.qseg = Segmented(opts, QUALITIES, self.set_quality)
        self.qseg.pack(side="left")
        self.qseg.set(self.quality)

        self.bar = tk.Frame(self.body, bg=BG)
        self.bar.pack(fill="x", padx=pad - 4, pady=(0, 10))
        self.edit_btns = []
        Flat(self.bar, "▶ 전체", self.play_all, fg=ACCENT).pack(side="left")
        Flat(self.bar, "▶ 선택부터", self.play_from_selected, fg=ACCENT).pack(side="left")
        Flat(self.bar, "■", self.stop).pack(side="left")
        self.btn_toqueue = Flat(self.bar, "＋ 내 목록에 담기", self.to_queue, fg=ACCENT)
        for text, cmd in [("비우기", self.clear), ("삭제", self.remove),
                          ("▼", lambda: self.move(1)), ("▲", lambda: self.move(-1))]:
            b = Flat(self.bar, text, cmd)
            b.pack(side="right")
            self.edit_btns.append(b)

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.winfo_x(), e.y_root - self.winfo_y()

    def _drag_move(self, e):
        self.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.attributes("-topmost", self.pinned)
        self.btn_pin.set_active(self.pinned)

    def toggle_mini(self):
        self.mini = not self.mini
        if self.mini:
            self._full_h = self.winfo_height()
            self.body.pack_forget()
            self.minibar.pack(fill="x")
            self.minsize(300, 60)
            self.update_idletasks()
            self.geometry(f"{self.winfo_width()}x{self.winfo_reqheight()}")
            self.minibar.refresh()
            self.after(1000, self._mini_tick)
        else:
            self.minibar.pack_forget()
            self.minsize(300, 200)
            self.body.pack(fill="both", expand=True)
            self.geometry(f"{self.winfo_width()}x{self._full_h}")
        self.btn_mini.set_active(self.mini)

    def _mini_tick(self):
        if self.mini:
            self.minibar.refresh()
            self.after(1000, self._mini_tick)

    def play_all_from_mini(self):
        if self.queue:
            self.play(0, items=list(self.queue))
            self.after(1500, self.minibar.refresh)

    # ---------------- 저장 ----------------
    def load(self):
        try:
            with open(DATA, encoding="utf-8") as f:
                self.queue = [tuple(x) for x in json.load(f)]
        except Exception:
            self.queue = []

    def save(self):
        with open(DATA, "w", encoding="utf-8") as f:
            json.dump(self.queue, f, ensure_ascii=False, indent=1)

    # ---------------- 화면 전환 ----------------
    def show(self, view, items=None, label=None):
        self.view = view
        if view != "pls" or items is None and not self.nav_stack:
            pass
        self.seg.set(view)
        is_q = view == "queue"
        self.set_row_btn()
        # 편집 버튼은 내 목록에서만, '내 목록에 담기'는 계정 탭에서만
        for b in self.edit_btns:
            b.pack_forget() if not is_q else b.pack(side="right")
        if is_q:
            self.btn_toqueue.pack_forget()
        else:
            self.btn_toqueue.pack(side="left")
        self.addrow.pack(fill="x", padx=12, pady=(8, 0), before=self.status_lbl) if is_q else self.addrow.pack_forget()

        if is_q:
            self.nav.pack_forget()
            self.nav_stack = []
            self.shown = self.queue
            self.status.set("링크(영상·재생목록·채널) 또는 검색어를 입력하세요")
        else:
            if view == "pls" and self.nav_stack:
                self.nav.pack(fill="x", padx=8, before=self.status_lbl)
                self.nav_label.config(text=self.nav_stack[-1][0])
            else:
                self.nav.pack_forget()
            if items is not None:
                self.shown = items
            else:
                self.shown = self.remote.get(view, [])
                self.fetch_remote(view)
        self.lb.reset()
        self.render()

    def render(self):
        if self.view in ("rec", "subs"):
            hid = set(self.hidden)
            self.shown = [x for x in self.shown if x[1] not in hid]
        folder = self.view == "pls" and not self.nav_stack
        rows = [{"title": t, "vid": None if folder else video_id(u), "folder": folder,
                 "chan": None if folder else CHAN.get(video_id(u))}
                for t, u in self.shown]
        self.lb.set_items(rows)
        if not folder:
            self.fill_channels()
        if self.view == "queue":
            self.save()
        self.prewarm()

    def fill_channels(self):
        """목록에 있는데 올린 사람 이름을 아직 모르는 영상은 뒤에서 한꺼번에 조회해서 채운다"""
        tried = self.__dict__.setdefault("_chan_tried", set())
        urls = [u for _, u in self.shown[:40]
                if video_id(u) and video_id(u) not in CHAN and video_id(u) not in tried][:8]
        if not urls or getattr(self, "_chan_busy", False):
            return
        tried.update(video_id(u) for u in urls)
        self._chan_busy = True

        def work():
            try:
                if fetch_channels(urls):
                    self.after(0, self.render)
            except Exception:
                pass
            finally:
                self._chan_busy = False
        threading.Thread(target=work, daemon=True).start()

    # ---------------- 계정 목록 ----------------
    def is_linked(self):
        return os.path.exists(LINKED_FLAG)

    def update_login_btn(self):
        self.btn_login.set_text("✓ 연동됨" if self.is_linked() else "로그인")

    def set_linked(self, ok):
        try:
            if ok:
                os.makedirs(PROFILE_ROOT, exist_ok=True)
                open(LINKED_FLAG, "w").close()
            elif os.path.exists(LINKED_FLAG):
                os.remove(LINKED_FLAG)
        except OSError:
            pass
        self.update_login_btn()

    def login(self):
        if self.is_linked() and not ask_dialog(self,
                "이미 연동됨",
                "다른 계정으로 바꾸겠습니까?"):
            return
        edge = edge_path()
        if not edge:
            ask_dialog(self, "Edge 없음", "Edge 실행 파일을 찾지 못했어요.", "확인", None)
            return
        os.makedirs(PROFILE_ROOT, exist_ok=True)
        self.login_token += 1
        self.status.set("로그인 창을 여는 중... 열린 창에서 구글에 로그인하세요 (끝나면 자동으로 닫혀요)")
        threading.Thread(target=self._login_worker, args=(edge, self.login_token), daemon=True).start()

    def _login_worker(self, edge, token):
        auto = websocket is not None
        if auto:
            close_profile_edge()     # 같은 프로필의 창이 이미 열려 있으면 디버그 포트 옵션이 무시되므로 먼저 닫는다
        port = free_port()
        args = [edge, f"--user-data-dir={PROFILE_ROOT}", "--no-first-run", "--no-default-browser-check"]
        if auto:
            args.append(f"--remote-debugging-port={port}")
        proc = subprocess.Popen(args + [LOGIN_URL])
        self.login_opened = True
        threading.Thread(target=self._raise_login_window, daemon=True).start()
        if not auto:
            return
        result = wait_for_youtube_login(port, proc, stop=lambda: token != self.login_token)
        if result != "stopped":
            self.after(0, lambda: self._login_done(result))

    def _login_done(self, result):
        if result == "ok":
            self.login_opened = False
            self.status.set("로그인 확인됨 · 계정에서 불러오는 중...")
            self.fetch_remote("rec", force=True)
        elif result == "closed":
            self.status.set("로그인 창이 닫혔어요 · 연동 확인 중...")
            self.fetch_remote("rec", force=True)
        else:
            self.status.set("로그인 창과 연결하지 못했어요. 로그인한 뒤 ⟳ 를 눌러주세요")

    def _raise_login_window(self):
        """앱에서 연 Edge 창이 뒤에 숨거나 최소화돼 있으면 맨 앞으로 가져온다"""
        user32 = ctypes.windll.user32
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
              "Where-Object { $_.CommandLine -like '*youtube-pip-player*edge-profile*' } | "
              "ForEach-Object { $_.ProcessId }")
        for _ in range(16):
            time.sleep(0.5)
            try:
                out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                     capture_output=True, text=True, timeout=20,
                                     creationflags=NOWIN).stdout
                pids = {int(x) for x in out.split() if x.isdigit()}
            except Exception:
                pids = set()
            found = []

            @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
            def cb(hwnd, _lp):
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in pids and user32.GetWindow(hwnd, 4) == 0 and \
                        user32.GetWindowTextLengthW(hwnd) > 0:
                    found.append(hwnd)
                return True
            user32.EnumWindows(cb, 0)
            if found:
                h = found[0]
                user32.keybd_event(0x12, 0, 0, 0)       # Alt 를 눌러 두면 앞으로 가져오기가 허용됨
                user32.ShowWindow(h, 9)                  # 최소화돼 있으면 복원
                user32.SetForegroundWindow(h)
                user32.keybd_event(0x12, 0, 2, 0)
                break
        self.after(0, lambda: self.status.set(
            "열린 창에서 구글에 로그인하세요 (끝나면 자동으로 닫혀요)" if websocket is not None else
            "열린 창에서 구글 로그인 후 ⟳ 를 눌러주세요 (그 창은 자동으로 닫혀요)"))

    def refresh_remote(self):
        if self.view == "queue":
            # 내 목록에서 ⟳ 를 눌러도 계정 연동 확인(로그인 직후)이 되도록 추천 목록을 불러온다
            self.status.set("계정 확인 중...")
            self.fetch_remote("rec", force=True)
            return
        if self.nav_stack:
            self.fetch_remote("pls", url=self.nav_stack[-1][1], force=True)
        else:
            self.fetch_remote(self.view, force=True)

    # --- 캐시 ---
    def load_cache(self):
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            self.cache = {k: {"t": v["t"], "items": [tuple(x) for x in v["items"]]}
                          for k, v in raw.items()}
        except Exception:
            self.cache = {}
        for view, url in REMOTE_URL.items():
            if url in self.cache:
                self.remote[view] = self.cache[url]["items"]

    def save_cache(self):
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False)
        except OSError:
            pass

    @staticmethod
    def ago(t):
        sec = int(time.time() - t)
        if sec < 60:
            return "방금"
        return f"{sec // 60}분 전"

    def current_target(self):
        if self.view == "queue":
            return None
        if self.view == "pls" and self.nav_stack:
            return self.nav_stack[-1][1]
        return REMOTE_URL[self.view]

    # --- 불러오기 ---
    def fetch_remote(self, view, url=None, force=False, silent=False):
        """캐시가 있으면 먼저 보여주고, 오래됐거나 force 면 뒤에서 새로 불러온다."""
        if not os.path.isdir(PROFILE):
            if not silent:
                self.status.set("먼저 '로그인'을 눌러 구글 계정에 로그인하세요")
            return
        target = url or REMOTE_URL[view]
        cached = self.cache.get(target)
        showing = target == self.current_target()
        if cached and not silent and showing:
            self.shown = cached["items"]
            self.render()
        if cached and not force and time.time() - cached["t"] < AUTO_REFRESH_SEC:
            if not silent and showing:
                self.status.set(f"{len(cached['items'])}개 · {self.ago(cached['t'])} 갱신")
            return
        if target in self.inflight:
            return
        self.inflight.add(target)
        if showing:
            n = len(cached["items"]) if cached else 0
            self.status.set(f"{n}개 · 갱신 중..." if cached else "계정에서 불러오는 중...")
        threading.Thread(target=self._remote_worker, args=(view, target, silent),
                         daemon=True).start()

    def _remote_worker(self, view, target, silent):
        items, err = [], None
        try:
            if self.login_opened:
                close_profile_edge()
                self.login_opened = False
            try:
                items = fetch_entries(target, auth=True, limit=200)
            except Exception as e:
                if "cookie" not in str(e).lower():
                    raise
                close_profile_edge()   # 앱 전용 Edge 창이 쿠키를 잡고 있었다면 닫고 한 번 더
                items = fetch_entries(target, auth=True, limit=200)
        except Exception as e:
            err = str(e)
        self.after(0, lambda: self._remote_done(view, target, items, err, silent))

    def _remote_done(self, view, target, items, err, silent):
        self.inflight.discard(target)
        showing = target == self.current_target()
        if err:
            low = err.lower()
            if "cookie" in low:
                err = "쿠키를 읽지 못했어요. 앱 전용 Edge 창을 닫고 ⟳ 를 눌러주세요"
            elif "not a bot" in low:
                err = "유튜브가 봇 확인을 요구해요. 잠시 뒤 ⟳ 를 다시 눌러주세요 (로그인 정보는 그대로예요)"
            elif "sign in" in low or "login" in low:
                self.set_linked(False)
                err += "  (로그인이 필요해요. '로그인' 후 구글에 로그인하세요)"
            if (showing or self.view == "queue") and not silent:
                self.status.set(f"실패: {err}")
            elif showing:
                self.status.set(f"{len(self.shown)}개 · 자동 갱신 실패 (이전 목록 표시 중)")
            return
        self.set_linked(True)
        self.kick_mark_worker()
        self.cache[target] = {"t": time.time(), "items": items}
        self.save_cache()
        if view in REMOTE_URL and target == REMOTE_URL[view]:
            self.remote[view] = items
        if showing:
            self.shown = items
            self.status.set(f"{len(items)}개 · 방금 갱신")
            self.render()
        elif self.view == "queue" and not silent:
            self.status.set("✓ 계정 연동 확인됨 · '추천'·'재생목록' 탭에서 볼 수 있어요")

    def prefetch(self):
        """앱을 켜면 탭을 열기 전에 미리 불러와 둔다 (캐시가 신선하면 건너뜀)"""
        if self.is_linked():
            self.fetch_remote("rec", silent=True)
            self.after(3000, lambda: self.fetch_remote("subs", silent=True))
            self.after(6000, lambda: self.fetch_remote("pls", silent=True))
        elif os.path.exists(os.path.join(PROFILE, "Network", "Cookies")):
            # 프로필에 로그인한 기록은 있는데 연동 표시만 없는 경우: 한 번 확인해서 되면 스스로 연동 처리
            self.fetch_remote("rec", silent=True)

    def mark_removed(self, items):
        """내 목록에서 뺀 영상은 다 본 것으로 보고 유튜브 시청 기록에 올린다.
        계정이 연동돼 있을 때만. 먼저 파일에 적어 두고(재부팅해도 남도록) 뒤에서 하나씩 올린다.
        '비우기'는 일부러 제외: 목록 전체를 한 번에 '봤다'로 올리는 건 의도가 아닐 가능성이 크다."""
        if not self.is_linked():
            return
        with self.mark_lock:
            have = {p["id"] for p in self.mark_pending}
            for title, url in items:
                vid = video_id(url)
                if vid and vid not in have:
                    self.mark_pending.append({"id": vid, "title": title, "url": url, "tries": 0, "t": time.time()})
                    have.add(vid)
            save_watch_pending(self.mark_pending)
        self.kick_mark_worker()

    def kick_mark_worker(self):
        """올릴 게 남아 있고 계정이 연동돼 있으면, 뒤에서 한 번에 하나씩 시청 기록에 올린다"""
        if not self.is_linked():
            return
        with self.mark_lock:
            if self.mark_runner or not self.mark_pending:
                return
            self.mark_runner = True
        threading.Thread(target=self._mark_pending_worker, daemon=True).start()

    def _mark_pending_worker(self):
        failed = set()      # 이번 실행에서 실패한 건 이번엔 더 시도하지 않는다 (다음에 켤 때/다음에 지울 때 다시)
        while True:
            with self.mark_lock:
                p = next((x for x in self.mark_pending if x["id"] not in failed), None)
                if p is None:
                    self.mark_runner = False
                    return
            ok = mark_watched(p["url"])
            with self.mark_lock:
                if ok:
                    self.mark_pending = [x for x in self.mark_pending if x["id"] != p["id"]]
                else:
                    failed.add(p["id"])
                    p["tries"] = p.get("tries", 0) + 1
                    if p["tries"] >= WATCH_MAX_TRIES:
                        self.mark_pending = [x for x in self.mark_pending if x["id"] != p["id"]]
                save_watch_pending(self.mark_pending)
            if ok:
                self.after(0, lambda t=p["title"]: self.status.set(f"✓ 시청 기록에 올렸어요 · {t}"))

    def auto_tick(self):
        """몇 분마다 자동 새로고침: 나중에 볼 영상 + 지금 보고 있는 목록"""
        try:
            self.resolver.want([u for _, u in self.queue[:30]], self.quality)   # 만료된 주소 갱신
            if self.is_linked():
                self.fetch_remote("rec", force=True, silent=True)
                cur = self.current_target()
                if cur and cur != REMOTE_URL["rec"]:
                    url = None if cur in REMOTE_URL.values() else cur
                    self.fetch_remote(self.view, url=url, force=True, silent=True)
        finally:
            self.after(AUTO_REFRESH_SEC * 1000, self.auto_tick)

    def go_back(self):
        if self.nav_stack:
            self.nav_stack.pop()
        self.show("pls", items=self.remote.get("pls", []))

    def activate(self):
        """더블클릭: 내 목록/영상은 그 영상부터 재생, 재생목록 항목은 안으로 들어가기"""
        sel = self.lb.curselection()
        if not sel:
            return
        if self.view == "pls" and not self.nav_stack:
            title, url = self.shown[sel[0]]
            self.nav_stack.append((title, url))
            self.set_row_btn()
            self.shown = []
            self.nav.pack(fill="x", padx=8, before=self.status_lbl)
            self.nav_label.config(text=title)
            self.lb.reset()
            self.render()
            self.fetch_remote("pls", url=url)
        else:
            self.play(sel[0])

    # ---------------- 내 목록 편집 ----------------
    def add(self):
        q = self.entry.get().strip()
        if not q:
            return
        self.status.set("불러오는 중...")
        threading.Thread(target=self._add_worker, args=(q,), daemon=True).start()

    def _add_worker(self, q):
        try:
            found = fetch_entries(q, auth=os.path.isdir(PROFILE))
        except Exception as e:
            self.after(0, lambda: self.status.set(f"실패: {e}"))
            return
        self.after(0, lambda: self._after_fetch(q, found))

    def _after_fetch(self, q, found):
        if not found:
            self.status.set("결과가 없어요")
            return
        if q.startswith(("http://", "https://")):
            self.queue.extend(found)
            self.entry.delete(0, "end")
            self.status.set(f"{len(found)}개 추가됨")
            if self.view == "queue":
                self.render()
        else:
            self.pick_dialog(found)

    def pick_dialog(self, found):
        win = tk.Toplevel(self, bg=BG)
        win.title("검색 결과")
        win.geometry("420x320")
        win.attributes("-topmost", True)
        lb = tk.Listbox(win, selectmode="extended", activestyle="none", bg=BG, fg=FG,
                        font=FONT_LIST, relief="flat", bd=0, highlightthickness=0,
                        selectbackground=ACCENT_BG, selectforeground=ACCENT)
        for t, _ in found:
            lb.insert("end", t)
        lb.pack(fill="both", expand=True, padx=12, pady=12)

        def ok():
            sel = lb.curselection()
            self.queue.extend(found[i] for i in sel)
            win.destroy()
            self.entry.delete(0, "end")
            self.status.set(f"{len(sel)}개 추가됨")
            if self.view == "queue":
                self.render()

        Flat(win, "선택한 영상 추가", ok).pack(pady=(0, 10))
        lb.bind("<Double-Button-1>", lambda e: ok())

    def to_queue(self):
        sel = self.lb.curselection() or range(len(self.shown))
        if self.view == "pls" and not self.nav_stack:
            self.status.set("재생목록은 더블클릭해서 안으로 들어간 뒤 담아주세요")
            return
        picked = [self.shown[i] for i in sel]
        self.queue.extend(picked)
        self.save()
        self.resolver.want([u for _, u in picked], self.quality)
        self.status.set(f"내 목록에 {len(picked)}개 담음")

    def move(self, d):
        sel = list(self.lb.curselection())
        if not sel:
            return
        order = sel if d < 0 else sel[::-1]
        new = []
        for i in order:
            j = i + d
            if 0 <= j < len(self.queue) and j not in new and j not in sel:
                self.queue[i], self.queue[j] = self.queue[j], self.queue[i]
                new.append(j)
            else:
                new.append(i)
        self.render()
        self.lb.set_selection(new)

    def remove(self):
        if self.view != "queue":
            return
        removed = []
        for i in reversed(self.lb.curselection()):
            self.hide_rec(self.queue[i][1])     # 지운 영상은 추천/구독에도 다시 안 뜨게
            removed.append(self.queue[i])
            del self.queue[i]
        self.lb.set_selection([])
        self.render()
        self.mark_removed(removed)

    def set_row_btn(self):
        """내 목록: 행마다 ✕(삭제) / 추천: ＋(내 목록에 담기) 와 ✕(관심 없음) / 재생목록 안: ＋"""
        add = ("+", ACCENT_BG, ACCENT, self.add_one)
        if self.view == "queue":
            self.lb.row_btns = [("✕", "#3a2a30", "#ff6b6b", self.remove_one)]
        elif self.view in ("rec", "subs"):
            self.lb.row_btns = [("✕", "#3a2a30", "#ff6b6b", self.dismiss_one), add]
        elif self.view == "pls" and not self.nav_stack:
            self.lb.row_btns = []
        else:
            self.lb.row_btns = [add]

    def hide_rec(self, url):
        if url not in self.hidden:
            self.hidden.append(url)
            self.hidden = self.hidden[-1000:]
            st = load_settings()
            st["hidden_rec"] = self.hidden
            save_settings(st)

    def dismiss_one(self, i):
        """추천에서 ✕: 이 영상을 목록에서 치우고, 다음 새로고침 때도 다시 안 나오게 한다"""
        if self.view in ("rec", "subs") and 0 <= i < len(self.shown):
            t, u = self.shown[i]
            self.hide_rec(u)
            self.lb.set_selection([])
            self.render()
            self.status.set(f"목록에서 치웠어요 · {t[:22]}")

    def add_one(self, i):
        if not (0 <= i < len(self.shown)):
            return
        t, u = self.shown[i]
        if any(u == q[1] for q in self.queue):
            self.status.set("이미 내 목록에 있어요")
        else:
            self.queue.append((t, u))
            self.save()
            self.resolver.want([u], self.quality)
            self.status.set(f"내 목록에 담음 · {t[:24]}")
        if self.view == "rec":      # 추천에서는 담으면 추천 목록에서 사라진다
            self.hide_rec(u)
            self.lb.set_selection([])
            self.render()

    def remove_one(self, i):
        if self.view == "queue" and 0 <= i < len(self.queue):
            self.hide_rec(self.queue[i][1])     # 지운 영상은 추천/구독에도 다시 안 뜨게
            item = self.queue[i]
            del self.queue[i]
            self.lb.set_selection([])
            self.render()
            self.mark_removed([item])

    def clear(self):
        if self.queue and ask_dialog(self, "비우기", "내 목록을 모두 지울까요?"):
            self.queue.clear()
            self.lb.set_selection([])
            self.render()

    # ---------------- 재생 (mpv 를 대기 상태로 켜 두고 IPC 로 명령) ----------------
    def mpv_alive(self):
        return self.proc is not None and self.proc.poll() is None

    def ensure_mpv(self):
        """mpv 를 '대기 상태'로 미리 켜 둔다. 재생할 때는 명령만 보내서 바로 시작된다."""
        if self.mpv_alive() and mpv_ipc(["get_property", "idle-active"]) is not None:
            return True
        if not os.path.exists(MPV):
            self.after(0, lambda: ask_dialog(self, "mpv 없음", f"mpv를 찾을 수 없어요:\n{MPV}", "확인", None))
            return False
        try:
            os.makedirs(os.path.dirname(INPUT_CONF), exist_ok=True)
            with open(INPUT_CONF, "w", encoding="utf-8") as f:
                # q / 창 닫기는 '종료'가 아니라 '정지'로 (mpv 를 계속 대기시켜 다음 재생을 빠르게)
                f.write("q stop\nQ stop\nCLOSE_WIN stop\n")
        except OSError:
            pass
        extra = []
        if os.path.isdir(PROFILE):
            # 나중에 볼 영상 등 비공개 영상도 재생되도록 같은 로그인 사용
            extra.append(f"--ytdl-raw-options=cookies-from-browser=edge:{PROFILE}")
        try:                           # 화면 배율에 맞춘 최소 창 크기를 mpv 의 Lua 에 알려준다
            dpi = ctypes.windll.user32.GetDpiForSystem() / 96
        except Exception:
            dpi = 1.0
        extra.append(f"--script-opts=yt-pip-min_w={round(resize_grips.MIN_W * dpi)},"
                     f"yt-pip-min_h={round(resize_grips.MIN_H * dpi)}")
        self.proc = subprocess.Popen(
            [MPV, *MPV_ARGS, f"--input-conf={INPUT_CONF}",
             f"--ytdl-format={ytdl_format(self.quality)}", *extra])
        for _ in range(30):
            time.sleep(0.2)
            if mpv_ipc(["get_property", "idle-active"]) is not None:
                return True
        return False

    def _warm_mpv(self):
        with self.mpv_lock:
            self.ensure_mpv()

    def prewarm(self):
        """보이는 목록의 앞쪽 영상 주소를 미리 가져와 둔다"""
        if self.view == "pls" and not self.nav_stack:
            return
        n = 30 if self.view == "queue" else 4      # 내 목록은 담아 둔 영상을 전부 미리 준비
        self.resolver.want([u for _, u in self.shown[:n]], self.quality)

    def on_select(self, i):
        if self.view == "pls" and not self.nav_stack:
            return
        self.resolver.want([u for _, u in self.shown[i:i + 2]], self.quality, front=True)

    def play(self, start=0, seek=None, items=None):
        explicit = items is not None
        items = items if explicit else self.shown
        if not explicit and self.view == "pls" and not self.nav_stack:
            self.status.set("재생목록은 더블클릭해서 안으로 들어가세요")
            return
        if not items:
            self.status.set("재생할 영상이 없어요")
            return
        self.status.set("여는 중...")
        threading.Thread(target=self._play_worker, args=(list(items), start, seek),
                         daemon=True).start()

    def _play_worker(self, items, start, seek):
        if self.login_opened:
            close_profile_edge()
            self.login_opened = False
        with self.mpv_lock:
            ok = self.ensure_mpv()
            # 영상을 여러 개 틀면 mpv 램이 안 줄어들어서, 너무 커졌으면 새로 켜고 시작
            mb = proc_mem_mb(self.proc.pid) if ok and self.mpv_alive() else None
            if mb and mb > 320:
                mpv_ipc(["quit"])
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    pass
                ok = self.ensure_mpv()
        if not ok:
            self.after(0, lambda: self.status.set("mpv 를 켜지 못했어요"))
            return
        self._load_items(items, start, seek)

    def _load_items(self, items, start, seek=None):
        """mpv 가 켜진 채로 재생목록을 통째로 교체 (같은 창에서 이어서)"""
        q = self.quality

        def entry(i):
            title, url = items[i]
            opts = {"force-media-title": title}
            target = url
            urls = self.resolver.get(url, q)
            if urls:                       # 미리 가져온 주소가 있으면 바로 재생
                # mpv 의 yt-dlp 연동이 쓰는 것과 같은 설정. 이게 없으면 위치 이동(seek) 때 재생이 멈춘다
                opts["stream-lavf-o"] = "request_size=10485760"
                opts["user-agent"] = STREAM_UA
                target = urls[0]
                if len(urls) > 1:
                    opts["audio-file"] = urls[1]
            return target, opts, urls is not None

        t0, o0, fast = entry(start)
        # 주의: 외부 오디오(audio-file)와 start 옵션을 같이 주면 재생이 멈추므로, 위치 이동은 로드 후에 seek 로 한다
        cmds = [["set_property", "ytdl-format", ytdl_format(q)],
                ["loadfile", t0, "replace", -1, o0]]
        for i in range(start + 1, len(items)):
            t, o, _ = entry(i)
            cmds.append(["loadfile", t, "append", -1, o])
        for j in range(start):
            t, o, _ = entry(j)
            cmds.append(["loadfile", t, "insert-at", j, o])
        win = load_settings().get("mpv_win")
        if win and mpv_ipc(["get_property", "idle-active"]) is True:    # 창이 새로 열릴 때만 저장된 자리 적용
            x, y, w, h = win
            gm = ctypes.windll.user32.GetSystemMetrics      # 모니터 구성이 바뀌어 화면 밖이면 안으로 당김
            vx, vy, vw, vh = gm(76), gm(77), gm(78), gm(79)
            x = max(vx, min(x, vx + vw - 120))
            y = max(vy, min(y, vy + vh - 120))
            mpv_ipc_many([["set_property", "geometry", f"+{x}+{y}"],
                          ["set_property", "autofit", f"{w}x{h}"]])
        res = mpv_ipc_many(cmds)
        self.playing_items = items
        if res[1] is None:
            msg = "재생을 시작하지 못했어요 (mpv 연결 실패)"
        elif fast:
            msg = f"재생 중 · {q}p"
        else:
            msg = f"재생 중 · {q}p (영상 주소 가져오는 중... 몇 초 걸려요)"
        self.after(0, lambda: self.status.set(msg))
        self.resolver.want([items[i][1] for i in range(start + 1, min(start + 4, len(items)))], q)
        if seek and res[1] is not None:
            for _ in range(80):          # 새 영상이 열릴 때까지 기다렸다가 보던 위치로 이동
                time.sleep(0.4)
                if mpv_ipc(["get_property", "time-pos"]) is not None:
                    mpv_ipc(["seek", float(seek), "absolute+exact"])
                    break

    def set_quality(self, key):
        self.quality = key
        self.qseg.set(key)
        save_settings({**load_settings(), "quality": key})
        threading.Thread(target=self._quality_worker, args=(key,), daemon=True).start()
        self.prewarm()

    def _quality_worker(self, key):
        """재생 중이면 같은 mpv 창에서, 보던 위치 그대로 새 화질로 바꾼다.
        새 화질 주소를 가져오는 동안에는 기존 영상이 계속 재생된다."""
        if mpv_ipc(["get_property", "idle-active"]) is not False:
            self.after(0, lambda: self.status.set(f"화질 {key}p (다음 재생부터 적용)"))
            return
        pos = mpv_ipc(["get_property", "playlist-pos"])
        items = self.playing_items
        if pos is None or not (0 <= pos < len(items)):
            return
        self.after(0, lambda: self.status.set(f"{key}p 로 바꾸는 중... (기존 영상은 계속 재생)"))
        self.resolver.resolve_now(items[pos][1], key)
        t = mpv_ipc(["get_property", "time-pos"])
        self._load_items(items, int(pos), t)

    def update_mem(self):
        try:
            app_mb = proc_mem_mb()
            mpv_mb = proc_mem_mb(self.proc.pid) if self.mpv_alive() else None
            self.gauge.set(app_mb, mpv_mb)
            self.remember_mpv_window()
            # 영상을 끄고 한참 지나도 mpv 가 램을 쥐고 있으면 껐다가 대기 상태로 다시 켜서 램을 돌려준다
            if mpv_mb and mpv_mb > 70 and not self.mpv_lock.locked():
                self._idle_ticks = getattr(self, "_idle_ticks", 0) + 1
                if self._idle_ticks >= 3:
                    self._idle_ticks = 0
                    threading.Thread(target=self._recycle_mpv, daemon=True).start()
            else:
                self._idle_ticks = 0
        finally:
            self.after(2000, self.update_mem)

    def remember_mpv_window(self):
        """사용자가 mpv 창을 옮기거나 크기를 바꾸면 기억해 두었다가 다음 영상 때 그 자리에서 연다"""
        if not self.mpv_alive():
            return
        r = mpv_window_rect(self.proc.pid)
        if not r or r[2] < 200 or r[3] < 120:
            return
        old = getattr(self, "_win_saved", None)
        if old and all(abs(a - b) <= 2 for a, b in zip(old, r)):
            return
        self._win_saved = r
        st = load_settings()
        st["mpv_win"] = list(r)
        save_settings(st)

    def _recycle_mpv(self):
        with self.mpv_lock:
            if mpv_ipc(["get_property", "idle-active"]) is not True:
                return          # 재생 중이면 건드리지 않음
            mpv_ipc(["quit"])
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
            self.ensure_mpv()

    def play_all(self):
        self.play(0)

    def play_from_selected(self):
        sel = self.lb.curselection()
        self.play(sel[0] if sel else 0)

    def stop(self):
        threading.Thread(target=lambda: mpv_ipc(["stop"]), daemon=True).start()
        self.status.set("정지")

    def on_close(self):
        try:
            self.grips.close()
            mpv_ipc(["quit"])
            if self.mpv_alive():
                self.proc.terminate()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
