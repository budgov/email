import hashlib
import hmac
import json
import os
import time
from deploy import ACCOUNT, TARGETS, request, require

target = os.environ['TARGET']
spec = TARGETS[target]
prefix = f'https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}'
auth = {'Authorization': 'Bearer ' + os.environ['CLOUDFLARE_API_TOKEN']}

def api(url):
    data = json.loads(request(url, auth))
    require(data.get('success'), 'API failed')
    return data['result']

for attempt in range(3):
    live = api(prefix + f'/workers/scripts/{target}/settings')
    print(target, 'settings resources', sorted(b['name'] for b in live['bindings'] if b['type'] not in ('plain_text','json','secret_text')))
    active = api(prefix + f'/workers/scripts/{target}/deployments')['deployments'][0]
    version_id = active['versions'][0]['version_id']
    version = api(prefix + f'/workers/scripts/{target}/versions/{version_id}')
    print(target, 'active version', version_id, 'bindings', sorted(b['name'] for b in version['resources']['bindings'] if b['type'] not in ('plain_text','json','secret_text')))
    timestamp = str(int(time.time()))
    signature = hmac.new(os.environ['JWT_SECRET'].encode(), f'GET\n/api/_deployment/health\n{timestamp}'.encode(), hashlib.sha256).hexdigest()
    health = json.loads(request('https://mail.' + spec['domain'] + '/api/_deployment/health', {'x-deploy-time': timestamp, 'x-deploy-signature': signature}))
    print(target, 'live runtime', json.dumps(health, sort_keys=True))
    require(health.get('ok'), 'Runtime health failed')
    if attempt < 2:
        time.sleep(15)
scripts = api(prefix + '/workers/scripts')
require('cloud-mail' not in {s['id'] for s in scripts}, 'Unexpected cloud-mail still exists')
print('cloud-mail absent; no deletion needed')
