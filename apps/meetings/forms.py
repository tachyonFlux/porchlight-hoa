from datetime import datetime, timedelta

from django import forms
from django.utils import timezone

from apps.core.uploads import validate_upload

from .models import Meeting, Poll, PollVote

DATETIME_WIDGET = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")
MAX_OPTIONS = 8


class PollForm(forms.ModelForm):
    """Poll details plus up to 8 candidate times (date + start + end)."""

    notify = forms.BooleanField(
        required=False, initial=True, label="Email everyone in the audience a link to vote"
    )

    class Meta:
        model = Poll
        fields = ["title", "description", "audience", "location", "closes_at"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3}), "closes_at": DATETIME_WIDGET}
        labels = {"audience": "Who votes", "closes_at": "Voting deadline (optional)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for i in range(MAX_OPTIONS):
            required = i == 0
            self.fields[f"date_{i}"] = forms.DateField(
                required=required, label="Date", widget=forms.DateInput(attrs={"type": "date"})
            )
            self.fields[f"start_{i}"] = forms.TimeField(
                required=required, label="Starts", widget=forms.TimeInput(attrs={"type": "time"})
            )
            self.fields[f"end_{i}"] = forms.TimeField(
                required=required, label="Ends", widget=forms.TimeInput(attrs={"type": "time"})
            )

    def option_rows(self):
        return [(self[f"date_{i}"], self[f"start_{i}"], self[f"end_{i}"]) for i in range(MAX_OPTIONS)]

    def clean(self):
        data = super().clean()
        options = []
        for i in range(MAX_OPTIONS):
            day, start, end = data.get(f"date_{i}"), data.get(f"start_{i}"), data.get(f"end_{i}")
            if not any((day, start, end)):
                continue
            if not all((day, start, end)):
                self.add_error(f"date_{i}", "Fill in the date, start and end time (or leave the row empty).")
                continue
            starts = timezone.make_aware(datetime.combine(day, start))
            ends = timezone.make_aware(datetime.combine(day, end))
            if ends <= starts:
                ends += timedelta(days=1)  # e.g. 11 PM – 12:30 AM
            options.append((starts, ends))
        if len(set(options)) != len(options):
            raise forms.ValidationError("Two of the times are the same.")
        if not options and not self.errors:
            raise forms.ValidationError("Add at least one possible time.")
        self.cleaned_options = sorted(options)
        return data


class VoteForm(forms.Form):
    def __init__(self, *args, options, existing, **kwargs):
        super().__init__(*args, **kwargs)
        self.options = list(options)
        for option in self.options:
            self.fields[f"option_{option.pk}"] = forms.ChoiceField(
                choices=PollVote.Choice.choices,
                widget=forms.RadioSelect,
                required=False,
                initial=existing.get(option.pk),
                label=str(option),
            )

    def choices(self):
        return {
            option.pk: self.cleaned_data[f"option_{option.pk}"]
            for option in self.options
            if self.cleaned_data.get(f"option_{option.pk}")
        }


class MeetingForm(forms.ModelForm):
    notify = forms.BooleanField(required=False, label="Email everyone who can see this meeting")
    virtual_link = forms.URLField(required=False, assume_scheme="https", label="Video call link")

    class Meta:
        model = Meeting
        fields = [
            "title",
            "kind",
            "audience",
            "starts_at",
            "ends_at",
            "location",
            "virtual_link",
            "agenda",
            "minutes_text",
            "minutes_file",
        ]
        widgets = {
            "starts_at": DATETIME_WIDGET,
            "ends_at": DATETIME_WIDGET,
            "agenda": forms.Textarea(attrs={"rows": 6}),
            "minutes_text": forms.Textarea(attrs={"rows": 8}),
        }
        help_texts = {"minutes_file": "PDF, Word, or text."}

    def clean_minutes_file(self):
        f = self.cleaned_data.get("minutes_file")
        if f and hasattr(f, "content_type"):  # a new upload, not the existing file
            validate_upload(f, allowed=("pdf", "docx", "txt", "md"))
        return f

    def clean(self):
        data = super().clean()
        if data.get("starts_at") and data.get("ends_at") and data["ends_at"] <= data["starts_at"]:
            self.add_error("ends_at", "The meeting has to end after it starts.")
        return data


class ClosePollForm(forms.Form):
    option = forms.ChoiceField(label="Meeting time", widget=forms.RadioSelect)
    kind = forms.ChoiceField(choices=Meeting.Kind.choices, initial=Meeting.Kind.GENERAL)
    virtual_link = forms.URLField(required=False, assume_scheme="https", label="Video call link (optional)")
    agenda = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))
    notify = forms.BooleanField(required=False, initial=True, label="Email everyone the meeting details")

    def __init__(self, *args, tallies, suggested=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["option"].choices = [
            (t.option.pk, f"{t.option}  ·  {t.yes} yes, {t.maybe} maybe, {t.no} no") for t in tallies
        ]
        if suggested:
            self.fields["option"].initial = suggested.pk
