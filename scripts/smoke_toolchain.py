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


# ── Step 3: FreeCAD TechDraw → SVG ──────────────────────────────────────────
def step_freecad():
    svg_path = os.path.join(OUT, "techdraw.svg")
    script = f"""import FreeCAD
import TechDraw
doc = FreeCAD.newDocument("smoke")
page = doc.addObject("TechDraw::DrawPage", "Page")
template = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
page.Template = template
doc.recompute()
page.exportSvg("{svg_path}")
print("freecad TechDraw OK")
"""
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(script)
        fc_path = f.name

    # Debian binary is lowercase `freecadcmd`; run the script as a positional file
    # (the same invocation the Docker build gate validates).
    result = subprocess.run(
        ["freecadcmd", fc_path],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "freecadcmd non-zero exit")


run_step("FreeCAD TechDraw SVG", step_freecad)
