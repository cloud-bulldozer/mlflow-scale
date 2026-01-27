import http from 'k6/http';
import { check } from 'k6';
import encoding from 'k6/encoding';
import { Trend, Counter } from 'k6/metrics';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';
import { sleep } from 'k6';

// --- CONFIGURATION ---
const BASE_URL = __ENV.MLFLOW_URL || 'NO_URL_PROVIDED';
const API_PREFIX = '/api/2.0/mlflow';
const API_PREFIX_v3 = '/api/3.0/mlflow';
const AUTH_TOKEN = __ENV.MLFLOW_TOKEN || 'NO_TOKEN_PROVIDED';

const TENANT_COUNT = parseInt(__ENV.TENANT_COUNT || '1');
const TOTAL_CONCURRENCY = parseInt(__ENV.CONCURRENCY || '10');

// Custom metrics for response times
const responseTimeMetrics = {
    create_experiment: new Trend('create_experiment_response_time'),
    create_prompt: new Trend('create_prompt_response_time'),
    create_prompt_version: new Trend('create_prompt_version_response_time'),
    create_run: new Trend('create_run_response_time'),
    log_metric: new Trend('log_metric_response_time'),
    log_parameter: new Trend('log_parameter_response_time'),
    log_artifact: new Trend('log_artifact_response_time'),
    update_run_status: new Trend('update_run_status_response_time'),
    search_prompts: new Trend('search_prompts_response_time'),
    search_experiments: new Trend('search_experiments_response_time'),
    get_experiment: new Trend('get_experiment_response_time'),
    search_runs: new Trend('search_runs_response_time'),
    get_run: new Trend('get_run_response_time'),
    list_artifacts: new Trend('list_artifacts_response_time'),
    fetch_artifact: new Trend('fetch_artifact_response_time'),
    list_workspaces: new Trend('list_workspaces_response_time'),
};

// Counter metrics for pass/fail counts
const statusCounters = {
    create_experiment: { passed: new Counter('create_experiment_passed'), failed: new Counter('create_experiment_failed') },
    create_prompt: { passed: new Counter('create_prompt_passed'), failed: new Counter('create_prompt_failed') },
    create_prompt_version: { passed: new Counter('create_prompt_version_passed'), failed: new Counter('create_prompt_version_failed') },
    create_run: { passed: new Counter('create_run_passed'), failed: new Counter('create_run_failed') },
    log_metric: { passed: new Counter('log_metric_passed'), failed: new Counter('log_metric_failed') },
    log_parameter: { passed: new Counter('log_parameter_passed'), failed: new Counter('log_parameter_failed') },
    log_artifact: { passed: new Counter('log_artifact_passed'), failed: new Counter('log_artifact_failed') },
    update_run_status: { passed: new Counter('update_run_status_passed'), failed: new Counter('update_run_status_failed') },
    search_prompts: { passed: new Counter('search_prompts_passed'), failed: new Counter('search_prompts_failed') },
    search_experiments: { passed: new Counter('search_experiments_passed'), failed: new Counter('search_experiments_failed') },
    get_experiment: { passed: new Counter('get_experiment_passed'), failed: new Counter('get_experiment_failed') },
    search_runs: { passed: new Counter('search_runs_passed'), failed: new Counter('search_runs_failed') },
    get_run: { passed: new Counter('get_run_passed'), failed: new Counter('get_run_failed') },
    list_artifacts: { passed: new Counter('list_artifacts_passed'), failed: new Counter('list_artifacts_failed') },
    fetch_artifact: { passed: new Counter('fetch_artifact_passed'), failed: new Counter('fetch_artifact_failed') },
    list_workspaces: { passed: new Counter('list_workspaces_passed'), failed: new Counter('list_workspaces_failed') },
};

// Workload configuration
export const options = {
    scenarios: {
        training: {
            executor: 'constant-vus',
            vus: Math.floor(TOTAL_CONCURRENCY * 0.2), // 20% Writes
            duration: __ENV.DURATION || '1m',
            gracefulStop: '10s',
            exec: 'trainingScenario',
        },
        browsing: {
            executor: 'constant-vus',
            vus: Math.ceil(TOTAL_CONCURRENCY * 0.8),  // 80% Reads
            duration: __ENV.DURATION || '1m',
            gracefulStop: '10s',
            exec: 'browsingScenario',
        },
    },
};

