"""
pdf_processor.py  v7.0  —  PaddleOCR 2.7.x + Image preprocessing
Place in: converter/utils/pdf_processor.py

IMPORTANT: Use PaddleOCR 2.7.3 — NOT 3.x (3.x has GPU init bug on PaddlePaddle 2.6.x)
  pip uninstall paddleocr paddlex -y
  pip install paddleocr==2.7.3
"""
import os
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance
from pdf2image import convert_from_path
from django.conf import settings

_paddle_ocr = None


def _get_paddle_ocr():
    """Load PaddleOCR 2.7.x once. Falls back to Tesseract if unavailable."""
    global _paddle_ocr
    if _paddle_ocr is not None:
        return _paddle_ocr

    use_gpu = getattr(settings, 'PADDLE_USE_GPU', True)
    lang    = getattr(settings, 'PADDLE_OCR_LANG', 'en')
    angle   = getattr(settings, 'PADDLE_USE_ANGLE', True)

    for gpu in ([True, False] if use_gpu else [False]):
        try:
            from paddleocr import PaddleOCR
            # PaddleOCR 2.7.x uses use_gpu= (NOT device= which is 3.x API)
            _paddle_ocr = PaddleOCR(
                use_angle_cls=angle, lang=lang,
                use_gpu=gpu, show_log=False,
            )
            print(f'[PaddleOCR 2.7.x] Ready  GPU={gpu}  lang={lang}')
            return _paddle_ocr
        except AttributeError as e:
            if 'set_optimization_level' in str(e) or 'tensorrt' in str(e).lower():
                print('[PaddleOCR] *** VERSION MISMATCH ***')
                print('[PaddleOCR] You have PaddleOCR 3.x installed which is incompatible')
                print('[PaddleOCR] with PaddlePaddle 2.6.x. Fix with:')
                print('[PaddleOCR]   pip uninstall paddleocr paddlex -y')
                print('[PaddleOCR]   pip install paddleocr==2.7.3')
                _paddle_ocr = 'tesseract_fallback'
                return _paddle_ocr
            if gpu:
                print(f'[PaddleOCR] GPU error ({e}), retrying CPU...')
                continue
            _paddle_ocr = 'tesseract_fallback'
            return _paddle_ocr
        except Exception as e:
            if gpu:
                print(f'[PaddleOCR] GPU failed ({e}), retrying CPU...')
                continue
            print(f'[PaddleOCR] Failed: {e} — falling back to Tesseract')
            _paddle_ocr = 'tesseract_fallback'
            return _paddle_ocr

    _paddle_ocr = 'tesseract_fallback'
    return _paddle_ocr


def preprocess_image(image: Image.Image, aggressive: bool = False) -> Image.Image:
    """Adaptive image preprocessing for GST invoice scans."""
    if image.mode != 'RGB':
        image = image.convert('RGB')
    w, h = image.size
    if w < 2200:
        scale = 2400 / w
        image = image.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    image = image.convert('L')
    image = ImageEnhance.Contrast(image).enhance(2.0 if aggressive else 1.8)
    image = ImageEnhance.Sharpness(image).enhance(1.6 if aggressive else 1.4)
    if aggressive:
        image = image.filter(ImageFilter.MedianFilter(size=3))
    arr     = np.array(image, dtype=np.float32)
    radius  = 25 if aggressive else 18
    blurred = np.array(image.filter(ImageFilter.GaussianBlur(radius=radius)), dtype=np.float32)
    thresh  = 10 if aggressive else 8
    binary  = np.where(arr < blurred - thresh, 0, 255).astype(np.uint8)
    return Image.fromarray(binary).convert('RGB')


def pdf_to_images(pdf_path: str) -> list:
    poppler = getattr(settings, 'POPPLER_PATH', None)
    kwargs  = dict(dpi=300, thread_count=4, use_cropbox=True, strict=False)
    if poppler:
        kwargs['poppler_path'] = poppler
    raw_pages = convert_from_path(pdf_path, **kwargs)
    out = []
    for i, img in enumerate(raw_pages):
        aggressive = (img.size[0] * img.size[1] > 2000000)
        out.append(preprocess_image(img, aggressive=aggressive))
        print(f'[PDF] Page {i+1} preprocessed (aggressive={aggressive})')
    return out


def load_image(image_path: str) -> Image.Image:
    img = Image.open(image_path).convert('RGB')
    aggressive = (img.size[0] * img.size[1] > 1500000)
    return preprocess_image(img, aggressive=aggressive)


