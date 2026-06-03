# EKS OpenTelemetry Monitoring Stack Setup Guide

## Overview

Full observability stack on Amazon EKS — custom app metrics queryable via **PromQL**, traces via **X-Ray**, and logs via **CloudWatch Logs**.

**Cluster:** eks-otel-cluster  
**Region:** us-east-2  
**Kubernetes Version:** 1.35  
**Node Group:** m5.large × 2 (private subnets, AL2023 AMI)

### How Custom App Metrics Flow to PromQL

This is the part the AWS docs under-explain. There are two distinct pipelines and they serve different purposes:

| Path | What it handles | Destination |
|------|----------------|-------------|
| ADOT auto-instrumentation → CW agent `:4316` (App Signals) | Traces + derived RED metrics | X-Ray + CloudWatch standard namespace (EMF) |
| App `/metrics` on `:8888` → CloudWatch cluster scraper | Custom prometheus_client counters/histograms | `monitoring.us-east-1.amazonaws.com` → **PromQL store** |

**The AWS blog post says send to port 4317 — that port does not exist on the CW agent service (v6.1.0).** The App Signals ports (4315/4316) only export via EMF to the standard CloudWatch namespace, not the PromQL store. Custom metrics must reach the PromQL store via the cluster scraper Prometheus pipeline.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                          EKS Cluster                               │
│                                                                    │
│  ┌─────────────────────┐   HTTP :8888/metrics   ┌───────────────┐ │
│  │   otel-demo-app     │ ◀────────────────────── │ CW Cluster    │ │
│  │  (2 replicas)       │                         │ Scraper       │ │
│  │                     │                         │ (Deployment)  │ │
│  │  prometheus_client  │                         └───────┬───────┘ │
│  │  Counter/Histogram  │                                 │         │
│  │  start_http_server  │   OTLP traces :4316             │         │
│  │  (0.0.0.0:8888)     │ ──────────────────────▶ ┌───────────────┐ │
│  └─────────────────────┘   (ADOT auto-instr)     │ CW Agent      │ │
│                                                   │ DaemonSet     │ │
└───────────────────────────────────────────────────┴───────┬───────┘
                                                            │
                     ┌──────────────────────────────────────┼──────┐
                     │              CloudWatch               │      │
                     │                                       │      │
                     │  ┌──────────────────────┐  ┌─────────▼────┐ │
                     │  │ monitoring endpoint  │  │  X-Ray       │ │
                     │  │ (PromQL store)       │  │  Traces      │ │
                     │  │                      │  └──────────────┘ │
                     │  │ rate(app_requests_   │                   │
                     │  │   total[5m])  ✅     │  ┌──────────────┐ │
                     │  └──────────────────────┘  │  CloudWatch  │ │
                     │                            │  Logs        │ │
                     │  CloudWatch Query Studio   └──────────────┘ │
                     └────────────────────────────────────────────┘
```

---

## Step 1: Create the EKS Cluster (CloudFormation)

CloudFormation template creates:
- VPC (192.168.0.0/16), 2 public + 2 private subnets across 2 AZs
- NAT Gateways (one per AZ)
- EKS cluster (v1.35, NO Auto Mode)
- Managed Node Group (m5.large, min 2, max 4, AL2023 AMI)
- IAM roles for cluster and nodes

```bash
aws cloudformation deploy \
  --template-file eks-otel-cluster-template.yaml \
  --stack-name eks-eks-otel-cluster-stack \
  --capabilities CAPABILITY_IAM \
  --region us-east-1
```

Takes ~15-20 minutes.

---

## Step 2: Configure kubectl Access

```bash
aws eks update-kubeconfig --name eks-otel-cluster --region us-east-1
```

If you get `401 Unauthorized`, the IAM role has an access entry but no policy. Fix:

```bash
aws eks associate-access-policy \
  --cluster-name eks-otel-cluster \
  --principal-arn arn:aws:iam::<ACCOUNT_ID>:role/admin \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
  --access-scope type=cluster \
  --region us-east-1
