"""Styling utilities for the PyQt6 GUI."""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap


APP_STYLESHEET = """
QMainWindow {
    background: #f5f6fa;
}

QToolBar {
    background: #ffffff;
    border-bottom: 1px solid #dfe4ea;
    padding: 8px;
    spacing: 6px;
}

QToolBar QToolButton {
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 8px 14px;
    color: #2f3542;
    font-size: 13px;
    font-weight: 500;
}

QToolBar QToolButton:hover {
    background: #eef2f7;
}

QToolBar QToolButton:pressed {
    background: #d8dee9;
}

QStatusBar {
    background: #ffffff;
    color: #747d8c;
    border-top: 1px solid #dfe4ea;
}

QStatusBar::item {
    border: none;
}

QTreeWidget {
    background: #ffffff;
    border: 1px solid #dfe4ea;
    border-radius: 10px;
    outline: 0;
    font-size: 14px;
    alternate-background-color: #fbfbfb;
}

QTreeWidget::item {
    padding: 5px;
    border-bottom: 1px solid #f1f2f6;
}

QTreeWidget::item:selected {
    background: #e1ecff;
    color: #2f3542;
    border-radius: 6px;
}

QTreeWidget::item:hover {
    background: #f5f7fa;
}

QTreeWidget::branch {
    background: transparent;
}

QTreeWidget::branch:has-children:closed {
    image: none;
}

QTreeWidget::branch:has-children:open {
    image: none;
}

QHeaderView::section {
    background: #f1f2f6;
    color: #57606f;
    padding: 8px;
    border: none;
    font-weight: 600;
}

QSplitter::handle {
    background: #dfe4ea;
}

QSplitter::handle:horizontal {
    width: 2px;
    margin: 0 4px;
    border-radius: 1px;
}

QLabel {
    color: #2f3542;
    font-size: 14px;
    background: transparent;
}
"""


COLOR_QCOLORS = {
    "grey": QColor(155, 155, 155),
    "blue": QColor(66, 133, 244),
    "red": QColor(234, 67, 53),
    "yellow": QColor(251, 188, 5),
    "green": QColor(52, 168, 83),
    "pink": QColor(255, 105, 180),
    "purple": QColor(171, 71, 188),
    "cyan": QColor(24, 188, 212),
    "orange": QColor(255, 153, 0),
}

COLOR_LIGHT_BG = {
    "grey": QColor(241, 242, 246),
    "blue": QColor(227, 242, 253),
    "red": QColor(255, 235, 238),
    "yellow": QColor(255, 253, 231),
    "green": QColor(232, 245, 233),
    "pink": QColor(252, 228, 236),
    "purple": QColor(243, 229, 245),
    "cyan": QColor(224, 247, 250),
    "orange": QColor(255, 243, 224),
}

DARK_TEXT = QColor(47, 53, 66)


def group_icon(color_name: str, size: int = 16) -> QPixmap:
    """Return a small rounded colored square icon for a tab group."""
    from PyQt6.QtGui import QIcon

    color = COLOR_QCOLORS.get(color_name, COLOR_QCOLORS["grey"])
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, size * 0.3, size * 0.3)
    painter.fillPath(path, color)
    painter.end()
    return QIcon(pixmap)
