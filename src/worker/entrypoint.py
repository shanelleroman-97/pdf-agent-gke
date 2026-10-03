#!/usr/bin/env python3
# Adapted from the GoogleCloudPlatform/kubernetes-engine-samples
# anthropic-agent-sandbox worker (Apache License 2.0). Changes: Python SDK
# worker instead of `ant`, PDF input staged by the dispatcher over HTTP,
# outputs collected by the dispatcher instead of a GCS FUSE mount.
"""Warm-pool entrypoint for one PDF-analysis session.

Lifecycle of a pod:
  waiting  - pre-warmed, serving only the dispatch endpoint
  running  - dispatcher POSTed the session binding + PDF; SDK worker runs tools
  done     - worker returned; outputs are ready for the dispatcher to collect
  failed   - worker raised or was cancelled; partial outputs still collectable

The pod has no GCP identity and can only reach api.anthropic.com, so the
dispatcher moves data in and out over this in-cluster HTTP API:
  POST /dispatch  headers X-Session-Id, X-Work-Id, [X-Work-Secret,
                  X-Input-Filename]; body is the raw PDF (may be empty)
  GET  /status    {"phase": ..., "session_id": ...}
  GET  /outputs   tar.gz of /workspace/outputs (only once done/failed)
NetworkPolicy only admits the dispatcher to port 8080.
"""
import asyncio
import contextlib
import io
import json
import os
import re
import signal
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from anthropic import AsyncAnthropic
from anthropic.lib.environments import EnvironmentWorker

PORT = int(os.environ.get("DISPATCH_PORT", "8080"))
WORKDIR = Path(os.environ.get("WORKDIR", "/workspace"))
INPUT_DIR = WORKDIR / "input"
OUTPUT_DIR = WORKDIR / "outputs"
MAX_INPUT_BYTES = int(os.environ.get("MAX_INPUT_BYTES", str(100 * 1024 * 1024)))
MAX_OUTPUT_BYTES = int(os.environ.get("MAX_OUTPUT_BYTES", str(200 * 1024 * 1024)))
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")


def log(event: str, **fields) -> None:
    """Structured log line on stderr (Cloud Logging parses JSON)."""
    print(json.dumps({"event": event, **fields}), file=sys.stderr, flush=True)


class State:
    """Pod state shared between the HTTP thread and the main thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.phase = "waiting"
        self.dispatch: dict | None = None
        self.dispatched = threading.Event()


STATE = State()


def build_outputs_tarball() -> bytes:
    """Tar regular files under OUTPUT_DIR. Symlinks and special files are
    skipped so the agent can't use them to pull other paths into the archive."""
    buf = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if OUTPUT_DIR.is_dir():
            for path in sorted(OUTPUT_DIR.rglob("*")):
                if path.is_symlink() or not path.is_file():
                    continue
                total += path.stat().st_size
                if total > MAX_OUTPUT_BYTES:
                    raise ValueError("outputs exceed MAX_OUTPUT_BYTES")
                tar.add(path, arcname=str(path.relative_to(OUTPUT_DIR)), recursive=False)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    """Dispatcher-facing API. Accepts exactly one dispatch per pod."""

    def _reply(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        """Bind this pod to a session and stage its input PDF."""
        if self.path != "/dispatch":
            return self._reply(404)
        session_id = self.headers.get("X-Session-Id", "")
        work_id = self.headers.get("X-Work-Id", "")
        filename = self.headers.get("X-Input-Filename") or "document.pdf"
        length = int(self.headers.get("Content-Length", "0"))
        if not (ID_RE.match(session_id) and ID_RE.match(work_id) and FILENAME_RE.match(filename)):
            return self._reply(400)
        if length > MAX_INPUT_BYTES:
            return self._reply(413)

        with STATE.lock:
            if STATE.phase != "waiting":
                return self._reply(409)
            body = self.rfile.read(length) if length else b""
            if body and not body.startswith(b"%PDF-"):
                return self._reply(415)
            if body:
                INPUT_DIR.mkdir(parents=True, exist_ok=True)
                (INPUT_DIR / filename).write_bytes(body)
            STATE.dispatch = {
                "session_id": session_id,
                "work_id": work_id,
                "work_secret": self.headers.get("X-Work-Secret") or None,
                "input": str(INPUT_DIR / filename) if body else None,
            }
            STATE.phase = "running"
            STATE.dispatched.set()
        self._reply(202)

    def do_GET(self):  # noqa: N802
        """Report phase, or hand over the outputs once the session has finished."""
        with STATE.lock:
            phase = STATE.phase
            session_id = (STATE.dispatch or {}).get("session_id")
        if self.path == "/status":
            body = json.dumps({"phase": phase, "session_id": session_id}).encode()
            return self._reply(200, body)
        if self.path == "/outputs":
            if phase not in ("done", "failed"):
                return self._reply(409)
            try:
                return self._reply(200, build_outputs_tarball(), "application/gzip")
            except ValueError:
                return self._reply(413)
        self._reply(404)

    def log_message(self, *_):
        """Suppress the stdlib per-request access log."""


async def run_session(dispatch: dict) -> None:
    """Serve the claimed work item with the SDK worker until the session ends."""
    environment_key = os.environ["ANTHROPIC_ENVIRONMENT_KEY"]
    async with AsyncAnthropic(auth_token=environment_key) as client:
        worker = EnvironmentWorker(
            client,
            environment_id=os.environ["ANTHROPIC_ENVIRONMENT_ID"],
            environment_key=environment_key,
            workdir=str(WORKDIR),
        )
        task = asyncio.create_task(
            worker.handle_item(
                work_id=dispatch["work_id"],
                session_id=dispatch["session_id"],
                work_secret=dispatch["work_secret"],
            )
        )
        # Cancel rather than die on SIGTERM so the worker can stop the work
        # item cleanly before the pod goes away.
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, task.cancel)
        await task


def main() -> None:
    """Wait for dispatch, run the session, then keep serving /outputs until reaped."""
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log("ready", port=PORT)

    STATE.dispatched.wait()
    dispatch = STATE.dispatch
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log("dispatched", session_id=dispatch["session_id"], work_id=dispatch["work_id"],
        has_input=dispatch["input"] is not None)

    phase = "done"
    try:
        asyncio.run(run_session(dispatch))
    except (Exception, asyncio.CancelledError) as e:  # noqa: BLE001 - report any failure to the dispatcher
        phase = "failed"
        log("session_failed", session_id=dispatch["session_id"], error=repr(e))
    with STATE.lock:
        STATE.phase = phase
    log("session_finished", session_id=dispatch["session_id"], phase=phase)

    # Block until the dispatcher collects outputs and deletes the claim.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with contextlib.suppress(KeyboardInterrupt):
        threading.Event().wait()


if __name__ == "__main__":
    main()
