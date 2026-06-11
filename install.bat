# PZ Mask Studio — Apache konfigurace pro Forpsi shared hosting.
# Umísti do KOŘENE aplikace (vedle složek public/, api/, storage/).

# ---- Limity uploadu (jinak velká videa selžou prázdnou odpovědí) ----
php_value upload_max_filesize 4096M
php_value post_max_size       4096M
php_value max_execution_time  600
php_value max_input_time      600
php_value memory_limit        512M

# ---- Bezpečnost: zakázat přístup k DB a interním složkám ----
<FilesMatch "\.(db|db-wal|db-shm|sqlite)$">
    Require all denied
</FilesMatch>

# schema, configy workeru apod. nejsou pod webrootem, ale pro jistotu:
<FilesMatch "\.(sql|json|md)$">
    Require all denied
</FilesMatch>

# storage/uploads a storage/results servírujeme staticky (worker je čte),
# ale zakážeme výpis adresářů
Options -Indexes
