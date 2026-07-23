"""
Grounded section-geometry proof: recover the transverse cut of the keyed shaft as a
faithful PLANAR SLICE of the exact build123d/OCP solid -- no FreeCAD, no SVG, no hatch.

section_view_proof.py (PR #9) established that freecadcmd will NOT hand us a section:
viewPartAsSvg(section) returned bytes identical to the base view (SEC_SVG_LEN ==
BASE_SVG_LEN == 691, SECTION_PATHS == BASE_PATHS == 4, HATCH_PRESENT no). The cut and
hatch are Gui-only. Decision: we self-compute the section from the solid we already
build -- the section IS a planar slice of that solid, so it is provably faithful, not
a render we must trust.

This slice proves we can recover that slice geometry cleanly. It does NOT render SVG
and does NOT self-draw a hatch -- that is the next slice, which builds on the result
structure reported here. Same epistemics as the prior proofs: introspect the API,
PRINT what we find, then hard-assert the grounded geometric facts. Guard every API
call so a wrong attribute is OBSERVED, not a crash.

Run via the deployed /run endpoint -- NOT locally, NOT in CI.
"""
import math
import os

from build123d import Box, Cylinder, Plane, Pos, Rectangle, export_step

OUT = "/work/out"
os.makedirs(OUT, exist_ok=True)
FACE_STEP = os.path.join(OUT, "section_face.step")

DISK_AREA = 452.389  # pi * 12^2, the full disk area for the REMOVED_AREA differential


# ── helpers ──────────────────────────────────────────────────────────────────
def _vxyz(v):
    """(x, y, z) from a build123d Vertex or raw point, guarded."""
    try:
        return (float(v.X), float(v.Y), float(v.Z))
    except Exception:
        pass
    try:
        t = v.to_tuple()
        return (float(t[0]), float(t[1]), float(t[2]))
    except Exception:
        pass
    try:
        return (float(v[0]), float(v[1]), float(v[2]))
    except Exception:
        return None


def _is_closed(w):
    try:
        r = w.is_closed
        return bool(r()) if callable(r) else bool(r)
    except Exception as e:
        return "ERR:" + repr(e)


# ── Step 1: the SAME keyed shaft as section_view_proof.py ─────────────────────
# Cylinder centred at origin, axis +Z; a full-length keyway seat on the +X surface.
part = Cylinder(radius=12, height=60) - Pos(12, 0, 0) * Box(6, 4, 60)
print("PART_TYPE:", type(part).__name__)

# ── Step 2: slice with the transverse plane z=0 (normal +Z, origin at world zero) ─
method = None          # "build123d_intersect" | "ocp_section"
section_type = None    # type name of the recovered geometry
verts = []             # unified list of (x, y, z) over all section vertices
wire_count = None
wire_closed = []
area = None            # face area (PRIMARY only) or None (fallback)
section_bd = None      # build123d shape (PRIMARY) for optional export

# PRIMARY — build123d high level: the boolean common of the solid with a face lying
# in z=0 IS the planar cut face (a 2D section), so area + wires are available.
try:
    big = Plane.XY * Rectangle(100, 100)          # a face lying in z=0
    section_bd = part & big                       # boolean common: the cut face
    section_type = type(section_bd).__name__
    print("SECTION_METHOD_TRY: build123d_intersect")
    print("SECTION_RAW_TYPE:", section_type)
    faces = []
    try:
        faces = list(section_bd.faces())
    except Exception as e:
        print("PRIMARY_FACES_ERR:", repr(e))
    print("PRIMARY_FACE_COUNT:", len(faces))
    if faces:
        method = "build123d_intersect"
        # vertices
        try:
            verts = [p for p in (_vxyz(v) for v in section_bd.vertices()) if p]
        except Exception as e:
            print("PRIMARY_VERTS_ERR:", repr(e))
        # wires (count + per-wire closed, guarded)
        try:
            wl = list(section_bd.wires())
            wire_count = len(wl)
            wire_closed = [_is_closed(w) for w in wl]
        except Exception as e:
            print("PRIMARY_WIRES_ERR:", repr(e))
        # area — prefer the shape's own .area, fall back to summing faces
        try:
            area = float(section_bd.area)
        except Exception:
            try:
                area = float(sum(f.area for f in faces))
            except Exception as e:
                print("PRIMARY_AREA_ERR:", repr(e))
                area = None
    else:
        print("PRIMARY_EMPTY_OR_NONFACE -> falling back to OCP section")
except Exception as e:
    print("PRIMARY_ERR:", repr(e))

