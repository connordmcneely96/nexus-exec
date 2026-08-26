"""
Orthographic view PRODUCER: emit a real orthographic view as SVG from EXACT projected
edges, and prove the emitted SVG — parsed back from text — still measures the true
geometry.

The HLR investigation (hlr_projection_proof.py FAILED loud; hlr_diagnostic.py
CONCLUSIVE) established that build123d's project_to_viewport is exact and headless and
returns real CIRCLE/LINE edges (no splines) — but ONLY with an EXPLICIT look_at. The
default look_at targets the bounding-box centre, which on an asymmetric part shifts the
frame AND explodes the visible set (13 edges vs the correct 4). Two hard rules carried
here:
  1. ALWAYS pass explicit look_at.
  2. RENDER FROM EXACT EDGES — never discretise geometry you intend to draw. CIRCLE ->
     SVG arc 'A', LINE -> SVG 'L'. Chord sampling injected the 0.168mm / 0.0055mm errors
     the diagnostic chased; there is no discretisation anywhere in this producer.

This closes the section_render debt where "rendered radius" was an identity round-trip
(to_model(to_sheet(x)) == x, reading the source). Here the readback parses the ACTUAL
emitted SVG STRING and inverts the sheet transform — nothing from the source edges.

Pure functions with no module-level side effects; the __main__ block runs the proof via
the deployed /run endpoint. A later composition slice imports these functions.
"""
import math
import os
import re
import subprocess
import sys

from build123d import Box, Cylinder, Pos

# OCP is always present (build123d is OCP-backed). Used only by the BSPLINE branch of
# edge_to_svg to recover the exact spline geometry (ported from spline_emit_proof.py).
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.Geom import Geom_BSplineCurve, Geom_TrimmedCurve
from OCP.GeomConvert import GeomConvert_BSplineCurveToBezierCurve

# ── sheet: A3 landscape, physical mm; model origin -> sheet centre, 1:1, y-flip ──
SHEET = {"w": 420, "h": 297, "cx": 210.0, "cy": 148.5}
A3_W_PT = 420.0 / 25.4 * 72.0   # 1190.551
A3_H_PT = 297.0 / 25.4 * 72.0   # 841.890
PAGE_TOL = 3.0

# Explicit look_at is mandatory (rule 1). Each view fixes origin/up/look_at=origin.
VIEWS = {
    "TOP":   {"origin": (0, 0, 100), "up": (0, 1, 0), "look_at": (0, 0, 0)},
    "FRONT": {"origin": (0, 100, 0), "up": (0, 0, 1), "look_at": (0, 0, 0)},
}


# ── small helpers ─────────────────────────────────────────────────────────────
def _xy(v):
    return (float(v.X), float(v.Y))


def _geom_type(edge):
    for acc in (lambda: edge.geom_type, lambda: edge.geom_type()):
        try:
            return str(acc())
        except Exception:
            continue
    return "?"


def _norm_geom(gt):
    # Exact terminal-token match, NOT substring. "LINE" is a substring of BSPLINE /
    # SPLINE / POLYLINE, so a substring test mis-classifies those as LINE and lets them
    # slip past edge_to_svg's STOP-never-fake guard. Take the token after the last '.'
    # ("GeomType.BSPLINE" -> "BSPLINE") and match it exactly; anything that is not
    # LINE/CIRCLE returns raw so edge_to_svg's `raise ValueError` fires.
    token = gt.rsplit(".", 1)[-1].strip().upper()
    if token == "CIRCLE":
        return "CIRCLE"
    if token == "LINE":
        return "LINE"
    return token


def _fmt(v):
    # High precision: the 1e-6 readback tolerance requires ~1e-10 coordinate fidelity.
    # %.4f (what section_render used) would inject ~4e-5 radius error at the sqrt(140)
    # corner and fail the exact-readback assertion.
    return format(v, ".10f")


def make_transform(sheet=SHEET):
    """model (x,y) -> sheet (sx,sy): translate origin to centre, 1:1 scale, y flipped."""
    cx, cy = sheet["cx"], sheet["cy"]

    def to_sheet(x, y):
        return (cx + x, cy - y)

    return to_sheet


