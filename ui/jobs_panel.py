"""Panel de trabajos del servidor — muestra progreso de cada scan y permite regenerar."""

import json
import pathlib
import threading
from datetime import datetime
from typing import Optional

from PySide6.QtCore    import Qt, Signal, Slot, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QProgressBar, QSizePolicy, QFileDialog,
)
from PySide6.QtGui import QColor, QPalette

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


class _JobCard(QFrame):
    regenerate_clicked = Signal(str)   # job_id
    export_clicked     = Signal(str)   # job_id

    def __init__(self, job: dict, parent=None):
        super().__init__(parent)
        self.job_id = job["job_id"]
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("QFrame { border: 1px solid #444; border-radius:4px; margin:2px; }")

        lay = QVBoxLayout(self)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 4, 6, 4)

        # Cabecera: ID + estado
        row1 = QHBoxLayout()
        self._lbl_id = QLabel(f"<b>#{job['job_id']}</b>")
        self._lbl_id.setFixedWidth(70)
        row1.addWidget(self._lbl_id)

        self._lbl_status = QLabel()
        self._lbl_status.setAlignment(Qt.AlignRight)
        row1.addWidget(self._lbl_status, 1)
        lay.addLayout(row1)

        # Mensaje
        self._lbl_msg = QLabel()
        self._lbl_msg.setWordWrap(True)
        self._lbl_msg.setStyleSheet("color:#aaa; font-size:11px;")
        lay.addWidget(self._lbl_msg)

        # Barra de progreso
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setFixedHeight(6)
        self._progress.setTextVisible(False)
        lay.addWidget(self._progress)

        # Info: frames + fecha
        row2 = QHBoxLayout()
        self._lbl_frames = QLabel()
        self._lbl_frames.setStyleSheet("color:#888; font-size:10px;")
        row2.addWidget(self._lbl_frames)

        self._btn_regen = QPushButton("↺ Regenerar")
        self._btn_regen.setFixedHeight(22)
        self._btn_regen.setToolTip("Volver a generar la malla desde las fotos guardadas")
        self._btn_regen.clicked.connect(lambda: self.regenerate_clicked.emit(self.job_id))
        row2.addWidget(self._btn_regen)

        self._btn_export = QPushButton("💾")
        self._btn_export.setFixedSize(28, 22)
        self._btn_export.setToolTip("Exportar como .refscan (ZIP con fotos + malla)")
        self._btn_export.clicked.connect(lambda: self.export_clicked.emit(self.job_id))
        row2.addWidget(self._btn_export)
        lay.addLayout(row2)

        self.update_job(job)

    def update_job(self, job: dict):
        status = job.get("status", "?")
        progress = job.get("progress", 0)
        message  = job.get("message", "")
        frames   = len(job.get("received_frames", []))
        error    = job.get("error", "")

        # Color del estado
        colors = {
            "done":         "#4CAF50",
            "preview_done": "#8BC34A",
            "running":      "#2196F3",
            "uploading":    "#FF9800",
            "waiting":      "#9E9E9E",
            "error":        "#F44336",
        }
        color = colors.get(status, "#9E9E9E")
        labels = {
            "done":         "✓ Listo",
            "preview_done": "☁ Nube lista",
            "running":      "⚙ Procesando",
            "uploading":    "↑ Subiendo",
            "waiting":      "— Espera",
            "error":        "✗ Error",
        }
        self._lbl_status.setText(f'<span style="color:{color}">{labels.get(status, status)}</span>')
        self._lbl_msg.setText(error if status == "error" else message)
        self._progress.setValue(progress)

        # Color de la barra
        bar_color = color
        self._progress.setStyleSheet(
            f"QProgressBar::chunk {{ background:{bar_color}; border-radius:3px; }}"
        )

        self._lbl_frames.setText(f"{frames} fotos")

        # El botón regenerar solo tiene sentido si hay fotos guardadas
        can_regen = status in ("done", "preview_done", "error") and frames > 0
        self._btn_regen.setEnabled(can_regen)


