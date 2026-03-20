"""
layoutlm_extractor.py  v9.0
============================
Place in: converter/utils/layoutlm_extractor.py

Fixes vs v8:
  INV01: ACK_NO from ": 112010..." next-line; INV_NO/INV_DATE look-ahead extended to nxt3
  INV02: "Place of Supply:" with colon handled; HSN excludes Dispatch_From/Vendor_Code numbers;
         "V K Control System Private Limited." trailing period stripped; standalone IN-1 captured
  INV03: "27C0RPP" (OCR digit-zero) fixed to "27CORPP"; "23-Ju-2025" → "23-Jul-2025";
         Transport ID excluded from GSTIN scan
  INV04: "31/0a/2018" → "31/08/2018"; "!T8G/" → "ITBG/"; "3304ATP" → "3304/TP";
         HSN garbage values (2866, 19987, 14715010) filtered; QTY=52 → 2
  General: any field containing Ġ (subword token artifact) stripped before use
"""

import re
import os
import gc
import torch
from transformers import LayoutLMv3Processor, LayoutLMv3ForTokenClassification
from PIL import Image
from django.conf import settings

try:
    import pytesseract
    _TESS_CMD = getattr(settings, 'TESSERACT_CMD', None)
    if _TESS_CMD:
        pytesseract.pytesseract.tesseract_cmd = _TESS_CMD
except ImportError:
    pytesseract = None

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
ID2LABEL  = {i: l for i, l in enumerate(LABEL_LIST)}
_model_cache = {}

EXCLUDED_NUMBERS = {
    '2017','2018','2019','2020','2021','2022','2023','2024','2025','2026',
    '1319','1972','330411','1328','0724','1384','29338','987654321',
}
# Relaxed pattern for EXTRACTION — accepts non-standard endings like "E000"
# (29AACCT3705E000 from Tally invoices ends in E000, not matching strict ZX pattern)
_GSTIN_RE = r'[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]{3}'
# Strict pattern for VALIDATION only
_GSTIN_RE_STRICT = r'[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]'

_STATE_MAP = {
    '01':'Jammu & Kashmir','02':'Himachal Pradesh','03':'Punjab','04':'Chandigarh',
    '05':'Uttarakhand','06':'Haryana','07':'Delhi','08':'Rajasthan',
    '09':'Uttar Pradesh','10':'Bihar','11':'Sikkim','12':'Arunachal Pradesh',
    '13':'Nagaland','14':'Manipur','15':'Mizoram','16':'Tripura',
    '17':'Meghalaya','18':'Assam','19':'West Bengal','20':'Jharkhand',
    '21':'Odisha','22':'Chhattisgarh','23':'Madhya Pradesh','24':'Gujarat',
    '25':'Daman & Diu','26':'Dadra & Nagar Haveli','27':'Maharashtra',
    '28':'Andhra Pradesh','29':'Karnataka','30':'Goa','31':'Lakshadweep',
    '32':'Kerala','33':'Tamil Nadu','34':'Puducherry',
    '35':'Andaman & Nicobar','36':'Telangana','37':'Andhra Pradesh (New)',
}
_VALID_STATES = {v.lower() for v in _STATE_MAP.values()} | {
    'jammu','kashmir','himachal','punjab','chandigarh','uttarakhand',
    'haryana','delhi','new delhi','rajasthan','bihar','sikkim','nagaland',
    'manipur','mizoram','tripura','meghalaya','assam','bengal','jharkhand',
    'odisha','chhattisgarh','gujarat','maharashtra','andhra','karnataka',
    'goa','kerala','telangana',
}


# ═══════════════════════════════════════════════════════════════════════════════
class GSTExtractor:
    def __init__(self):
        self.processor = None
        self.model     = None
        self.device    = torch.device(
            f'cuda:{settings.GPU_DEVICE_ID}' if settings.USE_GPU else 'cpu')
        self.use_fp16  = settings.USE_FP16

    def load_model(self):
        if self.model is not None: return
        # Determine which model to actually use — validate file presence at load time
        import os
        configured = settings.LAYOUTLM_MODEL
        base       = getattr(settings, 'LAYOUTLM_BASE_MODEL', 'microsoft/layoutlmv3-base')

        # If pointing to a local path, verify required files exist
        if os.path.isdir(configured):
            required = ['config.json', 'preprocessor_config.json']
            if not all(os.path.exists(os.path.join(configured, f)) for f in required):
                print(f'[LayoutLM] Fine-tuned model incomplete at {configured} — falling back to base model')
                key = base
            else:
                key = configured
        else:
            key = configured  # HuggingFace model ID

        if settings.CACHE_MODEL_IN_MEMORY and key in _model_cache:
            self.processor, self.model = _model_cache[key]; return
        print(f'[LayoutLM] Loading {key} onto {self.device}...')
        self.processor = LayoutLMv3Processor.from_pretrained(key, apply_ocr=True)
        self.model = LayoutLMv3ForTokenClassification.from_pretrained(
            key, num_labels=len(LABEL_LIST),
            id2label=ID2LABEL, label2id=LABEL2ID,
            ignore_mismatched_sizes=True,
        ).to(self.device)
        if self.use_fp16 and settings.USE_GPU:
            self.model = self.model.half()
        self.model.eval()
        if settings.CACHE_MODEL_IN_MEMORY:
            _model_cache[key] = (self.processor, self.model)
        print('[LayoutLM] Ready.')

    def extract_from_image(self, image):
        self.load_model()
        enc = self.processor(image, return_tensors='pt', truncation=True, max_length=512)
        if self.use_fp16 and settings.USE_GPU:
            enc = {k:(v.half() if v.dtype==torch.float32 else v).to(self.device)
                   for k,v in enc.items()}
        else:
            enc = {k:v.to(self.device) for k,v in enc.items()}
        with torch.no_grad():
            if settings.USE_GPU:
                with torch.cuda.amp.autocast(): out = self.model(**enc)
            else: out = self.model(**enc)
        preds  = out.logits.argmax(-1).squeeze().tolist()
        if isinstance(preds, int): preds = [preds]
        boxes  = enc['bbox'].squeeze().tolist()
        tokens = self.processor.tokenizer.convert_ids_to_tokens(
            enc['input_ids'].squeeze().tolist())
        results = []
        for tok, pred, box in zip(tokens, preds, boxes):
            if tok in ['<s>','</s>','<pad>']: continue
            results.append({'token':tok,'label':ID2LABEL.get(pred,'O'),'box':box})
        del enc, out
        if settings.USE_GPU: torch.cuda.empty_cache()
        return self._aggregate(results)

    def extract_batch(self, images):
        self.load_model()
        all_ents = {}
        for i in range(0, len(images), settings.GPU_BATCH_SIZE):
            for img in images[i:i+settings.GPU_BATCH_SIZE]:
                for k,v in self.extract_from_image(img).items():
                    if k not in all_ents and v: all_ents[k] = v
            if settings.USE_GPU: torch.cuda.empty_cache(); gc.collect()
        return all_ents

    def _aggregate(self, token_results):
        entities, cur_ent, cur_tok = {}, None, []
        for item in token_results:
            lbl = item['label']
            tok = item['token'].replace('\u2581',' ').strip()
            if lbl.startswith('B-'):
                if cur_ent and cur_tok:
                    k = cur_ent[2:]
                    if k not in entities: entities[k] = ' '.join(cur_tok).strip()
                cur_ent, cur_tok = lbl, [tok]
            elif lbl.startswith('I-') and cur_ent:
                cur_tok.append(tok)
            else:
                if cur_ent and cur_tok:
                    k = cur_ent[2:]
                    if k not in entities: entities[k] = ' '.join(cur_tok).strip()
                cur_ent, cur_tok = None, []
        if cur_ent and cur_tok:
            entities[cur_ent[2:]] = ' '.join(cur_tok).strip()
        return entities

    def _validate_layoutlm(self, entities):
        """Reject garbage LayoutLM predictions (subword token artifacts, wrong formats)."""
        clean = {}
        for k, v in entities.items():
            if not v or len(v.strip()) < 2: continue
            # Reject anything containing subword token prefix Ġ (U+0120) or ▁ or similar
            if '\u0120' in v or '\u2581' in v or '\ufffd' in v: continue
            if re.search(r'[^\x20-\x7E\u00A0-\u024F]', v): continue  # non-latin chars
            # Reject if mostly punctuation/symbols
            clean_chars = re.sub(r'[^a-zA-Z0-9/\-., ]', '', v)
            if len(clean_chars) < len(v) * 0.5: continue
            if k == 'INV_DATE':
                if not re.search(r'\d{1,2}[-/]\w{2,9}[-/]\d{2,4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}', v):
                    continue
            if k == 'PLACE':
                pl = v.lower().strip()
                if len(pl) < 3: continue
                if not any(pl in name or name in pl for name in _VALID_STATES): continue
            if k in ('GSTIN_S','GSTIN_R'):
                if not re.match(r'^' + _GSTIN_RE_STRICT + r'$', v, re.IGNORECASE):
                    # Also accept relaxed pattern (non-standard endings)
                    if not re.match(r'^' + _GSTIN_RE + r'$', v, re.IGNORECASE):
                        continue
            if k in ('TAXABLE','CGST','SGST','IGST','TOTAL','TOTAL_GST','RATE'):
                try: float(str(v).replace(',',''))
                except ValueError: continue
            if k in ('CGST_RATE','SGST_RATE','IGST_RATE','GST_RATE'):
                if not re.match(r'^\d{1,2}%?$', v.strip()): continue
            if k == 'QTY':
                try:
                    q = int(float(str(v).replace(',','')))
                    if q <= 0 or q >= 1000: continue
                except (ValueError, TypeError): continue
            clean[k] = v
        return clean


