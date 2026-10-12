from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core import audit
from apps.core.email import bulk_email_allowed, send_bulk
from apps.core.permissions import board_required
from apps.core.ratelimit import hit, too_many
from apps.core.uploads import serve_private
from apps.core.utils import absolute_url, client_ip

from . import services
from .forms import ClosePollForm, MeetingForm, PollForm, VoteForm
from .models import CalendarFeedToken, Meeting, Poll, PollOption, PollVote


def _notify(request, audience, template, context, obj):
    """Mass email the audience, within the per-person daily cap, and audit it."""
    if not bulk_email_allowed(request.user):
        messages.error(request, "Saved, but no email went out: you've reached today's limit for mass emails.")
        return 0
    emails = services.audience_users(audience).values_list("email", flat=True)
    count = send_bulk(emails, template, context)
    audit.record(
        request, "meetings.emailed", obj, {"template": template.rsplit("/", 1)[-1], "recipients": count}
    )
    return count


# ------------------------------------------------------------------ everyone


@login_required
def meeting_list(request):
    user = request.user
    if not (user.is_board or services.can_see(user, "residents")):
        raise PermissionDenied
    now = timezone.now()
    meetings = services.visible_meetings(user)
    open_polls = [
        p for p in services.visible_polls(user).filter(status=Poll.Status.OPEN) if p.accepting_votes
    ]
    voted = set(
        PollVote.objects.filter(user=user, option__poll__in=open_polls).values_list(
            "option__poll_id", flat=True
        )
    )
    return render(
        request,
        "meetings/meeting_list.html",
        {
            "upcoming": meetings.filter(ends_at__gte=now).order_by("starts_at"),
            "past": meetings.filter(ends_at__lt=now).order_by("-starts_at")[:30],
            "open_polls": open_polls,
            "voted": voted,
        },
    )


def _get_visible_meeting(request, pk):
    meeting = get_object_or_404(Meeting, pk=pk)
    if not services.can_see(request.user, meeting.audience):
        raise PermissionDenied
    return meeting


@login_required
def meeting_detail(request, pk):
    meeting = _get_visible_meeting(request, pk)
    return render(request, "meetings/meeting_detail.html", {"meeting": meeting})


@login_required
def meeting_ics(request, pk):
    meeting = _get_visible_meeting(request, pk)
    response = HttpResponse(
        services.calendar_for([meeting], name=meeting.title), content_type="text/calendar; charset=utf-8"
    )
    response["Content-Disposition"] = f'attachment; filename="meeting-{meeting.pk}.ics"'
    return response


@login_required
def minutes_file(request, pk):
    meeting = _get_visible_meeting(request, pk)
    if not meeting.minutes_file:
        raise Http404
    ext = meeting.minutes_file.name.rsplit(".", 1)[-1]
    return serve_private(
        meeting.minutes_file, f"minutes-{timezone.localtime(meeting.starts_at):%Y-%m-%d}.{ext}"
    )


def calendar_feed(request, token):
    """Subscribable feed. Calendar apps can't sign in, so the secret URL is the credential.

    Only failed lookups are rate limited: Google/Outlook fetch every subscriber's feed from
    a few shared IPs, and the token's entropy already rules out guessing.
    """
    feed = CalendarFeedToken.objects.select_related("user").filter(token=token, user__is_active=True).first()
    if feed is None:
        if not hit("calendar-feed-miss", client_ip(request), limit=30, period=3600):
            return too_many(request)
        raise Http404
    meetings = services.feed_meetings(feed).filter(starts_at__gte=timezone.now() - timedelta(days=365))
    response = HttpResponse(services.calendar_for(meetings), content_type="text/calendar; charset=utf-8")
    response["Cache-Control"] = "private, max-age=900"
    return response


@login_required
def calendar_subscribe(request):
    user = request.user
    if not (user.is_board or services.can_see(user, "residents")):
        raise PermissionDenied
    if request.method == "POST":
        if "include_board" in request.POST and user.is_board:
            feed = CalendarFeedToken.for_user(user, regenerate=True)
            feed.include_board = request.POST["include_board"] == "1"
            feed.save(update_fields=["include_board"])
            messages.success(request, "Calendar link updated. Re-subscribe with the new link below.")
        else:
            CalendarFeedToken.for_user(user, regenerate=True)
            messages.success(request, "New calendar link created. The old link no longer works.")
        return redirect("meetings:calendar")
    feed = CalendarFeedToken.for_user(user)
    https_url = absolute_url(reverse("meetings:calendar_feed", args=[feed.token]))
    webcal_url = "webcal://" + https_url.split("://", 1)[1]
    return render(
        request, "meetings/calendar.html", {"https_url": https_url, "webcal_url": webcal_url, "feed": feed}
    )


@login_required
def poll_detail(request, pk):
    poll = get_object_or_404(Poll, pk=pk)
    user = request.user
    if not services.can_see(user, poll.audience):
        raise PermissionDenied
    options = list(poll.options.all())
    existing = dict(PollVote.objects.filter(user=user, option__poll=poll).values_list("option_id", "choice"))
    form = VoteForm(request.POST if request.method == "POST" else None, options=options, existing=existing)
    if request.method == "POST":
        if not poll.accepting_votes:
            messages.error(request, "This poll is closed.")
            return redirect(poll)
        if form.is_valid():
            choices = form.choices()
            if not choices:
                messages.warning(request, "Pick an answer for at least one of the times.")
                return redirect(poll)
            services.record_votes(poll, user, choices)
            messages.success(
                request, "Thanks! Your availability is saved. You can change it until the poll closes."
            )
            return redirect(poll)
    tallies = services.tally(poll)
    voters = None
    if user.is_board:
        voters = {}
        for vote in PollVote.objects.filter(option__poll=poll).select_related("user"):
            voters.setdefault(vote.option_id, []).append(vote)
    return render(
        request,
        "meetings/poll_detail.html",
        {
            "poll": poll,
            "form": form,
            "rows": [(t, form[f"option_{t.option.pk}"]) for t in tallies],
            "voters": voters,
            "voter_count": services.voter_count(poll),
            "suggested": services.suggested_option(poll),
            "meeting": getattr(poll, "meeting", None),
        },
    )


