"""hwpx_edit 검증: 실제 .hwpx 들의 '사본'에 바꾸기·칸 채우기·문단 넣기를 하고,
한글 프로그램(COM)으로 열어 (1) 열리는지 (2) 바뀐 내용이 보이는지 (3) 원래 글자가 사라지지 않았는지 확인한다.

실행: python -X utf8 tools/check_hwpx_edit.py <hwpx 파일이나 폴더> ... [--keep 결과폴더]
원본은 절대 고치지 않는다 (임시 폴더에 결과를 쓴다).
"""
import html
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hwp_fast  # noqa: E402
import hwpx_edit  # noqa: E402

KEEP = re.compile(r"[0-9A-Za-z가-힣]")


def pick_word(text):
    words = [w for w in re.findall(r"[가-힣]{2,}", text)]
    c = Counter(words)
    return c.most_common(1)[0][0] if c else None


def pick_label(doc):
    for b in doc["blocks"]:
        if b["type"] == "table" and b["cols"] >= 2:
            for row in b["cells"]:
                if len(row) >= 2 and 1 <= len(row[0].strip()) <= 8 and "\n" not in row[0]:
                    return row[0].strip()
    return None


def main(args):
    keep = None
    if "--keep" in args:
        i = args.index("--keep")
        keep = Path(args[i + 1])
        args = args[:i] + args[i + 2:]
        keep.mkdir(parents=True, exist_ok=True)
    files = []
    for a in args:
        p = Path(a)
        files += sorted(p.rglob("*.hwpx")) if p.is_dir() else [p]
    import win32com.client as win32
    hwp = win32.gencache.EnsureDispatch("HWPFrame.HwpObject")
    hwp.RegisterModule("FilePathCheckDLL", "FilePathCheckerModule")
    hwp.XHwpWindows.Item(0).Visible = False
    tmp = Path(tempfile.mkdtemp(prefix="hwpx_edit_"))
    bad = 0
    try:
        for n, f in enumerate(files):
            try:
                doc = hwp_fast.parse(f)
            except hwp_fast.HwpError as e:
                print(f"skip  {f.name[:40]}  ({e})")
                continue
            src = tmp / f"in{n}.hwpx"
            shutil.copy(f, src)
            text = hwp_fast.to_text(doc)
            word, label = pick_word(text), pick_label(doc)
            ops = []
            if word:
                ops.append(("replace", word, "★바꿈★"))
            if label:
                ops.append(("fill", {label: "칸채움값"}, "right"))
            ops += [("insert", "끝에넣은문단", "end"), ("insert", "처음넣은문단", "start")]
            dst = (keep or tmp) / f"out{n}.hwpx"
            hwp.Open(str(src), "HWPX", "forceopen:true;versionwarning:false")
            orig_ref = html.unescape(hwp.GetTextFile("TEXT", "") or "")
            hwp.Clear(1)
            t0 = time.perf_counter()
            try:
                rep = hwpx_edit.edit(src, dst, ops)
            except hwpx_edit.HwpxEditError as e:
                print(f"ERR   {f.name[:40]}  {e}")
                bad += 1
                continue
            dt = (time.perf_counter() - t0) * 1000
            ok_open = hwp.Open(str(dst), "HWPX", "forceopen:true;versionwarning:false")
            ref = html.unescape(hwp.GetTextFile("TEXT", "") or "") if ok_open else ""
            hwp.Clear(1)
            problems = []
            if not ok_open:
                problems.append("한글에서 안 열림")
            if word and ref.count("★바꿈★") != rep["replaced"]:
                problems.append(f"바꿈 {rep['replaced']} vs 한글 {ref.count('★바꿈★')}")
            if label and rep["filled"] and "칸채움값" not in ref:
                problems.append("칸 채움 안 보임")
            for s_ in ("끝에넣은문단", "처음넣은문단"):
                if s_ not in ref:
                    problems.append(f"{s_} 안 보임")
            # 한글이 본 원본 글자 - (바꾼 낱말, 지운 칸 내용) 이 결과에 모두 남아 있어야 한다
            expect = Counter(KEEP.findall(orig_ref))
            if word:
                expect -= Counter({ch: orig_ref.count(word) * word.count(ch) for ch in set(word)})
            for old in rep.get("old_values", {}).values():
                expect -= Counter(KEEP.findall(old))
            lost = expect - Counter(KEEP.findall(ref))
            if sum(lost.values()):
                problems.append(f"글자 {sum(lost.values())}개 사라짐: {''.join(sorted(lost.elements()))[:30]}")
            status = "ok   " if not problems else "FAIL "
            bad += bool(problems)
            print(f"{status} {dt:5.0f} ms  바꿈 {rep['replaced']:3}  칸 {rep['filled'] or '-'}  {f.name[:36]}"
                  + ("  ← " + "; ".join(problems) if problems else ""))
    finally:
        hwp.Quit()
        if keep is None:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"실패 {bad} / {len(files)}")
    return bad


if __name__ == "__main__":
    sys.exit(1 if main(sys.argv[1:]) else 0)
