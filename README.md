# PZ AI Mask Studio

**Current stable build: PZ_MASK_v72_COLLEAGUES_AUTOINSTALL**

PZ AI Mask Studio is a local Windows tool for creating person/object masks from video and exporting clean luma mattes for editing, compositing and post-production workflows.

It runs locally in the browser with a PHP frontend/API and a Python GPU worker. The current package is prepared as a colleague-friendly auto-installer.

## Download

Use the latest installer package uploaded in this repository:

- `PZ_MASK_v72_COLLEAGUES_AUTOINSTALL.zip`

## Quick install for colleagues

1. Download and extract the ZIP.
2. Run `INSTALL_PZ_MASK_FOR_COLLEAGUES.bat`.
3. Select the target installation folder.
4. After installation, start the app with `START.bat`.
5. Open `http://127.0.0.1:8080`.

For daily use, colleagues should only run:

```bat
START.bat
```

Do not run `run_worker.bat` directly unless debugging.

## What the app can do now

### Video mask creation

- Upload video files and prepare them for mask tracking.
- Open a browser-based masking editor.
- Select the subject with points or a rectangle.
- Track the selected subject through the timeline.
- Preview the mask on black, white or red background.
- Export a black/white luma matte where white is the subject and black is the background.

### SAM2 tracking workflow

- Uses SAM2-based object/person selection.
- Supports manual click / rectangle selection for precise tracking.
- Remembers first object selection while models are loading.
- Supports keyframe-style correction workflow.
- Includes preview refresh fixes and alignment fixes from previous updates.

### MatAnyone / refine-edge workflow

- MatAnyone / refine-edge workflow is used when available.
- Better edge recovery around hair, shoulders, sleeves, feet and difficult silhouettes.
- Safer preview refinement so clothes/trousers are not accidentally eaten by edge cleanup.
- Improved silhouette cleanup for luma output.

### RMBG one-click luma mode

- Includes RMBG Luma mode for quick black/white H.264 luma mask generation.
- Can create a luma mask directly from a video without going through the full manual editor workflow.
- Supports RMBG-1.4 fallback and RMBG-2.0 when the user has access/token.

### Output

- H.264 luma video output.
- PNG sequence output when needed.
- Cleaner luma matte with halo reduction and final contrast fixes.
- Output is intended for Premiere / After Effects / compositing workflows.

### Local worker and status

- Local Python worker for GPU processing.
- CUDA/PyTorch device check window.
- Queue/job status in the web interface.
- Live tracking preview fallback while the worker is processing.
- Diagnostics script for sending logs.

## What changed in the current v72 update

### v72 — cumulative stable start fix + colleague installer

This is the current stable base.

- Added colleague auto-installer package.
- Replaced old v66 startup scripts completely.
- Startup no longer waits 20 seconds for the API.
- Startup no longer kills port `8080` automatically.
- Fixed the old Windows batch issue that caused `Access denied` / `Přístup byl odepřen`.
- Uses `127.0.0.1:8080` instead of `localhost` to avoid Windows IPv4/IPv6 connection problems.
- Starts frontend, opens browser, then starts worker.
- Worker waits/retries if the frontend is not ready yet.
- Jobs/New Job page is fully in English.
- Update packs should be built on top of v72 going forward.

### Previous important updates included in v72

- v71: Jobs/New Job page fully translated to English.
- v70: no API wait startup, faster and safer launch.
- v69: English success message after update and automatic restart logic.
- v68: safer update flow groundwork.
- v67: English job/progress/status messages.
- v66: forced `127.0.0.1` frontend/API/worker connection fix.
- v64/v65: stable full package and faster startup fixes.
- v56/v57: jobs thumbnails and live tracking preview fallback.
- v52-v54: safer refine-edge preview and silhouette protection.
- v43-v44: AUTO HQ workflow and Triton-safe SAM2 fallback.

## Recommended workflow

1. Start the app with `START.bat`.
2. Upload a video or image sequence.
3. Choose one of the modes:
   - `SAM2 + MatAnyone` for precise manual selection and tracking.
   - `RMBG Luma` for fast one-click luma matte generation.
4. Select/preview the subject.
5. Run tracking/matting.
6. Download the H.264 luma output or PNG sequence.

## Troubleshooting

If the app does not respond:

1. Run `STOP_PZ_MASK.bat`.
2. Start again with `START.bat`.
3. Open `http://127.0.0.1:8080`.

If logs are needed:

```bat
DIAGNOSE_PZ_MASK.bat
```

Then send the generated diagnostics ZIP.

## Important notes

- The app is intended for local Windows use.
- Third-party model weights are not bundled in this repository.
- Models are downloaded locally by the user when needed.
- Large runtime folders, model checkpoints, uploads, jobs and generated results should not be committed to GitHub.

## Main upstream projects

- SAM2: https://github.com/facebookresearch/sam2
- RMBG models: https://huggingface.co/briaai
- MatAnyone: https://github.com/pq-yang/MatAnyone
- FFmpeg: https://ffmpeg.org/
- PyTorch: https://pytorch.org/

## License

Application wrapper code is MIT licensed. Third-party models and packages remain under their own licenses.
