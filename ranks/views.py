from datetime import datetime
from urllib.parse import urlencode

from django.contrib import messages
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from .forms import (
    CheckpointScanForm,
    GraveyardForm,
    HistorySearchForm,
    RankSearchForm,
    RestoreForm,
    SaveRestoreForm,
    SaveSearchForm,
)
from .graveyard import (
    RankGraveyardError,
    category_names,
    get_action,
    get_action_overview,
    get_history_action,
    graveyard,
    is_map_only_search,
    list_map_names,
    list_checkpoint_player_maps,
    list_actions,
    preview_player_overview,
    preview_player_map,
    preview_target,
    restore,
    restore_entry,
    scan_checkpoint_clusters,
    schema_ready,
    search_live,
    search_map_leaderboard_page,
)
from .save_recovery import (
    candidate_target,
    live_similar_saves,
    load_candidate,
    restore_save,
    save_details,
    save_status,
    search_deleted_saves,
)


token_salt = 'ranks.graveyard.target'
save_token_salt = 'ranks.save_recovery.target'


def checkpoint_scan_query(player_name, map_name='', source_rank=''):
    return urlencode({
        'submit': 'checkpoint',
        'scan-source_rank': source_rank,
        'scan-map_name': map_name,
        'scan-player_name': player_name,
        'scan-mode': 'range',
        'scan-start_checkpoint': 1,
        'scan-end_checkpoint': 5,
        'scan-minimum_players': 4,
        'scan-leaderboard_depth': 100,
        'scan-checkpoint_margin': 0,
    })


def require_superuser(request):
    if not request.user.is_active or not request.user.is_superuser:
        raise PermissionDenied


def admin_context(model_admin, request, title):
    context = model_admin.admin_site.each_context(request)
    context.update({
        'opts': model_admin.model._meta,
        'app_label': model_admin.model._meta.app_label,
        'title': title,
    })
    return context


def sign_target(target):
    return signing.dumps(target, salt=token_salt, compress=True)


def load_target(value):
    try:
        return signing.loads(value, salt=token_salt, max_age=3600)
    except signing.BadSignature as error:
        raise RankGraveyardError('The confirmation expired. Search for the rank again.') from error


def add_graveyard_entry_tokens(preview, selected_targets=()):
    preview['selection_enabled'] = True
    for entry in preview['entries']:
        entry['graveyard_token'] = sign_target(entry['target'])
        entry['graveyard_checked'] = entry['target'] in selected_targets


def add_restore_entry_tokens(action):
    if action['restored_at'] is None:
        action['preview']['show_entry_actions'] = True
    for entry in action['preview']['entries']:
        if not entry['restored']:
            entry['restore_token'] = sign_target({
                'action_id': str(action['action_id']),
                'target': entry['target'],
            })


def review_preview(target):
    if target.get('kind') == 'player':
        return preview_player_overview(target['player_name'])
    return preview_target(target)


def graveyard_target(source, selected_targets, all_records):
    if all_records:
        if source.get('kind') != 'player' or selected_targets:
            raise RankGraveyardError('Invalid whole-player selection.')
        return source
    return {'kind': 'selection', 'source': source, 'targets': selected_targets}


def rank_page_sections(query):
    sections = (('live', 'live-'), ('checkpoint', 'scan-'), ('history', 'history-'))
    names = {section for section, prefix in sections}
    submitted = query.get('submit')
    if submitted in names:
        return submitted, submitted
    for section, prefix in sections:
        if any(key.startswith(prefix) for key in query):
            return section, section
    active = query.get('tab')
    return (active if active in names else 'live'), None


def category_tab_data(available_categories, requested_category):
    categories = list(category_names())
    categories.extend(sorted(available_categories - set(categories)))
    selected_category = (
        requested_category
        if requested_category in available_categories
        else next((name for name in categories if name in available_categories), None)
    )
    tabs = [
        {
            'name': name,
            'label': name or 'Unknown',
            'available': name in available_categories,
            'selected': name == selected_category,
        }
        for name in categories
    ]
    return tabs, selected_category


