"""
auto_train/trainer.py  v2.0
Skips fake invoices. Better labels. Saves model even if F1=0.
"""
import os, json, re, threading, traceback
from datetime import datetime

MIN_NEW_FOR_TRAINING = 5
MIN_FIELDS_TO_USE    = 4
INCREMENTAL_EPOCHS   = 8
FULL_RETRAIN_EVERY   = 40

FAKE_PATTERNS = [
    r'AAAAA0000A', r'Insert\s+Logo', r'ExcelDataPro', r'DO/MMYYYY',
    r'TRAINING USE ONLY', r'Wear Your Opinion', r'GogstBill',
    r'Street Address, Phone 1234567890', r'ABC Building, DEF Street',
    r'Your Company Name', r'SAMPLE INVOICE', r'TEST INVOICE',
    r'dummy', r'000-000-0000',
]

_lock = threading.Lock()


def schedule_training(triggered_by_user=None):
    t = threading.Thread(target=_safe_run, args=(triggered_by_user,), daemon=True)
    t.start()
    print('[AutoTrain] Background training thread started.')


def _safe_run(triggered_by_user=None):
    if not _lock.acquire(blocking=False):
        print('[AutoTrain] Already running — skipped.')
        return
    try:
        _run(triggered_by_user)
    except Exception as e:
        print(f'[AutoTrain] Fatal error: {e}')
        traceback.print_exc()
    finally:
        _lock.release()


def _is_fake(job):
    if job.field_count < MIN_FIELDS_TO_USE:
        return True
    data = job.get_data()
    raw  = data.get('raw_text', '')
    for p in FAKE_PATTERNS:
        if re.search(p, raw, re.IGNORECASE):
            return True
    gstin = data.get('GSTIN_S', '')
    if gstin and re.search(r'AAAAA|0000A', gstin):
        return True
    return False


def _run(triggered_by_user=None):
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gst_converter.settings')
    from django.conf import settings as dj
    from converter.models import InvoiceJob, TrainingRun, ModelVersion

    new_jobs  = list(InvoiceJob.objects.filter(status='done', used_for_training=False, uploaded_file__isnull=False))
    real_jobs = [j for j in new_jobs if not _is_fake(j)]
    fake_jobs = [j for j in new_jobs if _is_fake(j)]
    fake_count = len(fake_jobs)

    # Mark fakes as used so they don't keep reappearing
    for j in fake_jobs:
        j.used_for_training = True
        j.save(update_fields=['used_for_training'])

    count = len(real_jobs)
    if count < MIN_NEW_FOR_TRAINING:
        print(f'[AutoTrain] {count} real invoices < {MIN_NEW_FOR_TRAINING}. Skipping. (skipped {fake_count} fakes)')
        return

    total_trained = InvoiceJob.objects.filter(used_for_training=True, field_count__gte=MIN_FIELDS_TO_USE).count()
    full_retrain  = (total_trained % FULL_RETRAIN_EVERY) < count

    print(f'[AutoTrain] {"Full" if full_retrain else "Incremental"} training on {count} real invoices (skipped {fake_count} fakes)')

    run = TrainingRun.objects.create(
        status='running', invoices_used=count, epochs=INCREMENTAL_EPOCHS,
        triggered_by=triggered_by_user,
        log=f'Started: {datetime.now()}\nMode: {"full" if full_retrain else "incremental"}\nReal: {count}, Fakes skipped: {fake_count}\n'
    )

    try:
        labeled = _build_dataset(real_jobs, dj)
        run.log += f'Labeled: {labeled}\n'
        run.save(update_fields=['log'])

        if labeled < 2:
            run.status = 'error'; run.log += 'Not enough samples\n'
            run.save(update_fields=['status','log']); return

        best_f1 = _fine_tune(run, dj, full_retrain)

        for j in real_jobs:
            j.used_for_training = True
            j.save(update_fields=['used_for_training'])

        output_dir = _model_dir(dj)
        ModelVersion.objects.filter(is_active=True).update(is_active=False)
        latest = ModelVersion.objects.first()
        v = (latest.version + 1) if latest else 1
        ModelVersion.objects.create(
            version=v, training_run=run, f1_score=best_f1,
            invoices_trained_on=total_trained+count, model_path=output_dir,
            is_active=True,
            notes=f'Trained {count} real invoices. {"Full." if full_retrain else "Incremental."} F1={best_f1:.4f}'
        )

        try:
            from converter.utils import layoutlm_extractor as ext
            ext._model_cache.clear(); ext._extractor = None
            print('[AutoTrain] Model cache cleared.')
        except Exception as ce:
            print(f'[AutoTrain] Cache clear: {ce}')

        run.status='done'; run.finished_at=datetime.now()
        run.best_f1=best_f1; run.model_path=output_dir
        run.log+=f'Done. F1={best_f1:.4f}\n'; run.save()
        print(f'[AutoTrain] Model v{v} saved. F1={best_f1:.4f}')

    except Exception as e:
        run.status='error'; run.finished_at=datetime.now()
        run.log+=f'ERROR: {e}\n{traceback.format_exc()}'
        run.save(update_fields=['status','finished_at','log'])
        print(f'[AutoTrain] Error: {e}')


