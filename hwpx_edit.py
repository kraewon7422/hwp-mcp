"""hwpx_edit — 한글 프로그램 없이 .hwpx 문서를 직접 고치는 도구 (v1, 표준 라이브러리만 사용).

할 수 있는 일 (서식 유지, 원래 XML은 고친 글자 부분만 바뀜):
  replace : 글자 찾아 바꾸기 (글자 모양이 중간에 바뀌어 run 이 나뉜 글자도 찾음)
  fill    : 양식 칸 채우기 — '성명' 같은 제목 칸을 찾아 오른쪽(또는 아래) 칸에 값 넣기
  insert  : 문단 넣기 — 문서 끝 / 처음 / 어떤 글이 있는 문단 다음

고친 문단의 줄 배치 정보(linesegarray)는 지워서 한글이 열 때 다시 계산하게 한다.

사용법:
  python hwpx_edit.py replace 입력.hwpx 출력.hwpx --find 2026 --replace 2027
  python hwpx_edit.py fill    입력.hwpx 출력.hwpx --field 성명=홍길동 --field 부서=기획팀 [--below]
  python hwpx_edit.py insert  입력.hwpx 출력.hwpx --text "새 문단" [--where end|start|after:찾을글]
파이썬에서:
  from hwpx_edit import edit;  edit("in.hwpx", "out.hwpx", [("replace", "2026", "2027")])
"""
import html
import io
import re
import sys
import zipfile
from xml.sax.saxutils import escape

__version__ = "1.0"


class HwpxEditError(Exception):
    pass


TAG_RE = re.compile(r"<[^>]+>")
NAME_RE = re.compile(r"</?([\w.:-]+)")
ATTR_RE = re.compile(r'([\w:.-]+)="([^"]*)"')
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
SEP = "\uFFFF"


def _clean(s):
    """XML 에 넣을 수 없는 제어 문자를 지운다 (넣으면 한글이 파일을 못 연다)."""
    return _CTRL_CHARS.sub("", str(s))  # 다른 개체(표·도형·컨트롤) 경계: 이 문자를 넘어서는 찾기는 하지 않는다
INLINE_SEP = {"tab": "\t", "lineBreak": "\n"}


def _attrs(tag):
    return dict(ATTR_RE.findall(tag))


