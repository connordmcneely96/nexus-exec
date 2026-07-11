"""
Smoke test for the exec-toolchain: build123d, OpenSCAD, FreeCAD (TechDraw).
Run via the /run endpoint after deploy — NOT in CI.
Each step is isolated: one failure prints SMOKE_SKIP and continues.
All outputs land in /work/out/.
"""
import os
import subprocess
import sys
import tempfile

OUT = "/work/out"
os.makedirs(OUT, exist_ok=True)


def run_step(label, fn):
    try:
        fn()
        print(f"SMOKE_OK  {label}")
    except Exception as exc:
        print(f"SMOKE_SKIP {label}: {exc}", file=sys.stderr)


# ── Step 1: build123d — STEP + GLB ──────────────────────────────────────────
def step_build123d():
    from build123d import Box, export_step, export_gltf

    box = Box(10, 10, 10)
    step_path = os.path.join(OUT, "box.step")
    glb_path = os.path.join(OUT, "box.glb")
    export_step(box, step_path)
    export_gltf(box, glb_path)
    assert os.path.getsize(step_path) > 0, "STEP file is empty"
    assert os.path.getsize(glb_path) > 0, "GLB file is empty"


run_step("build123d box STEP+GLB", step_build123d)


# ── Step 2: OpenSCAD — render a .scad file to STL ───────────────────────────
def step_openscad():
    scad_src = "cube([10,10,10]);"
    with tempfile.NamedTemporaryFile(suffix=".scad", mode="w", delete=False) as f:
        f.write(scad_src)
        scad_path = f.name

    stl_path = os.path.join(OUT, "cube.stl")
    result = subprocess.run(
        ["openscad", "-o", stl_path, scad_path],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "openscad non-zero exit")
    assert os.path.getsize(stl_path) > 0, "STL file is empty"


run_step("OpenSCAD cube→STL", step_openscad)


# ── Step 3: FreeCAD TechDraw → SVG (per-view export; page-level is Gui-only) ─
def step_freecad():
    svg_path = os.path.join(OUT, "techdraw.svg")
    script = f"""import FreeCAD, Part, TechDraw
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

doc = FreeCAD.newDocument("smoke")
box = Part.makeBox(10, 10, 10)
feat = doc.addObject("Part::Feature", "Model")
feat.Shape = box
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
open("{svg_path}", "w").write(
    '<svg xmlns="http://www.w3.org/2000/svg" version="1.1">\\n' + svg + '\\n</svg>\\n'
)
print("freecad TechDraw OK")
"""
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(script)
        fc_path = f.name

    try:
        result = subprocess.run(
            ["freecadcmd", fc_path],
            capture_output=True, text=True, timeout=120,
        )
    finally:
        os.unlink(fc_path)

    if result.returncode != 0 or "freecad TechDraw OK" not in result.stdout:
        raise RuntimeError(
            f"freecadcmd failed (rc={result.returncode}): "
            + (result.stderr.strip() or result.stdout.strip() or "no output")
        )
    assert os.path.exists(svg_path) and os.path.getsize(svg_path) > 0, \
        f"techdraw.svg missing or empty"


run_step("FreeCAD TechDraw SVG", step_freecad)
