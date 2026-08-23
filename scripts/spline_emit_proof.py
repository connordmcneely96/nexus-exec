"""
Spline-emit capability proof (Option 1): emit HLR B-spline silhouette edges EXACTLY as
SVG cubic Beziers, and prove the emitted SVG — parsed back from text — reproduces the
OCP curve to 1e-6.

The multiview compose proof surfaced that HLR's FRONT view of the keyed shaft returns
FRONT_GEOM_TYPES ['BSPLINE','BSPLINE','LINE','LINE']. The exact-edge producer STOPs on
BSPLINE (the STOP-never-fake guard). Decision (Connor): emit splines exactly as SVG cubic
Beziers so every drawn line still traces to the solid. Option 2 (declared-tolerance
flattening) is the fallback ONLY if these curves prove rational or degree > 3 — which a
non-rational cubic cannot represent exactly.

This slice proves the capability in ISOLATION. It does NOT touch the producer
(edge_to_svg) — that wiring is the next slice. Standalone: imports only build123d, OCP,
and stdlib. Observe-then-assert. Runs via the deployed /run endpoint — NOT locally.
"""
import math
import os
import re
import subprocess
import sys

from build123d import Box, Cylinder, Pos

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.Geom import Geom_BSplineCurve, Geom_TrimmedCurve
from OCP.GeomConvert import GeomConvert_BSplineCurveToBezierCurve

OUT = "/work/out"
SVG_PATH = os.path.join(OUT, "spline_front.svg")
PDF_PATH = os.path.join(OUT, "spline_front.pdf")

CX, CY = 210.0, 148.5            # sheet transform, same idiom as the producer
A3_W_PT = 420.0 / 25.4 * 72.0    # 1190.551
A3_H_PT = 297.0 / 25.4 * 72.0    # 841.890
PAGE_TOL = 3.0
TOL = 1e-6

OBS = {}


def _fail(msg):
    print("=== SPLINE_EMIT diagnostics ===", flush=True)
    for k in sorted(OBS):
        print("  %s = %s" % (k, OBS[k]))
    print("SPLINE_EMIT_PROOF_FAIL:", msg, flush=True)
    sys.exit(1)


def _fmt(v):
    return format(v, ".10f")


def to_sheet(x, y):
    return (CX + x, CY - y)


def _geom_token(edge):
    for acc in (lambda: edge.geom_type, lambda: edge.geom_type()):
        try:
            return str(acc()).rsplit(".", 1)[-1].strip().upper()
        except Exception:
            continue
    return "?"


def _downcast(cls, handle):
    for name in ("DownCast", "DownCast_s"):
        f = getattr(cls, name, None)
        if f is None:
            continue
        try:
            r = f(handle)
            if r is not None:
                return r
        except Exception:
            continue
    return None


def _curve_and_range(topo_edge):
    """(Geom_Curve, first, last). OCP wraps the Standard_Real& outputs into the return
    tuple; some builds keep them as inputs — try both, guarded."""
    for call in (lambda: BRep_Tool.Curve_s(topo_edge, 0.0, 1.0),
                 lambda: BRep_Tool.Curve_s(topo_edge)):
        try:
            r = call()
        except Exception:
            continue
        if isinstance(r, tuple):
            if len(r) >= 3:
                return r[0], float(r[1]), float(r[2])
            if r:
                return r[0], None, None
        elif r is not None:
            return r, None, None
    return None, None, None


def _bspline_of(edge):
    """Recover the underlying Geom_BSplineCurve of a BSPLINE edge + its trim range."""
    curve, first, last = _curve_and_range(edge.wrapped)
    if curve is not None:
        tc = _downcast(Geom_TrimmedCurve, curve)
        if tc is not None:
            if first is None:
                try:
                    first, last = float(tc.FirstParameter()), float(tc.LastParameter())
                except Exception:
                    pass
            curve = tc.BasisCurve()
        bs = _downcast(Geom_BSplineCurve, curve)
        if bs is not None:
            if first is None or last is None:
                first, last = float(bs.FirstParameter()), float(bs.LastParameter())
            return bs, first, last
    # Fallback: BRepAdaptor route.
    try:
        ad = BRepAdaptor_Curve(edge.wrapped)
        bs = ad.BSpline()
        if bs is not None:
            return bs, float(ad.FirstParameter()), float(ad.LastParameter())
    except Exception as e:
        print("BSPLINE_FALLBACK_ERR:", repr(e))
    return None, None, None


