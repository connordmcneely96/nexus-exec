"""
Proof: self-render dimension annotations from FreeCAD's MEASURED data.

DIM_PROOF_VALUE_ONLY established that FreeCAD's App layer measures dimensions
(getDimValue -> 30.0) but viewPartAsSvg never DRAWS them (QGIViewDimension is
Gui-only; xvfb does not help). So we extract the measured edge endpoints + value
and render the dimension graphics OURSELVES as plain SVG, appended to the part
projection.

Phase 1 introspects the real FreeCAD 0.19 Python surface (printed, not assumed).
Phase 2 renders our own dimension group. Phase 3 hard-asserts it is really there.
Run via the /run endpoint after deploy — NOT in CI.
"""
import os
import re
import subprocess
import tempfile

from build123d import Box, Cylinder, export_step

OUT = "/work/out"
os.makedirs(OUT, exist_ok=True)

STEP_PATH   = os.path.join(OUT, "dim_part.step")
PLAIN_PATH  = os.path.join(OUT, "anno_plain.svg")
DIMMED_PATH = os.path.join(OUT, "anno_dimmed.svg")

# ── build123d part (same distinct-view solid as the proven scripts) ──────────
part = Box(30, 20, 10) - Cylinder(radius=4, height=20)
export_step(part, STEP_PATH)
assert os.path.getsize(STEP_PATH) > 0, "dim_part.step is empty"
print(f"STEP exported: {STEP_PATH}")

# ── FreeCAD script — token-replaced (NOT an f-string) to avoid brace clashes ─
FC_TEMPLATE = r'''import FreeCAD, Part, TechDraw
import glob, os, math, sys

# Template discovery — proven block.
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

doc = FreeCAD.newDocument("anno")
feat = doc.addObject("Part::Feature", "Model")
feat.Shape = Part.read("__STEP_PATH__")
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

# Add the dimension so the App layer measures it.
dim = doc.addObject("TechDraw::DrawViewDimension", "Dim1")
dim.Type = "DistanceX"
dim.References2D = [(view, "Edge1")]
page.addView(dim)
doc.recompute()
dim.X = 0
dim.Y = 25
doc.recompute()

# ── PHASE 1: INTROSPECT — print the real surface, guard everything ──────────
print("DIM_DIR:", [a for a in dir(dim) if not a.startswith("_")])
print("VIEW_DIR:", [a for a in dir(view) if not a.startswith("_")])

for meth in ("getLinearPoints", "getPointsOneEdge"):
    try:
        print("DIM_" + meth + ":", getattr(dim, meth)())
    except Exception as e:
        print("DIM_" + meth + "_ERR:", e)
for prop in ("LinearPoints",):
    try:
        print("DIM_" + prop + ":", getattr(dim, prop))
    except Exception as e:
        print("DIM_" + prop + "_ERR:", e)

def _xy(p):
    try:
        return (float(p.x), float(p.y))
    except Exception:
        return (float(p[0]), float(p[1]))

def _edge_ends(e):
    cand = []
    try:
        vs = e.Vertexes
        cand.append((vs[0].Point, vs[-1].Point))
    except Exception as ex:
        print("EDGE_Vertexes_ERR:", ex)
    try:
        cand.append((e.valueAt(e.FirstParameter), e.valueAt(e.LastParameter)))
    except Exception as ex:
        print("EDGE_valueAt_ERR:", ex)
    try:
        cand.append((e.firstVertex().Point, e.lastVertex().Point))
    except Exception as ex:
        print("EDGE_firstVertex_ERR:", ex)
    for a, b in cand:
        ax, ay = _xy(a)
        bx, by = _xy(b)
        if (ax, ay) != (bx, by):
            return (ax, ay), (bx, by)
    return None

edges = []
try:
    edges = view.getVisibleEdges()
    print("VISIBLE_EDGE_COUNT:", len(edges))
except Exception as e:
    print("getVisibleEdges_ERR:", e)

for idx in (0, 1):
    try:
        print("getEdgeByIndex(%d):" % idx, view.getEdgeByIndex(idx))
    except Exception as e:
        print("getEdgeByIndex(%d)_ERR:" % idx, e)

# Prefer the dimension's own points; fall back to the first visible edge.
p0 = p1 = None
try:
    lp = dim.getLinearPoints()
    a, b = lp[0], lp[1]
    ax, ay = _xy(a)
    bx, by = _xy(b)
    if (ax, ay) != (bx, by):
        p0, p1 = (ax, ay), (bx, by)
        print("POINTS_SOURCE: dim.getLinearPoints")
except Exception as e:
    print("dim_points_ERR:", e)

if p0 is None and edges:
    ends = _edge_ends(edges[0])
    if ends:
        p0, p1 = ends
        print("POINTS_SOURCE: getVisibleEdges[0]")

if p0 is None:
    print("ANNO_ERROR: no usable edge endpoints found")
    sys.exit(3)

val = float(dim.getDimValue())
val_str = "%.2f" % val

# ── PHASE 2: RENDER OUR OWN DIMENSION (pure geometry, no FreeCAD) ────────────
(x0, y0), (x1, y1) = p0, p1
dx, dy = x1 - x0, y1 - y0
L = math.hypot(dx, dy) or 1.0
ux, uy = dx / L, dy / L        # unit along the edge
nx, ny = -uy, ux              # unit perpendicular
OFF = 8.0                      # extension offset (mm)
AH = 2.0                       # arrowhead length (mm)

e0 = (x0 + nx * OFF, y0 + ny * OFF)
e1 = (x1 + nx * OFF, y1 + ny * OFF)

def _ln(a, b):
    return "<line x1='%.3f' y1='%.3f' x2='%.3f' y2='%.3f'/>" % (a[0], a[1], b[0], b[1])

def _arrow(tip, dux, duy):
    px, py = -duy, dux
    bx, by = tip[0] - dux * AH, tip[1] - duy * AH
    w = AH * 0.5
    c1 = (bx + px * w, by + py * w)
    c2 = (bx - px * w, by - py * w)
    return ("<polygon points='%.3f,%.3f %.3f,%.3f %.3f,%.3f' fill='black'/>"
            % (tip[0], tip[1], c1[0], c1[1], c2[0], c2[1]))

mx = (e0[0] + e1[0]) / 2.0 + nx * 3.5
my = (e0[1] + e1[1]) / 2.0 + ny * 3.5

group = (
    "<g stroke='black' stroke-width='0.3' fill='none'>"
    + _ln(p0, e0) + _ln(p1, e1) + _ln(e0, e1)
    + _arrow(e0, ux, uy) + _arrow(e1, -ux, -uy)
    + "<text x='%.3f' y='%.3f' font-size='3.5' fill='black' text-anchor='middle'>%s</text>"
      % (mx, my, val_str)
    + "</g>"
)

svg_body = TechDraw.viewPartAsSvg(view)

# Symmetric viewBox around origin sized to contain part + annotation.
_coords = [abs(v) for v in (x0, y0, x1, y1, e0[0], e0[1], e1[0], e1[1], mx, my)]
R = max(_coords) + 5.0
vb = "%.3f %.3f %.3f %.3f" % (-R, -R, 2 * R, 2 * R)
HDR = '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" viewBox="' + vb + '">\n'

open("__PLAIN_PATH__", "w").write(HDR + svg_body + "\n</svg>\n")
open("__DIMMED_PATH__", "w").write(HDR + svg_body + "\n" + group + "\n</svg>\n")

print("ANNO_P0:", "%.6f,%.6f" % (x0, y0))
print("ANNO_P1:", "%.6f,%.6f" % (x1, y1))
print("ANNO_VALUE_STR:", val_str)
print("ANNO_PTS_EQUAL:", p0 == p1)
print("ANNO_DONE")
'''

