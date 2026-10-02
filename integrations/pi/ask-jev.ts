/**
 * ask-jev — "Ask Jev" tool for Pi.
 *
 * Jev (https://docs.typesafe.ai) is a small, fast classifier that answers
 * typed questions — noul (yes/no), choice, or score — with probabilities,
 * in well under a second per item. This tool reads files locally, ships
 * them to the homelab's jev /ask endpoint (one HTTPS call, batched and
 * fanned out server-side), and returns one compact line per file.
 *
 * The point: classify or scout files WITHOUT ingesting them into this
 * agent's context — "which files have TODOs", "rate the risk of each",
 * "which of these are tests" — for a fraction of the tokens a read costs.
 *
 * Config:
 *   JEV_ASK_URL         default https://gerry.gobeep.xyz:8790/ask
 *   JEV_ASK_ACCESS_KEY   optional Bearer token if the server has auth on
 */

import type { Usage } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";

const JEV_ASK_URL = process.env.JEV_ASK_URL ?? "https://gerry.gobeep.xyz:8790/ask";
const JEV_ASK_ACCESS_KEY = process.env.JEV_ASK_ACCESS_KEY;
const JEV_ASK_PRIVATE_BACKEND = process.env.JEV_ASK_PRIVATE_BACKEND ?? "local";
const JEV_ASK_KEV_URL = process.env.JEV_ASK_KEV_URL ?? "http://127.0.0.1:8009/v1/systemone";
const KEV_MAX_STATE_CHARS = 32_000; // Kev-0.8B is validated to ~8k tokens; matches jevkit/jev.py

const MAX_FILE_BYTES = 200 * 1024; // matches the server's per-state cap
const SERVER_BATCH = 100; // matches the server's MAX_ASK_ITEMS
const MAX_FILES = 300;
const WALK_LIMIT = 5000; // entries visited while expanding one pattern
const SKIP_DIRS = new Set(["node_modules", ".git"]);

type Answer = {
	type?: string;
	noul?: number;
	choice?: string;
	score?: number;
	confidence?: number;
	legend?: Record<string, string>;
	error?: string;
};

const Instructions = Type.Union([
	Type.String(),
	Type.Record(Type.String(), Type.Any(), {
		description: "question in one field, data it refers to in the others (refer to fields in backticks)",
	}),
]);

const NoulQuestion = Type.Object({
	type: Type.Literal("noul", { description: "yes/no question" }),
	instructions: Instructions,
	criteria: Type.Optional(
		Type.Record(Type.String(), Type.String(), { description: "optional: what a yes and a no mean" }),
	),
});

const ChoiceQuestion = Type.Object({
	type: Type.Literal("choice", { description: "picks one option from a set you define" }),
	instructions: Instructions,
	criteria: Type.Record(Type.String(), Type.Union([Type.String(), Type.Null()]), {
		description: "option -> rubric description; the answer is one of these keys",
	}),
});

const ScoreQuestion = Type.Object({
	type: Type.Literal("score", { description: "rates along a rubric you define" }),
	instructions: Instructions,
	criteria: Type.Array(Type.String(), {
		description: "ordered level descriptions, low to high (2-10 levels)",
		minItems: 2,
		maxItems: 10,
	}),
});

const Params = Type.Object({
	questions: Type.Record(Type.String(), Type.Union([NoulQuestion, ChoiceQuestion, ScoreQuestion]), {
		description:
			"typed questions, keyed by name; the same questions are asked about every file. " +
			"noul: yes/no -> probability. choice: picks from criteria keys. score: weighted position along the criteria rubric.",
	}),
	files: Type.Optional(
		Type.Array(Type.String(), {
			description:
				'paths or globs to classify, e.g. ["src/**/*.go", "pkg/util_test.go"]. Same questions asked of every file.',
		}),
	),
	state: Type.Optional(Type.String({ description: "inline text to ask about, when not using files" })),
	context: Type.Optional(
		Type.String({ description: "task goal, prepended to every state so answers stay goal-relative" }),
	),
	private: Type.Optional(
		Type.Boolean({
			description:
				"answer with open-weight Kev instead of TypeSafe: local Kev on this machine, else the homelab's (JEV_ASK_PRIVATE_BACKEND=homelab forces it). Content never leaves your machines; weaker on deep semantic questions. Defaults to $JEV_ASK_PRIVATE=1",
		}),
	),
});

