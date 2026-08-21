from .settings import *  # noqa

DEBUG = False
ALLOWED_HOSTS = ['ddnet.org']
SECRET_KEY = 'replace-me'

RELEASE_LOG = '/home/django/log/release.log'
FIX_LOG = '/home/django/log/fix.log'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'django',
        'USER': 'django',
        'PASSWORD': 'replace-me',
        'HOST': 'localhost',
        'PORT': '5432',
    },
    'skins_db': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'skins',
        'USER': 'django',
        'PASSWORD': 'replace-me',
        'HOST': 'localhost',
        'PORT': '5432',
    },
    'ddnet_db': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': 'teeworlds',
        'USER': 'teeworlds',
        'PASSWORD': 'replace-me',
        'HOST': 'localhost',
        'PORT': '3306',
        'TIME_ZONE': 'Europe/Berlin',
        'OPTIONS': {
            'charset': 'utf8',
            'isolation_level': 'repeatable read',
        },
    },
}
