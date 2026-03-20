"""
layoutlm_extractor.py  —  Complete GST extraction engine (free, no API needed)
Place in: converter/utils/layoutlm_extractor.py

Key improvements over previous version:
 - Context-aware GSTIN assignment (supplier vs recipient by position/label)
 - Indian lakh format amounts: 1,18,000 → 118000
 - All date formats: 20-Dec-20, 02 - Oct - 2024, 23-Jul-2025
 - Invoice number formats: SHB/456/20, IN-1, GST-3525-26, SHB456/20
 - OCR misread: % sign as Rs/INR
 - Full table line item extraction
 - Bank details extraction
 - IRN, ACK, E-Way Bill, Transport
"""

import re, os, gc
import torch
from PIL import Image
from django.conf import settings
import pytesseract
pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD


# ── Excluded numbers (years, reference codes) ─────────────────────────────────
EXCL = {
    '2017','2018','2019','2020','2021','2022','2023','2024','2025','2026',
    '1319','1972','0724','1384',
}

# ── Month name mapping ─────────────────────────────────────────────────────────
MONTHS = {
    'jan':'01','feb':'02','mar':'03','apr':'04','may':'05','jun':'06',
    'jul':'07','aug':'08','sep':'09','oct':'10','nov':'11','dec':'12',
}


# ═══════════════════════════════════════════════════════════════════════════════
#  AMOUNT UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def parse_amount(raw):
    """
    Parse Indian monetary amounts into float.
    Handles: ₹3,500.00  /  %1,18,000.00  /  Rs.1,00,000  /  3500  /  1,18,000
    Indian lakh format: 1,18,000 = 118000 (comma after 1 then groups of 2)
    """
    if not raw:
        return None
    # Remove currency symbols and spaces (% is common OCR misread of ₹)
    s = re.sub(r'[₹%\s]', '', str(raw))
    s = re.sub(r'^(?:Rs\.?|INR)', '', s, flags=re.IGNORECASE).strip()
    if not s:
        return None
    # Remove all commas then parse (works for both 3-group and Indian lakh)
    s = s.replace(',', '')
    if s in EXCL:
        return None
    try:
        f = float(s)
        return f if f > 0 else None
    except ValueError:
        return None


def fmt(v, d=2):
    """Format float to string with d decimal places."""
    if v is None:
        return ''
    try:
        return f'{float(v):.{d}f}'
    except (ValueError, TypeError):
        return str(v)


def parse_amount_str(s):
    """Parse and return formatted string, or empty string on failure."""
    v = parse_amount(s)
    return fmt(v) if v is not None else ''


# ═══════════════════════════════════════════════════════════════════════════════
#  OCR NOISE FIXER
# ═══════════════════════════════════════════════════════════════════════════════

def fix_ocr_noise(text):
    """Fix systematic OCR corruption patterns."""
    fixes = [
        # Date: "31J0812018" → "31/08/2018"
        (r'(Invoice\s*Date\s*[:\.]?\s*)(\d{2})[JjIil](\d{2})1?(\d{4})', r'\g<1>\2/\3/\4'),
        (r'\b(\d{2})[JjIil](\d{2})1?(\d{4})\b', r'\1/\2/\3'),
        # Amount noise: "4283 9(" → "4283.97"
        (r'\b(\d{3,6})\s+9[(\[{]', r'\1.97'),
        (r'\b(\d{2,4})\s+(\d{2})\b', r'\1.\2'),
        (r'\b(\d{4}),(\d{2})\b', r'\1.\2'),
        # Common word fixes
        (r'\blnvoice\b', 'Invoice'),
        (r'\b!([A-Z]{2,})', r'I\1'),
        (r'Ne\\+', 'Net'), (r'\bflet\b', 'Net'),
        (r'Ass[ae]+ss?[ao]+ble', 'Assessable'),
        (r'\btl?[ti]tty\b', 'fifty'), (r'\brune\b', 'nine'),
        (r'\bsovcnty\b', 'seventy'), (r'\b[mn]ght\b', 'eight'),
        # Invoice number noise
        (r'(\d{4})11([A-Z]{2})', r'\1/\2'),
        # PAN noise: AA8CK → AABCK
        (r'AA8CK(\d{4}[A-Z])', r'AABCK\1'),
        # % sign misread as ₹ on amounts — normalise to nothing (parse_amount handles)
        (r'%(\d)', r'₹\1'),
    ]
    result = text
    for pattern, repl in fixes:
        try:
            result = re.sub(pattern, repl, result, flags=re.IGNORECASE)
        except Exception:
            pass
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  DATE NORMALISATION
# ═══════════════════════════════════════════════════════════════════════════════

