#!/bin/bash
#
# MLflow Performance Test Suite Runner for OpenShift/Kubernetes
#
# This script:
# 1. Creates tenant namespaces as required for each test step
# 2. Runs k6 load tests from a pod
# 3. Copies summary result files to localhost
# 4. Collects CPU and memory metrics from Prometheus for the mlflow pod
# 5. Generates final reports using report_summary.py
#

set -euo pipefail

# ========================================
# Configuration
# ========================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/results}"
NAMESPACE="${NAMESPACE:-opendatahub}"
K6_POD_NAME="${K6_POD_NAME:-k6-benchmark}"
MLFLOW_POD_LABEL="${MLFLOW_POD_LABEL:-mlflow.*}"
MLFLOW_NAMESPACE="${MLFLOW_NAMESPACE:-opendatahub}"

# Test configuration
TENANT_COUNTS=("1" "10" "100" "500")
CONCURRENCY_LEVELS=(5 10 20 50)
TEST_DURATION="${TEST_DURATION:-10m}"

# MLflow configuration
MLFLOW_URL="${MLFLOW_URL:-}"
MLFLOW_TOKEN="${MLFLOW_TOKEN:-}"

# Database backend configuration (sqlite or postgres)
DB_BACKEND="${DB_BACKEND:-sqlite}"

# ========================================
# Helper Functions
# ========================================

log_info() {
    echo "[INFO] $(date '+%Y-%m-%d %H:%M:%S') - $*"
}

log_error() {
    echo "[ERROR] $(date '+%Y-%m-%d %H:%M:%S') - $*" >&2
}

log_section() {
    echo ""
    echo "========================================"
    echo "$*"
    echo "========================================"
}

cleanup_on_exit() {
    log_section "Cleaning up..."
    log_info "Deleting load generator resources..."
    oc delete pod "${K6_POD_NAME}" -n "${NAMESPACE}" --ignore-not-found=true 2>/dev/null || true
    oc delete configmap k6-loadtest-script -n "${NAMESPACE}" --ignore-not-found=true 2>/dev/null || true
    
    log_info "Deleting tenant namespaces..."
    oc get namespaces -o name 2>/dev/null | grep '^namespace/tenant-' | xargs -r oc delete --ignore-not-found=true --wait=false 2>/dev/null || true

    log_info "Deleting MLflow..."
    oc delete mlflow mlflow --ignore-not-found=true 2>/dev/null || true
    oc wait --for=delete pod -l "app=mlflow" -n "${MLFLOW_NAMESPACE}" --timeout=120s || true

    # Cleanup Postgres if it was deployed
    if [[ "${DB_BACKEND}" == "postgres" ]]; then
        cleanup_postgres
    fi

    log_info "Cleanup completed successfully"
}

# ========================================
# Postgres Management
# ========================================

deploy_postgres() {
    log_info "Deploying PostgreSQL..."
    
    # Apply the Postgres manifest (PVC, Deployment, Service)
    if ! oc apply -f "${SCRIPT_DIR}/../manifests/Postgres.yml"; then
        log_error "Failed to apply PostgreSQL manifest"
        return 1
    fi
    
    # Wait for the Postgres pod to become ready
    log_info "Waiting for PostgreSQL pod to become ready..."
    if ! oc wait --for=condition=Available deployment/postgres -n "${NAMESPACE}" --timeout=300s; then
        log_error "Timeout waiting for PostgreSQL deployment to become ready"
        return 1
    fi
    
    # Additional wait for the pod to be fully ready
    if ! oc wait --for=condition=Ready pod -l "app=postgres" -n "${NAMESPACE}" --timeout=120s; then
        log_error "Timeout waiting for PostgreSQL pod to become ready"
        return 1
    fi
    
    log_info "PostgreSQL deployed successfully"
}

