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

// ── YouTube cache ────────────────────────────────────────────────────────────
interface YtCacheEntry {
  fileId: string
  url: string
  title: string
  thumbnail: string
  fileName: string
}
const ytCache = ref<YtCacheEntry[]>([])

function loadYtCache() {
  try {
    const raw = localStorage.getItem('yt-cache')
    if (raw) ytCache.value = JSON.parse(raw)
  } catch { ytCache.value = [] }
}

function saveYtCache() {
  localStorage.setItem('yt-cache', JSON.stringify(ytCache.value))
}

function addToYtCache(entry: YtCacheEntry) {
  // Replace existing entry for same URL
  ytCache.value = ytCache.value.filter(e => e.url !== entry.url)
  ytCache.value.unshift(entry)
  saveYtCache()
}

async function syncYtCache() {
  try {
    const resp = await fetch('http://127.0.0.1:8765/api/youtube/cache')
    if (resp.ok) {
      const serverEntries: YtCacheEntry[] = await resp.json()
      const serverIds = new Set(serverEntries.map(e => e.fileId))
      // Keep only local entries that still exist on server
      ytCache.value = ytCache.value.filter(e => serverIds.has(e.fileId))
      // Add any server entries not in local cache
      for (const se of serverEntries) {
        if (!ytCache.value.some(e => e.fileId === se.fileId)) {
          ytCache.value.push(se)
        }
      }
      saveYtCache()
    }
  } catch { /* backend not ready yet */ }
}

async function useCachedVideo(entry: YtCacheEntry) {
  ytFetching.value = true
  ytError.value = ''
  ytTitle.value = entry.title
  youtubeUrl.value = entry.url
  try {
    const dlResp = await fetch(`http://127.0.0.1:8765/api/youtube/download/${entry.fileId}`)
    if (!dlResp.ok) {
      // File expired on server – remove from cache
      ytCache.value = ytCache.value.filter(e => e.fileId !== entry.fileId)
      saveYtCache()
      ytError.value = 'Cached file expired. Please fetch again.'
      return
    }
    const blob = await dlResp.blob()
    const file = new File([blob], entry.fileName, { type: blob.type })
    audioFile.value = file
    audioFileName.value = entry.title || entry.fileName
    exportName.value = entry.title || 'youtube_audio'
    loadAudio(file)
  } catch {
    ytError.value = 'Failed to load cached file'
  } finally {
    ytFetching.value = false
  }
}

// ── Audio state ──────────────────────────────────────────────────────────────
const audioFile = ref<File | null>(null)
const audioFileName = ref('')
const audioBuffer = ref<AudioBuffer | null>(null)
const previewLoading = ref(false)
const previewDuration = ref(0)
const previewCurrentTime = ref(0)
const previewIsPlaying = ref(false)
const previewPlayheadPos = ref(0)
const seekOriginPos = ref<number | null>(null) // 0-1 fraction where user clicked to seek
const volumeGain = ref(1.0)

// ── Multi-crop state ─────────────────────────────────────────────────────────
interface CutRegion {
  id: string
  start: number // 0-1 fraction
  end: number   // 0-1 fraction
}
const cutRegions = ref<CutRegion[]>([])
const selectedRegionId = ref<string | null>(null)

// Pending cut region (being positioned, not yet applied)
const pendingRegion = ref<CutRegion | null>(null)


// ── Zoom & pan state ────────────────────────────────────────────────────────
const zoomLevel = ref(1)        // 1 = full view, higher = zoomed in
const viewStart = ref(0)        // left edge as 0-1 fraction of full audio
const MAX_ZOOM = 100
const MIN_ZOOM = 1

// ── Export / action state ────────────────────────────────────────────────────
const exporting = ref(false)
const exportName = ref('')
const actionMessage = ref('')
const actionError = ref('')

// ── Speaker diarization state ───────────────────────────────────────────────
interface SpeakerSegment {
  start: number // seconds
  end: number   // seconds
}
interface Speaker {
  label: string
  segments: SpeakerSegment[]
}
const speakerData = ref<Speaker[]>([])
const diarizing = ref(false)
const diarizeError = ref('')
const autoDetectSpeakers = ref(localStorage.getItem('autoDetectSpeakers') !== 'false')
const SPEAKER_COLORS = [
  '#3498db', // blue
  '#e74c3c', // red
  '#2ecc71', // green
  '#f39c12', // orange
  '#9b59b6', // purple
  '#1abc9c', // teal
]

// ── Canvas refs ──────────────────────────────────────────────────────────────
const waveformCanvasRef = ref<HTMLCanvasElement | null>(null)
const frequencyCanvasRef = ref<HTMLCanvasElement | null>(null)
const waveformSectionRef = ref<HTMLDivElement | null>(null)
const minimapCanvasRef = ref<HTMLCanvasElement | null>(null)
const speakerBarCanvasRef = ref<HTMLCanvasElement | null>(null)

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

// Pan-drag state
let panDragActive = false
let panStartX = 0
let panStartViewStart = 0
let panTotalDelta = 0

// Middle-mouse pan state
const middleMousePanning = ref(false)
let middlePanMoveBound: ((e: MouseEvent) => void) | null = null
let middlePanEndBound: ((e: MouseEvent) => void) | null = null

// ── Computed ─────────────────────────────────────────────────────────────────
const viewSpan = computed(() => 1 / zoomLevel.value)
const isZoomed = computed(() => zoomLevel.value > 1.01)


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

  // Check cache first
  const cachedEntry = ytCache.value.find(e => e.url === youtubeUrl.value.trim())
  if (cachedEntry) {
    await useCachedVideo(cachedEntry)
    return
  }

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
    let doneThumbnail = ''

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
              doneThumbnail = data.thumbnail || ''
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
      // Add to cache
      addToYtCache({
        fileId,
        url: youtubeUrl.value.trim(),
        title: doneTitle,
        thumbnail: doneThumbnail,
        fileName,
      })

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
  pendingRegion.value = null
  speakerData.value = []
  diarizeError.value = ''

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
        waveformPeaks = computePeaks(buffer, peakBuckets())
        nextTick(() => { drawWaveform(); drawMinimap() })
        if (autoDetectSpeakers.value) detectSpeakers()
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
  seekOriginPos.value = null
  previewOffsetSec = 0
  previewStartedAt = 0
  volumeGain.value = 1.0
  waveformPeaks = null
  actionMessage.value = ''
  actionError.value = ''
  zoomLevel.value = 1
  viewStart.value = 0
}