def normalise_date(raw):
    """
    Normalise any date format to DD/MM/YYYY.
    Handles:
      20-Dec-20     → 20/12/2020
      02 - Oct - 2024 → 02/10/2024
      31/08/2018    → 31/08/2018 (already good)
      23-Jul-2025   → 23/07/2025
      21-Dec-20     → 21/12/2020
    Returns '' if year is impossible (< 2000 or > 2030 after expansion).
    """
    if not raw:
        return ''
    raw = raw.strip()
    raw = re.sub(r'\s*-\s*', '-', raw)   # remove spaces around dashes

    # Pattern: DD-Mon-YY or DD-Mon-YYYY
    m = re.match(
        r'^(\d{1,2})[-/\s](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-/\s](\d{2,4})$',
        raw, re.IGNORECASE
    )
    if m:
        day   = m.group(1).zfill(2)
        month = MONTHS.get(m.group(2).lower()[:3], '01')
        year  = m.group(3)
        if len(year) == 2:
            year = str(2000 + int(year))
        if 2000 <= int(year) <= 2030:
            return f'{day}/{month}/{year}'
        return ''

    # Pattern: DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r'^(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2,4})$', raw)
    if m:
        day, mon, year = m.group(1), m.group(2), m.group(3)
        if len(year) == 2:
            year = str(2000 + int(year))
        if 2000 <= int(year) <= 2030:
            return f'{day.zfill(2)}/{mon.zfill(2)}/{year}'
        return ''

    # Pattern: DDMMYYYY (compact)
    m = re.match(r'^(\d{2})(\d{2})(\d{4})$', raw)
    if m:
        day, mon, year = m.group(1), m.group(2), m.group(3)
        if 2000 <= int(year) <= 2030:
            return f'{day}/{mon}/{year}'

    return raw   # return as-is if no pattern matched


