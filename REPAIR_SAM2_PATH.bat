PZ MASK v67 FAST ENCODE

Změny:
- H.264 luma export: preset slow -> veryfast.
- Default CRF: 8/10 -> 12.
- RMBG export už nemá natvrdo -preset slow; bere rychlý preset z configu.
- RMBG CRF rozsah zvýšen na 8–23, default 12.

Když budeš chtít maximální kvalitu místo rychlosti, změň ve worker/config.json:
  "h264_luma_preset": "slow",
  "h264_luma_crf": 8,
  "rmbg_h264_preset": "slow"
