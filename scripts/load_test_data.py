#!/usr/bin/env python3
"""Load test data into OpenSearch for ops-agent testing."""

import json
import random
import sys
from datetime import datetime, timedelta, timezone

try:
    import httpx as _http
    def _post(url, json_body, verify):
        r = _http.post(url, json=json_body, verify=verify, timeout=10)
        r.raise_for_status()
        return r.json()
    def _put(url, json_body, verify):
        r = _http.put(url, json=json_body, verify=verify, timeout=10)
        r.raise_for_status()
        return r.json()
except ImportError:
    import urllib.request, urllib.error
    def _request(method, url, json_body, verify):
        data = json.dumps(json_body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        import ssl
        ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()
        with urllib.request.urlopen(req, context=ctx) as resp:
            return json.loads(resp.read())
    def _post(url, json_body, verify):
        return _request("POST", url, json_body, verify)
    def _put(url, json_body, verify):
        return _request("PUT", url, json_body, verify)


OPENSEARCH_URL = "https://opensearch.talos.sleepfox.ru"
VERIFY_TLS = False  # set to True if using valid cert

INDEX = "ops-agent-test"

SERVICES = ["api-gateway", "auth-service", "billing", "notifications", "worker"]
LEVELS = ["INFO", "INFO", "INFO", "WARN", "ERROR"]
NAMESPACES = ["production", "staging", "production", "production", "staging"]
PODS = [f"{svc}-{suffix}" for svc in SERVICES for suffix in ["abc12", "def34", "xyz89"]]

MESSAGES = {
    "INFO": [
        "Request processed successfully",
        "User authenticated",
        "Cache hit for key {key}",
        "Health check passed",
        "Scheduled job completed in {ms}ms",
        "Connected to database",
        "Metrics exported",
    ],
    "WARN": [
        "High memory usage: {pct}%",
        "Slow query detected: {ms}ms",
        "Retry attempt {n} for downstream call",
        "Rate limit approaching for client {id}",
        "Disk usage at {pct}%",
    ],
    "ERROR": [
        "Failed to connect to database: timeout",
        "Unhandled exception in request handler",
        "Circuit breaker OPEN for service {svc}",
        "OOMKilled detected in pod {pod}",
        "HTTP 500 from upstream {svc}",
    ],
}


def random_message(level: str) -> str:
    tpl = random.choice(MESSAGES[level])
    return tpl.format(
        key=f"user:{random.randint(1000, 9999)}",
        ms=random.randint(50, 5000),
        pct=random.randint(60, 99),
        n=random.randint(1, 5),
        id=random.randint(100, 999),
        svc=random.choice(SERVICES),
        pod=random.choice(PODS),
    )


def generate_docs(count: int = 200) -> list[dict]:
    now = datetime.now(timezone.utc)
    docs = []
    for i in range(count):
        ts = now - timedelta(minutes=count - i, seconds=random.randint(0, 59))
        level = random.choices(LEVELS, weights=[50, 30, 20, 10, 5])[0]
        service = random.choice(SERVICES)
        pod = random.choice([p for p in PODS if p.startswith(service)])
        docs.append({
            "@timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "level": level,
            "service": service,
            "namespace": random.choice(NAMESPACES),
            "pod": pod,
            "message": random_message(level),
            "response_time_ms": random.randint(10, 5000) if level != "ERROR" else None,
            "status_code": random.choice([200, 200, 200, 201, 400, 404, 500])
                if level != "INFO" else random.choice([200, 200, 201]),
            "request_id": f"req-{random.randint(100000, 999999)}",
        })
    return docs


def create_index() -> None:
    mapping = {
        "mappings": {
            "properties": {
                "@timestamp": {"type": "date"},
                "level": {"type": "keyword"},
                "service": {"type": "keyword"},
                "namespace": {"type": "keyword"},
                "pod": {"type": "keyword"},
                "message": {"type": "text"},
                "response_time_ms": {"type": "integer"},
                "status_code": {"type": "integer"},
                "request_id": {"type": "keyword"},
            }
        }
    }
    try:
        result = _put(f"{OPENSEARCH_URL}/{INDEX}", mapping, VERIFY_TLS)
        print(f"Index created: {result.get('acknowledged', result)}")
    except Exception as e:
        if "resource_already_exists" in str(e):
            print(f"Index '{INDEX}' already exists, continuing.")
        else:
            raise


def bulk_index(docs: list[dict]) -> None:
    lines = []
    for doc in docs:
        lines.append(json.dumps({"index": {"_index": INDEX}}))
        lines.append(json.dumps(doc))
    body = "\n".join(lines) + "\n"

    try:
        import httpx
        resp = httpx.post(
            f"{OPENSEARCH_URL}/_bulk",
            content=body,
            headers={"Content-Type": "application/x-ndjson"},
            verify=VERIFY_TLS,
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
    except ImportError:
        import urllib.request, urllib.error, ssl
        data = body.encode()
        req = urllib.request.Request(f"{OPENSEARCH_URL}/_bulk", data=data, method="POST")
        req.add_header("Content-Type", "application/x-ndjson")
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, context=ctx) as r:
            result = json.loads(r.read())

    errors = [i for i in result.get("items", []) if "error" in i.get("index", {})]
    if errors:
        print(f"Bulk errors: {len(errors)}", file=sys.stderr)
    else:
        print(f"Indexed {len(docs)} documents, errors: 0")


def main():
    print(f"Target: {OPENSEARCH_URL}/{INDEX}")

    count = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    print(f"Generating {count} documents...")

    create_index()
    docs = generate_docs(count)
    bulk_index(docs)
    print("Done.")


if __name__ == "__main__":
    main()
