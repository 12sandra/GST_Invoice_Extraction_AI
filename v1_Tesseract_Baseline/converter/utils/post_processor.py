"""
post_processor.py  —  Mathematical validator and field corrector
Place in: converter/utils/post_processor.py
"""
import re


def _f(v):
    """Float parse — returns None on failure."""
    if not v:
        return None
    try:
        return float(re.sub(r'[,\s₹%]', '', str(v)).strip())
    except (ValueError, TypeError):
        return None


def fmt(v, d=2):
    """Format float to string."""
    if v is None:
        return ''
    try:
        return f'{float(v):.{d}f}'
    except (ValueError, TypeError):
        return str(v)


def post_process(data):
    """Run all validation and correction steps."""
    d = dict(data)
    d = _clean_strings(d)
    d = _fix_date_year(d)
    d = _fix_state_code(d)
    d = _fix_invoice_no(d)
    d = _fix_pan(d)
    d = _fix_cin(d)
    d = _fix_gst_rate(d)
    d = _validate_amounts(d)
    d = _fix_hsn(d)
    d = _fix_item_description(d)
    d = _fix_supplier(d)
    d = _fix_recipient(d)
    d = _fix_cust_order(d)
    d = _fix_refs(d)
    d = _propagate_line_items(d)

    found = [k for k, v in d.items() if v and k not in ('raw_text', 'LINE_ITEMS')]
    print(f'[PostProcess] {len(found)} fields: {found}')
    return d


def _clean_strings(d):
    skip = {'raw_text', 'LINE_ITEMS'}
    for k, v in d.items():
        if k in skip or not isinstance(v, str):
            continue
        d[k] = v.strip().rstrip('.,;|/ ')
    return d


def _fix_date_year(d):
    """
    Expand 2-digit years: 20-Dec-20 → 20/12/2020.
    Also normalise spaced dates: 02 - Oct - 2024 → 02/10/2024.
    """
    MONTHS = {
        'jan':'01','feb':'02','mar':'03','apr':'04','may':'05','jun':'06',
        'jul':'07','aug':'08','sep':'09','oct':'10','nov':'11','dec':'12',
    }
    for field in ['INV_DATE', 'ACK_DATE', 'OA_DATE', 'DBA_DATE',
                  'CHALLAN_DATE', 'DISPATCH_DATE', 'DUE_DATE']:
        date = d.get(field, '')
        if not date:
            continue

        # Normalise spaces around separators
        date = re.sub(r'\s*[-/]\s*', '-', date.strip())

        # DD-Mon-YY or DD-Mon-YYYY
        m = re.match(
            r'^(\d{1,2})-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*-(\d{2,4})$',
            date, re.IGNORECASE
        )
        if m:
            day   = m.group(1).zfill(2)
            month = MONTHS.get(m.group(2).lower()[:3], '01')
            year  = m.group(3)
            if len(year) == 2:
                year = str(2000 + int(year))
            if 2000 <= int(year) <= 2030:
                d[field] = f'{day}/{month}/{year}'
            continue

        # DD/MM/YY or DD-MM-YY (2-digit year)
        m = re.match(r'^(\d{1,2})[/\-](\d{1,2})[/\-](\d{2})$', date)
        if m:
            day, mon, year = m.group(1), m.group(2), m.group(3)
            d[field] = f'{day.zfill(2)}/{mon.zfill(2)}/{str(2000 + int(year))}'
            continue

        # Reject impossible years
        yr = re.search(r'\d{4}', date)
        if yr:
            y = int(yr.group())
            if y < 2000 or y > 2030:
                d[field] = ''

    return d


def _fix_state_code(d):
    if d.get('STATE_CODE'):
        return d
    for g in [d.get('GSTIN_S', ''), d.get('GSTIN_R', '')]:
        if g and len(g) >= 2 and g[:2].isdigit():
            d['STATE_CODE'] = g[:2]
            print(f'[PP] State code from GSTIN: {g[:2]}')
            return d
    raw = d.get('raw_text', '')
    m = re.search(r'State\s*(?:Name.*?Code|Code)\s*[:\-]?\s*(\d{2})\b', raw, re.IGNORECASE)
    if m:
        d['STATE_CODE'] = m.group(1)
    return d


