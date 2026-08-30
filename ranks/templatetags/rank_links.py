from django import template


register = template.Library()


@register.filter
def player_profile_url(name):
    slug_characters = "\t !\"#$%&'()*\\-/<=>?@[\\]^_`{|},.:]+"
    slug = ''.join(
        f'-{ord(character)}-'
        if character in slug_characters or ord(character) >= 128
        else character
        for character in str(name)
    )
    return f'/players/{slug}/'
