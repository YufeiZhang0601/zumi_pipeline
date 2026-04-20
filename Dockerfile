FROM python:3.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    exiftool \
    ffmpeg \
    git \
    iproute2 \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY requirements-docker.txt /tmp/requirements-docker.txt
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install --no-cache-dir --prefer-binary -r /tmp/requirements-docker.txt

COPY . /workspace

ENV PYTHONPATH=/workspace

CMD ["bash"]
