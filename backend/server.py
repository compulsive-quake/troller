import asyncio
import json
import os
import sys
import uuid
import shutil
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="Troller Voice Changer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent.parent
SEED_VC_DIR = BASE_DIR / "seed-vc"
MODELS_DIR = BASE_DIR / "models"
REFERENCES_DIR = BASE_DIR / "references"
OUTPUT_DIR = BASE_DIR / "output"
TRAINING_DIR = BASE_DIR / "training_data"

for d in [MODELS_DIR, REFERENCES_DIR, OUTPUT_DIR, TRAINING_DIR]:
    d.mkdir(exist_ok=True)

# Global state
voice_engine = None
training_jobs: dict = {}


def get_seed_vc_path():
    if not SEED_VC_DIR.exists():
        return None
    if str(SEED_VC_DIR) not in sys.path:
        sys.path.insert(0, str(SEED_VC_DIR))
    return SEED_VC_DIR


@app.get("/api/status")
async def status():
    seed_vc_exists = SEED_VC_DIR.exists()
    return {
        "status": "running",
        "seed_vc_installed": seed_vc_exists,
        "models_dir": str(MODELS_DIR),
        "references_dir": str(REFERENCES_DIR),
    }


@app.post("/api/setup")
async def setup_seed_vc():
    """Clone seed-vc repo and install deps, streaming progress via SSE."""
    from starlette.responses import StreamingResponse

    if SEED_VC_DIR.exists():
        return {"status": "already_installed", "path": str(SEED_VC_DIR)}

    async def stream_setup():
        # Step 1: Clone
        yield f"data: Cloning seed-vc repository...\n\n"
        process = await asyncio.create_subprocess_exec(
            "git", "clone", "--progress", "https://github.com/Plachtaa/seed-vc.git", str(SEED_VC_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # git clone writes progress to stderr
        async def read_lines(stream):
            buf = b""
            while True:
                chunk = await stream.read(256)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf or b"\r" in buf:
                    idx_n = buf.find(b"\n")
                    idx_r = buf.find(b"\r")
                    if idx_n == -1:
                        idx = idx_r
                    elif idx_r == -1:
                        idx = idx_n
                    else:
                        idx = min(idx_n, idx_r)
                    line = buf[:idx].decode(errors="replace").strip()
                    buf = buf[idx + 1:]
                    if line:
                        yield line

        async for line in read_lines(process.stderr):
            yield f"data: {line}\n\n"

        await process.wait()
        if process.returncode != 0:
            yield f"data: ERROR: Failed to clone seed-vc (exit code {process.returncode})\n\n"
            return

        yield f"data: Clone complete. Installing dependencies...\n\n"

        # Step 2: Install pip deps
        req_file = SEED_VC_DIR / "requirements.txt"
        if req_file.exists():
            pip_proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "--user", "-r", str(req_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async for line in read_lines(pip_proc.stdout):
                yield f"data: {line}\n\n"
            await pip_proc.wait()
            if pip_proc.returncode != 0:
                yield f"data: ERROR: pip install failed (exit code {pip_proc.returncode})\n\n"
                return

        yield f"data: DONE\n\n"

    return StreamingResponse(stream_setup(), media_type="text/event-stream")


@app.get("/api/models")
async def list_models():
    """List available voice models and reference audio files."""
    models = []

    # Built-in seed-vc models
    builtin = [
        {
            "id": "seed-uvit-tat-xlsr-tiny",
            "name": "Real-time VC (v1.0 - Tiny)",
            "type": "realtime",
            "version": "v1",
            "sample_rate": 22050,
            "config": "config_dit_mel_seed_uvit_xlsr_tiny.yml",
        },
        {
            "id": "seed-uvit-whisper-small-wavenet",
            "name": "Offline VC (v1.0 - Small)",
            "type": "offline",
            "version": "v1",
            "sample_rate": 22050,
            "config": "config_dit_mel_seed_uvit_whisper_small_wavenet.yml",
        },
        {
            "id": "seed-uvit-whisper-base-f0-44k",
            "name": "Singing VC (v1.0)",
            "type": "singing",
            "version": "v1",
            "sample_rate": 44100,
            "config": "config_dit_mel_seed_uvit_whisper_base_f0_44k.yml",
        },
        {
            "id": "v2-hubert-bsqvae",
            "name": "Voice & Accent VC (v2.0)",
            "type": "offline",
            "version": "v2",
            "sample_rate": 22050,
            "config": None,
        },
    ]
    models.extend(builtin)

    # Custom fine-tuned models
    if MODELS_DIR.exists():
        for model_dir in MODELS_DIR.iterdir():
            if model_dir.is_dir():
                meta_file = model_dir / "meta.json"
                if meta_file.exists():
                    with open(meta_file) as f:
                        meta = json.load(f)
                    meta["id"] = model_dir.name
                    meta["type"] = "custom"
                    models.append(meta)

    return {"models": models}


@app.get("/api/references")
async def list_references():
    """List uploaded reference voice audio files."""
    refs = []
    if REFERENCES_DIR.exists():
        for f in REFERENCES_DIR.iterdir():
            if f.suffix.lower() in (".wav", ".mp3", ".flac", ".m4a", ".ogg"):
                refs.append({
                    "id": f.stem,
                    "filename": f.name,
                    "path": str(f),
                })
    return {"references": refs}


@app.post("/api/references/upload")
async def upload_reference(file: UploadFile = File(...), name: str = Form("")):
    """Upload a reference voice audio file."""
    ref_name = name or file.filename or f"ref_{uuid.uuid4().hex[:8]}"
    safe_name = "".join(c for c in ref_name if c.isalnum() or c in "-_ ").strip()
    ext = Path(file.filename).suffix if file.filename else ".wav"
    dest = REFERENCES_DIR / f"{safe_name}{ext}"

    with open(dest, "wb") as f:
        content = await file.read()
        f.write(content)

    return {"status": "uploaded", "reference": {"id": safe_name, "filename": dest.name, "path": str(dest)}}


@app.delete("/api/references/{ref_id}")
async def delete_reference(ref_id: str):
    """Delete a reference voice audio file."""
    for f in REFERENCES_DIR.iterdir():
        if f.stem == ref_id:
            f.unlink()
            return {"status": "deleted"}
    return JSONResponse(status_code=404, content={"error": "Reference not found"})


@app.post("/api/convert")
async def convert_voice(
    source: UploadFile = File(...),
    reference_id: str = Form(...),
    model_id: str = Form("seed-uvit-whisper-small-wavenet"),
    diffusion_steps: int = Form(25),
    f0_condition: bool = Form(False),
    auto_f0_adjust: bool = Form(True),
    semi_tone_shift: int = Form(0),
):
    """Convert a voice file using the selected model and reference."""
    seed_vc = get_seed_vc_path()
    if not seed_vc:
        return JSONResponse(status_code=400, content={"error": "seed-vc not installed. Call /api/setup first."})

    # Save source to temp file
    source_path = OUTPUT_DIR / f"src_{uuid.uuid4().hex[:8]}{Path(source.filename).suffix}"
    with open(source_path, "wb") as f:
        f.write(await source.read())

    # Find reference file
    ref_path = None
    for f in REFERENCES_DIR.iterdir():
        if f.stem == reference_id:
            ref_path = f
            break
    if not ref_path:
        return JSONResponse(status_code=404, content={"error": "Reference not found"})

    output_path = OUTPUT_DIR / f"out_{uuid.uuid4().hex[:8]}.wav"

    # Check if this is a custom fine-tuned model
    custom_model_dir = MODELS_DIR / model_id
    is_custom = custom_model_dir.exists() and (custom_model_dir / "ft_model.pth").exists()

    # Determine which inference script to use
    is_v2 = model_id.startswith("v2-")
    script = "inference_v2.py" if is_v2 else "inference.py"

    cmd = [sys.executable, str(seed_vc / script)]
    if is_v2:
        cmd.extend([
            "--source", str(source_path),
            "--target", str(ref_path),
            "--output", str(output_path.parent),
            "--intelligibility-cfg-rate", "0.7",
            "--similarity-cfg-rate", "0.7",
        ])
    else:
        cmd.extend([
            "--source", str(source_path),
            "--target", str(ref_path),
            "--output", str(output_path.parent),
            "--diffusion-steps", str(diffusion_steps),
            "--fp16", "True",
        ])
        if is_custom:
            # Load custom fine-tuned checkpoint and its config
            cmd.extend(["--checkpoint", str(custom_model_dir / "ft_model.pth")])
            config_files = list(custom_model_dir.glob("*.yml"))
            if config_files:
                cmd.extend(["--config", str(config_files[0])])
        if f0_condition:
            cmd.extend(["--f0-condition", "True"])
        if auto_f0_adjust:
            cmd.extend(["--auto-f0-adjust", "True"])
        if semi_tone_shift != 0:
            cmd.extend(["--semi-tone-shift", str(semi_tone_shift)])

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(seed_vc),
    )
    stdout, stderr = await process.communicate()

    # Clean up source temp
    source_path.unlink(missing_ok=True)

    if process.returncode != 0:
        return JSONResponse(
            status_code=500,
            content={"error": f"Conversion failed: {stderr.decode()[-500:]}"},
        )

    # Find the output file (seed-vc writes to the output dir)
    output_files = sorted(OUTPUT_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    if output_files:
        return FileResponse(output_files[0], media_type="audio/wav", filename="converted.wav")

    return JSONResponse(status_code=500, content={"error": "No output file generated"})


@app.post("/api/tts")
async def text_to_speech(
    text: str = Form(...),
    reference_id: str = Form(...),
    model_id: str = Form("seed-uvit-whisper-small-wavenet"),
    tts_voice: str = Form("en-US-GuyNeural"),
    speed: float = Form(1.0),
    diffusion_steps: int = Form(25),
):
    """Generate speech from text, then convert to the reference voice using seed-vc."""
    seed_vc = get_seed_vc_path()
    if not seed_vc:
        return JSONResponse(status_code=400, content={"error": "seed-vc not installed. Call /api/setup first."})

    if not text.strip():
        return JSONResponse(status_code=400, content={"error": "Text cannot be empty"})

    # Find reference file
    ref_path = None
    for f in REFERENCES_DIR.iterdir():
        if f.stem == reference_id:
            ref_path = f
            break
    if not ref_path:
        return JSONResponse(status_code=404, content={"error": "Reference not found"})

    # Step 1: Generate base TTS audio using edge-tts
    tts_output = OUTPUT_DIR / f"tts_base_{uuid.uuid4().hex[:8]}.mp3"
    speed_pct = int((speed - 1.0) * 100)
    speed_str = f"+{speed_pct}%" if speed_pct >= 0 else f"{speed_pct}%"

    try:
        import edge_tts
        communicate = edge_tts.Communicate(text, tts_voice, rate=speed_str)
        await communicate.save(str(tts_output))
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"TTS generation failed: {str(e)}"})

    if not tts_output.exists() or tts_output.stat().st_size == 0:
        tts_output.unlink(missing_ok=True)
        return JSONResponse(status_code=500, content={"error": "TTS produced no audio"})

    # Step 2: Run seed-vc voice conversion on the TTS output
    is_v2 = model_id.startswith("v2-")
    script = "inference_v2.py" if is_v2 else "inference.py"

    custom_model_dir = MODELS_DIR / model_id
    is_custom = custom_model_dir.exists() and (custom_model_dir / "ft_model.pth").exists()

    cmd = [sys.executable, str(seed_vc / script)]
    if is_v2:
        cmd.extend([
            "--source", str(tts_output),
            "--target", str(ref_path),
            "--output", str(OUTPUT_DIR),
            "--intelligibility-cfg-rate", "0.7",
            "--similarity-cfg-rate", "0.7",
        ])
    else:
        cmd.extend([
            "--source", str(tts_output),
            "--target", str(ref_path),
            "--output", str(OUTPUT_DIR),
            "--diffusion-steps", str(diffusion_steps),
            "--fp16", "True",
        ])
        if is_custom:
            cmd.extend(["--checkpoint", str(custom_model_dir / "ft_model.pth")])
            config_files = list(custom_model_dir.glob("*.yml"))
            if config_files:
                cmd.extend(["--config", str(config_files[0])])

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(seed_vc),
    )
    stdout, stderr = await process.communicate()

    # Clean up TTS temp file
    tts_output.unlink(missing_ok=True)

    if process.returncode != 0:
        return JSONResponse(
            status_code=500,
            content={"error": f"Voice conversion failed: {stderr.decode()[-500:]}"},
        )

    # Find the output file
    output_files = sorted(OUTPUT_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    if output_files:
        return FileResponse(output_files[0], media_type="audio/wav", filename="tts_output.wav")

    return JSONResponse(status_code=500, content={"error": "No output file generated"})


@app.post("/api/train/start")
async def start_training(
    model_base: str = Form("seed-uvit-tat-xlsr-tiny"),
    run_name: str = Form(...),
    batch_size: int = Form(2),
    max_steps: int = Form(1000),
    save_every: int = Form(500),
):
    """Start a fine-tuning job."""
    seed_vc = get_seed_vc_path()
    if not seed_vc:
        return JSONResponse(status_code=400, content={"error": "seed-vc not installed"})

    dataset_dir = TRAINING_DIR / run_name
    if not dataset_dir.exists():
        return JSONResponse(status_code=400, content={"error": f"Training data directory not found: {dataset_dir}"})

    config_map = {
        "seed-uvit-tat-xlsr-tiny": "config_dit_mel_seed_uvit_xlsr_tiny.yml",
        "seed-uvit-whisper-small-wavenet": "config_dit_mel_seed_uvit_whisper_small_wavenet.yml",
        "seed-uvit-whisper-base-f0-44k": "config_dit_mel_seed_uvit_whisper_base_f0_44k.yml",
    }

    config = config_map.get(model_base)
    if not config:
        return JSONResponse(status_code=400, content={"error": "Invalid base model for training"})

    is_v2 = model_base.startswith("v2-")

    if is_v2:
        cmd = [
            sys.executable, "-m", "accelerate", "launch", str(seed_vc / "train_v2.py"),
            "--dataset-dir", str(dataset_dir),
            "--run-name", run_name,
            "--batch-size", str(batch_size),
            "--train-cfm",
        ]
    else:
        cmd = [
            sys.executable, str(seed_vc / "train.py"),
            "--config", str(seed_vc / "configs" / "presets" / config),
            "--dataset-dir", str(dataset_dir),
            "--run-name", run_name,
            "--batch-size", str(batch_size),
            "--max-steps", str(max_steps),
            "--save-every", str(save_every),
            "--num-workers", "0",
        ]
        # Resume from previous fine-tuned model if it exists
        prev_model = MODELS_DIR / run_name / "ft_model.pth"
        if prev_model.exists():
            cmd.extend(["--pretrained-ckpt", str(prev_model)])

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(seed_vc),
    )

    job_id = uuid.uuid4().hex[:8]
    training_jobs[job_id] = {
        "id": job_id,
        "run_name": run_name,
        "model_base": model_base,
        "status": "running",
        "process": process,
        "max_steps": max_steps,
        "log_lines": [],
        "log_subscribers": set(),
    }

    # Monitor in background
    asyncio.create_task(_monitor_training(job_id))

    return {"job_id": job_id, "status": "started"}


