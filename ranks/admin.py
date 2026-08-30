from django.contrib.admin import ModelAdmin
from django.urls import path

from ddnet_django import admin
from . import views
from .models import RankGraveyard, SaveControl


class RankGraveyardAdmin(ModelAdmin):
    def get_urls(self):
        return [
            path(
                '',
                self.admin_site.admin_view(self.index_view),
                name='ranks_rankgraveyard_changelist',
            ),
            path(
                'preview/',
                self.admin_site.admin_view(self.preview_view),
                name='ranks_rankgraveyard_preview',
            ),
            path(
                'graveyard/',
                self.admin_site.admin_view(self.commit_view),
                name='ranks_rankgraveyard_commit',
            ),
            path(
                'action/<uuid:action_id>/',
                self.admin_site.admin_view(self.detail_view),
                name='ranks_rankgraveyard_detail',
            ),
            path(
                'action/<uuid:action_id>/restore/',
                self.admin_site.admin_view(self.restore_view),
                name='ranks_rankgraveyard_restore',
            ),
        ]

    def index_view(self, request):
        return views.index(request, self)

    def preview_view(self, request):
        return views.preview(request, self)

    def commit_view(self, request):
        return views.commit(request, self)

    def detail_view(self, request, action_id):
        return views.detail(request, self, action_id)

    def restore_view(self, request, action_id):
        return views.restore_action(request, self, action_id)

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(RankGraveyard, RankGraveyardAdmin)


class SaveControlAdmin(ModelAdmin):
    def get_urls(self):
        return [
            path('', self.admin_site.admin_view(self.index_view),
                 name='ranks_savecontrol_changelist'),
            path('review/', self.admin_site.admin_view(self.review_view),
                 name='ranks_savecontrol_review'),
            path('restore/', self.admin_site.admin_view(self.restore_view),
                 name='ranks_savecontrol_restore'),
            path('action/<uuid:action_id>/', self.admin_site.admin_view(self.detail_view),
                 name='ranks_savecontrol_detail'),
            path('action/<uuid:action_id>/restore/',
                 self.admin_site.admin_view(self.restore_again_view),
                 name='ranks_savecontrol_restore_again'),
        ]

    def index_view(self, request):
        return views.save_index(request, self)

    def review_view(self, request):
        return views.save_review(request, self)

    def restore_view(self, request):
        return views.save_restore(request, self)

    def detail_view(self, request, action_id):
        return views.save_detail(request, self, action_id)

    def restore_again_view(self, request, action_id):
        return views.save_restore_again(request, self, action_id)

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(SaveControl, SaveControlAdmin)