def _fix_invoice_no(d):
    inv = d.get('INV_NO', '')
    if not inv:
        return d
    # Remove leading special chars
    inv = re.sub(r'^[\$#!]', '', inv)
    # Fix ! → I
    inv = re.sub(r'^!', 'I', inv)
    # Fix TBG → ITBG (dropped I)
    if re.match(r'^TBG/', inv, re.I):
        inv = 'I' + inv
    # Fix 11XX → /XX
    inv = re.sub(r'11([A-Z])', r'/\1', inv)
    d['INV_NO'] = inv.strip().rstrip('.,;| ')
    return d


def _fix_pan(d):
    if d.get('PAN'):
        # Fix OCR: AA8CK → AABCK (8 misread as B)
        d['PAN'] = re.sub(r'([A-Z]{2})8([A-Z]{2}\d{4}[A-Z])', r'\g<1>B\2', d['PAN'])
        return d
    raw = d.get('raw_text', '')
    for p in [
        r'PAN\s*(?:No\.?)?\s*[:\-]?\s*:?\s*([A-Z]{5}\d{4}[A-Z])',
        r'PAN\s*(?:No\.?)?\s*[:\-]?\s*:?\s*([A-Z]{2}[0-9][A-Z]{2}\d{4}[A-Z])',
    ]:
        m = re.search(p, raw, re.IGNORECASE)
        if m:
            pan = m.group(1).upper()
            # Fix 8→B at position 3
            pan = pan[:2] + re.sub(r'8', 'B', pan[2], count=1) + pan[3:]
            if re.match(r'^[A-Z]{5}\d{4}[A-Z]$', pan):
                d['PAN'] = pan
                print(f'[PP] PAN: {pan}')
                return d
    return d


def _fix_cin(d):
    if d.get('CIN'):
        return d
    raw = d.get('raw_text', '')
    m = re.search(r'\b([UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})\b', raw, re.IGNORECASE)
    if m:
        d['CIN'] = m.group(1).upper()
        print(f'[PP] CIN: {d["CIN"]}')
    return d


def _fix_gst_rate(d):
    """Derive missing GST rates from known values."""
    # From raw text table if missing
    if not d.get('GST_RATE'):
        raw = d.get('raw_text', '')
        # Look for @18% or 18% in table context
        m = re.search(r'@\s*(\d{1,2})\s*%', raw, re.IGNORECASE)
        if m:
            r = int(m.group(1))
            if r in (5, 12, 18, 28):
                d['GST_RATE'] = f'{r}%'
        else:
            # From item table row: HSN qty rate GST% taxable
            m = re.search(
                r'\b(?:4017|8302|1005|9987)\b.*?\s+(\d{2}(?:\.\d{2})?)\s*%',
                raw, re.IGNORECASE | re.DOTALL
            )
            if m:
                r = float(m.group(1))
                if r in (5, 12, 18, 28):
                    d['GST_RATE'] = f'{r:.0f}%'

    # Derive CGST/SGST from total GST rate
    if d.get('GST_RATE') and not d.get('CGST_RATE'):
        try:
            total = float(d['GST_RATE'].rstrip('%'))
            half  = total / 2
            d['CGST_RATE'] = f'{half:.0f}%'
            d['SGST_RATE'] = f'{half:.0f}%'
        except ValueError:
            pass

    # Derive total GST rate from CGST + SGST
    if not d.get('GST_RATE') and d.get('CGST_RATE') and d.get('SGST_RATE'):
        try:
            c = float(d['CGST_RATE'].rstrip('%'))
            s = float(d['SGST_RATE'].rstrip('%'))
            d['GST_RATE'] = f'{c+s:.0f}%'
        except ValueError:
            pass

    # Ensure % suffix on rate fields
    for f in ['CGST_RATE', 'SGST_RATE', 'IGST_RATE', 'GST_RATE']:
        v = d.get(f, '')
        if v and not v.endswith('%'):
            d[f] = v + '%'

    return d