// ── Speaker diarization ─────────────────────────────────────────────────────
async function detectSpeakers() {
  if (!audioFile.value && !audioBuffer.value) return
  diarizing.value = true
  diarizeError.value = ''
  speakerData.value = []

  try {
    // Build WAV blob from current audioBuffer (may have been cropped)
    let blob: Blob
    if (audioBuffer.value) {
      blob = audioBufferToWav(audioBuffer.value, 1.0)
    } else {
      blob = audioFile.value!
    }

    const formData = new FormData()
    formData.append('file', blob, 'audio.wav')

    const resp = await fetch('http://127.0.0.1:8765/api/diarize', {
      method: 'POST',
      body: formData,
    })

    if (!resp.ok) {
      const err = await resp.json().catch(() => null)
      throw new Error(err?.error || `Server error ${resp.status}`)
    }

    const data = await resp.json()
    speakerData.value = data.speakers || []
    nextTick(() => drawSpeakerBar())
  } catch (err: any) {
    diarizeError.value = err.message || 'Diarization failed'
  } finally {
    diarizing.value = false
  }
}

function deleteSpeakerAudio(speakerIndex: number) {
  if (!audioBuffer.value || !audioCtx) return
  const speaker = speakerData.value[speakerIndex]
  if (!speaker || !speaker.segments.length) return

  const wasPlaying = previewIsPlaying.value
  if (wasPlaying) stopPlayback()

  // Work on current buffer, removing segments from end to start to keep indices valid
  let buf = audioBuffer.value
  const duration = buf.duration
  const sortedSegs = [...speaker.segments].sort((a, b) => b.start - a.start)

  for (const seg of sortedSegs) {
    const cutStartSample = Math.floor((seg.start / duration) * buf.length)
    const cutEndSample = Math.min(Math.ceil((seg.end / duration) * buf.length), buf.length)
    const newLength = buf.length - (cutEndSample - cutStartSample)
    if (newLength <= 0) continue

    const newBuf = audioCtx!.createBuffer(buf.numberOfChannels, newLength, buf.sampleRate)
    for (let ch = 0; ch < buf.numberOfChannels; ch++) {
      const src = buf.getChannelData(ch)
      const dst = newBuf.getChannelData(ch)
      dst.set(src.subarray(0, cutStartSample), 0)
      dst.set(src.subarray(cutEndSample), cutStartSample)
    }
    buf = newBuf
  }

  // Adjust all speaker data to match the new audio
  const oldDuration = duration
  const removedSegs = [...speaker.segments].sort((a, b) => a.start - b.start)

  speakerData.value = speakerData.value
    .filter((_, i) => i !== speakerIndex)
    .map(sp => {
      let adjusted = [...sp.segments]
      // Process each removed segment from last to first
      for (let ri = removedSegs.length - 1; ri >= 0; ri--) {
        const rs = removedSegs[ri]
        const cutStartSec = rs.start
        const cutEndSec = rs.end
        const cutDur = cutEndSec - cutStartSec
        adjusted = adjusted
          .map(seg => {
            let s = seg.start, e = seg.end
            if (s >= cutStartSec && e <= cutEndSec) return null
            if (s < cutStartSec && e > cutStartSec && e <= cutEndSec) e = cutStartSec
            else if (s >= cutStartSec && s < cutEndSec && e > cutEndSec) s = cutEndSec
            else if (s < cutStartSec && e > cutEndSec) e -= cutDur
            if (s >= cutEndSec) { s -= cutDur; e -= cutDur }
            return { start: Math.round(s * 100) / 100, end: Math.round(e * 100) / 100 }
          })
          .filter((seg): seg is SpeakerSegment => seg !== null && seg.end > seg.start)
      }
      return { ...sp, segments: adjusted }
    })
    .filter(sp => sp.segments.length > 0)

  // Apply
  audioBuffer.value = buf
  previewDuration.value = buf.duration
  previewPlayheadPos.value = 0
  previewCurrentTime.value = 0
  previewOffsetSec = 0
  waveformPeaks = computePeaks(buf, peakBuckets())
  pendingRegion.value = null

  nextTick(() => { drawWaveform(); drawMinimap(); drawSpeakerBar() })
  if (wasPlaying) startPlayback()
}

