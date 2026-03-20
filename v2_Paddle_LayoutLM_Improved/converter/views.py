"""
converter/views.py  v4.0
All views. Login required everywhere. Auto-training after every upload.
No |list filter used anywhere. sections_map passed as plain dict.
"""
import os
import zipfile
from datetime import datetime

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse, Http404
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings

from .models import InvoiceJob, BatchJob, TrainingRun, ModelVersion

MAX_BATCH   = 20
ALLOWED_EXT = {'.pdf', '.jpg', '.jpeg', '.png', '.tiff', '.tif', '.webp', '.bmp'}


def _allowed(name):
    return os.path.splitext(name.lower())[1] in ALLOWED_EXT


def _sections(data):
    """Build display sections from extracted data dict. Only non-empty rows."""
    DEFS = [
        ('Supplier Details', [
            ('Supplier Name', 'SUPPLIER'), ('GSTIN', 'GSTIN_S'),
            ('PAN', 'PAN'), ('CIN', 'CIN'), ('State Code', 'STATE_CODE'),
            ('Service Tax No.', 'SERVICE_TAX'), ('FSSAI', 'FSSAI'),
            ('LUT Bond', 'LUT_BOND'), ('MSME No.', 'MSME_NO'), ('TAN', 'TAN'),
        ]),
        ('Buyer / Recipient', [
            ('Recipient Name', 'RECIPIENT'), ('Recipient GSTIN', 'GSTIN_R'),
            ('Recipient PAN', 'RECIPIENT_PAN'), ('Place of Supply', 'PLACE'),
            ('Vendor Code', 'VENDOR_CODE'),
        ]),
        ('Invoice Details', [
            ('Invoice No.', 'INV_NO'), ('Invoice Date', 'INV_DATE'),
            ('IRN', 'IRN'), ('Ack No.', 'ACK_NO'), ('Ack Date', 'ACK_DATE'),
            ('Challan No.', 'CHALLAN_NO'), ('Challan Date', 'CHALLAN_DATE'),
            ('E-Way Bill', 'EWAY_BILL'), ('Transport', 'TRANSPORT'),
            ('Transport ID', 'TRANSPORT_ID'), ('Vehicle No.', 'VEHICLE_NO'),
            ('Ship By', 'SHIP_BY'), ('Dispatch From', 'DISPATCH_FROM'),
            ('Dispatch Date', 'DISPATCH_DATE'), ('Cust. Order No.', 'CUST_ORDER'),
            ('OA No.', 'OA_NO'), ('OA Date', 'OA_DATE'),
            ('DBA No.', 'DBA_NO'), ('DBA Date', 'DBA_DATE'),
            ('Payment Terms', 'PAYMENT_TERMS'),
        ]),
        ('Item / Goods', [
            ('HSN / SAC Code', 'HSN'), ('Description', 'ITEM_DESC'),
            ('Quantity', 'QTY'), ('Unit', 'UOM'),
            ('Rate (₹)', 'RATE'), ('GST Rate %', 'GST_RATE'),
        ]),
        ('Tax & Amounts', [
            ('Taxable Value (₹)', 'TAXABLE'),
            ('CGST Rate %', 'CGST_RATE'), ('CGST (₹)', 'CGST'),
            ('SGST Rate %', 'SGST_RATE'), ('SGST (₹)', 'SGST'),
            ('IGST Rate %', 'IGST_RATE'), ('IGST (₹)', 'IGST'),
            ('Total GST (₹)', 'TOTAL_GST'),
            ('Net Payable (₹)', 'TOTAL'), ('In Words', 'TOTAL_WORDS'),
        ]),
        ('Bank & Payment', [
            ('Bank', 'BANK'), ('Account No.', 'AC_NO'),
            ('IFSC', 'IFSC'), ('UPI ID', 'UPI_ID'),
        ]),
    ]
    result = []
    for title, fields in DEFS:
        rows = [(lbl, data.get(key, '')) for lbl, key in fields if data.get(key, '')]
        if rows:
            result.append({'title': title, 'rows': rows})
    return result


def _process_one(job):
    """Extract a single invoice. Updates job record. Never raises."""
    from converter.utils.layoutlm_extractor import extract_gst_data
    from converter.utils.excel_generator import create_gst_excel

    job.status = 'processing'
    job.save(update_fields=['status'])
    try:
        data = extract_gst_data(job.uploaded_file.path)
        job.set_data(data)
        job.field_count = len([k for k in data if k != 'raw_text' and data[k]])

        ts      = datetime.now().strftime('%Y%m%d_%H%M%S')
        out_dir = os.path.join(settings.MEDIA_ROOT, 'outputs')
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f'GST_{job.pk}_{ts}.xlsx')

        create_gst_excel(data, out_path, job.original_filename)
        job.output_file  = f'outputs/{os.path.basename(out_path)}'
        job.status       = 'done'
        job.processed_at = datetime.now()
    except Exception as e:
        job.status    = 'error'
        job.error_msg = str(e)[:500]
        print(f'[Process] Error on {job.original_filename}: {e}')
    finally:
        job.save()


