"""Dispatcher unit tests: GCS and Kubernetes are faked, the worker is real.

Run: pip install -r tests/requirements.txt && pytest tests/
"""
import datetime
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import time
import types
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dispatcher = load("dispatcher_main", ROOT / "src/dispatcher/main.py")


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    @property
    def size(self):
        return len(self.store[self.name])

    def download_as_bytes(self):
        return self.store[self.name]

    def upload_from_string(self, data, content_type=None):
        self.store[self.name] = data if isinstance(data, bytes) else data.encode()


class FakeBucket:
    def __init__(self, name):
        self.name, self.store = name, {}

    def blob(self, name):
        return FakeBlob(self.store, name)

    def get_blob(self, name):
        return FakeBlob(self.store, name) if name in self.store else None


def make_dispatcher(**overrides):
    args = types.SimpleNamespace(
        environment_id="env_test", inputs_bucket="in", outputs_bucket="out", namespace="ns",
        template="t", warmpool="t", dispatch_port=0, ready_timeout=5, block_ms=1, idle_backoff=0,
        reclaim_older_than_ms=1, max_redispatch=3, max_input_bytes=1024, claim_ttl_seconds=60,
        reap_interval=1, unreachable_grace_seconds=300,
    )
    vars(args).update(overrides)
    d = dispatcher.Dispatcher.__new__(dispatcher.Dispatcher)
    d.args, d.inputs, d.outputs = args, FakeBucket("in"), FakeBucket("out")
    d.co = mock.MagicMock()
    d.deleted = []
    d.delete_claim = d.deleted.append
    return d


def session(**metadata):
    return types.SimpleNamespace(metadata=metadata)


# ---- load_input -------------------------------------------------------------

def test_load_input_none_without_metadata():
    assert make_dispatcher().load_input(session()) is None


@pytest.mark.parametrize("obj", ["secrets/x.pdf", "inputs/../outputs/x", "../inputs/x.pdf"])
def test_load_input_rejects_paths_outside_inputs(obj):
    with pytest.raises(ValueError):
        make_dispatcher().load_input(session(input_object=obj))


def test_load_input_size_limit_and_missing():
    d = make_dispatcher(max_input_bytes=10)
    d.inputs.store["inputs/a/big.pdf"] = b"%PDF-" + b"x" * 20
    with pytest.raises(ValueError):
        d.load_input(session(input_object="inputs/a/big.pdf"))
    with pytest.raises(FileNotFoundError):
        d.load_input(session(input_object="inputs/a/missing.pdf"))


def test_load_input_returns_filename_and_bytes():
    d = make_dispatcher()
    d.inputs.store["inputs/a/doc.pdf"] = b"%PDF-1.7"
    assert d.load_input(session(input_object="inputs/a/doc.pdf", input_filename="doc.pdf")) == (
        "doc.pdf", b"%PDF-1.7")


# ---- reaper against a real worker process --------------------------------------

@pytest.fixture
def worker(tmp_path):
    """Run the real worker entrypoint; its session fails fast (no API reachable)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = os.environ | {
        "ANTHROPIC_ENVIRONMENT_KEY": "sk-ant-oat01-fake", "ANTHROPIC_ENVIRONMENT_ID": "env_test",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "WORKDIR": str(tmp_path), "DISPATCH_PORT": str(port),
    }
    proc = subprocess.Popen([sys.executable, str(ROOT / "src/worker/entrypoint.py")], env=env,
                            stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            dispatcher.http("GET", f"http://127.0.0.1:{port}/status", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    yield port, tmp_path
    proc.terminate()
    proc.wait()


def claim(name, session_id, host="127.0.0.1", age_seconds=0):
    dispatched_at = (datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(seconds=age_seconds)).isoformat()
    return {"metadata": {
        "name": name,
        "labels": {dispatcher.SESSION_LABEL: session_id},
        "annotations": {dispatcher.POD_IP_ANNOTATION: host,
                        dispatcher.DISPATCHED_AT_ANNOTATION: dispatched_at},
    }}


def dispatch_to(port, session_id):
    dispatcher.http("POST", f"http://127.0.0.1:{port}/dispatch", body=b"%PDF-1.4 x", headers={
        "X-Session-Id": session_id, "X-Work-Id": "work_1", "X-Input-Filename": "doc.pdf"})


def wait_finished(port):
    for _ in range(100):
        if json.loads(dispatcher.http("GET", f"http://127.0.0.1:{port}/status"))["phase"] != "running":
            return
        time.sleep(0.1)
    raise AssertionError("worker never finished")


def test_reaper_deletes_claim_when_pod_ip_serves_another_session(worker):
    port, _ = worker
    d = make_dispatcher(dispatch_port=port)
    d.list_claims = lambda _sel: [claim("c1", "sesn_1")]
    # The pod at this IP was never given sesn_1 (IP reused), so the claim is stale.
    d.reap_once()
    assert d.deleted == ["c1"]


def test_reaper_collects_outputs_then_deletes(worker):
    port, workdir = worker
    dispatch_to(port, "sesn_ok")
    (workdir / "outputs").mkdir(exist_ok=True)
    (workdir / "outputs" / "report.md").write_text("# Report")
    (workdir / "outputs" / "findings.json").write_text('{"findings": []}')
    (workdir / "outputs" / "chart.png").write_bytes(b"png")
    wait_finished(port)

    d = make_dispatcher(dispatch_port=port)
    d.list_claims = lambda _sel: [claim("c1", "sesn_ok")]
    d.reap_once()

    assert d.deleted == ["c1"]
    store = d.outputs.store
    assert store["sesn_ok/report.md"] == b"# Report"
    assert json.loads(store["sesn_ok/status.json"])["files"] == ["report.md", "findings.json"]
    with tarfile.open(fileobj=io.BytesIO(store["sesn_ok/outputs.tar.gz"])) as tar:
        assert sorted(tar.getnames()) == ["chart.png", "findings.json", "report.md"]


def test_reaper_unreachable_respects_grace():
    d = make_dispatcher(dispatch_port=1, unreachable_grace_seconds=300)
    d.list_claims = lambda _sel: [claim("young", "sesn_a", age_seconds=10),
                                  claim("old", "sesn_b", age_seconds=600)]
    d.reap_once()
    assert d.deleted == ["old"]
