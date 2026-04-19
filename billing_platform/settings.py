import os
from pathlib import Path

from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config(
    "DJANGO_SECRET_KEY",
    default="dev-insecure-secret-key-change-me",
)

DEBUG = config("DJANGO_DEBUG", default=True, cast=bool)

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "billing",
]

MIDDLEWARE = [
    # Rate-limit admission runs first so a rejected request spends
    # no cycles on body parsing, auth, or view dispatch.
    "billing.interfaces.api.incoming.middleware.RateLimitMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "billing_platform.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

WSGI_APPLICATION = "billing_platform.wsgi.application"
ASGI_APPLICATION = "billing_platform.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
TIME_ZONE = "UTC"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "UNAUTHENTICATED_USER": None,
}

# --- Redis ---
# Single source of truth for the Redis URL. Used by the rate limiter,
# concurrency semaphore, and any simulated "shared resource" in tests.
REDIS_URL = config("REDIS_URL", default="redis://localhost:6379/0")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "kv": {
            "format": (
                "[%(asctime)s] level=%(levelname)s logger=%(name)s %(message)s"
            ),
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "kv",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
