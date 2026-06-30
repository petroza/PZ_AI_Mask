<?php
// PZ Mask Studio — REST API router
// Endpointy (přes ?action=… nebo PATH_INFO):
//
//  KLIENT (prohlížeč):
//   POST  create            – založí job (multipart upload videa/sekvence)
//   GET   job?token=…       – detail jobu + masky + prompty
//   GET   list              – seznam jobů
//   GET   frame?token=&i=   – vrátí náhledový frame (jpg)
//   GET   source-video?token= – streamuje původní video pro instantní první frame
//   POST  masks/save        – uloží masky a prompty (JSON)
//   POST  masks/brush       – upload brush PNG masky pro prompt
//   POST  enqueue           – zařadí job do fronty pro worker
//   GET   result?token=&f=  – stáhne výsledek (zip / jednotlivý alfa frame)
//   POST  delete            – smaže job
//   POST  delete-errors     – smaže všechny chybové joby
//   POST  delete-all        – smaže všechny joby
//
//  WORKER (X-Worker-Token):
//   POST  worker/claim          – atomicky převezme 1 queued job
//   POST  worker/progress       – update progress/stage
//   GET   worker/job?id=        – plný popis jobu vč. masek/promptů/cest
//   POST  worker/result-meta    – nahlásí hotovo + cesty výsledků
//   POST  worker/fail           – nahlásí chybu

declare(strict_types=1);
require __DIR__ . '/config.php';

$action = $_GET['action'] ?? trim($_SERVER['PATH_INFO'] ?? '', '/');
$method = $_SERVER['REQUEST_METHOD'];

try {
    switch ($action) {

        // ---------------- KLIENT ----------------
        case 'create':        require_post(); create_job(); break;
        case 'job':           get_job(); break;
        case 'list':          list_jobs(); break;
        case 'frame':         get_frame(); break;
        case 'source-video':  get_source_video(); break;
        case 'masks/save':    require_post(); save_masks(); break;
        case 'masks/brush':   require_post(); save_brush(); break;
        case 'preview/request': require_post(); preview_request(); break;
        case 'preview/warmup': require_post(); preview_warmup(); break;
        case 'preview/result':  preview_result(); break;
        case 'preview/mask':    preview_mask(); break;
        case 'enqueue':       require_post(); enqueue_job(); break;
        case 'result':        get_result(); break;
        case 'delete':        require_post(); delete_job(); break;
        case 'delete-errors': require_post(); delete_jobs_bulk(['error']); break;
        case 'delete-all':    require_post(); delete_jobs_bulk(null); break;
        case 'cancel':        require_post(); cancel_job(); break;
        case 'tracking-preview': tracking_preview(); break;
        case 'tracking-preview-image': tracking_preview_image(); break;
        case 'system-status': system_status(); break;
        case 'app/status':    app_status(); break;
        case 'update/install': require_post(); update_install(); break;

        // ---------------- WORKER ----------------
        case 'worker/claim':       require_post(); require_worker_auth(); worker_claim(); break;
        case 'worker/progress':    require_post(); require_worker_auth(); worker_progress(); break;
        case 'worker/status':      require_post(); require_worker_auth(); worker_status(); break;
        case 'worker/job':         require_worker_auth(); worker_job(); break;
        case 'worker/result-meta': require_post(); require_worker_auth(); worker_result_meta(); break;
        case 'worker/upload-result': require_post(); require_worker_auth(); worker_upload_result(); break;
        case 'worker/upload-frames': require_post(); require_worker_auth(); worker_upload_frames(); break;
        case 'worker/fail':        require_post(); require_worker_auth(); worker_fail(); break;
        case 'worker/preview-claim':  require_post(); require_worker_auth(); worker_preview_claim(); break;
        case 'worker/preview-result': require_post(); require_worker_auth(); worker_preview_result(); break;
        case 'worker/preview-fail':   require_post(); require_worker_auth(); worker_preview_fail(); break;

        case '':       json_out(['ok' => true, 'service' => 'PZ Mask Studio API', 'version' => 1]);
        default:       json_err('Unknown action: ' . $action, 404);
    }
} catch (Throwable $e) {
    json_err('Server error: ' . $e->getMessage(), 500);
}

// ============================================================================
//  Pomocné
// ============================================================================
function require_post(): void {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') json_err('POST required', 405);
}

function find_job_by_token(string $token): ?array {
    $st = db()->prepare('SELECT * FROM jobs WHERE token=?');
    $st->execute([$token]);
    $r = $st->fetch();
    return $r ?: null;
}

function job_dir(array $job): string {
    return JOBS_DIR . '/' . $job['token'];
}

function stop_flag_path(array $job): string {
    return job_dir($job) . '/.stop_requested';
}

function tracking_preview_json_path(array $job): string {
    return job_dir($job) . '/tracking_preview.json';
}

function tracking_preview_image_path(array $job): string {
    return job_dir($job) . '/tracking_preview.jpg';
}

// ============================================================================
//  KLIENT endpointy
// ============================================================================

function create_job(): void {
    $name = trim($_POST['name'] ?? 'Untitled');
    if (!isset($_FILES['file'])) json_err('File missing');
    $f = $_FILES['file'];
    if ($f['error'] !== UPLOAD_ERR_OK) json_err('Upload error (code ' . $f['error'] . ')');
    if ($f['size'] > MAX_UPLOAD_BYTES) json_err('File is too large');

    $ext = strtolower(pathinfo($f['name'], PATHINFO_EXTENSION));
    if (in_array($ext, ALLOWED_VIDEO_EXT, true)) {
        $sourceType = 'video';
    } elseif (in_array($ext, ALLOWED_SEQ_EXT, true)) {
        $sourceType = 'sequence';
    } else {
        json_err('Unsupported file type: .' . $ext);
    }

    $engine = ($_POST['engine'] ?? 'sam2') === 'rmbg' ? 'rmbg' : 'sam2';
    if ($engine === 'rmbg' && $sourceType !== 'video') {
        json_err('RMBG Luma mode currently supports video files only. For sequences use the SAM2 workflow.');
    }
    $rmbgModel = trim((string)($_POST['rmbg_model_id'] ?? 'briaai/RMBG-1.4'));
    if ($rmbgModel === '') $rmbgModel = 'briaai/RMBG-1.4';
    if (!in_array($rmbgModel, ['briaai/RMBG-1.4','briaai/RMBG-2.0'], true)) $rmbgModel = 'briaai/RMBG-1.4';
    $rmbgInvert = isset($_POST['rmbg_invert']) ? (int)(bool)filter_var($_POST['rmbg_invert'], FILTER_VALIDATE_BOOLEAN) : 0;
    $rmbgBlur   = max(0.0, min(8.0, (float)($_POST['rmbg_blur_radius'] ?? 0.0)));
    $rmbgGamma  = max(0.2, min(5.0, (float)($_POST['rmbg_gamma'] ?? 1.0)));
    $rmbgCpu    = isset($_POST['rmbg_force_cpu']) ? (int)(bool)filter_var($_POST['rmbg_force_cpu'], FILTER_VALIDATE_BOOLEAN) : 0;
    $rmbgCrf    = max(8, min(23, (int)($_POST['rmbg_crf'] ?? 12))); // FAST ENCODE default

    // Optional folder on disk where the finished mask is also copied (chosen in UI).
    $outputPath = trim((string)($_POST['output_path'] ?? ''));
    if (strlen($outputPath) > 1000) $outputPath = substr($outputPath, 0, 1000);

    $token = rand_token(16);
    $dest  = UPLOADS . '/' . $token . '.' . $ext;
    if (!move_uploaded_file($f['tmp_name'], $dest)) json_err('Cannot save upload');

    $initialStatus = $engine === 'rmbg' ? 'queued' : 'created';
    $initialMsg    = $engine === 'rmbg' ? 'In queue for RMBG Luma export' : 'Uploaded, waiting for frame extraction';

    $st = db()->prepare(
        'INSERT INTO jobs(token,name,source_type,source_path,status,stage_msg,engine,sam_model,matte_enabled,refine_enabled,refine_mode,
                          rmbg_model_id,rmbg_invert,rmbg_blur_radius,rmbg_gamma,rmbg_force_cpu,rmbg_crf,custom_output_path)
         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
    );
    // New jobs default to FAST: Base+, MatAnyone OFF, Refine OFF, refine_mode=fast.
    $st->execute([$token, $name, $sourceType, basename($dest), $initialStatus, $initialMsg, $engine, 'hiera_base_plus', 0, 0, 'fast',
                  $rmbgModel, $rmbgInvert, $rmbgBlur, $rmbgGamma, $rmbgCpu, $rmbgCrf, ($outputPath !== '' ? $outputPath : null)]);
    $jobId = (int) db()->lastInsertId();
    job_log($jobId, "Job created ($engine/$sourceType): " . $f['name']);

    // Note: the worker extracts frames on claim (ffmpeg/EXR needs the GPU box).
    json_out(['ok' => true, 'token' => $token, 'id' => $jobId, 'source_type' => $sourceType]);
}

