from .settings import *  # noqa

import pymysql

pymysql.install_as_MySQLdb()

DEBUG = True
ALLOWED_HOSTS = ['127.0.0.1', 'localhost']
FORCE_SCRIPT_NAME = '/django'
SECRET_KEY = 'local-dev-key-not-secret'
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

STATIC_ROOT = r'C:\var\www-django\static'
RELEASE_LOG = 'release.log'
FIX_LOG = 'fix.log'


def database(name):
    return {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': name,
        'USER': 'teeworlds',
        'PASSWORD': 'teeworlds',
        'HOST': '127.0.0.1',
        'PORT': '3306',
        'OPTIONS': {
            'charset': 'utf8',
            'isolation_level': 'repeatable read',
        },
    }


DATABASES = {
    'default': database('django_dev'),
    'skins_db': database('skins_dev'),
    'ddnet_db': database('teeworlds'),
}