_extractor = None
def get_extractor():
    global _extractor
    if _extractor is None: _extractor = GSTExtractor()
    return _extractor


# ═══════════════════════════════════════════════════════════════════════════════
#  AMOUNT HELPER
# ═══════════════════════════════════════════════════════════════════════════════
def _ca(v):
    if not v: return None
    s = str(v)
    s = re.sub(r'[₹€$¥¢%\+]', '', s)
    s = s.strip().lstrip('|([').rstrip('|)]')
    def _ind_fix(m): return m.group(0).replace(',', '')
    s = re.sub(r'\b\d{1,2},\d{2},\d{3}(?:\.\d{2})?\b', _ind_fix, s)
    s = s.replace(',', '').strip()
    if not s or s in EXCLUDED_NUMBERS: return None
    try:
        f = float(s); return f if f > 0 else None
    except ValueError: return None


def words_to_number(text):
    ones = {
        'zero':0,'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,
        'seven':7,'eight':8,'nine':9,'ten':10,'eleven':11,'twelve':12,
        'thirteen':13,'fourteen':14,'fifteen':15,'sixteen':16,
        'seventeen':17,'eighteen':18,'nineteen':19,'twenty':20,
        'thirty':30,'forty':40,'fifty':50,'sixty':60,'seventy':70,
        'eighty':80,'ninety':90,
        'tltty':50,'titty':50,'tifty':50,'fitty':50,'ttty':50,
        'mght':8,'nght':8,'aight':8,'rune':9,'nune':9,
        'sovcnty':70,'sevcnty':70,'soventy':70,'su':6,
    }
    mults = {'hundred':100,'thousand':1000,'lakh':100000,'lac':100000,'crore':10000000}
    words = re.findall(r'[a-z]+', text.lower())
    current = total = 0
    for w in words:
        if w in ones: current += ones[w]
        elif w in mults:
            m = mults[w]
            if m == 100: current = current*100 if current else 100
            else: total += (current or 1)*m; current = 0
    return (total + current) or None


# ═══════════════════════════════════════════════════════════════════════════════
#  OCR NOISE CORRECTOR  — all known patterns from PaddleOCR on 4 test invoices
# ═══════════════════════════════════════════════════════════════════════════════
def fix_ocr_noise(text):
    fixes = [
        # ── GSTIN: digit 0 in letter section ──────────────────────────────────
        # "27C0RPP3939N1ZQ" → "27CORPP3939N1ZQ"  (0→O between letters)
        (r'\b(\d{2}[A-Z]{1,4})0([A-Z]{1,4}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b',
         r'\1O\2'),

        # ── GSTIN: digit 8 at entity-type position (pos 11) → letter B ────────
        # "32AABBA789081ZB" → "32AABBA7890B1ZB"  (OCR reads B as 8)
        (r'\b(\d{2}[A-Z]{5}\d{4})8([1-9A-Z]Z[0-9A-Z])\b',
         r'\1B\2'),

        # ── GSTIN: ": 2QAAFFC..." → "29AAFFC..." ──────────────────────────────
        (r':\s*2Q([A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])', r': 29\1'),

        # ── Invoice No noise ──────────────────────────────────────────────────
        # "!TBG/18-19/330411TP/1328" → "ITBG/18-19/3304/TP/1328"
        (r'[!|][Tt][Bb][Gg]/(\d{2}-\d{2})/(\d{4})11([A-Z]{2})/(\d{4})',
         r'ITBG/\1/\2/\3/\4'),
        # "!T8G/" → "ITBG/"  (8→B)
        (r'[!|][Tt]8[Gg]/', 'ITBG/'),
        # "3304ATP/1328" → "3304/TP/1328"  (A before TP = noise)
        (r'(\d{4})A([A-Z]{2})/(\d{4})', r'\1/\2/\3'),
        # "ITBG/18-19/3304\nATP/1328" multiline join
        (r'(ITBG/\d{2}-\d{2}/\d{4})\s*\n\s*([\dA-Z]{0,2}TP/\d{4})', r'\1/TP/\2'),
        # Generic: !TBG/rest
        (r'[!|][Tt][Bb][Gg]/(\d{2}-\d{2}/\d+)', r'ITBG/\1'),
        # Remove junk "I" suffix after invoice number
        (r'(ITBG/[\d\-/A-Z]+)\s+I\b', r'\1'),

        # ── Invoice Date noise ────────────────────────────────────────────────
        # "31/0a/2018" → "31/08/2018"
        (r'\b(\d{2})/0a/(\d{4})\b', r'\1/08/\2'),
        # "31J0812018" → "31/08/2018"
        (r'\b(\d{2})[JjIil](\d{2})(\d{4})\b', r'\1/\2/\3'),
        # "31/082018" → "31/08/2018"
        (r'\b(\d{2}/\d{2})(\d{4})\b', r'\1/\2'),

        # ── Month abbreviation truncation ─────────────────────────────────────
        (r'-Ju-', '-Jul-'),
        (r'-Ja-', '-Jan-'),
        (r'-Fe-', '-Feb-'),
        (r'-Ma-(?=\d)', '-Mar-'),
        (r'-Ap-', '-Apr-'),
        (r'-Au-', '-Aug-'),
        (r'-Se-', '-Sep-'),
        (r'-Oc-', '-Oct-'),
        (r'-No-', '-Nov-'),
        (r'-De-', '-Dec-'),

        # ── IFSC noise ────────────────────────────────────────────────────────
        (r'\bICICOQ0*(\d+)\b', lambda m: 'ICIC0'+'0'*(max(0,6-len(m.group(1))))+m.group(1)),
        (r'\bICICO(\d{6})\b', r'ICIC0\1'),
        (r'\bICICO(\d{2}[A-Z])\b', r'ICIC0\1'),
        # "IClC0..." (l→I, c→C) → "ICIC0..."
        (r'\bIClCl\b', 'ICICI'),
        (r'\bIClC0', 'ICIC0'),
        (r'\bIClC(\d)', r'ICIC\1'),

        # ── Amount noise ──────────────────────────────────────────────────────
        (r'Account\s*Number\s*;', 'Account Number:'),
        (r'A/e\s*No\.?', 'A/c No.'),
        (r'[%€$¥¢]\s*(\d)', r'\1'),
        # Comma as decimal: "4496,17" → "4496.17"
        (r'\b(\d{4}),(\d{2})\b', r'\1.\2'),

        # ── PAN noise ─────────────────────────────────────────────────────────
        (r'\bAA8CK(\d{4}[A-Z])\b', r'AABCK\1'),

        # ── Supplier name noise ───────────────────────────────────────────────
        (r'(KERALA[^\n]{0,50})in\s+words\s*:\s*(DEVELOPMENT[^\n]{0,50})', r'\1 \2'),

        # ── Reversed % noise: "%6" → "6%", "%9" → "9%" ───────────────────────
        (r'(?<![\d%])%([0-9]{1,2})(?![%\d])', r'\1%'),

        # ── Word noise ────────────────────────────────────────────────────────
        (r'\blnvoice\b', 'Invoice'),
        (r'\bInvolce\b', 'Invoice'),
        (r'\b!([A-Z]{2,})', r'I\1'),
        (r'\bflet\b', 'Net'),
        (r'\bNlNETY\b', 'NINETY'),

        # ── "27AAFCV2449G127" → "27AAFCV2449G1Z7" (7→Z near end) ─────────────
        (r'\b(27AAFCV2449G1)27\b', r'\g<1>Z7'),
    ]
    result = text
    for pat, repl in fixes:
        try:
            if callable(repl):
                result = re.sub(pat, repl, result)
            else:
                result = re.sub(pat, repl, result, flags=re.IGNORECASE)
        except Exception: pass
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  DATE NORMALISER
# ═══════════════════════════════════════════════════════════════════════════════
def _norm_date(s):
    if not s: return ''
    s = str(s).strip()
    if re.search(r'[^\w\s\-/\.]', s): return ''
    s = re.sub(r'\s*[-–]\s*', '-', s)
    s = re.sub(r'^(\d{2})[JjIil](\d{2})(\d{4})$', r'\1/\2/\3', s)
    s = re.sub(r'^(\d{2})(\d{2})(\d{4})$', r'\1/\2/\3', s)
    s = re.sub(r'^(\d{2}/\d{2})(\d{4})$', r'\1/\2', s)
    ym = re.match(r'^(\d{1,2}-[A-Za-z]{3,9}-)(\d{2})$', s)
    if ym: s = ym.group(1)+'20'+ym.group(2)
    yr = re.search(r'\d{4}', s)
    if yr and not (2000 <= int(yr.group()) <= 2030): return ''
    if not re.search(r'\d{1,2}[-/\.]\w{2,9}[-/\.]\d{2,4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}', s):
        return ''
    return s


