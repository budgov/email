import json
import os
import urllib.request
from deploy import ACCOUNT, TARGETS, require

base = f'/accounts/{ACCOUNT}'


def api(path, method='GET'):
    req = urllib.request.Request('https://api.cloudflare.com/client/v4' + path, method=method,
        headers={'Authorization': 'Bearer ' + os.environ['CLOUDFLARE_API_TOKEN'], 'User-Agent': 'mail-deployment-maintenance'})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Cloudflare {method} request failed: HTTP {error.code}; no forced cleanup') from None
    require(payload.get('success'), 'Cloudflare request unsuccessful')
    return payload


def page_list(path):
    items = []
    page = 1
    while True:
        data = api(path + ('&' if '?' in path else '?') + f'per_page=50&page={page}')
        items.extend(data['result'])
        if page >= data.get('result_info', {}).get('total_pages', 1):
            return items
        page += 1


scripts = api(base + '/workers/scripts')['result']
names = {s['id'] for s in scripts}
print('Mail Workers present:', ', '.join(sorted(names & {'budgov-mail', 'juuer-mail', 'cloud-mail'})))
require(set(TARGETS) <= names, 'A production Worker is missing')
domains = api(base + '/workers/domains')['result']
require(not any(d.get('service') == 'cloud-mail' for d in domains), 'cloud-mail still owns a custom domain')
for target, spec in TARGETS.items():
    require(any(d.get('hostname') == 'mail.' + spec['domain'] and d.get('service') == target for d in domains), 'Production custom domain mismatch')
    bindings = api(base + f'/workers/scripts/{target}/settings')['result']['bindings']
    mapped = {b['name']: b for b in bindings}
    require(mapped['db']['id'] == spec['database_id'] and mapped['kv']['namespace_id'] == spec['kv_id'], 'Production resource mismatch')

zones = page_list(f'/zones?account.id={ACCOUNT}')
require({'budgov.com', 'juuer.com'} <= {z['name'] for z in zones}, 'Cannot inspect both production zones')
for zone in zones:
    routes = api(f'/zones/{zone["id"]}/workers/routes')['result']
    require(not any(r.get('script') == 'cloud-mail' for r in routes), 'cloud-mail still has a Worker route')
    if zone['name'] not in ('budgov.com', 'juuer.com'):
        continue
    rules = page_list(f'/zones/{zone["id"]}/email/routing/rules')
    catch_all = api(f'/zones/{zone["id"]}/email/routing/rules/catch_all')['result']
    rules.append(catch_all)
    require(not any(a.get('type') == 'worker' and 'cloud-mail' in a.get('value', []) for rule in rules for a in rule.get('actions', [])), 'cloud-mail is referenced by an email rule')
    target = 'budgov-mail' if zone['name'] == 'budgov.com' else 'juuer-mail'
    require(catch_all.get('enabled') and any(a.get('type') == 'worker' and a.get('value') == [target] for a in catch_all.get('actions', [])), 'Production email catch-all mismatch')
    print(zone['name'] + ': email catch-all routes to ' + target)

if 'cloud-mail' in names:
    api(base + '/workers/scripts/cloud-mail?force=false', 'DELETE')
    remaining = {s['id'] for s in api(base + '/workers/scripts')['result']}
    require('cloud-mail' not in remaining and set(TARGETS) <= remaining, 'Worker cleanup verification failed')
    print('Deleted unused cloud-mail Worker; no D1 databases or KV namespaces deleted')
else:
    print('cloud-mail already absent; nothing to delete')
