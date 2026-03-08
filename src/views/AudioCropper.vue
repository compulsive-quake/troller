<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick, computed } from 'vue'

defineProps<{ backendReady: boolean }>()

// ── Import state ─────────────────────────────────────────────────────────────
const importMode = ref<'file' | 'youtube'>('file')
const youtubeUrl = ref('')
const ytFetching = ref(false)
const ytPhase = ref('')
const ytProgress = ref(0)
const ytTitle = ref('')
const ytError = ref('')

// ── Audio state ──────────────────────────────────────────────────────────────
const audioFile = ref<File | null>(null)
const audioFileName = ref('')
const audioBuffer = ref<AudioBuffer | null>(null)
const previewLoading = ref(false)
const previewDuration = ref(0)
const previewCurrentTime = ref(0)
const previewIsPlaying = ref(false)
const previewPlayheadPos = ref(0)
const volumeGain = ref(1.0)

// ── Multi-crop state ─────────────────────────────────────────────────────────
interface CutRegion {
  id: string
  start: number // 0-1 fraction
  end: number   // 0-1 fraction
}
const cutRegions = ref<CutRegion[]>([])
const selectedRegionId = ref<string | null>(null)
const dragState = ref<{ regionId: string; handle: 'start' | 'end' } | null>(null)

// ── Export / action state ────────────────────────────────────────────────────
const exporting = ref(false)
const exportName = ref('')
const actionMessage = ref('')
const actionError = ref('')

// ── Canvas refs ──────────────────────────────────────────────────────────────
const waveformCanvasRef = ref<HTMLCanvasElement | null>(null)
const frequencyCanvasRef = ref<HTMLCanvasElement | null>(null)
const waveformSectionRef = ref<HTMLDivElement | null>(null)

// ── Internal audio state ─────────────────────────────────────────────────────
let audioCtx: AudioContext | null = null
let analyserNode: AnalyserNode | null = null
let gainNode: GainNode | null = null
let sourceNode: AudioBufferSourceNode | null = null
let waveformPeaks: Float32Array | null = null
let frequencyData: Uint8Array | null = null
let frequencyPeaks: Float32Array | null = null
let previewStartedAt = 0
let previewOffsetSec = 0
let animFrame: number | null = null
let peakDecayFrame: number | null = null
let lastPeakDecayTime = 0

// Drag listeners
let dragMoveBound: ((e: MouseEvent) => void) | null = null
let dragEndBound: (() => void) | null = null

// ── Computed ─────────────────────────────────────────────────────────────────
const sortedCutRegions = computed(() =>
  [...cutRegions.value].sort((a, b) => a.start - b.start)
)

const keptDuration = computed(() => {
  if (!previewDuration.value) return 0
  let cutTotal = 0
  for (const r of mergedCutRegions.value) {
    cutTotal += (r.end - r.start) * previewDuration.value
  }
  return previewDuration.value - cutTotal
})

// Merge overlapping cut regions for export
const mergedCutRegions = computed(() => {
  const sorted = [...cutRegions.value].sort((a, b) => a.start - b.start)
  const merged: { start: number; end: number }[] = []
  for (const r of sorted) {
    if (merged.length > 0 && r.start <= merged[merged.length - 1].end) {
      merged[merged.length - 1].end = Math.max(merged[merged.length - 1].end, r.end)
    } else {
      merged.push({ start: r.start, end: r.end })
    }
  }
  return merged
})

// Kept regions (inverse of cut regions)
const keptRegions = computed(() => {
  const cuts = mergedCutRegions.value
  const kept: { start: number; end: number }[] = []
  let pos = 0
  for (const cut of cuts) {
    if (cut.start > pos) {
      kept.push({ start: pos, end: cut.start })
    }
    pos = cut.end
  }
  if (pos < 1) {
    kept.push({ start: pos, end: 1 })
  }
  return kept
})

// ── File import ──────────────────────────────────────────────────────────────
function onFileSelected(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  audioFile.value = file
  audioFileName.value = file.name
  exportName.value = fileNameWithoutExt(file.name)
  loadAudio(file)
}

// ── YouTube import ───────────────────────────────────────────────────────────
async function fetchYoutube() {
  if (!youtubeUrl.value.trim()) return
  ytFetching.value = true
  ytError.value = ''
  ytPhase.value = 'metadata'
  ytProgress.value = 0
  ytTitle.value = ''

  try {
    const fetchUrl = `http://127.0.0.1:8765/api/youtube/fetch?url=${encodeURIComponent(youtubeUrl.value.trim())}`

    // Use fetch instead of EventSource so we can read error responses
    const resp = await fetch(fetchUrl)
    if (!resp.ok) {
      const errData = await resp.json().catch(() => null)
      ytError.value = errData?.error || `Server returned ${resp.status}`
      return
    }

    // Parse SSE stream manually
    const reader = resp.body!.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let fileId = ''
    let fileName = ''
    let doneTitle = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // Process complete SSE messages
      const lines = buffer.split('\n')
      buffer = lines.pop() || '' // Keep incomplete line in buffer

      let currentEvent = ''
      let currentData = ''
      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7)
        } else if (line.startsWith('data: ')) {
          currentData = line.slice(6)
        } else if (line === '' && currentEvent && currentData) {
          // Complete SSE message
          try {
            const data = JSON.parse(currentData)
            if (currentEvent === 'phase') {
              ytPhase.value = data.phase
            } else if (currentEvent === 'metadata') {
              ytTitle.value = data.title || ''
              if (!exportName.value) exportName.value = data.title || 'youtube_audio'
            } else if (currentEvent === 'progress') {
              ytProgress.value = data.percent || 0
            } else if (currentEvent === 'done') {
              fileId = data.fileId
              fileName = data.fileName || 'youtube.m4a'
              doneTitle = data.title || ''
            } else if (currentEvent === 'error') {
              ytError.value = data.message || 'YouTube fetch failed'
            }
          } catch { /* ignore parse errors */ }
          currentEvent = ''
          currentData = ''
        }
      }
    }

    // Download the file if we got a fileId
    if (fileId && !ytError.value) {
      const dlResp = await fetch(`http://127.0.0.1:8765/api/youtube/download/${fileId}`)
      if (!dlResp.ok) throw new Error('Failed to download audio file')
      const blob = await dlResp.blob()
      const file = new File([blob], fileName, { type: blob.type })
      audioFile.value = file
      audioFileName.value = doneTitle || fileName
      if (!exportName.value) exportName.value = doneTitle || 'youtube_audio'
      loadAudio(file)
    }
  } catch {
    // Error already set
  } finally {
    ytFetching.value = false
  }
}

