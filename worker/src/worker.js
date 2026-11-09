/**
 * Cloudflare Worker — receives the Telegram webhook and turns `/lagi`
 * messages into a GitHub repository_dispatch that runs the same bot
 * pipeline with cooldown bypassed (ticket T7).
 *
 * Free tier, zero dependencies, plain ES-module worker so it can be
 * deployed by pasting this file into the Cloudflare dashboard Quick Editor
 * (no local Node needed). Configuration lives ONLY in worker secrets/vars:
 *
 *   GITHUB_PAT        fine-grained PAT with Contents: write on the repo
 *   WEBHOOK_SECRET    the same value passed to Bot API setWebhook as
 *                     `secret_token`; Telegram then sends it on every
 *                     webhook POST in the X-Telegram-Bot-Api-Secret-Token
 *                     header and we verify it here before touching anything
 *   TELEGRAM_CHAT_ID  optional extra guard: ignore updates from other chats
 *   GITHUB_REPO       optional target repo, default below
 *
 * GitHub REST, one trivial subrequest per invocation (free plan allows 50).
 * Everything replies 2xx fast: Telegram keeps retrying until it gets a 2xx,
 * so anything we swallow must still answer 200.
 */
const DEFAULT_REPO = "IrfanFalz/bot-fakta-random";
const SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token";
const COMMAND_PREFIX = "/lagi";
const EVENT_TYPE = "send-fact"; // matches bot.yml `repository_dispatch.types`
const TRIGGER = "lagi";         // becomes client_payload.trigger -> on-demand path

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      // Health probe / dashboard happy-check; Telegram never calls this.
      return new Response("bot-fakta webhook up", { status: 200 });
    }

    // Verify the update really came from Telegram BEFORE parsing the body.
    // Fail CLOSED: without WEBHOOK_SECRET we cannot tell a real Telegram
    // webhook from an internet scan (code-review fix — previously an unset
    // secret degraded into accepting every POST). Provisioning must set the
    // same secret via setWebhook; until then the endpoint answers 403.
    const expected = env.WEBHOOK_SECRET || "";
    if (!expected) {
      return new Response("webhook secret not configured", { status: 403 });
    }
    const received = request.headers.get(SECRET_HEADER) || "";
    if (received !== expected) {
      return new Response("forbidden", { status: 403 });
    }

    const update = await readJson(request);
    const message = (update && update.message) || {};
    const text = typeof message.text === "string" ? message.text : "";
    const chatId = message.chat ? String(message.chat.id) : "";

    // Single personal bot: when the chat id is configured, swallow everyone
    // else instead of triggering on-demand sends for them.
    if (env.TELEGRAM_CHAT_ID && chatId !== String(env.TELEGRAM_CHAT_ID)) {
      return new Response("ok", { status: 200 });
    }

    if (text.startsWith(COMMAND_PREFIX)) {
      // Fire and forget — GitHub needs at most one quick subrequest, well
      // inside the free-plan budget. waitUntil keeps the +30s tail after the
      // response, so the fast 2xx ack below never holds the dispatch.
      const keepalive = ctx && typeof ctx.waitUntil === "function";
      const run = triggerActions(env);
      if (keepalive) {
        ctx.waitUntil(run);
      } else {
        run.catch(() => {});
      }
    }

    // Always 200: non-command updates and even bad JSON are swallowed so
    // Telegram stops retrying and the worker never crash-loops.
    return new Response("ok", { status: 200 });
  },
};

async function readJson(request) {
  try {
    const body = await request.json();
    return typeof body === "object" && body !== null ? body : null;
  } catch (_exc) {
    return null; // malformed body: swallowed with 200 above
  }
}

async function triggerActions(env) {
  const repo = env.GITHUB_REPO || DEFAULT_REPO;
  try {
    const response = await fetch(
      `https://api.github.com/repos/${repo}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_PAT}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "bot-fakta-worker", // GitHub API requires a UA
        },
        body: JSON.stringify({
          event_type: EVENT_TYPE,
          client_payload: { trigger: TRIGGER },
        }),
      }
    );
    // 204 = dispatched; 404 = wrong repo/token; 422 = dispatch rate-limited.
    // Nothing is retryable here that matters: the 15-minute poll fallback
    // inside the scheduled workflow run still delivers /lagi anyway.
    if (!response.ok) {
      console.warn(
        `dispatch ${response.status}: ${await response.clone().text().catch(() => "")}`
      );
    }
  } catch (exc) {
    console.warn(`dispatch failed: ${exc}`);
  }
}
