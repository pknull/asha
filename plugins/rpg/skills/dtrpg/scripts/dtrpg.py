#!/usr/bin/env python3
"""Deterministic DriveThruRPG library audit, staging, placement and claim tools."""

from __future__ import annotations

import argparse
import base64
from collections import defaultdict
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import fnmatch
from html.parser import HTMLParser
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit


ROOT_ENV = "DTRPG_LIBRARY_ROOT"
DEFAULT_STAGING = "~/Downloads/dtrpg-staging"
STATUSES = ("ignored", "present", "stale", "present_by_size", "missing")
RETRY_STATUSES = {429, 500, 502, 503, 504}
# watermark.drivethrurpg.com rejects httpx's default User-Agent with
# 503 "Blocked for not following robot.txt rules, Bad bot"; drpg sends these.
DOWNLOAD_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*",
                    "Accept-Encoding": "gzip, deflate, br"}


class CLIError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CLIError(message)


def read_json(path):
    with Path(path).expanduser().open(encoding="utf-8") as stream:
        return json.load(stream)


def iso_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # Treat legacy timestamps without an offset as UTC, never local time.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def updated_since_download(modified, downloaded):
    if not downloaded:
        return True
    modified, downloaded = iso_datetime(modified), iso_datetime(downloaded)
    return bool(modified and downloaded and modified > downloaded)


def normalize_product(product):
    """Accept API products and the public library JSON representation."""
    raw = "product_id" not in product
    modified = product.get("fileLastModified" if raw else "file_last_modified")
    downloaded = product.get("fileLastDownloaded" if raw else "file_last_downloaded")
    publisher = product.get("publisher")
    if isinstance(publisher, dict):
        publisher = publisher.get("name")
    return {
        "product_id": product.get("productId" if raw else "product_id"),
        "order_product_id": product.get("orderProductId" if raw else "order_product_id"),
        "name": product.get("name"), "publisher": publisher,
        "file_last_modified": modified, "file_last_downloaded": downloaded,
        "updated_since_download": updated_since_download(modified, downloaded),
        "files": [{key: file.get(key) for key in ("index", "filename", "size")}
                  for file in (product.get("files") or []) if isinstance(file, dict)],
    }


@contextmanager
def quiet_network():
    """Third-party diagnostics can contain auth query strings or signed URLs."""
    previous = logging.root.manager.disable
    logging.disable(sys.maxsize)
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            yield
    finally:
        logging.disable(previous)


def authenticate():
    token = os.environ.get("DRPG_TOKEN")
    if not token:
        raise CLIError("DRPG_TOKEN not set; export it before network commands")
    try:
        with quiet_network():
            from drpg.api import DrpgApi
            api = DrpgApi(token)
            api.token()
        return api
    except Exception as exc:
        # Do not echo dependency exception text: URLs can contain the API key.
        # The type alone separates a rejected key (AttributeError) from network trouble.
        raise RuntimeError(f"DTRPG authentication failed ({type(exc).__name__}); "
                           "check token and drpg interpreter") from None


def load_library(path=None, api=None):
    if path:
        data = read_json(path)
        products = data.get("products") or []
    else:
        api = api if api is not None else authenticate()
        try:
            with quiet_network():
                products = list(api.customer_products(per_page=50))
        except Exception:
            raise RuntimeError("DTRPG library request failed") from None
    products = [normalize_product(p) for p in products if isinstance(p, dict)]
    return {"products": products, "count": len(products)}


def read_tsv(path, columns):
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = [field.strip() for field in line.split("\t")]
        if len(fields) < columns:
            raise ValueError(f"Malformed TSV row {number} in {path.name}")
        rows.append(fields)
    return rows


def load_state(state_dir=None):
    state = Path(state_dir or os.environ.get("DTRPG_STATE_DIR", "~/.asha/dtrpg")).expanduser()
    placements = {row[0]: row[1] for row in read_tsv(state / "placements.tsv", 2)}
    ignores = [row[:3] for row in read_tsv(state / "ignore.tsv", 3)]
    return placements, ignores


def staging_path(root, staging):
    root, staging = Path(root).expanduser().resolve(), Path(staging).expanduser().resolve()
    if staging.is_relative_to(root):
        raise ValueError("Staging directory must be outside the library root")
    return staging


def safe_filename(filename):
    """Discard path components, traversal markers and control characters."""
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name)
    while ".." in name:
        name = name.replace("..", "_")
    name = name.strip()
    if not name or name in {".", "_"}:
        raise ValueError("Missing or unsafe filename")
    return name


def normalized_filename(name):
    return str(name or "").casefold().replace("_", " ")


def candidate_names(name):
    name = str(name or "")
    return {normalized_filename(name), normalized_filename(re.sub(r"^\d+-", "", name))}


