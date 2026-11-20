# NVIDIA host setup

This is the end-to-end checklist for running the GPU paths: NVENC encoding,
TensorRT 4K upscaling, SAM 2 rotoscoping and Real-ESRGAN. The CPU pipeline
needs none of this — see the main README for that.

Reference environment: Ubuntu 22.04 or 24.04, NVIDIA driver 550+, CUDA 12.x,
Python 3.11, an Ampere-or-newer GPU. Older cards work for NVENC and Real-ESRGAN;
SAM 2 and TensorRT are happiest on modern compute capabilities.

> Run everything below inside a **dedicated venv** on the GPU host. The pipeline
> itself has no GPU dependency; the GPU extras are opt-in.

---

## 1. Driver

```bash
sudo apt update
ubuntu-drivers devices              # pick the recommended proprietary driver
sudo apt install nvidia-driver-550  # or the version listed as recommended
sudo reboot
nvidia-smi                          # must list the GPU and a CUDA version
```

In containers, the host driver is what matters: install the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on the host and start the container with `--gpus all`. `nvidia-smi` inside the
container is the first checkpoint.

## 2. FFmpeg with NVENC

Ubuntu's FFmpeg is built with NVENC support; it needs the driver, not extra
packages:

```bash
sudo apt install -y ffmpeg
ffmpeg -hide_banner -encoders | grep -E 'h264_nvenc|hevc_nvenc'
ffmpeg -hide_banner -y -f lavfi -i testsrc2=size=1280x720:rate=30:duration=1 \
  -c:v h264_nvenc -an /tmp/nvenc.mp4 && ls -l /tmp/nvenc.mp4
```