function get_job(): void {
    $token = $_GET['token'] ?? '';
    $job = find_job_by_token($token);
    if (!$job) json_err('Job not found', 404);

    $masks = db()->prepare('SELECT * FROM masks WHERE job_id=? ORDER BY ord, id');
    $masks->execute([$job['id']]);
    $masks = $masks->fetchAll();

    foreach ($masks as &$m) {
        $p = db()->prepare('SELECT id,kind,x,y,val,brush_path FROM prompts WHERE mask_id=? ORDER BY id');
        $p->execute([$m['id']]);
        $m['prompts'] = $p->fetchAll();
    }
    unset($m);

    $log = db()->prepare('SELECT level,msg,created_at FROM job_log WHERE job_id=? ORDER BY id DESC LIMIT 30');
    $log->execute([$job['id']]);

    $job['results'] = result_info_for_job($job);
    json_out(['ok' => true, 'job' => $job, 'masks' => $masks, 'log' => $log->fetchAll()]);
}

function list_jobs(): void {
    $rows = db()->query(
        'SELECT id,token,name,source_type,status,progress,stage_msg,frame_count,
                width,height,engine,created_at,updated_at
         FROM jobs ORDER BY id DESC LIMIT 100'
    )->fetchAll();
    foreach ($rows as &$row) {
        $row['results'] = result_info_for_job($row);
    }
    unset($row);
    json_out(['ok' => true, 'jobs' => $rows]);
}

function get_frame(): void {
    $token = $_GET['token'] ?? '';
    $i     = max(0, (int) ($_GET['i'] ?? 0));
    $job = find_job_by_token($token);
    if (!$job) json_err('Job not found', 404);

    $path = sprintf('%s/frames/%06d.jpg', job_dir($job), $i);
    if (!is_file($path)) json_err('Frame not found (the job may not have prepared frames yet)', 404);

    header('Content-Type: image/jpeg');
    header('Cache-Control: public, max-age=3600');
    readfile($path);
    exit;
}

function get_source_video(): void {
    $token = $_GET['token'] ?? '';
    $job = find_job_by_token($token);
    if (!$job) json_err('Job not found', 404);
    if (($job['source_type'] ?? '') !== 'video') json_err('Source is not a video', 400);

    $path = UPLOADS . '/' . basename((string)$job['source_path']);
    if (!is_file($path)) json_err('Original video not found', 404);

    $size = filesize($path);
    if ($size === false || $size <= 0) json_err('Video has invalid size', 500);

    $ext = strtolower(pathinfo($path, PATHINFO_EXTENSION));
    $mimeMap = [
        'mp4' => 'video/mp4', 'm4v' => 'video/mp4', 'mov' => 'video/quicktime',
        'avi' => 'video/x-msvideo', 'mkv' => 'video/x-matroska'
    ];
    $mime = $mimeMap[$ext] ?? 'application/octet-stream';

    $start = 0;
    $end = $size - 1;
    $status = 200;
    $range = $_SERVER['HTTP_RANGE'] ?? '';
    if (preg_match('/bytes=(\d*)-(\d*)/', $range, $m)) {
        $status = 206;
        if ($m[1] !== '') $start = (int)$m[1];
        if ($m[2] !== '') $end = (int)$m[2];
        if ($m[1] === '' && $m[2] !== '') {
            $suffix = max(0, (int)$m[2]);
            $start = max(0, $size - $suffix);
            $end = $size - 1;
        }
        $start = max(0, min($start, $size - 1));
        $end = max($start, min($end, $size - 1));
    }

    $length = $end - $start + 1;
    http_response_code($status);
    header('Content-Type: ' . $mime);
    header('Accept-Ranges: bytes');
    header('Cache-Control: private, max-age=3600');
    header('Content-Length: ' . $length);
    if ($status === 206) header("Content-Range: bytes $start-$end/$size");

    while (ob_get_level() > 0) { @ob_end_clean(); }
    $fp = fopen($path, 'rb');
    if (!$fp) exit;
    fseek($fp, $start);
    $left = $length;
    $chunk = 1024 * 1024;
    while ($left > 0 && !feof($fp)) {
        $read = min($chunk, $left);
        $buf = fread($fp, $read);
        if ($buf === false || $buf === '') break;
        echo $buf;
        flush();
        $left -= strlen($buf);
    }
    fclose($fp);
    exit;
}

function save_masks(): void {
    $d = body_json();
    $token = $d['token'] ?? '';
    $job = find_job_by_token($token);
    if (!$job) json_err('Job not found', 404);
    $masks = $d['masks'] ?? [];

    $pdo = db();
    $pdo->beginTransaction();
    try {
        // Smaž staré masky tohoto jobu (CASCADE smaže i prompty)
        $pdo->prepare('DELETE FROM masks WHERE job_id=?')->execute([$job['id']]);

        $insM = $pdo->prepare(
            'INSERT INTO masks(job_id,label,color,keyframe,ord) VALUES(?,?,?,?,?)'
        );
        $insP = $pdo->prepare(
            'INSERT INTO prompts(mask_id,kind,x,y,val,brush_path) VALUES(?,?,?,?,?,?)'
        );
        foreach ($masks as $ord => $m) {
            $insM->execute([
                $job['id'],
                substr((string)($m['label'] ?? 'mask'), 0, 64),
                substr((string)($m['color'] ?? '#33dd88'), 0, 9),
                (int)($m['keyframe'] ?? 0),
                $ord,
            ]);
            $maskId = (int) $pdo->lastInsertId();
            foreach (($m['prompts'] ?? []) as $p) {
                $kind = (string)($p['kind'] ?? 'point');
                if ($kind === 'brush') {
                    $insP->execute([
                        $maskId, 'brush', null, null, 1, $p['brush_path'] ?? null,
                    ]);
                } elseif ($kind === 'box') {
                    // Kompatibilně se starým schématem DB: x/y = x0/y0, brush_path = "x1,y1".
                    $x0 = max(0.0, min(1.0, (float)($p['x0'] ?? $p['x'] ?? 0)));
                    $y0 = max(0.0, min(1.0, (float)($p['y0'] ?? $p['y'] ?? 0)));
                    $x1 = max(0.0, min(1.0, (float)($p['x1'] ?? 0)));
                    $y1 = max(0.0, min(1.0, (float)($p['y1'] ?? 0)));
                    $insP->execute([
                        $maskId, 'box', $x0, $y0, 1, $x1 . ',' . $y1,
                    ]);
                } else {
                    $insP->execute([
                        $maskId, 'point',
                        isset($p['x']) ? (float)$p['x'] : null,
                        isset($p['y']) ? (float)$p['y'] : null,
                        (int)($p['val'] ?? 1),
                        null,
                    ]);
                }
            }
        }
        $pdo->commit();
    } catch (Throwable $e) {
        $pdo->rollBack();
        json_err('Failed to save masks: ' . $e->getMessage(), 500);
    }
    job_log($job['id'], 'Masks saved (' . count($masks) . ' objects)');
    json_out(['ok' => true, 'count' => count($masks)]);
}

function save_brush(): void {
    $token = $_POST['token'] ?? '';
    $job = find_job_by_token($token);
    if (!$job) json_err('Job not found', 404);
    if (!isset($_FILES['brush'])) json_err('Brush file missing');
    $f = $_FILES['brush'];
    if ($f['error'] !== UPLOAD_ERR_OK) json_err('Brush upload error');

    $dir = job_dir($job) . '/brushes';
    if (!is_dir($dir)) @mkdir($dir, 0775, true);
    $fname = rand_token(12) . '.png';
    $rel   = 'brushes/' . $fname;
    if (!move_uploaded_file($f['tmp_name'], $dir . '/' . $fname)) {
        json_err('Cannot save brush mask');
    }
    json_out(['ok' => true, 'brush_path' => $rel]);
}