```

---

## Step 3: Convert Self-Managed Add-ons to Managed

EKS installs coredns, kube-proxy, and vpc-cni as self-managed by default (invisible to `aws eks list-addons`):

```bash
aws eks create-addon --cluster-name eks-otel-cluster --addon-name vpc-cni --region us-east-1 --resolve-conflicts OVERWRITE
aws eks create-addon --cluster-name eks-otel-cluster --addon-name kube-proxy --region us-east-1 --resolve-conflicts OVERWRITE
aws eks create-addon --cluster-name eks-otel-cluster --addon-name coredns --region us-east-1 --resolve-conflicts OVERWRITE
```

---

## Step 4: Enable OTel Enrichment (Account-Level)

**Required before installing the CloudWatch add-on.** Makes metrics sent to the OTLP store queryable via PromQL in CloudWatch Query Studio.

```bash
aws observabilityadmin start-telemetry-enrichment
aws cloudwatch start-otel-enrichment
```

Or via console: **CloudWatch → Settings → Enable OTel metric enrichment**.

---

## Step 5: Install CloudWatch Observability Add-on

**Must be v6.0.1+.** v5.x only publishes via EMF — metrics land in the standard CloudWatch namespace and are NOT queryable via PromQL. v6.x publishes via OTLP to the PromQL store.

```bash
# Check latest version
aws eks describe-addon-versions \
  --addon-name amazon-cloudwatch-observability \
  --kubernetes-version 1.35 \
  --region us-east-1 \
  --query 'addons[0].addonVersions[0].addonVersion'
# → "v6.1.0-eksbuild.1"

aws eks create-addon \
  --cluster-name eks-otel-cluster \
  --addon-name amazon-cloudwatch-observability \
  --addon-version v6.1.0-eksbuild.1 \
  --resolve-conflicts OVERWRITE \
  --region us-east-1
```

This installs:
- `cloudwatch-agent` DaemonSet — collects container insights + receives App Signals OTLP on ports 4315/4316
- `cloudwatch-agent-cluster-scraper` Deployment — scrapes kube-state-metrics, apiserver, and (after Step 7b) custom app pods
- `amazon-cloudwatch-observability-controller-manager` — operator that manages both agents via the `AmazonCloudWatchAgent` CRD

---

## Step 6: Attach IAM Policies to Node Role

```bash
NODE_ROLE=eks-eks-otel-cluster-stack-NodeInstanceRole-2Wy4Lt2ev2cZ

# CloudWatch agent (logs, metrics, OTLP)
aws iam attach-role-policy --role-name $NODE_ROLE \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy

# X-Ray traces
aws iam attach-role-policy --role-name $NODE_ROLE \
  --policy-arn arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess

# Grafana read access
aws iam attach-role-policy --role-name $NODE_ROLE \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchReadOnlyAccess
```

| Policy | Purpose |
|--------|---------|
| `AmazonEKSWorkerNodePolicy` | Node registration |
| `AmazonEKS_CNI_Policy` | VPC CNI networking |
| `AmazonEC2ContainerRegistryReadOnly` | Pull images |
| `AmazonSSMManagedInstanceCore` | SSM debugging |
| `CloudWatchAgentServerPolicy` | Push metrics, logs via OTLP |
| `AWSXRayDaemonWriteAccess` | Push traces to X-Ray |
| `CloudWatchReadOnlyAccess` | Grafana reads metrics/logs |

---

## Step 7: Deploy the Sample App

```bash
kubectl apply -f k8s-manifests.yaml
```

**What's in the manifest:**
- `Namespace: otel-demo`
- `ConfigMap` — Python app code (mounted as `/app/app.py`)
- `Deployment: otel-demo-app` (2 replicas)
- `Service: otel-demo-app` (ClusterIP :80)
- `Deployment: load-generator` — busybox continuously hitting all endpoints

**App design — two separate concerns:**

| Concern | Mechanism |
|---------|-----------|
| **Traces** | ADOT auto-instrumentation (injected by operator via `inject-python: "true"` annotation). Flask HTTP spans go to X-Ray automatically. |
| **Custom metrics (PromQL)** | `prometheus_client` library, `start_http_server(8888, addr="0.0.0.0")`. Exposed at `/metrics` on port 8888. |

**Why not use `opentelemetry.metrics.get_meter()`?**  
The ADOT operator injects `OTEL_METRICS_EXPORTER=none` into every auto-instrumented pod, disabling the OTel metrics pipeline entirely. Custom meters created with the OTel API are silently dropped. Use `prometheus_client` directly instead.

**Pod annotations that matter:**
```yaml
annotations:
  instrumentation.opentelemetry.io/inject-python: "true"  # triggers ADOT init container
  prometheus.io/scrape: "true"                             # tells scraper to include this pod
  prometheus.io/port: "8888"                               # port of the /metrics server
  prometheus.io/path: "/metrics"                           # metrics path
