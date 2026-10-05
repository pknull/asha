#!/usr/bin/env python3
"""fal.ai helper for the image-fal skill: direct REST, Python stdlib only.

Free reads:   search, schema, price, spend
Paid action:  run (submits one queue job after a price check against an
              approved ceiling), result (fetches a logged job's output)

Rules this helper enforces:
- FAL_KEY comes from the environment only and is sent only to fal's own API
  hosts (KEY_HOSTS), including status and result URLs fal returns. Requests
  that carry the key never follow redirects.
- Only files named with --file are uploaded. Input values are sent verbatim,
  even when one happens to be an existing local path.
- A paid submission is never retried, and every submission is appended to the
  generation log before the helper polls for its result.
"""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


QUEUE_URL = "https://queue.fal.run"
PLATFORM_URL = "https://api.fal.ai/v1"
REST_URL = "https://rest.fal.ai"
# Public, keyless queue OpenAPI per endpoint. The platform API's
# expand=openapi-3.0 answered expansion_failed for every model on 2026-10-05.
OPENAPI_URL = "https://fal.ai/api/openapi/queue/openapi.json"
# The only hosts that ever receive FAL_KEY: the queue (submit, status,
# result), the platform API (models, pricing) and the storage REST API
# (upload initiation). CDN and presigned storage URLs never get the key.
KEY_HOSTS = frozenset({"queue.fal.run", "api.fal.ai", "rest.fal.ai"})

DEFAULT_LOG = Path("Work/fal/generations.jsonl")
DEFAULT_OUT = Path("Work/fal/outputs")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
API_TIMEOUT = 60
TRANSFER_TIMEOUT = 600
GET_ATTEMPTS = 4
USER_AGENT = "asha-image-fal/1"

ENDPOINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)+$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")
CONTENT_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "video/mp4": ".mp4",
    "model/gltf-binary": ".glb",
}

_sleep = time.sleep


class FalError(RuntimeError):
    """An expected failure, reported without a traceback."""


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _HttpsRedirectOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


NO_REDIRECT_OPENER = urllib.request.build_opener(_RefuseRedirect)
DOWNLOAD_OPENER = urllib.request.build_opener(_HttpsRedirectOnly)


# --------------------------------------------------------------------- key


def key_host_allowed(url: Any) -> bool:
    """True only for an https URL on one of fal's API hosts."""
    if not isinstance(url, str) or not url or "\\" in url:
        return False
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or "@" in parts.netloc:
        return False
    if port not in (None, 443):
        return False
    return (parts.hostname or "").lower() in KEY_HOSTS


def fal_key() -> str:
    key = os.environ.get("FAL_KEY", "").strip()
    if not key:
        raise FalError(
            "FAL_KEY is not set. Add it to ~/.asha/secrets.env (see "
            "secrets.example in the asha repo) and relaunch through asha."
        )
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
        raise FalError("FAL_KEY contains characters that cannot be sent in a header")
    return key


def redact(text: str) -> str:
    key = os.environ.get("FAL_KEY", "").strip()
    if key:
        text = text.replace(key, "[redacted]")
    return text


