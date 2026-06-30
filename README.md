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

Stáhni instalační balík z tohoto repozitáře:

- **`PZ_MASK_v73_WIN11_LOCAL.zip`**

Pak:

1. **Rozbal celý ZIP** do nějaké složky (pravý klik → „Extrahovat vše").
   Nespouštěj soubory přímo ze ZIPu — nejdřív je rozbal.
2. Dvojklik na **`INSTALL_PZ_MASK.bat`**.
   - Když Windows ukáže „Windows ochránil váš počítač" → „Více informací" →
     „Přesto spustit".
   - Vyber cílovou složku (ideálně krátká cesta bez diakritiky, např.
     `C:\PZ_MASK`).
   - Instalace stáhne a připraví Python, PHP, modely a knihovny. Běží jen
     jednou a chvíli to trvá.
3. Po dokončení spouštěj appku z nainstalované složky přes **`START.bat`**.
   V prohlížeči se sám otevře `http://127.0.0.1:8080`.

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

- **Rychlejší a stabilní start:** `START.bat` už nečeká naprázdno 20 s na API
  a už automaticky nezabíjí port 8080. Když server běží, jen ho použije
  (nespouští druhý server ani druhého workera). Pořadí: frontend → prohlížeč
  → worker (worker se k API připojuje opakovaně sám).
- **Spolehlivější `STOP`:** ukončení primárně podle titulku okna
  (`taskkill /FI WINDOWTITLE`), funguje i na novějších Windows 11 bez WMIC.
- **Sjednocené verze:** `APP_VERSION.txt`, `START.bat`, `STOP_PZ_MASK.bat`
  i `update_manifest.json` nově hlásí jednotně v73.
- **Funkční jádro z v70 zachováno** (SAM2 / MatAnyone 2 / RMBG, PHP API,
  Python worker) — měnila se jen instalace, spouštění a balení.

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
