# ops-agent

A Kubernetes-native controller that connects Mattermost chat to [ZeroClaw](https://github.com/zeroclaw-labs/zeroclaw) AI agent sandboxes. Each user gets an isolated pod with persistent storage; the controller handles pod lifecycle, message streaming, and approval workflows.

---

## Architecture

```
Mattermost (Chat)
      │
      ▼
ops-agent controller pod
  ├─ ZeroClawPlugin       ← message handling, approval buttons
  ├─ RuntimeManager       ← per-user K8s resource lifecycle
  ├─ IdleReaper           ← background thread, scales down idle pods
  └─ Health/Metrics HTTP  ← /healthz, /readyz, /metrics
      │
      ▼  (WebSocket per message)
Per-user ZeroClaw pods (one Deployment + Service + PVC per Mattermost user)
```

The controller never stores state in a database. All state lives in Kubernetes objects: Deployments, Services, PVCs, and ConfigMaps.

---

## Request Flow

1. User posts a message in Mattermost.
2. `ZeroClawPlugin.handle_message()` calls `RuntimeManager.ensure_runtime()` which creates (or re-enables) the user's Deployment, Service, and PVC.
3. The controller posts a placeholder reply with a cursor (`▌`) and waits for the pod's `/health` endpoint to return 200.
4. A WebSocket is opened to the ZeroClaw pod with a Mattermost conversation scope-derived session id: `ws://{service-dns}:{port}/ws/chat?session_id=mm-{scope_hash}`.
5. The user's message is sent; frames stream back and are rendered into the Mattermost post in real time (~1 update/second).
6. When the stream ends (`done` frame), the final message replaces the placeholder.
7. After `IDLE_TIMEOUT_SECONDS` of inactivity the `IdleReaper` scales the pod to 0 replicas (PVC is kept, so resumption is fast).

### Frame types

| Frame | Effect |
|---|---|
| `chunk` | Appended to the reply with a trailing cursor |
| `tool_call` | Tool name + icon prepended to the reply |
| `tool_result` | Tool output appended |
| `approval_request` | Mattermost message with Approve/Deny buttons posted; stream blocks until user clicks or timeout |
| `done` | Stream ends, cursor removed |
| `error` | Error message posted |

---

## Approval Workflow

When ZeroClaw requests approval before executing a tool:

1. Controller posts an approval message with **Approve** / **Deny** buttons.
2. `_request_approval()` blocks on a `threading.Event` (up to `timeout_secs` from the frame, default 120 s).
3. User clicks a button → Mattermost fires a webhook to `{WEBHOOK_PUBLIC_URL}/hooks/approval`.
4. `handle_approval()` records the decision and signals the event.
5. Controller sends `{"type": "approval_response", "decision": "approve" | "deny"}` to ZeroClaw.

---

## Key Design Decisions

**Per-user isolated pods** — each Mattermost user maps to exactly one Deployment + Service + PVC. Cross-user data leakage is structurally impossible.

**HMAC-derived K8s names** — object names are `zc-{hmac(K8S_NAME_SECRET, mm_user_id)[:20]}`. Names are deterministic (survive restarts), non-reversible without the secret, and DNS-safe.

**Lazy creation, idle scale-down** — pods start on first message, scale to 0 after idle timeout. PVC and Service are kept, so the next message only waits for a new pod to start (~30–60 s cold, instant warm).

**Network isolation** — sandbox pods are restricted by NetworkPolicy to DNS (53) and HTTP/S (443/80) egress only; ingress is controller-only.

**ConfigMap-based ZeroClaw config** — LLM endpoint, model, and API key are written into a shared ConfigMap and mounted into every ZeroClaw pod as a TOML file. Changing the model requires only a ConfigMap update.

---

## Project Layout

```
app/
  main.py              # entry point → run_bot(get_settings())
  config.py            # Settings (Pydantic BaseSettings, reads env/.env)
  identity.py          # HMAC naming helpers: object_name, pvc_name, session_id
  metrics.py           # Prometheus metrics definitions
  logging.py           # JSON log formatter
  health.py            # HTTP server on :8080 (/healthz /readyz /metrics)
  bot/
    runner.py          # wires Settings → K8s clients → plugin → mmpy_bot.Bot
    plugin.py          # ZeroClawPlugin: handle_message, handle_approval
    stream_handler.py  # StreamHandler: frame → Mattermost post text
    formatting.py      # cursor char, update interval, tool icons, truncation
  k8s/
    client.py          # build_k8s_clients() — incluster or kubeconfig mode
    runtime.py         # RuntimeManager — ensure/scale/reap per-user resources
    reaper.py          # IdleReaper daemon thread
  zeroclaw/
    client.py          # chat_stream() — sync wrapper over async WebSocket

deploy/                # Production K8s manifests
  namespace.yaml
  rbac.yaml            # ServiceAccount + Role + RoleBinding
  deployment.yaml      # ops-agent-controller Deployment
  service.yaml         # ClusterIP on 8080 + 8579
  network-policy.yaml
  ingress.yaml         # nginx: /hooks/ → :8579
  secret.example.yaml

tests/
  test_identity.py
  test_runtime.py      # mocked K8s API
  test_plugin.py       # mocked WebSocket + Mattermost driver
```

---

## Configuration

All configuration is via environment variables (or a `.env` file). Copy `.env.example` and fill in required values.

### Mattermost

| Variable | Required | Description |
|---|---|---|
| `MATTERMOST_URL` | yes | Base URL, e.g. `https://chat.example.com` |
| `MATTERMOST_PORT` | yes | Port (usually 443 or 8065) |
| `MATTERMOST_TEAM` | yes | Team name |
| `MATTERMOST_BOT_TOKEN` | yes | Bot account token from Mattermost admin |
| `MATTERMOST_BOT_USERNAME` | yes | Bot username |
| `MATTERMOST_THREAD_REPLIES` | no | `true` matches official Mattermost channel semantics: each top-level post starts a thread-scoped context; `false` scopes context to channel + user |

### Kubernetes

| Variable | Required | Default | Description |
|---|---|---|---|
| `K8S_NAMESPACE` | no | `sandbox` | Namespace where user pods run |
| `K8S_MODE` | no | `incluster` | `incluster` (ServiceAccount) or `kubeconfig` |
| `K8S_KUBECONFIG_PATH` | no | — | Path to kubeconfig when `K8S_MODE=kubeconfig` |
| `K8S_NAME_SECRET` | yes | — | 32-byte hex secret for HMAC pod naming |

### LLM

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | yes | API key forwarded to every ZeroClaw pod |
| `OPENAI_BASE_URL` | yes | OpenAI-compatible endpoint |
| `OPENAI_MODEL` | yes | Model name, e.g. `gpt-4o-mini` |

### ZeroClaw Sandbox Pods

| Variable | Default | Description |
|---|---|---|
| `ZEROCLAW_IMAGE` | `ghcr.io/zeroclaw-labs/zeroclaw:latest` | Container image |
| `ZEROCLAW_PORT` | `42617` | WebSocket port inside the pod |
| `ZEROCLAW_CPU_REQUEST` | `500m` | CPU request |
| `ZEROCLAW_CPU_LIMIT` | `2` | CPU limit |
| `ZEROCLAW_MEMORY_REQUEST` | `1Gi` | Memory request |
| `ZEROCLAW_MEMORY_LIMIT` | `4Gi` | Memory limit |
| `ZEROCLAW_CONFIGMAP` | — | Name of the shared config ConfigMap |

### Storage

| Variable | Default | Description |
|---|---|---|
| `USER_PVC_SIZE` | `5Gi` | PVC size per user |
| `USER_PVC_STORAGE_CLASS` | — | Storage class (empty = cluster default) |

### Lifecycle & Webhooks

| Variable | Default | Description |
|---|---|---|
| `IDLE_TIMEOUT_SECONDS` | `3600` | Idle time before pod is scaled to 0 |
| `POD_READY_TIMEOUT_SECONDS` | `120` | Max wait for a pod to become healthy |
| `REAPER_INTERVAL_SECONDS` | `60` | How often idle check runs |
| `WEBHOOK_HOST_PORT` | `8579` | Port the approval webhook server listens on |
| `WEBHOOK_PUBLIC_URL` | — | URL Mattermost uses to deliver button clicks |
| `LOG_LEVEL` | `20` | Python logging level (20 = INFO) |

---

## Running Locally

### docker-compose (no Kubernetes)

```bash
cp .env.example .env
# edit .env: set Mattermost and OpenAI credentials
docker compose up
```

Mattermost starts on `http://localhost:8065`. ops-agent connects to it directly. ZeroClaw pods require a real K8s cluster, so this mode is best for iterating on the bot logic itself.

### Tilt + kind (full stack)

```bash
brew install tilt kind
kind create cluster --name ops-agent
cp .env.example .env
# edit .env
tilt up
```

Tilt builds the image, parses `.env` into a K8s Secret, and deploys the controller and Mattermost. `tilt down` tears everything down and deletes ZeroClaw runtime resources.

---

## Production Deployment

```bash
# 1. Create namespace and RBAC
kubectl apply -f deploy/namespace.yaml
kubectl apply -f deploy/rbac.yaml

# 2. Create secret from .env
kubectl create secret generic ops-agent-controller -n sandbox \
  --from-env-file=.env --dry-run=client -o yaml | kubectl apply -f -

# 3. Apply remaining manifests
kubectl apply -f deploy/network-policy.yaml
kubectl apply -f deploy/service.yaml
kubectl apply -f deploy/deployment.yaml
kubectl apply -f deploy/ingress.yaml   # requires nginx ingress controller

# 4. Verify
kubectl rollout status -n sandbox deployment/ops-agent-controller
curl https://your-domain/hooks/healthz   # or port-forward :8080
```

The `deploy/ingress.yaml` routes `/hooks/` to the webhook port (8579) so Mattermost can deliver button-click events.

---

## Health & Metrics

The controller exposes an HTTP server on port **8080**:

| Path | Description |
|---|---|
| `/healthz` | Always 200 while the process is running |
| `/readyz` | 200 after bot initialization, 503 before |
| `/metrics` | Prometheus metrics |

### Prometheus metrics

| Metric | Type | Description |
|---|---|---|
| `ops_agent_message_duration_seconds` | Histogram | End-to-end message processing time |
| `ops_agent_messages_total` | Counter | Messages by outcome: success / timeout / error |
| `ops_agent_pod_startup_seconds` | Histogram | Time from pod creation to first healthy response |
| `ops_agent_tool_calls_total` | Counter | Tool invocations by tool name |
| `ops_agent_approvals_total` | Counter | Approval decisions: approved / denied / timeout |
| `ops_agent_approval_wait_seconds` | Histogram | Time from approval request to user click |
| `ops_agent_active_clients` | Gauge | Messages currently in flight |
| `ops_agent_active_pods` | Gauge | Deployments with replicas > 0 |
| `ops_agent_pods_reaped_total` | Counter | Pods scaled down by idle reaper |
| `ops_agent_k8s_errors_total` | Counter | K8s API errors by operation |

---

## Tests

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests mock the Kubernetes API client and the Mattermost driver; no cluster required.

---

## RBAC Requirements

The controller's ServiceAccount needs the following permissions in `K8S_NAMESPACE`:

| API group | Resources | Verbs |
|---|---|---|
| core | pods, services, persistentvolumeclaims, configmaps, events | get, list, watch, create, update, patch, delete |
| apps | deployments | get, list, watch, create, update, patch, delete |

The manifests in `deploy/rbac.yaml` configure this automatically.
