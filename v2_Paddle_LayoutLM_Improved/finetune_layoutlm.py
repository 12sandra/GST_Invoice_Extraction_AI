"""
finetune_layoutlm.py  v4.0
============================
Place in: gst_project/finetune_layoutlm.py

FIX: "ValueError: Attempting to unscale FP16 gradients"
  The model must stay in FP32 during training.
  FP16/AMP only applies to the FORWARD PASS via torch.amp.autocast.
  GradScaler works with FP32 model + FP16 activations, NOT with a half() model.

Usage:
  python finetune_layoutlm.py          # full training
  python finetune_layoutlm.py --test   # just test saved model
"""

import os
import sys
import json
import time
import django
import argparse

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gst_converter.settings')
django.setup()

from django.conf import settings as django_settings
import torch
import numpy as np
from PIL import Image
from torch.utils.data import Dataset, DataLoader
from transformers import (
    LayoutLMv3Processor,
    LayoutLMv3ForTokenClassification,
    get_linear_schedule_with_warmup,
)

# ─── Config ───────────────────────────────────────────────────────────────────
BASE_DIR    = django_settings.BASE_DIR
DATASET_DIR = getattr(django_settings, 'DATASET_DIR',
                      os.path.join(BASE_DIR, 'dataset'))
HF_DIR      = os.path.join(DATASET_DIR, 'hf_dataset')
OUTPUT_DIR  = os.path.join(BASE_DIR, 'models', 'layoutlmv3_gst_finetuned')
BASE_MODEL  = getattr(django_settings, 'LAYOUTLM_BASE_MODEL',
                      'microsoft/layoutlmv3-base')

EPOCHS      = 20
BATCH_SIZE  = 2       # safe for 6GB VRAM
LR          = 2e-5
WARMUP_RATIO = 0.1
MAX_LENGTH  = 512
SEED        = 42

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
NUM_LABELS = len(LABEL_LIST)


# ─── Dataset ──────────────────────────────────────────────────────────────────
class GSTDataset(Dataset):
    def __init__(self, json_path, processor, max_length=512):
        with open(json_path, encoding='utf-8') as f:
            self.samples = json.load(f)
        self.processor  = processor
        self.max_length = max_length

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        sample   = self.samples[idx]
        img_path = sample['image_path']
        words    = sample['words']
        bboxes   = sample['bboxes']
        labels   = sample['labels']

        try:
            image = Image.open(img_path).convert('RGB')
        except Exception:
            image = Image.new('RGB', (224, 224), color='white')

        valid_words, valid_bboxes, valid_labels = [], [], []
        for w, b, l in zip(words, bboxes, labels):
            if (len(b) == 4 and 0 <= b[0] <= 1000 and 0 <= b[1] <= 1000
                    and 0 <= b[2] <= 1000 and 0 <= b[3] <= 1000
                    and b[2] > b[0] and b[3] > b[1]):
                valid_words.append(w)
                valid_bboxes.append(b)
                valid_labels.append(l)

        if not valid_words:
            valid_words  = ['[PAD]']
            valid_bboxes = [[0, 0, 1, 1]]
            valid_labels = [0]

        try:
            encoding = self.processor(
                image, valid_words,
                boxes=valid_bboxes,
                word_labels=valid_labels,
                max_length=self.max_length,
                padding='max_length',
                truncation=True,
                return_tensors='pt',
            )
        except Exception as e:
            print(f'  [Dataset] Encoding error for sample {idx}: {e}')
            encoding = self.processor(
                Image.new('RGB', (224, 224), 'white'),
                ['[PAD]'], boxes=[[0, 0, 1, 1]], word_labels=[0],
                max_length=self.max_length, padding='max_length',
                truncation=True, return_tensors='pt',
            )

        return {k: v.squeeze(0) for k, v in encoding.items()}


# ─── Metrics ──────────────────────────────────────────────────────────────────
def compute_f1(preds_flat, labels_flat):
    from collections import defaultdict
    tp = defaultdict(int); fp = defaultdict(int); fn = defaultdict(int)
    for pred, true in zip(preds_flat, labels_flat):
        if true == -100: continue
        if pred == true: tp[true] += 1
        else: fp[pred] += 1; fn[true] += 1
    f1_scores = []
    for lbl in set(list(tp.keys()) + list(fp.keys()) + list(fn.keys())):
        if lbl == 0: continue
        prec  = tp[lbl] / (tp[lbl]+fp[lbl]+1e-8)
        rec   = tp[lbl] / (tp[lbl]+fn[lbl]+1e-8)
        f1    = 2*prec*rec / (prec+rec+1e-8)
        f1_scores.append(f1)
    return sum(f1_scores) / len(f1_scores) if f1_scores else 0.0