def _model_dir(settings):
    p = getattr(settings, 'LAYOUTLM_FINETUNED_PATH', None)
    return str(p) if p else os.path.join(str(settings.BASE_DIR), 'models', 'layoutlmv3_gst_finetuned')


def _build_dataset(jobs, settings):
    import numpy as np
    from PIL import Image

    LABEL2ID = {
        'O':0,'B-GSTIN_S':1,'I-GSTIN_S':2,'B-GSTIN_R':3,'I-GSTIN_R':4,
        'B-INV_NO':5,'I-INV_NO':6,'B-INV_DATE':7,'I-INV_DATE':8,
        'B-TAXABLE':9,'I-TAXABLE':10,'B-CGST':11,'I-CGST':12,
        'B-SGST':13,'I-SGST':14,'B-IGST':15,'I-IGST':16,
        'B-TOTAL':17,'I-TOTAL':18,'B-HSN':19,'I-HSN':20,
        'B-ITEM_DESC':21,'I-ITEM_DESC':22,'B-SUPPLIER':23,'I-SUPPLIER':24,
        'B-RECIPIENT':25,'I-RECIPIENT':26,'B-PLACE':27,'I-PLACE':28,
        'B-RATE':29,'I-RATE':30,'B-QTY':31,'I-QTY':32,
        'B-CGST_RATE':33,'I-CGST_RATE':34,'B-SGST_RATE':35,'I-SGST_RATE':36,
    }

    ds_dir  = os.path.join(str(settings.BASE_DIR), 'dataset', 'hf_dataset')
    os.makedirs(ds_dir, exist_ok=True)
    ds_path = os.path.join(ds_dir, 'dataset.json')

    samples = []
    if os.path.exists(ds_path):
        try:
            with open(ds_path, encoding='utf-8') as f:
                existing = json.load(f)
            # Keep only high-quality existing samples
            for s in existing:
                non_o = sum(1 for l in s.get('labels',[]) if l!=0)
                if non_o >= 3:
                    samples.append(s)
            print(f'[AutoTrain] Kept {len(samples)} quality samples from existing dataset')
        except Exception:
            samples = []

    try:
        from converter.utils.pdf_processor import _get_paddle_ocr
        ocr = _get_paddle_ocr()
    except Exception as e:
        print(f'[AutoTrain] OCR load failed: {e}'); return 0

    labeled = 0
    for job in jobs:
        try:
            path = job.uploaded_file.path
            if not os.path.exists(path): continue
            ext = os.path.splitext(path)[1].lower()
            if ext == '.pdf':
                from converter.utils.pdf_processor import pdf_to_images
                imgs = pdf_to_images(path)
                if not imgs: continue
                img = imgs[0]
            else:
                from converter.utils.pdf_processor import load_image
                img = load_image(path)

            W, H = img.size
            result = ocr.ocr(import_np(img), cls=True)
            if not result or not result[0]: continue

            extracted = job.get_data()
            words, bboxes, labels = [], [], []
            for line in result[0]:
                if not line or len(line) < 2: continue
                box_pts, ti = line[0], line[1]
                if not ti: continue
                text = str(ti[0]).strip()
                conf = float(ti[1]) if len(ti)>1 else 1.0
                if not text or conf < 0.4: continue
                xs=[p[0] for p in box_pts]; ys=[p[1] for p in box_pts]
                norm=[max(0,int(1000*min(xs)/W)),max(0,int(1000*min(ys)/H)),
                      min(1000,int(1000*max(xs)/W)),min(1000,int(1000*max(ys)/H))]
                for w in text.split():
                    if w.strip():
                        words.append(w); bboxes.append(norm)
                        labels.append(_lbl(w, extracted, LABEL2ID))

            if not words: continue
            non_o = sum(1 for l in labels if l!=0)
            if non_o < 3:
                print(f'[AutoTrain] Job {job.pk}: {non_o} labeled — skipping')
                continue

            samples = [s for s in samples if s.get('id')!=str(job.pk)]
            samples.append({'id':str(job.pk),'image_path':path,'words':words,'bboxes':bboxes,'labels':labels,'non_o':non_o})
            labeled += 1
            print(f'[AutoTrain] Job {job.pk}: {len(words)} words, {non_o} labeled')
        except Exception as e:
            print(f'[AutoTrain] Label error {job.pk}: {e}')

    if not samples: return 0

    samples.sort(key=lambda s: s.get('non_o',0), reverse=True)
    with open(ds_path,'w',encoding='utf-8') as f: json.dump(samples, f, ensure_ascii=False)

    import random; shuffled=list(samples); random.shuffle(shuffled)
    split=max(1,int(len(shuffled)*0.8))
    train_data=shuffled[:split]; val_data=shuffled[split:] or shuffled[:max(1,split//4)]
    for name,data in [('train',train_data),('val',val_data)]:
        with open(os.path.join(ds_dir,f'{name}.json'),'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False)
    print(f'[AutoTrain] Dataset: {len(samples)} total, {len(train_data)} train, {len(val_data)} val')
    return labeled


def import_np(img):
    import numpy as np
    return np.array(img)


def _lbl(word, extracted, LABEL2ID):
    w = word.strip().upper()
    if not w: return LABEL2ID['O']
    GSTIN_RE = r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]{3}$'
    if re.match(GSTIN_RE, w):
        if w==extracted.get('GSTIN_S','').upper(): return LABEL2ID['B-GSTIN_S']
        if w==extracted.get('GSTIN_R','').upper(): return LABEL2ID['B-GSTIN_R']
        return LABEL2ID['B-GSTIN_S']
    inv=extracted.get('INV_NO','')
    if inv and len(inv)>3 and w==inv.upper(): return LABEL2ID['B-INV_NO']
    def amt(key):
        v=extracted.get(key,'')
        if not v: return False
        try:
            fv=float(str(v).replace(',',''))
            fw=float(re.sub(r'[,₹+%]','',word))
            return abs(fv-fw)<0.5 and fv>0
        except: return False
    for key,lbl in [('TAXABLE','B-TAXABLE'),('CGST','B-CGST'),('SGST','B-SGST'),
                     ('IGST','B-IGST'),('TOTAL','B-TOTAL'),('RATE','B-RATE'),('TOTAL_GST','B-TOTAL')]:
        if amt(key): return LABEL2ID[lbl]
    if re.match(r'^(5|9|12|14|18|28)%$',word):
        rv=word.rstrip('%')
        if rv==extracted.get('CGST_RATE','').rstrip('%'): return LABEL2ID['B-CGST_RATE']
        if rv==extracted.get('IGST_RATE','').rstrip('%'): return LABEL2ID['B-CGST_RATE']
    hsn=extracted.get('HSN','')
    if hsn and re.match(r'^\d{4,6}$',w):
        if any(w==c.strip().upper() for c in hsn.split(',')): return LABEL2ID['B-HSN']
    sup=extracted.get('SUPPLIER','').upper()
    if sup and len(w)>3:
        ws=sup.split()
        if w in ws: return LABEL2ID['B-SUPPLIER'] if ws.index(w)==0 else LABEL2ID['I-SUPPLIER']
    rec=extracted.get('RECIPIENT','').upper()
    if rec and len(w)>3:
        wr=rec.split()
        if w in wr: return LABEL2ID['B-RECIPIENT'] if wr.index(w)==0 else LABEL2ID['I-RECIPIENT']
    return LABEL2ID['O']


def _fine_tune(run, settings, full_retrain):
    import torch
    from transformers import LayoutLMv3Processor, LayoutLMv3ForTokenClassification, get_linear_schedule_with_warmup
    from torch.utils.data import Dataset, DataLoader
    from PIL import Image

    LABEL_LIST=['O','B-GSTIN_S','I-GSTIN_S','B-GSTIN_R','I-GSTIN_R',
        'B-INV_NO','I-INV_NO','B-INV_DATE','I-INV_DATE','B-TAXABLE','I-TAXABLE',
        'B-CGST','I-CGST','B-SGST','I-SGST','B-IGST','I-IGST','B-TOTAL','I-TOTAL',
        'B-HSN','I-HSN','B-ITEM_DESC','I-ITEM_DESC','B-SUPPLIER','I-SUPPLIER',
        'B-RECIPIENT','I-RECIPIENT','B-PLACE','I-PLACE','B-RATE','I-RATE',
        'B-QTY','I-QTY','B-CGST_RATE','I-CGST_RATE','B-SGST_RATE','I-SGST_RATE']
    LABEL2ID={l:i for i,l in enumerate(LABEL_LIST)}
    ID2LABEL={i:l for i,l in enumerate(LABEL_LIST)}

    output_dir=_model_dir(settings)
    os.makedirs(output_dir,exist_ok=True)
    base=getattr(settings,'LAYOUTLM_BASE_MODEL','microsoft/layoutlmv3-base')
    has_ft=(not full_retrain and
            os.path.exists(os.path.join(output_dir,'config.json')) and
            os.path.exists(os.path.join(output_dir,'preprocessor_config.json')))
    load_from=output_dir if has_ft else base
    print(f'[AutoTrain] Loading from: {"local fine-tuned" if has_ft else "base model"}')

    ds_dir=os.path.join(str(settings.BASE_DIR),'dataset','hf_dataset')
    train_path=os.path.join(ds_dir,'train.json')
    val_path=os.path.join(ds_dir,'val.json')
    if not os.path.exists(train_path): return 0.0
    with open(train_path) as f:
        if len(json.load(f))<1: return 0.0

    class DS(Dataset):
        def __init__(self,path,proc):
            with open(path,encoding='utf-8') as f: self.data=json.load(f)
            self.proc=proc
        def __len__(self): return len(self.data)
        def __getitem__(self,idx):
            s=self.data[idx]
            try: img=Image.open(s['image_path']).convert('RGB')
            except: img=Image.new('RGB',(224,224),'white')
            w,b,lb=s['words'],s['bboxes'],s['labels']
            vw,vb,vl=[],[],[]
            for wi,bi,li in zip(w,b,lb):
                if len(bi)==4 and bi[2]>bi[0] and bi[3]>bi[1]:
                    vw.append(wi);vb.append(bi);vl.append(li)
            if not vw: vw=['[PAD]'];vb=[[0,0,1,1]];vl=[0]
            try: enc=self.proc(img,vw,boxes=vb,word_labels=vl,max_length=512,padding='max_length',truncation=True,return_tensors='pt')
            except: enc=self.proc(Image.new('RGB',(224,224),'white'),['[PAD]'],boxes=[[0,0,1,1]],word_labels=[0],max_length=512,padding='max_length',truncation=True,return_tensors='pt')
            return {k:v.squeeze(0) for k,v in enc.items()}

    device=torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    processor=LayoutLMv3Processor.from_pretrained(load_from,apply_ocr=False)
    model=LayoutLMv3ForTokenClassification.from_pretrained(load_from,num_labels=len(LABEL_LIST),id2label=ID2LABEL,label2id=LABEL2ID,ignore_mismatched_sizes=True).to(device)

    train_dl=DataLoader(DS(train_path,processor),batch_size=2,shuffle=True,num_workers=0)
    val_dl=DataLoader(DS(val_path,processor),batch_size=2,shuffle=False,num_workers=0)

    optimizer=torch.optim.AdamW(model.parameters(),lr=2e-5,weight_decay=0.01)
    total_steps=len(train_dl)*run.epochs
    scheduler=get_linear_schedule_with_warmup(optimizer,num_warmup_steps=max(1,int(total_steps*0.1)),num_training_steps=total_steps)
    use_amp=(device.type=='cuda')
    scaler=torch.amp.GradScaler('cuda') if use_amp else None
    best_f1=0.0

    for epoch in range(1,run.epochs+1):
        model.train(); t_loss=0.0
        for batch in train_dl:
            batch={k:v.to(device) for k,v in batch.items()}
            optimizer.zero_grad()
            if use_amp:
                with torch.amp.autocast('cuda'): out=model(**batch); loss=out.loss
                scaler.scale(loss).backward(); scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(),1.0)
                scaler.step(optimizer); scaler.update()
            else:
                out=model(**batch); loss=out.loss; loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(),1.0); optimizer.step()
            scheduler.step(); t_loss+=loss.item()

        model.eval(); all_p,all_l=[],[]
        with torch.no_grad():
            for batch in val_dl:
                batch={k:v.to(device) for k,v in batch.items()}
                if use_amp:
                    with torch.amp.autocast('cuda'): out=model(**batch)
                else: out=model(**batch)
                for p,l in zip(out.logits.argmax(-1).cpu().numpy(),batch['labels'].cpu().numpy()):
                    all_p.extend(p.tolist()); all_l.extend(l.tolist())

        f1=_f1(all_p,all_l)
        line=f'Epoch {epoch}/{run.epochs}  loss={t_loss/max(1,len(train_dl)):.4f}  F1={f1:.4f}'
        print(f'[AutoTrain] {line}'); run.log+=line+'\n'; run.save(update_fields=['log'])

        if f1>best_f1:
            best_f1=f1
            model.save_pretrained(output_dir); processor.save_pretrained(output_dir)
            print(f'[AutoTrain] Best model saved (F1={f1:.4f})')

        if device.type=='cuda': torch.cuda.empty_cache()

    # Always save final model (even if F1=0 — model still learns)
    if best_f1==0.0:
        model.save_pretrained(output_dir); processor.save_pretrained(output_dir)
        print('[AutoTrain] Model saved (need more labeled data for non-zero F1)')

    return best_f1


def _f1(preds,labels):
    from collections import defaultdict
    tp=defaultdict(int);fp=defaultdict(int);fn=defaultdict(int)
    for p,t in zip(preds,labels):
        if t==-100: continue
        if p==t: tp[t]+=1
        else: fp[p]+=1;fn[t]+=1
    scores=[]
    for lbl in set(list(tp)+list(fp)+list(fn)):
        if lbl==0: continue
        pr=tp[lbl]/(tp[lbl]+fp[lbl]+1e-8); re=tp[lbl]/(tp[lbl]+fn[lbl]+1e-8)
        scores.append(2*pr*re/(pr+re+1e-8))
    return sum(scores)/len(scores) if scores else 0.0
