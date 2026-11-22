"""Fixed production config and read-only verification, independent of upstream config."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
TARGETS = json.loads((HERE / 'targets.json').read_text())
ACCOUNT = '420619639814cbe8dde95fb3d71bb5b5'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def request(url, headers=None):
    # Config on stdin avoids credentials in process arguments. Do not follow redirects.
    config = 'url = ' + json.dumps(url) + '\n'
    for key, value in (headers or {}).items():
        config += 'header = ' + json.dumps(key + ': ' + value) + '\n'
    result = subprocess.run(['curl', '--silent', '--fail', '--max-time', '90', '--config', '-'],
                            input=config, capture_output=True, text=True)
    require(result.returncode == 0, 'HTTP check failed (response and credentials suppressed)')
    return result.stdout


def settings(target):
    require(os.environ['CLOUDFLARE_ACCOUNT_ID'] == ACCOUNT, 'Unexpected Cloudflare account')
    result = json.loads(request(f'https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{target}/settings',
                               {'Authorization': 'Bearer ' + os.environ['CLOUDFLARE_API_TOKEN']}))
    require(result.get('success'), 'Cannot read current Worker settings')
    return result['result']


def config(target, live, source, revision):
    require(target in TARGETS, 'Unknown production Worker')
    require(re.fullmatch('[0-9a-f]{40}', source) and re.fullmatch('[0-9a-f]{40}', revision), 'Full commit SHA required')
    spec = TARGETS[target]
    bindings = {item['name']: item for item in live.get('bindings', [])}
    expected = {'db': ('d1', 'id', spec['database_id']), 'kv': ('kv_namespace', 'namespace_id', spec['kv_id'])}
    for name, (kind, key, value) in expected.items():
        if name in bindings:
            require(bindings[name]['type'] == kind and bindings[name].get(key) == value,
                    f'{name} points to another resource; refusing to deploy')
    require('jwt_secret' in bindings, 'Existing login secret missing; restore it before deploying')
    cfg = dict(name=target, main='production-entry.mjs', compatibility_date='2025-06-04', keep_vars=True,
               observability={'enabled': True},
               routes=[{'pattern': 'mail.' + spec['domain'], 'custom_domain': True}],
               d1_databases=[{'binding': 'db', 'database_name': spec['database_name'], 'database_id': spec['database_id']}],
               kv_namespaces=[{'binding': 'kv', 'id': spec['kv_id']}],
               ai={'binding': 'ai'}, send_email=[{'name': 'forward'}],
               durable_objects={'bindings': [{'name': 'ws', 'class_name': 'MailHub'}]},
               migrations=[{'tag': 'v1', 'new_sqlite_classes': ['MailHub']}],
               assets={'binding': 'assets', 'directory': './dist', 'not_found_handling': 'single-page-application',
                       'run_worker_first': ['/api/*', '/oss/*', '/static/*', '/attachments/*', '/proxy', '/realtime']},
               triggers={'crons': ['0 * * * *']},
               vars={'domain': [spec['domain']], 'admin': 'mail@' + spec['domain'],
                     'DEPLOY_TARGET': target, 'DEPLOY_SOURCE': source, 'DEPLOY_REVISION': revision})
    # Preserve optional live bindings and restrictions; never silently drop unfamiliar resources.
    for name, binding in bindings.items():
        kind = binding['type']
        if kind in ('plain_text', 'json', 'secret_text'):
            continue  # keep_vars and Wrangler preserve these; no secrets copied to disk.
        if name in ('db', 'kv', 'assets', 'ai', 'ws'):
            require(kind == {'db': 'd1', 'kv': 'kv_namespace', 'assets': 'assets', 'ai': 'ai', 'ws': 'durable_object_namespace'}[name], 'Binding type conflict: ' + name)
            if name == 'ws':
                require(binding.get('class_name') == 'MailHub' and not binding.get('script_name'), 'Unexpected websocket binding')
            continue
        if kind == 'send_email':
            email = {'name': name}
            for key in ('destination_address', 'allowed_destination_addresses', 'allowed_sender_addresses'):
                if key in binding:
                    email[key] = binding[key]
            cfg['send_email'] = [b for b in cfg['send_email'] if b['name'] != name] + [email]
        elif kind == 'r2_bucket':
            bucket = {'binding': name, 'bucket_name': binding['bucket_name']}
            if binding.get('jurisdiction'):
                bucket['jurisdiction'] = binding['jurisdiction']
            cfg.setdefault('r2_buckets', []).append(bucket)
        else:
            raise RuntimeError('Unrecognized existing binding; add explicit support before deployment: ' + name)
    return cfg


def prepare(target, worker, source, revision):
    live = settings(target)
    cfg = config(target, live, source, revision)
    jwt = next(b for b in live['bindings'] if b['name'] == 'jwt_secret')
    if jwt['type'] == 'plain_text':
        require(hmac.compare_digest(jwt['text'], os.environ['JWT_SECRET']), 'Environment login secret differs from live Worker')
    (worker / 'wrangler-production.json').write_text(json.dumps(cfg, indent=2) + '\n')
    shutil.copyfile(HERE / 'health.mjs', worker / 'deployment-health.mjs')
    shutil.copyfile(HERE / 'entry.mjs', worker / 'production-entry.mjs')
    print(f'{target}: fixed database/KV IDs, existing optional bindings and login settings preserved')


def verify(target, source, revision):
    live = settings(target)
    config(target, live, source, revision)
    names = {b['name'] for b in live['bindings']}
    require({'db', 'kv', 'ai', 'ws', 'assets', 'forward'} <= names, 'Required live binding missing')
    origin = 'https://mail.' + TARGETS[target]['domain']
    timestamp = str(int(time.time()))
    signature = hmac.new(os.environ['JWT_SECRET'].encode(),
                         f'GET\n/api/_deployment/health\n{timestamp}'.encode(), hashlib.sha256).hexdigest()
    health = json.loads(request(origin + '/api/_deployment/health', {'x-deploy-time': timestamp, 'x-deploy-signature': signature}))
    require(health.get('ok') and (health.get('target'), health.get('source'), health.get('deployment')) == (target, source, revision),
            'Live deployment health/revision mismatch')
    website = json.loads(request(origin + '/api/setting/websiteConfig'))
    require(website.get('code') == 200 and isinstance(website.get('data'), dict), 'Website configuration failed')
    html = request(origin + '/')
    assets = re.findall(r'(?:src|href)="(/assets/[^" ]+\.(?:js|css))"', html)
    require(assets, 'Frontend entry assets missing')
    for asset in assets:
        content = request(origin + asset)
        require(content.strip() and '<!doctype html' not in content[:100].lower(), 'Frontend asset returned HTML')
    print(f'{target}: database query, KV read, frontend, binding IDs and deployed revisions verified')
    print(json.dumps(health, sort_keys=True))


if __name__ == '__main__':
    try:
        mode, target, source, revision = sys.argv[1:5]
        require(target in TARGETS, 'Unknown Worker')
        if mode == 'prepare':
            prepare(target, Path(sys.argv[5]), source, revision)
        elif mode == 'init':
            result = request('https://mail.' + TARGETS[target]['domain'] + '/api/init/' + quote(os.environ['JWT_SECRET'], safe=''))
            require(result == 'success', 'Application migration/initialization failed')
            print(target + ': application initialization succeeded')
        elif mode == 'verify':
            for attempt in range(6):
                try:
                    verify(target, source, revision)
                    break
                except RuntimeError:
                    if attempt == 5:
                        raise
                    time.sleep(10)
        else:
            raise RuntimeError('Unknown mode')
    except Exception as error:
        # Never print HTTP bodies, URLs containing secrets or exception locals.
        print('Deployment stopped: ' + (str(error) if isinstance(error, RuntimeError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
