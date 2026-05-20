# relay — local development with kind
#
# Prerequisites:
#   brew install tilt kind
#   kind create cluster --name relay   (context becomes kind-relay)
#   cp .env.example .env && $EDITOR .env
#
# macOS: point MATTERMOST_URL at the docker-compose Mattermost:
#   MATTERMOST_URL=http://host.docker.internal
#   MATTERMOST_PORT=8065
#
# Linux: replace host.docker.internal with the Docker bridge gateway IP
#   (run: docker network inspect bridge | grep Gateway)
#
# Start everything:
#   tilt up
#
# Tear down (keeps kind cluster):
#   tilt down

# ── Safety: refuse to run against non-kind clusters ──────────────────────────
allow_k8s_contexts(["kind-kind", "kind-relay"])

load("ext://restart_process", "docker_build_with_restart")

IMAGE     = "relay:dev"
NAMESPACE = "sandbox"

# ── ZeroClaw image (base + gh CLI) ───────────────────────────────────────────
# Zeroclaw pods are created dynamically by runtime.py, so Tilt can't track the
# image via docker_build. Instead we build + kind-load explicitly.
# Set ZEROCLAW_IMAGE=zeroclaw-custom:dev in .env to use this locally.
local_resource(
    "zeroclaw-image",
    cmd = """
CLUSTER=$(kubectl config current-context | sed 's/^kind-//') && \
docker build -t zeroclaw-custom:dev -f Dockerfile.zeroclaw . && \
kind load docker-image zeroclaw-custom:dev --name "$CLUSTER"
""",
    deps   = ["Dockerfile.zeroclaw"],
    labels = ["infra"],
)

# ── Image ─────────────────────────────────────────────────────────────────────
# Syncs ./app into the running container and restarts the Python process
# without a full image rebuild on every code change.
docker_build_with_restart(
    IMAGE,
    context    = ".",
    dockerfile = "Dockerfile",
    entrypoint = ["python", "-m", "app.main"],
    only       = ["./app", "pyproject.toml", "uv.lock"],
    live_update = [
        sync("./app", "/app/app"),
    ],
)

# ── Parse .env → K8s Secret ───────────────────────────────────────────────────
def _env_secret(path, name, namespace):
    src = str(read_file(path, default = ""))
    if not src:
        fail("'%s' not found — cp .env.example .env and fill in values" % path)
    pairs = []
    for line in src.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().replace("\\", "\\\\").replace('"', '\\"')
        if k:
            pairs.append('  %s: "%s"' % (k, v))
    return "\n".join([
        "apiVersion: v1",
        "kind: Secret",
        "metadata:",
        "  name: " + name,
        "  namespace: " + namespace,
        "type: Opaque",
        "stringData:",
    ] + pairs + [""])

# ── Namespaces ────────────────────────────────────────────────────────────────
# local_resource with cmd (not serve_cmd) runs once and is never torn down by
# `tilt down`, so namespaces survive across Tilt restarts.
local_resource(
    "namespaces",
    cmd = "kubectl apply -f deploy/namespace.yaml",
    labels = ["infra"],
)

# ── RBAC + Secret ─────────────────────────────────────────────────────────────
k8s_yaml("deploy/rbac.yaml")
k8s_yaml(blob(_env_secret(".env", "relay-controller", NAMESPACE)))

# ── Network policy ────────────────────────────────────────────────────────────
k8s_yaml("deploy/network-policy.yaml")

# ── Service (webhook) ────────────────────────────────────────────────────────
k8s_yaml("deploy/service.yaml")

# ── Dev Deployment (1 replica, local image) ───────────────────────────────────
k8s_yaml(blob("""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: relay-controller
  namespace: sandbox
spec:
  replicas: 1
  selector:
    matchLabels:
      app: relay-controller
  template:
    metadata:
      labels:
        app: relay-controller
    spec:
      serviceAccountName: relay-controller
      containers:
        - name: controller
          image: relay:dev
          imagePullPolicy: Never
          envFrom:
            - secretRef:
                name: relay-controller
          ports:
            - containerPort: 8080
              name: health
            - containerPort: 8579
              name: webhook
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 15
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /readyz
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests:
              cpu: "250m"
              memory: "256Mi"
            limits:
              cpu: "1"
              memory: "1Gi"
"""))

# ── Port forward + dependency ordering ───────────────────────────────────────
k8s_resource(
    "relay-controller",
    port_forwards  = ["8080:8080", "0.0.0.0:8579:8579"],
    resource_deps  = ["namespaces", "mattermost"],
    labels         = ["app"],
)

# ── Cleanup: delete per-user zeroclaw resources on tilt down ─────────────────
# runtime.py creates Deployments/Services/PVCs/ConfigMap at request time; Tilt
# never sees them via k8s_yaml, so they must be deleted explicitly on teardown.
# Tilt sends SIGTERM to serve_cmd processes during `tilt down` — the trap fires.
local_resource(
    "zeroclaw-runtime-cleanup",
    serve_cmd = """
trap '
  kubectl delete deploy,svc,pvc -n sandbox \
    -l ai.relay.io/part-of=zeroclaw-runtime --ignore-not-found
  kubectl delete configmap zeroclaw-config -n sandbox --ignore-not-found
  exit 0
' TERM INT
while true; do sleep 86400 & wait $!; done
""",
    resource_deps = ["namespaces"],
    labels        = ["infra"],
)

# ── Infra: Mattermost + Postgres via docker-compose ──────────────────────────
local_resource(
    "mattermost",
    serve_cmd = "docker compose up postgres mattermost",
    readiness_probe = probe(
        period_secs = 15,
        http_get    = http_get_action(port = 8065, host = "localhost", path = "/api/v4/system/ping"),
    ),
    labels = ["infra"],
)
