#!/usr/bin/env python3
"""Bounded read-only Linux baseline over existing SSH. No remote files written."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time

SLUG = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
TARGET = re.compile(r"(?:[a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z")


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def validate_inventory(raw):
    if not isinstance(raw, dict) or type(raw.get('version')) is not int or raw.get('version') != 1:
        raise ValueError('inventory version must be 1')
    hosts = raw.get('hosts')
    if not isinstance(hosts, list) or not 1 <= len(hosts) <= 64:
        raise ValueError('inventory requires 1..64 records')
    ids = set()
    for h in hosts:
        if not isinstance(h, dict) or not isinstance(h.get('id'), str) or not SLUG.fullmatch(h['id']):
            raise ValueError('invalid host id')
        if h['id'] in ids:
            raise ValueError('duplicate host id')
        ids.add(h['id'])
        if h.get('kind') not in ('host', 'alias', 'template', 'controller-record'):
            raise ValueError('invalid inventory kind')
        if type(h.get('owned')) is not bool:
            raise ValueError('owned must be a boolean')
        ssh = h.get('ssh')
        if ssh is None:
            continue
        if not isinstance(ssh, dict) or not isinstance(ssh.get('target'), str) or not TARGET.fullmatch(ssh['target']):
            raise ValueError('invalid SSH target; use a configured alias for IPv6')
        if not isinstance(h.get('expected_hostname'), str) or not SLUG.fullmatch(h['expected_hostname']):
            raise ValueError('SSH requires an exact expected hostname')
        key = ssh.get('identity_file')
        if key is not None and (not isinstance(key, str) or not key.startswith('/') or any(ord(c) < 32 for c in key)):
            raise ValueError('identity_file must be an absolute local path')
    return hosts


def ssh_command(host):
    validate_inventory({'version': 1, 'hosts': [host]})
    argv = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UpdateHostKeys=no', '-o', 'ConnectTimeout=8', '-o', 'ConnectionAttempts=1',
            '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=1',
            '-o', 'ForwardAgent=no', '-o', 'ForwardX11=no', '-o', 'ClearAllForwardings=yes',
            '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'PermitLocalCommand=no',
            '-o', 'RemoteCommand=none', '-o', 'SessionType=default',
            '-o', 'SendEnv=-*']
    if host['ssh'].get('identity_file'):
        argv += ['-o', 'IdentitiesOnly=yes', '-i', host['ssh']['identity_file']]
    return argv + [host['ssh']['target'], 'python3 -I -S - ' + host['expected_hostname']]


def run_bounded(argv, input_data=None, timeout=8, limit=262144):
    """Cap combined pipe bytes and wall time; kill the entire subprocess group."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError:
        return {'status': 'error', 'returncode': None, 'stdout': '', 'stderr': ''}
    output = {'stdout': bytearray(), 'stderr': bytearray()}
    pending = memoryview(input_data.encode() if isinstance(input_data, str) else input_data or b'')
    status = 'ok'
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as sel:
        for stream, label in ((proc.stdout, 'stdout'), (proc.stderr, 'stderr')):
            os.set_blocking(stream.fileno(), False)
            sel.register(stream, selectors.EVENT_READ, label)
        if proc.stdin:
            os.set_blocking(proc.stdin.fileno(), False)
            if pending:
                sel.register(proc.stdin, selectors.EVENT_WRITE, 'stdin')
            else:
                proc.stdin.close()
        try:
            while sel.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = 'timeout'
                    break
                for key, _ in sel.select(min(remaining, 0.2)):
                    if key.data == 'stdin':
                        try:
                            count = os.write(key.fd, pending[:4096])
                            pending = pending[count:]
                        except BrokenPipeError:
                            pending = memoryview(b'')
                        if not pending:
                            sel.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        data = os.read(key.fd, 8192)
                        if not data:
                            sel.unregister(key.fileobj)
                            continue
                        used = sum(len(v) for v in output.values())
                        output[key.data].extend(data[:max(0, limit - used)])
                        if used + len(data) > limit:
                            status = 'output_limit'
                            break
                if status != 'ok':
                    break
            if status == 'ok':
                try:
                    proc.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    status = 'timeout'
        finally:
            # Also terminate descendants that inherited pipe descriptors.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream and not stream.closed:
                    stream.close()
    if status == 'ok' and proc.returncode:
        status = 'error'
    return dict(status=status, returncode=proc.returncode,
                **{k: bytes(v).decode('utf-8', errors='replace') for k, v in output.items()})


