FROM docker.io/cloudflare/sandbox:0.12.1

# libGL.so.1 required by cadquery-ocp (OpenCascade); offscreen/GL deps for OpenSCAD/FreeCAD headless.
# librsvg2-bin -> rsvg-convert (cairo PDF surface, no Qt, no display); poppler-utils -> pdftoppm/pdftotext;
# fonts-dejavu-core -> a real font so <text> renders as ink, not tofu.
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
      libgl1 \
      openscad \
      freecad \
      xvfb \
      libxrender1 \
      libxext6 \
      librsvg2-bin \
      poppler-utils \
      fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Install Python 3.12 via uv and create venv
RUN uv python install 3.12 && uv venv --python 3.12 /opt/venv

# Activate venv for subsequent layers and at runtime
ENV VIRTUAL_ENV="/opt/venv"
ENV PATH="/opt/venv/bin:${PATH}"

# Install build123d via uv (OpenCascade/OCP bundled). CadQuery deferred to its own
# lane: it imports OCP.IVtkOCC VTK bindings absent from build123d's OCP pin.
RUN uv pip install build123d

# Warm-import gates — each fails the build if the tool is broken
RUN python -c "import build123d; print('build123d import OK')"
RUN openscad --version
# FreeCAD headless gate: Debian binary is lowercase `freecadcmd`; run a script file and
# tee output so a failure is visible in the build log. grep sets the exit code (fail-closed).
RUN echo "freecad binaries present:" && (ls /usr/bin/ | grep -i freecad || true); \
    printf 'import FreeCAD, TechDraw\nprint("freecad OK")\n' > /tmp/fc_check.py; \
    freecadcmd /tmp/fc_check.py 2>&1 | tee /tmp/fc.log; \
    grep -q "freecad OK" /tmp/fc.log

# PDF render gate: rsvg-convert (SVG->PDF) then pdftoppm (PDF->PGM) must turn a
# <text font-size='3.5'>30.00</text> plus one <line> into real ink. A blank raster =
# broken font/toolchain = build fails. grep sets the exit code (fail-closed), matching
# the FreeCAD gate. The PGM is parsed P5 by a tiny inline python: any pixel below the
# darkness threshold (200) proves ink landed on the page. The threshold admits the
# anti-aliased grey glyphs of 3.5-unit text (min value ~128), so the font — not just the
# line — is proven to render. A blank page yields zero such pixels and fails.
RUN printf '%s\n' \
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 420 297">' \
      '  <line x1="50" y1="150" x2="200" y2="150" stroke="black" stroke-width="0.5"/>' \
      "  <text x=\"100\" y=\"150\" font-size=\"3.5\" fill=\"black\">30.00</text>" \
      '</svg>' > /tmp/gate.svg; \
    rsvg-convert -f pdf -o /tmp/gate.pdf /tmp/gate.svg; \
    pdftoppm -gray -r 150 /tmp/gate.pdf /tmp/gate; \
    python3 -c 'import re; d=open("/tmp/gate-1.pgm","rb").read(); m=re.match(rb"P5\s+\d+\s+\d+\s+\d+\s", d); pix=d[m.end():]; dark=sum(b<200 for b in pix); print(("PDF_GATE_OK" if dark>0 else "PDF_GATE_BLANK"), "dark_pixels=%d" % dark)' | tee /tmp/gate.log; \
    grep -q "PDF_GATE_OK" /tmp/gate.log

EXPOSE 8080
