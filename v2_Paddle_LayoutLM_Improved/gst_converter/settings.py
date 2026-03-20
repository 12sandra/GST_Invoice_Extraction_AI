"""
gst_converter/settings.py  —  Complete configuration  v7
Replace: gst_project/gst_converter/settings.py
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = 'django-insecure-gst-converter-secret-key-change-in-production-2024'

DEBUG = True

ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'accounts',
    'converter',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'gst_converter.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'gst_converter.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/converter/dashboard/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

# ─── Poppler (PDF → images) ──────────────────────────────────────────────────
# Windows: download from https://github.com/oschwartz10612/poppler-windows
# Extract to C:\poppler  → set path to C:\poppler\Library\bin
# Linux: set to None  (already in PATH via: sudo apt install poppler-utils)
POPPLER_PATH = r'C:\poppler\Library\bin'   # Windows — change if extracted elsewhere
# POPPLER_PATH = None                       # Linux/Mac

# ─── Tesseract (fallback OCR — optional) ─────────────────────────────────────
# PaddleOCR is the PRIMARY OCR engine. Tesseract is only used as fallback.
# Windows: download from https://github.com/UB-Mannheim/tesseract/wiki
# Linux: sudo apt install tesseract-ocr
import shutil as _shutil
def _find_tesseract():
    # 1. Check common Windows install paths
    for p in [
        r'C:\Program Files\Tesseract-OCR\tesseract.exe',
        r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
        r'C:\tesseract\tesseract.exe',
    ]:
        if os.path.isfile(p): return p
    # 2. Check PATH
    found = _shutil.which('tesseract')
    if found: return found
    # 3. Return default (may not exist — that's OK, PaddleOCR is primary)
    return r'C:\Program Files\Tesseract-OCR\tesseract.exe'

TESSERACT_CMD = _find_tesseract()

# ─── OCR ENGINE: PaddleOCR ───────────────────────────────────────────────────
# IMPORTANT: Install PaddleOCR 2.7.3 (NOT 3.x — 3.x has GPU init bug):
#   pip uninstall paddleocr paddlex -y
#   pip install paddleocr==2.7.3
#
# GPU version of PaddlePaddle:
#   pip install paddlepaddle-gpu==2.6.1.post120 \
#       -f https://www.paddlepaddle.org.cn/whl/windows/mkl/avx/stable.html
#
# CPU fallback:
#   pip install paddlepaddle
USE_PADDLE_OCR   = True
PADDLE_OCR_LANG  = 'en'
PADDLE_USE_ANGLE = True    # Auto-correct rotated/skewed documents
PADDLE_USE_GPU   = True    # Set False if GPU init fails or you have no CUDA

# ─── LayoutLMv3 model ────────────────────────────────────────────────────────
# After fine-tuning, point to your local checkpoint folder.
# Before fine-tuning, uses the base pretrained model from HuggingFace.
_local_model = BASE_DIR / 'models' / 'layoutlmv3_gst_finetuned'
# Only use local model if it actually contains model files (not just an empty folder)
_model_has_files = (
    _local_model.exists() and
    (_local_model / 'config.json').exists() and
    (_local_model / 'preprocessor_config.json').exists()
)
LAYOUTLM_MODEL      = str(_local_model) if _model_has_files else 'microsoft/layoutlmv3-base'
LAYOUTLM_BASE_MODEL = 'microsoft/layoutlmv3-base'
LAYOUTLM_FINETUNED_PATH = _local_model

# Auto-training config
AUTO_TRAIN_MIN_INVOICES = 5   # need 5+ real invoices to trigger training
AUTO_TRAIN_MIN_FIELDS   = 4   # skip invoices with fewer than 4 fields (likely fake/blank)

# ─── GPU Configuration ───────────────────────────────────────────────────────
try:
    import torch as _torch
    USE_GPU  = _torch.cuda.is_available()
    DEVICE   = 'cuda' if USE_GPU else 'cpu'
except ImportError:
    USE_GPU = False
    DEVICE  = 'cpu'

GPU_DEVICE_ID        = 0
USE_FP16             = USE_GPU       # Half-precision on GPU (saves VRAM)
GPU_BATCH_SIZE       = 4 if USE_GPU else 1
CACHE_MODEL_IN_MEMORY = True         # Keep model in GPU RAM between requests

# ─── Fine-tuning dataset ─────────────────────────────────────────────────────
DATASET_DIR = BASE_DIR / 'dataset'

# ─── Upload limits ────────────────────────────────────────────────────────────
DATA_UPLOAD_MAX_MEMORY_SIZE = 20971520   # 20 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 20971520