def index(request, model_admin):
    require_superuser(request)
    context = admin_context(model_admin, request, 'Rank Control')
    active_tab, submitted_tab = rank_page_sections(request.GET)
    search_requested = submitted_tab == 'live'
    checkpoint_requested = submitted_tab == 'checkpoint'
    history_requested = submitted_tab == 'history'
    search_form = RankSearchForm(request.GET if search_requested else None, prefix='live')
    history_form = HistorySearchForm(request.GET if history_requested else None, prefix='history')
    checkpoint_form = CheckpointScanForm(
        request.GET if checkpoint_requested else None,
        prefix='scan',
    )
    results = []
    result_groups = []
    result_page = None
    category_tabs = []
    selected_category = None
    checkpoint_results = []
    checkpoint_player = None
    checkpoint_groups = []
    checkpoint_page = None
    checkpoint_category_tabs = []
    checkpoint_selected_category = None
    checkpoint_source = None
    actions = []
    map_names = []
    error = None
    search_is_valid = search_requested and search_form.is_valid()
    map_only_search = search_is_valid and is_map_only_search(search_form.cleaned_data)
    checkpoint_is_valid = checkpoint_requested and checkpoint_form.is_valid()
    try:
        schema_ready()
        if active_tab != 'history':
            map_names = list_map_names()
        if search_requested:
            if search_is_valid:
                if map_only_search:
                    results, result_page = search_map_leaderboard_page(
                        search_form.cleaned_data, request.GET.get('page')
                    )
                else:
                    results = search_live(search_form.cleaned_data)
                if not search_form.cleaned_data.get('map_name'):
                    available_categories = {result['category'] for result in results}
                    category_tabs, selected_category = category_tab_data(
                        available_categories, request.GET.get('category')
                    )
                    results = [
                        result for result in results
                        if result['category'] == selected_category
                    ]
                    groups = {}
                    for result in results:
                        group = groups.setdefault(result['map_name'], {
                            'map_name': result['map_name'],
                            'category': result['category'],
                            'stars': result['stars'],
                            'rows': [],
                        })
                        group['rows'].append(result)
                    for group in groups.values():
                        group['rows'].sort(key=lambda row: row['time_value'])
                    result_page = Paginator(list(groups.values()), 100).get_page(
                        request.GET.get('page')
                    )
                    result_groups = result_page.object_list
                    results = [row for group in result_groups for row in group['rows']]
                for result in results:
                    target = result.pop('target')
                    result['token'] = sign_target(target)
                    result['player_links'] = [
                        {
                            'name': player_name,
                            'checkpoint_query': checkpoint_scan_query(player_name),
                        }
                        for player_name in result['player_names']
                    ]
                    if target['kind'] == 'rank' and result.get('has_checkpoints'):
                        result['checkpoint_query'] = checkpoint_scan_query(
                            result['player_names'][0], result['map_name'], result['token']
                        )
        if checkpoint_is_valid:
            checkpoint_player = checkpoint_form.cleaned_data.get('player_name')
            checkpoint_data = dict(checkpoint_form.cleaned_data)
            source_token = checkpoint_data.get('source_rank')
            if source_token:
                try:
                    source_target = load_target(source_token)
                    if source_target.get('kind') != 'rank':
                        raise RankGraveyardError('The checkpoint source is not a Normal Rank.')
                    source_preview = preview_target(source_target)
                    source_finish = source_preview['finishes'][0]
                    if (
                        source_finish['map_name'] != checkpoint_data.get('map_name')
                        or source_finish['player_name'] != checkpoint_player
                    ):
                        raise RankGraveyardError(
                            'The checkpoint source does not match the selected Map and Player.'
                        )
                    checkpoint_data['source_finish'] = source_finish
                    checkpoint_source = source_finish
                except RankGraveyardError as source_error:
                    checkpoint_form.add_error('source_rank', str(source_error))
                    checkpoint_is_valid = False
            if not checkpoint_is_valid:
                checkpoint_data = None
            if checkpoint_data and checkpoint_player and not checkpoint_data.get('map_name'):
                player_maps = list_checkpoint_player_maps(checkpoint_player)
                available_categories = {row['category'] for row in player_maps}
                checkpoint_category_tabs, checkpoint_selected_category = category_tab_data(
                    available_categories, request.GET.get('checkpoint_category')
                )
                category_maps = [
                    row for row in player_maps
                    if row['category'] == checkpoint_selected_category
                ]
                checkpoint_data['map_names'] = [row['map_name'] for row in category_maps]
                map_details = {row['map_name']: row for row in category_maps}
            elif checkpoint_data:
                map_details = {}
            else:
                map_details = {}
            checkpoint_results = (
                scan_checkpoint_clusters(checkpoint_data) if checkpoint_data else []
            )
            checkpoint_groups_by_map = {}
            for result in checkpoint_results:
                result['token'] = sign_target(result.pop('target'))
                if map_details:
                    result.update(map_details[result['map_name']])
                    group = checkpoint_groups_by_map.get(result['map_name'])
                    if group is None:
                        group = dict(map_details[result['map_name']], rows=[])
                        checkpoint_groups.append(group)
                        checkpoint_groups_by_map[result['map_name']] = group
                    group['rows'].append(result)
            checkpoint_groups.sort(key=lambda group: (
                group['stars'] or 0, group['map_name'].casefold()
            ))
            if map_details:
                checkpoint_page = Paginator(checkpoint_groups, 25).get_page(
                    request.GET.get('checkpoint_page')
                )
                checkpoint_groups = list(checkpoint_page.object_list)
        if active_tab == 'history':
            if history_requested:
                if history_form.is_valid():
                    actions = list_actions(dict(history_form.cleaned_data, control='rank'))
            else:
                actions = list_actions({'control': 'rank'})
    except (DatabaseError, RankGraveyardError):
        error = 'The records tables or permissions are not ready.'
    player_token = None
    if search_is_valid and search_form.cleaned_data.get('player_name'):
        player_token = sign_target({
            'kind': 'player',
            'player_name': search_form.cleaned_data['player_name'],
        })
    page_query = request.GET.copy()
    page_query.pop('page', None)
    category_query = request.GET.copy()
    category_query.pop('page', None)
    category_query.pop('category', None)
    checkpoint_page_query = request.GET.copy()
    checkpoint_page_query.pop('checkpoint_page', None)
    checkpoint_category_query = request.GET.copy()
    checkpoint_category_query.pop('checkpoint_page', None)
    checkpoint_category_query.pop('checkpoint_category', None)
    context.update({
        'search_form': search_form,
        'history_form': history_form,
        'checkpoint_form': checkpoint_form,
        'results': results,
        'result_groups': result_groups,
        'result_page': result_page,
        'page_query': page_query.urlencode(),
        'category_query': category_query.urlencode(),
        'category_tabs': category_tabs,
        'selected_category': selected_category,
        'checkpoint_results': checkpoint_results,
        'checkpoint_player': checkpoint_player,
        'checkpoint_groups': checkpoint_groups,
        'checkpoint_page': checkpoint_page,
        'checkpoint_page_query': checkpoint_page_query.urlencode(),
        'checkpoint_category_query': checkpoint_category_query.urlencode(),
        'checkpoint_category_tabs': checkpoint_category_tabs,
        'checkpoint_selected_category': checkpoint_selected_category,
        'checkpoint_source': checkpoint_source,
        'checkpoint_cross_map': checkpoint_is_valid and checkpoint_player and not checkpoint_form.cleaned_data.get('map_name'),
        'actions': actions,
        'map_names': map_names,
        'player_token': player_token,
        'search_requested': search_requested,
        'cross_map_search': search_is_valid and not search_form.cleaned_data.get('map_name'),
        'map_only_search': map_only_search,
        'map_rank_limit': (
            search_form.cleaned_data.get('map_rank_limit') if map_only_search else 100
        ),
        'active_tab': active_tab,
        'error': error,
    })
    return render(request, 'admin/ranks/rankgraveyard/change_list.html', context)


