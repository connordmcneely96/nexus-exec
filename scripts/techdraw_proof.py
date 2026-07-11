"""
Headless TechDraw proof: Box solid -> orthographic SVG + DXF via per-view export.
Run via the /run endpoint after deploy — NOT in CI.

Key facts:
- FreeCAD is NOT importable from the venv; all FreeCAD work runs as a freecadcmd subprocess.
- Page-level exports (exportPageAsSvg / writeDXFPage) are Gui-only no-ops headless.
- Per-view exports (TechDraw.viewPartAsSvg / TechDraw.writeDXFView) work headless.
- FreeCAD 0.19 requires a Template set on the page before DrawViewPart will project.
"""
import os
import subprocess
import sys
import tempfile

from build123d import Box, export_step

OUT = "/work/out"
os.makedirs(OUT, exist_ok=True)

STEP_PATH = os.path.join(OUT, "proof.step")
SVG_PATH  = os.path.join(OUT, "drawing_front.svg")
DXF_PATH  = os.path.join(OUT, "drawing_front.dxf")

# ── Step 1: build123d solid → STEP ──────────────────────────────────────────
box = Box(20, 20, 20)
export_step(box, STEP_PATH)
assert os.path.getsize(STEP_PATH) > 0, "proof.step is empty"
print(f"STEP exported: {STEP_PATH}")

# ── Step 2: FreeCAD per-view TechDraw → SVG + DXF ───────────────────────────
FC_SCRIPT = f"""import FreeCAD, Part, TechDraw
import glob, os

# Locate a TechDraw SVG template (required by FreeCAD 0.19 before projection).
_dirs = [
    "/usr/share/freecad/Mod/TechDraw/Templates",
    "/usr/share/freecad/data/Mod/TechDraw/Templates",
    "/usr/lib/freecad/Mod/TechDraw/Templates",
    "/usr/lib/freecad-python3/Mod/TechDraw/Templates",
]
_tmpls = []
for d in _dirs:
    _tmpls += sorted(glob.glob(os.path.join(d, "*.svg")))
print("TEMPLATES_FOUND:", _tmpls[:10])
if not _tmpls:
    raise RuntimeError("no TechDraw SVG templates found on image")

def _pick(cands):
    for key in ("A4_Landscape_blank", "A3_Landscape_blank", "A4_Landscape",
                "A3_Landscape", "Landscape", "blank"):
        for t in cands:
            if key.lower() in os.path.basename(t).lower():
                return t
    return cands[0]

_tmpl_path = _pick(_tmpls)
print("TEMPLATE_USED:", _tmpl_path)

doc = FreeCAD.newDocument("dwg")
feat = doc.addObject("Part::Feature", "Model")
feat.Shape = Part.read("{STEP_PATH}")
page = doc.addObject("TechDraw::DrawPage", "Page")
template = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
template.Template = _tmpl_path
page.Template = template
view = doc.addObject("TechDraw::DrawViewPart", "ViewFront")
page.addView(view)
view.Source = [feat]
view.Direction = FreeCAD.Vector(0, -1, 0)
view.Scale = 1.0
doc.recompute()
svg = TechDraw.viewPartAsSvg(view)
open("{SVG_PATH}", "w").write(
    '<svg xmlns="http://www.w3.org/2000/svg" version="1.1">\\n' + svg + '\\n</svg>\\n'
)
TechDraw.writeDXFView(view, "{DXF_PATH}")
print("DRAW_OK")
"""

with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
    f.write(FC_SCRIPT)
    fc_tmp = f.name

try:
    result = subprocess.run(
        ["freecadcmd", fc_tmp],
        capture_output=True, text=True, timeout=180,
    )
finally:
    os.unlink(fc_tmp)

# ── Hard assertions — any failure raises, which surfaces to /run stdout ──────
if result.returncode != 0 or "DRAW_OK" not in result.stdout:
    print("=== freecadcmd stdout ===", flush=True)
    print(result.stdout)
    print("=== freecadcmd stderr ===", flush=True)
    print(result.stderr)
    raise RuntimeError(
        f"freecadcmd failed (rc={result.returncode}, DRAW_OK present="
        f"{'DRAW_OK' in result.stdout})"
    )

assert os.path.exists(SVG_PATH) and os.path.getsize(SVG_PATH) > 0, \
    f"SVG missing or empty: {SVG_PATH}"

svg_content = open(SVG_PATH).read()
assert any(marker in svg_content for marker in ("<path", "points", 'd="')), \
    f"SVG contains no real geometry (no <path/points/d= found): {SVG_PATH!r}"

assert os.path.exists(DXF_PATH) and os.path.getsize(DXF_PATH) > 0, \
    f"DXF missing or empty: {DXF_PATH}"

print("TECHDRAW_PROOF_OK")
