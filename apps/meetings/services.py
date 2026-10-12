"""Polls, meetings and calendar output."""

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from icalendar import Calendar, Event

from apps.accounts.models import board_members
from apps.community.models import Residency
from apps.core import audit
from apps.core.models import HOASettings
from apps.core.utils import absolute_url

from .models import Audience, Meeting, Poll, PollOption, PollVote

User = get_user_model()


# ------------------------------------------------------------------ audiences


def board_users():
    return board_members()


def resident_users():
    return User.objects.filter(is_active=True, residencies__in=Residency.objects.current()).distinct()


def audience_users(audience):
    if audience == Audience.BOARD:
        return board_users()
    return User.objects.filter(is_active=True).filter(
        Q(pk__in=board_users().values("pk")) | Q(pk__in=resident_users().values("pk"))
    )


def can_see(user, audience):
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_board:
        return True
    return audience == Audience.RESIDENTS and Residency.objects.current().filter(user=user).exists()


def feed_meetings(feed):
    """What a calendar subscription shows. Board-only meetings need an explicit opt-in."""
    user = feed.user
    qs = visible_meetings(user)
    if user.is_board and not feed.include_board:
        qs = qs.filter(audience=Audience.RESIDENTS)
    return qs


def visible_meetings(user):
    qs = Meeting.objects.all()
    if user.is_board:
        return qs
    if can_see(user, Audience.RESIDENTS):
        return qs.filter(audience=Audience.RESIDENTS)
    return qs.none()


def visible_polls(user):
    qs = Poll.objects.all()
    if user.is_board:
        return qs
    if can_see(user, Audience.RESIDENTS):
        return qs.filter(audience=Audience.RESIDENTS)
    return qs.none()


# ------------------------------------------------------------------ polls


@dataclass
class OptionTally:
    option: PollOption
    yes: int = 0
    maybe: int = 0
    no: int = 0

    @property
    def score(self):
        return self.yes * 2 + self.maybe

    @property
    def sort_key(self):
        # Best score first, then most outright yeses, then earliest.
        return (-self.score, -self.yes, self.option.starts_at, self.option.pk)


def tally(poll):
    options = list(poll.options.all())
    tallies = {o.pk: OptionTally(o) for o in options}
    for vote in PollVote.objects.filter(option__poll=poll):
        t = tallies[vote.option_id]
        setattr(t, vote.choice, getattr(t, vote.choice) + 1)
    return [tallies[o.pk] for o in options]


def suggested_option(poll):
    tallies = [t for t in tally(poll) if t.yes or t.maybe]
    if not tallies:
        return None
    return min(tallies, key=lambda t: t.sort_key).option


def voter_count(poll):
    return PollVote.objects.filter(option__poll=poll).values("user").distinct().count()


def record_votes(poll, user, choices):
    """choices: {option_id: 'yes'|'maybe'|'no'}. Unknown options are ignored."""
    if not poll.accepting_votes:
        raise ValueError("This poll is closed.")
    valid = set(poll.options.values_list("pk", flat=True))
    with transaction.atomic():
        for option_id, choice in choices.items():
            if option_id not in valid or choice not in PollVote.Choice.values:
                continue
            PollVote.objects.update_or_create(option_id=option_id, user=user, defaults={"choice": choice})


@transaction.atomic
def close_poll(poll, option, meeting_fields, actor=None, request=None):
    """Close the poll on `option` and create the meeting. Returns (meeting, created).

    Idempotent per poll: a second (e.g. double-clicked) close returns the existing meeting
    with created=False, so callers don't email everyone twice.
    """
    poll = Poll.objects.select_for_update().get(pk=poll.pk)
    existing = Meeting.objects.filter(poll=poll).first()
    if existing:
        return existing, False
    if option.poll_id != poll.pk:
        raise ValueError("That time isn't part of this poll.")
    poll.status = Poll.Status.CLOSED
    poll.closed_at = timezone.now()
    poll.chosen_option = option
    poll.save(update_fields=["status", "closed_at", "chosen_option"])
    meeting = Meeting.objects.create(
        poll=poll,
        starts_at=option.starts_at,
        ends_at=option.ends_at,
        audience=poll.audience,
        created_by=actor,
        **{"title": poll.title, "location": poll.location, **meeting_fields},
    )
    audit.record(request or actor, "meetings.poll_closed", poll, {"meeting": meeting.pk, "time": str(option)})
    return meeting, True


# ------------------------------------------------------------------ calendar


def _event(meeting):
    event = Event()
    event.add("uid", f"{meeting.uid}@porchlight")
    event.add("dtstamp", meeting.updated_at or timezone.now())
    event.add("dtstart", meeting.starts_at)
    event.add("dtend", meeting.ends_at)
    event.add("summary", meeting.title)
    event.add("sequence", meeting.sequence)
    url = absolute_url(meeting.get_absolute_url())
    event.add("url", url)
    description = []
    if meeting.virtual_link:
        description.append(f"Join online: {meeting.virtual_link}")
    if meeting.agenda:
        description.append(f"Agenda:\n{meeting.agenda}")
    description.append(f"Details: {url}")
    event.add("description", "\n\n".join(description))
    if meeting.location:
        event.add("location", meeting.location)
    elif meeting.virtual_link:
        event.add("location", meeting.virtual_link)
    if meeting.cancelled_at:
        event.add("status", "CANCELLED")
    else:
        event.add("status", "CONFIRMED")
    return event


def calendar_for(meetings, name=None):
    hoa = HOASettings.load()
    cal = Calendar()
    cal.add("prodid", "-//Porchlight//HOA meetings//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", name or f"{hoa.display_name} meetings")
    cal.add("x-published-ttl", "PT6H")
    for meeting in meetings:
        cal.add_component(_event(meeting))
    return cal.to_ical()