def _validate_amounts(d):
    """
    Mathematical cross-validation.
    Rules:
      - Intra-state: CGST == SGST
      - Total GST = CGST + SGST + IGST
      - Net Amount = Taxable + Total GST - Discount
    """
    taxable   = _f(d.get('TAXABLE'))
    cgst      = _f(d.get('CGST'))
    sgst      = _f(d.get('SGST'))
    igst      = _f(d.get('IGST'))
    total_gst = _f(d.get('TOTAL_GST'))
    total     = _f(d.get('TOTAL'))

    gstin_s = d.get('GSTIN_S', '')
    gstin_r = d.get('GSTIN_R', '')
    is_intra = (not gstin_r) or (
        len(gstin_s) >= 2 and len(gstin_r) >= 2 and
        gstin_s[:2] == gstin_r[:2]
    )

    # Rule 1: CGST == SGST for intra-state
    if is_intra and cgst and sgst:
        diff = abs(cgst - sgst)
        if diff > 0 and diff / max(cgst, sgst) < 0.05:
            sgst = cgst
            d['SGST'] = fmt(cgst)
            print(f'[PP] SGST corrected to match CGST: {cgst}')

    # Rule 2: Compute Total GST
    if cgst and sgst:
        computed_gst = round(cgst + sgst + (igst or 0), 2)
        if not total_gst or abs(computed_gst - total_gst) > 5:
            d['TOTAL_GST'] = fmt(computed_gst)
            total_gst = computed_gst
            print(f'[PP] Total GST set: {computed_gst}')
    total_gst = _f(d.get('TOTAL_GST'))

    # Rule 3: Compute Net Amount
    if taxable and total_gst:
        disc    = _f(d.get('DISCOUNT')) or 0
        net     = round(taxable + total_gst - disc, 2)
        if not total:
            d['TOTAL'] = fmt(net)
            print(f'[PP] Net amount computed: {net}')
        elif total and abs(net - total) / net > 0.01:
            print(f'[PP] Net amount corrected: {total} → {net}')
            d['TOTAL'] = fmt(net)

    # Rule 4: Derive CGST/SGST from Total GST
    tg = _f(d.get('TOTAL_GST'))
    if tg and not _f(d.get('CGST')) and is_intra and not _f(d.get('IGST')):
        h = round(tg / 2, 2)
        d['CGST'] = fmt(h)
        d['SGST'] = fmt(h)
        print(f'[PP] CGST/SGST derived as half of Total GST: {h}')

    # Rule 5: Derive Taxable from Total - GST
    net = _f(d.get('TOTAL'))
    gst = _f(d.get('TOTAL_GST'))
    if net and gst and not _f(d.get('TAXABLE')):
        d['TAXABLE'] = fmt(round(net - gst, 2))
        print(f'[PP] Taxable derived: {d["TAXABLE"]}')

    # Format all amounts consistently
    for f in ['TAXABLE', 'CGST', 'SGST', 'IGST', 'TOTAL', 'TOTAL_GST']:
        v = _f(d.get(f))
        if v is not None:
            d[f] = fmt(v)

    return d


def _fix_hsn(d):
    """Remove false positives from HSN field."""
    raw = d.get('raw_text', '')
    hsn = d.get('HSN', '')

    if not hsn:
        # Try to find HSN in raw text
        for p in [
            r'(?:HSN|SAC)\s*[:\-]?\s*(\d{4,8})',
            r'^\s*\d+\s+(?:[A-Za-z][^\n]{3,50}\s+)?(\d{4,8})\s+\d',
        ]:
            m = re.search(p, raw, re.IGNORECASE | re.MULTILINE)
            if m:
                code = m.group(1)
                if not re.match(r'^(19|20)\d{2}$', code):
                    d['HSN'] = code
                    break
        return d

    # Build set of known amount integers to exclude
    amt_ints = set()
    for f in ['TAXABLE', 'CGST', 'SGST', 'IGST', 'TOTAL', 'TOTAL_GST', 'RATE']:
        v = d.get(f, '')
        if v:
            amt_ints.add(str(v).split('.')[0])

    codes = [c.strip() for c in hsn.split(',')]
    valid = []
    for code in codes:
        if not code or not code.isdigit():
            continue
        if not (4 <= len(code) <= 8):
            continue
        if code in amt_ints:
            continue
        if re.match(r'^(19|20)\d{2}$', code):
            continue
        if len(code) > 6:  # Too long for standalone HSN
            continue
        if code not in valid:
            valid.append(code)

    d['HSN'] = ', '.join(valid) if valid else ''
    return d


def _fix_item_description(d):
    """Remove boilerplate text, extract real item description."""
    desc = d.get('ITEM_DESC', '')
    BAD = ['certified', 'true and correct', 'gst act', 'declaration',
           'authorised', 'we declare', 'actual price']

    if desc and any(b in desc.lower() for b in BAD):
        d['ITEM_DESC'] = ''
        desc = ''

    if not desc:
        raw = d.get('raw_text', '')
        # Try from line items
        items = d.get('LINE_ITEMS', [])
        if items and isinstance(items, list) and items[0].get('DESCRIPTION'):
            d['ITEM_DESC'] = items[0]['DESCRIPTION']
            return d
        # Try from table row
        for p in [
            r'^\s*1\s+\|?\s*([A-Z\d][^\n|]{5,80})\s+\d{4,8}\s',
            r'(?:Description|Particulars)\s*[:\-]?\s*\n?([^\n]{10,200})',
        ]:
            m = re.search(p, raw, re.IGNORECASE | re.MULTILINE)
            if m:
                desc = m.group(1).strip()
                if len(desc) > 3 and not re.search(r'Qty|UoM|Rate|GST%|Taxable', desc, re.I):
                    d['ITEM_DESC'] = desc[:200]
                    break
    return d


