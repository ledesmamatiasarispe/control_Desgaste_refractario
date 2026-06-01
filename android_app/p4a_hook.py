"""
python-for-android build hook.
Agrega el bloque <provider> (FileProvider) al AndroidManifest.xml
y copia provider_paths.xml al directorio de recursos del proyecto.

Referenciado desde buildozer.spec:
    p4a.hook = p4a_hook.py
"""

import pathlib
import shutil


def after_compile_android_project(arch, project_dir):
    project = pathlib.Path(project_dir)
    _patch_manifest(project)
    _copy_provider_paths(project)


def _patch_manifest(project: pathlib.Path):
    manifest_path = project / "AndroidManifest.xml"
    if not manifest_path.exists():
        return

    manifest = manifest_path.read_text(encoding="utf-8")

    provider_block = """
    <provider
        android:name="androidx.core.content.FileProvider"
        android:authorities="${applicationId}.fileprovider"
        android:exported="false"
        android:grantUriPermissions="true">
        <meta-data
            android:name="android.support.FILE_PROVIDER_PATHS"
            android:resource="@xml/provider_paths"/>
    </provider>"""

    if "FileProvider" not in manifest:
        manifest = manifest.replace(
            "</application>",
            provider_block + "\n    </application>"
        )
        manifest_path.write_text(manifest, encoding="utf-8")
        print("[hook] FileProvider añadido al AndroidManifest.xml")
    else:
        print("[hook] FileProvider ya presente en AndroidManifest.xml")


def _copy_provider_paths(project: pathlib.Path):
    src = pathlib.Path(__file__).parent / "res" / "xml" / "provider_paths.xml"
    if not src.exists():
        print("[hook] WARNING: provider_paths.xml no encontrado")
        return

    xml_dir = project / "res" / "xml"
    xml_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, xml_dir / "provider_paths.xml")
    print(f"[hook] provider_paths.xml copiado a {xml_dir}")
