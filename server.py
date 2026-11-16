"""
hwp-mcp — 한글(HWP/HWPX) MCP server for Claude Code and claude.ai (v2.1)

Lets Claude find, read, preview, edit and create Hancom Hangul documents on a Windows PC.
  * read     : own stdlib parser (hwp_fast) — no Hangul needed, ~10 ms
  * edit     : .hwpx edited directly as XML (hwpx_edit) — no Hangul needed, ~50 ms
               .hwp edited through the installed Hangul (kept warm in the background, ~0.3 s)
  * preview  : page 1 instantly from the picture stored in the file; any page exactly via Hangul (~1 s)
Originals are never overwritten: edits go to a new '<name>_수정본' file; only files this server
wrote itself (and nobody changed since) are edited in place.

Run:
  python server.py                 stdio (Claude Code / Claude Desktop)
  pythonw server.py --http         127.0.0.1:8772/mcp, stateless (expose through a tunnel for claude.ai)
Settings (environment or .env next to this file, see .env.example):
  HWP_MCP_ROOTS, HWP_MCP_PUBLIC_HOSTS, HWP_MCP_READONLY, HWP_MCP_REMOTE_OPEN, HWP_MCP_NEW_DIR,
  HWP_MCP_KEEP_WARM_MIN, HWP_MCP_PREWARM, HWP_MCP_PORT
"""

from __future__ import annotations

import os
import sys

BASE_DIR_STR = os.path.dirname(os.path.abspath(__file__))

if sys.stderr is None:  # pythonw: no console → log file. Must happen BEFORE other imports.
    _logp = os.path.join(BASE_DIR_STR, "server.log")
    try:
        if os.path.getsize(_logp) > 5_000_000:
            os.replace(_logp, _logp + ".old")
    except OSError:
        pass
    _log = open(_logp, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log
    import faulthandler as _fh
    _fh.enable(_log)

import hashlib  # noqa: E402
import html  # noqa: E402
import itertools  # noqa: E402
import queue  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from mcp.server.fastmcp import FastMCP, Image  # noqa: E402

BASE_DIR = Path(BASE_DIR_STR)
sys.path.insert(0, BASE_DIR_STR)
import hwp_fast  # noqa: E402
import hwpx_edit  # noqa: E402
# Import on the main thread at startup: a first import of numpy (pulled in by pymupdf)
# inside a worker thread hangs in stdio mode.
import pymupdf  # noqa: E402

CACHE_DIR = BASE_DIR / "cache"
EXTS = (".hwp", ".hwpx")
FMT = {".hwpx": "HWPX", ".hwp": "HWP", ".pdf": "PDF"}
MAX_FILE = 200 * 1024 * 1024  # refuse to load bigger documents


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _load_env():
    f = BASE_DIR / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _flag(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


_load_env()
READONLY = _flag("HWP_MCP_READONLY")
REMOTE_OPEN = _flag("HWP_MCP_REMOTE_OPEN")
PREWARM = _flag("HWP_MCP_PREWARM", True)
KEEP_WARM = max(1, _int("HWP_MCP_KEEP_WARM_MIN", 30)) * 60


def _known_folder(guid: str) -> Path | None:
    """Real Desktop/Documents/Downloads path (also when moved to OneDrive)."""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]
        u = uuid.UUID(guid)
        g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) != 0:
            return None
        out = Path(p.value)
        ctypes.windll.ole32.CoTaskMemFree(p)
        return out
    except Exception:
        return None


FOLDER_IDS = {"Downloads": "374DE290-123F-4565-9164-39C4925E467B",
              "Desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
              "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7"}
KNOWN = {k: (_known_folder(v) or Path.home() / k) for k, v in FOLDER_IDS.items()}
ALIAS = {"다운로드": "Downloads", "바탕화면": "Desktop", "바탕 화면": "Desktop", "문서": "Documents", "내 문서": "Documents"}


def _roots() -> list[Path]:
    cands = list(KNOWN.values())
    cands += [Path(p) for p in os.getenv("HWP_MCP_ROOTS", "").split(";") if p.strip()]
    out = []
    for p in cands:
        try:
            r = p.resolve()
        except OSError:
            continue
        if r.is_dir() and r not in out:
            out.append(r)
    return out


ROOTS = _roots()
NEW_DIR = Path(os.getenv("HWP_MCP_NEW_DIR") or (KNOWN["Documents"] / "Claude 한글문서"))

mcp = FastMCP("hwp")


# ---------------------------------------------------------------------------
# Paths: only .hwp/.hwpx inside the allowed folders, never network/device paths
# ---------------------------------------------------------------------------
def _allowed(p: Path) -> bool:
    return any(p == r or p.is_relative_to(r) for r in ROOTS)


def _safe_text(raw: str) -> str:
    s = str(raw).strip().strip('"').strip("'")
    if any(ord(c) < 32 for c in s):
        raise ValueError("경로에 제어 문자가 있습니다")
    if s.startswith(("\\\\", "//", "\\??\\")):
        # \\server\share would make Windows contact that host (and could leak the login hash)
        raise ValueError("네트워크·장치 경로는 쓸 수 없습니다")
    if ":" in s[2:]:
        raise ValueError("경로 형식이 올바르지 않습니다")
    return s


def _expand_alias(raw: str) -> str:
    raw = _safe_text(raw)
    for k, v in list(ALIAS.items()) + [(v, v) for v in KNOWN]:
        if raw == k or raw.startswith(k + "/") or raw.startswith(k + "\\"):
            return str(KNOWN[v]) + raw[len(k):]
    return raw