// ── Audio loading ────────────────────────────────────────────────────────────
function loadAudio(file: File) {
  destroyAudio()
  resetState()
  previewLoading.value = true
  cutRegions.value = []
  selectedRegionId.value = null

  const reader = new FileReader()
  reader.onload = (e) => {
    const arrayBuffer = e.target?.result as ArrayBuffer
    if (!arrayBuffer) { previewLoading.value = false; return }

    audioCtx = new AudioContext()
    analyserNode = audioCtx.createAnalyser()
    analyserNode.fftSize = 256
    analyserNode.smoothingTimeConstant = 0.8
    frequencyData = new Uint8Array(analyserNode.frequencyBinCount)
    frequencyPeaks = new Float32Array(analyserNode.frequencyBinCount)


    audioCtx.decodeAudioData(arrayBuffer.slice(0))
      .then((buffer) => {
        audioBuffer.value = buffer
        previewDuration.value = buffer.duration
        previewLoading.value = false
        waveformPeaks = computePeaks(buffer, 600)
        nextTick(() => drawWaveform())
      })
      .catch(() => { previewLoading.value = false })
  }
  reader.readAsArrayBuffer(file)
}

function resetState() {
  previewDuration.value = 0
  previewCurrentTime.value = 0
  previewIsPlaying.value = false
  previewPlayheadPos.value = 0
  previewOffsetSec = 0
  previewStartedAt = 0
  volumeGain.value = 1.0
  waveformPeaks = null
  actionMessage.value = ''
  actionError.value = ''
}

function destroyAudio() {
  stopPlayback()
  if (animFrame !== null) { cancelAnimationFrame(animFrame); animFrame = null }
  if (peakDecayFrame !== null) { cancelAnimationFrame(peakDecayFrame); peakDecayFrame = null }
  if (audioCtx) { audioCtx.close().catch(() => {}); audioCtx = null }
  gainNode = null
  analyserNode = null
  frequencyData = null
  frequencyPeaks = null
  removeDragListeners()
}

// ── Waveform peaks ───────────────────────────────────────────────────────────
function computePeaks(buffer: AudioBuffer, numBuckets: number): Float32Array {
  const numChannels = buffer.numberOfChannels
  const totalSamples = buffer.length
  const blockSize = Math.floor(totalSamples / numBuckets)
  const peaks = new Float32Array(numBuckets)
  for (let i = 0; i < numBuckets; i++) {
    let max = 0
    const start = i * blockSize
    const end = i === numBuckets - 1 ? totalSamples : start + blockSize
    for (let ch = 0; ch < numChannels; ch++) {
      const channelData = buffer.getChannelData(ch)
      for (let j = start; j < end; j++) {
        const abs = Math.abs(channelData[j])
        if (abs > max) max = abs
      }
    }
    peaks[i] = max
  }
  return peaks
}

// ── Waveform drawing ─────────────────────────────────────────────────────────
function drawWaveform() {
  const canvasEl = waveformCanvasRef.value
  if (!canvasEl || !waveformPeaks) return

  const dpr = window.devicePixelRatio || 1
  const rect = canvasEl.getBoundingClientRect()
  canvasEl.width = rect.width * dpr
  canvasEl.height = rect.height * dpr

  const ctx = canvasEl.getContext('2d')
  if (!ctx) return
  ctx.scale(dpr, dpr)

  const W = rect.width
  const H = rect.height
  const peaks = waveformPeaks
  const n = peaks.length

  ctx.clearRect(0, 0, W, H)
  ctx.fillStyle = 'rgba(0,0,0,0.2)'
  ctx.fillRect(0, 0, W, H)

  const barW = W / n
  const midY = H / 2

  // Pre-compute which fractions are in cut regions
  for (let i = 0; i < n; i++) {
    const x = i * barW
    const frac = i / n
    const barH = Math.min(peaks[i] * volumeGain.value * midY * 0.95, midY)

    const isCut = isInCutRegion(frac)
    if (isCut) {
      ctx.fillStyle = 'rgba(231, 76, 60, 0.25)'
    } else {
      // Purple-to-orange gradient for kept regions
      const t = frac
      const r = Math.round(155 + (231 - 155) * t)
      const g = Math.round(89 + (76 - 89) * t)
      const b = Math.round(182 + (60 - 182) * t)
      ctx.fillStyle = `rgba(${r},${g},${b},0.85)`
    }

    ctx.fillRect(x, midY - barH, Math.max(barW - 0.5, 0.5), barH * 2)
  }

  // Draw cut region overlays with hatching
  const merged = mergedCutRegions.value
  for (const region of merged) {
    const x1 = region.start * W
    const x2 = region.end * W
    ctx.fillStyle = 'rgba(231, 76, 60, 0.08)'
    ctx.fillRect(x1, 0, x2 - x1, H)

    // Top/bottom border lines for cut regions
    ctx.strokeStyle = 'rgba(231, 76, 60, 0.4)'
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.moveTo(x1, 0); ctx.lineTo(x2, 0)
    ctx.moveTo(x1, H); ctx.lineTo(x2, H)
    ctx.stroke()
  }

  // Draw playhead
  if (previewIsPlaying.value || previewPlayheadPos.value > 0) {
    const px = previewPlayheadPos.value * W
    ctx.fillStyle = '#fff'
    ctx.fillRect(px - 1, 0, 2, H)
    // Triangle
    ctx.beginPath()
    ctx.moveTo(px - 5, 0)
    ctx.lineTo(px + 5, 0)
    ctx.lineTo(px, 7)
    ctx.closePath()
    ctx.fill()
  }
}

function isInCutRegion(frac: number): boolean {
  for (const r of cutRegions.value) {
    if (frac >= r.start && frac <= r.end) return true
  }
  return false
}

