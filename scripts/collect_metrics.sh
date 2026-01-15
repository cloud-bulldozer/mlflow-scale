#!/bin/bash

# Prometheus Metrics Collection Script for MLflow Performance Tests
# Collects aggregated metrics over a test window from Prometheus
# Output format: CSV with metadata comments

set -e

# Usage function
usage() {
    cat << EOF
Usage: $0 [OPTIONS]

Collect aggregated Prometheus metrics for MLflow pods over a test window.

REQUIRED ARGUMENTS:
  --start-time TIMESTAMP  Test start time (unix timestamp)
  --end-time TIMESTAMP    Test end time (unix timestamp)
  --output FILE           Output metrics file path

OPTIONAL ARGUMENTS:
  --namespace NAMESPACE   MLflow namespace (default: opendatahub)
  --pod-label LABEL       MLflow pod label selector regex (default: mlflow.*)
  --prom-route HOST       Prometheus route hostname (auto-detected if not provided)
  --prom-token TOKEN      Prometheus auth token (auto-detected if not provided)
  --test-id ID            Test identifier for the output header

EXAMPLES:
  $0 --start-time 1234567890 --end-time 1234568490 --output metrics.csv
  $0 --start-time \$(date -d '10 minutes ago' +%s) --end-time \$(date +%s) --output test-metrics.csv
  $0 --start-time 1234567890 --end-time 1234568490 --output metrics.csv --namespace mlflow-system

EOF
    exit 1
}

# Default values
NAMESPACE="opendatahub"
POD_LABEL="mlflow.*"
TEST_ID=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --start-time)
            START_TIME="$2"
            shift 2
            ;;
        --end-time)
            END_TIME="$2"
            shift 2
            ;;
        --output)
            OUTPUT_FILE="$2"
            shift 2
            ;;
        --namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        --pod-label)
            POD_LABEL="$2"
            shift 2
            ;;
        --prom-route)
            PROM_ROUTE="$2"
            shift 2
            ;;
        --prom-token)
            PROM_TOKEN="$2"
            shift 2
            ;;
        --test-id)
            TEST_ID="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Error: Unknown option: $1"
            usage
            ;;
    esac
done

# Validate required arguments
if [ -z "$START_TIME" ] || [ -z "$END_TIME" ] || [ -z "$OUTPUT_FILE" ]; then
    echo "Error: Missing required arguments (--start-time, --end-time, --output)"
    usage
fi

# Calculate duration from start/end times
DURATION=$((END_TIME - START_TIME))

# Helper function to parse Prometheus response value
parse_prom_value() {
    local response=$1
    if command -v jq &> /dev/null; then
        echo "$response" | jq -r '.data.result[0].value[1] // "N/A"' 2>/dev/null || echo "N/A"
    else
        echo "$response" | grep -o '"value":\[[^]]*\]' | head -1 | sed 's/"value":\[.*,"\?\([^"]*\)"\?\]/\1/' 2>/dev/null || echo "N/A"
    fi
}

# Helper function to collect a single metric (CSV output)
# Arguments: component, metric, aggregation, unit, prom_query, prom_route, prom_token, metrics_file, eval_time
collect_metric() {
    local component=$1
    local metric=$2
    local aggregation=$3
    local unit=$4
    local prom_query=$5
    local prom_route=$6
    local prom_token=$7
    local metrics_file=$8
    local eval_time=$9
    
    local response=$(curl -k -s -H "Authorization: Bearer $prom_token" \
        --data-urlencode "query=${prom_query}" \
        "https://$prom_route/api/v1/query?time=${eval_time}" 2>/dev/null)
    
    local value=$(parse_prom_value "$response")
    echo "${component},${metric},${aggregation},${unit},${value}" >> "$metrics_file"
}

# Create output directory if needed
mkdir -p "$(dirname "$OUTPUT_FILE")"

# Format timestamps for display
if date -r "$START_TIME" '+%Y-%m-%d %H:%M:%S' &>/dev/null; then
    # macOS date
    START_TIME_FMT=$(date -r "$START_TIME" '+%Y-%m-%d %H:%M:%S')
    END_TIME_FMT=$(date -r "$END_TIME" '+%Y-%m-%d %H:%M:%S')
else
    # GNU date
    START_TIME_FMT=$(date -d "@$START_TIME" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "$START_TIME")
    END_TIME_FMT=$(date -d "@$END_TIME" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "$END_TIME")
fi

# Write CSV header with metadata as comments
cat > "$OUTPUT_FILE" <<EOF
# MLflow Performance Test - Prometheus Metrics
# Generated: $(date '+%Y-%m-%d %H:%M:%S')
# Test ID: ${TEST_ID:-N/A}
# Namespace: ${NAMESPACE}
# Pod Label Pattern: ${POD_LABEL}
# Test Duration: ${DURATION}s
# Test Start: ${START_TIME_FMT}
# Test End: ${END_TIME_FMT}
component,metric,aggregation,unit,value
EOF

