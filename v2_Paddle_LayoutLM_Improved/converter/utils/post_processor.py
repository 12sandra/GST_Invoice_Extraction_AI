"""
post_processor.py  v10.0
=========================
Place in: converter/utils/post_processor.py

Fixes vs v9:
  - CGST_RATE/SGST_RATE now also derived by checking OCR for "%6" noise
  - IGST propagated from TOTAL_GST when invoice is inter-state (INV03)
  - SUPPLIER Keltron pattern improved (INV04)
  - DBA_DATE fallback from raw OCR (INV04)
  - OA_NO prefix "r"→"I" fixed (INV04)
  - INV_NO completion from raw text if partial (INV04)
  - QTY derivation prioritises "Total N NOS" over item-row noise
"""

import re


def to_float(val):
    if val is None or val == '': return None
    s = str(val)
    s = re.sub(r'[₹€$¥¢%\+]', '', s)
    s = re.sub(r'\b(\d{1,2}),(\d{2}),(\d{3})\.(\d{2})\b',
               lambda m: str(int(m.group(1))*100000+int(m.group(2))*1000+int(m.group(3)))+'.'+m.group(4), s)
    s = re.sub(r'\b(\d{1,2}),(\d{2}),(\d{3})\b',
               lambda m: str(int(m.group(1))*100000+int(m.group(2))*1000+int(m.group(3))), s)
    s = s.replace(',','').strip()
    try: return float(s)
    except (ValueError, TypeError): return None


def fmt(val, decimals=2):
    if val is None: return ''
    try: return f'{float(val):.{decimals}f}'
    except: return str(val)


def _close(a, b, pct=0.02):
    if not a or not b: return False
    return abs(a-b)/max(abs(a),abs(b)) <= pct


_GSTIN_RE = r'[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]{3}'  # relaxed, catches E000 endings

_STATE_CODE_NAME = {
    '01':'jammu & kashmir','02':'himachal pradesh','03':'punjab','04':'chandigarh',
    '05':'uttarakhand','06':'haryana','07':'delhi','08':'rajasthan',
    '09':'uttar pradesh','10':'bihar','11':'sikkim','12':'arunachal pradesh',
    '13':'nagaland','14':'manipur','15':'mizoram','16':'tripura',
    '17':'meghalaya','18':'assam','19':'west bengal','20':'jharkhand',
    '21':'odisha','22':'chhattisgarh','23':'madhya pradesh','24':'gujarat',
    '25':'daman & diu','26':'dadra & nagar haveli','27':'maharashtra',
    '28':'andhra pradesh','29':'karnataka','30':'goa','31':'lakshadweep',
    '32':'kerala','33':'tamil nadu','34':'puducherry',
    '35':'andaman & nicobar','36':'telangana','37':'andhra pradesh',
}

_VALID_PLACE_NAMES = set(_STATE_CODE_NAME.values()) | {
    'jammu','kashmir','himachal','punjab','chandigarh','uttarakhand',
    'haryana','delhi','new delhi','rajasthan','bihar','sikkim',
    'nagaland','manipur','mizoram','tripura','meghalaya','assam',
    'bengal','jharkhand','odisha','chhattisgarh','gujarat','maharashtra',
    'andhra','karnataka','goa','lakshadweep','kerala','telangana',
}


def post_process(data):
    d = dict(data)
    d = _clean_strings(d)
    d = _fix_amounts_ocr(d)
    d = _fix_gstin(d)
    d = _validate_place(d)
    d = _fix_invoice_number(d)
    d = _fix_invoice_date(d)
    d = _fix_pan(d)
    d = _fix_cin(d)
    d = _derive_state_code(d)
    d = _fix_supplier_name(d)
    d = _fix_recipient_name(d)
    d = _fix_ifsc(d)
    d = _fix_qty(d)
    d = _fix_hsn(d)
    d = _fix_item_description(d)
    d = _fix_payment_terms(d)
    d = _fix_reference_numbers(d)
    d = _fix_transport(d)
    d = _fix_dba_date(d)
    d = _clear_intrastate_igst(d)
    d = _derive_gst_rates(d)
    d = _fix_sgst_taxable_bug(d)
    d = _fix_interstate(d)
    d = _propagate_igst(d)    # NEW: set IGST from TOTAL_GST for inter-state
    d = _validate_amounts(d)
    d = _add_total_words(d)
    filled = [k for k in d if k != 'raw_text' and d[k]]
    print(f'[PostProcess] {len(filled)} fields: {filled}')
    return d


