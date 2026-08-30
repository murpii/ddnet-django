from django.db import models


class RankGraveyard(models.Model):
    class Meta:
        managed = False
        default_permissions = ()
        verbose_name = 'Rank Control'
        verbose_name_plural = 'Rank Control'


class SaveControl(models.Model):
    class Meta:
        managed = False
        default_permissions = ()
        verbose_name = 'Savegame Control'
        verbose_name_plural = 'Savegame Control'
