<script setup lang="ts">
import { ref, nextTick, onUnmounted } from "vue";

defineProps<{ backendReady: boolean }>();

const runName = ref("");
const baseModel = ref("seed-uvit-tat-xlsr-tiny");
const batchSize = ref(2);
const maxSteps = ref(1000);
const saveEvery = ref(500);
const trainingFiles = ref<FileList | null>(null);
const uploading = ref(false);
const training = ref(false);
const uploadStatus = ref("");
const trainingStatus = ref("");
const jobs = ref<any[]>([]);
const error = ref("");
const customModels = ref<{ id: string; name: string; base_model: string }[]>([]);
const continueFrom = ref("");

// Log viewer state
const logLines = ref<string[]>([]);
const activeLogJobId = ref<string | null>(null);
const logContainer = ref<HTMLElement | null>(null);
const autoScroll = ref(true);
let logSocket: WebSocket | null = null;

function onFilesSelect(e: Event) {
  const input = e.target as HTMLInputElement;
  trainingFiles.value = input.files;
}

async function uploadData() {
  if (!trainingFiles.value || !runName.value) return;

  uploading.value = true;
  uploadStatus.value = "";
  error.value = "";

  const formData = new FormData();
  formData.append("run_name", runName.value);
  for (let i = 0; i < trainingFiles.value.length; i++) {
    formData.append("files", trainingFiles.value[i]);
  }

  try {
    const resp = await fetch("http://127.0.0.1:8765/api/train/upload-data", {
      method: "POST",
      body: formData,
    });
    const data = await resp.json();
    if (resp.ok) {
      uploadStatus.value = `Uploaded ${data.count} files to ${data.directory}`;
    } else {
      error.value = data.error || "Upload failed";
    }
  } catch (e: any) {
    error.value = e.message;
  } finally {
    uploading.value = false;
  }
}

async function startTraining() {
  if (!runName.value) return;

  training.value = true;
  trainingStatus.value = "";
  error.value = "";

  const formData = new FormData();
  formData.append("run_name", runName.value);
  formData.append("model_base", baseModel.value);
  formData.append("batch_size", batchSize.value.toString());
  formData.append("max_steps", maxSteps.value.toString());
  formData.append("save_every", saveEvery.value.toString());

  try {
    const resp = await fetch("http://127.0.0.1:8765/api/train/start", {
      method: "POST",
      body: formData,
    });
    const data = await resp.json();
    if (resp.ok) {
      trainingStatus.value = `Training started (Job: ${data.job_id})`;
      pollJobs();
      loadCustomModels();
      connectToJobLogs(data.job_id);
    } else {
      error.value = data.error || "Failed to start training";
    }
  } catch (e: any) {
    error.value = e.message;
  } finally {
    training.value = false;
  }
}

async function pollJobs() {
  try {
    const resp = await fetch("http://127.0.0.1:8765/api/train/jobs");
    const data = await resp.json();
    jobs.value = data.jobs;
  } catch { /* ignore */ }
}

async function loadCustomModels() {
  try {
    const resp = await fetch("http://127.0.0.1:8765/api/models");
    const data = await resp.json();
    customModels.value = data.models.filter((m: any) => m.type === "custom");
  } catch { /* ignore */ }
}

function onContinueFromChange() {
  if (!continueFrom.value) return;
  const model = customModels.value.find((m) => m.id === continueFrom.value);
  if (model) {
    runName.value = model.name;
    baseModel.value = model.base_model;
  }
}

function getLogClass(line: string): string {
  if (line.startsWith("[system]")) return "log-system";
  if (line.startsWith("[stderr]")) return "log-stderr";
  return "log-stdout";
}

function connectToJobLogs(jobId: string) {
  // Disconnect previous
  if (logSocket) {
    logSocket.close();
    logSocket = null;
  }

  activeLogJobId.value = jobId;
  logLines.value = [];

  logSocket = new WebSocket(`ws://127.0.0.1:8765/ws/train/logs/${jobId}`);

  logSocket.onmessage = (event) => {
    logLines.value.push(event.data);
    if (autoScroll.value) {
      nextTick(() => {
        if (logContainer.value) {
          logContainer.value.scrollTop = logContainer.value.scrollHeight;
        }
      });
    }
  };

  logSocket.onclose = () => {
    logSocket = null;
  };

  logSocket.onerror = () => {
    // Fallback to GET endpoint
    fetchLogsViaHttp(jobId);
  };
}

