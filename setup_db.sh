#!/bin/sh
set -eu

./.venv/bin/python manage.py migrate --database=default
./.venv/bin/python manage.py migrate --database=ddnet_db
./.venv/bin/python manage.py migrate --database=skins_db

./.venv/bin/python manage.py loaddata MapCategory.json ServerType.json --database=ddnet_db