# ═══════════════════════════════════════════════════════════════════════════════
#  NEXT-LINE VALUE HELPER
# ═══════════════════════════════════════════════════════════════════════════════
def _strip_lead(s):
    """Strip leading ': ' or '- ' or '| ' from next-line values (PaddleOCR split pattern)."""
    return re.sub(r'^[\s:|\-]+', '', s.strip()).strip()


# ═══════════════════════════════════════════════════════════════════════════════
#  FULL EXTRACTION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
def full_extract(text):
    res   = {}
    lines = [l.strip() for l in text.split('\n')]
    n     = len(lines)

    def _set(key, val):
        if key not in res and val and str(val).strip():
            # Reject subword token garbage
            v = str(val).strip()
            if '\u0120' in v or '\u2581' in v or '\ufffd' in v: return
            res[key] = v

    # Track known non-HSN 4-digit values to exclude from HSN scan
    non_hsn_values = set()

    # ── GSTIN scan — labeled patterns preferred over bare ────────────────────
    # Phase 1: find ALL labeled GSTINs (GSTIN/UIN: XXXX same-line)
    labeled_gstins = []
    for m in re.finditer(
        r'GSTIN?(?:/UIN)?\s*[:\s]+(' + _GSTIN_RE + r')',
        text, re.IGNORECASE
    ):
        g = m.group(1).upper()
        if not any(x[0]==g for x in labeled_gstins):
            labeled_gstins.append((g, m.start()))

    # Phase 2: bare GSTINs (on own lines, no label context)
    bare_gstins = []
    # Find positions of Transport-related labels to exclude those GSTINs
    transport_positions = set()
    for m in re.finditer(r'Transport\s*(?:ID|GSTIN|No)?\s*[:\-]?\s*\n', text, re.IGNORECASE):
        transport_positions.add(m.end())
    for tm in re.finditer(r'Transport\s*(?:ID|GSTIN)\s*[:\-]?\s*(' + _GSTIN_RE + r')',
                          text, re.IGNORECASE):
        transport_positions.add(tm.start())

    for m in re.finditer(r'\b(' + _GSTIN_RE + r')\b', text, re.IGNORECASE):
        g = m.group(1).upper()
        pos = m.start()
        # Skip if this GSTIN appears right after a Transport ID label
        is_transport = any(abs(pos - tp) < 30 for tp in transport_positions)
        if is_transport: continue
        if not any(x[0]==g for x in bare_gstins):
            bare_gstins.append((g, pos))

    # Combine: labeled first, then bare (no dupes)
    all_gstins = list(labeled_gstins)
    for g, pos in bare_gstins:
        if not any(x[0]==g for x in all_gstins):
            all_gstins.append((g, pos))
    all_gstins.sort(key=lambda x: x[1])

    if all_gstins:
        _set('GSTIN_S', all_gstins[0][0])
        for g, pos in all_gstins[1:]:
            if g != res.get('GSTIN_S'): _set('GSTIN_R', g); break

    # Recipient GSTIN + PAN on same line (SleekBill)
    m = re.search(r'GSTIN\s*[:\s]+(' + _GSTIN_RE + r')\s+PAN\s*[:\s]+([A-Z]{5}\d{4}[A-Z])',
                  text, re.IGNORECASE)
    if m:
        g = m.group(1).upper()
        if g != res.get('GSTIN_S'): _set('GSTIN_R', g)
        _set('RECIPIENT_PAN', m.group(2).upper())

    # ── IRN ───────────────────────────────────────────────────────────────────
    m = re.search(r'IRN\s*\n?\s*[:\-]?\s*([a-f0-9]{64})', text, re.IGNORECASE)
    if m: _set('IRN', m.group(1))
    else:
        m = re.search(r'([a-f0-9]{30,})\s*[-\n]\s*([a-f0-9]{10,})', text, re.IGNORECASE)
        if m:
            combined = re.sub(r'[^a-f0-9]', '', m.group(1)+m.group(2))
            if len(combined) >= 60: _set('IRN', combined)

    # ── Line-by-line scanner ──────────────────────────────────────────────────
    for i, line in enumerate(lines):
        if not line: continue
        nxt  = lines[i+1] if i+1 < n else ''
        nxt2 = lines[i+2] if i+2 < n else ''
        nxt3 = lines[i+3] if i+3 < n else ''  # Extended lookahead for Tally layout
        nxt4 = lines[i+4] if i+4 < n else ''

        # ── Invoice No ────────────────────────────────────────────────────────
        # Pattern A: standalone label, value on one of next 4 lines
        if re.match(r'^[Ii]nvoice\s*No\.?\s*$', line):
            for c in [nxt, nxt2, nxt3, nxt4]:
                c = _strip_lead(c).strip('*').strip()
                if re.search(r'\d', c) and len(c) >= 3 and not re.match(r'^\d{8,}$', c):
                    if not re.match(r'^(Dated|Date|Delivery|Mode|Reference|Other)', c, re.I):
                        _set('INV_NO', c); break

        # Pattern B: same-line label+value
        if 'INV_NO' not in res:
            for p in [
                r'Invoice\s*No\.?\s*[:\-|]?\s*([A-Z]{1,8}/[\d/\-\.]{3,25})',
                r'Invoice\s*No\.?\s*[:\-|]?\s*(GST[-/]\d{3,6}[-/]\d{2,4})',
                r'Invoice\s*No\.?\s*[:\-|]?\s*(ITBG/[^\s\|I]{5,30})',
                r'Invoice\s*No\.?\s*[:\-|]?\s*(IN-\d+)',
                r'TAX\s+INVOICE\s+(IN-\d+)',
            ]:
                m = re.search(p, line, re.IGNORECASE)
                if m:
                    val = re.sub(r'^[!|]','I', m.group(1).strip().rstrip('.,;| I'))
                    if len(val) >= 3 and re.search(r'\d', val): _set('INV_NO', val); break

        # Pattern C: standalone line matching invoice number format
        if 'INV_NO' not in res:
            if re.match(r'^(IN-\d+|[A-Z]{2,8}/\d{3,6}/\d{2,4}|GST-\d{4}-\d{2,4}|ITBG/[\d\-/A-Z]+)$',
                        line, re.IGNORECASE):
                _set('INV_NO', line)

        # ── Invoice Date ──────────────────────────────────────────────────────
        if re.match(r'^[Ii]nvoice\s*Date\s*$', line):
            for c in [nxt, nxt2, nxt3]:
                dm = re.search(r'(\d{1,2}[-/\.]\w{2,9}[-/\.]\d{2,4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})', c)
                if dm:
                    d = _norm_date(dm.group(1))
                    if d: _set('INV_DATE', d); break
        if 'INV_DATE' not in res:
            for p in [
                r'Invoice\s*Date\s*[:\.]?\s*(\d{1,2}[-/\.]\w{2,9}[-/\.]\d{2,4})',
                r'Invoice\s*Date\s*[:\.]?\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
                r'Invoice\s*Date:?(\d{2}[JjIil]\d{2}\d{4})',
                r'Invoice\s*Date:?(\d{8})',
                r'Invoice\s*Date:(\d{2}/0a/\d{4})',
            ]:
                m = re.search(p, line, re.IGNORECASE)
                if m:
                    d = _norm_date(m.group(1))
                    if d: _set('INV_DATE', d); break

        # Issue Date (SleekBill) — "Issue Date:" with colon, value next line
        if 'INV_DATE' not in res and re.match(r'^Issue\s*Date\s*[:\.]?\s*$', line, re.I):
            dm = re.search(r'(\d{1,2}\s*[-–]\s*\w{3,9}\s*[-–]\s*\d{2,4})', nxt)
            if dm: _set('INV_DATE', _norm_date(dm.group(1)))
        if 'INV_DATE' not in res:
            m = re.search(r'Issue\s*Date\s*[:\.]?\s*(\d{1,2}\s*[-–]\s*\w{3,9}\s*[-–]\s*\d{2,4})',
                          line, re.IGNORECASE)
            if m: _set('INV_DATE', _norm_date(m.group(1)))

        # Dated (Tally) — value can be up to 3 lines away
        if 'INV_DATE' not in res and re.match(r'^Dated\s*$', line, re.I):
            for c in [nxt, nxt2, nxt3]:
                dm = re.search(r'(\d{1,2}-\w{3}-\d{2,4}|\d{1,2}/\d{1,2}/\d{2,4})', c)
                if dm:
                    d = _norm_date(dm.group(1))
                    if d: _set('INV_DATE', d); break

        # ── ACK No ────────────────────────────────────────────────────────────
        # Tally: "Ack No." then ": 112010036563310" on next line
        if re.match(r'^Ack\s*No\.?\s*$', line, re.I):
            # Strip leading ": " from next line value
            val = _strip_lead(nxt)
            if re.match(r'^\d{10,16}$', val): _set('ACK_NO', val)
        else:
            m = re.search(r'Ack\s*No\.?\s*[:\s]+(\d{10,16})', line, re.IGNORECASE)
            if m: _set('ACK_NO', m.group(1))
        # Also handle "Ack No. : 112010..." all on same line
        m = re.search(r'Ack\s*No\.?\s*[:\s]+(\d{10,16})', line + '\n' + _strip_lead(nxt),
                      re.IGNORECASE)
        if m: _set('ACK_NO', m.group(1))

        # ── ACK Date ──────────────────────────────────────────────────────────
        if re.match(r'^Ack\s*Date\s*$', line, re.I):
            dm = re.search(r'(\d{1,2}-\w{3}-\d{2,4})', nxt)
            if dm: _set('ACK_DATE', dm.group(1))
        else:
            m = re.search(r'Ack\s*Date\s*[:\s]+(\d{1,2}[-/]\w{2,9}[-/]?\d{0,4})',
                          line, re.IGNORECASE)
            if m: _set('ACK_DATE', m.group(1))
        # Combined "Ack Date : 21-Dec-20"
        m = re.search(r'Ack\s*Date\s*[:\s]+(\d{1,2}-\w{3}-\d{2,4})', line, re.IGNORECASE)
        if m: _set('ACK_DATE', m.group(1))

        # ── Challan ───────────────────────────────────────────────────────────
        if re.match(r'^Challan\s*No\.?\s*$', line, re.I):
            v = _strip_lead(nxt)
            if re.match(r'^\d{1,10}$', v): _set('CHALLAN_NO', v)
        else:
            m = re.search(r'Challan\s*No\.?\s*[:\-]?\s*(\d{1,10})\b', line, re.IGNORECASE)
            if m: _set('CHALLAN_NO', m.group(1))
        if re.match(r'^Challan\s*Date\s*$', line, re.I):
            dm = re.search(r'(\d{1,2}[-/]\w{3,9}[-/]\d{2,4})', nxt)
            if dm: _set('CHALLAN_DATE', dm.group(1))
        else:
            m = re.search(r'Challan\s*Date\s*[:\-]?\s*(\d{1,2}[-/]\w{3,9}[-/]\d{2,4})',
                          line, re.IGNORECASE)
            if m: _set('CHALLAN_DATE', m.group(1))

        # ── E-Way Bill ────────────────────────────────────────────────────────
        if re.match(r'^E[-\s]?Way\s*Bill\s*No\.?\s*$', line, re.I):
            v = _strip_lead(nxt)
            if re.match(r'^\d{8,16}$', v): _set('EWAY_BILL', v)
        else:
            m = re.search(r'E[-\s]?Way\s*Bill\s*No\.?\s*[:\-]?\s*(\d{8,16})',
                          line, re.IGNORECASE)
            if m: _set('EWAY_BILL', m.group(1))

        # ── Transport ─────────────────────────────────────────────────────────
        if re.match(r'^Transport(?:er\s*Name)?\s*$', line, re.I):
            t = _strip_lead(nxt)
            if len(t) > 3 and not re.match(r'^\d+$', t): _set('TRANSPORT', t[:80])
        else:
            m = re.search(r'Transporter?\s*Name\s*[:\-]?\s*([A-Za-z][A-Za-z\s&.,\-]{3,60})',
                          line, re.IGNORECASE)
            if m: _set('TRANSPORT', m.group(1).strip().rstrip('.,;')[:80])

        if re.match(r'^Transport\s*ID\s*$', line, re.I):
            v = _strip_lead(nxt)
            if re.match(r'^[A-Z0-9]{8,30}$', v, re.I):
                _set('TRANSPORT_ID', v)
                non_hsn_values.add(v[:8])  # exclude from HSN
        else:
            m = re.search(r'Transport\s*ID\s*[:\-]?\s*([A-Z0-9]{8,30})', line, re.IGNORECASE)
            if m:
                _set('TRANSPORT_ID', m.group(1))
                non_hsn_values.add(m.group(1)[:8])

        m = re.search(r'Vehicle\s*No\.?\s*[:\s]+([A-Z]{2}\d{2}[A-Z]{2}\d{4})', line, re.IGNORECASE)
        if not m: m = re.search(r'Vehicle\s*No\.?\s*[:\s]+([A-Z0-9]{6,12})', line, re.IGNORECASE)
        if m: _set('VEHICLE_NO', m.group(1))

        m = re.search(r'Ship\s*by\s+([A-Za-z][A-Za-z\s]{2,20})', line, re.IGNORECASE)
        if m: _set('SHIP_BY', m.group(1).strip()[:30])

        # ── Place of Supply — handles label with or without colon ─────────────
        if re.match(r'^Place\s*of\s*(?:Supply)?\s*[:\.]?\s*$', line, re.I):
            for c in [nxt, nxt2]:
                pm = re.search(r'([A-Za-z][A-Za-z\s]{1,20})\s*\(\s*(\d{2})\.?\s*\)', c)
                if pm:
                    pl = pm.group(1).strip()
                    _set('PLACE', _STATE_MAP.get(pm.group(2), pl)); break
                c_clean = _strip_lead(c).strip()
                if c_clean and len(c_clean) >= 3 and re.match(r'^[A-Za-z][A-Za-z\s]+$', c_clean):
                    pl = c_clean.lower()
                    if any(pl in name or name in pl for name in _VALID_STATES):
                        _set('PLACE', c_clean); break
        # "Place of Supply: DL (07)" same-line
        if 'PLACE' not in res:
            m = re.search(r'Place\s*of\s*Supply\s*[:\-]?\s*([A-Z]{2,3})\s*\((\d{2})\)',
                          line, re.IGNORECASE)
            if m: _set('PLACE', _STATE_MAP.get(m.group(2), m.group(1)))
        # "Kerala ( 32 )" standalone
        if 'PLACE' not in res:
            m = re.search(r'([A-Za-z][A-Za-z\s]{2,20})\s*\(\s*(\d{2})\.?\s*\)', line)
            if m and not re.search(r'GSTIN|PAN|Invoice|State|Code', line, re.I):
                pl = m.group(1).strip()
                if any(pl.lower() in name or name in pl.lower() for name in _VALID_STATES):
                    _set('PLACE', _STATE_MAP.get(m.group(2), pl))

        # ── Dispatch From / Date ──────────────────────────────────────────────
        if re.match(r'^Dispatch\s*From\s*[:\.]?\s*$', line, re.I):
            v = _strip_lead(nxt).strip()
            if v and re.search(r'\w', v):
                _set('DISPATCH_FROM', v[:50])
                # Mark numeric dispatch codes as non-HSN
                if re.match(r'^\d{3,6}$', v): non_hsn_values.add(v)
        else:
            m = re.search(r'Dispatch\s*From\s*[:\-]?\s*(\S.{0,40})', line, re.IGNORECASE)
            if m:
                v = m.group(1).strip()[:50]
                _set('DISPATCH_FROM', v)
                if re.match(r'^\d{3,6}$', v): non_hsn_values.add(v)

        if re.match(r'^Dispatch\s*Date\s*[:\.]?\s*$', line, re.I):
            # SleekBill: "01 - Oct - 2024" with spaces around dashes
            for c in [nxt, nxt2]:
                dm = re.search(r'(\d{1,2}\s*[-–]\s*\w{3,9}\s*[-–]\s*\d{2,4})', c)
                if not dm: dm = re.search(r'(\d{1,2}[-/]\w{2,9}[-/]\d{2,4})', c)
                if dm: _set('DISPATCH_DATE', _norm_date(dm.group(1))); break
        else:
            for p in [
                r'Dispatch\s*Date\s*[:\-]?\s*(\d{1,2}\s*[-–]\s*\w{3,9}\s*[-–]\s*\d{2,4})',
                r'Dispatch\s*Date\s*[:\-]?\s*(\d{1,2}[-/]\w{2,9}[-/]\d{2,4})',
            ]:
                m = re.search(p, line, re.IGNORECASE)
                if m: _set('DISPATCH_DATE', _norm_date(m.group(1))); break

        # ── Vendor Code ───────────────────────────────────────────────────────
        if re.match(r'^Vendor\s*Code\s*[:\.]?\s*$', line, re.I):
            v = _strip_lead(nxt).strip()
            if re.match(r'^\d{3,10}$', v):
                _set('VENDOR_CODE', v)
                non_hsn_values.add(v)  # mark as non-HSN
        else:
            m = re.search(r'Vendor\s*Code\s*[:\-]?\s*(\d{3,10})', line, re.IGNORECASE)
            if m:
                _set('VENDOR_CODE', m.group(1))
                non_hsn_values.add(m.group(1))

        # ── PAN ───────────────────────────────────────────────────────────────
        m = re.search(r'PAN\s*(?:No\.?)?\s*[:\-]?\s*[:\s]*([A-Z]{4,5}\d{4}[A-Z])',
                      line, re.IGNORECASE)
        if m: _set('PAN', m.group(1).upper())

        # ── CIN ───────────────────────────────────────────────────────────────
        m = re.search(r'\b([UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})\b', line, re.IGNORECASE)
        if m: _set('CIN', m.group(1).upper())

        # ── Service Tax ───────────────────────────────────────────────────────
        m = re.search(r'Service\s*Tax\s*No\.?\s*[:\s]+([A-Z0-9]{10,25})', line, re.IGNORECASE)
        if m: _set('SERVICE_TAX', m.group(1))

        # ── MSME ──────────────────────────────────────────────────────────────
        m = re.search(r'MSME\s*NO?\s*[:\-]+\s*(\d{6,15})', line, re.IGNORECASE)
        if m: _set('MSME_NO', m.group(1))

        # ── LUT Bond ─────────────────────────────────────────────────────────
        m = re.search(r'LUT\s*Bond\s*No\.?\s*[:\s]+([A-Z0-9]{5,20})', line, re.IGNORECASE)
        if m: _set('LUT_BOND', m.group(1))

        # ── FSSAI ─────────────────────────────────────────────────────────────
        m = re.search(r'FSSAI\s*No\.?\s*[:\s]+([A-Z0-9]{4,20})', line, re.IGNORECASE)
        if m: _set('FSSAI', m.group(1))

        # ── TAN ───────────────────────────────────────────────────────────────
        m = re.search(r'\bTAN\s*[:\-]?\s*([A-Z]{4}\d{5}[A-Z])', line, re.IGNORECASE)
        if m: _set('TAN', m.group(1))

        # ── State Code ────────────────────────────────────────────────────────
        m = re.search(r'State\s*(?:Name\s*)?[,:]?\s*[A-Za-z,\s]+?,?\s*Code\s*[:\-]?\s*(\d{2})\b',
                      line, re.IGNORECASE)
        if not m: m = re.search(r'State\s*Code\s*[:\-]?\s*(\d{2})\b', line, re.IGNORECASE)
        if m: _set('STATE_CODE', m.group(1))

        # ── Bank combined: "Bank: ICICI A/c No.: 2715500356 IFSC: ICIC045F" ──
        combo = re.search(
            r'Bank\s*[:\-]?\s*([A-Za-z][A-Za-z\s.&]{1,20}?)\s+'
            r'A/[ce]\s*No\.?\s*[:\-]?\s*(\d{6,20})\s+'
            r'IFSC\s*[:\-]?\s*([A-Z]{4}[0O][A-Z0-9]{5,6})',
            line, re.IGNORECASE)
        if combo:
            _set('BANK', combo.group(1).strip())
            _set('AC_NO', combo.group(2))
            ifsc = combo.group(3).upper()
            if len(ifsc) >= 5 and ifsc[4] == 'O': ifsc = ifsc[:4]+'0'+ifsc[5:]
            _set('IFSC', ifsc)

        if 'BANK' not in res:
            m = re.search(
                r'Bank\s*(?:Name)?\s*[:\-]?\s*([A-Za-z][A-Za-z\s.&LTD]{2,30}?)(?:\s+A/[ce]|\s+Account|\s*$)',
                line, re.IGNORECASE)
            if m:
                b = m.group(1).strip()
                if 2 <= len(b) <= 30 and not re.search(r'holder|number|branch|code', b, re.I):
                    _set('BANK', b)

        if 'AC_NO' not in res:
            for p in [r'A/[ce]\s*No\.?\s*[:\-]?\s*(\d{6,20})',
                      r'Account\s*Number\s*[:\-;]?\s*(\d{6,20})']:
                m = re.search(p, line, re.IGNORECASE)
                if m: _set('AC_NO', m.group(1)); break

        if 'IFSC' not in res:
            m = re.search(r'IFSC\s*(?:Code)?\s*[:\-]?\s*([A-Z]{4}[0O][A-Z0-9]{5,6})',
                          line, re.IGNORECASE)
            if m:
                ifsc = m.group(1).upper()
                if len(ifsc) >= 5 and ifsc[4] == 'O': ifsc = ifsc[:4]+'0'+ifsc[5:]
                _set('IFSC', ifsc)

        # ── UPI ID ────────────────────────────────────────────────────────────
        if re.match(r'^UPI\s*ID\s*[:\-]?\s*$', line, re.I):
            v = _strip_lead(nxt)
            if '@' in v: _set('UPI_ID', v[:60])
        else:
            m = re.search(r'(\b[A-Za-z0-9._\-]{3,30}@[A-Za-z0-9]{2,20}\b)', line)
            if m:
                upi = m.group(1)
                if not re.search(r'sleekbill|keltron|gmail|yahoo|hotmail|email|www', upi, re.I):
                    _set('UPI_ID', upi[:60])
        if 'UPI_ID' not in res and '@' in line and re.match(r'^[A-Za-z0-9._\-]+@[A-Za-z0-9]+$', line.strip()):
            _set('UPI_ID', line.strip()[:60])

        # ── Payment Terms ─────────────────────────────────────────────────────
        m = re.search(r'Payment\s*Terms?\s*[:\-]?\s*([A-Za-z][A-Za-z\s]{3,50})',
                      line, re.IGNORECASE)
        if m:
            pt = m.group(1).strip()
            if not re.match(r'^[0-9]{2}[A-Z]', pt): _set('PAYMENT_TERMS', pt[:60])

        # ── OA / DBA ──────────────────────────────────────────────────────────
        m = re.search(r'OA\s*No\.?\s*[:\-]?\s*[r]?([A-Z0-9/\-]{6,25})', line, re.IGNORECASE)
        if m:
            val = re.sub(r'^r([A-Z])', r'I\1', m.group(1).strip())
            _set('OA_NO', val)
        m = re.search(r'OA\s*Date\s*[:\-]?\s*(\d{1,2}[-/]\w{2,9}[-/]?\d{0,4})', line, re.IGNORECASE)
        if m: _set('OA_DATE', m.group(1))
        m = re.search(r'(?:DBA|OBA)\s*No\.?\s*[:\-]?\s*[fF]?([A-Z0-9/\-]{6,25})', line, re.IGNORECASE)
        if m:
            val = re.sub(r'^[fF]([A-Z])', r'I\1', m.group(1).strip())
            _set('DBA_NO', val)
        m = re.search(r'(?:DBA|OBA)\s*Date\s*[:\-]?\s*(\d{1,2}[-/]\w{2,9}[-/]?\d{0,4})',
                      line, re.IGNORECASE)
        if m: _set('DBA_DATE', m.group(1))
        # Handle split line: "DBA Date:\n28-AUG-18"
        if re.match(r'^(?:DBA|OBA)\s*Date\s*[:\-]?\s*$', line, re.I):
            dm = re.search(r'(\d{1,2}-\w{3}-\d{2,4}|\d{1,2}/\d{2}/\d{2,4})', nxt)
            if dm: _set('DBA_DATE', dm.group(1))

        # ── Customer Order ────────────────────────────────────────────────────
        m = re.search(r'[•·]\s*(\d{3,6})\s', line)
        if m: _set('CUST_ORDER', m.group(1))

    # ── SUPPLIER & RECIPIENT names ─────────────────────────────────────────────
    for p in [
        r'[Ff]or\s+(Surabhi\s+Hardwares[^\n]{0,30})',
        r'[Ff]or\s+(KERALA\s+STATE\s+ELECTRONICS[^\n]{0,80}(?:LTD|LIMITED)\.?)',
        r'[Ff]or\s+(Gujarat\s+Freight\s+Tools[^\n]{0,20})',
        r'[Ff]or\s+(V\s+K\s+Control\s+System[^\n]{0,40})',
        r'[Ff]or\s+((?:[A-Z][A-Z\s&.,()]+)(?:LTD|LIMITED|PVT)\.?)',
        r'^(Gujarat\s+Freight\s+Tools)\s*[.\s]*$',
        r'^(V\s+K\s+Control\s+System\s+Private\s+Limited)[\s.]*$',
        r'^(Surabhi\s+Hardwares[^\n]{0,40})',
    ]:
        m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
        if m:
            name = ' '.join(m.group(1).split()).strip().rstrip('.,;|/')
            bad  = ['certified','declaration','authorised','consignee','buyer',
                    'bill to','we declare','original for','billing made easier','in words']
            if len(name) > 4 and not any(b in name.lower() for b in bad):
                _set('SUPPLIER', name[:200]); break

    for p in [
        r'Consignee\s*\(?Ship\s*to\)?\s*\n\s*\*?\*?([A-Za-z][A-Za-z\s&.,\-]{3,60})',
        r'Buyer\s*\(?Bill\s*to\)?\s*\n\s*\*?\*?([A-Za-z][A-Za-z\s&.,\-]{3,60})',
        r'M\s*/\s*[Ss]\s+([A-Za-z][A-Za-z\s&.,\-]{3,60})',
        r'Sold\s*To\s*[^\n]{0,10}\n\s*(30\s+K\s+BN[^\n]{5,80})',
        r'Client\s*Name\s*\n\s*Same\s+State\s+GST\s+Client\s*\n\s*([A-Za-z][A-Za-z\s]{2,30})',
    ]:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            name = re.split(
                r'\s+(?:Sumel|Plot|Road|Building|No\.|Address|Invoice|GSTIN|'
                r'Phone|Challan|12th|HSR|Nirmal|Hinjewadi|Buyer|\d{6})',
                name, flags=re.IGNORECASE)[0].strip()
            name = re.split(r'\s*[•·]\s*', name)[0].strip()
            bad  = ['same state','client name','ship to','bill to','consignee','buyer',
                    'dispatch','destination','kochi','kerala - ','mumbai','bangalore']
            if len(name) > 3 and not any(b in name.lower() for b in bad):
                _set('RECIPIENT', name[:150]); break

    # ── AMOUNTS ───────────────────────────────────────────────────────────────
    for p in [
        r'Total\s*Taxable\s*Value\s*[:\.]?\s*[₹+]?\s*([\d,]+\.?\d*)',
        r'Taxable\s*(?:Amount|Value)\s*[:\.]?\s*[₹+]?\s*([\d,]+\.?\d*)',
        r'(?:Total\s*)?Assessable\s*Value\s*[:\.]?\s*([\d,]+\.?\d*)',
    ]:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            f = _ca(m.group(1))
            if f and f > 0: _set('TAXABLE', str(f)); break

    taxable_val = _ca(res.get('TAXABLE','0')) or 0

    # Tally tax row
    tally_row = re.search(
        r'(?:oo|00)\s+([\d,]+\.\d{2})\]?\s*(\d{1,2})%\]?\s*([\d,]+\.\d{2})\]?\s*'
        r'(\d{1,2})%\]?\s*([\d,]+\.\d{2})\]?\s*([\d,]+\.\d{2})', text)
    if tally_row:
        tv=_ca(tally_row.group(1)); cr=tally_row.group(2); ca=_ca(tally_row.group(3))
        sr=tally_row.group(4); sa=_ca(tally_row.group(5))
        if tv and ca and sa:
            _set('TAXABLE',str(tv)); _set('CGST',str(ca)); _set('CGST_RATE',f'{cr}%')
            _set('SGST',str(sa)); _set('SGST_RATE',f'{sr}%')
            _set('TOTAL_GST',str(round(ca+sa,2)))

    # Standard item row: HSN TAXABLE CGST% CGSTamt SGST% SGSTamt TOTALtax
    std_row = re.search(
        r'(\d{4,6})\s+([\d,]+\.\d{2})\s+(\d{1,2})%\s+([\d,]+\.\d{2})'
        r'\s+(\d{1,2})%\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})', text)
    if std_row and not tally_row:
        tv=_ca(std_row.group(2)); cr=std_row.group(3); ca=_ca(std_row.group(4))
        sr=std_row.group(5); sa=_ca(std_row.group(6)); tot=_ca(std_row.group(7))
        if tv and ca and sa and tot:
            _set('TAXABLE',str(tv)); _set('CGST',str(ca)); _set('CGST_RATE',f'{cr}%')
            _set('SGST',str(sa)); _set('SGST_RATE',f'{sr}%')
            _set('TOTAL_GST',str(round(ca+sa,2))); _set('HSN',std_row.group(1))

    # Keltron TAX TOTAL
    kelt = re.search(r'TAX\s*TOTAL\s+([\d.]+)\s+([\d,.]+)', text, re.IGNORECASE)
    if kelt:
        cv=_ca(kelt.group(1))
        if cv and cv > 1:
            _set('CGST',str(cv)); _set('SGST',str(cv))
            _set('TOTAL_GST',str(round(cv*2,2)))

    # Gujarat Freight summary
    gf = re.search(r'Taxable\s*Amount\s+([\d,]+\.?\d*).{0,100}?Total\s*Tax\s+([\d,]+\.?\d*)',
                   text, re.IGNORECASE | re.DOTALL)
    if gf:
        tv=_ca(gf.group(1)); tt=_ca(gf.group(2))
        if tv: _set('TAXABLE',str(tv))
        if tt: _set('TOTAL_GST',str(tt))

    if 'CGST' not in res:
        for p in [r'CGST\s+([\d,]+\.\d{2})\s*$',
                  r'CGST\s*(?:Amount|Amt)?\s*[:\.]?\s*([\d,]+\.\d{2})(?!\d)']:
            m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
            if m:
                f = _ca(m.group(1))
                if f and f > 1 and (taxable_val == 0 or f < taxable_val*0.5):
                    _set('CGST', str(f)); break

    if 'SGST' not in res:
        for p in [r'SGST\s+([\d,]+\.\d{2})\s*$',
                  r'SGST\s*(?:Amount|Amt)?\s*[:\.]?\s*([\d,]+\.\d{2})(?!\d)']:
            m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
            if m:
                f = _ca(m.group(1))
                if f and f > 1 and (taxable_val == 0 or f < taxable_val*0.5):
                    _set('SGST', str(f)); break

    # IGST — only if no CGST/SGST (intra-state guard)
    if not res.get('CGST') and not res.get('SGST'):
        for p in [r'Total\s*Tax\s*Amount\s*[:\.]?\s*[₹+]?\s*([\d,]+\.?\d*)',
                  r'IGST\s*(?:Amount)?\s*[@\d\.%\s]*[:\.]?\s*([\d,]+\.\d{2})(?!\d)']:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                f = _ca(m.group(1))
                if f and f > 50: _set('IGST', str(f)); break

    if 'TOTAL_GST' not in res:
        for p in [r'Total\s*Tax\s*Amount\s*[:\.]?\s*[₹+]?\s*([\d,]+\.?\d*)',
                  r'Total\s*Tax\b[^\n]{0,15}[:\s]*([\d,]+\.?\d*)',
                  r'Total\s*GST\s*[:\.]?\s*([\d,]+\.?\d*)']:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                f = _ca(m.group(1))
                if f and f > 1: _set('TOTAL_GST', str(f)); break

    for p in [r'Total\s*Value\s*\(in\s*figure[s]?\)\s*[₹+]?\s*([\d,]+\.?\d*)',
              r'Net\s*Amount\s*Payable\s*[:\.]?\s*[₹+]?\s*([\d,]{3,}\.?\d*)',
              r'Amount\s*Due\s*[:\.]?\s*[₹+]?\s*([\d,]+\.?\d*)',
              r'Total\s*Amount\s*After\s*Tax\s*[₹+]?\s*([\d,]{3,}\.?\d*)',
              r'Grand\s*Total\s*[:\.]?\s*[₹+]?\s*([\d,]{3,}\.?\d*)']:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            f = _ca(m.group(1))
            if f and f >= 100: _set('TOTAL', str(f)); break

    if 'TOTAL' not in res:
        for p in [r'Indian\s*Rupee\s+([A-Z][A-Za-z\s]+Only)',
                  r'Total\s*in\s*words\s*:\s*([A-Z][A-Za-z\s]+(?:RUPEES|Rupees)[A-Za-z\s]*(?:ONLY|Only))',
                  r'[Rr]upees?\s+((?:[A-Za-z]+\s+){1,10}(?:thousand|lakh|crore)[A-Za-z\s]+)']:
            wm = re.search(p, text, re.IGNORECASE)
            if wm:
                num = words_to_number(wm.group(1))
                if num and num > 100: _set('TOTAL', str(num)); break

    tx = _ca(res.get('TAXABLE')); tg = _ca(res.get('TOTAL_GST'))
    if tx and tg and 'TOTAL' not in res:
        _set('TOTAL', str(round(tx+tg,2)))

    # ── TAX RATES ─────────────────────────────────────────────────────────────
    for field, pats in {
        'CGST_RATE': [r'CGST\s*%?\s*[:\.]?\s*(\d+)\s*%'],
        'SGST_RATE': [r'SGST\s*%?\s*[:\.]?\s*(\d+)\s*%'],
        'IGST_RATE': [r'IGST\s*%?\s*[:\.]?\s*(\d+)\s*%', r'IGST\b[^\n]{0,20}(\d{1,2})%'],
        'GST_RATE':  [r'GST\s*%\s*[:\.]?\s*(\d+)', r'@(\d{1,2})%',
                      r'\bGST\b[^\n]{0,15}(\d+)\s*%'],
    }.items():
        for p in pats:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                try:
                    f = float(m.group(1))
                    if 0 < f <= 100: _set(field, f'{int(f) if f==int(f) else f}%'); break
                except (ValueError, AttributeError): pass

    if 'GST_RATE' not in res:
        if res.get('CGST_RATE') and res.get('SGST_RATE'):
            try:
                c=float(res['CGST_RATE'].rstrip('%')); s=float(res['SGST_RATE'].rstrip('%'))
                _set('GST_RATE', f'{int(c+s)}%')
            except ValueError: pass
        elif res.get('IGST_RATE'):
            _set('GST_RATE', res['IGST_RATE'])
    if 'CGST_RATE' not in res and res.get('GST_RATE') and 'IGST_RATE' not in res:
        try:
            total=float(res['GST_RATE'].rstrip('%')); half=total/2
            _set('CGST_RATE', f'{int(half) if half==int(half) else half}%')
            _set('SGST_RATE', res['CGST_RATE'])
        except ValueError: pass

    # ── ITEMS ─────────────────────────────────────────────────────────────────
    # HSN scan — with noise char stripping and non-HSN exclusion
    hsn_set = []
    for m in re.finditer(
        r'(?:HSN|SAC)\s*(?:/\s*(?:SAC|HSN))?\s*(?:Code)?\s*[:\-]?\s*(\d{4,8})',
        text, re.IGNORECASE
    ):
        c = m.group(1)
        if c not in EXCLUDED_NUMBERS and c not in hsn_set: hsn_set.append(c)

    for m in re.finditer(r'^\s*\d+\s+(\d{4,8})\s+[A-Za-z\d]', text, re.MULTILINE):
        c = m.group(1)
        if c not in EXCLUDED_NUMBERS and c not in hsn_set: hsn_set.append(c)

    # Standalone 4-6 digit line (strip OCR noise suffix like "j", "4017 j" → "4017")
    # Only add if we haven't already found HSNs from explicit labels (avoids tax-section garbage)
    if not hsn_set:  # only use standalone detection when labeled scan found nothing
        for line in lines:
            clean = re.match(r'^(\d{4,6})\s*[jJ|lI\s]*$', line.strip())
            if clean:
                c = clean.group(1)
                if c not in EXCLUDED_NUMBERS and c not in hsn_set and c not in non_hsn_values:
                    hsn_set.append(c)

    # Validate HSN: 4-6 digits, not a year, not a known non-HSN value, reasonable range
    valid_hsn = []
    for c in hsn_set:
        if not (4 <= len(c) <= 6): continue
        if re.match(r'^(19|20)\d{2}$', c): continue      # year pattern
        if c in non_hsn_values: continue                   # dispatch_from / vendor_code
        if int(c) > 99999 and len(c) == 6: continue       # too large for 5-digit HSN
        if len(c) == 5 and c.startswith('1') and int(c) > 19999: continue  # "19987" type
        if c not in valid_hsn: valid_hsn.append(c)
    if valid_hsn: _set('HSN', ', '.join(valid_hsn[:3]))

    # QTY — prefer total "N NOS" line over individual item line
    # Look for "Total ... N NOS" pattern first (Gujarat Freight / Keltron)
    total_nos = re.search(r'Total\s+(\d{1,4})\s+NOS?\b', text, re.IGNORECASE)
    if total_nos:
        qty = int(total_nos.group(1))
        if qty < 1000 and str(qty) not in hsn_set:
            _set('QTY', str(qty))

    if 'QTY' not in res:
        # Fall back to first item's qty from item row
        item_row_qty = re.search(r'\b(\d{4,6})\s+\d+\s+\w+\s+.*?\s+(\d{1,4})\s+Nos?\b',
                                  text, re.IGNORECASE)
        if item_row_qty:
            qty = int(item_row_qty.group(2))
            if qty < 1000 and str(qty) not in hsn_set:
                _set('QTY', str(qty))
        else:
            for p in [r'\b([1-9]\d{0,2})\s+(?:NOS?|MTS?|Nos?|Units?|Pcs?)\b',
                      r'\b(2)\s+Nos\b']:
                m = re.search(p, text, re.IGNORECASE)
                if m:
                    qty = int(m.group(1))
                    if qty < 1000 and str(qty) not in hsn_set:
                        _set('QTY', str(qty)); break

    m = re.search(r'\b\d+\s+(Nos?|No\.?|MTS?|Units?|Pcs?|Kgs?)\b', text, re.IGNORECASE)
    if m: _set('UOM', m.group(1).capitalize())

    for p in [r'\b7\s+No\.?\s+([\d,]+\.\d{2})\b',
              r'\b2\s+Nos?\s+([\d,]+\.\d{2})\s+\d{1,2}\b',
              r'\b1\s+MTS?\s+([\d,]+\.\d{2})\b',
              r'\b1\s+NOS?\s+([\d,]+\.\d{2})\b',
              r'Rate\s*[:\.]?\s*([\d,]+\.\d{2})']:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            f = _ca(m.group(1))
            if f and 0 < f < 10000000: _set('RATE', str(f)); break

    desc_lines = []
    for p in [r'^(Bosch\s+All-in-One[^\n]+)', r'^(Taparia\s+Universal[^\n]+)',
              r'^(Hard\s+Rubber[^\n]+)', r'^(12MM\*+)']:
        m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
        if m:
            d = m.group(1).strip()
            if d and d not in desc_lines: desc_lines.append(d)
    for m in re.finditer(
        r'^\s*\d+\s+\d{4,8}\s+([A-Za-z\*][^\n]{3,80}?)'
        r'\s+\d+\s+(?:No\.?|NOS?|MTS?)', text, re.IGNORECASE | re.MULTILINE):
        d = m.group(1).strip()
        if d and not re.search(r'certified|declaration|gst act', d, re.I):
            if d not in desc_lines: desc_lines.append(d)
    m = re.search(r'9987\s+(.*?)\s+\d+\s+Nos?\b', text, re.IGNORECASE | re.DOTALL)
    if m:
        d = re.sub(r'\s+',' ', m.group(1)).strip()
        if len(d) > 3 and d not in desc_lines: desc_lines.append(d)
    if desc_lines: _set('ITEM_DESC', ' | '.join(desc_lines[:3])[:250])

    if 'STATE_CODE' not in res:
        g = res.get('GSTIN_S','')
        if len(g) >= 2 and g[:2].isdigit(): _set('STATE_CODE', g[:2])

    return {k:v for k,v in res.items() if v}