cleanup_postgres() {
    log_info "Cleaning up PostgreSQL resources..."
    
    # Delete the deployment
    oc delete deployment postgres -n "${NAMESPACE}" --ignore-not-found=true 2>/dev/null || true
    
    # Wait for pods to be deleted
    oc wait --for=delete pod -l "app=postgres" -n "${NAMESPACE}" --timeout=120s 2>/dev/null || true
    
    # Delete the service
    oc delete service postgres -n "${NAMESPACE}" --ignore-not-found=true 2>/dev/null || true
    
    # Delete the PVC to ensure clean state
    oc delete pvc postgres-data -n "${NAMESPACE}" --ignore-not-found=true 2>/dev/null || true
    
    log_info "PostgreSQL cleanup completed"
}

# ========================================
# Namespace Management
# ========================================

ensure_tenant_namespaces() {
    local required_count=$1
    log_info "Ensuring ${required_count} tenant namespace(s) exist..."
    
    for i in $(seq 1 "${required_count}"); do
        local ns_name="tenant-${i}"
        if ! oc get namespace "${ns_name}" &>/dev/null; then
            oc create namespace "${ns_name}"
            log_info "Created namespace: ${ns_name}"
        fi
    done
}

# ========================================
# K6 Pod Management
# ========================================

setup_k6_configmap() {
    log_info "Creating k6 load test ConfigMap..."
    
    # Delete existing configmap if present
    oc delete configmap k6-loadtest-script -n "${NAMESPACE}" --ignore-not-found=true 2>/dev/null || true
    
    # Create configmap from the test scripts
    oc create configmap k6-loadtest-script \
        --from-file=mlflow_scale_test.js="${SCRIPT_DIR}/mlflow_scale_test.js" \
        --from-file=mlflow_prefill_tenants.js="${SCRIPT_DIR}/mlflow_prefill_tenants.js" \
        -n "${NAMESPACE}"
    
    log_info "ConfigMap created successfully"
}

start_k6_pod() {
    log_info "Starting k6 benchmark pod..."
    
    # Delete existing pod if present
    oc delete pod "${K6_POD_NAME}" -n "${NAMESPACE}" --ignore-not-found=true 2>/dev/null || true
    
    # Wait for pod to be fully deleted
    oc wait --for=delete pod/"${K6_POD_NAME}" -n "${NAMESPACE}" --timeout=60s 2>/dev/null || true
    
    # Create the k6 pod with environment variables substituted
    export MLFLOW_URL MLFLOW_TOKEN
    envsubst '${MLFLOW_URL} ${MLFLOW_TOKEN}' < "${SCRIPT_DIR}/k6-pod.yml" | oc apply -f -
    
    # Wait for pod to be ready
    log_info "Waiting for k6 pod to be ready..."
    oc wait --for=condition=Ready pod/"${K6_POD_NAME}" -n "${NAMESPACE}" --timeout=120s
    
    log_info "K6 pod is ready"
}

run_k6_test() {
    local tenant_count=$1
    local concurrency=$2
    local test_id="${tenant_count}_concurrency_${concurrency}"
    
    log_info "Running k6 test: Tenants=${tenant_count}, Concurrency=${concurrency}"
    
    local k6_cmd="k6 run -e TENANT_COUNT=${tenant_count} -e CONCURRENCY=${concurrency} -e DURATION=${TEST_DURATION}"
    
    # Add MLflow URL and token if provided
    if [[ -n "${MLFLOW_URL}" ]]; then
        k6_cmd="${k6_cmd} -e MLFLOW_URL=${MLFLOW_URL}"
    fi
    if [[ -n "${MLFLOW_TOKEN}" ]]; then
        k6_cmd="${k6_cmd} -e MLFLOW_TOKEN=${MLFLOW_TOKEN}"
    fi
    
    k6_cmd="${k6_cmd} /scripts/mlflow_scale_test.js --insecure-skip-tls-verify"
    
    # Execute the test in the k6 pod
    oc exec "${K6_POD_NAME}" -n "${NAMESPACE}" -- sh -c "${k6_cmd}"
    
    log_info "K6 test completed: ${test_id}"
}