function enqueue_job(): void {
    $d = body_json();
    $token = $d['token'] ?? '';
    $job = find_job_by_token($token);
    if (!$job) json_err('Job not found', 404);

    // přijmout nastavení výpočtu — RUN respektuje UI, nic nevynucuje AUTO HQ.
    $allowedSam = ['hiera_large','hiera_base_plus','base_plus','hiera_small','small','hiera_tiny','tiny'];
    $samModel = (string)($d['sam_model'] ?? $job['sam_model'] ?? 'hiera_base_plus');
    if (!in_array($samModel, $allowedSam, true)) $samModel = 'hiera_base_plus';
    if ($samModel === 'base_plus') $samModel = 'hiera_base_plus';
    if ($samModel === 'small') $samModel = 'hiera_small';
    if ($samModel === 'tiny') $samModel = 'hiera_tiny';
    $multi    = ($d['multi_mode'] ?? $job['multi_mode']) === 'combined' ? 'combined' : 'separate';
    $matte    = isset($d['matte_enabled']) ? (int)(bool)$d['matte_enabled'] : (int)($job['matte_enabled'] ?? 0);
    $allowedOut = ['h264_luma','h264_luma_soft','h264_luma_binary','prores4444','png_alpha',
                   'png16','png8','exr']; // posledni 3 = zpetna kompatibilita
    $outfmt   = in_array($d['output_format'] ?? '', $allowedOut, true)
                ? $d['output_format'] : ($job['output_format'] ?: 'h264_luma');

    $edgeFeather = max(0.0, min(20.0, (float)($d['matte_edge_feather'] ?? $job['matte_edge_feather'] ?? 0.75)));
    $edgeShrink  = max(-10, min(10, (int)($d['matte_edge_shrink'] ?? $job['matte_edge_shrink'] ?? -1)));
    $edgeChoke   = max(0, min(20, (int)($d['matte_edge_choke'] ?? $job['matte_edge_choke'] ?? 0)));
    $edgeCleanup = max(0, min(100, (int)($d['matte_edge_cleanup'] ?? $job['matte_edge_cleanup'] ?? 18)));
    $refineEnabled = isset($d['refine_enabled']) ? (int)(bool)$d['refine_enabled'] : (int)($job['refine_enabled'] ?? 0);
    $refineHair = max(0, min(100, (int)($d['refine_hair_detail'] ?? $job['refine_hair_detail'] ?? 78)));
    $refineRadius = max(1, min(20, (int)($d['refine_edge_radius'] ?? $job['refine_edge_radius'] ?? 12)));
    $refineFace = max(0, min(100, (int)($d['refine_face_detail'] ?? $job['refine_face_detail'] ?? 68)));
    $refineHand = max(0, min(100, (int)($d['refine_hand_detail'] ?? $job['refine_hand_detail'] ?? 58)));
    // V36: color decontaminate odstraněno – výstup je luma.
    // Pro kompatibilitu ukládáme Silhouette Smooth do starého sloupce refine_color_decontaminate.
    $refineSmooth = max(0, min(100, (int)($d['refine_silhouette_smooth'] ?? $d['refine_color_decontaminate'] ?? $job['refine_color_decontaminate'] ?? 24)));
    $refineFeather = max(0.0, min(10.0, (float)($d['refine_smart_feather'] ?? $job['refine_smart_feather'] ?? 0.5)));
    $refineChoke = max(-10, min(10, (int)($d['refine_smart_choke'] ?? $job['refine_smart_choke'] ?? 0)));
    $refineMode = in_array(strtolower((string)($d['refine_mode'] ?? $job['refine_mode'] ?? 'fast')), ['hq','fast'], true) ? strtolower((string)($d['refine_mode'] ?? $job['refine_mode'] ?? 'fast')) : 'fast';
    $refineAutoHair = isset($d['refine_auto_hair']) ? (int)(bool)$d['refine_auto_hair'] : (int)($job['refine_auto_hair'] ?? 0);
    $refineAutoFace = isset($d['refine_auto_face']) ? (int)(bool)$d['refine_auto_face'] : (int)($job['refine_auto_face'] ?? 0);
    $refineMaskContrast = max(0, min(100, (int)($d['refine_mask_contrast'] ?? $job['refine_mask_contrast'] ?? 18)));
    $refineLumaHalo = max(0, min(100, (int)($d['refine_luma_halo'] ?? $job['refine_luma_halo'] ?? 28)));
    $refineEdgeContrast = max(0, min(100, (int)($d['refine_edge_contrast'] ?? $job['refine_edge_contrast'] ?? 16)));

    db()->prepare(
        'UPDATE jobs SET status=?, stage_msg=?, progress=0, worker_id=NULL, error_msg=NULL,
                engine=\'sam2\', sam_model=?, multi_mode=?, matte_enabled=?, output_format=?,
                matte_edge_feather=?, matte_edge_shrink=?, matte_edge_choke=?, matte_edge_cleanup=?,
                refine_enabled=?, refine_hair_detail=?, refine_edge_radius=?, refine_face_detail=?, refine_hand_detail=?,
                refine_color_decontaminate=?, refine_smart_feather=?, refine_smart_choke=?, refine_mode=?,
                refine_auto_hair=?, refine_auto_face=?, refine_mask_contrast=?, refine_luma_halo=?, refine_edge_contrast=?,
                updated_at=datetime(\'now\')
         WHERE id=?'
    )->execute(['queued', 'In queue for worker', $samModel, $multi, $matte, $outfmt,
        $edgeFeather, $edgeShrink, $edgeChoke, $edgeCleanup,
        $refineEnabled, $refineHair, $refineRadius, $refineFace, $refineHand,
        $refineSmooth, $refineFeather, $refineChoke, $refineMode,
        $refineAutoHair, $refineAutoFace, $refineMaskContrast, $refineLumaHalo, $refineEdgeContrast, $job['id']]);

    // Před novým během smaž staré výsledky, aby PNG ZIP nebyl ze staršího výpočtu.
    $resDir = RESULTS . '/' . $job['token'];
    if (is_dir($resDir)) { foreach (glob($resDir . '/result*.*') ?: [] as $old) { @unlink($old); } }

    job_log($job['id'], "Queued (SAM=$samModel, multi=$multi, matte=$matte, out=$outfmt, feather=$edgeFeather, shrink=$edgeShrink, choke=$edgeChoke, cleanup=$edgeCleanup, refine=$refineEnabled, hair=$refineHair, radius=$refineRadius, smooth=$refineSmooth, contrast=$refineMaskContrast, halo=$refineLumaHalo, edgeContrast=$refineEdgeContrast)");
    json_out(['ok' => true, 'status' => 'queued']);
}


function result_info_for_job(array $job): array {
    $info = [
        'mp4' => false,
        'mov' => false,
        'zip' => false,
        'png_zip' => false,
        'files' => [],
    ];
    $token = (string)($job['token'] ?? '');
    if ($token === '') return $info;
    $resDir = RESULTS . '/' . $token;
    foreach (glob($resDir . '/result*.*') ?: [] as $p) {
        if (!is_file($p)) continue;
        $base = basename($p);
        $ext = strtolower(pathinfo($p, PATHINFO_EXTENSION));
        $kind = $ext;
        if ($ext === 'mp4') { $info['mp4'] = true; $kind = 'mp4'; }
        elseif ($ext === 'mov') { $info['mov'] = true; $kind = 'mov'; }
        elseif ($ext === 'zip') {
            $info['zip'] = true;
            $low = strtolower($base);
            if (str_contains($low, 'png_sequence') || str_contains($low, 'alpha') || $low === 'result.zip') {
                $info['png_zip'] = true;
                $kind = 'png_zip';
            } else {
                $kind = 'zip';
            }
        }
        $info['files'][] = [
            'name' => $base,
            'kind' => $kind,
            'size' => @filesize($p) ?: 0,
        ];
    }
    return $info;
}

function get_result(): void {
    $token = $_GET['token'] ?? '';
    $job = find_job_by_token($token);
    if (!$job) json_err('Job not found', 404);
    if ($job['status'] !== 'done') json_err('Job is not finished yet', 409);

    $resDir = RESULTS . '/' . $job['token'];
    $matches = glob($resDir . '/result*.*') ?: [];
    if (!$matches) json_err('Result not found', 404);

    // f=mp4|h264|mov|zip|png|png_sequence dovolí mít najednou MP4 i PNG ZIP.
    // Když uživatel klikne konkrétní tlačítko, nevracej tichý fallback na jiný typ souboru.
    $want = strtolower((string)($_GET['f'] ?? $_GET['kind'] ?? ''));
    $filtered = null;
    if (in_array($want, ['png','zip','png_zip','png_sequence','sequence'], true)) {
        $filtered = array_values(array_filter($matches, fn($p) => preg_match('/png_sequence|alpha|result\.zip$/i', basename($p)) && strtolower(pathinfo($p, PATHINFO_EXTENSION)) === 'zip'));
    } elseif (in_array($want, ['mp4','h264','h264_luma'], true)) {
        $filtered = array_values(array_filter($matches, fn($p) => strtolower(pathinfo($p, PATHINFO_EXTENSION)) === 'mp4'));
    } elseif ($want === 'mov' || $want === 'prores') {
        $filtered = array_values(array_filter($matches, fn($p) => strtolower(pathinfo($p, PATHINFO_EXTENSION)) === 'mov'));
    }
    if ($filtered !== null) {
        if (!$filtered) json_err('Requested result type is not available: ' . $want, 404);
        $matches = $filtered;
    }

    // Default: preferuj přímý media soubor (mp4/mov) před zipem.
    usort($matches, function ($a, $b) use ($want) {
        $rank = function ($p) use ($want) {
            $base = strtolower(basename($p));
            $e = strtolower(pathinfo($p, PATHINFO_EXTENSION));
            if (in_array($want, ['png','zip','png_zip','png_sequence','sequence'], true)) {
                if (str_contains($base, 'png_sequence')) return 0;
                if ($e === 'zip') return 1;
            }
            if ($e === 'mp4') return 0;
            if ($e === 'mov') return 1;
            if ($e === 'zip') return 2;
            return 3;
        };
        return $rank($a) <=> $rank($b);
    });
    $path = $matches[0];
    $ext  = strtolower(pathinfo($path, PATHINFO_EXTENSION));
    $types = ['mp4' => 'video/mp4', 'mov' => 'video/quicktime',
              'zip' => 'application/zip', 'png' => 'image/png'];
    $ctype = $types[$ext] ?? 'application/octet-stream';

    $safe = preg_replace('/[^A-Za-z0-9_.-]/', '_', $job['name']);
    $suffix = '';
    $base = strtolower(basename($path));
    if (strpos($base, 'png_sequence') !== false) $suffix = '_PNG_sequence';
    elseif ($ext === 'mp4') $suffix = '_H264_luma_AE';
    elseif ($ext === 'mov') $suffix = '_ProRes4444';
    header('Content-Type: ' . $ctype);
    header('Content-Disposition: attachment; filename="' . $safe . $suffix . '.' . $ext . '"');
    header('Content-Length: ' . filesize($path));
    header('Cache-Control: no-store');
    readfile($path);
    exit;
}

function delete_job_files(array $job): void {
    // Preview masky jsou uložené mimo job_dir, proto je smažeme zvlášť.
    $st = db()->prepare('SELECT mask_path FROM previews WHERE job_id=? AND mask_path IS NOT NULL');
    $st->execute([$job['id']]);
    foreach ($st->fetchAll() as $pv) {
        $rel = (string)($pv['mask_path'] ?? '');
        if ($rel !== '') @unlink(STORAGE . '/' . $rel);
    }

    if (!empty($job['source_path'])) {
        @unlink(UPLOADS . '/' . basename((string)$job['source_path']));
    }
    rrmdir(job_dir($job));
    rrmdir(RESULTS . '/' . $job['token']);
}

function delete_job_record(array $job): void {
    delete_job_files($job);
    db()->prepare('DELETE FROM jobs WHERE id=?')->execute([$job['id']]);
}

function delete_job(): void {
    $d = body_json();
    $token = $d['token'] ?? ($_POST['token'] ?? '');
    $job = find_job_by_token($token);
    if (!$job) json_err('Job not found', 404);

    delete_job_record($job);
    json_out(['ok' => true, 'deleted' => 1]);
}

function delete_jobs_bulk(?array $statuses = null): void {
    $pdo = db();
    if ($statuses === null) {
        $rows = $pdo->query('SELECT * FROM jobs ORDER BY id')->fetchAll();
    } else {
        $allowed = ['created','extracting','ready','queued','claimed','tracking','matting','rmbg','done','error'];
        $statuses = array_values(array_intersect($statuses, $allowed));
        if (!$statuses) json_err('Invalid delete filter');
        $ph = implode(',', array_fill(0, count($statuses), '?'));
        $st = $pdo->prepare("SELECT * FROM jobs WHERE status IN ($ph) ORDER BY id");
        $st->execute($statuses);
        $rows = $st->fetchAll();
    }

    $deleted = 0;
    foreach ($rows as $job) {
        delete_job_record($job);
        $deleted++;
    }
    
json_out(['ok' => true, 'deleted' => $deleted]);
}

function cancel_job(): void {
    $d = body_json();
    $token = (string)($d['token'] ?? $_POST['token'] ?? '');
    $job = find_job_by_token($token);
    if (!$job) json_err('Job not found', 404);

    $dir = job_dir($job);
    if (!is_dir($dir)) @mkdir($dir, 0777, true);
    @file_put_contents(stop_flag_path($job), (string)time());
    db()->prepare("UPDATE jobs SET stage_msg=?, updated_at=datetime('now') WHERE id=?")
       ->execute(['Stop request sent…', $job['id']]);
    job_log((int)$job['id'], 'Stop requested by user');
    json_out(['ok' => true]);
}

function tracking_preview(): void {
    $token = (string)($_GET['token'] ?? '');
    $job = find_job_by_token($token);
    if (!$job) json_err('Job not found', 404);

    $jp = tracking_preview_json_path($job);
    $ip = tracking_preview_image_path($job);
    if (!is_file($jp) || !is_file($ip)) {
        json_out(['ok' => true, 'exists' => false]);
    }
    $meta = json_decode((string)@file_get_contents($jp), true);
    if (!is_array($meta)) $meta = [];
    $stamp = (string)($meta['stamp'] ?? filemtime($ip) ?: time());
    json_out([
        'ok' => true,
        'exists' => true,
        'frame_index' => $meta['frame_index'] ?? null,
        'progress' => isset($meta['progress']) ? (float)$meta['progress'] : null,
        'stage' => $meta['stage'] ?? null,
        'message' => $meta['message'] ?? null,
        'updated_at' => $meta['updated_at'] ?? null,
        'stamp' => $stamp,
        'image_url' => 'api/index.php?action=tracking-preview-image&token=' . rawurlencode($job['token']),
    ]);
}

function tracking_preview_image(): void {
    $token = (string)($_GET['token'] ?? '');
    $job = find_job_by_token($token);
    if (!$job) json_err('Job not found', 404);
    $ip = tracking_preview_image_path($job);
    if (!is_file($ip)) json_err('Tracking preview not found', 404);
    header('Content-Type: image/jpeg');
    header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
    readfile($ip);
    exit;
}


function app_version(): string {
    $p = APP_ROOT . '/APP_VERSION.txt';
    if (is_file($p)) {
        $v = trim((string)@file_get_contents($p));
        if ($v !== '') return $v;
    }
    return 'v45-update-manager';
}

function app_status(): void {
    $updates = STORAGE . '/updates';
    $backups = STORAGE . '/backups/updates';
    if (!is_dir($updates)) @mkdir($updates, 0775, true);
    if (!is_dir($backups)) @mkdir($backups, 0775, true);
    $last = STORAGE . '/last_update.json';
    $lastData = null;
    if (is_file($last)) {
        $tmp = json_decode((string)@file_get_contents($last), true);
        if (is_array($tmp)) $lastData = $tmp;
    }
    json_out([
        'ok' => true,
        'version' => app_version(),
        'app_root' => APP_ROOT,
        'updates_dir' => $updates,
        'backups_dir' => $backups,
        'last_update' => $lastData,
        'worker_restart_flag' => is_file(STORAGE . '/.restart_worker'),
    ]);
}

function update_find_python(): ?string {
    $pfile = APP_ROOT . '/worker/python_path.txt';
    if (is_file($pfile)) {
        $p = trim((string)@file_get_contents($pfile));
        if ($p !== '' && is_file($p)) return $p;
    }
    $candidates = [
        APP_ROOT . '/runtime/miniconda/envs/maskstudio/python.exe',
        APP_ROOT . '/runtime/miniconda/envs/maskstudio/python',
        APP_ROOT . '/runtime/python/python.exe',
        'python',
        'python3',
    ];
    foreach ($candidates as $p) {
        if ($p === 'python' || $p === 'python3' || is_file($p)) return $p;
    }
    return null;
}


function start_worker_background(): array {
    // v64: prefer full START.bat first. Starting run_worker.bat alone can leave
    // the worker unable to reach localhost:8080 if the frontend/server is down.
    // V70: servisní BAT skripty jsou v nastroje\; staré cesty necháme jako
    // fallback pro instalace aktualizované přes update ZIP.
    $candidates = [
        APP_ROOT . '/START.bat',
        APP_ROOT . '/nastroje/run_worker.bat',
        APP_ROOT . '/START_PZ_MASK.bat',
        APP_ROOT . '/run_worker.bat',
    ];
    $launched = false;
    $used = null;
    $messages = [];
    foreach ($candidates as $bat) {
        if (!is_file($bat)) continue;
        $used = basename($bat);
        try {
            if (PHP_OS_FAMILY === 'Windows') {
                $cmd = 'cmd /c start "" ' . escapeshellarg($bat);
                @pclose(@popen($cmd, 'r'));
                $launched = true;
                $messages[] = 'launch sent via start: ' . $used;
                break;
            } else {
                $cmd = escapeshellcmd($bat) . ' >/dev/null 2>&1 &';
                @exec($cmd);
                $launched = true;
                $messages[] = 'launch sent: ' . $used;
                break;
            }
        } catch (Throwable $e) {
            $messages[] = 'launch error for ' . $used . ': ' . $e->getMessage();
        }
    }
    if (!$launched && !$used) $messages[] = 'no worker launcher found';
    return ['launched' => $launched, 'launcher' => $used, 'messages' => $messages];
}

function update_install(): void {
    if (!isset($_FILES['update_zip'])) json_err('Update ZIP missing');
    $f = $_FILES['update_zip'];
    if (($f['error'] ?? UPLOAD_ERR_NO_FILE) !== UPLOAD_ERR_OK) {
        json_err('Update ZIP upload error (code ' . (int)$f['error'] . ')');
    }
    $ext = strtolower(pathinfo((string)$f['name'], PATHINFO_EXTENSION));
    if ($ext !== 'zip') json_err('Update must be a ZIP');
    if ((int)$f['size'] <= 0) json_err('Update ZIP is empty');
    if ((int)$f['size'] > 512 * 1024 * 1024) json_err('Update ZIP is too large');

    $updates = STORAGE . '/updates';
    $backups = STORAGE . '/backups/updates';
    if (!is_dir($updates)) @mkdir($updates, 0775, true);
    if (!is_dir($backups)) @mkdir($backups, 0775, true);

    $stamp = date('Ymd_His');
    $zipPath = $updates . '/incoming_' . $stamp . '_' . preg_replace('/[^A-Za-z0-9._-]+/', '_', basename((string)$f['name']));
    if (!move_uploaded_file($f['tmp_name'], $zipPath)) json_err('Cannot save update ZIP');

    $py = update_find_python();
    if (!$py) json_err('Python runtime for the update installer not found');
    $script = APP_ROOT . '/tools/apply_update.py';
    if (!is_file($script)) json_err('tools/apply_update.py is missing');

    $cmd = escapeshellarg($py) . ' ' . escapeshellarg($script)
         . ' --zip ' . escapeshellarg($zipPath)
         . ' --app-root ' . escapeshellarg(APP_ROOT)
         . ' --backup-dir ' . escapeshellarg($backups);

    $descriptors = [
        0 => ['pipe', 'r'],
        1 => ['pipe', 'w'],
        2 => ['pipe', 'w'],
    ];
    $proc = @proc_open($cmd, $descriptors, $pipes, APP_ROOT);
    if (!is_resource($proc)) json_err('Cannot start the update installer');
    fclose($pipes[0]);
    $stdout = stream_get_contents($pipes[1]); fclose($pipes[1]);
    $stderr = stream_get_contents($pipes[2]); fclose($pipes[2]);
    $code = proc_close($proc);

    $result = json_decode((string)$stdout, true);
    if ($code !== 0 || !is_array($result) || empty($result['ok'])) {
        json_err('Update failed: ' . trim(($stderr ?: '') . "\n" . ($stdout ?: '')), 500);
    }

    @file_put_contents(STORAGE . '/.restart_worker', (string)time());
    $result['worker_restart_requested'] = true;

    // V49: after update always try to start the worker automatically.
    // If worker is already running, worker/run.py will notice .restart_worker and execv itself.
    // If worker is not running, we try to launch run_worker.bat / START.bat here.
    $workerLaunch = start_worker_background();
    $result['worker_auto_start'] = $workerLaunch;

    $result['uploaded_zip'] = basename($zipPath);
    $result['installed_at'] = date('c');
    @file_put_contents(STORAGE . '/last_update.json', json_encode($result, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT));
    json_out($result);
}

function worker_status_path(): string {
    return STORAGE . '/worker_status.json';
}

function local_status_cache_path(): string {
    return STORAGE . '/local_system_status_cache.json';
}

function local_status_lock_path(): string {
    return STORAGE . '/local_system_status_cache.lock';
}

function _num_from_text($v) {
    if (preg_match('/-?\d+(?:\.\d+)?/', (string)$v, $m)) return (float)$m[0];
    return null;
}

function _read_json_file_assoc(string $path): ?array {
    if (!is_file($path)) return null;
    $raw = @file_get_contents($path);
    $d = json_decode((string)$raw, true);
    return is_array($d) ? $d : null;
}


function _cmd_quote(string $cmd): string {
    $cmd = trim($cmd);
    if ($cmd === '') return $cmd;
    // Windows i Linux bezpečně zvládnou u cest s mezerami obyčejné uvozovky.
    if (preg_match('/\s/', $cmd) && $cmd[0] !== '"') {
        return '"' . str_replace('"', '\\"', $cmd) . '"';
    }
    return $cmd;
}

function _path_join_win(?string $base, string $suffix): ?string {
    if (!$base) return null;
    return rtrim($base, "\\/") . '\\' . ltrim($suffix, "\\/");
}

function _nvidia_smi_candidates(): array {
    $candidates = ['nvidia-smi'];
    $pf  = getenv('ProgramFiles') ?: 'C:\\Program Files';
    $pf86 = getenv('ProgramFiles(x86)') ?: 'C:\\Program Files (x86)';
    $sys = getenv('SystemRoot') ?: 'C:\\Windows';
    foreach ([
        _path_join_win($pf,  'NVIDIA Corporation\\NVSMI\\nvidia-smi.exe'),
        _path_join_win($pf86, 'NVIDIA Corporation\\NVSMI\\nvidia-smi.exe'),
        _path_join_win($sys, 'System32\\nvidia-smi.exe'),
    ] as $p) {
        if ($p && !in_array($p, $candidates, true)) $candidates[] = $p;
    }
    return $candidates;
}

function _powershell_candidates(): array {
    $sys = getenv('SystemRoot') ?: 'C:\\Windows';
    $candidates = [
        'powershell',
        'powershell.exe',
        'pwsh',
        'pwsh.exe',
        _path_join_win($sys, 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'),
    ];
    $out = [];
    foreach ($candidates as $c) {
        if ($c && !in_array($c, $out, true)) $out[] = $c;
    }
    return $out;
}

function _shell_out(string $cmd): string {
    $out = @shell_exec($cmd);
    return is_string($out) ? $out : '';
}

function _first_csv_data_line(string $out): string {
    foreach (preg_split('/\r?\n/', trim($out)) as $line) {
        $s = trim($line);
        if ($s === '') continue;
        $low = strtolower($s);
        if (strpos($low, 'name') !== false && (strpos($low, 'memory') !== false || strpos($low, 'utilization') !== false)) continue;
        if (strpos($low, 'error') !== false || strpos($low, 'not recognized') !== false || strpos($low, 'nen') !== false) continue;
        return $s;
    }
    return '';
}

function _run_powershell_json(string $script): ?array {
    // Spolehlivější než dlouhé -Command escapování: uložíme krátký PS1 do storage.
    $tmp = STORAGE . '/_hw_status_' . getmypid() . '_' . mt_rand(1000,9999) . '.ps1';
    @file_put_contents($tmp, $script);
    foreach (_powershell_candidates() as $ps) {
        $cmd = _cmd_quote($ps) . ' -NoProfile -ExecutionPolicy Bypass -File ' . escapeshellarg($tmp) . ' 2>&1';
        $out = _shell_out($cmd);
        $json = trim($out);
        $pos = strpos($json, '{');
        if ($pos !== false) $json = substr($json, $pos);
        $d = json_decode($json, true);
        if (is_array($d)) { @unlink($tmp); return $d; }
    }
    @unlink($tmp);
    return null;
}

function _collect_local_system_stats_slow(): array {
    // V67 FAST HW: sběr HW údajů je robustnější.
    // Worker heartbeat je nejlepší zdroj, ale když worker ještě neběží,
    // API se samo pokusí přečíst NVIDIA/CPU/RAM přes nvidia-smi, WMIC a PowerShell.
    $d = [
        'stats_source' => 'api-local-cache',
        'worker_id' => 'api-local',
        'device' => 'local',
        'gpu_name' => null, 'gpu_util' => null,
        'vram_used_mb' => null, 'vram_total_mb' => null,
        'cpu_percent' => null,
        'ram_used_mb' => null, 'ram_total_mb' => null, 'ram_percent' => null,
        'matanyone_status' => 'unknown',
        'matanyone_backend' => '—',
        'matanyone_msg' => 'local hardware stats',
        'status_msg' => '',
    ];
    $notes = [];

    // GPU / VRAM přes nvidia-smi. Zkoušíme PATH i typickou NVIDIA cestu,
    // protože PHP/Apache často nemá nvidia-smi v PATH.
    $gotGpu = false;
    foreach (_nvidia_smi_candidates() as $smi) {
        foreach (['--format=csv,noheader,nounits', '--format=csv,nounits', '--format=csv'] as $fmt) {
            $cmd = _cmd_quote($smi) . ' --query-gpu=name,utilization.gpu,memory.used,memory.total ' . $fmt . ' 2>&1';
            $line = _first_csv_data_line(_shell_out($cmd));
            if ($line !== '') {
                $parts = array_map('trim', explode(',', $line));
                if (count($parts) >= 4) {
                    $d['gpu_name'] = $parts[0] ?: null;
                    $d['gpu_util'] = _num_from_text($parts[1]);
                    $d['vram_used_mb'] = _num_from_text($parts[2]);
                    $d['vram_total_mb'] = _num_from_text($parts[3]);
                    $gotGpu = !empty($d['gpu_name']);
                    break 2;
                }
            }
        }
    }

    if (!$gotGpu) {
        foreach (_nvidia_smi_candidates() as $smi) {
            $out = _shell_out(_cmd_quote($smi) . ' -L 2>&1');
            if (preg_match('/GPU\s+\d+:\s+([^\(\r\n]+)/', $out, $m)) {
                $d['gpu_name'] = trim($m[1]);
                $gotGpu = true;
                break;
            }
        }
    }
    if (!$gotGpu) $notes[] = 'nvidia-smi unavailable to PHP/API';

    // CPU percent přes WMIC, když existuje.
    $out = _shell_out('wmic cpu get loadpercentage /value 2>NUL');
    if (preg_match('/LoadPercentage\s*=\s*([0-9.]+)/i', $out, $m)) {
        $d['cpu_percent'] = (float)$m[1];
    }

    // RAM přes WMIC.
    $out = _shell_out('wmic OS get FreePhysicalMemory,TotalVisibleMemorySize /Value 2>NUL');
    if (is_string($out) && trim($out) !== '') {
        $freeKb = null; $totalKb = null;
        if (preg_match('/FreePhysicalMemory\s*=\s*([0-9.]+)/i', $out, $m)) $freeKb = (float)$m[1];
        if (preg_match('/TotalVisibleMemorySize\s*=\s*([0-9.]+)/i', $out, $m)) $totalKb = (float)$m[1];
        if ($totalKb && $freeKb !== null) {
            $usedKb = max(0.0, $totalKb - $freeKb);
            $d['ram_total_mb'] = round($totalKb / 1024.0, 1);
            $d['ram_used_mb'] = round($usedKb / 1024.0, 1);
            $d['ram_percent'] = round(($usedKb / max(1.0, $totalKb)) * 100.0, 1);
        }
    }

    // Windows 11 často WMIC nemá. Fallback přes PowerShell/CIM.
    if ($d['cpu_percent'] === null || $d['ram_percent'] === null) {
        $ps = <<<'PS'
$ErrorActionPreference = 'SilentlyContinue'
$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
$os  = Get-CimInstance Win32_OperatingSystem
$total = [double]$os.TotalVisibleMemorySize
$free  = [double]$os.FreePhysicalMemory
$used  = [Math]::Max(0, $total - $free)
[pscustomobject]@{
  cpu_percent = [Math]::Round([double]$cpu, 1)
  ram_used_mb = [Math]::Round($used / 1024, 1)
  ram_total_mb = [Math]::Round($total / 1024, 1)
  ram_percent = [Math]::Round(($used / [Math]::Max(1, $total)) * 100, 1)
} | ConvertTo-Json -Compress
PS;
        $pd = _run_powershell_json($ps);
        if (is_array($pd)) {
            foreach (['cpu_percent','ram_used_mb','ram_total_mb','ram_percent'] as $k) {
                if (($d[$k] ?? null) === null && isset($pd[$k])) $d[$k] = (float)$pd[$k];
            }
        }
    }
    if ($d['cpu_percent'] === null || $d['ram_percent'] === null) $notes[] = 'CPU/RAM unavailable to PHP/API';

    $d['local_ts'] = microtime(true);
    $d['status_msg'] = $notes ? implode('; ', $notes) : 'hardware stats OK';
    if ($notes) $d['matanyone_msg'] = $d['status_msg'];
    return $d;
}

function local_system_stats_cached(float $ttlSec = 4.0): array {
    $cachePath = local_status_cache_path();
    $cached = _read_json_file_assoc($cachePath);
    $now = microtime(true);
    $cts = isset($cached['local_ts']) ? (float)$cached['local_ts'] : 0.0;
    if (is_array($cached) && $cts > 0 && ($now - $cts) < $ttlSec) {
        $cached['stats_source'] = 'api-local-cache';
        return $cached;
    }

    // Non-blocking lock: if another request is already collecting slow stats,
    // return cached values immediately instead of freezing editor/API.
    $lock = @fopen(local_status_lock_path(), 'c');
    if ($lock && !@flock($lock, LOCK_EX | LOCK_NB)) {
        if (is_array($cached)) {
            $cached['stats_source'] = 'api-local-cache-stale';
            return $cached;
        }
        return [
            'stats_source' => 'api-local-cache-wait',
            'gpu_name' => null, 'gpu_util' => null,
            'vram_used_mb' => null, 'vram_total_mb' => null,
            'cpu_percent' => null, 'ram_percent' => null,
            'matanyone_status' => 'unknown', 'matanyone_backend' => '—',
        ];
    }

    // Another request may have refreshed while we were opening the lock.
    $cached2 = _read_json_file_assoc($cachePath);
    $cts2 = isset($cached2['local_ts']) ? (float)$cached2['local_ts'] : 0.0;
    if (is_array($cached2) && $cts2 > 0 && ($now - $cts2) < $ttlSec) {
        if ($lock) { @flock($lock, LOCK_UN); @fclose($lock); }
        $cached2['stats_source'] = 'api-local-cache';
        return $cached2;
    }

    $d = _collect_local_system_stats_slow();
    @file_put_contents($cachePath, json_encode($d, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
    if ($lock) { @flock($lock, LOCK_UN); @fclose($lock); }
    return $d;
}

function merge_worker_and_local_stats(array $worker, array $local, bool $workerFresh): array {
    $out = $workerFresh ? $worker : array_merge($worker, ['fresh' => false]);
    foreach ($local as $k => $v) {
        if (($out[$k] ?? null) === null || ($out[$k] ?? '') === '—' || ($out[$k] ?? '') === '') {
            if ($v !== null && $v !== '') $out[$k] = $v;
        }
    }
    if (empty($out['stats_source'])) $out['stats_source'] = $workerFresh ? 'worker' : 'api-local-cache+stale-worker';
    return $out;
}

// CLIENT: živý stav lokálního stroje pro horní lištu editoru.
function system_status(): void {
    $path = worker_status_path();
    $worker = _read_json_file_assoc($path);
    $workerFresh = false;
    $age = null;

    if (is_array($worker)) {
        $ts = isset($worker['ts']) ? (float)$worker['ts'] : 0.0;
        $age = $ts > 0 ? max(0.0, microtime(true) - $ts) : null;
        $workerFresh = ($ts > 0 && $age !== null && $age < 12.0);
    }

    // If worker heartbeat is fresh, use it directly and do not run slow local commands.
    if ($workerFresh) {
        $worker['ok'] = true;
        $worker['fresh'] = true;
        $worker['worker_fresh'] = true;
        $worker['age_sec'] = $age;
        if (empty($worker['stats_source'])) $worker['stats_source'] = 'worker';
        json_out($worker);
    }

    // Worker is missing/stale -> use cached local stats, refreshed at most once per 4s.
    $local = local_system_stats_cached(4.0);
    if (is_array($worker)) {
        $d = merge_worker_and_local_stats($worker, $local, false);
    } else {
        $d = $local;
    }
    $d['ok'] = true;
    $d['fresh'] = true; // fresh local cache response from API perspective
    $d['worker_fresh'] = false;
    $d['age_sec'] = $age;
    if (empty($d['status_msg'])) $d['status_msg'] = is_array($worker) ? 'local cached stats; worker stale' : 'local cached stats; worker missing';
    json_out($d);
}

// WORKER: periodicky zapisuje GPU/VRAM/CPU/RAM. Nejde do DB, jen malý JSON.
function worker_status(): void {
    $d = body_json();
    $allowed = [
        'worker_id','device','job_id','stage','stage_msg',
        'matanyone_status','matanyone_backend','matanyone_msg',
        'gpu_name','gpu_util','vram_used_mb','vram_total_mb',
        'cpu_percent','ram_used_mb','ram_total_mb','ram_percent'
    ];
    $out = ['ts' => microtime(true), 'stats_source' => 'worker'];
    foreach ($allowed as $k) {
        if (array_key_exists($k, $d)) $out[$k] = $d[$k];
    }
    @file_put_contents(worker_status_path(), json_encode($out, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
    json_out(['ok' => true]);
}

// ============================================================================
//  WORKER endpointy
// ============================================================================

function worker_claim(): void {
    $d = body_json();
    $workerId = substr((string)($d['worker_id'] ?? 'worker'), 0, 64);
    $pdo = db();

    // DŮLEŽITÉ PRO RYCHLÝ EDITOR:
    // Worker už po uploadu nebere automaticky stav 'created', protože dlouhá
    // extrakce celého videa blokovala první interaktivní SAM masku. Created job
    // zůstane volný pro preview queue; celé video se rozbalí až po RUN (queued).
    $pdo->beginTransaction();
    try {
        $row = $pdo->query(
            "SELECT * FROM jobs WHERE status='queued'
             ORDER BY id ASC LIMIT 1"
        )->fetch();
        if (!$row) { $pdo->commit(); json_out(['ok' => true, 'job' => null]); }

        $pdo->prepare(
            "UPDATE jobs SET status='claimed', worker_id=?, stage_msg=?, updated_at=datetime('now')
             WHERE id=? AND status='queued'"
        )->execute([$workerId, 'Worker claimed the job', $row['id']]);
        $pdo->commit();
    } catch (Throwable $e) {
        $pdo->rollBack();
        json_err('Claim failed: ' . $e->getMessage(), 500);
    }

    // znovu načti aktuální stav + masky
    $st = $pdo->prepare('SELECT * FROM jobs WHERE id=?');
    $st->execute([$row['id']]);
    $job = $st->fetch();
    job_log($job['id'], "Worker '$workerId' claimed the job (status={$job['status']})");
    json_out(['ok' => true, 'job' => worker_job_payload($job)]);
}

function worker_job(): void {
    $id = (int)($_GET['id'] ?? 0);
    $st = db()->prepare('SELECT * FROM jobs WHERE id=?');
    $st->execute([$id]);
    $job = $st->fetch();
    if (!$job) json_err('Job not found', 404);
    json_out(['ok' => true, 'job' => worker_job_payload($job)]);
}

function worker_job_payload(array $job): array {
    $masks = db()->prepare('SELECT * FROM masks WHERE job_id=? ORDER BY ord, id');
    $masks->execute([$job['id']]);
    $masks = $masks->fetchAll();
    foreach ($masks as &$m) {
        $p = db()->prepare('SELECT kind,x,y,val,brush_path FROM prompts WHERE mask_id=? ORDER BY id');
        $p->execute([$m['id']]);
        $m['prompts'] = $p->fetchAll();
    }
    unset($m);

    return [
        'id'            => (int)$job['id'],
        'token'         => $job['token'],
        'name'          => $job['name'],
        'status'        => $job['status'],
        'source_type'   => $job['source_type'],
        'source_path'   => $job['source_path'],     // rel. k storage/uploads
        'frame_count'   => (int)$job['frame_count'],
        'width'         => (int)$job['width'],
        'height'        => (int)$job['height'],
        'fps'           => (float)$job['fps'],
        'engine'        => $job['engine'] ?? 'sam2',
        'sam_model'     => $job['sam_model'],
        'multi_mode'    => $job['multi_mode'],
        'matte_enabled' => (int)$job['matte_enabled'],
        'output_format' => $job['output_format'],
        'matte_edge_feather' => isset($job['matte_edge_feather']) ? (float)$job['matte_edge_feather'] : 0.75,
        'matte_edge_shrink'  => isset($job['matte_edge_shrink']) ? (int)$job['matte_edge_shrink'] : -1,
        'matte_edge_choke'   => isset($job['matte_edge_choke']) ? (int)$job['matte_edge_choke'] : 0,
        'matte_edge_cleanup' => isset($job['matte_edge_cleanup']) ? (int)$job['matte_edge_cleanup'] : 18,
        'refine_enabled' => isset($job['refine_enabled']) ? (int)$job['refine_enabled'] : 0,
        'refine_hair_detail' => isset($job['refine_hair_detail']) ? (int)$job['refine_hair_detail'] : 78,
        'refine_edge_radius' => isset($job['refine_edge_radius']) ? (int)$job['refine_edge_radius'] : 12,
        'refine_face_detail' => isset($job['refine_face_detail']) ? (int)$job['refine_face_detail'] : 68,
        'refine_hand_detail' => isset($job['refine_hand_detail']) ? (int)$job['refine_hand_detail'] : 58,
        'refine_silhouette_smooth' => isset($job['refine_color_decontaminate']) ? (int)$job['refine_color_decontaminate'] : 24,
        'refine_color_decontaminate' => 0,
        'refine_smart_feather' => isset($job['refine_smart_feather']) ? (float)$job['refine_smart_feather'] : 0.5,
        'refine_smart_choke' => isset($job['refine_smart_choke']) ? (int)$job['refine_smart_choke'] : 0,
        'refine_mode' => $job['refine_mode'] ?? 'hq',
        'refine_auto_hair' => isset($job['refine_auto_hair']) ? (int)$job['refine_auto_hair'] : 1,
        'refine_auto_face' => isset($job['refine_auto_face']) ? (int)$job['refine_auto_face'] : 1,
        'refine_mask_contrast' => isset($job['refine_mask_contrast']) ? (int)$job['refine_mask_contrast'] : 18,
        'refine_luma_halo' => isset($job['refine_luma_halo']) ? (int)$job['refine_luma_halo'] : 28,
        'refine_edge_contrast' => isset($job['refine_edge_contrast']) ? (int)$job['refine_edge_contrast'] : 16,
        'rmbg_model_id' => $job['rmbg_model_id'] ?? 'briaai/RMBG-1.4',
        'rmbg_invert'   => (int)($job['rmbg_invert'] ?? 0),
        'rmbg_blur_radius' => (float)($job['rmbg_blur_radius'] ?? 0),
        'rmbg_gamma'    => (float)($job['rmbg_gamma'] ?? 1),
        'rmbg_force_cpu'=> (int)($job['rmbg_force_cpu'] ?? 0),
        'rmbg_crf'      => (int)($job['rmbg_crf'] ?? 12),
        'masks'         => $masks,
    ];
}

function worker_progress(): void {
    $d = body_json();
    $id = (int)($d['id'] ?? 0);
    $st = db()->prepare('SELECT id FROM jobs WHERE id=?');
    $st->execute([$id]);
    if (!$st->fetch()) json_err('Job not found', 404);

    $status   = $d['status']   ?? null;   // tracking|matting|extracting|ready
    $progress = isset($d['progress']) ? max(0.0, min(1.0, (float)$d['progress'])) : null;
    $stage    = $d['stage_msg'] ?? null;

    // dynamický update jen poslaných polí
    $sets = ["updated_at=datetime('now')"]; $args = [];
    if ($status   !== null) { $sets[] = 'status=?';    $args[] = $status; }
    if ($progress !== null) { $sets[] = 'progress=?';  $args[] = $progress; }
    if ($stage    !== null) { $sets[] = 'stage_msg=?'; $args[] = $stage; }
    $args[] = $id;
    db()->prepare('UPDATE jobs SET ' . implode(',', $sets) . ' WHERE id=?')->execute($args);

    // volitelně metadata po extrakci
    if (isset($d['frame_count']) || isset($d['width'])) {
        db()->prepare(
            'UPDATE jobs SET frame_count=COALESCE(?,frame_count), width=COALESCE(?,width),
                    height=COALESCE(?,height), fps=COALESCE(?,fps) WHERE id=?'
        )->execute([
            $d['frame_count'] ?? null, $d['width'] ?? null,
            $d['height'] ?? null, $d['fps'] ?? null, $id,
        ]);
    }
    json_out(['ok' => true]);
}

function worker_result_meta(): void {
    $d = body_json();
    $id = (int)($d['id'] ?? 0);
    $st = db()->prepare('SELECT id FROM jobs WHERE id=?');
    $st->execute([$id]);
    if (!$st->fetch()) json_err('Job not found', 404);

    db()->prepare(
        "UPDATE jobs SET status='done', progress=1.0, stage_msg=?, updated_at=datetime('now')
         WHERE id=?"
    )->execute([$d['stage_msg'] ?? 'Done', $id]);
    job_log($id, 'Worker finished processing, results uploaded');
    json_out(['ok' => true]);
}

function worker_upload_frames(): void {
    $id = (int)($_GET['id'] ?? 0);
    $st = db()->prepare('SELECT token FROM jobs WHERE id=?');
    $st->execute([$id]);
    $job = $st->fetch();
    if (!$job) json_err('Job not found', 404);
    if (!isset($_FILES['frames']) || $_FILES['frames']['error'] !== UPLOAD_ERR_OK) {
        json_err('Frames ZIP missing');
    }
    $framesDir = JOBS_DIR . '/' . $job['token'] . '/frames';
    if (!is_dir($framesDir)) @mkdir($framesDir, 0775, true);

    // rozbal ZIP s náhledovými JPG (000000.jpg, 000001.jpg, …)
    $zip = new ZipArchive();
    $tmp = $_FILES['frames']['tmp_name'];
    if ($zip->open($tmp) !== true) json_err('Cannot open ZIP', 500);
    for ($i = 0; $i < $zip->numFiles; $i++) {
        $name = $zip->getNameIndex($i);
        if (!preg_match('/^\d{6}\.jpg$/', basename($name))) continue;  // jen očekávané framy
        $contents = $zip->getFromIndex($i);
        if ($contents !== false) file_put_contents($framesDir . '/' . basename($name), $contents);
    }
    $zip->close();

    $count = count(glob($framesDir . '/*.jpg'));
    job_log($id, "Uploaded $count preview frames");
    json_out(['ok' => true, 'frames' => $count]);
}

// Optional global default output folder, read from worker/config.json.
function _default_output_dir(): ?string {
    static $cached = false; static $val = null;
    if ($cached) return $val;
    $cached = true;
    $cfgPath = APP_ROOT . '/worker/config.json';
    if (is_file($cfgPath)) {
        $j = json_decode((string)file_get_contents($cfgPath), true);
        if (is_array($j) && !empty($j['output_dir']) && is_string($j['output_dir'])) {
            $val = $j['output_dir'];
        }
    }
    return $val;
}

function _safe_name(string $s): string {
    $s = preg_replace('/[^A-Za-z0-9._-]+/', '_', $s);
    $s = trim($s, '_.');
    if ($s === '') $s = 'mask';
    return substr($s, 0, 80);
}

// Copy a finished result file to the user-chosen output folder (or the config
// default), named after the job so multiple jobs do not overwrite each other.
function _copy_result_to_output(array $job, string $srcFile, string $destName): void {
    $out = trim((string)($job['custom_output_path'] ?? ''));
    if ($out === '') $out = (string)(_default_output_dir() ?? '');
    if ($out === '') return;
    if (!is_dir($out)) @mkdir($out, 0775, true);
    if (!is_dir($out) || !is_writable($out)) return;
    $base = _safe_name((string)($job['name'] ?? 'mask'));
    if ($destName === 'result.mp4')                 $suffix = $base . '_MASK.mp4';
    elseif ($destName === 'result.mov')             $suffix = $base . '_MASK.mov';
    elseif ($destName === 'result_png_sequence.zip') $suffix = $base . '_PNG_SEQUENCE.zip';
    elseif ($destName === 'result.zip')             $suffix = $base . '_MASK.zip';
    else                                            $suffix = $base . '_' . $destName;
    @copy($srcFile, rtrim($out, "/\\") . DIRECTORY_SEPARATOR . $suffix);
}

function worker_upload_result(): void {
    $id = (int)($_GET['id'] ?? 0);
    $st = db()->prepare('SELECT token, name, custom_output_path FROM jobs WHERE id=?');
    $st->execute([$id]);
    $job = $st->fetch();
    if (!$job) json_err('Job not found', 404);

    // 'file' = raw result (mp4/mov), 'zip' = PNG sequence / legacy ZIP.
    $field = null;
    if (isset($_FILES['file']) && $_FILES['file']['error'] === UPLOAD_ERR_OK) $field = 'file';
    elseif (isset($_FILES['zip']) && $_FILES['zip']['error'] === UPLOAD_ERR_OK) $field = 'zip';
    $f = $field ? $_FILES[$field] : null;
    if (!$f) json_err('Result file missing');

    $dir = RESULTS . '/' . $job['token'];
    if (!is_dir($dir)) @mkdir($dir, 0775, true);

    $orig = (string)($f['name'] ?? 'result.zip');
    $ext  = strtolower(pathinfo($orig, PATHINFO_EXTENSION) ?: 'zip');
    if (!preg_match('/^[a-z0-9]{1,5}$/', $ext)) $ext = 'bin';

    $lowOrig = strtolower($orig);
    if ($ext === 'zip' && ($field === 'zip' || strpos($lowOrig, 'png') !== false || strpos($lowOrig, 'sequence') !== false || strpos($lowOrig, 'alpha') !== false)) {
        $destName = 'result_png_sequence.zip';
    } elseif ($ext === 'mp4') {
        $destName = 'result.mp4';
    } elseif ($ext === 'mov') {
        $destName = 'result.mov';
    } elseif ($ext === 'zip') {
        $destName = 'result.zip';
    } else {
        $destName = 'result.' . $ext;
    }
    $dest = $dir . '/' . $destName;
    @unlink($dest); // don't overwrite other result types; MP4 and PNG ZIP can coexist

    if (!move_uploaded_file($f['tmp_name'], $dest)) {
        json_err('Cannot save result file', 500);
    }
    job_log($id, 'Result uploaded: ' . $destName . ' (' . round(filesize($dest) / 1048576, 1) . ' MB)');
    // Also copy to the user-chosen output folder (if any) for this job.
    try { _copy_result_to_output($job, $dest, $destName); } catch (Throwable $e) { /* non-fatal */ }
    json_out(['ok' => true, 'size' => filesize($dest), 'ext' => $ext, 'file' => $destName]);
}

function worker_fail(): void {
    $d = body_json();
    $id  = (int)($d['id'] ?? 0);
    $msg = substr((string)($d['error'] ?? 'unknown error'), 0, 500);
    db()->prepare(
        "UPDATE jobs SET status='error', error_msg=?, stage_msg=?, updated_at=datetime('now')
         WHERE id=?"
    )->execute([$msg, 'Error: ' . $msg, $id]);
    job_log($id, 'Worker reports error: ' . $msg, 'error');
    json_out(['ok' => true]);
}

// ---- util -------------------------------------------------------------------
function rrmdir(string $dir): void {
    if (!is_dir($dir)) return;
    $items = scandir($dir);
    foreach ($items as $it) {
        if ($it === '.' || $it === '..') continue;
        $p = $dir . '/' . $it;
        is_dir($p) ? rrmdir($p) : @unlink($p);
    }
    @rmdir($dir);
}

// ============================================================================
//  NÁHLEDY MASKY — interaktivní single-frame SAM
// ============================================================================

// KLIENT: vytvoří náhledový požadavek z kliknutých bodů. Vrátí preview id.
function preview_request(): void {
    $d = body_json();
    $token = $d['token'] ?? '';
    $job = find_job_by_token($token);
    if (!$job) json_err('Job not found', 404);

    $frameIndex = max(0, (int)($d['frame_index'] ?? 0));
    $samModel   = $d['sam_model'] ?? $job['sam_model'];

    // body: [{kind:point,x,y,val}, {kind:box,x0,y0,x1,y1}] — validuj a ořízni
    $points = [];
    foreach (($d['points'] ?? []) as $p) {
        $kind = (string)($p['kind'] ?? 'point');
        if ($kind === 'box') {
            $points[] = [
                'kind' => 'box',
                'x0' => max(0.0, min(1.0, (float)($p['x0'] ?? 0))),
                'y0' => max(0.0, min(1.0, (float)($p['y0'] ?? 0))),
                'x1' => max(0.0, min(1.0, (float)($p['x1'] ?? 0))),
                'y1' => max(0.0, min(1.0, (float)($p['y1'] ?? 0))),
            ];
        } else {
            $points[] = [
                'kind' => 'point',
                'x'   => max(0.0, min(1.0, (float)($p['x'] ?? 0))),
                'y'   => max(0.0, min(1.0, (float)($p['y'] ?? 0))),
                'val' => ((int)($p['val'] ?? 1)) === 0 ? 0 : 1,
            ];
        }
    }
    if (!$points) json_err('Preview requires at least one point or rectangle');

    // Zruš starší pending náhledy stejného jobu (chceme jen nejnovější).
    // Warmup požadavky necháme doběhnout, protože mezitím nahřívají model a embedding.
    db()->prepare("UPDATE previews SET status='error', error_msg='replaced by a newer preview'
                   WHERE job_id=? AND status IN ('pending','claimed')
                     AND points_json NOT LIKE '%\"warmup\"%'")
        ->execute([$job['id']]);

    $st = db()->prepare(
        'INSERT INTO previews(job_id,frame_index,sam_model,points_json,status)
         VALUES(?,?,?,?,?)'
    );
    $st->execute([$job['id'], $frameIndex, $samModel,
                  json_encode($points, JSON_UNESCAPED_SLASHES), 'pending']);
    $pid = (int) db()->lastInsertId();
    json_out(['ok' => true, 'preview_id' => $pid]);
}

// KLIENT: tichý warmup SAM image predictoru pro první full-quality frame.
// Nevrací masku; worker jen načte model a spočítá embedding prvního snímku.
function preview_warmup(): void {
    $d = body_json();
    $token = $d['token'] ?? '';
    $job = find_job_by_token($token);
    if (!$job) json_err('Job not found', 404);
    if (($job['source_type'] ?? '') !== 'video') json_out(['ok' => true, 'skipped' => true]);

    $samModel = $d['sam_model'] ?? $job['sam_model'];

    // Nehromaď warmupy stejného jobu/modelu.
    $pointsJson = json_encode([['kind' => 'warmup']], JSON_UNESCAPED_SLASHES);
    $st = db()->prepare("SELECT id FROM previews WHERE job_id=? AND sam_model=?
                         AND points_json LIKE '%\"warmup\"%'
                         AND status IN ('pending','claimed') LIMIT 1");
    $st->execute([$job['id'], $samModel]);
    $old = $st->fetch();
    if ($old) json_out(['ok' => true, 'preview_id' => (int)$old['id'], 'warmup' => true]);

    $ins = db()->prepare(
        'INSERT INTO previews(job_id,frame_index,sam_model,points_json,status)
         VALUES(?,?,?,?,?)'
    );
    $ins->execute([$job['id'], 0, $samModel, $pointsJson, 'pending']);
    json_out(['ok' => true, 'preview_id' => (int)db()->lastInsertId(), 'warmup' => true]);
}

// KLIENT: poll stavu náhledu. Když done, vrátí URL na PNG masku.
function preview_result(): void {
    $pid = (int)($_GET['id'] ?? 0);
    $st = db()->prepare('SELECT * FROM previews WHERE id=?');
    $st->execute([$pid]);
    $pv = $st->fetch();
    if (!$pv) json_err('Preview not found', 404);

    $out = ['ok' => true, 'status' => $pv['status']];
    if ($pv['status'] === 'done' && $pv['mask_path']) {
        $out['mask_url'] = 'api/index.php?action=preview/mask&id=' . $pid;
    } elseif ($pv['status'] === 'error') {
        $out['error'] = $pv['error_msg'];
    }
    json_out($out);
}

// KLIENT: servíruje hotovou náhledovou PNG masku.
function preview_mask(): void {
    $pid = (int)($_GET['id'] ?? 0);
    $st = db()->prepare('SELECT mask_path FROM previews WHERE id=?');
    $st->execute([$pid]);
    $pv = $st->fetch();
    if (!$pv || !$pv['mask_path']) json_err('Mask not found', 404);
    $path = STORAGE . '/' . $pv['mask_path'];
    if (!is_file($path)) json_err('Mask file missing', 404);
    header('Content-Type: image/png');
    header('Cache-Control: no-store');
    readfile($path);
    exit;
}

// WORKER: převezme 1 pending náhled.
function worker_preview_claim(): void {
    $d = body_json();
    $workerId = substr((string)($d['worker_id'] ?? 'worker'), 0, 64);
    $pdo = db();
    $pdo->beginTransaction();
    try {
        $row = $pdo->query("SELECT * FROM previews WHERE status='pending'
                            ORDER BY id DESC LIMIT 1")->fetch();  // nejnovější první
        if (!$row) { $pdo->commit(); json_out(['ok' => true, 'preview' => null]); }
        $pdo->prepare("UPDATE previews SET status='claimed', worker_id=?,
                       updated_at=datetime('now') WHERE id=? AND status='pending'")
            ->execute([$workerId, $row['id']]);
        $pdo->commit();
    } catch (Throwable $e) {
        $pdo->rollBack();
        json_err('Preview claim failed: ' . $e->getMessage(), 500);
    }

    // dohledej zdroj jobu, aby worker uměl pro první masku vytáhnout
    // full-quality první frame přímo ze zdrojového videa bez extrakce celého videa.
    $job = $pdo->prepare('SELECT token,source_type,source_path,width,height,fps FROM jobs WHERE id=?');
    $job->execute([$row['job_id']]);
    $job = $job->fetch();

    json_out(['ok' => true, 'preview' => [
        'id'          => (int)$row['id'],
        'token'       => $job ? $job['token'] : '',
        'source_type' => $job ? $job['source_type'] : '',
        'source_path' => $job ? $job['source_path'] : '',
        'width'       => $job ? (int)$job['width'] : 0,
        'height'      => $job ? (int)$job['height'] : 0,
        'fps'         => $job ? (float)$job['fps'] : 0,
        'frame_index' => (int)$row['frame_index'],
        'sam_model'   => $row['sam_model'],
        'points'      => json_decode($row['points_json'], true) ?: [],
    ]]);
}

// WORKER: nahraje hotovou náhledovou masku (PNG).
function worker_preview_result(): void {
    $id = (int)($_GET['id'] ?? 0);
    $st = db()->prepare('SELECT job_id FROM previews WHERE id=?');
    $st->execute([$id]);
    $pv = $st->fetch();
    if (!$pv) json_err('Preview not found', 404);
    if (!isset($_FILES['mask']) || $_FILES['mask']['error'] !== UPLOAD_ERR_OK) {
        json_err('PNG mask missing');
    }
    $dir = 'previews';
    $absDir = STORAGE . '/' . $dir;
    if (!is_dir($absDir)) @mkdir($absDir, 0775, true);
    $rel = $dir . '/' . $id . '.png';
    $dest = STORAGE . '/' . $rel;
    if (!move_uploaded_file($_FILES['mask']['tmp_name'], $dest)) {
        json_err('Cannot save mask', 500);
    }
    db()->prepare("UPDATE previews SET status='done', mask_path=?,
                   updated_at=datetime('now') WHERE id=?")->execute([$rel, $id]);
    json_out(['ok' => true]);
}

// WORKER: nahlásí chybu náhledu.
function worker_preview_fail(): void {
    $d = body_json();
    $id = (int)($d['id'] ?? 0);
    $msg = substr((string)($d['error'] ?? 'error'), 0, 500);
    db()->prepare("UPDATE previews SET status='error', error_msg=?,
                   updated_at=datetime('now') WHERE id=?")->execute([$msg, $id]);
    json_out(['ok' => true]);
}
