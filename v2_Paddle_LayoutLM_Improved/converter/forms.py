from django import forms

class DocumentUploadForm(forms.Form):
    DOCUMENT_TYPES = [
        ('auto', 'Auto Detect'),
        ('invoice', 'GST Invoice'),
        ('report', 'GST Report'),
        ('statement', 'Bank Statement'),
    ]
    file = forms.FileField(
        widget=forms.FileInput(attrs={
            'class': 'form-control',
            'accept': '.pdf,.jpg,.jpeg,.png',
            'id': 'fileUpload'
        }),
        help_text='Upload PDF or Image (JPG/PNG). Max size: 20MB'
    )
    document_type = forms.ChoiceField(
        choices=DOCUMENT_TYPES,
        widget=forms.Select(attrs={'class': 'form-select'}),
        initial='auto'
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': 'Optional notes about this document...'
        })
    )
