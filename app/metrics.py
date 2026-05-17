from prometheus_client import Counter, Gauge, Histogram

message_duration = Histogram(
    "ops_agent_message_duration_seconds",
    "End-to-end message processing time",
    buckets=[1, 3, 10, 30, 60, 120],
)
messages_total = Counter(
    "ops_agent_messages_total",
    "Messages processed by outcome",
    ["outcome"],
)
pod_startup_seconds = Histogram(
    "ops_agent_pod_startup_seconds",
    "Pod readiness wait time",
    buckets=[1, 2, 5, 10, 20, 30, 60],
)
tool_calls_total = Counter(
    "ops_agent_tool_calls_total",
    "Tool invocations by tool name",
    ["tool"],
)
approvals_total = Counter(
    "ops_agent_approvals_total",
    "Approval decisions by outcome",
    ["decision"],
)
approval_wait_seconds = Histogram(
    "ops_agent_approval_wait_seconds",
    "Time from approval request to user decision",
    buckets=[5, 10, 30, 60, 120],
)
active_clients = Gauge(
    "ops_agent_active_clients",
    "Messages currently being processed",
)
active_pods = Gauge(
    "ops_agent_active_pods",
    "Deployments with replicas > 0",
)
pods_reaped_total = Counter(
    "ops_agent_pods_reaped_total",
    "Pods scaled down by idle reaper",
)
k8s_errors_total = Counter(
    "ops_agent_k8s_errors_total",
    "Kubernetes API errors by operation",
    ["op"],
)
tokens_total = Counter(
    "ops_agent_tokens_total",
    "LLM tokens consumed by kind and model",
    ["kind", "model"],
)

# Label values for k8s_errors_total
K8S_OP_UPDATE_LAST_ACTIVITY = "update_last_activity"
K8S_OP_LIST_IDLE = "list_idle"
K8S_OP_SCALE_DOWN = "scale_down"
