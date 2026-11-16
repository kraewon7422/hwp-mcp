"""hwp_fast — 한글(.hwp / .hwpx) 문서를 빠르게 Markdown·텍스트·JSON으로 바꾸는 변환기 (v1).

- 표준 라이브러리만 사용 (pip 설치 불필요) → Claude Code, claude.ai 샌드박스 어디서든 그대로 실행.
- 한글 프로그램 없이 파일을 직접 해석: OLE(CFB) 컨테이너 → HWP 5.0 레코드 → 문단/표/각주/수식.
- HWPX(zip+XML), 배포용(열람 제한) HWP(AES-128 복호화)도 지원.
- 지원 안 함: 암호 걸린 문서, HWP 3.x 이하(→ 한글 프로그램으로 변환 필요).

사용법:
  python hwp_fast.py 문서.hwp                 # Markdown을 화면에 출력
  python hwp_fast.py 문서.hwp -f txt          # 순수 텍스트
  python hwp_fast.py *.hwp -o out_dir         # 여러 파일을 out_dir/*.md 로 저장
  python hwp_fast.py 문서.hwpx --time         # 걸린 시간 표시
파이썬에서:
  from hwp_fast import convert;  md = convert("문서.hwp")
"""
import io
import json
import re
import struct
import sys
import time
import zipfile
import zlib
from array import array
from pathlib import Path
import xml.etree.ElementTree as ET

__version__ = "1.0"


class HwpError(Exception):
    pass


# ───────────────────────── OLE / CFB 컨테이너 ─────────────────────────
CFB_MAGIC = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
ENDCHAIN, FREESECT = 0xFFFFFFFE, 0xFFFFFFFF


