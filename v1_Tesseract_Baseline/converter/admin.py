from django.contrib import admin
from .models import UploadedDocument

@admin.register(UploadedDocument)
class UploadedDocumentAdmin(admin.ModelAdmin):
    list_display = ['original_filename', 'user', 'status', 'invoice_number', 'total_amount', 'uploaded_at']
    list_filter = ['status', 'file_type', 'uploaded_at']
    search_fields = ['original_filename', 'invoice_number', 'gstin_supplier']
    readonly_fields = ['uploaded_at', 'processed_at']
