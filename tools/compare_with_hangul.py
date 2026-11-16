"""hwp_fast 정확도·속도 검사: 한글 프로그램(COM)이 뽑은 텍스트와 글자 단위로 비교한다.

실행: python -X utf8 tools/compare_with_hangul.py 파일1.hwp 파일2.hwpx ...   (폴더를 주면 그 안의 hwp/hwpx 전부)
출력: 파일별 재현율(한글이 뽑은 글자 중 hwp_fast도 뽑은 비율), 정밀도, 속도.
"""
import html
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hwp_fast  # noqa: E402

KEEP = re.compile(r"[0-9A-Za-z가-힣]")


def chars(s):
    return Counter(KEEP.findall(s))


def com_text(hwp, path):
    hwp.Open(str(Path(path).resolve()), "", "forceopen:true;versionwarning:false")
    t = html.unescape(hwp.GetTextFile("TEXT", "") or "")
    hwp.Clear(1)
    return t


def main(args):
    files = []
    for a in args:
        p = Path(a)
        files += sorted(x for x in p.rglob("*") if x.suffix.lower() in (".hwp", ".hwpx")) if p.is_dir() else [p]
    import win32com.client as win32
    t0 = time.perf_counter()
    hwp = win32.gencache.EnsureDispatch("HWPFrame.HwpObject")
    try:
        hwp.RegisterModule("FilePathCheckDLL", "FilePathCheckerModule")
    except Exception:
        pass
    hwp.XHwpWindows.Item(0).Visible = False
    print(f"한글 시작: {(time.perf_counter() - t0) * 1000:.0f} ms")
    worst = 1.0
    try:
        for f in files:
            t1 = time.perf_counter()
            try:
                fast = hwp_fast.convert(f, "txt", headers=True)
                ferr = ""
            except hwp_fast.HwpError as e:
                fast, ferr = "", str(e)
            tf = (time.perf_counter() - t1) * 1000
            t2 = time.perf_counter()
            ref = com_text(hwp, f)
            tc = (time.perf_counter() - t2) * 1000
            a, b = chars(ref), chars(fast)
            common = sum((a & b).values())
            rec = common / max(sum(a.values()), 1)
            prec = common / max(sum(b.values()), 1)
            if not ferr:
                worst = min(worst, rec)
            miss = "".join(sorted((a - b).elements()))[:40]
            print(f"{'FAIL '+ferr if ferr else 'ok':6} 재현 {rec:6.1%} 정밀 {prec:6.1%}  fast {tf:6.0f} ms  한글 {tc:6.0f} ms  "
                  f"{f.name[:40]}" + (f"  누락:{miss}" if rec < 0.995 and not ferr else ""))
    finally:
        hwp.Quit()
    print(f"최저 재현율: {worst:.1%}")


if __name__ == "__main__":
    main(sys.argv[1:])
