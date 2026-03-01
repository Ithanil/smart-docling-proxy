# Smart Docling Proxy

A transparent, lightweight FastAPI proxy for [docling-serve](https://github.com/DS4SD/docling-serve) that skips expensive OCR processing for documents that already contain native text.

## Motivation
Docling's default conversion still does OCR for certain elements, even if most of the document is embedded text (as long as `do_ocr = True`).
On CPU-only deployments, running OCR models (especially EasyOCR) is highly resource-intensive and drastically slows down document conversion or even leads to OOM.
If the vast majority of your documents does not need OCR, but you still need it as fallback for a few documents (without manual intervention), this proxy might be for you.

## What it does
This proxy sits right in front of the `docling-serve` container and intercepts requests to the `/v1/convert/source` and `/v1/convert/file` endpoints [1]. It implements a "Two-Pass" evaluation:

1. **Pass 1 (Fast Extraction):** Forwards the request to Docling with OCR disabled (`do_ocr: False`).
2. **Evaluation:** Checks the length of the extracted Markdown/Text. If it's greater than the configured `MIN_TEXT_LENGTH` (excluding placeholders like `<!-- image -->`), the proxy immediately returns the result.
3. **Pass 2 (Fallback to OCR):** If the text is too short (indicating an image-based or scanned PDF), it automatically retries the identical request with OCR enabled.

If a user explicitly requests `force_ocr: True`, the proxy skips Pass 1 and goes directly to full OCR routing.

## Configuration

The proxy is configured via environment variables:

| Variable | Default | Description |
|---|---|---|
| `DOCLING_URL` | `http://docling:5001` | The internal Docker network URL of the `docling-serve` container. |
| `MIN_TEXT_LENGTH` | `50` | The minimum number of characters required to skip the OCR fallback. |
| `DOCLING_TIMEOUT` | `180` | Client timeout in seconds. Ensure this matches Docling's `DOCLING_SERVE_MAX_SYNC_WAIT`. |
| `LOG_LEVEL` | `INFO` | Standard logging levels (`INFO`, `DEBUG`, `WARNING`, etc.). |

## Quick Start

1. Clone the repository and place the code alongside `docker-compose.yml`.
2. Start the services:
   ```bash
   docker-compose up -d
   ```
3. Point your client applications to the proxy's endpoint (by default, `http://docling-proxy:5001`).

## Minimal Usage Example

When calling the proxy, we strongly advise adjusting two native parameters in the payload:
* Set `document_timeout: 180` (or matching your proxy config) to sync component timeouts [1].
* For CPU-only deployments, set `ocr_engine` to `rapidocr` or `tesseract` for much faster inference than the default engine [1].

### Using the JSON `/v1/convert/source` endpoint:

```bash
curl -X 'POST' \
  'http://localhost:5001/v1/convert/source' \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
  "options": {
    "do_ocr": true,
    "force_ocr": false,
    "ocr_engine": "rapidocr",
    "document_timeout": 180 
  },
  "http_sources": [{"url": "https://arxiv.org/pdf/2206.01062"}]
}'
```

### Using the Multipart `/v1/convert/file` endpoint:

```bash
curl -X 'POST' \
  'http://localhost:5001/v1/convert/file' \
  -H 'accept: application/json' \
  -F 'do_ocr=true' \
  -F 'ocr_engine=rapidocr' \
  -F 'document_timeout=180' \
  -F 'files=@your_document.pdf;type=application/pdf'
```
