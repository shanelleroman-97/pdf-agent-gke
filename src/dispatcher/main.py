# Adapted from the GoogleCloudPlatform/kubernetes-engine-samples
# anthropic-agent-sandbox dispatcher (Apache License 2.0). Changes: stages
# each session's PDF from GCS into its pod, collects outputs back to GCS,
# reaps finished claims, sets a claim TTL as a backstop, and acks work items
# (the sample's raw poll never acked, so the SDK worker had no lease).
"""Poll the Anthropic work queue and run each session in its own sandbox pod.

Per work item:
  1. Read the session's metadata (input_object) with the API key.
  2. Download that one PDF from the inputs bucket.
  3. Claim a warm gVisor pod and POST the session binding + PDF to it.

A reaper thread watches dispatched pods; when one reports done/failed it
pulls the outputs tarball, writes it (plus report.md / findings.json) to
gs://<outputs>/<session_id>/, and deletes the claim.

Sandbox pods never hold GCP credentials or reach GCS: this process is the
only thing that moves data in or out of them.
"""
import argparse
import datetime
import io
import json
import logging
import os
import tarfile
import threading
import time
import urllib.error
import urllib.request

import anthropic
from google.cloud import storage
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig
from kubernetes import client as k8s
from kubernetes import config as k8s_config

log = logging.getLogger("dispatcher")
SBX_GROUP, SBX_VERSION = "extensions.agents.x-k8s.io", "v1alpha1"

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY = "pdf-agent-dispatcher"
SESSION_LABEL = "anthropic.com/session-id"
DISPATCH_COUNT_LABEL = "anthropic.com/dispatch-count"
POD_IP_ANNOTATION = "pdf-agent.dev/pod-ip"
DISPATCHED_AT_ANNOTATION = "pdf-agent.dev/dispatched-at"
# Files promoted out of the tarball so clients can fetch them directly.
PROMOTED_OUTPUTS = ("report.md", "findings.json")


def parse_args():
    """Parse CLI flags, with defaults from the matching env vars."""
    p = argparse.ArgumentParser()
    p.add_argument("--environment-id", default=os.environ.get("ANTHROPIC_ENVIRONMENT_ID"))
    p.add_argument("--inputs-bucket", default=os.environ.get("INPUTS_BUCKET"))
    p.add_argument("--outputs-bucket", default=os.environ.get("OUTPUTS_BUCKET"))
    p.add_argument("--namespace", default=os.environ.get("SANDBOX_NAMESPACE", "agent-sandbox"))
    p.add_argument("--template", default=os.environ.get("SANDBOX_TEMPLATE", "pdf-agent-worker"))
    p.add_argument("--warmpool", default=os.environ.get("SANDBOX_WARMPOOL", "pdf-agent-worker"))
    p.add_argument("--dispatch-port", type=int, default=int(os.environ.get("DISPATCH_PORT", "8080")))
    p.add_argument("--ready-timeout", type=int,
                   default=int(os.environ.get("SANDBOX_READY_TIMEOUT", "120")))
    p.add_argument("--block-ms", type=int, default=int(os.environ.get("POLL_BLOCK_MS", "900")))
    p.add_argument("--idle-backoff", type=float,
                   default=float(os.environ.get("POLL_IDLE_BACKOFF_SECONDS", "1.0")))
    p.add_argument("--reclaim-older-than-ms", type=int,
                   default=int(os.environ.get("RECLAIM_OLDER_THAN_MS", "120000")))
    p.add_argument("--max-redispatch", type=int,
                   default=int(os.environ.get("MAX_REDISPATCH", "3")))
    p.add_argument("--max-input-bytes", type=int,
                   default=int(os.environ.get("MAX_INPUT_BYTES", str(100 * 1024 * 1024))))
    p.add_argument("--claim-ttl-seconds", type=int,
                   default=int(os.environ.get("CLAIM_TTL_SECONDS", str(6 * 3600))),
                   help="Backstop: the controller deletes a claim this long after creation.")
    p.add_argument("--reap-interval", type=float,
                   default=float(os.environ.get("REAP_INTERVAL_SECONDS", "15")))
    p.add_argument("--unreachable-grace-seconds", type=int,
                   default=int(os.environ.get("UNREACHABLE_GRACE_SECONDS", "300")))
    return p.parse_args()


