# PDF Agent on GKE (Claude Managed Agents, self-hosted sandboxes)

Upload a PDF, get back a cited analysis (`report.md` + `findings.json`). Claude
runs on Anthropic's control plane; every tool call (pdftotext, OCR, Python)
runs in a gVisor sandbox pod in your GKE cluster, one pod per session.

Built on Google's [GKE Agent Sandbox sample](https://github.com/GoogleCloudPlatform/kubernetes-engine-samples/tree/main/ai-ml/anthropic-agent-sandbox)
and Anthropic's [self-hosted sandboxes](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes),
hardened for production use (see [What changed from the sample](#what-changed-from-the-sample)).

## Architecture

Full walkthrough, security model, and a self-hosted vs. cloud-sandbox comparison:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```
client ──► Submit API (Cloud Run, IAM)            ── holds API key
             1. PDF → gs://…-inputs/inputs/<uuid>/<name>.pdf
             2. sessions.create(metadata={input_object}, budget, outcome+rubric)
                        │
                        ▼
             Anthropic: agent loop (Claude Opus 5.5) + work queue
                        ▲ poll
GKE Autopilot (private nodes)                     │
  dispatcher ──────────────────────────────────────┘  ── env key + API key, GCS via WI
     3. reads session metadata, downloads that one PDF
     4. claims a warm pod, POSTs session binding + PDF (in-cluster)
     6. when the pod reports done: pulls outputs → gs://…-outputs/<session>/, deletes claim
  worker pod (gVisor) × N                          ── env key only; egress: api.anthropic.com
     5. SDK EnvironmentWorker runs the session's tool calls in /workspace
  stats-adapter ── scales the warm pool on queue depth   ── API key, metric writer
```

### Security model

| Component | Credentials | Network |
|---|---|---|
| Worker (sandbox) | Environment key only (serves one environment's queue). No K8s token, no GCP identity. | Egress to `api.anthropic.com:443` + DNS only. Ingress only from the dispatcher. |
| Dispatcher | Env key, API key (K8s Secrets); GCS read inputs / write outputs (Workload Identity) | Unrestricted egress (trusted) |
| Stats adapter | API key; `monitoring.metricWriter` | Unrestricted egress (trusted) |
| Submit API | API key (Secret Manager); GCS write inputs / read outputs | Cloud Run, IAM-authenticated |

- Sandboxes can't read other sessions' data: they never see GCS, and each pod
  receives only its own PDF.
- A malicious PDF can't exfiltrate to the internet: the only reachable host is
  Anthropic's API. Web search/fetch are disabled on the agent so document text
  doesn't leave via those tools either.
- The agent's prompt treats document content as untrusted; the rubric requires
  embedded instructions to be reported, not followed.
- Every session has a hard spend cap (`SESSION_BUDGET_CENTS`, default $10).
- Pods run as non-root, read-only root FS, all capabilities dropped,
  `restricted` Pod Security Standard enforced on the namespace.
- Secret values never pass through Terraform (no keys in state).

## Prerequisites

- `gcloud` ≥ 565 with the `beta` component, `terraform` ≥ 1.7, `kubectl` ≥ 1.30,
  `kustomize` ≥ 5.8, Python 3.12+
- A GCP project with billing; permissions to create clusters, VPCs, buckets,
  service accounts, Secret Manager secrets, Cloud Run services
- An Anthropic API key with Managed Agents access

## Setup

```bash
cp .env.example .env            # fill in PROJECT_ID, ANTHROPIC_API_KEY, SUBMIT_API_INVOKERS
source .env
python3 -m venv .venv && .venv/bin/pip install anthropic==1.11.0

# 1. Anthropic side (once)
.venv/bin/python setup/anthropic_setup.py environment   # → ANTHROPIC_ENVIRONMENT_ID
#    Console → Environments → this environment → "Generate environment key"
#    → ANTHROPIC_ENVIRONMENT_KEY
.venv/bin/python setup/anthropic_setup.py agent         # → ANTHROPIC_AGENT_ID
#    Put all three in .env, then `source .env` again.

# 2. Google Cloud side
make bootstrap      # Terraform state bucket
make infra          # ~20 min: VPC, NAT, Autopilot + Agent Sandbox, buckets, SAs, registry
make secrets        # adds key values to Secret Manager
make images         # Cloud Build: worker, dispatcher, stats-adapter, submit-api
make deploy         # OVERLAY=01_single_session (one warm pod) by default
make verify-egress  # sandbox can reach api.anthropic.com and nothing else
make api            # Submit API on Cloud Run; prints SUBMIT_API_URL
```

Smoke test:

```bash
export SUBMIT_API_URL=https://pdf-agent-submit-api-….run.app
python3 scripts/analyze.py some.pdf --focus "What are the payment terms?"
```

When it works, switch to autoscaling (warm pool 2–20 by queue depth):

```bash
make deploy OVERLAY=02_autoscale
```

## API

| Method | Path | |
|---|---|---|
| POST | `/v1/analyses` | multipart `file` (PDF ≤ 50 MB), optional `focus`. Returns `{id}` |
| GET | `/v1/analyses/{id}` | status, list cost, outcome evaluations, `outputs` (null until collected) |
| GET | `/v1/analyses/{id}/events` | SSE progress (tool calls, grader iterations, idle) |
| GET | `/v1/analyses/{id}/files/{name}` | `report.md`, `findings.json`, `outputs.tar.gz` |

For browser users, put the service behind IAP; the API records
`X-Goog-Authenticated-User-Email` as `submitted_by` on the session.

## Operations

- `make status` shows the warm pool, active claims, and pods.
- `make verify-egress` checks from inside a sandbox that only `api.anthropic.com`
  is reachable. Run it after every deploy or add-on upgrade.
- Logs: `kubectl -n pdf-agent logs deploy/pdf-agent-dispatcher` and worker pods (JSON lines).
- Each session's trace: `https://platform.claude.com/workspaces/default/sessions/<id>`.
- Tune the agent: edit `setup/anthropic_setup.py`, rerun `… agent` (new version; running sessions keep theirs).
- Tune the rubric / budget: `src/submit-api/main.py` (`RUBRIC`, `SESSION_BUDGET_CENTS`).
- Tests: `pip install -r tests/requirements.txt && pytest tests/`.

### Failure handling

- Dispatch fails → claim deleted, work item re-offered after `RECLAIM_OLDER_THAN_MS`;
  after `MAX_REDISPATCH` attempts it's stopped.
- Worker finishes or fails → reaper collects outputs, writes `status.json`
  (`phase: done|failed`), deletes the claim.
- Pod vanishes → claim deleted after `UNREACHABLE_GRACE_SECONDS`.
- Everything else → claims carry a TTL (`CLAIM_TTL_SECONDS`, 6h) and the
  controller deletes them.

## What changed from the sample

1. **No GCS in sandboxes.** The sample mounts the whole outputs bucket
   read/write into every pod via GCS FUSE (cross-session read/tamper) and allows
   egress to `storage.googleapis.com` and the metadata server. Here the
   dispatcher moves data in and out; sandboxes have no GCP identity.
2. **Input staging** from session metadata, per Anthropic's
   [Stage files for a session](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-workers#stage-files-for-a-session).
3. **Claim lifecycle.** The sample never deletes claims after a session ends;
   here the reaper does, with a TTL backstop.
4. **Secrets** out of Terraform; **remote state** in GCS.
5. **Private nodes** + Cloud NAT, dedicated VPC, minimal node service account,
   dedicated Cloud Build service account.
6. **Egress lockdown that actually holds.** Two Agent Sandbox defaults silently
   undo the sample's FQDN policy: the controller adds its own NetworkPolicy
   allowing the whole public internet (policies are additive), and it points
   pod DNS at 8.8.8.8/1.1.1.1, bypassing cluster DNS that FQDN policies rely
   on. The template sets `networkPolicyManagement: Unmanaged` and
   `dnsPolicy: ClusterFirst`; `make verify-egress` proves the result.
7. **Work items are acked** before hand-off. The sample's raw poll never acked,
   so the SDK worker had no lease and exited without running the session.
8. **Python SDK worker** (`EnvironmentWorker`) instead of the `ant` CLI, so custom
   tools can be added later.
9. **Submit API**, per-session **budget**, **outcome + rubric** kickoff.

## Known limits / next hardening steps

- Secrets reach pods as Kubernetes Secrets (synced by `make k8s-secrets`).
  Autopilot encrypts etcd at rest; for stricter setups use the Secret Manager
  CSI add-on and/or application-layer secrets encryption with Cloud KMS.
- The dispatcher is a single replica (`Recreate`). Restarts are safe (claims and
  pod IPs are recorded on the claim objects), but dispatching pauses while it's down.
- Agent Sandbox and Managed Agents self-hosted sandboxes are both in preview;
  `k8s-agent-sandbox` is pinned to the last `v1alpha1`-compatible release.
- Follow-up messages to a finished session start a fresh pod: the PDF is
  re-staged, but scratch files from the previous run are gone (outputs were
  collected to GCS).