def local_index(root):
    names, sizes = defaultdict(list), defaultdict(list)
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Library root is not a directory")
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in {"node_modules", ".git", "__MACOSX"})
        for name in sorted(files):
            path = Path(directory) / name
            if not path.is_file():
                continue
            relative = str(path.relative_to(root))
            names[normalized_filename(name)].append(relative)
            sizes[path.stat().st_size].append(relative)
    return names, sizes


def audit(library, root, placements=None, ignores=(), statuses=None):
    placements = placements or {}
    names, sizes = local_index(root)
    summary = dict.fromkeys(STATUSES, 0)
    files = []
    for product in library["products"]:
        for file in product.get("files") or []:
            filename = file.get("filename") or ""
            paths, reason, status = [], None, "missing"
            for product_id, pattern, explanation in ignores:
                if str(product.get("product_id")) == product_id and fnmatch.fnmatchcase(filename, pattern):
                    status, reason = "ignored", explanation
                    break
            else:
                paths = sorted({p for name in candidate_names(filename) for p in names.get(name, [])})
                if paths:
                    # The update date is per product; an exact size match means this
                    # file's bytes are current even if a sibling file changed.
                    size = file.get("size")
                    same_size = isinstance(size, int) and size > 0 and set(paths) & set(sizes.get(size, []))
                    status = "stale" if product.get("updated_since_download") and not same_size else "present"
                    if status == "stale" and not product.get("file_last_downloaded"):
                        reason = "never downloaded from DTRPG; compare with the local copy before fetching"
                elif isinstance(file.get("size"), (int, float)) and file["size"] > 0:
                    paths = sizes.get(file["size"], [])
                    if paths:
                        status = "present_by_size"
            summary[status] += 1
            files.append({
                "product_id": product.get("product_id"),
                "order_product_id": product.get("order_product_id"),
                "product": product.get("name"), "publisher": product.get("publisher"),
                **file, "status": status, "local_paths": paths,
                "suggested_dest": placements.get(str(product.get("product_id"))), "reason": reason,
            })
    return {"root": str(Path(root).expanduser().resolve()), "summary": summary,
            "files": [f for f in files if not statuses or f["status"] in statuses]}


def item_key(item):
    return int(item["order_product_id"]), int(item["index"])


def select_items(items, audit_path=None, statuses=("missing", "stale"), library=None):
    selected = {}
    available = {}
    if audit_path:
        for file in read_json(audit_path).get("files", []):
            available[item_key(file)] = file
            if file.get("status") in statuses:
                selected[item_key(file)] = file
    for product in (library or {}).get("products", []):
        for file in product["files"]:
            if product.get("order_product_id") is not None and file.get("index") is not None:
                entry = {**file, "order_product_id": product["order_product_id"]}
                available[item_key(entry)] = entry
    for value in items:
        try:
            order, index = value.split(":")
            key = int(order), int(index)
        except ValueError:
            raise CLIError("--item must be ORDER_PRODUCT_ID:INDEX") from None
        if key not in selected:
            if key not in available:
                raise CLIError(f"Item {order}:{index} not found in library")
            selected[key] = available[key]
    if not selected:
        raise CLIError("No items selected")
    return list(selected.values())


class DownloadError(Exception):
    """A safe, locally constructed download diagnostic."""


class RetryDownload(DownloadError):
    pass


def download_item(item, staging, api, http_get, transport_errors=(), sleep=time.sleep):
    """http_get(url) returns a context manager with status_code/iter_bytes."""
    result = {"order_product_id": item.get("order_product_id"), "index": item.get("index"),
              "filename": item.get("filename"), "status": "failed", "path": None,
              "bytes": 0, "error": None}
    part, owned_part = None, False
    try:
        filename = safe_filename(item.get("filename"))
        target = Path(staging) / filename
        result.update(filename=filename, path=str(target))
        if os.path.lexists(target):
            result["status"] = "skipped_exists"
            return result
        part = target.with_name(filename + ".part")
        # Exclusive creation preserves a pre-existing partial file too.
        with part.open("xb") as output:
            owned_part = True
            for attempt in range(5):
                output.seek(0)
                output.truncate()
                try:
                    with quiet_network():
                        prepared = api.prepare_download_url(*item_key(item))
                        with http_get(prepared["url"]) as response:
                            if response.status_code in RETRY_STATUSES:
                                raise RetryDownload(f"HTTP {response.status_code}")
                            if not 200 <= response.status_code < 300:
                                raise DownloadError(f"HTTP {response.status_code}")
                            for chunk in response.iter_bytes():
                                output.write(chunk)
                    break
                except (RetryDownload, *transport_errors):
                    if attempt == 4:
                        raise
                    sleep(2 ** (attempt + 1))
            result["bytes"] = output.tell()
        # link + unlink gives rename semantics without POSIX rename's overwrite.
        try:
            os.link(part, target)
        except FileExistsError:
            result.update(status="skipped_exists", bytes=0)
        else:
            result["status"] = "downloaded"
    except Exception as exc:
        # Do not return exception text from the API or HTTP client (signed URLs).
        result["error"] = str(exc) if isinstance(exc, DownloadError) else f"Download failed ({type(exc).__name__})"
    finally:
        if owned_part:
            part.unlink(missing_ok=True)
    return result


