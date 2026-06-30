<?php
// PZ Mask Studio — konfigurace a sdílené helpery
declare(strict_types=1);

// ---- Cesty ------------------------------------------------------------------
define('APP_ROOT',   dirname(__DIR__));               // .../MaskStudio
define('STORAGE',    APP_ROOT . '/storage');
define('UPLOADS',    STORAGE . '/uploads');
define('JOBS_DIR',   STORAGE . '/jobs');              // extrahované framy per job
define('RESULTS',    STORAGE . '/results');           // hotové alfa sekvence
define('DB_PATH',    STORAGE . '/maskstudio.db');

// ---- Bezpečnost / worker auth ----------------------------------------------
// Worker se autentizuje tímto tokenem (hlavička X-Worker-Token).
// ZMĚŇ na vlastní náhodný řetězec a stejný dej do worker/config.json!
define('WORKER_TOKEN', getenv('MASKSTUDIO_WORKER_TOKEN') ?: 'CHANGE-ME-worker-secret-7f3a9c');

// Limity uploadu
define('MAX_UPLOAD_BYTES', 4 * 1024 * 1024 * 1024);   // 4 GB
define('ALLOWED_VIDEO_EXT', ['mp4', 'mov', 'm4v', 'avi', 'mkv']);
define('ALLOWED_SEQ_EXT',   ['png', 'jpg', 'jpeg', 'exr', 'tif', 'tiff']);

// ---- DB ---------------------------------------------------------------------
// Zvyš při každé změně schématu/migrací — vynutí jejich jednorázové přehrání.
define('SCHEMA_VERSION', 73);

function db(): PDO {
    static $pdo = null;
    if ($pdo === null) {
        $first = !file_exists(DB_PATH);
        $pdo = new PDO('sqlite:' . DB_PATH);
        $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
        $pdo->setAttribute(PDO::ATTR_DEFAULT_FETCH_MODE, PDO::FETCH_ASSOC);
        $pdo->exec('PRAGMA journal_mode = WAL;');
        $pdo->exec('PRAGMA foreign_keys = ON;');
        $pdo->exec('PRAGMA busy_timeout = 5000;');
        $pdo->exec('PRAGMA synchronous = NORMAL;');

        // V70: schema.sql + migrace běží jen jednou po instalaci/aktualizaci,
        // ne při každém HTTP requestu (worker polluje API několikrát za sekundu).
        // Marker drží PRAGMA user_version přímo v DB souboru.
        $uv = (int)($pdo->query('PRAGMA user_version')->fetchColumn() ?: 0);
        if ($uv !== SCHEMA_VERSION) {
            // CREATE TABLE IF NOT EXISTS je nedestruktivní a opraví staré instalace,
            // kde už existuje DB, ale chybí novější tabulky (typicky previews).
            $schema = file_get_contents(APP_ROOT . '/db/schema.sql');
            if ($schema !== false) $pdo->exec($schema);

            // Lehká migrace pro starší DB při přepsání aplikace novým ZIPem.
            ensure_schema_columns($pdo);
            try { $pdo->exec('PRAGMA user_version = ' . SCHEMA_VERSION); } catch (Throwable $e) {}
        }
    }
    return $pdo;
}


