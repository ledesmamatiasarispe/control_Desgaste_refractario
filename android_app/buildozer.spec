[app]
title = Refractory Capture
package.name = refractorycapture
package.domain = com.refractoryanalyzer
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
version = 1.0

# Pillow para JPEG, requests para API GitHub + descarga APK
requirements = python3,kivy==2.3.0,kivymd==1.1.1,requests,plyer,pillow

orientation = portrait

# Android
android.permissions = CAMERA, INTERNET, WRITE_EXTERNAL_STORAGE, READ_EXTERNAL_STORAGE, ACCESS_NETWORK_STATE, REQUEST_INSTALL_PACKAGES
android.minapi = 24
android.targetapi = 33
android.ndk = 25c
android.sdk = 33
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.features = android.hardware.camera, android.hardware.camera.autofocus

[buildozer]
log_level = 2
warn_on_root = 1
