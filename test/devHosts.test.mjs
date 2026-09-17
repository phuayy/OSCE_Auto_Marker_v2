// Where the dev tooling listens and where it proxies to. One .env feeds the
// API and Vite; these are the rules that keep the two from disagreeing.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  DEFAULT_API_HOST,
  DEFAULT_API_PORT,
  DEFAULT_DEV_SERVER_HOST,
  DEFAULT_DEV_SERVER_PORT,
  DEFAULT_PREVIEW_PORT,
  apiProxyTarget,
  connectableHost,
  devServerBind,
} from '../scripts/dev-hosts.mjs';

test('the proxy dials loopback when the API binds every interface', () => {
  // 0.0.0.0 is a bind address, not one a client can connect to.
  assert.equal(connectableHost('0.0.0.0'), DEFAULT_API_HOST);
  assert.equal(connectableHost('::'), DEFAULT_API_HOST);
  assert.equal(connectableHost(''), DEFAULT_API_HOST);
  assert.equal(connectableHost(undefined), DEFAULT_API_HOST);
});

test('a specific bind address is dialled as given', () => {
  assert.equal(connectableHost('192.168.1.20'), '192.168.1.20');
  assert.equal(connectableHost('osce.internal'), 'osce.internal');
  // IPv6 literals need brackets in a URL; already-bracketed ones stay put.
  assert.equal(connectableHost('fe80::1'), '[fe80::1]');
  assert.equal(connectableHost('[fe80::1]'), '[fe80::1]');
});

test('the proxy target follows API_HOST and API_PORT', () => {
  assert.equal(apiProxyTarget({}), `http://${DEFAULT_API_HOST}:${DEFAULT_API_PORT}`);
  assert.equal(apiProxyTarget({ API_HOST: '0.0.0.0', API_PORT: '8790' }), 'http://127.0.0.1:8790');
  assert.equal(apiProxyTarget({ API_HOST: '10.0.0.5', API_PORT: ' 8787 ' }), 'http://10.0.0.5:8787');
});

test('a bad port falls back to the default rather than NaN', () => {
  assert.equal(apiProxyTarget({ API_PORT: 'eight' }), `http://${DEFAULT_API_HOST}:${DEFAULT_API_PORT}`);
  assert.equal(apiProxyTarget({ API_PORT: '0' }), `http://${DEFAULT_API_HOST}:${DEFAULT_API_PORT}`);
  assert.equal(devServerBind({ DEV_SERVER_PORT: '70000' }).port, DEFAULT_DEV_SERVER_PORT);
});

test('the dev server binds loopback unless told otherwise', () => {
  assert.deepEqual(devServerBind({}), {
    host: DEFAULT_DEV_SERVER_HOST,
    port: DEFAULT_DEV_SERVER_PORT,
    previewPort: DEFAULT_PREVIEW_PORT,
  });
  assert.deepEqual(devServerBind({ DEV_SERVER_HOST: '0.0.0.0', DEV_SERVER_PORT: '3000', PREVIEW_PORT: '3001' }), {
    host: '0.0.0.0',
    port: 3000,
    previewPort: 3001,
  });
});

test('API_HOST does not move the dev server, and DEV_SERVER_HOST does not move the proxy', () => {
  const env = { API_HOST: '0.0.0.0', DEV_SERVER_HOST: '192.168.1.20' };
  assert.equal(devServerBind(env).host, '192.168.1.20');
  assert.equal(apiProxyTarget(env), `http://127.0.0.1:${DEFAULT_API_PORT}`);
});
