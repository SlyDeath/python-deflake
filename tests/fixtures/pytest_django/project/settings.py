SECRET_KEY = "fixture"
DEBUG = False
USE_TZ = True
INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.auth", "shop"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "db.sqlite3"}}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
ROOT_URLCONF = "project.urls"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