class Section:
    """section XML 한 개를 한 번 훑어 문단·run·글자 조각·표 칸 위치를 기록한다."""

    def __init__(self, name, xml):
        self.name, self.xml = name, xml
        self.paras, self.segs, self.ts, self.cells = [], [], [], []
        self._scan()

    def _scan(self):
        xml = self.xml
        para_st, run_st, cell_st, tbl_st = [], [], [], []
        in_t, pos, tbl_n, lsa = None, 0, 0, None
        for m in TAG_RE.finditer(xml):
            s, e = m.span()
            tag = m.group(0)
            if in_t is not None and s > pos:
                self.segs.append((s - (s - pos), s))
                para_st[-1]["items"].append(("seg", len(self.segs) - 1))
            pos = e
            if tag[1] in "?!":
                continue
            closing, selfc = tag[1] == "/", tag.endswith("/>")
            nm = NAME_RE.match(tag)
            if nm is None:
                continue
            ln = nm.group(1).split(":")[-1]
            cur = para_st[-1] if para_st else None
            if ln == "p":
                if closing:
                    p = para_st.pop()
                    p["end"] = e
                elif not selfc:
                    p = {"i": len(self.paras), "start": s, "open_end": e, "tag": tag,
                         "parent": cur["i"] if cur else None, "cell": cell_st[-1]["i"] if cell_st else None,
                         "depth": len(para_st), "items": [], "runs": [], "lsa": None, "end": None}
                    self.paras.append(p)
                    para_st.append(p)
                continue
            if ln == "run" and cur is not None and in_t is None:
                if closing:
                    r = run_st.pop()
                    r["close_start"], r["end"] = s, e
                else:
                    r = {"start": s, "open_end": e, "tag": tag, "para": cur["i"], "self": selfc,
                         "close_start": None, "end": e if selfc else None}
                    cur["runs"].append(r)
                    if not selfc:
                        run_st.append(r)
                continue
            if ln == "t" and cur is not None:
                if closing and in_t is not None:
                    in_t["close_start"], in_t["end"] = s, e
                    self.ts.append(in_t)
                    in_t = None
                elif not closing and not selfc:
                    in_t = {"para": cur["i"], "start": s, "open_end": e, "run": run_st[-1] if run_st else None}
                continue
            if in_t is not None:  # <hp:t> 안의 탭·줄바꿈 등
                if not closing:
                    cur["items"].append(("sep", INLINE_SEP.get(ln, SEP)))
                continue
            if ln == "linesegarray" and cur is not None:
                if closing and lsa is not None:
                    lsa[1] = e
                    cur["lsa"] = tuple(lsa)
                    lsa = None
                elif selfc:
                    cur["lsa"] = (s, e)
                else:
                    lsa = [s, None]
                continue
            if lsa is not None:
                continue
            if ln == "tbl":
                if closing:
                    tbl_st.pop()
                elif not selfc:
                    tbl_n += 1
                    tbl_st.append(tbl_n)
            elif ln == "tc":
                if closing:
                    c = cell_st.pop()
                    c["end"] = e
                elif not selfc:
                    c = {"i": len(self.cells), "start": s, "tbl": tbl_st[-1] if tbl_st else 0, "row": 0, "col": 0,
                         "rs": 1, "cs": 1, "pdepth": len(para_st), "end": None}
                    self.cells.append(c)
                    cell_st.append(c)
            elif ln == "cellAddr" and cell_st:
                a = _attrs(tag)
                cell_st[-1]["row"], cell_st[-1]["col"] = int(a.get("rowAddr", 0)), int(a.get("colAddr", 0))
            elif ln == "cellSpan" and cell_st:
                a = _attrs(tag)
                cell_st[-1]["rs"], cell_st[-1]["cs"] = int(a.get("rowSpan", 1)), int(a.get("colSpan", 1))
            if cur is not None and not closing and ln not in ("run", "t", "linesegarray"):
                if not cur["items"] or cur["items"][-1] != ("sep", SEP):
                    cur["items"].append(("sep", SEP))

    # ---- 읽기 도우미 ----
    def seg_text(self, k):
        s, e = self.segs[k]
        return html.unescape(self.xml[s:e])

    def para_text(self, p, sep=True):
        out = []
        for kind, v in p["items"]:
            if kind == "seg":
                out.append(self.seg_text(v))
            elif sep:
                out.append(v)
        return "".join(out)

    def cell_paras(self, c):
        return [p for p in self.paras if p["cell"] == c["i"] and p["depth"] == c["pdepth"]]

    def cell_text(self, c):
        return "\n".join(self.para_text(p, sep=False) for p in self.cell_paras(c))


def _apply(xml, splices):
    """splices: (start, end, new) — 겹치지 않아야 한다. 뒤에서부터 적용."""
    splices = sorted(splices, key=lambda x: (x[0], x[1]))
    for (a1, b1, _), (a2, b2, _) in zip(splices, splices[1:]):
        if a2 < b1:
            raise HwpxEditError("내부 오류: 겹치는 수정")
    out, last = [], 0
    for a, b, new in splices:
        out.append(xml[last:a])
        out.append(new)
        last = b
    out.append(xml[last:])
    return "".join(out)


def _lsa_splices(sec, para_ids):
    return [(sec.paras[i]["lsa"][0], sec.paras[i]["lsa"][1], "") for i in para_ids if sec.paras[i]["lsa"]]


