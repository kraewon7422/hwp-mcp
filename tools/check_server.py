"""server.py 전체 기능 점검 (Windows + 한글 필요). 합성 시험 문서의 사본만 쓴다.

실행: python -X utf8 tools/check_server.py
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
work = Path(tempfile.mkdtemp(prefix="hwpmcp_"))
os.environ["HWP_MCP_ROOTS"] = str(work)
os.environ["HWP_MCP_NEW_DIR"] = str(work / "new")
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402

fails = []


def check(name, cond, info=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not cond:
        fails.append(name)


def timed(fn):
    t = time.perf_counter()
    r = fn()
    return r, time.perf_counter() - t


for f in ("plain.hwp", "plain.hwpx", "dist.hwp"):
    shutil.copy(ROOT / "samples" / f, work / f)
H, X = str(work / "plain.hwp"), str(work / "plain.hwpx")
try:
    # ---- read / find ----
    r, dt = timed(lambda: s.hwp_read(X))
    check("read hwpx", "| 항목 | 값 |" in r and dt < 0.5, f"{dt*1000:.0f} ms")
    r = s.hwp_read(str(work / "dist.hwp"))
    check("read 배포용 hwp", "배포용" in r and "가나다" in r)
    check("find by name", "plain.hwpx" in s.hwp_find("plain", folder=str(work)))
    check("read by bare name", "가나다" in s.hwp_read("dist.hwp"))
    # ---- preview: instant embedded page 1 ----
    r, dt = timed(lambda: s.hwp_preview(X))
    check("preview page1 instant (embedded)", len(r) == 2 and "저장된 미리보기" in r[0] and dt < 0.5, f"{dt*1000:.0f} ms")
    # ---- direct HWPX edits ----
    r, dt = timed(lambda: s.hwp_replace_text(X, "가나다", "라마바"))
    check("replace hwpx direct", "한글 없이" in r and "1곳" in r and dt < 0.5, f"{dt*1000:.0f} ms")
    X2 = str(work / "plain_수정본.hwpx")
    r = s.hwp_insert_text(X2, "처음 문단", "start")
    r2 = s.hwp_insert_text(X2, "끝 문단", "end")
    r3 = s.hwp_insert_text(X2, "둘째 다음", "after:둘째 문단")
    r4 = s.hwp_fill_form(X2, {"항목": "새값", "속도": "두줄\n값"})
    check("insert/fill hwpx direct", all("한글 없이" in x for x in (r, r2, r3, r4)), r4.splitlines()[0])
    md = s.hwp_read(X2)
    order = [md.find(t) for t in ("처음 문단", "라마바", "둘째 문단", "둘째 다음", "| 항목 | 새값 |", "끝 문단")]
    check("hwpx result content+order", all(i >= 0 for i in order) and order == sorted(order), str(order))
    # stale embedded preview must NOT be used after a direct edit → exact render via Hangul
    r, dt = timed(lambda: s.hwp_preview(X2))
    check("preview after direct edit = exact", "전체" in r[0] and len(r) == 2, f"{dt:.1f}s")
    # ---- Hangul (.hwp) edits ----
    r, dt = timed(lambda: s.hwp_replace_text(H, "가나다", "라마바"))
    check("replace hwp via Hangul", "1곳" in r, f"{dt:.1f}s")
    H2 = str(work / "plain_수정본.hwp")
    r, dt = timed(lambda: s.hwp_fill_form(H2, {"항목": "새값", "없는칸": "x"}))
    check("fill hwp via Hangul (warm)", "항목" in r and "없는칸" in r and dt < 3, f"{dt:.1f}s")
    r, dt = timed(lambda: s.hwp_insert_text(H2, "끝 문단", "end"))
    check("insert hwp via Hangul (warm)", dt < 3, f"{dt:.1f}s")
    md = s.hwp_read(H2)
    check("hwp result content", "라마바" in md and "| 항목 | 새값 |" in md and "끝 문단" in md)
    r, dt = timed(lambda: s.hwp_preview(H2, "1"))
    check("preview hwp exact (warm)", len(r) == 2, f"{dt:.1f}s")
    r, dt = timed(lambda: s.hwp_preview(H2, "1"))
    check("preview cached", dt < 0.3, f"{dt*1000:.0f} ms")
    r = s.hwp_create("# 제목\n\n본문 문단\n\n| 가 | 나 |\n|---|---|\n| 1 | 2 |", "새문서.hwpx")
    check("create", "새문서" in r and "| 가 | 나 |" in s.hwp_read(str(work / "new" / "새문서.hwpx")))
    # ---- safety ----
    for bad in ("C:/Windows/win.ini", str(work / "x.bat"), "C:/Windows/System32/../x.hwp"):
        try:
            s.hwp_read(bad)
            check(f"blocked {bad}", False)
        except ValueError:
            check(f"blocked {bad}", True)
    try:
        s.hwp_replace_text(X, "가나다", "x", out_path=X)
        check("refuse overwrite original", False)
    except ValueError:
        check("refuse overwrite original", True)
    for bad in (r"\\127.0.0.1\share\a.hwp", "//host/share/a.hwp", r"C:\dev\a.hwp:stream"):
        try:
            s.hwp_read(bad)
            check(f"blocked network/ADS path {bad}", False)
        except ValueError:
            check(f"blocked network/ADS path {bad}", True)
    # a real file that merely has '_수정본' in its name is an original → must not be edited in place
    fake = work / "보고서_수정본.hwpx"
    shutil.copy(ROOT / "samples" / "plain.hwpx", fake)
    r = s.hwp_replace_text(str(fake), "가나다", "XYZ")
    check("user file named _수정본 not overwritten", "가나다" in s.hwp_read(str(fake)) and "보고서_수정본_수정본" in r, r.splitlines()[-1])
    # an existing other file must not be clobbered via out_path
    try:
        s.hwp_replace_text(X, "가나다", "x", out_path=str(work / "dist.hwp"))
        check("refuse out_path onto existing file", False)
    except ValueError:
        check("refuse out_path onto existing file", True)
    try:
        s.hwp_preview(X, "1-999999999")
        check("huge page range handled", True)
    except Exception as e:  # noqa: BLE001
        check("huge page range handled", False, str(e))
    # ---- recovery from a hung job ----
    w = s.worker()
    try:
        w.run(lambda h: time.sleep(6), timeout=2)
        check("timeout raised", False)
    except TimeoutError:
        check("timeout raised", True)
    r, dt = timed(lambda: s.hwp_replace_text(H2, "라마바", "가나다", out_path=str(work / "after_timeout.hwp")))
    check("works again after timeout", "바꿈" in r, f"{dt:.1f}s")
finally:
    s.worker().shutdown()
    shutil.rmtree(work, ignore_errors=True)
print(f"\n실패 {len(fails)}: {fails}" if fails else "\n모두 통과")
sys.exit(1 if fails else 0)
