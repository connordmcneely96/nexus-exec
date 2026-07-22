"""
OBSERVATION proof: what does freecadcmd actually render for a SECTION view?

S2 needs section views (a cut through a keyway), not just the orthographic
projections multiview_proof.py already does. FreeCAD's App layer computes a
DrawViewSection, but the CUT-FACE HATCH and section geometry may be Gui-only --
the same App-computes / Gui-renders split annotation_proof.py proved for
dimensions (QGIViewDimension is Gui-only; xvfb does not help).

So before building the S2 feature, this proof OBSERVES what freecadcmd emits for a
section: it does NOT self-render a hatch, and it does NOT assume the section works.
It introspects the DrawViewSection surface, prints the observations the human needs
(path counts, hatch signals, whether the section reveals interior geometry), and
hard-asserts ONLY the things that must be true regardless of the Gui question --
namely that freecadcmd completed and that *some* section geometry rendered (catching
the total-failure / blank-section case).

Run via the deployed /run endpoint -- NOT locally, NOT in CI.
"""
import os
import subprocess
import tempfile

from build123d import Box, Cylinder, Pos, export_step

OUT = "/work/out"
os.makedirs(OUT, exist_ok=True)

STEP_PATH   = os.path.join(OUT, "sec_part.step")
BASE_SVG    = os.path.join(OUT, "sec_base.svg")
SECTION_SVG = os.path.join(OUT, "sec_section.svg")

# ── Step 1: build123d keyed shaft ────────────────────────────────────────────
# A cylinder with a rectangular keyway (key seat) cut into the +X surface, running
# the full axis length. The box is centred at x=12 (the surface at y=0) spanning
# x=9..15, so it removes a ~3mm-deep, 4mm-wide seat -- an INTERNAL feature a plain
# outer view shows only as edges, but a section cut reveals as a filled profile.
part = Cylinder(radius=12, height=60) - Pos(12, 0, 0) * Box(6, 4, 60)
export_step(part, STEP_PATH)
assert os.path.getsize(STEP_PATH) > 0, "sec_part.step is empty"
print(f"STEP exported: {STEP_PATH}")

# ── Step 2: FreeCAD — base view + section view, both to SVG ───────────────────
# f-string for {STEP_PATH}/{BASE_SVG}/{SECTION_SVG}. NO literal braces inside the
# body (they would collide with f-string interpolation) -- lists only, no dict/set
# literals. Sheet lock is A3 now (A3_Landscape first, A3 fallbacks, then A4).
FC_SCRIPT = f"""import FreeCAD, Part, TechDraw
import glob, os

# Template discovery — required before DrawViewPart projects.
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
    # Sheet is LOCKED to A3 landscape: prefer A3, then A4, then any landscape/blank.
    for key in ("A3_Landscape_blank", "A3_Landscape", "A4_Landscape_blank",
                "A4_Landscape", "Landscape", "blank"):
        for t in cands:
            if key.lower() in os.path.basename(t).lower():
                return t
    return cands[0]

_tmpl_path = _pick(_tmpls)
print("TEMPLATE_USED:", _tmpl_path)

doc = FreeCAD.newDocument("sec")
feat = doc.addObject("Part::Feature", "Model")
feat.Shape = Part.read("{STEP_PATH}")
page = doc.addObject("TechDraw::DrawPage", "Page")
template = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
template.Template = _tmpl_path
page.Template = template

# (a) base view — front, looking down -Y.
view = doc.addObject("TechDraw::DrawViewPart", "View_front")
page.addView(view)
view.Source = [feat]
view.Direction = FreeCAD.Vector(0, -1, 0)
view.Scale = 1.0
doc.recompute()

# (b) section view — transverse cut (normal +Z) through mid-height. The keyway spans
# the full axis, so the z=0 plane passes through the key seat and the cut face is the
# circular cross-section with the keyway notch. Introspect the real surface first,
# then set the plane properties guarded (this is what the human needs to see).
sec = doc.addObject("TechDraw::DrawViewSection", "Section")
page.addView(sec)
print("SEC_DIR:", [a for a in dir(sec) if not a.startswith("_")])

_setlog = []
try:
    sec.BaseView = view
    _setlog.append("BaseView=ok")
except Exception as e:
    _setlog.append("BaseView_ERR=" + str(e))
try:
    sec.Source = [feat]
    _setlog.append("Source=ok")
except Exception as e:
    _setlog.append("Source_ERR=" + str(e))
try:
    sec.SectionNormal = FreeCAD.Vector(0, 0, 1)
    _setlog.append("SectionNormal=ok")
except Exception as e:
    _setlog.append("SectionNormal_ERR=" + str(e))
try:
    sec.SectionOrigin = FreeCAD.Vector(0, 0, 0)
    _setlog.append("SectionOrigin=ok")
except Exception as e:
    _setlog.append("SectionOrigin_ERR=" + str(e))
try:
    sec.SectionDirection = "Up"
    _setlog.append("SectionDirection=Up")
except Exception as e:
    _setlog.append("SectionDirection_ERR=" + str(e))
try:
    sec.SectionSymbol = "A"
    _setlog.append("SectionSymbol=ok")
except Exception as e:
    _setlog.append("SectionSymbol_ERR=" + str(e))
try:
    sec.Scale = 1.0
    _setlog.append("Scale=ok")
except Exception as e:
    _setlog.append("Scale_ERR=" + str(e))
print("SEC_SET:", _setlog)

doc.recompute()

# Observe hatch-related attributes on the recomputed section, if present.
for _attr in ("HatchScale", "HatchPattern", "FileHatchPattern", "PatIncluded",
              "CutSurfaceDisplay", "SectionLineStyle"):
    try:
        print("SEC_ATTR_" + _attr + ":", getattr(sec, _attr))
    except Exception as e:
        print("SEC_ATTR_" + _attr + "_ERR:", str(e))

# Export both views' SVG. Guard each so a Gui-only failure is observed, not crashed.
try:
    _base_svg = TechDraw.viewPartAsSvg(view)
except Exception as e:
    print("BASE_SVG_ERR:", str(e))
    _base_svg = ""
open("{BASE_SVG}", "w").write(_base_svg)
print("BASE_SVG_LEN:", len(_base_svg))

try:
    _sec_svg = TechDraw.viewPartAsSvg(sec)
except Exception as e:
    print("SEC_SVG_ERR:", str(e))
    _sec_svg = ""
open("{SECTION_SVG}", "w").write(_sec_svg)
print("SEC_SVG_LEN:", len(_sec_svg))

print("SECTION_OK")
"""