def _arc_flags(sx, sy, ex, ey, cx, cy, mx, my):
    """large_arc/sweep from the REAL centre, endpoints, and one interior point, under the
    sheet y-flip. Model-space angles; sweep inverts because y is flipped on the sheet.
    (Same logic section_render_proof.py used to earn ARC_MODE: exact.)"""
    a_s = math.atan2(sy - cy, sx - cx)
    a_e = math.atan2(ey - cy, ex - cx)
    a_m = math.atan2(my - cy, mx - cx)
    two = 2 * math.pi
    d_m = (a_m - a_s) % two
    d_e = (a_e - a_s) % two
    if d_m <= d_e:                    # interior point on the CCW arc S->E
        ccw, swept = True, d_e
    else:
        ccw, swept = False, two - d_e
    large = 1 if swept > math.pi else 0
    # y-flip INVERTS the traversal sense: a model-CCW arc, with y negated onto the sheet,
    # is drawn clockwise (SVG sweep=1 = increasing-angle = clockwise in y-down). So the
    # SVG sweep flag is the OPPOSITE of the model ccw sense. The prior `1 if ccw else 0`
    # was inverted — it drew the rim bulging the wrong way (its endpoints still sat at
    # radius 12, so the radius-only readback in orthographic_render never caught it; the
    # arc-aware multiview alignment readback is what exposed it, verified by rendering:
    # sweep=1 gives the true 198..222mm disk, sweep=0 bulges to 245mm).
    sweep = 0 if ccw else 1
    return large, sweep


# ── the four required functions ──────────────────────────────────────────────
def project_view(part, view_name):
    """(visible_edges, hidden_edges) with an EXPLICIT look_at (rule 1)."""
    v = VIEWS[view_name]
    res = part.project_to_viewport(v["origin"], v["up"], v["look_at"])
    return list(res[0]), list(res[1])


# ── OCP B-spline helpers — ported verbatim from spline_emit_proof.py (proven green:
#    SAMPLE_MAX_DELTA 4.59e-11 vs OCP Value(t)). Do not re-derive. ────────────────
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
    try:                                     # fallback: BRepAdaptor route
        ad = BRepAdaptor_Curve(edge.wrapped)
        bs = ad.BSpline()
        if bs is not None:
            return bs, float(ad.FirstParameter()), float(ad.LastParameter())
    except Exception as e:
        print("BSPLINE_FALLBACK_ERR:", repr(e))
    return None, None, None


def _pole_xy(bez, j):
    """Pole j of a Bezier arc, in the view plane. Same convention _xy uses for LINE/CIRCLE
    endpoints: the flattened frame's in-plane axes are (.X, .Y) (the third is ~0)."""
    p = bez.Pole(j)
    return (float(p.X()), float(p.Y()))


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


def _bspline_bezier_arcs(edge):
    """Recover a BSPLINE edge as (arcs, is_rational, max_degree). Raises if the spline
    cannot be recovered. arcs are Geom_BezierCurve segments over the edge's trim range."""
    bs, first, last = _bspline_of(edge)
    if bs is None:
        raise ValueError("BSPLINE edge: could not recover Geom_BSplineCurve")
    rational = bool(bs.IsRational())
    try:
        conv = GeomConvert_BSplineCurveToBezierCurve(bs, first, last, 1e-9)
    except Exception:
        conv = GeomConvert_BSplineCurveToBezierCurve(bs)
    arcs = [conv.Arc(i) for i in range(1, int(conv.NbArcs()) + 1)]
    for b in arcs:
        rational = rational or bool(b.IsRational())
    max_deg = max((int(b.Degree()) for b in arcs), default=0)
    return arcs, rational, max_deg