def _resolve(path: str, must_exist: bool = True, base: Path | None = None) -> Path:
    """Absolute path, 'Downloads/x.hwp', '다운로드/x.hwp', or just a file name."""
    p = Path(_expand_alias(path))
    if p.suffix.lower() not in EXTS:
        # security: never hand anything else to os.startfile / Hangul
        raise ValueError(f"한글 문서(.hwp/.hwpx)만 다룰 수 있습니다: {path}")
    if p.is_absolute():
        cands = [p]
    else:
        cands = ([base / p] if base else []) + [r / p for r in ROOTS]
    for c in cands:
        try:
            c = c.resolve()
        except OSError:
            continue
        if c.suffix.lower() not in EXTS or not _allowed(c):
            continue
        if not must_exist or c.is_file():
            return c
    if must_exist and len(p.parts) == 1:
        hits = _search(p.name, limit=1, exact=True)  # _search only returns allowed files
        if hits:
            return hits[0]
    if must_exist:
        raise ValueError(f"파일을 찾지 못했거나 허용된 폴더 밖입니다: {path}\n허용 폴더: "
                         + ", ".join(map(str, ROOTS)) + "\n먼저 hwp_find 로 찾아보세요.")
    raise ValueError(f"허용된 폴더 밖에는 저장할 수 없습니다: {path}")


def _resolve_dir(folder: str) -> Path:
    p = Path(_expand_alias(folder))
    cands = [p] if p.is_absolute() else [r / p for r in ROOTS]
    for c in cands:
        try:
            c = c.resolve()
        except OSError:
            continue
        if c.is_dir() and _allowed(c):
            return c
    raise ValueError(f"허용된 폴더가 아니거나 없는 폴더입니다: {folder}\n허용 폴더: " + ", ".join(map(str, ROOTS)))


SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "__pycache__", "AppData", "site-packages"}
_isjunction = getattr(os.path, "isjunction", lambda _p: False)


def _search(query: str, limit: int = 20, exact: bool = False, roots: list[Path] | None = None) -> list[Path]:
    q = query.lower().strip()
    words = q.split()
    hits = {}
    for root in roots or ROOTS:
        for dirpath, dirnames, filenames in os.walk(root):
            # don't follow links/junctions out of the allowed folders
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                           and not os.path.islink(os.path.join(dirpath, d))
                           and not _isjunction(os.path.join(dirpath, d))]
            for f in filenames:
                fl = f.lower()
                if fl.endswith(EXTS) and not fl.startswith("~$") and \
                        ((exact and fl == q) or (not exact and all(w in fl for w in words))):
                    try:
                        full = (Path(dirpath) / f).resolve()
                    except OSError:
                        continue
                    if _allowed(full):
                        hits[str(full).lower()] = full
    files = []
    for h in hits.values():
        try:
            files.append((h.stat().st_mtime, h))
        except OSError:
            pass
    return [h for _, h in sorted(files, reverse=True)[:limit]]


# ---------------------------------------------------------------------------
# Registry of files this server wrote (shared by the stdio and HTTP servers via a file).
# Used to (1) only ever edit our own outputs in place and (2) know when the page picture
# stored inside a .hwpx is outdated because we edited the XML without Hangul.
# ---------------------------------------------------------------------------
OUTREG = CACHE_DIR / "outputs.tsv"


def _reg_load() -> dict[str, tuple[int, bool]]:
    out = {}
    try:
        for line in OUTREG.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                out[parts[0]] = (int(parts[1]), parts[2] == "1")
    except (OSError, ValueError):
        pass
    return out


def _reg_mark(path: Path, direct: bool):
    CACHE_DIR.mkdir(exist_ok=True)
    try:
        line = f"{str(path).lower()}\t{path.stat().st_mtime_ns}\t{int(direct)}\n"
        with open(OUTREG, "a", encoding="utf-8") as f:
            f.write(line)
        if OUTREG.stat().st_size > 400_000:  # compact
            reg = _reg_load()
            OUTREG.write_text("".join(f"{k}\t{m}\t{int(d)}\n" for k, (m, d) in list(reg.items())[-2000:]), encoding="utf-8")
    except OSError:
        pass


def _reg_get(path: Path):
    try:
        mt = path.stat().st_mtime_ns
    except OSError:
        return None
    e = _reg_load().get(str(path).lower())
    return e if e and e[0] == mt else None  # changed since we wrote it → not ours any more


def _is_ours(path: Path) -> bool:
    return _reg_get(path) is not None


def _stale(path: Path) -> bool:
    e = _reg_get(path)
    return bool(e and e[1])


def _out_path(src: Path, out: str | None) -> Path:
    """Where an edit is saved. Never overwrites an existing file unless this server wrote it
    (and nobody changed it since)."""
    if out:
        o = Path(_safe_text(out))
        if not o.suffix:
            o = o.with_suffix(src.suffix)
        o = _resolve(str(o), must_exist=False, base=src.parent)
        if o.suffix.lower() != src.suffix.lower():
            raise ValueError(f"저장 형식은 원본과 같아야 합니다 ({src.suffix})")
        if o.exists() and not _is_ours(o):
            raise ValueError(f"이미 있는 파일은 덮어쓰지 않습니다: {o.name} (다른 이름을 주세요)")
        return o
    if _is_ours(src):
        return src  # our own earlier result → keep editing it
    o, k = src.with_name(src.stem + "_수정본" + src.suffix), 2
    while o.exists():
        o, k = src.with_name(f"{src.stem}_수정본{k}{src.suffix}"), k + 1
    return o


