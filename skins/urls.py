'''URL-definitions for the skindatabase.'''

from django.urls import path
from skins.views import SkinListView
from . import views

app_name = "skins"

urlpatterns = [
    path("", SkinListView.as_view(), name="skin_list"),
    path("add-to-download", views.add_to_download),
    path("remove-from-download", views.remove_from_download),
    path("clear-download-list", views.clear_download_list),
    path("download-selected", views.download_selected),
]
