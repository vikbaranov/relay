# Security Audit

Audit date: 2026-05-17  
Branch: `stable`

---

## Critical

### 1. API key written to ConfigMap in plaintext
**File**: `app/k8s/runtime.py:139-155`

`openai_api_key` is embedded in a TOML string and stored in a Kubernetes ConfigMap. Any pod or principal with ConfigMap read access can retrieve it.

**Fix**: Store the TOML config in a `V1Secret` instead:
```python
body = client.V1Secret(
    metadata=client.V1ObjectMeta(name=s.zeroclaw_secret, namespace=self._ns),
    string_data={"config.toml": toml},
)
```

---

### 2. Approval webhook has no signature or origin verification (TOCTOU)
**File**: `app/bot/plugin.py:128-149`

The `handle_approval` handler trusts `request_id` from the HTTP payload without cryptographic verification. Any actor that can POST to the webhook endpoint can approve or deny arbitrary pending tool requests.

**Fix**: Sign each approval request with HMAC and verify on receipt:
```python
expected = hmac.new(self._secret, request_id.encode(), hashlib.sha256).hexdigest()
if not hmac.compare_digest(context.get("signature", ""), expected):
    self.driver.respond_to_web(event, {"error": "Invalid signature"})
    return
```
Also use UUID4 for request IDs and consider IP-restricting the webhook endpoint.

---

### 3. SSL verification defaults to `False`
**File**: `app/config.py:51`

```python
ssl_verify: bool = False
```

Disables TLS certificate verification by default, allowing man-in-the-middle attacks against Mattermost and any external service.

**Fix**: Default to `True`; override only in development:
```python
ssl_verify: bool = True
```

---

## High

### 4. `_pending_approvals` dict accessed without a lock
**File**: `app/bot/plugin.py:99-126`

Multiple threads read and write `_pending_approvals` concurrently with no synchronisation primitive. This is a race condition that can corrupt approval state or allow a double-approval.

**Fix**: Protect all access with `threading.RLock`.

---

### 5. No input validation on user messages (prompt injection)
**File**: `app/bot/plugin.py:232`

`message.text` is forwarded to ZeroClaw without length checks or content validation. An attacker can craft prompt injection payloads to escalate privileges or leak context.

**Fix**: Enforce a maximum message length and validate structure before forwarding.

---

### 6. Namespace pod security policy set to `privileged`
**File**: `deploy/namespace.yaml:6`

```yaml
pod-security.kubernetes.io/enforce: privileged
```

Permits any pod in the namespace to run with full host privileges, enabling container escape.

**Fix**: Use `restricted` or at minimum `baseline`:
```yaml
pod-security.kubernetes.io/enforce: restricted
pod-security.kubernetes.io/audit: restricted
pod-security.kubernetes.io/warn: restricted
```

---

### 7. Webhook server binds to `0.0.0.0` with no authentication
**File**: `app/bot/runner.py:41`

