"""Production settings: served by uWSGI behind Nginx."""

from .base import *  # noqa: F403
from .base import env

DEBUG = False
SECRET_KEY = env("DJANGO_SECRET_KEY")  # no default: fail fast if missing

# Nginx terminates the client connection and forwards the original scheme.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

SECURE_COOKIES = env.bool("DJANGO_SECURE_COOKIES", default=True)
SESSION_COOKIE_SECURE = SECURE_COOKIES
CSRF_COOKIE_SECURE = SECURE_COOKIES
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Hashed file names (app.3f2a1b.css) let Nginx serve /static/ with a 1-year,
# immutable cache policy: a new deploy produces new URLs.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}

# Nginx streams export files; Django only authorises the download.
EXPORTS_USE_X_ACCEL = env.bool("EXPORTS_USE_X_ACCEL", default=True)
