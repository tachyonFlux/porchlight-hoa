from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Home, HomeInvite


@receiver(post_save, sender=Home)
def create_invite_for_new_home(sender, instance, created, **kwargs):
    """Give every new home an invite code up front, so pages never need to create one."""
    if created:
        HomeInvite.active_for(instance)
