# PZ Mask Studio API — routing + limity
# Forpsi: PHP běží typicky jako CGI/FPM, hodnoty lze ladit i v .user.ini

<IfModule mod_rewrite.c>
    RewriteEngine On
    # vše směruj na index.php, akci předej jako PATH_INFO
    RewriteCond %{REQUEST_FILENAME} !-f
    RewriteCond %{REQUEST_FILENAME} !-d
    RewriteRule ^(.*)$ index.php/$1 [QSA,L]
</IfModule>

# Upload limity (4 GB videa). Na Forpsi případně přesunout do .user.ini
php_value upload_max_filesize 4096M
php_value post_max_size 4096M
php_value max_execution_time 600
php_value max_input_time 600
php_value memory_limit 512M

# CORS pro lokální worker (povol jen co potřebuješ; pro web UI na stejné doméně netřeba)
<IfModule mod_headers.c>
    Header set Access-Control-Allow-Origin "*"
    Header set Access-Control-Allow-Headers "Content-Type, X-Worker-Token"
    Header set Access-Control-Allow-Methods "GET, POST, OPTIONS"
</IfModule>