# ------------------------------------------------------------------ board


@board_required
def poll_create(request):
    form = PollForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            poll = form.save(commit=False)
            poll.created_by = request.user
            poll.save()
            PollOption.objects.bulk_create(
                PollOption(poll=poll, starts_at=s, ends_at=e) for s, e in form.cleaned_options
            )
            audit.record(request, "meetings.poll_created", poll)
        if form.cleaned_data["notify"]:
            count = _notify(
                request,
                poll.audience,
                "meetings/email/poll_open",
                {"poll": poll, "url": absolute_url(poll.get_absolute_url())},
                poll,
            )
            if count:
                messages.info(request, f"Emailing {count} people a link to vote.")
        messages.success(request, "Poll created.")
        return redirect(poll)
    return render(request, "meetings/poll_form.html", {"form": form})


@board_required
def poll_close(request, pk):
    poll = get_object_or_404(Poll, pk=pk)
    if poll.status == Poll.Status.CLOSED:
        return redirect(poll)
    tallies = services.tally(poll)
    form = ClosePollForm(request.POST or None, tallies=tallies, suggested=services.suggested_option(poll))
    if request.method == "POST" and form.is_valid():
        option = get_object_or_404(PollOption, pk=form.cleaned_data["option"], poll=poll)
        meeting, created = services.close_poll(
            poll,
            option,
            {
                "kind": form.cleaned_data["kind"],
                "virtual_link": form.cleaned_data["virtual_link"],
                "agenda": form.cleaned_data["agenda"],
            },
            actor=request.user,
            request=request,
        )
        if created and form.cleaned_data["notify"]:
            _notify(
                request,
                meeting.audience,
                "meetings/email/meeting_scheduled",
                {"meeting": meeting, "url": absolute_url(meeting.get_absolute_url())},
                meeting,
            )
        messages.success(request, f"Meeting scheduled for {meeting.poll.chosen_option}.")
        return redirect(meeting)
    return render(request, "meetings/poll_close.html", {"poll": poll, "form": form, "tallies": tallies})


@board_required
@require_POST
def poll_delete(request, pk):
    poll = get_object_or_404(Poll, pk=pk, status=Poll.Status.OPEN)
    audit.record(request, "meetings.poll_deleted", poll)
    poll.delete()
    messages.success(request, "Poll deleted.")
    return redirect("meetings:list")


@board_required
def meeting_create(request):
    form = MeetingForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        meeting = form.save(commit=False)
        meeting.created_by = request.user
        meeting.save()
        audit.record(request, "meetings.meeting_created", meeting)
        if form.cleaned_data["notify"]:
            _notify(
                request,
                meeting.audience,
                "meetings/email/meeting_scheduled",
                {"meeting": meeting, "url": absolute_url(meeting.get_absolute_url())},
                meeting,
            )
        messages.success(request, "Meeting added.")
        return redirect(meeting)
    return render(request, "meetings/meeting_form.html", {"form": form})


@board_required
def meeting_edit(request, pk):
    meeting = get_object_or_404(Meeting, pk=pk)
    # Capture the stored path now: validating the form overwrites it with the new upload.
    old_file = meeting.minutes_file.name
    form = MeetingForm(request.POST or None, request.FILES or None, instance=meeting)
    if request.method == "POST" and form.is_valid():
        meeting = form.save(commit=False)
        meeting.sequence = F("sequence") + 1
        meeting.save()
        meeting.refresh_from_db()
        if old_file and old_file != meeting.minutes_file.name:
            storage = meeting.minutes_file.storage
            transaction.on_commit(lambda: storage.delete(old_file))
        audit.record(request, "meetings.meeting_updated", meeting, {"fields": form.changed_data})
        if form.cleaned_data["notify"]:
            _notify(
                request,
                meeting.audience,
                "meetings/email/meeting_updated",
                {"meeting": meeting, "url": absolute_url(meeting.get_absolute_url())},
                meeting,
            )
        messages.success(request, "Meeting saved.")
        return redirect(meeting)
    return render(request, "meetings/meeting_form.html", {"form": form, "meeting": meeting})


@board_required
@require_POST
def meeting_cancel(request, pk):
    meeting = get_object_or_404(Meeting, pk=pk, cancelled_at__isnull=True)
    now = timezone.now()
    Meeting.objects.filter(pk=pk).update(cancelled_at=now, updated_at=now, sequence=F("sequence") + 1)
    meeting.refresh_from_db()
    audit.record(request, "meetings.meeting_cancelled", meeting)
    if request.POST.get("notify"):
        _notify(
            request,
            meeting.audience,
            "meetings/email/meeting_cancelled",
            {"meeting": meeting, "url": absolute_url(meeting.get_absolute_url())},
            meeting,
        )
    messages.success(request, "Meeting cancelled. Subscribed calendars will show it as cancelled.")
    return redirect(meeting)
