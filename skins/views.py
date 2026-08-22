import io
import os
import zipfile

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.http import require_POST

from ddnet.session_attributes import SessionAttribute, SessionAttributeUtil
from ddnet.views import DDListView
from .models import Skin


# Main view class
class SkinListView(DDListView):
    model = Skin
    items_total_count = 0

    def get(self, request, *args, **kwargs):
        # get total count
        self.items_total_count = Skin.objects.count()
        return super(SkinListView, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(SkinListView, self).get_context_data(**kwargs)
        context['selected_skins_to_download'] = SessionAttributeUtil.get(self.request, SessionAttribute.SELECTED_SKINS, [])
        # pass total count to the template
        context['items_total_count'] = self.items_total_count
        context['full_skins_pack_zip_url'] = settings.MEDIA_URL + 'skins.zip'
        return context


# Additional view methods
@require_POST
def add_to_download(request):
    # get the currently selected skins as set, which unfortunately is not serializable
    selected_skins = SessionAttributeUtil.get(request, SessionAttribute.SELECTED_SKINS, [])
    selected_skins_set = set(selected_skins)

    skin_name = __get_skin_name(request)
    if not skin_name or not Skin.objects.filter(skin_image=skin_name).exists():
        return HttpResponseBadRequest('Unknown skin')
    selected_skins_set.add(skin_name)

    selected_skins = list(selected_skins_set)
    SessionAttributeUtil.set(request, SessionAttribute.SELECTED_SKINS, selected_skins)

    # return current selection count - we do not need to send the whole list
    return HttpResponse(len(selected_skins))


@require_POST
def remove_from_download(request):
    selected_skins = SessionAttributeUtil.get(request, SessionAttribute.SELECTED_SKINS, [])

    skin_name = __get_skin_name(request)
    try:
        selected_skins.remove(skin_name)
    except ValueError:
        pass

    SessionAttributeUtil.set(request, SessionAttribute.SELECTED_SKINS, selected_skins)

    # return current selection count - we do not need to send the whole list
    return HttpResponse(len(selected_skins))


@require_POST
def clear_download_list(request):
    selected_skins = SessionAttributeUtil.get(request, SessionAttribute.SELECTED_SKINS, [])
    selected_skins.clear()

    SessionAttributeUtil.set(request, SessionAttribute.SELECTED_SKINS, selected_skins)

    return HttpResponse()


def download_selected(request):
    selected_skins = SessionAttributeUtil.get(request, SessionAttribute.SELECTED_SKINS, [])
    skins = list(Skin.objects.filter(skin_image__in=selected_skins))
    SessionAttributeUtil.set(
        request,
        SessionAttribute.SELECTED_SKINS,
        [skin.skin_image.name for skin in skins],
    )

    # use BytesIO to create in memory download zip file
    output_buffer = io.BytesIO()
    with zipfile.ZipFile(output_buffer, 'w') as zip_file_to_download:
        # add skins to zip file
        for skin in skins:
            zip_file_to_download.write(skin.skin_image.path, os.path.basename(skin.skin_image.name))

    # restore buffer to the beginning - at the end the buffer is on the last added file
    output_buffer.seek(0)
    response = HttpResponse(output_buffer, content_type='application/zip')
    response['Content-Disposition'] = 'attachment; filename=selected_skins.zip'

    return response


def __get_skin_name(request):
    return request.POST.get('skinName')
