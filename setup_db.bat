@echo off
setlocal
set "DJANGO_SETTINGS_MODULE=ddnet_django.settings_local"

".venv\Scripts\python.exe" manage.py migrate --database=default
if errorlevel 1 exit /b %errorlevel%

".venv\Scripts\python.exe" manage.py migrate --database=ddnet_db
if errorlevel 1 exit /b %errorlevel%

".venv\Scripts\python.exe" manage.py migrate --database=skins_db
if errorlevel 1 exit /b %errorlevel%

".venv\Scripts\python.exe" manage.py loaddata MapCategory.json ServerType.json --database=ddnet_db
endlocal
