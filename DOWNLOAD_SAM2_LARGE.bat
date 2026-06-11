-- PZ Mask Studio — SQLite schéma
-- Spustit: sqlite3 storage/maskstudio.db < db/schema.sql

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Joby (jeden upload = jeden job)
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT UNIQUE NOT NULL,          -- náhodný identifikátor (URL)
    name            TEXT NOT NULL,                 -- uživatelský název
    source_type     TEXT NOT NULL,                 -- 'video' | 'sequence'
    source_path     TEXT,                          -- cesta k uploadu (rel. ke storage)
    frame_count     INTEGER DEFAULT 0,
    width           INTEGER DEFAULT 0,
    height          INTEGER DEFAULT 0,
    fps             REAL DEFAULT 25.0,
    -- workflow stav
    status          TEXT NOT NULL DEFAULT 'created',
        -- created | extracting | ready | queued | claimed | tracking | matting | done | error
    progress        REAL DEFAULT 0.0,              -- 0..1
    stage_msg       TEXT DEFAULT '',               -- lidsky čitelný stav
    -- engine / režim
    engine          TEXT DEFAULT 'sam2',              -- sam2 | rmbg
    -- nastavení výpočtu SAM2
    sam_model       TEXT DEFAULT 'hiera_base_plus', -- hiera_large|hiera_base_plus|hiera_small|hiera_tiny (base_plus = rozumný default na 12GB)
    multi_mode      TEXT DEFAULT 'combined',       -- separate | combined
    matte_enabled   INTEGER DEFAULT 0,             -- 0/1 zapnout MatAnyone fázi
    output_format   TEXT DEFAULT 'h264_luma',          -- h264_luma | png16 | png8 | exr
    matte_edge_feather REAL DEFAULT 0.75,              -- změkčení okraje v px
    matte_edge_shrink  INTEGER DEFAULT -1,            -- contract/expand okraje v px
    matte_edge_choke   INTEGER DEFAULT 0,             -- dodatečný choke/ořez masky dovnitř v px
    matte_edge_cleanup INTEGER DEFAULT 18,            -- potlačení chlupatého okraje 0..100
    refine_enabled INTEGER DEFAULT 0,                   -- Refine Edge postprocess 0/1
    refine_hair_detail INTEGER DEFAULT 78,              -- vlasové detaily 0..100
    refine_edge_radius INTEGER DEFAULT 12,               -- boundary radius px
    refine_face_detail INTEGER DEFAULT 68,              -- uši/tvář/kontura hlavy 0..100
    refine_hand_detail INTEGER DEFAULT 58,              -- prsty/ruce 0..100
    refine_color_decontaminate INTEGER DEFAULT 24,      -- V36 legacy storage: silhouette smooth 0..100
    refine_smart_feather REAL DEFAULT 0.5,              -- jemné změkčení po refine
    refine_smart_choke INTEGER DEFAULT 0,               -- choke/expand po refine -10..10
    refine_mode TEXT DEFAULT 'fast',                    -- fast | hq
    refine_auto_hair INTEGER DEFAULT 0,                 -- auto region vlasů
    refine_auto_face INTEGER DEFAULT 0,                 -- auto kontura obličeje
    refine_mask_contrast INTEGER DEFAULT 18,             -- V38 finalni kontrast alfa/luma masky 0..100
    refine_luma_halo INTEGER DEFAULT 28,                 -- V39 potlaceni sedeho/bileho luma lemu 0..100
    refine_edge_contrast INTEGER DEFAULT 16,             -- V39 kontrast jen v okrajove zone 0..100
    -- nastavení RMBG Luma
    rmbg_model_id   TEXT DEFAULT 'briaai/RMBG-1.4', -- otevřenější fallback; RMBG-2.0 je gated
    rmbg_invert     INTEGER DEFAULT 0,
    rmbg_blur_radius REAL DEFAULT 0.0,
    rmbg_gamma      REAL DEFAULT 1.0,
    rmbg_force_cpu  INTEGER DEFAULT 0,
    rmbg_crf        INTEGER DEFAULT 12,
    -- worker
    worker_id       TEXT,
    error_msg       TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- Masky/objekty v rámci jobu (multi-object)
CREATE TABLE IF NOT EXISTS masks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    label           TEXT NOT NULL DEFAULT 'mask',
    color           TEXT NOT NULL DEFAULT '#33dd88', -- swatch barva v UI
    keyframe        INTEGER NOT NULL DEFAULT 0,       -- frame index keyframu
    ord             INTEGER NOT NULL DEFAULT 0,       -- pořadí ve stacku
    created_at      TEXT DEFAULT (datetime('now'))
);

-- Prompty (body / brush stroky) pro danou masku
-- Body: kind='point', x,y v normalizovaných souřadnicích 0..1, val=1 (FG) / 0 (BG)
-- Brush: kind='brush', uloženo jako PNG maska v brush_path
CREATE TABLE IF NOT EXISTS prompts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mask_id         INTEGER NOT NULL REFERENCES masks(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,                 -- 'point' | 'brush'
    x               REAL,                          -- pro point (0..1)
    y               REAL,
    val             INTEGER DEFAULT 1,             -- 1=FG/paint, 0=BG/erase
    brush_path      TEXT,                          -- pro brush (rel. cesta k PNG masce)
    created_at      TEXT DEFAULT (datetime('now'))
);

-- Log událostí jobu (pro debug a UI feed)
CREATE TABLE IF NOT EXISTS job_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    level           TEXT DEFAULT 'info',           -- info | warn | error
    msg             TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_masks_job   ON masks(job_id);
CREATE INDEX IF NOT EXISTS idx_prompts_mask ON prompts(mask_id);
CREATE INDEX IF NOT EXISTS idx_log_job     ON job_log(job_id);

-- Náhledové požadavky (interaktivní single-frame SAM maska).
-- Krátká životnost: klient vytvoří, worker zpracuje, klient si vyzvedne masku.
CREATE TABLE IF NOT EXISTS previews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    frame_index INTEGER NOT NULL DEFAULT 0,
    sam_model   TEXT NOT NULL DEFAULT 'hiera_large',
    points_json TEXT NOT NULL DEFAULT '[]',          -- [{x,y,val}, ...]
    status      TEXT NOT NULL DEFAULT 'pending',      -- pending | claimed | done | error
    mask_path   TEXT,                                 -- rel. cesta k PNG masce (po dokončení)
    error_msg   TEXT,
    worker_id   TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_previews_status ON previews(status);
CREATE INDEX IF NOT EXISTS idx_previews_job    ON previews(job_id);
