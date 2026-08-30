from datetime import timedelta

from django import forms
from django.utils import timezone


def leaderboard_depth_choices():
    return tuple((str(depth), f'Top {depth} Players') for depth in (50, 100, 250, 500, 1000))


def checkpoint_margin_choices():
    return (
        ('0', 'Exact'),
        ('1', '1 Tick (0.02 Seconds)'),
        ('2', '2 Ticks (0.04 Seconds)'),
        ('5', '5 Ticks (0.10 Seconds)'),
    )


def map_rank_limit_choices():
    return (
        ('50', 'Top 50'),
        ('100', 'Top 100'),
        ('250', 'Top 250'),
        ('500', 'Top 500'),
        ('all', 'All Players'),
    )


class RankSearchForm(forms.Form):
    map_name = forms.CharField(
        label='Map',
        max_length=128,
        required=False,
        widget=forms.TextInput(attrs={'list': 'rank-map-options'}),
    )
    player_name = forms.CharField(label='Player', max_length=16, required=False)
    time = forms.FloatField(label='Time', required=False)
    finished_on = forms.DateField(
        label='Finished On',
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    game_id = forms.CharField(label='GameID', max_length=64, required=False)
    map_rank_limit = forms.ChoiceField(
        label='Map Results',
        choices=map_rank_limit_choices(),
        initial='100',
        required=False,
        help_text='Used only for map-only searches.',
    )

    def clean(self):
        cleaned_data = super().clean()
        limit = cleaned_data.get('map_rank_limit') or '100'
        cleaned_data['map_rank_limit'] = None if limit == 'all' else int(limit)
        has_map = bool(cleaned_data.get('map_name'))
        has_player = bool(cleaned_data.get('player_name'))
        if not has_map and not has_player:
            raise forms.ValidationError('Enter a map or player.')
        return cleaned_data


class HistorySearchForm(forms.Form):
    query = forms.CharField(label='Map, Player, Or Admin', max_length=128, required=False)
    game_id = forms.CharField(label='GameID', max_length=64, required=False)
    action_type = forms.ChoiceField(
        label='Type',
        required=False,
        choices=(
            ('', 'All'),
            ('rank', 'Normal Rank'),
            ('team', 'Team Rank'),
            ('player', 'Player'),
            ('checkpoint', 'Checkpoint Scan'),
        ),
    )
    state = forms.ChoiceField(
        required=False,
        choices=(
            ('', 'All'),
            ('active', 'Outside Live Database'),
            ('restored', 'In Live Database'),
        ),
    )


class CheckpointScanForm(forms.Form):
    source_rank = forms.CharField(required=False, widget=forms.HiddenInput)
    map_name = forms.CharField(
        label='Map',
        max_length=128,
        required=False,
        widget=forms.TextInput(attrs={'list': 'rank-map-options'}),
    )
    player_name = forms.CharField(label='Player', max_length=16, required=False)
    mode = forms.ChoiceField(
        label='Match Mode',
        choices=(('range', 'Checkpoint Range'), ('single', 'Selected CP Only')),
    )
    start_checkpoint = forms.IntegerField(
        label='Starting CP', min_value=1, max_value=25, initial=1
    )
    end_checkpoint = forms.IntegerField(
        label='Ending CP', min_value=1, max_value=25, initial=5, required=False
    )
    minimum_players = forms.IntegerField(
        label='Minimum Players', min_value=2, max_value=100, initial=4,
        help_text='The number of distinct players who must match the selected checkpoints within the chosen margin.',
    )
    leaderboard_depth = forms.ChoiceField(
        label='Leaderboard Depth',
        choices=leaderboard_depth_choices(),
        initial='100',
        required=False,
        help_text='For player scans, check each map\'s fastest finish from this many distinct players.',
    )
    checkpoint_margin = forms.ChoiceField(
        label='Checkpoint Margin',
        choices=checkpoint_margin_choices(),
        initial='0',
        required=False,
        help_text='Used only for player scans. Each selected checkpoint may differ by this much in either direction.',
    )

    def clean(self):
        cleaned_data = super().clean()
        cleaned_data['leaderboard_depth'] = int(
            cleaned_data.get('leaderboard_depth') or 100
        )
        cleaned_data['checkpoint_margin'] = int(
            cleaned_data.get('checkpoint_margin') or 0
        )
        if not cleaned_data.get('map_name') and not cleaned_data.get('player_name'):
            raise forms.ValidationError('Enter a map or player.')
        start = cleaned_data.get('start_checkpoint')
        end = cleaned_data.get('end_checkpoint')
        if cleaned_data.get('mode') == 'single' and start is not None:
            cleaned_data['end_checkpoint'] = start
        elif end is None:
            self.add_error('end_checkpoint', 'This field is required.')
        elif start is not None and start > end:
            self.add_error('end_checkpoint', 'Ending CP must be after or equal to Starting CP.')
        return cleaned_data


class GraveyardForm(forms.Form):
    target = forms.CharField(widget=forms.HiddenInput)
    all_records = forms.BooleanField(required=False, widget=forms.HiddenInput)
    reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={'rows': 4}))


class RestoreForm(forms.Form):
    reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={'rows': 4}))
    target = forms.CharField(required=False, widget=forms.HiddenInput)


class SaveSearchForm(forms.Form):
    map_name = forms.CharField(label='Map', max_length=128, required=False)
    player_name = forms.CharField(label='Player', max_length=16, required=False)
    code = forms.CharField(label='Save Code', max_length=128, required=False)
    game_uuid = forms.UUIDField(label='Teehistorian ID', required=False)
    deleted_after = forms.DateTimeField(
        label='Deleted After',
        widget=forms.DateTimeInput(
            format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}
        ),
    )
    deleted_before = forms.DateTimeField(
        label='Deleted Before',
        widget=forms.DateTimeInput(
            format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        now = timezone.localtime().replace(second=0, microsecond=0)
        self.fields['deleted_after'].initial = now - timedelta(days=1)
        self.fields['deleted_before'].initial = now

    def clean(self):
        cleaned_data = super().clean()
        if not any(
            cleaned_data.get(name)
            for name in ('map_name', 'player_name', 'code', 'game_uuid')
        ):
            raise forms.ValidationError(
                'Enter a map, player, save code, or Teehistorian ID.'
            )
        start = cleaned_data.get('deleted_after')
        end = cleaned_data.get('deleted_before')
        if start and end and start >= end:
            self.add_error('deleted_before', 'Deleted Before must be after Deleted After.')
        return cleaned_data


class SaveRestoreForm(forms.Form):
    target = forms.CharField(widget=forms.HiddenInput)
    reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={'rows': 4}))
