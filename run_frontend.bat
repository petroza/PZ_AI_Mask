PZ_MASK v67 FAST ENCODE + HW STATUS FIX

Změny:
- horní lišta HW už nezůstane jen na GPU — / VRAM — / CPU — / RAM —
- API nově čte NVIDIA přes nvidia-smi i z typické cesty C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe
- fallback pro Windows 11 bez WMIC: CPU/RAM přes PowerShell Get-CimInstance
- worker heartbeat má stejné fallbacky: nvidia-smi PATH, NVIDIA NVSMI cesta, torch CUDA, WMIC, PowerShell
- když HW status ještě není dostupný, UI vypíše jasné „HW čekám…“ místo prázdných pomlček
- zachováno rychlé enkódování: H.264 preset veryfast + CRF 12
