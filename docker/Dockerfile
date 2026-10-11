# Experimental: the pipeline in a container (ffmpeg + COLMAP + Brush + splat_studio).
#
# Build (from the repo root):  docker build -t splat-studio -f docker/Dockerfile .
# Run (via the wrapper):       docker/splat IMG_1234.MOV --quality preview
#
# GPU: Brush trains through Vulkan.
#  - Linux + NVIDIA: run with --gpus all (needs nvidia-container-toolkit) -> full speed.
#  - macOS (Docker Desktop) / no GPU: Mesa's software Vulkan works but is very slow, so on a
#    Mac the wrapper runs frames + COLMAP here and trains with native Brush on the Mac's GPU.

# ---------------------------------------------------------------- stage 1: build Brush
FROM rust:1.93 AS brush-builder
ARG BRUSH_REF=1388f74c6fe0236f68ee4915564bf00e9d2e3747
RUN git clone https://github.com/ArthurBrussee/brush.git /build && cd /build && git checkout "$BRUSH_REF"
WORKDIR /build
RUN cargo build --release -p brush-app

# ---------------------------------------------------------------- stage 2: runtime
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive \
    QT_QPA_PLATFORM=offscreen \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=all \
    SPLAT_IN_CONTAINER=1 \
    SPLAT_STUDIO_RATES=/work/.splat_cache/rates.json \
    PYTHONPATH=/opt/splat-studio/src \
    HOME=/tmp \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg colmap python3 python3-opencv libvulkan1 mesa-vulkan-drivers ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=brush-builder /build/target/release/brush /usr/local/bin/brush
COPY src /opt/splat-studio/src

WORKDIR /work
ENTRYPOINT ["python3", "-m", "splat_studio", "run"]
CMD ["--help"]
