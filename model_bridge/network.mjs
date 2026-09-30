import { execFileSync } from "node:child_process";
import { Agent, EnvHttpProxyAgent, getGlobalDispatcher, setGlobalDispatcher } from "undici";

const internetSettingsKey = "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings";
const loopbackHosts = "localhost,127.0.0.1,::1";

export function parseWindowsProxySettings(output) {
	const values = {};
	for (const line of output.split(/\r?\n/)) {
		const match = line.match(/^\s*(ProxyEnable|ProxyServer|ProxyOverride)\s+REG_\w+\s+(.*?)\s*$/);
		if (match) values[match[1]] = match[2];
	}
	return {
		enabled: Number(values.ProxyEnable) === 1,
		server: values.ProxyServer ?? "",
		bypass: values.ProxyOverride ?? "",
	};
}

function readWindowsProxySettings() {
	try {
		return parseWindowsProxySettings(execFileSync("reg.exe", ["query", internetSettingsKey], {
			encoding: "utf8", timeout: 3000, windowsHide: true,
		}));
	} catch {
		return null;
	}
}

function proxyUrl(value, defaultProtocol = "http") {
	if (!value?.trim()) return "";
	try {
		const url = new URL(value.includes("://") ? value.trim() : `${defaultProtocol}://${value.trim()}`);
		if (!["http:", "https:", "socks:", "socks5:"].includes(url.protocol) || !url.hostname) throw new Error();
		// ProxyAgent decodes these fields when creating its authentication header.
		decodeURIComponent(url.username);
		decodeURIComponent(url.password);
		return url.href;
	} catch {
		// Never include the configured URI: it may contain a proxy password.
		throw new Error("订阅网络代理配置无效，请检查 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY 或 Windows 系统代理地址。");
	}
}

export function resolveProxyConfiguration(env = process.env, {
	platform = process.platform,
	readWindowsProxy = readWindowsProxySettings,
} = {}) {
	const readEnv = (name) => env[name.toLowerCase()] ?? env[name];
	const http = readEnv("HTTP_PROXY")?.trim() || undefined;
	const https = readEnv("HTTPS_PROXY")?.trim() || undefined;
	const all = readEnv("ALL_PROXY")?.trim() || undefined;
	const noProxy = readEnv("NO_PROXY");
	if ([http, https, all].some((value) => value !== undefined)) {
		const httpProxy = proxyUrl(http ?? all ?? "");
		const httpsProxy = proxyUrl(https ?? all ?? httpProxy);
		return { source: "environment", httpProxy, httpsProxy, noProxy: noProxy ?? loopbackHosts };
	}
	const system = platform === "win32" ? readWindowsProxy() : null;
	if (system?.enabled && system.server.trim()) {
		let httpProxy;
		let httpsProxy;
		if (system.server.includes("=")) {
			const addresses = {};
			for (const entry of system.server.split(";")) {
				const match = entry.match(/^\s*(http|https|socks)\s*=\s*(.*?)\s*$/i);
				if (match) addresses[match[1].toLowerCase()] = match[2];
			}
			const socksProxy = proxyUrl(addresses.socks ?? "", "socks5");
			httpProxy = proxyUrl(addresses.http ?? socksProxy);
			httpsProxy = proxyUrl(addresses.https ?? (socksProxy || httpProxy));
		} else {
			httpProxy = httpsProxy = proxyUrl(system.server);
		}
		const bypassHosts = system.bypass.split(";").map((host) => host.trim())
			.filter((host) => host && host !== "<local>");
		return {
			source: "windows-system", httpProxy, httpsProxy,
			noProxy: noProxy ?? [loopbackHosts, ...bypassHosts].join(","),
		};
	}
	return { source: "direct", httpProxy: "", httpsProxy: "", noProxy: noProxy ?? loopbackHosts };
}

export function configureSubscriptionNetwork(env = process.env, options) {
	const { source, ...proxyOptions } = resolveProxyConfiguration(env, options);
	const proxyConfigured = Boolean(proxyOptions.httpProxy || proxyOptions.httpsProxy);
	const dispatcher = proxyConfigured ? new EnvHttpProxyAgent(proxyOptions) : new Agent();
	const previous = getGlobalDispatcher();
	// pi-ai uses native fetch for OAuth and SDK calls. Undici shares its global
	// dispatcher with Node's fetch, including the minimum supported Node 22.19.
	setGlobalDispatcher(dispatcher);
	return {
		status: { source, proxy_configured: proxyConfigured },
		async close() {
			setGlobalDispatcher(previous);
			await dispatcher.close();
		},
	};
}