def _check_size(p: Path):
    if p.stat().st_size > MAX_FILE:
        raise ValueError(f"파일이 너무 큽니다 ({p.stat().st_size // 1_000_000} MB)")


# ---------------------------------------------------------------------------
# Hangul worker: one hidden Hangul, used only from its own thread (COM rule).
# Kept warm for KEEP_WARM seconds; user jobs run before background jobs.
# ---------------------------------------------------------------------------
_HANGUL = None


def hangul_installed() -> bool:
    global _HANGUL
    if _HANGUL is None:
        try:
            import winreg
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "HWPFrame.HwpObject"))
            _HANGUL = True
        except OSError:
            _HANGUL = False
    return _HANGUL


class HwpWorker:
    PIDFILE = CACHE_DIR / "hwp_pids.txt"  # "<hwp pid> <ctime> <server pid> <ctime>" per hidden Hangul we started

    def __init__(self):
        self.q: queue.PriorityQueue = queue.PriorityQueue()
        self.seq = itertools.count()
        self.hwp = None
        self.my_pids: list[str] = []
        self.last = time.time()
        self.closed = False
        self._reap_stale()
        threading.Thread(target=self._loop, daemon=True, name="hwp-com").start()

    @staticmethod
    def _alive(pid, ct) -> bool:
        import psutil
        try:
            return abs(psutil.Process(int(pid)).create_time() - float(ct)) < 1
        except (psutil.Error, ValueError):
            return False

    @classmethod
    def _reap_stale(cls):
        """Close hidden Hangul processes left by a server that was killed. Never touches a Hangul the
        user opened or one that belongs to another live server."""
        import psutil
        if not cls.PIDFILE.exists():
            return
        keep = []
        for line in cls.PIDFILE.read_text(encoding="ascii", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) != 4 or not cls._alive(parts[0], parts[1]):
                continue
            if cls._alive(parts[2], parts[3]):
                keep.append(line)
                continue
            try:
                psutil.Process(int(parts[0])).kill()
            except psutil.Error:
                pass
        cls.PIDFILE.write_text("".join(x + "\n" for x in keep), encoding="ascii")

    def _start(self):
        import psutil
        import win32com.client as win32
        import win32process
        if not hangul_installed():
            raise RuntimeError("이 PC에 한글(한컴오피스)이 설치되어 있지 않습니다. 읽기와 .hwpx 편집만 가능합니다.")
        hwp = win32.gencache.EnsureDispatch("HWPFrame.HwpObject")
        # exact process id of this instance, from its main window
        _, pid = win32process.GetWindowThreadProcessId(hwp.XHwpWindows.Item(0).WindowHandle)
        me = psutil.Process()
        line = f"{pid} {psutil.Process(pid).create_time()} {me.pid} {me.create_time()}"
        self.my_pids.append(line)
        CACHE_DIR.mkdir(exist_ok=True)
        with open(self.PIDFILE, "a", encoding="ascii") as f:
            f.write(line + "\n")
        self.hwp = hwp
        if not hwp.RegisterModule("FilePathCheckDLL", "FilePathCheckerModule"):
            self._quit()
            raise RuntimeError("한글 자동화 보안 모듈이 등록되어 있지 않습니다 (setup.ps1 을 실행하세요). "
                               "등록 없이는 한글이 '접근 허용' 창을 띄우고 멈춥니다.")
        hwp.XHwpWindows.Item(0).Visible = False
        # Official flags (HwpAutomation doc, GetMessageBoxMode): auto-answer message boxes
        # OK / OK-of-OKCANCEL / YES-of-YESNOCANCEL / YES-of-YESNO
        hwp.SetMessageBoxMode(0x00000001 | 0x00000010 | 0x00001000 | 0x00010000)
        return hwp

    def _quit(self):
        import psutil
        if self.hwp is not None:
            try:
                self.hwp.Clear(1)
                self.hwp.Quit()
            except Exception:
                pass
            self.hwp = None
        for line in self.my_pids:  # make sure it is really gone (COM may have failed but the process lives)
            pid, ct = line.split()[:2]
            for _ in range(20):
                if not self._alive(pid, ct):
                    break
                time.sleep(0.1)
            else:
                try:
                    psutil.Process(int(pid)).kill()
                except psutil.Error:
                    pass
        if self.my_pids and self.PIDFILE.exists():
            rest = [x for x in self.PIDFILE.read_text(encoding="ascii").splitlines() if x not in self.my_pids]
            self.PIDFILE.write_text("".join(x + "\n" for x in rest), encoding="ascii")
        self.my_pids = []

    def kill_hwp(self):
        """Called from another thread when a job hangs (e.g. a dialog): kill only our Hangul;
        the blocked COM call then fails and the worker thread recovers."""
        import psutil
        for line in list(self.my_pids):
            pid, ct = line.split()[:2]
            if self._alive(pid, ct):
                try:
                    psutil.Process(int(pid)).kill()
                except psutil.Error:
                    pass

    def _quit_job(self, _hwp):
        self._quit()

    def _loop(self):
        import pythoncom
        pythoncom.CoInitialize()
        while True:
            try:
                _, _, fn, box, done = self.q.get(timeout=30)
            except queue.Empty:
                if self.hwp is not None:
                    keep = min(KEEP_WARM, 120) if self.others_warm() else KEEP_WARM
                    if time.time() - self.last > keep:
                        self._quit()
                continue
            if fn == self._quit_job:  # never start Hangul just to close it
                self._quit()
                done.set()
                continue
            if self.closed or box.get("cancelled"):
                box["error"] = RuntimeError("작업이 취소되었습니다")
                done.set()
                continue
            box["started"].set()
            for attempt in (1, 2):
                try:
                    if self.hwp is None:
                        self._start()
                    box["result"] = fn(self.hwp)
                    box.pop("error", None)
                    break
                except Exception as e:
                    box["error"] = e
                    msg = str(e)
                    dead = any(k in msg for k in ("RPC", "-2147023174", "-2147417848", "-2147023170", "-2147220995"))
                    if dead:  # Hangul died/was killed → clean up, restart once and retry
                        self.hwp = None
                        self._quit()
                        if box.get("cancelled"):
                            break
                        continue
                    break  # normal error (e.g. text not found): keep Hangul running
            try:
                if self.hwp is not None:
                    self.hwp.Clear(1)
            except Exception:
                self.hwp = None
                self._quit()
            self.last = time.time()
            done.set()

    def submit(self, fn, prio=5):
        box, done = {"started": threading.Event()}, threading.Event()
        self.q.put((prio, next(self.seq), fn, box, done))
        return box, done

    def run(self, fn, timeout=120, prio=0, queue_wait=300):
        box, done = self.submit(fn, prio)
        if not box["started"].wait(queue_wait):  # still queued behind other work
            box["cancelled"] = True
            raise TimeoutError("한글이 다른 작업으로 바빠서 시작하지 못했습니다. 잠시 뒤 다시 시도해 주세요.")
        if not done.wait(timeout):  # measured from when the job started
            box["cancelled"] = True
            self.kill_hwp()
            done.wait(20)
            raise TimeoutError("한글 프로그램이 응답하지 않아 다시 시작했습니다. 한 번 더 시도해 주세요.")
        if "error" in box:
            raise box["error"]
        return box["result"]

    @classmethod
    def others_warm(cls) -> bool:
        """True if another live hwp-mcp server (another Claude window, or the claude.ai server) already
        keeps a hidden Hangul. Then this one doesn't pre-start its own and closes any it starts quickly,
        so there is about one warm Hangul per PC instead of one per window."""
        me = str(os.getpid())
        try:
            lines = cls.PIDFILE.read_text(encoding="ascii", errors="ignore").splitlines()
        except OSError:
            return False
        for line in lines:
            p = line.split()
            if len(p) == 4 and p[2] != me and cls._alive(p[0], p[1]) and cls._alive(p[2], p[3]):
                return True
        return False

    def warm(self):
        if PREWARM and self.hwp is None and hangul_installed() and not self.others_warm():
            self.submit(lambda h: None, prio=9)

    def shutdown(self):
        """At exit: close our hidden Hangul and drop queued background jobs (they would start a new one)."""
        self.closed = True
        if self.hwp is None and not self.my_pids:
            return
        done = threading.Event()
        self.q.put((-1, next(self.seq), self._quit_job, {"started": threading.Event()}, done))
        done.wait(15)


