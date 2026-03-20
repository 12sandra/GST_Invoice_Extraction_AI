"""
prepare_dataset.py  v3.0
========================
Auto-label your GST invoice dataset using PaddleOCR + regex pipeline.
Place in: gst_project/prepare_dataset.py

Usage:
  python prepare_dataset.py

What it does:
  1. Reads all images from dataset/images/  (jpg, png, gif, webp, jfif)
  2. Converts non-standard formats (webp, gif, jfif) to PNG
  3. Runs PaddleOCR on each image (GPU-accelerated)
  4. Uses the regex field extractor to auto-label each word
  5. Saves word-level label files to  dataset/labels/
  6. Creates a HuggingFace dataset at  dataset/hf_dataset/
  7. Generates a summary report  dataset/labeling_report.txt

After running, check dataset/labels/*.txt and fix any wrong labels.
Then run:  python finetune_layoutlm.py
"""

import os
import sys
import json
import time
import shutil
import django

# ─── Django setup ─────────────────────────────────────────────────────────────
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gst_converter.settings')
django.setup()

from django.conf import settings as django_settings
from PIL import Image
import numpy as np

# ─── Config ───────────────────────────────────────────────────────────────────
DATASET_DIR  = getattr(django_settings, 'DATASET_DIR',
                       os.path.join(os.path.dirname(__file__), 'dataset'))
IMAGES_DIR   = os.path.join(DATASET_DIR, 'images')
LABELS_DIR   = os.path.join(DATASET_DIR, 'labels')
HF_DIR       = os.path.join(DATASET_DIR, 'hf_dataset')
REPORT_PATH  = os.path.join(DATASET_DIR, 'labeling_report.txt')

os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(LABELS_DIR, exist_ok=True)
os.makedirs(HF_DIR,     exist_ok=True)

# ─── Field label map ──────────────────────────────────────────────────────────
LABEL_LIST = [
    'O',
    'B-GSTIN_S','I-GSTIN_S','B-GSTIN_R','I-GSTIN_R',
    'B-INV_NO','I-INV_NO','B-INV_DATE','I-INV_DATE',
    'B-TAXABLE','I-TAXABLE','B-CGST','I-CGST',
    'B-SGST','I-SGST','B-IGST','I-IGST',
    'B-TOTAL','I-TOTAL','B-HSN','I-HSN',
    'B-ITEM_DESC','I-ITEM_DESC','B-SUPPLIER','I-SUPPLIER',
    'B-RECIPIENT','I-RECIPIENT','B-PLACE','I-PLACE',
    'B-RATE','I-RATE','B-QTY','I-QTY',
    'B-CGST_RATE','I-CGST_RATE','B-SGST_RATE','I-SGST_RATE',
]
LABEL2ID = {l: i for i, l in enumerate(LABEL_LIST)}


# ─── Convert any image to PNG ─────────────────────────────────────────────────
def to_png(src_path):
    """Convert any image format to PNG. Returns path to PNG file."""
    ext = os.path.splitext(src_path)[1].lower()
    if ext in ('.jpg', '.jpeg', '.png'):
        return src_path
    png_path = os.path.splitext(src_path)[0] + '_converted.png'
    if os.path.exists(png_path):
        return png_path
    try:
        img = Image.open(src_path).convert('RGB')
        img.save(png_path, 'PNG')
        print(f'  Converted {os.path.basename(src_path)} → PNG')
        return png_path
    except Exception as e:
        print(f'  [WARN] Cannot convert {src_path}: {e}')
        return None


# ─── Load PaddleOCR ───────────────────────────────────────────────────────────
def get_ocr():
    from paddleocr import PaddleOCR
    use_gpu = getattr(django_settings, 'PADDLE_USE_GPU', True)
    lang    = getattr(django_settings, 'PADDLE_OCR_LANG', 'en')
    for gpu in ([True, False] if use_gpu else [False]):
        try:
            ocr = PaddleOCR(use_angle_cls=True, lang=lang, use_gpu=gpu, show_log=False)
            print(f'[PaddleOCR] Loaded GPU={gpu}')
            return ocr
        except Exception as e:
            if gpu:
                print(f'[PaddleOCR] GPU failed ({e}), trying CPU...')
                continue
            raise
    return None


