import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtGui     import QSurfaceFormat, QPalette, QColor


def _dark_palette(app: QApplication):
    p = QPalette()
    c = {
        QPalette.ColorRole.Window:          QColor(42,  42,  46),
        QPalette.ColorRole.WindowText:      QColor(220, 220, 220),
        QPalette.ColorRole.Base:            QColor(30,  30,  34),
        QPalette.ColorRole.AlternateBase:   QColor(50,  50,  54),
        QPalette.ColorRole.ToolTipBase:     QColor(255, 255, 220),
        QPalette.ColorRole.ToolTipText:     QColor(0,   0,   0),
        QPalette.ColorRole.Text:            QColor(220, 220, 220),
        QPalette.ColorRole.Button:          QColor(55,  55,  60),
        QPalette.ColorRole.ButtonText:      QColor(220, 220, 220),
        QPalette.ColorRole.BrightText:      QColor(255, 0,   0),
        QPalette.ColorRole.Link:            QColor(88,  148, 255),
        QPalette.ColorRole.Highlight:       QColor(60,  120, 200),
        QPalette.ColorRole.HighlightedText: QColor(255, 255, 255),
    }
    for role, color in c.items():
        p.setColor(role, color)
    app.setPalette(p)


def main():
    # Must be set before QApplication
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setSamples(4)
    fmt.setDepthBufferSize(24)
    fmt.setStencilBufferSize(8)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication(sys.argv)
    app.setApplicationName("Refractory Analyzer")
    app.setStyle("Fusion")
    _dark_palette(app)

    from app.main_window import MainWindow
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