def _fix_supplier(d):
    """Ensure full supplier name (handles multi-line Kerala Keltron invoice)."""
    sup = d.get('SUPPLIER', '')
    if not sup or (
        'KERALA' in sup.upper() and
        'ELECTRONICS' not in sup.upper() and
        'DEVELOPMENT' not in sup.upper()
    ):
        raw = d.get('raw_text', '')
        m = re.search(
            r'For\s+(KERALA\s+STATE\s+ELECTRONICS[^\n]*\n?DEVELOPMENT[^\n]*LTD\.?)',
            raw, re.IGNORECASE
        )
        if m:
            full = ' '.join(m.group(1).split()).strip()
            d['SUPPLIER'] = full[:200]
            print(f'[PP] Supplier fixed: {d["SUPPLIER"]}')
    return d


def _fix_recipient(d):
    """Remove header text captured as recipient name."""
    rec = d.get('RECIPIENT', '')
    BAD = ['customer order', 'invoice no', 'delivery', 'gstin', 'same state']

    if rec and any(b in rec.lower() for b in BAD):
        d['RECIPIENT'] = ''
        rec = ''

    if not rec:
        raw = d.get('raw_text', '')
        for p in [
            r'Sold\s*To\s*[•·:→\-]?\s*\n?\s*\*{0,2}([A-Z][^\n]{5,100})',
            r'(?:M/S|Client\s*Name)\s*[:\-]?\s*\n?\s*([A-Z][^\n]{3,80})',
            r'Consignee\s*\(Ship\s*to\)\s*\n\s*\*{0,2}([A-Z][^\n]{5,100})',
        ]:
            m = re.search(p, raw, re.IGNORECASE)
            if m:
                nm = re.sub(r'\*+', '', m.group(1)).strip()
                if (len(nm) > 3 and
                        not re.search(r'Customer Order|Invoice|Delivery|GST\s*No', nm, re.I)):
                    d['RECIPIENT'] = nm[:150]
                    break
    return d


def _fix_cust_order(d):
    """Fix customer order number — extract real numeric value."""
    co = d.get('CUST_ORDER', '')
    if not co or (len(co) <= 4 and not co.isdigit()):
        raw = d.get('raw_text', '')
        # Pattern: bullet + number + Invoice Date
        m = re.search(r'[•·]\s*(\d{3,6})\s+Invoice', raw, re.IGNORECASE)
        if m:
            d['CUST_ORDER'] = m.group(1)
            return d
        # Pattern: number between order label and invoice date
        m = re.search(
            r'Customer\s*Order[^\n]{0,30}\n[^\n]{0,20}[•·]?\s*(\d{3,6})\b',
            raw, re.IGNORECASE
        )
        if m:
            d['CUST_ORDER'] = m.group(1)
    return d


def _fix_refs(d):
    """Fix reference number OCR noise."""
    for f in ['OA_NO', 'DBA_NO']:
        v = d.get(f, '')
        if v:
            v = re.sub(r'^r([A-Z])', r'I\1', v)  # rTP → ITP
            v = re.sub(r'[{}\[\]()]', '', v)
            d[f] = v.strip()
    return d


def _propagate_line_items(d):
    """Propagate first line item values to top-level fields if missing."""
    items = d.get('LINE_ITEMS', [])
    if not items or not isinstance(items, list):
        return d
    first = items[0]
    # Collect unique HSN codes
    hsns = list(dict.fromkeys(
        item.get('HSN', '') for item in items if item.get('HSN', '')
    ))
    if hsns and not d.get('HSN'):
        d['HSN'] = ', '.join(hsns)
    for k in ['QTY', 'UOM', 'RATE', 'ITEM_DESC']:
        item_key = {
            'QTY': 'QTY', 'UOM': 'UOM', 'RATE': 'RATE', 'ITEM_DESC': 'DESCRIPTION'
        }[k]
        if not d.get(k) and first.get(item_key):
            d[k] = first[item_key]
    return d