def preview(request, model_admin):
    require_superuser(request)
    token = request.GET.get('target')
    if not token:
        return HttpResponseBadRequest('Missing rank target.')
    try:
        target = load_target(token)
        preview_map = request.GET.get('preview_map')
        if preview_map:
            if target.get('kind') != 'player':
                raise RankGraveyardError('Map previews require a player review.')
            result = preview_player_map(target['player_name'], preview_map)
            add_graveyard_entry_tokens(result)
            context = admin_context(model_admin, request, 'Review Individual Ranks')
            context.update({
                'preview': result,
                'table_entries': result['entries'],
                'table_heading': preview_map,
            })
            return render(
                request,
                'admin/ranks/rankgraveyard/_preview_table.html',
                context,
            )
        result = review_preview(target)
        if not result.get('player_scope'):
            add_graveyard_entry_tokens(result)
    except RankGraveyardError as error:
        messages.error(request, str(error))
        return redirect('admin:ranks_rankgraveyard_changelist')
    context = admin_context(model_admin, request, 'Confirm Records Action')
    context.update({
        'preview': result,
        'form': GraveyardForm(initial={'target': token}),
    })
    return render(request, 'admin/ranks/rankgraveyard/preview.html', context)


def commit(request, model_admin):
    require_superuser(request)
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required.')
    form = GraveyardForm(request.POST)
    try:
        selected_targets = [
            load_target(token) for token in request.POST.getlist('selected')
        ]
    except RankGraveyardError as error:
        messages.error(request, str(error))
        return redirect('admin:ranks_rankgraveyard_changelist')
    if not form.is_valid():
        return render_invalid_preview(
            request, model_admin, form, selected_targets=selected_targets
        )
    try:
        source = load_target(form.cleaned_data['target'])
        if form.cleaned_data['all_records']:
            target = graveyard_target(source, selected_targets, True)
        else:
            if not selected_targets:
                form.add_error(None, 'Select at least one rank.')
                return render_invalid_preview(request, model_admin, form, target=source)
            target = graveyard_target(source, selected_targets, False)
        action_id = graveyard(
            target,
            request.user.pk,
            request.user.get_username(),
            form.cleaned_data['reason'],
        )
    except (DatabaseError, RankGraveyardError) as error:
        messages.error(request, str(error))
        return redirect('admin:ranks_rankgraveyard_changelist')
    messages.success(request, 'The selected records were moved out of the live database.')
    return redirect('admin:ranks_rankgraveyard_detail', action_id=action_id)


