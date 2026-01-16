import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend } from 'k6/metrics';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';
import exec from 'k6/execution';

// --- CONFIGURATION ---
const BASE_URL = __ENV.MLFLOW_URL || 'NO_URL_PROVIDED';
const API_PREFIX = '/api/2.0/mlflow';
const AUTH_TOKEN = __ENV.MLFLOW_TOKEN || 'NO_TOKEN_PROVIDED';

const TENANT_COUNT = parseInt(__ENV.TENANT_COUNT || '1');
const EXPERIMENTS_PER_TENANT = parseInt(__ENV.EXPERIMENTS_PER_TENANT || '10');
const RUNS_PER_EXPERIMENT = parseInt(__ENV.RUNS_PER_EXPERIMENT || '3');
const METRICS_PER_RUN = parseInt(__ENV.METRICS_PER_RUN || '3');
const PARAMS_PER_RUN = parseInt(__ENV.PARAMS_PER_RUN || '5');
const ARTIFACTS_PER_RUN = parseInt(__ENV.ARTIFACTS_PER_RUN || '2');
const PROMPTS_PER_EXPERIMENT = parseInt(__ENV.PROMPTS_PER_EXPERIMENT || '3');
const CONCURRENCY = parseInt(__ENV.CONCURRENCY || '100');


// Workload configuration
export const options = {
    scenarios: {
        create_experiments: {
            executor: 'shared-iterations',
            vus: Math.min(CONCURRENCY, TENANT_COUNT),
            iterations: TENANT_COUNT,
            maxDuration: __ENV.MAX_DURATION || '30m',
            gracefulStop: '1m',
            exec: 'createExperimentsScenario',
        },
    },
};

function getHeadersForTenant(tenantId) {
    const headers = { 'Content-Type': 'application/json', 'Authorization': `Bearer ${AUTH_TOKEN}` };
    headers['X-MLFLOW-WORKSPACE'] = `tenant-${tenantId}`;
    return headers;
}

function validateResponse(res) {
    return check(res, {
        'status is 2xx': (r) => r.status >= 200 && r.status < 300,
        'no error_code in response': (r) => {
            try {
                const json = r.json();
                return !json.error_code;
            } catch (e) {
                return false;
            }
        },
    });
}

export function createExperimentsScenario() {
    // Each iteration handles one tenant; k6 distributes iterations across VUs
    const tenantId = exec.scenario.iterationInTest + 1;
    const config = { headers: getHeadersForTenant(tenantId) };

    for (let i = 0; i < EXPERIMENTS_PER_TENANT; i++) {
        const expRes = http.post(
            `${BASE_URL}${API_PREFIX}/experiments/create`,
            JSON.stringify({ name: `Exp-${uuidv4()}` }),
            { ...config, tags: { name: 'create_experiment' } }
        );
        if (!validateResponse(expRes)) {
            continue;
        }
        const expId = expRes.json().experiment_id;
        if (!expId) {
            continue;
        }

        for (let p = 0; p < PROMPTS_PER_EXPERIMENT; p++) {
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
            if (!validateResponse(createPromptRes)) {
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
            validateResponse(createPromptVersionRes);
        }

        for (let r = 0; r < RUNS_PER_EXPERIMENT; r++) {
            const runRes = http.post(
                `${BASE_URL}${API_PREFIX}/runs/create`,
                JSON.stringify({ experiment_id: expId, start_time: Date.now() }),
                { ...config, tags: { name: 'create_run' } }
            );
            if (!validateResponse(runRes)) {
                continue;
            }
            const runId = runRes.json().run?.info.run_id;
            const artifactUri = runRes.json().run?.info.artifact_uri;

            if (runId) {
                const timestamp = Date.now();
                for (let m = 1; m <= METRICS_PER_RUN; m++) {
                    const metricRes = http.post(
                        `${BASE_URL}${API_PREFIX}/runs/log-metric`,
                        JSON.stringify({ run_id: runId, key: `metric${m}`, value: Math.random(), timestamp: timestamp }),
                        { ...config, tags: { name: 'log_metric' } }
                    );
                    validateResponse(metricRes);
                }

                for (let p = 1; p <= PARAMS_PER_RUN; p++) {
                    const paramRes = http.post(
                        `${BASE_URL}${API_PREFIX}/runs/log-parameter`,
                        JSON.stringify({ run_id: runId, key: `param${p}`, value: String(Math.random()) }),
                        { ...config, tags: { name: 'log_parameter' } }
                    );
                    validateResponse(paramRes);
                }

                if (artifactUri && artifactUri.startsWith('mlflow-artifacts:/')) {
                    const largeJsonData = {
                        data: 'x'.repeat(10000),
                        metadata: {
                            created_at: new Date().toISOString(),
                            run_id: runId,
                            experiment_id: expId,
                        },
                    };
                    const largeData = JSON.stringify(largeJsonData);

                    for (let a = 0; a < ARTIFACTS_PER_RUN; a++) {
                        const artifactPath = `artifact_${a + 1}.json`;
                        const uploadUrl = `${BASE_URL}/api/2.0/mlflow-artifacts/artifacts/${expId}/${runId}/artifacts/${artifactPath}`;
                        const artifactRes = http.put(uploadUrl, largeData, {
                            headers: { ...config.headers, 'Content-Type': 'application/octet-stream' },
                            tags: { name: 'log_artifact' },
                        });
                        validateResponse(artifactRes);
                    }
                }

                const updateRes = http.post(
                    `${BASE_URL}${API_PREFIX}/runs/update`,
                    JSON.stringify({ run_id: runId, status: 'FINISHED', end_time: Date.now() }),
                    { ...config, tags: { name: 'update_run_status' } }
                );
                validateResponse(updateRes);
            }
        }
    }
}
