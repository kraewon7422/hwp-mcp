"""Regenerate the synthetic test documents in samples/ with the installed Hangul (Windows).

  plain.hwp / plain.hwpx  : two paragraphs, a 2x2 table, a last paragraph
  dist.hwp                : the same as a distribution (배포용) document — exercises the AES path
  dist_secured.hwpx       : a distribution HWPX (encrypted XML) — must be rejected with a clear message

Hangul writes the Windows user name into the document properties (author / last saved by);
this script blanks those so the samples carry no personal data.

Run: python -X utf8 tools/make_samples.py
"""
import getpass
import io
import re
import shutil
import sys
import zipfile
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples"
PLACEHOLDER = "hwpmcp"


def scrub_hwp(path: Path, user: str):
    """Overwrite the user name inside the OLE file in place (same length → container stays valid)."""
    data = bytearray(path.read_bytes())
    for enc in ("utf-16le", "latin-1"):
        old = user.encode(enc)
        new = (PLACEHOLDER * 10)[:len(user)].encode(enc)
        data = bytearray(bytes(data).replace(old, new))
    path.write_bytes(bytes(data))


def scrub_hwpx(path: Path, user: str):
    zin = zipfile.ZipFile(io.BytesIO(path.read_bytes()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zout:
        for info in zin.infolist():
            body = zin.read(info.filename)
            if info.filename == "Contents/content.hpf":
                txt = body.decode("utf-8")
                txt = re.sub(r'(<opf:meta name="(?:creator|lastsaveby)"[^>]*>)[^<]*(<)', r"\1" + PLACEHOLDER + r"\2", txt)
                body = txt.encode("utf-8")
            ni = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            ni.compress_type = info.compress_type
            ni.external_attr = info.external_attr
            zout.writestr(ni, body)
    path.write_bytes(buf.getvalue())


def main():
    import win32com.client as win32
    OUT.mkdir(exist_ok=True)
    user = getpass.getuser()
    h = win32.gencache.EnsureDispatch("HWPFrame.HwpObject")
    if not h.RegisterModule("FilePathCheckDLL", "FilePathCheckerModule"):
        sys.exit("security module not registered — run setup.ps1 first")
    h.XHwpWindows.Item(0).Visible = False

    def text(s):
        a = h.HParameterSet.HInsertText
        h.HAction.GetDefault("InsertText", a.HSet)
        a.Text = s
        h.HAction.Execute("InsertText", a.HSet)

    def security(pw="test12345"):
        act = h.CreateAction("FileSetSecurity")
        st = act.CreateSet()
        act.GetDefault(st)
        st.SetItem("Password", pw)
        st.SetItem("NoPrint", 0)
        st.SetItem("NoCopy", 0)
        return act.Execute(st)

    try:
        h.Clear(1)
        text("배포용 시험 문서입니다. 가나다 ABC 123.")
        h.HAction.Run("BreakPara")
        text("둘째 문단: 특수문자 ① ※ ▶ ☐ 〇 — “따옴표”")
        h.HAction.Run("BreakPara")
        t = h.HParameterSet.HTableCreation
        h.HAction.GetDefault("TableCreate", t.HSet)
        t.Rows, t.Cols, t.WidthType, t.HeightType = 2, 2, 2, 0
        h.HAction.Execute("TableCreate", t.HSet)
        for k, s in enumerate(["항목", "값", "속도", "빠름"]):
            text(s)
            if k < 3:
                h.HAction.Run("TableRightCell")
        h.HAction.Run("Cancel")
        h.HAction.Run("MoveDocEnd")
        text("마지막 문단 끝.")
        assert h.SaveAs(str(OUT / "plain.hwp"), "HWP", "")
        assert h.SaveAs(str(OUT / "plain.hwpx"), "HWPX", "")
        h.Clear(1)
        # distribution .hwp: open a copy, protect it, save in place
        shutil.copy(OUT / "plain.hwp", OUT / "dist.hwp")
        h.Open(str(OUT / "dist.hwp"), "HWP", "forceopen:true")
        security()
        h.Save(True)
        h.Clear(1)
        # distribution .hwpx: Hangul encrypts the open .hwpx file when protection is applied
        shutil.copy(OUT / "plain.hwpx", OUT / "dist_secured.hwpx")
        h.Open(str(OUT / "dist_secured.hwpx"), "HWPX", "forceopen:true")
        security()
        h.Save(True)
        h.Clear(1)
    finally:
        h.Quit()
    for f in ("plain.hwp", "dist.hwp"):
        scrub_hwp(OUT / f, user)
    for f in ("plain.hwpx", "dist_secured.hwpx"):
        scrub_hwpx(OUT / f, user)
    for f in sorted(OUT.iterdir()):
        raw = f.read_bytes()
        leak = user.encode("utf-16le") in raw or user.encode() in raw
        print(f"{f.name:22s} {len(raw):6d} bytes  user-name-free={not leak}")


if __name__ == "__main__":
    main()
