from django.db import models
from django.contrib.auth.models import User

class UploadedDocument(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='documents')
    original_file = models.FileField(upload_to='uploads/')
    original_filename = models.CharField(max_length=255)
    file_type = models.CharField(max_length=10)  # pdf, jpg, png
    output_excel = models.FileField(upload_to='outputs/', blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    error_message = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(blank=True, null=True)

    # Extracted data summary
    invoice_number = models.CharField(max_length=100, blank=True)
    gstin_supplier = models.CharField(max_length=20, blank=True)
    gstin_recipient = models.CharField(max_length=20, blank=True)
    invoice_date = models.CharField(max_length=50, blank=True)
    total_amount = models.CharField(max_length=50, blank=True)
    total_tax = models.CharField(max_length=50, blank=True)

    def __str__(self):
        return f'{self.original_filename} - {self.user.username}'

    class Meta:
        ordering = ['-uploaded_at']
        verbose_name = 'Uploaded Document'
