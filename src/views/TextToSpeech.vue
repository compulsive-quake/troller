<script setup lang="ts">
import { ref, onBeforeUnmount, computed } from "vue";

defineProps<{ backendReady: boolean }>();

const text = ref("");
const selectedReference = ref("");
const selectedModel = ref("seed-uvit-whisper-small-wavenet");
const ttsVoice = ref("en-US-GuyNeural");
const speed = ref(1.0);
const diffusionSteps = ref(25);
const generating = ref(false);
const resultUrl = ref("");
const error = ref("");
const references = ref<{ id: string; filename: string }[]>([]);
const models = ref<{ id: string; name: string; type: string; version: string }[]>([]);

// Progress state
const progress = ref(0);
const stageLabel = ref("");
const device = ref("");
const deviceName = ref("");
const currentJobId = ref("");
let pollTimer: ReturnType<typeof setInterval> | null = null;

// GPU stats
const gpuStats = ref<{
  available: boolean;
  utilization: number;
  temperature: number;
  memory_used_mb: number;
  memory_total_mb: number;
  name: string;
  power_draw_w: number | null;
  power_limit_w: number | null;
} | null>(null);

const gpuMemoryPercent = computed(() => {
  if (!gpuStats.value || !gpuStats.value.memory_total_mb) return 0;
  return Math.round((gpuStats.value.memory_used_mb / gpuStats.value.memory_total_mb) * 100);
});

const tempColor = computed(() => {
  if (!gpuStats.value) return 'var(--success)';
  const t = gpuStats.value.temperature;
  if (t < 60) return 'var(--success)';
  if (t < 80) return 'var(--warning)';
  return 'var(--danger)';
});

const utilizationColor = computed(() => {
  if (!gpuStats.value) return 'var(--text-muted)';
  const u = gpuStats.value.utilization;
  if (u < 50) return 'var(--success)';
  if (u < 85) return 'var(--warning)';
  return 'var(--accent)';
});

const ttsVoices = [
  { id: "en-US-GuyNeural", label: "Guy (US Male)" },
  { id: "en-US-JennyNeural", label: "Jenny (US Female)" },
  { id: "en-US-AriaNeural", label: "Aria (US Female)" },
  { id: "en-US-DavisNeural", label: "Davis (US Male)" },
  { id: "en-GB-RyanNeural", label: "Ryan (UK Male)" },
  { id: "en-GB-SoniaNeural", label: "Sonia (UK Female)" },
  { id: "en-AU-WilliamNeural", label: "William (AU Male)" },
  { id: "en-AU-NatashaNeural", label: "Natasha (AU Female)" },
  { id: "es-ES-AlvaroNeural", label: "Alvaro (Spanish Male)" },
  { id: "fr-FR-HenriNeural", label: "Henri (French Male)" },
  { id: "de-DE-ConradNeural", label: "Conrad (German Male)" },
  { id: "ja-JP-KeitaNeural", label: "Keita (Japanese Male)" },
  { id: "zh-CN-YunxiNeural", label: "Yunxi (Chinese Male)" },
];

async function loadReferences() {
  try {
    const resp = await fetch("http://127.0.0.1:8765/api/references");
    const data = await resp.json();
    references.value = data.references;
  } catch { /* ignore */ }
}

