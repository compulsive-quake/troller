<script setup lang="ts">
import { ref, onMounted } from "vue";
import { invoke } from "@tauri-apps/api/core";
import Sidebar from "./components/Sidebar.vue";
import RealTimeVC from "./views/RealTimeVC.vue";
import VoiceConvert from "./views/VoiceConvert.vue";
import TrainingPanel from "./views/TrainingPanel.vue";
import ModelManager from "./views/ModelManager.vue";
import SetupPanel from "./views/SetupPanel.vue";
import TextToSpeech from "./views/TextToSpeech.vue";
import PrerequisitesPanel from "./views/PrerequisitesPanel.vue";
import AudioCropper from "./views/AudioCropper.vue";

const currentView = ref("realtime");
const backendStatus = ref<"stopped" | "starting" | "running" | "error">("stopped");
const backendMessage = ref("");

async function startBackend() {
  backendStatus.value = "starting";
  try {
    const result = await invoke<string>("start_backend");
    backendMessage.value = result;
    // Wait a moment for server to start then check
    setTimeout(checkBackend, 2000);
  } catch (e: any) {
    backendStatus.value = "error";
    backendMessage.value = e;
  }
}

async function checkBackend() {
  try {
    const resp = await fetch("http://127.0.0.1:8765/api/status");
    if (resp.ok) {
      backendStatus.value = "running";
      await checkPrerequisites();
    }
  } catch {
    backendStatus.value = "error";
    backendMessage.value = "Backend not responding";
  }
}

async function checkPrerequisites() {
  try {
    const resp = await fetch("http://127.0.0.1:8765/api/prerequisites");
    if (!resp.ok) return;
    const data = await resp.json();
    const hasMissing = data.prerequisites.some(
      (p: any) => !p.installed && !p.optional
    );
    if (hasMissing) {
      currentView.value = "prerequisites";
    }
  } catch {
    /* ignore - backend may not be ready yet */
  }
}

onMounted(() => {
  startBackend();
});
</script>

<template>
  <div class="app-layout">
    <Sidebar :current-view="currentView" @navigate="currentView = $event" />
    <main class="main-content">
      <div v-if="backendStatus !== 'running'" class="backend-status">
        <div v-if="backendStatus === 'starting'" class="status-banner starting">
          Starting backend server...
        </div>
        <div v-else-if="backendStatus === 'error'" class="status-banner error">
          <span>Backend error: {{ backendMessage }}</span>
          <button @click="startBackend">Retry</button>
        </div>
        <div v-else class="status-banner stopped">
          <span>Backend stopped</span>
          <button @click="startBackend">Start</button>
        </div>
      </div>

      <RealTimeVC v-if="currentView === 'realtime'" :backend-ready="backendStatus === 'running'" />
      <VoiceConvert v-else-if="currentView === 'convert'" :backend-ready="backendStatus === 'running'" />
      <TextToSpeech v-else-if="currentView === 'tts'" :backend-ready="backendStatus === 'running'" />
      <AudioCropper v-else-if="currentView === 'cropper'" :backend-ready="backendStatus === 'running'" />
      <TrainingPanel v-else-if="currentView === 'training'" :backend-ready="backendStatus === 'running'" />
      <ModelManager v-else-if="currentView === 'models'" :backend-ready="backendStatus === 'running'" />
      <SetupPanel v-else-if="currentView === 'setup'" :backend-ready="backendStatus === 'running'" />
      <PrerequisitesPanel v-else-if="currentView === 'prerequisites'" :backend-ready="backendStatus === 'running'" />
    </main>
  </div>
</template>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

:root {
  --bg-primary: #1a1a2e;
  --bg-secondary: #16213e;
  --bg-tertiary: #0f3460;
  --accent: #e94560;
  --accent-hover: #ff6b81;
  --text-primary: #eee;
  --text-secondary: #aaa;
  --text-muted: #666;
  --border: #2a2a4a;
  --success: #4ecdc4;
  --warning: #f39c12;
  --danger: #e74c3c;
}

body {
  font-family: "Segoe UI", Inter, system-ui, -apple-system, sans-serif;
  background: var(--bg-primary);
  color: var(--text-primary);
  overflow: hidden;
  height: 100vh;
}

.app-layout {
  display: flex;
  height: 100vh;
}

.main-content {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
}

.backend-status {
  margin-bottom: 16px;
}

.status-banner {
  padding: 12px 20px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 14px;
}

.status-banner.starting {
  background: rgba(243, 156, 18, 0.15);
  border: 1px solid var(--warning);
  color: var(--warning);
}

.status-banner.error {
  background: rgba(231, 76, 60, 0.15);
  border: 1px solid var(--danger);
  color: var(--danger);
}

.status-banner.stopped {
  background: rgba(102, 102, 102, 0.15);
  border: 1px solid var(--text-muted);
  color: var(--text-secondary);
}

.status-banner button {
  padding: 6px 16px;
  border-radius: 6px;
  border: none;
  background: var(--accent);
  color: white;
  cursor: pointer;
  font-size: 13px;
}

.status-banner button:hover {
  background: var(--accent-hover);
}

button {
  cursor: pointer;
  border: none;
  border-radius: 8px;
  padding: 10px 20px;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.2s;
}

.btn-primary {
  background: var(--accent);
  color: white;
}
.btn-primary:hover {
  background: var(--accent-hover);
}
.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-secondary {
  background: var(--bg-tertiary);
  color: var(--text-primary);
  border: 1px solid var(--border);
}
.btn-secondary:hover {
  background: var(--border);
}

.card {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 16px;
}

.card h2 {
  font-size: 18px;
  margin-bottom: 16px;
  color: var(--text-primary);
}

input[type="text"],
input[type="number"],
select {
  background: var(--bg-primary);
  border: 1px solid var(--border);
  color: var(--text-primary);
  padding: 10px 14px;
  border-radius: 8px;
  font-size: 14px;
  width: 100%;
}

input:focus,
select:focus {
  outline: none;
  border-color: var(--accent);
}

label {
  display: block;
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: 6px;
}

.form-group {
  margin-bottom: 16px;
}
</style>