def _pole_xyz(bez, j):
    p = bez.Pole(j)
    return (float(p.X()), float(p.Y()), float(p.Z()))


def _value_xyz(bez, t):
    p = bez.Value(t)
    return (float(p.X()), float(p.Y()), float(p.Z()))


def _elevate_to_cubic(poles):
    """Exact degree elevation to cubic (4 control points). poles: list of 2D tuples."""
    n = len(poles)
    if n == 4:
        return [tuple(p) for p in poles]
    if n == 3:
        p0, p1, p2 = poles
        return [tuple(p0),
                (p0[0] / 3 + 2 * p1[0] / 3, p0[1] / 3 + 2 * p1[1] / 3),
                (2 * p1[0] / 3 + p2[0] / 3, 2 * p1[1] / 3 + p2[1] / 3),
                tuple(p2)]
    if n == 2:
        p0, p1 = poles
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        return [tuple(p0),
                (p0[0] + dx / 3, p0[1] + dy / 3),
                (p0[0] + 2 * dx / 3, p0[1] + 2 * dy / 3),
                tuple(p1)]
    raise ValueError("bezier arc has %d poles (degree %d > 3)" % (n, n - 1))


def _cubic_eval(cp, t):
    mt = 1.0 - t
    b0, b1, b2, b3 = mt ** 3, 3 * mt * mt * t, 3 * mt * t * t, t ** 3
    return (b0 * cp[0][0] + b1 * cp[1][0] + b2 * cp[2][0] + b3 * cp[3][0],
            b0 * cp[0][1] + b1 * cp[1][1] + b2 * cp[2][1] + b3 * cp[3][1])


def _parse_cubics(svg_text):
    """READBACK: parse only the emitted STRING; return each 'C' arc as (P0,P1,P2,P3) in
    MODEL space (inverting the sheet transform). Shares no variable with the emit path."""
    def to_model(sx, sy):
        return (sx - CX, CY - sy)

    numre = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
    arcs = []
    for d in re.findall(r'\bd\s*=\s*"([^"]*)"', svg_text):
        cur = None
        for letter, argstr in re.findall(r'([MLCZmlcz])([^MLCZmlcz]*)', d):
            nums = [float(x) for x in re.findall(numre, argstr)]
            u = letter.upper()
            if u in ("M", "L"):
                for k in range(0, len(nums) - 1, 2):
                    cur = to_model(nums[k], nums[k + 1])
            elif u == "C":
                for k in range(0, len(nums) - 5, 6):
                    p1 = to_model(nums[k], nums[k + 1])
                    p2 = to_model(nums[k + 2], nums[k + 3])
                    p3 = to_model(nums[k + 4], nums[k + 5])
                    arcs.append((cur, p1, p2, p3))
                    cur = p3
    return arcs


# ── Step 1: part + FRONT projection, select BSPLINE edges ────────────────────
os.makedirs(OUT, exist_ok=True)
part = Cylinder(radius=12, height=60) - Pos(12, 0, 0) * Box(6, 4, 60)
try:
    res = part.project_to_viewport((0, 100, 0), (0, 0, 1), (0, 0, 0))  # EXPLICIT look_at
    visible = list(res[0])
except Exception as e:
    _fail("project_to_viewport(FRONT) raised: %r" % (e,))

print("PROJECT_CALL: project_to_viewport((0,100,0),(0,0,1),(0,0,0))  [FRONT, explicit look_at]")
front_types = [_geom_token(e) for e in visible]
print("FRONT_GEOM_TYPES:", front_types)
bspline_edges = [e for e in visible if _geom_token(e) == "BSPLINE"]
OBS["BSPLINE_EDGE_COUNT"] = len(bspline_edges)
print("BSPLINE_EDGE_COUNT:", len(bspline_edges))

