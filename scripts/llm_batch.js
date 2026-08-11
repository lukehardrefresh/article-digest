#!/usr/bin/env node
/**
 * LLM batch summarizer.
 * Usage: node llm_batch.js <input.json> <output.json>
 * input.json:  [{title, description, source}, ...]
 * output.json: [{index, tags, summary, skip}, ...]
 *
 * Backend selection via env var LLM_PROVIDER:
 *   - "zai"     (default)  Uses the z-ai-web-dev-sdk. No key needed if running inside Z.AI.
 *   - "openai"             Uses any OpenAI-compatible /chat/completions endpoint.
 *                          Requires OPENAI_API_KEY. Optional: OPENAI_BASE_URL, OPENAI_MODEL.
 *
 *   Example (OpenAI-compatible):
 *     LLM_PROVIDER=openai OPENAI_API_KEY=sk-... OPENAI_MODEL=gpt-4o-mini node llm_batch.js in.json out.json
 *
 *   Example (Z.AI, default):
 *     node llm_batch.js in.json out.json
 */
const fs = require('fs');

const PROVIDER = (process.env.LLM_PROVIDER || 'zai').toLowerCase();

const systemPrompt = `You are a senior analyst curating a weekly tech & policy digest.
For each article, you produce:
1. A 1-2 sentence summary (max 50 words). Concise, fact-dense, no marketing fluff.
2. Topic tags from this fixed set: AI, REG, FIN, BIZ, EMG, SEC, AUS
   - AI = artificial intelligence, ML, LLMs, agents, AI industry
   - REG = regulation, antitrust, privacy, policy, government
   - FIN = finance, fintech, markets, deals, funding, crypto
   - BIZ = business, M&A, leadership, strategy, product launches
   - EMG = emerging tech, biotech, climate tech, space, robotics
   - SEC = security, privacy breaches, surveillance, infosec
   - AUS = primarily about Australia or Australian companies/policy/people (use even if other tags apply; this is a region marker, not a topic)
   Pick 1-3 tags from AI/REG/FIN/BIZ/EMG/SEC, and ADD AUS if the article is primarily Australian. Max 4 tags total (e.g. ["AI","REG","AUS"]).
   If article doesn't fit any of AI/REG/FIN/BIZ/EMG/SEC, set SKIP=true.

Respond as a JSON array ONLY. No markdown fences, no prose before or after.
Object shape: {"index": <int>, "tags": ["AI","REG"], "summary": "...", "skip": false}

Skip articles that are clearly off-topic (lifestyle, recipes, celebrity gossip, pure product reviews with no business/policy angle, sports).`;

/**
 * Extract the first balanced top-level JSON array from `content`.
 * Tolerant of surrounding prose or markdown fences.
 */
function extractJsonArray(content) {
  const start = content.indexOf('[');
  if (start < 0) return null;
  let depth = 0, end = -1, inStr = false, esc = false;
  for (let i = start; i < content.length; i++) {
    const c = content[i];
    if (esc) { esc = false; continue; }
    if (c === '\\') { esc = true; continue; }
    if (c === '"') { inStr = !inStr; continue; }
    if (inStr) continue;
    if (c === '[') depth++;
    else if (c === ']') { depth--; if (depth === 0) { end = i; break; } }
  }
  if (end > 0) {
    try { return JSON.parse(content.slice(start, end + 1)); } catch { /* fall through */ }
  }
  try { return JSON.parse(content); } catch { return null; }
}

/**
 * Call the LLM once and return the raw content string. Throws on non-retryable error.
 * Retries with backoff are handled by the caller.
 */
async function callLLM(userPrompt) {
  if (PROVIDER === 'openai') {
    const baseUrl = (process.env.OPENAI_BASE_URL || 'https://api.openai.com/v1').replace(/\/+$/, '');
    const model = process.env.OPENAI_MODEL || 'gpt-4o-mini';
    const apiKey = process.env.OPENAI_API_KEY;
    if (!apiKey) throw new Error('OPENAI_API_KEY is required when LLM_PROVIDER=openai');
    const resp = await fetch(`${baseUrl}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        model,
        messages: [
          { role: 'system', content: systemPrompt },
          { role: 'user', content: userPrompt },
        ],
        temperature: 0.3,
      }),
    });
    if (!resp.ok) {
      const body = await resp.text();
      const err = new Error(`OpenAI HTTP ${resp.status}: ${body.slice(0, 200)}`);
      err.statusCode = resp.status;
      throw err;
    }
    const data = await resp.json();
    return data.choices?.[0]?.message?.content || '';
  }

  // Default: Z.AI SDK
  const ZAI = (await import('z-ai-web-dev-sdk')).default;
  const zai = await ZAI.create();
  const completion = await zai.chat.completions.create({
    messages: [
      { role: 'assistant', content: systemPrompt },
      { role: 'user', content: userPrompt },
    ],
    thinking: { type: 'disabled' },
  });
  return completion.choices?.[0]?.message?.content || '';
}

async function main() {
  const inPath = process.argv[2];
  const outPath = process.argv[3];
  if (!inPath || !outPath) {
    console.error('Usage: node llm_batch.js <input.json> <output.json>');
    process.exit(2);
  }
  const articles = JSON.parse(fs.readFileSync(inPath, 'utf-8'));

  let userLines = ['Summarize each article. Output JSON array only.\n'];
  articles.forEach((a, i) => {
    userLines.push(`[${i}] TITLE: ${a.title}`);
    if (a.description) userLines.push(`    DESC: ${a.description.slice(0, 400)}`);
    if (a.source) userLines.push(`    SRC: ${a.source}`);
    userLines.push('');
  });
  const userPrompt = userLines.join('\n');

  let lastErr = null;
  let parsed = null;
  for (let attempt = 1; attempt <= 5 && !parsed; attempt++) {
    try {
      const content = await callLLM(userPrompt);
      parsed = extractJsonArray(content);
      if (!parsed) {
        // JSON parse failure — don't retry; the model won't change on a backoff
        lastErr = `JSON parse failed; content preview: ${(content || '').slice(0, 200)}`;
        break;
      }
    } catch (apiErr) {
      lastErr = apiErr.message;
      const status = apiErr.statusCode;
      const isRate = status === 429 || (apiErr.message && apiErr.message.includes('429'));
      const wait = isRate ? (10000 * attempt) : (3000 * attempt);
      console.error(`  attempt ${attempt} failed: ${apiErr.message}; waiting ${wait}ms`);
      if (attempt < 5) {
        await new Promise(r => setTimeout(r, wait));
      }
    }
  }

  if (!parsed) {
    console.error('Falling back to default entries. Last error:', lastErr);
    parsed = articles.map((_, i) => ({ index: i, tags: ['BIZ'], summary: '', skip: false, _failed: true }));
  }

  fs.writeFileSync(outPath, JSON.stringify(parsed, null, 2));
  console.error(`OK: wrote ${parsed.length} entries to ${outPath} (failures: ${parsed.filter(e => e._failed).length})`);
}

main().catch(err => {
  console.error('FATAL:', err.message);
  process.exit(1);
});
