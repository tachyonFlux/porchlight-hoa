from django import forms

from apps.community.models import Home

from .models import Announcement, CourtesyNotice


class AnnouncementForm(forms.ModelForm):
    send_email = forms.BooleanField(
        required=False, initial=True, label="Also email it to everyone in the audience"
    )

    class Meta:
        model = Announcement
        fields = ["title", "body", "audience", "pinned"]
        widgets = {"body": forms.Textarea(attrs={"rows": 8})}
        labels = {"audience": "Who can see it"}


class NoticeForm(forms.ModelForm):
    send_email = forms.BooleanField(required=False, initial=True, label="Email the household")

    class Meta:
        model = CourtesyNotice
        fields = ["home", "subject", "body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 7})}
        help_texts = {"body": "Keep it friendly. Assume good intent; most things are simple oversights."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["home"].queryset = Home.objects.filter(is_active=True)


class ReplyForm(forms.Form):
    body = forms.CharField(label="Reply", widget=forms.Textarea(attrs={"rows": 3}), max_length=4000)


class ResolveForm(forms.Form):
    note = forms.CharField(label="Note (optional)", required=False, max_length=300)


class ThreadForm(forms.Form):
    subject = forms.CharField(max_length=150)
    body = forms.CharField(label="Message", widget=forms.Textarea(attrs={"rows": 6}), max_length=10000)
    notify = forms.BooleanField(required=False, initial=True, label="Email the other board members")


class MessageForm(forms.Form):
    body = forms.CharField(label="Reply", widget=forms.Textarea(attrs={"rows": 4}), max_length=10000)
    notify = forms.BooleanField(required=False, label="Email the other board members")
