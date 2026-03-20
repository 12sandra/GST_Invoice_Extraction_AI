from django.contrib import admin
from .models import InvoiceJob, BatchJob, TrainingRun, ModelVersion


@admin.register(InvoiceJob)
class InvoiceJobAdmin(admin.ModelAdmin):
    list_display  = ('pk', 'original_filename', 'status', 'field_count',
                     'inv_no', 'uploaded_by', 'uploaded_at')
    list_filter   = ('status', 'used_for_training')
    search_fields = ('original_filename', 'extracted_data')
    readonly_fields = ('uploaded_at', 'processed_at', 'extracted_data', 'error_msg')


@admin.register(BatchJob)
class BatchJobAdmin(admin.ModelAdmin):
    list_display  = ('pk', 'status', 'total_files', 'done_files', 'uploaded_by', 'created_at')
    list_filter   = ('status',)
    readonly_fields = ('created_at', 'done_files', 'error_msg')


@admin.register(TrainingRun)
class TrainingRunAdmin(admin.ModelAdmin):
    list_display  = ('pk', 'status', 'invoices_used', 'epochs', 'best_f1', 'started_at')
    list_filter   = ('status',)
    readonly_fields = ('started_at', 'finished_at', 'log')


@admin.register(ModelVersion)
class ModelVersionAdmin(admin.ModelAdmin):
    list_display  = ('version', 'f1_score', 'invoices_trained_on', 'is_active', 'created_at')
    list_filter   = ('is_active',)
