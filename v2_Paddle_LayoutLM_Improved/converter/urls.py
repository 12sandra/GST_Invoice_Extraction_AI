from django.urls import path
from . import views

app_name = 'converter'

urlpatterns = [
    path('dashboard/',                   views.dashboard,           name='dashboard'),
    path('history/',                     views.history,             name='history'),
    path('model-status/',                views.model_status,        name='model_status'),

    path('upload/',                      views.upload,              name='upload'),
    path('results/<int:pk>/',            views.results,             name='results'),
    path('download/<int:pk>/',           views.download_excel,      name='download'),

    path('batch/',                       views.batch_upload,        name='batch_upload'),
    path('batch/<int:pk>/',              views.batch_results,       name='batch_results'),
    path('batch/<int:pk>/status/',       views.batch_status,        name='batch_status'),
    path('batch/<int:pk>/download/',     views.batch_download_excel,name='batch_download_excel'),
    path('batch/<int:pk>/download-zip/', views.batch_download_zip,  name='batch_download_zip'),
]
