from django import template

from apps.core.money import format_money

register = template.Library()


@register.filter
def money(cents):
    return format_money(cents)


@register.filter
def money_signed(cents):
    return format_money(cents, signed=True)


@register.filter
def neg(value):
    return -int(value or 0)


@register.filter
def get_item(mapping, key):
    return (mapping or {}).get(key, [])


@register.filter
def field(form, name):
    return form[name]


@register.filter
def split(value, sep=" "):
    return value.split(sep)
