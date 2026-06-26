FROM docker.io/cloudflare/sandbox:0.12.1

# libGL.so.1 required by cadquery-ocp (OpenCascade)
RUN apt-get update -qq && apt-get install -y --no-install-recommends libgl1 && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Install Python 3.12 via uv and create venv
RUN uv python install 3.12 && uv venv --python 3.12 /opt/venv

# Activate venv for subsequent layers and at runtime
ENV VIRTUAL_ENV="/opt/venv"
ENV PATH="/opt/venv/bin:${PATH}"

# Install build123d via uv (OpenCascade bundled in wheel)
RUN uv pip install build123d

# Warm import — fails build if broken
RUN python -c "import build123d; print('build123d import OK')"

EXPOSE 8080