// ── Frequency scope ──────────────────────────────────────────────────────────
function drawFrequencyScope() {
  const canvasEl = frequencyCanvasRef.value
  if (!canvasEl || !analyserNode || !frequencyData || !audioCtx) return

  const dpr = window.devicePixelRatio || 1
  const rect = canvasEl.getBoundingClientRect()
  canvasEl.width = rect.width * dpr
  canvasEl.height = rect.height * dpr

  const ctx = canvasEl.getContext('2d')
  if (!ctx) return
  ctx.scale(dpr, dpr)

  const W = rect.width
  const H = rect.height
  const scaleH = 14
  const specH = H - scaleH

  analyserNode.getByteFrequencyData(frequencyData)

  ctx.fillStyle = 'rgba(0, 0, 0, 0.25)'
  ctx.fillRect(0, 0, W, H)

  const bins = frequencyData.length
  const nyquist = audioCtx.sampleRate / 2
  const maxFreq = 20000
  const maxBin = Math.min(bins, Math.ceil(maxFreq / nyquist * bins))
  const barW = W / maxBin

  for (let i = 0; i < maxBin; i++) {
    const value = frequencyData[i] / 255
    const barH = value * specH
    const t = i / maxBin
    const r = Math.round(155 + (231 - 155) * t)
    const g = Math.round(89 + (76 - 89) * t)
    const b = Math.round(182 + (60 - 182) * t)
    ctx.fillStyle = `rgba(${r},${g},${b},0.85)`
    ctx.fillRect(i * barW, specH - barH, Math.max(barW - 0.5, 0.5), barH)

    // Peak indicators
    if (frequencyPeaks) {
      if (value > frequencyPeaks[i]) frequencyPeaks[i] = value
      const peakY = specH - frequencyPeaks[i] * specH
      ctx.fillStyle = 'rgba(255, 255, 255, 0.9)'
      ctx.fillRect(i * barW, peakY, Math.max(barW - 0.5, 0.5), 2)
    }
  }

  // Peak decay
  if (frequencyPeaks) {
    const now = performance.now()
    const dt = (now - lastPeakDecayTime) / 1000
    lastPeakDecayTime = now
    for (let i = 0; i < frequencyPeaks.length; i++) {
      const liveVal = i < bins ? frequencyData[i] / 255 : 0
      frequencyPeaks[i] = Math.max(liveVal, frequencyPeaks[i] - 0.12 * dt)
    }
  }

  // Frequency scale
  drawFrequencyScale(ctx, W, specH, maxFreq)
}

function drawFrequencyScale(ctx: CanvasRenderingContext2D, W: number, specH: number, maxFreq: number) {
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.12)'
  ctx.lineWidth = 1
  ctx.beginPath()
  ctx.moveTo(0, specH)
  ctx.lineTo(W, specH)
  ctx.stroke()

  const freqs = [100, 500, 1000, 2000, 5000, 10000, 20000]
  ctx.font = '9px monospace'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'top'

  for (const freq of freqs) {
    if (freq > maxFreq) continue
    const x = (freq / maxFreq) * W
    if (x < 12 || x > W - 12) continue
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.15)'
    ctx.beginPath()
    ctx.moveTo(x, specH)
    ctx.lineTo(x, specH + 3)
    ctx.stroke()
    const label = freq >= 1000 ? `${freq / 1000}k` : `${freq}`
    ctx.fillStyle = 'rgba(255, 255, 255, 0.35)'
    ctx.fillText(label, x, specH + 3)
  }
}

// ── Playback ─────────────────────────────────────────────────────────────────
function togglePlayback() {
  if (previewIsPlaying.value) {
    pausePlayback()
  } else {
    startPlayback()
  }
}

function startPlayback() {
  if (!audioCtx || !audioBuffer.value) return
  if (audioCtx.state === 'suspended') audioCtx.resume()
  if (peakDecayFrame !== null) { cancelAnimationFrame(peakDecayFrame); peakDecayFrame = null }

  const startOffset = previewOffsetSec
  const duration = previewDuration.value - startOffset
  if (duration <= 0) {
    previewOffsetSec = 0
    return startPlayback()
  }

  const source = audioCtx.createBufferSource()
  source.buffer = audioBuffer.value
  gainNode = audioCtx.createGain()
  gainNode.gain.value = volumeGain.value
  if (analyserNode) {
    source.connect(gainNode)
    gainNode.connect(analyserNode)
    analyserNode.connect(audioCtx.destination)
  } else {
    source.connect(gainNode)
    gainNode.connect(audioCtx.destination)
  }
  source.start(0, startOffset, duration)
  source.onended = () => {
    if (previewIsPlaying.value) {
      previewIsPlaying.value = false
      previewOffsetSec = 0
      previewCurrentTime.value = 0
      previewPlayheadPos.value = 0
      drawWaveform()
      startPeakDecay()
    }
  }

  sourceNode = source
  previewStartedAt = audioCtx.currentTime
  previewIsPlaying.value = true
  scheduleAnimation()
}

function pausePlayback() {
  if (!previewIsPlaying.value || !audioCtx) return
  const elapsed = audioCtx.currentTime - previewStartedAt
  previewOffsetSec = Math.min(previewOffsetSec + elapsed, previewDuration.value)
  stopPlayback()
  startPeakDecay()
}

function stopPlayback() {
  if (sourceNode) {
    try { sourceNode.onended = null; sourceNode.stop() } catch {}
    sourceNode = null
  }
  previewIsPlaying.value = false
  if (animFrame !== null) { cancelAnimationFrame(animFrame); animFrame = null }
}

function scheduleAnimation() {
  if (animFrame !== null) cancelAnimationFrame(animFrame)
  const tick = () => {
    if (!previewIsPlaying.value || !audioCtx) return
    const elapsed = audioCtx.currentTime - previewStartedAt
    const currentSec = Math.min(previewOffsetSec + elapsed, previewDuration.value)
    previewCurrentTime.value = currentSec
    previewPlayheadPos.value = previewDuration.value > 0 ? currentSec / previewDuration.value : 0
    drawWaveform()
    drawFrequencyScope()
    animFrame = requestAnimationFrame(tick)
  }
  animFrame = requestAnimationFrame(tick)
}

function startPeakDecay() {
  if (peakDecayFrame !== null) return
  lastPeakDecayTime = performance.now()
  const decay = (now: number) => {
    if (!frequencyPeaks) return
    const dt = (now - lastPeakDecayTime) / 1000
    lastPeakDecayTime = now
    let anyAlive = false
    for (let i = 0; i < frequencyPeaks.length; i++) {
      if (frequencyPeaks[i] > 0) {
        frequencyPeaks[i] = Math.max(0, frequencyPeaks[i] - 0.18 * dt)
        if (frequencyPeaks[i] > 0.001) anyAlive = true
      }
    }
    drawFrequencyScopeStatic()
    if (anyAlive) {
      peakDecayFrame = requestAnimationFrame(decay)
    } else {
      peakDecayFrame = null
    }
  }
  peakDecayFrame = requestAnimationFrame(decay)
}