# ── Step 2+3: introspect each spline; convert to Bezier arcs; introspect arcs ─
edges_data = []          # per edge: {"arcs":[bez...], "degree","nbpoles","rational"}
any_rational = False
max_arc_degree = 0
for ei, e in enumerate(bspline_edges):
    bs, first, last = _bspline_of(e)
    if bs is None:
        _fail("edge %d: could not recover Geom_BSplineCurve" % ei)
    try:
        deg = int(bs.Degree())
        nbp = int(bs.NbPoles())
        rat = bool(bs.IsRational())
    except Exception as ex:
        _fail("edge %d: bspline introspection failed: %r" % (ei, ex))
    print("EDGE[%d] SPLINE_DEGREE=%d SPLINE_NB_POLES=%d SPLINE_IS_RATIONAL=%s"
          % (ei, deg, nbp, rat))
    any_rational = any_rational or rat
    try:
        conv = GeomConvert_BSplineCurveToBezierCurve(bs, first, last, 1e-9)
    except Exception:
        try:
            conv = GeomConvert_BSplineCurveToBezierCurve(bs)
            print("EDGE[%d] NOTE: ranged conversion unavailable, converted full curve" % ei)
        except Exception as ex:
            _fail("edge %d: GeomConvert failed: %r" % (ei, ex))
    nb = int(conv.NbArcs())
    arcs = []
    arc_specs = []
    for i in range(1, nb + 1):
        bez = conv.Arc(i)
        adeg = int(bez.Degree())
        arat = bool(bez.IsRational())
        arcs.append(bez)
        arc_specs.append((adeg, arat))
        any_rational = any_rational or arat
        max_arc_degree = max(max_arc_degree, adeg)
    print("EDGE[%d] NB_BEZIER_ARCS=%d  arcs(degree,rational)=%s" % (ei, nb, arc_specs))
    edges_data.append({"arcs": arcs, "degree": deg, "nbpoles": nbp, "rational": rat})

OBS["ANY_RATIONAL"] = any_rational
OBS["MAX_ARC_DEGREE"] = max_arc_degree

# ── Step 4: THE FORK — do not fake it ────────────────────────────────────────
if OBS["BSPLINE_EDGE_COUNT"] >= 1 and (any_rational or max_arc_degree > 3):
    print("OPTION1_IMPOSSIBLE: rational=%s max_degree=%s"
          % (any_rational, max_arc_degree))
    print("Exact non-rational cubic emission cannot represent this curve "
          "(rational and/or degree>3). Option 2 (declared-tolerance flattening) required.")
    sys.exit(1)

# ── choose the two in-plane (view-plane) axes: drop the flattened axis (min range).
#    For FRONT this yields (world x, world z) == the brief's (x, z), robust to whether HLR
#    flattens into world coords or a local viewport frame.
all_poles_3d = []
for ed in edges_data:
    for bez in ed["arcs"]:
        for j in range(1, int(bez.NbPoles()) + 1):
            all_poles_3d.append(_pole_xyz(bez, j))
if len(all_poles_3d) < 2:
    _fail("no spline poles collected")
rng = [max(p[i] for p in all_poles_3d) - min(p[i] for p in all_poles_3d) for i in range(3)]
drop = rng.index(min(rng))
keep = [i for i in range(3) if i != drop]
OBS["AXIS_RANGES"] = [round(r, 6) for r in rng]
OBS["KEPT_AXES"] = keep
print("VIEW_PLANE axis ranges=%s  dropped_axis=%d  kept=%s (FRONT expects x + z)"
      % (OBS["AXIS_RANGES"], drop, keep))


def to2d(xyz):
    return (xyz[keep[0]], xyz[keep[1]])


# ── Step 5: EXACT CUBIC EMISSION ─────────────────────────────────────────────
emitted_arcs = []        # flat, in emission order: (cubic_poles_2d, bez)
d_subpaths = []
for ed in edges_data:
    parts = None
    for bez in ed["arcs"]:
        poles2d = [to2d(_pole_xyz(bez, j)) for j in range(1, int(bez.NbPoles()) + 1)]
        cubic = _elevate_to_cubic(poles2d)
        emitted_arcs.append((cubic, bez))
        s = [to_sheet(*cp) for cp in cubic]
        if parts is None:                       # start the subpath at the first pole
            parts = ["M %s %s" % (_fmt(s[0][0]), _fmt(s[0][1]))]
        parts.append("C %s %s %s %s %s %s"
                      % (_fmt(s[1][0]), _fmt(s[1][1]), _fmt(s[2][0]), _fmt(s[2][1]),
                         _fmt(s[3][0]), _fmt(s[3][1])))
    if parts:
        d_subpaths.append(" ".join(parts))

