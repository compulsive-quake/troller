<script setup lang="ts">
import { ref, onMounted, watch, nextTick } from "vue";

defineProps<{ backendReady: boolean }>();

const seedVcInstalled = ref(false);
const installing = ref(false);
const installLog = ref("");
const statusInfo = ref<any>(null);
const logEl = ref<HTMLElement | null>(null);

watch(installLog, () => {
  nextTick(() => {
    if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight;
  });
});

const cudaAvailable = ref(false);
const cudaEnabled = ref(false);
const cudaVersion = ref<string | null>(null);
const gpuName = ref<string | null>(null);
const cudaToggling = ref(false);
const cudaInstalling = ref(false);
const cudaInstallStage = ref("");
const cudaDownloadProgress = ref(0);
const cudaDownloadTotal = ref(0);
const cudaInstallError = ref("");

async function checkStatus() {
  try {
    const resp = await fetch("http://127.0.0.1:8765/api/status");
    const data = await resp.json();
    statusInfo.value = data;
    seedVcInstalled.value = data.seed_vc_installed;
  } catch { /* ignore */ }
}

async function fetchCudaSettings() {
  try {
    const resp = await fetch("http://127.0.0.1:8765/api/cuda");
    const data = await resp.json();
    cudaAvailable.value = data.cuda_available;
    cudaEnabled.value = data.cuda_enabled;
    cudaVersion.value = data.cuda_version;
    gpuName.value = data.gpu_name;
  } catch { /* ignore */ }
}

async function toggleCuda() {
  cudaToggling.value = true;
  try {
    const form = new FormData();
    form.append("enabled", String(!cudaEnabled.value));
    const resp = await fetch("http://127.0.0.1:8765/api/cuda", { method: "POST", body: form });
    const data = await resp.json();
    cudaEnabled.value = data.cuda_enabled;
  } catch { /* ignore */ }
  cudaToggling.value = false;
}

async function installCuda() {
  cudaInstalling.value = true;
  cudaInstallStage.value = "downloading";
  cudaDownloadProgress.value = 0;
  cudaDownloadTotal.value = 0;
  cudaInstallError.value = "";

  try {
    const resp = await fetch("http://127.0.0.1:8765/api/cuda/install", { method: "POST" });
    const reader = resp.body?.getReader();
    if (!reader) throw new Error("No response stream");

    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (!line.startsWith("data: ") || line === "data: [DONE]") continue;
        const data = JSON.parse(line.slice(6));
        if (data.stage === "downloading") {
          cudaInstallStage.value = "downloading";
          cudaDownloadProgress.value = data.downloaded;
          cudaDownloadTotal.value = data.total;
        } else if (data.stage === "launching") {
          cudaInstallStage.value = "launching";
        } else if (data.stage === "launched") {
          cudaInstallStage.value = "launched";
        } else if (data.stage === "error") {
          cudaInstallError.value = data.error;
          cudaInstallStage.value = "error";
        }
      }
    }
  } catch (e: any) {
    cudaInstallError.value = e.message;
    cudaInstallStage.value = "error";
  }

  if (cudaInstallStage.value !== "error") {
    cudaInstallStage.value = "launched";
  }
  cudaInstalling.value = false;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
  if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
}

async function installSeedVC() {
  installing.value = true;
  installLog.value = "";

  try {
    const resp = await fetch("http://127.0.0.1:8765/api/setup", { method: "POST" });

    // Check if it's a JSON response (already installed)
    const contentType = resp.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const data = await resp.json();
      installLog.value = data.status === "already_installed"
        ? `seed-vc already installed at: ${data.path}`
        : `Error: ${data.error || "Unknown error"}`;
      await checkStatus();
      return;
    }

    // Stream SSE progress
    const reader = resp.body!.getReader();
    const decoder = new TextDecoder();
    let done = false;
    while (!done) {
      const { value, done: streamDone } = await reader.read();
      done = streamDone;
      if (value) {
        const text = decoder.decode(value, { stream: true });
        for (const line of text.split("\n")) {
          if (line.startsWith("data: ")) {
            const msg = line.slice(6);
            if (msg === "DONE") {
              installLog.value += "\nInstallation complete!";
              seedVcInstalled.value = true;
            } else if (msg.startsWith("ERROR:")) {
              installLog.value += "\n" + msg;
            } else {
              installLog.value += msg + "\n";
            }
          }
        }
      }
    }
    await checkStatus();
  } catch (e: any) {
    installLog.value += `\nError: ${e.message}`;
  } finally {
    installing.value = false;
  }
}

