"""
HLR projection proof: can we self-render the orthographic projections too, and drop
FreeCAD from the drawing path entirely?

FreeCAD headless has failed this lane twice for the same structural reason: dimensions
are Gui-only (annotation_proof.py) and sections are Gui-only (section_view_proof.py
returned a section SVG byte-identical to the base view). We now self-compute and
self-render sections from the exact solid (section_geometry_proof.py +
section_render_proof.py, both green).

The open question this slice answers -- and ONLY this: build123d exposes
project_to_viewport(), OCP hidden-line removal returning visible + hidden edge lists as
real geometry. If it works headless, the whole drawing comes from one renderer in one
coordinate system. This slice does NOT build a sheet, compose views, or emit a drawing.
Observe, then assert the grounded facts.

Same epistemics as the prior proofs: introspect the API, PRINT what we find, then
hard-assert. Guard every call -- observe, do not crash. Runs via the deployed /run
endpoint -- NOT locally, NOT in CI.
"""
import inspect
import math
import sys

from build123d import Box, Cylinder, Pos

# OCP is always present (build123d is OCP-backed); used for chord-tolerance
# discretisation and the direct-HLR fallback.
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

CHORD_TOL = 0.05
TRUE_MAX_R = 12.0
TRUE_MIN_R = math.sqrt(85.0)   # 9.2195 — the keyway floor corner (9, +/-2)

# Silhouette extents. NOTE on BBOX_W: the keyway box spans x in [9,15], y in [-2,2] and
# so removes the cylinder's +X rim point (12,0). The solid's max X is therefore the
# notch-opening corner x = sqrt(140) = 11.8322 (NOT 12), while min X is the untouched
# -X rim at -12. So the true silhouette width is 12 + sqrt(140) = 23.8322, not the
# nominal Ø24 (short by 0.168 mm — 3x the 0.05 tolerance). The max *radius* is still
# 12.0 because those corners sit at sqrt(140 + 4) = 12. Both FRONT and TOP share this X
# extent. The brief's "BBOX_W ~ 24.0 +/- 0.05" is the nominal diameter and cannot hold
# on this part; we assert the grounded value and flag it. Y and Z extents are unaffected.
X_EXTENT = 12.0 + math.sqrt(140.0)   # 23.8322 — true silhouette width (keyway bites +X)
Y_EXTENT = 24.0                       # disk reaches y=+/-12 at x~0 (keyway far away)
Z_EXTENT = 60.0                       # keyway is full-length, z in [-30, 30]

OBS = {}


def _fail(msg):
    print("=== HLR_PROJECTION diagnostics ===", flush=True)
    for k in sorted(OBS):
        print("  %s = %s" % (k, OBS[k]))
    print("HLR_PROJECTION_PROOF_FAIL:", msg, flush=True)
    sys.exit(1)


# ── vector helpers (pure python) ─────────────────────────────────────────────
def _norm(v):
    m = math.sqrt(sum(c * c for c in v)) or 1.0
    return (v[0] / m, v[1] / m, v[2] / m)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


# ── edge discretisation: <= CHORD_TOL chord error, in whatever frame HLR emits ─
def _discretize(topo_edge):
    try:
        ad = BRepAdaptor_Curve(topo_edge)
        algo = GCPnts_QuasiUniformDeflection(ad, CHORD_TOL)
        if algo.IsDone() and algo.NbPoints() >= 2:
            pts = []
            for i in range(1, algo.NbPoints() + 1):
                p = algo.Value(i)
                pts.append((p.X(), p.Y(), p.Z()))
            return pts
    except Exception as e:
        print("DISCRETIZE_ERR:", repr(e))
    try:                                    # fallback: adaptor endpoints
        ad = BRepAdaptor_Curve(topo_edge)
        out = []
        for u in (ad.FirstParameter(), ad.LastParameter()):
            p = ad.Value(u)
            out.append((p.X(), p.Y(), p.Z()))
        return out
    except Exception as e:
        print("DISCRETIZE_FALLBACK_ERR:", repr(e))
        return []


def _edges_of(comp):
    edges = []
    try:
        if comp is None:
            return edges
        exp = TopExp_Explorer(comp, TopAbs_EDGE)
        while exp.More():
            edges.append(TopoDS.Edge_s(exp.Current()))
            exp.Next()
    except Exception as e:
        print("EDGES_OF_ERR:", repr(e))
    return edges


# ── direct OCP HLR fallback (HLRBRep_Algo + HLRBRep_HLRToShape) ───────────────
def _ocp_hlr(shape, cam, up):
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.HLRAlgo import HLRAlgo_Projector
    from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape

    n = _norm(cam)                          # projection-plane normal = view axis
    ux = _norm(_cross(up, n))               # local X so that local Y ~ up
    ax2 = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*n), gp_Dir(*ux))
    algo = HLRBRep_Algo()
    algo.Add(shape)
    algo.Projector(HLRAlgo_Projector(ax2))
    algo.Update()
    algo.Hide()
    hlr = HLRBRep_HLRToShape(algo)
    vis, hid = [], []
    for getter in ("VCompound", "OutLineVCompound", "Rg1LineVCompound"):
        try:
            vis += _edges_of(getattr(hlr, getter)())
        except Exception as e:
            print("HLR_" + getter + "_ERR:", repr(e))
    for getter in ("HCompound", "OutLineHCompound", "Rg1LineHCompound"):
        try:
            hid += _edges_of(getattr(hlr, getter)())
        except Exception:
            pass
    return vis, hid