function getHeaders() {
    return {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${AUTH_TOKEN}`,
        'X-MLFLOW-WORKSPACE': `tenant-${Math.floor(Math.random() * TENANT_COUNT) + 1}`,
    };
}

function validateResponse(res, metricName) {
    const checkResult = check(res, {
        'status is 2xx': (r) => r.status >= 200 && r.status < 300,
        'no error_code in response': (r) => {
            try {
                const json = r.json();
                return !json.error_code;
            } catch (e) {
                return false;
            }
        }
    });
    
    if (metricName) {
        const duration = res.timings.duration;
        
        // Track response time for passed requests only
        if (checkResult && responseTimeMetrics[metricName]) {
            responseTimeMetrics[metricName].add(duration);
        }
        
        if (statusCounters[metricName]) {
            if (checkResult) {
                statusCounters[metricName].passed.add(1);
            } else {
                statusCounters[metricName].failed.add(1);
            }
        }

    }
    
    return checkResult;
}

// --- TASK: TRAINING PIPELINE (WRITES) ---
export function trainingScenario() {
    const config = { headers: getHeaders() };

    const expRes = http.post(`${BASE_URL}${API_PREFIX}/experiments/create`, 
        JSON.stringify({ name: `Exp-${uuidv4()}` }), { ...config, tags: { name: 'create_experiment' } });
    if (!validateResponse(expRes, 'create_experiment')) {
        return;
    }
    const expId = expRes.json().experiment_id;

    if (expId) {
        for (let i = 0; i < 3; i++) {
            const promptName = `${expId}-Prompt-${uuidv4()}`;
            const promptText = `Summarize experiment ${expId} results in one paragraph.`;
            const commitMessage = 'Synthetic prompt created by k6 scale test.';
            const promptExperimentIds = JSON.stringify([String(expId)]);

            const createPromptRes = http.post(
                `${BASE_URL}${API_PREFIX}/registered-models/create`,
                JSON.stringify({
                    name: promptName,
                    description: commitMessage,
                    tags: [
                        { key: 'mlflow.prompt.is_prompt', value: 'true' },
                    ],
                }),
                { ...config, tags: { name: 'create_prompt' } }
            );
            if (!validateResponse(createPromptRes, 'create_prompt')) {
                continue;
            }

            const createPromptVersionRes = http.post(
                `${BASE_URL}${API_PREFIX}/model-versions/create`,
            JSON.stringify({
                name: promptName,
                source: 'dummy-source',
                description: commitMessage,
                tags: [
                    { key: 'mlflow.prompt.is_prompt', value: 'true' },
                    { key: 'mlflow.prompt.text', value: promptText },
                    { key: 'mlflow.prompt.type', value: 'text' },
                    { key: 'mlflow.prompt.experiment_ids', value: promptExperimentIds },
                ],
            }),
                { ...config, tags: { name: 'create_prompt_version' } }
            );
            validateResponse(createPromptVersionRes, 'create_prompt_version');
        }
        for (let i = 0; i < 3; i++) {
            const runRes = http.post(`${BASE_URL}${API_PREFIX}/runs/create`, 
                JSON.stringify({ experiment_id: expId, start_time: Date.now() }), { ...config, tags: { name: 'create_run' } });
            if (!validateResponse(runRes, 'create_run')) {
                continue;
            }
            const runId = runRes.json().run?.info.run_id;
            const artifactUri = runRes.json().run?.info.artifact_uri;

            if (runId) {
                // Log 3 random metrics
                const timestamp = Date.now();
                for (let j = 1; j <= 3; j++) {
                    const metricRes = http.post(`${BASE_URL}${API_PREFIX}/runs/log-metric`,
                        JSON.stringify({ run_id: runId, key: `metric${j}`, value: Math.random(), timestamp: timestamp }),
                        { ...config, tags: { name: 'log_metric' } });
                    validateResponse(metricRes, 'log_metric');
                }
                
                // Log 5 random parameters
                for (let j = 1; j <= 5; j++) {
                    const paramRes = http.post(`${BASE_URL}${API_PREFIX}/runs/log-parameter`,
                        JSON.stringify({ run_id: runId, key: `param${j}`, value: String(Math.random()) }),
                        { ...config, tags: { name: 'log_parameter' } });
                    validateResponse(paramRes, 'log_parameter');
                }
                
                // Upload 2 large artifacts using artifact URI
                if (artifactUri) {
                    // Generate large JSON data (10KB+)
                    const largeJsonData = {
                        data: "x".repeat(10000),
                        metadata: {
                            created_at: new Date().toISOString(),
                            run_id: runId,
                            experiment_id: expId
                        }
                    };
                    const largeData = JSON.stringify(largeJsonData);
                    const artifactPaths = ["prompt.json", "model.json"];
                    
                    for (const artifactPath of artifactPaths) {
                        let uploadUrl;
                        if (artifactUri.startsWith('mlflow-artifacts:/')) {
                            uploadUrl = `${BASE_URL}/api/2.0/mlflow-artifacts/artifacts/${expId}/${runId}/artifacts/${artifactPath}`;
                        } else {
                            console.error(`Unsupported artifact URI: ${artifactUri}`);
                            continue;
                        }

                        const artifactRes = http.put(uploadUrl, largeData, {
                            headers: { ...config.headers, 'Content-Type': 'application/octet-stream' },
                            tags: { name: 'log_artifact' }
                        });
                        validateResponse(artifactRes, 'log_artifact');
                    }
                }
                
                // Update run status to FINISHED
                const updateRes = http.post(`${BASE_URL}${API_PREFIX}/runs/update`,
                    JSON.stringify({ run_id: runId, status: "FINISHED", end_time: Date.now() }),
                    { ...config, tags: { name: 'update_run_status' } });
                validateResponse(updateRes, 'update_run_status');
            }
        }
    }
    sleep(1);
}

// --- TASK: UI BROWSING (READS) ---
export function browsingScenario() {
    const config = { headers: getHeaders() };

    // List Workspaces
    const listWorkspacesRes = http.get(`${BASE_URL}${API_PREFIX_v3}/workspaces`,
        { ...config, tags: { name: 'list_workspaces' } });
    validateResponse(listWorkspacesRes, 'list_workspaces');

    // Search Prompts
    const searchPromptsRes = http.get(
        `${BASE_URL}${API_PREFIX}/registered-models/search?filter=tags.%60mlflow.prompt.is_prompt%60+%3D+%27true%27`,
        { ...config, tags: { name: 'search_prompts' } }
    );
    validateResponse(searchPromptsRes, 'search_prompts');

    // Search Experiments
    const searchExpRes = http.post(`${BASE_URL}${API_PREFIX}/experiments/search`,
        JSON.stringify({ max_results: 25 }), { ...config, tags: { name: 'search_experiments' } });
    if (!validateResponse(searchExpRes, 'search_experiments')) {
        return;
    }
    const exps = searchExpRes.json().experiments || [];

    // Iterate through 5 experiments
    const maxExps = Math.min(exps.length, 5);
    for (let i = 0; i < maxExps; i++) {
        const targetExpId = exps[i].experiment_id;

        // Get Experiment
        const getExpRes = http.get(`${BASE_URL}${API_PREFIX}/experiments/get?experiment_id=${targetExpId}`, 
            { ...config, tags: { name: 'get_experiment' } });
        if (!validateResponse(getExpRes, 'get_experiment')) {
            continue;
        }

        // Search Runs
        const runSearch = http.post(`${BASE_URL}${API_PREFIX}/runs/search`,
            JSON.stringify({ experiment_ids: [targetExpId], max_results: 10 }),
            { ...config, tags: { name: 'search_runs' } });
        if (!validateResponse(runSearch, 'search_runs')) {
            continue;
        }

        const runs = runSearch.json().runs || [];
        // Iterate through 3 runs in each experiment
        const maxRuns = Math.min(runs.length, 3);
        for (let j = 0; j < maxRuns; j++) {
            const targetRunId = runs[j].info.run_id;
            const getRunRes = http.get(`${BASE_URL}${API_PREFIX}/runs/get?run_id=${targetRunId}`, 
                { ...config, tags: { name: 'get_run' } });
            if (!validateResponse(getRunRes, 'get_run')) {
                continue;
            }
            
            // List Artifacts
            const listArtifactsRes = http.get(`${BASE_URL}${API_PREFIX}/artifacts/list?run_id=${targetRunId}`, 
                { ...config, tags: { name: 'list_artifacts' } });
            if (!validateResponse(listArtifactsRes, 'list_artifacts')) {
                continue;
            }
            
            // Fetch one random artifact
            const artifacts = listArtifactsRes.json().files || [];
            const fileArtifacts = artifacts.filter(artifact => !artifact.is_dir);
            if (fileArtifacts.length > 0) {
                const randomArtifact = fileArtifacts[Math.floor(Math.random() * fileArtifacts.length)];
                const artifactPath = randomArtifact.path || randomArtifact.file_path || randomArtifact.name;
                const fetchArtifactRes = http.get(
                    `${BASE_URL}/api/2.0/mlflow-artifacts/artifacts/${targetExpId}/${targetRunId}/artifacts/${artifactPath}`,
                    { ...config, tags: { name: 'fetch_artifact' } }
                );
                validateResponse(fetchArtifactRes, 'fetch_artifact');
            }
        }
    }
    sleep(1);
}

export function handleSummary(data) {
    const filename = `/tmp/summary_tenants-${TENANT_COUNT}_concurrency_${TOTAL_CONCURRENCY}.json`;
    
    const summary = {
        concurrency: TOTAL_CONCURRENCY,
        tenants: TENANT_COUNT,
        data: data,
        timestamp: new Date().toISOString()
    };
    
    return {
        [filename]: JSON.stringify(summary, null, 2)
    };
}