function ensure_schema_columns(PDO $pdo): void {
    $cols = [];
    foreach ($pdo->query('PRAGMA table_info(jobs)')->fetchAll() as $c) {
        $cols[$c['name']] = true;
    }
    $add = [
        'engine'           => "ALTER TABLE jobs ADD COLUMN engine TEXT DEFAULT 'sam2'",
        'rmbg_model_id'    => "ALTER TABLE jobs ADD COLUMN rmbg_model_id TEXT DEFAULT 'briaai/RMBG-1.4'",
        'rmbg_invert'      => "ALTER TABLE jobs ADD COLUMN rmbg_invert INTEGER DEFAULT 0",
        'rmbg_blur_radius' => "ALTER TABLE jobs ADD COLUMN rmbg_blur_radius REAL DEFAULT 0.0",
        'rmbg_gamma'       => "ALTER TABLE jobs ADD COLUMN rmbg_gamma REAL DEFAULT 1.0",
        'rmbg_force_cpu'   => "ALTER TABLE jobs ADD COLUMN rmbg_force_cpu INTEGER DEFAULT 0",
        'rmbg_crf'         => "ALTER TABLE jobs ADD COLUMN rmbg_crf INTEGER DEFAULT 12",
        'matte_edge_feather' => "ALTER TABLE jobs ADD COLUMN matte_edge_feather REAL DEFAULT 0.75",
        'matte_edge_shrink'  => "ALTER TABLE jobs ADD COLUMN matte_edge_shrink INTEGER DEFAULT -1",
        'matte_edge_choke'   => "ALTER TABLE jobs ADD COLUMN matte_edge_choke INTEGER DEFAULT 0",
        'matte_edge_cleanup' => "ALTER TABLE jobs ADD COLUMN matte_edge_cleanup INTEGER DEFAULT 18",
        'refine_enabled' => "ALTER TABLE jobs ADD COLUMN refine_enabled INTEGER DEFAULT 0",
        'refine_hair_detail' => "ALTER TABLE jobs ADD COLUMN refine_hair_detail INTEGER DEFAULT 78",
        'refine_edge_radius' => "ALTER TABLE jobs ADD COLUMN refine_edge_radius INTEGER DEFAULT 12",
        'refine_face_detail' => "ALTER TABLE jobs ADD COLUMN refine_face_detail INTEGER DEFAULT 68",
        'refine_hand_detail' => "ALTER TABLE jobs ADD COLUMN refine_hand_detail INTEGER DEFAULT 58",
        'refine_color_decontaminate' => "ALTER TABLE jobs ADD COLUMN refine_color_decontaminate INTEGER DEFAULT 24",
        'refine_smart_feather' => "ALTER TABLE jobs ADD COLUMN refine_smart_feather REAL DEFAULT 0.5",
        'refine_smart_choke' => "ALTER TABLE jobs ADD COLUMN refine_smart_choke INTEGER DEFAULT 0",
        'refine_mode' => "ALTER TABLE jobs ADD COLUMN refine_mode TEXT DEFAULT 'fast'",
        'refine_auto_hair' => "ALTER TABLE jobs ADD COLUMN refine_auto_hair INTEGER DEFAULT 0",
        'refine_auto_face' => "ALTER TABLE jobs ADD COLUMN refine_auto_face INTEGER DEFAULT 0",
        'refine_mask_contrast' => "ALTER TABLE jobs ADD COLUMN refine_mask_contrast INTEGER DEFAULT 18",
        'refine_luma_halo' => "ALTER TABLE jobs ADD COLUMN refine_luma_halo INTEGER DEFAULT 28",
        'refine_edge_contrast' => "ALTER TABLE jobs ADD COLUMN refine_edge_contrast INTEGER DEFAULT 16",
        'custom_output_path' => "ALTER TABLE jobs ADD COLUMN custom_output_path TEXT",
    ];
    foreach ($add as $name => $sql) {
        if (!isset($cols[$name])) {
            try { $pdo->exec($sql); } catch (Throwable $e) { /* ignore if concurrent */ }
        }
    }

    // V67 FAST CONTROL: starší DB měla defaulty AUTO HQ. Nové joby mají být FAST,
    // ale existující rozpracované joby nepřepisujeme.
    try { $pdo->exec("UPDATE jobs SET sam_model='hiera_base_plus' WHERE status='created' AND (sam_model IS NULL OR sam_model='' OR sam_model='hiera_large')"); } catch (Throwable $e) {}
    try { $pdo->exec("UPDATE jobs SET matte_enabled=0 WHERE status='created' AND matte_enabled IS NULL"); } catch (Throwable $e) {}
    try { $pdo->exec("UPDATE jobs SET refine_mode='fast' WHERE status='created' AND (refine_mode IS NULL OR refine_mode='' OR refine_mode='hq') AND refine_enabled=0"); } catch (Throwable $e) {}

    // V35: robustní migrace náhledové fronty. Starší DB může existovat bez tabulky previews
    // a pak worker/preview-claim padá na HTTP 500. Tohle ji vždy bezpečně vytvoří.
    try {
        $pdo->exec("CREATE TABLE IF NOT EXISTS previews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            frame_index INTEGER NOT NULL DEFAULT 0,
            sam_model TEXT NOT NULL DEFAULT 'hiera_large',
            points_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            mask_path TEXT,
            error_msg TEXT,
            worker_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )");
        $pdo->exec("CREATE INDEX IF NOT EXISTS idx_previews_status ON previews(status)");
        $pdo->exec("CREATE INDEX IF NOT EXISTS idx_previews_job ON previews(job_id)");

        $pcols = [];
        foreach ($pdo->query('PRAGMA table_info(previews)')->fetchAll() as $c) {
            $pcols[$c['name']] = true;
        }
        $padd = [
            'frame_index' => "ALTER TABLE previews ADD COLUMN frame_index INTEGER NOT NULL DEFAULT 0",
            'sam_model'   => "ALTER TABLE previews ADD COLUMN sam_model TEXT NOT NULL DEFAULT 'hiera_large'",
            'points_json' => "ALTER TABLE previews ADD COLUMN points_json TEXT NOT NULL DEFAULT '[]'",
            'status'      => "ALTER TABLE previews ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'",
            'mask_path'   => "ALTER TABLE previews ADD COLUMN mask_path TEXT",
            'error_msg'   => "ALTER TABLE previews ADD COLUMN error_msg TEXT",
            'worker_id'   => "ALTER TABLE previews ADD COLUMN worker_id TEXT",
            'created_at'  => "ALTER TABLE previews ADD COLUMN created_at TEXT",
            'updated_at'  => "ALTER TABLE previews ADD COLUMN updated_at TEXT",
        ];
        foreach ($padd as $name => $sql) {
            if (!isset($pcols[$name])) {
                try { $pdo->exec($sql); } catch (Throwable $e) { /* ignore if concurrent */ }
            }
        }
    } catch (Throwable $e) {
        // API má běžet dál; konkrétní endpoint pak případnou chybu vrátí s textem.
    }
}