function drawFrequencyScopeStatic() {
  const canvasEl = frequencyCanvasRef.value
  if (!canvasEl || !frequencyPeaks || !audioCtx) return

  const dpr = window.devicePixelRatio || 1
  const rect = canvasEl.getBoundingClientRect()
  canvasEl.width = rect.width * dpr
  canvasEl.height = rect.height * dpr
  const ctx = canvasEl.getContext('2d')
  if (!ctx) return
  ctx.scale(dpr, dpr)

  const W = rect.width
  const H = rect.height
  const scaleH = 14
  const specH = H - scaleH

  ctx.fillStyle = 'rgba(0, 0, 0, 0.25)'
  ctx.fillRect(0, 0, W, H)

  const bins = frequencyPeaks.length
  const nyquist = audioCtx.sampleRate / 2
  const maxFreq = 20000
  const maxBin = Math.min(bins, Math.ceil(maxFreq / nyquist * bins))
  const barW = W / maxBin

  for (let i = 0; i < maxBin; i++) {
    if (frequencyPeaks[i] > 0.001) {
      const peakY = specH - frequencyPeaks[i] * specH
      ctx.fillStyle = 'rgba(255, 255, 255, 0.9)'
      ctx.fillRect(i * barW, peakY, Math.max(barW - 0.5, 0.5), 2)
    }
  }

  drawFrequencyScale(ctx, W, specH, maxFreq)
}

// ── Waveform click / seek ────────────────────────────────────────────────────
function onWaveformMousedown(event: MouseEvent) {
  const canvasEl = waveformCanvasRef.value
  if (!canvasEl || !audioBuffer.value) return
  const rect = canvasEl.getBoundingClientRect()
  const frac = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width))

  // Check if clicking near a cut region handle
  const handleHit = findHandleAt(frac)
  if (handleHit) {
    startHandleDrag(event, handleHit.regionId, handleHit.handle)
    return
  }

  seekTo(frac * previewDuration.value)
}

function seekTo(timeSec: number) {
  const wasPlaying = previewIsPlaying.value
  if (wasPlaying) stopPlayback()
  previewOffsetSec = Math.max(0, Math.min(previewDuration.value, timeSec))
  previewCurrentTime.value = previewOffsetSec
  previewPlayheadPos.value = previewDuration.value > 0 ? previewOffsetSec / previewDuration.value : 0
  drawWaveform()
  if (wasPlaying) startPlayback()
}

