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

async function checkStatus() {
  try {
    const resp = await fetch("http://127.0.0.1:8765/api/status");
    const data = await resp.json();
    statusInfo.value = data;
    seedVcInstalled.value = data.seed_vc_installed;
  } catch { /* ignore */ }
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

onMounted(checkStatus);
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
</style>