fc_script = (FC_TEMPLATE
             .replace("__STEP_PATH__", STEP_PATH)
             .replace("__PLAIN_PATH__", PLAIN_PATH)
             .replace("__DIMMED_PATH__", DIMMED_PATH))

with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
    f.write(fc_script)
    fc_tmp = f.name

try:
    result = subprocess.run(
        ["freecadcmd", fc_tmp],
        capture_output=True, text=True, timeout=300,
    )
finally:
    os.unlink(fc_tmp)


def _dump_and_raise(msg):
    print("=== freecadcmd stdout ===", flush=True)
    print(result.stdout)
    print("=== freecadcmd stderr ===", flush=True)
    print(result.stderr)
    raise RuntimeError(msg)


# Echo the introspection so it is visible even on success.
for line in result.stdout.splitlines():
    if any(k in line for k in (
        "TEMPLATES_FOUND", "TEMPLATE_USED", "DIM_DIR", "VIEW_DIR", "DIM_get",
        "DIM_Linear", "VISIBLE_EDGE_COUNT", "getEdgeByIndex", "POINTS_SOURCE",
        "ANNO_P0", "ANNO_P1", "ANNO_VALUE_STR", "ANNO_PTS_EQUAL",
    )):
        print("  " + line)

# ── PHASE 3: HARD ASSERTIONS ─────────────────────────────────────────────────
if result.returncode != 0 or "ANNO_DONE" not in result.stdout:
    _dump_and_raise(
        f"freecadcmd failed (rc={result.returncode}, "
        f"ANNO_DONE present={'ANNO_DONE' in result.stdout})"
    )

if not (os.path.exists(PLAIN_PATH) and os.path.getsize(PLAIN_PATH) > 0):
    _dump_and_raise("anno_plain.svg missing or empty")
if not (os.path.exists(DIMMED_PATH) and os.path.getsize(DIMMED_PATH) > 0):
    _dump_and_raise("anno_dimmed.svg missing or empty")

plain = open(PLAIN_PATH).read()
dimmed = open(DIMMED_PATH).read()

if not (len(dimmed) > len(plain)):
    _dump_and_raise(
        f"annotated SVG not larger than plain (plain={len(plain)}, dimmed={len(dimmed)})"
    )

if "<text" not in dimmed or "<polygon" not in dimmed:
    _dump_and_raise("annotation missing <text or <polygon in dimmed SVG")

m = re.search(r"ANNO_VALUE_STR:\s*(\S+)", result.stdout)
if not m:
    _dump_and_raise("ANNO_VALUE_STR not printed by FreeCAD script")
val_str = m.group(1)
if val_str not in dimmed:
    _dump_and_raise(f"formatted value {val_str!r} not present in dimmed SVG")

# Points must be distinct (degenerate p0==p1 = wrong edge grabbed).
p0m = re.search(r"ANNO_P0:\s*([-\d.]+),([-\d.]+)", result.stdout)
p1m = re.search(r"ANNO_P1:\s*([-\d.]+),([-\d.]+)", result.stdout)
if not (p0m and p1m):
    _dump_and_raise("ANNO_P0/ANNO_P1 not printed")
p0 = (float(p0m.group(1)), float(p0m.group(2)))
p1 = (float(p1m.group(1)), float(p1m.group(2)))
if p0 == p1:
    _dump_and_raise(f"degenerate points p0==p1=={p0} (wrong edge)")

print(f"\nvalue={val_str}  p0={p0}  p1={p1}  "
      f"plain_len={len(plain)}  dimmed_len={len(dimmed)}")
print("ANNOTATION_PROOF_OK")
