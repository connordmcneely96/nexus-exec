"""
Multiview compose proof: place TWO orthographic views (TOP + FRONT) on ONE A3 sheet
through a SHARED transform, and prove — by parsing the emitted SVG string back — that
they register against each other along the shared X axis.

Builds on orthographic_render.py (PR #14): exact-edge emission + independent string
readback. This slice imports its producer/parser (project_view, view_path_d /
edge_to_svg, parse_path_extremes) — no emission logic is duplicated here.

The two views are the same physical part along a shared X axis, so once each view's own
transform is inverted, their silhouettes' X bounds must coincide. A drift means the
transform was not actually shared. Runs via the deployed /run endpoint — NOT locally.

Live question this answers (do NOT work around it): does FRONT stay inside the producer's
LINE/CIRCLE coverage? If FRONT projects any other edge type, the producer would STOP on
it (correct) — we report FRONT_HAS_NONLINEAR and FAIL LOUD with the type list.
"""
import math
import os
import re
import subprocess
import sys

from build123d import Box, Cylinder, Pos

from orthographic_render import (
    A3_H_PT,
    A3_W_PT,
    PAGE_TOL,
    _geom_type,
    _norm_geom,
    edge_emit_mode,
    make_transform,
    parse_path_extremes,
    project_view,
    view_path_d,
)

# A3 landscape; SHARED x-origin (CX) and scale (1:1) for both views — only the vertical
# placement (CY) differs. Third-angle-ish stack: TOP above, FRONT below.
SHEET_W, SHEET_H = 420, 297
CX = 210.0
CY_TOP = 90.0        # TOP is 24 tall  -> sheet y in [78, 102]
CY_FRONT = 200.0     # FRONT is 60 tall -> sheet y in [170, 230]
STROKE = 0.35


