from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core import audit
from apps.core.permissions import board_required

from . import services
from .forms import CompleteForm, DeadlineForm
from .models import Deadline


@board_required
def deadline_list(request):
    open_items = list(Deadline.objects.filter(completed_at__isnull=True).select_related("owner"))
    done = (
        Deadline.objects.filter(completed_at__isnull=False)
        .select_related("completed_by")
        .order_by("-completed_at")[:30]
    )
    return render(
        request,
        "compliance/deadline_list.html",
        {
            "overdue": [d for d in open_items if d.urgency == "overdue"],
            "soon": [d for d in open_items if d.urgency == "soon"],
            "later": [d for d in open_items if d.urgency == "later"],
            "done": done,
            "complete_form": CompleteForm(),
            "has_any": Deadline.objects.exists(),
        },
    )


@board_required
def deadline_create(request):
    form = DeadlineForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        deadline = form.save()
        audit.record(request, "compliance.created", deadline)
        messages.success(request, "Deadline added.")
        return redirect("compliance:list")
    return render(request, "compliance/deadline_form.html", {"form": form})


@board_required
def deadline_edit(request, pk):
    deadline = get_object_or_404(Deadline, pk=pk)
    form = DeadlineForm(request.POST or None, instance=deadline)
    if request.method == "POST":
        if "delete" in request.POST:
            audit.record(request, "compliance.deleted", deadline)
            deadline.delete()
            messages.success(request, "Deadline deleted.")
            return redirect("compliance:list")
        if form.is_valid():
            if {"due_date", "remind_days"} & set(form.changed_data):
                deadline.reminders.all().delete()  # new schedule, fresh reminders
            form.save()
            audit.record(request, "compliance.updated", deadline, {"fields": form.changed_data})
            messages.success(request, "Deadline saved.")
            return redirect("compliance:list")
    return render(request, "compliance/deadline_form.html", {"form": form, "deadline": deadline})


@board_required
@require_POST
def deadline_complete(request, pk):
    deadline = get_object_or_404(Deadline, pk=pk)
    form = CompleteForm(request.POST)
    note = form.cleaned_data["note"] if form.is_valid() else ""
    nxt = services.complete(deadline, request.user, note, request=request)
    if nxt:
        messages.success(
            request,
            f"Done! Next year's is scheduled for {nxt.due_date:%B} {nxt.due_date.day}, {nxt.due_date.year}.",
        )
    else:
        messages.success(request, "Marked done.")
    return redirect("compliance:list")


@board_required
@require_POST
def deadline_reopen(request, pk):
    deadline = get_object_or_404(Deadline, pk=pk)
    services.reopen(deadline, request.user, request=request)
    messages.success(request, "Reopened.")
    return redirect("compliance:list")


@board_required
@require_POST
def add_suggestions(request):
    created = services.add_suggestions(request.user, request=request)
    if created:
        messages.success(request, f"Added {len(created)} suggested deadlines. Check each date and adjust it.")
    else:
        messages.info(request, "All the suggested deadlines are already on your list.")
    return redirect("compliance:list")