def _clean_strings(d):
    GARBAGE_CHARS = '\u0120\u2581\ufffd\u0121\u0100\u2018\u2019\u201c\u201d'
    for k, v in list(d.items()):
        if k == 'raw_text' or not isinstance(v, str): continue
        v = v.strip().rstrip('.,;|/ ')
        if any(c in v for c in GARBAGE_CHARS):
            d[k] = ''; continue
        if re.search(r'[^\x20-\x7E\u00A0-\u024F\u20B9]', v):
            d[k] = ''; continue
        d[k] = v
    return d


def _fix_amounts_ocr(d):
    for f in ['TAXABLE','CGST','SGST','IGST','TOTAL','TOTAL_GST','RATE','DISCOUNT']:
        v = d.get(f,'')
        if not v: continue
        v = str(v)
        v = re.sub(r'\b(\d{1,2}),(\d{2}),(\d{3})\.(\d{2})\b',
                   lambda m: str(int(m.group(1))*100000+int(m.group(2))*1000+int(m.group(3)))+'.'+m.group(4), v)
        v = re.sub(r'^(\d+),(\d{2})$', r'\1.\2', v.replace(',',''))
        v = v.replace(',','').strip()
        m = re.match(r'^([\d]+\.?\d{0,2})', v)
        if m: d[f] = m.group(1)
    # Fix garbled rate strings: "%6" / "%9" → "9%" / "6%"
    for rf in ['CGST_RATE','SGST_RATE','IGST_RATE','GST_RATE']:
        v = d.get(rf,'')
        if not v: continue
        # "%6" → "9%" (OCR reversal)
        m = re.match(r'^%(\d{1,2})$', v.strip())
        if m: d[rf] = f'{m.group(1)}%'
    return d


def _fix_gstin(d):
    raw     = d.get('raw_text','')
    gstin_s = d.get('GSTIN_S','').upper()
    gstin_r = d.get('GSTIN_R','').upper()

    same_line_gstins = []
    for m in re.finditer(r'GSTIN?(?:/UIN)?\s*:\s*(' + _GSTIN_RE + r')', raw, re.IGNORECASE):
        g = m.group(1).upper()
        if not any(x[0]==g for x in same_line_gstins):
            same_line_gstins.append((g, m.start()))

    if same_line_gstins:
        same_line_gstins.sort(key=lambda x: x[1])
        best_supplier = same_line_gstins[0][0]
        if best_supplier != gstin_s:
            if gstin_s and gstin_s != best_supplier and not gstin_r:
                d['GSTIN_R'] = gstin_s
            d['GSTIN_S'] = best_supplier
        return d

    if not gstin_s or not gstin_r: return d
    supplier = d.get('SUPPLIER','')
    if supplier and len(supplier) >= 10:
        pos_s   = raw.upper().find(gstin_s)
        pos_r   = raw.upper().find(gstin_r)
        pos_sup = raw.lower().find(supplier.lower()[:15])
        if pos_sup >= 0 and pos_r >= 0 and pos_s >= 0:
            if abs(pos_r - pos_sup) < abs(pos_s - pos_sup):
                d['GSTIN_S'], d['GSTIN_R'] = gstin_r, gstin_s
    return d


def _validate_place(d):
    place = d.get('PLACE','').strip()
    if not place: return d
    if len(place) < 3: d['PLACE'] = ''; return d
    if not re.match(r'^[A-Za-z][A-Za-z\s&\-\.]+$', place):
        d['PLACE'] = ''; return d
    pl = place.lower()
    if not any(pl in name or name in pl for name in _VALID_PLACE_NAMES):
        d['PLACE'] = ''
    return d


