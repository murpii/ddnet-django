'''URL-definitions for Servers.'''

from django.urls import path
from .views import BroadcastLogView

app_name = "servers"

urlpatterns = [
    path("api/broadcast/log/", BroadcastLogView.as_view(), name="broadcast_log"),
]