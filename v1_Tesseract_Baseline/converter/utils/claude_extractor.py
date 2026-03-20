"""
claude_extractor.py — Primary GST extractor using Claude Vision API
Place in: converter/utils/claude_extractor.py

pip install anthropic
Set ANTHROPIC_API_KEY = 'sk-ant-...' in gst_converter/settings.py
"""
import os, re, json, base64
from io import BytesIO
from PIL import Image
from django.conf import settings

PROMPT = """You are an expert Indian GST invoice data extractor.
Read every part of this invoice and extract ALL visible data.

RULES:
1. Return ONLY valid JSON. Start { end }. No markdown.
2. Extract EXACTLY as written. Do not infer or calculate.
3. Empty string "" for missing fields.
4. Numbers: strip Rs/INR/Rs./comma -> "3500.00"
5. GSTIN exactly 15 chars. PAN exactly 10 chars.
6. Extract ALL rows from the item table.
7. SELLER = supplier. BUYER = recipient/client/consignee.

Return this JSON:
{
  "SUPPLIER":"","GSTIN_S":"","PAN":"","CIN":"","STATE_CODE":"",
  "SERVICE_TAX":"","TAN":"","FSSAI":"","LUT_BOND":"","MSME":"",
  "SUPPLIER_ADDRESS":"","SUPPLIER_EMAIL":"","SUPPLIER_PHONE":"","SUPPLIER_WEBSITE":"",
  "IRN":"","ACK_NO":"","ACK_DATE":"",
  "INV_NO":"","INV_DATE":"","DUE_DATE":"",
  "CHALLAN_NO":"","CHALLAN_DATE":"","EWAY_BILL":"",
  "TRANSPORT_NAME":"","TRANSPORT_ID":"","VEHICLE_NO":"","SHIP_BY":"",
  "DISPATCH_FROM":"","DISPATCH_DATE":"",
  "RECIPIENT":"","GSTIN_R":"","RECIPIENT_PAN":"","RECIPIENT_ADDRESS":"",
  "PLACE_OF_SUPPLY":"","VENDOR_CODE":"",
  "CUST_ORDER":"","OA_NO":"","OA_DATE":"","DBA_NO":"","DBA_DATE":"",
  "PAYMENT_TERMS":"","MODE_OF_DISPATCH":"",
  "LINE_ITEMS":[{
    "SL_NO":"","DESCRIPTION":"","HSN":"","QTY":"","UOM":"","RATE":"","DISCOUNT":"",
    "TAXABLE_VALUE":"","CGST_RATE":"","CGST_AMT":"","SGST_RATE":"","SGST_AMT":"",
    "IGST_RATE":"","IGST_AMT":"","TOTAL":""
  }],
  "TAXABLE":"","CGST_RATE":"","CGST":"","SGST_RATE":"","SGST":"",
  "IGST_RATE":"","IGST":"","TOTAL_GST":"","GST_RATE":"","DISCOUNT":"",
  "TOTAL":"","TOTAL_WORDS":"",
  "BANK_NAME":"","ACCOUNT_NO":"","IFSC":"","BRANCH":"","ACCOUNT_HOLDER":"","UPI_ID":""
}"""

def _img_to_b64(image, max_px=1568):
    w, h = image.size
    if max(w, h) > max_px:
        s = max_px / max(w, h)
        image = image.resize((int(w*s), int(h*s)), Image.LANCZOS)
    if image.mode != 'RGB':
        image = image.convert('RGB')
    buf = BytesIO()
    image.save(buf, format='JPEG', quality=92)
    buf.seek(0)
    return base64.standard_b64encode(buf.read()).decode('utf-8')

def _parse_json(raw):
    t = raw.strip()
    if t.startswith('`'):
        lines = t.split('\n')
        t = '\n'.join(lines[1:-1] if lines[-1].strip()=='```' else lines[1:])
    s=t.find('{'); e=t.rfind('}')
    if s>=0 and e>s: t=t[s:e+1]
    try: return json.loads(t)
    except:
        try: return json.loads(re.sub(r',(\s*[}\]])',r'\1',t))
        except: print('[Claude] JSON parse failed'); return None

