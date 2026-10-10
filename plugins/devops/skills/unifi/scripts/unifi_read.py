#!/usr/bin/env python3
"""Read four explicitly selected legacy UniFi Network endpoints; no mutations."""
import argparse
import datetime
import http.client
import json
import os
import re
import signal
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

RESOURCES = {'clients': 'stat/sta', 'devices': 'stat/device',
             'health': 'stat/health', 'remembered': 'rest/user'}
LIMIT = 2 * 1024 * 1024
FIELDS = {'name', 'hostname', 'ip', 'mac', 'model', 'version', 'state', 'uptime',
          'last_seen', 'first_seen', 'is_wired', 'is_guest', 'sw_mac', 'sw_port',
          'ap_mac', 'network', 'network_id', 'subsystem', 'status', 'overheating',
          'general_temperature', 'num_sta', 'num_adopted', 'num_disconnected',
          'rx_bytes', 'tx_bytes', 'satisfaction'}
NESTED = {'system-stats': {'cpu', 'mem', 'uptime'},
          'sys_stats': {'mem_total', 'mem_used', 'loadavg_1', 'loadavg_5', 'loadavg_15'}}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'redirect refused', headers, fp)


def validate_request(resource, base_url, token):
    if resource not in RESOURCES:
        raise ValueError('unsupported resource')
    if not isinstance(base_url, str) or any(ord(c) < 33 or ord(c) > 126 for c in base_url):
        raise ValueError('invalid base URL')
    parsed = urllib.parse.urlsplit(base_url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or not re.fullmatch(r'/proxy/network/api/s/[A-Za-z0-9_-]+/?', parsed.path)):
        raise ValueError('require HTTPS legacy Network site base')
    # Access validates malformed/out-of-range port before constructing a request.
    _ = parsed.port
    if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError('missing or invalid API key')


def project(record):
    safe = {k: v for k, v in record.items() if k in FIELDS and
            (v is None or type(v) in (str, int, float, bool))}
    for key, allowed in NESTED.items():
        value = record.get(key)
        if isinstance(value, dict):
            safe[key] = {k: v for k, v in value.items() if k in allowed and type(v) in (str, int, float, bool)}
    return safe


def contains_token(value, token):
    if isinstance(value, str):
        return token in value
    if isinstance(value, dict):
        return any(contains_token(v, token) for v in value.values())
    if isinstance(value, list):
        return any(contains_token(v, token) for v in value)
    return False


def fetch(resource, base_url, token, ca_file=None):
    validate_request(resource, base_url, token)
    try:
        context = ssl.create_default_context(cafile=ca_file)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                   urllib.request.HTTPSHandler(context=context), NoRedirect())
        request = urllib.request.Request(base_url.rstrip('/') + '/' + RESOURCES[resource],
                   headers={'X-API-KEY': token, 'Accept': 'application/json'}, method='GET')
        with opener.open(request, timeout=8) as response:
            body = response.read(LIMIT + 1)
        if len(body) > LIMIT:
            return {'status': 'error', 'error': 'response_too_large'}
        payload = json.loads(body)
        if (not isinstance(payload, dict) or not isinstance(payload.get('meta'), dict)
                or payload['meta'].get('rc') != 'ok' or not isinstance(payload.get('data'), list)
                or any(not isinstance(record, dict) for record in payload['data'])):
            return {'status': 'error', 'error': 'unexpected_API_response'}
        data = [project(record) for record in payload['data']]
        # Defense against a reflected auth value, even in an allowed string field.
        if contains_token(data, token):
            return {'status': 'error', 'error': 'credential_reflection_refused'}
        return {'status': 'ok', 'resource': resource, 'count': len(data), 'data': data,
                'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'coverage': 'returned controller records only; completeness and ownership unverified'}
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return {'status': 'error', 'error': 'HTTP_' + str(code)}
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        # Never emit URL, exception text, response body or headers containing credentials.
        return {'status': 'error', 'error': 'transport_TLS_or_response_failure'}


def expired(signum, frame):
    raise TimeoutError('request deadline')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('resource', choices=RESOURCES)
    args = parser.parse_args()
    if not hasattr(signal, 'SIGALRM'):
        print(json.dumps({'status': 'error', 'error': 'POSIX_deadline_support_required'}))
        return 2
    # No token/base arguments: wrapper-scoped environment, never process argv.
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(30)
    try:
        result = fetch(args.resource, os.environ.get('UNIFI_BASE_URL', ''),
                       os.environ.get('UNIFI_API_KEY', ''), os.environ.get('UNIFI_CA_CERT') or None)
    except ValueError:
        result = {'status': 'error', 'error': 'invalid_configuration',
                  'note': 'set UNIFI_BASE_URL, UNIFI_API_KEY; trusted CA via UNIFI_CA_CERT if needed'}
    except TimeoutError:
        result = {'status': 'error', 'error': 'deadline_exceeded'}
    finally:
        signal.alarm(0)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
