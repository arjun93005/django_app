from .base import *  # noqa: F403
from .base import REST_FRAMEWORK

DEBUG = True
ALLOWED_HOSTS = ["*"]

REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = [
    "rest_framework.renderers.JSONRenderer",
    "rest_framework.renderers.BrowsableAPIRenderer",
]
