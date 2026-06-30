# PZ AI Mask Studio

**Aktuální verze: `PZ_MASK_v73_WIN11_LOCAL`**

PZ AI Mask Studio je **lokální** Windows nástroj pro tvorbu masek osob/objektů
z videa a export čistých luma matte pro střih, kompozit a postprodukci
(Premiere / After Effects). Běží **jen na tvém PC** ve webovém prohlížeči na
adrese `http://127.0.0.1:8080` — nic se nenahrává na web ani do cloudu.

Pod kapotou: PHP frontend/API + Python GPU worker (SAM 2.1 tracking,
MatAnyone 2 matting, RMBG luma).

---

## Stažení a instalace (Windows 11, lokálně)

Vyber si jeden ze dvou balíků v tomto repozitáři:

- **`PZ_MASK_v73_WIN11_PORTABLE.zip`** — *doporučeno.* Portable: rozbalíš
  kamkoliv (i na externí disk), nainstaluješ do té samé složky a spouštíš
  odtud. Žádná instalace do systému, žádná práva správce.
- **`PZ_MASK_v73_WIN11_LOCAL.zip`** — installer s výběrem cílové složky
  (zkopíruje appku do zvolené složky a tam nainstaluje runtime).

### Portable (doporučeno)

1. **Rozbal celý ZIP** do složky (krátká cesta bez diakritiky, např.
   `C:\PZ_MASK`). Nespouštěj soubory přímo ze ZIPu — nejdřív je rozbal.
2. Dvojklik na **`INSTALL_HERE.bat`** — stáhne a připraví Python, PyTorch
   (CUDA), modely a PHP přímo do té složky (běží jen jednou).
   - Když Windows ukáže „Windows ochránil váš počítač" → „Více informací" →
     „Přesto spustit".
3. Spouštěj přes **`START.bat`**. V prohlížeči se sám otevře
   `http://127.0.0.1:8080`.

### Installer s výběrem složky

1. Rozbal celý ZIP, dvojklik na **`INSTALL_PZ_MASK.bat`**, vyber cílovou složku.
2. Po dokončení spouštěj appku z té složky přes **`START.bat`**.

> Pozn.: „portable" znamená celé v jedné složce bez instalace do systému.
> Runtime (Python/PyTorch/modely, několik GB) se kvůli velikosti nedá přibalit
> do ZIPu, proto se stáhne jednou online při instalaci. Pak appka běží offline.

### Co potřebuješ

- Windows 10/11 (64-bit).
- NVIDIA GPU s aktuálním ovladačem (doporučeno; bez něj jede pomalu na CPU).
- ~15 GB volného místa.
- Internet **při první instalaci** (stahuje se několik GB: PyTorch CUDA,
  modely, PHP). Potom appka funguje offline.

### Každodenní použití

```bat
START.bat            spustí vše (server + worker + prohlížeč)
STOP_PZ_MASK.bat     vše zastaví
UNINSTALL.bat        kompletně odinstaluje (smaže nainstalovanou složku)
```

---

## Co appka umí

- **SAM2 + MatAnyone** — ruční přesný výběr a trackování objektu/osoby:
  klikem nebo obdélníkem označíš subjekt, tracking ho projede celou stopou,
  MatAnyone 2 dotáhne jemné okraje (vlasy, ramena, rukávy).
- **RMBG Luma** — jedním klikem černobílá H.264 luma maska přímo z videa,
  bez editoru (RMBG-1.4 fallback, RMBG-2.0 s HF tokenem).
- **Výstup** — H.264 luma video (bílá = objekt, černá = pozadí) nebo PNG
  sekvence; připravené pro track matte v Premiere / After Effects.
- **Lokální worker** — kontrola CUDA/PyTorch, fronta úloh a stav v rozhraní,
  živý náhled trackingu.

## Co je nového ve v73

- **Oprava SAM2 (`GlobalHydra is not initialized`):** worker teď spolehlivě
  inicializuje Hydru přímo na složku s configy. Bez toho spadl každý preview
  i tracking a úloha se zasekla na „čeká na extrakci framů".
- **Oprava instalace končící s CPU PyTorchem:** instalátory ověří CUDA a v
  případě potřeby přeinstalují CUDA wheely; PowerShellový installer nově
  instaluje torch 2.5.1 (SAM2 vyžaduje ≥2.5.1, dřív byl 2.3.1).
- **Výběr výstupní složky:** pole „Output folder" na stránce nové úlohy —
  předem zvolíš cestu (pamatuje se) a hotová maska se tam zkopíruje,
  pojmenovaná podle úlohy.
- **Turbo režim (rychlejší zpracování):** na NVIDIA GPU se zapnou bezpečná
  zrychlení (cudnn.benchmark, TF32, matmul „high") a H.264 export jede přes
  NVENC (GPU enkodér) s fallbackem na libx264; řízeno blokem `performance`
  v `worker/config.json`.
- **Celé rozhraní a hlášky v angličtině** (hlavní stránka, editor, RMBG,
  update, worker i API).
- **Rychlejší a stabilní start:** `START.bat` už nečeká 20 s na API a
  nezabíjí port 8080; běžící server jen použije. Pořadí: frontend → prohlížeč
  → worker.
- **Spolehlivější `STOP`:** ukončení podle titulku okna (funguje i na novějších
  Windows 11 bez WMIC).
- **Funkční jádro z v70 zachováno** (SAM2 / MatAnyone 2 / RMBG, PHP API,
  Python worker).

## Řešení potíží

- Appka nereaguje: `STOP_PZ_MASK.bat`, pak znovu `START.bat`.
- V prohlížeči vždy `127.0.0.1`, ne `localhost`.
- Blokovaný PowerShell: instalátor používá čistě CMD; když přesto selže,
  spusť `nastroje\INSTALL_NO_POWERSHELL.bat`.
- Logy k odeslání: `nastroje\DIAGNOSE_PZ_MASK.bat`.

## Poznámky

- Aplikace je určená pro lokální použití na Windows.
- Váhy modelů třetích stran nejsou součástí repozitáře — stahují se lokálně
  při instalaci / prvním běhu.
- Velké runtime složky, checkpointy, uploady, joby a výsledky se necommitují.

## Hlavní upstream projekty

- SAM2: https://github.com/facebookresearch/sam2
- RMBG modely: https://huggingface.co/briaai
- MatAnyone: https://github.com/pq-yang/MatAnyone
- FFmpeg: https://ffmpeg.org/
- PyTorch: https://pytorch.org/

## Licence

Kód wrapperu aplikace je pod licencí MIT. Modely a balíčky třetích stran
zůstávají pod svými vlastními licencemi.
