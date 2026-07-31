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


def edge_to_svg(edge, to_sheet):
    """Exact geometry, NO discretisation. LINE -> 'M..L..'; CIRCLE -> 'M..A..'.
    Raises if a CIRCLE lacks radius/arc_center accessors — the producer STOPS rather than
    fall back to sampling (rule 2)."""
    gt = _norm_geom(_geom_type(edge))
    p0 = _xy(edge @ 0.0)
    p1 = _xy(edge @ 1.0)
    s0 = to_sheet(*p0)
    s1 = to_sheet(*p1)
    if gt == "LINE":
        return "M %s %s L %s %s" % (_fmt(s0[0]), _fmt(s0[1]), _fmt(s1[0]), _fmt(s1[1]))
    if gt == "CIRCLE":
        r = float(edge.radius)              # raises -> producer STOPS (no fallback)
        c = _xy(edge.arc_center)            # raises -> producer STOPS
        mid = _xy(edge @ 0.5)
        large, sweep = _arc_flags(p0[0], p0[1], p1[0], p1[1], c[0], c[1], mid[0], mid[1])
        return ("M %s %s A %s %s 0 %d %d %s %s"
                % (_fmt(s0[0]), _fmt(s0[1]), _fmt(r), _fmt(r), large, sweep,
                   _fmt(s1[0]), _fmt(s1[1])))
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

    # Step 3: emit exact SVG; EMIT_MODE exact per edge, or FAIL naming the edge.
    to_sheet = make_transform(SHEET)
    for i, e in enumerate(visible):
        try:
            edge_to_svg(e, to_sheet)
            print("EMIT_MODE[%d] type=%s: exact" % (i, geom_types[i]))
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

    # NEGATIVE TEST — the reason commit 1 exists. A non-LINE/CIRCLE edge MUST make
    # edge_to_svg raise, never silently emit. Before the _norm_geom fix, "LINE" was a
    # substring of "BSPLINE" so a spline was mis-classified as LINE and drawn straight,
    # defeating the STOP-never-fake guard. Build a real spline edge and prove it raises.
    from build123d import Spline
    neg_edges = list(Spline([(0, 0, 0), (5, 5, 0), (10, -3, 0), (15, 4, 0)]).edges())
    if not neg_edges:
        fail("could not construct a spline edge for the negative test")
    neg_edge = neg_edges[0]
    print("NEG_TEST_EDGE_GEOM:", _norm_geom(_geom_type(neg_edge)))
    try:
        edge_to_svg(neg_edge, to_sheet)
        fail("edge_to_svg emitted a non-LINE/CIRCLE edge instead of raising "
             "(STOP-guard defeated)")
    except ValueError:
        print("NEG_TEST_BSPLINE: raised as expected.")

    print("value_summary: edges=%d types=%s arc_r=%s rendered_r=[%r,%r] page=%s"
          % (OBS["VISIBLE_EDGES"], sorted(geom_types), parsed["arc_radii"],
             OBS["RENDERED_MIN_RADIUS"], OBS["RENDERED_MAX_RADIUS"], OBS["PAGE_PT"]))
    print("ORTHO_RENDER_PROOF_OK")


if __name__ == "__main__":
    _main()
