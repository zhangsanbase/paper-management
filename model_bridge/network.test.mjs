import assert from "node:assert/strict";
import http from "node:http";
import net from "node:net";
import test from "node:test";
import { configureSubscriptionNetwork, parseWindowsProxySettings, resolveProxyConfiguration } from "./network.mjs";

test("explicit proxy variables take precedence over the Windows proxy and preserve NO_PROXY", () => {
	const config = resolveProxyConfiguration({
		http_proxy: "http://lowercase.invalid:8080", HTTP_PROXY: "http://uppercase.invalid:8080",
		HTTPS_PROXY: "http://https.invalid:8080", ALL_PROXY: "socks5://all.invalid:1080", NO_PROXY: "*",
	}, { platform: "win32", readWindowsProxy: () => { throw new Error("should not read Windows proxy"); } });
	assert.deepEqual(config, {
		source: "environment", httpProxy: "http://lowercase.invalid:8080/",
		httpsProxy: "http://https.invalid:8080/", noProxy: "*",
	});
	const socks = resolveProxyConfiguration({ ALL_PROXY: "socks5://all.invalid:1080" });
	assert.equal(socks.httpProxy, "socks5://all.invalid:1080");
	assert.equal(socks.httpsProxy, socks.httpProxy);
	assert.equal(socks.noProxy, "localhost,127.0.0.1,::1");
});

test("Windows manual proxy endpoints and bypass rules are inherited when the environment is unset", () => {
	const settings = parseWindowsProxySettings(`
HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings
    ProxyEnable    REG_DWORD    0x1
    ProxyServer    REG_SZ    http=127.0.0.1:7890;https=127.0.0.1:7891
    ProxyOverride    REG_SZ    <local>;*.example.test;intranet.test
`);
	const config = resolveProxyConfiguration({}, { platform: "win32", readWindowsProxy: () => settings });
	assert.deepEqual(config, {
		source: "windows-system", httpProxy: "http://127.0.0.1:7890/", httpsProxy: "http://127.0.0.1:7891/",
		noProxy: "localhost,127.0.0.1,::1,*.example.test,intranet.test",
	});
	const shared = resolveProxyConfiguration({}, {
		platform: "win32", readWindowsProxy: () => ({ enabled: true, server: "127.0.0.1:7890", bypass: "" }),
	});
	assert.equal(shared.httpProxy, "http://127.0.0.1:7890/");
	assert.equal(shared.httpsProxy, shared.httpProxy);
	assert.equal(resolveProxyConfiguration({}, {
		platform: "win32", readWindowsProxy: () => ({ ...settings, enabled: false }),
	}).source, "direct");
	assert.equal(resolveProxyConfiguration({ HTTP_PROXY: "" }, {
		platform: "win32", readWindowsProxy: () => settings,
	}).source, "windows-system");
});

test("invalid proxy settings fail without exposing credentials", () => {
	assert.throws(() => resolveProxyConfiguration({ HTTPS_PROXY: "ftp://private-user:private-password@proxy.invalid" }), (error) => {
		assert.match(error.message, /代理配置无效/);
		assert.ok(!error.message.includes("private-user"));
		assert.ok(!error.message.includes("private-password"));
		return true;
	});
});

async function mockProxy(t) {
	const connections = new Set();
	const tunnels = [];
	const forwarded = [];
	const bodies = [];
	const target = http.createServer(async (request, response) => {
		let body = "";
		for await (const chunk of request) body += chunk;
		bodies.push({ method: request.method, body });
		response.setHeader("Content-Type", "application/json");
		response.end('{"ok":true}');
	});
	const proxy = http.createServer((request, response) => {
		const destination = new URL(request.url);
		if (destination.origin !== `http://127.0.0.1:${target.address().port}`) {
			response.writeHead(403).end();
			return;
		}
		forwarded.push(request.url);
		const upstream = http.request(destination, { method: request.method }, (result) => {
			response.writeHead(result.statusCode, result.headers);
			result.pipe(response);
		});
		upstream.on("error", () => response.destroy());
		request.pipe(upstream);
	});
	proxy.on("connect", (request, client, head) => {
		tunnels.push(request.url);
		// The fixture accepts tunnels only to its own local target.
		if (request.url !== `127.0.0.1:${target.address().port}`) {
			client.end("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n");
			return;
		}
		const upstream = net.connect(target.address().port, "127.0.0.1", () => {
			client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
			if (head.length) upstream.write(head);
			client.pipe(upstream);
			upstream.pipe(client);
		});
		connections.add(upstream);
		upstream.on("error", () => client.destroy());
		upstream.on("close", () => connections.delete(upstream));
		client.on("error", () => upstream.destroy());
		client.on("close", () => upstream.destroy());
	});
	for (const server of [target, proxy]) {
		server.on("connection", (socket) => {
			connections.add(socket);
			socket.on("close", () => connections.delete(socket));
		});
		await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
	}
	t.after(async () => {
		for (const socket of connections) socket.destroy();
		await Promise.all([target, proxy].map((server) => new Promise((resolve) => server.close(resolve))));
	});
	return {
		targetUrl: `http://127.0.0.1:${target.address().port}/token`,
		proxyUrl: `http://127.0.0.1:${proxy.address().port}`,
		tunnels, forwarded, bodies,
	};
}

test("native fetch uses the selected proxy for token-style requests and respects NO_PROXY", { timeout: 10000 }, async (t) => {
	const fixture = await mockProxy(t);
	let network = configureSubscriptionNetwork({ HTTP_PROXY: fixture.proxyUrl, NO_PROXY: "" });
	try {
		assert.deepEqual(network.status, { source: "environment", proxy_configured: true });
		const response = await fetch(fixture.targetUrl, {
			method: "POST", body: "grant_type=authorization_code&code=synthetic-code", signal: AbortSignal.timeout(3000),
		});
		assert.deepEqual(await response.json(), { ok: true });
		assert.equal(fixture.forwarded.length + fixture.tunnels.length, 1);
		assert.deepEqual(fixture.bodies, [{ method: "POST", body: "grant_type=authorization_code&code=synthetic-code" }]);
	} finally {
		await network.close();
	}
	network = configureSubscriptionNetwork({ HTTP_PROXY: fixture.proxyUrl, NO_PROXY: "127.0.0.1" });
	try {
		const response = await fetch(fixture.targetUrl, { signal: AbortSignal.timeout(3000) });
		assert.equal(response.status, 200);
		await response.text();
		assert.equal(fixture.forwarded.length + fixture.tunnels.length, 1, "NO_PROXY should bypass the proxy");
	} finally {
		await network.close();
	}
});

test("native HTTPS fetch uses the Windows system proxy with no environment proxy", { timeout: 10000 }, async (t) => {
	const fixture = await mockProxy(t);
	const network = configureSubscriptionNetwork({}, {
		platform: "win32", readWindowsProxy: () => ({ enabled: true, server: fixture.proxyUrl, bypass: "" }),
	});
	try {
		assert.deepEqual(network.status, { source: "windows-system", proxy_configured: true });
		// A CONNECT rejection proves HTTPS reached the proxy without DNS lookup
		// or traffic to a real provider. Account authentication is not required.
		await assert.rejects(fetch("https://subscription-test.invalid/token", { signal: AbortSignal.timeout(3000) }));
		assert.deepEqual(fixture.tunnels, ["subscription-test.invalid:443"]);
	} finally {
		await network.close();
	}
});
