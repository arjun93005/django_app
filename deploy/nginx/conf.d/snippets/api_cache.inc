# Micro-caching for API GETs (included by the /api/ locations).
#
# Django marks cacheable responses with `Cache-Control: public, max-age=N`
# (N = 5-60s); Nginx honours that, so freshness is decided by the app.
# Even a 5s cache collapses a burst of identical requests into one upstream hit.

uwsgi_cache            api_cache;
uwsgi_cache_key        "$request_method|$host|$request_uri|$http_accept";
uwsgi_cache_methods    GET HEAD;
uwsgi_cache_bypass     $skip_cache;     # authenticated: always go to Django
uwsgi_no_cache         $skip_cache;
# Stampede protection: one request populates an expired entry, the others wait
# (up to 2s) instead of all hitting Django at once...
uwsgi_cache_lock          on;
uwsgi_cache_lock_timeout  2s;
# ...or are served the stale copy while a background request refreshes it.
uwsgi_cache_use_stale          updating error timeout http_500 http_502 http_503;
uwsgi_cache_background_update  on;
# add_header in a location replaces the server-level ones, so repeat them.
add_header X-Cache-Status $upstream_cache_status always;
add_header X-Request-ID $request_id always;
add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options DENY always;
add_header Referrer-Policy same-origin always;