# ───────────────────────── 바꾸기 ─────────────────────────
def _replace(sec, find, repl):
    if not find:
        return 0, []
    splices, touched, count = [], set(), 0
    for p in sec.paras:
        segs = [v for kind, v in p["items"] if kind == "seg"]
        if not segs:
            continue
        # 문단 글자 = 조각들 + 경계 문자. 조각 위치표를 만든다.
        texts, owner, buf = {}, [], []
        for kind, v in p["items"]:
            if kind == "seg":
                t = sec.seg_text(v)
                texts[v] = list(t)
                owner += [(v, j) for j in range(len(t))]
                buf.append(t)
            else:
                owner.append(None)
                buf.append(v)
        full = "".join(buf)
        hits, i = [], full.find(find)
        while i >= 0:
            hits.append(i)
            i = full.find(find, i + len(find))
        if not hits:
            continue
        changed = set()
        for i in reversed(hits):
            span = owner[i:i + len(find)]
            if any(o is None for o in span):
                continue  # 다른 개체를 넘어서는 글자 — 건너뜀
            first_seg, first_j = span[0]
            # 찾은 글자를 지우고 첫 조각 위치에 바꿀 글자를 넣는다
            for seg, j in reversed(span):
                texts[seg][j] = ""
            texts[first_seg][first_j] = repl
            changed |= {seg for seg, _ in span}
            count += 1
        for seg in changed:
            s, e = sec.segs[seg]
            splices.append((s, e, escape("".join(texts[seg]))))
        if changed:
            touched.add(p["i"])
    return count, splices + _lsa_splices(sec, touched)


# ───────────────────────── 새 문단 만들기 ─────────────────────────
def _new_para_xml(para_tag, run_tag, line):
    ptag = re.sub(r'\bid="[^"]*"', 'id="0"', para_tag, count=1)
    ptag = re.sub(r'\bpageBreak="1"', 'pageBreak="0"', ptag)
    ptag = re.sub(r'\bcolumnBreak="1"', 'columnBreak="0"', ptag)
    rtag = run_tag[:-2] + ">" if run_tag.endswith("/>") else run_tag
    body = f"<hp:t>{escape(line)}</hp:t>" if line else ""
    return f"{ptag}{rtag}{body}</hp:run></hp:p>"


def _text_run_tag(sec, p):
    """문단에서 글자가 있는 run 의 여는 태그 (없으면 첫 run, 그것도 없으면 기본)."""
    for t in sec.ts:
        if t["para"] == p["i"] and t["run"] is not None:
            return t["run"]["tag"]
    if p["runs"]:
        return p["runs"][0]["tag"]
    return '<hp:run charPrIDRef="0">'


# ───────────────────────── 양식 칸 채우기 ─────────────────────────
def _norm(s):
    return re.sub(r"[\s:：]+", "", s)


def _fill(sec, fields, direction, done, skipped):
    splices, used = [], set()
    for label, value in fields.items():
        if label in done:
            continue
        want = _norm(label)
        if not want:
            continue  # 빈 제목은 모든 빈 칸과 맞으므로 거부
        cands = [c for c in sec.cells if _norm(sec.cell_text(c)) == want] or \
                [c for c in sec.cells if want and want in _norm(sec.cell_text(c)) and len(_norm(sec.cell_text(c))) <= len(want) + 6]
        for lc in cands:
            if direction == "below":
                tr, tc = lc["row"] + lc["rs"], lc["col"]
            else:
                tr, tc = lc["row"], lc["col"] + lc["cs"]
            target = next((c for c in sec.cells if c["tbl"] == lc["tbl"] and c["row"] <= tr < c["row"] + c["rs"]
                           and c["col"] <= tc < c["col"] + c["cs"]), None)
            if target is None or target["i"] in used:
                continue
            paras = sec.cell_paras(target)
            if not paras:
                continue
            if any(q["parent"] in {p["i"] for p in paras} for q in sec.paras) or \
                    any(item == ("sep", SEP) for p in paras for item in p["items"]):
                skipped.add(label)  # 칸 안에 표·그림·개체가 있음 → 지우지 않고 건너뜀
                continue
            run_tag = _text_run_tag(sec, paras[0])
            lines = str(value).replace("\r\n", "\n").split("\n")
            new = "".join(_new_para_xml(paras[0]["tag"] if k == 0 else paras[0]["tag"], run_tag, ln)
                          for k, ln in enumerate(lines))
            splices.append((paras[0]["start"], paras[-1]["end"], new))
            used.add(target["i"])
            done[label] = sec.cell_text(target)  # 지워진 이전 내용 (보고용)
            break
    return splices


