from django.urls import path

from . import views

app_name = "documents"

urlpatterns = [
    path("documents/", views.document_list, name="list"),
    path("documents/<int:pk>/download/", views.download, name="download"),
    path("board/documents/new/", views.document_create, name="create"),
    path("board/documents/<int:pk>/edit/", views.document_edit, name="edit"),
    path("board/documents/categories/", views.categories, name="categories"),
    path("board/share-links/", views.share_list, name="share_list"),
    path("board/share-links/new/", views.share_create, name="share_create"),
    path("board/share-links/<int:pk>/", views.share_detail, name="share_detail"),
    path("board/share-links/<int:pk>/revoke/", views.share_revoke, name="share_revoke"),
    path("s/<str:token>/", views.shared, name="shared"),
    path("s/<str:token>/<int:doc_pk>/", views.shared_download, name="shared_download"),
]
