# PZ AI Mask Studio

Local Windows tool for video mask creation and luma matte export.

## What it does

- Browser editor for video masking
- Local Python worker
- PHP API backend
- SAM2 based mask selection and tracking
- RMBG fallback mode
- PNG sequence and H.264 luma output
- Windows install and start scripts

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
