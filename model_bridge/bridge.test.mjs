import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { safeMessage } from "./index.mjs";

const bridgeScript = fileURLToPath(new URL("./index.mjs", import.meta.url));

test("bridge multiplexes JSONL requests by ID and never returns credentials", async (t) => {
	const directory = await fs.mkdtemp(path.join(os.tmpdir(), "paper-manager-bridge-"));
	t.after(() => fs.rm(directory, { recursive: true, force: true }));
	const authFile = path.join(directory, "model_auth.json");
	const contextFile = path.join(directory, "pi_auth_context.json");
	await fs.writeFile(authFile, JSON.stringify({ profiles: { local: { openai: {
		type: "oauth",
		access: "synthetic-credential-a",
		refresh: "synthetic-credential-b",
	} } } }));
	const child = spawn(process.execPath, [bridgeScript, authFile, contextFile], { stdio: ["pipe", "pipe", "ignore"] });
	t.after(() => child.kill());
	const lines = readline.createInterface({ input: child.stdout });
	const output = lines[Symbol.asyncIterator]();
	const frames = [
		{ id: "runtime", method: "runtime.status" },
		{ id: "auth", method: "auth.status", params: { profile_id: "local", provider: "openai" } },
		{ id: "unknown", method: "unsupported.method" },
	];
	child.stdin.end(`${frames.map((frame) => JSON.stringify(frame)).join("\n")}\n`);
	const responses = await Promise.all(frames.map(async () => {
		const line = await output.next();
		assert.equal(line.done, false);
		return JSON.parse(line.value);
	}));
	await once(child, "close");
	const byId = new Map(responses.map((frame) => [frame.id, frame]));
	assert.equal(byId.get("runtime").result.version, "0.99.1");
	assert.equal(byId.get("runtime").result.installed, true);
	assert.deepEqual(byId.get("auth").result, { authenticated: true });
	assert.equal(byId.get("unknown").ok, false);
	const serialized = JSON.stringify(responses);
	assert.equal(serialized.includes("synthetic-credential-a"), false);
	assert.equal(serialized.includes("synthetic-credential-b"), false);
});

test("ChatGPT login persists and passes a stable installation UUID", { timeout: 15000 }, async (t) => {
	const directory = await fs.mkdtemp(path.join(os.tmpdir(), "paper-manager-chatgpt-login-"));
	t.after(() => fs.rm(directory, { recursive: true, force: true }));
	const authFile = path.join(directory, "model_auth.json");
	const contextFile = path.join(directory, "pi_auth_context.json");
	const child = spawn(process.execPath, [bridgeScript, authFile, contextFile], { stdio: ["pipe", "pipe", "ignore"] });
	t.after(() => child.kill());
	const lines = readline.createInterface({ input: child.stdout });
	const output = lines[Symbol.asyncIterator]();
	let nextRequestId = 0;
	const request = async (method, params = {}) => {
		const id = `request-${++nextRequestId}`;
		child.stdin.write(`${JSON.stringify({ id, method, params })}\n`);
		for (;;) {
			const line = await output.next();
			assert.equal(line.done, false, "bridge exited before responding");
			const frame = JSON.parse(line.value);
			if (frame.id !== id) continue;
			assert.equal(frame.ok, true, frame.error);
			return frame.result;
		}
	};

	const sessionId = "chatgpt-login-test";
	const started = await request("login.start", {
		profile_id: "local-profile",
		provider: "openai",
		session_id: sessionId,
	});
	assert.equal(started.status, "running");
	const context = JSON.parse(await fs.readFile(contextFile, "utf8"));
	assert.match(context.device_id, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);

	let status;
	for (let attempt = 0; attempt < 100; attempt++) {
		status = await request("login.status", { session_id: sessionId, after_event_id: 0 });
		if (status.events.some((event) => event.type === "auth_url" || event.type === "error")) break;
		await new Promise((resolve) => setTimeout(resolve, 20));
	}
	assert.equal(status.status, "running");
	assert.ok(status.events.some((event) => event.type === "auth_url"), JSON.stringify(status.events));
	assert.ok(!status.events.some((event) => event.type === "error"));

	const cancelled = await request("login.cancel", { session_id: sessionId });
	assert.equal(cancelled.cancelled, true);
	child.stdin.end();
	await once(child, "close");

	const persistedAgain = JSON.parse(await fs.readFile(contextFile, "utf8"));
	assert.equal(persistedAgain.device_id, context.device_id);
});

test("unsupported-region OAuth errors explain the provider rejection in Chinese", () => {
	const message = safeMessage(new Error(
		'OpenAI OAuth token request failed (403): {"error":{"code":"unsupported_country_region_territory"}}',
	));
	assert.match(message, /令牌交换时返回 403/);
	assert.match(message, /官方支持地区/);
	assert.match(message, /代理设置及网络出口/);
	assert.match(message, /unsupported_country_region_territory/);
});

test("error messages redact quoted credentials, callback codes and proxy passwords", () => {
	const marker = "synthetic-credential-alpha";
	for (const field of ["access_token", "refresh_token", "access", "refresh", "api_key", "password", "token", "code_verifier"]) {
		assert.ok(!safeMessage(JSON.stringify({ [field]: marker, error: "upstream failure" })).includes(marker), field);
		assert.ok(!safeMessage(`${field}='${marker}'`).includes(marker), field);
		assert.ok(!safeMessage(`${field}=${marker}`).includes(marker), field);
	}
	assert.ok(!safeMessage(`Bearer ${marker}`).includes(marker));
	assert.ok(!safeMessage(`http://user:${marker}@proxy.invalid`).includes(marker));
	assert.ok(!safeMessage(`http://127.0.0.1/callback?code=${marker}&state=${marker}`).includes(marker));
	assert.equal(safeMessage("model not found; HTTP 404"), "model not found; HTTP 404");
});

test("invalid proxy settings return JSONL errors while credential status remains available", { timeout: 10000 }, async (t) => {
	const directory = await fs.mkdtemp(path.join(os.tmpdir(), "paper-manager-proxy-error-"));
	t.after(() => fs.rm(directory, { recursive: true, force: true }));
	const child = spawn(process.execPath, [bridgeScript, path.join(directory, "auth.json"), path.join(directory, "context.json")], {
		stdio: ["pipe", "pipe", "ignore"],
		env: {
			...process.env,
			HTTPS_PROXY: "ftp://private-user:private-password@proxy.invalid",
			https_proxy: "ftp://private-user:private-password@proxy.invalid",
		},
	});
	t.after(() => child.kill());
	const lines = readline.createInterface({ input: child.stdout });
	const output = lines[Symbol.asyncIterator]();
	const params = { profile_id: "local", provider: "openai", session_id: "bad-proxy" };
	const frames = [
		{ id: "runtime", method: "runtime.status" },
		{ id: "auth", method: "auth.status", params },
		{ id: "login", method: "login.start", params },
	];
	child.stdin.end(`${frames.map((frame) => JSON.stringify(frame)).join("\n")}\n`);
	const responses = await Promise.all(frames.map(async () => {
		const line = await output.next();
		assert.equal(line.done, false);
		return JSON.parse(line.value);
	}));
	await once(child, "close");
	const byId = new Map(responses.map((frame) => [frame.id, frame]));
	assert.equal(byId.get("runtime").result.network.source, "unavailable");
	assert.deepEqual(byId.get("auth").result, { authenticated: false });
	assert.equal(byId.get("login").ok, false);
	assert.match(byId.get("login").error, /代理配置无效/);
	assert.ok(!JSON.stringify(responses).includes("private-user"));
	assert.ok(!JSON.stringify(responses).includes("private-password"));
});