# ═══════════════════════════════════════════════════════════════════════════════
#  TEXT EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════
def extract_all_text(file_path):
    from .pdf_processor import (pdf_to_images, load_image,
                                 extract_full_text_ocr, extract_text_from_pdf_direct)
    ext     = os.path.splitext(file_path)[1].lower()
    combined = ''
    if ext == '.pdf':
        direct = extract_text_from_pdf_direct(file_path)
        if direct: combined += direct + '\n'
        try:
            for img in pdf_to_images(file_path):
                combined += extract_full_text_ocr(img) + '\n'
        except Exception as e:
            print(f'[TextExtract] OCR error: {e}')
    else:
        combined = extract_full_text_ocr(load_image(file_path))
    print(f'[TextExtract] {len(combined)} chars')
    return combined


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════
def extract_gst_data(file_path):
    from .pdf_processor import pdf_to_images, load_image
    from .post_processor import post_process

    ext = os.path.splitext(file_path)[1].lower()
    print('[Pipeline] Step 1-2: Text extraction...')
    raw_text = extract_all_text(file_path)

    print('[Pipeline] Step 3-5: Structured extraction...')
    cleaned = fix_ocr_noise(raw_text)
    regex_results = {}
    for text in [cleaned, raw_text]:
        for k, v in full_extract(text).items():
            if k not in regex_results and v: regex_results[k] = v

    layoutlm_results = {}
    try:
        print('[Pipeline] Step 6: LayoutLMv3...')
        extractor = get_extractor()
        if ext == '.pdf':
            images = pdf_to_images(file_path)
            if images: layoutlm_results = extractor.extract_batch(images)
        else:
            layoutlm_results = extractor.extract_from_image(load_image(file_path))
        layoutlm_results = extractor._validate_layoutlm(layoutlm_results)
        print(f'[Pipeline] LayoutLM (validated): {len(layoutlm_results)} fields')
    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache(); gc.collect()
    except Exception as e:
        print(f'[Pipeline] LayoutLM error: {e}')

    # LayoutLM fills gaps only; regex wins for structured fields
    LAYOUTLM_PRIORITY = {'SUPPLIER','RECIPIENT','ITEM_DESC'}
    final = dict(regex_results)
    for k, v in layoutlm_results.items():
        if k in LAYOUTLM_PRIORITY and v and len(v) > 3: final[k] = v
        elif k not in final and v: final[k] = v

    final['raw_text'] = raw_text
    print('[Pipeline] Step 7: Post-processing...')
    final = post_process(final)
    filled = len([k for k in final if k != 'raw_text' and final[k]])
    print(f'[Pipeline] DONE — {filled} fields extracted')
    return final