# ── project one view: prefer project_to_viewport, else OCP direct ────────────
def _project(part, cam, up):
    method = None
    vis, hid = [], []
    if hasattr(part, "project_to_viewport"):
        try:
            res = part.project_to_viewport(cam, up)
            visible, hidden = res[0], res[1]
            vis = [e.wrapped for e in visible]
            hid = [e.wrapped for e in hidden]
            method = "project_to_viewport"
        except Exception as e:
            print("PROJECT_TO_VIEWPORT_ERR:", repr(e))
            method = None
    if method is None:
        vis, hid = _ocp_hlr(part.wrapped, cam, up)
        method = "ocp_direct"
    return method, vis, hid


# ── Step 1: the same keyed shaft as the earlier proofs ───────────────────────
part = Cylinder(radius=12, height=60) - Pos(12, 0, 0) * Box(6, 4, 60)

# ── Step 2: INTROSPECT the projection API before using it ────────────────────
OBS["HAS_PROJECT_TO_VIEWPORT"] = hasattr(part, "project_to_viewport")
print("HAS_PROJECT_TO_VIEWPORT:", OBS["HAS_PROJECT_TO_VIEWPORT"])
if OBS["HAS_PROJECT_TO_VIEWPORT"]:
    try:
        print("PROJECT_SIG:", str(inspect.signature(part.project_to_viewport)))
    except Exception as e:
        print("PROJECT_SIG_ERR:", repr(e))

# camera points (parallel projection: distance is irrelevant, direction is what matters)
VIEWS = {
    "FRONT": dict(cam=(0.0, 100.0, 0.0), up=(0.0, 0.0, 1.0)),   # view along -Y -> XZ plane
    "TOP":   dict(cam=(0.0, 0.0, 100.0), up=(0.0, 1.0, 0.0)),   # view along -Z -> XY plane
}
print("CALL_FRONT: project_to_viewport((0,100,0), up=(0,0,1))  [view along -Y]")
print("CALL_TOP:   project_to_viewport((0,0,100), up=(0,1,0))  [view along -Z]")


# ── Steps 3-4: project, discretise, reduce to 2D in the view plane ───────────
def _process(name, cfg):
    method, vis_edges, hid_edges = _project(part, cfg["cam"], cfg["up"])
    OBS[name + "_HLR_METHOD"] = method

    vis_pts_per_edge = [_discretize(e) for e in vis_edges]
    vis_pts_per_edge = [pe for pe in vis_pts_per_edge if pe]
    all_vis = [p for pe in vis_pts_per_edge for p in pe]
    if len(all_vis) < 2:
        _fail("%s produced < 2 visible points (HLR returned no usable geometry)" % name)

    # Reduce 3D -> 2D by dropping the axis with the smallest range. This is robust to
    # whether HLR flattens into world coords (view axis ~ const) or a local viewport
    # frame (local Z ~ 0): either way the two in-plane axes survive, in axis order.
    rng = []
    for i in range(3):
        col = [p[i] for p in all_vis]
        rng.append(max(col) - min(col))
    drop = rng.index(min(rng))
    keep = [i for i in range(3) if i != drop]
    OBS[name + "_AXIS_RANGES"] = [round(r, 4) for r in rng]
    OBS[name + "_DROPPED_AXIS"] = drop

    def to2d(p):
        return (p[keep[0]], p[keep[1]])

    vis2d = [to2d(p) for p in all_vis]
    # vertices = endpoints of each visible edge (matches section_geometry_proof, which
    # measured radius at wire vertices; edge interiors would report the floor midpoint
    # (9,0)=r9.0, not the corner sqrt(85), and the cross-check must compare like for like)
    vtx2d = []
    for pe in vis_pts_per_edge:
        vtx2d.append(to2d(pe[0]))
        vtx2d.append(to2d(pe[-1]))

    a = [q[0] for q in vis2d]
    b = [q[1] for q in vis2d]
    bbox_w = max(a) - min(a)
    bbox_h = max(b) - min(b)

    OBS[name + "_VISIBLE_EDGES"] = len(vis_edges)
    OBS[name + "_HIDDEN_EDGES"] = len(hid_edges)
    OBS[name + "_VISIBLE_POINTS"] = len(all_vis)
    OBS[name + "_BBOX_W"] = round(bbox_w, 4)
    OBS[name + "_BBOX_H"] = round(bbox_h, 4)
    if name == "TOP":
        radii = [math.hypot(qx, qy) for (qx, qy) in vtx2d]
        OBS["TOP_MIN_RADIUS"] = round(min(radii), 4)
        OBS["TOP_MAX_RADIUS"] = round(max(radii), 4)
    return name


