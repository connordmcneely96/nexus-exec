"""
Third-angle convention proof (FRONT + TOP): enforce +X-to-the-RIGHT in BOTH views and
prove registration by readback.

The plain compose proof FAILED correctly: TOP_X_EXTENT [-12, +11.832] vs FRONT_X_EXTENT
[-11.832, +12] — the two views were MIRROR images along X. project_to_viewport picks a
camera-local right/up per view, so TOP's +X points right while FRONT's +X points left. A
drawing where the keyway sits on opposite sides in TOP and FRONT is WRONG.

Fix (Connor's ruling): the THIRD-ANGLE (ASME/US) convention — FRONT and TOP SHARE the
horizontal model-X axis in the SAME sense (+X to the right in BOTH). This is derived from
the SOLID's own asymmetry (the keyway removes the +X rim so max_x = sqrt(140) < 12, while
Y and Z are symmetric about 0) — NOT from hand-derived camera math (the camera setup was
what was wrong). Each view's horizontal orientation is read off the flattened projection,
oriented to +X-right, and registration is PROVEN by parsing the emitted string back.

Scope: this is the CONVENTION proof; the producer (edge_to_svg / orthographic_render) is
edited in a LATER slice. Runs via the deployed /run endpoint — NOT locally.
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
    parse_path_extremes,
    project_view,
    view_path_d,
)

SHEET_W, SHEET_H = 420, 297
CX = 210.0            # SHARED horizontal origin — the whole point is a common X sense
CY_TOP = 90.0         # TOP above
CY_FRONT = 200.0      # FRONT below
STROKE = 0.35
TOL = 1e-6
ASYM_TOL = 0.05       # the keyway asymmetry is ~0.168 mm — well above noise, below the bite
SQRT140 = math.sqrt(140.0)   # 11.8321595662 — +X (keyway) half-width
SQRT85 = math.sqrt(85.0)     # 9.21954445729 — notch floor corner radius


def _oriented_transform(cx, cy, flip):
    """model/flattened (x, y) -> sheet. Horizontal flip mirrors the view so +model-X lands
    at +sheet-X (right). Vertical keeps the standard y-flip (+up): edge_to_svg's arc sweep
    assumes it, and TOP carries an arc, so flipping the vertical here would mis-draw that
    arc. The strict third-angle TOP +Y-down fold is deferred to the producer slice (it
    needs arc-sweep handling, the same class as the horizontal FLIP_ARC limit below); it
    does not affect X-registration, which is what this slice proves."""
    def to_sheet(x, y):
        return (cx + (-x if flip else x), cy - y)
    return to_sheet


def _main():
    OUT = "/work/out"
    os.makedirs(OUT, exist_ok=True)
    svg_path = os.path.join(OUT, "compose_top_front.svg")
    pdf_path = os.path.join(OUT, "compose_top_front.pdf")
    OBS = {}

    def fail(msg):
        print("=== THIRD_ANGLE_COMPOSE diagnostics ===", flush=True)
        for k in sorted(OBS):
            print("  %s = %s" % (k, OBS[k]))
        print("THIRD_ANGLE_COMPOSE_PROOF_FAIL:", msg, flush=True)
        sys.exit(1)

    part = Cylinder(radius=12, height=60) - Pos(12, 0, 0) * Box(6, 4, 60)

    # ── Step 1: bounding box — establish X as the ASYMMETRIC axis (max != -min). ──
    try:
        bb = part.bounding_box()
        bx = (float(bb.min.X), float(bb.max.X))
        by = (float(bb.min.Y), float(bb.max.Y))
        bz = (float(bb.min.Z), float(bb.max.Z))
    except Exception as e:
        fail("bounding_box() failed: %r" % (e,))

    def _asym(lo, hi):
        return abs(abs(hi) - abs(lo))

    OBS["BBOX_MODEL"] = "X[%r,%r] Y[%r,%r] Z[%r,%r]" % (bx[0], bx[1], by[0], by[1], bz[0], bz[1])
    print("BBOX_MODEL:", OBS["BBOX_MODEL"])
    print("BBOX_ASYM: X=%.4f Y=%.4f Z=%.4f  (X asymmetric = keyway signature; identify X by "
          "asymmetry, NOT range size — X range 23.83 vs Y range 24.0 are too close)"
          % (_asym(*bx), _asym(*by), _asym(*bz)))
    # The operative signal below is the per-view FLATTENED extent (arc-aware, from the real
    # projected edges) — reliable even if OCP's bbox is loose (reports 12 instead of sqrt140).

    # ── Steps 2-3: per view — project, measure raw flattened X, decide +X-right flip. ──
    views = {}
    for name in ("TOP", "FRONT"):
        try:
            vis, _hidden = project_view(part, name)
        except Exception as e:
            fail("project_view(%s) raised: %r" % (name, e))
        gts = [_norm_geom(_geom_type(e)) for e in vis]
        modes = []
        for i, e in enumerate(vis):
            try:
                modes.append(edge_emit_mode(e))
            except Exception as ex:
                fail("%s edge %d (%s) not exact-emittable (rational/degree>3 spline): %r"
                     % (name, i, gts[i], ex))

        # RAW emit with the identity horizontal (cx=0, no flip) so the parsed min_x/max_x
        # ARE the flattened X-extent (arc-aware). Nothing is written; measurement only.
        raw_d = view_path_d(vis, _oriented_transform(0.0, 0.0, False))
        raw = parse_path_extremes(raw_d, 0.0, 0.0)
        if raw["min_x"] is None:
            fail("%s produced no parseable coordinates" % name)
        rxmin, rxmax = raw["min_x"], raw["max_x"]
        asym = _asym(rxmin, rxmax)
        print("%s RAW_FLAT_X_EXTENT=[%r, %r]  asym=%.4f  modes=%s"
              % (name, rxmin, rxmax, asym, modes))

        # The horizontal live axis must be model X (asymmetric). If it is symmetric, model X
        # is not on the horizontal axis (unexpected for FRONT/TOP) — STOP, do not guess.
        if asym <= ASYM_TOL:
            fail("%s horizontal flattened axis is symmetric (asym=%.4f <= %.2f) — model X is "
                 "not the horizontal axis; cannot orient by X" % (name, asym, ASYM_TOL))
        # The notch (+model-X) end is the extreme with the SMALLER |value| (keyway shortens
        # +X to sqrt(140) < 12). FLIP the view iff that end sits on the negative side.
        notch = rxmax if abs(rxmax) < abs(rxmin) else rxmin
        flip = notch < 0

        # SCOPE LIMIT: a horizontal flip mirrors arc handedness; a flipped view with a CIRCLE
        # would need its sweep flipped too (producer work). STOP rather than emit a mirrored
        # arc with unflipped sweep. FRONT (the flipped view) has no arc, so this is not hit.
        if flip and any(m == "arc" for m in modes):
            OBS["FLIP_ARC_UNHANDLED"] = name
            fail("%s must be horizontally FLIPPED but contains a CIRCLE/arc — a mirrored arc "
                 "needs its sweep flipped (deferred to the producer slice). STOP." % name)

        print("%s FLIP_APPLIED=%s" % (name, flip))
        views[name] = {"vis": vis, "flip": flip, "raw": (rxmin, rxmax), "modes": modes}

    # ── Step 4: oriented emit — shared CX + scale, per-view CY, per-view horizontal flip. ──
    top_tf = _oriented_transform(CX, CY_TOP, views["TOP"]["flip"])
    front_tf = _oriented_transform(CX, CY_FRONT, views["FRONT"]["flip"])
    try:
        top_d = view_path_d(views["TOP"]["vis"], top_tf)
        front_d = view_path_d(views["FRONT"]["vis"], front_tf)
    except Exception as e:
        fail("edge_to_svg STOPped during oriented emission: %r" % e)

    # PATH_COMMANDS coverage guard (%.10f emits no letters, so any letter is a command).
    cmds = set(re.findall(r"[A-Za-z]", top_d + " " + front_d))
    OBS["PATH_COMMANDS"] = "".join(sorted(cmds))
    print("PATH_COMMANDS:", OBS["PATH_COMMANDS"])
    unsupported = cmds - set("MLAZmlaz")
    if unsupported:
        fail("emitted SVG contains path command(s) %s outside {M,L,A,Z} that "
             "parse_path_extremes does not handle (e.g. 'C' cubic)." % sorted(unsupported))

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

    # ── READBACK — parse each view's path from the emitted STRING under its own transform ──
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

    pinfo = subprocess.run(["pdfinfo", pdf_path], capture_output=True, text=True)
    m = re.search(r"Page size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts", pinfo.stdout)
    if not m:
        fail("could not read page size:\n%s" % pinfo.stdout)
    pw, ph = float(m.group(1)), float(m.group(2))
    OBS["PAGE_PT"] = "%.2f x %.2f" % (pw, ph)

    if top["min_x"] is None or front["min_x"] is None:
        fail("a view produced no parseable coordinates after orientation")
    d_min = abs(top["min_x"] - front["min_x"])
    d_max = abs(top["max_x"] - front["max_x"])

    # ── OBSERVE (raw + corrected + flip) before asserting ────────────────────
    print("OBSERVE TOP   raw_X=%s corrected_X=[%r, %r] flip=%s"
          % (views["TOP"]["raw"], top["min_x"], top["max_x"], views["TOP"]["flip"]))
    print("OBSERVE FRONT raw_X=%s corrected_X=[%r, %r] flip=%s"
          % (views["FRONT"]["raw"], front["min_x"], front["max_x"], views["FRONT"]["flip"]))
    print("OBSERVE X_EXTENT_DELTA: min_x=%.3e  max_x=%.3e" % (d_min, d_max))
    print("OBSERVE TOP_RADII: min=%r max=%r" % (top["min_radius"], top["max_radius"]))
    print("OBSERVE PAGE_PT:", OBS["PAGE_PT"])

    # ── ASSERTIONS (1e-6) ────────────────────────────────────────────────────
    # (a) REGISTRATION: shared X axis must line up across the two views.
    if d_min > TOL:
        fail("X-extent MIN drift %.3e > 1e-6 (TOP=%r FRONT=%r)"
             % (d_min, top["min_x"], front["min_x"]))
    if d_max > TOL:
        fail("X-extent MAX drift %.3e > 1e-6 (TOP=%r FRONT=%r)"
             % (d_max, top["max_x"], front["max_x"]))
    # (b) CANONICAL ORIENTATION: BOTH read back min_x=-12 and max_x=+sqrt(140) — the keyway
    #     on +X in BOTH. (Identical mirror-flipped extents would still pass (a); this proves
    #     they are not both-wrong-the-same-way.)
    for nm, vv in (("TOP", top), ("FRONT", front)):
        if abs(vv["min_x"] - (-12.0)) > TOL:
            fail("%s min_x %r not -12.0 +/- 1e-6 (keyway not on +X / not canonical)"
                 % (nm, vv["min_x"]))
        if abs(vv["max_x"] - SQRT140) > TOL:
            fail("%s max_x %r not +sqrt(140)=%.10f +/- 1e-6 (keyway not on +X / not canonical)"
                 % (nm, vv["max_x"], SQRT140))
    # (c) true geometry, from the parsed string.
    if abs(top["max_radius"] - 12.0) > TOL:
        fail("TOP max radius %r not 12.0 +/- 1e-6" % top["max_radius"])
    if abs(top["min_radius"] - SQRT85) > TOL:
        fail("TOP min radius %r not sqrt(85) +/- 1e-6" % top["min_radius"])
    if abs(front["max_x"] - SQRT140) > TOL:
        fail("FRONT max_x %r not sqrt(140) +/- 1e-6" % front["max_x"])
    # (d) A3 landscape.
    if not os.path.exists(pdf_path):
        fail("compose_top_front.pdf missing")
    if abs(pw - A3_W_PT) > PAGE_TOL or abs(ph - A3_H_PT) > PAGE_TOL:
        fail("PAGE_PT %.2f x %.2f not A3 landscape %.2f x %.2f (+/-%.1f)"
             % (pw, ph, A3_W_PT, A3_H_PT, PAGE_TOL))

    print("value_summary: TOP_flip=%s FRONT_flip=%s top_x=[%r,%r] front_x=[%r,%r] "
          "delta=(%.2e,%.2e) top_r=[%r,%r] page=%s"
          % (views["TOP"]["flip"], views["FRONT"]["flip"], top["min_x"], top["max_x"],
             front["min_x"], front["max_x"], d_min, d_max,
             top["min_radius"], top["max_radius"], OBS["PAGE_PT"]))
    print("THIRD_ANGLE_COMPOSE_PROOF_OK")


if __name__ == "__main__":
    _main()