onMounted(() => {
  checkStatus();
  fetchCudaSettings();
});
</script>

<template>
  <div class="setup-panel">
    <h1>Setup</h1>
    <p class="description">Configure Troller and install dependencies</p>

    <div class="card">
      <h2>System Status</h2>
      <div class="status-grid">
        <div class="status-item">
          <span class="status-label">Backend Server</span>
          <span :class="['status-value', backendReady ? 'ok' : 'err']">
            {{ backendReady ? "Running" : "Not Running" }}
          </span>
        </div>
        <div class="status-item">
          <span class="status-label">seed-vc Engine</span>
          <span :class="['status-value', seedVcInstalled ? 'ok' : 'err']">
            {{ seedVcInstalled ? "Installed" : "Not Installed" }}
          </span>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>CUDA / GPU Acceleration</h2>
      <div class="cuda-section">
        <div class="cuda-toggle-row">
          <div class="cuda-label">
            <span class="toggle-title">Enable CUDA</span>
            <span class="toggle-desc">Use GPU for inference (faster). Takes effect on next model load.</span>
          </div>
          <button
            class="toggle-switch"
            :class="{ active: cudaEnabled, disabled: !cudaAvailable }"
            :disabled="!cudaAvailable || cudaToggling"
            @click="toggleCuda"
          >
            <span class="toggle-knob" />
          </button>
        </div>
        <div class="cuda-status-grid">
          <div class="status-item">
            <span class="status-label">CUDA Available</span>
            <span :class="['status-value', cudaAvailable ? 'ok' : 'err']">
              {{ cudaAvailable ? "Yes" : "No" }}
            </span>
          </div>
          <div class="status-item">
            <span class="status-label">Rendering</span>
            <span :class="['status-value', cudaEnabled ? 'ok' : 'warn']">
              {{ cudaEnabled ? "GPU (CUDA)" : "CPU" }}
            </span>
          </div>
          <div v-if="gpuName" class="status-item">
            <span class="status-label">GPU</span>
            <span class="status-value">{{ gpuName }}</span>
          </div>
          <div v-if="cudaVersion" class="status-item">
            <span class="status-label">CUDA Version</span>
            <span class="status-value">{{ cudaVersion }}</span>
          </div>
        </div>
        <div v-if="!cudaAvailable" class="cuda-hint">
          <p>CUDA is not available. Install NVIDIA drivers and CUDA toolkit to enable GPU acceleration.</p>

          <div v-if="cudaInstallStage === 'downloading'" class="cuda-progress">
            <div class="progress-bar-container">
              <div
                class="progress-bar-fill"
                :style="{ width: cudaDownloadTotal ? (cudaDownloadProgress / cudaDownloadTotal * 100) + '%' : '0%' }"
              />
            </div>
            <span class="progress-text">
              Downloading... {{ formatBytes(cudaDownloadProgress) }} / {{ formatBytes(cudaDownloadTotal) }}
            </span>
          </div>

          <div v-else-if="cudaInstallStage === 'launching'" class="cuda-install-status">
            Launching installer...
          </div>

          <div v-else-if="cudaInstallStage === 'launched'" class="cuda-install-status success">
            Installer launched. Complete the setup wizard, then restart Troller to detect CUDA.
            <button class="btn-cuda-recheck" @click="fetchCudaSettings">
              Re-check CUDA
            </button>
          </div>

          <div v-else-if="cudaInstallStage === 'error'" class="cuda-install-status error">
            Error: {{ cudaInstallError }}
          </div>

          <button
            v-if="!cudaInstalling && cudaInstallStage !== 'launched'"
            class="btn-cuda-download"
            @click="installCuda"
          >
            {{ cudaInstallStage === 'error' ? 'Retry Download' : 'Install CUDA Toolkit' }}
          </button>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>seed-vc Installation</h2>
      <p class="card-desc">
        seed-vc is the AI voice conversion engine that powers Troller.
        It will be cloned from GitHub and its dependencies installed automatically.
      </p>

      <button
        v-if="!seedVcInstalled"
        class="btn-primary"
        :disabled="!backendReady || installing"
        @click="installSeedVC"
      >
        {{ installing ? "Installing..." : "Install seed-vc" }}
      </button>
      <div v-else class="installed-badge">Installed</div>

      <div v-if="installLog" ref="logEl" class="install-log">{{ installLog }}</div>
    </div>

    <div class="card">
      <h2>About</h2>
      <div class="about-info">
        <p><strong>Troller</strong> v0.1.0</p>
        <p>Real-time voice changer powered by seed-vc</p>
        <p class="tech-stack">Vue 3 + Tauri + Python + seed-vc</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.setup-panel h1 {
  font-size: 28px;
  margin-bottom: 8px;
}

