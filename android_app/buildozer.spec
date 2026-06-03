[app]
title = Refractory Capture
package.name = refractorycapture
package.domain = com.refractoryanalyzer
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
version = 1.0.3

# TEST MINIMO — solo kivy, sin kivymd/requests/plyer/pillow
requirements = python3,kivy==2.3.0

orientation = portrait

# Android
android.permissions = INTERNET
android.minapi = 24
android.targetapi = 33
android.ndk = 25c
android.sdk = 33
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
# android.features no soportado en p4a v2023.09.16 (las permisos de CAMERA ya implican la feature)

[buildozer]
log_level = 2
warn_on_root = 1

# Pinnar p4a a versión estable 2023 que usa recipes propias (no wheels)
p4a.fork = kivy
p4a.branch = v2023.09.16
