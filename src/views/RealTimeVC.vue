<script setup lang="ts">
import { ref, onUnmounted } from "vue";

defineProps<{ backendReady: boolean }>();

const isActive = ref(false);
const selectedReference = ref("");
const selectedModel = ref("seed-uvit-tat-xlsr-tiny");
const inputDevice = ref("");
const outputDevice = ref("");
const latency = ref(0);
const volume = ref(80);
const references = ref<{ id: string; filename: string }[]>([]);
const models = ref<{ id: string; name: string; type: string; version: string }[]>([]);
const audioDevices = ref<{ input: MediaDeviceInfo[]; output: MediaDeviceInfo[] }>({ input: [], output: [] });

const inputLevel = ref(0);
const outputLevel = ref(0);

let ws: WebSocket | null = null;
let audioContext: AudioContext | null = null;
let mediaStream: MediaStream | null = null;
let processorNode: ScriptProcessorNode | null = null;
let inputAnalyser: AnalyserNode | null = null;
let meterRafId: number | null = null;
let nextPlayTime = 0;
let chunkSendTime = 0;

async function loadDevices() {
  try {
    // Request permission first
    await navigator.mediaDevices.getUserMedia({ audio: true });
    const devices = await navigator.mediaDevices.enumerateDevices();
    audioDevices.value = {
      input: devices.filter((d) => d.kind === "audioinput"),
      output: devices.filter((d) => d.kind === "audiooutput"),
    };
    if (audioDevices.value.input.length > 0 && !inputDevice.value) {
      inputDevice.value = audioDevices.value.input[0].deviceId;
    }
    if (audioDevices.value.output.length > 0 && !outputDevice.value) {
      outputDevice.value = audioDevices.value.output[0].deviceId;
    }
  } catch (e) {
    console.error("Failed to enumerate devices:", e);
  }
}

async function loadReferences() {
  try {
    const resp = await fetch("http://127.0.0.1:8765/api/references");
    const data = await resp.json();
    references.value = data.references;
  } catch {
    // Backend not ready
  }
}

async function toggleVoiceChanger() {
  if (isActive.value) {
    stopVoiceChanger();
  } else {
    await startVoiceChanger();
  }
}

async function startVoiceChanger() {
  if (!selectedReference.value) {
    alert("Please select a reference voice first");
    return;
  }

  try {
    const sampleRate = 16000;
    audioContext = new AudioContext({ sampleRate });
    await audioContext.resume();
    nextPlayTime = 0;

    // Route output to selected device if supported
    if (outputDevice.value && "setSinkId" in audioContext) {
      try {
        await (audioContext as any).setSinkId(outputDevice.value);
      } catch (e) {
        console.warn("Could not set output device:", e);
      }
    }

    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: inputDevice.value ? { exact: inputDevice.value } : undefined,
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
        sampleRate: { ideal: sampleRate },
      },
    });

    // Connect WebSocket
    ws = new WebSocket("ws://127.0.0.1:8765/ws/realtime");

    ws.onopen = () => {
      ws!.send(
        JSON.stringify({
          reference_id: selectedReference.value,
          model_id: selectedModel.value,
          sample_rate: sampleRate,
        })
      );
    };

    ws.onmessage = async (event) => {
      if (typeof event.data === "string") {
        const msg = JSON.parse(event.data);
        if (msg.status === "ready") {
          startAudioCapture(sampleRate);
        } else if (msg.error) {
          console.error("WS error:", msg.error);
          stopVoiceChanger();
        }
      } else {
        // Received processed audio bytes - play them
        if (chunkSendTime > 0) {
          latency.value = Math.round(performance.now() - chunkSendTime);
        }
        const arrayBuffer = await event.data.arrayBuffer();
        const float32 = new Float32Array(arrayBuffer);
        playAudio(float32, sampleRate);
      }
    };

    ws.onerror = () => {
      stopVoiceChanger();
    };

    ws.onclose = () => {
      if (isActive.value) stopVoiceChanger();
    };

    isActive.value = true;
  } catch (e) {
    console.error("Failed to start:", e);
    stopVoiceChanger();
  }
}

