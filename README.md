# PZ AI Mask Studio

Local Windows tool for video mask creation and luma matte export.

## Current build

**PZ_MASK_v42_QUEUE_STATUS_TOPBAR**

This build adds a top-bar worker queue indicator in the editor, so users can immediately see that the job queue is active after creating a new image/job.

Top bar states:

- `QUEUE idle` — no active worker jobs
- `QUEUE X · run Y · wait Z` — active queue count, running jobs, waiting jobs

## What it does

- Browser editor for video masking
- Local Python worker
- PHP API backend
- SAM2 based mask selection and tracking
- RMBG fallback mode
- MatAnyone / refine-edge workflow when available
- Luma matte cleanup, halo suppression and final mask contrast
- PNG sequence and H.264 luma output
- Windows install and start scripts

## Recent update notes

### v42 — Queue status topbar

- Added visible queue state in the editor header.
- Polls the jobs list every 2 seconds.
- Shows whether the worker queue is idle, waiting, or actively processing.
- Makes first-use and new-job behavior clearer for users.

### v41 — Fast edge tuning preview

- Faster low-resolution slider preview while adjusting edge settings.
- Automatic 100% edge preview after slider release.

### v40 — Preview model memory fix

- If SAM model download starts during the first object selection, the selection is remembered.
- After the model is ready, the saved point/rectangle is retried automatically.

### v39 — Luma halo / auto edge fix

- Added Luma Halo Killer.
- Added Edge Contrast.
- Added Auto Edge Fix preset.
- Output stays pure luma matte.

## Start

Run `START.bat` from the project folder and open the local web UI.

## Sources

Third-party model weights are not bundled in this repository. Models are downloaded locally by the user when needed.

Main upstream projects:

- SAM2: https://github.com/facebookresearch/sam2
- RMBG models: https://huggingface.co/briaai
- MatAnyone: https://github.com/pq-yang/MatAnyone
- FFmpeg: https://ffmpeg.org/
- PyTorch: https://pytorch.org/

## License

Application wrapper code is MIT licensed. Third-party models and packages remain under their own licenses.