// ── Cut region management ────────────────────────────────────────────────────
function addCutRegion() {
  // Add a new cut region centered on the current playhead position
  const center = previewPlayheadPos.value || 0.5
  const halfWidth = 0.05
  const region: CutRegion = {
    id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`,
    start: Math.max(0, center - halfWidth),
    end: Math.min(1, center + halfWidth),
  }
  cutRegions.value.push(region)
  selectedRegionId.value = region.id
  drawWaveform()
}

function removeCutRegion(id: string) {
  cutRegions.value = cutRegions.value.filter(r => r.id !== id)
  if (selectedRegionId.value === id) selectedRegionId.value = null
  drawWaveform()
}

function removeAllCutRegions() {
  cutRegions.value = []
  selectedRegionId.value = null
  drawWaveform()
}

function selectRegion(id: string) {
  selectedRegionId.value = selectedRegionId.value === id ? null : id
}

// ── Handle dragging ──────────────────────────────────────────────────────────
function findHandleAt(frac: number): { regionId: string; handle: 'start' | 'end' } | null {
  const threshold = 0.01 // 1% of total width
  for (const r of cutRegions.value) {
    if (Math.abs(frac - r.start) < threshold) return { regionId: r.id, handle: 'start' }
    if (Math.abs(frac - r.end) < threshold) return { regionId: r.id, handle: 'end' }
  }
  return null
}

function startHandleDrag(event: MouseEvent, regionId: string, handle: 'start' | 'end') {
  event.preventDefault()
  event.stopPropagation()
  dragState.value = { regionId, handle }
  selectedRegionId.value = regionId

  dragMoveBound = (e: MouseEvent) => onDragMove(e)
  dragEndBound = () => onDragEnd()
  document.addEventListener('mousemove', dragMoveBound)
  document.addEventListener('mouseup', dragEndBound)
}

function onHandleMousedown(event: MouseEvent, regionId: string, handle: 'start' | 'end') {
  startHandleDrag(event, regionId, handle)
}

function onDragMove(event: MouseEvent) {
  const canvasEl = waveformCanvasRef.value
  if (!canvasEl || !dragState.value) return
  const rect = canvasEl.getBoundingClientRect()
  const frac = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width))

  const region = cutRegions.value.find(r => r.id === dragState.value!.regionId)
  if (!region) return

  if (dragState.value.handle === 'start') {
    region.start = Math.min(frac, region.end - 0.005)
  } else {
    region.end = Math.max(frac, region.start + 0.005)
  }
  drawWaveform()
}

function onDragEnd() {
  dragState.value = null
  removeDragListeners()
}

function removeDragListeners() {
  if (dragMoveBound) { document.removeEventListener('mousemove', dragMoveBound); dragMoveBound = null }
  if (dragEndBound) { document.removeEventListener('mouseup', dragEndBound); dragEndBound = null }
}

// ── Keyboard controls ────────────────────────────────────────────────────────
function onKeydown(event: KeyboardEvent) {
  if (event.code === 'Space') {
    event.preventDefault()
    togglePlayback()
  }
}

// ── Export / crop audio ──────────────────────────────────────────────────────
function buildCroppedBuffer(): AudioBuffer | null {
  if (!audioBuffer.value || !audioCtx) return null

  const buf = audioBuffer.value
  const sampleRate = buf.sampleRate
  const numChannels = buf.numberOfChannels
  const kept = keptRegions.value

  if (kept.length === 0) return null

  // Calculate total kept samples
  let totalSamples = 0
  const segments: { startSample: number; endSample: number }[] = []
  for (const k of kept) {
    const startSample = Math.floor(k.start * buf.length)
    const endSample = Math.ceil(k.end * buf.length)
    segments.push({ startSample, endSample })
    totalSamples += endSample - startSample
  }

  if (totalSamples <= 0) return null

  const croppedBuffer = audioCtx.createBuffer(numChannels, totalSamples, sampleRate)
  for (let ch = 0; ch < numChannels; ch++) {
    const src = buf.getChannelData(ch)
    const dst = croppedBuffer.getChannelData(ch)
    let offset = 0
    for (const seg of segments) {
      dst.set(src.subarray(seg.startSample, seg.endSample), offset)
      offset += seg.endSample - seg.startSample
    }
  }

  return croppedBuffer
}

function audioBufferToWav(buffer: AudioBuffer, gain: number = 1.0): Blob {
  const numChannels = buffer.numberOfChannels
  const sampleRate = buffer.sampleRate
  const numSamples = buffer.length
  const bytesPerSample = 2
  const blockAlign = numChannels * bytesPerSample
  const dataSize = numSamples * blockAlign
  const headerSize = 44

  const arrayBuffer = new ArrayBuffer(headerSize + dataSize)
  const view = new DataView(arrayBuffer)

  const writeStr = (offset: number, str: string) => {
    for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i))
  }

  writeStr(0, 'RIFF')
  view.setUint32(4, 36 + dataSize, true)
  writeStr(8, 'WAVE')
  writeStr(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, numChannels, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * blockAlign, true)
  view.setUint16(32, blockAlign, true)
  view.setUint16(34, 16, true)
  writeStr(36, 'data')
  view.setUint32(40, dataSize, true)

  let offset = 44
  const channels: Float32Array[] = []
  for (let ch = 0; ch < numChannels; ch++) channels.push(buffer.getChannelData(ch))
  for (let i = 0; i < numSamples; i++) {
    for (let ch = 0; ch < numChannels; ch++) {
      const sample = Math.max(-1, Math.min(1, channels[ch][i] * gain))
      view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true)
      offset += 2
    }
  }

  return new Blob([arrayBuffer], { type: 'audio/wav' })
}

function downloadCroppedAudio() {
  const croppedBuffer = buildCroppedBuffer()
  if (!croppedBuffer) return

  const wavBlob = audioBufferToWav(croppedBuffer, volumeGain.value)
  const name = exportName.value || 'cropped'
  const url = URL.createObjectURL(wavBlob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${name}.wav`
  a.click()
  URL.revokeObjectURL(url)
}

// ── Set as reference voice ───────────────────────────────────────────────────
async function setAsReference() {
  const croppedBuffer = buildCroppedBuffer()
  if (!croppedBuffer) { actionError.value = 'No audio to export'; return }

  exporting.value = true
  actionMessage.value = ''
  actionError.value = ''

  try {
    const wavBlob = audioBufferToWav(croppedBuffer, volumeGain.value)
    const name = exportName.value || 'reference'
    const file = new File([wavBlob], `${name}.wav`, { type: 'audio/wav' })

    const formData = new FormData()
    formData.append('file', file)
    formData.append('name', name)

    const resp = await fetch('http://127.0.0.1:8765/api/references/upload', {
      method: 'POST',
      body: formData,
    })

    if (!resp.ok) throw new Error('Upload failed')
    const data = await resp.json()
    actionMessage.value = `Reference voice "${data.reference.filename}" saved successfully`
  } catch (err: any) {
    actionError.value = err.message || 'Failed to save reference'
  } finally {
    exporting.value = false
  }
}

// ── Use for training ─────────────────────────────────────────────────────────
async function useForTraining() {
  const croppedBuffer = buildCroppedBuffer()
  if (!croppedBuffer) { actionError.value = 'No audio to export'; return }

  exporting.value = true
  actionMessage.value = ''
  actionError.value = ''

  try {
    const wavBlob = audioBufferToWav(croppedBuffer, volumeGain.value)
    const name = exportName.value || 'training'
    const file = new File([wavBlob], `${name}.wav`, { type: 'audio/wav' })

    const formData = new FormData()
    formData.append('files', file)
    formData.append('run_name', name)

    const resp = await fetch('http://127.0.0.1:8765/api/train/upload-data', {
      method: 'POST',
      body: formData,
    })

    if (!resp.ok) throw new Error('Upload failed')
    const data = await resp.json()
    actionMessage.value = `Training data uploaded: ${data.count} file(s) to "${data.directory}"`
  } catch (err: any) {
    actionError.value = err.message || 'Failed to upload training data'
  } finally {
    exporting.value = false
  }
}

// ── Preview kept regions only ────────────────────────────────────────────────
function playKeptOnly() {
  const croppedBuffer = buildCroppedBuffer()
  if (!croppedBuffer || !audioCtx) return

  stopPlayback()
  if (audioCtx.state === 'suspended') audioCtx.resume()
  if (peakDecayFrame !== null) { cancelAnimationFrame(peakDecayFrame); peakDecayFrame = null }

  const source = audioCtx.createBufferSource()
  source.buffer = croppedBuffer
  gainNode = audioCtx.createGain()
  gainNode.gain.value = volumeGain.value
  if (analyserNode) {
    source.connect(gainNode)
    gainNode.connect(analyserNode)
    analyserNode.connect(audioCtx.destination)
  } else {
    source.connect(gainNode)
    gainNode.connect(audioCtx.destination)
  }
  source.start(0)
  source.onended = () => {
    previewIsPlaying.value = false
    startPeakDecay()
  }
  sourceNode = source
  previewIsPlaying.value = true

  // Simple animation for kept-only playback
  const startTime = audioCtx.currentTime
  const totalDur = croppedBuffer.duration
  if (animFrame !== null) cancelAnimationFrame(animFrame)
  const tick = () => {
    if (!previewIsPlaying.value || !audioCtx) return
    const elapsed = audioCtx.currentTime - startTime
    previewCurrentTime.value = Math.min(elapsed, totalDur)
    drawFrequencyScope()
    animFrame = requestAnimationFrame(tick)
  }
  animFrame = requestAnimationFrame(tick)
}

// ── Utilities ────────────────────────────────────────────────────────────────
function formatTime(seconds: number): string {
  if (!isFinite(seconds) || seconds < 0) return '0:00'
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

function fileNameWithoutExt(name: string): string {
  const lastDot = name.lastIndexOf('.')
  return lastDot > 0 ? name.substring(0, lastDot) : name
}

// ── Lifecycle ────────────────────────────────────────────────────────────────
onMounted(() => {
  lastPeakDecayTime = performance.now()
})

onUnmounted(() => {
  destroyAudio()
})
</script>

<template>
  <div class="audio-cropper">
    <h1 class="view-title">Audio Cropper</h1>
    <p class="view-subtitle">Import audio, mark regions to cut, and export clean voice for training or reference</p>

    <!-- Import section -->
    <div class="card import-card">
      <div class="import-tabs">
        <button :class="['tab-btn', { active: importMode === 'file' }]" @click="importMode = 'file'">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
          File Import
        </button>
        <button :class="['tab-btn', { active: importMode === 'youtube' }]" @click="importMode = 'youtube'">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M23.5 6.2c-.3-1-1-1.8-2-2C19.8 3.6 12 3.6 12 3.6s-7.8 0-9.5.5c-1 .3-1.8 1-2 2C0 7.9 0 12 0 12s0 4.1.5 5.8c.3 1 1 1.8 2 2 1.7.5 9.5.5 9.5.5s7.8 0 9.5-.5c1-.3 1.8-1 2-2 .5-1.7.5-5.8.5-5.8s0-4.1-.5-5.8zM9.5 15.6V8.4l6.3 3.6-6.3 3.6z"/></svg>
          YouTube Import
        </button>
      </div>

      <!-- File import -->
      <div v-if="importMode === 'file'" class="import-body">
        <label class="file-drop-zone">
          <input type="file" accept=".mp3,.wav,.flac,.m4a,.ogg" @change="onFileSelected" />
          <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          <span v-if="!audioFile">Click or drag to import MP3, WAV, FLAC, M4A, or OGG</span>
          <span v-else class="file-name">{{ audioFileName }}</span>
        </label>
      </div>

      <!-- YouTube import -->
      <div v-if="importMode === 'youtube'" class="import-body">
        <div class="yt-input-row">
          <input
            type="text"
            v-model="youtubeUrl"
            placeholder="Paste YouTube URL..."
            @keydown.enter="fetchYoutube"
            :disabled="ytFetching"
          />
          <button class="btn-primary yt-fetch-btn" @click="fetchYoutube" :disabled="ytFetching || !youtubeUrl.trim()">
            {{ ytFetching ? 'Fetching...' : 'Fetch' }}
          </button>
        </div>
        <div v-if="ytFetching" class="yt-progress">
          <div class="yt-phase">{{ ytPhase === 'metadata' ? 'Getting info...' : ytPhase === 'downloading' ? 'Downloading...' : 'Processing...' }}</div>
          <div v-if="ytPhase === 'downloading'" class="yt-progress-bar">
            <div class="yt-progress-fill" :style="{ width: ytProgress + '%' }"></div>
          </div>
          <div v-if="ytTitle" class="yt-title">{{ ytTitle }}</div>
        </div>
        <div v-if="ytError" class="yt-error">{{ ytError }}</div>
      </div>
    </div>

    <!-- Waveform & cropper -->
    <div v-if="audioBuffer" class="card waveform-card">
      <!-- Frequency scope -->
      <div class="frequency-scope-container">
        <canvas ref="frequencyCanvasRef" class="frequency-scope-canvas"></canvas>
      </div>

      <!-- Waveform -->
      <div
        ref="waveformSectionRef"
        class="waveform-section"
        tabindex="-1"
        @keydown="onKeydown"
      >
        <div class="waveform-container">
          <canvas
            ref="waveformCanvasRef"
            class="waveform-canvas"
            @mousedown="onWaveformMousedown"
          ></canvas>

          <!-- Cut region handles -->
          <template v-for="region in cutRegions" :key="region.id">
            <!-- Cut overlay -->
            <div
              class="cut-overlay"
              :class="{ selected: selectedRegionId === region.id }"
              :style="{ left: (region.start * 100) + '%', width: ((region.end - region.start) * 100) + '%' }"
              @click.stop="selectRegion(region.id)"
            >
              <button class="cut-remove-btn" @click.stop="removeCutRegion(region.id)" title="Remove this cut region">x</button>
            </div>

            <!-- Start handle -->
            <div
              class="crop-handle crop-handle-start"
              :class="{ selected: selectedRegionId === region.id }"
              :style="{ left: (region.start * 100) + '%' }"
              @mousedown.stop="onHandleMousedown($event, region.id, 'start')"
            >
              <div class="crop-handle-line"></div>
              <div class="crop-handle-grip">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
              </div>
              <div class="crop-handle-label">{{ formatTime(region.start * previewDuration) }}</div>
            </div>

            <!-- End handle -->
            <div
              class="crop-handle crop-handle-end"
              :class="{ selected: selectedRegionId === region.id }"
              :style="{ left: (region.end * 100) + '%' }"
              @mousedown.stop="onHandleMousedown($event, region.id, 'end')"
            >
              <div class="crop-handle-line"></div>
              <div class="crop-handle-grip">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M16 5v14L5 12z"/></svg>
              </div>
              <div class="crop-handle-label">{{ formatTime(region.end * previewDuration) }}</div>
            </div>
          </template>
        </div>
      </div>

      <!-- Controls row -->
      <div class="controls-row">
        <button class="play-btn" @click="togglePlayback" :title="previewIsPlaying ? 'Pause' : 'Play'">
          <svg v-if="!previewIsPlaying" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
          <svg v-else xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
        </button>

        <div class="time-info">
          <span class="current-time">{{ formatTime(previewCurrentTime) }}</span>
          <span class="time-sep">/</span>
          <span class="duration">{{ formatTime(previewDuration) }}</span>
        </div>

        <button class="btn-secondary add-cut-btn" @click="addCutRegion" title="Add a cut region at playhead position">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 15h2V7c0-1.1-.9-2-2-2H9v2h8v8zM7 17V1H5v4H1v2h4v10c0 1.1.9 2 2 2h10v4h2v-4h4v-2H7z"/></svg>
          Add Cut Region
        </button>

        <button v-if="cutRegions.length > 0" class="btn-secondary clear-cuts-btn" @click="removeAllCutRegions" title="Remove all cut regions">
          Clear All
        </button>

        <div v-if="cutRegions.length > 0" class="cut-info">
          <span>{{ cutRegions.length }} cut region{{ cutRegions.length > 1 ? 's' : '' }}</span>
          <span class="kept-duration">Kept: {{ formatTime(keptDuration) }}</span>
        </div>
      </div>

      <!-- Cut regions list -->
      <div v-if="cutRegions.length > 0" class="cut-regions-list">
        <div
          v-for="(region, index) in sortedCutRegions"
          :key="region.id"
          :class="['cut-region-item', { selected: selectedRegionId === region.id }]"
          @click="selectRegion(region.id)"
        >
          <span class="cut-label">Cut {{ index + 1 }}</span>
          <span class="cut-range">{{ formatTime(region.start * previewDuration) }} – {{ formatTime(region.end * previewDuration) }}</span>
          <span class="cut-duration">({{ formatTime((region.end - region.start) * previewDuration) }})</span>
          <button class="cut-delete-btn" @click.stop="removeCutRegion(region.id)" title="Remove">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>
      </div>

      <!-- Preview kept audio -->
      <div v-if="cutRegions.length > 0" class="preview-kept-row">
        <button class="btn-secondary preview-kept-btn" @click="playKeptOnly" :disabled="previewIsPlaying">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
          Preview Kept Audio
        </button>
      </div>

      <!-- Hints -->
      <div class="hints">
        Space: play/pause · Click waveform: seek · Drag red handles: adjust cut regions · Add Cut Region to mark parts to remove
      </div>
    </div>

    <!-- Loading state -->
    <div v-if="previewLoading" class="card loading-card">
      <div class="loading-spinner"></div>
      <span>Analyzing audio...</span>
    </div>

    <!-- Export / action section -->
    <div v-if="audioBuffer" class="card export-card">
      <h2>Export & Use</h2>

      <div class="form-group">
        <label>Name</label>
        <input type="text" v-model="exportName" placeholder="Name for export..." />
      </div>

      <div class="export-actions">
        <button class="btn-primary" @click="downloadCroppedAudio" :disabled="exporting">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          Download WAV
        </button>

        <button class="btn-reference" @click="setAsReference" :disabled="exporting || !backendReady">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>
          Set as Reference Voice
        </button>

        <button class="btn-training" @click="useForTraining" :disabled="exporting || !backendReady">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a7 7 0 0 1 7 7c0 2.4-1.2 4.5-3 5.7V17a2 2 0 0 1-2 2h-4a2 2 0 0 1-2-2v-2.3C6.2 13.5 5 11.4 5 9a7 7 0 0 1 7-7z"/><path d="M9 22h6"/></svg>
          Use for Training
        </button>
      </div>

      <div v-if="actionMessage" class="action-success">{{ actionMessage }}</div>
      <div v-if="actionError" class="action-error">{{ actionError }}</div>
    </div>
  </div>
</template>

<style scoped>
.audio-cropper {
  max-width: 1000px;
}

.view-title {
  font-size: 24px;
  font-weight: 700;
  margin-bottom: 4px;
}

.view-subtitle {
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: 20px;
}

/* ── Import card ─────────────────────────────────────────────────────────── */
.import-card {
  padding: 0;
  overflow: hidden;
}

.import-tabs {
  display: flex;
  border-bottom: 1px solid var(--border);
}

.tab-btn {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 12px 16px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 14px;
  border: none;
  border-bottom: 2px solid transparent;
  transition: all 0.2s;
}

.tab-btn:hover {
  color: var(--text-primary);
  background: rgba(255,255,255,0.03);
}

.tab-btn.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

.tab-btn svg {
  flex-shrink: 0;
}

.import-body {
  padding: 20px;
}

/* File drop zone */
.file-drop-zone {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 32px;
  border: 2px dashed var(--border);
  border-radius: 8px;
  cursor: pointer;
  color: var(--text-secondary);
  font-size: 14px;
  transition: all 0.2s;
}

.file-drop-zone:hover {
  border-color: var(--accent);
  color: var(--text-primary);
  background: rgba(233, 69, 96, 0.05);
}

.file-drop-zone input[type="file"] {
  display: none;
}

.file-name {
  color: var(--accent);
  font-weight: 600;
}

/* YouTube */
.yt-input-row {
  display: flex;
  gap: 8px;
}

.yt-input-row input {
  flex: 1;
}

.yt-fetch-btn {
  flex-shrink: 0;
  padding: 10px 20px;
}

.yt-progress {
  margin-top: 12px;
}

.yt-phase {
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: 6px;
}

.yt-progress-bar {
  height: 4px;
  background: rgba(255, 255, 255, 0.1);
  border-radius: 2px;
  overflow: hidden;
  margin-bottom: 6px;
}

.yt-progress-fill {
  height: 100%;
  background: #ff4444;
  transition: width 0.3s;
  border-radius: 2px;
}

.yt-title {
  font-size: 13px;
  color: var(--text-primary);
  font-weight: 500;
}

.yt-error {
  margin-top: 8px;
  padding: 8px 12px;
  background: rgba(231, 76, 60, 0.15);
  border: 1px solid rgba(231, 76, 60, 0.3);
  border-radius: 6px;
  color: var(--danger);
  font-size: 13px;
}

/* ── Waveform card ───────────────────────────────────────────────────────── */
.waveform-card {
  padding: 16px;
}

.frequency-scope-container {
  width: 100%;
  height: 62px;
  margin-bottom: 8px;
  border-radius: 8px;
  overflow: hidden;
  background: rgba(0, 0, 0, 0.25);
  border: 1px solid rgba(255, 255, 255, 0.08);
}

.frequency-scope-canvas {
  display: block;
  width: 100%;
  height: 100%;
}

.waveform-section {
  outline: none;
}

.waveform-container {
  position: relative;
  width: 100%;
  height: 120px;
  border-radius: 8px;
  overflow: visible;
  cursor: crosshair;
  background: rgba(0, 0, 0, 0.25);
  border: 1px solid rgba(255, 255, 255, 0.08);
}

.waveform-canvas {
  display: block;
  width: 100%;
  height: 100%;
  border-radius: 8px;
}

/* ── Cut overlays ────────────────────────────────────────────────────────── */
.cut-overlay {
  position: absolute;
  top: 0;
  height: 100%;
  background: rgba(231, 76, 60, 0.15);
  border-top: 2px solid rgba(231, 76, 60, 0.5);
  border-bottom: 2px solid rgba(231, 76, 60, 0.5);
  z-index: 2;
  cursor: pointer;
  transition: background 0.15s;
}

.cut-overlay:hover,
.cut-overlay.selected {
  background: rgba(231, 76, 60, 0.25);
}

.cut-remove-btn {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: rgba(231, 76, 60, 0.8);
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s;
  padding: 0;
  border: none;
  line-height: 1;
}

.cut-overlay:hover .cut-remove-btn {
  opacity: 1;
}

/* ── Crop handles ────────────────────────────────────────────────────────── */
.crop-handle {
  position: absolute;
  top: -6px;
  bottom: -6px;
  width: 0;
  z-index: 5;
  cursor: ew-resize;
  user-select: none;
}

.crop-handle-line {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  width: 2px;
  transform: translateX(-50%);
  background: rgba(231, 76, 60, 0.9);
  box-shadow: 0 0 4px rgba(231, 76, 60, 0.5);
  transition: background 0.15s, box-shadow 0.15s;
}

.crop-handle.selected .crop-handle-line {
  background: #f39c12;
  box-shadow: 0 0 8px rgba(243, 156, 18, 0.7);
}

.crop-handle-grip {
  position: absolute;
  top: 50%;
  transform: translate(-50%, -50%);
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: rgba(231, 76, 60, 0.9);
  border: 2px solid rgba(255, 255, 255, 0.8);
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);
  transition: background 0.15s, border-color 0.15s, transform 0.15s;
}

.crop-handle.selected .crop-handle-grip {
  background: #f39c12;
  border-color: #f39c12;
}

.crop-handle-grip svg {
  width: 10px;
  height: 10px;
  color: #fff;
}

.crop-handle-grip:hover {
  transform: translate(-50%, -50%) scale(1.15);
}

.crop-handle-label {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 50%;
  transform: translateX(-50%);
  background: rgba(0, 0, 0, 0.75);
  color: rgba(255, 255, 255, 0.9);
  font-size: 0.7rem;
  font-family: monospace;
  padding: 2px 5px;
  border-radius: 4px;
  white-space: nowrap;
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.15s;
}

.crop-handle:hover .crop-handle-label,
.crop-handle.selected .crop-handle-label {
  opacity: 1;
}

/* ── Controls row ────────────────────────────────────────────────────────── */
.controls-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  flex-wrap: wrap;
}

