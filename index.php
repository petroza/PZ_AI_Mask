PZ MASK v70 — SPEEDUP CHANGELOG (2026-06-11)
=============================================

Kvalita výstupu se NEMĚNÍ (stejné modely, stejné parametry enkódování).
Všechny změny jsou čistě výkonové. Jediná změna chování: full framy se
nově extrahují jako JPG q100 místo PNG (viz bod 6) — lze vrátit v configu.

1) PHP API — migrace DB jen jednou (api/config.php)
   Schema.sql + ~25 ALTER TABLE migrací běželo při KAŽDÉM HTTP requestu.
   Worker přitom polluje API několikrát za sekundu. Nově se migrace pouští
   jen když nesedí PRAGMA user_version (tj. jednou po instalaci/updatu).
   Přidáno PRAGMA synchronous=NORMAL (bezpečné s WAL).
   => citelně nižší latence každého API requestu, snappier editor.

2) ZIP bez zbytečné komprese (worker/run.py: zip_dir)
   JPG/PNG/MP4/MOV se do ZIPu ukládají jako STORED místo DEFLATED.
   Komprese už komprimovaných dat ušetří ~0 % místa a stála spoustu CPU —
   typicky previews.zip plný full-res JPG q100 a výsledné PNG sekvence.
   => výrazně rychlejší "Preparing framů pro editor" a balení výsledků.

3) Paralelní čtení masek při exportu (worker/run.py)
   package_sam_luma_h264 i package_sam_prores4444 načítaly tisíce alfa PNG
   sekvenčně. Nově paralelně přes thread pool (deterministické pořadí merge).
   ProRes: dočasné RGBA framy se ukládají s PNG kompresí 1 (mažou se po encode).

4) RMBG engine — FP16 + překryv GPU/IO (worker/rmbg_engine.py)
   - torch.inference_mode() místo no_grad().
   - FP16 autocast na CUDA (automatický fallback na FP32, kdyby model selhal).
   - Úprava + ukládání PNG masek běží ve vláknech na pozadí, takže se
     překrývá s GPU inferencí dalšího snímku.
   => RMBG režim typicky 1.5–2× rychlejší na GPU.

5) SAM2 tracking — jeden GPU->CPU přenos per frame (worker/pipeline.py)
   Maska všech objektů se z GPU stahuje jedním přenosem místo per-objekt.

6) Full framy jako JPG q100 (worker/config.json + run.py default)
   Vizuálně bezztrátové, SAM2 video predictor je čte přímo:
   - odpadá pomalý PNG zápis při extrakci (a ~5x větší soubory na disku),
   - odpadá celá PNG->JPG sam2_jpg transcode cache,
   - editor framy se kopírují 1:1 místo rekomprese.
   Striktně bezztrátový režim: v worker/config.json nastav
   "full_frame_format": "png".

7) Extrakce videa jedním průchodem (worker/extract.py)
   Full framy + náhledy se generují v JEDNOM ffmpeg příkazu (dvě výstupní
   cesty) — video se nedekóduje dvakrát. OpenCV fallback nově zapisuje
   snímky ve vláknech na pozadí (překryv s dekódováním).

8) Rychlejší anti-flicker postprocess (worker/pipeline.py)
   - Detekce střihů dekóduje snímky v 1/8 rozlišení (nativní JPEG reduced
     decode) a paralelně.
   - Finální luma halo killer (čte frame z disku per snímek) běží paralelně.
   - Kopie framů pro MatAnyone2 (Windows bez symlinků) běží paralelně.

9) Worker heartbeat bez subprocess smrště (worker/run.py)
   Heartbeat (každých 1.5 s) si pamatuje, který nvidia-smi exe/formát
   funguje (1 subprocess místo až 18 pokusů) a jméno GPU cachuje. WMIC /
   PowerShell fallbacky se po prvním selhání nezkouší pořád dokola.

10) Úklid BAT souborů — přehledná struktura
   V hlavní složce zůstaly jen 4 spouštěcí soubory:
     START.bat, STOP_PZ_MASK.bat, install.bat, UNINSTALL.bat
   Vše ostatní (opravy, diagnostika, stahování modelů, alternativní
   instalace) je v podsložce nastroje\ — popis v nastroje\CTI_ME.txt.
   Smazány bajtově identické duplicity:
     INSTALL_UNLOCKED.bat (= install.bat),
     REPAIR_CUDA_TORCH_SAM2_251.bat (= REPAIR_CUDA_TORCH.bat),
     MASK_START.bat + START_PZ_MASK.bat (sloučeno do START.bat).
   Přesunuté skripty mají opravené cesty (běží o úroveň výš) a všechny
   odkazy v PHP/Python/instalátorech jsou aktualizované.