// ---- JSON helpery -----------------------------------------------------------
function json_out($data, int $code = 200): void {
    http_response_code($code);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
    header('Pragma: no-cache');
    echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function json_err(string $msg, int $code = 400): void {
    json_out(['ok' => false, 'error' => $msg], $code);
}

function body_json(): array {
    $raw = file_get_contents('php://input');
    if ($raw === '' || $raw === false) return [];
    $d = json_decode($raw, true);
    return is_array($d) ? $d : [];
}

// ---- Drobnosti --------------------------------------------------------------
function rand_token(int $len = 16): string {
    return substr(bin2hex(random_bytes($len)), 0, $len);
}

function require_worker_auth(): void {
    $tok = $_SERVER['HTTP_X_WORKER_TOKEN'] ?? '';
    if (!hash_equals(WORKER_TOKEN, $tok)) {
        json_err('Neautorizovaný worker', 401);
    }
}

function ensure_dirs(): void {
    foreach ([STORAGE, UPLOADS, JOBS_DIR, RESULTS] as $d) {
        if (!is_dir($d)) @mkdir($d, 0775, true);
    }
}

function job_log(int $jobId, string $msg, string $level = 'info'): void {
    $st = db()->prepare('INSERT INTO job_log(job_id,level,msg) VALUES(?,?,?)');
    $st->execute([$jobId, $level, $msg]);
}

function touch_job(int $jobId): void {
    db()->prepare("UPDATE jobs SET updated_at=datetime('now') WHERE id=?")->execute([$jobId]);
}

ensure_dirs();
