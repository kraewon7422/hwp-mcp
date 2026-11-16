"""Automated tests for hwp_fast and hwpx_edit. Standard library only, no Hangul needed (runs on Linux CI).

Run: python tests/run_tests.py
"""
import io
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
sys.path.insert(0, str(ROOT))
import hwp_fast  # noqa: E402
import hwpx_edit  # noqa: E402

PLAIN_MD = """배포용 시험 문서입니다. 가나다 ABC 123.

둘째 문단: 특수문자 ① ※ ▶ ☐ 〇 — “따옴표”

| 항목 | 값 |
|---|---|
| 속도 | 빠름 |

마지막 문단 끝.
"""

results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, None))
    except Exception as e:  # noqa: BLE001
        results.append((fn.__name__, f"{type(e).__name__}: {e}"))
    return fn


def tmpdir():
    return Path(tempfile.mkdtemp(prefix="hwpmcp_test_"))


# ───────── hwp_fast ─────────
@test
def aes_fips197_vector():
    k = bytes(range(16))
    assert hwp_fast.aes128_ecb_decrypt(k, bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")).hex() == \
        "00112233445566778899aabbccddeeff"


@test
def hwp_binary_to_markdown():
    assert hwp_fast.convert(SAMPLES / "plain.hwp") == PLAIN_MD


@test
def hwpx_to_markdown():
    assert hwp_fast.convert(SAMPLES / "plain.hwpx") == PLAIN_MD


@test
def distribution_hwp_decrypts():
    doc = hwp_fast.parse(SAMPLES / "dist.hwp")
    assert doc["meta"]["distribution"] is True
    assert hwp_fast.to_markdown(doc) == PLAIN_MD


@test
def encrypted_hwpx_clear_error():
    try:
        hwp_fast.parse(SAMPLES / "dist_secured.hwpx")
    except hwp_fast.HwpError as e:
        assert "암호화된 HWPX" in str(e)
    else:
        raise AssertionError("no error")


@test
def not_a_hangul_file():
    for data in (b"hello world", b"PK\x03\x04garbage"):
        try:
            hwp_fast.parse(data)
        except hwp_fast.HwpError:
            pass
        else:
            raise AssertionError("no error")


@test
def text_and_json_formats():
    txt = hwp_fast.convert(SAMPLES / "plain.hwp", "txt")
    assert "항목\t값" in txt and "속도\t빠름" in txt
    import json
    d = json.loads(hwp_fast.convert(SAMPLES / "plain.hwpx", "json"))
    assert [b["type"] for b in d["blocks"]] == ["p", "p", "table", "p"]


@test
def number_formats():
    f = hwp_fast._num_fmt
    assert [f(3, 0), f(3, 1), f(4, 2), f(4, 3), f(2, 4), f(2, 5), f(2, 8), f(3, 10), f(2, 12)] == \
        ["3", "③", "IV", "iv", "B", "b", "나", "ㄷ", "이"]


@test
def layout_box_tables_unwrap():
    inner = {"type": "table", "rows": 1, "cols": 2, "cells": [["a", "b"]]}
    # a cell that contains a table → the outer table is a layout box: contents come out in order
    out = hwp_fast._table_or_unwrap(2, 1, [(0, 0, 1, 1, [{"type": "p", "text": "제목"}]), (1, 0, 1, 1, [inner])])
    assert out == [{"type": "p", "text": "제목"}, inner]


@test
def full_width_rows_become_headings_and_empty_columns_drop():
    cells = [(0, 0, 1, 3, "1. 제목"), (1, 0, 1, 1, "가"), (1, 1, 1, 1, ""), (1, 2, 1, 1, "나"),
             (2, 0, 1, 1, "다"), (2, 2, 1, 1, "라")]
    out = hwp_fast._table_blocks(3, 3, cells)
    assert out[0] == {"type": "p", "text": "1. 제목"}
    assert out[1]["cells"] == [["가", "나"], ["다", "라"]]


@test
def cli_converts_to_folder():
    d = tmpdir()
    r = subprocess.run([sys.executable, str(ROOT / "hwp_fast.py"), str(SAMPLES / "plain.hwp"), "-o", str(d)],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert (d / "plain.md").read_text(encoding="utf-8") == PLAIN_MD


# ───────── hwpx_edit ─────────
def edited(ops, src=SAMPLES / "plain.hwpx"):
    d = tmpdir()
    out = d / "out.hwpx"
    rep = hwpx_edit.edit(src, out, ops)
    return rep, hwp_fast.convert(out), out


@test
def edit_replace():
    rep, md, _ = edited([("replace", "문단", "PARA")])
    assert rep["replaced"] == 2 and md.count("PARA") == 2 and "문단" not in md


@test
def edit_replace_across_runs():
    # split '가나다' into two runs with different char shapes, as Hangul does after a formatting change
    d = tmpdir()
    src = d / "split.hwpx"
    zin = zipfile.ZipFile(SAMPLES / "plain.hwpx")
    with zipfile.ZipFile(src, "w") as zout:
        for info in zin.infolist():
            body = zin.read(info.filename)
            if info.filename == "Contents/section0.xml":
                body = body.decode().replace("가나다 ABC", '가나</hp:t></hp:run><hp:run charPrIDRef="0"><hp:t>다 ABC', 1).encode()
            zout.writestr(info, body)
    rep, md, _ = edited([("replace", "가나다", "XYZ")], src)
    assert rep["replaced"] == 1 and "XYZ ABC" in md and "가나" not in md


@test
def edit_does_not_match_across_objects():
    # '값' and '속도' are in different table cells → '값속도' must not match
    rep, md, _ = edited([("replace", "값속도", "X")])
    assert rep["replaced"] == 0


@test
def edit_fill_right_and_below():
    rep, md, _ = edited([("fill", {"항목": "새 값", "속도": "두 줄\n값"}, "right")])
    assert rep["filled"] == ["항목", "속도"] and rep["old_values"] == {"항목": "값", "속도": "빠름"}
    assert "| 항목 | 새 값 |" in md and "| 속도 | 두 줄<br>값 |" in md
    rep, md, _ = edited([("fill", {"항목": "아래"}, "below")])  # the cell under '항목' held '속도'
    assert "| 항목 | 값 |" in md and "| 아래 | 빠름 |" in md and rep["old_values"] == {"항목": "속도"}


@test
def edit_fill_missing_label():
    rep, _, _ = edited([("fill", {"없는 칸": "x"}, "right")])
    assert rep["missing"] == ["없는 칸"] and not rep["filled"]


@test
def edit_insert_positions():
    rep, md, out = edited([("insert", "처음", "start"), ("insert", "끝1\n끝2", "end"), ("insert", "둘째 다음", "after:둘째 문단")])
    order = [md.find(t) for t in ("처음", "배포용 시험", "둘째 문단", "둘째 다음", "| 항목", "마지막", "끝1", "끝2")]
    assert all(i >= 0 for i in order) and order == sorted(order), order
    # the section settings must stay in the (new) first paragraph exactly once
    xml = zipfile.ZipFile(out).read("Contents/section0.xml").decode()
    assert xml.count("<hp:secPr") == 1 and xml.index("<hp:secPr") < xml.index("처음") < xml.index("배포용 시험")


@test
def edit_keeps_zip_layout_and_valid_xml():
    _, _, out = edited([("replace", "빠름", "느림 & <특수>")])
    zi, zo = zipfile.ZipFile(SAMPLES / "plain.hwpx"), zipfile.ZipFile(out)
    assert [i.filename for i in zi.infolist()] == [i.filename for i in zo.infolist()]
    assert zo.infolist()[0].filename == "mimetype" and zo.infolist()[0].compress_type == zipfile.ZIP_STORED
    import xml.etree.ElementTree as ET
    ET.fromstring(zo.read("Contents/section0.xml"))
    assert "느림 & <특수>" in hwp_fast.convert(out)
    assert "느림" in zo.read("Preview/PrvText.txt").decode("utf-8")


@test
def edit_refuses_bad_input():
    for src, dst in ((SAMPLES / "plain.hwpx", SAMPLES / "plain.hwpx"), (SAMPLES / "dist_secured.hwpx", tmpdir() / "x.hwpx")):
        try:
            hwpx_edit.edit(src, dst, [("replace", "a", "b")])
        except hwpx_edit.HwpxEditError:
            pass
        else:
            raise AssertionError("no error")


@test
def edit_cli():
    d = tmpdir()
    r = subprocess.run([sys.executable, str(ROOT / "hwpx_edit.py"), "fill", str(SAMPLES / "plain.hwpx"), str(d / "o.hwpx"),
                        "--field", "항목=CLI"], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "| 항목 | CLI |" in hwp_fast.convert(d / "o.hwpx")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    bad = [r for r in results if r[1]]
    for name, err in results:
        print(("ok   " if not err else "FAIL ") + name + (f"\n     {err}" if err else ""))
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    sys.exit(1 if bad else 0)