.play-btn {
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(46, 204, 113, 0.2);
  border: 1px solid rgba(46, 204, 113, 0.5);
  border-radius: 50%;
  cursor: pointer;
  transition: all 0.2s;
  color: #2ecc71;
  padding: 0;
}

.play-btn svg {
  width: 18px;
  height: 18px;
  color: #2ecc71;
}

.play-btn:hover {
  background: rgba(46, 204, 113, 0.35);
  border-color: #2ecc71;
  transform: scale(1.08);
}

.time-info {
  display: flex;
  align-items: center;
  gap: 3px;
  font-family: monospace;
  font-size: 0.85rem;
}

.current-time {
  color: #2ecc71;
  font-weight: 600;
}

.time-sep {
  color: rgba(255, 255, 255, 0.3);
}

.duration {
  color: rgba(255, 255, 255, 0.5);
}

.add-cut-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  font-size: 13px;
}

.add-cut-btn svg {
  color: var(--danger);
}

.clear-cuts-btn {
  padding: 6px 14px;
  font-size: 13px;
  color: var(--danger);
  background: rgba(231, 76, 60, 0.1);
  border-color: rgba(231, 76, 60, 0.3);
}

.clear-cuts-btn:hover {
  background: rgba(231, 76, 60, 0.2);
}