def make_http_get(httpx):
    return lambda url: httpx.stream("GET", url, follow_redirects=True, timeout=60,
                                    headers=DOWNLOAD_HEADERS)


def fetch(items, root, staging, delay=2, dry_run=False, api=None, http_get=None,
          transport_errors=(), sleep=time.sleep):
    staging = staging_path(root, staging)
    if not math.isfinite(delay) or delay < 0:
        raise CLIError("--delay must be finite and nonnegative")
    if dry_run:
        results = []
        for item in items:
            filename = safe_filename(item.get("filename"))
            path = staging / filename
            results.append({"order_product_id": item.get("order_product_id"), "index": item.get("index"),
                            "filename": filename, "path": str(path), "bytes": 0, "error": None,
                            "status": "skipped_exists" if os.path.lexists(path) else "would_download"})
        return {"staging": str(staging), "results": results}
    api = api if api is not None else authenticate()
    if http_get is None:
        import httpx
        http_get = make_http_get(httpx)
        transport_errors = (httpx.TransportError,)
    staging.mkdir(parents=True, exist_ok=True)
    results = []
    for number, item in enumerate(items):
        if number:
            sleep(delay)
        results.append(download_item(item, staging, api, http_get, transport_errors, sleep))
    return {"staging": str(staging), "results": results}


def place(fetch_data, library, root, staging, placements, apply=False):
    root = Path(root).expanduser().resolve()
    staging = staging_path(root, staging)
    products = {str(p.get("order_product_id")): p for p in library["products"]}
    moves, unplaced, conflicts = [], [], []
    planned = set()
    for result in fetch_data.get("results", []):
        if result.get("status") != "downloaded" or not result.get("path"):
            continue
        src = Path(result["path"]).expanduser().resolve()
        if not src.is_relative_to(staging):
            raise ValueError("Fetch source is outside staging")
        if not src.is_file():
            continue
        product = products.get(str(result.get("order_product_id")), {})
        folder = placements.get(str(product.get("product_id")))
        if folder is None:
            unplaced.append({"src": str(src), "product_id": product.get("product_id"),
                             "order_product_id": result.get("order_product_id")})
            continue
        relative = Path(folder)
        dest = root / relative / src.name
        if relative.is_absolute() or not dest.resolve().is_relative_to(root):
            raise ValueError("Placement destination is outside the library root")
        move = {"src": str(src), "dest": str(dest)}
        if os.path.lexists(dest) or dest in planned:
            conflicts.append(move)
            continue
        planned.add(dest)
        if apply:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                output = dest.open("xb")
            except FileExistsError:
                conflicts.append(move)
                continue
            # Exclusive copy also supports staging and library on different disks.
            with output, src.open("rb") as source:
                shutil.copyfileobj(source, output)
                os.fchmod(output.fileno(), 0o664)
            src.unlink()
        moves.append(move)
    return {"moves": moves, "unplaced": unplaced, "conflicts": conflicts}


def normalize_title(title):
    title = str(title or "").casefold().translate(str.maketrans({
        "–": "-", "—": "-", "−": "-", "’": "'", "‘": "'", "“": '"', "”": '"',
    }))
    title = re.sub(r"['\"]", "", title)
    title = re.sub(r"\s*-\s*", "-", title)
    return " ".join(title.split())


class OfferParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.offers, self.anchor = [], None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            query = parse_qs(urlsplit(href).query)
            codes = query.get("discount") or query.get("discountId")
            self.anchor = (codes[0], []) if codes else None

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            code, pieces = self.anchor
            title = " ".join("".join(pieces).split())
            if title:
                self.offers.append((title, code))
            self.anchor = None


def parse_message(data):
    if not re.search(rb"^[!-9;-~]+:", data, re.MULTILINE):
        compact = b"".join(data.split())
        try:
            decoded = base64.b64decode(compact + b"=" * (-len(compact) % 4), altchars=b"-_", validate=True)
        except ValueError:
            pass
        else:
            if re.search(rb"^[!-9;-~]+:", decoded, re.MULTILINE):
                data = decoded
    return BytesParser(policy=policy.default).parsebytes(data)