def edge_emit_mode(edge):
    """The mode edge_to_svg will use: 'line' | 'arc' | 'polyline' | 'cubic'. Raises on the
    same STOP conditions as edge_to_svg (unsupported type, rational spline, degree>3), so a
    caller can classify/observe without disagreeing with what edge_to_svg does."""
    gt = _norm_geom(_geom_type(edge))
    if gt == "LINE":
        return "line"
    if gt == "CIRCLE":
        return "arc"
    if gt == "BSPLINE":
        _arcs, rational, max_deg = _bspline_bezier_arcs(edge)
        if rational or max_deg > 3:
            raise ValueError("BSPLINE edge is rational=%s / max_degree=%d — Option-2 case; "
                             "STOP" % (rational, max_deg))
        return "polyline" if max_deg == 1 else "cubic"
    raise ValueError("unsupported geom_type %r (no discretization fallback in producer)"
                     % gt)


def edge_to_svg(edge, to_sheet):
    """Exact geometry, NO chord discretisation. LINE -> 'M..L..'; CIRCLE -> 'M..A..';
    non-rational BSPLINE (degree <= 3) -> its exact Bezier arcs: degree-1 as a polyline of
    'L' segments (a degree-1 spline IS a polyline — emit one, keeping the path data honest
    about a straight edge wearing a BSPLINE tag), degree 2/3 as cubic 'C' arcs via exact
    degree elevation (the spline_emit_proof path).
    Raises -> the producer STOPS (never fakes): a CIRCLE lacking radius/arc_center, a
    RATIONAL spline, or any arc degree > 3 (the Option-2 case, not yet decided)."""
    gt = _norm_geom(_geom_type(edge))
    if gt == "LINE":
        s0 = to_sheet(*_xy(edge @ 0.0))
        s1 = to_sheet(*_xy(edge @ 1.0))
        return "M %s %s L %s %s" % (_fmt(s0[0]), _fmt(s0[1]), _fmt(s1[0]), _fmt(s1[1]))
    if gt == "CIRCLE":
        p0 = _xy(edge @ 0.0)
        p1 = _xy(edge @ 1.0)
        s0 = to_sheet(*p0)
        s1 = to_sheet(*p1)
        r = float(edge.radius)              # raises -> producer STOPS (no fallback)
        c = _xy(edge.arc_center)            # raises -> producer STOPS
        mid = _xy(edge @ 0.5)
        large, sweep = _arc_flags(p0[0], p0[1], p1[0], p1[1], c[0], c[1], mid[0], mid[1])
        return ("M %s %s A %s %s 0 %d %d %s %s"
                % (_fmt(s0[0]), _fmt(s0[1]), _fmt(r), _fmt(r), large, sweep,
                   _fmt(s1[0]), _fmt(s1[1])))
    if gt == "BSPLINE":
        arcs, rational, max_deg = _bspline_bezier_arcs(edge)
        if rational or max_deg > 3:
            raise ValueError("BSPLINE edge is rational=%s / max_degree=%d — exact "
                             "non-rational cubic cannot represent it (Option-2 case); STOP"
                             % (rational, max_deg))
        parts = None
        if max_deg == 1:                     # polyline: pole sequence IS the vertex chain
            for b in arcs:
                s0 = to_sheet(*_pole_xy(b, 1))
                s1 = to_sheet(*_pole_xy(b, 2))
                if parts is None:
                    parts = ["M %s %s" % (_fmt(s0[0]), _fmt(s0[1]))]
                parts.append("L %s %s" % (_fmt(s1[0]), _fmt(s1[1])))
        else:                                # degree 2/3 -> one cubic C per arc
            for b in arcs:
                poles = [_pole_xy(b, j) for j in range(1, int(b.NbPoles()) + 1)]
                cubic = _elevate_to_cubic(poles)
                s = [to_sheet(*cp) for cp in cubic]
                if parts is None:
                    parts = ["M %s %s" % (_fmt(s[0][0]), _fmt(s[0][1]))]
                parts.append("C %s %s %s %s %s %s"
                             % (_fmt(s[1][0]), _fmt(s[1][1]), _fmt(s[2][0]), _fmt(s[2][1]),
                                _fmt(s[3][0]), _fmt(s[3][1])))
        if not parts:
            raise ValueError("BSPLINE edge produced no Bezier arcs")
        return " ".join(parts)
    raise ValueError("unsupported geom_type %r (no discretization fallback in producer)"
                     % gt)


