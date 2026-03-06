<script setup lang="ts">
import { ref } from "vue";

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

async function generate() {
  if (!text.value.trim() || !selectedReference.value) return;

  generating.value = true;
  error.value = "";
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

    if (!resp.ok) {
      const data = await resp.json();
      error.value = data.error || "Generation failed";
      return;
    }

    const blob = await resp.blob();
    resultUrl.value = URL.createObjectURL(blob);
  } catch (e: any) {
    error.value = e.message || "Generation failed";
  } finally {
    generating.value = false;
  }
}

function downloadResult() {
  if (!resultUrl.value) return;
  const a = document.createElement("a");
  a.href = resultUrl.value;
  a.download = "tts_output.wav";
  a.click();
}

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
      </div>
    </div>

    <div class="action-section">
      <button
        class="btn-primary generate-btn"
        :disabled="!backendReady || !text.trim() || !selectedReference || generating"
        @click="generate"
      >
        {{ generating ? "Generating..." : "Generate Speech" }}
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

.action-section {
  margin-bottom: 16px;
}

.generate-btn {
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
  .tts-layout {
    grid-template-columns: 1fr;
  }
}
</style>
