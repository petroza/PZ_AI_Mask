NASTROJE — opravné a servisní skripty PZ Mask Studio
=====================================================

Běžně je NEPOTŘEBUJEŠ. Aplikace se spouští přes START.bat v hlavní složce.
Tyhle skripty použij, jen když tě o to požádá chybová hláška, nebo když
něco nefunguje.

INSTALACE / ALTERNATIVY
  INSTALL_NO_POWERSHELL.bat   Čistě CMD instalátor (když Windows blokuje
                              PowerShell skripty). Volá ho i instalátor
                              INSTALL_TO_CHOSEN_FOLDER.bat.
  UNBLOCK_AND_INSTALL.bat     Odblokuje stažené soubory (Mark-of-the-Web)
                              a spustí instalaci.
  tools_install_ffmpeg.cmd    Ruční stažení přenosného FFmpeg.

OPRAVY (spusť, když o to požádá chybová hláška)
  REPAIR_CUDA_TORCH.bat       "Torch not compiled with CUDA enabled" —
                              přeinstaluje PyTorch s CUDA pro RTX.
  REPAIR_FFMPEG.bat           "FFmpeg nebyl nalezen / WinError 2" —
                              doinstaluje přibalený FFmpeg fallback.
  REPAIR_SAM2_PATH.bat        Oprava SAM2 import chyby ("running Python
                              from the parent directory...").

MODELY
  DOWNLOAD_SAM2_LARGE.bat       Stáhne SAM2 Hiera Large checkpoint.
  DOWNLOAD_SAM2_ALL_MODELS.bat  Stáhne všechny SAM2 checkpointy.
  SET_HF_TOKEN_FOR_RMBG2.bat    Uloží Hugging Face token pro gated
                                model briaai/RMBG-2.0.

DIAGNOSTIKA / RUČNÍ SPOUŠTĚNÍ
  DIAGNOSE_PZ_MASK.bat        Vytvoří diagnostický ZIP pro nahlášení chyby.
  CHECK_MATANYONE2.bat        Ověří, že MatAnyone2 funguje (HQ matting).
  run_frontend.bat            Jen frontend/server (http://127.0.0.1:8080).
  run_worker.bat              Jen GPU worker (frontend už musí běžet).
  RUN_WORKER_ONLY.bat         Totéž s upozorněním.
