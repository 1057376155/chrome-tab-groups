"""PyQt6 main window for the tab group manager."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import Qt, QThreadPool, QRunnable, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import snss_parser
from .bridge import Bridge
from .chrome_opener import open_url, open_urls
from .config import BRIDGE_URL, DB_PATH
from .db import Database
from .style import (
    APP_STYLESHEET,
    app_icon,
    COLOR_LIGHT_BG,
    COLOR_QCOLORS,
    DARK_TEXT,
    FaviconLoader,
    group_icon,
    url_title,
)


class WorkerSignals(QObject):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)


class ScanWorker(QRunnable):
    """Run SNSS scanning in a background thread."""

    def __init__(self, profile_dir: Optional[str] = None):
        super().__init__()
        self.profile_dir = profile_dir
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            profiles = snss_parser.get_profiles()
            if self.profile_dir:
                selected = {self.profile_dir: profiles.get(self.profile_dir, {"name": self.profile_dir, "email": ""})}
            else:
                selected = profiles
            results: List[Dict[str, Any]] = []
            for pdir, pinfo in selected.items():
                raw = snss_parser.scan_profile(pdir, pinfo)
                groups = snss_parser.deduplicate_groups(raw)
                results.append(
                    {
                        "profile_dir": pdir,
                        "profile_name": pinfo.get("name", pdir),
                        "email": pinfo.get("email", ""),
                        "groups": groups,
                    }
                )
            self.signals.finished.emit(results)
        except Exception as exc:  # pragma: no cover
            self.signals.error.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Chrome Tab Group Manager")
        self.resize(1200, 700)

        self.db = Database()
        self.thread_pool = QThreadPool()

        self._build_ui()

        self.favicons = FaviconLoader()
        self.bridge = Bridge(self.db)
        self.bridge.signals.snapshot_received.connect(self.on_snapshot_received)
        self.bridge.signals.restore_ack.connect(self.on_restore_ack)
        self.bridge.signals.log_message.connect(self.log_status)
        if not self.bridge.start():
            QMessageBox.warning(
                self,
                "Bridge 启动失败",
                f"无法绑定本地端口 {self.bridge.port}（可能已有实例在运行）。\n"
                "实时捕获/恢复功能将不可用，但仍可从文件扫描。",
            )

        self.refresh_tree()
        self.update_connection_status(False)

    def _build_ui(self) -> None:
        # Central widget and splitter
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        # Left: search box + tabbed tree (Current / History)
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        # Search box at the top of the left pane. Filters both trees as the
        # user types — non-matching nodes are hidden, ancestors of matches are
        # expanded, and matches are highlighted.
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索标题或 URL…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search_changed)
        left_layout.addWidget(self.search_edit)

        # Two trees wrapped in a QTabWidget so scanned/captured snapshots
        # (Current) and manually-saved windows (History) are kept separate.
        # Both trees share the same callbacks; get_selected_node() reads the
        # currently-active tab so all toolbar actions work on whichever view
        # the user is looking at.
        self.tabs = QTabWidget()
        self.tree_current = self._make_tree()
        self.tree_saved = self._make_tree()
        self.tabs.addTab(self.tree_current, "当前")
        self.tabs.addTab(self.tree_saved, "历史")
        # Switching tabs clears the selection in the tree we're leaving, which
        # fires itemSelectionChanged — guard against it clearing the detail
        # panel by re-reading the new tab's selection.
        self.tabs.currentChanged.connect(self._on_tab_changed)
        left_layout.addWidget(self.tabs)

        # Right: details
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.detail_label = QLabel("选择一个标签组或标签页查看详情")
        self.detail_label.setWordWrap(True)
        right_layout.addWidget(self.detail_label)

        self.detail_list = QTreeWidget()
        self.detail_list.setHeaderLabels(["标题", "URL"])
        self.detail_list.setColumnCount(2)
        self.detail_list.setAlternatingRowColors(True)
        self.detail_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.detail_list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # Single-click opens: the detail rows are leaf actions (open URL /
        # restore group), so clicking once should act immediately rather than
        # requiring a double-click.
        self.detail_list.itemClicked.connect(self.on_detail_click)
        right_layout.addWidget(self.detail_list)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setSizes([450, 750])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        # Toolbar
        toolbar = QToolBar("Main")
        self.addToolBar(toolbar)

        self._add_action(toolbar, "从文件扫描", self.scan_from_files, "Ctrl+R")
        self._add_action(toolbar, "从 Chrome 捕获", self.capture_from_chrome, "Ctrl+L")
        self._add_action(toolbar, "刷新列表", self.refresh_tree, "F5")
        toolbar.addSeparator()
        self._add_action(toolbar, "保存窗口", self.save_selected_window, "Ctrl+S")
        self._add_action(toolbar, "打开标签页", self.open_selected_tab, "Return")
        self._add_action(toolbar, "打开整组", self.open_selected_group, "Ctrl+O")
        self._add_action(toolbar, "恢复为 Chrome 标签组", self.restore_selected_group, "Ctrl+Shift+R")
        self._add_action(toolbar, "恢复窗口", self.restore_selected_window, "Ctrl+Shift+W")
        toolbar.addSeparator()
        self._add_action(toolbar, "删除快照", self.delete_selected_snapshot, "Delete")

        # Status bar
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage(f"DB: {DB_PATH}")

        # Keep the connection status updated
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(
            lambda: self.update_connection_status(self.bridge.is_extension_connected)
        )
        self._status_timer.start(1000)

    def _add_action(
        self,
        toolbar: QToolBar,
        text: str,
        slot,
        shortcut: Optional[str] = None,
    ) -> QAction:
        action = QAction(text, self)
        action.triggered.connect(slot)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        toolbar.addAction(action)
        return action

    def closeEvent(self, event) -> None:
        self.bridge.stop()
        event.accept()

    def log_status(self, message: str) -> None:
        self.status.showMessage(message, 5000)

    def update_connection_status(self, connected: bool) -> None:
        text = "扩展已连接" if connected else "扩展未连接"
        self.status.showMessage(f"Bridge: {BRIDGE_URL} | {text} | DB: {DB_PATH}")

    def on_snapshot_received(self, snapshot_id: int) -> None:
        self.log_status(f"收到并保存快照 #{snapshot_id}")
        # Live captures land in the Current tab; don't touch the History tab.
        self._refresh_current()

    def on_restore_ack(self, command_id: str, success: bool, message: str) -> None:
        msg = f"恢复成功: {message}" if success else f"恢复失败: {message}"
        self.log_status(msg)
        if not success:
            QMessageBox.warning(self, "恢复失败", message)

    # ------------------------------------------------------------------
    # Tree & data helpers
    # ------------------------------------------------------------------
    def _make_tree(self) -> QTreeWidget:
        """Build a configured tree widget shared by the Current/History tabs.

        Both trees get the same column setup, signals, and alternating-row
        styling; only their data source differs (see _refresh_current /
        _refresh_saved).
        """
        tree = QTreeWidget()
        tree.setHeaderLabels(["名称", "颜色 / 数量"])
        tree.setColumnCount(2)
        tree.setAlternatingRowColors(True)
        # Hide the secondary column — the left trees show titles only.
        tree.setHeaderHidden(True)
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        tree.header().setStretchLastSection(False)
        tree.setColumnWidth(1, 0)
        tree.itemDoubleClicked.connect(self.on_tree_double_click)
        tree.itemSelectionChanged.connect(self.on_selection_changed)
        return tree

    @property
    def tree(self) -> QTreeWidget:
        """The currently-active tree (Current or History tab).

        Kept as a property so the many places that historically referenced
        self.tree keep working transparently — they always operate on
        whichever tab is visible.
        """
        return self.tabs.currentWidget()

    def _on_tab_changed(self, _index: int) -> None:
        """When the user switches tabs, refresh the detail panel from the
        newly-active tree's current selection (or clear it if none)."""
        tree = self.tabs.currentWidget()
        items = tree.selectedItems() if tree is not None else []
        if items:
            data = items[0].data(0, Qt.ItemDataRole.UserRole) or {}
            self.update_detail_panel(data)
        else:
            self.detail_list.clear()
            self.detail_label.setText("选择一个标签组或标签页查看详情")

    # ------------------------------------------------------------------
    # Search / filter
    # ------------------------------------------------------------------
    def _on_search_changed(self, text: str) -> None:
        """Filter both trees by the search box content.

        Matching is case-insensitive against the node's title (column 0) and,
        for tab nodes, the URL stored in UserRole. When the query is empty,
        all nodes are shown again with highlighting cleared.
        """
        query = text.strip().lower()
        self._apply_filter(self.tree_current, query)
        self._apply_filter(self.tree_saved, query)

    def _apply_filter(self, tree: QTreeWidget, query: str) -> None:
        """Hide non-matching nodes in ``tree``; expand the ancestor chain of
        any match so it stays visible; highlight matches.

        Returns True if a subtree contains a match (so the caller can keep
        its parent visible). A node matches if its own title/url matches OR
        any of its descendants match — that way a profile/snapshot/window that
        contains a matching tab is shown with the path expanded down to it.
        """
        highlight = QColor(255, 248, 176)  # pale yellow
        no_highlight = QColor(0, 0, 0, 0)  # transparent (clears highlight)

        tree.blockSignals(True)
        try:
            if not query:
                # Reset: show everything, clear highlights, collapse back to
                # the natural expanded state (profile/snapshot/window expanded).
                self._reset_filter(tree, tree.invisibleRootItem())
                return

            self._filter_recursive(tree.invisibleRootItem(), query, highlight, no_highlight)
        finally:
            tree.blockSignals(False)

    def _filter_recursive(self, item, query: str, highlight, no_highlight) -> bool:
        """Recursively filter ``item``'s children. Returns True if any node
        in this subtree matches the query."""
        any_match = False
        for i in range(item.childCount()):
            child = item.child(i)
            child_match = self._filter_recursive(child, query, highlight, no_highlight)

            # A node matches on its own title/url, or if a descendant matches.
            data = child.data(0, Qt.ItemDataRole.UserRole) or {}
            title = (child.text(0) or "").lower()
            url = (data.get("url") or "").lower()
            self_match = query in title or query in url
            matched = self_match or child_match

            # Show/hide: hide only if neither this node nor any descendant
            # matches. Hidden nodes don't need highlight work.
            child.setHidden(not matched)
            if self_match:
                child.setBackground(0, highlight)
            else:
                child.setBackground(0, no_highlight)

            # If something in this subtree matched, expand ancestors so the
            # match is reachable.
            if matched:
                any_match = True
                item.setExpanded(True)
        return any_match

    def _reset_filter(self, tree, item) -> None:
        """Clear all filtering: un-hide everything, clear highlights."""
        for i in range(item.childCount()):
            child = item.child(i)
            child.setHidden(False)
            child.setBackground(0, QColor(0, 0, 0, 0))
            self._reset_filter(tree, child)

    def refresh_tree(self) -> None:
        """Refresh both the Current and History trees."""
        self._refresh_current()
        self._refresh_saved()
        # Re-apply the current search filter to the freshly rebuilt trees.
        self._on_search_changed(self.search_edit.text())

    def _refresh_current(self) -> None:
        """Populate the Current tab with scanned/captured snapshots.

        Shows non-saved profiles and their non-saved snapshots. Signals are
        blocked during the rebuild so itemSelectionChanged doesn't fire
        spuriously while items are being added/removed.
        """
        tree = self.tree_current
        tree.blockSignals(True)
        tree.clear()
        for profile in self.db.get_profiles(exclude_saved=True):
            self._populate_profile(tree, profile, saved_only=False)
        tree.blockSignals(False)

    def _refresh_saved(self) -> None:
        """Populate the History tab with manually-saved windows."""
        tree = self.tree_saved
        tree.blockSignals(True)
        tree.clear()
        for profile in self.db.get_saved_profiles():
            self._populate_profile(tree, profile, saved_only=True)
        tree.blockSignals(False)

    def _populate_profile(
        self,
        parent: QTreeWidget,
        profile,
        saved_only: bool,
    ) -> None:
        """Render one profile and its snapshots under ``parent``.

        ``saved_only=True`` pulls source='saved' snapshots (History tab);
        ``False`` pulls everything that isn't 'saved' (Current tab).
        """
        profile_item = QTreeWidgetItem(parent)
        profile_item.setText(0, profile.name)
        profile_item.setText(1, profile.profile_dir)
        profile_item.setData(
            0,
            Qt.ItemDataRole.UserRole,
            {"type": "profile", "id": profile.id},
        )
        profile_font = QFont(profile_item.font(0))
        profile_font.setBold(True)
        profile_font.setPointSize(profile_font.pointSize() + 1)
        profile_item.setFont(0, profile_font)
        profile_item.setExpanded(True)

        if saved_only:
            snaps = self.db.get_snapshots(profile.id, source="saved")
        else:
            snaps = [
                s for s in self.db.get_snapshots(profile.id)
                if s.source != "saved"
            ]

        for snap in snaps:
            snap_item = QTreeWidgetItem(profile_item)
            snap_item.setText(
                0,
                f"{snap.created_at.strftime('%Y-%m-%d %H:%M:%S')} [{snap.source}]",
            )
            snap_item.setText(1, "")
            snap_item.setData(
                0,
                Qt.ItemDataRole.UserRole,
                {"type": "snapshot", "id": snap.id},
            )
            snap_font = QFont(snap_item.font(0))
            snap_font.setItalic(True)
            snap_item.setFont(0, snap_font)
            snap_item.setForeground(0, QColor(116, 125, 140))
            snap_item.setExpanded(True)

            groups = self.db.get_groups(snap.id)
            windows = self.db.get_windows(snap.id)

            if windows:
                groups_by_window: Dict[Any, List] = {}
                for g in groups:
                    groups_by_window.setdefault(g.window_id, []).append(g)
                for w in windows:
                    win_item = QTreeWidgetItem(snap_item)
                    w_groups = groups_by_window.get(w.id, [])
                    w_tabs = sum(len(self.db.get_tabs(g.id)) for g in w_groups)
                    win_item.setText(0, f"{w.title}  ({len(w_groups)} 组, {w_tabs} 标签)")
                    win_item.setText(1, "")
                    win_item.setData(
                        0,
                        Qt.ItemDataRole.UserRole,
                        {"type": "window", "id": w.id, "snapshot_id": snap.id, "title": w.title},
                    )
                    win_font = QFont(win_item.font(0))
                    win_font.setBold(True)
                    win_item.setFont(0, win_font)
                    win_item.setForeground(0, QColor(60, 90, 160))
                    win_item.setExpanded(True)
                    for group in w_groups:
                        self._add_group_item(win_item, group, snap.id)
            else:
                for group in groups:
                    self._add_group_item(snap_item, group, snap.id)

    def _load_favicon(self, item: QTreeWidgetItem, url: str) -> None:
        """Attach the site favicon to a tree item asynchronously.

        Runs entirely on the Qt main thread: FaviconLoader uses
        QNetworkAccessManager whose ``finished`` signal is delivered on the
        main thread, so setIcon is always safe. If the tree was rebuilt (item
        invalidated) by the time the icon arrives, setIcon is a harmless
        no-op on a detached item.
        """
        from urllib.parse import urlparse

        try:
            host = urlparse(url).netloc
        except Exception:
            host = ""
        if not host:
            return
        # Capture the row's URL so we can verify the item still represents it
        # when the callback fires (tree rebuilds reuse widget memory).
        def _apply(icon):
            try:
                # TreeWidgetItemWeakRef is overkill; just check it's still
                # bound to this url. An item reused after a rebuild will have
                # a different url, so we skip to avoid wrong icons.
                data = item.data(0, Qt.ItemDataRole.UserRole) or {}
                if data.get("url") == url:
                    item.setIcon(0, icon)
            except RuntimeError:
                # Item was already garbage-collected (C++ side deleted).
                pass

        self.favicons.get(host, _apply)

    def _add_group_item(self, parent: QTreeWidgetItem, group, snapshot_id: int) -> None:
        """Append a group node (with its tabs) under ``parent``."""
        group_item = QTreeWidgetItem(parent)
        group_item.setText(0, group.title)
        color = group.color_name or "grey"
        tabs = self.db.get_tabs(group.id)
        group_item.setText(1, f"{len(tabs)} 个标签")
        group_item.setData(
            0,
            Qt.ItemDataRole.UserRole,
            {
                "type": "group",
                "id": group.id,
                "title": group.title,
                "color": color,
                "snapshot_id": snapshot_id,
            },
        )
        group_item.setIcon(0, group_icon(color))

        bold_font = QFont(group_item.font(0))
        bold_font.setBold(True)
        group_item.setFont(0, bold_font)

        bg = COLOR_LIGHT_BG.get(color, QColor(241, 242, 246))
        group_item.setBackground(0, bg)
        group_item.setForeground(0, DARK_TEXT)
        group_item.setBackground(1, bg)
        group_item.setForeground(1, COLOR_QCOLORS.get(color, DARK_TEXT))
        group_item.setExpanded(not group.collapsed)

        for tab in tabs:
            tab_item = QTreeWidgetItem(group_item)
            tab_item.setText(0, url_title(tab.url, tab.title))
            # Left tree: title only. The URL belongs in the right-side detail
            # panel, where there's room to read it; duplicating it in column 1
            # here just clutters the tree.
            tab_item.setText(1, "")
            tab_item.setData(
                0,
                Qt.ItemDataRole.UserRole,
                {
                    "type": "tab",
                    "id": tab.id,
                    "url": tab.url,
                },
            )
            # Fetch the favicon asynchronously and apply it once loaded.
            # Callback captures tab_item by reference; if the item was removed
            # (tree rebuilt) by the time the icon arrives, setIcon is a no-op.
            self._load_favicon(tab_item, tab.url)

    def on_selection_changed(self) -> None:
        # The signal can come from either the Current or History tree. Prefer
        # the actual sender so we read the right tree even if the active tab
        # differs (e.g. a programmatic selection change in the background tree).
        sender = self.sender()
        tree = sender if isinstance(sender, QTreeWidget) else self.tree
        # Ignore selection changes from a tree that isn't the active tab —
        # otherwise clearing a background tree during refresh would wipe the
        # detail panel the user is currently looking at.
        if tree is not self.tree:
            return
        items = tree.selectedItems()
        if not items:
            self.detail_list.clear()
            self.detail_label.setText("选择一个标签组或标签页查看详情")
            return
        item = items[0]
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        self.update_detail_panel(data)

    def update_detail_panel(self, data: Dict[str, Any]) -> None:
        self.detail_list.clear()
        node_type = data.get("type")

        if node_type == "tab":
            url = data.get("url", "")
            self.detail_label.setText(f"<b>标签页</b><br>{url}")
            row = QTreeWidgetItem(self.detail_list)
            row.setText(0, "打开")
            row.setText(1, url)
            row.setData(0, Qt.ItemDataRole.UserRole, {"type": "tab", "url": url})
        elif node_type == "group":
            group_id = data["id"]
            tabs = self.db.get_tabs(group_id)
            self.detail_label.setText(
                f"<b>{data.get('title', '(untitled)')}</b> — {len(tabs)} 个标签页"
            )
            for tab in tabs:
                row = QTreeWidgetItem(self.detail_list)
                row.setText(0, url_title(tab.url, tab.title))
                row.setText(1, tab.url)
                row.setData(0, Qt.ItemDataRole.UserRole, {"type": "tab", "url": tab.url})
                self._load_favicon(row, tab.url)
        elif node_type == "window":
            win_id = data["id"]
            groups = self.db.get_groups_by_window(win_id)
            total_tabs = sum(len(self.db.get_tabs(g.id)) for g in groups)
            self.detail_label.setText(
                f"<b>{data.get('title', '窗口')}</b><br>共 {len(groups)} 组，{total_tabs} 个标签页"
            )
            for group in groups:
                tabs = self.db.get_tabs(group.id)
                row = QTreeWidgetItem(self.detail_list)
                row.setText(0, f"{group.title} ({len(tabs)} tabs)")
                row.setText(1, "")
                row.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    {
                        "type": "group",
                        "id": group.id,
                        "title": group.title,
                        "color": group.color_name,
                    },
                )
        elif node_type == "snapshot":
            snap_id = data["id"]
            groups = self.db.get_groups(snap_id)
            total_tabs = sum(len(self.db.get_tabs(g.id)) for g in groups)
            self.detail_label.setText(
                f"<b>快照</b><br>共 {len(groups)} 组，{total_tabs} 个标签页"
            )
            for group in groups:
                tabs = self.db.get_tabs(group.id)
                row = QTreeWidgetItem(self.detail_list)
                row.setText(0, f"{group.title} ({len(tabs)} tabs)")
                row.setText(1, "")
                row.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    {
                        "type": "group",
                        "id": group.id,
                        "title": group.title,
                        "color": group.color_name,
                    },
                )
        elif node_type == "profile":
            profile_id = data.get("id")
            snaps = self.db.get_snapshots(profile_id)
            total_groups = sum(len(self.db.get_groups(s.id)) for s in snaps)
            total_tabs = sum(
                len(self.db.get_tabs(g.id))
                for s in snaps
                for g in self.db.get_groups(s.id)
            )
            self.detail_label.setText(
                f"<b>Profile</b><br>{len(snaps)} 个快照，共 {total_groups} 组，{total_tabs} 个标签页"
            )
            for snap in snaps:
                groups = self.db.get_groups(snap.id)
                snap_tabs = sum(len(self.db.get_tabs(g.id)) for g in groups)
                row = QTreeWidgetItem(self.detail_list)
                row.setText(0, f"{snap.created_at.strftime('%Y-%m-%d %H:%M')} [{snap.source}] ({snap_tabs} tabs)")
                row.setText(1, "")
        else:
            self.detail_label.setText("选择一个标签组或标签页查看详情")

    def on_tree_double_click(self, item: QTreeWidgetItem, column: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        node_type = data.get("type")
        if node_type == "tab":
            open_url(data.get("url", ""))
        elif node_type == "group":
            if self.bridge.is_extension_connected:
                self.restore_group_by_id(data["id"])
            else:
                self.open_group_by_id(data["id"])
        elif node_type == "window":
            if self.bridge.is_extension_connected:
                self.restore_window_by_id(data["id"])
            else:
                self.open_window_by_id(data["id"])

    def on_detail_click(self, item: QTreeWidgetItem, column: int) -> None:
        """Single-click handler for the right-side detail panel.

        Acts immediately on the clicked row: a tab row opens its URL in
        Chrome, a group row restores/opens the group.
        """
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if data.get("type") == "tab":
            open_url(data.get("url", ""))
        elif data.get("type") == "group":
            # Mirror the left-tree behavior: restore as a native Chrome group
            # when the extension is connected, otherwise just open the URLs.
            if self.bridge.is_extension_connected:
                self.restore_group_by_id(data["id"])
            else:
                self.open_group_by_id(data["id"])

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def scan_from_files(self) -> None:
        self.status.showMessage("正在扫描 Chrome session 文件…")
        worker = ScanWorker()
        worker.signals.finished.connect(self._on_scan_finished)
        worker.signals.error.connect(self._on_scan_error)
        self.thread_pool.start(worker)

    def _on_scan_finished(self, results: List[Dict[str, Any]]) -> None:
        total_groups = 0
        total_tabs = 0
        for r in results:
            groups = r["groups"]
            for g in groups:
                total_tabs += len(g.get("tabs", []))
            total_groups += len(groups)
            self.db.import_snapshot(
                r["profile_dir"], r["profile_name"], r["email"], groups, source="snss"
            )
        self._refresh_current()
        self.log_status(
            f"扫描完成: {len(results)} profiles, {total_groups} groups, {total_tabs} tabs"
        )

    def _on_scan_error(self, message: str) -> None:
        QMessageBox.critical(self, "扫描失败", message)
        self.log_status(f"扫描失败: {message}")

    def capture_from_chrome(self) -> None:
        if not self.bridge.is_extension_connected:
            QMessageBox.information(
                self,
                "扩展未连接",
                "请确保 Chrome 扩展已安装并启用，然后重试。",
            )
            return
        self.bridge.request_capture()

    def get_selected_node(self) -> Optional[Dict[str, Any]]:
        items = self.tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, Qt.ItemDataRole.UserRole) or {}

    def open_selected_tab(self) -> None:
        data = self.get_selected_node()
        if not data:
            return
        if data.get("type") == "tab":
            open_url(data.get("url", ""))
        elif data.get("type") == "group":
            self.open_group_by_id(data["id"])

    def open_selected_group(self) -> None:
        data = self.get_selected_node()
        if data and data.get("type") == "group":
            self.open_group_by_id(data["id"])

    def open_group_by_id(self, group_id: int) -> None:
        tabs = self.db.get_tabs(group_id)
        urls = [t.url for t in tabs if t.url.startswith("http")]
        if urls:
            open_urls(urls)
            self.log_status(f"已打开 {len(urls)} 个标签页")

    def restore_selected_group(self) -> None:
        data = self.get_selected_node()
        if data and data.get("type") == "group":
            self.restore_group_by_id(data["id"])

    def restore_group_by_id(self, group_id: int) -> None:
        if not self.bridge.is_extension_connected:
            QMessageBox.information(
                self,
                "扩展未连接",
                "请确保 Chrome 扩展已安装并启用，再尝试恢复为标签组。",
            )
            return
        group_row = self.db.get_group_by_id(group_id)
        if group_row is None:
            return

        tabs = self.db.get_tabs(group_row.id)
        urls = [t.url for t in tabs if t.url.startswith("http")]
        if not urls:
            QMessageBox.information(self, "无 URL", "该组没有可打开的标签页。")
            return
        self.bridge.request_restore(
            group_row.title, group_row.color_name or "blue", urls
        )

    def restore_selected_window(self) -> None:
        data = self.get_selected_node()
        if data and data.get("type") == "window":
            self.restore_window_by_id(data["id"])

    def save_selected_window(self) -> None:
        """Save the selected window as a standalone snapshot.

        The window's groups + tabs are copied into a new snapshot flagged
        ``source='saved'`` so it survives independently in the tree (under a
        ⭐ "已保存窗口" profile) and can be opened/restored later like a
        history entry, even if the original scan snapshot is deleted.
        """
        data = self.get_selected_node()
        if not data or data.get("type") != "window":
            QMessageBox.information(
                self,
                "请选择窗口",
                "请先在左侧选中一个窗口节点，再点「保存窗口」。",
            )
            return
        win = self.db.get_window_by_id(data["id"])
        if win is None:
            return
        new_sid = self.db.save_window_as_snapshot(data["id"], title=win.title)
        if new_sid is None:
            QMessageBox.information(self, "无法保存", "该窗口没有可保存的标签。")
            return
        # Only the History tree changes (the saved snapshot lands there);
        # refreshing just it preserves the Current tab's expand/scroll state.
        self._refresh_saved()
        # Jump to the History tab so the user immediately sees the result.
        self.tabs.setCurrentWidget(self.tree_saved)
        self.log_status(f"已保存窗口「{win.title}」到历史")

    def open_window_by_id(self, window_id: int) -> None:
        groups = self.db.get_groups_by_window(window_id)
        urls: list[str] = []
        for g in groups:
            for t in self.db.get_tabs(g.id):
                if t.url.startswith("http"):
                    urls.append(t.url)
        if urls:
            open_urls(urls)
            self.log_status(f"已打开窗口 {len(urls)} 个标签页")

    def restore_window_by_id(self, window_id: int) -> None:
        if not self.bridge.is_extension_connected:
            QMessageBox.information(
                self,
                "扩展未连接",
                "请确保 Chrome 扩展已安装并启用，再尝试恢复窗口。",
            )
            return
        win = self.db.get_window_by_id(window_id)
        if win is None:
            return
        groups = self.db.get_groups_by_window(window_id)
        payload_groups = []
        for g in groups:
            urls = [t.url for t in self.db.get_tabs(g.id) if t.url.startswith("http")]
            if urls:
                payload_groups.append(
                    {"title": g.title, "color": g.color_name or "grey", "urls": urls}
                )
        if not payload_groups:
            QMessageBox.information(self, "无 URL", "该窗口没有可打开的标签页。")
            return
        self.bridge.request_restore_window(win.title, payload_groups)

    def delete_selected_snapshot(self) -> None:
        data = self.get_selected_node()
        if not data or data.get("type") != "snapshot":
            QMessageBox.information(self, "请选择快照", "请先选择一个快照再删除。")
            return
        reply = QMessageBox.question(
            self,
            "确认删除",
            "删除这个快照及其所有标签组？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.db.delete_snapshot(data["id"])
            self.refresh_tree()
            self.log_status("快照已删除")


def run() -> None:
    app = QApplication([])
    # Native platform style (no custom stylesheet). See gui.py run().
    app.setApplicationName("Chrome Tab Group Manager")
    # Set the app icon so the macOS Dock (and window title bar) show our
    # branded icon instead of the generic Python interpreter icon.
    app.setWindowIcon(app_icon(256))
    app.setFont(QFont("SF Pro Text", 13) if sys.platform == "darwin" else QFont("Segoe UI", 13))
    window = MainWindow()
    window.show()
    app.exec()
