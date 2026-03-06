<script setup lang="ts">
import { ref, onMounted } from "vue";

defineProps<{ backendReady: boolean }>();

const models = ref<any[]>([]);
const references = ref<any[]>([]);
const uploadFile = ref<File | null>(null);
const refName = ref("");
const uploading = ref(false);

async function loadModels() {
  try {
    const resp = await fetch("http://127.0.0.1:8765/api/models");
    const data = await resp.json();
    models.value = data.models;
  } catch { /* ignore */ }
}

async function loadReferences() {
  try {
    const resp = await fetch("http://127.0.0.1:8765/api/references");
    const data = await resp.json();
    references.value = data.references;
  } catch { /* ignore */ }
}

function onFileSelect(e: Event) {
  const input = e.target as HTMLInputElement;
  if (input.files && input.files.length > 0) {
    uploadFile.value = input.files[0];
    if (!refName.value) {
      refName.value = input.files[0].name.replace(/\.[^.]+$/, "");
    }
  }
}

async function uploadReference() {
  if (!uploadFile.value) return;
  uploading.value = true;

  const formData = new FormData();
  formData.append("file", uploadFile.value);
  formData.append("name", refName.value || uploadFile.value.name);

  try {
    const resp = await fetch("http://127.0.0.1:8765/api/references/upload", {
      method: "POST",
      body: formData,
    });
    if (resp.ok) {
      uploadFile.value = null;
      refName.value = "";
      await loadReferences();
    }
  } catch { /* ignore */ }
  uploading.value = false;
}

async function deleteReference(id: string) {
  try {
    await fetch(`http://127.0.0.1:8765/api/references/${id}`, { method: "DELETE" });
    await loadReferences();
  } catch { /* ignore */ }
}

onMounted(() => {
  loadModels();
  loadReferences();
});
</script>

<template>
  <div class="model-manager">
    <h1>Models & References</h1>
    <p class="description">Manage voice models and reference audio</p>

    <div class="card">
      <h2>Upload Reference Voice</h2>
      <p class="card-desc">Upload audio samples (1-30s) of the voice you want to clone</p>
      <div class="upload-row">
        <div class="form-group" style="flex: 1">
          <label>Name</label>
          <input type="text" v-model="refName" placeholder="Voice name" />
        </div>
        <div class="form-group" style="flex: 2">
          <label>Audio File</label>
          <div class="file-upload">
            <input type="file" accept="audio/*" @change="onFileSelect" id="ref-upload" />
            <label for="ref-upload" class="file-label">
              {{ uploadFile ? uploadFile.name : "Choose file..." }}
            </label>
          </div>
        </div>
        <button
          class="btn-primary upload-btn"
          :disabled="!backendReady || !uploadFile || uploading"
          @click="uploadReference"
        >
          {{ uploading ? "Uploading..." : "Upload" }}
        </button>
      </div>
    </div>

    <div class="card">
      <h2>Reference Voices</h2>
      <div v-if="references.length === 0" class="empty-state">
        No reference voices uploaded yet
      </div>
      <div v-else class="ref-list">
        <div v-for="r in references" :key="r.id" class="ref-item">
          <div class="ref-info">
            <span class="ref-name">{{ r.id }}</span>
            <span class="ref-file">{{ r.filename }}</span>
          </div>
          <button class="btn-danger-sm" @click="deleteReference(r.id)">Delete</button>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>Available Models</h2>
      <div class="models-grid">
        <div v-for="m in models" :key="m.id" class="model-card">
          <div class="model-header">
            <span class="model-name">{{ m.name }}</span>
            <span :class="['model-badge', m.version || 'custom']">{{ m.version || "custom" }}</span>
          </div>
          <div class="model-details">
            <div><strong>Type:</strong> {{ m.type }}</div>
            <div v-if="m.sample_rate"><strong>Sample Rate:</strong> {{ m.sample_rate }} Hz</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.model-manager h1 {
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
}

.upload-row {
  display: flex;
  gap: 12px;
  align-items: flex-end;
}

.file-upload input[type="file"] {
  display: none;
}

.file-label {
  display: block;
  padding: 10px 14px;
  background: var(--bg-primary);
  border: 2px dashed var(--border);
  border-radius: 8px;
  cursor: pointer;
  text-align: center;
  color: var(--text-secondary);
  font-size: 14px;
}

.file-label:hover {
  border-color: var(--accent);
}

.upload-btn {
  white-space: nowrap;
  margin-bottom: 16px;
}

.empty-state {
  color: var(--text-muted);
  text-align: center;
  padding: 24px;
}

.ref-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ref-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: var(--bg-primary);
  border-radius: 8px;
  border: 1px solid var(--border);
}

.ref-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.ref-name {
  font-weight: 600;
  font-size: 14px;
}

.ref-file {
  font-size: 12px;
  color: var(--text-muted);
}

.btn-danger-sm {
  padding: 6px 14px;
  font-size: 12px;
  background: rgba(231, 76, 60, 0.15);
  color: var(--danger);
  border: 1px solid var(--danger);
  border-radius: 6px;
}

.btn-danger-sm:hover {
  background: rgba(231, 76, 60, 0.3);
}

.models-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}

.model-card {
  padding: 16px;
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: 8px;
}

.model-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.model-name {
  font-weight: 600;
  font-size: 14px;
}

.model-badge {
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
}

.model-badge.v1 {
  background: rgba(78, 205, 196, 0.2);
  color: var(--success);
}

.model-badge.v2 {
  background: rgba(233, 69, 96, 0.2);
  color: var(--accent);
}

.model-badge.custom {
  background: rgba(243, 156, 18, 0.2);
  color: var(--warning);
}

.model-details {
  font-size: 13px;
  color: var(--text-secondary);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
</style>