# ─── Training ──────────────────────────────────────────────────────────────────
def train():
    torch.manual_seed(SEED)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'[Train] Device: {device}')
    if device.type == 'cuda':
        print(f'[Train] GPU: {torch.cuda.get_device_name(0)}  '
              f'VRAM: {torch.cuda.get_device_properties(0).total_memory//1024//1024} MB')

    train_path = os.path.join(HF_DIR, 'train.json')
    val_path   = os.path.join(HF_DIR, 'val.json')
    if not os.path.exists(train_path):
        print(f'\n[ERROR] Dataset not found: {train_path}')
        print('Run first:  python prepare_dataset.py')
        sys.exit(1)

    with open(train_path) as f: n_train = len(json.load(f))
    with open(val_path)   as f: n_val   = len(json.load(f))
    print(f'[Train] Dataset: {n_train} train / {n_val} val samples')

    print(f'[Train] Loading base model: {BASE_MODEL}')
    processor = LayoutLMv3Processor.from_pretrained(BASE_MODEL, apply_ocr=False)

    # ── IMPORTANT: Keep model in FP32 for training ─────────────────────────
    # GradScaler + autocast = FP32 model with FP16 forward pass (AMP)
    # DO NOT call model.half() here — that causes "unscale FP16 gradients" error
    model = LayoutLMv3ForTokenClassification.from_pretrained(
        BASE_MODEL, num_labels=NUM_LABELS,
        id2label=ID2LABEL, label2id=LABEL2ID,
        ignore_mismatched_sizes=True,
    ).to(device)
    # model stays FP32 — do NOT call model.half()

    train_ds = GSTDataset(train_path, processor, MAX_LENGTH)
    val_ds   = GSTDataset(val_path,   processor, MAX_LENGTH)
    train_dl = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True, num_workers=0)
    val_dl   = DataLoader(val_ds,   batch_size=BATCH_SIZE, shuffle=False, num_workers=0)

    optimizer = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=0.01)
    total_steps  = len(train_dl) * EPOCHS
    warmup_steps = int(total_steps * WARMUP_RATIO)
    scheduler    = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=warmup_steps, num_training_steps=total_steps)

    # AMP scaler — works with FP32 model + autocast (correct pattern)
    use_amp = (device.type == 'cuda')
    scaler  = torch.amp.GradScaler('cuda') if use_amp else None

    best_f1   = 0.0
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f'\n[Train] Starting  ({EPOCHS} epochs  batch={BATCH_SIZE}  lr={LR})\n')
    print(f'  {"Epoch":>5}  {"Train Loss":>12}  {"Val Loss":>10}  {"Val F1":>8}  {"LR":>10}')
    print('  ' + '-'*52)

    for epoch in range(1, EPOCHS+1):
        # ── Train ─────────────────────────────────────────────────────────
        model.train()
        train_loss = 0.0
        t0 = time.time()
        for batch in train_dl:
            batch = {k: v.to(device) for k, v in batch.items()}
            optimizer.zero_grad()
            if use_amp:
                # Correct AMP pattern: FP32 model + autocast for forward pass
                with torch.amp.autocast('cuda'):
                    outputs = model(**batch)
                    loss    = outputs.loss
                scaler.scale(loss).backward()
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                scaler.step(optimizer)
                scaler.update()
            else:
                outputs = model(**batch)
                loss    = outputs.loss
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
            scheduler.step()
            train_loss += loss.item()
        train_loss /= len(train_dl)

        # ── Validate ───────────────────────────────────────────────────────
        model.eval()
        val_loss   = 0.0
        all_preds  = []
        all_labels = []
        with torch.no_grad():
            for batch in val_dl:
                batch = {k: v.to(device) for k, v in batch.items()}
                if use_amp:
                    with torch.amp.autocast('cuda'):
                        outputs = model(**batch)
                else:
                    outputs = model(**batch)
                val_loss += outputs.loss.item()
                preds  = outputs.logits.argmax(-1).cpu().numpy()
                labels = batch['labels'].cpu().numpy()
                for p_seq, l_seq in zip(preds, labels):
                    all_preds.extend(p_seq.tolist())
                    all_labels.extend(l_seq.tolist())

        val_loss /= max(len(val_dl), 1)
        f1 = compute_f1(all_preds, all_labels)
        lr = scheduler.get_last_lr()[0]
        elapsed = time.time() - t0

        is_best = f1 > best_f1
        marker  = '  ✅ BEST' if is_best else ''
        print(f'  {epoch:>5}  {train_loss:>12.4f}  {val_loss:>10.4f}  '
              f'{f1:>8.4f}  {lr:>10.2e}  ({elapsed:.0f}s){marker}')

        if is_best:
            best_f1 = f1
            model.save_pretrained(OUTPUT_DIR)
            processor.save_pretrained(OUTPUT_DIR)

        if device.type == 'cuda':
            torch.cuda.empty_cache()

    print(f'\n[Train] ✅ Done!  Best Val F1: {best_f1:.4f}')
    print(f'[Train] Model saved → {OUTPUT_DIR}')
    print('\nRestart the Django server to use the fine-tuned model:')
    print('  python manage.py runserver')


def test_model():
    if not os.path.exists(os.path.join(OUTPUT_DIR, 'config.json')):
        print('[Test] No saved model found.')
        return
    print('\n[Test] Loading saved model...')
    processor = LayoutLMv3Processor.from_pretrained(OUTPUT_DIR, apply_ocr=True)
    model = LayoutLMv3ForTokenClassification.from_pretrained(OUTPUT_DIR)
    model.eval()
    print(f'[Test] ✅ Model loaded from {OUTPUT_DIR}')
    print(f'[Test] Labels: {model.config.num_labels}  Classes: {list(model.config.id2label.values())[:5]}...')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()
    if args.test: test_model()
    else: train(); test_model()