def view_path_d(edges, to_sheet):
    """The 'd' attribute for one view: each edge's exact command, space-joined. Exposed so
    the multiview composer can place several views (each with its own transform) into ONE
    sheet without duplicating emission logic."""
    return " ".join(edge_to_svg(e, to_sheet) for e in edges)


def emit_view_svg(edges, to_sheet, sheet=SHEET):
    """Full A3-landscape <svg>, one <path> concatenating each edge's exact command."""
    return ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            'width="%dmm" height="%dmm" viewBox="0 0 %d %d">\n'
            '<path d="%s" fill="none" stroke="black" stroke-width="0.35"/>\n'
            '</svg>\n' % (sheet["w"], sheet["h"], sheet["w"], sheet["h"],
                          view_path_d(edges, to_sheet)))


def parse_svg_extremes(svg_text, sheet=SHEET):
    """THE READBACK. Parse the emitted SVG STRING only: pull every path command's
    coordinates and every 'A' command's radius, invert the sheet transform back to model
    space, and return min/max endpoint radius + the emitted arc radii.

    Independence: this reads ONLY svg_text and the known sheet constants (cx, cy). It
    shares no variable with the emit path — not `edges`, not the pre-transform model
    points. The round-trip is proven through the emitted text, closing the
    section_render identity-round-trip debt.
    """
    cx, cy = sheet["cx"], sheet["cy"]
    parts = [parse_path_extremes(d, cx, cy)
             for d in re.findall(r'\bd\s*=\s*"([^"]*)"', svg_text)]
    parts = [p for p in parts if p["endpoint_count"] > 0]
    if not parts:
        return {"min_radius": None, "max_radius": None, "arc_radii": [],
                "min_x": None, "max_x": None, "endpoint_count": 0}
    return {
        "min_radius": min(p["min_radius"] for p in parts),
        "max_radius": max(p["max_radius"] for p in parts),
        "arc_radii": [a for p in parts for a in p["arc_radii"]],
        "min_x": min(p["min_x"] for p in parts),
        "max_x": max(p["max_x"] for p in parts),
        "endpoint_count": sum(p["endpoint_count"] for p in parts),
    }


def _arc_extreme_points(x0, y0, x1, y1, rx, ry, large, sweep):
    """SVG elliptical-arc (x-axis-rotation 0) -> the two endpoints PLUS the arc's
    axis-extreme points (cardinal parameter angles) that fall within the swept range.
    Endpoints alone under-measure a major arc: the rim's leftmost point (-12,0) is arc
    INTERIOR, not an endpoint, so a purely endpoint readback would miss the true X bound
    and break the multiview alignment check. Recovers the centre from the emitted string
    (SVG 1.1 Appendix F.6.5/F.6.6) — no source knowledge."""
    pts = [(x0, y0), (x1, y1)]
    if rx <= 0 or ry <= 0:
        return pts
    x1p = (x0 - x1) / 2.0
    y1p = (y0 - y1) / 2.0
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1.0:
        s = math.sqrt(lam)
        rx *= s
        ry *= s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = 0.0 if den == 0 else math.sqrt(max(0.0, num / den))
    if large == sweep:
        coef = -coef
    cxp = coef * (rx * y1p / ry)
    cyp = coef * (-(ry * x1p / rx))
    cx_a = cxp + (x0 + x1) / 2.0
    cy_a = cyp + (y0 + y1) / 2.0

    def _angle(ux, uy, vx, vy):
        ln = math.hypot(ux, uy) * math.hypot(vx, vy)
        c = 1.0 if ln == 0 else max(-1.0, min(1.0, (ux * vx + uy * vy) / ln))
        a = math.acos(c)
        return -a if (ux * vy - uy * vx) < 0 else a

    ux0, uy0 = (x1p - cxp) / rx, (y1p - cyp) / ry
    ux1, uy1 = (-x1p - cxp) / rx, (-y1p - cyp) / ry
    theta0 = _angle(1.0, 0.0, ux0, uy0)
    dtheta = _angle(ux0, uy0, ux1, uy1)
    if not sweep and dtheta > 0:
        dtheta -= 2.0 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2.0 * math.pi
    lo, hi = min(theta0, theta0 + dtheta), max(theta0, theta0 + dtheta)
    for k in range(-4, 5):                       # cardinal params 0, +/-pi/2, +/-pi, ...
        cand = (math.pi / 2.0) * k
        if lo - 1e-12 <= cand <= hi + 1e-12:
            pts.append((cx_a + rx * math.cos(cand), cy_a + ry * math.sin(cand)))
    return pts


