"""
Diagnostic: can TechDraw dimensions be RENDERED headless?

Runs one FreeCAD dimension script twice — under bare `freecadcmd` and under
`xvfb-run` — and compares the projected SVG before vs. after adding a DistanceX
dimension. Dimensions are drawn by QGIViewDimension (a Gui/Qt class); the
question is whether a real X display (xvfb) makes viewPartAsSvg include the
dimension geometry, or whether only the App layer computes the value while the
SVG stays byte-identical (meaning we must draw dimensions ourselves).

This is a PROOF, not a gate: a clean negative is the answer we want, so a
negative verdict does NOT raise.
Run via the /run endpoint after deploy — NOT in CI.
"""
import os
import re
import subprocess
import tempfile

from build123d import Box, Cylinder, export_step

OUT = "/work/out"
os.makedirs(OUT, exist_ok=True)

STEP_PATH = os.path.join(OUT, "dim_part.step")

# ── Step 1: build123d part (same distinct-view solid as multiview_proof) ─────
part = Box(30, 20, 10) - Cylinder(radius=4, height=20)
export_step(part, STEP_PATH)
assert os.path.getsize(STEP_PATH) > 0, "dim_part.step is empty"
print(f"STEP exported: {STEP_PATH}")


# ── FreeCAD script builder (label baked in so each run writes distinct SVGs) ─
def make_fc_script(label):
    before_path = os.path.join(OUT, f"dim_before_{label}.svg")
    after_path = os.path.join(OUT, f"dim_after_{label}.svg")
    # f-string: only {STEP_PATH}, {before_path}, {after_path} interpolate.
    # No other braces appear in the FreeCAD body.
    return f"""import FreeCAD, Part, TechDraw
import glob, os

# Template discovery — proven block from multiview_proof.py.
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

doc = FreeCAD.newDocument("dim")
feat = doc.addObject("Part::Feature", "Model")
feat.Shape = Part.read("{STEP_PATH}")
page = doc.addObject("TechDraw::DrawPage", "Page")
template = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
template.Template = _tmpl_path
page.Template = template
view = doc.addObject("TechDraw::DrawViewPart", "View_front")
page.addView(view)
view.Source = [feat]
view.Direction = FreeCAD.Vector(0, -1, 0)
view.Scale = 1.0
doc.recompute()

# (a) enumerate what the projected view exposes
try:
    if hasattr(view, "getVisibleEdges"):
        print("EDGE_COUNT:", len(view.getVisibleEdges()))
    else:
        print("EDGE_COUNT:", "n/a")
except Exception as e:
    print("EDGE_COUNT_ERR:", e)
print("HAS_getEdgeByIndex:", hasattr(view, "getEdgeByIndex"))

# (b) baseline SVG
svg_a = TechDraw.viewPartAsSvg(view)
open("{before_path}", "w").write(svg_a)

# (c) add a DistanceX dimension referencing Edge1 (1-indexed)
dim = doc.addObject("TechDraw::DrawViewDimension", "Dim1")
dim.Type = "DistanceX"
dim.References2D = [(view, "Edge1")]
page.addView(dim)
doc.recompute()
dim.X = 0
dim.Y = 25
doc.recompute()

for attr in ("getDimValue", "getDimString"):
    try:
        print("DIM_VALUE_" + attr + ":", getattr(dim, attr)())
    except Exception as e:
        print("DIM_VALUE_" + attr + "_ERR:", e)

# (d) SVG after adding the dimension
svg_b = TechDraw.viewPartAsSvg(view)
open("{after_path}", "w").write(svg_b)

# (e) report difference
print("SVG_DIFFERS:", svg_a != svg_b, "len_a:", len(svg_a), "len_b:", len(svg_b))
print("FC_DONE")
"""


def run_freecad(label, cmd_prefix):
    """Run the label's FreeCAD script; return a dict of parsed results."""
    script = make_fc_script(label)
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(script)
        tmp = f.name
    try:
        result = subprocess.run(
            cmd_prefix + [tmp],
            capture_output=True, text=True, timeout=300,
        )
    finally:
        os.unlink(tmp)

    before_path = os.path.join(OUT, f"dim_before_{label}.svg")
    after_path = os.path.join(OUT, f"dim_after_{label}.svg")
    before = _read(before_path)
    after = _read(after_path)

    # Did the App layer compute a numeric dimension value?
    value_computed = bool(
        re.search(r"DIM_VALUE_getDim(?:Value|String):\s*\S", result.stdout)
    )

    return {
        "label": label,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "before_len": len(before) if before is not None else None,
        "after_len": len(after) if after is not None else None,
        "svg_differs": (before is not None and after is not None and before != after),
        "svg_larger": (
            before is not None and after is not None and len(after) > len(before)
        ),
        "value_computed": value_computed,
    }


def _read(path):
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read()
    return None


def _echo(r):
    print(f"\n=== RUN {r['label']} (rc={r['returncode']}) ===")
    for line in r["stdout"].splitlines():
        if any(k in line for k in (
            "EDGE_COUNT", "HAS_getEdgeByIndex", "DIM_VALUE_",
            "SVG_DIFFERS", "TEMPLATES_FOUND", "TEMPLATE_USED",
        )):
            print("  " + line)
    print(f"  before_len={r['before_len']} after_len={r['after_len']} "
          f"differs={r['svg_differs']} larger={r['svg_larger']} "
          f"value_computed={r['value_computed']}")
    if r["returncode"] != 0:
        print("  --- stderr ---")
        print("  " + (r["stderr"].strip() or "(empty)"))


# ── Step 2/3: run twice and compare ──────────────────────────────────────────
bare = run_freecad("BARE", ["freecadcmd"])
xvfb = run_freecad("XVFB", ["xvfb-run", "-a", "freecadcmd"])

_echo(bare)
_echo(xvfb)

# ── Step 4: verdict (exactly one; negatives do NOT raise) ────────────────────
value_computed = bare["value_computed"] or xvfb["value_computed"]
neither_changed = (bare["svg_differs"] is False) and (xvfb["svg_differs"] is False)

print()
if xvfb["svg_differs"] and xvfb["svg_larger"]:
    print("DIM_PROOF_XVFB_RENDERS")
elif value_computed and neither_changed:
    print("DIM_PROOF_VALUE_ONLY")
else:
    reasons = []
    if bare["returncode"] != 0:
        reasons.append(f"BARE rc={bare['returncode']}")
    if xvfb["returncode"] != 0:
        reasons.append(f"XVFB rc={xvfb['returncode']}")
    if not value_computed:
        reasons.append("no DIM_VALUE computed by App layer")
    if bare["before_len"] is None or xvfb["before_len"] is None:
        reasons.append("SVG output missing for a run")
    if xvfb["svg_differs"] and not xvfb["svg_larger"]:
        reasons.append("XVFB SVG changed but did not grow")
    print("DIM_PROOF_FAILED: " + ("; ".join(reasons) or "inconclusive"))
