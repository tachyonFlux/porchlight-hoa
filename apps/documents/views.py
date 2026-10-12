from itertools import groupby

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.core import audit
from apps.core.permissions import board_required
from apps.core.ratelimit import hit, too_many
from apps.core.uploads import serve_private
from apps.core.utils import absolute_url, client_ip
from apps.meetings.services import can_see

from . import services
from .forms import CategoryForm, DocumentForm, ShareLinkForm
from .models import Document, DocumentCategory, ShareLink

NEW_LINK_SESSION_KEY = "new_share_link"


# ------------------------------------------------------------------ library


@login_required
def document_list(request):
    if not can_see(request.user, "residents"):
        raise PermissionDenied
    docs = list(services.visible_documents(request.user))
    groups = [(cat, list(items)) for cat, items in groupby(docs, key=lambda d: d.category)]
    return render(request, "documents/document_list.html", {"groups": groups})


@login_required
def download(request, pk):
    document = get_object_or_404(Document, pk=pk)
    if not services.can_download(request.user, document):
        raise PermissionDenied
    return serve_private(document.file, document.download_name)


def _save_upload(form, document):
    upload = form.cleaned_data.get("file")
    if upload and hasattr(upload, "content_type"):
        document.original_name = upload.name[:200]
        document.file_size = upload.size


@board_required
def document_create(request):
    form = DocumentForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        document = form.save(commit=False)
        document.uploaded_by = request.user
        _save_upload(form, document)
        document.save()
        audit.record(request, "documents.uploaded", document, {"visibility": document.visibility})
        messages.success(request, "Document added.")
        return redirect("documents:list")
    return render(request, "documents/document_form.html", {"form": form})


@board_required
def document_edit(request, pk):
    document = get_object_or_404(Document, pk=pk)
    old_file = document.file.name  # before validation replaces it
    form = DocumentForm(request.POST or None, request.FILES or None, instance=document)
    if request.method == "POST":
        if "delete" in request.POST:
            storage, name = document.file.storage, document.file.name
            audit.record(request, "documents.deleted", document)
            document.delete()
            transaction.on_commit(lambda: storage.delete(name))
            messages.success(request, "Document deleted.")
            return redirect("documents:list")
        if form.is_valid():
            document = form.save(commit=False)
            _save_upload(form, document)
            document.save()
            if old_file != document.file.name:
                storage = document.file.storage
                transaction.on_commit(lambda: storage.delete(old_file))
            audit.record(request, "documents.updated", document, {"fields": form.changed_data})
            messages.success(request, "Document saved.")
            return redirect("documents:list")
    return render(request, "documents/document_form.html", {"form": form, "document": document})


@board_required
def categories(request):
    form = CategoryForm(request.POST or None)
    if request.method == "POST":
        if request.POST.get("delete", "").isdigit():
            DocumentCategory.objects.filter(pk=int(request.POST["delete"])).delete()
            messages.success(request, "Category removed. Its documents are now uncategorized.")
            return redirect("documents:categories")
        if form.is_valid():
            form.save()
            messages.success(request, "Category added.")
            return redirect("documents:categories")
    return render(
        request, "documents/categories.html", {"form": form, "categories": DocumentCategory.objects.all()}
    )


# ------------------------------------------------------------------ share links (board)


@board_required
def share_list(request):
    links = ShareLink.objects.prefetch_related("documents").select_related("created_by")
    new_link = request.session.pop(NEW_LINK_SESSION_KEY, None)
    return render(request, "documents/share_list.html", {"links": links, "new_link": new_link})


@board_required
def share_create(request):
    initial = {}
    if request.GET.get("document", "").isdigit():
        initial["documents"] = [int(request.GET["document"])]
    form = ShareLinkForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        link, raw = services.create_share_link(
            request.user, d["label"], list(d["documents"]), form.expires_at(), d["max_views"], request=request
        )
        # Only the hash is stored, so this is the one time the full link can be shown.
        request.session[NEW_LINK_SESSION_KEY] = {
            "label": link.label,
            "url": absolute_url(reverse("documents:shared", args=[raw])),
        }
        return redirect("documents:share_list")
    return render(request, "documents/share_form.html", {"form": form})


@board_required
def share_detail(request, pk):
    link = get_object_or_404(ShareLink.objects.prefetch_related("documents"), pk=pk)
    return render(
        request,
        "documents/share_detail.html",
        {"link": link, "accesses": link.accesses.select_related("document")[:200]},
    )


@board_required
@require_POST
def share_revoke(request, pk):
    link = get_object_or_404(ShareLink, pk=pk)
    services.revoke(link, request.user, request=request)
    messages.success(request, "Link revoked. It no longer works.")
    return redirect("documents:share_detail", pk=link.pk)


# ------------------------------------------------------------------ share links (public)


def _no_store(response):
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _shared_link_or_none(request, token):
    link = ShareLink.find(token)
    if link is None and not hit("share-miss", client_ip(request), limit=30, period=3600):
        return None, too_many(request)
    return link, None


def shared(request, token):
    link, blocked = _shared_link_or_none(request, token)
    if blocked:
        return blocked
    if link is None:
        return _no_store(render(request, "documents/shared_gone.html", status=404))
    if not link.is_active:
        services.log_access(request, link, allowed=False)
        return _no_store(render(request, "documents/shared_gone.html", {"link": link}, status=410))
    services.log_access(request, link)
    return _no_store(
        render(request, "documents/shared.html", {"link": link, "token": token, "docs": link.documents.all()})
    )


def shared_download(request, token, doc_pk):
    link, blocked = _shared_link_or_none(request, token)
    if blocked:
        return blocked
    if link is None:
        return _no_store(render(request, "documents/shared_gone.html", status=404))
    document = link.documents.filter(pk=doc_pk).first()
    if document is None:
        return _no_store(render(request, "documents/shared_gone.html", status=404))
    if not services.claim_download(link):
        services.log_access(request, link, document, allowed=False)
        link.refresh_from_db()
        return _no_store(render(request, "documents/shared_gone.html", {"link": link}, status=410))
    services.log_access(request, link, document)
    return _no_store(serve_private(document.file, document.download_name))