def extract_full_text_ocr(image: Image.Image) -> str:
    """Extract full text using PaddleOCR with Tesseract fallback."""
    ocr = _get_paddle_ocr()
    if ocr == 'tesseract_fallback':
        return _tesseract_text(image)
    try:
        arr    = np.array(image)
        result = ocr.ocr(arr, cls=True)
        lines  = []
        if result and result[0]:
            for line in result[0]:
                if not line or len(line) < 2: continue
                ti   = line[1]
                if not ti: continue
                text = str(ti[0]).strip()
                conf = float(ti[1]) if len(ti) > 1 else 1.0
                if text and conf > 0.25:
                    lines.append(text)
        combined = '\n'.join(lines)
        if len(combined.strip()) < 20:
            return _tesseract_text(image)
        return combined
    except Exception as e:
        print(f'[PaddleOCR] Error: {e}')
        return _tesseract_text(image)


def extract_ocr_data(image: Image.Image) -> list:
    """Word-level OCR with normalised bboxes [0-1000]. Used by LayoutLMv3."""
    ocr = _get_paddle_ocr()
    if ocr == 'tesseract_fallback':
        return _tesseract_words(image)
    W, H  = image.size
    words = []
    try:
        arr    = np.array(image)
        result = ocr.ocr(arr, cls=True)
        if not result or not result[0]:
            return _tesseract_words(image)
        for line in result[0]:
            if not line or len(line) < 2: continue
            box_pts = line[0]; ti = line[1]
            if not ti: continue
            text = str(ti[0]).strip()
            conf = float(ti[1]) if len(ti) > 1 else 1.0
            if not text or conf < 0.25: continue
            xs = [p[0] for p in box_pts]; ys = [p[1] for p in box_pts]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            norm = [max(0,int(1000*x0/W)), max(0,int(1000*y0/H)),
                    min(1000,int(1000*x1/W)), min(1000,int(1000*y1/H))]
            sub = text.split()
            if not sub: continue
            if len(sub) == 1:
                words.append({'text':text,'confidence':conf,'bbox':norm})
            else:
                tw = norm[2]-norm[0]; chars=[len(w) for w in sub]
                tc = sum(chars) or 1; xc = norm[0]
                for sw, cc in zip(sub, chars):
                    sw_w = int(tw*cc/tc)
                    words.append({'text':sw,'confidence':conf,
                                  'bbox':[xc,norm[1],xc+sw_w,norm[3]]})
                    xc += sw_w
    except Exception as e:
        print(f'[PaddleOCR] Word error: {e}')
        return _tesseract_words(image)
    return words


def _tesseract_text(image: Image.Image) -> str:
    try:
        import pytesseract
        cmd = getattr(settings, 'TESSERACT_CMD', None)
        if cmd: pytesseract.pytesseract.tesseract_cmd = cmd
        best = ''
        for cfg in ['--psm 6 --oem 3','--psm 4 --oem 3','--psm 3 --oem 3','--psm 11 --oem 3']:
            try:
                txt = pytesseract.image_to_string(image, lang='eng', config=cfg)
                if len(txt.strip()) > len(best.strip()): best = txt
            except Exception: pass
        return best
    except Exception as e:
        print(f'[Tesseract] {e}')
        return ''


def _tesseract_words(image: Image.Image) -> list:
    try:
        import pytesseract
        cmd = getattr(settings, 'TESSERACT_CMD', None)
        if cmd: pytesseract.pytesseract.tesseract_cmd = cmd
        ocr = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT,
                                         lang='eng', config='--psm 6 --oem 3')
        W, H = image.size; words = []
        for i in range(len(ocr['text'])):
            t = ocr['text'][i].strip(); c = int(ocr['conf'][i])
            if not t or c < 20: continue
            x,y,w,h = ocr['left'][i],ocr['top'][i],ocr['width'][i],ocr['height'][i]
            words.append({'text':t,'confidence':c,
                          'bbox':[int(1000*x/W),int(1000*y/H),
                                  int(1000*(x+w)/W),int(1000*(y+h)/H)]})
        return words
    except Exception as e:
        print(f'[Tesseract words] {e}'); return []


def extract_text_from_pdf_direct(pdf_path: str) -> str:
    try:
        import pdfplumber
        full_text = ''
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text: full_text += text + '\n'
        if len(full_text.strip()) > 80:
            print(f'[pdfplumber] {len(full_text)} chars from text layer')
            return full_text
    except Exception as e:
        print(f'[pdfplumber] {e}')
    return ''


def process_document(file_path: str) -> list:
    ext = os.path.splitext(file_path)[1].lower(); all_words = []
    if ext == '.pdf':
        images = pdf_to_images(file_path)
        for page_num, img in enumerate(images):
            for w in extract_ocr_data(img): w['page'] = page_num+1; all_words.append(w)
    else:
        for w in extract_ocr_data(load_image(file_path)): w['page'] = 1; all_words.append(w)
    return all_words
