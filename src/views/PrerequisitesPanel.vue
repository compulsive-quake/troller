<script setup lang="ts">
import { ref, computed, onMounted, watch, nextTick } from "vue";

defineProps<{ backendReady: boolean }>();

interface Prerequisite {
  id: string;
  name: string;
  installed: boolean;
  version: string | null;
  description: string;
  install_hint: string;
  auto_install: boolean;
  optional?: boolean;
  missing?: string[];
}

const prerequisites = ref<Prerequisite[]>([]);
const loading = ref(false);
const installing = ref<Record<string, boolean>>({});
const installLogs = ref<Record<string, string>>({});
const installSuccess = ref<Record<string, boolean>>({});
const installError = ref<Record<string, boolean>>({});
const installingAll = ref(false);
const logEls = ref<Record<string, HTMLElement>>({});

watch(installLogs, () => {
  nextTick(() => {
    for (const [id, el] of Object.entries(logEls.value)) {
      if (el && installing.value[id]) el.scrollTop = el.scrollHeight;
    }
  });
}, { deep: true });

const allRequired = computed(() =>
  prerequisites.value.filter((p) => !p.optional)
);
const allRequiredInstalled = computed(() =>
  allRequired.value.length > 0 && allRequired.value.every((p) => p.installed)
);
const installedCount = computed(
  () => prerequisites.value.filter((p) => p.installed).length
);
const anyMissingInstallable = computed(() =>
  prerequisites.value.some((p) => !p.installed && p.auto_install)
);
const anyInstalling = computed(() =>
  Object.values(installing.value).some(Boolean)
);

async function checkAll() {
  loading.value = true;
  try {
    const resp = await fetch("http://127.0.0.1:8765/api/prerequisites");
    const data = await resp.json();
    prerequisites.value = data.prerequisites;
  } catch {
    /* backend not ready */
  } finally {
    loading.value = false;
  }
}

async function install(item: Prerequisite) {
  installing.value[item.id] = true;
  installLogs.value[item.id] = "";
  installSuccess.value[item.id] = false;
  installError.value[item.id] = false;
  try {
    const form = new FormData();
    form.append("item_id", item.id);
    const resp = await fetch("http://127.0.0.1:8765/api/prerequisites/install", {
      method: "POST",
      body: form,
    });
    const contentType = resp.headers.get("content-type") || "";

    if (contentType.includes("text/event-stream")) {
      // Streaming SSE response — show live progress
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
          try {
            const data = JSON.parse(line.slice(6));
            if (data.stage === "progress") {
              installLogs.value[item.id] += data.line + "\n";
            } else if (data.stage === "done") {
              installSuccess.value[item.id] = true;
              installLogs.value[item.id] += "\nInstalled successfully!";
              // Auto-update the item status
              item.installed = true;
              await checkAll();
            } else if (data.stage === "error") {
              installError.value[item.id] = true;
              installLogs.value[item.id] += `\nError: ${data.error}`;
            }
          } catch { /* skip malformed JSON */ }
        }
      }
    } else {
      // JSON response
      const data = await resp.json();
      if (resp.ok) {
        installLogs.value[item.id] = data.log || "Installed successfully";
        installSuccess.value[item.id] = true;
        await checkAll();
      } else {
        installLogs.value[item.id] = `Error: ${data.error}`;
        installError.value[item.id] = true;
      }
    }
  } catch (e: any) {
    installLogs.value[item.id] = `Error: ${e.message}`;
    installError.value[item.id] = true;
  } finally {
    installing.value[item.id] = false;
  }
}

async function installAllMissing() {
  installingAll.value = true;
  const missing = prerequisites.value.filter(
    (p) => !p.installed && p.auto_install
  );
  for (const item of missing) {
    await install(item);
  }
  installingAll.value = false;
}

onMounted(checkAll);
</script>

<template>
  <div class="prereq-panel">
    <h1>Prerequisites</h1>
    <p class="description">Check and install required tools and dependencies</p>

    <div class="summary-bar card">
      <div class="summary-left">
        <span
          :class="['summary-icon', allRequiredInstalled ? 'ok' : 'warn']"
        >
          {{ allRequiredInstalled ? "&#10003;" : "!" }}
        </span>
        <span v-if="loading">Checking...</span>
        <span v-else-if="prerequisites.length === 0">Backend not available</span>
        <span v-else>
          {{ installedCount }} / {{ prerequisites.length }} installed
        </span>
      </div>
      <div class="summary-actions">
        <button class="btn-secondary" :disabled="loading || anyInstalling" @click="checkAll">
          {{ loading ? "Checking..." : "Re-check All" }}
        </button>
        <button
          v-if="anyMissingInstallable"
          class="btn-primary"
          :disabled="loading || anyInstalling"
          @click="installAllMissing"
        >
          <span v-if="installingAll" class="btn-spinner"></span>
          {{ installingAll ? "Installing..." : "Install All Missing" }}
        </button>
      </div>
    </div>

    <div class="prereq-list">
      <div
        v-for="item in prerequisites"
        :key="item.id"
        :class="['prereq-card', 'card', { optional: item.optional, 'just-installed': installSuccess[item.id] && item.installed }]"
      >
        <div class="prereq-header">
          <div class="prereq-info">
            <span v-if="installing[item.id]" class="status-spinner"></span>
            <span v-else :class="['status-dot', item.installed ? 'ok' : item.optional ? 'opt' : 'err']" />
            <h3>{{ item.name }}</h3>
            <span v-if="item.optional" class="optional-tag">Optional</span>
            <span v-if="installing[item.id]" class="installing-tag">Installing</span>
          </div>
          <div class="prereq-status">
            <span v-if="installing[item.id]" class="installing-label">
              <span class="btn-spinner"></span>
              Installing...
            </span>
            <span v-else-if="item.installed" class="version-badge">
              {{ item.version || "Installed" }}
            </span>
            <span v-else class="not-installed">Not Installed</span>
          </div>
        </div>

        <p class="prereq-desc">{{ item.description }}</p>

        <div v-if="item.missing && item.missing.length > 0 && !item.installed" class="missing-list">
          <span class="missing-label">Missing packages:</span>
          <code v-for="pkg in item.missing" :key="pkg" class="missing-pkg">{{ pkg }}</code>
        </div>

        <!-- Install progress bar -->
        <div v-if="installing[item.id]" class="install-progress">
          <div class="install-progress-bar">
            <div class="install-progress-fill"></div>
          </div>
        </div>

        <div v-if="!item.installed && !installing[item.id]" class="prereq-actions">
          <button
            v-if="item.auto_install && backendReady"
            class="btn-primary btn-sm"
            :disabled="anyInstalling"
            @click="install(item)"
          >
            Install
          </button>
          <span class="hint">{{ item.install_hint }}</span>
        </div>

        <div v-if="installLogs[item.id] && installing[item.id]" :ref="(el) => { if (el) logEls[item.id] = el as HTMLElement }" class="install-log install-log-streaming">
          {{ installLogs[item.id] }}
        </div>

        <div v-if="installLogs[item.id] && !installing[item.id]" :class="['install-log', { 'install-log-success': installSuccess[item.id], 'install-log-error': installError[item.id] }]">
          {{ installLogs[item.id] }}
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.prereq-panel h1 {
  font-size: 28px;
  margin-bottom: 8px;
}

