PZ MASK STUDIO — jak spustit
============================

  START.bat           Spustí všechno (server + worker + prohlížeč).
  STOP_PZ_MASK.bat    Zastaví běžící server a worker.
  install.bat         První instalace (stáhne Python, modely, PHP...).
  UNINSTALL.bat       Kompletní odinstalace (smaže celou složku).

  nastroje\           Opravné a servisní skripty — běžně je nepotřebuješ.
                      Popis každého skriptu: nastroje\CTI_ME.txt

Typický postup:
  1. install.bat   (jen jednou; případně nastroje\INSTALL_NO_POWERSHELL.bat,
                    když Windows blokuje PowerShell)
  2. START.bat
  3. V prohlížeči se otevře http://127.0.0.1:8080