class CFB:
    """Compound File Binary 읽기 전용 파서 (olefile 대체, 의존성 없음)."""

    def __init__(self, data: bytes):
        if data[:8] != CFB_MAGIC:
            raise HwpError("OLE 파일이 아닙니다")
        self.d = data
        u16 = lambda o: struct.unpack_from("<H", data, o)[0]
        u32 = lambda o: struct.unpack_from("<I", data, o)[0]
        if u16(0x1E) not in (9, 12) or u16(0x20) != 6:
            raise HwpError("OLE 머리가 올바르지 않습니다")
        self.ss = 1 << u16(0x1E)
        self.mss = 1 << u16(0x20)
        nfat, dir_start, self.cutoff = u32(0x2C), u32(0x30), u32(0x38)
        minifat_start, difat_start, ndifat = u32(0x3C), u32(0x44), u32(0x48)

        difat = list(struct.unpack_from("<109I", data, 0x4C))
        s, seen, max_sec = difat_start, set(), len(data) // self.ss + 1
        while s < 0xFFFFFFFA and s not in seen and len(seen) < min(ndifat + 1, max_sec):
            seen.add(s)
            ent = struct.unpack_from("<%dI" % (self.ss // 4), self._sector(s))
            difat.extend(ent[:-1])
            s = ent[-1]
        fat = array("I")
        for sec in difat[:nfat]:
            if sec < 0xFFFFFFFA:
                fat.frombytes(self._sector(sec))
        if sys.byteorder == "big":
            fat.byteswap()
        self.fat = fat

        dirdata = self._chain_bytes(dir_start, self.fat, self._sector)
        self.entries = []
        for off in range(0, len(dirdata) - 127, 128):
            e = dirdata[off:off + 128]
            nlen = struct.unpack_from("<H", e, 0x40)[0]
            name = e[:max(nlen - 2, 0)].decode("utf-16le", "replace")
            typ = e[0x42]
            left, right, child = struct.unpack_from("<III", e, 0x44)
            start, size = struct.unpack_from("<II", e, 0x74)
            self.entries.append((name, typ, left, right, child, start, size))
        if not self.entries:
            raise HwpError("OLE 디렉터리가 비어 있습니다")
        root = self.entries[0]
        self.ministream = self._chain_bytes(root[5], self.fat, self._sector)[:root[6]]
        mf = array("I")
        mf.frombytes(self._chain_bytes(minifat_start, self.fat, self._sector))
        if sys.byteorder == "big":
            mf.byteswap()
        self.minifat = mf

        self.paths = {}
        self._walk(root[4], "")

    def _sector(self, s):
        off = (s + 1) * self.ss
        b = self.d[off:off + self.ss]
        return b if len(b) == self.ss else b.ljust(self.ss, b"\0")

    def _mini(self, s):
        off = s * self.mss
        return self.ministream[off:off + self.mss]

    @staticmethod
    def _chain_bytes(start, table, getter):
        out, s, seen = [], start, set()
        while s < 0xFFFFFFFA and s < len(table) and s not in seen:
            seen.add(s)
            out.append(getter(s))
            s = table[s]
        return b"".join(out)

    def _walk(self, idx, prefix):
        stack, seen = [(idx, prefix)], set()
        while stack:
            i, pre = stack.pop()
            if i >= len(self.entries) or i in seen:
                continue
            seen.add(i)
            name, typ, left, right, child, _, _ = self.entries[i]
            stack += [(left, pre), (right, pre)]
            path = pre + name
            self.paths[path] = i
            if typ == 1:  # storage
                stack.append((child, path + "/"))

    def exists(self, path):
        return path in self.paths

    def read(self, path):
        if path not in self.paths:
            raise HwpError(f"스트림 없음: {path}")
        _, typ, _, _, _, start, size = self.entries[self.paths[path]]
        if size < self.cutoff:
            return self._chain_bytes(start, self.minifat, self._mini)[:size]
        return self._chain_bytes(start, self.fat, self._sector)[:size]


# ───────────────────────── AES-128 복호화 (배포용 문서) ─────────────────────────
def _gmul(a, b):
    p = 0
    while b:
        if b & 1:
            p ^= a
        a = ((a << 1) ^ 0x11B) if a & 0x80 else (a << 1)
        b >>= 1
    return p


def _make_sbox():
    sbox = [0] * 256
    for x in range(256):
        inv = 0 if x == 0 else next(y for y in range(1, 256) if _gmul(x, y) == 1)
        s = inv
        for k in range(1, 5):
            s ^= ((inv << k) | (inv >> (8 - k))) & 0xFF
        sbox[x] = s ^ 0x63
    return sbox


_AES = {}


def _aes_init():
    if _AES:
        return _AES
    sbox = _make_sbox()
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    _AES.update(sbox=sbox, inv=inv,
                m9=[_gmul(i, 9) for i in range(256)], m11=[_gmul(i, 11) for i in range(256)],
                m13=[_gmul(i, 13) for i in range(256)], m14=[_gmul(i, 14) for i in range(256)])
    return _AES


def _aes_round_keys(key):
    T = _aes_init()
    sbox = T["sbox"]
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [sbox[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = _gmul(rcon, 2)
        w.append([a ^ b for a, b in zip(w[i - 4], t)])
    return [sum(w[4 * r:4 * r + 4], []) for r in range(11)]


def aes128_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    T = _aes_init()
    inv, m9, m11, m13, m14 = T["inv"], T["m9"], T["m11"], T["m13"], T["m14"]
    rk = _aes_round_keys(key)
    # InvShiftRows 위치표: new[r+4c] = old[r+4((c-r)%4)]
    shift = [r + 4 * ((c - r) % 4) for c in range(4) for r in range(4)]
    out = bytearray()
    for off in range(0, len(data) - 15, 16):
        s = [b ^ k for b, k in zip(data[off:off + 16], rk[10])]
        for rnd in range(9, -1, -1):
            s = [inv[s[j]] for j in shift]
            k = rk[rnd]
            s = [a ^ b for a, b in zip(s, k)]
            if rnd:
                t = []
                for c in range(0, 16, 4):
                    a0, a1, a2, a3 = s[c:c + 4]
                    t += [m14[a0] ^ m11[a1] ^ m13[a2] ^ m9[a3],
                          m9[a0] ^ m14[a1] ^ m11[a2] ^ m13[a3],
                          m13[a0] ^ m9[a1] ^ m14[a2] ^ m11[a3],
                          m11[a0] ^ m13[a1] ^ m9[a2] ^ m14[a3]]
                s = t
        out += bytes(s)
    return bytes(out)


def _distribute_key(payload: bytes) -> bytes:
    """배포용 문서 DISTRIBUTE_DOC_DATA(256바이트)에서 AES 키 16바이트를 꺼낸다."""
    seed = struct.unpack_from("<I", payload, 0)[0]
    state, dec, n, key = seed, bytearray(payload), 0, 0

    def rand():
        nonlocal state
        state = (state * 214013 + 2531011) & 0xFFFFFFFF
        return (state >> 16) & 0x7FFF

    for i in range(256):
        if n == 0:
            key = rand() & 0xFF
            n = (rand() & 0x0F) + 1
        if i >= 4:
            dec[i] ^= key
        n -= 1
    off = 4 + (seed & 0x0F)
    return bytes(dec[off:off + 16])


# ───────────────────────── HWP 5.0 레코드 ─────────────────────────
TAG_DISTRIBUTE = 28
TAG_PARA_HEADER, TAG_PARA_TEXT, TAG_CTRL_HEADER, TAG_LIST_HEADER = 66, 67, 71, 72
TAG_TABLE, TAG_SHAPE_PICTURE, TAG_EQEDIT = 77, 85, 88
EXT_CTRL = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}
INLINE_CTRL = {4, 5, 6, 7, 8, 9, 19, 20}


class Rec:
    __slots__ = ("tag", "level", "data", "children")

    def __init__(self, tag, level, data):
        self.tag, self.level, self.data, self.children = tag, level, data, []


def _records(data: bytes):
    root, stack, pos, n = Rec(-1, -1, b""), None, 0, len(data)
    stack = [root]
    while pos + 4 <= n:
        h = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        tag, lvl, size = h & 0x3FF, (h >> 10) & 0x3FF, h >> 20
        if size == 0xFFF:
            size = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        r = Rec(tag, lvl, data[pos:pos + size])
        pos += size
        while stack[-1].level >= lvl:
            stack.pop()
        stack[-1].children.append(r)
        stack.append(r)
    return root


MAX_STREAM = 256 * 1024 * 1024  # 압축 해제 상한 (압축 폭탄 방지)
MAX_ROWS, MAX_COLS = 2000, 200    # 표 크기 상한 (조작된 표 크기로 메모리 고갈 방지)


def _inflate(b: bytes) -> bytes:
    try:
        d = zlib.decompressobj(-15)
        out = d.decompress(b, MAX_STREAM)
        if d.unconsumed_tail:
            raise HwpError("본문이 너무 큽니다 (256MB 초과)")
        return out
    except zlib.error as e:
        raise HwpError(f"압축 해제 실패: {e}")


def _units(b: bytes):
    u = array("H")
    u.frombytes(b[:len(b) // 2 * 2])
    if sys.byteorder == "big":
        u.byteswap()
    return u


def _ustr(units) -> str:
    return array("H", units).tobytes().decode("utf-16le", "replace") if units else ""


def _parse_para_text(b: bytes):
    """→ [str | int(확장 컨트롤 순번 표식)]"""
    u, i, n, buf, segs = _units(b), 0, None, [], []
    n = len(u)
    while i < n:
        c = u[i]
        if c >= 32:
            buf.append(c)
            i += 1
        elif c in EXT_CTRL:
            if buf:
                segs.append(_ustr(buf))
                buf = []
            segs.append(c)  # int 표식
            i += 8
        elif c in INLINE_CTRL:
            if c == 9:
                buf.append(9)
            i += 8
        else:
            if c == 10:
                buf.append(10)
            elif c in (30, 31):
                buf.append(32)
            elif c == 24:
                buf.append(45)
            i += 1
    if buf:
        segs.append(_ustr(buf))
    return segs


class _Ctx:
    def __init__(self, headers):
        self.headers = headers
        self.footnotes = []
        self.header_blocks = []
        self.para_shapes = {}   # id -> (머리 종류 0없음/1개요/2번호/3글머리표, 수준 0~6, 번호·글머리표 id)
        self.numberings = {}    # id -> [(형식 문자열, 숫자 모양 코드, 시작 번호) × 7]
        self.bullets = {}       # id -> 글머리표 문자
        self.outline_id = 1
        self.counters = {}

    def prefix(self, shape_id):
        """문단 번호/글머리표 문자열 (없으면 '')."""
        kind, level, nid = self.para_shapes.get(shape_id, (0, 0, 0))
        if kind == 3:
            ch = self.bullets.get(nid, "")
            return ch + " " if ch and ch.isprintable() else ""
        if kind in (1, 2):
            if kind == 1:
                nid = self.outline_id
            levels = self.numberings.get(nid)
            if not levels:
                return ""
            level = min(level, len(levels) - 1)
            cnt = self.counters.setdefault(nid, [None] * len(levels))
            cnt[level] = (cnt[level] + 1) if cnt[level] is not None else levels[level][2]
            for j in range(level + 1, len(cnt)):
                cnt[j] = None
            fmt_str = levels[level][0]
            if not fmt_str:
                return ""

            def sub(m):
                k = int(m.group(1)) - 1
                if k >= len(levels):
                    return ""
                v = cnt[k] if cnt[k] is not None else levels[k][2]
                return _num_fmt(v, levels[k][1])
            return re.sub(r"\^(\d)", sub, fmt_str) + " "
        return ""


_HANGUL = "가나다라마바사아자차카타파하"
_JAMO = "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ"
_KNUM = "일이삼사오육칠팔구십"
_CNUM = "一二三四五六七八九十"
HWPX_NUMFMT = ["DIGIT", "CIRCLED_DIGIT", "ROMAN_CAPITAL", "ROMAN_SMALL", "LATIN_CAPITAL", "LATIN_SMALL",
               "CIRCLED_LATIN_CAPITAL", "CIRCLED_LATIN_SMALL", "HANGUL_SYLLABLE", "CIRCLED_HANGUL_SYLLABLE",
               "HANGUL_JAMO", "CIRCLED_HANGUL_JAMO", "HANGUL_PHONETIC", "IDEOGRAPH", "CIRCLED_IDEOGRAPH"]


def _roman(n):
    out = ""
    for v, s in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
                 (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= v:
            out, n = out + s, n - v
    return out


def _num_fmt(n, code):
    n = max(n, 1)
    i = n - 1
    if code == 1 and n <= 20:
        return chr(0x2460 + i)
    if code in (2, 3):
        r = _roman(n)
        return r if code == 2 else r.lower()
    if code in (4, 5):
        return chr((0x41 if code == 4 else 0x61) + i % 26)
    if code == 6:
        return chr(0x24B6 + i % 26)
    if code == 7:
        return chr(0x24D0 + i % 26)
    if code == 8:
        return _HANGUL[i % 14]
    if code == 9:
        return chr(0x326E + i % 14)
    if code == 10:
        return _JAMO[i % 14]
    if code == 11:
        return chr(0x3260 + i % 14)
    if code == 12 and n <= 10:
        return _KNUM[i]
    if code == 13 and n <= 10:
        return _CNUM[i]
    if code == 14 and n <= 10:
        return chr(0x3280 + i)
    return str(n)


def _load_docinfo(raw: bytes, ctx):
    """HWP 5.0 DocInfo에서 문단 모양·번호·글머리표 정의를 읽는다 (실패해도 본문 변환은 계속)."""
    nums, buls, shapes = [], [], []

    def walk(node):
        for r in node.children:
            if r.tag == 23:
                nums.append(r.data)
            elif r.tag == 24:
                buls.append(r.data)
            elif r.tag == 25:
                shapes.append(r.data)
            walk(r)
    walk(_records(raw))
    for i, d in enumerate(nums, 1):
        try:
            pos, lv = 0, []
            for _ in range(7):
                prop = struct.unpack_from("<I", d, pos)[0]
                pos += 12
                ln = struct.unpack_from("<H", d, pos)[0]
                pos += 2
                fmt = d[pos:pos + 2 * ln].decode("utf-16le", "replace")
                pos += 2 * ln
                lv.append([fmt, (prop >> 5) & 0xF, 1])
            start = struct.unpack_from("<H", d, pos)[0] if pos + 2 <= len(d) else 1
            pos += 2
            for k in range(7):
                lv[k][2] = struct.unpack_from("<I", d, pos + 4 * k)[0] if pos + 4 * k + 4 <= len(d) else start
                lv[k][2] = lv[k][2] if 0 < lv[k][2] < 10000 else 1
            ctx.numberings[i] = [tuple(x) for x in lv]
        except struct.error:
            pass
    for i, d in enumerate(buls, 1):
        if len(d) >= 14:
            ctx.bullets[i] = d[12:14].decode("utf-16le", "replace")
    for i, d in enumerate(shapes):
        if len(d) >= 32:
            p1 = struct.unpack_from("<I", d, 0)[0]
            ctx.para_shapes[i] = ((p1 >> 23) & 3, (p1 >> 25) & 7, struct.unpack_from("<H", d, 30)[0])


def _ctrl_id(rec):
    return struct.pack(">I", struct.unpack_from("<I", rec.data, 0)[0]).decode("latin-1") if len(rec.data) >= 4 else ""


def _collect_paras(node, ctx):
    """node 아래(재귀)의 모든 문단을 블록으로."""
    blocks = []
    for ch in node.children:
        if ch.tag == TAG_PARA_HEADER:
            blocks += _render_para(ch, ctx)
        elif ch.tag != TAG_CTRL_HEADER:
            blocks += _collect_paras(ch, ctx)
    return blocks


def _has_tag(node, tag):
    return any(ch.tag == tag or _has_tag(ch, tag) for ch in node.children)


def _blocks_text(blocks):
    out = []
    for b in blocks:
        if b["type"] == "p":
            out.append(b["text"])
        else:
            out.append(" / ".join(" | ".join(c for c in row if c) for row in b["cells"]))
    return "\n".join(t for t in out if t.strip())


def _render_table(ctrl, ctx):
    rows = cols = 0
    for ch in ctrl.children:
        if ch.tag == TAG_TABLE and len(ch.data) >= 8:
            rows, cols = struct.unpack_from("<HH", ch.data, 4)
            break
    cells, cur = [], None
    for ch in ctrl.children:
        if ch.tag == TAG_LIST_HEADER:
            cur = {"raw": ch.data, "blocks": []}
            cells.append(cur)
            if any(g.tag == TAG_PARA_HEADER for g in ch.children):
                cur["blocks"] += _collect_paras(ch, ctx)
        elif ch.tag == TAG_PARA_HEADER and cur is not None:
            cur["blocks"] += _render_para(ch, ctx)

    def addr(raw, off):
        if len(raw) < off + 8:
            return None
        return struct.unpack_from("<HHHH", raw, off)  # col, row, colspan, rowspan

    # 셀 속성 시작 위치는 문서마다 6 또는 8 바이트 → 맞는 쪽을 고른다
    best = 8
    for off in (8, 6):
        ok = all((a := addr(c["raw"], off)) and a[0] < max(cols, 1) and a[1] < max(rows, 1)
                 and a[2] >= 1 and a[3] >= 1 for c in cells)
        if ok:
            best = off
            break
    info = []
    for k, c in enumerate(cells):
        a = addr(c["raw"], best) or (k % max(cols, 1), k // max(cols, 1), 1, 1)
        info.append((a[1], a[0], a[3], a[2], c["blocks"]))
    return _table_or_unwrap(rows, cols, info)


def _table_or_unwrap(rows, cols, info):
    """info: (row, col, rowspan, colspan, blocks).
    안에 표가 들어 있는 '틀(네모 상자) 표'는 칸 내용을 순서대로 풀어 놓고,
    일반 표는 Markdown 표 한 개로 만든다."""
    if any(b["type"] == "table" for *_, blocks in info for b in blocks):
        out = []
        for *_, blocks in sorted(info, key=lambda x: (x[0], x[1])):
            out += blocks
        return out
    return _table_blocks(rows, cols, [(r, c, rs, cs, _blocks_text(b)) for r, c, rs, cs, b in info])


def _table_blocks(rows, cols, grid_cells):
    """grid_cells: (row, col, rowspan, colspan, text) → 블록 목록.
    - 가로 전체를 합친 제목 줄(예: '1. 토론의 의미')은 표 밖 문단으로 꺼내 표를 나눈다.
    - 빈 줄·빈 열(병합 때문에 생긴 칸)은 지운다."""
    if grid_cells:
        rows = max(rows, max(r + max(rs, 1) for r, _, rs, _, _ in grid_cells))
        cols = max(cols, max(c + max(cs, 1) for _, c, _, cs, _ in grid_cells))
    rows, cols = min(rows, MAX_ROWS), min(cols, MAX_COLS)
    grid = [[""] * cols for _ in range(rows)]
    full = {}
    for r, c, _, cs, t in grid_cells:
        if 0 <= r < rows and 0 <= c < cols:
            grid[r][c] = t
            if c == 0 and cs >= cols and cols > 1:
                full[r] = t
    out, chunk = [], []

    def emit():
        rows_ = [row for row in chunk if any(x.strip() for x in row)]
        if rows_:
            keep = [j for j in range(cols) if any(row[j].strip() for row in rows_)]
            g = [[row[j] for j in keep] for row in rows_]
            out.append({"type": "table", "rows": len(g), "cols": len(keep), "cells": g})
        chunk.clear()

    for r in range(rows):
        if r in full:
            emit()
            if full[r].strip():
                out.append({"type": "p", "text": full[r]})
        else:
            chunk.append(grid[r])
    emit()
    return out


def _render_ctrl(ctrl, ctx):
    """→ (inline_text or None, [blocks])"""
    cid = _ctrl_id(ctrl)
    if cid == "tbl ":
        return None, _render_table(ctrl, ctx)
    if cid == "gso ":
        inner = _collect_paras(ctrl, ctx)
        if inner:
            return None, inner
        return ("[그림]" if _has_tag(ctrl, TAG_SHAPE_PICTURE) else ""), []
    if cid == "eqed":
        for ch in ctrl.children:
            if ch.tag == TAG_EQEDIT and len(ch.data) >= 6:
                ln = struct.unpack_from("<H", ch.data, 4)[0]
                script = ch.data[6:6 + 2 * ln].decode("utf-16le", "replace").strip()
                return f"[수식: {script}]", []
        return "[수식]", []
    if cid in ("fn  ", "en  "):
        ctx.footnotes.append(_blocks_text(_collect_paras(ctrl, ctx)))
        return f"[^{len(ctx.footnotes)}]", []
    if cid in ("head", "foot"):
        if ctx.headers:
            t = _blocks_text(_collect_paras(ctrl, ctx))
            if t and t not in ctx.header_blocks:
                ctx.header_blocks.append(t)
        return "", []
    return "", []


def _render_para(para, ctx):
    text_rec = next((c for c in para.children if c.tag == TAG_PARA_TEXT), None)
    ctrls = [c for c in para.children if c.tag == TAG_CTRL_HEADER]
    segs = _parse_para_text(text_rec.data) if text_rec else []
    blocks, line, k = [], [], 0
    if len(para.data) >= 10 and any(isinstance(s, str) and s.strip() for s in segs):
        pre = ctx.prefix(struct.unpack_from("<H", para.data, 8)[0])
        if pre:
            line.append(pre)

    def flush():
        t = "".join(line)
        if t.strip():
            blocks.append({"type": "p", "text": t.rstrip()})
        line.clear()

    for s in segs:
        if isinstance(s, str):
            line.append(s)
            continue
        if k >= len(ctrls):
            continue
        inline, sub = _render_ctrl(ctrls[k], ctx)
        k += 1
        if sub:
            flush()
            blocks += sub
        elif inline:
            line.append(inline)
    for c in ctrls[k:]:  # 텍스트 표식 없이 남은 컨트롤
        inline, sub = _render_ctrl(c, ctx)
        if sub:
            flush()
            blocks += sub
        elif inline:
            line.append(inline)
    flush()
    return blocks


def _parse_hwp5(data: bytes, headers=False):
    cfb = CFB(data)
    fh = cfb.read("FileHeader")
    if not fh.startswith(b"HWP Document File"):
        raise HwpError("HWP 5.0 문서가 아닙니다")
    flags = struct.unpack_from("<I", fh, 36)[0]
    ver = struct.unpack_from("<I", fh, 32)[0]
    compressed, password, distribute = flags & 1, flags & 2, flags & 4
    if password:
        raise HwpError("암호가 걸린 문서입니다 (한글에서 암호를 풀고 다시 저장해 주세요)")

    def sections(prefix):
        names = [p for p in cfb.paths if re.fullmatch(prefix + r"/Section\d+", p)]
        return sorted(names, key=lambda p: int(p.rsplit("Section", 1)[1]))

    secs = sections("ViewText") if distribute else []
    use_view = bool(secs)
    if not secs:
        secs = sections("BodyText")
    if not secs:
        raise HwpError("본문 섹션을 찾지 못했습니다")
    ctx, blocks = _Ctx(headers), []
    try:
        di = cfb.read("DocInfo")
        _load_docinfo(_inflate(di) if compressed else di, ctx)
    except (HwpError, struct.error):
        pass
    for name in secs:
        raw = cfb.read(name)
        if use_view:
            h = struct.unpack_from("<I", raw, 0)[0]
            if h & 0x3FF != TAG_DISTRIBUTE:
                raise HwpError("배포용 문서 머리 레코드가 없습니다")
            if len(raw) > 64 * 1024 * 1024:
                raise HwpError("배포용 본문이 너무 큽니다")
            key = _distribute_key(raw[4:260])
            raw = aes128_ecb_decrypt(key, raw[260:])
        if compressed:
            raw = _inflate(raw)
        blocks += _collect_paras(_records(raw), ctx)
    v = f"{ver >> 24 & 255}.{ver >> 16 & 255}.{ver >> 8 & 255}.{ver & 255}"
    meta = {"format": "hwp", "version": v, "distribution": bool(distribute), "sections": len(secs)}
    return meta, blocks, ctx


# ───────────────────────── HWPX ─────────────────────────
def _ln(tag):
    return tag.rsplit("}", 1)[-1]


def _x_text(t):
    out = [t.text or ""]
    for sub in t:
        n = _ln(sub.tag)
        if n == "tab":
            out.append("\t")
        elif n == "lineBreak":
            out.append("\n")
        elif n in ("nbSpace", "fwSpace"):
            out.append(" ")
        elif n == "hyphen":
            out.append("-")
        out.append(sub.tail or "")
    return "".join(out)


def _x_sublist_blocks(elem, ctx):
    blocks = []
    for sl in elem.iter():
        if _ln(sl.tag) == "subList":
            for p in sl:
                if _ln(p.tag) == "p":
                    blocks += _x_para(p, ctx)
            break
    return blocks


def _x_table(tbl, ctx):
    rows = int(tbl.get("rowCnt", 0) or 0)
    cols = int(tbl.get("colCnt", 0) or 0)
    cells = []
    for tr in tbl:
        if _ln(tr.tag) != "tr":
            continue
        for tc in tr:
            if _ln(tc.tag) != "tc":
                continue
            r = c = None
            rs = cs = 1
            blocks = []
            for ch in tc:
                n = _ln(ch.tag)
                if n == "cellAddr":
                    c, r = int(ch.get("colAddr", 0)), int(ch.get("rowAddr", 0))
                elif n == "cellSpan":
                    cs, rs = int(ch.get("colSpan", 1)), int(ch.get("rowSpan", 1))
                elif n == "subList":
                    for p in ch:
                        if _ln(p.tag) == "p":
                            blocks += _x_para(p, ctx)
            cells.append((r if r is not None else len(cells) // max(cols, 1),
                          c if c is not None else len(cells) % max(cols, 1), rs, cs, blocks))
    return _table_or_unwrap(rows, cols, cells)


def _x_para(p, ctx):
    blocks, line = [], []

    def flush():
        t = "".join(line)
        if t.strip():
            blocks.append({"type": "p", "text": t.rstrip()})
        line.clear()

    has_text = any(_ln(t.tag) == "t" and "".join(t.itertext()).strip() for t in p.iter())
    if has_text and p.get("paraPrIDRef"):
        pre = ctx.prefix(int(p.get("paraPrIDRef")))
        if pre:
            line.append(pre)
    for run in p:
        if _ln(run.tag) != "run":
            continue
        for el in run:
            n = _ln(el.tag)
            if n == "t":
                line.append(_x_text(el))
            elif n == "tbl":
                flush()
                blocks += _x_table(el, ctx)
            elif n == "equation":
                sc = next((s.text for s in el.iter() if _ln(s.tag) == "script"), "") or ""
                line.append(f"[수식: {sc.strip()}]")
            elif n == "pic":
                line.append("[그림]")
            elif n == "ctrl":
                for c in el:
                    cn = _ln(c.tag)
                    if cn in ("footNote", "endNote"):
                        ctx.footnotes.append(_blocks_text(_x_sublist_blocks(c, ctx)))
                        line.append(f"[^{len(ctx.footnotes)}]")
                    elif cn in ("header", "footer") and ctx.headers:
                        t = _blocks_text(_x_sublist_blocks(c, ctx))
                        if t and t not in ctx.header_blocks:
                            ctx.header_blocks.append(t)
            elif n in ("secPr", "colPr", "linesegarray"):
                continue
            else:  # 글상자 등 도형 안 글
                inner = _x_sublist_blocks(el, ctx)
                if inner:
                    flush()
                    blocks += inner
    flush()
    return blocks


def _load_hwpx_header(root, ctx):
    kinds = {"OUTLINE": 1, "NUMBER": 2, "BULLET": 3}
    for el in root.iter():
        n = _ln(el.tag)
        if n == "paraPr":
            h = next((x for x in el.iter() if _ln(x.tag) == "heading"), None)
            if h is not None:
                ctx.para_shapes[int(el.get("id", -1))] = (kinds.get(h.get("type"), 0), int(h.get("level", 0) or 0),
                                                         int(h.get("idRef", 0) or 0))
        elif n == "numbering":
            lv = [("", 0, 1)] * 7
            for ph in el:
                if _ln(ph.tag) == "paraHead":
                    k = int(ph.get("level", 1)) - 1
                    if 0 <= k < 7:
                        code = HWPX_NUMFMT.index(ph.get("numFormat")) if ph.get("numFormat") in HWPX_NUMFMT else 0
                        lv[k] = (ph.text or "", code, int(ph.get("start", 1) or 1))
            ctx.numberings[int(el.get("id", 0))] = lv
        elif n == "bullet":
            ctx.bullets[int(el.get("id", 0))] = el.get("char", "")


def _parse_hwpx(data: bytes, headers=False):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HwpError("HWPX(zip) 파일이 손상되었습니다")
    names = zf.namelist()
    if "META-INF/manifest.xml" in names and b"encryption-data" in zf.read("META-INF/manifest.xml"):
        raise HwpError("암호화된 HWPX(배포용·암호 문서)입니다 — 한글 프로그램으로 열어야 합니다 (hwp_open_in_hangul / hwp_preview)")
    if any(i.file_size > MAX_STREAM for i in zf.infolist()):
        raise HwpError("HWPX 안의 파일이 너무 큽니다 (256MB 초과)")
    secs = sorted((n for n in names if re.fullmatch(r"Contents/section\d+\.xml", n)),
                  key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
    if not secs:
        raise HwpError("HWPX 본문(section*.xml)을 찾지 못했습니다")
    ctx, blocks = _Ctx(headers), []
    if "Contents/header.xml" in names:
        try:
            _load_hwpx_header(ET.fromstring(zf.read("Contents/header.xml")), ctx)
        except ET.ParseError:
            pass
    for s in secs:
        root = ET.fromstring(zf.read(s))
        for el in root.iter():
            if _ln(el.tag) == "secPr" and el.get("outlineShapeIDRef"):
                ctx.outline_id = int(el.get("outlineShapeIDRef"))
                break
        for p in root:
            if _ln(p.tag) == "p":
                blocks += _x_para(p, ctx)
    return {"format": "hwpx", "version": "", "distribution": False, "sections": len(secs)}, blocks, ctx


# ───────────────────────── 공개 API ─────────────────────────
def parse(src, headers=False):
    """src: 경로 또는 bytes → dict(meta, blocks, footnotes, headers)"""
    data = Path(src).read_bytes() if not isinstance(src, (bytes, bytearray)) else bytes(src)
    try:
        if data[:8] == CFB_MAGIC:
            meta, blocks, ctx = _parse_hwp5(data, headers)
        elif data[:4] == b"PK\x03\x04":
            meta, blocks, ctx = _parse_hwpx(data, headers)
        elif data[:17] == b"HWP Document File":
            raise HwpError("HWP 3.x 이하 옛 문서입니다 — 한글에서 열어 새 형식으로 저장해 주세요")
        else:
            raise HwpError("한글 문서(.hwp/.hwpx)가 아닙니다")
    except HwpError:
        raise
    except (struct.error, LookupError, ValueError, TypeError, RecursionError, MemoryError, OverflowError,
            NotImplementedError, RuntimeError, EOFError, zlib.error, zipfile.BadZipFile, ET.ParseError) as e:
        raise HwpError(f"문서가 손상되었거나 지원하지 않는 구조입니다 ({type(e).__name__})")
    return {"meta": meta, "blocks": blocks, "footnotes": ctx.footnotes, "headers": ctx.header_blocks}


def _md_cell(t):
    return t.replace("|", "\\|").replace("\n", "<br>").strip()


def to_markdown(doc):
    out = []
    for h in doc["headers"]:
        out.append("> [머리말/꼬리말] " + h.replace("\n", " "))
    for b in doc["blocks"]:
        if b["type"] == "p":
            out.append(b["text"])
            continue
        g = b["cells"]
        if not g or not g[0]:
            continue
        if len(g) == 1 and len(g[0]) == 1:  # 1칸짜리 표 = 상자
            out.append("\n".join("> " + ln for ln in g[0][0].split("\n")))
            continue
        lines = ["| " + " | ".join(_md_cell(c) for c in g[0]) + " |",
                 "|" + "---|" * len(g[0])]
        lines += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in g[1:]]
        out.append("\n".join(lines))
    for i, f in enumerate(doc["footnotes"], 1):
        out.append(f"[^{i}]: " + f.replace("\n", " "))
    return "\n\n".join(out) + "\n"


def to_text(doc):
    out = list(doc["headers"])
    for b in doc["blocks"]:
        if b["type"] == "p":
            out.append(b["text"])
        else:
            out.append("\n".join("\t".join(c.replace("\n", " ") for c in row) for row in b["cells"]))
    for i, f in enumerate(doc["footnotes"], 1):
        out.append(f"[{i}] {f}")
    return "\n".join(out) + "\n"


def convert(src, fmt="md", headers=False):
    doc = parse(src, headers)
    if fmt == "json":
        return json.dumps(doc, ensure_ascii=False, indent=1)
    return to_text(doc) if fmt == "txt" else to_markdown(doc)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="한글(.hwp/.hwpx) → Markdown/텍스트/JSON 빠른 변환기")
    ap.add_argument("files", nargs="+")
    ap.add_argument("-f", "--format", choices=["md", "txt", "json"], default="md")
    ap.add_argument("-o", "--out", help="결과를 저장할 폴더 (없으면 화면 출력)")
    ap.add_argument("--headers", action="store_true", help="머리말/꼬리말 포함")
    ap.add_argument("--time", action="store_true", help="걸린 시간 표시")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    rc = 0
    for f in a.files:
        t0 = time.perf_counter()
        try:
            res = convert(f, a.format, a.headers)
        except (HwpError, OSError) as e:
            print(f"[실패] {f}: {e}", file=sys.stderr)
            rc = 1
            continue
        dt = (time.perf_counter() - t0) * 1000
        if a.out:
            o = Path(a.out)
            o.mkdir(parents=True, exist_ok=True)
            dst = o / (Path(f).stem + "." + a.format)
            dst.write_text(res, encoding="utf-8")
            print(f"{f} -> {dst}" + (f"  ({dt:.0f} ms)" if a.time else ""))
        else:
            sys.stdout.write(res)
            if a.time:
                print(f"\n({dt:.0f} ms)", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
