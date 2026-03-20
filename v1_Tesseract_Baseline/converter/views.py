import os
from datetime import datetime
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import FileResponse, Http404
from django.conf import settings
from .models import UploadedDocument
from .forms import DocumentUploadForm
from .utils.layoutlm_extractor import extract_gst_data
from .utils.excel_generator import create_gst_excel


@login_required
def dashboard(request):
    docs = UploadedDocument.objects.filter(user=request.user)
    context = {
        'docs': docs[:10],
        'total': docs.count(),
        'completed': docs.filter(status='completed').count(),
        'failed': docs.filter(status='failed').count(),
        'processing': docs.filter(status='processing').count(),
    }
    return render(request, 'converter/dashboard.html', context)


@login_required
def upload_document(request):
    if request.method == 'POST':
        form = DocumentUploadForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded_file = request.FILES['file']
            ext = os.path.splitext(uploaded_file.name)[1].lower()

            if ext not in ['.pdf', '.jpg', '.jpeg', '.png']:
                messages.error(request, 'Invalid file type. Please upload PDF, JPG, or PNG.')
                return render(request, 'converter/upload.html', {'form': form})

            # Save uploaded file
            doc = UploadedDocument.objects.create(
                user=request.user,
                original_file=uploaded_file,
                original_filename=uploaded_file.name,
                file_type=ext.strip('.'),
                status='processing'
            )

            try:
                file_path = doc.original_file.path

                # Extract GST data using LayoutLM
                extracted = extract_gst_data(file_path)

                # Update doc with extracted summary
                doc.invoice_number = extracted.get('INV_NO', '')
                doc.gstin_supplier = extracted.get('GSTIN_S', '')
                doc.gstin_recipient = extracted.get('GSTIN_R', '')
                doc.invoice_date = extracted.get('INV_DATE', '')
                doc.total_amount = extracted.get('TOTAL', '')
                doc.total_tax = str(
                    (float(extracted.get('CGST', 0) or 0) +
                     float(extracted.get('SGST', 0) or 0) +
                     float(extracted.get('IGST', 0) or 0))
                )

                # Generate Excel
                excel_filename = f'GST_{doc.id}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
                excel_path = os.path.join(settings.MEDIA_ROOT, 'outputs', excel_filename)
                create_gst_excel(extracted, excel_path, uploaded_file.name)

                doc.output_excel = f'outputs/{excel_filename}'
                doc.status = 'completed'
                doc.processed_at = datetime.now()
                doc.save()

                messages.success(request, f'File processed successfully! Excel report is ready.')
                return redirect('results', doc_id=doc.id)

            except Exception as e:
                doc.status = 'failed'
                doc.error_message = str(e)
                doc.save()
                messages.error(request, f'Processing failed: {str(e)}')
                return redirect('dashboard')
    else:
        form = DocumentUploadForm()

    return render(request, 'converter/upload.html', {'form': form})


@login_required
def results(request, doc_id):
    doc = get_object_or_404(UploadedDocument, id=doc_id, user=request.user)
    return render(request, 'converter/results.html', {'doc': doc})


@login_required
def download_excel(request, doc_id):
    doc = get_object_or_404(UploadedDocument, id=doc_id, user=request.user)
    if not doc.output_excel:
        raise Http404('Excel file not available.')
    response = FileResponse(
        open(doc.output_excel.path, 'rb'),
        as_attachment=True,
        filename=f'GST_Report_{doc.original_filename}.xlsx'
    )
    return response


@login_required
def delete_document(request, doc_id):
    doc = get_object_or_404(UploadedDocument, id=doc_id, user=request.user)
    doc.delete()
    messages.success(request, 'Document deleted.')
    return redirect('dashboard')
