---
name: hwp-fast
description: Read Hangul documents (.hwp / .hwpx) in under a second and edit .hwpx without Hangul. Use this whenever the user uploads a .hwp or .hwpx file or asks to read, summarize, analyze, translate, extract tables from, or edit (replace text, fill in a form, add paragraphs) a Hangul/한글 document. Do NOT install pyhwp, hwp5txt, LibreOffice or olefile and do not write a parser — they are slow and break tables and numbering. 한글 파일(.hwp/.hwpx)을 올리거나 읽기·요약·표 추출·수정을 부탁하면 이 스킬을 먼저 쓴다.
---

# hwp-fast — Hangul documents, fast

Two single-file tools that need **only the Python standard library** (no pip install):

| Script | Does | Speed |
|---|---|---|
| `hwp_fast.py` | .hwp / .hwpx / distribution (배포용) .hwp → Markdown, text or JSON. Tables as Markdown tables, paragraph numbering, bullets, footnotes, equations (Hangul equation script) | ~10 ms |
| `hwpx_edit.py` | edit .hwpx: replace text, fill form cells, insert paragraphs — formatting preserved | ~50 ms |

`SKILL_DIR` below is the folder that contains this SKILL.md.

## Read
```bash
python3 SKILL_DIR/hwp_fast.py "/mnt/user-data/uploads/문서.hwp"            # Markdown to stdout
python3 SKILL_DIR/hwp_fast.py "/mnt/user-data/uploads/문서.hwpx" -f txt    # plain text
python3 SKILL_DIR/hwp_fast.py /mnt/user-data/uploads/*.hwp* -o /tmp/out   # many files → /tmp/out/*.md
```
Options: `-f md|txt|json`, `--headers` (include header/footer), `--time`.
Long documents: write with `-o` and read only the parts you need.

## Edit (.hwpx only — never overwrite the upload; write a new file to /mnt/user-data/outputs/)
```bash
python3 SKILL_DIR/hwpx_edit.py replace IN.hwpx OUT.hwpx --find 2026 --replace 2027
python3 SKILL_DIR/hwpx_edit.py fill    IN.hwpx OUT.hwpx --field 성명=홍길동 --field 부서=기획팀   # value goes right of the label; --below for the cell below
python3 SKILL_DIR/hwpx_edit.py insert  IN.hwpx OUT.hwpx --text "새 문단" --where end            # end | start | after:<text>
```
It prints a report, e.g. `{'replaced': 3, ...}` or `{'filled': ['성명'], 'missing': ['부서'], ...}`. Check it, then
verify with `hwp_fast.py OUT.hwpx` and give the user the output file.
`.hwp` (binary) cannot be edited here — ask the user to save it as .hwpx in Hangul (파일 → 다른 이름으로 저장 → HWPX),
or use the hwp-mcp server on their PC.

## When it can't
| Message | Meaning | What to do |
|---|---|---|
| 암호가 걸린 문서 | password-protected | ask the user to remove the password in Hangul and upload again |
| 암호화된 HWPX(배포용·암호) | distribution/encrypted .hwpx | not readable without Hangul — ask for a PDF export or a screenshot |
| HWP 3.x 이하 | 1990s format | ask the user to re-save it in Hangul |

- `[그림]` marks a picture; `[수식: …]` is Hangul equation script (e.g. `a over b` = a/b).
- Layout "boxes" (tables that only frame other tables) are unwrapped into normal paragraphs and tables.
