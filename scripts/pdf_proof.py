"""
Proof: our self-rendered dimension SVG survives SVG -> PDF with the value intact.

The image has no PDF producer of its own -- FreeCAD's PDF export is Gui-only and
unreachable under freecadcmd (proven in annotation_proof.py). So our signable
drawings self-render dimensions as raw SVG: <line>, <polygon> arrowheads, and
<text font-size='3.5'>30.00</text>. The risk this slice closes is a converter that
silently mangles that text -- a wrong dimension on a drawing someone signs. This
proof MUST fail if the value does not land as ink.

It isolates the SVG -> PDF variable ONLY (no FreeCAD import). Two SVGs are built at
the LOCKED A3-landscape sheet (420mm x 297mm): `plain` with part lines but no value,
and `dimmed` with the identical lines PLUS our dimension group. Both are rendered to
PDF by rsvg-convert. We then introspect (print page sizes, byte sizes, dark-pixel
counts) and hard-assert: A3 MediaBox, and STRICTLY more ink in dimmed than plain --
which proves the value + arrows rendered independent of whether cairo embedded the
text or outlined it. Text recovery via pdftotext is logged as a bonus, never a gate.

Run via the deployed /run endpoint -- NOT locally, NOT in CI.
"""
import os
import re
import subprocess
import sys

OUT = "/work/out"
os.makedirs(OUT, exist_ok=True)

VALUE = "30.00"                       # known target string
A3_W_PT = 420.0 / 25.4 * 72.0         # 1190.55 pt  (A3 landscape width)
A3_H_PT = 297.0 / 25.4 * 72.0         # 841.89 pt   (A3 landscape height)
TOL_PT = 3.0
DARK_THR = 200                        # anti-aliased 3.5pt glyph ink is grey (~128)

PLAIN_SVG = os.path.join(OUT, "plain.svg")
DIMMED_SVG = os.path.join(OUT, "dimmed.svg")
PLAIN_PDF = os.path.join(OUT, "plain.pdf")
DIMMED_PDF = os.path.join(OUT, "dimmed.pdf")

# A3 landscape is LOCKED: physical mm dimensions on the SVG so rsvg emits an A3
# MediaBox (viewBox alone renders at 96dpi -> a 315x222pt page, NOT A3).
SVG_OPEN = ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            'width="420mm" height="297mm" viewBox="0 0 420 297">')
SVG_CLOSE = "</svg>"


def _ln(a, b):
    return "<line x1='%.3f' y1='%.3f' x2='%.3f' y2='%.3f'/>" % (a[0], a[1], b[0], b[1])


def _poly(pts):
    return "<polygon points='%s' fill='black'/>" % " ".join("%.3f,%.3f" % p for p in pts)


# ── Shared part lines (a small U-shaped outline). Present in BOTH svgs. ───────
BASE = [
    ((160.0, 180.0), (260.0, 180.0)),
    ((160.0, 180.0), (160.0, 140.0)),
    ((260.0, 180.0), (260.0, 140.0)),
]
base_svg = ("<g stroke='black' stroke-width='0.35' fill='none'>"
            + "".join(_ln(a, b) for a, b in BASE) + "</g>")

# ── Dimension group EXACTLY as our annotation renders it: extension lines, a
#    dimension line, two <polygon> arrowheads, and the font-size 3.5 value text. ─
p0, p1 = (160.0, 140.0), (260.0, 140.0)     # measured edge endpoints
e0, e1 = (160.0, 128.0), (260.0, 128.0)     # extension-line ends (offset "up")
dl0, dl1 = (160.0, 130.0), (260.0, 130.0)   # dimension line
AH = 2.0                                     # arrowhead length
ah0 = [dl0, (dl0[0] + AH, dl0[1] - 1.0), (dl0[0] + AH, dl0[1] + 1.0)]
ah1 = [dl1, (dl1[0] - AH, dl1[1] - 1.0), (dl1[0] - AH, dl1[1] + 1.0)]
tx, ty = 210.0, 126.0

dim_group = (
    "<g stroke='black' stroke-width='0.3' fill='none'>"
    + _ln(p0, e0) + _ln(p1, e1) + _ln(dl0, dl1)
    + _poly(ah0) + _poly(ah1)
    + ("<text x='%.3f' y='%.3f' font-size='3.5' fill='black' text-anchor='middle'>%s</text>"
       % (tx, ty, VALUE))
    + "</g>"
)

with open(PLAIN_SVG, "w") as f:
    f.write(SVG_OPEN + "\n" + base_svg + "\n" + SVG_CLOSE + "\n")
with open(DIMMED_SVG, "w") as f:
    f.write(SVG_OPEN + "\n" + base_svg + "\n" + dim_group + "\n" + SVG_CLOSE + "\n")


# ── Helpers ──────────────────────────────────────────────────────────────────
def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=kw.get("text", True),
                          timeout=120)


