#!/usr/bin/env python3
"""
Patches the CloudWatch cluster scraper to scrape annotated pods and forward
custom Prometheus metrics to the CloudWatch PromQL store.

The AmazonCloudWatchAgent CRD controls the ConfigMap — direct ConfigMap edits
are reconciled away. This script edits the CR directly.

Usage:
  python3 patch-cluster-scraper.py          # apply
  python3 patch-cluster-scraper.py --revert # remove the app_pods pipeline
"""

import json
import subprocess
import sys

try:
    import yaml
except ImportError:
    print("pyyaml required: pip3 install pyyaml --break-system-packages")
    sys.exit(1)

RECEIVER_KEY = "prometheus/app_pods"
PIPELINE_KEY = "metrics/app_pods"


def get_cr():
    result = subprocess.run(
        ["kubectl", "get", "amazoncloudwatchagent", "cloudwatch-agent-cluster-scraper",
         "-n", "amazon-cloudwatch", "-o", "json"],
        capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def replace_cr(cr):
    with open("/tmp/cw-cluster-scraper-patch.json", "w") as f:
        json.dump(cr, f)
    subprocess.run(["kubectl", "replace", "-f", "/tmp/cw-cluster-scraper-patch.json"], check=True)
    subprocess.run(
        ["kubectl", "rollout", "restart", "deployment/cloudwatch-agent-cluster-scraper",
         "-n", "amazon-cloudwatch"],
        check=True
    )
    print("Cluster scraper patched and restarted.")


def apply():
    cr = get_cr()
    cfg = yaml.safe_load(cr["spec"]["otelConfig"])

    if RECEIVER_KEY in cfg.get("receivers", {}):
        print(f"{RECEIVER_KEY} already present — nothing to do.")
        return

    # Add Prometheus pod discovery receiver.
    #
    # Note: OTel confmap (CWAgent 1.300067) treats $N in YAML values as env var
    # references and does not support $$ escaping. To build pod_ip:port without
    # using $1:$2 in `replacement`, use separator=':' so the receiver joins the
    # two source labels with ':' directly.
    cfg["receivers"][RECEIVER_KEY] = {
        "config": {
            "scrape_configs": [{
                "job_name": "app-pods",
                "scrape_interval": "30s",
                "kubernetes_sd_configs": [{"role": "pod"}],
                "relabel_configs": [
                    {
                        "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_scrape"],
                        "action": "keep",
                        "regex": "true",
                    },
                    {
                        "source_labels": [
                            "__meta_kubernetes_pod_ip",
                            "__meta_kubernetes_pod_annotation_prometheus_io_port",
                        ],
                        "separator": ":",
                        "target_label": "__address__",
                        "action": "replace",
                    },
                    {
                        "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_path"],
                        "target_label": "__metrics_path__",
                        "action": "replace",
                    },
                    {
                        "source_labels": ["__meta_kubernetes_namespace"],
                        "target_label": "namespace",
                        "action": "replace",
                    },
                    {
                        "source_labels": ["__meta_kubernetes_pod_name"],
                        "target_label": "pod",
                        "action": "replace",
                    },
                ],
            }]
        }
    }

    cfg["service"]["pipelines"][PIPELINE_KEY] = {
        "receivers": [RECEIVER_KEY],
        "processors": [
            "filter/cw_k8s_ci_v0_scrape_metadata",
            "metricstarttime/cw_k8s_ci_v0",
            "transform/cw_k8s_ci_v0_set_cluster_name",
            "k8sattributes/cw_k8s_ci_v0_pod",
            "resourcedetection/cw_k8s_ci_v0",
            "transform/cw_k8s_ci_v0_clear_schema_url",
            "transform/cw_k8s_ci_v0_set_cloud_resource_id",
            "awsattributelimit/cw_k8s_ci_v0",
            "batch/cw_k8s_ci_v0_cwotel",
        ],
        "exporters": ["otlphttp/cw_k8s_ci_v0_cwotel"],
    }

    cr["spec"]["otelConfig"] = yaml.dump(cfg, default_flow_style=False, allow_unicode=True)
    replace_cr(cr)


def revert():
    cr = get_cr()
    cfg = yaml.safe_load(cr["spec"]["otelConfig"])

    changed = False
    if RECEIVER_KEY in cfg.get("receivers", {}):
        del cfg["receivers"][RECEIVER_KEY]
        changed = True
    if PIPELINE_KEY in cfg.get("service", {}).get("pipelines", {}):
        del cfg["service"]["pipelines"][PIPELINE_KEY]
        changed = True

    if not changed:
        print("Nothing to revert.")
        return

    cr["spec"]["otelConfig"] = yaml.dump(cfg, default_flow_style=False, allow_unicode=True)
    replace_cr(cr)
    print("app_pods pipeline removed.")


if __name__ == "__main__":
    if "--revert" in sys.argv:
        revert()
    else:
        apply()