def render_invalid_preview(
    request, model_admin, form, target=None, selected_targets=()
):
    try:
        target = target or load_target(form.data.get('target', ''))
        result = review_preview(target)
        if not result.get('player_scope'):
            add_graveyard_entry_tokens(result, selected_targets)
    except RankGraveyardError as error:
        messages.error(request, str(error))
        return redirect('admin:ranks_rankgraveyard_changelist')
    context = admin_context(model_admin, request, 'Confirm Records Action')
    context.update({'preview': result, 'form': form})
    return render(request, 'admin/ranks/rankgraveyard/preview.html', context)


def detail(request, model_admin, action_id):
    require_superuser(request)
    try:
        action = get_action_overview(action_id)
        archive_map = request.GET.get('archive_map')
        if archive_map:
            group = next(
                (
                    item for item in action['preview']['action_map_groups']
                    if item['map_name'] == archive_map
                ),
                None,
            )
            if group is None:
                raise RankGraveyardError('This action has no ranks on that map.')
            if not group['individual_preview']:
                raise RankGraveyardError(
                    'This map has too many archived ranks for an inline review.'
                )
            action = get_action(action_id, map_name=archive_map)
        add_restore_entry_tokens(action)
    except (DatabaseError, RankGraveyardError) as error:
        if request.GET.get('archive_map'):
            return HttpResponseBadRequest(str(error))
        messages.error(request, str(error))
        return redirect('admin:ranks_rankgraveyard_changelist')
    if request.GET.get('archive_map'):
        return render(request, 'admin/ranks/rankgraveyard/_preview_table.html', {
            'preview': action['preview'],
            'table_heading': request.GET['archive_map'],
            'table_entries': action['preview']['entries'],
        })
    context = admin_context(model_admin, request, 'Records Action')
    context.update({'action': action, 'restore_form': RestoreForm()})
    return render(request, 'admin/ranks/rankgraveyard/detail.html', context)


