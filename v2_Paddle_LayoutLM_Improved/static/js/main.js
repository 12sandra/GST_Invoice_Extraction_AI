// File upload drag and drop
document.addEventListener('DOMContentLoaded', function() {
  const dropZone = document.getElementById('dropZone');
  const fileInput = document.getElementById('fileUpload');
  const fileSelected = document.getElementById('fileSelected');
  const selectedFileName = document.getElementById('selectedFileName');
  const uploadForm = document.getElementById('uploadForm');
  const submitBtn = document.getElementById('submitBtn');
  const processingOverlay = document.getElementById('processingOverlay');

  if (dropZone && fileInput) {
    ['dragenter', 'dragover'].forEach(e => {
      dropZone.addEventListener(e, () => dropZone.classList.add('dragover'), false);
    });
    ['dragleave', 'drop'].forEach(e => {
      dropZone.addEventListener(e, () => dropZone.classList.remove('dragover'), false);
    });
    dropZone.addEventListener('drop', function(e) {
      e.preventDefault();
      const files = e.dataTransfer.files;
      if (files.length > 0) {
        fileInput.files = files;
        showFileSelected(files[0].name);
      }
    });
    fileInput.addEventListener('change', function() {
      if (this.files.length > 0) showFileSelected(this.files[0].name);
    });
  }

  function showFileSelected(name) {
    if (fileSelected && selectedFileName) {
      selectedFileName.textContent = name;
      fileSelected.style.display = 'flex';
    }
  }

  if (uploadForm) {
    uploadForm.addEventListener('submit', function() {
      if (processingOverlay) processingOverlay.style.display = 'block';
      if (submitBtn) { submitBtn.disabled = true; submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Processing...'; }
    });
  }
});
