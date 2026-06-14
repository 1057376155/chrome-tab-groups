"""Local HTTP bridge that lets the bundled Chrome extension talk to Python."""

import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from .config import BRIDGE_HOST, BRIDGE_PORT
from .db import Database


# How long a long-poll /pending request holds the connection open waiting for a
# command before returning NONE. Lets the extension get sub-second responsiveness
# even though chrome.alarms only fires ~once a minute.
LONG_POLL_TIMEOUT = 25.0

# A connection is considered live if the extension has contacted us within
# this window. Long-polling means a healthy extension pings us continuously.
CONNECTION_TTL = 30.0

# How long a queued command is allowed to stay "pending" before we give up
# and report it as failed. Bounds the damage of a lost /ack so a single
# dropped ack does not cause the command to be re-issued forever.
PENDING_MAX_AGE = 120.0


class BridgeSignals(QObject):
    """Signals must live in the main thread and are queued to it automatically."""

    extension_connected = pyqtSignal(bool)
    snapshot_received = pyqtSignal(int)  # snapshot id
    restore_ack = pyqtSignal(str, bool, str)  # command_id, success, message
    log_message = pyqtSignal(str)


class Bridge:
    """HTTP bridge between the PyQt6 GUI and the bundled Chrome extension."""

    def __init__(self, db: Database, host: str = BRIDGE_HOST, port: int = BRIDGE_PORT):
        self.db = db
        self.host = host
        self.port = port
        self.signals = BridgeSignals()
        self._commands: queue.Queue[Dict[str, Any]] = queue.Queue()
        self._lock = threading.Lock()
        self._pending_command: Optional[Dict[str, Any]] = None
        self._last_seen = 0.0
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        """Start the HTTP server. Returns False (and logs) if the port is busy."""
        if self._server is not None:
            return True
        handler = _make_handler(self)
        try:
            self._server = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as exc:
            self._server = None
            self.signals.log_message.emit(
                f"Bridge failed to bind {self.host}:{self.port}: {exc}"
            )
            return False
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.signals.log_message.emit(f"Bridge listening on {self.host}:{self.port}")
        return True

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None
            self.signals.log_message.emit("Bridge stopped")

    def _touch(self) -> None:
        """Mark that the extension just contacted us (from any HTTP handler)."""
        with self._lock:
            was_connected = (time.time() - self._last_seen) < CONNECTION_TTL
            self._last_seen = time.time()
        if not was_connected:
            self.signals.extension_connected.emit(True)

    @property
    def is_extension_connected(self) -> bool:
        with self._lock:
            return (time.time() - self._last_seen) < CONNECTION_TTL

    def request_capture(self) -> None:
        self._commands.put({"id": f"capture_{uuid.uuid4().hex[:8]}", "type": "CAPTURE"})
        self.signals.log_message.emit("Requested live capture from Chrome extension")

    def request_restore(
        self, title: str, color_name: str, urls: list[str]
    ) -> None:
        if not urls:
            return
        command = {
            "id": f"restore_{uuid.uuid4().hex[:8]}",
            "type": "RESTORE",
            "payload": {
                "title": title,
                "color": color_name or "blue",
                "urls": urls,
            },
        }
        self._commands.put(command)
        self.signals.log_message.emit(
            f"Requested restore of '{title}' ({len(urls)} tabs)"
        )

    def request_restore_window(
        self, window_title: str, groups: list[Dict[str, Any]]
    ) -> None:
        """Queue a RESTORE_WINDOW command.

        ``groups`` is a list of {title, color, urls} dicts describing every
        group/tab that lived in this window; the extension opens them all in
        one new Chrome window and rebuilds each non-virtual group natively.
        """
        payload_groups = []
        total = 0
        for g in groups:
            urls = [u for u in g.get("urls", []) if u.startswith("http")]
            if not urls:
                continue
            payload_groups.append(
                {
                    "title": g.get("title", ""),
                    "color": g.get("color", "grey"),
                    "urls": urls,
                }
            )
            total += len(urls)
        if not payload_groups:
            return
        command = {
            "id": f"restore_window_{uuid.uuid4().hex[:8]}",
            "type": "RESTORE_WINDOW",
            "payload": {
                "window_title": window_title,
                "groups": payload_groups,
            },
        }
        self._commands.put(command)
        self.signals.log_message.emit(
            f"Requested restore of '{window_title}' ({len(payload_groups)} groups, {total} tabs)"
        )

    def _pop_command(self, wait: float = 0.0) -> Optional[Dict[str, Any]]:
        """Pop the next command to send to the extension.

        With ``wait > 0`` this blocks up to ``wait`` seconds for a command to
        arrive (long-polling), so the extension gets an immediate response even
        though chrome.alarms throttles it to ~once per minute. A pending
        command that has not been acked yet is re-issued so a dropped ack does
        not strand the command forever.

        To bound the damage of a permanently-lost ack, a command is abandoned
        after PENDING_MAX_AGE seconds and reported as failed to the GUI;
        otherwise a single dropped ack would cause duplicate RESTOREs on every
        subsequent poll indefinitely.
        """
        # The extension is talking to us right now regardless of outcome.
        self._touch()

        with self._lock:
            if self._pending_command is None:
                # Pop from the queue *non-blocking* inside the lock; if it
                # would block, release the lock first so other handlers
                # (/status, /ack) are not stalled for the whole long-poll.
                try:
                    self._pending_command = self._commands.get_nowait()
                    self._pending_command["_first_sent"] = time.time()
                except queue.Empty:
                    self._pending_command = None

        # If the queue was empty and we are long-polling, wait outside the
        # lock so concurrent handlers stay responsive.
        if self._pending_command is None and wait > 0:
            try:
                cmd = self._commands.get(timeout=wait)
            except queue.Empty:
                return None
            with self._lock:
                cmd["_first_sent"] = time.time()
                self._pending_command = cmd
            return cmd

        if self._pending_command is None:
            return None

        # Expire a pending command whose ack was lost too long ago.
        first_sent = self._pending_command.get("_first_sent", time.time())
        if time.time() - first_sent > PENDING_MAX_AGE:
            expired = self._pending_command
            with self._lock:
                self._pending_command = None
            self.signals.restore_ack.emit(
                expired.get("id", ""), False, "恢复超时：扩展未在限定时间内确认"
            )
            self.signals.log_message.emit(
                f"Command {expired.get('id')} expired without ack"
            )
            return None

        return self._pending_command

    def _handle_snapshot(self, data: Dict[str, Any]) -> None:
        profile_dir = data.get("profile_dir", "Live")
        profile_name = data.get("profile_name", "Live Capture")
        email = data.get("email", "")
        groups = data.get("groups", [])
        try:
            snapshot_id = self.db.import_snapshot(
                profile_dir, profile_name, email, groups, source="extension"
            )
            self.signals.snapshot_received.emit(snapshot_id)
            self.signals.log_message.emit(
                f"Imported live snapshot #{snapshot_id} with {len(groups)} groups"
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.signals.log_message.emit(f"Failed to import snapshot: {exc}")

    def _handle_ack(self, command_id: str, success: bool, message: str) -> None:
        with self._lock:
            if (
                self._pending_command is not None
                and self._pending_command.get("id") == command_id
            ):
                self._pending_command = None
        self.signals.restore_ack.emit(command_id, success, message)
        status = "done" if success else "failed"
        self.signals.log_message.emit(f"Restore {status}: {message}")


def _make_handler(bridge: Bridge):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # suppress default logging
            pass

        def _send_json(self, status: int, body: Any) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            if self.path == "/status":
                # The extension's popup pings this to show connection state.
                bridge._touch()
                self._send_json(200, {"ok": True, "port": bridge.port})
            elif self.path == "/pending":
                # Long-poll: hold the request open briefly so the extension
                # gets a command within milliseconds of it being queued.
                parsed = self.path
                cmd = bridge._pop_command(wait=LONG_POLL_TIMEOUT)
                self._send_json(200, cmd or {"type": "NONE"})
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            # Guard against malformed / oversized Content-Length so a bad
            # request cannot OOM the bridge or crash the handler thread with
            # an unhandled ValueError.
            MAX_BODY = 50 * 1024 * 1024  # 50 MB cap
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "bad content-length"})
                return
            if length < 0 or length > MAX_BODY:
                self._send_json(413, {"error": "payload too large"})
                return
            body = self.rfile.read(length).decode("utf-8")
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._send_json(400, {"error": "bad json"})
                return

            # Any POST means the extension is alive too.
            bridge._touch()

            if self.path == "/snapshot":
                bridge._handle_snapshot(data)
                self._send_json(200, {"ok": True})
            elif self.path == "/ack":
                bridge._handle_ack(
                    data.get("id", ""),
                    bool(data.get("success")),
                    data.get("message", ""),
                )
                self._send_json(200, {"ok": True})
            else:
                self._send_json(404, {"error": "not found"})

    return _Handler