async function loadModels() {
  try {
    const resp = await fetch("http://127.0.0.1:8765/api/models");
    const data = await resp.json();
    models.value = data.models;
  } catch { /* ignore */ }
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function generate() {
  if (!text.value.trim() || !selectedReference.value) return;

  generating.value = true;
  error.value = "";
  progress.value = 0;
  stageLabel.value = "Starting...";
  device.value = "";
  deviceName.value = "";
  gpuStats.value = null;
  if (resultUrl.value) {
    URL.revokeObjectURL(resultUrl.value);
    resultUrl.value = "";
  }

  const formData = new FormData();
  formData.append("text", text.value);
  formData.append("reference_id", selectedReference.value);
  formData.append("model_id", selectedModel.value);
  formData.append("tts_voice", ttsVoice.value);
  formData.append("speed", speed.value.toString());
  formData.append("diffusion_steps", diffusionSteps.value.toString());

  try {
    const resp = await fetch("http://127.0.0.1:8765/api/tts", {
      method: "POST",
      body: formData,
    });

    const contentType = resp.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      const blob = await resp.blob();
      resultUrl.value = URL.createObjectURL(blob);
      generating.value = false;
      return;
    }

    if (!resp.ok) {
      const data = await resp.json();
      error.value = data.error || "Generation failed";
      generating.value = false;
      return;
    }

    const data = await resp.json();
    const jobId = data.job_id;
    currentJobId.value = jobId;
    device.value = data.device;
    deviceName.value = data.device_name;

    // Poll for progress
    pollTimer = setInterval(async () => {
      try {
        const statusResp = await fetch(`http://127.0.0.1:8765/api/tts/status/${jobId}`);
        const status = await statusResp.json();

        progress.value = status.progress;
        stageLabel.value = status.stage_label;
        device.value = status.device;
        deviceName.value = status.device_name;

        // Update GPU stats if provided
        if (status.gpu) {
          gpuStats.value = status.gpu;
        }

        if (status.status === "completed") {
          stopPolling();
          const dlResp = await fetch(`http://127.0.0.1:8765/api/tts/download/${jobId}`);
          const ct = dlResp.headers.get("content-type") || "";
          if (!dlResp.ok || ct.includes("application/json")) {
            const errData = await dlResp.json();
            error.value = errData.error || "Download failed";
          } else {
            const blob = await dlResp.blob();
            resultUrl.value = URL.createObjectURL(blob);
          }
          generating.value = false;
        } else if (status.status === "failed") {
          stopPolling();
          error.value = status.error || "Generation failed";
          generating.value = false;
        } else if (status.status === "cancelled") {
          stopPolling();
          generating.value = false;
        }
      } catch {
        /* ignore transient fetch errors */
      }
    }, 500);
  } catch (e: any) {
    error.value = e.message || "Generation failed";
    generating.value = false;
  }
}

async function cancelGeneration() {
  if (!currentJobId.value) return;
  try {
    await fetch(`http://127.0.0.1:8765/api/tts/cancel/${currentJobId.value}`, { method: "POST" });
  } catch { /* ignore */ }
  stopPolling();
  generating.value = false;
}

function downloadResult() {
  if (!resultUrl.value) return;
  const a = document.createElement("a");
  a.href = resultUrl.value;
  a.download = "tts_output.wav";
  a.click();
}

onBeforeUnmount(stopPolling);

loadReferences();
loadModels();
</script>