with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
    f.write(FC_SCRIPT)
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
    raise SystemExit("SECTION_VIEW_PROOF_FAIL: " + msg)


# Echo the FreeCAD introspection so it is visible on success too.
for line in result.stdout.splitlines():
    if any(k in line for k in (
        "TEMPLATES_FOUND", "TEMPLATE_USED", "SEC_DIR", "SEC_SET", "SEC_ATTR_",
        "BASE_SVG_ERR", "SEC_SVG_ERR", "BASE_SVG_LEN", "SEC_SVG_LEN",
    )):
        print("  " + line)

# ── HARD ASSERTION (a): freecadcmd completed and printed SECTION_OK ──────────
if result.returncode != 0 or "SECTION_OK" not in result.stdout:
    _dump_and_raise(
        "freecadcmd failed (rc=%d, SECTION_OK present=%s)"
        % (result.returncode, "SECTION_OK" in result.stdout)
    )


def _read(path):
    if not os.path.exists(path):
        return ""
    with open(path) as fh:
        return fh.read()


base_svg = _read(BASE_SVG)
sec_svg = _read(SECTION_SVG)

# ── OBSERVATIONS (print, do NOT assert — this is the data the human decides on) ─
base_paths = base_svg.count("<path")
sec_paths = sec_svg.count("<path")
sec_lines = sec_svg.count("<line")
patt = sec_svg.count("<pattern")
url_fill = sec_svg.count("url(#")
hatch_kw = sec_svg.lower().count("hatch")

# Hatch: a <pattern> def, a url(#) fill reference, or a "hatch" keyword are strong
# signals of cut-face hatching. Many short parallel <line> segments are a weaker
# signal — printed raw so the human can judge parallel-line hatching too.
hatch_present = "yes" if (patt > 0 or url_fill > 0 or hatch_kw > 0) else "no"
# Differs: the section reveals geometry the base does not if its path count is
# materially different. Raw counts + svg lengths printed as backup.
section_differs = "yes" if sec_paths != base_paths else "no"

print("SECTION_PATHS:", sec_paths)
print("BASE_PATHS:", base_paths)
print("HATCH_PRESENT:", hatch_present)
print("  hatch_signals: pattern=%d url_fill=%d hatch_kw=%d line_segments=%d"
      % (patt, url_fill, hatch_kw, sec_lines))
print("SECTION_DIFFERS:", section_differs)
print("  base_svg_len=%d section_svg_len=%d" % (len(base_svg), len(sec_svg)))

# ── HARD ASSERTIONS (b) + (c): the section must have rendered SOME geometry ───
if not (os.path.exists(SECTION_SVG) and len(sec_svg) > 0):
    _dump_and_raise("sec_section.svg missing or empty")
if sec_paths < 1:
    _dump_and_raise(
        "sec_section.svg contains no <path> (section rendered blank) -- "
        "section geometry is Gui-only under freecadcmd"
    )

print("value_summary: section_paths=%d base_paths=%d hatch=%s differs=%s"
      % (sec_paths, base_paths, hatch_present, section_differs))
print("SECTION_VIEW_PROOF_OK")