# ─── Auto-labeler: assigns BIO labels to words using regex ────────────────────
import re

_GSTIN_RE = r'[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]'
_DATE_RE  = r'\d{1,2}[-/]\w{3,9}[-/]\d{2,4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}'
_HSN_VALID = {'1005','4017','8302','9987','9988','8471','8523','8524','9983',
              '9984','9985','9986','0101','3004','3305','6109','6203',
              '7214','7308','8414','8482','8544','9506'}


def label_word(word, context_text, extracted):
    """
    Assign a BIO label to a single word based on:
    - Direct regex match
    - Extracted field values (from the full text extraction)
    - Context from surrounding text
    """
    w = word.strip().upper()
    if not w: return 'O'

    # ── GSTIN ────────────────────────────────────────────────────────────────
    if re.match(_GSTIN_RE, w, re.IGNORECASE):
        gstin_s = extracted.get('GSTIN_S','').upper()
        gstin_r = extracted.get('GSTIN_R','').upper()
        if w == gstin_s: return 'B-GSTIN_S'
        if w == gstin_r: return 'B-GSTIN_R'
        return 'B-GSTIN_S'  # default first occurrence = supplier

    # ── Invoice No ───────────────────────────────────────────────────────────
    inv_no = extracted.get('INV_NO','')
    if inv_no and (w == inv_no.upper() or w in inv_no.upper()):
        return 'B-INV_NO'

    # ── Invoice Date ─────────────────────────────────────────────────────────
    inv_date = extracted.get('INV_DATE','')
    if inv_date and re.match(_DATE_RE, word, re.IGNORECASE):
        if word.replace(' ','-') in inv_date or inv_date in word:
            return 'B-INV_DATE'

    # ── HSN ──────────────────────────────────────────────────────────────────
    hsn = extracted.get('HSN','')
    if w in _HSN_VALID or (hsn and w in hsn.upper()):
        if re.match(r'^\d{4,6}$', w): return 'B-HSN'

    # ── Amounts ──────────────────────────────────────────────────────────────
    def _amt_match(field):
        val = extracted.get(field,'')
        if not val: return False
        try:
            fv = float(str(val).replace(',',''))
            fw = float(re.sub(r'[,₹]','', word))
            return abs(fv - fw) < 0.5
        except (ValueError, TypeError):
            return False

    if _amt_match('TAXABLE'): return 'B-TAXABLE'
    if _amt_match('CGST'):    return 'B-CGST'
    if _amt_match('SGST'):    return 'B-SGST'
    if _amt_match('IGST'):    return 'B-IGST'
    if _amt_match('TOTAL'):   return 'B-TOTAL'
    if _amt_match('RATE'):    return 'B-RATE'

    # ── GST rates ─────────────────────────────────────────────────────────────
    if re.match(r'^(5|9|12|14|18|28)%?$', w):
        if '9' in w: return 'B-CGST_RATE'
        return 'B-CGST_RATE'

    # ── Supplier name ────────────────────────────────────────────────────────
    supplier = extracted.get('SUPPLIER','').upper()
    if supplier and len(w) > 3:
        words_in_supplier = supplier.split()
        if w in words_in_supplier:
            idx = words_in_supplier.index(w)
            return 'B-SUPPLIER' if idx == 0 else 'I-SUPPLIER'

    # ── Recipient name ────────────────────────────────────────────────────────
    recipient = extracted.get('RECIPIENT','').upper()
    if recipient and len(w) > 3:
        words_in_rec = recipient.split()
        if w in words_in_rec:
            idx = words_in_rec.index(w)
            return 'B-RECIPIENT' if idx == 0 else 'I-RECIPIENT'

    return 'O'