export function globToRegex(pattern: string): RegExp {
	let re = "";
	for (let i = 0; i < pattern.length; i++) {
		const c = pattern[i];
		if (c === "*") {
			if (pattern[i + 1] === "*") {
				if (pattern[i + 2] === "/") {
					re += "(?:[^/]*/)*";
					i += 2;
				} else {
					re += ".*";
					i += 1;
				}
			} else {
				re += "[^/]*";
			}
		} else if (c === "?") {
			re += "[^/]";
		} else if ("\\^$.|+()[]{}".includes(c)) {
			re += "\\" + c;
		} else {
			re += c;
		}
	}
	return new RegExp(`^${re}$`);
}

/** Walk dir collecting files. Returns true if the entry budget ran out —
 * matches past that point are silently absent, so callers must warn. */
function walk(dir: string, out: string[], budget: { left: number }): boolean {
	if (--budget.left < 0) return true;
	let entries;
	try {
		entries = readdirSync(dir, { withFileTypes: true });
	} catch {
		return false;
	}
	let hitLimit = false;
	for (const e of entries) {
		if (budget.left < 0) {
			hitLimit = true;
			break;
		}
		const p = join(dir, e.name);
		if (e.isDirectory()) {
			if (SKIP_DIRS.has(e.name)) continue;
			if (walk(p, out, budget)) {
				hitLimit = true;
				break;
			}
		} else if (e.isFile()) {
			out.push(p);
		}
	}
	return hitLimit;
}

/** Deepest leading directory of a pattern that contains no metacharacters,
 * so a `src/...` prefix glob only walks from `src` instead of the whole tree. */
export function staticBase(absPattern: string): string {
	const absolute = isAbsolute(absPattern);
	const parts = absPattern.split("/");
	const base: string[] = absolute ? [""] : [];
	for (let i = absolute ? 1 : 0; i < parts.length; i++) {
		const part = parts[i];
		if (!part || /[*?]/.test(part)) break;
		base.push(part);
	}
	return base.join("/") || (absolute ? "/" : ".");
}

export function expandPattern(pattern: string): { files: string[]; hitLimit: boolean } {
	const abs = resolve(pattern);
	if (!/[*?]/.test(pattern)) {
		let st;
		try {
			st = statSync(abs);
		} catch {
			return { files: [], hitLimit: false };
		}
		if (st.isFile()) {
			return { files: [abs], hitLimit: false };
		}
		if (st.isDirectory()) {
			const files: string[] = [];
			const hitLimit = walk(abs, files, { left: WALK_LIMIT });
			return { files, hitLimit };
		}
		return { files: [], hitLimit: false };
	}
	const base = staticBase(abs);
	const candidates: string[] = [];
	const hitLimit = walk(base, candidates, { left: WALK_LIMIT });
	const re = globToRegex(abs);
	return { files: candidates.filter((f) => re.test(f)), hitLimit };
}

type Item = { id: string; state: string; truncated: boolean };
type Skipped = { path: string; reason: string };

function readFiles(paths: string[], context: string | undefined): { items: Item[]; skipped: Skipped[] } {
	const items: Item[] = [];
	const skipped: Skipped[] = [];
	for (const abs of paths) {
		const id = relative(process.cwd(), abs) || abs;
		try {
			const stat = statSync(abs);
			if (stat.size > 10 * 1024 * 1024) {
				skipped.push({ path: id, reason: `too large (${Math.round(stat.size / 1024)}KB)` });
				continue;
			}
			const buf = readFileSync(abs);
			if (buf.subarray(0, 1024).includes(0)) {
				skipped.push({ path: id, reason: "binary" });
				continue;
			}
			const truncated = buf.length > MAX_FILE_BYTES;
			const content = buf.subarray(0, MAX_FILE_BYTES).toString("utf8");
			const state =
				(context ? `[task: ${context}]\n\n` : "") + `[file: ${id}]\n\n${content}`;
			items.push({ id, state, truncated });
		} catch (e) {
			skipped.push({ path: id, reason: e instanceof Error ? e.message : String(e) });
		}
	}
	return { items, skipped };
}

async function askServer(
	items: Item[],
	questions: unknown,
	privateMode: boolean,
	signal?: AbortSignal,
): Promise<AskResponse> {
	const headers: Record<string, string> = { "Content-Type": "application/json" };
	if (JEV_ASK_ACCESS_KEY) headers.Authorization = `Bearer ${JEV_ASK_ACCESS_KEY}`;
	const timeout = AbortSignal.timeout(600_000);
	const res = await fetch(JEV_ASK_URL, {
		method: "POST",
		headers,
		body: JSON.stringify({ items: items.map(({ id, state }) => ({ id, state })), questions, private: privateMode }),
		signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
	});
	if (!res.ok) {
		throw new Error(`jev ${JEV_ASK_URL} -> ${res.status}: ${(await res.text()).slice(0, 300)}`);
	}
	return (await res.json()) as AskResponse;
}