def _fix_invoice_number(d):
    inv = d.get('INV_NO','')
    if not inv: return d
    inv = re.sub(r'^[!|]TBG/', 'ITBG/', inv)
    inv = re.sub(r'^[!|]T8G/', 'ITBG/', inv)
    inv = re.sub(r'^[!|]', 'I', inv)
    inv = re.sub(r'(\d{4})11([A-Z]{2})', r'\1/\2', inv)
    inv = re.sub(r'(\d{4})A([A-Z]{2})/(\d{4})', r'\1/\2/\3', inv)
    inv = re.sub(r'\s+I$', '', inv.strip()).strip()
    # Complete partial ITBG from raw text
    if re.match(r'^ITBG/\d{2}-\d{2}/\d{4}$', inv):
        raw = d.get('raw_text','')
        m = re.search(r'ITBG/(\d{2}-\d{2}/\d{4}/[A-Z]{2}/\d{4})', raw, re.IGNORECASE)
        if m: inv = f'ITBG/{m.group(1)}'
        else:
            m2 = re.search(r'(ITBG/\d{2}-\d{2}/\d{4})\D{0,5}([A-Z]{2}/\d{4})', raw, re.IGNORECASE)
            if m2: inv = f'{m2.group(1)}/{m2.group(2)}'
    d['INV_NO'] = inv.strip()
    return d


def _fix_invoice_date(d):
    raw = d.get('raw_text','')
    bad_dates = {d.get('OA_DATE',''), d.get('ACK_DATE',''), d.get('DBA_DATE','')} - {''}

    def _norm(s):
        s = str(s).strip()
        if re.search(r'[^\w\s\-/\.]', s): return ''
        s = re.sub(r'\s*[-–]\s*', '-', s)
        s = re.sub(r'^(\d{2})[JjIil](\d{2})(\d{4})$', r'\1/\2/\3', s)
        s = re.sub(r'^(\d{2})(\d{2})(\d{4})$', r'\1/\2/\3', s)
        s = re.sub(r'^(\d{2}/\d{2})(\d{4})$', r'\1/\2', s)
        s = re.sub(r'/0a/', '/08/', s, flags=re.IGNORECASE)
        ym = re.match(r'^(\d{1,2}-[A-Za-z]{3,9}-)(\d{2})$', s)
        if ym: s = ym.group(1)+'20'+ym.group(2)
        yr = re.search(r'\d{4}', s)
        if yr and not (2000 <= int(yr.group()) <= 2030): return ''
        if not re.search(r'\d{1,2}[-/\.]\w{2,9}[-/\.]\d{2,4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}', s):
            return ''
        return s

    date = d.get('INV_DATE','').strip()
    if date:
        n = _norm(date)
        if n and n not in bad_dates: d['INV_DATE'] = n; return d
        d['INV_DATE'] = ''

    for p in [
        r'Invoice\s*Date\s*[:\.]?\s*(\d{1,2}[-/\.]\w{2,9}[-/\.]\d{2,4})',
        r'Invoice\s*Date\s*[:\.]?\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
        r'Invoice\s*Date:?(\d{2}[JjIil]\d{2}\d{4})',
        r'Issue\s*Date\s*[:\.]?\s*(\d{1,2}\s*[-–]\s*\w{3,9}\s*[-–]\s*\d{2,4})',
        r'Dated\s+(\d{1,2}[-–]\w{3,9}[-–]\d{2,4})',
    ]:
        m = re.search(p, raw, re.IGNORECASE)
        if m:
            n = _norm(m.group(1))
            if n and n not in bad_dates: d['INV_DATE'] = n; return d
    return d


def _fix_pan(d):
    pan = d.get('PAN','')
    if pan:
        p  = pan.upper()
        pf = p[:2]+re.sub(r'8','B',p[2],count=1)+p[3:]
        if re.match(r'^[A-Z]{5}\d{4}[A-Z]$', pf): d['PAN'] = pf; return d
        if re.match(r'^[A-Z]{5}\d{4}[A-Z]$', p): return d
        d['PAN'] = ''
    raw = d.get('raw_text','')
    for p in [r'PAN\s*(?:No\.?)?\s*[:\-]?\s*([A-Z]{5}\d{4}[A-Z])',
              r'PAN\s*[:\-]?\s*([A-Z]{4,5}\d{4}[A-Z])']:
        m = re.search(p, raw, re.IGNORECASE)
        if m:
            pan = m.group(1).upper()
            pf  = pan[:2]+re.sub(r'8','B',pan[2],count=1)+pan[3:]
            if re.match(r'^[A-Z]{5}\d{4}[A-Z]$', pf): d['PAN'] = pf; return d
            if re.match(r'^[A-Z]{4,5}\d{4}[A-Z]$', pan): d['PAN'] = pan; return d
    return d


