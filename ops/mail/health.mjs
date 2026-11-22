const path = '/api/_deployment/health';
const encoder = new TextEncoder();

// Authenticate a short-lived, purpose-specific signature, never a secret in a URL.
async function authorized(request, secret) {
  const timestamp = request.headers.get('x-deploy-time') || '';
  const signature = request.headers.get('x-deploy-signature') || '';
  if (!secret || !/^\d{10}$/.test(timestamp) || !/^[a-f0-9]{64}$/.test(signature)) return false;
  if (Math.abs(Date.now() / 1000 - Number(timestamp)) > 120) return false;
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['verify']);
  const bytes = Uint8Array.from(signature.match(/../g), hex => parseInt(hex, 16));
  return crypto.subtle.verify('HMAC', key, bytes, encoder.encode(`GET\n${path}\n${timestamp}`));
}

export function withDeploymentHealth(upstream) {
  if (!['fetch', 'email', 'scheduled'].every(name => typeof upstream[name] === 'function')) {
    throw new Error('Upstream no longer implements the required mail handlers');
  }
  return {
    ...upstream,
    async fetch(request, env, ctx) {
      if (new URL(request.url).pathname !== path) return upstream.fetch(request, env, ctx);
      if (request.method !== 'GET' || !await authorized(request, env.jwt_secret)) {
        return new Response('Not Found', { status: 404, headers: { 'Cache-Control': 'no-store' } });
      }
      const checks = {
        db: async () => (await env.db.prepare('SELECT 1 AS ok').first()).ok === 1,
        kv: async () => { await env.kv.get('__deployment_read_probe__'); return true; },
        assets: async () => (await env.assets.fetch(new Request(new URL('/', request.url)))).ok,
        ws: async () => typeof env.ws?.idFromName === 'function' && typeof env.ws?.get === 'function',
        forward: async () => typeof env.forward?.send === 'function',
        ai: async () => typeof env.ai?.run === 'function',
      };
      const results = Object.fromEntries(await Promise.all(Object.entries(checks).map(async ([name, check]) => {
        try { return [name, await check()]; } catch { return [name, false]; }
      })));
      const ok = Object.values(results).every(Boolean);
      return Response.json({ ok, checks: results, target: env.DEPLOY_TARGET,
        source: env.DEPLOY_SOURCE, deployment: env.DEPLOY_REVISION },
      { status: ok ? 200 : 503, headers: { 'Cache-Control': 'no-store' } });
    },
  };
}
