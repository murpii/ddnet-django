from django.urls import path  # or: from django.urls import path, re_path
from django.views.generic import RedirectView
from django.contrib.admin import ModelAdmin

from ddnet_django import admin
from .models import Broadcast
from .views import BroadcastView


class BroadcastAdmin(ModelAdmin):
    def get_urls(self):
        urls = super().get_urls()
        info = Broadcast._meta.app_label, Broadcast._meta.model_name

        custom = [
            path(
                "broadcast/",
                self.admin_site.admin_view(BroadcastView.as_view()),
                name="broadcast",
            ),
            path(
                "",
                RedirectView.as_view(pattern_name="admin:broadcast", permanent=False),
                name=f"{info[0]}_{info[1]}_changelist",
            ),
        ]
        return custom + urls


admin.site.register(Broadcast, BroadcastAdmin)