def _svg2pdf(svg, pdf):
    r = _run(["rsvg-convert", "-f", "pdf", "-o", pdf, svg])
    if r.returncode != 0:
        _fail("rsvg-convert failed for %s\nstdout:%s\nstderr:%s"
              % (svg, r.stdout, r.stderr))


def _page_size_pt(pdf):
    r = _run(["pdfinfo", pdf])
    m = re.search(r"Page size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts", r.stdout)
    if not m:
        _fail("could not read page size from pdfinfo %s:\n%s" % (pdf, r.stdout))
    return float(m.group(1)), float(m.group(2))


def _dark_pixels(pdf, thr=DARK_THR):
    # Rasterise to a P5 PGM at 150dpi and count bytes below the darkness threshold.
    stem = pdf[:-4] + "_ras"
    r = subprocess.run(["pdftoppm", "-gray", "-r", "150", pdf, stem], timeout=120)
    if r.returncode != 0:
        _fail("pdftoppm failed for %s (rc=%d)" % (pdf, r.returncode))
    pgm = stem + "-1.pgm"
    if not os.path.exists(pgm):
        _fail("expected PGM not produced: %s" % pgm)
    d = open(pgm, "rb").read()
    m = re.match(rb"P5\s+(\d+)\s+(\d+)\s+(\d+)\s", d)
    if not m:
        _fail("not a P5 PGM: %s" % pgm)
    w, h = int(m.group(1)), int(m.group(2))
    pix = d[m.end():]
    return w, h, sum(1 for b in pix if b < thr)


def _fail(msg):
    print("=== PDF_FIDELITY diagnostics ===", flush=True)
    for pth in (PLAIN_SVG, DIMMED_SVG, PLAIN_PDF, DIMMED_PDF):
        print("  exists=%-5s size=%-8s %s"
              % (os.path.exists(pth),
                 os.path.getsize(pth) if os.path.exists(pth) else "-", pth))
    print("PDF_FIDELITY_PROOF_FAIL:", msg, flush=True)
    sys.exit(1)


# ── Render ───────────────────────────────────────────────────────────────────
_svg2pdf(PLAIN_SVG, PLAIN_PDF)
_svg2pdf(DIMMED_SVG, DIMMED_PDF)

# ── PHASE 1: INTROSPECT — print the real measurements before asserting. ──────
pw, ph = _page_size_pt(DIMMED_PDF)
plain_w, plain_h, plain_dark = _dark_pixels(PLAIN_PDF)
dim_w, dim_h, dim_dark = _dark_pixels(DIMMED_PDF)

print("SVG_OPEN:", SVG_OPEN)
print("TARGET_VALUE:", VALUE)
print("PLAIN_PDF_BYTES:", os.path.getsize(PLAIN_PDF))
print("DIMMED_PDF_BYTES:", os.path.getsize(DIMMED_PDF))
print("DIMMED_PAGE_PT: %.2f x %.2f  (A3 target %.2f x %.2f, tol +/-%.1f)"
      % (pw, ph, A3_W_PT, A3_H_PT, TOL_PT))
print("RASTER_PLAIN:  %dx%d dark=%d" % (plain_w, plain_h, plain_dark))
print("RASTER_DIMMED: %dx%d dark=%d" % (dim_w, dim_h, dim_dark))
print("INK_DELTA:", dim_dark - plain_dark)

# ── PHASE 2: HARD ASSERTIONS ─────────────────────────────────────────────────
# (a) both PDFs exist; dimmed is non-empty.
if not os.path.exists(PLAIN_PDF):
    _fail("plain.pdf missing")
if not (os.path.exists(DIMMED_PDF) and os.path.getsize(DIMMED_PDF) > 0):
    _fail("dimmed.pdf missing or empty")

# (b) dimmed MediaBox is A3 landscape within tolerance.
if abs(pw - A3_W_PT) > TOL_PT or abs(ph - A3_H_PT) > TOL_PT:
    _fail("dimmed page %.2f x %.2f pt not A3 landscape %.2f x %.2f (+/-%.1f)"
          % (pw, ph, A3_W_PT, A3_H_PT, TOL_PT))

# (c) INK DIFFERENTIAL — the real gate. The value + arrows must add ink.
if not (dim_dark > plain_dark):
    _fail("no ink differential: dimmed dark=%d <= plain dark=%d "
          "(the value/arrows did not render)" % (dim_dark, plain_dark))

# (d) SOFT SIGNAL — text-layer recovery is a bonus, NOT a gate (cairo may outline).
rec = _run(["pdftotext", DIMMED_PDF, "-"])
recovered = VALUE in (rec.stdout or "")
print("PDFTEXT_RECOVERED:", "yes" if recovered else "no")

print("value=%s  plain_dark=%d  dimmed_dark=%d  ink_delta=%d  page=%.1fx%.1fpt"
      % (VALUE, plain_dark, dim_dark, dim_dark - plain_dark, pw, ph))
print("PDF_FIDELITY_PROOF_OK")
