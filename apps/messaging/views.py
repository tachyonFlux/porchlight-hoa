from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.community.models import Residency
from apps.core import audit
from apps.core.email import bulk_email_allowed
from apps.core.permissions import board_required
from apps.core.utils import is_int
from apps.meetings.services import can_see

from . import services
from .forms import AnnouncementForm, MessageForm, NoticeForm, ReplyForm, ResolveForm, ThreadForm
from .models import Announcement, BoardThread, CourtesyNotice
from .notice_templates import TEMPLATES

# ------------------------------------------------------------------ announcements


@login_required
def announcement_list(request):
    if not can_see(request.user, "residents"):
        raise PermissionDenied
    items = services.visible_announcements(request.user)
    return render(request, "messaging/announcement_list.html", {"items": items[:100]})


@login_required
def announcement_detail(request, pk):
    item = get_object_or_404(services.visible_announcements(request.user), pk=pk)
    return render(request, "messaging/announcement_detail.html", {"item": item})


@board_required
def announcement_create(request):
    form = AnnouncementForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.created_by = request.user
        item.save()
        audit.record(request, "messaging.announcement_posted", item)
        if form.cleaned_data["send_email"]:
            if bulk_email_allowed(request.user):
                count = services.email_announcement(item, request)
                messages.info(request, f"Emailing {count} people.")
            else:
                messages.error(
                    request, "Posted, but not emailed: you've reached today's limit for mass emails."
                )
        messages.success(request, "Announcement posted.")
        return redirect(item)
    return render(request, "messaging/announcement_form.html", {"form": form})


@board_required
def announcement_edit(request, pk):
    item = get_object_or_404(Announcement, pk=pk)
    form = AnnouncementForm(request.POST or None, instance=item, initial={"send_email": False})
    if request.method == "POST":
        if "delete" in request.POST:
            audit.record(request, "messaging.announcement_deleted", item)
            item.delete()
            messages.success(request, "Announcement deleted.")
            return redirect("messaging:announcements")
        if form.is_valid():
            form.save()
            audit.record(request, "messaging.announcement_edited", item)
            if form.cleaned_data["send_email"]:
                if bulk_email_allowed(request.user):
                    count = services.email_announcement(item, request)
                    messages.info(request, f"Emailing {count} people.")
                else:
                    messages.error(
                        request, "Saved, but not emailed: you've reached today's limit for mass emails."
                    )
            messages.success(request, "Announcement saved.")
            return redirect(item)
    return render(request, "messaging/announcement_form.html", {"form": form, "item": item})


# ------------------------------------------------------------------ courtesy notices


@login_required
def notice_list(request):
    user = request.user
    if user.is_board and request.GET.get("mine") != "1":
        notices = CourtesyNotice.objects.select_related("home")
        status = request.GET.get("status", "open")
        if status in CourtesyNotice.Status.values:
            notices = notices.filter(status=status)
        scope = "all"
    else:
        homes = Residency.objects.current().filter(user=user).values("home_id")
        notices = CourtesyNotice.objects.select_related("home").filter(home_id__in=homes)
        status, scope = "", "mine"
    return render(
        request, "messaging/notice_list.html", {"notices": notices, "status": status, "scope": scope}
    )


@board_required
def notice_create(request):
    key = request.GET.get("template", "")
    initial = {}
    if key in TEMPLATES:
        initial["subject"], initial["body"] = TEMPLATES[key]
    if is_int(request.GET.get("home", "")):
        initial["home"] = int(request.GET["home"])
    form = NoticeForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        notice = form.save(commit=False)
        notice.created_by = request.user
        notice.save()
        audit.record(request, "messaging.notice_sent", notice, {"home": str(notice.home)})
        if form.cleaned_data["send_email"]:
            if not services.notice_email_allowed(notice.home):
                messages.error(
                    request, "Saved, but not emailed: this home already got several notices today."
                )
                return redirect(notice)
            count = services.send_notice(notice, request)
            if not count:
                messages.warning(request, "Nobody at this home has an account yet, so no email was sent.")
        messages.success(request, "Courtesy notice saved.")
        return redirect(notice)
    return render(
        request, "messaging/notice_form.html", {"form": form, "templates": TEMPLATES, "chosen": key}
    )


def _get_notice(request, pk):
    notice = get_object_or_404(CourtesyNotice.objects.select_related("home"), pk=pk)
    if not services.can_view_notice(request.user, notice):
        raise PermissionDenied
    return notice


@login_required
def notice_detail(request, pk):
    notice = _get_notice(request, pk)
    return render(
        request,
        "messaging/notice_detail.html",
        {
            "notice": notice,
            "replies": notice.replies.select_related("author"),
            "reply_form": ReplyForm(),
            "resolve_form": ResolveForm(),
        },
    )


@login_required
@require_POST
def notice_reply(request, pk):
    notice = _get_notice(request, pk)
    form = ReplyForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Please write a reply (up to 4,000 characters).")
        return redirect(notice)
    try:
        services.reply_to_notice(notice, request.user, form.cleaned_data["body"])
        messages.success(request, "Reply sent. Thank you!")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect(notice)


@board_required
@require_POST
def notice_resolve(request, pk):
    notice = get_object_or_404(CourtesyNotice, pk=pk)
    action = request.POST.get("action")
    if action == "resolve":
        services.set_notice_status(
            notice, request.user, CourtesyNotice.Status.RESOLVED, request.POST.get("note", "")[:300], request
        )
        messages.success(request, "Marked resolved.")
    elif action == "reopen":
        services.set_notice_status(notice, request.user, CourtesyNotice.Status.OPEN, request=request)
        messages.success(request, "Reopened.")
    return redirect(notice)


# ------------------------------------------------------------------ board room


@board_required
def board_room(request):
    return render(
        request, "messaging/board_room.html", {"threads": services.threads_with_unread(request.user)}
    )


@board_required
def thread_create(request):
    form = ThreadForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        thread = services.start_thread(request.user, d["subject"], d["body"], notify=d["notify"])
        return redirect(thread)
    return render(request, "messaging/thread_form.html", {"form": form})


@board_required
def thread_detail(request, pk):
    thread = get_object_or_404(BoardThread, pk=pk)
    form = MessageForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        services.post_message(
            thread, request.user, form.cleaned_data["body"], notify=form.cleaned_data["notify"]
        )
        return redirect(thread)
    thread_messages = list(thread.messages.select_related("author"))
    if thread_messages:
        services.mark_read(thread, request.user, thread_messages[-1].created_at)
    return render(
        request,
        "messaging/thread_detail.html",
        {"thread": thread, "thread_messages": thread_messages, "form": form},
    )