svg_text = ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            'width="420mm" height="297mm" viewBox="0 0 420 297">\n'
            '<path d="%s" fill="none" stroke="black" stroke-width="0.35"/>\n'
            '</svg>\n' % " ".join(d_subpaths))
with open(SVG_PATH, "w") as f:
    f.write(svg_text)
r = subprocess.run(["rsvg-convert", "-f", "pdf", "-o", PDF_PATH, SVG_PATH],
                   capture_output=True, text=True, timeout=120)
if r.returncode != 0:
    _fail("rsvg-convert failed: %s" % r.stderr)

# ── Step 6: READBACK — parse the emitted STRING and compare to OCP ───────────
parsed_arcs = _parse_cubics(svg_text)
if len(parsed_arcs) != len(emitted_arcs):
    _fail("parsed %d cubic arcs but emitted %d" % (len(parsed_arcs), len(emitted_arcs)))

ctrl_max = 0.0
sample_max = 0.0
ts = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
for (cubic, bez), parsed in zip(emitted_arcs, parsed_arcs):
    # (i) control points: parsed (from string) vs OCP cubic poles (post-elevation)
    for a, b in zip(parsed, cubic):
        ctrl_max = max(ctrl_max, math.hypot(a[0] - b[0], a[1] - b[1]))
    # (ii) sample: reconstructed cubic (parsed control pts) vs OCP arc value, view-plane
    for t in ts:
        rx, ry = _cubic_eval(parsed, t)
        ox, oy = to2d(_value_xyz(bez, t))
        sample_max = max(sample_max, math.hypot(rx - ox, ry - oy))

OBS["CTRLPT_MAX_DELTA"] = ctrl_max
OBS["SAMPLE_MAX_DELTA"] = sample_max

pinfo = subprocess.run(["pdfinfo", PDF_PATH], capture_output=True, text=True)
m = re.search(r"Page size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts", pinfo.stdout)
if not m:
    _fail("could not read page size:\n%s" % pinfo.stdout)
pw, ph = float(m.group(1)), float(m.group(2))
OBS["PAGE_PT"] = "%.2f x %.2f" % (pw, ph)
OBS["PDF_BYTES"] = os.path.getsize(PDF_PATH)

# ── Step 7: OBSERVATIONS ─────────────────────────────────────────────────────
for k in ("BSPLINE_EDGE_COUNT", "ANY_RATIONAL", "MAX_ARC_DEGREE", "KEPT_AXES",
          "CTRLPT_MAX_DELTA", "SAMPLE_MAX_DELTA", "PAGE_PT", "PDF_BYTES"):
    print("%s: %r" % (k, OBS[k]))

# ── Step 8: HARD ASSERTIONS ──────────────────────────────────────────────────
if OBS["BSPLINE_EDGE_COUNT"] < 1:
    _fail("no BSPLINE edges to test (BSPLINE_EDGE_COUNT=0)")
if any_rational or max_arc_degree > 3:
    _fail("rational or degree>3 slipped past the fork (rational=%s max_degree=%s)"
          % (any_rational, max_arc_degree))
if ctrl_max >= TOL:
    _fail("CTRLPT_MAX_DELTA=%r not < 1e-6" % ctrl_max)
if sample_max >= TOL:
    _fail("SAMPLE_MAX_DELTA=%r not < 1e-6 (drawn curve != OCP silhouette)" % sample_max)
if not os.path.exists(PDF_PATH):
    _fail("spline_front.pdf missing")
if abs(pw - A3_W_PT) > PAGE_TOL or abs(ph - A3_H_PT) > PAGE_TOL:
    _fail("PAGE_PT %.2f x %.2f not A3 landscape %.2f x %.2f (+/-%.1f)"
          % (pw, ph, A3_W_PT, A3_H_PT, PAGE_TOL))

print("value_summary: bsplines=%d arcs=%d ctrl_delta=%.2e sample_delta=%.2e page=%s"
      % (OBS["BSPLINE_EDGE_COUNT"], len(emitted_arcs), ctrl_max, sample_max, OBS["PAGE_PT"]))
print("SPLINE_EMIT_PROOF_OK")
