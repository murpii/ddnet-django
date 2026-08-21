'''URL-definitions for the skindatabase.'''

from django.urls import path
from .views import ReleaseLogView, FixLogView

app_name = "maps"

urlpatterns = [
    path("api/release/log/", ReleaseLogView.as_view(), name="release_log"),
    path("api/fix/log/", FixLogView.as_view(), name="fix_log"),
]
