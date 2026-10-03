#!/usr/bin/env python3
"""Submit a PDF to the Submit API, follow progress, and download the report.

  python scripts/analyze.py report.pdf [--focus "What are the payment terms?"]

Needs SUBMIT_API_URL (the Cloud Run URL) and a gcloud login with
roles/run.invoker on the service. Standard library only.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def id_token() -> str:
    """Google ID token for calling an IAM-protected Cloud Run service."""
    return subprocess.run(["gcloud", "auth", "print-identity-token"],
                          check=True, capture_output=True, text=True).stdout.strip()


def request(method: str, url: str, token: str, *, body: bytes | None = None,
            content_type: str | None = None, timeout: float = 60):
    """Open an authenticated request; caller reads/closes the response."""
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    return urllib.request.urlopen(
        urllib.request.Request(url, data=body, method=method, headers=headers), timeout=timeout
    )


def multipart(fields: dict[str, str], file_field: str, path: Path) -> tuple[bytes, str]:
    """Encode a multipart/form-data body with one file."""
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{path.name}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n".encode() + path.read_bytes() + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main() -> None:
    """CLI entry point."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pdf", type=Path)
    p.add_argument("--focus", default="")
    p.add_argument("--out", type=Path, default=Path("."), help="directory for report.md / findings.json")
    p.add_argument("--api", default=os.environ.get("SUBMIT_API_URL"))
    args = p.parse_args()
    if not args.api:
        sys.exit("Set SUBMIT_API_URL or pass --api")
    api = args.api.rstrip("/")
    token = id_token()

    body, ctype = multipart({"focus": args.focus}, "file", args.pdf)
    with request("POST", f"{api}/v1/analyses", token, body=body, content_type=ctype) as r:
        analysis = json.load(r)
    aid = analysis["id"]
    print(f"analysis {aid} started")

    # Follow progress. The stream ends when the session goes idle.
    with request("GET", f"{api}/v1/analyses/{aid}/events", token, timeout=3600) as r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev["type"] == "tool":
                print(f"  · {ev['name']}: {ev['detail'][:120]}")
            elif ev["type"] == "evaluation":
                print(f"  ✓ grader iteration {ev['iteration']}: {ev['result']}")
            elif ev["type"] == "error":
                print(f"  ! {ev['message']}")
            elif ev["type"] == "idle":
                print(f"session idle ({ev['reason']})")

    # The dispatcher collects outputs shortly after the worker exits.
    for _ in range(60):
        with request("GET", f"{api}/v1/analyses/{aid}", token) as r:
            state = json.load(r)
        if state["outputs"]:
            break
        time.sleep(5)
    else:
        sys.exit("outputs were not collected in time; check the dispatcher logs")

    args.out.mkdir(parents=True, exist_ok=True)
    for name in state["outputs"]["files"]:
        with request("GET", f"{api}/v1/analyses/{aid}/files/{name}", token) as r:
            (args.out / f"{aid}-{name}").write_bytes(r.read())
        print(f"saved {args.out / f'{aid}-{name}'}")
    print(f"list cost: {state.get('list_cost')}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()[:500]}")