run_prefill_tenants() {
    local tenant_count=$1
    
    log_info "Prefilling data for ${tenant_count} tenant(s)..."
    
    local k6_cmd="k6 run -e TENANT_COUNT=${tenant_count} --no-summary"
    
    # Add MLflow URL and token if provided
    if [[ -n "${MLFLOW_URL}" ]]; then
        k6_cmd="${k6_cmd} -e MLFLOW_URL=${MLFLOW_URL}"
    fi
    if [[ -n "${MLFLOW_TOKEN}" ]]; then
        k6_cmd="${k6_cmd} -e MLFLOW_TOKEN=${MLFLOW_TOKEN}"
    fi
    
    k6_cmd="${k6_cmd} /scripts/mlflow_prefill_tenants.js --insecure-skip-tls-verify"
    
    # Execute the prefill script in the k6 pod
    if oc exec "${K6_POD_NAME}" -n "${NAMESPACE}" -- sh -c "${k6_cmd}"; then
        log_info "Prefilling data for ${tenant_count} tenant(s) completed"
        return 0
    else
        log_error "Prefilling data for ${tenant_count} tenant(s) failed"
        return 1
    fi
}

run_mlflow_cleanup() {
    log_info "Running cleanup - recreating MLflow instance..."
    
    # Delete the MLflow custom resource
    log_info "Deleting MLflow resource..."
    if ! oc delete mlflow mlflow --ignore-not-found=true; then
        log_error "Failed to delete MLflow resource"
        return 1
    fi
    
    # Wait for the mlflow pod to be deleted
    log_info "Waiting for MLflow pod to be deleted..."
    if ! oc wait --for=delete pod -l "app=mlflow" -n "${MLFLOW_NAMESPACE}" --timeout=300s; then
        log_error "Timeout waiting for MLflow pod to be deleted"
        return 1
    fi
    
    # Handle Postgres backend - cleanup and redeploy for clean state
    if [[ "${DB_BACKEND}" == "postgres" ]]; then
        cleanup_postgres
        if ! deploy_postgres; then
            log_error "Failed to deploy PostgreSQL"
            return 1
        fi
    fi
    
    # Select the appropriate MLflow manifest based on backend
    local mlflow_manifest
    if [[ "${DB_BACKEND}" == "postgres" ]]; then
        mlflow_manifest="${SCRIPT_DIR}/../manifests/MLflow_Postgres.yml"
    else
        mlflow_manifest="${SCRIPT_DIR}/../manifests/MLflow.yml"
    fi
    
    # Re-apply the MLflow manifest
    log_info "Re-applying MLflow manifest: ${mlflow_manifest}"
    if ! oc apply -f "${mlflow_manifest}" -n "${MLFLOW_NAMESPACE}"; then
        log_error "Failed to apply MLflow manifest"
        return 1
    fi
    
    # Wait for the MLflow pod to become ready
    log_info "Waiting for MLflow pod to become ready..."
    if ! oc wait --for=condition=Ready pod -l "app=mlflow" -n "${MLFLOW_NAMESPACE}" --timeout=300s; then
        log_error "Timeout waiting for MLflow pod to become ready"
        return 1
    fi
    
    log_info "MLflow cleanup and recreation completed successfully"
}

copy_results_from_pod() {
    local tenant_count=$1
    local concurrency=$2
    
    local summary_filename="summary_tenants-${tenant_count}_concurrency_${concurrency}.json"
    
    log_info "Copying result file: ${summary_filename}"
    
    # Copy from pod's /tmp directory to local results directory
    if oc cp "${NAMESPACE}/${K6_POD_NAME}:/tmp/${summary_filename}" "${RESULTS_DIR}/${summary_filename}" 2>/dev/null; then
        log_info "Successfully copied: ${summary_filename}"
    else
        log_error "Failed to copy ${summary_filename} - file may not exist"
    fi
}

# ========================================
# Prometheus Metrics Collection
# ========================================

# Convert duration string (e.g., "10m", "1h", "30s") to seconds
duration_to_seconds() {
    local duration=$1
    local value="${duration%[smhd]}"
    local unit="${duration: -1}"
    
    case "$unit" in
        s) echo "$value" ;;
        m) echo $((value * 60)) ;;
        h) echo $((value * 3600)) ;;
        d) echo $((value * 86400)) ;;
        *) echo "$duration" ;;  # Assume already in seconds if no unit
    esac
}