# ───────────────────────── 문단 넣기 ─────────────────────────
def _insert(sec, text, where, is_first, is_last):
    lines = text.replace("\r\n", "\n").split("\n")
    top = [p for p in sec.paras if p["depth"] == 0]
    if not top:
        return None
    if where.startswith("after:"):
        key = where[6:]
        target = next((p for p in sec.paras if key and key in sec.para_text(p, sep=False)), None)
        if target is None:
            return None
        rt = _text_run_tag(sec, target)
        new = "".join(_new_para_xml(target["tag"], rt, ln) for ln in lines)
        return [(target["end"], target["end"], new)]
    if where == "end" and is_last:
        last = top[-1]
        rt = _text_run_tag(sec, last)
        new = "".join(_new_para_xml(last["tag"], rt, ln) for ln in lines)
        return [(last["end"], last["end"], new)]
    if where == "start" and is_first:
        p0 = top[0]
        if not p0["runs"] or p0["runs"][0]["self"]:
            rt = _text_run_tag(sec, p0)
            new = "".join(_new_para_xml(p0["tag"], rt, ln) for ln in lines)
            return [(p0["start"], p0["start"], new)]
        r0 = p0["runs"][0]
        inner = sec.xml[r0["open_end"]:r0["close_start"]]
        m = re.match(r"(?:\s*(?:<hp:secPr\b.*?</hp:secPr>|<hp:ctrl>.*?</hp:ctrl>))+", inner, re.S)
        head = m.group(0) if m else ""
        rt = _text_run_tag(sec, p0)
        first = re.sub(r'\bpageBreak="1"', 'pageBreak="0"', p0["tag"])
        rtag = rt[:-2] + ">" if rt.endswith("/>") else rt
        new = f"{first}{r0['tag']}{head}</hp:run>{rtag}<hp:t>{escape(lines[0])}</hp:t></hp:run></hp:p>"
        new += "".join(_new_para_xml(p0["tag"], rt, ln) for ln in lines[1:])
        out = [(p0["start"], p0["start"], new)]
        if head:  # 원래 첫 문단에서 구역 설정을 떼어 냄
            out.append((r0["open_end"], r0["open_end"] + len(head), ""))
        return out
    return None


# ───────────────────────── 파일 단위 ─────────────────────────
def _section_names(zf):
    names = [n for n in zf.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)]
    return sorted(names, key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))