# ─── Process one image ────────────────────────────────────────────────────────
def process_image(img_path, ocr_engine):
    """Run OCR + auto-label on one image. Returns list of (word, bbox, label)."""
    from converter.utils.pdf_processor import preprocess_image
    from converter.utils.layoutlm_extractor import full_extract, fix_ocr_noise

    # Preprocess
    try:
        img = Image.open(img_path).convert('RGB')
        img_pre = preprocess_image(img, aggressive=(img.size[0]*img.size[1] > 1500000))
    except Exception as e:
        print(f'  [ERROR] Cannot open {img_path}: {e}')
        return []

    W, H = img_pre.size

    # OCR
    try:
        arr    = np.array(img_pre)
        result = ocr_engine.ocr(arr, cls=True)
        if not result or not result[0]:
            print(f'  [WARN] No OCR result for {os.path.basename(img_path)}')
            return []
    except Exception as e:
        print(f'  [ERROR] OCR failed for {img_path}: {e}')
        return []

    # Build word list + full text
    word_data   = []
    full_text_lines = []
    for line in result[0]:
        if not line or len(line) < 2: continue
        box_pts   = line[0]; ti = line[1]
        if not ti: continue
        text = str(ti[0]).strip()
        conf = float(ti[1]) if len(ti) > 1 else 1.0
        if not text or conf < 0.3: continue
        full_text_lines.append(text)
        xs = [p[0] for p in box_pts]; ys = [p[1] for p in box_pts]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        norm = [max(0,int(1000*x0/W)), max(0,int(1000*y0/H)),
                min(1000,int(1000*x1/W)), min(1000,int(1000*y1/H))]
        # Split line into individual words with interpolated boxes
        sub_words = text.split()
        if not sub_words: continue
        if len(sub_words) == 1:
            word_data.append({'word': text, 'bbox': norm, 'conf': conf})
        else:
            tw = norm[2]-norm[0]; chars=[len(sw) for sw in sub_words]
            tc = sum(chars) or 1; xc = norm[0]
            for sw, cc in zip(sub_words, chars):
                sw_w = int(tw*cc/tc)
                word_data.append({
                    'word': sw, 'conf': conf,
                    'bbox': [xc, norm[1], xc+sw_w, norm[3]]
                })
                xc += sw_w

    full_text = '\n'.join(full_text_lines)

    # Extract fields from OCR text
    cleaned   = fix_ocr_noise(full_text)
    extracted = {}
    for t in [cleaned, full_text]:
        for k, v in full_extract(t).items():
            if k not in extracted and v: extracted[k] = v

    # Label each word
    labeled = []
    for item in word_data:
        lbl = label_word(item['word'], full_text, extracted)
        labeled.append({
            'word':  item['word'],
            'bbox':  item['bbox'],
            'label': lbl,
        })

    return labeled, extracted, full_text


# ─── Save label file ──────────────────────────────────────────────────────────
def save_label_file(labeled_words, label_path):
    """Save word-level labels in CoNLL-like format: WORD LABEL"""
    with open(label_path, 'w', encoding='utf-8') as f:
        for item in labeled_words:
            word = item['word'].replace('\t', ' ').replace('\n', ' ')
            f.write(f"{word}\t{item['label']}\n")


