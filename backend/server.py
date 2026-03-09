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
cuda_enabled: bool = True  # When False, force CPU even if CUDA is available


def get_seed_vc_path():
    if not SEED_VC_DIR.exists():
        return None
    if str(SEED_VC_DIR) not in sys.path:
        sys.path.insert(0, str(SEED_VC_DIR))
    return SEED_VC_DIR


def _refresh_path():
    """Reload PATH from the Windows registry so newly-installed tools are found."""
    if sys.platform == "win32":
        import winreg
        parts = []
        for hive, subkey in [
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, r"Environment"),
        ]:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    val, _ = winreg.QueryValueEx(key, "Path")
                    parts.append(val)
            except OSError:
                pass
        if parts:
            os.environ["PATH"] = ";".join(parts)


@app.get("/api/prerequisites")
async def check_prerequisites():
    """Check all required system tools and return their status."""
    _refresh_path()
    results = []

    # 1. Python
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 8)
    results.append({
        "id": "python",
        "name": "Python 3.8+",
        "installed": py_ok,
        "version": py_version,
        "description": "Required to run the backend server and seed-vc engine",
        "install_hint": "Download from https://www.python.org/downloads/",
        "auto_install": False,
    })

    # 2. pip
    pip_version = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            pip_version = stdout.decode().strip().split()[1]
    except FileNotFoundError:
        pass
    results.append({
        "id": "pip",
        "name": "pip",
        "installed": pip_version is not None,
        "version": pip_version,
        "description": "Python package manager for installing dependencies",
        "install_hint": "Run: python -m ensurepip --upgrade",
        "auto_install": False,
    })

    # 4. ffmpeg
    ffmpeg_version = None
    ffmpeg_candidates = ["ffmpeg"]
    if sys.platform == "win32":
        # Common winget / manual install locations
        for base in [
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
            r"C:\ffmpeg\bin",
            r"C:\Program Files\ffmpeg\bin",
            r"C:\tools\ffmpeg\bin",
        ]:
            candidate = os.path.join(base, "ffmpeg.exe")
            if os.path.isfile(candidate):
                ffmpeg_candidates.insert(0, candidate)
                break
    for ffmpeg_bin in ffmpeg_candidates:
        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_bin, "-version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                first_line = stdout.decode().split("\n")[0]
                ffmpeg_version = first_line.strip()
                break
        except FileNotFoundError:
            continue
    results.append({
        "id": "ffmpeg",
        "name": "FFmpeg",
        "installed": ffmpeg_version is not None,
        "version": ffmpeg_version,
        "description": "Required for audio format conversion (used by librosa/torchaudio)",
        "install_hint": "Download from https://ffmpeg.org/download.html or run: winget install ffmpeg",
        "auto_install": True,
    })

    # 5. CUDA / GPU
    cuda_available = False
    cuda_version = None
    try:
        import torch
        cuda_available = torch.cuda.is_available()
        if cuda_available:
            cuda_version = torch.version.cuda
    except ImportError:
        pass
    results.append({
        "id": "cuda",
        "name": "CUDA (GPU)",
        "installed": cuda_available,
        "version": cuda_version,
        "description": "GPU acceleration for real-time voice conversion (optional but strongly recommended)",
        "install_hint": "Install NVIDIA drivers and CUDA toolkit from https://developer.nvidia.com/cuda-downloads",
        "auto_install": False,
        "optional": True,
    })

    # 6. scikit-learn
    sklearn_version = None
    try:
        import importlib.metadata
        sklearn_version = importlib.metadata.version("scikit-learn")
    except Exception:
        pass
    results.append({
        "id": "scikit_learn",
        "name": "scikit-learn",
        "installed": sklearn_version is not None,
        "version": sklearn_version,
        "description": "Machine learning library used for audio feature clustering and analysis",
        "install_hint": "Run: pip install scikit-learn",
        "auto_install": True,
    })

    # 7. Backend Python packages
    backend_req = BASE_DIR / "backend" / "requirements.txt"
    missing_packages = []
    if backend_req.exists():
        import importlib.metadata
        for line in backend_req.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pkg_name = line.split(">=")[0].split("==")[0].split("[")[0].strip()
            # Map pip names to importable names
            import_map = {
                "pyyaml": "yaml",
                "python-multipart": "multipart",
                "uvicorn": "uvicorn",
                "edge-tts": "edge_tts",
                "huggingface_hub": "huggingface_hub",
            }
            lookup = import_map.get(pkg_name, pkg_name.replace("-", "_"))
            try:
                importlib.metadata.version(pkg_name)
            except importlib.metadata.PackageNotFoundError:
                # Try alternate name
                try:
                    importlib.metadata.version(lookup)
                except importlib.metadata.PackageNotFoundError:
                    missing_packages.append(line)

    results.append({
        "id": "backend_packages",
        "name": "Backend Python Packages",
        "installed": len(missing_packages) == 0,
        "version": f"{len(missing_packages)} missing" if missing_packages else "All installed",
        "description": "Python dependencies for the backend server (FastAPI, PyTorch, etc.)",
        "install_hint": f"Run: pip install -r backend/requirements.txt",
        "auto_install": True,
        "missing": missing_packages,
    })

    # 7. huggingface_hub[hf_xet]
    hf_xet_installed = False
    try:
        importlib.metadata.version("hf_xet")
        hf_xet_installed = True
    except Exception:
        pass
    results.append({
        "id": "hf_xet",
        "name": "HuggingFace XET Extension",
        "installed": hf_xet_installed,
        "version": None,
        "description": "Fast model downloads from HuggingFace Hub",
        "install_hint": 'Run: pip install "huggingface_hub[hf_xet]"',
        "auto_install": True,
    })

    # 8. yt-dlp (for YouTube audio import)
    yt_dlp_version = None
    yt_dlp_cmd = _find_yt_dlp()
    if yt_dlp_cmd:
        try:
            proc = await asyncio.create_subprocess_exec(
                *yt_dlp_cmd, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                yt_dlp_version = stdout.decode().strip()
        except FileNotFoundError:
            pass
    results.append({
        "id": "yt_dlp",
        "name": "yt-dlp",
        "installed": yt_dlp_version is not None,
        "version": yt_dlp_version,
        "description": "YouTube audio downloader for Import Audio (optional)",
        "install_hint": "Run: pip install yt-dlp",
        "auto_install": True,
        "optional": True,
    })

    # 9. seed-vc
    seed_vc_installed = SEED_VC_DIR.exists()
    results.append({
        "id": "seed_vc",
        "name": "seed-vc Engine",
        "installed": seed_vc_installed,
        "version": None,
        "description": "AI voice conversion engine that powers Troller",
        "install_hint": "Install via the Setup tab",
        "auto_install": True,
    })

    # 10. nemo_toolkit (optional, for speaker diarization)
    nemo_version = None
    try:
        nemo_version = importlib.metadata.version("nemo_toolkit")
    except Exception:
        pass
    results.append({
        "id": "nemo_toolkit",
        "name": "NVIDIA NeMo Toolkit",
        "installed": nemo_version is not None,
        "version": nemo_version,
        "description": "Speaker diarization support (optional, large download ~2GB+)",
        "install_hint": 'Run: pip install cmake Cython && pip install "nemo_toolkit[asr]>=2.0.0"',
        "auto_install": True,
        "optional": True,
    })

    return {"prerequisites": results}


@app.post("/api/prerequisites/install")
async def install_prerequisite(item_id: str = Form(...)):
    """Install a prerequisite by ID."""
    if item_id == "backend_packages":
        req_file = BASE_DIR / "backend" / "requirements.txt"
        if not req_file.exists():
            return JSONResponse(status_code=400, content={"error": "requirements.txt not found"})
        from starlette.responses import StreamingResponse

        # Install steps to work around dependency conflicts:
        # 1. pkuseg needs numpy at build time (--no-build-isolation)
        # 2. descript-audiotools pins protobuf<3.20 which forces downgrades
        #    and breaks onnx/nemo; install it with --no-deps to avoid that
        # 3. Install remaining requirements normally
        PRE_STEPS = [
            (["--no-build-isolation", "pkuseg==0.0.25"], "Pre-installing pkuseg (no build isolation)..."),
            (["--no-deps", "descript-audiotools>=0.7.2"], "Installing descript-audiotools (skipping deps to avoid protobuf conflict)..."),
        ]
        # Packages handled in pre-steps that should be skipped in the main install
        SKIP_PKGS = {"descript-audio-codec"}

        async def _pip_install(args):
            """Run pip install, stream output lines, return exit code."""
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            lines = []
            async for raw_line in proc.stdout:
                text = raw_line.decode(errors="replace").rstrip()
                if text:
                    lines.append(text)
            await proc.wait()
            return proc.returncode, lines

        async def stream_backend_install():
            # Pre-steps: install packages that need special handling
            for pip_args, label in PRE_STEPS:
                yield f"data: {json.dumps({'stage': 'progress', 'line': label})}\n\n"
                rc, lines = await _pip_install(pip_args)
                for text in lines:
                    yield f"data: {json.dumps({'stage': 'progress', 'line': text})}\n\n"
                if rc != 0:
                    yield f"data: {json.dumps({'stage': 'error', 'error': f'Failed: {label}'})}\n\n"
                    yield "data: [DONE]\n\n"
                    return

            # Main install: filter out pre-handled packages, install the rest
            all_lines = [
                line.strip() for line in req_file.read_text().splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            main_pkgs = []
            for line in all_lines:
                pkg_name = line.split(">=")[0].split("==")[0].split(">")[0].split("<")[0].split("[")[0].strip().lower()
                if pkg_name not in SKIP_PKGS:
                    main_pkgs.append(line)

            yield f"data: {json.dumps({'stage': 'progress', 'line': 'Installing packages...'})}\n\n"
            rc, lines = await _pip_install(main_pkgs)
            for text in lines:
                yield f"data: {json.dumps({'stage': 'progress', 'line': text})}\n\n"
            if rc == 0:
                yield f"data: {json.dumps({'stage': 'done'})}\n\n"
            else:
                yield f"data: {json.dumps({'stage': 'error', 'error': f'pip exited with code {rc}'})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream_backend_install(), media_type="text/event-stream")

    elif item_id == "hf_xet":
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "huggingface_hub[hf_xet]",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return JSONResponse(status_code=500, content={
                "error": f"pip install failed: {stderr.decode()[-500:]}"
            })
        return {"status": "installed", "id": item_id, "log": stdout.decode()[-500:]}

    elif item_id == "ffmpeg":
        # Try winget on Windows
        proc = await asyncio.create_subprocess_exec(
            "winget", "install", "--id", "Gyan.FFmpeg", "-e", "--accept-source-agreements", "--accept-package-agreements",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return JSONResponse(status_code=500, content={
                "error": f"winget install failed: {stderr.decode()[-500:]}. Install manually from https://ffmpeg.org/download.html"
            })
        return {"status": "installed", "id": item_id, "log": stdout.decode()[-500:]}

    elif item_id == "yt_dlp":
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "yt-dlp",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return JSONResponse(status_code=500, content={
                "error": f"pip install failed: {stderr.decode()[-500:]}"
            })
        return {"status": "installed", "id": item_id, "log": stdout.decode()[-500:]}

    elif item_id == "scikit_learn":
        from starlette.responses import StreamingResponse

        async def stream_pip_install():
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "scikit-learn",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async for line in proc.stdout:
                text = line.decode(errors="replace").rstrip()
                if text:
                    yield f"data: {json.dumps({'stage': 'progress', 'line': text})}\n\n"
            await proc.wait()
            if proc.returncode == 0:
                yield f"data: {json.dumps({'stage': 'done'})}\n\n"
            else:
                yield f"data: {json.dumps({'stage': 'error', 'error': f'pip exited with code {proc.returncode}'})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream_pip_install(), media_type="text/event-stream")

    elif item_id == "nemo_toolkit":
        from starlette.responses import StreamingResponse

        async def stream_nemo_install():
            # Install build tools first (cmake, Cython required by nemo)
            env = os.environ.copy()
            scripts_dir = os.path.join(os.path.dirname(sys.executable), "Scripts")
            env["PATH"] = scripts_dir + os.pathsep + env.get("PATH", "")

            yield f"data: {json.dumps({'stage': 'progress', 'line': 'Installing build tools (cmake, Cython)...'})}\n\n"
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "cmake", "Cython",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            async for line in proc.stdout:
                text = line.decode(errors="replace").rstrip()
                if text:
                    yield f"data: {json.dumps({'stage': 'progress', 'line': text})}\n\n"
            await proc.wait()
            if proc.returncode != 0:
                yield f"data: {json.dumps({'stage': 'error', 'error': 'Failed to install build tools'})}\n\n"
                yield "data: [DONE]\n\n"
                return

            yield f"data: {json.dumps({'stage': 'progress', 'line': 'Installing nemo_toolkit[asr] (this may take several minutes)...'})}\n\n"
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "nemo_toolkit[asr]>=2.0.0",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            async for line in proc.stdout:
                text = line.decode(errors="replace").rstrip()
                if text:
                    yield f"data: {json.dumps({'stage': 'progress', 'line': text})}\n\n"
            await proc.wait()
            if proc.returncode == 0:
                yield f"data: {json.dumps({'stage': 'done'})}\n\n"
            else:
                yield f"data: {json.dumps({'stage': 'error', 'error': f'pip exited with code {proc.returncode}'})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream_nemo_install(), media_type="text/event-stream")

    elif item_id == "seed_vc":
        # Delegate to existing setup endpoint
        return await setup_seed_vc()

    else:
        return JSONResponse(status_code=400, content={"error": f"Cannot auto-install '{item_id}'. Please install manually."})


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
        import zipfile
        import io
        import httpx
        import shutil

        # Step 1: Download zip archive from GitHub
        zip_url = "https://github.com/Plachtaa/seed-vc/archive/refs/heads/main.zip"
        yield f"data: Downloading seed-vc from GitHub...\n\n"

        try:
            zip_path = BASE_DIR / "seed-vc-download.zip"
            async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
                async with client.stream("GET", zip_url) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length", 0))
                    downloaded = 0
                    with open(zip_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total:
                                pct = int(downloaded / total * 100)
                                mb = downloaded / (1024 * 1024)
                                total_mb = total / (1024 * 1024)
                                yield f"data: Downloading... {mb:.1f} / {total_mb:.1f} MB ({pct}%)\n\n"
                            else:
                                mb = downloaded / (1024 * 1024)
                                yield f"data: Downloading... {mb:.1f} MB\n\n"

            yield f"data: Download complete. Extracting...\n\n"

            # Extract zip - GitHub archives extract to seed-vc-main/
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(BASE_DIR)

            # Rename extracted folder to seed-vc
            extracted_dir = BASE_DIR / "seed-vc-main"
            if extracted_dir.exists():
                shutil.move(str(extracted_dir), str(SEED_VC_DIR))
            else:
                # Find whatever directory was extracted
                for item in BASE_DIR.iterdir():
                    if item.is_dir() and item.name.startswith("seed-vc-"):
                        shutil.move(str(item), str(SEED_VC_DIR))
                        break

            # Clean up zip file
            zip_path.unlink(missing_ok=True)

            if not SEED_VC_DIR.exists():
                yield f"data: ERROR: Failed to extract seed-vc archive\n\n"
                return

            yield f"data: Extraction complete. Installing dependencies...\n\n"

        except Exception as e:
            # Clean up on failure
            zip_path = BASE_DIR / "seed-vc-download.zip"
            if zip_path.exists():
                zip_path.unlink(missing_ok=True)
            yield f"data: ERROR: Download failed: {e}\n\n"
            return

        # Step 2: Install pip deps
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


@app.get("/api/device")
async def get_device_info():
    """Return whether CUDA GPU or CPU is being used for inference."""
    device = "cpu"
    device_name = "CPU"
    try:
        import torch
        if cuda_enabled and torch.cuda.is_available():
            device = "cuda"
            device_name = torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return {"device": device, "device_name": device_name}


@app.get("/api/cuda")
async def get_cuda_settings():
    """Return CUDA availability and whether it's enabled."""
    global cuda_enabled
    cuda_available = False
    cuda_version = None
    gpu_name = None
    try:
        import torch
        cuda_available = torch.cuda.is_available()
        if cuda_available:
            cuda_version = torch.version.cuda
            gpu_name = torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return {
        "cuda_available": cuda_available,
        "cuda_enabled": cuda_enabled and cuda_available,
        "cuda_version": cuda_version,
        "gpu_name": gpu_name,
    }


@app.post("/api/cuda")
async def set_cuda_enabled(enabled: bool = Form(...)):
    """Enable or disable CUDA. Takes effect on next model load."""
    global cuda_enabled
    cuda_enabled = enabled
    return {"cuda_enabled": cuda_enabled}


from fastapi.responses import StreamingResponse
import httpx

CUDA_INSTALLER_URL = "https://developer.download.nvidia.com/compute/cuda/12.6.3/local_installers/cuda_12.6.3_561.17_windows.exe"
CUDA_INSTALLER_FILENAME = "cuda_installer.exe"


@app.post("/api/cuda/install")
async def install_cuda_toolkit():
    """Download the CUDA toolkit installer and launch it. Streams progress as SSE."""
    import tempfile

    installer_path = Path(tempfile.gettempdir()) / CUDA_INSTALLER_FILENAME

    async def stream_progress():
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=600.0)) as client:
                async with client.stream("GET", CUDA_INSTALLER_URL) as response:
                    total = int(response.headers.get("content-length", 0))
                    downloaded = 0
                    yield f"data: {json.dumps({'stage': 'downloading', 'downloaded': 0, 'total': total})}\n\n"

                    with open(installer_path, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
                            f.write(chunk)
                            downloaded += len(chunk)
                            yield f"data: {json.dumps({'stage': 'downloading', 'downloaded': downloaded, 'total': total})}\n\n"

            yield f"data: {json.dumps({'stage': 'launching'})}\n\n"

            # Launch the installer (non-blocking)
            import subprocess
            subprocess.Popen([str(installer_path)], shell=True)

            yield f"data: {json.dumps({'stage': 'launched', 'path': str(installer_path)})}\n\n"
            yield "data: [DONE]\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'stage': 'error', 'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(stream_progress(), media_type="text/event-stream")


# ---- GPU Stats ----

@app.get("/api/gpu/stats")
async def gpu_stats():
    """Return GPU usage, temperature, and memory info via nvidia-smi."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total,name,power.draw,power.limit",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return {"available": False}

        line = stdout.decode().strip().split("\n")[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            return {"available": False}

        return {
            "available": True,
            "utilization": int(parts[0]) if parts[0].isdigit() else 0,
            "temperature": int(parts[1]) if parts[1].isdigit() else 0,
            "memory_used_mb": int(parts[2]) if parts[2].isdigit() else 0,
            "memory_total_mb": int(parts[3]) if parts[3].isdigit() else 0,
            "name": parts[4],
            "power_draw_w": float(parts[5]) if len(parts) > 5 else None,
            "power_limit_w": float(parts[6]) if len(parts) > 6 else None,
        }
    except FileNotFoundError:
        return {"available": False}
    except Exception:
        return {"available": False}


# TTS job storage for progress tracking
_tts_jobs: dict = {}
_chatterbox_model = None


def _get_chatterbox_model():
    """Lazy-load and cache the Chatterbox TTS model."""
    global _chatterbox_model
    if _chatterbox_model is None:
        from chatterbox.tts import ChatterboxTTS
        import torch
        device = "cuda" if cuda_enabled and torch.cuda.is_available() else "cpu"
        _chatterbox_model = ChatterboxTTS.from_pretrained(device=device)
    return _chatterbox_model


@app.post("/api/tts")
async def text_to_speech(
    text: str = Form(...),
    reference_id: str = Form(...),
    exaggeration: float = Form(0.5),
    cfg_weight: float = Form(0.5),
):
    """Start TTS generation with Chatterbox and return a job ID for progress tracking."""
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

    # Detect device
    device = "cpu"
    device_name = "CPU"
    try:
        import torch
        if cuda_enabled and torch.cuda.is_available():
            device = "cuda"
            device_name = torch.cuda.get_device_name(0)
    except ImportError:
        pass

    job_id = uuid.uuid4().hex[:8]
    _tts_jobs[job_id] = {
        "status": "running",
        "stage": "tts_generate",
        "stage_label": "Loading Chatterbox model...",
        "progress": 0,
        "device": device,
        "device_name": device_name,
        "error": None,
        "output_file": None,
    }

    asyncio.create_task(_run_tts_job(
        job_id, text, ref_path, exaggeration, cfg_weight,
    ))

    return {"job_id": job_id, "device": device, "device_name": device_name}


async def _run_tts_job(
    job_id: str, text: str, ref_path: Path,
    exaggeration: float, cfg_weight: float,
):
    """Run Chatterbox TTS in background, updating progress in _tts_jobs."""
    job = _tts_jobs[job_id]

    job["stage"] = "tts_generate"
    job["stage_label"] = f"Loading Chatterbox model on {job['device_name']}..."
    job["progress"] = 5

    try:
        import torchaudio as ta

        # Load model (cached after first call)
        loop = asyncio.get_event_loop()
        model = await loop.run_in_executor(None, _get_chatterbox_model)

        if job["status"] == "cancelled":
            return

        job["stage_label"] = f"Generating speech on {job['device_name']}..."
        job["progress"] = 20

        # Run inference in executor to avoid blocking the event loop
        def _generate():
            return model.generate(
                text,
                audio_prompt_path=str(ref_path),
                exaggeration=exaggeration,
                cfg_weight=cfg_weight,
            )

        wav = await loop.run_in_executor(None, _generate)

        if job["status"] == "cancelled":
            return

        job["stage"] = "finalizing"
        job["stage_label"] = "Saving output..."
        job["progress"] = 90

        # Save output
        output_name = f"tts_{job_id}.wav"
        output_path = OUTPUT_DIR / output_name
        ta.save(str(output_path), wav, model.sr)

        job["output_file"] = output_name
        job["status"] = "completed"
        job["progress"] = 100
        job["stage_label"] = "Done"

    except Exception as e:
        job["status"] = "failed"
        job["error"] = f"TTS generation failed: {str(e)}"


@app.get("/api/tts/status/{job_id}")
async def tts_job_status(job_id: str):
    """Get TTS job progress with GPU stats."""
    if job_id not in _tts_jobs:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    job = _tts_jobs[job_id]
    result = {
        "status": job["status"],
        "stage": job["stage"],
        "stage_label": job["stage_label"],
        "progress": job["progress"],
        "device": job["device"],
        "device_name": job["device_name"],
        "error": job["error"],
        "output_file": job["output_file"],
    }
    # Include GPU stats during active generation
    if job["status"] == "running" and job["device"] == "cuda":
        try:
            gpu_data = await gpu_stats()
            if gpu_data.get("available"):
                result["gpu"] = gpu_data
        except Exception:
            pass
    return result


@app.post("/api/tts/cancel/{job_id}")
async def tts_cancel(job_id: str):
    """Cancel a running TTS job."""
    if job_id not in _tts_jobs:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    job = _tts_jobs[job_id]
    if job["status"] != "running":
        return {"status": job["status"]}
    job["status"] = "cancelled"
    job["stage_label"] = "Cancelled"
    return {"status": "cancelled"}


@app.get("/api/tts/download/{job_id}")
async def tts_download(job_id: str):
    """Download completed TTS output."""
    if job_id not in _tts_jobs:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    job = _tts_jobs[job_id]
    if job["status"] != "completed" or not job["output_file"]:
        return JSONResponse(status_code=400, content={"error": "Job not completed"})
    file_path = OUTPUT_DIR / job["output_file"]
    if not file_path.exists():
        return JSONResponse(status_code=404, content={"error": "Output file not found"})
    return FileResponse(file_path, media_type="audio/wav", filename="tts_output.wav")


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


# ---- Speaker Diarization (NVIDIA Sortformer) ----

# Lazy-loaded Sortformer model (cached after first use)
_sortformer_model = None

def _get_sortformer_model():
    global _sortformer_model
    if _sortformer_model is None:
        try:
            from nemo.collections.asr.models import SortformerEncLabelModel
        except ImportError:
            raise RuntimeError(
                "nemo_toolkit is not installed. Install it from the Prerequisites tab (NVIDIA NeMo Toolkit)."
            )
        _sortformer_model = SortformerEncLabelModel.from_pretrained(
            "nvidia/diar_sortformer_4spk-v1"
        )
        _sortformer_model.eval()
        # Prevent Windows file locking: disable DataLoader worker subprocesses
        # so manifest.json isn't held open during temp dir cleanup
        if hasattr(_sortformer_model, 'cfg'):
            from omegaconf import OmegaConf, open_dict
            with open_dict(_sortformer_model.cfg):
                if hasattr(_sortformer_model.cfg, 'test_ds'):
                    _sortformer_model.cfg.test_ds.num_workers = 0
        import torch
        if torch.cuda.is_available():
            _sortformer_model = _sortformer_model.cuda()
    return _sortformer_model

@app.post("/api/diarize")
async def diarize_audio(file: UploadFile = File(...)):
    """Perform speaker diarization using NVIDIA Sortformer 4-speaker model.

    Uses the pretrained diar_sortformer_4spk-v1 model which handles up to
    4 speakers with end-to-end neural diarization (no manual clustering).
    """
    import tempfile
    import librosa
    import soundfile as sf

    tmp_path = None
    wav16k_path = None
    try:
        # Save upload to temp file
        suffix = Path(file.filename or "audio.wav").suffix or ".wav"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            tmp.write(await file.read())

        # Load and resample to 16kHz mono WAV (Sortformer requirement)
        y, sr = librosa.load(tmp_path, sr=16000, mono=True)
        duration = len(y) / sr

        if duration < 1.0:
            return JSONResponse(
                status_code=400,
                content={"error": "Audio too short for diarization (min 1s)"},
            )

        # Write 16kHz WAV for Sortformer
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as wav_tmp:
            wav16k_path = wav_tmp.name
        sf.write(wav16k_path, y, 16000)

        # Run Sortformer diarization
        model = _get_sortformer_model()

        # Run diarization — patch TemporaryDirectory cleanup for Windows file locking
        import tempfile as _tempfile
        _orig_cleanup = _tempfile.TemporaryDirectory.cleanup
        def _safe_cleanup(self):
            import shutil, time as _t
            for attempt in range(5):
                try:
                    _orig_cleanup(self)
                    return
                except PermissionError:
                    _t.sleep(0.2 * (attempt + 1))
            # Final fallback: ignore cleanup failure on Windows
            try:
                shutil.rmtree(self.name, ignore_errors=True)
            except Exception:
                pass
        _tempfile.TemporaryDirectory.cleanup = _safe_cleanup
        try:
            diarize_result = model.diarize(audio=wav16k_path, batch_size=1)
        finally:
            _tempfile.TemporaryDirectory.cleanup = _orig_cleanup
        print(f"[diarize] returned type={type(diarize_result).__name__}, value={diarize_result!r}")

        # Parse results: diarize() returns [["start end speaker", ...]]
        # Each segment string is "start_sec end_sec speaker_label"
        segments = []
        raw_segments = diarize_result[0] if isinstance(diarize_result, (list, tuple)) and diarize_result else []
        for seg in raw_segments:
            parts = str(seg).strip().split()
            if len(parts) >= 3:
                segments.append((float(parts[0]), float(parts[1]), parts[2]))

        print(f"[diarize] parsed {len(segments)} segments")

        if not segments:
            return {
                "speakers": [
                    {
                        "label": "Person 1",
                        "segments": [{"start": 0.0, "end": round(duration, 2)}],
                    }
                ]
            }

        # Build per-speaker segment lists
        speaker_segments: dict[str, list[dict]] = {}
        for start, end, spk_label in segments:
            if spk_label not in speaker_segments:
                speaker_segments[spk_label] = []
            speaker_segments[spk_label].append({
                "start": round(float(start), 2),
                "end": round(float(end), 2),
            })

        # Merge consecutive or overlapping segments for each speaker
        for spk in speaker_segments:
            merged = []
            for seg in sorted(speaker_segments[spk], key=lambda s: s["start"]):
                if merged and seg["start"] <= merged[-1]["end"] + 0.1:
                    merged[-1]["end"] = max(merged[-1]["end"], seg["end"])
                else:
                    merged.append(dict(seg))
            speaker_segments[spk] = merged

        # Re-index speakers starting from 0
        sorted_speakers = sorted(speaker_segments.keys())
        result = {
            "speakers": [
                {
                    "label": f"Person {idx + 1}",
                    "segments": speaker_segments[spk],
                }
                for idx, spk in enumerate(sorted_speakers)
            ]
        }
        return result

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Diarization failed: {str(e)}"},
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        if wav16k_path and os.path.exists(wav16k_path):
            os.unlink(wav16k_path)


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

        device = torch.device("cuda" if (cuda_enabled and torch.cuda.is_available()) else "cpu")

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

        # 10. Peak-normalize to prevent clipping (vocoder can exceed [-1, 1])
        peak = output_16k.abs().max()
        if peak > 0.95:
            output_16k = output_16k * (0.95 / peak)

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

        # Include device info in the ready message
        device_type = "cpu"
        device_name = "CPU"
        if session.models and session.models.get("device"):
            d = session.models["device"]
            device_type = d.type
            if d.type == "cuda":
                import torch
                device_name = torch.cuda.get_device_name(d.index or 0)
            else:
                device_name = "CPU"
        await websocket.send_json({"status": "ready", "device": device_type, "device_name": device_name})

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


# ---- YouTube Audio Fetch ----

import subprocess
import tempfile
import time as _time
import re as _re

# ── YouTube cache settings & helpers ──────────────────────────────────────────

YT_CACHE_DIR = BASE_DIR / "yt_cache"
YT_CACHE_DIR.mkdir(exist_ok=True)
_YT_CACHE_INDEX = "index.json"  # metadata file inside cache dir

_yt_cache_settings: dict = {
    "cache_dir": str(YT_CACHE_DIR),
    "max_size_mb": 128,
    "max_items": 10,
    "max_age_days": 7,
}

_SETTINGS_FILE = BASE_DIR / "backend" / "yt_cache_settings.json"


def _load_yt_cache_settings():
    """Load cache settings from disk."""
    global _yt_cache_settings
    if _SETTINGS_FILE.exists():
        try:
            with open(_SETTINGS_FILE, "r") as f:
                saved = json.load(f)
            _yt_cache_settings.update(saved)
        except Exception:
            pass
    # Ensure cache dir exists
    Path(_yt_cache_settings["cache_dir"]).mkdir(parents=True, exist_ok=True)


def _save_yt_cache_settings():
    """Persist cache settings to disk."""
    with open(_SETTINGS_FILE, "w") as f:
        json.dump(_yt_cache_settings, f, indent=2)


def _get_cache_dir() -> Path:
    return Path(_yt_cache_settings["cache_dir"])


def _load_cache_index() -> dict:
    """Load the cache index (fileId -> metadata) from disk."""
    idx_path = _get_cache_dir() / _YT_CACHE_INDEX
    if idx_path.exists():
        try:
            with open(idx_path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cache_index(index: dict):
    """Write cache index to disk."""
    idx_path = _get_cache_dir() / _YT_CACHE_INDEX
    with open(idx_path, "w") as f:
        json.dump(index, f, indent=2)


def _cache_entry_path(file_id: str, filename: str) -> Path:
    """Return path where a cached audio file lives."""
    return _get_cache_dir() / f"{file_id}_{filename}"


def _cleanup_yt_cache():
    """Enforce cache policies: max age (7 days), max items (10), max size (128 MB)."""
    index = _load_cache_index()
    now = _time.time()
    max_age_secs = _yt_cache_settings["max_age_days"] * 86400
    max_size_bytes = _yt_cache_settings["max_size_mb"] * 1024 * 1024
    max_items = _yt_cache_settings["max_items"]
    changed = False

    # 1) Remove entries older than max_age_days
    expired = [fid for fid, e in index.items() if now - e.get("created", 0) > max_age_secs]
    for fid in expired:
        _delete_cache_entry(index, fid)
        changed = True

    # 2) Remove oldest entries if count exceeds max_items
    if len(index) > max_items:
        sorted_entries = sorted(index.items(), key=lambda x: x[1].get("created", 0))
        while len(index) > max_items:
            fid, _ = sorted_entries.pop(0)
            _delete_cache_entry(index, fid)
            changed = True

    # 3) Remove oldest entries if total size exceeds max_size_mb
    total = _calc_cache_size(index)
    if total > max_size_bytes:
        sorted_entries = sorted(index.items(), key=lambda x: x[1].get("created", 0))
        while total > max_size_bytes and sorted_entries:
            fid, entry = sorted_entries.pop(0)
            fpath = Path(entry.get("path", ""))
            if fpath.exists():
                total -= fpath.stat().st_size
            _delete_cache_entry(index, fid)
            changed = True

    if changed:
        _save_cache_index(index)


def _delete_cache_entry(index: dict, file_id: str):
    """Delete a single cache entry (file + index record)."""
    entry = index.pop(file_id, None)
    if entry:
        fpath = Path(entry.get("path", ""))
        if fpath.exists():
            try:
                fpath.unlink()
            except Exception:
                pass


def _calc_cache_size(index: dict) -> int:
    """Return total bytes of cached audio files."""
    total = 0
    for entry in index.values():
        fpath = Path(entry.get("path", ""))
        if fpath.exists():
            total += fpath.stat().st_size
    return total


# Load settings & run initial cleanup on startup
_load_yt_cache_settings()
_cleanup_yt_cache()


def _find_yt_dlp():
    """Find yt-dlp executable or fallback to python -m yt_dlp."""
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    # Fallback: run as a Python module (works when installed via pip but not on PATH)
    try:
        import importlib.util
        if importlib.util.find_spec("yt_dlp"):
            return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        pass
    return None


# ── YouTube cache settings API ────────────────────────────────────────────────

@app.get("/api/settings/yt-cache")
async def get_yt_cache_settings():
    """Return current YouTube cache settings and usage stats."""
    index = _load_cache_index()
    total_bytes = _calc_cache_size(index)
    return {
        "cache_dir": _yt_cache_settings["cache_dir"],
        "max_size_mb": _yt_cache_settings["max_size_mb"],
        "max_items": _yt_cache_settings["max_items"],
        "max_age_days": _yt_cache_settings["max_age_days"],
        "current_items": len(index),
        "current_size_mb": round(total_bytes / (1024 * 1024), 2),
    }


@app.post("/api/settings/yt-cache")
async def update_yt_cache_settings(
    cache_dir: Optional[str] = Form(None),
    max_size_mb: Optional[int] = Form(None),
):
    """Update YouTube cache settings."""
    if cache_dir is not None:
        cache_dir = cache_dir.strip()
        if cache_dir:
            new_dir = Path(cache_dir)
            new_dir.mkdir(parents=True, exist_ok=True)
            # Move existing cache files to new location if different
            old_dir = _get_cache_dir()
            if str(new_dir.resolve()) != str(old_dir.resolve()):
                index = _load_cache_index()
                for fid, entry in index.items():
                    old_path = Path(entry["path"])
                    if old_path.exists():
                        new_path = new_dir / old_path.name
                        shutil.move(str(old_path), str(new_path))
                        entry["path"] = str(new_path)
                # Move index file
                old_idx = old_dir / _YT_CACHE_INDEX
                if old_idx.exists():
                    old_idx.unlink()
                _yt_cache_settings["cache_dir"] = str(new_dir)
                _save_cache_index(index)

    if max_size_mb is not None and max_size_mb > 0:
        _yt_cache_settings["max_size_mb"] = max_size_mb

    _save_yt_cache_settings()
    _cleanup_yt_cache()
    return await get_yt_cache_settings()


@app.delete("/api/settings/yt-cache")
async def clear_yt_cache():
    """Delete all cached YouTube files."""
    index = _load_cache_index()
    for fid in list(index.keys()):
        _delete_cache_entry(index, fid)
    _save_cache_index(index)
    return {"status": "cleared"}


# ── YouTube fetch & download API ──────────────────────────────────────────────

@app.get("/api/youtube/fetch")
async def youtube_fetch_stream(url: str = ""):
    """Fetch audio from YouTube URL with SSE progress streaming."""
    from starlette.responses import StreamingResponse

    url = url.strip()
    if not url:
        return JSONResponse(status_code=400, content={"error": "YouTube URL is required"})

    yt_dlp = _find_yt_dlp()
    if not yt_dlp:
        return JSONResponse(status_code=400, content={"error": "yt-dlp not found. Install with: pip install yt-dlp"})

    async def event_stream():
        def send_event(event: str, data: dict):
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        try:
            # Phase 1: Metadata
            yield send_event("phase", {"phase": "metadata"})

            meta_proc = await asyncio.create_subprocess_exec(
                *yt_dlp, "--no-download", "-j", "--no-warnings", url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            meta_stdout, _ = await meta_proc.communicate()

            video_title = "youtube_audio"
            video_duration = 0
            video_thumbnail = ""
            try:
                meta = json.loads(meta_stdout.decode())
                video_title = meta.get("title", meta.get("fulltitle", "youtube_audio"))
                video_duration = meta.get("duration", 0)
                video_thumbnail = meta.get("thumbnail", "")
            except Exception:
                pass

            yield send_event("metadata", {"title": video_title, "durationSeconds": video_duration, "thumbnail": video_thumbnail})

            # Phase 2: Download
            yield send_event("phase", {"phase": "downloading"})

            cache_dir = _get_cache_dir()
            safe_title = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', video_title).strip() or "youtube_audio"
            # Download to a temp dir first, then move to cache
            tmp_dir = tempfile.gettempdir()
            out_template = os.path.join(tmp_dir, f"{safe_title}.%(ext)s")

            dl_proc = await asyncio.create_subprocess_exec(
                *yt_dlp,
                "-f", "bestaudio[ext=m4a]/bestaudio",
                "-o", out_template,
                "--no-playlist", "--no-warnings", "--force-overwrites", "--newline",
                url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            progress_re = _re.compile(r'\[download]\s+([\d.]+)%')

            async for line in dl_proc.stdout:
                text = line.decode(errors="replace")
                match = progress_re.search(text)
                if match:
                    yield send_event("progress", {"percent": float(match.group(1))})

            await dl_proc.wait()

            if dl_proc.returncode != 0:
                stderr_out = await dl_proc.stderr.read()
                yield send_event("error", {"message": stderr_out.decode(errors="replace") or "yt-dlp failed"})
                return

            # Phase 3: Find output file and move to persistent cache
            yield send_event("phase", {"phase": "processing"})

            out_path = None
            for ext in ["m4a", "webm", "opus", "ogg", "mp3"]:
                candidate = os.path.join(tmp_dir, f"{safe_title}.{ext}")
                if os.path.exists(candidate):
                    out_path = candidate
                    break

            if not out_path:
                yield send_event("error", {"message": "yt-dlp did not produce an output file"})
                return

            # Move file into persistent cache directory
            file_id = uuid.uuid4().hex[:12]
            filename = os.path.basename(out_path)
            cached_path = str(_cache_entry_path(file_id, filename))
            shutil.move(out_path, cached_path)

            # Update cache index
            index = _load_cache_index()
            index[file_id] = {
                "path": cached_path,
                "filename": filename,
                "title": video_title,
                "created": _time.time(),
                "url": url,
                "thumbnail": video_thumbnail,
            }
            _save_cache_index(index)

            # Enforce cache limits (evict oldest if needed)
            _cleanup_yt_cache()

            yield send_event("done", {
                "fileId": file_id,
                "fileName": filename,
                "title": video_title,
                "durationSeconds": video_duration,
                "thumbnail": video_thumbnail,
            })

        except Exception as e:
            yield send_event("error", {"message": str(e)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/api/youtube/download/{file_id}")
async def youtube_download(file_id: str):
    """Download a previously fetched YouTube audio file."""
    index = _load_cache_index()
    entry = index.get(file_id)
    if not entry or not os.path.exists(entry["path"]):
        return JSONResponse(status_code=404, content={"error": "File not found or expired"})

    return FileResponse(entry["path"], filename=entry["filename"])


@app.get("/api/youtube/cache")
async def youtube_cache_list():
    """Return list of cached YouTube downloads that still have files on disk."""
    _cleanup_yt_cache()
    index = _load_cache_index()
    items = []
    for file_id, entry in index.items():
        if os.path.exists(entry.get("path", "")):
            items.append({
                "fileId": file_id,
                "url": entry.get("url", ""),
                "title": entry.get("title", ""),
                "thumbnail": entry.get("thumbnail", ""),
                "fileName": entry.get("filename", ""),
            })
    return items


@app.get("/api/youtube/cache/lookup")
async def youtube_cache_lookup(url: str = ""):
    """Check if a YouTube URL is already cached. Returns the cache entry or 404."""
    url = url.strip()
    if not url:
        return JSONResponse(status_code=400, content={"error": "URL is required"})
    index = _load_cache_index()
    for file_id, entry in index.items():
        if entry.get("url") == url and os.path.exists(entry.get("path", "")):
            return {
                "fileId": file_id,
                "fileName": entry.get("filename", ""),
                "title": entry.get("title", ""),
                "thumbnail": entry.get("thumbnail", ""),
            }
    return JSONResponse(status_code=404, content={"error": "Not in cache"})


@app.get("/api/output/{filename}")
async def get_output_file(filename: str):
    file_path = OUTPUT_DIR / filename
    if not file_path.exists():
        return JSONResponse(status_code=404, content={"error": "File not found"})
    return FileResponse(file_path, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