def parse_path_extremes(d, cx, cy):
    """Parse ONE path 'd' string against a (cx, cy) sheet transform. Returns model-space
    extremes: min/max radius (about the view's model origin), the emitted arc radii, and
    min/max X. Arcs are measured at their true extent (endpoints + in-sweep cardinal
    points), not endpoints alone. Exposed so the composer reads back each view separately
    under that view's own transform. Reads only the string + (cx, cy) — shares no variable
    with the emit path.

    Generalisation note (multiview slice): added X-extent (min_x/max_x) + arc-aware extent
    and split per-path parsing out of parse_svg_extremes, which now aggregates over paths.
    Backward compatible — original keys (min_radius/max_radius/arc_radii/endpoint_count)
    unchanged (arc-aware points are all on the true geometry, so radii extremes are
    identical)."""
    def to_model_r(sx, sy):
        return math.hypot(sx - cx, cy - sy)

    numre = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
    radii = []
    arc_radii = []
    xs = []

    def add(sx, sy):
        radii.append(to_model_r(sx, sy))
        xs.append(sx - cx)

    cur = None
    for letter, argstr in re.findall(r'([MLAZmlaz])([^MLAZmlaz]*)', d):
        nums = [float(x) for x in re.findall(numre, argstr)]
        u = letter.upper()
        if u in ("M", "L"):
            for k in range(0, len(nums) - 1, 2):
                cur = (nums[k], nums[k + 1])
                add(*cur)
        elif u == "A":
            for k in range(0, len(nums) - 6, 7):
                rx, ry = nums[k], nums[k + 1]
                large, sweep = int(round(nums[k + 3])), int(round(nums[k + 4]))
                end = (nums[k + 5], nums[k + 6])
                arc_radii.append(rx)                            # rx (1:1 == model r)
                if cur is not None:
                    for (px, py) in _arc_extreme_points(cur[0], cur[1], end[0], end[1],
                                                        rx, ry, large, sweep):
                        add(px, py)
                else:
                    add(*end)
                cur = end
    return {
        "min_radius": min(radii) if radii else None,
        "max_radius": max(radii) if radii else None,
        "arc_radii": arc_radii,
        "min_x": min(xs) if xs else None,
        "max_x": max(xs) if xs else None,
        "endpoint_count": len(radii),
    }