If the encoders are listed but the test fails with `Cannot load libcuda.so.1`,
the driver/device is not visible to the process (common in containers and on
CI images). If the encoders are missing, install an NVENC-enabled build such as
[jellyfin-ffmpeg](https://github.com/jellyfin/jellyfin-ffmpeg) or compile FFmpeg
with `--enable-nvenc --enable-nvdec` and the NVIDIA codec headers.

Verify what the pipeline will choose:

```bash
python -c "from framefrog import ffmpeg; print(ffmpeg.choose_encoder({}))"
# h264_nvenc on a working host, libx264 otherwise
```

`auto` requires both the encoder and a visible `/dev/nvidia*` device. Override
with `FRAMEFROG_FORCE_NVENC=1` only when the device lives somewhere non-standard.

## 3. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -r requirements.txt

# CUDA build of torch matching the driver (cu124 shown; pick the right index)
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124

pip install -r requirements-gpu.txt
pip install git+https://github.com/facebookresearch/sam2.git
```

Notes:

- `basicsr` 1.4.2 imports `torchvision.transforms.functional_tensor`, which moved
  in torchvision 0.17+. If Real-ESRGAN import fails, pin `torchvision<0.17` or
  use a maintained `basicsr` fork.
- `tensorrt` in pip ships the runtime libraries; the `trtexec` tool comes from
  the TensorRT tar/APT packages (next section).
- Do not install `onnxruntime-gpu` and `onnxruntime` side by side; RIFE works
  with either, just pick one.

## 4. Model weights

Weights are never committed. Fetch only what you need:

```bash
python scripts/fetch_models.py --sam2 small     # ~150 MB (large is ~900 MB)
python scripts/fetch_models.py --realesrgan     # ~64 MB
python scripts/fetch_models.py --rife           # 21 MB ONNX for interpolation
```

The scripts print the size and SHA-256 of every file. `assets/models/` is
git-ignored.

SAM 2 configs are resolved by name against the installed `sam2` package. If the
default `configs/sam2.1/sam2.1_hiera_l.yaml` is not found, point at the absolute
path:

```bash
python -c "import sam2, os; print(os.path.dirname(sam2.__file__))"
# then pass --sam2-config <that path>/configs/sam2.1/sam2.1_hiera_small.yaml
```

## 5. TensorRT engine

The upscaler loads a prebuilt engine. Contract:

- one input, `float32` RGB NCHW in `[0, 1]`
- one output, RGB NCHW `float32` in `[0, 1]` or `uint8`
- dynamic spatial axes are supported; dynamic batch/channel axes are rejected

Export your upscaling network to ONNX with that contract, then build on the same
GPU/driver family that will run it:

```bash
# trtexec ships with the TensorRT tar (or `apt install tensorrt`)
trtexec --onnx=upscaler.onnx --saveEngine=assets/models/upscaler.engine \
        --fp16 --shapes=input:1x3x1080x1920
```

Static engines only accept the resolution they were built for; build one per
source resolution or export dynamic H/W. Engines are **not portable across
GPU architectures or TensorRT major versions** — rebuild on the target host.

Run a job with it:

```bash
python main.py --input rushes/spot.mov \
  --stages upscale,deliver \
  --upscale-backend tensorrt --upscale-engine assets/models/upscaler.engine \
  --target-height 2160 --encoder nvenc
```

Real-ESRGAN can be used without TensorRT:

```bash
python main.py --input rushes/spot.mov --stages upscale,deliver \
  --upscale-backend realesrgan --upscale-model assets/models/RealESRGAN_x4plus.pth
```

## 6. Verify before a real job

```bash
python -m app.gpu_check --all
```

Expected shape of a healthy output:

```
FrameFrog GPU smoke test
  ffmpeg True · nvenc_compiled=True · nvidia_device=True · encoder=h264_nvenc
  torch    2.x · cuda=True · NVIDIA ...
  tensorrt {'available': True} · pycuda {'available': True} · sam2 {'available': True}
  encode      ok       encoder=h264_nvenc · bytes=...
  tensorrt    ok       ...
  sam2        ok       frames=8 · coverage={...}
  realesrgan  ok       ...
Result: PASS
```

Exit code 0 means every requested check passed. A CPU-only host still exits 0
with the model checks skipped; pass the weights flags explicitly to require them.

If you only want to test SAM 2, pass its checkpoint alone:

```bash
python -m app.gpu_check \
  --sam2-checkpoint assets/models/sam2.1_hiera_small.pt \
  --sam2-config /path/to/sam2/configs/sam2.1/sam2.1_hiera_small.yaml
```

## 7. Full GPU job

The canonical end-to-end invocation, including per-shot fan-out:

```bash
python main.py --input rushes/spot.mov \
  --stages scene_detect,rotoscope,color_grade,interpolate,upscale,deliver \
  --sam2-checkpoint assets/models/sam2.1_hiera_small.pt \
  --upscale-backend tensorrt --upscale-engine assets/models/upscaler.engine \
  --target-height 2160 --encoder auto \
  --fanout auto --fanout-mode queue --fanout-workers 4
```

The manifest records the selected encoder per stage, the upscale backend and
engine, the rotoscope seed/coverage, and per-shot timings.

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Cannot load libcuda.so.1` | Driver not installed/visible; in containers run with `--gpus all` and install `nvidia-container-toolkit`. |
| NVENC listed but encode fails | Same as above; also check for NVENC session limits on consumer GPUs (a few concurrent encoders). |
| `OpenEncodeSessionEx failed` | Driver too old, GPU lacks the requested codec (e.g. HEVC on an old card), or session limit reached. |
| TensorRT `serialization version mismatch` | Engine built with a different TensorRT major; rebuild on the host. |
| TensorRT `invalid shape` | Static engine built for another resolution; rebuild or export dynamic H/W. |
| SAM 2 CUDA OOM | Use the `small` checkpoint, shorter clips, or a bigger GPU; the stage processes frame-by-frame but the model stays resident. |
| SAM 2 config not found | Pass `--sam2-config` as an absolute path into the installed package (section 4). |
| `basicsr` import error | torchvision moved `functional_tensor`; pin `torchvision<0.17` or use a fixed fork. |
| `auto` picks libx264 on a GPU box | `/dev/nvidia*` not visible to the process; fix the container device mapping, or set `FRAMEFROG_FORCE_NVENC=1` if you know the device is reachable. |
| Real-ESRGAN very slow on CPU | It is a GPU network; pass `--upscale-backend ffmpeg` for the Lanczos fallback. |

## 9. Containers and serverless

- Docker: base the image on an NVIDIA CUDA runtime image, install FFmpeg and the
  Python deps there, and run with `--gpus all`. Mount `assets/models` instead of
  baking engines into the image.
- Cerebrium/other serverless GPUs: the same steps apply inside the build; model
  weights and engines are best fetched at build time or mounted from storage.
  `python -m app.gpu_check --all` makes a good health probe in the image.