def remote_baseline(expected):
    import socket
    actual = socket.gethostname()
    result = {'hostname': actual, 'observed_at': stamp(), 'checks': {}}
    if actual != expected:
        result['status'] = 'identity_mismatch'
        return result
    os.environ.update({'LC_ALL': 'C', 'SYSTEMD_PAGER': 'cat', 'PAGER': 'cat'})
    result['status'] = 'ok'
    checks = [
        ('kernel', ['uname', '-srmo']), ('uptime', ['uptime']),
        ('reboots', ['last', '-x', '-n', '6', 'reboot', 'shutdown']),
        ('cpu_count', ['getconf', '_NPROCESSORS_ONLN']), ('memory', ['free', '-m']),
        ('cpu_sample', ['vmstat', '1', '3']),
        ('space', ['df', '-l', '-P', '-h', '-x', 'tmpfs', '-x', 'devtmpfs', '-x', 'overlay']),
        ('inodes', ['df', '-l', '-P', '-i', '-x', 'tmpfs', '-x', 'devtmpfs', '-x', 'overlay']),
        ('mounts', ['findmnt', '-rn', '-o', 'TARGET,SOURCE,FSTYPE']),
        ('block_devices', ['lsblk', '-o', 'NAME,TYPE,SIZE,FSTYPE,MOUNTPOINT,ROTA']),
        ('failed_services', ['systemctl', '--failed', '--no-legend', '--no-pager', '--plain']),
        ('time_sync', ['timedatectl', 'show', '-p', 'NTPSynchronized', '-p', 'NTP', '-p', 'Timezone']),
        ('pi_temperature', ['vcgencmd', 'measure_temp']),
        ('pi_throttling', ['vcgencmd', 'get_throttled']),
        ('pi_core_voltage', ['vcgencmd', 'measure_volts', 'core']),
    ]
    for name, cmd in checks:
        r = run_bounded(cmd)
        # Utility errors and arbitrary stderr are not report payloads.
        result['checks'][name] = {'status': r['status'], 'returncode': r['returncode'],
                                  'output': r['stdout'] if r['status'] == 'ok' else ''}
    for name, path in [('os', '/etc/os-release'), ('pressure_cpu', '/proc/pressure/cpu'),
                       ('pressure_memory', '/proc/pressure/memory'), ('pressure_io', '/proc/pressure/io'),
                       ('mdraid', '/proc/mdstat')]:
        r = run_bounded(['head', '-c', '8192', path])
        value = r['stdout']
        if name == 'os':
            value = '\n'.join(line for line in value.splitlines() if line.startswith(('ID=', 'VERSION_ID=', 'PRETTY_NAME=')))
        result['checks'][name] = {'status': r['status'], 'returncode': r['returncode'],
                                  'output': value if r['status'] == 'ok' else ''}
    # No elevation: journal visibility is explicitly incomplete for an ordinary user.
    r = run_bounded(['journalctl', '--since', '24 hours ago', '-p', 'warning', '-n', '1000',
                     '--no-pager', '-o', 'json'], limit=1048576)
    counts = {'oom': 0, 'storage': 0, 'thermal_power': 0}
    patterns = {'oom': r'out of memory|oom-kill|killed process',
                'storage': r'I/O error|medium error|EXT4-fs error|read-only file system',
                'thermal_power': r'under.?voltage|overheat|throttl'}
    records = 0
    malformed = False
    if r['status'] == 'ok':
        for line in r['stdout'].splitlines():
            try:
                entry = json.loads(line)
                msg = str(entry.get('MESSAGE', ''))
            except (ValueError, AttributeError):
                malformed = True
                continue
            records += 1
            for key, pattern in patterns.items():
                counts[key] += bool(re.search(pattern, msg, re.I))
    result['checks']['recent_errors'] = {
        'status': 'error' if malformed else r['status'], 'returncode': r['returncode'],
        'records': records, 'possibly_capped': records >= 1000, 'category_counts': counts,
        'visibility': 'unverified; effective-user journal access only; no raw messages exported'}
    for name in ('storage_health', 'workload_health', 'backup_freshness'):
        result['checks'][name] = {'status': 'unverified', 'reason': 'requires documented host-specific follow-up'}
    result['finished_at'] = stamp()
    return result


