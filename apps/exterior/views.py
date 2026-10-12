from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.community.models import Home, Residency
from apps.core.uploads import serve_private

from . import services
from .forms import CommentForm, RequestForm, StatusForm
from .models import ExteriorRequest, RequestPhoto


def _my_homes(user):
    return Home.objects.filter(residencies__in=Residency.objects.current().filter(user=user), is_active=True)


def _get_viewable(request, pk):
    req = get_object_or_404(ExteriorRequest.objects.select_related("home", "submitted_by"), pk=pk)
    if not services.can_view(request.user, req):
        raise PermissionDenied
    return req


@login_required
def request_list(request):
    user = request.user
    status = request.GET.get("status", "")
    if user.is_board and request.GET.get("mine") != "1":
        reqs = ExteriorRequest.objects.select_related("home")
        scope = "all"
    else:
        reqs = ExteriorRequest.objects.select_related("home").filter(home__in=_my_homes(user))
        scope = "mine"
    if status == "open":
        reqs = reqs.filter(status__in=ExteriorRequest.OPEN_STATUSES)
    elif status in ExteriorRequest.Status.values:
        reqs = reqs.filter(status=status)
    return render(
        request,
        "exterior/request_list.html",
        {
            "reqs": reqs,
            "status": status,
            "scope": scope,
            "can_submit": _my_homes(user).exists(),
            "statuses": ExteriorRequest.Status.choices,
        },
    )


@login_required
def request_new(request):
    homes = _my_homes(request.user)
    if not homes.exists():
        raise PermissionDenied
    form = RequestForm(request.POST or None, request.FILES or None, homes=homes)
    if request.method == "POST" and form.is_valid():
        home = form.cleaned_data.get("home") or homes.first()
        req = services.submit(home, request.user, form.cleaned_data, form.cleaned_data["photos"])
        messages.success(
            request, "Thanks! Your request was sent to the board. You'll get an email when it's reviewed."
        )
        return redirect(req)
    return render(request, "exterior/request_form.html", {"form": form})


@login_required
def request_detail(request, pk):
    req = _get_viewable(request, pk)
    user = request.user
    ctx = {
        "req": req,
        "events": services.visible_events(user, req),
        "photos": req.photos.all(),
        "household": services.can_act_as_household(user, req),
        "comment_form": CommentForm(show_board_only=user.is_board),
    }
    if user.is_board:
        allowed = services.BOARD_TRANSITIONS[ExteriorRequest.Status(req.status)]
        ctx["status_form"] = (
            StatusForm(choices=[(s.value, s.label) for s in ExteriorRequest.Status if s in allowed])
            if allowed
            else None
        )
    return render(request, "exterior/request_detail.html", ctx)


@login_required
@require_POST
def request_comment(request, pk):
    req = _get_viewable(request, pk)
    if req.status == ExteriorRequest.Status.WITHDRAWN:
        messages.error(request, "This request was withdrawn.")
        return redirect(req)
    form = CommentForm(request.POST, request.FILES, show_board_only=request.user.is_board)
    if form.is_valid():
        try:
            services.comment(
                req,
                request.user,
                form.cleaned_data["note"],
                board_only=form.cleaned_data.get("board_only", False),
                photos=form.cleaned_data["photos"],
            )
            messages.success(request, "Comment added.")
        except ValueError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, " ".join(str(e) for errs in form.errors.values() for e in errs))
    return redirect(req)


@login_required
@require_POST
def request_status(request, pk):
    if not request.user.is_board:
        raise PermissionDenied
    req = _get_viewable(request, pk)
    try:
        services.board_set_status(
            req, request.user, request.POST.get("status", ""), request.POST.get("note", ""), request=request
        )
        messages.success(request, "Status updated and the household has been emailed.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect(req)


@login_required
@require_POST
def request_withdraw(request, pk):
    req = _get_viewable(request, pk)
    if not services.can_act_as_household(request.user, req):
        raise PermissionDenied
    try:
        services.withdraw(req, request.user)
        messages.success(request, "Request withdrawn.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect(req)


@login_required
def photo(request, pk):
    item = get_object_or_404(RequestPhoto.objects.select_related("request__home"), pk=pk)
    if not services.can_view(request.user, item.request):
        raise PermissionDenied
    return serve_private(item.image, f"photo-{item.pk}.{item.image.name.rsplit('.', 1)[-1]}")