type AskResponse = {
	answers: Record<string, Record<string, Answer>>;
	usage?: { input_tokens?: number; output_tokens?: number };
};

/** Private mode on this machine: one request per item to a local Kev server.
 * Undefined if nothing is listening, so the caller falls back to the homelab (also private). */
async function askLocalKev(items: Item[], questions: unknown, signal?: AbortSignal): Promise<AskResponse | undefined> {
	const answers: AskResponse["answers"] = {};
	let inputTokens = 0;
	let outputTokens = 0;
	for (const [i, item] of items.entries()) {
		const timeout = AbortSignal.timeout(120_000);
		try {
			const res = await fetch(JEV_ASK_KEV_URL, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ state: item.state.slice(0, KEV_MAX_STATE_CHARS), model: "kev-latest", questions }),
				signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
			});
			if (!res.ok) {
				answers[item.id] = { error: `kev ${res.status}: ${(await res.text()).slice(0, 300)}` } as Record<string, Answer>;
				continue;
			}
			const data = (await res.json()) as AskResponse & { answers: Record<string, Answer> };
			answers[item.id] = data.answers;
			if (item.state.length > KEV_MAX_STATE_CHARS) item.truncated = true;
			inputTokens += data.usage?.input_tokens ?? 0;
			outputTokens += data.usage?.output_tokens ?? 0;
		} catch (e) {
			if (signal?.aborted) throw e;
			if (i === 0 && (e as { cause?: { code?: string } }).cause?.code === "ECONNREFUSED") return undefined;
			answers[item.id] = { error: `kev: ${e instanceof Error ? e.message : String(e)}` } as Record<string, Answer>;
		}
	}
	return { answers, usage: { input_tokens: inputTokens, output_tokens: outputTokens } };
}

function renderAnswer(name: string, a: Answer): string {
	if (a.error) return `${name}: ERROR ${a.error}`;
	if (a.type === "noul") return `${name}: ${a.noul !== undefined && a.noul >= 0.5 ? "yes" : "no"} (${fmt(a.noul)})`;
	if (a.type === "choice") return `${name}: ${a.choice} (conf ${fmt(a.confidence)})`;
	if (a.type === "score") {
		const levels = Object.keys(a.legend ?? {}).length;
		const top = a.legend?.[String(Math.round(a.score ?? 0))] ?? "";
		return `${name}: ${fmt(a.score)}/${Math.max(levels - 1, 1)}${top ? ` ${top}` : ""}`;
	}
	return `${name}: ${JSON.stringify(a)}`;
}

function fmt(n: number | undefined): string {
	return n === undefined ? "?" : n.toFixed(2);
}

