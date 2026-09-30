import crypto from "node:crypto";
import fs from "node:fs/promises";
import readline from "node:readline";
import { fileURLToPath, pathToFileURL } from "node:url";

import { createModels } from "@earendil-works/pi-ai";
import { anthropicProvider } from "@earendil-works/pi-ai/providers/anthropic";
import { githubCopilotProvider } from "@earendil-works/pi-ai/providers/github-copilot";
import { openaiProvider } from "@earendil-works/pi-ai/providers/openai";
import { configureSubscriptionNetwork } from "./network.mjs";

const providers = new Map([
	["openai", openaiProvider],
	["anthropic", anthropicProvider],
	["github-copilot", githubCopilotProvider],
]);

const [, , authFileArgument, contextFileArgument] = process.argv;
const authFile = authFileArgument;
const contextFile = contextFileArgument;
const instances = new Map();
const sessions = new Map();
const promptWaiters = new Map();
const profileLocks = new Map();
const providerLocks = new Map();
let persistenceQueue = Promise.resolve();
let outputQueue = Promise.resolve();
let networkStatus;
let networkError;


export function safeMessage(error) {
	const message = String(error instanceof Error ? error.message : error)
		.replace(/(https?|socks5?):\/\/[^/\s@]+@/gi, "$1://[已隐藏]@")
		.replace(/\bBearer\s+[^\s"'<>]+/gi, "Bearer [已隐藏]")
		.replace(/\b((?:access|refresh)(?:[_-]?token)?|api[_-]?key|authorization|password|secret|token|code_verifier)(["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s&,"'<>}\]]+)/gi, "$1$2[已隐藏]")
		.replace(/([?&](?:code|state)=)[^&\s"'<>]+/gi, "$1[已隐藏]")
		.replace(/\b(?:sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/g, "[已隐藏]")
		.replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, "[已隐藏]");
	if (message.includes("unsupported_country_region_territory")) {
		return "ChatGPT 订阅登录未完成：OpenAI 在令牌交换时返回 403（unsupported_country_region_territory）。请检查应用进程的代理设置及网络出口，并确认当前所在地在 OpenAI 官方支持地区；所在地受支持且网络设置正确仍报错时，请联系 OpenAI 支持。";
	}
	return message.slice(0, 500);
}

function writeFrame(frame, output = process.stdout) {
	outputQueue = outputQueue.catch(() => {}).then(() => new Promise((resolve, reject) => {
		const encoded = `${JSON.stringify(frame)}\n`;
		output.write(encoded, (error) => error ? reject(error) : resolve());
	}));
	return outputQueue;
}

async function readObject(path) {
	try {
		const value = JSON.parse(await fs.readFile(path, "utf8"));
		return value && typeof value === "object" && !Array.isArray(value) ? value : {};
	} catch (error) {
		if (error?.code === "ENOENT") return {};
		throw error;
	}
}

async function atomicWrite(path, value) {
	await fs.mkdir(new URL(".", pathToFileURL(path)), { recursive: true });
	const temporary = `${path}.${crypto.randomUUID()}.tmp`;
	try {
		await fs.writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
		await fs.rename(temporary, path);
	} finally {
		await fs.rm(temporary, { force: true });
	}
}

function withLock(locks, key, callback) {
	const previous = locks.get(key) ?? Promise.resolve();
	const next = previous.catch(() => {}).then(callback);
	locks.set(key, next);
	return next.finally(() => {
		if (locks.get(key) === next) locks.delete(key);
	});
}

function makeCredentialStore(profileId) {
	return {
		async read(providerId, options) {
			options?.signal?.throwIfAborted();
			const state = await readObject(authFile);
			return state.profiles?.[profileId]?.[providerId];
		},
		async list(options) {
			options?.signal?.throwIfAborted();
			const state = await readObject(authFile);
			return Object.entries(state.profiles?.[profileId] ?? {})
				.map(([providerId, credential]) => ({ providerId, type: credential.type }));
		},
		async modify(providerId, callback, options) {
			return withLock(providerLocks, providerId, () => withLock(profileLocks, profileId, () => {
				const next = persistenceQueue.then(async () => {
					options?.signal?.throwIfAborted();
					const state = await readObject(authFile);
					state.profiles ??= {};
					const credentials = state.profiles[profileId] ?? {};
					const updated = await callback(credentials[providerId]);
					options?.signal?.throwIfAborted();
					if (updated !== undefined) {
						credentials[providerId] = updated;
						state.profiles[profileId] = credentials;
						await atomicWrite(authFile, state);
					}
					return updated ?? credentials[providerId];
				});
				persistenceQueue = next.catch(() => {});
				return next;
			}));
		},
		async delete(providerId, options) {
			await withLock(providerLocks, providerId, () => withLock(profileLocks, profileId, () => {
				const next = persistenceQueue.then(async () => {
					options?.signal?.throwIfAborted();
					const state = await readObject(authFile);
					if (!state.profiles?.[profileId]?.[providerId]) return;
					delete state.profiles[profileId][providerId];
					if (!Object.keys(state.profiles[profileId]).length) delete state.profiles[profileId];
					await atomicWrite(authFile, state);
				});
				persistenceQueue = next.catch(() => {});
				return next;
			}));
		},
	};
}

function modelsFor(profileId, providerId) {
	if (!providers.has(providerId)) throw new Error("不支持此订阅服务商。");
	const key = `${profileId}:${providerId}`;
	if (!instances.has(key)) {
		const collection = createModels({
			credentials: makeCredentialStore(profileId),
			authContext: {
				async env() { return undefined; },
				async fileExists() { return false; },
			},
		});
		collection.setProvider(providers.get(providerId)());
		instances.set(key, collection);
	}
	return instances.get(key);
}

async function persistDeviceId() {
	return withLock(profileLocks, "__context__", async () => {
		const context = await readObject(contextFile);
		if (!context.device_id) {
			context.device_id = crypto.randomUUID();
			await atomicWrite(contextFile, context);
		}
		return context.device_id;
	});
}

function appendEvent(session, event) {
	const eventId = session.events.length ? session.events.at(-1).id + 1 : 1;
	session.events.push({ id: eventId, ...event });
	if (session.events.length > 100) session.events.splice(0, session.events.length - 100);
}

async function startLogin(profileId, providerId, sessionId) {
	if (sessions.has(sessionId)) return { session_id: sessionId, status: sessions.get(sessionId).status };
	const collection = modelsFor(profileId, providerId);
	const provider = collection.getProvider(providerId);
	if (!provider?.auth.oauth?.isSubscription) throw new Error("此服务商不支持订阅登录。");
	// pi-ai's LoginOptions.getDeviceId is synchronous. Persist the installation
	// UUID before starting login, then expose it through a synchronous callback.
	const deviceId = providerId === "openai" ? await persistDeviceId() : undefined;
	const session = {
		id: sessionId,
		profileId,
		providerId,
		status: "running",
		createdAt: Date.now(),
		events: [],
		prompts: new Map(),
		controller: new AbortController(),
		loginTask: undefined,
	};
	sessions.set(sessionId, session);
	session.loginTask = collection.login(providerId, "oauth", {
		signal: session.controller.signal,
		prompt: (request) => new Promise((resolve, reject) => {
			if (request.signal?.aborted) {
				reject(request.signal.reason ?? new Error("登录已取消。"));
				return;
			}
			const promptId = crypto.randomUUID();
			const abort = () => {
				session.prompts.delete(promptId);
				reject(request.signal.reason ?? new Error("登录已取消。"));
			};
			request.signal?.addEventListener("abort", abort, { once: true });
			session.prompts.set(promptId, {
				prompt: request,
				resolve: (answer) => {
					request.signal?.removeEventListener("abort", abort);
					resolve(answer);
				},
				reject: (error) => {
					request.signal?.removeEventListener("abort", abort);
					reject(error);
				},
			});
			const { signal: _signal, ...safePrompt } = request;
			appendEvent(session, { type: "prompt", prompt_id: promptId, prompt: safePrompt });
		}),
		notify: (event) => {
			const safeEvent = { ...event };
			if (typeof safeEvent.message === "string") safeEvent.message = safeMessage(safeEvent.message);
			appendEvent(session, safeEvent);
		},
	}, deviceId ? { getDeviceId: () => deviceId } : undefined)
		.then(() => {
			session.status = "completed";
			appendEvent(session, { type: "completed", message: "服务商登录成功。" });
		})
		.catch((error) => {
			session.status = session.controller.signal.aborted ? "cancelled" : "error";
			const message = session.status === "cancelled" ? "登录已取消。" : safeMessage(error);
			appendEvent(session, { type: session.status, message });
		})
		.finally(() => {
			clearTimeout(session.timeoutHandle);
			for (const waiter of session.prompts.values()) waiter.reject(new Error("登录已结束。"));
			session.prompts.clear();
		});
	session.timeoutHandle = setTimeout(() => {
			if (session.status === "running") session.controller.abort(new Error("登录授权超时，请重新开始登录。"));
		}, 15 * 60 * 1000);
	session.timeoutHandle.unref?.();
	return { session_id: sessionId, status: session.status };
}

async function sessionStatus(sessionId, afterEventId) {
	const session = sessions.get(sessionId);
	if (!session) return { session_id: sessionId, status: "expired", events: [], next_event_id: afterEventId };
	const events = session.events.filter((event) => event.id > afterEventId);
	return {
		session_id: session.id,
		profile_id: session.profileId,
		status: session.status,
		events,
		next_event_id: events.at(-1)?.id ?? afterEventId,
	};
}

async function handle(method, params = {}) {
	const { profile_id: profileId, provider: providerId } = params;
	if (networkError && ["login.start", "models.list", "models.complete"].includes(method)) throw networkError;
	switch (method) {
		case "runtime.status": {
			const packagePath = fileURLToPath(new URL("./node_modules/@earendil-works/pi-ai/package.json", import.meta.url));
			try {
				const data = await fs.readFile(packagePath, "utf8");
				const pkg = JSON.parse(data);
				return { installed: pkg.version === "0.99.1", version: pkg.version, node_version: process.versions.node, network: networkStatus };
			} catch {
				return { installed: false, version: null, node_version: process.versions.node, network: networkStatus };
			}
		}
		case "models.list": {
			const collection = modelsFor(profileId, providerId);
			const models = await collection.getAvailable(providerId);
			return models.map(({ id, name }) => ({ id, name: name || id }));
		}
		case "auth.status": {
			const credential = await makeCredentialStore(profileId).read(providerId);
			return { authenticated: credential?.type === "oauth" };
		}
		case "auth.logout": {
			for (const session of sessions.values()) {
				if (session.profileId === profileId && session.providerId === providerId && session.status === "running") {
					session.controller.abort();
					await session.loginTask;
				}
			}
			await modelsFor(profileId, providerId).logout(providerId);
			instances.delete(`${profileId}:${providerId}`);
			return { authenticated: false };
		}
		case "auth.remove-profile": {
			for (const providerIdForProfile of providers.keys()) {
				await handle("auth.logout", { profile_id: profileId, provider: providerIdForProfile });
			}
			return { removed: true };
		}
		case "login.start":
			return startLogin(profileId, providerId, params.session_id);
		case "login.status":
			return sessionStatus(params.session_id, params.after_event_id ?? 0);
		case "login.respond": {
			const session = sessions.get(params.session_id);
			const waiter = session?.prompts.get(params.prompt_id);
			if (!waiter) throw new Error("登录提示已过期，请重新开始登录。");
			const answer = String(params.answer ?? "");
			if (waiter.prompt.type === "select" && !waiter.prompt.options.some(({ id }) => id === answer)) {
				throw new Error("请选择有效的授权选项。");
			}
			session.prompts.delete(params.prompt_id);
			waiter.resolve(answer);
			return { accepted: true };
		}
		case "login.cancel": {
			const session = sessions.get(params.session_id);
			if (session?.status === "running") session.controller.abort();
			return { cancelled: true };
		}
		case "models.complete": {
			const collection = modelsFor(profileId, providerId);
			const model = collection.getModel(providerId, params.model);
			if (!model) throw new Error("所选模型不可用，请刷新模型列表并重新选择。");
			const messages = Array.isArray(params.messages) ? params.messages : [];
			const systemPrompt = messages.find(({ role }) => role === "system")?.content || "";
			const userContent = messages.filter(({ role }) => role !== "system").map(({ content }) => content).join("\n");
			const response = await collection.complete(model, {
				systemPrompt,
				messages: [{ role: "user", content: userContent, timestamp: Date.now() }],
			}, { temperature: 0.1 });
			if (response.stopReason === "error") throw new Error(response.errorMessage || "模型请求失败。");
			const content = response.content.filter(({ type }) => type === "text").map(({ text }) => text).join("").trim();
			if (!content) throw new Error("模型没有返回可用的 JSON 文本。");
			return { content };
		}
		default:
			throw new Error("不支持的模型适配请求。");
	}
}

export async function run(input = process.stdin, output = process.stdout) {
	let network;
	try {
		network = configureSubscriptionNetwork();
		networkStatus = network.status;
		networkError = undefined;
	} catch (error) {
		networkError = error;
		networkStatus = { source: "unavailable", proxy_configured: false, error: safeMessage(error) };
	}
	const lines = readline.createInterface({ input, crlfDelay: Infinity });
	const requests = new Set();
	for await (const line of lines) {
		const request = (async () => {
			let frame;
			try {
				frame = JSON.parse(line);
				const result = await handle(frame.method, frame.params);
				await writeFrame({ id: frame.id, ok: true, result }, output);
			} catch (error) {
				await writeFrame({ id: frame?.id ?? null, ok: false, error: safeMessage(error) }, output);
			}
		})();
		requests.add(request);
		void request.finally(() => requests.delete(request)).catch(() => {});
	}
	await Promise.allSettled(requests);
	for (const session of sessions.values()) session.controller.abort();
	await Promise.allSettled([...sessions.values()].map(({ loginTask }) => loginTask));
	await network?.close();
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
	await run();
}