.description {
  color: var(--text-secondary);
  margin-bottom: 24px;
}

.summary-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}

.summary-left {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 16px;
  font-weight: 600;
}

.summary-icon {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  font-weight: 700;
}

.summary-icon.ok {
  background: rgba(78, 205, 196, 0.2);
  color: var(--success);
}

.summary-icon.warn {
  background: rgba(243, 156, 18, 0.2);
  color: var(--warning);
}

.summary-actions {
  display: flex;
  gap: 8px;
}

.prereq-list {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.prereq-card {
  margin-bottom: 12px;
  transition: border-color 0.4s, box-shadow 0.4s;
}

.prereq-card.just-installed {
  border-color: var(--success);
  box-shadow: 0 0 12px rgba(78, 205, 196, 0.15);
}

.prereq-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.prereq-info {
  display: flex;
  align-items: center;
  gap: 10px;
}

.prereq-info h3 {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
}

.status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
  transition: background 0.3s;
}

.status-dot.ok {
  background: var(--success);
}

.status-dot.err {
  background: var(--danger);
}

.status-dot.opt {
  background: var(--warning);
}

/* Spinning status indicator while installing */
.status-spinner {
  width: 10px;
  height: 10px;
  border: 2px solid rgba(243, 156, 18, 0.3);
  border-top-color: var(--warning);
  border-radius: 50%;
  flex-shrink: 0;
  animation: spin 0.8s linear infinite;
}

.installing-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(243, 156, 18, 0.15);
  color: var(--warning);
  font-weight: 500;
  animation: pulse 1.5s ease-in-out infinite;
}

.installing-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--warning);
  font-weight: 500;
}

.optional-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(243, 156, 18, 0.15);
  color: var(--warning);
  font-weight: 500;
}

.version-badge {
  font-size: 13px;
  padding: 4px 12px;
  border-radius: 6px;
  background: rgba(78, 205, 196, 0.15);
  color: var(--success);
  font-weight: 500;
  font-family: "Cascadia Code", "Fira Code", monospace;
}

.not-installed {
  font-size: 13px;
  color: var(--danger);
  font-weight: 500;
}

.prereq-desc {
  color: var(--text-secondary);
  font-size: 13px;
  margin-bottom: 10px;
  line-height: 1.4;
}

.missing-list {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
}

.missing-label {
  font-size: 12px;
  color: var(--text-muted);
}

.missing-pkg {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(231, 76, 60, 0.1);
  color: var(--danger);
}

/* Install progress bar */
.install-progress {
  margin: 8px 0;
}

.install-progress-bar {
  height: 3px;
  background: rgba(255, 255, 255, 0.08);
  border-radius: 2px;
  overflow: hidden;
}

.install-progress-fill {
  height: 100%;
  background: var(--warning);
  border-radius: 2px;
  animation: indeterminate 1.5s ease-in-out infinite;
}

.prereq-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 4px;
}

.btn-sm {
  padding: 6px 16px;
  font-size: 13px;
}

.hint {
  font-size: 12px;
  color: var(--text-muted);
  font-family: "Cascadia Code", "Fira Code", monospace;
}

.install-log {
  margin-top: 10px;
  padding: 10px 14px;
  background: var(--bg-primary);
  border-radius: 6px;
  font-family: "Cascadia Code", "Fira Code", monospace;
  font-size: 12px;
  color: var(--text-secondary);
  white-space: pre-wrap;
  max-height: 150px;
  overflow-y: auto;
  border-left: 3px solid var(--border);
}

.install-log-success {
  border-left-color: var(--success);
  color: var(--success);
}

.install-log-error {
  border-left-color: var(--danger);
  color: var(--danger);
}

.install-log-streaming {
  border-left-color: var(--warning);
  color: var(--text-secondary);
  max-height: 200px;
}

.prereq-card.optional {
  opacity: 0.85;
}

/* Spinner for buttons */
.btn-spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin-right: 6px;
  vertical-align: middle;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

@keyframes indeterminate {
  0% { width: 0%; margin-left: 0; }
  50% { width: 60%; margin-left: 20%; }
  100% { width: 0%; margin-left: 100%; }
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}
</style>
