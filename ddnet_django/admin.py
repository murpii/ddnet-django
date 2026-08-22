from django.contrib import admin
from django.urls import re_path as url

from django.contrib.auth.models import Group, User, Permission
from django.contrib.auth.admin import GroupAdmin, UserAdmin

from maps.views import MapReleaseView, MapFixView
from servers.views import BroadcastView


class DDNetAdmin(admin.AdminSite):
    site_header = 'DDNet Administration'
    site_title = 'DDNet Administration'
    site_url = 'https://ddnet.org/'

site = DDNetAdmin()
site.enable_nav_sidebar = False

MapReleaseView.admin = site
MapFixView.admin = site
BroadcastView.admin = site

site.register(Group, GroupAdmin)
site.register(User, UserAdmin)
site.register(Permission)
