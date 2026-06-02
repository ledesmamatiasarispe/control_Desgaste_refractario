#!/usr/bin/env bash
# Compila el APK de Refractory Capture usando Podman + Ubuntu 20.04
# Uso: ./build_apk.sh
#
# Primera ejecución: ~30-60 min (descarga SDK/NDK ~3 GB)
# Siguientes:        ~5-10 min  (caché en ~/.buildozer_local)

set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$REPO_DIR/android_app"
CACHE_DIR="$HOME/.buildozer_local"
IMAGE="refractory-buildozer:latest"

mkdir -p "$CACHE_DIR"

# ── Construir imagen si no existe ────────────────────────────────────────────
if ! podman image exists "$IMAGE"; then
    echo ">>> Construyendo imagen de build (una sola vez)..."
    podman build -t "$IMAGE" -f - "$REPO_DIR" <<'DOCKERFILE'
FROM ubuntu:20.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq && apt-get install -y \
    python3 python3-pip build-essential git \
    ffmpeg libsdl2-dev libsdl2-image-dev libsdl2-mixer-dev libsdl2-ttf-dev \
    libportmidi-dev libswscale-dev libavformat-dev libavcodec-dev \
    zlib1g-dev libgstreamer1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    libltdl-dev libffi-dev libssl-dev autoconf libtool pkg-config \
    zip unzip openjdk-17-jdk ccache curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --upgrade pip wheel setuptools && \
    pip3 install "buildozer==1.5.0" "cython==0.29.37" virtualenv

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH="$JAVA_HOME/bin:$PATH"
ENV ANDROID_SDK_ROOT=/root/.buildozer/android/platform/android-sdk

WORKDIR /app
DOCKERFILE
    echo ">>> Imagen construida."
fi

# ── Ejecutar buildozer ───────────────────────────────────────────────────────
echo ">>> Compilando APK..."
echo "    App:   $APP_DIR"
echo "    Caché: $CACHE_DIR"
echo ""

podman run --rm \
    -v "$APP_DIR:/app:z" \
    -v "$CACHE_DIR:/root/.buildozer:z" \
    -e CCACHE_DIR=/root/.buildozer/.ccache \
    -w /app \
    "$IMAGE" \
    bash -c "yes | buildozer -v android debug 2>&1"

# ── Copiar APK al directorio raíz ───────────────────────────────────────────
APK=$(find "$APP_DIR/bin" -name "*.apk" 2>/dev/null | head -1)
if [ -n "$APK" ]; then
    VERSION=$(python3 "$APP_DIR/version.py")
    DEST="$REPO_DIR/refractory-capture-v${VERSION}.apk"
    cp "$APK" "$DEST"
    echo ""
    echo "✓ APK generado: $DEST"
    ls -lh "$DEST"
else
    echo "✗ No se encontró el APK. Revisá el log arriba."
    exit 1
fi