def restore_action(request, model_admin, action_id):
    require_superuser(request)
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required.')
    form = RestoreForm(request.POST)
    if not form.is_valid():
        try:
            action = get_action_overview(action_id)
        except (DatabaseError, RankGraveyardError) as error:
            messages.error(request, str(error))
            return redirect('admin:ranks_rankgraveyard_changelist')
        context = admin_context(model_admin, request, 'Records Action')
        context.update({'action': action, 'restore_form': form})
        return render(request, 'admin/ranks/rankgraveyard/detail.html', context)
    try:
        actor_id = request.user.pk
        actor_name = request.user.get_username()
        reason = form.cleaned_data['reason']
        if form.cleaned_data['target']:
            selected = load_target(form.cleaned_data['target'])
            if selected.get('action_id') != str(action_id):
                raise RankGraveyardError('The restore target does not belong to this action.')
            restore_entry(action_id, selected['target'], actor_id, actor_name, reason)
        else:
            restore(action_id, actor_id, actor_name, reason)
    except (DatabaseError, RankGraveyardError) as error:
        messages.error(request, str(error))
    else:
        messages.success(request, 'The selected records were restored to the live database.')
    return redirect('admin:ranks_rankgraveyard_detail', action_id=action_id)


def sign_save_target(target):
    return signing.dumps(target, salt=save_token_salt, compress=True)


def add_save_display_times(save):
    save['saved_at_display'] = datetime.fromisoformat(save['timestamp'])
    save['deleted_at_display'] = (
        datetime.fromisoformat(save['deleted_at']) if save.get('deleted_at') else None
    )
    if 'game_uuid' not in save:
        save['game_uuid'] = save_details(save['savegame'])[1]


def load_save_target(value):
    try:
        target = signing.loads(value, salt=save_token_salt, max_age=3600)
    except signing.BadSignature as error:
        raise RankGraveyardError(
            'The save selection expired. Search for the deleted save again.'
        ) from error
    required = {
        'source_file', 'start_position', 'stop_position', 'map_name', 'code',
        'payload_hash',
    }
    if (
        not isinstance(target, dict)
        or set(target) != required
        or not isinstance(target['source_file'], str)
        or not isinstance(target['start_position'], int)
        or not isinstance(target['stop_position'], int)
        or target['start_position'] < 0
        or target['stop_position'] <= target['start_position']
        or not isinstance(target['map_name'], str)
        or not isinstance(target['code'], str)
        or not isinstance(target['payload_hash'], str)
    ):
        raise RankGraveyardError('The selected save reference is invalid.')
    return target


