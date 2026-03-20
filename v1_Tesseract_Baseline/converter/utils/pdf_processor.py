"""
pdf_processor.py  —  Image preprocessing and OCR
Place in: converter/utils/pdf_processor.py
"""
import os
import pytesseract
from PIL import Image, ImageFilter, ImageEnhance
import numpy as np
from pdf2image import convert_from_path
from django.conf import settings

pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD


def preprocess_image(image):
    """
    Adaptive preprocessing for scanned GST invoices.
    Enhances contrast and sharpness without destroying thin characters.
    """
    w, h = image.size
    # Upscale small images
    if w < 1600:
        scale = 2200 / w
        image = image.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    image = image.convert('L')
    image = ImageEnhance.Contrast(image).enhance(1.8)
    image = ImageEnhance.Sharpness(image).enhance(1.6)
    image = image.filter(ImageFilter.MedianFilter(size=1))

    # Adaptive threshold using local neighbourhood
    img_array = np.array(image, dtype=np.float32)
    blurred   = np.array(
        image.filter(ImageFilter.GaussianBlur(radius=20)),
        dtype=np.float32
    )
    binary = np.where(img_array < blurred - 6, 0, 255).astype(np.uint8)
    return Image.fromarray(binary).convert('RGB')


def pdf_to_images(pdf_path):
    """Convert PDF pages to preprocessed PIL Images at 300 DPI."""
    images = convert_from_path(
        pdf_path, dpi=300,
        poppler_path=settings.POPPLER_PATH,
        thread_count=4, use_cropbox=True, strict=False,
    )
    out = []
    for i, img in enumerate(images):
        out.append(preprocess_image(img))
        print(f'[PDF] Page {i+1} preprocessed')
    return out


def load_image(image_path):
    """Load and preprocess an image file."""
    img = Image.open(image_path).convert('RGB')
    return preprocess_image(img)


def extract_text_from_pdf_direct(pdf_path):
    """Use pdfplumber to extract embedded text layer — most accurate for text PDFs."""
    try:
        import pdfplumber
        full_text = ''
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    full_text += text + '\n'
        if len(full_text.strip()) > 80:
            print(f'[pdfplumber] {len(full_text)} chars')
            return full_text
    except Exception as e:
        print(f'[pdfplumber] {e}')
    return ''


def extract_full_text_ocr(image):
    """Try multiple Tesseract PSM modes and return the longest result."""
    best = ''
    for cfg in ['--psm 6 --oem 3', '--psm 4 --oem 3', '--psm 3 --oem 3']:
        try:
            txt = pytesseract.image_to_string(image, lang='eng', config=cfg)
            if len(txt.strip()) > len(best.strip()):
                best = txt
        except Exception as e:
            print(f'[OCR] {cfg}: {e}')
    return best


def extract_ocr_data(image):
    """Word-level OCR with normalised bounding boxes for LayoutLM."""
    ocr = pytesseract.image_to_data(
        image, output_type=pytesseract.Output.DICT,
        lang='eng', config='--psm 6 --oem 3',
    )
    W, H = image.size
    words = []
    for i in range(len(ocr['text'])):
        t = ocr['text'][i].strip()
        c = int(ocr['conf'][i])
        if not t or c < 20:
            continue
        x, y, w, h = ocr['left'][i], ocr['top'][i], ocr['width'][i], ocr['height'][i]
        words.append({
            'text': t, 'confidence': c,
            'bbox': [
                int(1000 * x / W), int(1000 * y / H),
                int(1000 * (x + w) / W), int(1000 * (y + h) / H),
            ],
        })
    return words


def process_document(file_path):
    """Process any file. Returns list of word dicts with page numbers."""
    ext = os.path.splitext(file_path)[1].lower()
    all_words = []
    if ext == '.pdf':
        images = pdf_to_images(file_path)
        for page_num, img in enumerate(images):
            for w in extract_ocr_data(img):
                w['page'] = page_num + 1
                all_words.append(w)
    else:
        for w in extract_ocr_data(load_image(file_path)):
            w['page'] = 1
            all_words.append(w)
    return all_words