for _n, _cfg in VIEWS.items():
    _process(_n, _cfg)

# HLR_METHOD is the same across views; surface it once as the headline observation.
OBS["HLR_METHOD"] = OBS.get("FRONT_HLR_METHOD")
print("HLR_METHOD:", OBS["HLR_METHOD"])

# ── OBSERVATIONS ─────────────────────────────────────────────────────────────
for k in ("FRONT_VISIBLE_EDGES", "FRONT_HIDDEN_EDGES", "FRONT_VISIBLE_POINTS",
          "FRONT_BBOX_W", "FRONT_BBOX_H", "FRONT_AXIS_RANGES", "FRONT_DROPPED_AXIS",
          "TOP_VISIBLE_EDGES", "TOP_HIDDEN_EDGES", "TOP_VISIBLE_POINTS",
          "TOP_BBOX_W", "TOP_BBOX_H", "TOP_AXIS_RANGES", "TOP_DROPPED_AXIS",
          "TOP_MIN_RADIUS", "TOP_MAX_RADIUS"):
    print("%s: %s" % (k, OBS.get(k)))

# OBSERVE (do NOT assert): hidden edges. The keyway floor sits behind cylinder material
# from -Y, so it is expected among the FRONT hidden edges (not the visible silhouette).
print("OBSERVE FRONT_HIDDEN_EDGES=%s (keyway floor expected here: behind the front "
      "cylinder wall from -Y)" % OBS.get("FRONT_HIDDEN_EDGES"))
print("OBSERVE TOP_HIDDEN_EDGES=%s (bottom rim, behind the top face)"
      % OBS.get("TOP_HIDDEN_EDGES"))
print("OBSERVE BBOX_W target=%.4f (=12+sqrt(140)) NOT 24.0: the keyway removes the +X "
      "rim (max X=sqrt(140)=11.8322); max RADIUS is still 12.0 at the notch corners"
      % X_EXTENT)

# ── Step 5: HARD ASSERTIONS ──────────────────────────────────────────────────
# (a) both views returned real edge geometry
if OBS["FRONT_VISIBLE_EDGES"] < 4:
    _fail("FRONT_VISIBLE_EDGES=%s < 4" % OBS["FRONT_VISIBLE_EDGES"])
if OBS["TOP_VISIBLE_EDGES"] < 4:
    _fail("TOP_VISIBLE_EDGES=%s < 4" % OBS["TOP_VISIBLE_EDGES"])
# (b) FRONT silhouette: X extent = keyway-bitten width 23.8322 (see X_EXTENT note),
#     Z extent = 60-long shaft.
if abs(OBS["FRONT_BBOX_W"] - X_EXTENT) > 0.05:
    _fail("FRONT_BBOX_W=%s not %.4f +/- 0.05 (keyway-bitten +X width)"
          % (OBS["FRONT_BBOX_W"], X_EXTENT))
if abs(OBS["FRONT_BBOX_H"] - Z_EXTENT) > 0.05:
    _fail("FRONT_BBOX_H=%s not 60.0 +/- 0.05" % OBS["FRONT_BBOX_H"])
# (c) TOP silhouette: X extent = 23.8322 (same keyway bite), Y extent = Ø24 disk.
if abs(OBS["TOP_BBOX_W"] - X_EXTENT) > 0.05:
    _fail("TOP_BBOX_W=%s not %.4f +/- 0.05 (keyway-bitten +X width)"
          % (OBS["TOP_BBOX_W"], X_EXTENT))
if abs(OBS["TOP_BBOX_H"] - Y_EXTENT) > 0.05:
    _fail("TOP_BBOX_H=%s not 24.0 +/- 0.05" % OBS["TOP_BBOX_H"])
# (d) CROSS-CHECK: the TOP outer profile IS the section profile (full-length keyway).
#     rim 12.0 and notch floor corner sqrt(85)=9.2195 — the SAME values
#     section_geometry_proof.py recovered by boolean intersect, here via HLR projection.
if abs(OBS["TOP_MAX_RADIUS"] - TRUE_MAX_R) > 0.05:
    _fail("TOP_MAX_RADIUS=%s not 12.0 +/- 0.05" % OBS["TOP_MAX_RADIUS"])
if abs(OBS["TOP_MIN_RADIUS"] - TRUE_MIN_R) > 0.05:
    _fail("TOP_MIN_RADIUS=%s not %.4f +/- 0.05" % (OBS["TOP_MIN_RADIUS"], TRUE_MIN_R))

print("value_summary: method=%s front_bbox=%.3fx%.3f top_bbox=%.3fx%.3f "
      "top_r=[%.4f,%.4f]"
      % (OBS["HLR_METHOD"], OBS["FRONT_BBOX_W"], OBS["FRONT_BBOX_H"],
         OBS["TOP_BBOX_W"], OBS["TOP_BBOX_H"],
         OBS["TOP_MIN_RADIUS"], OBS["TOP_MAX_RADIUS"]))
print("HLR_PROJECTION_PROOF_OK")