function onSpeakerBarClick(event: MouseEvent) {
  const canvasEl = speakerBarCanvasRef.value
  if (!canvasEl || !speakerData.value.length || !previewDuration.value) return

  const rect = canvasEl.getBoundingClientRect()
  const clickX = (event.clientX - rect.left) / rect.width
  // Convert viewport-relative click to full-audio fraction
  const clickFrac = viewStart.value + clickX * viewSpan.value
  const clickSec = clickFrac * previewDuration.value

  // Find which speaker segment was clicked
  for (const speaker of speakerData.value) {
    for (const seg of speaker.segments) {
      if (clickSec >= seg.start && clickSec <= seg.end) {
        // Set this segment as the pending cut region
        const startFrac = seg.start / previewDuration.value
        const endFrac = seg.end / previewDuration.value
        pendingRegion.value = {
          id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`,
          start: startFrac,
          end: endFrac,
        }
        drawWaveform()
        drawMinimap()
        return
      }
    }
  }
}

function drawSpeakerBar() {
  const canvasEl = speakerBarCanvasRef.value
  if (!canvasEl || !speakerData.value.length || !previewDuration.value) return

  const dpr = window.devicePixelRatio || 1
  const rect = canvasEl.getBoundingClientRect()
  canvasEl.width = rect.width * dpr
  canvasEl.height = rect.height * dpr

  const ctx = canvasEl.getContext('2d')
  if (!ctx) return
  ctx.scale(dpr, dpr)

  const W = rect.width
  const H = rect.height
  const duration = previewDuration.value
  const vStart = viewStart.value
  const vSpan = viewSpan.value

  ctx.clearRect(0, 0, W, H)

  for (let si = 0; si < speakerData.value.length; si++) {
    const speaker = speakerData.value[si]
    const color = SPEAKER_COLORS[si % SPEAKER_COLORS.length]

    for (const seg of speaker.segments) {
      const segStartFrac = seg.start / duration
      const segEndFrac = seg.end / duration

      // Map to viewport
      const x1 = (segStartFrac - vStart) / vSpan * W
      const x2 = (segEndFrac - vStart) / vSpan * W

      if (x2 < 0 || x1 > W) continue // off-screen

      const drawX = Math.max(0, x1)
      const drawW = Math.min(W, x2) - drawX

      ctx.fillStyle = color
      ctx.globalAlpha = 0.7
      ctx.beginPath()
      ctx.roundRect(drawX, 1, Math.max(drawW, 1), H - 2, 2)
      ctx.fill()
    }
  }
  ctx.globalAlpha = 1.0
}

// ── Zoom & pan helpers ──────────────────────────────────────────────────────
function viewportToFrac(vp: number): number {
  // Convert viewport-relative fraction (0-1) to full-audio fraction (0-1)
  return viewStart.value + vp * viewSpan.value
}

function viewportToCollapsedFrac(vp: number): number {
  // Convert viewport-relative fraction to collapsed fraction (for zoom anchoring)
  return viewStart.value + vp * viewSpan.value
}

function clampViewStart() {
  const maxStart = Math.max(0, 1 - viewSpan.value)
  viewStart.value = Math.max(0, Math.min(maxStart, viewStart.value))
}

function zoomAt(newZoom: number, anchorFrac: number) {
  // Zoom centered on anchorFrac (full-audio 0-1 fraction)
  const oldSpan = viewSpan.value
  const oldOffset = anchorFrac - viewStart.value
  const anchorRatio = oldOffset / oldSpan // where anchor is in viewport (0-1)

  const oldBuckets = peakBuckets()
  zoomLevel.value = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, newZoom))
  const newBuckets = peakBuckets()
  const newSpan = 1 / zoomLevel.value
  viewStart.value = anchorFrac - anchorRatio * newSpan
  clampViewStart()

  // Recompute peaks at higher resolution when zoom changes enough
  if (newBuckets !== oldBuckets) recomputePeaks()

  drawWaveform()
  // Minimap canvas may not exist yet if we just zoomed in, wait for DOM
  nextTick(() => drawMinimap())
}

function zoomIn() {
  const center = viewStart.value + viewSpan.value / 2
  zoomAt(zoomLevel.value * 1.5, center)
}

function zoomOut() {
  const center = viewStart.value + viewSpan.value / 2
  zoomAt(zoomLevel.value / 1.5, center)
}

function zoomReset() {
  zoomLevel.value = 1
  viewStart.value = 0
  recomputePeaks()
  drawWaveform()
  drawMinimap()
}

function onWaveformWheel(event: WheelEvent) {
  if (!audioBuffer.value) return
  event.preventDefault()

  const canvasEl = waveformCanvasRef.value
  if (!canvasEl) return
  const rect = canvasEl.getBoundingClientRect()
  const cursorViewportFrac = (event.clientX - rect.left) / rect.width
  const cursorFrac = viewportToCollapsedFrac(cursorViewportFrac)

  if (event.shiftKey) {
    // Shift+wheel = pan
    const panAmount = viewSpan.value * 0.1 * Math.sign(event.deltaY)
    viewStart.value += panAmount
    clampViewStart()
    drawWaveform()
    drawMinimap()
  } else {
    // Regular wheel = zoom
    const factor = event.deltaY < 0 ? 1.25 : 1 / 1.25
    zoomAt(zoomLevel.value * factor, cursorFrac)
  }
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
  if (middlePanMoveBound) { document.removeEventListener('mousemove', middlePanMoveBound); middlePanMoveBound = null }
  if (middlePanEndBound) { document.removeEventListener('mouseup', middlePanEndBound); middlePanEndBound = null }
  middleMousePanning.value = false
}

// ── Waveform peaks ───────────────────────────────────────────────────────────
const BASE_PEAKS = 600

function peakBuckets(): number {
  // Scale bucket count with zoom so bars stay ~the same pixel width
  return Math.min(Math.round(BASE_PEAKS * zoomLevel.value), 20000)
}

function recomputePeaks() {
  if (audioBuffer.value) {
    waveformPeaks = computePeaks(audioBuffer.value, peakBuckets())
  }
}

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

  const midY = H / 2
  const vStart = viewStart.value
  const vSpan = viewSpan.value

  // Always draw full waveform — cut regions are shown as DOM overlays
  {
    const startIdx = Math.max(0, Math.floor(vStart * n))
    const endIdx = Math.min(n, Math.ceil((vStart + vSpan) * n))

    const rawBarW = W / (vSpan * n)
    const maxBarPx = 3 // cap bar width so waveform stays granular
    const barW = Math.min(rawBarW, maxBarPx)
    const gap = rawBarW > maxBarPx ? (rawBarW - maxBarPx) / 2 : 0

    for (let i = startIdx; i < endIdx; i++) {
      const frac = i / n
      const x = (frac - vStart) / vSpan * W + gap
      const barH = Math.min(peaks[i] * volumeGain.value * midY * 0.95, midY)

      const t = frac
      const r = Math.round(155 + (231 - 155) * t)
      const g = Math.round(89 + (76 - 89) * t)
      const b = Math.round(182 + (60 - 182) * t)
      ctx.fillStyle = `rgba(${r},${g},${b},0.85)`

      ctx.fillRect(x, midY - barH, Math.max(barW - 0.5, 0.5), barH * 2)
    }
  }

  // Draw time scale when zoomed in
  if (zoomLevel.value > 1.5) {
    drawTimeScale(ctx, W, H, vStart, vSpan)
  }

  // Draw seek origin marker
  if (seekOriginPos.value !== null && previewIsPlaying.value) {
    const originPx = (seekOriginPos.value - vStart) / vSpan * W
    if (originPx >= -5 && originPx <= W + 5) {
      ctx.fillStyle = 'rgba(255, 255, 255, 0.25)'
      ctx.fillRect(originPx - 0.5, 0, 1, H)
      // Small diamond marker
      ctx.fillStyle = 'rgba(255, 255, 255, 0.4)'
      ctx.beginPath()
      ctx.moveTo(originPx, H - 10)
      ctx.lineTo(originPx + 4, H - 5)
      ctx.lineTo(originPx, H)
      ctx.lineTo(originPx - 4, H - 5)
      ctx.closePath()
      ctx.fill()
    }
  }

  // Draw playhead
  if (previewIsPlaying.value || previewPlayheadPos.value > 0) {
    const px = (previewPlayheadPos.value - vStart) / vSpan * W
    if (px >= -5 && px <= W + 5) {
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

  // Update speaker bar whenever waveform redraws
  drawSpeakerBar()
}

function drawTimeScale(ctx: CanvasRenderingContext2D, W: number, H: number, vStart: number, vSpan: number) {
  if (!previewDuration.value) return
  const duration = previewDuration.value

  const visibleDurationEst = vSpan * duration

  // Choose a nice tick interval based on visible duration
  const targetTicks = 8
  const rawInterval = visibleDurationEst / targetTicks
  const niceIntervals = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60]
  let tickInterval = niceIntervals[niceIntervals.length - 1]
  for (const ni of niceIntervals) {
    if (ni >= rawInterval) { tickInterval = ni; break }
  }

  ctx.font = '9px monospace'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'bottom'

  const visibleStartSec = vStart * duration
  const visibleEndSec = (vStart + vSpan) * duration
  const firstTick = Math.ceil(visibleStartSec / tickInterval) * tickInterval
  for (let t = firstTick; t <= visibleEndSec; t += tickInterval) {
    const frac = t / duration
    const x = (frac - vStart) / vSpan * W
    if (x < 0 || x > W) continue
    drawTimeTick(ctx, x, t, tickInterval, W, H)
  }
}

function drawTimeTick(ctx: CanvasRenderingContext2D, x: number, t: number, tickInterval: number, _W: number, H: number) {
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.15)'
  ctx.lineWidth = 1
  ctx.beginPath()
  ctx.moveTo(x, H - 12)
  ctx.lineTo(x, H)
  ctx.stroke()

  const label = tickInterval < 1
    ? `${t.toFixed(tickInterval < 0.1 ? 2 : 1)}s`
    : formatTime(t)
  ctx.fillStyle = 'rgba(255, 255, 255, 0.35)'
  ctx.fillText(label, x, H - 1)
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
      seekOriginPos.value = null
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
    drawMinimap()
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

// ── Minimap ─────────────────────────────────────────────────────────────────
function drawMinimap() {
  const canvasEl = minimapCanvasRef.value
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
  const midY = H / 2

  ctx.clearRect(0, 0, W, H)
  ctx.fillStyle = 'rgba(0,0,0,0.3)'
  ctx.fillRect(0, 0, W, H)

  // Always draw full waveform on minimap
  const barW = W / n
  for (let i = 0; i < n; i++) {
    const barH = Math.min(peaks[i] * volumeGain.value * midY * 0.9, midY)
    ctx.fillStyle = 'rgba(155, 89, 182, 0.5)'
    ctx.fillRect(i * barW, midY - barH, Math.max(barW - 0.3, 0.3), barH * 2)
  }

  // Draw pending cut region on minimap
  if (pendingRegion.value) {
    const rx = pendingRegion.value.start * W
    const rw = (pendingRegion.value.end - pendingRegion.value.start) * W
    ctx.fillStyle = 'rgba(231, 76, 60, 0.3)'
    ctx.fillRect(rx, 0, rw, H)
  }

  // Draw viewport indicator
  const vx = viewStart.value * W
  const vw = viewSpan.value * W
  ctx.fillStyle = 'rgba(0, 0, 0, 0.5)'
  ctx.fillRect(0, 0, vx, H)
  ctx.fillRect(vx + vw, 0, W - vx - vw, H)
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.7)'
  ctx.lineWidth = 1.5
  ctx.strokeRect(vx + 0.5, 0.5, vw - 1, H - 1)

  // Seek origin on minimap
  if (seekOriginPos.value !== null && previewIsPlaying.value) {
    const opx = seekOriginPos.value * W
    ctx.fillStyle = 'rgba(255, 255, 255, 0.3)'
    ctx.fillRect(opx - 0.5, 0, 1, H)
  }

  // Playhead on minimap
  if (previewIsPlaying.value || previewPlayheadPos.value > 0) {
    const px = previewPlayheadPos.value * W
    ctx.fillStyle = 'rgba(255, 255, 255, 0.8)'
    ctx.fillRect(px - 0.5, 0, 1, H)
  }
}

let minimapDragging = false
function onMinimapMousedown(event: MouseEvent) {
  const canvasEl = minimapCanvasRef.value
  if (!canvasEl) return
  event.preventDefault()
  minimapDragging = true

  const rect = canvasEl.getBoundingClientRect()
  const frac = (event.clientX - rect.left) / rect.width
  // Center viewport on click position
  viewStart.value = frac - viewSpan.value / 2
  clampViewStart()
  drawWaveform()
  drawMinimap()

  const onMove = (e: MouseEvent) => {
    if (!minimapDragging) return
    const f = (e.clientX - rect.left) / rect.width
    viewStart.value = f - viewSpan.value / 2
    clampViewStart()
    drawWaveform()
    drawMinimap()
  }
  const onUp = () => {
    minimapDragging = false
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
  }
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}

// ── Waveform click / seek ────────────────────────────────────────────────────
function onWaveformMiddleMousedown(event: MouseEvent) {
  if (event.button !== 1) return
  event.preventDefault()
  if (!audioBuffer.value || zoomLevel.value <= 1) return

  const canvasEl = waveformCanvasRef.value
  if (!canvasEl) return

  middleMousePanning.value = true
  const startX = event.clientX
  const startViewStart = viewStart.value
  const rect = canvasEl.getBoundingClientRect()

  if (middlePanMoveBound) document.removeEventListener('mousemove', middlePanMoveBound)
  if (middlePanEndBound) document.removeEventListener('mouseup', middlePanEndBound)

  middlePanMoveBound = (e: MouseEvent) => {
    const dx = e.clientX - startX
    const fracDelta = -(dx / rect.width) * viewSpan.value
    viewStart.value = startViewStart + fracDelta
    clampViewStart()
    drawWaveform()
    drawMinimap()
  }

  middlePanEndBound = () => {
    middleMousePanning.value = false
    if (middlePanMoveBound) { document.removeEventListener('mousemove', middlePanMoveBound); middlePanMoveBound = null }
    if (middlePanEndBound) { document.removeEventListener('mouseup', middlePanEndBound); middlePanEndBound = null }
  }

  document.addEventListener('mousemove', middlePanMoveBound)
  document.addEventListener('mouseup', middlePanEndBound)
}

function onWaveformMousedown(event: MouseEvent) {
  if (event.button !== 0) return // only left click
  const canvasEl = waveformCanvasRef.value
  if (!canvasEl || !audioBuffer.value) return
  const rect = canvasEl.getBoundingClientRect()

  const startX = event.clientX
  const startViewportFrac = Math.max(0, Math.min(1, (startX - rect.left) / rect.width))
  const startFrac = viewportToFrac(startViewportFrac)
  let dragging = false

  removeDragListeners()

  // Cancel any existing pending region when starting a new drag
  if (pendingRegion.value) {
    pendingRegion.value = null
  }

  dragMoveBound = (e: MouseEvent) => {
    const dx = Math.abs(e.clientX - startX)
    if (dx < 4) return // dead zone before starting drag

    dragging = true
    const currentViewportFrac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
    const currentFrac = Math.max(0, Math.min(1, viewportToFrac(currentViewportFrac)))
    const regionStart = Math.min(startFrac, currentFrac)
    const regionEnd = Math.max(startFrac, currentFrac)

    if (!pendingRegion.value) {
      pendingRegion.value = {
        id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`,
        start: regionStart,
        end: regionEnd,
      }
    } else {
      pendingRegion.value.start = regionStart
      pendingRegion.value.end = regionEnd
    }
    drawWaveform()
    drawMinimap()
  }

  dragEndBound = () => {
    removeDragListeners()
    if (!dragging) {
      // Barely moved — treat as seek click
      seekTo(startFrac * previewDuration.value)
    }
  }

  document.addEventListener('mousemove', dragMoveBound)
  document.addEventListener('mouseup', dragEndBound)
}

function seekTo(timeSec: number) {
  const wasPlaying = previewIsPlaying.value
  if (wasPlaying) stopPlayback()
  previewOffsetSec = Math.max(0, Math.min(previewDuration.value, timeSec))
  previewCurrentTime.value = previewOffsetSec
  previewPlayheadPos.value = previewDuration.value > 0 ? previewOffsetSec / previewDuration.value : 0
  seekOriginPos.value = previewPlayheadPos.value
  drawWaveform()
  drawMinimap()
  if (wasPlaying) startPlayback()
}

// ── Cut region management ────────────────────────────────────────────────────
function addCutRegion() {
  // Create a pending cut region starting from the current playhead position
  if (pendingRegion.value) return // already have a pending region
  const start = previewPlayheadPos.value || 0
  const width = 0.1
  pendingRegion.value = {
    id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`,
    start: Math.max(0, start),
    end: Math.min(1, start + width),
  }
  drawWaveform()
  drawMinimap()
}

function applyPendingCut() {
  if (!pendingRegion.value || !audioBuffer.value || !audioCtx) return

  const wasPlaying = previewIsPlaying.value
  if (wasPlaying) stopPlayback()

  const buf = audioBuffer.value
  const sampleRate = buf.sampleRate
  const numChannels = buf.numberOfChannels
  const cutStart = Math.floor(pendingRegion.value.start * buf.length)
  const cutEnd = Math.ceil(pendingRegion.value.end * buf.length)
  const newLength = buf.length - (cutEnd - cutStart)

  if (newLength <= 0) {
    pendingRegion.value = null
    return
  }

  // Build new buffer without the cut section
  const newBuffer = audioCtx.createBuffer(numChannels, newLength, sampleRate)
  for (let ch = 0; ch < numChannels; ch++) {
    const src = buf.getChannelData(ch)
    const dst = newBuffer.getChannelData(ch)
    dst.set(src.subarray(0, cutStart), 0)
    dst.set(src.subarray(cutEnd), cutStart)
  }

  // Update playhead to stay in a valid position
  const cutStartFrac = pendingRegion.value.start
  const cutEndFrac = pendingRegion.value.end
  const cutWidth = cutEndFrac - cutStartFrac

  let newPlayheadFrac = previewPlayheadPos.value
  if (newPlayheadFrac >= cutEndFrac) {
    newPlayheadFrac -= cutWidth
  } else if (newPlayheadFrac > cutStartFrac) {
    newPlayheadFrac = cutStartFrac
  }
  // Normalize to new duration
  const newDuration = newBuffer.duration
  newPlayheadFrac = Math.max(0, Math.min(1, newPlayheadFrac / (1 - cutWidth)))

  // Track the cut for display purposes
  cutRegions.value.push({ ...pendingRegion.value })
  pendingRegion.value = null

  // Adjust view window: the audio shrank, so shift viewStart to account for removed section
  if (viewStart.value > cutStartFrac) {
    viewStart.value = Math.max(0, viewStart.value - cutWidth)
  }
  clampViewStart()

  // Apply new buffer
  audioBuffer.value = newBuffer
  previewDuration.value = newDuration
  previewPlayheadPos.value = newPlayheadFrac
  previewCurrentTime.value = newPlayheadFrac * newDuration
  previewOffsetSec = previewCurrentTime.value
  waveformPeaks = computePeaks(newBuffer, peakBuckets())

  // ── Adjust speaker segments to match the cropped audio ──────────────
  if (speakerData.value.length) {
    const oldDuration = buf.duration
    const cutStartSec = cutStartFrac * oldDuration
    const cutEndSec = cutEndFrac * oldDuration
    const cutDuration = cutEndSec - cutStartSec

    speakerData.value = speakerData.value.map(speaker => {
      const adjusted = speaker.segments
        .map(seg => {
          let s = seg.start
          let e = seg.end
          // Segment entirely within the cut — remove it
          if (s >= cutStartSec && e <= cutEndSec) return null
          // Segment overlaps cut start — trim end to cut boundary
          if (s < cutStartSec && e > cutStartSec && e <= cutEndSec) {
            e = cutStartSec
          }
          // Segment overlaps cut end — trim start to cut boundary
          else if (s >= cutStartSec && s < cutEndSec && e > cutEndSec) {
            s = cutEndSec
          }
          // Segment spans entire cut — shrink by cut duration
          else if (s < cutStartSec && e > cutEndSec) {
            e -= cutDuration
          }
          // Shift segments after the cut
          if (s >= cutEndSec) {
            s -= cutDuration
            e -= cutDuration
          } else if (e > cutStartSec && s < cutStartSec) {
            // already trimmed above, no shift needed for start
          }
          return { start: Math.round(s * 100) / 100, end: Math.round(e * 100) / 100 }
        })
        .filter((seg): seg is SpeakerSegment => seg !== null && seg.end > seg.start)
      return { ...speaker, segments: adjusted }
    }).filter(speaker => speaker.segments.length > 0)

    nextTick(() => drawSpeakerBar())
  }

  drawWaveform()
  drawMinimap()

  if (wasPlaying) startPlayback()
}

function cancelPendingCut() {
  pendingRegion.value = null
  drawWaveform()
  drawMinimap()
}

function removeDragListeners() {
  if (dragMoveBound) { document.removeEventListener('mousemove', dragMoveBound); dragMoveBound = null }
  if (dragEndBound) { document.removeEventListener('mouseup', dragEndBound); dragEndBound = null }
}

// ── Cut overlay positioning helpers ──────────────────────────────────────────
function cutOverlayStyle(region: CutRegion) {
  const vStart = viewStart.value
  const vSpan = viewSpan.value
  const left = (region.start - vStart) / vSpan * 100
  const right = (region.end - vStart) / vSpan * 100
  return {
    left: `${Math.max(0, left)}%`,
    width: `${Math.min(100, right) - Math.max(0, left)}%`,
  }
}

function cutHandleX(frac: number): string {
  const vStart = viewStart.value
  const vSpan = viewSpan.value
  return `${(frac - vStart) / vSpan * 100}%`
}

function startHandleDrag(_regionId: string, edge: 'start' | 'end', _event: MouseEvent) {
  const canvasEl = waveformCanvasRef.value
  if (!canvasEl || !pendingRegion.value) return
  const rect = canvasEl.getBoundingClientRect()

  removeDragListeners()

  dragMoveBound = (e: MouseEvent) => {
    if (!pendingRegion.value) return
    const viewportFrac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
    const frac = viewStart.value + viewportFrac * viewSpan.value
    const clamped = Math.max(0, Math.min(1, frac))

    if (edge === 'start') {
      pendingRegion.value.start = Math.min(clamped, pendingRegion.value.end - 0.0001)
    } else {
      pendingRegion.value.end = Math.max(clamped, pendingRegion.value.start + 0.0001)
    }
    drawWaveform()
    drawMinimap()
  }

  dragEndBound = () => {
    removeDragListeners()
  }

  document.addEventListener('mousemove', dragMoveBound)
  document.addEventListener('mouseup', dragEndBound)
}

// ── Keyboard controls ────────────────────────────────────────────────────────
function onKeydown(event: KeyboardEvent) {
  if (event.code === 'Space') {
    event.preventDefault()
    togglePlayback()
  } else if (event.key === '=' || event.key === '+') {
    event.preventDefault()
    zoomIn()
  } else if (event.key === '-') {
    event.preventDefault()
    zoomOut()
  } else if (event.key === '0') {
    event.preventDefault()
    zoomReset()
  } else if (event.key === 'ArrowLeft' && isZoomed.value) {
    event.preventDefault()
    viewStart.value -= viewSpan.value * 0.2
    clampViewStart()
    drawWaveform()
    drawMinimap()
  } else if (event.key === 'ArrowRight' && isZoomed.value) {
    event.preventDefault()
    viewStart.value += viewSpan.value * 0.2
    clampViewStart()
    drawWaveform()
    drawMinimap()
  }
}

// ── Export / crop audio ──────────────────────────────────────────────────────

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
  if (!audioBuffer.value) return

  const wavBlob = audioBufferToWav(audioBuffer.value, volumeGain.value)
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
  if (!audioBuffer.value) { actionError.value = 'No audio to export'; return }
  const croppedBuffer = audioBuffer.value

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
  if (!audioBuffer.value) { actionError.value = 'No audio to export'; return }
  const croppedBuffer = audioBuffer.value

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
  loadYtCache()
  syncYtCache()
})

onUnmounted(() => {
  destroyAudio()
})
</script>

<template>
  <div class="audio-cropper">
    <h1 class="view-title">Import Audio</h1>
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

        <!-- YouTube cache list -->
        <div v-if="ytCache.length" class="yt-cache">
          <div class="yt-cache-header">Previously downloaded</div>
          <div class="yt-cache-list">
            <button
              v-for="item in ytCache"
              :key="item.fileId"
              class="yt-cache-item"
              @click="useCachedVideo(item)"
              :disabled="ytFetching"
            >
              <img
                v-if="item.thumbnail"
                :src="item.thumbnail"
                class="yt-cache-thumb"
                alt=""
              />
              <div v-else class="yt-cache-thumb yt-cache-thumb-placeholder">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
              </div>
              <span class="yt-cache-title">{{ item.title || item.fileName }}</span>
            </button>
          </div>
        </div>
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
        <div class="waveform-container" :class="{ 'pan-cursor': middleMousePanning }" @wheel.prevent="onWaveformWheel" @mousedown="onWaveformMousedown" @mousedown.middle="onWaveformMiddleMousedown" @auxclick.middle.prevent>
          <canvas
            ref="waveformCanvasRef"
            class="waveform-canvas"
          ></canvas>

          <!-- Pending cut region overlay -->
          <template v-if="pendingRegion">
            <div
              class="cut-overlay selected"
              :style="cutOverlayStyle(pendingRegion)"
            >
              <button class="cut-remove-btn" style="opacity:1" @click.stop="cancelPendingCut" title="Cancel">&times;</button>
            </div>
            <!-- Left handle -->
            <div
              class="crop-handle selected"
              :style="{ left: cutHandleX(pendingRegion.start) }"
              @mousedown.stop.prevent="startHandleDrag(pendingRegion.id, 'start', $event)"
            >
              <div class="crop-handle-line"></div>
              <div class="crop-handle-grip">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 18l-6-6 6-6"/></svg>
              </div>
              <span class="crop-handle-label">{{ formatTime(pendingRegion.start * previewDuration) }}</span>
            </div>
            <!-- Right handle -->
            <div
              class="crop-handle selected"
              :style="{ left: cutHandleX(pendingRegion.end) }"
              @mousedown.stop.prevent="startHandleDrag(pendingRegion.id, 'end', $event)"
            >
              <div class="crop-handle-line"></div>
              <div class="crop-handle-grip">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
              </div>
              <span class="crop-handle-label">{{ formatTime(pendingRegion.end * previewDuration) }}</span>
            </div>
          </template>
        </div>

        <!-- Speaker diarization bar -->
        <div v-if="speakerData.length" class="speaker-bar-container">
          <canvas ref="speakerBarCanvasRef" class="speaker-bar-canvas" @click="onSpeakerBarClick"></canvas>
        </div>
      </div>

      <!-- Minimap (shown when zoomed) -->
      <div v-if="isZoomed" class="minimap-container">
        <canvas
          ref="minimapCanvasRef"
          class="minimap-canvas"
          @mousedown="onMinimapMousedown"
        ></canvas>
      </div>

      <!-- Zoom controls -->
      <div class="zoom-controls">
        <button class="zoom-btn" @click="zoomOut" :disabled="zoomLevel <= MIN_ZOOM" title="Zoom out">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="5" y1="12" x2="19" y2="12"/></svg>
        </button>
        <span class="zoom-label">{{ zoomLevel < 10 ? zoomLevel.toFixed(1) : Math.round(zoomLevel) }}x</span>
        <button class="zoom-btn" @click="zoomIn" :disabled="zoomLevel >= MAX_ZOOM" title="Zoom in">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        </button>
        <button v-if="isZoomed" class="zoom-btn zoom-reset-btn" @click="zoomReset" title="Reset zoom">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>
        </button>
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

        <template v-if="pendingRegion">
          <button class="btn-cut apply-cut-btn" @click="applyPendingCut" title="Cut the selected region">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9C6 7.34 7.34 6 9 6s3 1.34 3 3-1.34 3-3 3-3-1.34-3-3zM6 15c0-1.66 1.34-3 3-3s3 1.34 3 3-1.34 3-3 3-3-1.34-3-3zM20 4L8.12 15.88M14.47 14.48L20 20M8.12 8.12L12 12"/></svg>
            Cut
          </button>
          <button class="btn-secondary cancel-cut-btn" @click="cancelPendingCut" title="Cancel">
            Cancel
          </button>
          <div class="cut-info">
            <span class="pending-label">Cutting:</span>
            <span class="cut-range">{{ formatTime(pendingRegion.start * previewDuration) }} – {{ formatTime(pendingRegion.end * previewDuration) }}</span>
            <span class="cut-duration">({{ formatTime((pendingRegion.end - pendingRegion.start) * previewDuration) }})</span>
          </div>
        </template>

        <template v-else>
          <button class="btn-secondary add-cut-btn" @click="addCutRegion" title="Add a cut region at playhead position">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 15h2V7c0-1.1-.9-2-2-2H9v2h8v8zM7 17V1H5v4H1v2h4v10c0 1.1.9 2 2 2h10v4h2v-4h4v-2H7z"/></svg>
            Add Cut Region
          </button>

          <div v-if="cutRegions.length > 0" class="cut-info">
            <span>{{ cutRegions.length }} cut{{ cutRegions.length > 1 ? 's' : '' }} applied</span>
            <span class="kept-duration">Duration: {{ formatTime(previewDuration) }}</span>
          </div>
        </template>
      </div>

      <!-- Speaker diarization -->
      <div class="speaker-section">
        <div class="speaker-controls">
          <label class="auto-detect-toggle" title="Automatically detect speakers when audio is loaded">
            <input
              type="checkbox"
              :checked="autoDetectSpeakers"
              @change="(e: Event) => { autoDetectSpeakers = (e.target as HTMLInputElement).checked; localStorage.setItem('autoDetectSpeakers', String(autoDetectSpeakers)) }"
            />
            <span>Auto-detect</span>
          </label>
          <button
            class="btn-secondary detect-speakers-btn"
            @click="detectSpeakers"
            :disabled="diarizing"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
            {{ diarizing ? 'Detecting...' : speakerData.length ? 'Re-detect Speakers' : 'Detect Speakers' }}
          </button>

          <div v-if="diarizing" class="speaker-loading">
            <div class="loading-spinner small"></div>
            <span>Analyzing voices...</span>
          </div>

          <div v-if="diarizeError" class="speaker-error">{{ diarizeError }}</div>
        </div>

        <!-- Speaker legend -->
        <div v-if="speakerData.length" class="speaker-legend">
          <div
            v-for="(speaker, idx) in speakerData"
            :key="speaker.label"
            class="speaker-legend-item"
          >
            <span class="speaker-color-dot" :style="{ background: SPEAKER_COLORS[idx % SPEAKER_COLORS.length] }"></span>
            <span class="speaker-label">{{ speaker.label }}</span>
            <button
              class="speaker-delete-btn"
              @click="deleteSpeakerAudio(idx)"
              title="Delete all audio from this speaker"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            </button>
          </div>
        </div>
      </div>

      <!-- Hints -->
      <div class="hints">
        Space: play/pause · Click: seek · Drag: create cut region · Scroll: zoom · Shift+Scroll: pan
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

/* YouTube cache */
.yt-cache {
  margin-top: 12px;
}

.yt-cache-header {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--text-secondary);
  margin-bottom: 6px;
}

.yt-cache-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 200px;
  overflow-y: auto;
}

.yt-cache-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 8px;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;
  text-align: left;
  color: var(--text-primary);
}

.yt-cache-item:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.08);
  border-color: rgba(255, 255, 255, 0.15);
}

.yt-cache-item:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.yt-cache-thumb {
  width: 48px;
  height: 36px;
  border-radius: 4px;
  object-fit: cover;
  flex-shrink: 0;
  background: rgba(0, 0, 0, 0.3);
}

.yt-cache-thumb-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-secondary);
}

.yt-cache-title {
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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

.waveform-container.pan-cursor {
  cursor: grabbing;
}

.waveform-canvas {
  display: block;
  width: 100%;
  height: 100%;
  border-radius: 8px;
}

/* ── Minimap ─────────────────────────────────────────────────────────────── */
.minimap-container {
  width: 100%;
  height: 32px;
  margin-top: 6px;
  border-radius: 4px;
  overflow: hidden;
  cursor: pointer;
  border: 1px solid rgba(255, 255, 255, 0.08);
}

.minimap-canvas {
  display: block;
  width: 100%;
  height: 100%;
}

/* ── Zoom controls ───────────────────────────────────────────────────────── */
.zoom-controls {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 6px;
}

.zoom-btn {
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 5px;
  color: var(--text-secondary);
  cursor: pointer;
  padding: 0;
  transition: all 0.15s;
}

.zoom-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.12);
  color: var(--text-primary);
}

.zoom-btn:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}

.zoom-reset-btn {
  margin-left: 4px;
}

.zoom-label {
  font-size: 11px;
  font-family: monospace;
  color: var(--text-secondary);
  min-width: 36px;
  text-align: center;
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

.apply-cut-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 16px;
  font-size: 13px;
  font-weight: 600;
  background: rgba(231, 76, 60, 0.2);
  border: 1px solid rgba(231, 76, 60, 0.5);
  color: var(--danger);
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s;
}

.apply-cut-btn:hover {
  background: rgba(231, 76, 60, 0.35);
  border-color: var(--danger);
}

.cancel-cut-btn {
  padding: 6px 14px;
  font-size: 13px;
}

.pending-label {
  color: var(--danger);
  font-weight: 600;
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

/* ── Speaker diarization ─────────────────────────────────────────────────── */
.speaker-bar-container {
  width: 100%;
  height: 14px;
  margin-top: 3px;
  border-radius: 4px;
  overflow: hidden;
  background: rgba(0, 0, 0, 0.15);
  border: 1px solid rgba(255, 255, 255, 0.06);
}

.speaker-bar-canvas {
  display: block;
  width: 100%;
  height: 100%;
  cursor: pointer;
}

.speaker-section {
  margin-top: 8px;
}

.speaker-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.auto-detect-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
}

.auto-detect-toggle input {
  accent-color: var(--accent);
  cursor: pointer;
}

.detect-speakers-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  font-size: 12px;
}

.detect-speakers-btn svg {
  color: #3498db;
}

.speaker-loading {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}

.loading-spinner.small {
  width: 14px;
  height: 14px;
  border-width: 1.5px;
}

.speaker-error {
  font-size: 12px;
  color: var(--danger);
}

.speaker-legend {
  display: flex;
  gap: 14px;
  margin-top: 6px;
  flex-wrap: wrap;
}

.speaker-legend-item {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-secondary);
}

.speaker-color-dot {
  width: 10px;
  height: 10px;
  border-radius: 3px;
  flex-shrink: 0;
}

.speaker-label {
  font-weight: 500;
}

.speaker-delete-btn {
  width: 20px;
  height: 20px;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  color: var(--text-secondary);
  border: 1px solid transparent;
  border-radius: 4px;
  cursor: pointer;
  opacity: 0;
  transition: all 0.15s;
}

.speaker-legend-item:hover .speaker-delete-btn {
  opacity: 0.6;
}

.speaker-delete-btn:hover {
  opacity: 1 !important;
  color: var(--danger);
  background: rgba(231, 76, 60, 0.15);
  border-color: rgba(231, 76, 60, 0.3);
}
</style>