def edit(src, dst, ops):
    """ops: [("replace", 찾을글, 바꿀글) | ("fill", {제목: 값}, "right"|"below") | ("insert", 글, "end"|"start"|"after:글")]
    → 결과 보고 dict. src 와 dst 가 같으면 거부한다."""
    if str(src) == str(dst):
        raise HwpxEditError("원본과 같은 파일에 저장할 수 없습니다")
    with open(src, "rb") as f:
        data = f.read()
    try:
        zin = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HwpxEditError("HWPX(zip) 파일이 아니거나 손상되었습니다")
    names = zin.namelist()
    if "META-INF/manifest.xml" in names and b"encryption-data" in zin.read("META-INF/manifest.xml"):
        raise HwpxEditError("암호화된 HWPX(배포용·암호 문서)는 직접 고칠 수 없습니다")
    secs = _section_names(zin)
    if not secs:
        raise HwpxEditError("HWPX 본문(section*.xml)을 찾지 못했습니다")
    xmls = {n: zin.read(n).decode("utf-8") for n in secs}
    report = {"replaced": 0, "filled": [], "missing": [], "inserted": 0}
    for op in ops:
        kind = op[0]
        if kind == "replace":
            for n in secs:
                sec = Section(n, xmls[n])
                cnt, sp = _replace(sec, _clean(op[1]), _clean(op[2]))
                if cnt:
                    xmls[n] = _apply(sec.xml, sp)
                    report["replaced"] += cnt
        elif kind == "fill":
            done, skipped = {}, set()
            fields = {_clean(k): _clean(v) for k, v in op[1].items()}
            op = (op[0], fields) + tuple(op[2:])
            for n in secs:
                sec = Section(n, xmls[n])
                sp = _fill(sec, fields, op[2] if len(op) > 2 else "right", done, skipped)
                if sp:
                    xmls[n] = _apply(sec.xml, sp)
            report["filled"] += [k for k in op[1] if k in done]
            report.setdefault("old_values", {}).update({k: v for k, v in done.items() if v is not True})
            report["missing"] += [k for k in op[1] if k not in done]
            report.setdefault("skipped", [])
            report["skipped"] += [k for k in op[1] if k not in done and k in skipped]
        elif kind == "insert":
            where = op[2] if len(op) > 2 else "end"
            ok = False
            order = secs if where != "end" else list(reversed(secs))
            for n in order:
                sec = Section(n, xmls[n])
                sp = _insert(sec, _clean(op[1]), where, n == secs[0], n == secs[-1])
                if sp:
                    xmls[n] = _apply(sec.xml, sp)
                    ok = True
                    break
            if not ok:
                raise HwpxEditError(f"문단을 넣을 위치를 찾지 못했습니다: {where}")
            report["inserted"] += len(op[1].split("\n"))
        else:
            raise HwpxEditError(f"모르는 작업: {kind}")
    # 미리보기 글자(Preview/PrvText.txt)도 새 내용으로
    prv = None
    try:
        import hwp_fast  # 같은 폴더에 있으면 사용
        tmp = io.BytesIO()
        _write_zip(zin, tmp, xmls, None)
        prv = hwp_fast.to_text(hwp_fast.parse(tmp.getvalue()))[:1024]
    except Exception:
        pass
    with open(dst, "wb") as f:
        _write_zip(zin, f, xmls, prv)
    return report


def _write_zip(zin, fobj, xmls, prv):
    with zipfile.ZipFile(fobj, "w") as zout:
        for info in zin.infolist():
            if info.filename in xmls:
                body = xmls[info.filename].encode("utf-8")
            elif info.filename == "Preview/PrvText.txt" and prv is not None:
                body = prv.encode("utf-8")
            else:
                body = zin.read(info.filename)
            ni = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            ni.compress_type = info.compress_type
            ni.external_attr = info.external_attr
            zout.writestr(ni, body)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="한글 없이 .hwpx 고치기")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("replace", "fill", "insert"):
        s = sub.add_parser(name)
        s.add_argument("src")
        s.add_argument("dst")
        if name == "replace":
            s.add_argument("--find", required=True)
            s.add_argument("--replace", required=True)
        elif name == "fill":
            s.add_argument("--field", action="append", required=True, help="제목=값 (여러 번)")
            s.add_argument("--below", action="store_true", help="오른쪽 대신 아래 칸")
        else:
            s.add_argument("--text", required=True)
            s.add_argument("--where", default="end")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if a.cmd == "replace":
        ops = [("replace", a.find, a.replace)]
    elif a.cmd == "fill":
        if any("=" not in f for f in a.field):
            ap.error("--field 는 제목=값 형식이어야 합니다")
        fields = dict(f.split("=", 1) for f in a.field)
        ops = [("fill", fields, "below" if a.below else "right")]
    else:
        ops = [("insert", a.text.replace("\\n", "\n"), a.where)]
    try:
        print(edit(a.src, a.dst, ops))
    except HwpxEditError as e:
        print(f"[실패] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
