{
  "version": "v70-speedup",
  "merge_json": {
    "worker/config.json": {
      "auto_hq": false,
      "auto_hq_force_large": false,
      "sam2_vos_optimized": "auto",
      "full_frame_format": "jpg",
      "matanyone": {
        "max_size": "auto",
        "auto_max_size_ladder": [1920, 1536, 1280, 960]
      }
    }
  },
  "skip": [
    "storage",
    "runtime",
    "api/config.php",
    "worker/config.json",
    "worker/hf_token.txt",
    "worker/python_path.txt"
  ]
}
