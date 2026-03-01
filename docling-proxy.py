import os
import json
import uuid
import re
import logging
import httpx
import anyio
import requests
from fastapi import FastAPI, Request, Response
app = FastAPI()
DOCLING_URL = os.getenv("DOCLING_URL", "http://docling:5001")
MIN_TEXT_LENGTH = int(os.getenv("MIN_TEXT_LENGTH", "50"))
DOCLING_TIMEOUT = int(os.getenv("DOCLING_TIMEOUT", "180"))
# ---- Logging ---------------------------------------------------------------
# DEBUG should behave like INFO -> map DEBUG to INFO.
_raw_level = os.getenv("LOG_LEVEL", "INFO").upper()
if _raw_level == "DEBUG":
    _raw_level = "INFO"
LOG_LEVEL = getattr(logging, _raw_level, logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("docling_proxy")
def _request_id(request: Request) -> str:
    # If the client provides one, reuse it; else generate.
    return request.headers.get("x-request-id") or uuid.uuid4().hex
# ---- Proxy helpers ---------------------------------------------------------
SAFE_FORWARD_HEADERS = {}  # we don't need to forward any headers
def get_safe_headers(original_headers) -> dict:
    return {k: v for k, v in original_headers.items() if k.lower() in SAFE_FORWARD_HEADERS}
# Pre-compile the regexes for performance
OCR_TAG_PATTERN = re.compile(r'<!--\s*[a-zA-Z0-9_-]+\s*-->', re.IGNORECASE)
WHITESPACE_PATTERN = re.compile(r'\s+')
def extract_text_length(response_body: bytes) -> int:
    try:
        data = json.loads(response_body)
        doc = data.get("document", {})
        text_content = doc.get("text_content") or ""
        md_content = doc.get("md_content") or ""
        def get_clean_length(raw_text: str) -> int:
            if not raw_text:
                return 0
            # 1. Remove the OCR placeholders
            cleaned = OCR_TAG_PATTERN.sub("", raw_text)
            # 2. Condense leftover consecutive spaces into a single space and strip edges.
            cleaned = WHITESPACE_PATTERN.sub(" ", cleaned).strip()
            return len(cleaned)
        return max(get_clean_length(text_content), get_clean_length(md_content))
    except Exception:
        # Fallback to inf if response isn't JSON (e.g., zip output) to trigger pass 2
        return float("inf")
def _post_multipart_requests(url: str, data, files, headers: dict, timeout: int):
    r = requests.post(url, data=data, files=files, headers=headers, timeout=timeout)
    return r.status_code, r.content, dict(r.headers)
# ---- Routes ----------------------------------------------------------------
@app.post("/v1/convert/source")
async def proxy_source(request: Request):
    rid = _request_id(request)
    path = "/v1/convert/source"
    headers = get_safe_headers(request.headers)
    # Try JSON parse first
    try:
        body = await request.json()
        is_json = True
    except Exception:
        is_json = False
        raw_body = await request.body()
    async with httpx.AsyncClient(timeout=DOCLING_TIMEOUT) as client:
        url = f"{DOCLING_URL}{path}"
        # If not JSON, raw-forward unchanged (no 2-pass editing possible safely)
        if not is_json:
            logger.info("rid=%s route=%s decision=raw_forward_non_json", rid, path)
            resp = await client.post(url, content=raw_body, headers=headers)
            logger.info("rid=%s route=%s upstream_status=%s", rid, path, resp.status_code)
            return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
        options = body.get("options", {}) or {}
        force_ocr = bool(options.get("force_ocr", False))
        # Pass 1: No OCR
        if not force_ocr:
            logger.info("rid=%s route=%s decision=pass1_no_ocr", rid, path)
            pass1_body = dict(body)
            pass1_options = dict(options)
            pass1_options["do_ocr"] = False
            pass1_body["options"] = pass1_options
            try:
                resp1 = await client.post(url, json=pass1_body, headers=headers)
            except httpx.TimeoutException:
                logger.error("rid=%s route=%s event=timeout phase=pass1", rid, path)
                raise
            except Exception:
                logger.exception("rid=%s route=%s event=exception phase=pass1", rid, path)
                raise
            if resp1.status_code == 200:
                text_len = extract_text_length(resp1.content)
                if text_len >= MIN_TEXT_LENGTH:
                    logger.info("rid=%s route=%s decision=return_pass1 status=200 text_len=%s", rid, path, text_len)
                    return Response(content=resp1.content, status_code=resp1.status_code, headers=dict(resp1.headers))
                else:
                    logger.info("rid=%s route=%s decision=retry_pass2 reason=text_too_short text_len=%s", rid, path, text_len)
            elif resp1.status_code >= 400:
                # Keep control-flow visible; detailed content not logged.
                logger.warning("rid=%s route=%s decision=return_pass1_error upstream_status=%s", rid, path, resp1.status_code)
                return Response(content=resp1.content, status_code=resp1.status_code, headers=dict(resp1.headers))
            else:
                logger.info("rid=%s route=%s decision=retry_pass2 upstream_status=%s", rid, path, resp1.status_code)
        else:
            logger.info("rid=%s route=%s decision=skip_pass1_force_ocr", rid, path)
        # Pass 2: Retry with OCR (or original body)
        logger.info("rid=%s route=%s decision=pass2_with_ocr", rid, path)
        try:
            resp2 = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException:
            logger.error("rid=%s route=%s event=timeout phase=pass2", rid, path)
            raise
        except Exception:
            logger.exception("rid=%s route=%s event=exception phase=pass2", rid, path)
            raise
        logger.info("rid=%s route=%s decision=return_pass2 upstream_status=%s", rid, path, resp2.status_code)
        return Response(content=resp2.content, status_code=resp2.status_code, headers=dict(resp2.headers))
@app.post("/v1/convert/file")
async def proxy_file(request: Request):
    rid = _request_id(request)
    path = "/v1/convert/file"
    form = await request.form()
    data_tuples = []
    files_tuples = []
    force_ocr = False
    file_count = 0
    for key, value in form.multi_items():
        if isinstance(value, str):
            data_tuples.append((key, value))
            if key == "force_ocr" and value.lower() == "true":
                force_ocr = True
        else:
            # Starlette UploadFile
            content = await value.read()
            file_count += 1
            files_tuples.append(
                (key, (value.filename or "upload", content, value.content_type or "application/octet-stream"))
            )
    headers = get_safe_headers(request.headers)
    url = f"{DOCLING_URL}{path}"
    logger.info("rid=%s route=%s received form_fields=%s files=%s force_ocr=%s",
                rid, path, len(data_tuples), file_count, force_ocr)
    # Pass 1: No OCR
    if not force_ocr:
        logger.info("rid=%s route=%s decision=pass1_no_ocr", rid, path)
        pass1_data = [(k, v) for (k, v) in data_tuples if k != "do_ocr"]
        pass1_data.append(("do_ocr", "false"))
        try:
            status1, content1, hdrs1 = await anyio.to_thread.run_sync(
                _post_multipart_requests, url, pass1_data, files_tuples, headers, DOCLING_TIMEOUT
            )
        except requests.Timeout:
            logger.error("rid=%s route=%s event=timeout phase=pass1", rid, path)
            raise
        except Exception:
            logger.exception("rid=%s route=%s event=exception phase=pass1", rid, path)
            raise
        if status1 == 200:
            text_len = extract_text_length(content1)
            if text_len >= MIN_TEXT_LENGTH:
                logger.info("rid=%s route=%s decision=return_pass1 status=200 text_len=%s", rid, path, text_len)
                return Response(content=content1, status_code=status1, headers=hdrs1)
            else:
                logger.info("rid=%s route=%s decision=retry_pass2 reason=text_too_short text_len=%s", rid, path, text_len)
        elif status1 >= 400:
            logger.warning("rid=%s route=%s decision=return_pass1_error upstream_status=%s", rid, path, status1)
            return Response(content=content1, status_code=status1, headers=hdrs1)
        else:
            logger.info("rid=%s route=%s decision=retry_pass2 upstream_status=%s", rid, path, status1)
    else:
        logger.info("rid=%s route=%s decision=skip_pass1_force_ocr", rid, path)
    # Pass 2: Retry with OCR
    logger.info("rid=%s route=%s decision=pass2_with_ocr", rid, path)
    try:
        status2, content2, hdrs2 = await anyio.to_thread.run_sync(
            _post_multipart_requests, url, data_tuples, files_tuples, headers, DOCLING_TIMEOUT
        )
    except requests.Timeout:
        logger.error("rid=%s route=%s event=timeout phase=pass2", rid, path)
        raise
    except Exception:
        logger.exception("rid=%s route=%s event=exception phase=pass2", rid, path)
        raise
    logger.info("rid=%s route=%s decision=return_pass2 upstream_status=%s", rid, path, status2)
    return Response(content=content2, status_code=status2, headers=hdrs2)