_worker: HwpWorker | None = None
_wlock = threading.Lock()


def worker() -> HwpWorker:
    global _worker
    with _wlock:
        if _worker is None:
            import atexit
            _worker = HwpWorker()
            atexit.register(_worker.shutdown)
    return _worker


def _open(hwp, path: Path):
    if not hwp.Open(str(path), FMT[path.suffix.lower()], "forceopen:true;versionwarning:false;suspendpassword:true"):
        raise RuntimeError(f"한글에서 열지 못했습니다: {path.name}")


def _save(hwp, out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    if not hwp.SaveAs(str(out), FMT[out.suffix.lower()], ""):
        raise RuntimeError(f"저장 실패: {out}")


def _insert_text(hwp, text: str):
    lines = text.replace("\r\n", "\n").split("\n")
    for i, ln in enumerate(lines):
        if ln:
            a = hwp.HParameterSet.HInsertText
            hwp.HAction.GetDefault("InsertText", a.HSet)
            a.Text = ln
            hwp.HAction.Execute("InsertText", a.HSet)
        if i < len(lines) - 1:
            hwp.HAction.Run("BreakPara")


def _find(hwp, text: str) -> bool:
    a = hwp.HParameterSet.HFindReplace
    hwp.HAction.GetDefault("RepeatFind", a.HSet)
    a.FindString = text
    a.IgnoreMessage = 1
    a.Direction = hwp.FindDir("Forward")
    a.FindType = 1
    return bool(hwp.HAction.Execute("RepeatFind", a.HSet))


# ---------------------------------------------------------------------------
# Caches: parsed documents, rendered PDFs
# ---------------------------------------------------------------------------
_doc_cache: dict[str, dict] = {}


def _key(path: Path) -> str:
    st = path.stat()
    return f"{path}|{st.st_mtime_ns}|{st.st_size}"


def _fast(path: Path) -> dict:
    k = _key(path)
    if k not in _doc_cache:
        _check_size(path)
        if len(_doc_cache) > 64:
            _doc_cache.clear()
        _doc_cache[k] = hwp_fast.parse(path, headers=True)
    return _doc_cache[k]


def _pdf_path(path: Path) -> Path:
    return CACHE_DIR / (hashlib.sha1(_key(path).encode()).hexdigest()[:16] + ".pdf")


def _render_job(path: Path, pdf: Path):
    def job(hwp):
        if pdf.exists():
            return
        tmp = pdf.with_suffix(".tmp.pdf")
        _open(hwp, path)
        _save(hwp, tmp)
        os.replace(tmp, pdf)
    return job


def _prune_cache():
    pdfs = sorted(CACHE_DIR.glob("*.pdf"), key=lambda x: x.stat().st_mtime)
    for old in pdfs[:-60]:
        old.unlink(missing_ok=True)


def _pdf_for(path: Path) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    pdf = _pdf_path(path)
    if not pdf.exists():
        _check_size(path)
        worker().run(_render_job(path, pdf))
        _prune_cache()
    return pdf


def _prerender(path: Path):
    """Background: render the PDF so a later preview is instant (low priority)."""
    if PREWARM and hangul_installed() and (worker().hwp is not None or not HwpWorker.others_warm()):
        CACHE_DIR.mkdir(exist_ok=True)
        pdf = _pdf_path(path)
        if not pdf.exists() and path.stat().st_size <= MAX_FILE:
            worker().submit(_render_job(path, pdf), prio=7)


def _embedded_preview(path: Path) -> bytes | None:
    """The page-1 picture Hangul stores inside the file (PrvImage), as PNG."""
    try:
        data = path.read_bytes()
        if data[:4] == b"PK\x03\x04":
            import io
            import zipfile
            z = zipfile.ZipFile(io.BytesIO(data))
            info = next((i for i in z.infolist() if i.filename.lower().startswith("preview/prvimage")), None)
            img = z.read(info) if info and info.file_size < 20_000_000 else None
        else:
            cfb = hwp_fast.CFB(data)
            img = cfb.read("PrvImage") if cfb.exists("PrvImage") else None
        if not img or len(img) < 100 or len(img) > 20_000_000:
            return None
        return pymupdf.Pixmap(img).tobytes("png")
    except Exception:
        return None


def _parse_pages(spec: str, n: int) -> list[int]:
    pages = []
    try:
        for part in (spec or "1").replace(" ", "").split(","):
            if not part:
                continue
            if part.lower() in ("all", "전체"):
                return list(range(1, n + 1))
            if "-" in part:
                a, b = part.split("-", 1)
                a, b = max(1, int(a or 1)), min(n, int(b or n))
                pages += list(range(a, b + 1))
            else:
                pages.append(int(part))
    except ValueError:
        raise ValueError("pages 는 '1', '1-3', '2,5', 'all' 같은 형식이어야 합니다")
    return [p for p in dict.fromkeys(pages) if 1 <= p <= n]


def _after_edit(out: Path, direct: bool):
    _doc_cache.clear()
    _reg_mark(out, direct)
    _prerender(out)


# ---------------------------------------------------------------------------
# Tools: read
# ---------------------------------------------------------------------------
@mcp.tool()
def hwp_find(query: str = "", folder: str = "", limit: int = 20) -> str:
    """Find Hangul documents (.hwp/.hwpx) on this PC by file name, newest first.

    query: words that appear in the file name (space-separated; empty = all recent documents)
    folder: limit to '다운로드'/'Downloads', '바탕화면'/'Desktop', '문서'/'Documents' or a path (optional)
    """
    worker().warm()  # a read/edit/preview usually follows → start Hangul in the background
    limit = max(1, min(limit, 100))
    roots = [_resolve_dir(folder)] if folder else None
    hits = _search(query, limit=limit, roots=roots)
    if not hits:
        return f"'{query}' 이(가) 들어간 한글 문서를 찾지 못했습니다. 찾는 폴더: " + ", ".join(map(str, roots or ROOTS))
    lines = [f"{len(hits)}개 (최근 수정 순):"]
    for h in hits:
        st = h.stat()
        lines.append(f"- {h}  ({max(1, st.st_size // 1024)} KB, {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))})")
    return "\n".join(lines)


@mcp.tool()
def hwp_read(path: str, format: str = "md", offset: int = 0, max_chars: int = 60000) -> str:
    """Read a Hangul document as Markdown (default) or plain text — tables as tables, plus numbering,
    bullets, footnotes and equations. Parses the file directly (no Hangul needed): usually ~10 ms,
    also for distribution-protected (배포용) .hwp.

    path: full path, 'Downloads/file.hwp', '다운로드/파일.hwp', or just the file name
    format: 'md' | 'txt'
    offset/max_chars: for long documents (the result says which offset to use next)
    """
    p = _resolve(path)
    offset, max_chars = max(0, offset), max(1000, min(max_chars, 200_000))
    t0 = time.perf_counter()
    note = ""
    try:
        doc = _fast(p)
        body = hwp_fast.to_text(doc) if format == "txt" else hwp_fast.to_markdown(doc)
        meta = doc["meta"]
        how = f"{meta['format'].upper()}{' 배포용' if meta['distribution'] else ''}, 직접 해석"
    except hwp_fast.HwpError as e:  # encrypted HWPX, HWP 3.x … → let Hangul extract the text
        if not hangul_installed():
            raise
        note = f"(직접 해석 불가: {e} → 한글 프로그램으로 추출)\n"

        def job(hwp):
            _open(hwp, p)
            return hwp.GetTextFile("TEXT", "") or ""
        body = html.unescape(worker().run(job))
        if not body.strip():
            raise ValueError(f"{p.name}: 배포용(열람 제한) 문서라 한글도 글자 추출을 막습니다. "
                             "hwp_preview 로 1쪽 그림을 보거나 PC의 한글에서 직접 여세요.")
        how = "한글 프로그램"
    dt = (time.perf_counter() - t0) * 1000
    worker().warm()
    _prerender(p)
    total = len(body)
    chunk = body[offset:offset + max_chars]
    head = f"# {p.name}\n({how}, {dt:.0f} ms, {total:,}자)\n{note}\n"
    tail = ""
    if offset + max_chars < total:
        tail = f"\n\n…(계속: offset={offset + max_chars} 로 다시 호출, 남은 {total - offset - max_chars:,}자)"
    return head + chunk + tail


@mcp.tool()
def hwp_preview(path: str, pages: str = "1", width: int = 1000, exact: bool = False) -> list:
    """Show pages as images exactly as Hangul draws them.
    Page 1 comes back instantly from the picture stored in the file (when it is up to date);
    other pages, or exact=True, are rendered by the installed Hangul (~1 s, cached afterwards).

    pages: '1', '1-3', '2,5', 'all' (max 6 per call)
    width: image width in pixels (400–1600)
    """
    p = _resolve(path)
    t0 = time.perf_counter()
    pdf = _pdf_path(p)
    stale = _stale(p)
    if not pdf.exists() and not exact and (pages or "1").strip() == "1" and not stale:
        img = _embedded_preview(p)
        if img:
            worker().warm()
            _prerender(p)
            return [f"{p.name} — 1쪽 (파일에 저장된 미리보기, {(time.perf_counter() - t0) * 1000:.0f} ms. "
                    f"마지막으로 한글에서 저장한 모습입니다. 다른 쪽은 pages 로 요청)",
                    Image(data=img, format="png")]
    if not hangul_installed():
        return [f"{p.name}: 이 PC에 한글이 없어 쪽 그림을 만들 수 없습니다"
                + (" (이 파일은 고친 뒤라 저장된 미리보기가 옛 모습입니다)" if stale else "")]
    try:
        pdf = _pdf_for(p)
    except RuntimeError as e:  # e.g. distribution documents: Hangul refuses to export them
        img = None if stale else _embedded_preview(p)
        if img:
            return [f"{p.name}: 한글이 쪽 그림 만들기를 거부했습니다 (배포용 문서일 수 있음). "
                    "대신 파일에 저장된 1쪽 미리보기입니다.", Image(data=img, format="png")]
        raise RuntimeError(f"{p.name}: 쪽 그림을 만들 수 없습니다 ({e}). 배포용 문서라면 PC의 한글에서 직접 여세요.")
    doc = pymupdf.open(pdf)
    try:
        n = doc.page_count
        want = _parse_pages(pages, n)[:6]
        if not want:
            return [f"{p.name}: 쪽 번호가 범위를 벗어났습니다 (전체 {n}쪽)"]
        width = max(400, min(width, 1600))
        out: list = [f"{p.name} — 전체 {n}쪽 중 {', '.join(map(str, want))}쪽 ({time.perf_counter() - t0:.1f}초)"]
        for i in want:
            page = doc[i - 1]
            zoom = width / page.rect.width
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            out.append(Image(data=pix.tobytes("jpeg", jpg_quality=82), format="jpeg"))
        return out
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Tools: edit (registered only when not read-only)
# ---------------------------------------------------------------------------
def hwp_open_in_hangul(path: str) -> str:
    """Open the document in a normal Hangul window on this PC so the user can view and edit it directly.
    After the user saves (Ctrl+S), hwp_read shows the new content."""
    p = _resolve(path)
    if p.suffix.lower() not in EXTS or not p.is_file():
        raise ValueError("한글 문서만 열 수 있습니다")
    os.startfile(str(p))  # the user's own Hangul window, separate from the hidden automation one
    return f"PC 화면에 한글로 열었습니다: {p}\n고친 뒤 Ctrl+S 로 저장하면 hwp_read 로 다시 읽을 수 있어요."


def _direct(p: Path, out: Path, ops) -> dict | None:
    """.hwpx → edit without Hangul. Returns None when the change must go through Hangul."""
    if p.suffix.lower() != ".hwpx":
        return None
    _check_size(p)
    tmp = out.with_name(out.stem + ".~tmp" + out.suffix)
    try:
        rep = hwpx_edit.edit(p, tmp, ops)
    except Exception:  # unsupported structure etc. → Hangul does it
        tmp.unlink(missing_ok=True)
        return None
    os.replace(tmp, out)
    return rep


def hwp_replace_text(path: str, find: str, replace: str, out_path: str = "") -> str:
    """Find and replace text everywhere in the document, keeping formatting.
    The original stays untouched; the result is saved as a new '<name>_수정본' file (or out_path).
    .hwpx is edited directly (no Hangul, ~50 ms); .hwp goes through Hangul (~0.3 s when warm)."""
    if not find:
        raise ValueError("find 가 비어 있습니다")
    p = _resolve(path)
    t0 = time.perf_counter()
    try:
        before = hwp_fast.to_text(_fast(p)).count(find)
    except hwp_fast.HwpError:
        before = -1
    if before == 0:
        return f"'{find}' 을(를) 문서에서 찾지 못했습니다. 바꾼 것 없음."
    out = _out_path(p, out_path or None)
    rep = _direct(p, out, [("replace", find, replace)])
    if rep is not None:
        complete = before < 0 or rep["replaced"] >= before
        if complete or not hangul_installed():
            _after_edit(out, True)
            msg = f"'{find}' → '{replace}' {rep['replaced']}곳 바꿈 (한글 없이 직접, {(time.perf_counter() - t0) * 1000:.0f} ms)."
            if not complete:
                msg += f"\n주의: 문서에는 {before}곳이 있지만 표 칸·개체 경계에 걸친 곳은 바꾸지 못했습니다 (한글이 있으면 모두 바꿀 수 있음)."
            return msg + f"\n저장: {out}"

    def job(hwp):
        _open(hwp, p)
        a = hwp.HParameterSet.HFindReplace
        hwp.HAction.GetDefault("AllReplace", a.HSet)
        a.FindString = find
        a.ReplaceString = replace
        a.IgnoreMessage = 1
        a.Direction = hwp.FindDir("AllDoc")
        a.FindType = 1
        hwp.HAction.Execute("AllReplace", a.HSet)
        _save(hwp, out)
    worker().run(job)
    _after_edit(out, False)
    try:
        after = hwp_fast.to_text(_fast(out)).count(find) if find not in replace else 0
        n = f"{before - after}곳" if before >= 0 else "모두"
    except hwp_fast.HwpError:
        n = "모두"
    return f"'{find}' → '{replace}' {n} 바꿈 (한글 사용, {time.perf_counter() - t0:.1f}초).\n저장: {out}"


def hwp_insert_text(path: str, text: str, where: str = "end", out_path: str = "") -> str:
    """Insert paragraphs (one per line). The original stays untouched; saved as a new '_수정본' file.

    where: 'end' | 'start' | 'after:<text>' (right after the paragraph that contains <text>)
    """
    if where not in ("end", "start") and not (where.startswith("after:") and len(where) > 6):
        raise ValueError("where 는 'end', 'start', 'after:<찾을 글>' 중 하나여야 합니다")
    p = _resolve(path)
    out = _out_path(p, out_path or None)
    t0 = time.perf_counter()
    rep = _direct(p, out, [("insert", text, where)])
    if rep is not None:
        _after_edit(out, True)
        return f"문단을 넣었습니다 ({where}, 한글 없이 직접, {(time.perf_counter() - t0) * 1000:.0f} ms).\n저장: {out}"

    def job(hwp):
        _open(hwp, p)
        if where == "start":
            hwp.HAction.Run("MoveDocBegin")
            _insert_text(hwp, text)
            hwp.HAction.Run("BreakPara")
        elif where.startswith("after:"):
            hwp.HAction.Run("MoveDocBegin")
            if not _find(hwp, where[6:]):
                raise ValueError(f"'{where[6:]}' 을(를) 찾지 못했습니다")
            hwp.HAction.Run("Cancel")
            hwp.HAction.Run("MoveParaEnd")
            hwp.HAction.Run("BreakPara")
            _insert_text(hwp, text)
        else:
            hwp.HAction.Run("MoveDocEnd")
            hwp.HAction.Run("BreakPara")
            _insert_text(hwp, text)
        _save(hwp, out)
    worker().run(job)
    _after_edit(out, False)
    return f"문단을 넣었습니다 ({where}, 한글 사용, {time.perf_counter() - t0:.1f}초).\n저장: {out}"


def hwp_fill_form(path: str, fields: dict[str, str], direction: str = "right", out_path: str = "") -> str:
    """Fill a form: find the label cell (e.g. '성명') and put the value in the cell to its right (or below).
    The old content of that cell is replaced (cells holding pictures/objects are skipped).
    The original stays untouched; saved as a new '_수정본' file.

    fields: {"성명": "홍길동", "부서": "기획팀"}
    direction: 'right' | 'below'
    """
    fields = {str(k): str(v) for k, v in fields.items() if str(k).strip()}
    if not fields:
        raise ValueError("fields 가 비어 있습니다")
    p = _resolve(path)
    t0 = time.perf_counter()
    direction = "below" if direction == "below" else "right"
    if p.suffix.lower() == ".hwp" and not hangul_installed():
        raise RuntimeError(".hwp 편집에는 한글이 필요합니다 (.hwpx 는 한글 없이 고칠 수 있음)")
    out = _out_path(p, out_path or None)
    rep = _direct(p, out, [("fill", dict(fields), direction)])
    skipped = (rep or {}).get("skipped", [])
    if rep is not None and (not rep["missing"] or not hangul_installed()):
        _after_edit(out, True)
        msg = f"채운 칸 {len(rep['filled'])}개: {', '.join(rep['filled']) or '없음'} (한글 없이 직접, {(time.perf_counter() - t0) * 1000:.0f} ms)"
        if rep["missing"]:
            msg += f"\n못 채운 제목 칸: {', '.join(rep['missing'])}"
        if skipped:
            msg += f"\n그림·개체가 든 칸이라 건너뜀: {', '.join(skipped)}"
        return msg + f"\n저장: {out}"
    src = out if rep is not None else p  # direct edit filled some → let Hangul do the rest on that copy
    todo = {k: v for k, v in fields.items() if rep is None or (k in rep["missing"] and k not in skipped)}
    move = "TableRightCell" if direction == "right" else "TableLowerCell"

    def job(hwp):
        _open(hwp, src)
        done, missing = [], []
        for label, value in todo.items():
            hwp.HAction.Run("MoveDocBegin")
            found = False
            for _ in range(50):  # skip matches outside tables
                if not _find(hwp, label):
                    break
                hwp.HAction.Run("Cancel")
                if hwp.CellShape is not None:
                    found = True
                    break
            if not found:
                missing.append(label)
                continue
            hwp.HAction.Run(move)
            hwp.HAction.Run("MoveListBegin")
            hwp.HAction.Run("MoveSelListEnd")
            hwp.HAction.Run("Delete")
            _insert_text(hwp, value)
            done.append(label)
        _save(hwp, out)
        return done, missing
    done, missing = worker().run(job)
    _after_edit(out, False)
    filled = (rep["filled"] if rep else []) + done
    msg = f"채운 칸 {len(filled)}개: {', '.join(filled) or '없음'} ({time.perf_counter() - t0:.1f}초)"
    if missing:
        msg += f"\n못 찾은 제목 칸: {', '.join(missing)}"
    if skipped:
        msg += f"\n그림·개체가 든 칸이라 건너뜀: {', '.join(skipped)}"
    return msg + f"\n저장: {out}"


def _md_blocks(md: str):
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                yield "table", rows[:200]
            continue
        m = re.match(r"(#{1,3})\s+(.*)", ln)
        if m:
            yield f"h{len(m.group(1))}", m.group(2)
        elif ln.strip():
            yield "p", re.sub(r"\*\*(.+?)\*\*", r"\1", ln)
        i += 1


def hwp_create(markdown: str, file_name: str, folder: str = "") -> str:
    """Create a new Hangul document from simple Markdown:
    '# title', '## heading', '### small heading', paragraphs, '- bullet', '| table | rows |' (first row = header).

    file_name: e.g. '회의록.hwpx' (.hwp or .hwpx; default .hwpx)
    folder: where to save (default: Documents\\Claude 한글문서)
    """
    name = Path(_safe_text(file_name)).name
    if not name.lower().endswith(EXTS):
        name += ".hwpx"
    out = ((_resolve_dir(folder) if folder else NEW_DIR) / name).resolve()
    if out.suffix.lower() not in EXTS or not (_allowed(out) or out.parent == NEW_DIR.resolve()):
        raise ValueError("허용된 폴더 밖이거나 한글 확장자가 아닙니다")
    if out.exists():
        out = out.with_name(out.stem + time.strftime("_%H%M%S") + out.suffix)
    blocks = list(_md_blocks(markdown[:500_000]))
    size = {"h1": 18, "h2": 14, "h3": 12}
    t0 = time.perf_counter()

    def char(hwp, pt, bold):
        a = hwp.HParameterSet.HCharShape
        hwp.HAction.GetDefault("CharShape", a.HSet)
        a.Height = int(pt * 100)
        a.Bold = 1 if bold else 0
        hwp.HAction.Execute("CharShape", a.HSet)

    def job(hwp):
        hwp.Clear(1)
        first = True
        for kind, val in blocks:
            if not first:
                hwp.HAction.Run("BreakPara")
            first = False
            if kind == "table":
                ncol = min(max(len(r) for r in val), 30)
                t = hwp.HParameterSet.HTableCreation
                hwp.HAction.GetDefault("TableCreate", t.HSet)
                t.Rows, t.Cols, t.WidthType, t.HeightType = len(val), ncol, 2, 0
                hwp.HAction.Execute("TableCreate", t.HSet)
                for ri, row in enumerate(val):
                    for ci in range(ncol):
                        cell = row[ci] if ci < len(row) else ""
                        if ri == 0:
                            char(hwp, 10, True)
                        if cell:
                            _insert_text(hwp, re.sub(r"\*\*(.+?)\*\*", r"\1", cell).replace("<br>", "\n"))
                        if ri == 0:
                            char(hwp, 10, False)
                        if not (ri == len(val) - 1 and ci == ncol - 1):
                            hwp.HAction.Run("TableRightCell")
                hwp.HAction.Run("Cancel")
                hwp.HAction.Run("MoveDocEnd")
            elif kind in size:
                char(hwp, size[kind], True)
                _insert_text(hwp, val)
                char(hwp, 10, False)
            else:
                _insert_text(hwp, val)
        _save(hwp, out)
    worker().run(job)
    _after_edit(out, False)
    return f"새 문서를 만들었습니다: {out} ({time.perf_counter() - t0:.1f}초)"


WRITE_TOOLS = (hwp_open_in_hangul, hwp_replace_text, hwp_insert_text, hwp_fill_form, hwp_create)
if not READONLY:
    for _fn in WRITE_TOOLS:
        mcp.tool()(_fn)


# ---------------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="hwp-mcp: Hangul document tools for Claude")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP (for claude.ai via a tunnel)")
    parser.add_argument("--port", type=int, default=_int("HWP_MCP_PORT", 8772))
    args = parser.parse_args()
    if args.http:
        from mcp.server.transport_security import TransportSecuritySettings
        if not REMOTE_OPEN and not READONLY:
            # remote callers must not pop documents open in the user's real Hangul unless allowed
            mcp._tool_manager._tools.pop("hwp_open_in_hangul", None)
        hosts = [h.strip() for h in os.getenv("HWP_MCP_PUBLIC_HOSTS", "").split(",") if h.strip()]
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = args.port
        mcp.settings.streamable_http_path = "/mcp"
        mcp.settings.stateless_http = True
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*"] + hosts + [h + ":*" for h in hosts],
            allowed_origins=["https://claude.ai", "https://*.claude.ai"],
        )
        tools = sorted(t.name for t in mcp._tool_manager.list_tools())
        print(f"hwp-mcp HTTP on 127.0.0.1:{args.port}/mcp  public hosts={hosts or '-'}  tools={tools}", flush=True)
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
