# Troller

Real-time voice changer desktop app powered by seed-vc.

## Environment

- Platform: Windows 11
- Models: Opus 4.6

## Tech Stack
- **Frontend**: Vue 3 + TypeScript
- **Desktop Shell**: Tauri v2 (Rust)
- **Backend**: Python FastAPI server (port 8765)
- **Voice Engine**: [seed-vc](https://github.com/Plachtaa/seed-vc)

## Architecture

Tauri app spawns a Python FastAPI backend process on launch. Frontend communicates with backend via HTTP REST and WebSocket.

```
src/                  Vue 3 frontend
src-tauri/            Tauri Rust shell (manages Python process lifecycle)
backend/              Python FastAPI server wrapping seed-vc
seed-vc/              Cloned seed-vc repo (git-ignored, installed via Setup tab or /api/setup)
models/               Custom fine-tuned models (git-ignored)
references/           Reference voice audio files (git-ignored)
output/               Conversion output files (git-ignored)
training_data/        Training datasets (git-ignored)
```

## Features

### Real-Time Voice Changer (`src/views/RealTimeVC.vue`)
- Captures microphone audio via Web Audio API
- Streams audio chunks over WebSocket to backend (`/ws/realtime`)
- Backend runs seed-vc inference (tiny model, 10 diffusion steps) and returns converted audio
- Configurable input/output devices, reference voice, volume

### File-Based Voice Conversion (`src/views/VoiceConvert.vue`)
- Upload source audio file + select reference voice
- Supports v1 models (offline, singing) and v2 model
- Adjustable diffusion steps, F0 conditioning, auto F0 adjust, semitone shift
- Download converted WAV output

### Model Training (`src/views/TrainingPanel.vue`)
- Upload training audio data (WAV/FLAC/MP3, 1-30s clips)
- Fine-tune v1 models (real-time tiny, offline small, singing) or v2
- Configurable batch size, max steps, save interval
- Background training job monitoring

### Model & Reference Manager (`src/views/ModelManager.vue`)
- Upload/delete reference voice audio files
- View built-in seed-vc models (v1 tiny, v1 small, v1 singing, v2)
- View custom fine-tuned models

### Setup (`src/views/SetupPanel.vue`)
- One-click seed-vc installation (clones repo + installs pip deps)
- System status display (backend server, seed-vc engine)

## API Endpoints (backend/server.py)

- `GET  /api/status` - Backend and seed-vc status
- `POST /api/setup` - Clone and install seed-vc
- `GET  /api/models` - List available models
- `GET  /api/references` - List reference voices
- `POST /api/references/upload` - Upload reference audio
- `DELETE /api/references/{id}` - Delete reference
- `POST /api/convert` - File-based voice conversion
- `POST /api/train/start` - Start training job
- `GET  /api/train/status/{id}` - Training job status
- `GET  /api/train/jobs` - List all training jobs
- `POST /api/train/upload-data` - Upload training audio
- `WS   /ws/realtime` - Real-time voice conversion stream

## Running

1. `pip install -r backend/requirements.txt`
2. `npm install`
3. `npm run tauri dev`
4. Install seed-vc via Setup tab (first run only)
5. Upload reference voice in Models tab
6. Use Real-Time VC or Convert File