<template>
  <div class="text-to-speech">
    <h1>Text to Speech</h1>
    <p class="description">Type text and hear it spoken in any voice</p>

    <div class="tts-layout">
      <div class="card">
        <h2>Text Input</h2>
        <div class="form-group">
          <label>What should it say?</label>
          <textarea
            v-model="text"
            class="text-input"
            placeholder="Enter text to convert to speech..."
            rows="5"
          ></textarea>
          <small class="hint">{{ text.length }} characters</small>
        </div>

        <div class="form-group">
          <label>Base TTS Voice</label>
          <select v-model="ttsVoice">
            <option v-for="v in ttsVoices" :key="v.id" :value="v.id">{{ v.label }}</option>
          </select>
          <small class="hint">The starting voice before conversion to your reference</small>
        </div>

        <div class="form-group">
          <label>Speed: {{ speed.toFixed(1) }}x</label>
          <input type="range" min="0.5" max="2.0" step="0.1" v-model.number="speed" />
        </div>
      </div>

      <div class="card">
        <h2>Voice Settings</h2>
        <div class="form-group">
          <label>Reference Voice</label>
          <select v-model="selectedReference">
            <option value="" disabled>Select reference voice...</option>
            <option v-for="r in references" :key="r.id" :value="r.id">{{ r.filename }}</option>
          </select>
          <small class="hint">The target voice to clone</small>
        </div>

        <div class="form-group">
          <label>Model</label>
          <select v-model="selectedModel">
            <option v-for="m in models" :key="m.id" :value="m.id">{{ m.name }}</option>
          </select>
        </div>

        <div class="form-group">
          <label>Diffusion Steps: {{ diffusionSteps }}</label>
          <input type="range" min="4" max="50" v-model.number="diffusionSteps" />
          <small class="hint">Lower = faster, Higher = better quality</small>
        </div>

        <button
          class="btn-primary generate-btn"
          :disabled="!backendReady || !text.trim() || !selectedReference || generating"
          @click="generate"
        >
          {{ generating ? "Generating..." : "Generate Speech" }}
        </button>
      </div>
    </div>

    <div v-if="generating" class="card progress-card">
      <div class="progress-header">
        <h2>Generating</h2>
        <div class="progress-header-right">
          <span :class="['device-badge', device]">
            {{ device === 'cuda' ? 'GPU' : 'CPU' }}: {{ deviceName }}
          </span>
          <button class="btn-cancel" @click="cancelGeneration">Cancel</button>
        </div>
      </div>

      <div class="progress-bar-track">
        <div class="progress-bar-fill" :style="{ width: progress + '%' }"></div>
      </div>
      <div class="progress-info">
        <span class="stage-label">{{ stageLabel }}</span>
        <span class="progress-pct">{{ progress }}%</span>
      </div>

      <div class="progress-steps">
        <div :class="['step', { active: progress >= 1, done: progress >= 15 }]">
          <span class="step-dot"></span>
          <span>Text to Speech</span>
        </div>
        <div :class="['step', { active: progress >= 15, done: progress >= 88 }]">
          <span class="step-dot"></span>
          <span>GPU Inference</span>
        </div>
        <div :class="['step', { active: progress >= 88, done: progress >= 100 }]">
          <span class="step-dot"></span>
          <span>Finalizing</span>
        </div>
      </div>

      <!-- GPU Stats -->
      <div v-if="gpuStats && gpuStats.available" class="gpu-panel">
        <div class="gpu-panel-header">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/></svg>
          <span>{{ gpuStats.name }}</span>
        </div>

        <div class="gpu-gauges">
          <!-- Utilization gauge -->
          <div class="gpu-gauge">
            <div class="gauge-label">
              <span>Usage</span>
              <span class="gauge-value" :style="{ color: utilizationColor }">{{ gpuStats.utilization }}%</span>
            </div>
            <div class="gauge-bar-track">
              <div class="gauge-bar-fill" :style="{ width: gpuStats.utilization + '%', background: utilizationColor }"></div>
            </div>
          </div>

          <!-- Temperature gauge -->
          <div class="gpu-gauge">
            <div class="gauge-label">
              <span>Temp</span>
              <span class="gauge-value" :style="{ color: tempColor }">{{ gpuStats.temperature }}°C</span>
            </div>
            <div class="gauge-bar-track">
              <div class="gauge-bar-fill" :style="{ width: (gpuStats.temperature / 100) * 100 + '%', background: tempColor }"></div>
            </div>
          </div>

          <!-- Memory gauge -->
          <div class="gpu-gauge">
            <div class="gauge-label">
              <span>VRAM</span>
              <span class="gauge-value">{{ gpuMemoryPercent }}%</span>
            </div>
            <div class="gauge-bar-track">
              <div class="gauge-bar-fill gpu-mem-fill" :style="{ width: gpuMemoryPercent + '%' }"></div>
            </div>
            <div class="gauge-sub">{{ gpuStats.memory_used_mb }} / {{ gpuStats.memory_total_mb }} MB</div>
          </div>

          <!-- Power gauge -->
          <div v-if="gpuStats.power_draw_w != null" class="gpu-gauge">
            <div class="gauge-label">
              <span>Power</span>
              <span class="gauge-value">{{ Math.round(gpuStats.power_draw_w!) }}W</span>
            </div>
            <div class="gauge-bar-track">
              <div
                class="gauge-bar-fill gpu-power-fill"
                :style="{ width: gpuStats.power_limit_w ? (gpuStats.power_draw_w! / gpuStats.power_limit_w * 100) + '%' : '0%' }"
              ></div>
            </div>
            <div v-if="gpuStats.power_limit_w" class="gauge-sub">/ {{ Math.round(gpuStats.power_limit_w) }}W limit</div>
          </div>
        </div>
      </div>
    </div>

    <div v-if="error" class="error-msg">{{ error }}</div>

    <div v-if="resultUrl" class="card result-card">
      <div class="result-header">
        <h2>Result</h2>
        <span v-if="deviceName" :class="['device-badge', device]">
          Generated on {{ device === 'cuda' ? 'GPU' : 'CPU' }}: {{ deviceName }}
        </span>
      </div>
      <audio :src="resultUrl" controls class="audio-player"></audio>
      <button class="btn-secondary" @click="downloadResult">Download WAV</button>
    </div>
  </div>
</template>

<style scoped>
.text-to-speech h1 {
  font-size: 28px;
  margin-bottom: 8px;
}

.description {
  color: var(--text-secondary);
  margin-bottom: 24px;
}

.tts-layout {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 24px;
}