def _where(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
        return f"{parts.scheme}://{parts.hostname or '?'}{parts.path}"
    except ValueError:
        return "<unparseable url>"


# --------------------------------------------------------------- transport


def _send(request, *, timeout, limit, opener=None) -> bytes:
    """Perform one request; return the body, refusing more than limit bytes."""
    with (opener or NO_REDIRECT_OPENER).open(request, timeout=timeout) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise FalError(f"response from {_where(request.full_url)} exceeds {limit} bytes")
    return body


def _stream_to(request, handle, *, timeout, limit) -> int:
    """Copy a download into handle; refuse more than limit bytes."""
    total = 0
    with DOWNLOAD_OPENER.open(request, timeout=timeout) as response:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                return total
            total += len(chunk)
            if total > limit:
                raise FalError(f"download from {_where(request.full_url)} exceeds {limit} bytes")
            handle.write(chunk)


def api_request(method: str, url: str, *, body: Any = None, auth: bool = True,
                retry: bool | None = None, timeout: int = API_TIMEOUT) -> Any:
    """JSON request to a fal API host. Only GET is retried unless told otherwise."""
    if auth and not key_host_allowed(url):
        raise FalError(f"refusing to send FAL_KEY to {_where(url)}: not a fal API host")
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    if auth:
        # Unredirected: never forwarded, and redirects are refused anyway.
        request.add_unredirected_header("Authorization", "Key " + fal_key())
    attempts = GET_ATTEMPTS if (method == "GET" if retry is None else retry) else 1
    for attempt in range(1, attempts + 1):
        try:
            raw = _send(request, timeout=timeout, limit=MAX_RESPONSE_BYTES,
                        opener=NO_REDIRECT_OPENER)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < attempts:
                _sleep(2 * attempt)
                continue
            detail = ""
            try:
                detail = exc.read(2000).decode("utf-8", "replace")
            except Exception:
                pass
            raise FalError(redact(f"fal {method} {_where(url)} -> HTTP {exc.code}: {detail}")) from None
        except urllib.error.URLError as exc:
            if attempt < attempts:
                _sleep(2 * attempt)
                continue
            raise FalError(redact(f"fal {method} {_where(url)}: {exc.reason}")) from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise FalError(f"fal {method} {_where(url)} returned non-JSON") from None
    raise FalError(f"fal {method} {_where(url)}: no response")


def _require_https(value: Any, what: str) -> str:
    if not isinstance(value, str) or urllib.parse.urlsplit(value).scheme != "https":
        raise FalError(f"fal returned a {what} that is not an https URL")
    return value


# -------------------------------------------------------------- arguments


def check_endpoint(endpoint: str) -> str:
    if not ENDPOINT_RE.match(endpoint or ""):
        raise FalError(f"not a fal endpoint id: {endpoint!r} (expected owner/model[/path])")
    return endpoint


def parse_file_args(values: list[str] | None) -> list[tuple[str, Path]]:
    pairs = []
    for value in values or []:
        field, sep, path = value.partition("=")
        if not sep or not FIELD_RE.match(field) or not path:
            raise FalError(f"--file takes FIELD=PATH, got {value!r}")
        source = Path(path).expanduser()
        if not source.is_file():
            raise FalError(f"--file {field}: no such file: {source}")
        size = source.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise FalError(f"--file {field}: {source} is {size} bytes; the limit is {MAX_UPLOAD_BYTES}")
        pairs.append((field, source))
    return pairs


def load_input(text: str | None, path: str | None) -> dict:
    if text is not None and path is not None:
        raise FalError("give --input or --input-file, not both")
    raw = text if text is not None else (Path(path).read_text() if path else "{}")
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise FalError(f"input is not JSON: {exc}") from None
    if not isinstance(value, dict):
        raise FalError("input must be a JSON object")
    return value


def plan_files(inputs: dict, files: list[tuple[str, Path]]) -> None:
    """Refuse ambiguous file fields before anything is uploaded."""
    seen: set[str] = set()
    for field, _path in files:
        if field in inputs:
            raise FalError(f"--file {field}: the input already sets {field}")
        if field in seen and not field.endswith("_urls"):
            raise FalError(f"--file {field} given twice; only *_urls fields take a list")
        seen.add(field)


def build_input(inputs: dict, files: list[tuple[str, Path]], uploader=None) -> dict:
    """Input values pass verbatim; only the named files are uploaded."""
    uploader = uploader or upload_file
    plan_files(inputs, files)
    built = dict(inputs)
    for field, path in files:
        url = uploader(path)
        if field.endswith("_urls"):
            built.setdefault(field, []).append(url)
        else:
            built[field] = url
    return built


# ---------------------------------------------------------------- uploads


def upload_file(path: Path) -> str:
    """Upload one explicitly named local file to fal storage; return its URL."""
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix.lower() == ".glb":
        content_type = "model/gltf-binary"
    init = api_request(
        "POST", f"{REST_URL}/storage/upload/initiate?storage_type=gcs",
        body={"file_name": path.name, "content_type": content_type}, retry=False,
    )
    upload_url = _require_https(init.get("upload_url"), "upload URL")
    file_url = _require_https(init.get("file_url"), "file URL")
    request = urllib.request.Request(
        upload_url, data=path.read_bytes(), method="PUT",
        headers={"Content-Type": content_type, "User-Agent": USER_AGENT},
    )
    try:
        _send(request, timeout=TRANSFER_TIMEOUT, limit=MAX_RESPONSE_BYTES,
              opener=NO_REDIRECT_OPENER)
    except urllib.error.HTTPError as exc:
        raise FalError(f"upload of {path.name} -> HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise FalError(f"upload of {path.name}: {exc.reason}") from None
    return file_url


# ---------------------------------------------------------------- catalog


def lookup_price(endpoint: str) -> dict:
    query = urllib.parse.urlencode({"endpoint_id": endpoint}, safe="/")
    reply = api_request("GET", f"{PLATFORM_URL}/models/pricing?{query}")
    for entry in reply.get("prices") or []:
        if entry.get("endpoint_id") == endpoint:
            price = entry.get("unit_price")
            # A zero would pass any approval ceiling; treat it as unpriced.
            if isinstance(price, (int, float)) and math.isfinite(price) and price > 0:
                return {
                    "unit_price": float(price),
                    "unit": entry.get("unit") or "unit",
                    "currency": entry.get("currency") or "USD",
                }
    raise FalError(f"fal lists no price for {endpoint}; refusing to estimate or submit")


def historical_estimate(endpoint: str, calls: int) -> dict:
    """fal's own estimate from the endpoint's historical average cost per call."""
    reply = api_request("POST", f"{PLATFORM_URL}/models/pricing/estimate", retry=True, body={
        "estimate_type": "historical_api_price",
        "endpoints": {endpoint: {"call_quantity": calls}},
    })
    total = reply.get("total_cost")
    # Zero means fal has no call history for the endpoint, not a free call.
    if not (isinstance(total, (int, float)) and math.isfinite(total) and total > 0):
        raise FalError(
            f"fal has no call history to estimate {endpoint}; estimate with --units "
            f"from its billing unit instead"
        )
    return {"total_cost": float(total), "currency": reply.get("currency") or "USD"}


def estimate_cost(endpoint: str, units: float | None, calls: int | None) -> dict:
    """Price an endpoint by billing units or by fal's per-call history."""
    if units is not None and calls is not None:
        raise FalError("give --units or --calls, not both")
    if calls is not None and calls < 1:
        raise FalError("--calls must be at least 1")
    if units is not None and not (math.isfinite(units) and units > 0):
        raise FalError("--units must be positive")
    price = lookup_price(endpoint)
    if calls is not None:
        history = historical_estimate(endpoint, calls)
        return {**price, "estimate_basis": "historical_api_price", "calls": calls,
                "estimated_cost": round(history["total_cost"], 6),
                "currency": history["currency"]}
    units = 1.0 if units is None else units
    return {**price, "estimate_basis": "unit_price", "units": units,
            "estimated_cost": round(price["unit_price"] * units, 6)}


def search_models(query: str, category: str | None, limit: int) -> list[dict]:
    params = {"q": query, "status": "active", "limit": str(limit)}
    if category:
        params["category"] = category
    reply = api_request("GET", f"{PLATFORM_URL}/models?{urllib.parse.urlencode(params)}")
    rows = []
    for model in reply.get("models") or []:
        meta = model.get("metadata") or {}
        rows.append({
            "endpoint_id": model.get("endpoint_id"),
            "name": meta.get("display_name"),
            "category": meta.get("category"),
            "status": meta.get("status"),
        })
    return rows


def model_schema(endpoint: str) -> dict:
    query = urllib.parse.urlencode({"endpoint_id": endpoint}, safe="/")
    api = api_request("GET", f"{OPENAPI_URL}?{query}", auth=False)
    if not isinstance(api, dict) or not isinstance(api.get("paths"), dict):
        raise FalError(f"fal returned no OpenAPI schema for {endpoint}")
    components = (api.get("components") or {}).get("schemas") or {}

    def resolve(ref):
        return components.get(str(ref).rsplit("/", 1)[-1], {}) if ref else {}

    request_schema: dict = {}
    output_schema: dict = {}
    for path, operations in (api.get("paths") or {}).items():
        post = (operations or {}).get("post") or {}
        content = ((post.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
        if content and not request_schema:
            request_schema = resolve((content.get("schema") or {}).get("$ref"))
        get = (operations or {}).get("get") or {}
        if path.endswith("/requests/{request_id}") and get:
            body = (((get.get("responses") or {}).get("200") or {}).get("content") or {})
            ref = ((body.get("application/json") or {}).get("schema") or {}).get("$ref")
            output_schema = resolve(ref) or output_schema

    def fields(schema):
        out = {}
        for name, spec in (schema.get("properties") or {}).items():
            row = {k: spec[k] for k in ("type", "default", "enum") if k in spec}
            if "anyOf" in spec:
                row["type"] = " | ".join(
                    a.get("type") or str(a.get("$ref", "")).rsplit("/", 1)[-1]
                    for a in spec["anyOf"]
                )
            if "$ref" in spec:
                row["type"] = str(spec["$ref"]).rsplit("/", 1)[-1]
            if "description" in spec:
                row["description"] = str(spec["description"])[:200]
            out[name] = row
        return out

    return {
        "endpoint_id": endpoint,
        "required": request_schema.get("required", []),
        "input": fields(request_schema),
        "output": fields(output_schema),
    }


# --------------------------------------------------------------------- log


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loggable(value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith("data:"):
            return f"data:...[{len(value)} chars]"
        return value if len(value) <= 500 else value[:500] + "...[truncated]"
    if isinstance(value, list):
        return [_loggable(v) for v in value]
    if isinstance(value, dict):
        return {k: _loggable(v) for k, v in value.items()}
    return value


def append_log(log: Path, record: dict) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    line = redact(json.dumps({"time": _now(), **record}, sort_keys=True))
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_log(log: Path) -> list[dict]:
    if not log.is_file():
        return []
    records = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


# ---------------------------------------------------------------- outputs


def _output_urls(value: Any):
    if isinstance(value, dict):
        url = value.get("url")
        if isinstance(url, str):
            yield url, str(value.get("content_type") or "")
        for key, child in value.items():
            if key != "url":
                yield from _output_urls(child)
    elif isinstance(value, list):
        for child in value:
            yield from _output_urls(child)


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(name).name).strip("_")
    return stem[:64] or "fal"


def download_outputs(result: dict, out: Path, name: str) -> tuple[list[str], list[str]]:
    """Download every https output file into out; never overwrite."""
    out.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(name)
    files: list[str] = []
    skipped: list[str] = []
    for url, content_type in _output_urls(result):
        if urllib.parse.urlsplit(url).scheme != "https":
            skipped.append(url[:80])
            continue
        suffix = Path(urllib.parse.urlsplit(url).path).suffix
        if not EXT_RE.match(suffix):
            suffix = CONTENT_EXT.get(content_type.split(";")[0].strip(), "")
        index = len(files) + 1
        while True:
            label = stem if index == 1 else f"{stem}_{index}"
            target = out / f"{label}{suffix.lower()}"
            try:
                handle = open(target, "xb")
                break
            except FileExistsError:
                index += 1
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with handle:
            _stream_to(request, handle, timeout=TRANSFER_TIMEOUT, limit=MAX_DOWNLOAD_BYTES)
        files.append(str(target))
    return files, skipped


# ------------------------------------------------------------------- queue


def poll(status_url: str, request_id: str, timeout: float) -> None:
    started = time.monotonic()
    while True:
        status = api_request("GET", status_url)
        state = status.get("status")
        if state == "COMPLETED":
            if status.get("error"):
                raise FalError(f"request {request_id} failed: {status.get('error')}")
            return
        if state not in ("IN_QUEUE", "IN_PROGRESS"):
            raise FalError(f"request {request_id} reported status {state!r}")
        if time.monotonic() - started > timeout:
            raise FalError(
                f"request {request_id} still {state} after {timeout:.0f}s; it is logged, "
                f"fetch it later with: result {request_id}"
            )
        _sleep(1.0 if time.monotonic() - started < 30 else 3.0)


def collect(endpoint: str, request_id: str, response_url: str, out: Path, name: str,
            log: Path) -> dict:
    if not key_host_allowed(response_url):
        raise FalError(
            f"refusing to send FAL_KEY to {_where(response_url)}: not a fal API host "
            f"(request {request_id} is logged)"
        )
    result = api_request("GET", response_url)
    files, skipped = download_outputs(result, out, name)
    append_log(log, {
        "event": "completed",
        "endpoint": endpoint,
        "request_id": request_id,
        "files": files,
        "skipped_outputs": skipped,
        "seed": result.get("seed") if isinstance(result, dict) else None,
    })
    return {"endpoint": endpoint, "request_id": request_id, "files": files,
            "skipped_outputs": skipped}


# ---------------------------------------------------------------- commands


def cmd_run(args) -> dict:
    endpoint = check_endpoint(args.endpoint)
    inputs = load_input(args.input, args.input_file)
    files = parse_file_args(args.file)
    plan_files(inputs, files)
    if not (math.isfinite(args.approved_cost) and args.approved_cost > 0):
        raise FalError("--approved-cost must be the positive amount the user approved")
    if args.units is not None and args.calls is not None:
        raise FalError("give --units or --calls, not both")
    price = estimate_cost(endpoint, args.units, args.calls)
    estimate = price["estimated_cost"]
    if estimate > args.approved_cost + 1e-9:
        basis = (f"fal's historical average for {args.calls} call(s)"
                 if args.calls is not None else
                 f"{price['units']:g} x {price['unit_price']:g} per {price['unit']}")
        raise FalError(
            f"estimated {estimate:g} {price['currency']} ({basis}) exceeds the approved "
            f"{args.approved_cost:g}; state the new estimate and ask again"
        )
    built = build_input(inputs, files)
    job = api_request("POST", f"{QUEUE_URL}/{endpoint}", body=built, retry=False)
    request_id = str(job.get("request_id") or "")
    if not REQUEST_ID_RE.match(request_id):
        raise FalError("fal accepted the job but returned no usable request id")
    status_url = job.get("status_url") or f"{QUEUE_URL}/{endpoint}/requests/{request_id}/status"
    response_url = job.get("response_url") or f"{QUEUE_URL}/{endpoint}/requests/{request_id}"
    append_log(args.log, {
        "event": "submitted",
        "endpoint": endpoint,
        "request_id": request_id,
        "unit_price": price["unit_price"],
        "unit": price["unit"],
        "currency": price["currency"],
        "estimate_basis": price["estimate_basis"],
        "units": price.get("units"),
        "calls": price.get("calls"),
        "estimated_cost": estimate,
        "approved_cost": args.approved_cost,
        "status_url": status_url,
        "response_url": response_url,
        "uploads": {field: str(path) for field, path in files},
        "input": _loggable(built),
        "name": args.name,
        "out": str(args.out),
    })
    for url in (status_url, response_url):
        if not key_host_allowed(url):
            raise FalError(
                f"refusing to send FAL_KEY to {_where(url)}: not a fal API host "
                f"(request {request_id} is logged)"
            )
    poll(status_url, request_id, args.timeout)
    summary = collect(endpoint, request_id, response_url, args.out, args.name, args.log)
    summary.update(estimated_cost=estimate, currency=price["currency"])
    return summary


def cmd_result(args) -> dict:
    if not REQUEST_ID_RE.match(args.request_id):
        raise FalError(f"not a request id: {args.request_id!r}")
    matches = [r for r in read_log(args.log)
               if r.get("event") == "submitted" and r.get("request_id") == args.request_id]
    if not matches:
        raise FalError(f"request {args.request_id} is not in {args.log}")
    record = matches[-1]
    endpoint = check_endpoint(str(record.get("endpoint") or ""))
    response_url = str(record.get("response_url")
                       or f"{QUEUE_URL}/{endpoint}/requests/{args.request_id}")
    name = args.name or record.get("name") or args.request_id
    return collect(endpoint, args.request_id, response_url, args.out, name, args.log)


def cmd_spend(args) -> dict:
    totals: dict[str, float] = {}
    submissions = completed = 0
    for record in read_log(args.log):
        if record.get("event") == "submitted":
            submissions += 1
            cost = record.get("estimated_cost")
            if isinstance(cost, (int, float)):
                currency = str(record.get("currency") or "USD")
                totals[currency] = round(totals.get(currency, 0.0) + cost, 6)
        elif record.get("event") == "completed":
            completed += 1
    return {"log": str(args.log), "submissions": submissions, "completed": completed,
            "estimated_total": totals}


def cmd_price(args) -> dict:
    endpoint = check_endpoint(args.endpoint)
    if args.units is None and args.calls is None:
        return {"endpoint_id": endpoint, **lookup_price(endpoint)}
    return {"endpoint_id": endpoint, **estimate_cost(endpoint, args.units, args.calls)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fal_api.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="search fal's model catalog (free)")
    search.add_argument("query")
    search.add_argument("--category", help="e.g. text-to-image, image-to-3d, text-to-audio")
    search.add_argument("--limit", type=int, default=15)

    schema = sub.add_parser("schema", help="input and output fields of an endpoint (free)")
    schema.add_argument("endpoint")

    price = sub.add_parser("price", help="unit price of an endpoint (free)")
    price.add_argument("endpoint")
    price.add_argument("--units", type=float, help="estimate this many billing units")
    price.add_argument("--calls", type=int,
                       help="estimate this many calls from fal's historical average")

    run = sub.add_parser("run", help="PAID: submit one job, wait, download outputs")
    run.add_argument("endpoint")
    run.add_argument("--input", help="input JSON object; values are sent verbatim")
    run.add_argument("--input-file", help="file holding the input JSON object")
    run.add_argument("--file", action="append", metavar="FIELD=PATH",
                     help="upload this user-named local file into FIELD (repeat for *_urls)")
    run.add_argument("--units", type=float,
                     help="billing units the estimate assumes (default 1)")
    run.add_argument("--calls", type=int,
                     help="estimate from fal's historical average cost per call instead")
    run.add_argument("--approved-cost", type=float, required=True,
                     help="the amount the user approved for this job")
    run.add_argument("--timeout", type=float, default=900.0, help="seconds to wait")
    run.add_argument("--name", default="fal", help="output file stem")

    result = sub.add_parser("result", help="fetch and download a logged request")
    result.add_argument("request_id")
    result.add_argument("--name", help="output file stem")

    sub.add_parser("spend", help="total the estimated spend in the log")

    for command in (run, result, sub.choices["spend"]):
        command.add_argument("--log", type=Path, default=DEFAULT_LOG,
                             help=f"generation log (default {DEFAULT_LOG})")
    for command in (run, result):
        command.add_argument("--out", type=Path, default=DEFAULT_OUT,
                             help=f"output directory (default {DEFAULT_OUT})")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "search": lambda: search_models(args.query, args.category, args.limit),
        "schema": lambda: model_schema(check_endpoint(args.endpoint)),
        "price": lambda: cmd_price(args),
        "run": lambda: cmd_run(args),
        "result": lambda: cmd_result(args),
        "spend": lambda: cmd_spend(args),
    }
    try:
        reply = handlers[args.command]()
    except FalError as exc:
        print(json.dumps({"ok": False, "error": redact(str(exc))}), file=sys.stderr)
        return 1
    except OSError as exc:
        print(json.dumps({"ok": False, "error": redact(f"{type(exc).__name__}: {exc}")}),
              file=sys.stderr)
        return 1
    print(json.dumps(reply, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