def now_iso() -> str:
    """Current UTC time as RFC 3339."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def http(method: str, url: str, *, body: bytes | None = None, headers: dict | None = None,
         timeout: float = 30) -> bytes:
    """One HTTP request to a sandbox pod; raises on non-2xx."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class Dispatcher:
    """Claims work, stages inputs, and collects outputs."""

    def __init__(self, args):
        self.args = args
        # Environment key: poll/stop work items for this one environment only.
        self.work = anthropic.Anthropic(auth_token=os.environ["ANTHROPIC_ENVIRONMENT_KEY"])
        # Org API key: read session metadata. Never forwarded to a sandbox.
        self.api = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.gcs = storage.Client()
        self.inputs = self.gcs.bucket(args.inputs_bucket)
        self.outputs = self.gcs.bucket(args.outputs_bucket)
        self.sbx = SandboxClient(
            connection_config=SandboxInClusterConnectionConfig(
                use_pod_ip=True, server_port=args.dispatch_port
            )
        )
        self.co = k8s.CustomObjectsApi()
        self.core = k8s.CoreV1Api()

    # ---- claim bookkeeping -------------------------------------------------

    def list_claims(self, selector: str) -> list[dict]:
        """SandboxClaims in our namespace matching a label selector."""
        resp = self.co.list_namespaced_custom_object(
            SBX_GROUP, SBX_VERSION, self.args.namespace, "sandboxclaims", label_selector=selector
        )
        return resp.get("items", [])

    def delete_claim(self, name: str) -> None:
        """Delete a claim; the controller tears down its pod. 404 is success."""
        try:
            self.co.delete_namespaced_custom_object(
                SBX_GROUP, SBX_VERSION, self.args.namespace, "sandboxclaims", name
            )
        except k8s.ApiException as e:
            if e.status != 404:
                raise

    def reap_stale_claims(self, session_id: str) -> int:
        """Delete any prior claim for this session; return the highest dispatch count."""
        prev = 0
        for item in self.list_claims(f"{SESSION_LABEL}={session_id}"):
            meta = item["metadata"]
            prev = max(prev, int((meta.get("labels") or {}).get(DISPATCH_COUNT_LABEL, "0")))
            log.info("session=%s reaping stale claim=%s (dispatch #%d)", session_id, meta["name"], prev)
            self.delete_claim(meta["name"])
        return prev

    # ---- dispatch ----------------------------------------------------------

    def load_input(self, session) -> tuple[str, bytes] | None:
        """Fetch the session's PDF named in metadata.input_object, if any."""
        object_name = (session.metadata or {}).get("input_object")
        if not object_name:
            return None
        # Only accept objects the Submit API writes; reject traversal tricks.
        if not object_name.startswith("inputs/") or ".." in object_name:
            raise ValueError(f"refusing input_object {object_name!r}")
        blob = self.inputs.get_blob(object_name)
        if blob is None:
            raise FileNotFoundError(f"gs://{self.inputs.name}/{object_name} does not exist")
        if blob.size > self.args.max_input_bytes:
            raise ValueError(f"input is {blob.size} bytes, over max-input-bytes")
        filename = (session.metadata or {}).get("input_filename") or "document.pdf"
        return filename, blob.download_as_bytes()

    def dispatch(self, item) -> None:
        """Claim a warm pod for one work item and stage the session into it."""
        session_id, work_id = item.data.id, item.id
        prev = self.reap_stale_claims(session_id)
        attempt = prev + 1
        if attempt > self.args.max_redispatch:
            # Stop zombie work items (e.g. deleted sessions keep being re-offered).
            log.warning("session=%s exceeded max-redispatch=%d; stopping work item",
                        session_id, self.args.max_redispatch)
            self.work.beta.environments.work.stop(work_id, environment_id=self.args.environment_id)
            return

        session = self.api.beta.sessions.retrieve(session_id)
        if session.environment_id != self.args.environment_id:
            raise RuntimeError(f"session {session_id} belongs to {session.environment_id}")
        staged = self.load_input(session)

        sb = self.sbx.create_sandbox(
            template=self.args.template,
            namespace=self.args.namespace,
            warmpool=self.args.warmpool,
            labels={
                MANAGED_BY_LABEL: MANAGED_BY,
                SESSION_LABEL: session_id,
                DISPATCH_COUNT_LABEL: str(attempt),
            },
            sandbox_ready_timeout=self.args.ready_timeout,
            shutdown_after_seconds=self.args.claim_ttl_seconds,
        )
        acked = False
        try:
            # Read the pod IP from the core API: sb.get_pod_ip() is None on GKE
            # and the per-claim Service DNS is too fresh to resolve.
            pod_name = sb.get_pod_name()
            pod = self.core.read_namespaced_pod(pod_name, sb.namespace)
            host = pod.status.pod_ip
            if not host:
                raise RuntimeError(f"pod {pod_name} bound but has no podIP")
            # Record where the session runs so the reaper survives restarts.
            self.co.patch_namespaced_custom_object(
                SBX_GROUP, SBX_VERSION, self.args.namespace, "sandboxclaims", sb.claim_name,
                {"metadata": {"annotations": {
                    POD_IP_ANNOTATION: host,
                    DISPATCHED_AT_ANNOTATION: now_iso(),
                }}},
            )
            # Claim the item only now that a pod and the input are ready. Until
            # this ack the queue re-offers the item if we fail; after it, the
            # pod's worker holds the lease (handle_item requires a claimed item).
            self.work.beta.environments.work.ack(work_id, environment_id=self.args.environment_id)
            acked = True
            headers = {
                "X-Session-Id": session_id,
                "X-Work-Id": work_id,
                "Content-Type": "application/pdf",
            }
            if item.secret:
                headers["X-Work-Secret"] = item.secret
            body = b""
            if staged:
                headers["X-Input-Filename"] = staged[0]
                body = staged[1]
            http("POST", f"http://{host}:{self.args.dispatch_port}/dispatch",
                 body=body, headers=headers, timeout=60)
            log.info("session=%s work=%s -> pod=%s host=%s input_bytes=%d",
                     session_id, work_id, pod_name, host, len(body))
        except Exception:
            sb.terminate()
            if acked:
                # Acked items aren't re-offered; stop it so the session doesn't
                # sit waiting on a worker that will never come.
                self.work.beta.environments.work.stop(
                    work_id, environment_id=self.args.environment_id, force=True
                )
            raise

    def poll_forever(self) -> None:
        """Poll the Anthropic work queue forever and dispatch each item."""
        log.info("polling environment=%s template=%s ns=%s",
                 self.args.environment_id, self.args.template, self.args.namespace)
        while True:
            try:
                item = self.work.beta.environments.work.poll(
                    self.args.environment_id,
                    block_ms=self.args.block_ms,
                    reclaim_older_than_ms=self.args.reclaim_older_than_ms,
                )
            except anthropic.APIError as e:
                log.warning("poll error: %s; backing off", e)
                time.sleep(5)
                continue

            if item is None:
                # Empty queue returns immediately; back off to avoid ~3 req/s idle.
                time.sleep(self.args.idle_backoff)
                continue
            if item.data.type != "session":
                continue

            try:
                self.dispatch(item)
            except Exception:
                log.exception("dispatch failed session=%s work=%s; will be reclaimed",
                              item.data.id, item.id)

    # ---- output collection -------------------------------------------------

    def collect_outputs(self, session_id: str, host: str, phase: str) -> None:
        """Copy a finished pod's outputs to gs://<outputs>/<session_id>/."""
        tarball = http("GET", f"http://{host}:{self.args.dispatch_port}/outputs", timeout=120)
        prefix = f"{session_id}/"
        self.outputs.blob(prefix + "outputs.tar.gz").upload_from_string(
            tarball, content_type="application/gzip"
        )
        promoted = []
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
            for name in PROMOTED_OUTPUTS:
                try:
                    member = tar.getmember(name)
                except KeyError:
                    continue
                if not member.isfile():
                    continue
                content_type = "text/markdown" if name.endswith(".md") else "application/json"
                self.outputs.blob(prefix + name).upload_from_string(
                    tar.extractfile(member).read(), content_type=content_type
                )
                promoted.append(name)
        # Written last: clients treat status.json as "outputs are complete".
        self.outputs.blob(prefix + "status.json").upload_from_string(
            json.dumps({"phase": phase, "collected_at": now_iso(), "files": promoted}),
            content_type="application/json",
        )
        log.info("session=%s collected outputs (%d bytes, promoted=%s)",
                 session_id, len(tarball), promoted)

    def reap_once(self) -> None:
        """Collect and release every dispatched pod that has finished."""
        for claim in self.list_claims(f"{MANAGED_BY_LABEL}={MANAGED_BY}"):
            meta = claim["metadata"]
            name = meta["name"]
            session_id = (meta.get("labels") or {}).get(SESSION_LABEL)
            annotations = meta.get("annotations") or {}
            host = annotations.get(POD_IP_ANNOTATION)
            if not session_id or not host:
                continue  # still being dispatched
            try:
                status = json.loads(http("GET", f"http://{host}:{self.args.dispatch_port}/status",
                                         timeout=5))
            except (OSError, ValueError) as e:
                dispatched_at = annotations.get(DISPATCHED_AT_ANNOTATION, "")
                age = time.time() - datetime.datetime.fromisoformat(dispatched_at).timestamp() \
                    if dispatched_at else float("inf")
                if age > self.args.unreachable_grace_seconds:
                    log.warning("session=%s claim=%s unreachable (%s); deleting", session_id, name, e)
                    self.delete_claim(name)
                continue

            if status.get("session_id") != session_id:
                # Pod is gone and its IP was reused by another pod.
                log.warning("session=%s claim=%s pod IP now serves %s; deleting",
                            session_id, name, status.get("session_id"))
                self.delete_claim(name)
                continue
            if status.get("phase") in ("done", "failed"):
                try:
                    self.collect_outputs(session_id, host, status["phase"])
                except Exception:
                    log.exception("session=%s output collection failed; will retry", session_id)
                    continue
                self.delete_claim(name)

    def reap_forever(self) -> None:
        """Run reap_once on an interval; never let an error kill the thread."""
        while True:
            try:
                self.reap_once()
            except Exception:
                log.exception("reaper tick failed")
            time.sleep(self.args.reap_interval)


def main():
    """Start the reaper thread and poll the work queue."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
    # One line per poll (~1/s) drowns everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args()
    for flag in ("environment_id", "inputs_bucket", "outputs_bucket"):
        if not getattr(args, flag):
            raise SystemExit(f"--{flag.replace('_', '-')} is not set")

    k8s_config.load_incluster_config()
    dispatcher = Dispatcher(args)
    threading.Thread(target=dispatcher.reap_forever, name="reaper", daemon=True).start()
    dispatcher.poll_forever()


if __name__ == "__main__":
    main()
