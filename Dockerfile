FROM nvidia/cuda:12.9.2-cudnn-runtime-ubuntu24.04

ARG COMFYUI_REF=v0.30.0
ENV BUILTIN_CATALOG_DIR=/opt/comfy-cloud/catalog \
    COMFYUI_DIR=/opt/ComfyUI \
    COMFYUI_URL=http://127.0.0.1:8188 \
    DEBIAN_FRONTEND=noninteractive \
    MODE=pod \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates ffmpeg git python3 python3-pip python3-venv && \
    rm -rf /var/lib/apt/lists/*

RUN git clone --filter=blob:none --branch "${COMFYUI_REF}" --depth 1 \
      https://github.com/Comfy-Org/ComfyUI.git /opt/ComfyUI \
    && python3 -m pip install --break-system-packages -r /opt/ComfyUI/requirements.txt

WORKDIR /opt/comfy-cloud
COPY pyproject.toml README.md ./
COPY src ./src
COPY catalog ./catalog
COPY profiles ./profiles
RUN python3 -m pip install --break-system-packages .

EXPOSE 8000
CMD ["python3", "-m", "comfy_cloud.supervisor"]