async def _read_stream(stream, job_id: str, stream_name: str):
    """Read a subprocess stream line by line and store in job logs."""
    job = training_jobs[job_id]
    while True:
        line = await stream.readline()
        if not line:
            break
        text = line.decode(errors="replace").rstrip("\n\r")
        log_entry = f"[{stream_name}] {text}"
        job["log_lines"].append(log_entry)
        # Notify all WebSocket subscribers
        dead = set()
        for ws in job["log_subscribers"]:
            try:
                await ws.send_text(log_entry)
            except Exception:
                dead.add(ws)
        job["log_subscribers"] -= dead


async def _monitor_training(job_id: str):
    job = training_jobs[job_id]
    process = job["process"]

    # Read stdout and stderr concurrently, line by line
    await asyncio.gather(
        _read_stream(process.stdout, job_id, "stdout"),
        _read_stream(process.stderr, job_id, "stderr"),
    )
    await process.wait()

    job["status"] = "completed" if process.returncode == 0 else "failed"
    final_msg = f"[system] Training {job['status']} (exit code {process.returncode})"
    job["log_lines"].append(final_msg)
    for ws in job["log_subscribers"]:
        try:
            await ws.send_text(final_msg)
        except Exception:
            pass
    job["log_subscribers"] = set()
    del job["process"]

    # Copy model files and save metadata if successful
    if job["status"] == "completed":
        model_out = MODELS_DIR / job["run_name"]
        model_out.mkdir(exist_ok=True)

        # Copy trained model files from seed-vc/runs/<run_name>/
        runs_dir = SEED_VC_DIR / "runs" / job["run_name"]
        if runs_dir.exists():
            # Copy the final fine-tuned model
            ft_model = runs_dir / "ft_model.pth"
            if ft_model.exists():
                shutil.copy2(ft_model, model_out / "ft_model.pth")
            # Copy the config file
            for cfg_file in runs_dir.glob("*.yml"):
                shutil.copy2(cfg_file, model_out / cfg_file.name)

        # Find the config filename
        config_name = None
        for f in model_out.glob("*.yml"):
            config_name = f.name
            break

        with open(model_out / "meta.json", "w") as f:
            json.dump({
                "name": job["run_name"],
                "base_model": job["model_base"],
                "max_steps": job["max_steps"],
                "config": config_name,
            }, f)


