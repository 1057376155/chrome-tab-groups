"""SQLite data layer for Chrome tab groups."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import DB_PATH


@dataclass
class Profile:
    id: int
    profile_dir: str
    name: str
    email: str
    created_at: datetime


@dataclass
class Snapshot:
    id: int
    profile_id: int
    source: str
    created_at: datetime
    profile_dir: str = ""
    profile_name: str = ""


@dataclass
class Window:
    id: int
    snapshot_id: int
    source_window_id: Optional[int]  # Chrome 原生 windowId, None 表示未知
    title: str
    sort_order: int


@dataclass
class Group:
    id: int
    snapshot_id: int
    title: str
    color_name: str
    color_id: int
    collapsed: bool
    uuid: str
    sort_order: int
    window_id: Optional[int] = None  # FK -> windows.id, None = 无窗口归属(老数据)


@dataclass
class Tab:
    id: int
    group_id: int
    title: str
    url: str
    original_tab_id: Optional[int]
    sort_order: int


class Database:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = __import__("threading").Lock()
        self._ensure_tables()

    def _connect(self) -> sqlite3.Connection:
        """Return the shared connection, opening it if needed.

        A single shared connection is reused across all queries. SQLite
        foreign-key enforcement (and therefore ON DELETE CASCADE) only works
        when ``PRAGMA foreign_keys=ON`` is set per-connection, so it must be
        applied here. All writes go through ``with self._lock`` for safety.

        ``busy_timeout`` makes SQLite wait (instead of immediately raising
        "database is locked") when another connection is writing, which gives
        the GUI thread and the bridge HTTP threads a grace period during
        concurrent access.
        """
        if self._conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")  # 5s wait on contention
            self._conn = conn
        return self._conn

    def _ensure_tables(self) -> None:
        conn = self._connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_dir TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                email TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                fingerprint TEXT,
                FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS windows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                source_window_id INTEGER,
                title TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                window_id INTEGER,
                title TEXT NOT NULL,
                color_name TEXT,
                color_id INTEGER,
                collapsed INTEGER NOT NULL DEFAULT 0,
                uuid TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE,
                FOREIGN KEY (window_id) REFERENCES windows(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tabs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                title TEXT,
                url TEXT NOT NULL,
                original_tab_id INTEGER,
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_groups_snapshot ON groups(snapshot_id);
            CREATE INDEX IF NOT EXISTS idx_windows_snapshot ON windows(snapshot_id);
            CREATE INDEX IF NOT EXISTS idx_tabs_group ON tabs(group_id);
            """
        )
        # Ensure the fingerprint column exists (added after initial release;
        # the index below references it so it must be created after the column).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
        if "fingerprint" not in cols:
            conn.execute("ALTER TABLE snapshots ADD COLUMN fingerprint TEXT")
        # Ensure groups.window_id exists (added when window dimension was
        # introduced). Old DBs created the table without this column, and the
        # index below references it so it must be created after the column.
        g_cols = {r[1] for r in conn.execute("PRAGMA table_info(groups)")}
        if "window_id" not in g_cols:
            conn.execute("ALTER TABLE groups ADD COLUMN window_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_fp ON snapshots(profile_id, fingerprint)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_groups_window ON groups(window_id)"
        )
        conn.commit()

    @staticmethod
    def _snapshot_fingerprint(groups: List[Dict[str, Any]]) -> str:
        """Stable hash of a snapshot's content for de-duplication.

        Two snapshots with the same groups (same titles/colors/tab URLs in the
        same order, in the same windows) produce the same fingerprint, so
        repeated scans of an unchanged Chrome profile collapse into one row.
        The source window id is part of the hash so that two windows holding
        otherwise-identical groups are not treated as duplicates.
        """
        import hashlib
        import json as _json

        payload = []
        for g in sorted(
            groups,
            key=lambda x: (
                x.get("window_id") is not None and x.get("window_id") or 0,
                x.get("title", ""),
                x.get("uuid", ""),
            ),
        ):
            tabs = sorted(
                g.get("tabs", []),
                key=lambda t: (t.get("url", ""), t.get("title", "")),
            )
            payload.append(
                {
                    "window": g.get("window_id"),
                    "title": g.get("title", ""),
                    "color": g.get("color_name", ""),
                    "uuid": g.get("uuid", ""),
                    "tabs": [
                        {"url": t.get("url", ""), "title": t.get("title", "")}
                        for t in tabs
                    ],
                }
            )
        return hashlib.sha1(_json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()

    def ensure_profile(self, profile_dir: str, name: str, email: str = "") -> Profile:
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "SELECT id, profile_dir, name, email, created_at FROM profiles WHERE profile_dir = ?",
                (profile_dir,),
            )
            row = cur.fetchone()
            if row:
                return Profile(**dict(row))

            now = datetime.now().isoformat()
            cur = conn.execute(
                "INSERT INTO profiles (profile_dir, name, email, created_at) VALUES (?, ?, ?, ?)",
                (profile_dir, name, email, now),
            )
            conn.commit()
            return Profile(
                id=cur.lastrowid,
                profile_dir=profile_dir,
                name=name,
                email=email,
                created_at=datetime.fromisoformat(now),
            )

    def import_snapshot(
        self,
        profile_dir: str,
        profile_name: str,
        email: str,
        groups: List[Dict[str, Any]],
        source: str = "snss",
    ) -> int:
        """Import a snapshot and return its id.

        If the most recent snapshot for this profile has the same content
        fingerprint, that existing snapshot id is returned instead of creating
        a duplicate — repeated scans of an unchanged profile stay idempotent.

        Groups may carry a ``window_id`` field (the Chrome-native window id).
        We aggregate those into ``windows`` rows and link each group to its
        window. Groups without a window_id are linked to a synthetic
        "(unknown window)" row so the tree still has a window layer.
        """
        profile = self.ensure_profile(profile_dir, profile_name, email)
        fingerprint = self._snapshot_fingerprint(groups)
        now = datetime.now().isoformat()

        with self._lock:
            conn = self._connect()
            # De-dup: reuse the latest snapshot if its content is identical.
            cur = conn.execute(
                """SELECT id FROM snapshots
                   WHERE profile_id = ? AND fingerprint = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (profile.id, fingerprint),
            )
            existing = cur.fetchone()
            if existing is not None:
                return existing["id"]

            cur = conn.execute(
                "INSERT INTO snapshots (profile_id, source, created_at, fingerprint) VALUES (?, ?, ?, ?)",
                (profile.id, source, now, fingerprint),
            )
            snapshot_id = cur.lastrowid

            # Aggregate the distinct source window ids referenced by the
            # incoming groups, preserving first-seen order for stable display.
            # window_id of None/0 means "unknown" — still gets a window row so
            # the GUI tree has a consistent 5-level shape.
            seen: Dict[Any, int] = {}  # source_window_id -> sort order
            ordered: List[Any] = []
            for g in groups:
                wid = g.get("window_id")
                if wid not in seen:
                    seen[wid] = len(ordered)
                    ordered.append(wid)
            if not ordered:
                ordered = [None]

            # Insert one window row per distinct source window id.
            db_window_ids: Dict[Any, int] = {}
            for sort_idx, src_wid in enumerate(ordered):
                title = (
                    f"窗口 {sort_idx + 1}"
                    if src_wid is not None
                    else "(未知窗口)"
                )
                cur = conn.execute(
                    """INSERT INTO windows
                       (snapshot_id, source_window_id, title, sort_order)
                       VALUES (?, ?, ?, ?)""",
                    (snapshot_id, src_wid, title, sort_idx),
                )
                db_window_ids[src_wid] = cur.lastrowid

            for g_idx, g in enumerate(groups):
                src_wid = g.get("window_id")
                db_wid = db_window_ids.get(src_wid, db_window_ids[ordered[0]])
                cur = conn.execute(
                    """
                    INSERT INTO groups
                    (snapshot_id, window_id, title, color_name, color_id, collapsed, uuid, sort_order)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        db_wid,
                        g.get("title", "(untitled)")[:500],
                        g.get("color_name", ""),
                        g.get("color_id", -1),
                        1 if g.get("collapsed") else 0,
                        g.get("uuid", "")[:64],
                        g_idx,
                    ),
                )
                group_id = cur.lastrowid
                tabs = g.get("tabs", [])
                for t_idx, t in enumerate(tabs):
                    conn.execute(
                        """
                        INSERT INTO tabs
                        (group_id, title, url, original_tab_id, sort_order)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            group_id,
                            (t.get("title") or "")[:1000],
                            t.get("url", ""),
                            t.get("tab_id"),
                            t_idx,
                        ),
                    )
            conn.commit()
        return snapshot_id

    def get_profiles(self, exclude_saved: bool = False) -> List[Profile]:
        """Return all profiles.

        With ``exclude_saved=True``, profiles created by "save window" (whose
        profile_dir starts with the ``__saved__/`` prefix) are filtered out —
        those belong to the History tab, not the Current tab.
        """
        conn = self._connect()
        if exclude_saved:
            # substr() prefix match avoids LIKE's wildcard semantics (where
            # '_' matches any char) — we want a literal "__saved__/" prefix.
            cur = conn.execute(
                """SELECT id, profile_dir, name, email, created_at
                   FROM profiles
                   WHERE substr(profile_dir, 1, 10) != '__saved__/'
                   ORDER BY name"""
            )
        else:
            cur = conn.execute(
                "SELECT id, profile_dir, name, email, created_at FROM profiles ORDER BY name"
            )
        return [Profile(**dict(row)) for row in cur.fetchall()]

    def get_saved_profiles(self) -> List[Profile]:
        """Return only the synthetic profiles created by "save window"."""
        conn = self._connect()
        cur = conn.execute(
            """SELECT id, profile_dir, name, email, created_at
               FROM profiles
               WHERE substr(profile_dir, 1, 10) = '__saved__/'
               ORDER BY name"""
        )
        return [Profile(**dict(row)) for row in cur.fetchall()]

    def get_snapshots(
        self,
        profile_id: Optional[int] = None,
        source: Optional[str] = None,
    ) -> List[Snapshot]:
        """Return snapshots, optionally filtered by profile and/or source.

        ``source`` accepts a single value (e.g. 'saved'); pass
        ``source='saved'`` for the History tab, or filter on the caller side
        for "not saved" (current snapshots). When ``profile_id`` is given,
        results are scoped to that profile.
        """
        conn = self._connect()
        clauses = []
        params: list = []
        if profile_id is not None:
            clauses.append("s.profile_id = ?")
            params.append(profile_id)
        if source is not None:
            clauses.append("s.source = ?")
            params.append(source)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = conn.execute(
            f"""SELECT s.id, s.profile_id, s.source, s.created_at,
                       p.profile_dir, p.name AS profile_name
                FROM snapshots s
                JOIN profiles p ON p.id = s.profile_id
                {where}
                ORDER BY s.created_at DESC""",
            params,
        )
        return [
            Snapshot(
                id=row["id"],
                profile_id=row["profile_id"],
                source=row["source"],
                created_at=datetime.fromisoformat(row["created_at"]),
                profile_dir=row["profile_dir"],
                profile_name=row["profile_name"],
            )
            for row in cur.fetchall()
        ]

    def get_windows(self, snapshot_id: int) -> List[Window]:
        conn = self._connect()
        cur = conn.execute(
            """SELECT id, snapshot_id, source_window_id, title, sort_order
               FROM windows WHERE snapshot_id = ? ORDER BY sort_order""",
            (snapshot_id,),
        )
        return [
            Window(
                id=row["id"],
                snapshot_id=row["snapshot_id"],
                source_window_id=row["source_window_id"],
                title=row["title"] or "",
                sort_order=row["sort_order"],
            )
            for row in cur.fetchall()
        ]

    def get_window_by_id(self, window_id: int) -> Optional[Window]:
        conn = self._connect()
        cur = conn.execute(
            """SELECT id, snapshot_id, source_window_id, title, sort_order
               FROM windows WHERE id = ?""",
            (window_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return Window(
            id=row["id"],
            snapshot_id=row["snapshot_id"],
            source_window_id=row["source_window_id"],
            title=row["title"] or "",
            sort_order=row["sort_order"],
        )

    def get_groups(self, snapshot_id: int) -> List[Group]:
        conn = self._connect()
        cur = conn.execute(
            """SELECT id, snapshot_id, window_id, title, color_name, color_id,
                      collapsed, uuid, sort_order
               FROM groups WHERE snapshot_id = ? ORDER BY sort_order""",
            (snapshot_id,),
        )
        return [self._row_to_group(row) for row in cur.fetchall()]

    def get_groups_by_window(self, window_id: int) -> List[Group]:
        """All groups belonging to a window, ordered as stored."""
        conn = self._connect()
        cur = conn.execute(
            """SELECT id, snapshot_id, window_id, title, color_name, color_id,
                      collapsed, uuid, sort_order
               FROM groups WHERE window_id = ? ORDER BY sort_order""",
            (window_id,),
        )
        return [self._row_to_group(row) for row in cur.fetchall()]

    @staticmethod
    def _row_to_group(row) -> Group:
        return Group(
            id=row["id"],
            snapshot_id=row["snapshot_id"],
            title=row["title"],
            color_name=row["color_name"] or "",
            color_id=row["color_id"] if row["color_id"] is not None else -1,
            collapsed=bool(row["collapsed"]),
            uuid=row["uuid"] or "",
            sort_order=row["sort_order"],
            window_id=row["window_id"] if "window_id" in row.keys() else None,
        )

    def get_group_by_id(self, group_id: int) -> Optional[Group]:
        """Fetch a single group by primary key (replaces O(S*G) scans)."""
        conn = self._connect()
        cur = conn.execute(
            """SELECT id, snapshot_id, window_id, title, color_name, color_id,
                      collapsed, uuid, sort_order
               FROM groups WHERE id = ?""",
            (group_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_group(row)

    def get_tabs(self, group_id: int) -> List[Tab]:
        conn = self._connect()
        cur = conn.execute(
            """SELECT id, group_id, title, url, original_tab_id, sort_order
               FROM tabs WHERE group_id = ? ORDER BY sort_order""",
            (group_id,),
        )
        return [
            Tab(
                id=row["id"],
                group_id=row["group_id"],
                title=row["title"] or "",
                url=row["url"],
                original_tab_id=row["original_tab_id"],
                sort_order=row["sort_order"],
            )
            for row in cur.fetchall()
        ]

    def delete_snapshot(self, snapshot_id: int) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
            conn.commit()

    def delete_profile(self, profile_id: int) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
            conn.commit()

    def get_latest_snapshot_for_profile(
        self, profile_dir: str
    ) -> Optional[datetime]:
        conn = self._connect()
        cur = conn.execute(
            """SELECT s.created_at
               FROM snapshots s
               JOIN profiles p ON p.id = s.profile_id
               WHERE p.profile_dir = ?
               ORDER BY s.created_at DESC LIMIT 1""",
            (profile_dir,),
        )
        row = cur.fetchone()
        return datetime.fromisoformat(row["created_at"]) if row else None

    def save_window_as_snapshot(self, window_id: int, title: str = "") -> Optional[int]:
        """Copy a single window (with its groups + tabs) into a standalone
        snapshot so the user can keep it as a "saved" / history entry that
        survives independently of the snapshot it came from.

        Returns the new snapshot id, or None if the source window had no
        groups. The new snapshot uses source='saved' so the GUI can render
        it with a distinct label.
        """
        src_win = self.get_window_by_id(window_id)
        if src_win is None:
            return None
        src_groups = self.get_groups_by_window(window_id)
        # Reconstruct the group dicts in the shape import_snapshot expects.
        groups_data: List[Dict[str, Any]] = []
        for g in src_groups:
            tabs = self.get_tabs(g.id)
            groups_data.append(
                {
                    "title": g.title,
                    "color_name": g.color_name,
                    "color_id": g.color_id,
                    "collapsed": g.collapsed,
                    "uuid": g.uuid,
                    "window_id": src_win.source_window_id,
                    "tabs": [
                        {"url": t.url, "title": t.title, "tab_id": t.original_tab_id}
                        for t in tabs
                    ],
                }
            )
        if not groups_data:
            return None
        # Resolve the parent profile (window -> snapshot -> profile).
        conn = self._connect()
        cur = conn.execute(
            "SELECT profile_id FROM snapshots WHERE id = ?",
            (src_win.snapshot_id,),
        )
        row = cur.fetchone()
        profile_id = row["profile_id"] if row else None
        if profile_id is None:
            return None
        cur = conn.execute(
            "SELECT profile_dir, name, email FROM profiles WHERE id = ?",
            (profile_id,),
        )
        prow = cur.fetchone()
        if prow is None:
            return None
        label = title or src_win.title or "已保存窗口"
        # Use a synthetic profile dir for saved windows so they cluster
        # separately from scanned snapshots in the tree.
        saved_profile_dir = f"__saved__/{label}"
        return self.import_snapshot(
            saved_profile_dir,
            f"⭐ {label}",
            prow["email"] or "",
            groups_data,
            source="saved",
        )
