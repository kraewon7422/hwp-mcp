"""Build dist/hwp-fast.zip — the claude.ai skill (SKILL.md + hwp_fast.py + hwpx_edit.py).

Run: python scripts/build_skill.py
Upload the zip in claude.ai → Settings → Capabilities → Skills.
"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = {"hwp-fast/SKILL.md": ROOT / "skill" / "hwp-fast" / "SKILL.md",
         "hwp-fast/hwp_fast.py": ROOT / "hwp_fast.py",
         "hwp-fast/hwpx_edit.py": ROOT / "hwpx_edit.py"}

out = ROOT / "dist" / "hwp-fast.zip"
out.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for arc, src in FILES.items():
        z.write(src, arc)
print(out, [i.filename for i in zipfile.ZipFile(out).infolist()])