# ── proof (runs via /run; no module-level side effects above this line) ──────
def _main():
    OUT = "/work/out"
    os.makedirs(OUT, exist_ok=True)
    svg_path = os.path.join(OUT, "ortho_top.svg")
    pdf_path = os.path.join(OUT, "ortho_top.pdf")

    OBS = {}

    def fail(msg):
        print("=== ORTHO_RENDER diagnostics ===", flush=True)
        for k in sorted(OBS):
            print("  %s = %s" % (k, OBS[k]))
        print("ORTHO_RENDER_PROOF_FAIL:", msg, flush=True)
        sys.exit(1)

    part = Cylinder(radius=12, height=60) - Pos(12, 0, 0) * Box(6, 4, 60)

    # Step 2: project TOP with EXPLICIT look_at (rule 1). Print the exact call.
    print("PROJECT_CALL: project_to_viewport(origin=%s, up=%s, look_at=%s)  [EXPLICIT]"
          % (VIEWS["TOP"]["origin"], VIEWS["TOP"]["up"], VIEWS["TOP"]["look_at"]))
    try:
        visible, hidden = project_view(part, "TOP")
    except Exception as e:
        fail("project_view raised: %r" % (e,))

    geom_types = [_norm_geom(_geom_type(e)) for e in visible]
    OBS["VISIBLE_EDGES"] = len(visible)
    OBS["GEOM_TYPES"] = geom_types
    print("VISIBLE_EDGES:", len(visible))
    print("GEOM_TYPES:", geom_types)

    # Step 3: emit each edge; report its EMIT_MODE (line|arc|polyline|cubic), or FAIL.
    to_sheet = make_transform(SHEET)
    for i, e in enumerate(visible):
        try:
            edge_to_svg(e, to_sheet)
            print("EMIT_MODE[%d] type=%s: %s" % (i, geom_types[i], edge_emit_mode(e)))
        except Exception as ex:
            OBS["EMIT_FAIL_EDGE"] = i
            fail("edge %d (%s) not exactly emittable: %r" % (i, geom_types[i], ex))

    # Step 4: emit full SVG, write, rsvg-convert to PDF.
    svg_text = emit_view_svg(visible, to_sheet, SHEET)
    with open(svg_path, "w") as f:
        f.write(svg_text)
    r = subprocess.run(["rsvg-convert", "-f", "pdf", "-o", pdf_path, svg_path],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        fail("rsvg-convert failed: %s" % r.stderr)

    # Step 5: READBACK — parse the emitted STRING, invert the transform.
    parsed = parse_svg_extremes(svg_text, SHEET)
    OBS["ARC_RADIUS_EMITTED"] = parsed["arc_radii"]
    OBS["RENDERED_MIN_RADIUS"] = parsed["min_radius"]
    OBS["RENDERED_MAX_RADIUS"] = parsed["max_radius"]

    # page size + bytes
    pinfo = subprocess.run(["pdfinfo", pdf_path], capture_output=True, text=True)
    m = re.search(r"Page size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts", pinfo.stdout)
    if not m:
        fail("could not read page size:\n%s" % pinfo.stdout)
    pw, ph = float(m.group(1)), float(m.group(2))
    OBS["PAGE_PT"] = "%.2f x %.2f" % (pw, ph)
    OBS["PDF_BYTES"] = os.path.getsize(pdf_path)

    # Step 6: OBSERVATIONS
    for k in ("VISIBLE_EDGES", "GEOM_TYPES", "ARC_RADIUS_EMITTED",
              "RENDERED_MIN_RADIUS", "RENDERED_MAX_RADIUS", "PAGE_PT", "PDF_BYTES"):
        print("%s: %r" % (k, OBS[k]))

    # Step 7: HARD ASSERTIONS (tolerance 1e-6 — exact emission earns exact readback)
    TRUE_MAX_R = 12.0
    TRUE_MIN_R = math.sqrt(85.0)   # 9.21954445729
    TOL = 1e-6

    # (a) explicit look_at gives the clean 4-edge set (13 would mean the rule was broken)
    if OBS["VISIBLE_EDGES"] != 4:
        fail("VISIBLE_EDGES=%d != 4 (explicit look_at should give exactly 4)"
             % OBS["VISIBLE_EDGES"])
    if sorted(geom_types) != ["CIRCLE", "LINE", "LINE", "LINE"]:
        fail("GEOM_TYPES sorted=%s != [CIRCLE, LINE, LINE, LINE]" % sorted(geom_types))
    # (b) every edge emitted exact — guaranteed by reaching here (any failure fail()ed above)
    # (c) the 'A' command carries the true rim radius, exactly
    if not parsed["arc_radii"]:
        fail("no arc radius parsed from the emitted SVG")
    for ar in parsed["arc_radii"]:
        if abs(ar - TRUE_MAX_R) > TOL:
            fail("ARC_RADIUS_EMITTED=%r not 12.0 +/- 1e-6" % ar)
    # (d) rendered extremes, computed FROM THE PARSED STRING
    if OBS["RENDERED_MAX_RADIUS"] is None or abs(OBS["RENDERED_MAX_RADIUS"] - TRUE_MAX_R) > TOL:
        fail("RENDERED_MAX_RADIUS=%r not 12.0 +/- 1e-6" % OBS["RENDERED_MAX_RADIUS"])
    if OBS["RENDERED_MIN_RADIUS"] is None or abs(OBS["RENDERED_MIN_RADIUS"] - TRUE_MIN_R) > TOL:
        fail("RENDERED_MIN_RADIUS=%r not %.11f +/- 1e-6"
             % (OBS["RENDERED_MIN_RADIUS"], TRUE_MIN_R))
    # (e) PDF exists, A3 landscape
    if not os.path.exists(pdf_path):
        fail("ortho_top.pdf missing")
    if abs(pw - A3_W_PT) > PAGE_TOL or abs(ph - A3_H_PT) > PAGE_TOL:
        fail("PAGE_PT %.2f x %.2f not A3 landscape %.2f x %.2f (+/-%.1f)"
             % (pw, ph, A3_W_PT, A3_H_PT, PAGE_TOL))

    # POSITIVE — a non-rational degree-3 spline now EMITS exact cubics (no raise). This is
    # the capability this slice folds in; the old test (which asserted a plain Spline RAISED)
    # is now wrong because such a spline emits.
    from build123d import Spline
    pos_edges = list(Spline([(0, 0, 0), (5, 5, 0), (10, -3, 0), (15, 4, 0)]).edges())
    if not pos_edges:
        fail("could not construct a spline edge for the positive test")
    pos_edge = pos_edges[0]
    print("POS_TEST_EDGE_GEOM:", _norm_geom(_geom_type(pos_edge)),
          "mode:", edge_emit_mode(pos_edge))
    pos_path = edge_to_svg(pos_edge, to_sheet)
    if "C " not in pos_path:
        fail("degree-3 spline did not emit a cubic 'C' path: %r" % pos_path[:80])
    print("NEG_TEST_CUBIC_EMITS: degree-3 spline emitted cubic C without raising.")

    # NEGATIVE — the STOP guard must still fire. Build a RATIONAL bspline edge (non-uniform
    # weights) and assert edge_to_svg raises. If it cannot be built, report SKIPPED — never
    # fake it, never delete the guard.
    try:
        from build123d import Edge
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
        from OCP.gp import gp_Pnt
        from OCP.TColgp import TColgp_Array1OfPnt
        from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal

        poles = TColgp_Array1OfPnt(1, 4)
        for j, (px, py, pz) in enumerate([(0, 0, 0), (5, 8, 0), (10, -8, 0), (15, 0, 0)], 1):
            poles.SetValue(j, gp_Pnt(float(px), float(py), float(pz)))
        weights = TColStd_Array1OfReal(1, 4)          # non-uniform -> RATIONAL
        for j, w in enumerate([1.0, 2.5, 0.5, 1.0], 1):
            weights.SetValue(j, w)
        knots = TColStd_Array1OfReal(1, 2)
        knots.SetValue(1, 0.0)
        knots.SetValue(2, 1.0)
        mults = TColStd_Array1OfInteger(1, 2)
        mults.SetValue(1, 4)
        mults.SetValue(2, 4)
        rbs = Geom_BSplineCurve(poles, weights, knots, mults, 3)
        rat_edge = Edge(BRepBuilderAPI_MakeEdge(rbs).Edge())
        built = True
    except Exception as ex:
        built = False
        print("NEG_TEST_SKIPPED: could not construct a rational bspline edge: %r" % ex)

    if built:
        print("NEG_TEST_RATIONAL_GEOM:", _norm_geom(_geom_type(rat_edge)),
              "IsRational:", bool(rbs.IsRational()))
        try:
            edge_to_svg(rat_edge, to_sheet)
            fail("edge_to_svg emitted a RATIONAL bspline instead of raising "
                 "(STOP-guard defeated)")
        except ValueError:
            print("NEG_TEST_RATIONAL_STOPS: rational bspline raised as expected.")

    print("value_summary: edges=%d types=%s arc_r=%s rendered_r=[%r,%r] page=%s"
          % (OBS["VISIBLE_EDGES"], sorted(geom_types), parsed["arc_radii"],
             OBS["RENDERED_MIN_RADIUS"], OBS["RENDERED_MAX_RADIUS"], OBS["PAGE_PT"]))
    print("ORTHO_RENDER_PROOF_OK")


if __name__ == "__main__":
    _main()
