\# GST Invoice Extraction AI: Evolution from Tesseract to LayoutLMv3



This repository contains two versions of an AI-powered system designed to extract structured data (GSTIN, Dates, Amounts, Line Items) from PDF and Image invoices and convert them into organized Excel datasets.



\## 📌 Project Overview

The goal of this project is to automate the manual entry of GST data. We transitioned from a traditional OCR approach to a state-of-the-art Multimodal Transformer model to improve accuracy in complex layouts.



\---



\## 🏗 Repository Structure



\### \[v1\_Tesseract\_Baseline]

This version represents the initial prototype.

\* \*\*Technology:\*\* Tesseract OCR, OpenCV, Django.

\* \*\*Method:\*\* Uses rule-based coordinate extraction and Regex to find GST patterns.

\* \*\*Limitation:\*\* Struggled with multi-page invoices and varied table structures.



\### \[v2\_Paddle\_LayoutLM\_Improved]

The current "Improved Model" that solves layout-based extraction challenges.

\* \*\*Technology:\*\* PaddleOCR, LayoutLMv3 (HuggingFace), Django.

\* \*\*Method:\*\* \* \*\*PaddleOCR:\*\* Used for high-accuracy text detection (better than Tesseract for tilted/low-quality scans).

&#x20;   \* \*\*LayoutLMv3:\*\* A multimodal model that processes both \*\*Text\*\* and \*\*Image Position\*\* (spatial layout) to understand where a "Total" or "GST Number" is located regardless of the invoice design.

\* \*\*Status:\*\* This version is optimized for high-performance environments (GPU-required for training).



\---



\## 🚀 Technical Workflow



1\. \*\*Preprocessing:\*\* PDF to Image conversion using `pdf2image`.

2\. \*\*OCR Layer:\*\* Text detection and recognition via PaddleOCR.

3\. \*\*Feature Extraction:\*\* LayoutLMv3 processes the spatial tokens.

4\. \*\*Post-Processing:\*\* Data cleaning and validation of GST formats.

5\. \*\*Export:\*\* Generation of structured `.xlsx` files via `Openpyxl`.







\---



\## 🛠 Setup \& Installation



\### Local Development (CPU/Testing)

1\. Clone the repo:

&#x20;  ```bash

&#x20;  git clone \[https://github.com/12sandra/GST\_Invoice\_Extraction\_AI.git](https://github.com/12sandra/GST\_Invoice\_Extraction\_AI.git)



2\. Navigate to the desired version:





cd v2\_Paddle\_LayoutLM\_Improved



3\. Install dependencies:



Bash

pip install -r requirements.txt

