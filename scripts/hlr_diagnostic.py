"""
HLR DIAGNOSTIC — dump what project_to_viewport actually returns. No fix, no assertions.

hlr_projection_proof.py ran on the deployed container and produced values that are
geometrically impossible for this part:
    FRONT_BBOX_W    23.8377  (true 23.8322 — off 0.0055, though it passed +/-0.05)
    TOP_BBOX_W      24.0     (true 23.8322 — needs a point at x=+12 the keyway REMOVES)
    TOP_MAX_RADIUS  12.3589  (NO point on this solid has XY-radius > 12)
    TOP_MIN_RADIUS  9.4109   (expected sqrt(85)=9.2195, or 9.0 if the floor edge split)
project_to_viewport works (method + flattening confirmed: third-axis range 0.0). The
question is WHAT geometry it returns. This slice ONLY dumps data — no assertions, no
non-zero exit on geometry values. Runs via the deployed /run endpoint.

Same epistemics as the prior proofs, but pure observation: self-ground on the solid's
own bounding box first, then compare everything against THAT, not hardcoded constants.
"""
import math
import sys

from build123d import Box, Cylinder, Pos
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GCPnts import GCPnts_QuasiUniformDeflection

CHORD = 0.05
SQRT140 = math.sqrt(140.0)   # 11.832159566199232 — true +X notch-opening corner


def _vt(v):
    try:
        return (float(v.X), float(v.Y), float(v.Z))
    except Exception:
        pass
    try:
        t = v.to_tuple()
        return (float(t[0]), float(t[1]), float(t[2]))
    except Exception:
        return None


def _discretize(topo_edge):
    try:
        ad = BRepAdaptor_Curve(topo_edge)
        algo = GCPnts_QuasiUniformDeflection(ad, CHORD)
        if algo.IsDone() and algo.NbPoints() >= 2:
            return [(algo.Value(i).X(), algo.Value(i).Y(), algo.Value(i).Z())
                    for i in range(1, algo.NbPoints() + 1)]
    except Exception as e:
        print("  DISCRETIZE_ERR:", repr(e))
    try:
        ad = BRepAdaptor_Curve(topo_edge)
        return [(ad.Value(u).X(), ad.Value(u).Y(), ad.Value(u).Z())
                for u in (ad.FirstParameter(), ad.LastParameter())]
    except Exception as e:
        print("  DISCRETIZE_FALLBACK_ERR:", repr(e))
        return []


def _geom_type(e):
    for f in (lambda: e.geom_type, lambda: e.geom_type()):
        try:
            return str(f())
        except Exception:
            continue
    return "?"


def _edge_pt(e, t):
    for get in (lambda: e @ t, lambda: e.position_at(t)):
        try:
            return _vt(get())
        except Exception:
            continue
    return None


def _reduce_axis(all_pts):
    rng = []
    for i in range(3):
        col = [p[i] for p in all_pts]
        rng.append(max(col) - min(col))
    drop = rng.index(min(rng))
    keep = [i for i in range(3) if i != drop]
    return drop, keep, rng


# ── the part ─────────────────────────────────────────────────────────────────
part = Cylinder(radius=12, height=60) - Pos(12, 0, 0) * Box(6, 4, 60)
print("PART_TYPE:", type(part).__name__)
print("HAS_PROJECT_TO_VIEWPORT:", hasattr(part, "project_to_viewport"))

# ── STEP 1: SELF-GROUND on the solid's own bounding box ──────────────────────
try:
    bb = part.bounding_box()
    mn, mx = _vt(bb.min), _vt(bb.max)
    print("BBOX_SOLID: xmin=%r xmax=%r ymin=%r ymax=%r zmin=%r zmax=%r"
          % (mn[0], mx[0], mn[1], mx[1], mn[2], mx[2]))
    print("BBOX_SIZE:   %r x %r x %r"
          % (mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2]))
    print("BBOX_CENTER: %r %r %r  (non-zero X center => default look_at is shifted)"
          % ((mn[0] + mx[0]) / 2.0, (mn[1] + mx[1]) / 2.0, (mn[2] + mx[2]) / 2.0))
except Exception as e:
    print("BBOX_SOLID_ERR:", repr(e))


def _project(args):
    """Return (visible_list, hidden_list) or raise."""
    res = part.project_to_viewport(*args)
    return list(res[0]), list(res[1])


def _run_bbox(label, args):
    """Run project_to_viewport, print visible count + 2D bbox. Return dict or None."""
    try:
        vis, hid = _project(args)
    except Exception as ex:
        print("%s_ERROR: %r" % (label, ex))
        return None
    edge_pts = [(e, _discretize(e.wrapped)) for e in vis]
    all_pts = [p for _, dp in edge_pts for p in dp]
    print("%s_VISIBLE_EDGES: %d   HIDDEN_EDGES: %d" % (label, len(vis), len(hid)))
    if not all_pts:
        print("%s_BBOX: (no points)" % label)
        return {"vis": vis, "edge_pts": edge_pts, "keep": None}
    drop, keep, rng = _reduce_axis(all_pts)
    xs = [p[keep[0]] for p in all_pts]
    ys = [p[keep[1]] for p in all_pts]
    print("%s_BBOX: xmin=%r xmax=%r ymin=%r ymax=%r  W=%r H=%r "
          "(dropped_axis=%d ranges=%s)"
          % (label, min(xs), max(xs), min(ys), max(ys),
             max(xs) - min(xs), max(ys) - min(ys), drop,
             [round(r, 6) for r in rng]))
    return {"vis": vis, "edge_pts": edge_pts, "keep": keep}