class JobsPanel(QWidget):
    """Panel lateral que lista los trabajos del servidor Flask y permite regenerar scans."""

    mesh_ready = Signal(str, str)   # path, name — para importar automáticamente

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server_ip: Optional[str] = None
        self._cards: dict[str, _JobCard] = {}
        self._browse_root: str = str(pathlib.Path.home() / ".refractory_capture" / "jobs")

        lay = QVBoxLayout(self)
        lay.setSpacing(4)
        lay.setContentsMargins(4, 4, 4, 4)

        # Cabecera
        hdr = QHBoxLayout()
        lbl = QLabel("Trabajos del servidor")
        lbl.setStyleSheet("font-weight:bold; font-size:12px;")
        hdr.addWidget(lbl)

        self._btn_refresh = QPushButton("↻")
        self._btn_refresh.setFixedSize(24, 24)
        self._btn_refresh.setToolTip("Actualizar lista")
        self._btn_refresh.clicked.connect(self._fetch_jobs)
        hdr.addWidget(self._btn_refresh)
        lay.addLayout(hdr)

        btns = QHBoxLayout()
        btn_folder = QPushButton("📁 Carpeta de fotos")
        btn_folder.setToolTip("Importar carpeta con fotos crudas del celular")
        btn_folder.clicked.connect(self._browse_raw_folder)
        btns.addWidget(btn_folder)

        btn_refscan = QPushButton("📄 Abrir .refscan")
        btn_refscan.setToolTip("Importar archivo .refscan exportado anteriormente")
        btn_refscan.clicked.connect(self._browse_refscan)
        btns.addWidget(btn_refscan)
        lay.addLayout(btns)

        self._lbl_server = QLabel("Sin servidor")
        self._lbl_server.setStyleSheet("color:#888; font-size:10px;")
        lay.addWidget(self._lbl_server)

        # Área scrollable de cards
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._cards_widget = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_widget)
        self._cards_layout.setSpacing(4)
        self._cards_layout.addStretch()
        scroll.setWidget(self._cards_widget)
        lay.addWidget(scroll, 1)

        self._lbl_status = QLabel("")
        self._lbl_status.setStyleSheet("color:#888; font-size:10px;")
        self._lbl_status.setWordWrap(True)
        lay.addWidget(self._lbl_status)

        # Timer de polling cada 3s cuando hay jobs activos
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._fetch_jobs)
        self._timer.start(3000)

    def set_server_ip(self, ip: str):
        self._server_ip = ip
        self._lbl_server.setText(f"📡 {ip}:5005")
        self._fetch_jobs()

    def _effective_ip(self) -> str:
        """IP del servidor — usa localhost si no se configuró una IP externa."""
        return self._server_ip or "127.0.0.1"

    def _fetch_jobs(self):
        threading.Thread(target=self._do_fetch, daemon=True).start()

    def _do_fetch(self):
        try:
            # Acceso directo al módulo del servidor (mismo proceso, más rápido)
            import sys
            srv = sys.modules.get("server")
            if srv is not None:
                with srv._lock:
                    jobs = [j.to_dict() for j in srv._jobs.values()]
            elif _HAS_REQUESTS:
                r = _requests.get(f"http://{self._effective_ip()}:5005/jobs", timeout=3)
                jobs = r.json()
            else:
                return

            from PySide6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(self, "_update_jobs",
                                     Qt.QueuedConnection,
                                     Q_ARG(str, json.dumps(jobs)))
        except Exception:
            pass

    @Slot(str)
    def _update_jobs(self, jobs_json: str):
        jobs = json.loads(jobs_json)
        if not jobs:
            self._lbl_status.setText("No hay trabajos en el servidor")
            return
        self._lbl_status.setText("")

        # Ordenar por created_at descendente (más nuevos primero)
        jobs.sort(key=lambda j: j.get("created_at", 0), reverse=True)

        seen = set()
        for job in jobs:
            jid = job["job_id"]
            seen.add(jid)
            if jid in self._cards:
                self._cards[jid].update_job(job)
            else:
                card = _JobCard(job)
                card.regenerate_clicked.connect(self._on_regenerate)
                card.export_clicked.connect(self._on_export)
                self._cards[jid] = card
                # Insertar antes del stretch
                self._cards_layout.insertWidget(
                    self._cards_layout.count() - 1, card)

        # Eliminar cards de jobs que ya no existen
        for jid in list(self._cards.keys()):
            if jid not in seen:
                card = self._cards.pop(jid)
                self._cards_layout.removeWidget(card)
                card.deleteLater()

    def _on_regenerate(self, job_id: str):
        if not _HAS_REQUESTS:
            return
        ip = self._effective_ip()
        self._lbl_status.setText(f"Regenerando {job_id}…")
        threading.Thread(
            target=self._do_regenerate, args=(ip, job_id), daemon=True
        ).start()

    def _do_regenerate(self, ip: str, job_id: str):
        try:
            # Primero verificar que el job siga en preview_done o done
            r = _requests.get(f"http://{ip}:5005/status/{job_id}", timeout=5)
            status = r.json().get("status", "")

            if status in ("preview_done",):
                # Usar continue_reconstruct (ya tiene el SfM hecho)
                _requests.post(f"http://{ip}:5005/continue_reconstruct/{job_id}",
                               json={}, timeout=5)
            else:
                # Re-correr desde cero (full pipeline)
                _requests.post(f"http://{ip}:5005/start_reconstruct/{job_id}",
                               json={"mode": "full"}, timeout=5)
        except Exception as e:
            from PySide6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(self, "_set_status",
                                     Qt.QueuedConnection,
                                     Q_ARG(str, f"Error: {e}"))

    def _browse_raw_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "Seleccionar carpeta con fotos o trabajos", self._browse_root,
        )
        if not path:
            return
        self._lbl_status.setText(f"Importando {pathlib.Path(path).name}…")
        # Intentar carga directa en el mismo proceso (más confiable que HTTP)
        threading.Thread(target=self._do_import_direct,
                         args=(path,), daemon=True).start()

    def _browse_refscan(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Abrir archivo .refscan",
            self._browse_root,
            "Escaneos (*.refscan);;Todos los archivos (*)",
        )
        if not path:
            return
        self._lbl_status.setText(f"Importando {pathlib.Path(path).name}…")
        threading.Thread(target=self._do_import_refscan,
                         args=(self._effective_ip(), path), daemon=True).start()

    def _do_import_direct(self, folder: str):
        """Carga trabajos directamente desde el módulo servidor (mismo proceso)."""
        try:
            import sys, pathlib as pl
            # Importar módulo del servidor embebido
            srv = sys.modules.get("server")
            if srv is None:
                # Fallback a HTTP si el módulo no está cargado
                self._do_import(self._effective_ip(), folder)
                return

            root = pl.Path(folder)
            loaded = []

            # Detectar si la carpeta ES un job (tiene images/*.jpg)
            img_dir = root / "images" if (root / "images").exists() else root
            direct = list(img_dir.glob("*.jpg"))

            if direct:
                # La carpeta seleccionada es un job directamente
                dirs_to_load = [root]
            else:
                # La carpeta contiene subcarpetas de jobs
                dirs_to_load = [d for d in sorted(root.iterdir()) if d.is_dir()]

            for job_dir in dirs_to_load:
                job = srv._load_job_from_dir(job_dir)
                if job:
                    with srv._lock:
                        srv._jobs[job.job_id] = job
                    loaded.append(job.job_id)

            if loaded:
                msg = f"✓ {len(loaded)} trabajos cargados"
            else:
                msg = "No se encontraron fotos en la carpeta seleccionada"

            from PySide6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(self, "_set_status",
                                     Qt.QueuedConnection, Q_ARG(str, msg))
            self._fetch_jobs()
        except Exception as e:
            from PySide6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(self, "_set_status",
                                     Qt.QueuedConnection, Q_ARG(str, f"Error: {e}"))

    def _do_import(self, ip: str, folder: str):
        try:
            r = _requests.post(
                f"http://{ip}:5005/import_folder",
                json={"folder": folder}, timeout=30,
            )
            data = r.json()
            msg = f"Error: {data['error']}" if "error" in data \
                  else f"Importado — {data['frames']} fotos (job {data['job_id']})"
            from PySide6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(self, "_set_status", Qt.QueuedConnection, Q_ARG(str, msg))
            self._fetch_jobs()
        except Exception as e:
            from PySide6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(self, "_set_status", Qt.QueuedConnection, Q_ARG(str, f"Error: {e}"))

    def _do_import_refscan(self, ip: str, path: str):
        try:
            r = _requests.post(
                f"http://{ip}:5005/import_refscan",
                json={"path": path}, timeout=60,
            )
            data = r.json()
            msg = f"Error: {data['error']}" if "error" in data \
                  else f"✓ .refscan importado — {data['frames']} fotos (job {data['job_id']})"
            from PySide6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(self, "_set_status", Qt.QueuedConnection, Q_ARG(str, msg))
            self._fetch_jobs()
        except Exception as e:
            from PySide6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(self, "_set_status", Qt.QueuedConnection, Q_ARG(str, f"Error: {e}"))

    def _on_export(self, job_id: str):
        if not _HAS_REQUESTS:
            return
        dest, _ = QFileDialog.getSaveFileName(
            self, "Guardar escaneo como…",
            str(pathlib.Path(self._browse_root) / f"scan_{job_id}.refscan"),
            "Escaneos (*.refscan)",
        )
        if not dest:
            return
        self._lbl_status.setText(f"Exportando {job_id}…")
        threading.Thread(target=self._do_export,
                         args=(self._effective_ip(), job_id, dest), daemon=True).start()

    def _do_export(self, ip: str, job_id: str, dest: str):
        try:
            r = _requests.get(f"http://{ip}:5005/export/{job_id}", timeout=120, stream=True)
            if r.status_code == 200:
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
                size_mb = pathlib.Path(dest).stat().st_size / 1_048_576
                msg = f"✓ Guardado: {pathlib.Path(dest).name} ({size_mb:.1f} MB)"
            else:
                msg = f"Error {r.status_code}: {r.text[:100]}"
            from PySide6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(self, "_set_status", Qt.QueuedConnection, Q_ARG(str, msg))
        except Exception as e:
            from PySide6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(self, "_set_status", Qt.QueuedConnection, Q_ARG(str, f"Error: {e}"))

    @Slot(str)
    def _set_status(self, msg: str):
        self._lbl_status.setText(msg)
