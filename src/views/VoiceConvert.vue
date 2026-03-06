<script setup lang="ts">
import { ref } from "vue";

defineProps<{ backendReady: boolean }>();

const sourceFile = ref<File | null>(null);
const selectedReference = ref("");
const selectedModel = ref("seed-uvit-whisper-small-wavenet");
const diffusionSteps = ref(25);
const f0Condition = ref(false);
const autoF0Adjust = ref(true);
const semiToneShift = ref(0);
const converting = ref(false);
const resultUrl = ref("");
const error = ref("");
const references = ref<{ id: string; filename: string }[]>([]);
const models = ref<{ id: string; name: string; type: string; version: string }[]>([]);

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

function onFileSelect(e: Event) {
  const input = e.target as HTMLInputElement;
  if (input.files && input.files.length > 0) {
    sourceFile.value = input.files[0];
  }
}

async function convert() {
  if (!sourceFile.value || !selectedReference.value) return;

  converting.value = true;
  error.value = "";
  resultUrl.value = "";

  const formData = new FormData();
  formData.append("source", sourceFile.value);
  formData.append("reference_id", selectedReference.value);
  formData.append("model_id", selectedModel.value);
  formData.append("diffusion_steps", diffusionSteps.value.toString());
  formData.append("f0_condition", f0Condition.value.toString());
  formData.append("auto_f0_adjust", autoF0Adjust.value.toString());
  formData.append("semi_tone_shift", semiToneShift.value.toString());

  try {
    const resp = await fetch("http://127.0.0.1:8765/api/convert", {
      method: "POST",
      body: formData,
    });

    if (!resp.ok) {
      const data = await resp.json();
      error.value = data.error || "Conversion failed";
      return;
    }

    const blob = await resp.blob();
    resultUrl.value = URL.createObjectURL(blob);
  } catch (e: any) {
    error.value = e.message || "Conversion failed";
  } finally {
    converting.value = false;
  }
}

function downloadResult() {
  if (!resultUrl.value) return;
  const a = document.createElement("a");
  a.href = resultUrl.value;
  a.download = "converted.wav";
  a.click();
}

loadReferences();
loadModels();
</script>

<template>
  <div class="voice-convert">
    <h1>Voice Conversion</h1>
    <p class="description">Convert audio files to a different voice</p>

    <div class="convert-layout">
      <div class="card">
        <h2>Source Audio</h2>
        <div class="form-group">
          <label>Audio File</label>
          <div class="file-upload">
            <input type="file" accept="audio/*" @change="onFileSelect" id="source-file" />
            <label for="source-file" class="file-label">
              {{ sourceFile ? sourceFile.name : "Choose audio file..." }}
            </label>
          </div>
        </div>

        <div class="form-group">
          <label>Reference Voice</label>
          <select v-model="selectedReference">
            <option value="" disabled>Select reference voice...</option>
            <option v-for="r in references" :key="r.id" :value="r.id">{{ r.filename }}</option>
          </select>
        </div>

        <div class="form-group">
          <label>Model</label>
          <select v-model="selectedModel">
            <option v-for="m in models" :key="m.id" :value="m.id">{{ m.name }}</option>
          </select>
        </div>
      </div>

      <div class="card">
        <h2>Settings</h2>
        <div class="form-group">
          <label>Diffusion Steps: {{ diffusionSteps }}</label>
          <input type="range" min="4" max="50" v-model.number="diffusionSteps" />
          <small class="hint">Lower = faster, Higher = better quality</small>
        </div>

        <div class="form-group checkbox-group">
          <label>
            <input type="checkbox" v-model="f0Condition" />
            F0 Conditioning (for singing)
          </label>
        </div>

        <div class="form-group checkbox-group">
          <label>
            <input type="checkbox" v-model="autoF0Adjust" />
            Auto F0 Adjust
          </label>
        </div>

        <div class="form-group">
          <label>Semitone Shift: {{ semiToneShift }}</label>
          <input type="range" min="-12" max="12" v-model.number="semiToneShift" />
        </div>
      </div>
    </div>

    <div class="action-section">
      <button
        class="btn-primary convert-btn"
        :disabled="!backendReady || !sourceFile || !selectedReference || converting"
        @click="convert"
      >
        {{ converting ? "Converting..." : "Convert Voice" }}
      </button>
    </div>

    <div v-if="error" class="error-msg">{{ error }}</div>

    <div v-if="resultUrl" class="card result-card">
      <h2>Result</h2>
      <audio :src="resultUrl" controls class="audio-player"></audio>
      <button class="btn-secondary" @click="downloadResult">Download WAV</button>
    </div>
  </div>
</template>

<style scoped>
.voice-convert h1 {
  font-size: 28px;
  margin-bottom: 8px;
}

.description {
  color: var(--text-secondary);
  margin-bottom: 24px;
}

.convert-layout {
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
  transition: border-color 0.2s;
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

input[type="range"] {
  width: 100%;
  accent-color: var(--accent);
}

.checkbox-group label {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  color: var(--text-primary);
  font-size: 14px;
}

.checkbox-group input[type="checkbox"] {
  accent-color: var(--accent);
  width: 16px;
  height: 16px;
}

.convert-btn {
  padding: 14px 40px;
  font-size: 16px;
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
  .convert-layout {
    grid-template-columns: 1fr;
  }
}
</style>