function startAudioCapture(_sampleRate: number) {
  if (!audioContext || !mediaStream) return;

  const source = audioContext.createMediaStreamSource(mediaStream);
  const bufferSize = 4096;
  processorNode = audioContext.createScriptProcessor(bufferSize, 1, 1);

  // Input VU meter analyser
  inputAnalyser = audioContext.createAnalyser();
  inputAnalyser.fftSize = 256;
  inputAnalyser.smoothingTimeConstant = 0.5;
  source.connect(inputAnalyser);

  processorNode.onaudioprocess = (e) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const inputData = e.inputBuffer.getChannelData(0);
    chunkSendTime = performance.now();
    ws.send(inputData.buffer);
  };

  source.connect(processorNode);
  // Connect to destination so the ScriptProcessor runs (required by spec),
  // but it outputs silence — actual playback happens in playAudio()
  processorNode.connect(audioContext.destination);

  // Start meter animation loop
  startMeterLoop();
}

function startMeterLoop() {
  const inputBuf = new Uint8Array(inputAnalyser?.fftSize ?? 256);

  function tick() {
    if (inputAnalyser) {
      inputAnalyser.getByteTimeDomainData(inputBuf);
      let peak = 0;
      for (let i = 0; i < inputBuf.length; i++) {
        const v = Math.abs(inputBuf[i] - 128);
        if (v > peak) peak = v;
      }
      inputLevel.value = Math.min(100, Math.round((peak / 128) * 100));
    }
    meterRafId = requestAnimationFrame(tick);
  }
  tick();
}

function playAudio(float32Data: Float32Array, sampleRate: number) {
  if (!audioContext) return;
  const buffer = audioContext.createBuffer(1, float32Data.length, sampleRate);
  buffer.copyToChannel(float32Data, 0);
  const source = audioContext.createBufferSource();
  source.buffer = buffer;

  const gainNode = audioContext.createGain();
  gainNode.gain.value = volume.value / 100;
  source.connect(gainNode);
  gainNode.connect(audioContext.destination);

  // Schedule chunks back-to-back to avoid gaps and overlaps
  const now = audioContext.currentTime;
  if (nextPlayTime < now) {
    nextPlayTime = now;
  }
  source.start(nextPlayTime);
  nextPlayTime += buffer.duration;

  // Compute output level from the buffer
  let peak = 0;
  for (let i = 0; i < float32Data.length; i++) {
    const v = Math.abs(float32Data[i]);
    if (v > peak) peak = v;
  }
  outputLevel.value = Math.min(100, Math.round(peak * 100 * (volume.value / 100)));
}

function stopVoiceChanger() {
  isActive.value = false;
  if (meterRafId !== null) {
    cancelAnimationFrame(meterRafId);
    meterRafId = null;
  }
  if (ws) {
    ws.close();
    ws = null;
  }
  if (processorNode) {
    processorNode.disconnect();
    processorNode = null;
  }
  if (inputAnalyser) {
    inputAnalyser.disconnect();
    inputAnalyser = null;
  }
  if (mediaStream) {
    mediaStream.getTracks().forEach((t) => t.stop());
    mediaStream = null;
  }
  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }
  latency.value = 0;
  inputLevel.value = 0;
  outputLevel.value = 0;
}

async function loadModels() {
  try {
    const resp = await fetch("http://127.0.0.1:8765/api/models");
    const data = await resp.json();
    models.value = data.models;
  } catch { /* ignore */ }
}

loadDevices();
loadReferences();
loadModels();

onUnmounted(() => {
  stopVoiceChanger();
});
</script>