@app.get("/api/train/status/{job_id}")
async def training_status(job_id: str):
    if job_id not in training_jobs:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    job = {k: v for k, v in training_jobs[job_id].items() if k not in ("process", "log_subscribers", "log_lines")}
    job["log_count"] = len(training_jobs[job_id].get("log_lines", []))
    return job


@app.get("/api/train/jobs")
async def list_training_jobs():
    jobs = []
    for job_id, job in training_jobs.items():
        jobs.append({k: v for k, v in job.items() if k not in ("process", "log_subscribers", "log_lines")})
    return {"jobs": jobs}


@app.get("/api/train/logs/{job_id}")
async def get_training_logs(job_id: str, offset: int = 0):
    """Get accumulated log lines for a training job."""
    if job_id not in training_jobs:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    job = training_jobs[job_id]
    lines = job.get("log_lines", [])
    return {"job_id": job_id, "offset": offset, "lines": lines[offset:], "total": len(lines)}


@app.websocket("/ws/train/logs/{job_id}")
async def stream_training_logs(websocket: WebSocket, job_id: str):
    """Stream training logs in real-time via WebSocket."""
    await websocket.accept()

    if job_id not in training_jobs:
        await websocket.send_json({"error": "Job not found"})
        await websocket.close()
        return

    job = training_jobs[job_id]

    # Send all existing log lines first
    for line in job.get("log_lines", []):
        await websocket.send_text(line)

    # If job is already done, close
    if job["status"] != "running":
        await websocket.close()
        return

    # Subscribe for new lines
    job["log_subscribers"].add(websocket)
    try:
        # Keep connection alive until client disconnects or job ends
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        job["log_subscribers"].discard(websocket)