def probe_host(host):
    if host.get('owned') is not True or host.get('kind') != 'host':
        return {'id': host['id'], 'status': 'excluded', 'observed_at': stamp()}
    if not host.get('ssh'):
        return {'id': host['id'], 'status': 'unavailable', 'observed_at': stamp()}
    r = run_bounded(ssh_command(host), input_data=Path(__file__).read_text(), timeout=210, limit=1048576)
    if r['status'] != 'ok':
        status = r['status']
        if status == 'error':
            stderr = r['stderr'].lower()
            status = ('host_key_failure' if 'host key verification failed' in stderr or 'identification has changed' in stderr
                      else 'authentication_failed' if 'permission denied' in stderr else 'ssh_failed')
        return {'id': host['id'], 'status': status, 'observed_at': stamp(),
                'note': 'no host health conclusion; SSH or remote Python unavailable'}
    try:
        data = json.loads(r['stdout'])
        if not isinstance(data, dict):
            raise ValueError()
        if data.get('hostname') != host['expected_hostname']:
            return {'id': host['id'], 'status': 'identity_mismatch', 'observed_at': stamp()}
        if data.get('status') != 'ok' or not isinstance(data.get('checks'), dict):
            raise ValueError()
    except ValueError:
        return {'id': host['id'], 'status': 'invalid_response', 'observed_at': stamp()}
    data['id'] = host['id']
    return data


def main():
    # stdin execution on a known host, always gate before any health command.
    if sys.argv[0] == '-':
        print(json.dumps(remote_baseline(sys.argv[1])))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', default=os.environ.get('ASHA_DEVOPS_INVENTORY',
                        str(Path.home() / '.asha/devops/inventory.json')))
    parser.add_argument('--host', action='append', help='exact inventory id; repeat to select')
    parser.add_argument('--list', action='store_true', help='resolve records without network access')
    args = parser.parse_args()
    try:
        with open(args.inventory) as source:
            raw = source.read(262145)
        if len(raw) > 262144:
            raise ValueError('inventory too large')
        hosts = validate_inventory(json.loads(raw))
        if args.host:
            if set(args.host) - {h['id'] for h in hosts}:
                raise ValueError('unknown host selection')
            hosts = [h for h in hosts if h['id'] in args.host]
    except (OSError, ValueError):
        print(json.dumps({'status': 'invalid_inventory', 'note': 'read private inventory contract; no network attempted'}))
        return 2
    if args.list:
        print(json.dumps({'status': 'ok', 'hosts': [
            {k: h.get(k) for k in ('id', 'kind', 'owned', 'expected_hostname', 'availability')} for h in hosts]}))
        return 0
    results = [probe_host(h) for h in hosts]
    print(json.dumps({'observed_at': stamp(), 'hosts': results}, indent=2))
    return 0 if all(h['status'] in ('ok', 'excluded') for h in results) else 1


if __name__ == '__main__':
    sys.exit(main())
