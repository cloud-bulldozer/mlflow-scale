# MLflow Scale & Performance Testing Suite

> 🚀 Automated performance & scalability testing for MLflow with multi-tenant workspaces on OpenShift/Kubernetes

A comprehensive collection of scripts for running performance & scale tests for MLflow with the workspaces multi-tenancy feature. This project automates the deployment of MLflow, test artifacts deployment, prefilling the test data, running a series of tests, collecting results, and providing CSV summary along with charts.

---

## ✨ Features

- **Automated Test Suite** — Full test lifecycle management including setup, execution, and cleanup
- **Multi-Tenant Testing** — Validate MLflow performance across different tenant configurations
- **Prometheus Integration** — Automatic CPU/memory metrics collection from cluster
- **Rich Visualizations** — Auto-generated charts for response times, throughput, and resource utilization
- **Realistic Workloads** — 80/20 read/write split simulating actual MLflow usage patterns

---

## 📋 Prerequisites

| Requirement | Description |
|-------------|-------------|
| `oc` | OpenShift CLI configured with cluster access |
| `jq` | JSON processor for parsing results |
| `curl` | HTTP client for Prometheus queries |
| `envsubst` | Environment variable substitution |
| `python3` | Python 3.x with `pandas` and `matplotlib` |

Install Python dependencies:

```bash
pip install -r scripts/requirements.txt
```

---

## 🚀 Quick Start

### 1. Deploy Dependencies

Infrasturcture pre-requisite: OpenShift cluster with installed [OpenDataHub operator](https://github.com/opendatahub-io/opendatahub-operator)
```bash
# Apply OpenDataHub manifests
oc apply -f manifests/DSCInitialization.yml
oc apply -f manifests/DataScienceCluster.yml
```

Install the mlflow-operator from the [repo](https://github.com/opendatahub-io/mlflow-operator):
```bash
make deploy-to-platform IMG=quay.io/mlflow-operator/mlflow-operator:master PLATFORM=odh
```

### 2. Run the Test Suite

```bash
# Set required environment variables
export MLFLOW_URL="https://your-data-science-gateway.example.com/mlflow"
export MLFLOW_TOKEN="sha256~xxxxxxxxxxxx"

# Run the full test suite
./scripts/run_suite.sh
```

### 3. View Results

Results are saved to `scripts/results/`:

```bash
ls scripts/results/
# summary_*.json       — Raw k6 test results
# metrics_*.csv        — Prometheus metrics per test
# report_summary.csv   — Consolidated CSV report
# chart_*.png          — Visualization charts
```

---

## ⚙️ Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RESULTS_DIR` | `./results` | Directory to store test results |
| `NAMESPACE` | `opendatahub` | Kubernetes namespace for k6 pod |
| `K6_POD_NAME` | `k6-benchmark` | Name for the k6 load generator pod |
| `MLFLOW_NAMESPACE` | `opendatahub` | Namespace where MLflow is running |
| `MLFLOW_URL` | — | MLflow server URL (required) |
| `MLFLOW_TOKEN` | — | MLflow authentication token (required) |
| `TEST_DURATION` | `5m` | Duration for each test iteration |

### Test Matrix

The default test matrix can be modified in `run_suite.sh`:

```bash
TENANT_COUNTS=("1" "10" "100" "500")  # Number of tenants
CONCURRENCY_LEVELS=(5 10 20)          # Concurrency per test
TEST_DURATION="10m"                   # Duration per test
```

---

## 📊 Test Scenarios

The k6 test script (`mlflow_scale_test.js`) simulates realistic MLflow usage:

### Training Scenario (20% of total load)

Simulates ML training pipelines writing to MLflow:

| Operation | Description |
|-----------|-------------|
| `create_experiment` | Create a new experiment |
| `create_prompt` | Create 3 prompts per experiment |
| `create_prompt_version` | Create version for each prompt |
| `create_run` | Start 3 runs per experiment |
| `log_metric` | Log 3 metrics per run |
| `log_parameter` | Log 5 parameters per run |
| `log_artifact` | Upload 2 artifacts (~10KB each) |
| `update_run_status` | Mark run as FINISHED |

### Browsing Scenario (80% of total load)

Simulates users browsing MLflow UI:

| Operation | Description |
|-----------|-------------|
| `search_prompts` | Search prompts (up to 100 results) |
| `search_experiments` | List up to 25 experiments |
| `get_experiment` | Fetch experiment details |
| `search_runs` | Search runs in experiment |
| `get_run` | Fetch individual run details |
| `list_artifacts` | List run artifacts |
| `fetch_artifact` | Download artifact content |

---

## 📈 Generated Charts

| Chart | Description |
|-------|-------------|
| `chart_summary_dashboard.png` | Overview of throughput, requests, and failures |
| `chart_response_times_by_concurrency.png` | P95 latency vs concurrency |
| `chart_response_times_by_tenants.png` | P95 latency vs tenant count |
| `chart_throughput_heatmap.png` | Request rate heatmap (tenants × concurrency) |
| `chart_response_times_p95_heatmap.png` | P95 latency heatmap |
| `chart_passed_counts.png` | Successful operations by config |
| `chart_cpu_utilization.png` | CPU usage by component |
| `chart_memory_utilization.png` | Memory usage by component |
| `chart_mlflow_cpu_by_concurrency.png` | MLflow CPU vs concurrency |
| `chart_mlflow_cpu_by_tenants.png` | MLflow CPU vs tenant count |

---

## 📁 Project Structure

```
mlflow-scale/
├── manifests/                    # Kubernetes/OpenShift manifests
│   ├── DataScienceCluster.yml    # OpenDataHub cluster config
│   ├── DSCInitialization.yml     # DSC initialization
│   ├── MLflow.yml                # MLflow CR definition
│   └── MLflow_Postgres.yml       # PostgreSQL backend config
│
├── scripts/
│   ├── run_suite.sh              # Main test suite orchestrator
│   ├── mlflow_scale_test.js      # k6 load test script
│   ├── mlflow_prefill_tenants.js # k6 script to prefill tenant data
│   ├── collect_metrics.sh        # Prometheus metrics collector
│   ├── report_summary.py         # Report & chart generator
│   ├── k6-pod.yml                # k6 pod specification
│   └── requirements.txt          # Python dependencies
│
└── README.md
```

---

## 🔧 Advanced Usage

### Running Individual Components

```bash
# Collect Prometheus metrics manually
./scripts/collect_metrics.sh \
  --start-time $(date -d '10 minutes ago' +%s) \
  --end-time $(date +%s) \
  --output metrics.csv

# Generate reports from existing results
cd scripts/results
python3 ../report_summary.py \
  --pattern "summary_*.json" \
  --metrics-pattern "metrics_*.csv" \
  --output-dir .
```

### Running k6 Tests Manually

```bash
# Exec into the k6 pod
oc exec -it k6-benchmark -n opendatahub -- sh

# Run a single tenant test
k6 run \
  -e MLFLOW_URL=https://mlflow.example.com \
  -e MLFLOW_TOKEN=sha256~xxx \
  -e CONCURRENCY=10 \
  -e DURATION=5m \
  -e TENANT_COUNT=1 \
  /scripts/mlflow_scale_test.js

# Run a multi-tenant test
k6 run \
  -e MLFLOW_URL=https://mlflow.example.com \
  -e MLFLOW_TOKEN=sha256~xxx \
  -e CONCURRENCY=50 \
  -e DURATION=5m \
  -e TENANT_COUNT=100 \
  /scripts/mlflow_scale_test.js
```

---

## 📝 License

This project is released under the [Apache License 2.0](LICENSE).
