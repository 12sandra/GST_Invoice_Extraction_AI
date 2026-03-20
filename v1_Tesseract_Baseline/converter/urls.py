from django.urls import path
from . import views

urlpatterns = [
    path('dashboard/', views.dashboard, name='dashboard'),
    path('upload/', views.upload_document, name='upload'),
    path('results/<int:doc_id>/', views.results, name='results'),
    path('download/<int:doc_id>/', views.download_excel, name='download_excel'),
    path('delete/<int:doc_id>/', views.delete_document, name='delete_document'),
]