```

**App pip installs** (in the Deployment command):
```
flask==3.1.0 prometheus-client
```
Do NOT install `opentelemetry-*` packages — the ADOT init container injects the correct SDK version. Installing your own causes version conflicts.

---

## Step 7b: Patch the CloudWatch Cluster Scraper

The cluster scraper needs a new pipeline to scrape annotated pods and forward metrics to the PromQL store. The scraper is managed by the `AmazonCloudWatchAgent` CRD — **edit the CR, not the ConfigMap** (direct ConfigMap edits are reconciled away immediately by the operator).

Run `patch-cluster-scraper.py` once after the add-on is installed:

```bash
pip3 install pyyaml --break-system-packages  # if not already installed
python3 patch-cluster-scraper.py
```

This adds a `prometheus/app_pods` receiver and `metrics/app_pods` pipeline that:
1. Discovers pods with `prometheus.io/scrape: "true"` annotation via Kubernetes service discovery
2. Scrapes `/metrics` on the annotated port
3. Forwards to `monitoring.us-east-1.amazonaws.com` (the PromQL store)

**Key relabeling gotcha:** The OTel Collector in CWAgent 1.300067 treats `$1`, `$2`, etc. in YAML values as environment variable references. `$$` escaping is not supported. To build `pod_ip:port` without using `$1:$2` in `replacement`, use `separator: ':'` on the two source labels — the Prometheus receiver joins them with `:` and uses the concatenated value directly.

---

## Step 8: Verify Telemetry

### Traces (X-Ray)

CloudWatch → X-Ray → Traces

Spans visible: `GET /api/orders` → `get_orders` → `db_query` + `process_results`  
`POST /api/orders` → `create_order` → `validate_order` + `db_insert`  
10% of POST requests are intentional errors.

### Logs

```
/aws/containerinsights/eks-otel-cluster/application
/aws/containerinsights/eks-otel-cluster/performance
```

### Metrics (PromQL)

CloudWatch → Metrics → Query Studio → switch to **PromQL** tab

**Container Insights (built-in from add-on):**
```promql
container_cpu_usage_seconds_total
container_memory_working_set_bytes
node_cpu_seconds_total
rate(container_network_receive_bytes_total[5m])
```

**Custom app metrics (from prometheus_client):**
```promql
# Request rate by endpoint and status
rate(app_requests_total[5m])

# p99 latency
histogram_quantile(0.99, rate(app_request_duration_ms_bucket[5m]))

# Order creation rate
rate(app_orders_created_total[5m])

# Error rate (5xx responses)
rate(app_requests_total{status="500"}[5m])
```

**AWS vended EC2 metrics (enriched):**
```promql
avg by (InstanceId) (histogram_avg(CPUUtilization{"@instrumentation.name"="cloudwatch.aws/ec2"}))
```

> **Note:** Allow 1-2 minutes after the cluster scraper starts for metrics to appear. The scrape interval is 30s and CloudWatch indexes data before it's queryable.

---

## Step 9: Grafana Integration (Optional)

Amazon Managed Grafana requires IAM Identity Center. Use self-hosted Grafana instead.

### Install

```bash
helm repo add grafana https://grafana.github.io/helm-charts && helm repo update

helm install grafana grafana/grafana \
  --namespace grafana --create-namespace \
  --set adminPassword='admin123' \
  --set service.type=ClusterIP \
  --set persistence.enabled=false
```

`persistence.enabled=false` is required unless the EBS CSI driver is installed.

### Fix IMDS hop limit

EKS sets the IMDS hop limit to 1 by default, blocking containers from reaching instance metadata. Increase to 2 on each node so Grafana can pick up the node IAM role:

```bash
for id in $(aws ec2 describe-instances \
  --filters "Name=tag:kubernetes.io/cluster/eks-otel-cluster,Values=owned" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].InstanceId' \
  --output text --region us-east-1); do
  aws ec2 modify-instance-metadata-options \
    --instance-id $id \
    --http-put-response-hop-limit 2 \
    --region us-east-1