.cut-info {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: var(--text-secondary);
  margin-left: auto;
}

.kept-duration {
  color: #2ecc71;
  font-weight: 600;
  font-family: monospace;
}

/* ── Cut regions list ────────────────────────────────────────────────────── */
.cut-regions-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.cut-region-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 10px;
  background: rgba(231, 76, 60, 0.08);
  border: 1px solid rgba(231, 76, 60, 0.2);
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}

.cut-region-item:hover,
.cut-region-item.selected {
  background: rgba(231, 76, 60, 0.15);
  border-color: rgba(231, 76, 60, 0.4);
}

.cut-label {
  color: var(--danger);
  font-weight: 600;
}

.cut-range {
  color: var(--text-primary);
  font-family: monospace;
}

.cut-duration {
  color: var(--text-muted);
  font-family: monospace;
}

.cut-delete-btn {
  width: 18px;
  height: 18px;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  color: var(--danger);
  border: none;
  border-radius: 50%;
  cursor: pointer;
  opacity: 0.5;
  transition: opacity 0.15s;
}

.cut-delete-btn:hover {
  opacity: 1;
  background: rgba(231, 76, 60, 0.2);
}

/* ── Preview kept row ────────────────────────────────────────────────────── */
.preview-kept-row {
  margin-top: 8px;
}