def _process_batch(batch):
    """Process all invoices in a batch. Never raises."""
    from converter.utils.excel_generator import create_batch_excel

    batch.status = 'processing'
    batch.save(update_fields=['status'])

    jobs = list(batch.invoices.order_by('pk'))
    for job in jobs:
        _process_one(job)
        batch.done_files += 1
        batch.save(update_fields=['done_files'])

    try:
        ts      = datetime.now().strftime('%Y%m%d_%H%M%S')
        out_dir = os.path.join(settings.MEDIA_ROOT, 'batch_outputs')
        os.makedirs(out_dir, exist_ok=True)

        done_jobs = [j for j in jobs if j.status == 'done']
        if done_jobs:
            batch_data = [{'filename': j.original_filename, 'data': j.get_data()}
                          for j in done_jobs]
            xls_path = os.path.join(out_dir, f'GST_Batch_{batch.pk}_{ts}.xlsx')
            create_batch_excel(batch_data, xls_path)
            batch.batch_excel = f'batch_outputs/{os.path.basename(xls_path)}'

        zip_path = os.path.join(out_dir, f'GST_Batch_{batch.pk}_{ts}_all.zip')
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for j in jobs:
                if j.output_file and j.status == 'done':
                    fp = os.path.join(settings.MEDIA_ROOT, str(j.output_file))
                    if os.path.exists(fp):
                        zf.write(fp, os.path.basename(fp))
        batch.batch_zip = f'batch_outputs/{os.path.basename(zip_path)}'
        batch.status    = 'done'
    except Exception as e:
        batch.status    = 'error'
        batch.error_msg = str(e)[:500]
        print(f'[Batch] Error: {e}')
    finally:
        batch.save()


def _trigger_training(user=None):
    """Schedule background training. Never raises."""
    try:
        from auto_train.trainer import schedule_training
        schedule_training(triggered_by_user=user)
    except Exception as e:
        print(f'[AutoTrain] Schedule error: {e}')


# ── Dashboard ──────────────────────────────────────────────────────────────────
@login_required
def dashboard(request):
    user   = request.user
    recent = InvoiceJob.objects.filter(
        uploaded_by=user, batch__isnull=True).order_by('-uploaded_at')[:8]
    batches = BatchJob.objects.filter(
        uploaded_by=user).order_by('-created_at')[:5]

    return render(request, 'converter/dashboard.html', {
        'recent_jobs':    recent,
        'recent_batches': batches,
        'total_invoices': InvoiceJob.objects.filter(uploaded_by=user, status='done').count(),
        'total_batches':  BatchJob.objects.filter(uploaded_by=user).count(),
        'active_model':   ModelVersion.objects.filter(is_active=True).first(),
        'latest_run':     TrainingRun.objects.filter(status='done').first(),
    })


# ── History ────────────────────────────────────────────────────────────────────
@login_required
def history(request):
    q      = request.GET.get('q', '').strip()
    status = request.GET.get('status', '')
    jobs   = InvoiceJob.objects.filter(
        uploaded_by=request.user, batch__isnull=True)
    if q:
        jobs = jobs.filter(original_filename__icontains=q)
    if status:
        jobs = jobs.filter(status=status)
    jobs = jobs.order_by('-uploaded_at')[:200]

    batches = BatchJob.objects.filter(
        uploaded_by=request.user).order_by('-created_at')[:50]

    return render(request, 'converter/history.html', {
        'jobs': jobs, 'batches': batches, 'q': q, 'status': status,
    })


# ── Model Status ───────────────────────────────────────────────────────────────
@login_required
def model_status(request):
    return render(request, 'converter/model_status.html', {
        'versions': ModelVersion.objects.order_by('-version')[:10],
        'runs':     TrainingRun.objects.order_by('-started_at')[:10],
    })


# ── Single Upload ──────────────────────────────────────────────────────────────
@login_required
@require_http_methods(['GET', 'POST'])
def upload(request):
    if request.method == 'GET':
        return render(request, 'converter/upload.html')

    f = request.FILES.get('invoice_file')
    if not f:
        messages.error(request, 'Please select a file.')
        return render(request, 'converter/upload.html')
    if not _allowed(f.name):
        messages.error(request, 'Unsupported file type. Use PDF, JPG, PNG.')
        return render(request, 'converter/upload.html')
    if f.size > 20 * 1024 * 1024:
        messages.error(request, 'File too large (max 20 MB).')
        return render(request, 'converter/upload.html')

    job = InvoiceJob(original_filename=f.name, status='pending',
                     uploaded_by=request.user)
    job.uploaded_file = f
    job.save()
    _process_one(job)
    _trigger_training(request.user)
    return redirect('converter:results', pk=job.pk)


