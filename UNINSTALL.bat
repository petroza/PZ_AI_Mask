<?php
/**
 * Router pro PHP built-in server (jen lokalni test).
 * Mapuje:
 *    /                -> public/index.html
 *    /editor.html     -> public/editor.html
 *    /api/...         -> api/index.php (s predanim query)
 *    /storage/...     -> staticke soubory ze storage/
 *
 * Spousti se pres START.bat (pripadne nastroje\run_frontend.bat). Na Forpsi se NEPOUZIVA
 * (tam je docroot rovnou public/ a .htaccess resi zbytek).
 */

$root = __DIR__;
$uri  = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
$uri  = rawurldecode($uri);

// API -> api/index.php
if (preg_match('#^/api/index\.php#', $uri) || strpos($uri, '/api/') === 0) {
    // nechame index.php zpracovat (action je v $_GET)
    chdir($root . '/api');
    require $root . '/api/index.php';
    return true;
}

// staticke soubory ze storage (nahledy framu, vysledky)
if (strpos($uri, '/storage/') === 0) {
    $f = $root . $uri;
    if (is_file($f)) {
        $ext = strtolower(pathinfo($f, PATHINFO_EXTENSION));
        $mt = ['jpg'=>'image/jpeg','jpeg'=>'image/jpeg','png'=>'image/png',
               'zip'=>'application/zip','webm'=>'video/webm','mp4'=>'video/mp4'];
        if (isset($mt[$ext])) header('Content-Type: ' . $mt[$ext]);
        readfile($f);
        return true;
    }
    http_response_code(404);
    return true;
}

// koren -> index.html
if ($uri === '/' || $uri === '') {
    readfile($root . '/public/index.html');
    return true;
}

// ostatni -> hledej v public/
$pub = $root . '/public' . $uri;
if (is_file($pub)) {
    $ext = strtolower(pathinfo($pub, PATHINFO_EXTENSION));
    $mt = ['html'=>'text/html','js'=>'text/javascript','css'=>'text/css',
           'png'=>'image/png','jpg'=>'image/jpeg','svg'=>'image/svg+xml',
           'json'=>'application/json','ico'=>'image/x-icon'];
    if (isset($mt[$ext])) header('Content-Type: ' . $mt[$ext]);
    readfile($pub);
    return true;
}

// fallback do rootu (kdyby neco)
if (is_file($root . $uri)) {
    return false; // nechame PHP server obslouzit sam
}

http_response_code(404);
echo "404";
return true;