# ── STEP 2: FOUR TOP runs to isolate look_at / distance / focus ──────────────
print("---- STEP 2: TOP view, four calls ----")
TOP_CALLS = [
    ("T1", ((0, 0, 100), (0, 1, 0))),
    ("T2", ((0, 0, 100), (0, 1, 0), (0, 0, 0))),
    ("T3", ((0, 0, 1000), (0, 1, 0), (0, 0, 0))),
    ("T4", ((0, 0, 100), (0, 1, 0), (0, 0, 0), None)),
]
top_runs = {}
for _label, _args in TOP_CALLS:
    top_runs[_label] = _run_bbox(_label, _args)
print("NOTE: if T2_BBOX differs from T1_BBOX, the default look_at is the culprit.")

# ── STEP 3: per-edge dump for T2 (or T1 if T2 failed) ────────────────────────
print("---- STEP 3: per-edge dump ----")
chosen = "T2" if top_runs.get("T2") else "T1"
data = top_runs.get(chosen)
print("PER_EDGE_VIEW:", chosen)
if data and data.get("keep") is not None:
    keep = data["keep"]
    edge_pts = data["edge_pts"]
    g_maxx = (-1.0, None, None)   # (|x|, edge_index, (x,y))
    g_maxr = (-1.0, None, None)   # (r,   edge_index, (x,y))
    for i, (e, dp) in enumerate(edge_pts):
        s = _edge_pt(e, 0.0)
        en = _edge_pt(e, 1.0)
        s2 = (s[keep[0]], s[keep[1]]) if s else None
        e2 = (en[keep[0]], en[keep[1]]) if en else None
        maxabs_x = 0.0
        maxr = 0.0
        maxr_pt = None
        maxx_pt = None
        for p in dp:
            x, y = p[keep[0]], p[keep[1]]
            r = math.hypot(x, y)
            if abs(x) > maxabs_x:
                maxabs_x = abs(x)
                maxx_pt = (x, y)
            if r > maxr:
                maxr = r
                maxr_pt = (x, y)
        if i < 40:
            print("EDGE[%d] type=%s npts=%d start=%s end=%s maxabs_x=%r max_r=%r"
                  % (i, _geom_type(e), len(dp), s2, e2, maxabs_x, maxr))
        if maxx_pt is not None and maxabs_x > g_maxx[0]:
            g_maxx = (maxabs_x, i, maxx_pt)
        if maxr_pt is not None and maxr > g_maxr[0]:
            g_maxr = (maxr, i, maxr_pt)
    if len(edge_pts) > 40:
        print("... (%d visible edges total; first 40 shown)" % len(edge_pts))
    print("MAXX_EDGE:", g_maxx[1])
    print("MAXX_POINT:", g_maxx[2], " |x|=%r" % g_maxx[0])
    print("MAXR_EDGE:", g_maxr[1])
    print("MAXR_POINT:", g_maxr[2], " r=%r" % g_maxr[0])
else:
    print("PER_EDGE: no usable %s run" % chosen)

# ── STEP 4: EXACTNESS TEST on the FRONT view ─────────────────────────────────
print("---- STEP 4: FRONT exactness ----")
try:
    fvis, fhid = _project(((0, 100, 0), (0, 0, 1), (0, 0, 0)))
    fedge = [(e, _discretize(e.wrapped)) for e in fvis]
    fall = [p for _, dp in fedge for p in dp]
    if fall:
        fdrop, fkeep, frng = _reduce_axis(fall)
        print("FRONT_DROPPED_AXIS:", fdrop, " ranges=", [repr(r) for r in frng])
        # max |x| (the true extreme — will be the -X rim at -12) ...
        best_abs = None
        # ... and max +x (the keyway-affected +X notch corner, where sqrt(140) lives)
        best_pos = None
        for p in fall:
            x = p[fkeep[0]]
            if best_abs is None or abs(x) > abs(best_abs[0]):
                best_abs = (x, p)
            if best_pos is None or x > best_pos[0]:
                best_pos = (x, p)
        print("FRONT_MAXABSX_FULL:", repr(best_abs[0]),
              " raw3d=", repr(best_abs[1]), " (max |x| — expect the -X rim ~ -12)")
        print("FRONT_MAXX_FULL:", repr(best_pos[0]),
              " raw3d=", repr(best_pos[1]), " (max +x — the +X notch corner)")
        print("FRONT_MAXX_DELTA:", repr(best_pos[0] - SQRT140),
              " (vs sqrt(140)=%r)" % SQRT140)
        print("FRONT_GEOM_TYPES:", [_geom_type(e) for e, _ in fedge])
    else:
        print("FRONT: no visible points")
except Exception as ex:
    print("FRONT_ERROR: %r" % ex)
    fvis = []

# ── STEP 5: RAW TYPE observations ────────────────────────────────────────────
print("---- STEP 5: raw types ----")
sample = None
for _lbl in ("T2", "T1"):
    d = top_runs.get(_lbl)
    if d and d.get("vis"):
        sample = d["vis"]
        break
if sample is None and 'fvis' in dir() and fvis:
    sample = fvis
if sample:
    print("RAW_ELEMENT_TYPE:", type(sample[0]).__name__)
    gts = [_geom_type(e) for e in sample]
    print("RAW_GEOM_TYPES:", gts)
    non_lc = [g for g in gts
              if "LINE" not in g.upper() and "CIRC" not in g.upper()]
    print("NON_LINE_OR_CIRCLE_TYPES:", non_lc)
    print("HAS_SPLINE_LIKE:",
          any(k in g.upper() for g in gts
              for k in ("BSPLINE", "BEZIER", "SPLINE", "ELLIPSE", "OFFSET")))
else:
    print("RAW_TYPES: no sample edges available")

print("HLR_DIAGNOSTIC_DONE")
sys.stdout.flush()