done
```

### Access

```bash
kubectl port-forward svc/grafana 3000:80 -n grafana
# open http://localhost:3000  login: admin / admin123
```

### Add CloudWatch data source

1. Connections → Data Sources → Add → **CloudWatch**
2. Authentication Provider: `AWS SDK Default`
3. Default Region: `us-east-1`
4. Save & Test

### Add PromQL data source

```bash
kubectl exec -n grafana deploy/grafana -- grafana cli plugins install grafana-amazonprometheus-datasource
kubectl rollout restart deployment grafana -n grafana
kubectl port-forward svc/grafana 3000:80 -n grafana
```

Add data source → **Amazon Managed Service for Prometheus**:
- URL: `https://monitoring.us-east-1.amazonaws.com`
- Service: `monitoring`
- SigV4 auth: enabled, Region: `us-east-1`

---

## Key Lessons Learned

| Issue | Root Cause | Fix |
|-------|-----------|-----|
| Nodes visible but addons missing | EKS installs vpc-cni, kube-proxy, coredns as self-managed (not in `aws eks list-addons`) | `aws eks create-addon --resolve-conflicts OVERWRITE` for each |
| `401 Unauthorized` on kubectl | Access entry existed but no policy bound | `aws eks associate-access-policy` with `AmazonEKSClusterAdminPolicy` |
| Metrics export 500/403 | Node role missing CloudWatch and X-Ray permissions | Attach `CloudWatchAgentServerPolicy` + `AWSXRayDaemonWriteAccess` |
| `ModuleNotFoundError: opentelemetry` | Missing `inject-python: "true"` annotation — ADOT init container not injected | Add annotation to pod template metadata |
| PromQL returns nothing — Container Insights | Add-on v5.x publishes via EMF only, not OTLP | Upgrade to v6.0.1+ (`v6.1.0-eksbuild.1`) |
| PromQL returns nothing — OTel enrichment | Account-level enrichment was `NOT_STARTED` | `aws observabilityadmin start-telemetry-enrichment` (Step 4) |
| `meter.create_counter()` not in PromQL | ADOT operator injects `OTEL_METRICS_EXPORTER=none` — OTel metrics pipeline disabled | Use `prometheus_client.Counter/Histogram` directly |
| AWS blog says port `4317` — nothing arrives | Port 4317 does not exist on the CW agent service. Ports 4315/4316 (App Signals) export via EMF to standard CloudWatch namespace, NOT the PromQL store | Use the cluster scraper Prometheus pipeline (Step 7b) |
| `start_http_server(8888, addr="")` crashes | Empty string causes `socket.getaddrinfo("")` DNS failure | Use `addr="0.0.0.0"` explicitly |
| ConfigMap edits immediately reverted | `amazon-cloudwatch-observability-controller-manager` operator reconciles ConfigMaps from `AmazonCloudWatchAgent` CRD | Edit the CR with `kubectl replace` on the `amazoncloudwatchagent` resource |
| `$1:$2` in relabeling → `env var "2" invalid` | OTel confmap treats `$N` as env var references; `$$` escaping not supported in CWAgent 1.300067 | Use `separator: ':'` with no `replacement` field |
| "Summary datapoints not supported" warnings | `prometheus_client` exposes Summary-type process metrics; CloudWatch OTLP store only supports Counter, Gauge, Histogram | Benign — custom Counter/Histogram metrics are accepted fine |
| OTel SDK version conflict | Auto-instrumentation injects SDK 1.40.0; manually installing a different version causes `TypeError` | Do not install `opentelemetry-*` packages in the app — let the init container handle it |

---

## Cleanup

```bash
# App
kubectl delete -f k8s-manifests.yaml

# Revert cluster scraper CR (remove app_pods pipeline)
python3 patch-cluster-scraper.py --revert  # or kubectl edit amazoncloudwatchagent

# CloudWatch add-on
aws eks delete-addon --cluster-name eks-otel-cluster --addon-name amazon-cloudwatch-observability --region us-east-1

# Core add-ons
aws eks delete-addon --cluster-name eks-otel-cluster --addon-name vpc-cni --region us-east-1
aws eks delete-addon --cluster-name eks-otel-cluster --addon-name kube-proxy --region us-east-1
aws eks delete-addon --cluster-name eks-otel-cluster --addon-name coredns --region us-east-1

# Cluster + VPC
aws cloudformation delete-stack --stack-name eks-eks-otel-cluster-stack --region us-east-1

# OTel enrichment (optional)
aws observabilityadmin stop-telemetry-enrichment
aws cloudwatch stop-otel-enrichment
```