def plain_titles(text):
    """Plain offers have no reliable codes; recover quoted/copy-of/bullet titles."""
    titles = re.findall(r'["“]([^"”\n]+)["”]', text)
    if not titles:
        titles = re.findall(r"(?:copy of|title:)\s*(?:\r?\n\s*)?([^\r\n]+)", text, re.IGNORECASE)
        titles = [title.rstrip(". ") for title in titles]
    if not titles:
        titles = re.findall(r"^\s*[-*•]\s+(.+)$", text, re.MULTILINE)
    return [(title, None) for title in titles]


def claims(eml_dir, library):
    products = {normalize_title(p.get("name")): p.get("name") for p in library["products"] if p.get("name")}
    offers, seen = [], set()
    directory = Path(eml_dir).expanduser()
    if not directory.is_dir():
        raise ValueError("Email directory does not exist")
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        message = parse_message(path.read_bytes())
        html, plain = [], []
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type() == "text/html":
                html.append(part.get_content())
            elif part.get_content_type() == "text/plain":
                plain.append(part.get_content())
        if html:
            parser = OfferParser()
            for body in html:
                parser.feed(body)
            parser.close()
            found = parser.offers
        else:
            found = plain_titles("\n".join(plain))
        for title, code in found:
            if (title, code) in seen:
                continue
            seen.add((title, code))
            matched = products.get(normalize_title(title))
            offers.append({"title": title, "code": code,
                           "claim_url": "https://www.drivethrurpg.com/en/browse?" + urlencode({"discountId": code}) if code else None,
                           "claimed": matched is not None, "matched_product": matched,
                           "message_date": str(message.get("Date", "")), "subject": str(message.get("Subject", "")),
                           "source_file": str(path)})
    claimed = sum(offer["claimed"] for offer in offers)
    return {"offers": offers, "summary": {"claimed": claimed, "unclaimed": len(offers) - claimed}}


def build_parser():
    parser = Parser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("library", "audit", "fetch", "place", "claims"):
        child = sub.add_parser(command)
        child.add_argument("--library-json")
        child.add_argument("--root")
        child.add_argument("--staging")
        if command in {"audit", "fetch"}:
            child.add_argument("--status", default="missing,stale" if command == "fetch" else None)
        if command == "fetch":
            child.add_argument("--item", action="append", default=[])
            child.add_argument("--from-audit")
            child.add_argument("--delay", type=float, default=2)
            child.add_argument("--dry-run", action="store_true")
        elif command == "place":
            child.add_argument("--from-fetch", required=True)
            child.add_argument("--apply", action="store_true")
        elif command == "claims":
            child.add_argument("--eml-dir", required=True)
    return parser


def library_root(root):
    root = root or os.environ.get(ROOT_ENV)
    if not root:
        raise CLIError(f"{ROOT_ENV} not set; export it or pass --root")
    return root


def execute(args):
    staging = args.staging or DEFAULT_STAGING
    if args.command in {"audit", "fetch", "place"}:
        args.root = library_root(args.root)
        staging_path(args.root, staging)
    statuses = None
    if getattr(args, "status", None) is not None:
        statuses = {s.strip() for s in args.status.split(",")}
        if not statuses <= set(STATUSES):
            raise CLIError("Unknown --status value")
    if args.command == "fetch":
        # An audit already supplies filenames for explicit items it contains.
        # Only resolve against the library when requested items are absent.
        audit_keys = set()
        if args.from_audit:
            audit_keys = {item_key(f) for f in read_json(args.from_audit).get("files", [])}
        requested = set()
        for item in args.item:
            try:
                order, index = item.split(":")
                requested.add((int(order), int(index)))
            except ValueError:
                raise CLIError("--item must be ORDER_PRODUCT_ID:INDEX") from None
        library = load_library(args.library_json) if requested - audit_keys else None
        items = select_items(args.item, args.from_audit, statuses, library)
        return fetch(items, args.root, staging, args.delay, args.dry_run)
    library = load_library(args.library_json)
    if args.command == "library":
        return library
    if args.command == "claims":
        return claims(args.eml_dir, library)
    placements, ignores = load_state()
    if args.command == "audit":
        return audit(library, args.root, placements, ignores, statuses)
    data = read_json(args.from_fetch)
    return place(data, library, args.root, args.staging or data.get("staging") or staging, placements, args.apply)


def main(argv=None):
    code = 0
    try:
        result = execute(build_parser().parse_args(argv))
        if result.get("results") and all(r["status"] == "failed" for r in result["results"]):
            code = 1
    except Exception as exc:
        result = {"error": str(exc), "detail": type(exc).__name__}
        code = 2 if isinstance(exc, CLIError) else 1
    encoded = json.dumps(result, ensure_ascii=False)
    token = os.environ.get("DRPG_TOKEN")
    if token:
        encoded = encoded.replace(json.dumps(token, ensure_ascii=False)[1:-1], "[REDACTED]")
    print(encoded)
    return code


if __name__ == "__main__":
    sys.exit(main())