The approval webhook accepts connections on all interfaces. Combined with the missing signature check (issue #2), this exposes the approval endpoint to the network.

**Fix**: Bind to `localhost` or the pod's internal IP only.

---

### 8. HMAC for K8s object names truncated to 80 bits
**File**: `app/identity.py:5-6`

```python
return hmac.new(secret, mm_user_id.encode(), hashlib.sha256).hexdigest()[:20]
```

Truncation reduces the collision space. If the `k8s_name_secret` is compromised, all user identities become enumerable.

**Fix**: Keep full HMAC output where DNS length allows; otherwise document the truncation risk explicitly.

---

### 9. Partial user IDs logged in runtime logs
**File**: `app/k8s/runtime.py:334`

```python
logger.info("created Deployment %s for user %s", name, mm_user_id[:8] + "…")
```

Partial IDs in exported logs enable user activity correlation.

**Fix**: Log only an opaque hash of the user ID:
```python
user_hash = hashlib.sha256(mm_user_id.encode()).hexdigest()[:12]
logger.info("created Deployment %s for user %s", name, user_hash)
```

---

### 10. RBAC grants unrestricted verbs with no resourceName scope
**File**: `deploy/rbac.yaml:14-19`

The service account can create, patch, and delete any Deployment or Pod in the namespace without restriction. A compromised controller pod could delete other users' runtimes.

**Fix**: Restrict with `resourceNames` matching the `zc-*` naming pattern and apply least-privilege verbs per resource type.

---

## Medium

### 11. `zeroclaw:latest` image tag is unpinned
**File**: `app/config.py:19`

Unpinned `latest` tag introduces silent supply chain risk on pod restarts.

**Fix**: Pin to a specific version tag or image digest.

---

### 12. `json.loads()` without error handling in WS client
**File**: `app/zeroclaw/client.py:27`

A malformed JSON frame from a compromised ZeroClaw pod raises an unhandled `JSONDecodeError` and crashes the bot.

**Fix**: Wrap in `try/except json.JSONDecodeError` and log then skip the frame.

---

### 13. Direct dict key access on WS frames
**File**: `app/bot/plugin.py:39-40`

```python
request_id = frame["request_id"]
tool = frame["tool"]
```

Missing keys raise `KeyError` and crash the handler thread.

**Fix**: Use `.get()` with validation before proceeding.

---

### 14. Session IDs are plain and enumerable
**File**: `app/identity.py:18-20`

```python
return f"mm-{mm_user_id}-{thread_id}"
```

Session IDs are predictable from public Mattermost identifiers.

**Fix**: Derive from HMAC using a server-side secret.

---

### 15. No per-user rate limiting
**File**: `app/bot/plugin.py:184,210`

Any authenticated Mattermost user can create unlimited pods and WebSocket sessions, enabling resource exhaustion.

**Fix**: Enforce a per-user request quota (e.g., 10 requests/minute).

---

### 16. Dependency versions have no upper bounds
**File**: `pyproject.toml`

`>=` constraints without upper bounds allow transitive major-version bumps that may introduce breaking changes or vulnerabilities.

**Fix**: Add compatible-release upper bounds (e.g., `mmpy-bot>=2.2.1,<3.0`).

---

### 17. Inter-pod communication uses plain HTTP/WS
**File**: `app/k8s/runtime.py:45,213-214`

Health checks and chat WebSocket connections use `http://` and `ws://`. In shared or untrusted cluster environments, traffic is visible in plaintext.

**Fix**: Use `https://` and `wss://` with internal certificates where possible.

---

### 18. Controller pod has unrestricted egress
**File**: `deploy/network-policy.yaml:35-48`

```yaml
egress:
  - {}
```

The controller can reach any endpoint in the cluster or internet, enabling lateral movement if compromised.

**Fix**: Restrict egress to the Mattermost service, LLM API, and the sandbox namespace only.

---

## Low

### 19. Debug logs include full frame data
**File**: `app/zeroclaw/client.py:30`

Tool arguments and approval request contents are emitted at `DEBUG` level, leaking sensitive data if debug logging is enabled in production.

**Fix**: Sanitize or redact sensitive fields before logging frames.

---

### 20. Container images are not digest-pinned or scanned
**File**: `Dockerfile`

No SHA256 digest pinning or CI image scanning. External images (especially `ghcr.io/zeroclaw-labs/zeroclaw`) are trusted without verification.

**Fix**: Pin base images with `@sha256:...` digests and add a scanning step (Trivy, Grype) to CI.

---

### 21. No CSRF protection on webhook endpoints
**File**: `app/bot/plugin.py:128`

Webhook endpoints lack origin or CSRF token validation. Mitigated if the webhook is not web-accessible, but the approval signature check (issue #2) should be implemented regardless.

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 3 |
| High | 7 |
| Medium | 8 |
| Low | 3 |
| **Total** | **21** |

### Immediate actions (before any production use)

1. Move `openai_api_key` into a K8s Secret
2. Add HMAC signature verification to the approval webhook
3. Change `ssl_verify` default to `True`
4. Add `threading.RLock` around `_pending_approvals`
5. Change namespace PSP from `privileged` to `restricted`

### Before first deployment

6. Add input validation and length checks on user messages
7. Tighten RBAC with `resourceNames` and least-privilege verbs
8. Restrict webhook listener to internal interface
9. Add per-user rate limiting
10. Fix bare dict key access on WS frames

### Next release

11. Pin all dependency upper bounds
12. Use WSS/HTTPS for ZeroClaw inter-pod traffic
13. Tighten network egress policy
14. Strengthen session ID generation
15. Sanitize runtime logs
