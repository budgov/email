import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { withDeploymentHealth } from './health.mjs';
globalThis.crypto ??= webcrypto;
const path = '/api/_deployment/health';
const upstream = { fetch: () => new Response('upstream'), email() {}, scheduled() {} };
const wrapped = withDeploymentHealth(upstream);
function env() {
  return { jwt_secret: 'local-test-only', db: { prepare: () => ({ first: async () => ({ ok: 1 }) }) },
    kv: { get: async () => null }, assets: { fetch: async () => new Response('app') },
    ws: { idFromName() {}, get() {} }, forward: { send() {} }, ai: { run() {} } };
}
async function signed(secret, seconds = 0) {
  const timestamp = String(Math.floor(Date.now() / 1000) + seconds);
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const signature = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(`GET\n${path}\n${timestamp}`));
  return new Request('https://example.com' + path, { headers: { 'x-deploy-time': timestamp,
    'x-deploy-signature': Buffer.from(signature).toString('hex') } });
}
test('normal requests, mail and scheduled handlers are preserved', async () => {
  assert.equal(await (await wrapped.fetch(new Request('https://example.com/api/login'), {}, {})).text(), 'upstream');
  assert.equal(wrapped.email, upstream.email);
  assert.equal(wrapped.scheduled, upstream.scheduled);
});
test('unauthenticated, wrong-secret and expired probes cannot query storage', async () => {
  const e = env();
  e.db.prepare = () => { throw new Error('must not access'); };
  for (const request of [new Request('https://example.com' + path), await signed('wrong'), await signed(e.jwt_secret, -300)]) {
    assert.equal((await wrapped.fetch(request, e, {})).status, 404);
  }
});
test('real read probes pass and missing/broken storage fails closed', async () => {
  const e = env();
  assert.equal((await wrapped.fetch(await signed(e.jwt_secret), e, {})).status, 200);
  delete e.db;
  let response = await wrapped.fetch(await signed(e.jwt_secret), e, {});
  assert.equal(response.status, 503);
  assert.equal((await response.json()).checks.db, false);
  e.kv.get = async () => { throw new Error('private storage error'); };
  response = await wrapped.fetch(await signed(e.jwt_secret), e, {});
  assert.equal((await response.text()).includes('private storage error'), false);
});
test('upstream cannot silently remove the email handler', () => {
  assert.throws(() => withDeploymentHealth({ fetch() {}, scheduled() {} }));
});