# ─── Build HuggingFace dataset ────────────────────────────────────────────────
def build_hf_dataset(all_samples):
    """
    Create a JSON dataset file suitable for fine-tuning LayoutLMv3.
    Format: list of {id, words, bboxes, labels, image_path}
    """
    dataset_path = os.path.join(HF_DIR, 'dataset.json')
    with open(dataset_path, 'w', encoding='utf-8') as f:
        json.dump(all_samples, f, indent=2, ensure_ascii=False)
    print(f'\n[Dataset] Saved {len(all_samples)} samples → {dataset_path}')

    # Train/val split (80/20)
    split_idx   = int(len(all_samples) * 0.8)
    train_data  = all_samples[:split_idx]
    val_data    = all_samples[split_idx:]
    for split, data in [('train', train_data), ('val', val_data)]:
        path = os.path.join(HF_DIR, f'{split}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f'[Dataset] {split}: {len(data)} samples → {path}')


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print('='*60)
    print('  GST Dataset Auto-Labeler  v3.0')
    print('='*60)

    # Find all images
    SUPPORTED = ('.jpg','.jpeg','.png','.gif','.webp','.jfif','.bmp','.tiff')
    image_files = []
    for fname in sorted(os.listdir(IMAGES_DIR)):
        if os.path.splitext(fname)[1].lower() in SUPPORTED:
            image_files.append(os.path.join(IMAGES_DIR, fname))

    if not image_files:
        print(f'\n[ERROR] No images found in {IMAGES_DIR}')
        print('Please copy your invoice images to that folder and run again.')
        sys.exit(1)

    print(f'\nFound {len(image_files)} images in {IMAGES_DIR}')

    # Load OCR
    print('\nLoading PaddleOCR...')
    ocr = get_ocr()

    all_samples = []
    report_lines = ['GST Dataset Labeling Report', '='*50, '']
    ok_count  = 0
    err_count = 0

    for i, img_path in enumerate(image_files, 1):
        fname = os.path.basename(img_path)
        print(f'\n[{i}/{len(image_files)}] {fname}')

        # Convert to supported format
        converted = to_png(img_path)
        if not converted:
            err_count += 1
            report_lines.append(f'[SKIP] {fname} — cannot convert')
            continue

        t0 = time.time()
        try:
            result = process_image(converted, ocr)
            if not result:
                err_count += 1
                report_lines.append(f'[FAIL] {fname} — no OCR output')
                continue

            labeled_words, extracted, full_text = result
        except Exception as e:
            print(f'  [ERROR] {e}')
            err_count += 1
            report_lines.append(f'[FAIL] {fname} — {e}')
            continue

        elapsed = time.time() - t0

        # Save label file
        stem       = os.path.splitext(fname)[0]
        label_path = os.path.join(LABELS_DIR, f'{stem}.txt')
        save_label_file(labeled_words, label_path)

        # Build HF sample
        words  = [item['word']  for item in labeled_words]
        bboxes = [item['bbox']  for item in labeled_words]
        labels = [LABEL2ID.get(item['label'], 0) for item in labeled_words]

        all_samples.append({
            'id':         str(i),
            'image_path': converted,
            'words':      words,
            'bboxes':     bboxes,
            'labels':     labels,
            'label_names': [item['label'] for item in labeled_words],
        })

        # Count labeled fields
        non_o = sum(1 for item in labeled_words if item['label'] != 'O')
        fields_found = list(extracted.keys())

        print(f'  ✓ {len(words)} words, {non_o} labeled  |  fields: {fields_found[:8]}')
        print(f'    Time: {elapsed:.1f}s  |  Labels: {label_path}')

        report_lines.append(
            f'[OK] {fname}  |  {len(words)} words  |  '
            f'{non_o} labeled  |  fields: {",".join(fields_found[:6])}  |  {elapsed:.1f}s'
        )
        ok_count += 1

    # Build HF dataset
    build_hf_dataset(all_samples)

    # Write report
    report_lines += [
        '',
        f'Total: {len(image_files)} images',
        f'  Success: {ok_count}',
        f'  Failed:  {err_count}',
        '',
        'Next step: Review labels in dataset/labels/*.txt',
        'Then run:  python finetune_layoutlm.py',
    ]
    with open(REPORT_PATH, 'w') as f:
        f.write('\n'.join(report_lines))
    print(f'\n[Report] {REPORT_PATH}')

    print('\n' + '='*60)
    print(f'  Done!  {ok_count}/{len(image_files)} images labeled.')
    print(f'  Dataset: {HF_DIR}')
    print('  Next:    python finetune_layoutlm.py')
    print('='*60)


if __name__ == '__main__':
    main()