<template>
  <div class="realtime-vc">
    <h1>Real-Time Voice Changer</h1>
    <p class="description">Change your voice in real-time using AI voice conversion</p>

    <div class="controls-grid">
      <div class="card">
        <h2>Audio Devices</h2>
        <div class="form-group">
          <label>Input Device (Microphone)</label>
          <select v-model="inputDevice" :disabled="isActive">
            <option v-for="d in audioDevices.input" :key="d.deviceId" :value="d.deviceId">
              {{ d.label || "Microphone " + d.deviceId.slice(0, 8) }}
            </option>
          </select>
        </div>
        <div class="form-group">
          <label>Output Device (Speaker)</label>
          <select v-model="outputDevice" :disabled="isActive">
            <option v-for="d in audioDevices.output" :key="d.deviceId" :value="d.deviceId">
              {{ d.label || "Speaker " + d.deviceId.slice(0, 8) }}
            </option>
          </select>
        </div>
      </div>

      <div class="card">
        <h2>Voice Settings</h2>
        <div class="form-group">
          <label>Reference Voice</label>
          <select v-model="selectedReference">
            <option value="" disabled>Select a reference voice...</option>
            <option v-for="r in references" :key="r.id" :value="r.id">
              {{ r.filename }}
            </option>
          </select>
          <small v-if="references.length === 0" class="hint">
            Upload reference voices in the Models tab
          </small>
        </div>
        <div class="form-group">
          <label>Model</label>
          <select v-model="selectedModel" :disabled="isActive">
            <option v-for="m in models" :key="m.id" :value="m.id">{{ m.name }}</option>
          </select>
        </div>
        <div class="form-group">
          <label>Output Volume: {{ volume }}%</label>
          <input type="range" min="0" max="100" v-model.number="volume" />
        </div>
      </div>
    </div>

    <div class="action-section">
      <button
        :class="['toggle-btn', { active: isActive }]"
        :disabled="!backendReady"
        @click="toggleVoiceChanger"
      >
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path v-if="!isActive" d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3zM19 10v2a7 7 0 0 1-14 0v-2" />
          <path v-else d="M18 6L6 18M6 6l12 12" />
        </svg>
        {{ isActive ? "Stop" : "Start" }} Voice Changer
      </button>

      <div v-if="isActive" class="status-indicators">
        <div class="indicator">
          <span class="dot live"></span>
          <span>LIVE</span>
        </div>
        <div class="indicator">
          <span class="label">Latency:</span>
          <span :class="['value', { warn: latency > 200 }]">{{ latency }}ms</span>
        </div>
      </div>
    </div>

    <div v-if="isActive" class="vu-meters">
      <div class="vu-meter">
        <div class="vu-label">
          <span>Input (Mic)</span>
          <span class="vu-db">{{ inputLevel }}%</span>
        </div>
        <div class="vu-track">
          <div class="vu-fill" :class="{ hot: inputLevel > 85 }" :style="{ width: inputLevel + '%' }"></div>
        </div>
      </div>
      <div class="vu-meter">
        <div class="vu-label">
          <span>Output</span>
          <span class="vu-db">{{ outputLevel }}%</span>
        </div>
        <div class="vu-track">
          <div class="vu-fill output-fill" :class="{ hot: outputLevel > 85 }" :style="{ width: outputLevel + '%' }"></div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.realtime-vc h1 {
  font-size: 28px;
  margin-bottom: 8px;
}

.description {
  color: var(--text-secondary);
  margin-bottom: 24px;
}

.controls-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 24px;
}

.hint {
  display: block;
  margin-top: 6px;
  color: var(--text-muted);
  font-size: 12px;
}

input[type="range"] {
  width: 100%;
  accent-color: var(--accent);
}

.action-section {
  display: flex;
  align-items: center;
  gap: 24px;
}

.toggle-btn {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px 32px;
  font-size: 18px;
  font-weight: 600;
  border-radius: 12px;
  background: var(--accent);
  color: white;
  transition: all 0.3s;
}

.toggle-btn:hover:not(:disabled) {
  background: var(--accent-hover);
  transform: scale(1.02);
}

.toggle-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.toggle-btn.active {
  background: var(--danger);
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(231, 76, 60, 0.4); }
  50% { box-shadow: 0 0 0 12px rgba(231, 76, 60, 0); }
}

.status-indicators {
  display: flex;
  gap: 20px;
}

.indicator {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
}

.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--success);
}

.dot.live {
  animation: blink 1s infinite;
}

@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

.label {
  color: var(--text-secondary);
}

.value {
  color: var(--success);
  font-weight: 600;
}

.value.warn {
  color: var(--warning);
}

.vu-meters {
  margin-top: 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.vu-meter {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.vu-label {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  color: var(--text-secondary);
}

.vu-db {
  font-weight: 600;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}

.vu-track {
  height: 12px;
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
}

.vu-fill {
  height: 100%;
  border-radius: 6px;
  background: linear-gradient(90deg, var(--success) 0%, #4ecd84 70%, var(--warning) 90%);
  transition: width 0.06s linear;
}

.vu-fill.hot {
  background: linear-gradient(90deg, var(--success) 0%, var(--warning) 60%, var(--danger) 100%);
}

.output-fill {
  background: linear-gradient(90deg, var(--accent) 0%, #6ec6ff 70%, var(--warning) 90%);
}

.output-fill.hot {
  background: linear-gradient(90deg, var(--accent) 0%, var(--warning) 60%, var(--danger) 100%);
}

@media (max-width: 800px) {
  .controls-grid {
    grid-template-columns: 1fr;
  }
}
</style>
