#!/usr/bin/env bash
# Compila el APK de Refractory Capture usando Podman + Ubuntu 22.04
# Uso: ./build_apk.sh

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
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq && apt-get install -y \
    python3 python3-pip build-essential git \
    ffmpeg libsdl2-dev libsdl2-image-dev libsdl2-mixer-dev libsdl2-ttf-dev \
    libportmidi-dev libswscale-dev libavformat-dev libavcodec-dev \
    zlib1g-dev libgstreamer1.0-0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    libltdl-dev libffi-dev libssl-dev autoconf libtool pkg-config \
    zip unzip openjdk-17-jdk ccache curl cmake \
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

echo ">>> Compilando APK..."
echo "    App:   $APP_DIR"
echo "    Caché: $CACHE_DIR"
echo ""

# Si p4a existe sin .git, eliminarlo para que buildozer re-clone limpio
P4A_HOST="$APP_DIR/.buildozer/android/platform/python-for-android"
if [ -d "$P4A_HOST" ] && [ ! -d "$P4A_HOST/.git" ]; then
    echo ">>> p4a sin .git — eliminando para re-clonación limpia..."
    rm -rf "$P4A_HOST"
fi

# ── Script Python de parche (sin problemas de quoting) ───────────────────────
PATCH_SCRIPT="$(mktemp /tmp/patch_p4a_XXXX.py)"
cat > "$PATCH_SCRIPT" << 'PYEOF'
#!/usr/bin/env python3
"""Patch p4a's pythonpackage.py to not crash when pip dry-run finds no binary wheels.

The problem: newer p4a calls pip with --only-binary=:all: --dry-run to check for
prebuilt wheels. When none exist (pure Python packages on Android), pip exits with
code 1 or 2 and p4a raises BuildInterruptingException instead of falling back to
its own recipes.

The fix: return an empty result instead of raising when pip fails.
"""
import sys, re, os

path = sys.argv[1]
if not os.path.exists(path):
    print(f"File not found: {path}")
    sys.exit(0)

content = open(path).read()

if '# PATCHED_BY_BUILD_SCRIPT' in content:
    print("Already patched, skipping.")
    sys.exit(0)

if '--only-binary' not in content:
    print("Pattern '--only-binary' not found — file may be a different version.")
    # Show what's in the file for debugging
    lines = content.split('\n')
    print(f"File has {len(lines)} lines. First 20:")
    for i, l in enumerate(lines[:20]):
        print(f"  {i+1}: {l}")
    sys.exit(0)

patched = content

# Fix 1: except sh.ErrorReturnCode_N -> return {} instead of raising
patched = re.sub(
    r'(except\s+sh\.ErrorReturnCode[_\w]*(?:\s+as\s+\w+)?\s*:)\s*\n(\s+)raise\b',
    r'\1\n\2return {}  # PATCHED_BY_BUILD_SCRIPT',
    patched
)

# Fix 2: bare except + raise
patched = re.sub(
    r'(except\s+Exception(?:\s+as\s+\w+)?\s*:)\s*\n(\s+)raise\b',
    r'\1\n\2return {}  # PATCHED_BY_BUILD_SCRIPT',
    patched
)

# Fix 3: if returncode != 0 -> return {} instead of raising
patched = re.sub(
    r'(if\s+\w*\.?returncode\s*!=\s*0\s*:)\s*\n(\s+)raise\b',
    r'\1\n\2return {}  # PATCHED_BY_BUILD_SCRIPT',
    patched
)

# Fix 4: Replace ALL raise BuildInterruptingException in functions that deal with pip/packages
# This is a broader fix as a fallback
patched = re.sub(
    r'raise\s+Build(?:Interrupting)?Exception\([^)]*only.binary[^)]*\)',
    'return {}  # PATCHED_BY_BUILD_SCRIPT',
    patched, flags=re.DOTALL
)

if patched == content:
    print("WARNING: No patterns matched. Showing lines around '--only-binary':")
    lines = content.split('\n')
    for i, line in enumerate(lines):
        if '--only-binary' in line:
            start = max(0, i - 8)
            end = min(len(lines), i + 20)
            print(f"\n--- Context around line {i+1} ---")
            for j in range(start, end):
                print(f"  {j+1}: {lines[j]}")
else:
    open(path, 'w').write(patched)
    print(f"Successfully patched {path}")
PYEOF

# ── Script interno del contenedor ────────────────────────────────────────────
INNER_SCRIPT="$(mktemp /tmp/buildozer_inner_XXXX.sh)"
cat > "$INNER_SCRIPT" << 'INNEREOF'
#!/usr/bin/env bash
set -e
P4A=/app/.buildozer/android/platform/python-for-android
PYPACK="$P4A/pythonforandroid/pythonpackage.py"
BUILD=/app/.buildozer/android/platform/build-arm64-v8a_armeabi-v7a

echo ">>> Primera pasada (descarga p4a y dependencias)..."
yes | buildozer -v android debug 2>&1 || true

echo ""
echo ">>> Aplicando parche a pythonpackage.py..."
python3 /tmp/patch_p4a.py "$PYPACK"

# También parchear Python a 3.10.10 si el recipe tiene 3.14.x
for recipe in hostpython3 python3; do
    f="$P4A/pythonforandroid/recipes/$recipe/__init__.py"
    if [ -f "$f" ]; then
        sed -i "s/version = '3\.[0-9]*\.[0-9]*'/version = '3.10.10'/" "$f" 2>/dev/null || true
        sed -i 's/version = "3\.[0-9]*\.[0-9]*"/version = "3.10.10"/' "$f" 2>/dev/null || true
        echo ">>> $recipe: $(grep 'version = ' $f | head -1)"
    fi
done

# Limpiar builds de Python si la version cambio
rm -rf "$BUILD/build/other_builds/hostpython3" \
       "$BUILD/build/other_builds/python3" \
       "$BUILD/dists"

echo ""
echo ">>> Segunda pasada (compilación con parche aplicado)..."
yes | buildozer -v android debug 2>&1
INNEREOF
chmod +x "$INNER_SCRIPT"

podman run --rm \
    -v "$APP_DIR:/app:z" \
    -v "$CACHE_DIR:/root/.buildozer:z" \
    -v "$INNER_SCRIPT:/tmp/build_inner.sh:z" \
    -v "$PATCH_SCRIPT:/tmp/patch_p4a.py:z" \
    -e CCACHE_DIR=/root/.buildozer/.ccache \
    -w /app \
    "$IMAGE" \
    bash /tmp/build_inner.sh

rm -f "$INNER_SCRIPT" "$PATCH_SCRIPT"

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
    echo "✗ No se encontró el APK."
    exit 1
fi