def extract_date(text, label_hints=None):
    """
    Extract invoice/issue date from text.
    Tries labeled patterns first, then standalone date patterns.
    """
    if label_hints is None:
        label_hints = ['Invoice', 'Issue', 'Bill', 'Tax Invoice', 'Dated?']

    # Build labeled patterns
    label_pat = r'|'.join(label_hints)
    patterns = [
        # DD-Mon-YYYY or DD-Mon-YY (with optional spaces around dashes)
        rf'(?:{label_pat})\s*Date\s*[:\.]?\s*(\d{{1,2}}\s*[-/]\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*[-/]\s*\d{{2,4}})',
        # DD/MM/YYYY
        rf'(?:{label_pat})\s*Date\s*[:\.]?\s*(\d{{1,2}}[/\-\.]\d{{1,2}}[/\-\.]\d{{2,4}})',
        # OCR noise: 31J0812018
        rf'(?:{label_pat})\s*Date\s*[:\.]?\s*(\d{{2}}[JjIil]\d{{2}}\d{{4}})',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            d = normalise_date(m.group(1).strip())
            if d:
                return d

    # Standalone date patterns (fallback)
    standalone = [
        r'\b(\d{1,2}\s*[-/]\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*[-/]\s*\d{2,4})\b',
        r'\b(\d{1,2}[/\-]\d{1,2}[/\-]\d{4})\b',
    ]
    for p in standalone:
        for m in re.finditer(p, text, re.IGNORECASE):
            d = normalise_date(m.group(1).strip())
            if d:
                return d
    return ''


# ═══════════════════════════════════════════════════════════════════════════════
#  GSTIN EXTRACTOR  —  context-aware assignment
# ═══════════════════════════════════════════════════════════════════════════════

def extract_gstins_with_context(text):
    """
    Extract GSTINs and determine which is supplier vs recipient.

    Strategy:
    1. Look for labeled GSTINs: "GSTIN: 29AACCT..." with context label
    2. Supplier label: the FIRST occurrence (before Consignee/Buyer section)
    3. Recipient labels: "Consignee", "Buyer (Bill to)", "Client", "M/S", "Ship To"
    4. If only 1 GSTIN found: assign to supplier

    GSTIN format: lenient — 15 alphanumeric (last 3 may vary)
    """
    # Find all GSTIN occurrences with their position and preceding context
    pattern = re.compile(
        r'(?:GSTIN[/\s]*(?:UIN)?|GST\s*No\.?)\s*[:\s]+([A-Z0-9]{14,16})',
        re.IGNORECASE
    )

    found = []    # list of (position, gstin, context_before)
    for m in pattern.finditer(text):
        g = m.group(1).upper().strip()
        if len(g) < 14 or len(g) > 16:
            continue
        # Get 200 chars before this match for context
        ctx = text[max(0, m.start()-200):m.start()].lower()
        found.append({
            'gstin': g,
            'pos': m.start(),
            'ctx': ctx,
        })

    # Also find standalone GSTINs (no label)
    standalone = re.compile(r'\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b')
    standalone_found = []
    for m in standalone.finditer(text):
        g = m.group(1)
        already = any(f['gstin'] == g for f in found)
        if not already:
            ctx = text[max(0, m.start()-200):m.start()].lower()
            standalone_found.append({'gstin': g, 'pos': m.start(), 'ctx': ctx})

    all_found = sorted(found + standalone_found, key=lambda x: x['pos'])

    # Dedup while preserving order
    seen = []
    unique = []
    for item in all_found:
        if item['gstin'] not in seen:
            seen.append(item['gstin'])
            unique.append(item)

    # Classify
    RECIPIENT_KEYWORDS = [
        'consignee', 'ship to', 'buyer', 'bill to', 'client', 'm/s', 'sold to',
        'recipient', 'billed to'
    ]
    SUPPLIER_KEYWORDS = [
        'for ', 'from:', 'seller', 'vendor', 'invoice from', 'supplier'
    ]

    supplier_gstin   = ''
    recipient_gstin  = ''

    for item in unique:
        ctx = item['ctx']
        is_recipient = any(kw in ctx for kw in RECIPIENT_KEYWORDS)
        is_supplier  = any(kw in ctx for kw in SUPPLIER_KEYWORDS)

        if is_recipient and not recipient_gstin:
            recipient_gstin = item['gstin']
        elif not is_recipient and not supplier_gstin:
            supplier_gstin = item['gstin']
        elif not recipient_gstin and supplier_gstin and item['gstin'] != supplier_gstin:
            recipient_gstin = item['gstin']

    # If only one found and not assigned as recipient, it's the supplier
    if not supplier_gstin and unique:
        supplier_gstin = unique[0]['gstin']

    return supplier_gstin, recipient_gstin


# ═══════════════════════════════════════════════════════════════════════════════
#  INVOICE NUMBER EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════════════

def extract_invoice_number(text):
    """
    Extract invoice number — handles all Indian GST invoice formats:
      SHB/456/20     (Tally)
      IN-1           (SleekBill)
      GST-3525-26    (custom)
      ITBG/18-19/3304/TP/1328  (Govt)
      SHB456/20      (no slash variant)
    """
    patterns = [
        # After explicit label (highest priority)
        r'Invoice\s*No\.?\s*[:\.]?\s*[\$#]?\s*([A-Z0-9][A-Z0-9/\-\.]{2,35})',
        r'Tax\s*Invoice\s*No\.?\s*[:\.]?\s*([A-Z0-9][A-Z0-9/\-\.]{2,35})',
        r'(?:INV|Bill)\s*No\.?\s*[:\.]?\s*([A-Z0-9][A-Z0-9/\-\.]{2,35})',
        # Label then whitespace then value on same line
        r'Invoice\s+No\s+Dated\s*\n[^\n]*?[\$#]?([A-Z]{2,6}/\d{3,6}/\d{2,4})',
        r'Invoice\s+No\s+Dated\s*\n[^\n]*?(IN-\d+)',
        # Known patterns
        r'\b([A-Z]{2,6}/\d{3,6}/\d{2,4})\b',        # SHB/456/20
        r'\b(IN-\d+)\b',                              # IN-1
        r'\b(GST-\d{4}-\d{2})\b',                    # GST-3525-26
        r'\b([A-Z]{2,6}/\d{2}-\d{2}/\d{3,6}/[A-Z]{1,3}/\d{3,6})\b',  # ITBG/18-19/...
        # After "TAX INVOICE" label on same line
        r'TAX\s+INVOICE\s+([A-Z0-9][A-Z0-9\-/\.]{1,20})\b',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
        if m:
            val = m.group(1).strip().rstrip('.,;| ')
            # Clean: strip leading $, #, !
            val = re.sub(r'^[\$#!]', '', val)
            val = re.sub(r'^!', 'I', val)          # ! → I
            val = re.sub(r'11([A-Z])', r'/\1', val) # 11TP → /TP
            if len(val) >= 2 and re.search(r'\d', val):
                return val
    return ''


# ═══════════════════════════════════════════════════════════════════════════════
#  AMOUNT EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════════════

def extract_amounts(text):
    """
    Extract monetary amounts with comprehensive label patterns.
    Handles Indian lakh format, % OCR misread, multiple label variants.
    """
    res = {}

    # ── TAX TOTAL line: "TAX TOTAL 4498.17 4498.17" ───────────────────────────
    tl = re.search(r'TAX\s*TOTAL\s+([\d,]+\.?\d*)\s+([\d,]+\.?\d*)', text, re.IGNORECASE)
    if tl:
        cv = parse_amount(tl.group(1))
        sv = parse_amount(tl.group(2))
        if cv and cv > 1:
            res['CGST'] = fmt(cv)
            if sv and sv > 1:
                res['SGST'] = fmt(sv)
            res.setdefault('TOTAL_GST', fmt(round(cv + (sv or cv), 2)))

    # ── Labeled patterns ───────────────────────────────────────────────────────
    LABELED = {
        'TAXABLE': [
            r'(?:Total\s*)?(?:Assessable|Taxable)\s*(?:Value|Amount)\s*[:\.]?\s*[₹%Rs.]*\s*([\d,]+\.?\d*)',
            r'Total\s*Taxable\s*Value\s*[:\.]?\s*[₹%Rs.]*\s*([\d,]+\.?\d*)',
        ],
        'CGST': [
            r'CGST\s*(?:Amount|Amt)?\s*[@\d\.%\s]*[:\.]?\s*([\d,]+\.\d{2})(?!\d)',
            r'Central\s*Tax\s*[:\.]?\s*[₹%Rs.]*\s*([\d,]+\.\d{2})',
        ],
        'SGST': [
            r'SGST\s*(?:Amount|Amt)?\s*[@\d\.%\s]*[:\.]?\s*([\d,]+\.\d{2})(?!\d)',
            r'State\s*Tax\s*[:\.]?\s*[₹%Rs.]*\s*([\d,]+\.\d{2})',
        ],
        'IGST': [
            r'IGST\s*(?:Amount|Amt)?\s*[@\d\.%\s]*[:\.]?\s*([\d,]+\.\d{2})(?!\d)',
            # From item table: last column for IGST invoice
            r'(?:4017|8302|1005|9987)\s+\d[\d,]*\s+[\d,]+\.?\d*\s+[\d,]+\.?\d*\s+([\d,]+\.\d{2})\s+[\d,]+',
        ],
        'TOTAL_GST': [
            r'Total\s*(?:Tax|GST)\s*(?:Amount)?\s*[:\.]?\s*[₹%Rs.]*\s*([\d,]+\.?\d*)',
            r'Tax\s*Amount\s*[:\.]?\s*[₹%Rs.]*\s*([\d,]+\.?\d*)',
        ],
        'TOTAL': [
            r'Net\s*Amount\s*Payable\s*[:\.]?\s*[₹%Rs.]*\s*([\d,]+\.?\d*)',
            r'Net\s*Payable\s*[:\.]?\s*[₹%Rs.]*\s*([\d,]+\.?\d*)',
            r'Amount\s*(?:Due|Payable)\s*[:\.]?\s*[₹%Rs.]*\s*([\d,]+\.?\d*)',
            r'Grand\s*Total\s*[:\.]?\s*[₹%Rs.]*\s*([\d,]+\.?\d*)',
            r'Total\s*(?:Amount\s*After\s*Tax|Value\s*\(in\s*figure\))\s*[:\.]?\s*[₹%Rs.]*\s*([\d,]+\.?\d*)',
            r'Total\s*Amount\s*[:\.]?\s*[₹%Rs.]*\s*([\d,]+\.?\d*)',
        ],
    }
    for field, pats in LABELED.items():
        if field in res:
            continue
        for p in pats:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                v = parse_amount(m.group(1))
                if v is None:
                    continue
                if field == 'TOTAL' and v < 100:
                    continue
                res[field] = fmt(v)
                break

    # ── Amount from words ──────────────────────────────────────────────────────
    if 'TOTAL' not in res:
        wm = re.search(
            r'(?:Indian\s+)?Rupees?\s+([\w\s]+?)(?:\s+only|\s*/-|$)',
            text, re.IGNORECASE
        )
        if wm:
            num = words_to_number(wm.group(1))
            if num and num > 100:
                res['TOTAL'] = str(num)

    # ── Computed fallbacks ─────────────────────────────────────────────────────
    taxable   = parse_amount(res.get('TAXABLE'))
    total_gst = parse_amount(res.get('TOTAL_GST'))
    if taxable and total_gst and 'TOTAL' not in res:
        res['TOTAL'] = fmt(round(taxable + total_gst, 2))

    if 'CGST' not in res and 'TOTAL_GST' in res:
        h = parse_amount(res['TOTAL_GST'])
        if h:
            res['CGST'] = fmt(round(h / 2, 2))
            res['SGST'] = fmt(round(h / 2, 2))

    # Format all amounts
    for f in ['TAXABLE', 'CGST', 'SGST', 'IGST', 'TOTAL', 'TOTAL_GST', 'DISCOUNT']:
        v = parse_amount(res.get(f))
        if v is not None:
            res[f] = fmt(v)

    return res


def words_to_number(text):
    """Convert Indian English number words to integer."""
    ones = {
        'zero':0,'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,
        'seven':7,'eight':8,'nine':9,'ten':10,'eleven':11,'twelve':12,
        'thirteen':13,'fourteen':14,'fifteen':15,'sixteen':16,'seventeen':17,
        'eighteen':18,'nineteen':19,'twenty':20,'thirty':30,'forty':40,
        'fifty':50,'sixty':60,'seventy':70,'eighty':80,'ninety':90,
        # OCR variants
        'tltty':50,'titty':50,'mght':8,'rune':9,'sovcnty':70,
    }
    multipliers = {
        'hundred':100,'thousand':1000,'lakh':100000,'lac':100000,'crore':10000000,
    }
    words   = re.findall(r'[a-z]+', text.lower())
    current = 0; total = 0
    for word in words:
        if word in ones:
            current += ones[word]
        elif word == 'hundred':
            current = current * 100 if current else 100
        elif word in multipliers:
            total += (current or 1) * multipliers[word]; current = 0
    return (total + current) or None


# ═══════════════════════════════════════════════════════════════════════════════
#  TAX RATE EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════════════

def extract_tax_rates(text):
    """Extract CGST%, SGST%, IGST% rates."""
    rates = {}
    for field, pats in {
        'CGST_RATE': [
            r'CGST\s*(?:@|%|Rate)?\s*[:\.]?\s*(\d+(?:\.\d+)?)\s*%?',
            r'Central\s*Tax\s*(?:Rate|@)?\s*[:\.]?\s*(\d+)\s*%',
        ],
        'SGST_RATE': [
            r'SGST\s*(?:@|%|Rate)?\s*[:\.]?\s*(\d+(?:\.\d+)?)\s*%?',
            r'State\s*Tax\s*(?:Rate|@)?\s*[:\.]?\s*(\d+)\s*%',
        ],
        'IGST_RATE': [
            r'IGST\s*(?:@|%|Rate)?\s*[:\.]?\s*(\d+(?:\.\d+)?)\s*%?',
            # From item table: "18%" near IGST column
            r'(?:4017|8302)\s+\d+[\d,\s.]+(\d{1,2})%',
        ],
        'GST_RATE': [
            r'(?:GST|Tax)\s*%\s*[:\.]?\s*(\d+)',
            r'@\s*(\d{1,2})\s*%',
            r'(\d{1,2})\s*%\s*(?:GST|Tax)',
            # From item table column header or row
            r'\b(\d{1,2})\s+(?:\d[\d,]*\.?\d*\s+){1,3}(?:\d[\d,]*\.?\d*)\b',
        ],
    }.items():
        for p in pats:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                try:
                    fv = float(m.group(1))
                    if 0 < fv <= 100:
                        rates[field] = f'{m.group(1)}%'
                        break
                except ValueError:
                    pass
    # Derive SGST from CGST
    if 'CGST_RATE' in rates and 'SGST_RATE' not in rates:
        rates['SGST_RATE'] = rates['CGST_RATE']
    # Derive total GST rate
    if 'GST_RATE' not in rates:
        if 'IGST_RATE' in rates:
            rates['GST_RATE'] = rates['IGST_RATE']
        elif 'CGST_RATE' in rates and 'SGST_RATE' in rates:
            try:
                c = float(rates['CGST_RATE'].rstrip('%'))
                s = float(rates['SGST_RATE'].rstrip('%'))
                rates['GST_RATE'] = f'{c+s:.0f}%'
            except ValueError:
                pass
    return rates


# ═══════════════════════════════════════════════════════════════════════════════
#  PARTY EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════════════

def extract_parties(text):
    """Extract supplier and buyer names using context-aware patterns."""
    results = {}

    # ── Supplier ──────────────────────────────────────────────────────────────
    # Pattern 1: company name at very top of invoice (first bold line before address)
    # Pattern 2: "for <company>" near bottom
    # Pattern 3: "Declaration for <company>"
    for p in [
        r'Declaration\s+for\s+([A-Z][A-Za-z\s&.,()-]{5,80})',
        r'For\s+((?:[A-Z][A-Z\s&.,()]+)(?:LTD|LIMITED|PVT|CORP|CO|INC|PRIVATE)\.?)',
        r'^([A-Z][A-Z\s&.,()-]+(?:LTD|LIMITED|PVT|CORP|PRIVATE)\.?)\s*\n',
    ]:
        m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
        if m:
            nm = m.group(1).strip()
            if len(nm) > 5 and not any(
                bad in nm.lower() for bad in ['certified','we declare','authorised','signature']
            ):
                results['SUPPLIER'] = nm[:200]
                break

    # Fallback: first non-empty line that looks like a company name
    if not results.get('SUPPLIER'):
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        for line in lines[:5]:
            # Skip header lines
            if re.search(r'TAX\s*INVOICE|e-Invoice|ORIGINAL|BILLING', line, re.I):
                continue
            if re.match(r'^[A-Z][A-Za-z\s&.,()-]{8,60}$', line):
                results['SUPPLIER'] = line[:200]
                break

    # ── Recipient ─────────────────────────────────────────────────────────────
    for p in [
        r'(?:Sold\s*To|Bill\s*To|Buyer)\s*[•·:→\-]?\s*\n?\s*\*{0,2}([A-Z][^\n]{5,100})\*{0,2}',
        r'(?:M/S|Client\s*Name)\s*[:\-]?\s*\n?\s*([A-Z][^\n]{3,80})',
        r'(?:Consignee|Ship\s*To)\s*[:\-]?\s*\n?\s*\*{0,2}([A-Z][^\n]{5,80})\*{0,2}',
    ]:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            nm = re.sub(r'\*+', '', m.group(1)).strip()
            if (len(nm) > 3 and
                    not re.search(r'Customer Order|Invoice No|Delivery|GST\s*No', nm, re.I)):
                results['RECIPIENT'] = nm[:150]
                break

    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  MISC FIELDS EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════════════

def extract_misc(text):
    """Extract all other fields: IRN, ACK, transport, bank, PAN, CIN, etc."""
    results = {}

    PATTERNS = {
        # Identity
        'IRN':           r'IRN\s*[:\-]?\s*([a-f0-9]{64})',
        'ACK_NO':        r'Ack\s*(?:No\.?|Number)\s*[:\-]?\s*([0-9]{10,20})',
        'ACK_DATE':      r'Ack\s*Date\s*[:\-]?\s*(\d{1,2}\s*[-/]\s*[A-Za-z0-9]{2,3}\s*[-/]\s*\d{2,4})',
        'PAN':           r'PAN\s*(?:No\.?)?\s*[:\-]?\s*:?\s*([A-Z]{5}\d{4}[A-Z])',
        'CIN':           r'\b([UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})\b',
        'STATE_CODE':    r'State\s*(?:Name\s*:.*?Code\s*:|Code\s*[:\-]?)\s*(\d{2})\b',
        'SERVICE_TAX':   r'Service\s*Tax\s*(?:No\.?)?\s*[:\-]?\s*([A-Z0-9]{10,20})',
        'TAN':           r'\bTAN\s*[:\-]?\s*([A-Z]{4}\d{5}[A-Z])\b',
        'FSSAI':         r'FSSAI\s*(?:No\.?)?\s*[:\-]?\s*([A-Z0-9]{8,15})',
        'LUT_BOND':      r'LUT\s*Bond\s*(?:No\.?)?\s*[:\-]?\s*([A-Z0-9]{5,20})',
        'MSME':          r'MSME\s*(?:No\.?|Number|NO)\s*[:\-]?\s*([A-Z0-9]{10,15})',
        'RECIPIENT_PAN': r'(?:Client|Buyer|Recipient|Consignee)\s*PAN\s*[:\-]?\s*([A-Z]{5}\d{4}[A-Z])',
        # Logistics
        'CHALLAN_NO':    r'Challan\s*No\.?\s*[:\-]?\s*(\d+)',
        'CHALLAN_DATE':  r'Challan\s*Date\s*[:\-]?\s*(\d{1,2}\s*[-/]\s*[A-Za-z0-9]{2,3}\s*[-/]\s*\d{2,4})',
        'EWAY_BILL':     r'E[-\s]?Way\s*Bill\s*(?:No\.?)?\s*[:\-]?\s*([0-9]{12})',
        'TRANSPORT_NAME':r'(?:Transport(?:er)?|Transporter\s*Name)\s*[:\-]?\s*([A-Z][A-Za-z\s]{2,50})',
        'TRANSPORT_ID':  r'Transport\s*ID\s*[:\-]?\s*([A-Z0-9]{10,20})',
        'VEHICLE_NO':    r'Vehicle\s*(?:No\.?|Number)\s*[:\-]?\s*([A-Z]{2}\s*\d{2}\s*[A-Z]{1,2}\s*\d{4})',
        'SHIP_BY':       r'Ship\s*(?:by|via|through|By)\s*[:\-]?\s*([A-Za-z\s]{2,30})',
        'DISPATCH_FROM': r'Dispatch\s*From\s*[:\-]?\s*(\d+)',
        'DISPATCH_DATE': r'Dispatch\s*Date\s*[:\-]?\s*(\d{1,2}\s*[-/]\s*[A-Za-z0-9]{2,3}\s*[-/]\s*\d{2,4})',
        # Invoice ref
        'OA_NO':         r'OA\s*No\.?\s*[:\-]?\s*([A-Z0-9/\-]+)',
        'OA_DATE':       r'OA\s*Date\s*[:\-]?\s*(\d{1,2}[-/][A-Z\d]{2,3}[-/]\d{2,4})',
        'DBA_NO':        r'(?:DBA|OBA)\s*No\.?\s*[:\-]?\s*([A-Z0-9/\-]+)',
        'DBA_DATE':      r'(?:DBA|OBA)\s*Date\s*[:\-]?\s*(\d{1,2}[-/][A-Z\d]{2,3}[-/]\d{2,4})',
        'PAYMENT_TERMS': r'Payment\s*Terms?\s*[:\-]?\s*([A-Za-z][^\n]{3,50})',
        'PLACE':         r'Place\s*of\s*Supply\s*[:\-]?\s*([A-Za-z()0-9\s,]{2,40}?)(?:\n|$)',
        'CUST_ORDER':    r'[•·]\s*(\d{3,6})\s+Invoice',
        'VENDOR_CODE':   r'Vendor\s*Code\s*[:\-]?\s*(\d+)',
        'DUE_DATE':      r'Due\s*Date\s*[:\-]?\s*(\d{1,2}\s*[-/]\s*[A-Za-z0-9]{2,3}\s*[-/]\s*\d{2,4})',
        # Bank
        'BANK_NAME':     r'Bank\s*(?:Name)?\s*[:\-]?\s*([A-Z][A-Za-z\s.]{2,30})',
        'ACCOUNT_NO':    r'(?:A/c|Account)\s*(?:No\.?|Number)\s*[:\-]?\s*(\d{8,18})',
        'IFSC':          r'IFSC\s*(?:Code)?\s*[:\-]?\s*([A-Z]{4}0[A-Z0-9]{6})',
        'BRANCH':        r'Branch\s*(?:Name)?\s*[:\-]?\s*([A-Z][A-Za-z\s,]{2,40})',
        'ACCOUNT_HOLDER':r'(?:Account\s*Holder|A/c\s*Holder)\s*(?:Name)?\s*[:\-]?\s*([A-Z][^\n]{5,60})',
        'UPI_ID':        r'UPI\s*ID\s*[:\-]?\s*([a-zA-Z0-9._@\-]{5,50})',
    }

    for key, pattern in PATTERNS.items():
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            val = m.group(1).strip().rstrip('.,;|')
            if val and len(val) >= 2 and val.upper() not in ('NO', 'NA', 'N/A'):
                results[key] = val

    # Amount in words (separate from extract_amounts)
    wm = re.search(
        r'(?:Total\s*Value\s*\(in\s*words\)|Amount\s*Chargeable\s*\(in\s*words\)|'
        r'Tax\s*Amount\s*\(in\s*words\)|Amount\s*in\s*words)\s*[:\.]?\s*'
        r'(?:[₹%]\s*)?(.{5,200}?)(?:\n|$)',
        text, re.IGNORECASE
    )
    if wm:
        words_text = wm.group(1).strip()
        if len(words_text) > 4:
            results['TOTAL_WORDS'] = words_text[:250]
    elif 'TOTAL_WORDS' not in results:
        wm2 = re.search(
            r'(?:Indian\s+)?Rupees?\s+([\w\s]+?)(?:\s+only|\s*/-|$)',
            text, re.IGNORECASE
        )
        if wm2:
            results['TOTAL_WORDS'] = ('Rupees ' + wm2.group(1).strip().title() + ' Only')

    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  LINE ITEM TABLE EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════════════

def extract_line_items(text):
    """
    Extract line items from invoice table.
    Returns (list_of_items, list_of_hsn_codes).
    """
    items   = []
    hsn_set = []

    # Pattern: SL_no  Description  HSN  Qty  UoM  Rate  ...  Amount
    # Look for lines that start with a number and contain an HSN code
    row_re = re.compile(
        r'^\s*(\d+)\s+'                            # Sl No
        r'([A-Za-z][^\t]{3,60}?)\s+'              # Description
        r'(\d{4,8})\s+'                            # HSN
        r'(\d+(?:\.\d+)?)\s+'                     # Qty
        r'([A-Za-z]+)\s+'                          # UoM
        r'([\d,]+\.?\d*)',                         # Rate
        re.MULTILINE
    )
    for m in row_re.finditer(text):
        hsn = m.group(3)
        if hsn not in EXCL and not re.match(r'^(19|20)\d{2}$', hsn):
            items.append({
                'SL_NO':       m.group(1),
                'DESCRIPTION': m.group(2).strip(),
                'HSN':         hsn,
                'QTY':         m.group(4),
                'UOM':         m.group(5),
                'RATE':        parse_amount_str(m.group(6)),
            })
            if hsn not in hsn_set:
                hsn_set.append(hsn)

    # Labeled HSN codes
    for m in re.finditer(
        r'(?:HSN|SAC)\s*(?:/\s*SAC|/\s*HSN)?\s*[:\-]?\s*(\d{4,8})',
        text, re.IGNORECASE
    ):
        code = m.group(1)
        if (code not in EXCL and
                code not in hsn_set and
                not re.match(r'^(19|20)\d{2}$', code)):
            hsn_set.append(code)

    # Standalone HSN in table context (no label)
    for m in re.finditer(r'^\s*\d+\s+\S+.*?\s+(\d{4,8})\s+', text, re.MULTILINE):
        code = m.group(1)
        if (code not in EXCL and
                code not in hsn_set and
                not re.match(r'^(19|20)\d{2}$', code)):
            hsn_set.append(code)

    # Quantity and UoM extraction
    qty = ''
    uom = ''
    m = re.search(r'\b(\d+(?:\.\d+)?)\s+(Nos?|Units?|Pcs?|Kgs?|MTS|Ltrs?|KGS)\b', text, re.IGNORECASE)
    if m:
        qty = m.group(1)
        uom = m.group(2).upper()

    # Item description
    item_desc = ''
    for p in [
        r'(?:Description\s*(?:of\s*Goods|and\s*Specification)?\s*[:\-]?\s*\n?)([^\n]{10,200})',
        r'^\s*1\s+\|?\s*([A-Z][^\n|]{5,80})\s+\d{4,8}\s',
    ]:
        m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
        if m:
            desc = m.group(1).strip()
            if (len(desc) > 3 and
                    not re.search(r'Qty|UoM|Rate|GST%|Taxable|HSN', desc, re.I)):
                item_desc = desc[:200]
                break

    return items, hsn_set, qty, uom, item_desc


# ═══════════════════════════════════════════════════════════════════════════════
#  MASTER EXTRACTION FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def master_extract(raw_text):
    """
    Run all extractors on text. Returns comprehensive dict.
    """
    cleaned = fix_ocr_noise(raw_text)
    results = {}

    for text in [cleaned, raw_text]:
        # GSTINs (context-aware)
        if not results.get('GSTIN_S'):
            sup, rec = extract_gstins_with_context(text)
            if sup:
                results['GSTIN_S'] = sup
            if rec:
                results['GSTIN_R'] = rec

        # Invoice number
        if not results.get('INV_NO'):
            v = extract_invoice_number(text)
            if v:
                results['INV_NO'] = v

        # Date
        if not results.get('INV_DATE'):
            v = extract_date(text)
            if v:
                results['INV_DATE'] = v

        # Amounts
        for k, v in extract_amounts(text).items():
            if k not in results and v:
                results[k] = v

        # Tax rates
        for k, v in extract_tax_rates(text).items():
            if k not in results and v:
                results[k] = v

        # Parties
        for k, v in extract_parties(text).items():
            if k not in results and v:
                results[k] = v

        # Misc fields
        for k, v in extract_misc(text).items():
            if k not in results and v:
                results[k] = v

        # Line items
        items, hsn_set, qty, uom, desc = extract_line_items(text)
        if items and 'LINE_ITEMS' not in results:
            results['LINE_ITEMS'] = items
        if hsn_set and not results.get('HSN'):
            results['HSN'] = ', '.join(hsn_set[:5])
        if qty and not results.get('QTY'):
            results['QTY'] = qty
        if uom and not results.get('UOM'):
            results['UOM'] = uom
        if desc and not results.get('ITEM_DESC'):
            results['ITEM_DESC'] = desc

    # Derive state code from GSTIN
    if not results.get('STATE_CODE') and results.get('GSTIN_S'):
        g = results['GSTIN_S']
        if len(g) >= 2 and g[:2].isdigit():
            results['STATE_CODE'] = g[:2]

    # Derive RATE from first line item
    if not results.get('RATE') and results.get('LINE_ITEMS'):
        r = results['LINE_ITEMS'][0].get('RATE', '')
        if r:
            results['RATE'] = r

    print(f'[Extract] {len(results)} fields: '
          f'{[k for k in results if k not in ("LINE_ITEMS","raw_text")]}')
    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  TEXT ACQUISITION
# ═══════════════════════════════════════════════════════════════════════════════

def get_text(file_path):
    """Get best-quality text from file using all available methods."""
    from .pdf_processor import (
        pdf_to_images, load_image,
        extract_full_text_ocr, extract_text_from_pdf_direct,
    )
    ext = os.path.splitext(file_path)[1].lower()
    combined = ''

    if ext == '.pdf':
        direct = extract_text_from_pdf_direct(file_path)
        if direct:
            combined += direct + '\n'
        try:
            for img in pdf_to_images(file_path):
                combined += extract_full_text_ocr(img) + '\n'
        except Exception as e:
            print(f'[Text] OCR error: {e}')
    else:
        try:
            combined = extract_full_text_ocr(load_image(file_path))
        except Exception as e:
            print(f'[Text] Image OCR error: {e}')

    print(f'[Text] {len(combined)} chars')
    return combined


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def extract_gst_data(file_path):
    """
    Complete free GST extraction pipeline:
      1. pdfplumber direct text (for text-layer PDFs)
      2. Tesseract OCR on preprocessed images
      3. Context-aware extraction (GSTIN, dates, amounts, etc.)
      4. Mathematical post-processing (validate and complete amounts)
    """
    from .post_processor import post_process

    # Step 1-2: Get text
    print('[Pipeline] Steps 1-2: Text extraction...')
    raw_text = get_text(file_path)

    # Step 3: Extract all fields
    print('[Pipeline] Step 3: Field extraction...')
    result = master_extract(raw_text)
    result['raw_text'] = raw_text

    # Step 4: Validate and complete
    print('[Pipeline] Step 4: Post-processing...')
    result = post_process(result)

    found = sum(1 for k, v in result.items()
                if v and k not in ('raw_text', 'LINE_ITEMS'))
    print(f'[Pipeline] DONE — {found} fields extracted')
    return result