@login_required
def results(request, pk):
    job = get_object_or_404(InvoiceJob, pk=pk)
    return render(request, 'converter/results.html', {
        'job':      job,
        'sections': _sections(job.get_data()),
    })


@login_required
def download_excel(request, pk):
    job = get_object_or_404(InvoiceJob, pk=pk, status='done')
    if not job.output_file:
        raise Http404('No Excel generated.')
    path = os.path.join(settings.MEDIA_ROOT, str(job.output_file))
    if not os.path.exists(path):
        raise Http404('File not found on disk.')
    with open(path, 'rb') as fh:
        content = fh.read()
    resp = HttpResponse(content,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = f'attachment; filename="{os.path.basename(path)}"'
    return resp


# ── Batch Upload ───────────────────────────────────────────────────────────────
@login_required
@require_http_methods(['GET', 'POST'])
def batch_upload(request):
    if request.method == 'GET':
        return render(request, 'converter/batch_upload.html',
                      {'max_files': MAX_BATCH})

    files = request.FILES.getlist('invoice_files')
    if not files:
        messages.error(request, 'Please select at least one file.')
        return render(request, 'converter/batch_upload.html', {'max_files': MAX_BATCH})
    if len(files) > MAX_BATCH:
        messages.error(request, f'Maximum {MAX_BATCH} files allowed per batch.')
        return render(request, 'converter/batch_upload.html', {'max_files': MAX_BATCH})

    valid   = [f for f in files if _allowed(f.name) and f.size <= 20 * 1024 * 1024]
    skipped = [f.name for f in files if f not in valid]

    if not valid:
        messages.error(request, 'No valid files. Use PDF, JPG, PNG under 20 MB.')
        return render(request, 'converter/batch_upload.html', {'max_files': MAX_BATCH})

    if skipped:
        messages.warning(request,
            f'Skipped {len(skipped)} file(s): {", ".join(skipped[:3])}{"..." if len(skipped)>3 else ""}')

    batch = BatchJob.objects.create(
        status='pending',
        total_files=len(valid),
        uploaded_by=request.user,
    )
    for f in valid:
        job = InvoiceJob(batch=batch, original_filename=f.name,
                         status='pending', uploaded_by=request.user)
        job.uploaded_file = f
        job.save()

    _process_batch(batch)
    _trigger_training(request.user)
    return redirect('converter:batch_results', pk=batch.pk)


@login_required
def batch_results(request, pk):
    batch = get_object_or_404(BatchJob, pk=pk)
    jobs  = list(batch.invoices.order_by('pk'))
    # Build sections map as plain dict {job_pk: sections_list}
    # — no Django template filter needed
    sections_map = {}
    for job in jobs:
        if job.status == 'done':
            sections_map[job.pk] = _sections(job.get_data())
    return render(request, 'converter/batch_results.html', {
        'batch':        batch,
        'jobs':         jobs,          # plain list — no |list filter needed
        'sections_map': sections_map,
    })


@login_required
def batch_status(request, pk):
    batch = get_object_or_404(BatchJob, pk=pk)
    return JsonResponse({
        'status':   batch.status,
        'total':    batch.total_files,
        'done':     batch.done_files,
        'progress': batch.progress_pct,
    })


@login_required
def batch_download_excel(request, pk):
    batch = get_object_or_404(BatchJob, pk=pk, status='done')
    if not batch.batch_excel:
        raise Http404('Combined Excel not available.')
    path = os.path.join(settings.MEDIA_ROOT, str(batch.batch_excel))
    if not os.path.exists(path):
        raise Http404('File not found on disk.')
    with open(path, 'rb') as fh:
        content = fh.read()
    resp = HttpResponse(content,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = f'attachment; filename="GST_Batch_{pk}.xlsx"'
    return resp


@login_required
def batch_download_zip(request, pk):
    batch = get_object_or_404(BatchJob, pk=pk, status='done')
    if not batch.batch_zip:
        raise Http404('ZIP not available.')
    path = os.path.join(settings.MEDIA_ROOT, str(batch.batch_zip))
    if not os.path.exists(path):
        raise Http404('File not found on disk.')
    with open(path, 'rb') as fh:
        content = fh.read()
    resp = HttpResponse(content, content_type='application/zip')
    resp['Content-Disposition'] = f'attachment; filename="GST_Batch_{pk}_all.zip"'
    return resp
