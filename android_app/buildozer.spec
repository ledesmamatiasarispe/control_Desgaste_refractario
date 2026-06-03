[app]
title = Refractory Capture
package.name = refractorycapture
package.domain = com.refractoryanalyzer
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
version = 1.0.4

# TEST MINIMO — solo kivy, sin kivymd/requests/plyer/pillow
requirements = python3,kivy==2.3.0

orientation = portrait

# Android
android.permissions = INTERNET
android.minapi = 24
android.targetapi = 35
# NDK r27c genera alineacion de 16 KB requerida por Android 15+
android.ndk = 27c
android.sdk = 35
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1

# Pinnar p4a a versión estable 2023 que usa recipes propias (no wheels)
p4a.fork = kivy
p4a.branch = v2023.09.16
