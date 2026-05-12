from kubernetes import client, config


def build_k8s_clients(
    mode: str,
    kubeconfig_path: str | None = None,
) -> tuple[client.CoreV1Api, client.AppsV1Api]:
    if mode == "incluster":
        config.load_incluster_config()
    else:
        config.load_kube_config(config_file=kubeconfig_path)
    return client.CoreV1Api(), client.AppsV1Api()