.preview-kept-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  font-size: 13px;
  color: #2ecc71;
  background: rgba(46, 204, 113, 0.1);
  border-color: rgba(46, 204, 113, 0.3);
}

.preview-kept-btn:hover:not(:disabled) {
  background: rgba(46, 204, 113, 0.2);
}

.preview-kept-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.hints {
  margin-top: 8px;
  font-size: 0.72rem;
  color: rgba(255, 255, 255, 0.25);
  line-height: 1.3;
}

/* ── Loading ─────────────────────────────────────────────────────────────── */
.loading-card {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 32px;
  color: var(--text-secondary);
  font-size: 14px;
}

.loading-spinner {
  width: 20px;
  height: 20px;
  border: 2px solid rgba(155, 89, 182, 0.3);
  border-top-color: #9b59b6;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* ── Export card ──────────────────────────────────────────────────────────── */
.export-card h2 {
  font-size: 16px;
  margin-bottom: 12px;
}

.export-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.export-actions button {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 10px 18px;
  font-size: 14px;
  font-weight: 500;
}

.btn-reference {
  background: rgba(155, 89, 182, 0.2);
  border: 1px solid rgba(155, 89, 182, 0.5);
  color: #9b59b6;
  border-radius: 8px;
  transition: all 0.2s;
}

.btn-reference:hover:not(:disabled) {
  background: rgba(155, 89, 182, 0.35);
  border-color: #9b59b6;
}

.btn-reference:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-training {
  background: rgba(52, 152, 219, 0.2);
  border: 1px solid rgba(52, 152, 219, 0.5);
  color: #3498db;
  border-radius: 8px;
  transition: all 0.2s;
}

.btn-training:hover:not(:disabled) {
  background: rgba(52, 152, 219, 0.35);
  border-color: #3498db;
}

.btn-training:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.action-success {
  margin-top: 12px;
  padding: 8px 14px;
  background: rgba(46, 204, 113, 0.15);
  border: 1px solid rgba(46, 204, 113, 0.3);
  border-radius: 6px;
  color: #2ecc71;
  font-size: 13px;
}

.action-error {
  margin-top: 12px;
  padding: 8px 14px;
  background: rgba(231, 76, 60, 0.15);
  border: 1px solid rgba(231, 76, 60, 0.3);
  border-radius: 6px;
  color: var(--danger);
  font-size: 13px;
}
</style>
