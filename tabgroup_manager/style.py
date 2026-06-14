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


def url_title(url: str, title: str = "") -> str:
    """Choose a readable label for a tab.

    Prefer the stored title; when it's empty/blank fall back to the URL's
    registered domain (``mail.google.com`` rather than the full URL with
    query strings), so users never see a long raw URL in the tree.
    """
    if title and title.strip():
        return title.strip()
    try:
        from urllib.parse import urlparse

        host = urlparse(url).netloc
        if host:
            # Drop leading "www." for a cleaner look.
            return host[4:] if host.startswith("www.") else host
    except Exception:
        pass
    return url


class FaviconLoader:
    """Async favicon fetcher with an in-memory cache.

    Favicons are pulled from Google's S2 favicon service
    (``https://www.google.com/s2/favicons?domain=<host>``), which returns a
    normalized 16-32px PNG for any domain. Requests are fired asynchronously
    via QNetworkAccessManager so the UI never blocks; results are cached by
    host so a window full of google.com tabs triggers exactly one fetch.

    Usage: ``loader.get(host, callback)`` where callback receives a QIcon (or
    an empty icon on failure). The callback fires on the Qt main thread.
    """

    # gstatic's faviconV2 endpoint is what Google's s2 service redirects to
    # under the hood — hitting it directly avoids a 301 hop (Qt's
    # QNetworkAccessManager does follow redirects after we enable the attribute
    # below, but cutting out the redirect is faster and more reliable). It
    # returns a normalized PNG (32x32 by default) for any URL.
    _GSTATIC = (
        "https://t3.gstatic.com/faviconV2"
        "?client=SOCIAL&type=FAVICON"
        "&fallback_opts=TYPE,SIZE,URL&url=http://{host}&size=32"
    )

    def __init__(self):
        from PyQt6.QtNetwork import QNetworkAccessManager

        self._nam = QNetworkAccessManager()
        # Follow redirects (301/302) — some favicon URLs redirect to a CDN,
        # and gstatic itself occasionally rotates hosts.
        from PyQt6.QtNetwork import QNetworkRequest
        self._nam.setRedirectPolicy(
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy
        )
        self._nam.finished.connect(self._on_finished)
        # host -> QPixmap (success), or None (already failed, don't retry)
        self._cache: dict = {}
        # host -> list of pending callbacks
        self._pending: dict = {}

    def get(self, host: str, callback):
        """Fetch the favicon for ``host``. Calls ``callback(QIcon)`` when ready.

        If the icon is already cached, the callback still fires (deferred to
        the next event-loop tick) so callers can treat all paths uniformly.
        """
        from PyQt6.QtCore import QTimer
        from PyQt6.QtNetwork import QNetworkRequest
        from PyQt6.QtCore import QUrl

        if not host:
            callback(_empty_icon())
            return

        # Cache hit — deliver on the next tick to keep the call async.
        if host in self._cache:
            pix = self._cache[host]
            QTimer.singleShot(0, lambda: callback(_pix_to_icon(pix)))
            return

        # Already in flight — queue the callback alongside the others.
        if host in self._pending:
            self._pending[host].append(callback)
            return

        # New fetch.
        self._pending[host] = [callback]
        url = self._GSTATIC.format(host=host)
        req = QNetworkRequest(QUrl(url))
        req.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader,
                       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)")
        self._nam.get(req)

    def _on_finished(self, reply):
        from PyQt6.QtGui import QPixmap
        from PyQt6.QtNetwork import QNetworkRequest

        host = self._host_from_url(reply.url().toString())
        callbacks = self._pending.pop(host, [])
        if not callbacks:
            reply.deleteLater()
            return

        pix = QPixmap()
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        http_ok = status is not None and 200 <= int(status) <= 299
        if http_ok and reply.error() == reply.NetworkError.NoError:
            pix.loadFromData(reply.readAll())
        # Cache even failures (as None) so we don't hammer the service.
        self._cache[host] = pix if not pix.isNull() else None
        icon = _pix_to_icon(self._cache[host])
        for cb in callbacks:
            cb(icon)
        reply.deleteLater()

    @staticmethod
    def _host_from_url(fetch_url: str) -> str:
        # Reverse of _GSTATIC.format(host=...): pull the url= query param and
        # strip the "http://" prefix we added.
        from urllib.parse import urlparse, parse_qs

        qs = parse_qs(urlparse(fetch_url).query)
        inner = qs.get("url", [""])[0]
        return inner.replace("http://", "") if inner else ""


def _pix_to_icon(pix) -> "QIcon":
    from PyQt6.QtGui import QIcon

    if pix is None or pix.isNull():
        return QIcon()
    # Scale to a small icon size for crisp display in the tree.
    scaled = pix.scaled(
        16, 16, Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    return QIcon(scaled)


def _empty_icon() -> "QIcon":
    from PyQt6.QtGui import QIcon

    return QIcon()


def app_icon(size: int = 256) -> "QIcon":
    """The application icon: stacked colored tab cards on a rounded tile.

    Drawn programmatically (no image asset needed) so the project stays a
    single Python package. The three cards echo Chrome's tab-group color
    palette, hinting at the app's purpose (managing tab groups). Works at any
    size; callers typically pass 256 for crisp Dock/title-bar rendering.
    """
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QIcon

    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Background rounded tile (soft neutral so the colored cards pop).
    tile = QPainterPath()
    margin = size * 0.04
    tile.addRoundedRect(
        QRectF(margin, margin, size - 2 * margin, size - 2 * margin),
        size * 0.2, size * 0.2,
    )
    p.fillPath(tile, QColor(248, 249, 252))

    # Three stacked tab cards, each rotated slightly for a fanned look.
    # Colors mirror Chrome's tab group palette (blue/red/green).
    cards = [QColor(66, 133, 244), QColor(234, 67, 53), QColor(52, 168, 83)]
    card_w = size * 0.42
    card_h = size * 0.54
    cx, cy = size * 0.5, size * 0.54
    for i, color in enumerate(cards):
        p.setBrush(color)
        p.setPen(Qt.PenStyle.NoPen)
        p.save()
        p.translate(cx, cy)
        p.rotate(-18 + i * 18)  # fan: -18°, 0°, +18°
        p.drawRoundedRect(
            QRectF(-card_w / 2, -card_h / 2, card_w, card_h),
            size * 0.06, size * 0.06,
        )
        p.restore()

    p.end()
    return QIcon(pix)