export default function askJevExtension(pi: ExtensionAPI) {
	pi.registerTool({
		name: "ask_jev",
		label: "Ask Jev",
		description:
			"Ask Jev — a fast, cheap classifier on the homelab — typed questions (noul yes/no, choice, score) about files or text, WITHOUT reading them into context. " +
			"Files are read locally and sent to the jev server; answers come back as one compact line per file with probabilities. " +
			"Use it instead of read/grep when you need judgments about content (does this file contain X? which of these are Y? rate the risk of Z?), " +
			"not the content itself — especially the same questions across many files at once via globs (*, ?, ** only — no braces or classes). " +
			"Handles up to 300 files per call.",
		promptSnippet: "Classify or judge files/text via the ask_jev tool instead of reading them when only judgments are needed.",
		promptGuidelines: [
			"Prefer ask_jev over read/grep when you need judgments about files (contains X? which files are Y? rate risk), not their contents.",
			"Phrase questions generically ('Does this file...') — the same questions are asked of every file in the batch.",
		],
		parameters: Params,
		async execute(_toolCallId, params, signal) {
			if (!params.files?.length && !params.state) {
				throw new Error("provide files (paths or globs) or an inline state to ask about");
			}

			let items: Item[] = [];
			let skipped: Skipped[] = [];

			if (params.files?.length) {
				const matched = new Set<string>();
				const unmatched: string[] = [];
				const walkLimitHits: string[] = [];
				for (const pattern of params.files) {
					const { files: found, hitLimit } = expandPattern(pattern);
					if (found.length === 0) unmatched.push(pattern);
					if (hitLimit) walkLimitHits.push(pattern);
					for (const f of found) matched.add(f);
				}
				const paths = [...matched].sort();
				if (paths.length === 0) {
					throw new Error(`no files matched: ${params.files.join(", ")}`);
				}
				if (paths.length > MAX_FILES) {
					throw new Error(
						`${paths.length} files matched (max ${MAX_FILES}) — narrow the glob, e.g. by directory or extension`,
					);
				}
				({ items, skipped } = readFiles(paths, params.context));
				if (unmatched.length) {
					skipped.push({ path: unmatched.join(", "), reason: "no files matched" });
				}
				if (walkLimitHits.length) {
					skipped.push({
						path: walkLimitHits.join(", "),
					reason: `walk limit (${WALK_LIMIT} entries) hit — matches may be incomplete, narrow the glob`,
				});
				}
			} else if (params.state) {
				const state =
					(params.context ? `[task: ${params.context}]\n\n` : "") + `[state]\n\n${params.state}`;
				items = [{ id: "state", state, truncated: false }];
			}

			const privateMode = params.private ?? process.env.JEV_ASK_PRIVATE === "1";
			const batches: Item[][] = [];
			for (let i = 0; i < items.length; i += SERVER_BATCH) {
				batches.push(items.slice(i, i + SERVER_BATCH));
			}
			const local =
				privateMode && JEV_ASK_PRIVATE_BACKEND === "local"
					? await askLocalKev(items, params.questions, signal)
					: undefined;
			const host = new URL(JEV_ASK_URL).host;
			const backend = local
				? "private: local Kev"
				: privateMode
					? `private: homelab Kev via ${host}${JEV_ASK_PRIVATE_BACKEND === "local" ? " (no local Kev running)" : ""}`
					: `jev ${host}`;
			const settled: PromiseSettledResult<AskResponse>[] = local
				? [{ status: "fulfilled", value: local }]
				: await Promise.allSettled(batches.map((b) => askServer(b, params.questions, privateMode, signal)));
			const batchErrors: { batch: Item[]; message: string }[] = [];
			const responses: AskResponse[] = [];
			for (let i = 0; i < settled.length; i++) {
				const s = settled[i];
				if (s.status === "fulfilled") {
					responses.push(s.value);
				} else {
					batchErrors.push({
						batch: batches[i],
						message: s.reason instanceof Error ? s.reason.message : String(s.reason),
					});
				}
			}
			if (responses.length === 0) {
				throw new Error(batchErrors.map((e) => e.message).join("; "));
			}

			const answers: Record<string, Record<string, Answer>> = {};
			let inputTokens = 0;
			let outputTokens = 0;
			for (const r of responses) {
				Object.assign(answers, r.answers);
				inputTokens += r.usage?.input_tokens ?? 0;
				outputTokens += r.usage?.output_tokens ?? 0;
			}
			for (const e of batchErrors) {
				for (const item of e.batch) {
					answers[item.id] = { error: e.message };
				}
			}

			const lines: string[] = [];
			lines.push(
				`${items.length} item${items.length === 1 ? "" : "s"} asked${
					skipped.length ? `, ${skipped.length} skipped` : ""
				} — ${backend}`,
			);
			for (const item of items) {
				const itemAnswers = answers[item.id];
				if (!itemAnswers) {
					lines.push(`${item.id} — no answer returned`);
					continue;
				}
				if (itemAnswers.error) {
					lines.push(`${item.id} — jev request failed: ${itemAnswers.error}`);
					continue;
				}
				const rendered = Object.entries(itemAnswers)
					.map(([name, a]) => renderAnswer(name, a))
					.join(" | ");
				lines.push(
					`${item.id} — ${rendered}${item.truncated ? " [truncated: only the start was evaluated]" : ""}`,
				);
			}
			for (const s of skipped) {
				lines.push(`skipped: ${s.path} (${s.reason})`);
			}
			if (inputTokens || outputTokens) {
				lines.push(`jev tokens: ${inputTokens} in / ${outputTokens} out`);
			}

			const usage: Usage = {
				input: inputTokens,
				output: outputTokens,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: inputTokens + outputTokens,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			};

			return {
				content: [{ type: "text", text: lines.join("\n") }],
				details: {
					items: items.map((i) => ({ path: i.id, answers: answers[i.id] })),
					skipped,
					jevUsage: { inputTokens, outputTokens },
				},
				usage,
			};
		},
	});
}
