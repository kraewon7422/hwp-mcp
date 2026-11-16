# hwp-mcp — Hangul (.hwp / .hwpx) documents for Claude

[![tests](https://github.com/kraewon7422/hwp-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/kraewon7422/hwp-mcp/actions/workflows/test.yml)
![license](https://img.shields.io/badge/license-MIT-blue)

An [MCP](https://modelcontextprotocol.io) server that lets **Claude Code, Claude Desktop and claude.ai** find, read, preview,
edit and create **Hancom Hangul (한글)** documents — the word-processor format used across Korean government, schools
and companies — plus a **claude.ai skill** for files uploaded straight into a chat.

The core is two dependency-free, single-file tools written from the HWP 5.0 / HWPX specifications:

| | What | Speed (measured) |
|---|---|---|
| `hwp_fast.py` | .hwp, .hwpx and distribution-protected (배포용) .hwp → Markdown / text / JSON | **~5 ms** per document |
| `hwpx_edit.py` | edit .hwpx without Hangul: replace text, fill form cells, insert paragraphs — formatting kept | **~10–50 ms** |

Everything Hangul itself is needed for (exact page images, editing binary .hwp, creating documents) goes through the
installed Hangul, which the server keeps warm in the background.

## Why

Claude can't open `.hwp` files natively. Converting them in a chat usually means installing an old library at runtime,
and tables, numbering and boxes come out mangled. I wanted Claude to read a Korean form in milliseconds,
see the page exactly as Hangul draws it, and fill it in — without breaking the formatting.

## Numbers

Measured on 45 real-world documents (forms, reports, worksheets, application sheets — not included in this repo):

| | hwp-mcp | Hangul via automation |
|---|---|---|
| Text accuracy vs. Hangul's own text export | **100 %** of Hangul's letters/digits present in all 45 | (reference) |
| Read one document | **~5 ms** | ~60 ms + ~4 s to start Hangul |
| Edit .hwpx (replace / fill / insert) | **10–50 ms**, verified on 20 real files: Hangul opens every result, counts match, nothing else lost | 4.5 s cold / 0.2 s warm |
| Preview page 1 | **~20 ms** (picture stored in the file) | ~1 s warm (exact render, any page) |

It also keeps what plain text extractors drop: tables as Markdown tables, paragraph numbering (1. 가. ①), bullets,
footnotes and equations — and it **unwraps layout boxes** (tables used only as frames around other tables), which
otherwise turn a worksheet into one unreadable line.

## Install (Windows)

1. Python 3.10+ ([python.org](https://www.python.org/downloads/), tick *Add python.exe to PATH*).
2. Download this repository (Code → Download ZIP) and unzip it somewhere permanent.
3. Double-click **`설치하기.bat`** (or run `.\setup.ps1`). It installs the Python packages, registers Hancom's
   **official automation security module** (downloaded from
   [hancom-io/devcenter-archive](https://github.com/hancom-io/devcenter-archive/tree/main/hwp-automation), SHA-256 checked —
   without it Hangul shows an "allow access?" dialog and automation hangs), runs the tests and adds the server to Claude Code.
   It also prints the snippet for Claude Desktop.

Hangul (Hancom Office) is optional: without it you can still read every format and edit .hwpx.

### claude.ai

claude.ai needs a public HTTPS URL. `.\setup.ps1 -Remote` starts the HTTP server in the background
(`127.0.0.1:8772/mcp`, scheduled task `HWPMCP`), creates a random secret path and prints the
[Tailscale Funnel](https://tailscale.com/kb/1223/funnel) command plus the URL to paste into
*claude.ai → Settings → Connectors*. **The secret path is the only key** — anyone with the URL can use the tools on the
folders below, so remote access is **read-only by default** (find/read/preview). Add `-AllowRemoteEdit` to allow editing;
`hwp_open_in_hangul` stays off remotely unless `HWP_MCP_REMOTE_OPEN=1`.

### claude.ai skill (files uploaded into a chat)

`python scripts/build_skill.py` → upload `dist/hwp-fast.zip` in *claude.ai → Settings → Capabilities → Skills*
(also attached to each [release](https://github.com/kraewon7422/hwp-mcp/releases)). Claude then reads uploaded
.hwp/.hwpx in under a second and can edit .hwpx — standard library only, nothing to install in the sandbox.

## Tools

| Tool | Does |
|---|---|
| `hwp_find` | find documents by file name (newest first) |
| `hwp_read` | Markdown or text, with offset paging for long documents |
| `hwp_preview` | pages as images, exactly as Hangul renders them |
| `hwp_open_in_hangul` | open the file in a normal Hangul window on the PC for the user to edit |
| `hwp_replace_text` | find & replace, formatting kept |
| `hwp_insert_text` | insert paragraphs at the start, end or after a given text |
| `hwp_fill_form` | `{"성명": "홍길동"}` → value goes into the cell right of (or below) the label |
| `hwp_create` | new document from simple Markdown (headings, paragraphs, tables) |

**Originals are never overwritten** — every edit goes to a new `<name>_수정본.hwp(x)` (`_수정본2`, … if taken). Only files this
server wrote itself, unchanged since, are edited further in place; an existing file is never replaced via `out_path`.

Ask Claude in plain language: *"다운로드에 있는 공문 요약해 줘"*, *"성명 칸에 홍길동 넣어 줘"*, *"2쪽 보여 줘"*.

## Safety

- Only `.hwp` / `.hwpx` files inside **Downloads, Desktop, Documents** (Windows' real locations, OneDrive included) and
  folders you add in `HWP_MCP_ROOTS` are accessible. Other file types, paths outside them, `..`, network (`\host\share`)
  paths and links/junctions leading out are refused — `hwp_open_in_hangul` only ever opens a Hangul document.
- Crafted files can't exhaust memory: decompression, table size, file size and page ranges are capped; malformed input
  ends in a clear error (fuzzed with 3,000 corrupted files).
- The hidden Hangul instance is tracked by its exact process id (from its window handle): a crashed or hung one is killed
  and restarted, one left behind by a killed server is cleaned up on the next start. Hangul windows the user opened are
  never touched.
- The HTTP server has no login of its own: it listens on 127.0.0.1 only and the tunnel's secret path is the key, so any
  program running on the PC itself could call it.
- The Hancom security module is registered for your Windows account (HKCU) and approves file access for **every** Hangul
  automation client on the PC, not only this server. `setup.ps1 -Uninstall` removes it.

## Limits

- **Encrypted / distribution .hwpx**: not readable (the encryption is undocumented; Hangul itself also refuses to export
  text or PDF from distribution documents). Distribution **.hwp** is readable — its scheme is in Hancom's published spec.
  Please respect the author's intent when you use text from distribution documents.
- **Password-protected** documents and **HWP 3.x** (1990s) files: open and re-save them in Hangul first.
- Editing binary **.hwp**, page previews beyond page 1, and `hwp_create` need Hangul on Windows. The server is Windows-only;
  `hwp_fast.py` / `hwpx_edit.py` run anywhere.
- Equations come out as Hangul equation script (e.g. `{a} over {b}`), pictures as `[그림]`.

## How it works

- **hwp_fast** — a small OLE/CFB reader → HWP 5.0 record tree (zlib) → paragraphs, controls, tables (cell addresses and
  spans), footnotes, equations; numbering and bullets are rebuilt from the DocInfo definitions. Distribution documents:
  the 256-byte header seeds MSVC `rand()` to recover the AES-128 key, then the ViewText streams are decrypted with a
  pure-Python AES (checked against the FIPS-197 vector). HWPX is parsed from its XML.
- **hwpx_edit** — scans the section XML once and records where every paragraph, run, text piece and table cell is,
  then splices only the changed text (matches can span runs, never other objects). It removes the line-layout cache
  (`linesegarray`) of touched paragraphs so Hangul re-flows them, and keeps the zip layout identical.
- **server** — FastMCP; one hidden Hangul driven from a single COM thread with a priority queue (user requests before
  background pre-rendering). See `.env.example` for settings.

## Development

```bash
python tests/run_tests.py                                    # 20 tests, no Hangul needed (CI: Linux + Windows)
python -X utf8 tools/check_server.py                         # every tool end-to-end (needs Hangul)
python -X utf8 tools/compare_with_hangul.py <folder>         # accuracy vs. Hangul's own text export
python -X utf8 tools/check_hwpx_edit.py <folder>             # edits verified by reopening in Hangul (works on copies)
python -X utf8 tools/make_samples.py                         # regenerate samples/ (scrubs the author name)
```

## License

MIT. The Hancom security module is not redistributed; `setup.ps1` downloads it from Hancom's repository.
"HWP" and "Hangul (한글)" are trademarks of Hancom Inc.; this project is not affiliated with Hancom.

---

# hwp-mcp — Claude가 한글 문서를 읽고, 보고, 고치게

**Claude Code · Claude Desktop · claude.ai**에서 한컴 **한글(.hwp/.hwpx)** 문서를 찾고, 읽고, 쪽 그림으로 미리보고,
고치고, 새로 만드는 MCP 서버입니다. 채팅에 바로 올린 파일을 위한 **claude.ai 스킬**도 들어 있습니다.

핵심은 HWP 5.0 / HWPX 규격을 보고 직접 만든, 설치할 것이 없는 한 파일짜리 도구 두 개입니다.

- `hwp_fast.py` — .hwp, .hwpx, 배포용 .hwp → Markdown/텍스트/JSON. 문서 하나 **약 0.005초**
- `hwpx_edit.py` — 한글 없이 .hwpx 고치기 (글자 바꾸기·양식 칸 채우기·문단 넣기), 서식 유지. **0.01~0.05초**

정확한 쪽 그림, .hwp 편집, 새 문서 만들기는 설치된 한글을 씁니다. 서버가 한글을 뒤에서 미리 켜 두기 때문에 편집 0.2초, 미리보기 1초 정도 걸립니다.

## 왜 만들었나
Claude는 .hwp를 직접 못 엽니다. 채팅에서 변환하려면 오래된 라이브러리를 그때그때 설치해야 해서 느리고, 표·번호·상자가 깨집니다.
한국 양식 문서를 순식간에 읽고, 한글 화면 그대로 보고, 서식을 깨지 않고 채우게 하고 싶었습니다.

## 측정 결과 (실제 문서 45개, 저장소에는 없음)
- 한글 프로그램이 뽑은 글자와 비교하면 45개 모두 **100% 포함**됩니다.
- 읽기는 약 5 ms입니다 (한글 자동화는 약 60 ms에 시작 시간 4초가 더 듭니다).
- .hwpx 편집: 실제 파일 20개를 고친 뒤 한글로 다시 열어 확인했습니다. 모두 열리고, 바꾼 개수가 맞고, 다른 글자는 하나도 사라지지 않았습니다.
- 1쪽 미리보기는 파일에 저장된 그림을 써서 약 0.02초입니다. 다른 쪽은 한글이 그려서 약 1초 걸립니다.
- 표는 Markdown 표로, 문단 번호(1. 가. ①)·글머리표·각주·수식은 그대로 살립니다. 표를 감싸는 **네모 틀은 풀어서** 보여 줍니다.

## 설치 (Windows)
1. Python 3.10 이상을 설치합니다 (설치할 때 *Add python.exe to PATH* 체크).
2. 이 저장소를 내려받아(Code → Download ZIP) 지우지 않을 폴더에 풉니다.
3. **`설치하기.bat`**을 더블클릭합니다. 다음을 자동으로 합니다.
   - 필요한 파이썬 패키지 설치
   - **한컴 공식 자동화 보안 모듈** 내려받기·지문(SHA-256) 확인·등록. 이 모듈이 없으면 한글이 '접근 허용' 창을 띄우고 멈춥니다.
   - 자체 시험 실행
   - Claude Code에 등록

한글이 없어도 읽기와 .hwpx 편집은 됩니다.

**claude.ai:** `.\setup.ps1 -Remote`를 실행하면 서버가 뒤에서 켜지고, Tailscale Funnel 명령과 claude.ai에 넣을 주소를 알려 줍니다.
**주소의 비밀 경로가 유일한 열쇠**이니 남에게 보여 주지 마세요. 원격은 기본이 읽기 전용이고, `-AllowRemoteEdit`를 붙이면 고치기도 됩니다.

**claude.ai 스킬:** `python scripts/build_skill.py`로 만든 `dist/hwp-fast.zip`을 claude.ai → 설정 → 기능 → 스킬에 올립니다 (릴리스에도 첨부돼 있어요).

## 이렇게 말하면 됩니다
"다운로드에 있는 공문 요약해 줘" · "2쪽 보여 줘" · "성명 칸에 홍길동, 부서 칸에 기획팀" ·
"2026을 2027로 다 바꿔 줘" · "회의록을 한글 파일로 만들어 줘" · "그 파일 한글로 열어 줘"

**원본은 절대 덮어쓰지 않습니다.** 결과는 항상 새 파일 `이름_수정본`(이미 있으면 `_수정본2` …)으로 저장되고, 이 서버가 만든 파일만 이어서 고칩니다.

## 안전
- 다운로드·바탕화면·문서(OneDrive 포함)와 `HWP_MCP_ROOTS`에 넣은 폴더의 **.hwp/.hwpx만** 다룹니다. 다른 파일, 다른 폴더, 네트워크 경로, 밖으로 이어지는 바로가기 폴더는 거절합니다.
- 조작된 파일로 메모리를 고갈시킬 수 없게 압축 해제·표 크기·파일 크기·쪽 범위에 상한을 두었습니다 (손상 파일 3,000개로 시험).
- 숨은 한글은 이 서버가 띄운 것만 정확한 프로세스 번호로 관리하고, 멈추면 다시 띄웁니다. 사용자가 연 한글 창은 건드리지 않습니다.
- 원격(claude.ai) 접근은 **기본이 읽기 전용**입니다. 고치기까지 허용하려면 `-AllowRemoteEdit`를 붙이세요.
- 한컴 보안 모듈은 내 Windows 계정에 등록되어, 이 서버뿐 아니라 PC의 **모든** 한글 자동화 프로그램의 파일 접근을 허용합니다. `setup.ps1 -Uninstall`로 지울 수 있습니다.

## 안 되는 것
- **암호화된 배포용 .hwpx**는 읽을 수 없습니다 (한글도 배포용 문서의 글자·PDF 추출을 막습니다). 배포용 **.hwp**는 한컴이 공개한 규격대로 읽습니다. 문서 작성자의 뜻을 존중해 사용해 주세요.
- 암호 문서와 HWP 3.x 문서는 한글에서 다시 저장한 뒤 쓰세요.
- .hwp 편집, 2쪽 이후 미리보기, 새 문서 만들기는 Windows의 한글이 필요합니다.

MIT 라이선스. 한컴 보안 모듈은 함께 배포하지 않고, 설치할 때 한컴 저장소에서 내려받습니다. "한글"과 "HWP"는 한컴의 상표이며, 이 프로젝트는 한컴과 관계없습니다.