def _main():
    OUT = "/work/out"
    os.makedirs(OUT, exist_ok=True)
    svg_path = os.path.join(OUT, "compose_top_front.svg")
    pdf_path = os.path.join(OUT, "compose_top_front.pdf")
    OBS = {}

    def fail(msg):
        print("=== MULTIVIEW_COMPOSE diagnostics ===", flush=True)
        for k in sorted(OBS):
            print("  %s = %s" % (k, OBS[k]))
        print("MULTIVIEW_COMPOSE_PROOF_FAIL:", msg, flush=True)
        sys.exit(1)

    part = Cylinder(radius=12, height=60) - Pos(12, 0, 0) * Box(6, 4, 60)

    # ── Step 2: project both views (explicit look_at is inside project_view) ──
    views = {}
    for name in ("TOP", "FRONT"):
        try:
            vis, _hidden = project_view(part, name)
        except Exception as e:
            fail("project_view(%s) raised: %r" % (name, e))
        gts = [_norm_geom(_geom_type(e)) for e in vis]
        OBS[name + "_VISIBLE_EDGES"] = len(vis)
        OBS[name + "_GEOM_TYPES"] = gts
        print("%s_VISIBLE_EDGES: %d" % (name, len(vis)))
        print("%s_GEOM_TYPES: %s" % (name, gts))
        views[name] = (vis, gts)

    # ── FRONT now emits: edge_to_svg handles degree-1 BSPLINE (as L segments), so the
    #    old FRONT_HAS_NONLINEAR abort is gone. Every FRONT edge must be one the producer
    #    can emit (LINE / CIRCLE / non-rational BSPLINE deg<=3). A RATIONAL or degree>3
    #    spline STILL fails loud — the genuine Option-2 case, not worked around. ──
    front_vis, front_gts = views["FRONT"]
    front_modes = []
    for i, e in enumerate(front_vis):
        try:
            front_modes.append(edge_emit_mode(e))
        except Exception as ex:
            OBS["FRONT_UNEMITTABLE"] = "edge %d (%s): %r" % (i, front_gts[i], ex)
            print("FRONT_UNEMITTABLE:", OBS["FRONT_UNEMITTABLE"])
            fail("FRONT edge %d (%s) is not exact-emittable (rational spline or degree>3) "
                 "— the genuine Option-2 case, not worked around: %r"
                 % (i, front_gts[i], ex))
    OBS["FRONT_EMIT_MODES"] = front_modes
    print("FRONT_EMIT_MODES:", front_modes)

    # ── Step 3: SHARED transform — same CX + scale, only CY differs ──────────
    top_tf = make_transform({"cx": CX, "cy": CY_TOP})
    front_tf = make_transform({"cx": CX, "cy": CY_FRONT})

    # ── Step 4: emit both views into ONE svg (producer STOPs if any edge is inexact) ──
    try:
        top_d = view_path_d(views["TOP"][0], top_tf)
        front_d = view_path_d(front_vis, front_tf)
    except Exception as e:
        fail("edge_to_svg STOPped during emission: %r" % e)

    # CONFIRM the readback covers every command the emitted SVG contains. FRONT's degree-1
    # splines emit as L, so the existing M/L/A parse_path_extremes covers them (%.10f emits
    # no letters, so any letter here is a path command). A 'C' would need cubic-parse support
    # — not added here (untested) — before a curved part could compose; fail loud if it ever
    # appears rather than silently mis-reading it.
    cmds = set(re.findall(r"[A-Za-z]", top_d + " " + front_d))
    OBS["PATH_COMMANDS"] = "".join(sorted(cmds))
    print("PATH_COMMANDS:", OBS["PATH_COMMANDS"])
    unsupported = cmds - set("MLAZmlaz")
    if unsupported:
        fail("emitted SVG contains path command(s) %s that parse_path_extremes does not "
             "handle (e.g. 'C' cubic). A curved part needs C-parse support before it can "
             "compose; not adding untested C-parsing now." % sorted(unsupported))

    svg = ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
           'width="%dmm" height="%dmm" viewBox="0 0 %d %d">\n'
           '<path data-view="top" d="%s" fill="none" stroke="black" stroke-width="%.2f"/>\n'
           '<path data-view="front" d="%s" fill="none" stroke="black" stroke-width="%.2f"/>\n'
           '</svg>\n'
           % (SHEET_W, SHEET_H, SHEET_W, SHEET_H, top_d, STROKE, front_d, STROKE))
    with open(svg_path, "w") as f:
        f.write(svg)
    r = subprocess.run(["rsvg-convert", "-f", "pdf", "-o", pdf_path, svg_path],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        fail("rsvg-convert failed: %s" % r.stderr)

    # ── Step 5: READBACK — extract each view's path from the emitted STRING, invert
    #    that view's own transform. Independent of the emit path (parses text only). ──
    view_ds = {}
    for tag in re.findall(r'<path\b[^>]*>', svg):
        mv = re.search(r'data-view="([^"]*)"', tag)
        md = re.search(r'\bd="([^"]*)"', tag)
        if mv and md:
            view_ds[mv.group(1)] = md.group(1)
    if "top" not in view_ds or "front" not in view_ds:
        fail("could not extract both view paths from the emitted SVG string")

    top = parse_path_extremes(view_ds["top"], CX, CY_TOP)
    front = parse_path_extremes(view_ds["front"], CX, CY_FRONT)

    OBS["TOP_X_EXTENT"] = (top["min_x"], top["max_x"])
    OBS["FRONT_X_EXTENT"] = (front["min_x"], front["max_x"])
    OBS["TOP_RADII"] = (top["min_radius"], top["max_radius"])

    pinfo = subprocess.run(["pdfinfo", pdf_path], capture_output=True, text=True)
    m = re.search(r"Page size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts", pinfo.stdout)
    if not m:
        fail("could not read page size:\n%s" % pinfo.stdout)
    pw, ph = float(m.group(1)), float(m.group(2))
    OBS["PAGE_PT"] = "%.2f x %.2f" % (pw, ph)

    if top["min_x"] is None or front["min_x"] is None:
        fail("a view produced no parseable coordinates (top=%s front=%s)"
             % (top["min_x"], front["min_x"]))
    d_min = abs(top["min_x"] - front["min_x"])
    d_max = abs(top["max_x"] - front["max_x"])
    front_maxabs_x = max(abs(front["min_x"]), abs(front["max_x"]))

    # ── OBSERVE before asserting ──────────────────────────────────────────────
    print("OBSERVE TOP_X_EXTENT:   [%r, %r]" % (top["min_x"], top["max_x"]))
    print("OBSERVE FRONT_X_EXTENT: [%r, %r]" % (front["min_x"], front["max_x"]))
    print("OBSERVE X_EXTENT_DELTA: min_x=%.3e  max_x=%.3e" % (d_min, d_max))
    print("OBSERVE TOP_RADII: min=%r max=%r" % (top["min_radius"], top["max_radius"]))
    print("OBSERVE FRONT max_x=%r (the +X half-width sqrt(140)); FRONT max|x|=%r "
          "(the -X rim at -12)" % (front["max_x"], front_maxabs_x))
    print("OBSERVE PAGE_PT:", OBS["PAGE_PT"])

    # ── HARD ASSERTIONS (1e-6) ────────────────────────────────────────────────
    TOL = 1e-6
    SQRT140 = math.sqrt(140.0)   # 11.8321595662 — shaft +X half-width (keyway side)
    SQRT85 = math.sqrt(85.0)     # 9.21954445729 — notch floor corner radius

    # (a) the reason this slice exists: shared X axis must register across the two views
    if d_min > TOL:
        fail("X-extent MIN drift %.3e > 1e-6 (TOP.min_x=%r FRONT.min_x=%r) — transform "
             "not shared" % (d_min, top["min_x"], front["min_x"]))
    if d_max > TOL:
        fail("X-extent MAX drift %.3e > 1e-6 (TOP.max_x=%r FRONT.max_x=%r) — transform "
             "not shared" % (d_max, top["max_x"], front["max_x"]))
    # (b) both views read back to the true geometry from the emitted string
    if abs(top["max_radius"] - 12.0) > TOL:
        fail("TOP max radius %r not 12.0 +/- 1e-6" % top["max_radius"])
    if abs(top["min_radius"] - SQRT85) > TOL:
        fail("TOP min radius %r not sqrt(85) +/- 1e-6" % top["min_radius"])
    if abs(top["max_x"] - SQRT140) > TOL:
        fail("TOP max_x %r not sqrt(140) +/- 1e-6" % top["max_x"])
    if abs(top["min_x"] - (-12.0)) > TOL:
        fail("TOP min_x %r not -12.0 +/- 1e-6" % top["min_x"])
    if abs(front["max_x"] - SQRT140) > TOL:
        fail("FRONT max_x %r not sqrt(140)=%.10f +/- 1e-6" % (front["max_x"], SQRT140))
    # (c) A3 landscape
    if not os.path.exists(pdf_path):
        fail("compose_top_front.pdf missing")
    if abs(pw - A3_W_PT) > PAGE_TOL or abs(ph - A3_H_PT) > PAGE_TOL:
        fail("PAGE_PT %.2f x %.2f not A3 landscape %.2f x %.2f (+/-%.1f)"
             % (pw, ph, A3_W_PT, A3_H_PT, PAGE_TOL))

    print("value_summary: top_x=[%r,%r] front_x=[%r,%r] delta=(%.2e,%.2e) "
          "top_r=[%r,%r] page=%s"
          % (top["min_x"], top["max_x"], front["min_x"], front["max_x"],
             d_min, d_max, top["min_radius"], top["max_radius"], OBS["PAGE_PT"]))
    print("MULTIVIEW_COMPOSE_PROOF_OK")


if __name__ == "__main__":
    _main()