# Get Prometheus route and token if not provided
if [ -z "$PROM_ROUTE" ]; then
    PROM_ROUTE=$(oc get route -n openshift-monitoring thanos-querier -o jsonpath='{.spec.host}' 2>/dev/null || \
                 oc get route -n openshift-monitoring prometheus-k8s -o jsonpath='{.spec.host}' 2>/dev/null || true)
fi

if [ -z "$PROM_TOKEN" ]; then
    PROM_TOKEN=$(oc whoami -t 2>/dev/null || true)
    if [ -z "$PROM_TOKEN" ]; then
        PROM_TOKEN=$(oc create token prometheus-k8s -n openshift-monitoring --duration=10m 2>/dev/null || true)
    fi
fi

# Check if Prometheus is accessible
if [ -z "$PROM_ROUTE" ] || [ -z "$PROM_TOKEN" ]; then
    echo "# ERROR: Unable to access Prometheus - metrics not available" >> "$OUTPUT_FILE"
    echo "# Note: Set --prom-route and --prom-token manually if auto-detection fails." >> "$OUTPUT_FILE"
    exit 1
fi

# Use the test duration for range queries
range="${DURATION}s"

# MLflow pods
collect_metric "mlflow" "cpu" "avg" "cores" \
    "max(avg_over_time(irate(container_cpu_usage_seconds_total{namespace=\"${NAMESPACE}\",pod=~\"${POD_LABEL}\",container=\"mlflow\"}[2m])[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "mlflow" "cpu" "max" "cores" \
    "max(max_over_time(irate(container_cpu_usage_seconds_total{namespace=\"${NAMESPACE}\",pod=~\"${POD_LABEL}\",container=\"mlflow\"}[2m])[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "mlflow" "memory" "avg" "bytes" \
    "max(avg_over_time(container_memory_working_set_bytes{namespace=\"${NAMESPACE}\",pod=~\"${POD_LABEL}\",container=\"mlflow\"}[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "mlflow" "memory" "max" "bytes" \
    "max(max_over_time(container_memory_working_set_bytes{namespace=\"${NAMESPACE}\",pod=~\"${POD_LABEL}\",container=\"mlflow\"}[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "mlflow" "pod_count" "current" "count" \
    "count(container_memory_working_set_bytes{namespace=\"${NAMESPACE}\",pod=~\"${POD_LABEL}\",container=\"mlflow\"})" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

# PostgreSQL
collect_metric "postgresql" "cpu" "avg" "cores" \
    "max(avg_over_time(irate(container_cpu_usage_seconds_total{namespace=\"${NAMESPACE}\",pod=~\".*postgres.*|.*postgresql.*\",container!=\"\",container!=\"POD\"}[2m])[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "postgresql" "cpu" "max" "cores" \
    "max(max_over_time(irate(container_cpu_usage_seconds_total{namespace=\"${NAMESPACE}\",pod=~\".*postgres.*|.*postgresql.*\",container!=\"\",container!=\"POD\"}[2m])[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "postgresql" "memory" "avg" "bytes" \
    "max(avg_over_time(container_memory_working_set_bytes{namespace=\"${NAMESPACE}\",pod=~\".*postgres.*|.*postgresql.*\",container!=\"\",container!=\"POD\"}[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "postgresql" "memory" "max" "bytes" \
    "max(max_over_time(container_memory_working_set_bytes{namespace=\"${NAMESPACE}\",pod=~\".*postgres.*|.*postgresql.*\",container!=\"\",container!=\"POD\"}[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

# k6-benchmark
collect_metric "k6_benchmark" "cpu" "avg" "cores" \
    "max(avg_over_time(irate(container_cpu_usage_seconds_total{namespace=\"${NAMESPACE}\",pod=\"k6-benchmark\",container!=\"\",container!=\"POD\"}[2m])[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "k6_benchmark" "cpu" "max" "cores" \
    "max_over_time(irate(container_cpu_usage_seconds_total{namespace=\"${NAMESPACE}\",pod=\"k6-benchmark\",container!=\"\",container!=\"POD\"}[2m])[$range:])" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "k6_benchmark" "memory" "avg" "bytes" \
    "max(avg_over_time(container_memory_working_set_bytes{namespace=\"${NAMESPACE}\",pod=\"k6-benchmark\",container!=\"\",container!=\"POD\"}[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "k6_benchmark" "memory" "max" "bytes" \
    "max(max_over_time(container_memory_working_set_bytes{namespace=\"${NAMESPACE}\",pod=\"k6-benchmark\",container!=\"\",container!=\"POD\"}[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

# Data Science Gateway
collect_metric "data_science_gateway" "cpu" "avg" "cores" \
    "max(avg_over_time(irate(container_cpu_usage_seconds_total{namespace=\"openshift-ingress\",pod=~\"data-science-gateway.*\",container!=\"\",container!=\"POD\"}[2m])[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "data_science_gateway" "cpu" "max" "cores" \
    "max(max_over_time(irate(container_cpu_usage_seconds_total{namespace=\"openshift-ingress\",pod=~\"data-science-gateway.*\",container!=\"\",container!=\"POD\"}[2m])[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "data_science_gateway" "memory" "avg" "bytes" \
    "max(avg_over_time(container_memory_working_set_bytes{namespace=\"openshift-ingress\",pod=~\"data-science-gateway.*\",container!=\"\",container!=\"POD\"}[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "data_science_gateway" "memory" "max" "bytes" \
    "max(max_over_time(container_memory_working_set_bytes{namespace=\"openshift-ingress\",pod=~\"data-science-gateway.*\",container!=\"\",container!=\"POD\"}[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

# Kube Auth Proxy
collect_metric "kube_auth_proxy" "cpu" "avg" "cores" \
    "max(avg_over_time(irate(container_cpu_usage_seconds_total{namespace=\"openshift-ingress\",pod=~\"kube-auth-proxy.*\",container!=\"\",container!=\"POD\"}[2m])[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "kube_auth_proxy" "cpu" "max" "cores" \
    "max(max_over_time(irate(container_cpu_usage_seconds_total{namespace=\"openshift-ingress\",pod=~\"kube-auth-proxy.*\",container!=\"\",container!=\"POD\"}[2m])[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "kube_auth_proxy" "memory" "avg" "bytes" \
    "max(avg_over_time(container_memory_working_set_bytes{namespace=\"openshift-ingress\",pod=~\"kube-auth-proxy.*\",container!=\"\",container!=\"POD\"}[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "kube_auth_proxy" "memory" "max" "bytes" \
    "max(max_over_time(container_memory_working_set_bytes{namespace=\"openshift-ingress\",pod=~\"kube-auth-proxy.*\",container!=\"\",container!=\"POD\"}[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

# Router
collect_metric "router" "cpu" "avg" "cores" \
    "max(avg_over_time(irate(container_cpu_usage_seconds_total{namespace=\"openshift-ingress\",pod=~\"router-default.*\",container!=\"\",container!=\"POD\"}[2m])[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "router" "cpu" "max" "cores" \
    "max(max_over_time(irate(container_cpu_usage_seconds_total{namespace=\"openshift-ingress\",pod=~\"router-default.*\",container!=\"\",container!=\"POD\"}[2m])[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "router" "memory" "avg" "bytes" \
    "max(avg_over_time(container_memory_working_set_bytes{namespace=\"openshift-ingress\",pod=~\"router-default.*\",container!=\"\",container!=\"POD\"}[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "router" "memory" "max" "bytes" \
    "max(max_over_time(container_memory_working_set_bytes{namespace=\"openshift-ingress\",pod=~\"router-default.*\",container!=\"\",container!=\"POD\"}[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

# OpenShift OAuth
collect_metric "openshift_oauth" "cpu" "avg" "cores" \
    "max(avg_over_time(irate(container_cpu_usage_seconds_total{namespace=\"openshift-authentication\",pod=~\"oauth-openshift.*\",container!=\"\",container!=\"POD\"}[2m])[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "openshift_oauth" "cpu" "max" "cores" \
    "max(max_over_time(irate(container_cpu_usage_seconds_total{namespace=\"openshift-authentication\",pod=~\"oauth-openshift.*\",container!=\"\",container!=\"POD\"}[2m])[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "openshift_oauth" "memory" "avg" "bytes" \
    "max(avg_over_time(container_memory_working_set_bytes{namespace=\"openshift-authentication\",pod=~\"oauth-openshift.*\",container!=\"\",container!=\"POD\"}[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "openshift_oauth" "memory" "max" "bytes" \
    "max(max_over_time(container_memory_working_set_bytes{namespace=\"openshift-authentication\",pod=~\"oauth-openshift.*\",container!=\"\",container!=\"POD\"}[$range:]))" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

# Cluster-wide
collect_metric "cluster" "cpu_utilization" "avg" "percent" \
    "max(avg_over_time((1 - avg(irate(node_cpu_seconds_total{mode=\"idle\"}[2m])))[$range:]) * 100)" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

collect_metric "cluster" "cpu_utilization" "max" "percent" \
    "max(max_over_time((1 - min(irate(node_cpu_seconds_total{mode=\"idle\"}[2m])))[$range:]) * 100)" \
    "$PROM_ROUTE" "$PROM_TOKEN" "$OUTPUT_FILE" "$END_TIME"

echo "Metrics saved to: $OUTPUT_FILE"
