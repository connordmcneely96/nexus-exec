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
    u = gt.upper()
    if "CIRC" in u:
        return "CIRCLE"
    if "LINE" in u:
        return "LINE"
    return u


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
    sweep = 1 if ccw else 0           # y-flip turns model-CCW into screen-CW
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


def emit_view_svg(edges, to_sheet, sheet=SHEET):
    """Full A3-landscape <svg>, one <path> concatenating each edge's exact command."""
    d = " ".join(edge_to_svg(e, to_sheet) for e in edges)
    return ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            'width="%dmm" height="%dmm" viewBox="0 0 %d %d">\n'
            '<path d="%s" fill="none" stroke="black" stroke-width="0.35"/>\n'
            '</svg>\n' % (sheet["w"], sheet["h"], sheet["w"], sheet["h"], d))


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

    def to_model_r(sx, sy):
        return math.hypot(sx - cx, cy - sy)

    num = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
    endpoint_radii = []
    arc_radii = []
    for d in re.findall(r'\bd\s*=\s*"([^"]*)"', svg_text):
        for letter, argstr in re.findall(r'([MLAZmlaz])([^MLAZmlaz]*)', d):
            nums = [float(x) for x in re.findall(num, argstr)]
            u = letter.upper()
            if u in ("M", "L"):
                for k in range(0, len(nums) - 1, 2):
                    endpoint_radii.append(to_model_r(nums[k], nums[k + 1]))
            elif u == "A":
                for k in range(0, len(nums) - 6, 7):
                    arc_radii.append(nums[k])                       # rx (1:1 == model r)
                    endpoint_radii.append(to_model_r(nums[k + 5], nums[k + 6]))
    return {
        "min_radius": min(endpoint_radii) if endpoint_radii else None,
        "max_radius": max(endpoint_radii) if endpoint_radii else None,
        "arc_radii": arc_radii,
        "endpoint_count": len(endpoint_radii),
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

    print("value_summary: edges=%d types=%s arc_r=%s rendered_r=[%r,%r] page=%s"
          % (OBS["VISIBLE_EDGES"], sorted(geom_types), parsed["arc_radii"],
             OBS["RENDERED_MIN_RADIUS"], OBS["RENDERED_MAX_RADIUS"], OBS["PAGE_PT"]))
    print("ORTHO_RENDER_PROOF_OK")


if __name__ == "__main__":
    _main()