.text-input {
  width: 100%;
  background: var(--bg-primary);
  border: 1px solid var(--border);
  color: var(--text-primary);
  padding: 12px 14px;
  border-radius: 8px;
  font-size: 14px;
  font-family: inherit;
  resize: vertical;
  min-height: 120px;
}

.text-input:focus {
  outline: none;
  border-color: var(--accent);
}

.text-input::placeholder {
  color: var(--text-muted);
}

.hint {
  display: block;
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 12px;
}

input[type="range"] {
  width: 100%;
  accent-color: var(--accent);
}

.generate-btn {
  width: 100%;
  padding: 14px 40px;
  font-size: 16px;
  margin-top: 8px;
}

/* Progress card */
.progress-card {
  margin-bottom: 16px;
}

.progress-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.progress-header h2 {
  margin-bottom: 0;
}

.progress-header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.btn-cancel {
  padding: 6px 16px;
  background: rgba(231, 76, 60, 0.15);
  color: var(--danger, #ff6b6b);
  border: 1px solid var(--danger, #ff6b6b);
  border-radius: 6px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s;
}

.btn-cancel:hover {
  background: rgba(231, 76, 60, 0.3);
}

.device-badge {
  font-size: 12px;
  font-weight: 600;
  padding: 4px 12px;
  border-radius: 6px;
  font-family: "Cascadia Code", "Fira Code", monospace;
}

.device-badge.cuda {
  background: rgba(78, 205, 196, 0.15);
  color: var(--success);
}

.device-badge.cpu {
  background: rgba(243, 156, 18, 0.15);
  color: var(--warning);
}

.progress-bar-track {
  width: 100%;
  height: 8px;
  background: var(--bg-primary);
  border-radius: 4px;
  overflow: hidden;
  margin-bottom: 8px;
}

.progress-bar-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--accent), var(--accent-hover));
  border-radius: 4px;
  transition: width 0.4s ease;
}

.progress-info {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.stage-label {
  font-size: 13px;
  color: var(--text-secondary);
}

.progress-pct {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  font-family: "Cascadia Code", "Fira Code", monospace;
}

.progress-steps {
  display: flex;
  gap: 24px;
  margin-bottom: 16px;
}

.step {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-muted);
  transition: color 0.3s;
}

.step.active {
  color: var(--text-secondary);
}

.step.done {
  color: var(--success);
}

.step-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
  transition: background 0.3s;
}

.step.active .step-dot {
  background: var(--accent);
  box-shadow: 0 0 6px var(--accent);
}

.step.done .step-dot {
  background: var(--success);
}

/* GPU Stats Panel */
.gpu-panel {
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
}

.gpu-panel-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 12px;
  font-family: "Cascadia Code", "Fira Code", monospace;
}

.gpu-panel-header svg {
  color: var(--success);
  flex-shrink: 0;
}

.gpu-gauges {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr 1fr;
  gap: 16px;
}

.gpu-gauge {
  min-width: 0;
}

.gauge-label {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 11px;
  color: var(--text-muted);
  margin-bottom: 4px;
}

.gauge-value {
  font-weight: 700;
  font-family: "Cascadia Code", "Fira Code", monospace;
  font-size: 12px;
}

.gauge-bar-track {
  height: 4px;
  background: rgba(255, 255, 255, 0.08);
  border-radius: 2px;
  overflow: hidden;
}

.gauge-bar-fill {
  height: 100%;
  border-radius: 2px;
  transition: width 0.5s ease;
}

.gpu-mem-fill {
  background: #9b59b6;
}

.gpu-power-fill {
  background: #e67e22;
}

.gauge-sub {
  font-size: 10px;
  color: var(--text-muted);
  margin-top: 2px;
  font-family: "Cascadia Code", "Fira Code", monospace;
}

/* Result */
.result-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.result-header h2 {
  margin-bottom: 0;
}

.error-msg {
  margin-top: 16px;
  padding: 12px 20px;
  background: rgba(231, 76, 60, 0.15);
  border: 1px solid var(--danger);
  border-radius: 8px;
  color: var(--danger);
}

.result-card {
  margin-top: 24px;
}

.audio-player {
  width: 100%;
  margin-bottom: 12px;
}

@media (max-width: 800px) {
  .tts-layout {
    grid-template-columns: 1fr;
  }

  .gpu-gauges {
    grid-template-columns: 1fr 1fr;
  }
}
</style>
