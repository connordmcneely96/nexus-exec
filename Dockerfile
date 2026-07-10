FROM docker.io/cloudflare/sandbox:0.12.1

# libGL.so.1 required by cadquery-ocp (OpenCascade); offscreen/GL deps for OpenSCAD/FreeCAD headless
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
      libgl1 \
      openscad \
      freecad \
      xvfb \
      libxrender1 \
      libxext6 \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Install Python 3.12 via uv and create venv
RUN uv python install 3.12 && uv venv --python 3.12 /opt/venv

# Activate venv for subsequent layers and at runtime
ENV VIRTUAL_ENV="/opt/venv"
ENV PATH="/opt/venv/bin:${PATH}"

# Install build123d + cadquery via uv in one solve so OCP version is co-resolved
RUN uv pip install build123d cadquery

# Warm-import gates — each fails the build if the tool is broken
RUN python -c "import build123d; print('build123d import OK')"
RUN openscad --version
RUN python -c "import cadquery; print('cadquery OK')"
RUN printf 'import FreeCAD, TechDraw\nprint("freecad OK")\n' | FreeCADCmd 2>&1 | grep -q "freecad OK"

EXPOSE 8080