def save_index(request, model_admin):
    require_superuser(request)
    active_tab = 'history' if request.GET.get('tab') == 'history' else 'recovery'
    search_requested = request.GET.get('submit') == 'save'
    history_requested = request.GET.get('submit') == 'history'
    search_form = SaveSearchForm(request.GET if search_requested else None, prefix='save')
    history_form = HistorySearchForm(
        request.GET if history_requested else None, prefix='history'
    )
    history_form.fields.pop('game_id')
    history_form.fields.pop('action_type')
    results = []
    result_count = 0
    actions = []
    error = None
    try:
        schema_ready()
        if search_requested and search_form.is_valid():
            results, result_count = search_deleted_saves(search_form.cleaned_data)
            for result in results:
                add_save_display_times(result)
                result['token'] = sign_save_target(candidate_target(result))
                result['status'], result['state_key'] = save_status(result)
        if active_tab == 'history':
            if not history_requested or history_form.is_valid():
                history_data = history_form.cleaned_data if history_requested else {}
                actions = list_actions(dict(history_data, control='save'))
    except (DatabaseError, RankGraveyardError) as caught:
        error = str(caught)
    context = admin_context(model_admin, request, 'Savegame Control')
    context.update({
        'active_tab': active_tab,
        'search_form': search_form,
        'history_form': history_form,
        'results': results,
        'result_count': result_count,
        'actions': actions,
        'error': error,
    })
    return render(request, 'admin/ranks/savecontrol/change_list.html', context)


def save_review(request, model_admin):
    require_superuser(request)
    token = request.GET.get('target')
    if not token:
        return HttpResponseBadRequest('Missing save target.')
    try:
        save = load_candidate(load_save_target(token))
        add_save_display_times(save)
        status, state_key = save_status(save)
        similar_saves, similar_saves_truncated = live_similar_saves(save)
    except (DatabaseError, RankGraveyardError) as error:
        messages.error(request, str(error))
        return redirect('admin:ranks_savecontrol_changelist')
    context = admin_context(model_admin, request, 'Confirm Save Recovery')
    context.update({
        'save': save,
        'status': status,
        'state_key': state_key,
        'similar_saves': similar_saves,
        'similar_saves_truncated': similar_saves_truncated,
        'form': SaveRestoreForm(initial={'target': token}),
    })
    return render(request, 'admin/ranks/savecontrol/review.html', context)


def save_restore(request, model_admin):
    require_superuser(request)
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required.')
    form = SaveRestoreForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Enter a reason for the save recovery.')
        return redirect('admin:ranks_savecontrol_changelist')
    try:
        save = load_candidate(load_save_target(form.cleaned_data['target']))
        action_id = restore_save(
            save, request.user.pk, request.user.get_username(),
            form.cleaned_data['reason'],
        )
    except (DatabaseError, RankGraveyardError) as error:
        messages.error(request, str(error))
        return redirect('admin:ranks_savecontrol_changelist')
    messages.success(request, 'The deleted save was restored to the live database.')
    return redirect('admin:ranks_savecontrol_detail', action_id=action_id)


def save_detail(request, model_admin, action_id):
    require_superuser(request)
    try:
        action = get_history_action(action_id)
        if action['target_type'] != 'save_restore':
            raise RankGraveyardError('This History action is not a save recovery.')
        add_save_display_times(action['details']['save'])
        action['status'], action['state_key'] = save_status(action['details']['save'])
    except (DatabaseError, RankGraveyardError) as error:
        messages.error(request, str(error))
        return redirect('admin:ranks_savecontrol_changelist')
    context = admin_context(model_admin, request, 'Save Recovery History')
    context.update({'action': action, 'restore_form': RestoreForm()})
    return render(request, 'admin/ranks/savecontrol/detail.html', context)


def save_restore_again(request, model_admin, action_id):
    require_superuser(request)
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required.')
    form = RestoreForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Enter a reason for the save recovery.')
        return redirect('admin:ranks_savecontrol_detail', action_id=action_id)
    try:
        source = get_history_action(action_id)
        if source['target_type'] != 'save_restore':
            raise RankGraveyardError('This History action is not a save recovery.')
        new_action_id = restore_save(
            source['details']['save'], request.user.pk, request.user.get_username(),
            form.cleaned_data['reason'], source_action_id=action_id,
        )
    except (DatabaseError, RankGraveyardError) as error:
        messages.error(request, str(error))
        return redirect('admin:ranks_savecontrol_detail', action_id=action_id)
    messages.success(request, 'The saved recovery data was restored again.')
    return redirect('admin:ranks_savecontrol_detail', action_id=new_action_id)