collect_prometheus_metrics() {
    local start_time=$1
    local end_time=$2
    local test_id=$3
    local output_file="${RESULTS_DIR}/metrics_${test_id}.csv"
    
    log_info "Collecting Prometheus metrics for test: ${test_id}"
    
    # Build collect_metrics.sh command with arguments
    local collect_cmd="${SCRIPT_DIR}/collect_metrics.sh"
    collect_cmd="${collect_cmd} --start-time ${start_time}"
    collect_cmd="${collect_cmd} --end-time ${end_time}"
    collect_cmd="${collect_cmd} --output ${output_file}"
    collect_cmd="${collect_cmd} --namespace ${MLFLOW_NAMESPACE}"
    collect_cmd="${collect_cmd} --pod-label ${MLFLOW_POD_LABEL}"
    collect_cmd="${collect_cmd} --test-id ${test_id}"
    collect_cmd="${collect_cmd} --db-backend ${DB_BACKEND}"
    
    # Execute the metrics collection script
    if ${collect_cmd}; then
        log_info "Metrics saved to: ${output_file}"
    else
        log_error "Failed to collect Prometheus metrics"
        return 1
    fi
}

# ========================================
# Report Generation
# ========================================

generate_report() {
    log_section "Generating Final Report"
    
    cd "${RESULTS_DIR}"
    
    if [[ ! -f "${SCRIPT_DIR}/report_summary.py" ]]; then
        log_error "report_summary.py not found at ${SCRIPT_DIR}"
        return 1
    fi
    
    # Check if we have any summary files
    if ! ls summary_*.json 1>/dev/null 2>&1; then
        log_error "No summary files found in ${RESULTS_DIR}"
        return 1
    fi
    
    log_info "Running report_summary.py..."
    python3 "${SCRIPT_DIR}/report_summary.py" \
        --pattern "summary_*.json" \
        --output-dir "${RESULTS_DIR}" \
        --csv-name "report_summary.csv"
    
    log_info "Report generation completed"
}

# ========================================
# Main Execution
# ========================================