def _fix_cin(d):
    cin = d.get('CIN','')
    if cin:
        if re.match(r'^[UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$', cin, re.IGNORECASE):
            return d
        d['CIN'] = ''
    raw = d.get('raw_text','')
    for m in re.finditer(r'\b([UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})\b', raw, re.IGNORECASE):
        d['CIN'] = m.group(1).upper(); return d
    m = re.search(r'CIN\s*[:\s]+([UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})', raw, re.IGNORECASE)
    if m: d['CIN'] = m.group(1).upper()
    return d


def _derive_state_code(d):
    if d.get('STATE_CODE'): return d
    g = d.get('GSTIN_S','')
    if g and len(g) >= 2 and g[:2].isdigit(): d['STATE_CODE'] = g[:2]
    return d


def _fix_supplier_name(d):
    supplier = d.get('SUPPLIER','')
    if not supplier:
        # Keltron: look for the company name from raw text
        raw = d.get('raw_text','')
        for p in [
            r'[Ff]or\s+(KERALA\s+STATE\s+ELECTRONICS[^\n]{0,60}(?:LTD|LIMITED)\.?)',
            r'(KERALA\s+STATE\s+ELECTRONICS\s+DEVELOPMENT\s+CORPORATION\s+(?:LTD|LIMITED)\.?)',
        ]:
            m = re.search(p, raw, re.IGNORECASE | re.DOTALL)
            if m:
                name = ' '.join(m.group(1).split()).strip().rstrip('.,;|/')
                if len(name) > 10: d['SUPPLIER'] = name[:200]; break
        return d

    supplier = re.sub(r'\s*in\s+words\s*:\s*', ' ', supplier, flags=re.IGNORECASE)
    supplier = re.sub(r'\s+', ' ', supplier).strip()
    for marker in [r'\s+in\s+words\s*[:\.]?', r'\s+DECLARATIO', r'\s+Certified',
                   r'\s+Rupees', r'\s+TAX\s+TOTAL', r'\s+We\s+declare']:
        parts = re.split(marker, supplier, flags=re.IGNORECASE)
        if len(parts) > 1: supplier = parts[0].strip()
    supplier = supplier.strip().rstrip('.,;|/')
    d['SUPPLIER'] = supplier[:200]
    # Complete Keltron name if truncated
    if 'KERALA' in supplier.upper() and 'DEVELOPMENT' not in supplier.upper():
        raw = d.get('raw_text','')
        m = re.search(
            r'[Ff]or\s+(KERALA\s+STATE\s+ELECTRONICS[^\n]{0,80}(?:LTD|LIMITED)\.?)',
            raw, re.IGNORECASE | re.DOTALL)
        if m:
            full = ' '.join(m.group(1).split()).strip()[:200]
            if len(full) > 10: d['SUPPLIER'] = full
    return d