.description {
  color: var(--text-secondary);
  margin-bottom: 24px;
}

.card-desc {
  color: var(--text-secondary);
  font-size: 13px;
  margin-bottom: 16px;
  line-height: 1.5;
}

.status-grid {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.status-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  background: var(--bg-primary);
  border-radius: 8px;
}

.status-label {
  font-weight: 500;
}

.status-value {
  font-weight: 600;
  font-size: 13px;
}

.status-value.ok {
  color: var(--success);
}

.status-value.err {
  color: var(--danger);
}

.installed-badge {
  display: inline-block;
  padding: 8px 20px;
  background: rgba(78, 205, 196, 0.15);
  color: var(--success);
  border-radius: 8px;
  font-weight: 600;
}

.install-log {
  margin-top: 12px;
  padding: 12px 16px;
  background: var(--bg-primary);
  border-radius: 8px;
  font-family: "Cascadia Code", "Fira Code", monospace;
  font-size: 13px;
  color: var(--text-secondary);
  white-space: pre-wrap;
  max-height: 300px;
  overflow-y: auto;
}

.about-info {
  color: var(--text-secondary);
  font-size: 14px;
  line-height: 1.8;
}

.tech-stack {
  color: var(--text-muted);
  font-size: 12px;
  margin-top: 4px;
}

.cuda-section {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.cuda-toggle-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  background: var(--bg-primary);
  border-radius: 8px;
}

.cuda-label {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.toggle-title {
  font-weight: 600;
  font-size: 14px;
}

.toggle-desc {
  font-size: 12px;
  color: var(--text-muted);
}

.toggle-switch {
  position: relative;
  width: 44px;
  height: 24px;
  border-radius: 12px;
  border: none;
  background: var(--bg-tertiary, #3a3a4a);
  cursor: pointer;
  transition: background 0.2s;
  flex-shrink: 0;
}

.toggle-switch.active {
  background: var(--success, #4ecdc4);
}

.toggle-switch.disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.toggle-knob {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: white;
  transition: transform 0.2s;
}

.toggle-switch.active .toggle-knob {
  transform: translateX(20px);
}

.cuda-status-grid {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.status-value.warn {
  color: var(--warning, #f0c040);
}

.cuda-hint {
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.5;
  padding: 12px 16px;
  background: rgba(240, 192, 64, 0.1);
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.cuda-hint p {
  margin: 0;
}

.btn-cuda-download {
  align-self: flex-start;
  padding: 8px 16px;
  background: var(--accent, #6c5ce7);
  color: white;
  border: none;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: opacity 0.2s;
}

.btn-cuda-download:hover {
  opacity: 0.85;
}

.cuda-progress {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.progress-bar-container {
  width: 100%;
  height: 8px;
  background: var(--bg-tertiary, #2a2a3a);
  border-radius: 4px;
  overflow: hidden;
}

.progress-bar-fill {
  height: 100%;
  background: var(--accent, #6c5ce7);
  border-radius: 4px;
  transition: width 0.3s ease;
}

.progress-text {
  font-size: 11px;
  color: var(--text-muted);
  font-family: "Cascadia Code", "Fira Code", monospace;
}

.cuda-install-status {
  font-size: 13px;
  color: var(--text-secondary);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.cuda-install-status.success {
  color: var(--success, #4ecdc4);
}

.cuda-install-status.error {
  color: var(--danger, #ff6b6b);
}

.btn-cuda-recheck {
  align-self: flex-start;
  padding: 6px 14px;
  background: var(--bg-tertiary, #3a3a4a);
  color: var(--text-primary, #fff);
  border: none;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: opacity 0.2s;
}

.btn-cuda-recheck:hover {
  opacity: 0.85;
}
</style>