async function fetchLogsViaHttp(jobId: string) {
  try {
    const resp = await fetch(`http://127.0.0.1:8765/api/train/logs/${jobId}`);
    const data = await resp.json();
    if (resp.ok) {
      logLines.value = data.lines;
      nextTick(() => {
        if (logContainer.value && autoScroll.value) {
          logContainer.value.scrollTop = logContainer.value.scrollHeight;
        }
      });
    }
  } catch { /* ignore */ }
}

function clearLog() {
  if (logSocket) {
    logSocket.close();
    logSocket = null;
  }
  activeLogJobId.value = null;
  logLines.value = [];
}

onUnmounted(() => {
  if (logSocket) {
    logSocket.close();
    logSocket = null;
  }
});

pollJobs();
loadCustomModels();
</script>

<template>
  <div class="training-panel">
    <h1>Model Training</h1>
    <p class="description">Fine-tune voice models with your own audio data</p>

    <div class="training-layout">
      <div class="card">
        <h2>Upload Training Data</h2>
        <div v-if="customModels.length > 0" class="form-group">
          <label>Continue Training Existing Model</label>
          <select v-model="continueFrom" @change="onContinueFromChange">
            <option value="">New model (start from scratch)</option>
            <option v-for="m in customModels" :key="m.id" :value="m.id">{{ m.name }}</option>
          </select>
          <small class="hint">Pick an existing model to add more training data and continue fine-tuning it.</small>
        </div>
        <div class="form-group">
          <label>Run Name</label>
          <input type="text" v-model="runName" placeholder="my_voice_model" />
        </div>
        <div class="form-group">
          <label>Audio Files (WAV, FLAC, MP3)</label>
          <div class="file-upload">
            <input type="file" accept="audio/*" multiple @change="onFilesSelect" id="training-files" />
            <label for="training-files" class="file-label">
              {{ trainingFiles ? `${trainingFiles.length} file(s) selected` : "Choose audio files..." }}
            </label>
          </div>
          <small class="hint">1-30 seconds per file, clean audio preferred</small>
        </div>
        <button
          class="btn-primary"
          :disabled="!backendReady || !trainingFiles || !runName || uploading"
          @click="uploadData"
        >
          {{ uploading ? "Uploading..." : "Upload Data" }}
        </button>
        <div v-if="uploadStatus" class="success-msg">{{ uploadStatus }}</div>
      </div>

      <div class="card">
        <h2>Training Configuration</h2>
        <div class="form-group">
          <label>Base Model</label>
          <select v-model="baseModel">
            <option value="seed-uvit-tat-xlsr-tiny">Real-time VC (Tiny)</option>
            <option value="seed-uvit-whisper-small-wavenet">Offline VC (Small)</option>
            <option value="seed-uvit-whisper-base-f0-44k">Singing VC</option>
          </select>
          <small class="hint">
            <strong>Tiny</strong> — fastest, for real-time use.
            <strong>Small</strong> — higher quality, for offline file conversion.
            <strong>Singing</strong> — preserves pitch/melody, for singing voices.
          </small>
        </div>
        <div class="form-group">
          <label>Batch Size</label>
          <input type="number" v-model.number="batchSize" min="1" max="16" />
          <small class="hint">
            Samples per training step. Higher = faster but uses more VRAM.
            Use 2-4 for 8GB GPU, 4-8 for 12GB+. Reduce if you get CUDA out-of-memory errors.
          </small>
        </div>
        <div class="form-group">
          <label>Max Steps</label>
          <input type="number" v-model.number="maxSteps" min="100" max="100000" step="100" />
          <small class="hint">
            Total training iterations. 500-1000 for a quick test, 2000-5000 for good quality,
            5000+ for best results. More data usually needs more steps.
          </small>
        </div>
        <div class="form-group">
          <label>Save Every N Steps</label>
          <input type="number" v-model.number="saveEvery" min="100" max="10000" step="100" />
          <small class="hint">
            Saves a checkpoint every N steps so you can resume or compare versions.
            The final model is always saved when training completes.
          </small>
        </div>
        <button
          class="btn-primary"
          :disabled="!backendReady || !runName || training"
          @click="startTraining"
        >
          {{ training ? "Starting..." : "Start Training" }}
        </button>
      </div>
    </div>

    <div v-if="error" class="error-msg">{{ error }}</div>
    <div v-if="trainingStatus" class="success-msg standalone">{{ trainingStatus }}</div>

    <div v-if="jobs.length > 0" class="card">
      <h2>Training Jobs</h2>
      <table class="jobs-table">
        <thead>
          <tr>
            <th>Job ID</th>
            <th>Run Name</th>
            <th>Base Model</th>
            <th>Status</th>
            <th>Logs</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="job in jobs" :key="job.id">
            <td>{{ job.id }}</td>
            <td>{{ job.run_name }}</td>
            <td>{{ job.model_base }}</td>
            <td>
              <span :class="['status-badge', job.status]">{{ job.status }}</span>
            </td>
            <td>
              <button
                class="btn-small"
                :class="{ active: activeLogJobId === job.id }"
                @click="connectToJobLogs(job.id)"
              >
                View Logs
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <button class="btn-secondary" @click="pollJobs" style="margin-top: 12px">Refresh</button>
    </div>
    <div v-if="activeLogJobId" class="card log-card">
      <div class="log-header">
        <h2>Engine Log <span class="log-job-id">{{ activeLogJobId }}</span></h2>
        <div class="log-controls">
          <label class="auto-scroll-toggle">
            <input type="checkbox" v-model="autoScroll" /> Auto-scroll
          </label>
          <button class="btn-small" @click="fetchLogsViaHttp(activeLogJobId!)">Refresh</button>
          <button class="btn-small danger" @click="clearLog">Close</button>
        </div>
      </div>
      <div ref="logContainer" class="log-container">
        <div v-if="logLines.length === 0" class="log-empty">Waiting for output...</div>
        <div v-for="(line, i) in logLines" :key="i" :class="['log-line', getLogClass(line)]">{{ line }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.training-panel h1 {
  font-size: 28px;
  margin-bottom: 8px;
}

.description {
  color: var(--text-secondary);
  margin-bottom: 24px;
}

.training-layout {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 24px;
}

.file-upload input[type="file"] {
  display: none;
}

.file-label {
  display: block;
  padding: 12px 16px;
  background: var(--bg-primary);
  border: 2px dashed var(--border);
  border-radius: 8px;
  cursor: pointer;
  text-align: center;
  color: var(--text-secondary);
}

.file-label:hover {
  border-color: var(--accent);
}

.hint {
  display: block;
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 12px;
}

.success-msg {
  margin-top: 12px;
  padding: 8px 12px;
  background: rgba(78, 205, 196, 0.15);
  border-radius: 6px;
  color: var(--success);
  font-size: 13px;
}

.success-msg.standalone {
  margin-bottom: 16px;
}

.error-msg {
  margin-bottom: 16px;
  padding: 12px 20px;
  background: rgba(231, 76, 60, 0.15);
  border: 1px solid var(--danger);
  border-radius: 8px;
  color: var(--danger);
}

.jobs-table {
  width: 100%;
  border-collapse: collapse;
}

.jobs-table th,
.jobs-table td {
  padding: 10px 12px;
  text-align: left;
  border-bottom: 1px solid var(--border);
}

.jobs-table th {
  color: var(--text-secondary);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.status-badge {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 600;
}

.status-badge.running {
  background: rgba(243, 156, 18, 0.2);
  color: var(--warning);
}

.status-badge.completed {
  background: rgba(78, 205, 196, 0.2);
  color: var(--success);
}

.status-badge.failed {
  background: rgba(231, 76, 60, 0.2);
  color: var(--danger);
}

.log-card {
  margin-top: 16px;
}

.log-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.log-header h2 {
  margin: 0;
}

.log-job-id {
  font-size: 13px;
  color: var(--text-muted);
  font-weight: 400;
  margin-left: 8px;
}

.log-controls {
  display: flex;
  align-items: center;
  gap: 8px;
}

.auto-scroll-toggle {
  font-size: 12px;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
}

.log-container {
  background: #0d1117;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  height: 360px;
  overflow-y: auto;
  font-family: "Consolas", "Monaco", "Courier New", monospace;
  font-size: 12px;
  line-height: 1.5;
}

.log-empty {
  color: var(--text-muted);
  font-style: italic;
}

.log-line {
  white-space: pre-wrap;
  word-break: break-all;
}

.log-stdout {
  color: #c9d1d9;
}

.log-stderr {
  color: #f0883e;
}

.log-system {
  color: #58a6ff;
  font-weight: 600;
}

.btn-small {
  padding: 4px 10px;
  font-size: 11px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg-secondary);
  color: var(--text-primary);
  cursor: pointer;
}

.btn-small:hover {
  background: var(--bg-primary);
}

.btn-small.active {
  border-color: var(--accent);
  color: var(--accent);
}

.btn-small.danger {
  color: var(--danger);
}

.btn-small.danger:hover {
  background: rgba(231, 76, 60, 0.1);
}

@media (max-width: 800px) {
  .training-layout {
    grid-template-columns: 1fr;
  }
}
</style>
