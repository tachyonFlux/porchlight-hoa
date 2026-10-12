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
from apps.core.email import send_bulk
from apps.core.permissions import board_required
from apps.core.ratelimit import rate_limit
from apps.core.uploads import serve_private
from apps.core.utils import absolute_url

from . import services
from .forms import ClosePollForm, MeetingForm, PollForm, VoteForm
from .models import CalendarFeedToken, Meeting, Poll, PollOption, PollVote


def _notify(audience, template, context):
    emails = services.audience_users(audience).values_list("email", flat=True)
    return send_bulk(emails, template, context)


# ------------------------------------------------------------------ everyone


@login_required
def meeting_list(request):
    user = request.user
    if not (user.is_board or services.can_see(user, "residents")):
        raise PermissionDenied
    now = timezone.now()
    meetings = services.visible_meetings(user)
    open_polls = list(services.visible_polls(user).filter(status=Poll.Status.OPEN))
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
        services.calendar_for([meeting], name=meeting.title), content_type="text/calendar"
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


@rate_limit("calendar-feed", limit=120, period=3600, methods=("GET",))
def calendar_feed(request, token):
    """Subscribable feed. Calendar apps can't sign in, so the secret URL is the credential."""
    feed = CalendarFeedToken.objects.select_related("user").filter(token=token, user__is_active=True).first()
    if feed is None:
        raise Http404
    meetings = services.visible_meetings(feed.user).filter(
        starts_at__gte=timezone.now() - timedelta(days=365)
    )
    response = HttpResponse(services.calendar_for(meetings), content_type="text/calendar; charset=utf-8")
    response["Cache-Control"] = "private, max-age=900"
    return response


@login_required
def calendar_subscribe(request):
    if not (request.user.is_board or services.can_see(request.user, "residents")):
        raise PermissionDenied
    if request.method == "POST":
        CalendarFeedToken.for_user(request.user, regenerate=True)
        messages.success(request, "New calendar link created. The old link no longer works.")
        return redirect("meetings:calendar")
    feed = CalendarFeedToken.for_user(request.user)
    https_url = absolute_url(reverse("meetings:calendar_feed", args=[feed.token]))
    webcal_url = "webcal://" + https_url.split("://", 1)[1]
    return render(request, "meetings/calendar.html", {"https_url": https_url, "webcal_url": webcal_url})


@login_required
def poll_detail(request, pk):
    poll = get_object_or_404(Poll, pk=pk)
    user = request.user
    if not services.can_see(user, poll.audience):
        raise PermissionDenied
    options = list(poll.options.all())
    existing = dict(PollVote.objects.filter(user=user, option__poll=poll).values_list("option_id", "choice"))
    form = VoteForm(request.POST or None, options=options, existing=existing)
    if request.method == "POST":
        if not poll.accepting_votes:
            messages.error(request, "This poll is closed.")
            return redirect(poll)
        if form.is_valid():
            services.record_votes(poll, user, form.choices())
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
                poll.audience,
                "meetings/email/poll_open",
                {"poll": poll, "url": absolute_url(poll.get_absolute_url())},
            )
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
        meeting = services.close_poll(
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
        if form.cleaned_data["notify"]:
            _notify(
                meeting.audience,
                "meetings/email/meeting_scheduled",
                {"meeting": meeting, "url": absolute_url(meeting.get_absolute_url())},
            )
        messages.success(request, f"Meeting scheduled for {option}.")
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
                meeting.audience,
                "meetings/email/meeting_scheduled",
                {"meeting": meeting, "url": absolute_url(meeting.get_absolute_url())},
            )
        messages.success(request, "Meeting added.")
        return redirect(meeting)
    return render(request, "meetings/meeting_form.html", {"form": form})


@board_required
def meeting_edit(request, pk):
    meeting = get_object_or_404(Meeting, pk=pk)
    form = MeetingForm(request.POST or None, request.FILES or None, instance=meeting)
    if request.method == "POST" and form.is_valid():
        old_file = meeting.minutes_file.name if "minutes_file" in form.changed_data else None
        meeting = form.save(commit=False)
        meeting.sequence = F("sequence") + 1
        meeting.save()
        meeting.refresh_from_db()
        if old_file:
            meeting.minutes_file.storage.delete(old_file)
        audit.record(request, "meetings.meeting_updated", meeting, {"fields": form.changed_data})
        if form.cleaned_data["notify"]:
            _notify(
                meeting.audience,
                "meetings/email/meeting_updated",
                {"meeting": meeting, "url": absolute_url(meeting.get_absolute_url())},
            )
        messages.success(request, "Meeting saved.")
        return redirect(meeting)
    return render(request, "meetings/meeting_form.html", {"form": form, "meeting": meeting})


@board_required
@require_POST
def meeting_cancel(request, pk):
    meeting = get_object_or_404(Meeting, pk=pk, cancelled_at__isnull=True)
    Meeting.objects.filter(pk=pk).update(cancelled_at=timezone.now(), sequence=F("sequence") + 1)
    meeting.refresh_from_db()
    audit.record(request, "meetings.meeting_cancelled", meeting)
    if request.POST.get("notify"):
        _notify(
            meeting.audience,
            "meetings/email/meeting_cancelled",
            {"meeting": meeting, "url": absolute_url(meeting.get_absolute_url())},
        )
    messages.success(request, "Meeting cancelled. Subscribed calendars will show it as cancelled.")
    return redirect(meeting)