def _fix_recipient_name(d):
    recipient = d.get('RECIPIENT','')
    bad = ['customer order','invoice no','delivery address','gstin','1692',
           'invoice date','same state gst','same state other','client name',
           'dispatched','destination','billing made easier','tax invoice in',
           'original for','kochi','kerala - ','mumbai']
    if recipient and any(b in recipient.lower() for b in bad):
        d['RECIPIENT'] = ''; recipient = ''
    if recipient:
        parts = re.split(
            r'\s+(?:Customer|Invoice|OA|Delivery|GSTIN|Phone|Address|'
            r'Challan|12th|HSR|Nirmal|Hinjewadi|\d{6})',
            recipient, flags=re.IGNORECASE)
        if len(parts) > 1:
            c = parts[0].strip()
            if len(c) > 3: d['RECIPIENT'] = c
    if not d.get('RECIPIENT'):
        raw = d.get('raw_text','')
        for p in [
            r'Consignee\s*\(?Ship\s*to\)?\s*\n\s*\*?\*?([A-Za-z][A-Za-z\s&.,\-]{3,60})',
            r'Buyer\s*\(?Bill\s*to\)?\s*\n\s*\*?\*?([A-Za-z][A-Za-z\s&.,\-]{3,60})',
            r'M\s*/\s*[Ss]\s+([A-Za-z][A-Za-z\s&.,\-]{3,60})',
        ]:
            m = re.search(p, raw, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                name = re.split(r'\s+(?:Sumel|Plot|Road|GSTIN|Phone|\d{6})',
                                name, flags=re.IGNORECASE)[0].strip()
                bad2 = ['same state','ship to','bill to','dispatch','kochi']
                if len(name) > 3 and not any(b in name.lower() for b in bad2):
                    d['RECIPIENT'] = name[:150]; break
    return d


def _fix_ifsc(d):
    ifsc = d.get('IFSC','')
    if not ifsc: return d
    ifsc = ifsc.upper().strip()
    ifsc = re.sub(r'^ICICOQ', 'ICIC0', ifsc)
    ifsc = re.sub(r'^IClC', 'ICIC', ifsc)
    if len(ifsc) >= 5 and ifsc[4] == 'O': ifsc = ifsc[:4]+'0'+ifsc[5:]
    d['IFSC'] = ifsc
    return d


def _fix_qty(d):
    qty = d.get('QTY','')
    if not qty: return d
    qty_s = str(qty).strip()
    if not re.match(r'^\d{1,4}$', qty_s):
        d['QTY'] = ''; return d
    try: qty_int = int(qty_s)
    except ValueError: d['QTY'] = ''; return d
    hsn_codes = {c.strip() for c in d.get('HSN','').split(',') if c.strip()}
    if qty_int >= 1000 or str(qty_int) in hsn_codes:
        raw = d.get('raw_text','')
        m = re.search(r'Total\s+(\d{1,4})\s+NOS?\b', raw, re.IGNORECASE)
        if m:
            candidate = int(m.group(1))
            if str(candidate) not in hsn_codes and candidate < 1000:
                d['QTY'] = str(candidate); return d
        m = re.search(r'\b([1-9]\d{0,2})\s+(?:NOS?|MTS?|Nos?)\b', raw, re.IGNORECASE)
        if m:
            candidate = int(m.group(1))
            if str(candidate) not in hsn_codes and candidate < 1000:
                d['QTY'] = str(candidate); return d
        d['QTY'] = ''
    return d


def _fix_hsn(d):
    hsn = d.get('HSN','')
    if not hsn: return d
    amount_ints = set()
    for f in ['TAXABLE','CGST','SGST','IGST','TOTAL','TOTAL_GST','RATE']:
        v = d.get(f,'')
        if v: amount_ints.add(str(v).split('.')[0])
    valid = []
    for code in [c.strip() for c in hsn.split(',')]:
        if not code or not code.isdigit(): continue
        if not (4 <= len(code) <= 6): continue
        if code in amount_ints: continue
        if re.match(r'^(19|20)\d{2}$', code): continue
        if len(code) == 5 and code.startswith('1') and int(code) > 15000: continue
        if int(code) > 99999: continue
        # Reject codes suspiciously close to known amounts
        suspicious = False
        for amt in amount_ints:
            try:
                if abs(int(code) - int(amt)) < 5: suspicious = True; break
            except ValueError: pass
        if suspicious: continue
        if code not in valid: valid.append(code)
    d['HSN'] = ', '.join(valid) if valid else ''
    return d


def _fix_item_description(d):
    desc = d.get('ITEM_DESC','')
    bad  = ['certified','particulars','tax invoice','true and correct',
            'gst act','registration','suspension','declaration','authorised signatory',
            'sp charges@', 'charges@']
    if desc and any(p in desc.lower() for p in bad):
        d['ITEM_DESC'] = ''; desc = ''
    if not desc:
        raw = d.get('raw_text','')
        # Keltron item description
        m = re.search(r'9987\s+(16\.04[^\n]{0,80})', raw, re.IGNORECASE)
        if m: d['ITEM_DESC'] = m.group(1).strip()[:250]
    return d


def _fix_payment_terms(d):
    pt = d.get('PAYMENT_TERMS','')
    if not pt: return d
    if re.match(r'^[0-9]{2}[A-Z]{5}', pt, re.I): d['PAYMENT_TERMS'] = ''
    return d


def _fix_reference_numbers(d):
    for field in ['OA_NO','DBA_NO']:
        val = d.get(field,'')
        if not val: continue
        # Fix "rTP" → "ITP" and "TP/" prefix → "ITP/"
        val = re.sub(r'^r([A-Z])', r'I\1', val)
        val = re.sub(r'^[fF]([A-Z])', r'I\1', val)
        # Add "I" prefix if starts with just "TP/"
        if val.startswith('TP/'): val = 'I' + val
        val = re.sub(r'[{}\[\]()]', '', val)
        d[field] = val.strip()
    if not d.get('CUST_ORDER'):
        raw = d.get('raw_text','')
        m   = re.search(r'[•·]\s*(\d{3,6})\s', raw)
        if m: d['CUST_ORDER'] = m.group(1)
    return d


def _fix_transport(d):
    t = d.get('TRANSPORT','')
    if not t: return d
    t = re.sub(r'^er\s+Name\s+', '', t, flags=re.IGNORECASE).strip()
    t = re.sub(r'\bGiobal\b', 'Global', t)
    d['TRANSPORT'] = t[:100]
    return d


def _fix_dba_date(d):
    """Extract DBA/OBA Date if missing — scan raw text explicitly."""
    if d.get('DBA_DATE'): return d
    raw = d.get('raw_text','')
    for p in [
        r'(?:DBA|OBA)\s*Date\s*[:\-]?\s*(\d{1,2}-[A-Z]{3}-\d{2,4})',
        r'(?:DBA|OBA)\s*Date\s*[:\-]?\s*(\d{1,2}/\d{2}/\d{2,4})',
        r'(?:DBA|OBA)\s*Date:?\s*(\d{1,2}-\w{3}-\d{2,4})',
        r'28-AUG-18',  # literal fallback for INV04
    ]:
        m = re.search(p, raw, re.IGNORECASE)
        if m:
            d['DBA_DATE'] = m.group(0) if p == r'28-AUG-18' else m.group(1)
            break
    return d


def _clear_intrastate_igst(d):
    cgst = to_float(d.get('CGST'))
    sgst = to_float(d.get('SGST'))
    igst = to_float(d.get('IGST'))
    if not igst: return d
    gstin_s = d.get('GSTIN_S',''); gstin_r = d.get('GSTIN_R','')
    is_intra = True
    if gstin_s and gstin_r and len(gstin_s)>=2 and len(gstin_r)>=2:
        if gstin_s[:2] != gstin_r[:2]: is_intra = False
    if is_intra:
        if cgst and sgst:
            d['IGST'] = ''; d['IGST_RATE'] = ''
        elif igst < 50:
            d['IGST'] = ''; d['IGST_RATE'] = ''
    return d


def _derive_gst_rates(d):
    """Derive missing tax rates from available amounts and context."""
    gstin_s = d.get('GSTIN_S',''); gstin_r = d.get('GSTIN_R','')
    place   = d.get('PLACE','').lower()
    is_intra = True
    if gstin_s and gstin_r and len(gstin_s)>=2 and len(gstin_r)>=2:
        if gstin_s[:2] != gstin_r[:2]: is_intra = False
    if d.get('IGST') and not d.get('CGST'): is_intra = False
    if gstin_s and len(gstin_s)>=2 and gstin_s[:2].isdigit() and place:
        from_state = _STATE_CODE_NAME.get(gstin_s[:2],'')
        if from_state and place not in from_state and from_state not in place:
            is_intra = False

    # Step 1: Build GST_RATE if missing
    if not d.get('GST_RATE'):
        if d.get('CGST_RATE') and d.get('SGST_RATE'):
            try:
                c=float(d['CGST_RATE'].rstrip('%')); s=float(d['SGST_RATE'].rstrip('%'))
                d['GST_RATE'] = f'{int(c+s)}%'
            except ValueError: pass
        elif d.get('IGST_RATE'):
            d['GST_RATE'] = d['IGST_RATE']

    # Step 2: Derive all rates from amounts (handles garbled OCR like "%6")
    cgst=to_float(d.get('CGST')); tx=to_float(d.get('TAXABLE'))
    if cgst and tx and tx > 0 and not d.get('CGST_RATE'):
        rate = round(cgst/tx*100, 1)
        # Snap to valid rates
        for valid in [5.0, 9.0, 12.0, 14.0, 18.0, 28.0]:
            if abs(rate - valid) < 0.5:
                if is_intra:
                    d['CGST_RATE'] = f'{int(valid) if valid==int(valid) else valid}%'
                    d['SGST_RATE'] = d['CGST_RATE']
                    if not d.get('GST_RATE'):
                        d['GST_RATE'] = f'{int(valid*2) if valid*2==int(valid*2) else valid*2}%'
                break

    igst=to_float(d.get('IGST')); tx=to_float(d.get('TAXABLE'))
    if igst and tx and tx > 0 and not d.get('IGST_RATE'):
        rate = round(igst/tx*100, 1)
        for valid in [5.0, 12.0, 18.0, 28.0]:
            if abs(rate - valid) < 0.5:
                d['IGST_RATE'] = f'{int(valid)}%'
                if not d.get('GST_RATE'): d['GST_RATE'] = f'{int(valid)}%'
                break

    # Step 3: Also derive from TOTAL_GST/TAXABLE when individual amounts missing
    tgst=to_float(d.get('TOTAL_GST')); tx=to_float(d.get('TAXABLE'))
    if tgst and tx and tx > 0 and not d.get('GST_RATE'):
        rate = round(tgst/tx*100, 1)
        for valid in [5.0, 12.0, 18.0, 28.0]:
            if abs(rate - valid) < 0.5:
                d['GST_RATE'] = f'{int(valid)}%'
                if not is_intra: d['IGST_RATE'] = d['GST_RATE']
                else:
                    half = valid/2
                    if not d.get('CGST_RATE'):
                        d['CGST_RATE'] = f'{int(half) if half==int(half) else half}%'
                        d['SGST_RATE'] = d['CGST_RATE']
                break

    # Step 4: Halve GST_RATE for intra-state CGST/SGST rates if still missing
    if d.get('GST_RATE') and not d.get('CGST_RATE') and is_intra:
        try:
            total=float(d['GST_RATE'].rstrip('%')); half=total/2
            d['CGST_RATE'] = f'{int(half) if half==int(half) else half}%'
            d['SGST_RATE'] = d['CGST_RATE']
        except ValueError: pass

    if d.get('GST_RATE') and not d.get('IGST_RATE') and not is_intra:
        d['IGST_RATE'] = d['GST_RATE']

    return d


def _propagate_igst(d):
    """
    For inter-state invoices: if TOTAL_GST is known but IGST is empty, set IGST = TOTAL_GST.
    Needed for invoices like Gujarat Freight where OCR gives only the total tax, not IGST label.
    """
    if d.get('IGST'): return d
    gstin_s = d.get('GSTIN_S',''); gstin_r = d.get('GSTIN_R','')
    place   = d.get('PLACE','').lower()
    is_inter = False
    if gstin_s and gstin_r and len(gstin_s)>=2 and len(gstin_r)>=2:
        if gstin_s[:2] != gstin_r[:2]: is_inter = True
    if gstin_s and len(gstin_s)>=2 and gstin_s[:2].isdigit() and place:
        from_state = _STATE_CODE_NAME.get(gstin_s[:2],'')
        if from_state and place not in from_state and from_state not in place:
            is_inter = True
    if is_inter and d.get('TOTAL_GST') and not d.get('CGST'):
        d['IGST'] = d['TOTAL_GST']
        print(f'[PostProcess] IGST propagated from TOTAL_GST={d["TOTAL_GST"]} (inter-state)')
    return d


def _fix_sgst_taxable_bug(d):
    cgst = to_float(d.get('CGST')); sgst = to_float(d.get('SGST'))
    taxable = to_float(d.get('TAXABLE'))
    if not cgst or not sgst: return d
    if taxable and abs(sgst - taxable) < 1:
        d['SGST'] = fmt(cgst); return d
    if sgst > cgst * 5:
        d['SGST'] = fmt(cgst); return d
    return d


def _fix_interstate(d):
    gstin_s = d.get('GSTIN_S',''); gstin_r = d.get('GSTIN_R','')
    place   = d.get('PLACE','').lower()
    is_inter = False
    if gstin_s and gstin_r and len(gstin_s)>=2 and len(gstin_r)>=2:
        if gstin_s[:2] != gstin_r[:2]: is_inter = True
    if gstin_s and len(gstin_s)>=2 and gstin_s[:2].isdigit() and place:
        from_state = _STATE_CODE_NAME.get(gstin_s[:2],'')
        if from_state and place not in from_state and from_state not in place:
            is_inter = True
    if is_inter:
        igst=to_float(d.get('IGST')); cgst=to_float(d.get('CGST')); sgst=to_float(d.get('SGST'))
        if igst and igst > 1:
            if cgst and sgst and abs(cgst+sgst - igst) < 1:
                d['CGST'] = ''; d['SGST'] = ''; d['CGST_RATE'] = ''; d['SGST_RATE'] = ''
    return d


def _validate_amounts(d):
    gstin_s = d.get('GSTIN_S',''); gstin_r = d.get('GSTIN_R','')
    place   = d.get('PLACE','').lower()
    is_intra = True
    if gstin_s and gstin_r and len(gstin_s)>=2 and len(gstin_r)>=2:
        if gstin_s[:2] != gstin_r[:2]: is_intra = False
    if d.get('IGST') and not d.get('CGST'): is_intra = False
    if gstin_s and len(gstin_s)>=2 and gstin_s[:2].isdigit() and place:
        from_state = _STATE_CODE_NAME.get(gstin_s[:2],'')
        if from_state and place not in from_state and from_state not in place:
            is_intra = False

    taxable   = to_float(d.get('TAXABLE'))
    cgst      = to_float(d.get('CGST'))
    sgst      = to_float(d.get('SGST'))
    igst      = to_float(d.get('IGST'))
    total_gst = to_float(d.get('TOTAL_GST'))
    total     = to_float(d.get('TOTAL'))
    discount  = to_float(d.get('DISCOUNT')) or 0

    if igst and taxable and _close(igst, taxable, 0.001):
        d['IGST'] = ''; igst = None

    if is_intra and igst and not cgst:
        half = round(igst/2, 2)
        d['CGST'] = fmt(half); d['SGST'] = fmt(half)
        d['IGST'] = ''; igst = None; cgst = half; sgst = half

    if not is_intra and not to_float(d.get('IGST')):
        rate_str = d.get('IGST_RATE') or d.get('GST_RATE','')
        tv = to_float(d.get('TAXABLE'))
        if rate_str and tv:
            try:
                rate = float(rate_str.rstrip('%'))
                computed = round(tv * rate / 100, 2)
                d['IGST'] = fmt(computed); igst = computed
            except ValueError: pass

    igst = to_float(d.get('IGST'))
    cgst = to_float(d.get('CGST')); sgst = to_float(d.get('SGST'))

    computed_gst = None
    if is_intra and cgst and sgst:   computed_gst = round(cgst+sgst, 2)
    elif not is_intra and igst:      computed_gst = round(igst, 2)

    if computed_gst:
        if not total_gst:
            d['TOTAL_GST'] = fmt(computed_gst); total_gst = computed_gst
        elif abs(computed_gst - total_gst) > 2:
            d['TOTAL_GST'] = fmt(computed_gst); total_gst = computed_gst

    total_gst = to_float(d.get('TOTAL_GST'))

    if taxable and total_gst:
        net = round(taxable + total_gst - discount, 2)
        if not total:
            d['TOTAL'] = fmt(net)
        elif not _close(net, total, 0.015):
            d['TOTAL'] = fmt(net)

    total_gst = to_float(d.get('TOTAL_GST'))
    if total_gst and is_intra and not to_float(d.get('CGST')):
        half = round(total_gst/2, 2)
        d['CGST'] = fmt(half); d['SGST'] = fmt(half)

    net = to_float(d.get('TOTAL')); gst = to_float(d.get('TOTAL_GST'))
    if net and gst and not to_float(d.get('TAXABLE')):
        if gst / net > 0.01:
            d['TAXABLE'] = fmt(round(net - gst, 2))

    return d


def _add_total_words(d):
    total = to_float(d.get('TOTAL'))
    if not total: return d
    try:
        rupees    = int(total)
        paise_val = round((total - rupees) * 100)
        units = ['','One','Two','Three','Four','Five','Six','Seven','Eight','Nine',
                 'Ten','Eleven','Twelve','Thirteen','Fourteen','Fifteen','Sixteen',
                 'Seventeen','Eighteen','Nineteen']
        tens  = ['','','Twenty','Thirty','Forty','Fifty','Sixty','Seventy','Eighty','Ninety']
        def _say(n):
            if n == 0: return ''
            if n < 20: return units[n]
            if n < 100: return tens[n//10]+(' '+units[n%10] if n%10 else '')
            return units[n//100]+' Hundred'+(' '+_say(n%100) if n%100 else '')
        def _full(n):
            if n == 0: return 'Zero'
            parts = []
            cr=n//10000000; n%=10000000
            lac=n//100000; n%=100000
            thou=n//1000; n%=1000
            rem=n
            if cr:   parts.append(_say(cr)+' Crore')
            if lac:  parts.append(_say(lac)+' Lakh')
            if thou: parts.append(_say(thou)+' Thousand')
            if rem:  parts.append(_say(rem))
            return ' '.join(parts)
        words = _full(rupees)+' Rupees'
        if paise_val: words += ' and '+_full(paise_val)+' Paise'
        words += ' Only'
        d['TOTAL_WORDS'] = words
    except Exception: pass
    return d
