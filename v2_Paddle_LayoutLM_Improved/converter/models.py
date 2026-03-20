import json
from django.db import models
from django.contrib.auth.models import User


class BatchJob(models.Model):
    STATUS = [('pending','Pending'),('processing','Processing'),
              ('done','Done'),('error','Error')]
    uploaded_by       = models.ForeignKey(User, on_delete=models.SET_NULL,
                                          null=True, blank=True, related_name='batches')
    created_at        = models.DateTimeField(auto_now_add=True)
    status            = models.CharField(max_length=20, choices=STATUS, default='pending')
    total_files       = models.IntegerField(default=0)
    done_files        = models.IntegerField(default=0)
    batch_excel       = models.FileField(upload_to='batch_outputs/', blank=True, null=True)
    batch_zip         = models.FileField(upload_to='batch_outputs/', blank=True, null=True)
    error_msg         = models.TextField(blank=True)
    used_for_training = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Batch #{self.pk} ({self.total_files} files, {self.status})'

    @property
    def progress_pct(self):
        if not self.total_files:
            return 0
        return int(self.done_files / self.total_files * 100)


class InvoiceJob(models.Model):
    STATUS = [('pending','Pending'),('processing','Processing'),
              ('done','Done'),('error','Error')]
    uploaded_by       = models.ForeignKey(User, on_delete=models.SET_NULL,
                                          null=True, blank=True, related_name='invoices')
    batch             = models.ForeignKey(BatchJob, on_delete=models.CASCADE,
                                          null=True, blank=True, related_name='invoices')
    original_filename = models.CharField(max_length=255)
    uploaded_file     = models.FileField(upload_to='uploads/')
    status            = models.CharField(max_length=20, choices=STATUS, default='pending')
    extracted_data    = models.TextField(blank=True)
    output_file       = models.FileField(upload_to='outputs/', blank=True, null=True)
    uploaded_at       = models.DateTimeField(auto_now_add=True)
    processed_at      = models.DateTimeField(null=True, blank=True)
    error_msg         = models.TextField(blank=True)
    field_count       = models.IntegerField(default=0)
    used_for_training = models.BooleanField(default=False)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'Invoice #{self.pk} — {self.original_filename} ({self.status})'

    def get_data(self):
        if not self.extracted_data:
            return {}
        try:
            return json.loads(self.extracted_data)
        except Exception:
            return {}

    def set_data(self, d):
        self.extracted_data = json.dumps(d, ensure_ascii=False, default=str)

    @property
    def inv_no(self):
        return self.get_data().get('INV_NO', '—')

    @property
    def supplier(self):
        return self.get_data().get('SUPPLIER', '—')

    @property
    def total(self):
        return self.get_data().get('TOTAL', '—')


class TrainingRun(models.Model):
    STATUS = [('pending','Pending'),('running','Running'),
              ('done','Done'),('error','Error')]
    started_at    = models.DateTimeField(auto_now_add=True)
    finished_at   = models.DateTimeField(null=True, blank=True)
    status        = models.CharField(max_length=20, choices=STATUS, default='pending')
    invoices_used = models.IntegerField(default=0)
    epochs        = models.IntegerField(default=5)
    best_f1       = models.FloatField(null=True, blank=True)
    model_path    = models.CharField(max_length=500, blank=True)
    log           = models.TextField(blank=True)
    triggered_by  = models.ForeignKey(User, on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name='training_runs')

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        f1 = f'{self.best_f1:.3f}' if self.best_f1 else 'N/A'
        return f'TrainingRun #{self.pk} ({self.status}, F1={f1})'


class ModelVersion(models.Model):
    version             = models.IntegerField(default=1)
    created_at          = models.DateTimeField(auto_now_add=True)
    training_run        = models.OneToOneField(TrainingRun, on_delete=models.SET_NULL,
                                               null=True, blank=True)
    f1_score            = models.FloatField(null=True, blank=True)
    invoices_trained_on = models.IntegerField(default=0)
    model_path          = models.CharField(max_length=500)
    is_active           = models.BooleanField(default=False)
    notes               = models.TextField(blank=True)

    class Meta:
        ordering = ['-version']

    def __str__(self):
        f1 = f'{self.f1_score:.3f}' if self.f1_score else 'N/A'
        return f'Model v{self.version} (F1={f1}, active={self.is_active})'