def _call_api(b64, key, pg=1):
    print(f'[Claude] Calling Vision API page {pg}...')
    payload = {
        'model':'claude-sonnet-4-20250514','max_tokens':4096,
        'messages':[{'role':'user','content':[
            {'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':b64}},
            {'type':'text','text':PROMPT},
        ]}]
    }
    try:
        import anthropic
        c = anthropic.Anthropic(api_key=key)
        r = c.messages.create(**payload)
        return _parse_json(r.content[0].text)
    except ImportError:
        pass
    except Exception as e:
        print(f'[Claude] SDK error: {e}'); return None
    import urllib.request
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=json.dumps(payload).encode(),
        headers={'x-api-key':key,'anthropic-version':'2023-06-01','content-type':'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return _parse_json(json.loads(r.read())['content'][0]['text'])
    except Exception as e:
        print(f'[Claude] HTTP error: {e}'); return None

def _merge_pages(pages):
    m={}; items=[]
    for p in pages:
        if not p: continue
        for k,v in p.items():
            if k=='LINE_ITEMS':
                if isinstance(v,list): items.extend(v)
            elif not m.get(k) and v and str(v).strip():
                m[k]=v
    if items: m['LINE_ITEMS']=items
    return m

def extract_with_claude(file_path):
    key = getattr(settings,'ANTHROPIC_API_KEY','')
    if not key or key.startswith('your-'):
        print('[Claude] No API key — using regex fallback'); return None
    ext = os.path.splitext(file_path)[1].lower()
    pages=[]
    try:
        if ext=='.pdf':
            from pdf2image import convert_from_path
            for i,img in enumerate(convert_from_path(file_path,dpi=200,
                    poppler_path=settings.POPPLER_PATH,thread_count=2)):
                r=_call_api(_img_to_b64(img),key,i+1)
                if r: pages.append(r)
        else:
            img=Image.open(file_path).convert('RGB')
            r=_call_api(_img_to_b64(img),key,1)
            if r: pages.append(r)
    except Exception as e:
        print(f'[Claude] File error: {e}'); return None
    if not pages: return None
    merged=_merge_pages(pages)
    print(f'[Claude] Merged: {len(merged)} keys')
    return merged

def normalise_claude_output(raw):
    if not raw: return {}
    d={}
    NUM={'TAXABLE','CGST','SGST','IGST','TOTAL','TOTAL_GST','DISCOUNT','RATE'}
    PCT={'CGST_RATE','SGST_RATE','IGST_RATE','GST_RATE'}
    MAP={
        'SUPPLIER':'SUPPLIER','GSTIN_S':'GSTIN_S','PAN':'PAN','CIN':'CIN',
        'STATE_CODE':'STATE_CODE','SERVICE_TAX':'SERVICE_TAX','TAN':'TAN',
        'FSSAI':'FSSAI','LUT_BOND':'LUT_BOND','MSME':'MSME',
        'SUPPLIER_ADDRESS':'SUPPLIER_ADDRESS','SUPPLIER_EMAIL':'SUPPLIER_EMAIL',
        'SUPPLIER_PHONE':'SUPPLIER_PHONE','SUPPLIER_WEBSITE':'SUPPLIER_WEBSITE',
        'IRN':'IRN','ACK_NO':'ACK_NO','ACK_DATE':'ACK_DATE',
        'INV_NO':'INV_NO','INV_DATE':'INV_DATE','DUE_DATE':'DUE_DATE',
        'CHALLAN_NO':'CHALLAN_NO','CHALLAN_DATE':'CHALLAN_DATE',
        'EWAY_BILL':'EWAY_BILL','TRANSPORT_NAME':'TRANSPORT_NAME',
        'TRANSPORT_ID':'TRANSPORT_ID','VEHICLE_NO':'VEHICLE_NO',
        'SHIP_BY':'SHIP_BY','DISPATCH_FROM':'DISPATCH_FROM','DISPATCH_DATE':'DISPATCH_DATE',
        'RECIPIENT':'RECIPIENT','GSTIN_R':'GSTIN_R','RECIPIENT_PAN':'RECIPIENT_PAN',
        'RECIPIENT_ADDRESS':'RECIPIENT_ADDRESS','PLACE_OF_SUPPLY':'PLACE',
        'VENDOR_CODE':'VENDOR_CODE','CUST_ORDER':'CUST_ORDER',
        'OA_NO':'OA_NO','OA_DATE':'OA_DATE','DBA_NO':'DBA_NO','DBA_DATE':'DBA_DATE',
        'PAYMENT_TERMS':'PAYMENT_TERMS','MODE_OF_DISPATCH':'MODE_OF_DISPATCH',
        'TAXABLE':'TAXABLE','CGST_RATE':'CGST_RATE','CGST':'CGST',
        'SGST_RATE':'SGST_RATE','SGST':'SGST','IGST_RATE':'IGST_RATE','IGST':'IGST',
        'TOTAL_GST':'TOTAL_GST','GST_RATE':'GST_RATE','DISCOUNT':'DISCOUNT',
        'TOTAL':'TOTAL','TOTAL_WORDS':'TOTAL_WORDS',
        'BANK_NAME':'BANK_NAME','ACCOUNT_NO':'ACCOUNT_NO','IFSC':'IFSC',
        'BRANCH':'BRANCH','ACCOUNT_HOLDER':'ACCOUNT_HOLDER','UPI_ID':'UPI_ID',
        'LINE_ITEMS':'LINE_ITEMS',
    }
    for ck,ak in MAP.items():
        v=raw.get(ck,'')
        if v is None: v=''
        if isinstance(v,list): d[ak]=v; continue
        sv=str(v).strip()
        if not sv: continue
        if ak in NUM:
            sv=re.sub(r'[₹\s,]','',sv)
            sv=re.sub(r'^(?:INR|Rs\.?)','',sv,flags=re.IGNORECASE).strip()
        d[ak]=sv
    for f in PCT:
        v=d.get(f,'')
        if v and not v.endswith('%'): d[f]=v+'%'
    if not d.get('STATE_CODE') and d.get('GSTIN_S'):
        g=d['GSTIN_S']
        if len(g)>=2 and g[:2].isdigit(): d['STATE_CODE']=g[:2]
    items=d.get('LINE_ITEMS',[])
    if isinstance(items,list):
        clean=[]
        for item in items:
            if not isinstance(item,dict): continue
            ci={}
            for k,v in item.items():
                cv=str(v).strip() if v is not None else ''
                if k in ('RATE','TAXABLE_VALUE','CGST_AMT','SGST_AMT','IGST_AMT','TOTAL'):
                    cv=re.sub(r'[₹\s,]','',cv)
                ci[k]=cv
            if ci.get('DESCRIPTION') or ci.get('HSN'): clean.append(ci)
        d['LINE_ITEMS']=clean
        if d['LINE_ITEMS']:
            first=d['LINE_ITEMS'][0]
            hsns=list(dict.fromkeys(i.get('HSN','') for i in d['LINE_ITEMS'] if i.get('HSN')))
            if hsns and not d.get('HSN'): d['HSN']=', '.join(hsns)
            if not d.get('ITEM_DESC') and first.get('DESCRIPTION'): d['ITEM_DESC']=first['DESCRIPTION']
            for k in ['QTY','UOM','RATE']:
                if not d.get(k) and first.get(k): d[k]=first[k]
    print(f'[Claude] Normalised: {len([k for k,v in d.items() if v and k!="LINE_ITEMS"])} fields')
    return d