main() {
    log_section "MLflow Performance Test Suite"
    log_info "Results directory: ${RESULTS_DIR}"
    log_info "Namespace: ${NAMESPACE}"
    log_info "Database backend: ${DB_BACKEND}"
    log_info "Test duration: ${TEST_DURATION}"
    log_info "Tenant counts: ${TENANT_COUNTS[*]}"
    log_info "Concurrency levels: ${CONCURRENCY_LEVELS[*]}"
    
    # Create results directory
    mkdir -p "${RESULTS_DIR}"
    
    # Set up trap for cleanup
    trap cleanup_on_exit EXIT
    
    # Set up k6 pod
    log_section "Setting Up K6 Load Test Pod"
    setup_k6_configmap
    start_k6_pod
    
    # Run test suite
    log_section "Running Test Suite"
    local test_count=0
    local total_tests=$(( ${#TENANT_COUNTS[@]} * ${#CONCURRENCY_LEVELS[@]} ))
    
    for tenant_count in "${TENANT_COUNTS[@]}"; do
        # Ensure required tenant namespaces exist for this test
        ensure_tenant_namespaces "${tenant_count}"

        # Run cleanup - ensure a clean MLflow instance
        if ! run_mlflow_cleanup; then
           log_error "Failed to cleanup MLflow before test suite"
           exit 1
        fi
        
        # Prefill MLflow data
        if ! run_prefill_tenants "${tenant_count}"; then
            log_error "Failed to prefill MLflow data for ${tenant_count} tenant(s)"
            exit 1
        fi
        
        for concurrency in "${CONCURRENCY_LEVELS[@]}"; do
            ((test_count++))

            local test_id="${tenant_count}_concurrency_${concurrency}"
            log_section "Test ${test_count}/${total_tests}: Tenants=${tenant_count}, Concurrency=${concurrency}"
                        
            # Record start time for Prometheus query
            local start_time
            start_time=$(date +%s)
            
            # Run the k6 test
            if run_k6_test "${tenant_count}" "${concurrency}"; then
                # Record end time
                local end_time
                end_time=$(date +%s)
                
                # Copy results from pod
                copy_results_from_pod "${tenant_count}" "${concurrency}"
                
                # Collect Prometheus metrics
                collect_prometheus_metrics "${start_time}" "${end_time}" "${test_id}" || \
                    log_error "Failed to collect Prometheus metrics for ${test_id}"
                
            else
                log_error "Test failed: ${test_id}"
            fi
            
            # Brief pause between tests
            log_info "Waiting 10 seconds before next test..."
            sleep 10
        done
    done
    
    # Generate final report
    generate_report
    
    log_section "Test Suite Completed"
    log_info "Results saved to: ${RESULTS_DIR}"
    log_info "K6 summary files: ${RESULTS_DIR}/summary_*.json"
    log_info "Prometheus metrics: ${RESULTS_DIR}/metrics_*.csv"
    log_info "CSV report: ${RESULTS_DIR}/report_summary.csv"
}

# ========================================
# Script Entry Point
# ========================================

# Show usage if help requested
if [[ "${1:-}" == "-h" ]] || [[ "${1:-}" == "--help" ]]; then
    cat << EOF
MLflow Performance Test Suite Runner

Usage: $(basename "$0") [OPTIONS]

Environment Variables:
  RESULTS_DIR          Directory to store results (default: ./results)
  NAMESPACE            Kubernetes namespace for k6 pod (default: opendatahub)
  K6_POD_NAME          Name for the k6 pod (default: k6-benchmark)
  MLFLOW_POD_LABEL     Pod label regex for MLflow pods (default: mlflow.*)
  MLFLOW_NAMESPACE     Namespace where MLflow is running (default: opendatahub)
  MLFLOW_URL           MLflow server URL (passed to k6 as MLFLOW_URL)
  MLFLOW_TOKEN         MLflow authentication token (passed to k6 as MLFLOW_TOKEN)
  TEST_DURATION        Duration for each test (passed to k6 as DURATION, default: 10m)
  DB_BACKEND           Database backend: 'sqlite' or 'postgres' (default: sqlite)
                       When 'postgres', deploys PostgreSQL and uses MLflow_Postgres.yml
                       Cleanup between runs includes tearing down Postgres deployment and PVC

K6 Test Variables (passed automatically based on test matrix):
  CONCURRENCY          Concurrent users count (from CONCURRENCY_LEVELS array)
  TENANT_COUNT         Number of tenants (from TENANT_COUNTS array)

Examples:
  # Run with defaults (SQLite backend)
  ./$(basename "$0")

  # Run with PostgreSQL backend
  DB_BACKEND=postgres ./$(basename "$0")

  # Run with custom configuration
  MLFLOW_URL=https://mlflow.example.com TEST_DURATION=5m ./$(basename "$0")

  # Run with custom results directory
  RESULTS_DIR=/tmp/mlflow-results ./$(basename "$0")

  # Run PostgreSQL backend with custom results directory
  DB_BACKEND=postgres RESULTS_DIR=/tmp/mlflow-postgres-results ./$(basename "$0")

Requirements:
  - oc CLI configured with cluster access
  - jq for JSON processing
  - curl for Prometheus queries
  - envsubst for environment variable substitution
  - Python 3 with pandas and matplotlib for report generation

EOF
    exit 0
fi

# Check prerequisites
for cmd in oc jq curl python3 envsubst; do
    if ! command -v "${cmd}" &>/dev/null; then
        log_error "Required command not found: ${cmd}"
        exit 1
    fi
done

# Validate DB_BACKEND
if [[ "${DB_BACKEND}" != "sqlite" && "${DB_BACKEND}" != "postgres" ]]; then
    log_error "Invalid DB_BACKEND: ${DB_BACKEND}. Must be 'sqlite' or 'postgres'"
    exit 1
fi

# Run main
main "$@"