@app.post("/api/train/upload-data")
async def upload_training_data(
    files: list[UploadFile] = File(...),
    run_name: str = Form(...),
):
    """Upload audio files for training."""
    dataset_dir = TRAINING_DIR / run_name
    dataset_dir.mkdir(parents=True, exist_ok=True)

    uploaded = []
    for file in files:
        dest = dataset_dir / (file.filename or f"{uuid.uuid4().hex[:8]}.wav")
        with open(dest, "wb") as f:
            f.write(await file.read())
        uploaded.append(str(dest))

    return {"status": "uploaded", "count": len(uploaded), "directory": str(dataset_dir)}


# ---- Real-time Voice Conversion via WebSocket ----

# Global cache for loaded RT models (heavy: ~1-2 GB on GPU, load once)
_rt_models = None
_rt_model_id = None
_rt_lock = None  # initialized lazily as threading.Lock


def _get_rt_models(model_id="seed-uvit-tat-xlsr-tiny"):
    """Load and cache real-time VC models in-process. Returns dict with model components."""
    global _rt_models, _rt_model_id, _rt_lock
    import threading
    if _rt_lock is None:
        _rt_lock = threading.Lock()

    with _rt_lock:
        if _rt_models is not None and _rt_model_id == model_id:
            return _rt_models

        import torch
        import yaml

        seed_vc = get_seed_vc_path()
        if not seed_vc:
            raise RuntimeError("seed-vc not installed")
        if str(seed_vc) not in sys.path:
            sys.path.insert(0, str(seed_vc))

        from modules.commons import build_model, load_checkpoint, recursive_munch
        from hf_utils import load_custom_model_from_hf
        from modules.audio import mel_spectrogram

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        # Determine checkpoint and config
        custom_model_dir = MODELS_DIR / model_id
        if custom_model_dir.exists() and (custom_model_dir / "ft_model.pth").exists():
            dit_checkpoint_path = str(custom_model_dir / "ft_model.pth")
            config_files = list(custom_model_dir.glob("*.yml"))
            dit_config_path = str(config_files[0]) if config_files else None
        else:
            dit_checkpoint_path, dit_config_path = load_custom_model_from_hf(
                "Plachta/Seed-VC",
                "DiT_uvit_tat_xlsr_ema.pth",
                "config_dit_mel_seed_uvit_xlsr_tiny.yml",
            )

        config = yaml.safe_load(open(dit_config_path, "r"))
        model_params = recursive_munch(config["model_params"])
        model_params.dit_type = "DiT"
        model = build_model(model_params, stage="DiT")
        hop_length = config["preprocess_params"]["spect_params"]["hop_length"]
        sr = config["preprocess_params"]["sr"]

        model, _, _, _ = load_checkpoint(
            model, None, dit_checkpoint_path,
            load_only_params=True, ignore_modules=[], is_distributed=False,
        )
        for key in model:
            model[key].eval()
            model[key].to(device)
        model.cfm.estimator.setup_caches(max_batch_size=1, max_seq_length=8192)

        # CAMPPlus speaker embedding extractor
        from modules.campplus.DTDNN import CAMPPlus
        campplus_ckpt_path = load_custom_model_from_hf(
            "funasr/campplus", "campplus_cn_common.bin", config_filename=None
        )
        campplus_model = CAMPPlus(feat_dim=80, embedding_size=192)
        campplus_model.load_state_dict(torch.load(campplus_ckpt_path, map_location="cpu"))
        campplus_model.eval().to(device)

        # Vocoder
        vocoder_type = model_params.vocoder.type
        if vocoder_type == "bigvgan":
            from modules.bigvgan import bigvgan
            bigvgan_model = bigvgan.BigVGAN.from_pretrained(
                model_params.vocoder.name, use_cuda_kernel=False
            )
            bigvgan_model.remove_weight_norm()
            vocoder_fn = bigvgan_model.eval().to(device)
        elif vocoder_type == "hifigan":
            from modules.hifigan.generator import HiFTGenerator
            from modules.hifigan.f0_predictor import ConvRNNF0Predictor
            hift_config = yaml.safe_load(
                open(str(seed_vc / "configs" / "hifigan.yml"), "r")
            )
            hift_gen = HiFTGenerator(
                **hift_config["hift"],
                f0_predictor=ConvRNNF0Predictor(**hift_config["f0_predictor"]),
            )
            hift_path = load_custom_model_from_hf(
                "FunAudioLLM/CosyVoice-300M", "hift.pt", None
            )
            hift_gen.load_state_dict(torch.load(hift_path, map_location="cpu"))
            vocoder_fn = hift_gen.eval().to(device)
        elif vocoder_type == "vocos":
            vocos_config = yaml.safe_load(
                open(model_params.vocoder.vocos.config, "r")
            )
            vocos_model_params = recursive_munch(vocos_config["model_params"])
            vocos = build_model(vocos_model_params, stage="mel_vocos")
            vocos, _, _, _ = load_checkpoint(
                vocos, None, model_params.vocoder.vocos.path,
                load_only_params=True, ignore_modules=[], is_distributed=False,
            )
            for key in vocos:
                vocos[key].eval().to(device)
            vocoder_fn = vocos.decoder
        else:
            raise ValueError(f"Unknown vocoder type: {vocoder_type}")

        # Speech tokenizer (semantic feature extractor)
        speech_tokenizer_type = model_params.speech_tokenizer.type
        if speech_tokenizer_type == "xlsr":
            from transformers import Wav2Vec2FeatureExtractor, Wav2Vec2Model
            st_name = config["model_params"]["speech_tokenizer"]["name"]
            output_layer = config["model_params"]["speech_tokenizer"]["output_layer"]
            feat_ext = Wav2Vec2FeatureExtractor.from_pretrained(st_name)
            wav2vec = Wav2Vec2Model.from_pretrained(st_name)
            wav2vec.encoder.layers = wav2vec.encoder.layers[:output_layer]
            wav2vec = wav2vec.to(device).eval().half()

            def semantic_fn(waves_16k):
                inputs = feat_ext(
                    [waves_16k[b].cpu().numpy() for b in range(len(waves_16k))],
                    return_tensors="pt", return_attention_mask=True,
                    padding=True, sampling_rate=16000,
                ).to(device)
                with torch.no_grad():
                    return wav2vec(inputs.input_values.half()).last_hidden_state.float()

        elif speech_tokenizer_type == "whisper":
            from transformers import AutoFeatureExtractor, WhisperModel
            whisper_name = model_params.speech_tokenizer.name
            whisper_model = WhisperModel.from_pretrained(
                whisper_name, torch_dtype=torch.float16
            ).to(device)
            del whisper_model.decoder
            whisper_feat_ext = AutoFeatureExtractor.from_pretrained(whisper_name)

            def semantic_fn(waves_16k):
                inputs = whisper_feat_ext(
                    [waves_16k.squeeze(0).cpu().numpy()],
                    return_tensors="pt", return_attention_mask=True,
                )
                feats = whisper_model._mask_input_features(
                    inputs.input_features, attention_mask=inputs.attention_mask
                ).to(device)
                with torch.no_grad():
                    out = whisper_model.encoder(
                        feats.to(whisper_model.encoder.dtype),
                        head_mask=None, output_attentions=False,
                        output_hidden_states=False, return_dict=True,
                    )
                S = out.last_hidden_state.to(torch.float32)
                return S[:, : waves_16k.size(-1) // 320 + 1]

        elif speech_tokenizer_type == "cnhubert":
            from transformers import Wav2Vec2FeatureExtractor, HubertModel
            hubert_name = config["model_params"]["speech_tokenizer"]["name"]
            hubert_feat_ext = Wav2Vec2FeatureExtractor.from_pretrained(hubert_name)
            hubert_model = HubertModel.from_pretrained(hubert_name)
            hubert_model = hubert_model.to(device).eval().half()

            def semantic_fn(waves_16k):
                inputs = hubert_feat_ext(
                    [waves_16k[b].cpu().numpy() for b in range(len(waves_16k))],
                    return_tensors="pt", return_attention_mask=True,
                    padding=True, sampling_rate=16000,
                ).to(device)
                with torch.no_grad():
                    return hubert_model(inputs.input_values.half()).last_hidden_state.float()
        else:
            raise ValueError(f"Unknown speech tokenizer: {speech_tokenizer_type}")

        # Mel spectrogram function
        mel_fn_args = {
            "n_fft": config["preprocess_params"]["spect_params"]["n_fft"],
            "win_size": config["preprocess_params"]["spect_params"]["win_length"],
            "hop_size": hop_length,
            "num_mels": config["preprocess_params"]["spect_params"]["n_mels"],
            "sampling_rate": sr,
            "fmin": config["preprocess_params"]["spect_params"].get("fmin", 0),
            "fmax": None
            if config["preprocess_params"]["spect_params"].get("fmax", "None") == "None"
            else 8000,
            "center": False,
        }
        to_mel = lambda x: mel_spectrogram(x, **mel_fn_args)

        _rt_models = {
            "model": model,
            "semantic_fn": semantic_fn,
            "vocoder_fn": vocoder_fn,
            "campplus_model": campplus_model,
            "to_mel": to_mel,
            "mel_fn_args": mel_fn_args,
            "device": device,
            "sr": sr,
            "hop_length": hop_length,
        }
        _rt_model_id = model_id
        print(f"RT models loaded: {model_id} (sr={sr}, hop={hop_length}, device={device})")
        return _rt_models


class RealtimeVCSession:
    """Real-time voice conversion session with in-process model inference.

    Keeps models loaded on GPU and processes audio chunks directly through
    the inference pipeline (semantic extraction -> DiT -> vocoder) without
    spawning subprocesses. Follows the same streaming architecture as
    seed-vc's real-time-gui.py with SOLA crossfading for smooth output.
    """

    # Streaming parameters (matching real-time-gui.py defaults)
    EXTRA_TIME_CE = 2.5      # left context for content encoder (seconds)
    EXTRA_TIME = 0.5          # left context for DiT (seconds)
    EXTRA_TIME_RIGHT = 2.0    # right context (seconds)
    CROSSFADE_TIME = 0.05     # SOLA crossfade (seconds)
    DIFFUSION_STEPS = 10
    INFERENCE_CFG_RATE = 0.7
    MAX_PROMPT_LENGTH = 3.0   # max reference audio length (seconds)
    FP16 = True

    def __init__(self, reference_path: str, model_id: str = "seed-uvit-tat-xlsr-tiny"):
        self.reference_path = reference_path
        self.model_id = model_id
        self.models = None
        self.initialized = False

        # Reference features (computed once per session)
        self.prompt_condition = None
        self.mel2 = None
        self.style2 = None

        # Streaming state
        self.input_buf_16k = None
        self.sola_buffer = None
        self.fade_in_window = None
        self.fade_out_window = None

    def initialize(self):
        """Load models (cached globally) and preprocess reference audio."""
        import torch
        import torchaudio
        import librosa

        self.models = _get_rt_models(self.model_id)
        device = self.models["device"]
        sr = self.models["sr"]
        model = self.models["model"]
        to_mel = self.models["to_mel"]
        campplus_model = self.models["campplus_model"]
        semantic_fn = self.models["semantic_fn"]

        # ---- Preprocess reference audio (done once) ----
        ref_audio, _ = librosa.load(self.reference_path, sr=sr)
        ref_audio = ref_audio[: int(self.MAX_PROMPT_LENGTH * sr)]
        ref_tensor = torch.from_numpy(ref_audio).float().to(device)

        ref_16k = torchaudio.functional.resample(ref_tensor, sr, 16000)

        S_ori = semantic_fn(ref_16k.unsqueeze(0))

        feat2 = torchaudio.compliance.kaldi.fbank(
            ref_16k.unsqueeze(0), num_mel_bins=80, dither=0, sample_frequency=16000
        )
        feat2 = feat2 - feat2.mean(dim=0, keepdim=True)
        self.style2 = campplus_model(feat2.unsqueeze(0))

        self.mel2 = to_mel(ref_tensor.unsqueeze(0))

        target2_lengths = torch.LongTensor([self.mel2.size(2)]).to(device)
        self.prompt_condition = model.length_regulator(
            S_ori, ylens=target2_lengths, n_quantizers=3, f0=None
        )[0]

        # ---- Set up streaming buffers ----
        zc = sr // 50  # samples per 20ms frame at model sr
        self.zc = zc
        self.sr = sr
        self.device = device

        # Block size aligned to zc (matches ~256ms from frontend's 4096 @ 16kHz)
        block_time = 0.25
        self.block_frame = int(np.round(block_time * sr / zc)) * zc

        # Extra context frames at model sr
        extra_ce = int(np.round(self.EXTRA_TIME_CE * sr / zc)) * zc
        extra_right = int(np.round(self.EXTRA_TIME_RIGHT * sr / zc)) * zc
        crossfade = int(np.round(self.CROSSFADE_TIME * sr / zc)) * zc

        self.sola_buffer_frame = min(crossfade, 4 * zc)
        self.sola_search_frame = zc
        self.extra_frame = extra_ce
        self.extra_frame_right = extra_right

        # Total buffer at 16kHz (mapped from model sr buffer)
        total_model_samples = extra_ce + crossfade + self.sola_search_frame + self.block_frame + extra_right
        total_16k_samples = 320 * total_model_samples // zc
        self.input_buf_16k = torch.zeros(total_16k_samples, device=device, dtype=torch.float32)

        # SOLA crossfade state (at model sr)
        self.sola_buffer = torch.zeros(self.sola_buffer_frame, device=device, dtype=torch.float32)
        self.fade_in_window = (
            torch.sin(0.5 * np.pi * torch.linspace(0, 1, steps=self.sola_buffer_frame, device=device))
            ** 2
        )
        self.fade_out_window = 1 - self.fade_in_window

        # Frame counts for inference (in 20ms frames)
        self.skip_head = extra_ce // zc
        self.skip_tail = extra_right // zc
        self.return_length = (self.block_frame + self.sola_buffer_frame + self.sola_search_frame) // zc
        self.ce_dit_difference = self.EXTRA_TIME_CE - self.EXTRA_TIME

        # Resampler: model sr -> 16kHz (for sending output to frontend)
        self.resampler_to_16k = torchaudio.transforms.Resample(
            orig_freq=sr, new_freq=16000, dtype=torch.float32
        ).to(device)

        self.initialized = True
        print(f"RT session ready (ref={self.reference_path}, model={self.model_id})")

    def process_chunk(self, audio_16k: np.ndarray) -> np.ndarray:
        """Process a 16kHz float32 audio chunk. Returns 16kHz float32 output."""
        import torch
        import torch.nn.functional as F

        if not self.initialized:
            self.initialize()

        model = self.models["model"]
        semantic_fn = self.models["semantic_fn"]
        vocoder_fn = self.models["vocoder_fn"]

        # Shift rolling buffer left and append new audio
        n = len(audio_16k)
        self.input_buf_16k[:-n] = self.input_buf_16k[n:].clone()
        self.input_buf_16k[-n:] = torch.from_numpy(audio_16k).to(self.device)

        # ---- Inference (mirrors real-time-gui.py custom_infer) ----
        with torch.no_grad():
            # 1. Extract semantic features from full 16kHz buffer
            S_alt = semantic_fn(self.input_buf_16k.unsqueeze(0))

            # 2. Skip content-encoder/DiT context difference
            ce_dit_frame_diff = int(self.ce_dit_difference * 50)
            S_alt = S_alt[:, ce_dit_frame_diff:]

            # 3. Length-regulate source features -> mel-frame-aligned condition
            remaining_frames = self.skip_head + self.return_length + self.skip_tail - ce_dit_frame_diff
            target_lengths = torch.LongTensor(
                [int(remaining_frames / 50 * self.sr // self.models["hop_length"])]
            ).to(self.device)
            cond = model.length_regulator(S_alt, ylens=target_lengths, n_quantizers=3, f0=None)[0]

            # 4. Concatenate reference prompt + source condition
            cat_condition = torch.cat([self.prompt_condition, cond], dim=1)

            # 5. Diffusion inference
            with torch.autocast(
                device_type=self.device.type,
                dtype=torch.float16 if self.FP16 else torch.float32,
            ):
                vc_target = model.cfm.inference(
                    cat_condition,
                    torch.LongTensor([cat_condition.size(1)]).to(self.device),
                    self.mel2,
                    self.style2,
                    None,
                    n_timesteps=self.DIFFUSION_STEPS,
                    inference_cfg_rate=self.INFERENCE_CFG_RATE,
                )
                vc_target = vc_target[:, :, self.mel2.size(-1) :]

                # 6. Vocoder: mel -> waveform (at model sr)
                vc_wave = vocoder_fn(vc_target).squeeze()

        # 7. Extract the return portion
        output_len = self.return_length * self.sr // 50
        tail_len = self.skip_tail * self.sr // 50
        if tail_len > 0:
            infer_wav = vc_wave[-output_len - tail_len : -tail_len]
        else:
            infer_wav = vc_wave[-output_len:]

        # 8. SOLA crossfade for smooth block boundaries
        conv_input = infer_wav[None, None, : self.sola_buffer_frame + self.sola_search_frame]
        cor_nom = F.conv1d(conv_input, self.sola_buffer[None, None, :])
        cor_den = torch.sqrt(
            F.conv1d(
                conv_input ** 2,
                torch.ones(1, 1, self.sola_buffer_frame, device=self.device),
            )
            + 1e-8
        )
        if cor_nom.numel() > 1:
            sola_offset = torch.argmax(cor_nom[0, 0] / cor_den[0, 0]).item()
        else:
            sola_offset = 0

        infer_wav = infer_wav[sola_offset:]
        infer_wav[: self.sola_buffer_frame] *= self.fade_in_window
        infer_wav[: self.sola_buffer_frame] += self.sola_buffer * self.fade_out_window
        self.sola_buffer[:] = infer_wav[
            self.block_frame : self.block_frame + self.sola_buffer_frame
        ]

        output_wav = infer_wav[: self.block_frame]

        # 9. Resample model sr -> 16kHz for frontend
        output_16k = self.resampler_to_16k(output_wav.unsqueeze(0)).squeeze(0)
        return output_16k.cpu().numpy()


@app.websocket("/ws/realtime")
async def realtime_voice_conversion(websocket: WebSocket):
    await websocket.accept()

    session = None
    try:
        # First message is config JSON
        config_msg = await websocket.receive_text()
        config = json.loads(config_msg)

        reference_id = config.get("reference_id", "")
        model_id = config.get("model_id", "seed-uvit-tat-xlsr-tiny")

        # Find reference audio file
        ref_path = None
        for f in REFERENCES_DIR.iterdir():
            if f.stem == reference_id:
                ref_path = str(f)
                break

        if not ref_path:
            await websocket.send_json({"error": "Reference not found"})
            await websocket.close()
            return

        # Initialize session: loads models (cached) + preprocesses reference
        session = RealtimeVCSession(ref_path, model_id)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, session.initialize)
        await websocket.send_json({"status": "ready"})

        # Stream audio chunks
        while True:
            audio_bytes = await websocket.receive_bytes()
            audio_chunk = np.frombuffer(audio_bytes, dtype=np.float32)
            output = await loop.run_in_executor(None, session.process_chunk, audio_chunk)
            await websocket.send_bytes(output.astype(np.float32).tobytes())

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"RT WebSocket error: {e}")
        import traceback
        traceback.print_exc()
        try:
            await websocket.send_json({"error": str(e)})
        except Exception:
            pass


@app.get("/api/output/{filename}")
async def get_output_file(filename: str):
    file_path = OUTPUT_DIR / filename
    if not file_path.exists():
        return JSONResponse(status_code=404, content={"error": "File not found"})
    return FileResponse(file_path, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
