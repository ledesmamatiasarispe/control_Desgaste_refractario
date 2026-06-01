[app]
title = Refractory Capture
package.name = refractorycapture
package.domain = com.refractoryanalyzer
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
version = 1.0

# Dependencies — Pillow for JPEG encoding, requests for GitHub API + download
requirements = python3,kivy==2.3.0,kivymd==1.1.1,requests,plyer,pillow

# Orientation
orientation = portrait

# Android settings
android.permissions = CAMERA, INTERNET, WRITE_EXTERNAL_STORAGE, READ_EXTERNAL_STORAGE, ACCESS_NETWORK_STATE, REQUEST_INSTALL_PACKAGES
android.minapi = 24
android.targetapi = 33
android.ndk = 25c
android.sdk = 33

# Architecture (modern phones use arm64-v8a)
android.archs = arm64-v8a, armeabi-v7a

# Use the latest NDK build tools
android.accept_sdk_license = True

# Enable camera feature in manifest
android.features = android.hardware.camera, android.hardware.camera.autofocus

# Build hook: adds FileProvider block to AndroidManifest + copies provider_paths.xml
p4a.hook = p4a_hook.py

# Include res/xml folder so provider_paths.xml is packaged in the APK
android.add_resources = res/xml/provider_paths.xml:xml/provider_paths.xml

[buildozer]
log_level = 2
warn_on_root = 1