# FALLBACK — OCP BRepAlgoAPI_Section yields section EDGES (no face, so no area).
# The radius + planarity assertions below still ground faithfulness on the fallback.
if method is None:
    try:
        from OCP.BRep import BRep_Tool
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
        from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
        from OCP.TopAbs import TopAbs_EDGE, TopAbs_VERTEX
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS

        s = BRepAlgoAPI_Section(part.wrapped, gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)))
        s.Build()
        shp = s.Shape()
        method = "ocp_section"
        section_type = "ocp_edges"
        print("SECTION_METHOD_TRY: ocp_section")

        # edge count
        ec = 0
        ee = TopExp_Explorer(shp, TopAbs_EDGE)
        while ee.More():
            ec += 1
            ee.Next()
        print("OCP_EDGE_COUNT:", ec)

        # vertices (raw OCP -> (x, y, z))
        ve = TopExp_Explorer(shp, TopAbs_VERTEX)
        while ve.More():
            try:
                vtx = TopoDS.Vertex_s(ve.Current())
                p = BRep_Tool.Pnt_s(vtx)
                verts.append((p.X(), p.Y(), p.Z()))
            except Exception as e:
                print("OCP_VERTEX_ERR:", repr(e))
            ve.Next()
        area = None  # no face on the fallback
    except Exception as e:
        print("FALLBACK_ERR:", repr(e))

print("SECTION_METHOD:", method)

# ── Step 3: optional export for eyeball inspection (non-fatal) ────────────────
if section_bd is not None:
    try:
        export_step(section_bd, FACE_STEP)
        print("SECTION_EXPORT:", FACE_STEP, os.path.getsize(FACE_STEP), "bytes")
    except Exception as e:
        print("SECTION_EXPORT_ERR:", repr(e))
else:
    print("SECTION_EXPORT: skipped (no build123d shape on fallback path)")

# ── Step 4: OBSERVATIONS — compute + PRINT everything before asserting ────────
vertex_count = len(verts)
max_abs_z = max((abs(p[2]) for p in verts), default=None)
radii = [math.hypot(p[0], p[1]) for p in verts]
min_radius = min(radii) if radii else None
max_radius = max(radii) if radii else None
section_area = area
removed_area = (DISK_AREA - section_area) if section_area is not None else None

print("SECTION_TYPE:", section_type)
print("WIRE_COUNT:", wire_count)
print("WIRE_CLOSED:", wire_closed)
print("VERTEX_COUNT:", vertex_count)
print("MAX_ABS_Z:", max_abs_z)
print("MIN_RADIUS:", min_radius)
print("MAX_RADIUS:", max_radius)
print("SECTION_AREA:", section_area if section_area is not None else "n/a")
print("DISK_AREA:", DISK_AREA)
print("REMOVED_AREA:", removed_area if removed_area is not None else "n/a")


# ── Step 5: HARD ASSERTIONS — the grounded faithfulness claim ─────────────────
def _fail(msg):
    print("=== section geometry observations (failure) ===", flush=True)
    print("  method=%s section_type=%s" % (method, section_type))
    print("  vertex_count=%s max_abs_z=%s" % (vertex_count, max_abs_z))
    print("  min_radius=%s max_radius=%s" % (min_radius, max_radius))
    print("  section_area=%s removed_area=%s" % (section_area, removed_area))
    print("  sample_verts=%s" % (verts[:8],))
    raise SystemExit("SECTION_GEOMETRY_PROOF_FAIL: " + msg)


# (a) section is non-empty
if vertex_count < 3:
    _fail("section empty: VERTEX_COUNT=%s < 3" % vertex_count)

# (b) PLANAR — the slice lies in the z=0 plane
if max_abs_z is None or max_abs_z >= 1e-3:
    _fail("not planar: MAX_ABS_Z=%s (>= 1e-3)" % max_abs_z)

# (c) NOTCH REVEALED, signal 1 — a boundary point inside r=9.5 (plain disk = 12 everywhere)
if min_radius is None or min_radius > 9.5:
    _fail("no notch (signal 1): MIN_RADIUS=%s (> 9.5)" % min_radius)

# (d) RIM PRESENT — the section reaches the r=12 rim
if max_radius is None or not (11.8 <= max_radius <= 12.05):
    _fail("rim missing: MAX_RADIUS=%s (not in [11.8, 12.05])" % max_radius)

# (e) NOTCH REVEALED, signal 2 — a keyway-sized bite removed (PRIMARY/face path only)
if section_area is not None:
    if not (5.0 <= removed_area <= 20.0):
        _fail("notch area wrong (signal 2): REMOVED_AREA=%s (not in [5.0, 20.0])"
              % removed_area)
    print("NOTCH_SIGNALS: 2 (min_radius + removed_area)")
else:
    print("NOTCH_SIGNALS: 1 (min_radius; area unavailable on fallback)")

print("value_summary: method=%s verts=%d min_r=%.3f max_r=%.3f removed_area=%s"
      % (method, vertex_count, min_radius, max_radius,
         ("%.3f" % removed_area) if removed_area is not None else "n/a"))
print("SECTION_GEOMETRY_PROOF_OK")
