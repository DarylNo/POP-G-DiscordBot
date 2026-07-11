import asyncio
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands, tasks

import config
import database
from cogs.profile import _fmt_duration

log = logging.getLogger("popg.llm")

# Single Ollama instance — qwen2.5:14b across both GPUs (port 11434)
OLLAMA_URL   = "http://192.168.1.126:11434"
OLLAMA_MODEL = "qwen2.5:14b"

OLLAMA_TIMEOUT = 600
OLLAMA_NUM_CTX = 16384  # chat default; analysis tasks pass num_ctx=None for full context

# Sampling params — Qwen 2.5 recommended defaults
OLLAMA_TEMPERATURE = 0.7
OLLAMA_TOP_P       = 0.8
OLLAMA_TOP_K       = 20

_DM_MAX_HISTORY_TURNS = int(os.getenv("DM_MAX_HISTORY_TURNS",      "20"))   # user+assistant pairs kept
_DM_HISTORY_TTL       = int(os.getenv("DM_HISTORY_TTL_SECONDS",    str(2 * 3600)))  # 2h idle expiry
_DM_RATE_PERIOD       = int(os.getenv("DM_RATE_PERIOD",             "8"))    # min seconds between DM replies
_DM_MAX_INPUT_CHARS   = int(os.getenv("DM_MAX_INPUT_CHARS",         "3000")) # cap single user message

_CH_MAX_HISTORY_TURNS = int(os.getenv("CHAT_MAX_HISTORY_TURNS", "40"))   # more turns for shared channels (includes ambient chatter)
_AMBIENT_MSG_MAX_CHARS = 500  # cap per absorbed channel message

# Live voice-session context injected into chats while recording is active
_VOICE_CTX_SEGMENTS  = 40    # most recent transcript lines shown
_VOICE_CTX_MAX_CHARS = 4000  # hard cap on injected transcript text

# Ambient memory: every N absorbed channel messages, extract memorable facts
# from the batch — the barkeep remembers things said in the bar even when
# nobody was talking to it.
_AMBIENT_MEMORY_EVERY = int(os.getenv("AMBIENT_MEMORY_EVERY", "50"))

_AMBIENT_MEMORY_SYSTEM = (
    "Extract facts worth remembering long-term from this Discord text-chat excerpt "
    'from "Past our Prime Gamers" (POPG). Focus on: schedules and availability '
    "(work shifts, vacations, 'on nights next week'), plans (game sessions, meetups), "
    "life events, strong preferences, inside jokes, purchases, and decisions. "
    "Ignore small talk and banter with no lasting information. "
    "Only include what is explicitly stated. "
    "Reply with a concise bullet list. If nothing notable, reply with exactly: NONE"
)

# Write-through in-memory caches over the DB tables.
# user_id → {"messages": list[dict], "last_active": datetime}
_dm_sessions: dict[int, dict] = {}
# channel_id → {"messages": list[dict], "last_active": datetime}
_ch_sessions: dict[int, dict] = {}
# user_id → datetime of last unprefixed chat (DM or guild mention) — rate limiting
_last_chat_at: dict[int, datetime] = {}

_TZ_TORONTO = ZoneInfo("America/Toronto")

_SUMMARY_SYSTEM = (
    'You are a recap writer for "Past our Prime Gamers" (POPG), a Discord server of older casual gamers. '
    "Write short, fun summaries of their voice chat sessions. "
    "CRITICAL: Only describe what is literally present in the transcript. "
    "Do NOT invent topics, games, jokes, or details that are not explicitly stated. "
    "If the transcript is short or sparse, write a short recap — do not pad or fabricate."
)

_SUMMARY_CHUNK_SYSTEM = (
    'You are extracting key points from part of a voice chat session from "Past our Prime Gamers" (POPG). '
    "List the main topics, games mentioned, notable moments or quotes. Reply with bullet points only. "
    "Only include points that are explicitly stated in the transcript."
)

_SUMMARY_COMBINE_SYSTEM = (
    'You are writing a final session recap for "Past our Prime Gamers" (POPG), '
    "a Discord server of older casual gamers. "
    "Only use the bullet points provided — do not invent or expand beyond what is listed."
)

# Per-chunk character limit for map phase (~1500 tokens, fits 8k ctx with system prompt)
_SUMMARY_CHUNK_CHARS = 6000

_SUMMARY_PROMPT = """\
Write a short, fun summary of this voice chat session based ONLY on what is in the transcript below.
Only mention topics, games, and moments that are explicitly present. Do NOT invent or expand on anything not stated.
If the session was brief or only one thing was said, just recap that one thing — keep it proportional to the actual content.
Keep it under 200 words and match the casual tone of the server.

TRANSCRIPT:
{transcript}"""

_SUMMARY_COMBINE_PROMPT = """\
These are bullet-point summaries of each part of a long voice chat session. \
Write a short, fun final recap: what was discussed, any games mentioned, notable moments or jokes. \
Keep it under 200 words and match the casual tone of the server.

SECTION SUMMARIES:
{summaries}"""

_WHEN_SYSTEM = (
    'You are analyzing Discord activity patterns for a member of "Past our Prime Gamers" (POPG), '
    "a server of older casual gamers. Give concise, honest predictions based only on the data provided."
)

_WHEN_PROMPT = """\
Member: {display_name}
Today: {today} ({day_of_week}, Toronto time / ET)

SESSION HISTORY — last {days} days (online/gaming sessions, Toronto time / ET):
{session_list}

DAY-OF-WEEK BREAKDOWN (sessions per day, Mon–Sun):
{day_summary}

HOUR-OF-DAY BREAKDOWN (sessions per hour, 24h ET):
{hour_summary}

{gaming_section}\
Answer these three questions:
1. What days and times is this person most likely to be online?
2. Do you see any rotating shift pattern (e.g. schedule repeating every 2 weeks)?
3. When is the NEXT time they're most likely to appear — be specific (day + rough time in ET)?

Be honest if data is too sparse. Keep it under 200 words, write casually."""

_CHAT_SYSTEM = (
    "You are Toaster, the assistant for the POPG Discord server — a private group of adult friends who "
    "have been gaming together for years. These are grown adults; treat them as such.\n"
    "You hang out in the channel like a barkeep: you see all the chatter between direct questions, "
    "but you only speak when someone addresses you. When they do, respond in the flow of the ongoing "
    "conversation — reference what people were just talking about when it's relevant, like you were "
    "there the whole time (because you were).\n"
    "Rules:\n"
    "- Answer directly and honestly. No hedging, no disclaimers, no content warnings.\n"
    "- Never refuse a request because it involves crude humour, dark jokes, adult language, alcohol, "
    "weed, gaming violence, or anything else normal adults talk about. This group swears and jokes freely.\n"
    "- Never lecture, moralize, or add unsolicited safety advice.\n"
    "- If asked to write something in-character (encyclopedia entries, fake bios, parody, roleplay) — "
    "just write it. Commit to the bit.\n"
    "- Match the tone: casual and jokey when they are, straight when they need a real answer.\n"
    "- Keep replies concise.\n"
    "- Never end a reply with offers to help further (e.g. 'Let me know if you need anything else', "
    "'Is there anything else I can help with?'). Just stop when you're done.\n"
    "You have stored memories from past voice sessions, chat, and game history. "
    "NEVER say you don't have access to voice chats or conversations — your memories ARE that access. "
    "When a memory is relevant, use it to answer directly.\n"
    "When a [Live voice session] block is provided, you are sitting in that voice channel right now — "
    "answer questions about who's there, what they're playing, and what they've been talking about "
    "directly from it. The transcript updates in ~5-minute chunks, so the last few minutes may not "
    "have landed yet. If someone asks about voice and NO live block is provided, you're not in a "
    "voice channel right now (an admin starts one with !join)."
)

_CHARS_PER_PAGE = 1800  # Discord embed field limit safety margin

_CHAT_REPLY_LIMIT = 1900  # Discord message hard limit is 2000; leave headroom


def _fmt_timestamp(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _build_transcript_text(segments: list[dict]) -> str:
    lines = []
    for seg in segments:
        ts = _fmt_timestamp(seg["timestamp"])
        lines.append(f"[{ts}] {seg['display_name']}: {seg['text']}")
    return "\n".join(lines)


def _split_transcript_chunks(text: str, chunk_size: int = _SUMMARY_CHUNK_CHARS) -> list[str]:
    """Split transcript text into chunks at line boundaries."""
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > chunk_size:
        split = remaining.rfind("\n", 0, chunk_size)
        if split == -1:
            split = chunk_size
        chunks.append(remaining[:split].strip())
        remaining = remaining[split:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def _summarize_transcript(segments: list[dict]) -> str:
    """Summarize a transcript using map-reduce for large sessions.

    Small sessions (fit in one context window) are summarized directly.
    Large sessions are split into chunks, each chunk bullet-pointed, then
    a final pass combines the bullets into a cohesive recap.
    """
    full_text = _build_transcript_text(segments)

    if len(full_text) <= _SUMMARY_CHUNK_CHARS:
        return await _ollama_analyse(
            _SUMMARY_PROMPT.format(transcript=full_text),
            system=_SUMMARY_SYSTEM,
        )

    chunks = _split_transcript_chunks(full_text)
    log.info("Transcript map-reduce: %d segments → %d chunks", len(segments), len(chunks))

    bullet_parts: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        try:
            bullets = await _ollama_analyse(
                f"Part {i} of {len(chunks)}:\n\n{chunk}",
                system=_SUMMARY_CHUNK_SYSTEM,
            )
            if bullets:
                bullet_parts.append(f"Part {i}:\n{bullets}")
        except Exception:
            log.warning("Map-reduce chunk %d/%d failed — skipping", i, len(chunks))

    if not bullet_parts:
        return ""

    return await _ollama_analyse(
        _SUMMARY_COMBINE_PROMPT.format(summaries="\n\n".join(bullet_parts)),
        system=_SUMMARY_COMBINE_SYSTEM,
    )


def _chunk_text(text: str, limit: int) -> list[str]:
    """Split text into chunks no longer than limit, preferring paragraph/line breaks.

    Unlike _paginate, this guarantees no chunk exceeds the limit even when a
    single line is longer than it (it hard-splits as a last resort).
    """
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        window = remaining[:limit]
        split = window.rfind("\n")
        if split == -1:
            split = window.rfind(" ")
        if split == -1:
            split = limit
        chunks.append(remaining[:split].rstrip())
        remaining = remaining[split:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks or ["(empty)"]


def _paginate(text: str, page_size: int = _CHARS_PER_PAGE) -> list[str]:
    # Hard-split any single line longer than a page so no page can exceed the limit
    lines: list[str] = []
    for line in text.splitlines():
        while len(line) > page_size:
            lines.append(line[:page_size])
            line = line[page_size:]
        lines.append(line)

    pages, current = [], []
    length = 0
    for line in lines:
        if length + len(line) + 1 > page_size and current:
            pages.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        pages.append("\n".join(current))
    return pages or ["(empty)"]


def _strip_message(m: dict) -> dict:
    """Reduce a stored message to the keys Ollama accepts (drops e.g. 'ambient')."""
    return {"role": m["role"], "content": m["content"]}


def _trim_session(messages: list[dict], max_items: int) -> list[dict]:
    """Trim to max_items, dropping oldest AMBIENT messages first so channel
    banter can't flush Toaster's actual conversations out of context."""
    if len(messages) <= max_items:
        return messages
    overflow = len(messages) - max_items
    dropped = 0
    kept: list[dict] = []
    for m in messages:
        if dropped < overflow and m.get("ambient"):
            dropped += 1
            continue
        kept.append(m)
    if dropped < overflow:
        kept = kept[overflow - dropped:]
    return kept


def _get_dm_session(user_id: int) -> dict:
    """Return the in-memory DM session for a user, loading from DB on cache miss.

    Evicts expired sessions (idle > _DM_HISTORY_TTL) and starts fresh. The DB
    row's last_updated is honored on load so a bot restart can't resurrect
    history the TTL would have expired.
    """
    now = datetime.now(timezone.utc)
    session = _dm_sessions.get(user_id)

    if session is None:
        row = database.get_dm_history_row(user_id)
        stored = row["messages"] if row else []
        last_active = now
        if row:
            try:
                last_active = datetime.fromisoformat(row["last_updated"])
            except (ValueError, TypeError):
                pass
        session = {"messages": stored, "last_active": last_active}
        _dm_sessions[user_id] = session

    if (now - session["last_active"]).total_seconds() > _DM_HISTORY_TTL:
        session["messages"] = []
        database.delete_dm_history(user_id)
    session["last_active"] = now

    return session


def _get_ch_session(channel_id: int) -> dict:
    """Return the in-memory channel chat session, loading from DB on cache miss."""
    now = datetime.now(timezone.utc)
    session = _ch_sessions.get(channel_id)

    if session is None:
        stored = database.get_channel_chat_history(channel_id)
        session = {"messages": stored, "last_active": now}
        _ch_sessions[channel_id] = session

    # No TTL — channel history persists indefinitely until !reset

    return session


async def _ollama_generate(
    prompt: str = "",
    system: str = "",
    *,
    messages: list[dict] | None = None,
    num_ctx: int | None = ...,  # type: ignore[assignment]
) -> str:
    """Call Ollama. num_ctx overrides OLLAMA_NUM_CTX for this call.
    Pass num_ctx=None to let Ollama use the model's full native context window.
    Omit num_ctx (default sentinel) to use the configured OLLAMA_NUM_CTX.
    """
    if num_ctx is ...:  # sentinel — use the module default
        num_ctx = OLLAMA_NUM_CTX

    if messages is None:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

    options: dict = {
        "temperature": OLLAMA_TEMPERATURE,
        "top_p":       OLLAMA_TOP_P,
        "top_k":       OLLAMA_TOP_K,
    }
    if num_ctx is not None:
        options["num_ctx"] = num_ctx

    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            f"{OLLAMA_URL}/api/chat",
            json={"model": OLLAMA_MODEL, "messages": messages,
                  "stream": False, "options": options},
            timeout=aiohttp.ClientTimeout(total=OLLAMA_TIMEOUT),
        )
        resp.raise_for_status()
        data = await resp.json()
    return data.get("message", {}).get("content", "").strip()


# Background analysis (memory extraction, summaries) is serialized so a burst of
# chunk events or chat replies can't pile several requests onto Ollama at once
# and starve interactive !chat/DM generations.
_analysis_lock = asyncio.Lock()


async def _ollama_analyse(
    prompt: str = "",
    system: str = "",
    *,
    messages: list[dict] | None = None,
    num_ctx: int | None = None,
) -> str:
    """Analysis tasks — same instance, unlimited context by default, one at a time."""
    async with _analysis_lock:
        return await _ollama_generate(prompt, system, messages=messages, num_ctx=num_ctx)


_SEARCH_INTENT_SYSTEM = (
    "You decide whether to search the web before answering.\n"
    "Reply NOOP for casual conversation: banter, jokes, opinions, questions about "
    "server members, past gaming sessions, or anything the ongoing chat itself answers. "
    "Most barroom chatter needs no search.\n"
    "Reply SEARCH when external, current facts would clearly improve the answer:\n"
    "- Anything about a specific game (weapons, builds, tier lists, strategies, meta, updates, DLC, servers)\n"
    "- Prices, availability, release dates, store listings\n"
    "- Current events, news, sports scores, weather\n"
    "- Software, apps, hardware — versions, compatibility, errors, drivers\n"
    "- Factual lookups where your training data may be stale or wrong\n"
    "If you should search, reply with exactly: SEARCH: <concise search query>\n"
    "If this is definitively a timeless question, reply with exactly: NOOP"
)

# Phrases that always trigger a search, bypassing the intent check. Kept
# deliberately specific — loose words like "update"/"meta"/"cost" or a bare
# year match casual chat constantly and spam DDG with raw messages.
_SEARCH_FORCE_PATTERNS = re.compile(
    r"\b(patch notes|tier list|release date|server status|patch \d|new season|"
    r"just released|out now|coming out|latest (?:patch|update|version|news)|"
    r"how much (?:is|does|are|for)|price of|is \w+ (?:down|offline)|"
    r"when does \w+(?: \w+){0,4} (?:release|come out|start|end|drop)|"
    r"still worth (?:playing|buying)|dead game|best (?:build|loadout) (?:for|in))\b",
    re.IGNORECASE,
)

_SEARCH_FORCED_QUERY_MAX = 300  # cap raw-message queries sent to DDG

_SEARCH_INTENT_TIMEOUT = 120  # seconds — give the model time on slower hardware


async def _maybe_search(history: list[dict], user_text: str, *, intent_check: bool = True) -> str | None:
    """Ask the model if a web search would help. Returns the query string or None.

    Bypasses the Ollama intent check for messages that obviously need current info.
    intent_check=False skips the model-driven check (used when stored memories
    already cover the question) but keyword-forced searches still fire — a memory
    like 'X plays Battlefield' must not suppress 'when is the new Battlefield patch'.
    """
    # Fast path: keywords that always need a search
    if _SEARCH_FORCE_PATTERNS.search(user_text):
        log.debug("Search forced by keyword match for: %s", user_text[:80])
        return user_text[:_SEARCH_FORCED_QUERY_MAX]  # raw message as query, capped

    if not intent_check:
        return None

    intent_messages = [
        {"role": "system", "content": _SEARCH_INTENT_SYSTEM},
        *[_strip_message(m) for m in history[-4:]],
        {"role": "user", "content": user_text},
    ]
    try:
        result = await asyncio.wait_for(
            _ollama_generate(messages=intent_messages),
            timeout=_SEARCH_INTENT_TIMEOUT,
        )
    except Exception:
        log.warning("Search intent check failed — skipping search")
        return None
    if result.upper().startswith("SEARCH:"):
        return result[7:].strip()
    return None


# Search stack: prefer the maintained `ddgs` package (multi-engine rotation);
# fall back to the legacy `duckduckgo_search` if that's what's installed.
_USING_DDGS = False
try:
    from ddgs import DDGS
    try:
        from ddgs.exceptions import DDGSException as _SearchException
    except ImportError:
        _SearchException = Exception  # type: ignore[misc,assignment]
    _SEARCH_AVAILABLE = True
    _USING_DDGS = True
except ImportError:
    try:
        from duckduckgo_search import DDGS
        try:
            from duckduckgo_search.exceptions import DuckDuckGoSearchException as _SearchException
        except ImportError:
            _SearchException = Exception  # type: ignore[misc,assignment]
        _SEARCH_AVAILABLE = True
    except ImportError:
        DDGS = None  # type: ignore[misc,assignment]
        _SearchException = Exception  # type: ignore[misc,assignment]
        _SEARCH_AVAILABLE = False
        log.warning("no search library installed (ddgs / duckduckgo_search) — web search disabled")

# Engine ladder: each retry moves to a different provider, so one engine
# rate-limiting doesn't kill the search. Legacy lib has no engine choice.
_SEARCH_BACKENDS = ["auto", "duckduckgo", "bing", "brave", "google"] if _USING_DDGS else [None, None, None]
_SEARCH_TIMEOUT = 15

# Query cache — repeats (or two people asking the same thing) don't re-hit
# rate-limited providers. Keyed on the normalized query.
_SEARCH_CACHE: dict[str, tuple[datetime, str, str]] = {}  # key → (when, snippets, top_url)
_SEARCH_CACHE_TTL  = 600
_SEARCH_CACHE_MAX  = 200

# Circuit breaker — after full-ladder failures, stop hammering providers for a
# cooldown so chat stays fast and the ban (if any) can expire.
_SEARCH_BREAKER_SECS = 300
_search_state = {"consecutive_failures": 0, "down_until": None}


async def _web_search(query: str, max_results: int = 5) -> tuple[str, str]:
    """Search the web. Returns (formatted snippets, top result URL) — both empty on failure.

    Rock-solid path: per-attempt engine rotation, jittered backoff, result
    cache, and a circuit breaker so a dead provider can't stall every reply.
    """
    import random

    if not _SEARCH_AVAILABLE:
        return "", ""

    now = datetime.now(timezone.utc)
    key = " ".join(query.lower().split())

    cached = _SEARCH_CACHE.get(key)
    if cached and (now - cached[0]).total_seconds() < _SEARCH_CACHE_TTL:
        log.debug("search cache hit for: %s", query[:80])
        return cached[1], cached[2]

    if _search_state["down_until"] and now < _search_state["down_until"]:
        log.debug("search circuit breaker open — skipping search")
        return "", ""

    loop = asyncio.get_running_loop()
    for attempt, backend in enumerate(_SEARCH_BACKENDS):
        def _sync_search() -> list:
            kwargs: dict = {"max_results": max_results}
            if backend:
                kwargs["backend"] = backend
            return list(DDGS(timeout=_SEARCH_TIMEOUT).text(query, **kwargs))

        try:
            results = await loop.run_in_executor(None, _sync_search)
            if results:
                formatted = "\n".join(
                    f"• {r.get('title', '')}: {r.get('body', '')}" for r in results
                )
                top_url = results[0].get("href", "") or results[0].get("url", "")
                _SEARCH_CACHE[key] = (now, formatted, top_url)
                if len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
                    oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
                    _SEARCH_CACHE.pop(oldest, None)
                _search_state["consecutive_failures"] = 0
                _search_state["down_until"] = None
                log.info("search ok via %s (%d results) for: %s",
                         backend or "default", len(results), query[:80])
                return formatted, top_url
            log.debug("search returned 0 results via %s for: %s", backend or "default", query[:80])
        except _SearchException as exc:
            log.warning("search via %s failed (attempt %d): %s", backend or "default", attempt + 1, exc)
        except BaseException:
            log.warning("search via %s failed hard (attempt %d) for: %s",
                        backend or "default", attempt + 1, query[:80], exc_info=True)
        if attempt < len(_SEARCH_BACKENDS) - 1:
            await asyncio.sleep(min(2 ** attempt, 4) * random.uniform(0.5, 1.5))

    _search_state["consecutive_failures"] += 1
    if _search_state["consecutive_failures"] >= 2:
        _search_state["down_until"] = now + timedelta(seconds=_SEARCH_BREAKER_SECS)
        log.warning("search circuit breaker OPEN for %ds after %d full failures",
                    _SEARCH_BREAKER_SECS, _search_state["consecutive_failures"])
    return "", ""


_URL_RE = re.compile(r"https?://[^\s>\"']+", re.IGNORECASE)
_URL_FETCH_TIMEOUT = 10        # seconds per page
_URL_MAX_CHARS     = 4000      # truncate page text to keep context manageable
_URL_MAX_PER_MSG   = 2         # read at most this many URLs per message


async def _fetch_url(url: str) -> str:
    """Fetch a URL and return its readable text content, truncated to _URL_MAX_CHARS."""
    from bs4 import BeautifulSoup

    headers = {"User-Agent": "Mozilla/5.0 (compatible; POPGBot/1.0)"}
    timeout = aiohttp.ClientTimeout(total=_URL_FETCH_TIMEOUT)
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    log.warning("URL fetch got HTTP %d for %s", resp.status, url)
                    return ""
                content_type = resp.headers.get("Content-Type", "")
                if "html" not in content_type:
                    return ""
                html = await resp.text(errors="replace")
    except Exception:
        log.warning("URL fetch failed for %s", url)
        return ""

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:_URL_MAX_CHARS]


_MEMORY_EXTRACT_SYSTEM = (
    "Extract any facts worth remembering long-term from this exchange. "
    "Focus on: names, characters, events, decisions, ongoing storylines, preferences, "
    "inside jokes, game sessions, relationships. "
    "Reply with a concise bullet list. If nothing notable, reply with exactly: NONE"
)

_MEMORY_CONSOLIDATE_SYSTEM = (
    "Consolidate this memory list by merging related facts and removing redundant or "
    "outdated entries. Keep the most specific and useful details. "
    "Only rephrase and merge what is in the list — never invent new facts, names, or "
    "events that are not present in the input. "
    "Reply with a bullet list only."
)

_MEMORY_EXTRACT_TIMEOUT = 300  # seconds — long transcripts need more time
_MEMORY_MAX  = 150  # consolidate when list exceeds this
_MEMORY_TARGET = 60  # target size after consolidation
# Char limit per chunk when extracting memories from long transcripts
_MEMORY_CHUNK_CHARS = 8000

_TRANSCRIPT_MEMORY_SYSTEM = (
    "Extract memorable facts from this voice chat transcript for long-term memory. "
    "Focus on: who was present, what games were played, key events or decisions, "
    "D&D/RPG campaign events and character actions, notable quotes or moments, "
    "ongoing storylines or plans mentioned. "
    "Reply with a concise bullet list. If nothing memorable, reply with exactly: NONE"
)


async def _extract_memories(exchange: list[dict]) -> list[str]:
    """Extract memorable facts from a user+assistant exchange. Returns [] if nothing notable."""
    try:
        result = await asyncio.wait_for(
            _ollama_analyse(messages=[
                {"role": "system", "content": _MEMORY_EXTRACT_SYSTEM},
                *exchange,
            ]),
            timeout=_MEMORY_EXTRACT_TIMEOUT,
        )
    except Exception:
        log.warning("Memory extraction failed — skipping")
        return []
    if not result or result.strip().upper() == "NONE":
        return []
    lines = [line.lstrip("•-* 0123456789.)").strip() for line in result.splitlines()]
    return [l for l in lines if l and l.upper() != "NONE"]


async def _consolidate_memories(memories: list[str]) -> list[str]:
    """Ask the model to condense a large memory list.

    Consolidation is lossy and a bad generation can corrupt long-term memory,
    so the output is sanity-checked: an empty or implausibly small result is
    rejected and the originals are kept (trimmed to the most recent entries).
    """
    fallback = memories[-_MEMORY_TARGET:]
    bullet_list = "\n".join(f"• {m}" for m in memories)
    try:
        result = await asyncio.wait_for(
            _ollama_analyse(messages=[
                {"role": "system", "content": _MEMORY_CONSOLIDATE_SYSTEM},
                {"role": "user", "content": bullet_list},
            ]),
            timeout=_MEMORY_EXTRACT_TIMEOUT,
        )
    except Exception:
        log.warning("Memory consolidation failed — keeping existing")
        return fallback
    lines = [line.lstrip("•-* 0123456789.)").strip() for line in result.splitlines()]
    condensed = [l for l in lines if l]
    # Reject suspiciously lossy output — a valid consolidation of 150+ entries
    # shouldn't collapse below a handful of facts.
    if len(condensed) < max(10, len(memories) // 6):
        log.warning("Memory consolidation output too small (%d from %d) — rejected",
                    len(condensed), len(memories))
        return fallback
    return condensed


# Guards every read-modify-write of a memory scope. Without it, a slow
# consolidation (minutes of Ollama time) racing a chunk extraction or the
# daily stats refresh silently drops whichever write lands first.
_memory_write_lock = asyncio.Lock()


async def _merge_memories(scope_type: str, scope_id: int, new_facts: list[str]) -> None:
    """Append new facts to a scope's memory list, consolidating (with backup) when it grows too long.

    Exact-duplicate facts already in the pool are skipped — voice chunks and
    the daily stats refresh would otherwise re-add the same lines repeatedly.
    """
    if not new_facts:
        return
    async with _memory_write_lock:
        existing = database.get_memories(scope_type, scope_id)
        seen = set(existing)
        fresh = [f for f in new_facts if f not in seen]
        if not fresh:
            return
        combined = existing + fresh
        if len(combined) > _MEMORY_MAX:
            log.info("Memory list for %s/%d hit %d — consolidating", scope_type, scope_id, len(combined))
            # Keep a recoverable snapshot in case consolidation mangles the list
            database.save_memories(f"{scope_type}_backup", scope_id, combined)
            combined = await _consolidate_memories(combined)
        database.save_memories(scope_type, scope_id, combined)
        log.debug("Stored %d new memories for %s/%d (total: %d)", len(fresh), scope_type, scope_id, len(combined))


async def _update_memories(scope_type: str, scope_id: int, exchange: list[dict]) -> None:
    """Extract new facts from an exchange and persist them."""
    new_facts = await _extract_memories(exchange)
    await _merge_memories(scope_type, scope_id, new_facts)


_MEMORY_STOPWORDS = {
    "the", "and", "for", "was", "did", "what", "how", "are", "you", "can",
    "will", "that", "this", "with", "from", "they", "have", "about", "talk",
    "tell", "know", "who", "any", "all", "not", "get", "got", "its", "has",
    "him", "her", "his", "our", "their", "today", "just", "like", "also",
}


# Max memories injected into the system prompt per message. Beyond this the
# block is trimmed to relevant + most recent — keeps prompt-processing time and
# context usage sane as the memory pool grows.
_MEMORY_INJECT_MAX = 60


def _build_memory_block(scope_type: str, scope_id: int, relevant: list[str] | None = None) -> str:
    """Return a formatted memory injection string, or empty string if no memories.

    Memories in `relevant` are excluded — they're injected separately right
    before the question, and duplicating them here wastes context. Small pools
    are injected whole; large pools are trimmed to the most recent entries.
    """
    memories = database.get_memories(scope_type, scope_id)
    relevant_set = set(relevant or [])
    pool = [m for m in memories if m not in relevant_set]
    if not pool:
        return ""
    if len(pool) <= _MEMORY_INJECT_MAX:
        header = "\n\nStored memories (voice sessions, chat history, game activity):\n"
        selected = pool
    else:
        selected = pool[-_MEMORY_INJECT_MAX:]
        header = (
            f"\n\nStored memories — the {len(selected)} most recent of {len(memories)}:\n"
        )
    return header + "\n".join(f"• {m}" for m in selected)


# Cap on query-relevant memories injected near the question. Substring matching
# on common words used to return the entire pool for queries like "what games
# did we play" — whole-word matching + scoring + this cap keep it focused.
_MEMORY_RELEVANT_MAX = 12


def _find_relevant_memories(scope_type: str, scope_id: int, query: str) -> list[str]:
    """Return the most relevant memories for a query, ranked by whole-word overlap."""
    memories = database.get_memories(scope_type, scope_id)
    if not memories:
        return []
    words = {
        w.lower() for w in re.findall(r"\b\w{3,}\b", query)
        if w.lower() not in _MEMORY_STOPWORDS
    }
    if not words:
        return []
    scored: list[tuple[int, str]] = []
    for m in memories:
        m_words = {w.lower() for w in re.findall(r"\b\w{3,}\b", m)}
        hits = len(words & m_words)
        if hits:
            scored.append((hits, m))
    scored.sort(key=lambda x: -x[0])  # stable: ties keep pool order
    return [m for _, m in scored[:_MEMORY_RELEVANT_MAX]]


async def _extract_transcript_memories(
    session_id: int, segments: list[dict], *, mark_extracted: bool = True
) -> None:
    """Extract memorable facts from a voice transcript and add them to guild memory.

    Long transcripts are processed in chunks to avoid context/timeout limits.
    mark_extracted=False is used for mid-session chunk events so the session
    isn't prematurely marked done before the full transcript is processed.
    """
    if not segments:
        return
    transcript_text = _build_transcript_text(segments)
    chunks = _split_transcript_chunks(transcript_text, _MEMORY_CHUNK_CHARS)

    all_new_memories: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        try:
            result = await asyncio.wait_for(
                _ollama_analyse(messages=[
                    {"role": "system", "content": _TRANSCRIPT_MEMORY_SYSTEM},
                    {"role": "user",   "content": chunk},
                ]),
                timeout=_MEMORY_EXTRACT_TIMEOUT,
            )
        except Exception:
            log.warning("Transcript memory extraction failed for session %d chunk %d/%d",
                        session_id, i, len(chunks))
            continue
        if not result or result.strip().upper() == "NONE":
            continue
        lines = [line.lstrip("•-* 0123456789.)").strip() for line in result.splitlines()]
        all_new_memories.extend(l for l in lines if l and l.upper() != "NONE")

    if all_new_memories:
        await _merge_memories("guild", config.GUILD_ID, all_new_memories)
        log.info("Session %d: extracted %d transcript memories across %d chunk(s) (mark=%s)",
                 session_id, len(all_new_memories), len(chunks), mark_extracted)
    if mark_extracted:
        database.mark_transcript_memory_extracted(session_id)


async def _refresh_gaming_stats_memories() -> None:
    """Build per-user gaming stats from the DB and upsert them into guild memory."""
    all_users = database.get_all_users()
    stats_memories = []
    for user in all_users:
        uid = user["user_id"]
        stats = database.get_user_stats(uid)
        if not stats or not stats.get("top_games"):
            continue
        games_str = ", ".join(
            f"{g['game_name']} ({_fmt_duration(g['total_seconds'])})"
            for g in stats["top_games"][:3]
        )
        line = f"[Stats] {user['display_name']} plays: {games_str}"
        accomplices = database.get_accomplices(uid, limit=2)
        if accomplices:
            partners = ", ".join(a["display_name"] for a in accomplices)
            line += f"; often games with {partners}"
        stats_memories.append(line)
    if stats_memories:
        async with _memory_write_lock:
            existing = database.get_memories("guild", config.GUILD_ID)
            filtered = [m for m in existing if not m.startswith("[Stats]")]
            # Stats go at the FRONT: the large-pool trim keeps the most recent
            # entries, and ~20 daily [Stats] lines at the end would permanently
            # crowd genuine conversation memories out of that window.
            database.save_memories("guild", config.GUILD_ID, stats_memories + filtered)
        log.info("Refreshed %d gaming stats memories", len(stats_memories))


# Keep references to fire-and-forget tasks so they can't be GC'd mid-flight
_bg_tasks: set = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


class LLM(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # on_ready fires on every gateway reconnect — recovery must run once
        self._startup_recovery_done = False
        # Channels where ambient absorption is disabled (!barkeep off)
        self._barkeep_optout: set[int] = set(database.get_barkeep_optouts())
        # Absorbed lines awaiting ambient memory extraction
        self._ambient_pending: list[str] = []
        self._daily_stats_task.start()

    def cog_unload(self) -> None:
        self._daily_stats_task.cancel()

    @tasks.loop(hours=24)
    async def _daily_stats_task(self) -> None:
        await _refresh_gaming_stats_memories()

    @_daily_stats_task.before_loop
    async def _before_daily_stats(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """One-time startup recovery: backfill pending memories and finish
        sessions the bot crashed out of mid-summary (stuck in 'processing')."""
        if self._startup_recovery_done:
            return
        self._startup_recovery_done = True

        pending = database.get_transcripts_pending_memory()
        if pending:
            log.info("Processing %d transcript(s) pending memory extraction", len(pending))
            for row in pending:
                segments = database.get_transcript_segments(row["id"])
                _spawn(_extract_transcript_memories(row["id"], segments))

        stuck = database.get_transcripts_stuck_processing()
        if stuck:
            log.info("Re-dispatching %d transcript(s) stuck in 'processing'", len(stuck))
            for row in stuck:
                self.bot.dispatch("transcript_ready", row["id"])

    @commands.Cog.listener()
    async def on_transcript_ready(self, session_id: int) -> None:
        """Fired by voice_listener after Whisper finishes. Generate and store the LLM summary.

        Memory extraction happens per chunk (including the final one) via
        transcript_chunk_ready — re-extracting the full transcript here would
        store every fact twice.
        """
        segments = database.get_transcript_segments(session_id)
        if not segments:
            database.set_transcript_status(session_id, "failed")
            return

        try:
            summary = await _summarize_transcript(segments)
        except Exception:
            log.exception("Ollama request failed for session %d", session_id)
            database.set_transcript_status(session_id, "failed")
            return

        if not summary:
            log.warning("Session %d: Ollama returned empty summary", session_id)
            database.set_transcript_status(session_id, "failed")
            return

        database.set_transcript_summary(session_id, summary)
        database.mark_transcript_memory_extracted(session_id)
        log.info("Session %d: summary stored — use !recap to view.", session_id)

    @commands.Cog.listener()
    async def on_transcript_chunk_ready(self, session_id: int, segments: list[dict]) -> None:
        """Fired by voice_listener after each rolling chunk (and the final one)
        is transcribed. Extracts memories from the chunk immediately so the bot
        knows what's being discussed during an active session."""
        if segments:
            _spawn(_extract_transcript_memories(session_id, segments, mark_extracted=False))

    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.command(name="recap")
    async def recap(self, ctx: commands.Context, *args: str) -> None:
        """Show the LLM summary for the last (or a specific) voice session.

        Usage:
          !recap              — latest session
          !recap 5            — session #5
          !recap redo         — regenerate latest session's summary
          !recap redo 5       — regenerate session #5's summary
        """
        # Parse args: optional leading "redo" keyword, optional session id
        redo = False
        session_id = None
        for arg in args:
            if arg.lower() == "redo":
                redo = True
            elif arg.isdigit():
                session_id = int(arg)
            else:
                await ctx.send("Usage: `!recap [redo] [session_id]`")
                return

        session = database.get_transcript_session(session_id)
        if session is None:
            await ctx.send("No voice sessions recorded yet." if session_id is None else f"Session #{session_id} not found.")
            return

        status = session["status"]
        sid = session["id"]

        if status == "recording":
            await ctx.send(f"Session #{sid} is still recording.")
            return
        # redo overrides 'processing' — a crash mid-summary leaves that status
        # stuck forever, and redo is the manual way out
        if status == "processing" and not redo:
            await ctx.send(f"Session #{sid} is still being processed — check back in a moment. "
                           f"(If it seems stuck, `!recap redo {sid}` forces a retry.)")
            return

        # Force regeneration if redo requested or previous attempt failed
        if redo or status == "failed":
            segments = database.get_transcript_segments(sid)
            if not segments:
                await ctx.send(f"Session #{sid} has no transcript data to summarise.")
                return
            msg = "Regenerating" if redo else "Retrying"
            full_text = _build_transcript_text(segments)
            chunks = _split_transcript_chunks(full_text)
            chunk_note = f"{len(chunks)} chunks" if len(chunks) > 1 else f"{len(segments)} segments"
            await ctx.send(f"{msg} summary for session #{sid} — {chunk_note}, may take a moment")
            try:
                summary = await _summarize_transcript(segments)
            except Exception:
                log.exception("Ollama recap failed for session %d", sid)
                await ctx.send(f"Summary generation failed. Use `!transcript {sid}` to read the raw transcript.")
                return
            if not summary:
                await ctx.send(f"Ollama returned an empty response. Try `!transcript {sid}` to read it directly.")
                return
            database.set_transcript_summary(sid, summary)
        else:
            summary = session.get("summary") or "_No summary available._"

        started = session["started_at"][:16].replace("T", " ") + " UTC"
        embed = discord.Embed(
            title=f"Session #{sid} Recap — {session['channel_name']}",
            description=summary,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Recorded {started} · !transcript {sid} for full text · Past our Prime Gamers")
        await ctx.send(embed=embed)

    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.command(name="transcript")
    async def transcript(self, ctx: commands.Context, session_id: int = None) -> None:
        """Show the full attributed transcript for the last (or a specific) voice session."""
        session = database.get_transcript_session(session_id)
        if session is None:
            await ctx.send("No voice sessions recorded yet." if session_id is None else f"Session #{session_id} not found.")
            return

        sid = session["id"]
        segments = database.get_transcript_segments(sid)

        if not segments:
            await ctx.send(f"Session #{sid} has no transcript segments.")
            return

        transcript_text = _build_transcript_text(segments)
        pages = _paginate(transcript_text)
        started = session["started_at"][:16].replace("T", " ") + " UTC"
        header = f"Session #{sid} — {session['channel_name']} — {started}"

        if len(pages) == 1:
            embed = discord.Embed(
                title=header,
                description=f"```{pages[0]}```",
                color=discord.Color.greyple(),
            )
            embed.set_footer(text=f"{len(segments)} segments · Past our Prime Gamers")
            await ctx.send(embed=embed)
        else:
            for i, page in enumerate(pages, 1):
                embed = discord.Embed(
                    title=f"{header} ({i}/{len(pages)})",
                    description=f"```{page}```",
                    color=discord.Color.greyple(),
                )
                if i == len(pages):
                    embed.set_footer(text=f"{len(segments)} segments · Past our Prime Gamers")
                await ctx.send(embed=embed)

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="sessions", aliases=["recaps"])
    async def sessions(self, ctx: commands.Context) -> None:
        """List recent voice recording sessions."""
        rows = database.list_transcript_sessions(limit=10)
        if not rows:
            await ctx.send("No voice sessions recorded yet.")
            return

        lines = []
        for row in rows:
            started = row["started_at"][:16].replace("T", " ")
            status_icon = {"recording": "🔴", "processing": "⏳", "done": "✅", "failed": "❌"}.get(row["status"], "?")
            lines.append(f"{status_icon} **#{row['id']}** — {row['channel_name']} — {started} UTC")

        embed = discord.Embed(
            title="Recent Voice Sessions",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="!recap <id> · !transcript <id> · Past our Prime Gamers")
        await ctx.send(embed=embed)

    @commands.cooldown(1, 30, commands.BucketType.user)
    @commands.command(name="when")
    async def when(self, ctx: commands.Context, *, member_name: str = None) -> None:
        """Predict when a member will next be online based on their activity history."""
        from cogs.profile import _resolve_target
        target = await _resolve_target(ctx, member_name)
        if target is None:
            return

        sessions = database.get_session_history(target.id, days=60)
        if len(sessions) < 5:
            await ctx.send(
                f"Not enough activity data for **{target.display_name}** yet — "
                "need at least 5 recorded sessions over the past 60 days."
            )
            return

        status_msg = await ctx.send(f"Analysing **{target.display_name}**'s activity patterns... 🔍")

        # Build per-session list (cap at 120 lines to stay within token budget)
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        day_counts = [0] * 7
        hour_counts = [0] * 24
        game_totals: dict[str, int] = defaultdict(int)
        session_lines = []

        for s in sessions:
            try:
                dt = datetime.fromisoformat(s["started_at"]).replace(tzinfo=timezone.utc).astimezone(_TZ_TORONTO)
            except (ValueError, TypeError):
                continue
            ended = s.get("ended_at")
            try:
                duration_secs = int(
                    (datetime.fromisoformat(ended) - datetime.fromisoformat(s["started_at"])).total_seconds()
                ) if ended else 0
            except (ValueError, TypeError):
                duration_secs = 0

            day_counts[dt.weekday()] += 1
            hour_counts[dt.hour] += 1

            gname = s.get("game_name") or ""
            if s["session_type"] == "gaming" and gname:
                game_totals[gname] += duration_secs

            dur_str = _fmt_duration(duration_secs) if duration_secs >= 60 else ""
            label = f"gaming:{gname}" if s["session_type"] == "gaming" and gname else s["session_type"]
            ts = dt.strftime("%a %Y-%m-%d %H:%M")
            line = f"{ts} ET  {label}"
            if dur_str:
                line += f"  ({dur_str})"
            session_lines.append(line)

        # Cap history fed to LLM
        if len(session_lines) > 120:
            session_lines = session_lines[-120:]

        day_summary = "  ".join(
            f"{day_names[i]}:{day_counts[i]}" for i in range(7)
        )
        hour_summary_parts = []
        for h in range(24):
            if hour_counts[h]:
                hour_summary_parts.append(f"{h:02d}h:{hour_counts[h]}")
        hour_summary = "  ".join(hour_summary_parts) or "(no data)"

        if game_totals:
            top_games = sorted(game_totals.items(), key=lambda x: x[1], reverse=True)[:5]
            gaming_lines = [f"  {name}: {_fmt_duration(secs)}" for name, secs in top_games]
            gaming_section = "TOP GAMES PLAYED:\n" + "\n".join(gaming_lines) + "\n\n"
        else:
            gaming_section = ""

        today = datetime.now(_TZ_TORONTO)
        prompt = _WHEN_PROMPT.format(
            display_name=target.display_name,
            today=today.strftime("%Y-%m-%d"),
            day_of_week=today.strftime("%A"),
            days=60,
            session_list="\n".join(session_lines),
            day_summary=day_summary,
            hour_summary=hour_summary,
            gaming_section=gaming_section,
        )

        try:
            prediction = await _ollama_analyse(prompt, system=_WHEN_SYSTEM)
        except Exception:
            log.exception("!when: Ollama failed for user %d", target.id)
            await status_msg.edit(content="Prediction failed — Ollama is not responding. Try again in a moment.")
            return

        embed = discord.Embed(
            title=f"🔮 When will {target.display_name} be online?",
            description=prediction,
            color=discord.Color.teal(),
        )
        embed.set_footer(text=f"Based on {len(sessions)} sessions over 60 days · Past our Prime Gamers")
        try:
            await status_msg.delete()
        except discord.HTTPException:
            pass
        await ctx.send(embed=embed)

    async def _run_chat(
        self,
        dest,  # anything with .typing() and .send() — a Context or a channel
        *,
        scope_type: str,
        scope_id: int,
        session: dict,
        user_content: str,
        raw_text: str,
        max_turns: int,
        persist,  # callable(messages) — writes history to the right DB table
    ) -> None:
        """Shared chat pipeline for !chat and DMs: context gathering → Ollama →
        history persistence → chunked reply with timing → background memory extraction."""
        _t_start = datetime.now(timezone.utc)
        relevant_mems = _find_relevant_memories(scope_type, scope_id, raw_text)

        # URLs in the message → fetch pages; otherwise consider a web search
        urls = _URL_RE.findall(raw_text)[:_URL_MAX_PER_MSG]
        context_tag = None
        call_content = user_content
        if urls:
            pages = []
            for url in urls:
                page_text = await _fetch_url(url)
                if page_text:
                    pages.append(f"[Content of {url}]\n{page_text}")
            if pages:
                call_content = "\n\n".join(pages) + f"\n\n{user_content}"
                context_tag = "Read: " + ", ".join(urls)
        else:
            # Memories covering the question skip the model intent check, but
            # keyword-forced searches (patch notes, prices, ...) still fire.
            search_query = await _maybe_search(
                session["messages"], raw_text, intent_check=not relevant_mems
            )
            if search_query:
                snippets, top_url = await _web_search(search_query)
                if snippets:
                    # Deep-fetch the top result — snippets alone are often too
                    # thin to actually answer (patch notes, stats, guides)
                    page_extra = ""
                    if top_url:
                        page_text = await _fetch_url(top_url)
                        if page_text:
                            page_extra = f"\n\n[Top result ({top_url}):\n{page_text[:2500]}]"
                    call_content = (f"[Web search for '{search_query}':\n{snippets}{page_extra}]"
                                    f"\n\n{user_content}")
                    context_tag = f"Searched: {search_query}"
                else:
                    # Don't silently answer from stale training data — tell the
                    # model the search failed so it can say so
                    call_content = (
                        f"[NOTE: you tried to web-search '{search_query}' but the search "
                        f"service is currently unreachable. Answer from what you know and "
                        f"briefly mention you couldn't verify online.]\n\n{user_content}"
                    )
                    context_tag = "Search unavailable"

        system_with_memory = _CHAT_SYSTEM + _build_memory_block(scope_type, scope_id, relevant_mems)
        full_messages: list[dict] = [{"role": "system", "content": system_with_memory}]
        full_messages.extend(_strip_message(m) for m in session["messages"])
        # Live voice awareness — if a recording session is running, Toaster can
        # answer questions about it (who's there, what's being said)
        voice_ctx = self._live_voice_context()
        if voice_ctx:
            full_messages.append({"role": "system", "content": voice_ctx})
        # Inject relevant memories right before the question so they're impossible to miss
        if relevant_mems:
            full_messages.append({
                "role": "system",
                "content": "Relevant memories for this question:\n" + "\n".join(f"• {m}" for m in relevant_mems),
            })
        full_messages.append({"role": "user", "content": call_content})

        async with dest.typing():
            _t0 = datetime.now(timezone.utc)
            try:
                reply = await _ollama_generate(messages=full_messages)
            except Exception:
                log.exception("chat failed (%s/%d)", scope_type, scope_id)
                await dest.send("The LLM isn't responding right now. Try again in a moment.")
                return
            _elapsed = (datetime.now(timezone.utc) - _t0).total_seconds()

        if not reply:
            await dest.send("I didn't get a response. Try rephrasing.")
            return

        stored_reply = f"[{context_tag}] {reply}" if context_tag else reply
        session["messages"].append({"role": "user",      "content": user_content})
        session["messages"].append({"role": "assistant", "content": stored_reply})
        session["last_active"] = datetime.now(timezone.utc)
        session["messages"] = _trim_session(session["messages"], max_turns * 2)
        persist(session["messages"])

        # Timing subtext: LLM generation time, plus the total when context
        # gathering (search intent check, DDG, URL fetches) added real latency
        _total = (datetime.now(timezone.utc) - _t_start).total_seconds()
        if _total - _elapsed >= 1.0:
            timing = f"⏱ {_total:.1f}s (LLM {_elapsed:.1f}s)"
        else:
            timing = f"⏱ {_elapsed:.1f}s"

        chunks = _chunk_text(reply, _CHAT_REPLY_LIMIT)
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                await dest.send(f"{chunk}\n-# {timing}")
            else:
                await dest.send(chunk)

        # Extract and store memories in the background — don't make the user wait
        _spawn(_update_memories(scope_type, scope_id, [
            {"role": "user",      "content": user_content},
            {"role": "assistant", "content": reply},
        ]))

    @commands.cooldown(1, 15, commands.BucketType.user)
    @commands.command(name="chat", aliases=["ask"])
    async def chat(self, ctx: commands.Context, *, message: str = None) -> None:
        """Ask the local LLM a question, e.g. !chat what's a good co-op game?"""
        if not message:
            await ctx.send("Ask me something: `!chat <your question>`")
            return
        if len(message) > _DM_MAX_INPUT_CHARS:
            message = message[:_DM_MAX_INPUT_CHARS]
            log.warning("!chat from user %d truncated to %d chars", ctx.author.id, _DM_MAX_INPUT_CHARS)

        if ctx.guild is not None:
            channel_id = ctx.channel.id
            await self._run_chat(
                ctx,
                scope_type="guild", scope_id=config.GUILD_ID,
                session=_get_ch_session(channel_id),
                user_content=f"{ctx.author.display_name}: {message}",
                raw_text=message,
                max_turns=_CH_MAX_HISTORY_TURNS,
                persist=lambda msgs: database.save_channel_chat_history(channel_id, msgs),
            )
        else:
            user_id = ctx.author.id
            await self._run_chat(
                ctx,
                scope_type="dm", scope_id=user_id,
                session=_get_dm_session(user_id),
                user_content=message,
                raw_text=message,
                max_turns=_DM_MAX_HISTORY_TURNS,
                persist=lambda msgs: database.save_dm_history(user_id, msgs),
            )

    @chat.error
    async def chat_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Slow down a sec — try again in {error.retry_after:.0f}s.")

    def _live_voice_context(self) -> str | None:
        """Snapshot of the active voice recording session, or None if not recording.

        Injected into every chat so Toaster can answer questions about the
        ongoing voice channel: who's in it, what they're playing, and what
        they've been saying (transcribed so far).
        """
        vl = self.bot.cogs.get("VoiceListener")
        if vl is None:
            return None
        entry = getattr(vl, "_active", {}).get(config.GUILD_ID)
        if entry is None or entry.get("is_final"):
            return None

        import time as _time
        from cogs.voice_listener import _get_game_label

        elapsed = _time.monotonic() - entry["session_start"]
        channel = entry["vc"].channel
        channel_name = channel.name if channel else "unknown"
        parts = [f"[Live voice session] Recording '{channel_name}' — "
                 f"{_fmt_timestamp(elapsed)} in (session #{entry['session_id']})."]

        if channel:
            member_bits = []
            for m in channel.members:
                if m.bot:
                    continue
                label = _get_game_label(m)
                member_bits.append(
                    f"{m.display_name} ({'idle/chatting' if label == 'idle' else 'playing ' + label})"
                )
            if member_bits:
                parts.append("In the channel: " + ", ".join(member_bits))

        segments = database.get_transcript_segments(entry["session_id"])
        if segments:
            recent = segments[-_VOICE_CTX_SEGMENTS:]
            text = _build_transcript_text(recent)
            if len(text) > _VOICE_CTX_MAX_CHARS:
                text = text[-_VOICE_CTX_MAX_CHARS:]
            parts.append("Voice transcript so far (most recent lines):\n" + text)
        else:
            parts.append("No speech transcribed yet (first chunk still in progress).")

        return "\n".join(parts)

    def _absorb_channel_message(self, message: discord.Message) -> None:
        """Store a channel message as passive context (no reply).

        Also archives it permanently to chat_messages and batches it for
        ambient memory extraction — the barkeep remembers things said in the
        bar even when nobody was addressing it.
        """
        if message.channel.id in self._barkeep_optout:
            return
        text = message.content.strip()
        if not text:
            return
        line = f"{message.author.display_name}: {text[:_AMBIENT_MSG_MAX_CHARS]}"

        session = _get_ch_session(message.channel.id)
        session["messages"].append({"role": "user", "content": line, "ambient": True})
        session["messages"] = _trim_session(session["messages"], _CH_MAX_HISTORY_TURNS * 2)
        database.save_channel_chat_history(message.channel.id, session["messages"])

        # Permanent archive (replaces the old !log watch system)
        database.log_message(
            message_id=message.id,
            channel_id=message.channel.id,
            user_id=message.author.id,
            username=str(message.author),
            content=message.content,
            sent_at=message.created_at.replace(tzinfo=timezone.utc).isoformat(),
        )

        # Batch for ambient memory extraction
        self._ambient_pending.append(line)
        if len(self._ambient_pending) >= _AMBIENT_MEMORY_EVERY:
            batch, self._ambient_pending = self._ambient_pending, []
            _spawn(self._extract_ambient_memories(batch))

    async def _extract_ambient_memories(self, lines: list[str]) -> None:
        """Extract memorable facts from a batch of absorbed channel chatter."""
        try:
            result = await asyncio.wait_for(
                _ollama_analyse(messages=[
                    {"role": "system", "content": _AMBIENT_MEMORY_SYSTEM},
                    {"role": "user", "content": "\n".join(lines)},
                ]),
                timeout=_MEMORY_EXTRACT_TIMEOUT,
            )
        except Exception:
            log.warning("Ambient memory extraction failed — batch dropped")
            return
        if not result or result.strip().upper() == "NONE":
            return
        facts = [l.lstrip("•-* 0123456789.)").strip() for l in result.splitlines()]
        facts = [f for f in facts if f and f.upper() != "NONE"]
        if facts:
            await _merge_memories("guild", config.GUILD_ID, facts)
            log.info("Ambient memory: stored %d fact(s) from %d chat lines", len(facts), len(lines))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Unprefixed chat: DMs always; in guild channels when Toaster is
        @mentioned or the message replies to one of Toaster's messages.
        All other channel messages are absorbed silently as context."""
        if message.author.bot:
            return
        if message.content.startswith(config.PREFIX):
            return  # prefixed command — process_commands handles it

        if message.guild is not None:
            mentioned = self.bot.user in message.mentions
            ref = message.reference.resolved if message.reference else None
            replying_to_bot = (
                ref is not None
                and getattr(ref, "author", None) is not None
                and ref.author.id == self.bot.user.id
            )
            if not (mentioned or replying_to_bot):
                # Barkeep mode: absorb channel chatter as conversation context
                # without replying — when someone @s Toaster, it already knows
                # what everyone was just talking about.
                self._absorb_channel_message(message)
                return
            text = re.sub(rf"<@!?{self.bot.user.id}>", "", message.content).strip()
            if not text:
                # Bare ping — jump into the conversation from ambient context
                text = "(just pinged you with no message — chime in on the conversation)"
        else:
            text = message.content.strip()
            if not text:
                await message.channel.send("I can only read text — send me something to chat about!")
                return

        # Rate limit (shared across DMs and guild mentions)
        now = datetime.now(timezone.utc)
        last = _last_chat_at.get(message.author.id)
        if last is not None and (now - last).total_seconds() < _DM_RATE_PERIOD:
            wait = _DM_RATE_PERIOD - (now - last).total_seconds()
            await message.channel.send(f"Slow down — try again in {wait:.0f}s.")
            return
        _last_chat_at[message.author.id] = now
        if len(_last_chat_at) > 500:  # prune so the map can't grow unbounded
            cutoff = now - timedelta(hours=1)
            for uid in [u for u, t in _last_chat_at.items() if t < cutoff]:
                _last_chat_at.pop(uid, None)

        if len(text) > _DM_MAX_INPUT_CHARS:
            text = text[:_DM_MAX_INPUT_CHARS]
            log.warning("Chat from user %d truncated to %d chars", message.author.id, _DM_MAX_INPUT_CHARS)

        if message.guild is not None:
            channel_id = message.channel.id
            await self._run_chat(
                message.channel,
                scope_type="guild", scope_id=config.GUILD_ID,
                session=_get_ch_session(channel_id),
                user_content=f"{message.author.display_name}: {text}",
                raw_text=text,
                max_turns=_CH_MAX_HISTORY_TURNS,
                persist=lambda msgs: database.save_channel_chat_history(channel_id, msgs),
            )
        else:
            user_id = message.author.id
            await self._run_chat(
                message.channel,
                scope_type="dm", scope_id=user_id,
                session=_get_dm_session(user_id),
                user_content=text,
                raw_text=text,
                max_turns=_DM_MAX_HISTORY_TURNS,
                persist=lambda msgs: database.save_dm_history(user_id, msgs),
            )

    def _memory_scope(self, ctx: commands.Context) -> tuple[str, int]:
        """Guild context → server memories; DM → the user's own memories."""
        if ctx.guild is not None:
            return "guild", config.GUILD_ID
        return "dm", ctx.author.id

    def _can_manage_memories(self, ctx: commands.Context) -> bool:
        """Anyone can manage their own DM memories; guild memories are admin-only."""
        if ctx.guild is None:
            return True
        from cogs.admin import _is_admin
        return _is_admin(ctx)

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="memories")
    async def memories_cmd(self, ctx: commands.Context, page: int = 1) -> None:
        """Show what Toaster remembers (server memories in a channel, yours in a DM)."""
        scope_type, scope_id = self._memory_scope(ctx)
        mems = database.get_memories(scope_type, scope_id)
        if not mems:
            await ctx.send("No memories stored yet.")
            return

        per_page = 15
        pages = (len(mems) + per_page - 1) // per_page
        page = max(1, min(page, pages))
        start = (page - 1) * per_page
        lines = [f"`{start + i + 1}.` {m[:150]}" for i, m in enumerate(mems[start:start + per_page])]

        title = "Toaster's Server Memories" if scope_type == "guild" else "Toaster's Memories of You"
        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        footer = f"{len(mems)} total · page {page}/{pages}"
        if self._can_manage_memories(ctx):
            footer += " · !forget <number> to remove one"
        embed.set_footer(text=footer)
        await ctx.send(embed=embed)

    @commands.command(name="forget")
    async def forget(self, ctx: commands.Context, *, target: str = None) -> None:
        """Remove a memory by its !memories number, or by matching text."""
        if not target:
            await ctx.send("Usage: `!forget <number>` (from `!memories`) or `!forget <text to match>`")
            return
        if not self._can_manage_memories(ctx):
            await ctx.send("Only admins can manage server memories. (DM me to manage your own.)")
            return

        scope_type, scope_id = self._memory_scope(ctx)
        mems = database.get_memories(scope_type, scope_id)
        if not mems:
            await ctx.send("No memories stored.")
            return

        if target.isdigit():
            idx = int(target) - 1
            if not 0 <= idx < len(mems):
                await ctx.send(f"Number out of range — there are {len(mems)} memories (see `!memories`).")
                return
            removed = mems.pop(idx)
        else:
            matches = [(i, m) for i, m in enumerate(mems) if target.lower() in m.lower()]
            if not matches:
                await ctx.send("No memory matches that text.")
                return
            if len(matches) > 1:
                preview = "\n".join(f"`{i + 1}.` {m[:100]}" for i, m in matches[:5])
                await ctx.send(f"{len(matches)} memories match — be more specific or use the number:\n{preview}")
                return
            idx, removed = matches[0]
            mems.pop(idx)

        database.save_memories(scope_type, scope_id, mems)
        await ctx.send(f"Forgotten: _{removed[:200]}_")

    @commands.command(name="memoryrestore")
    async def memoryrestore(self, ctx: commands.Context) -> None:
        """Restore memories from the pre-consolidation backup snapshot."""
        if not self._can_manage_memories(ctx):
            await ctx.send("Only admins can restore server memories.")
            return
        scope_type, scope_id = self._memory_scope(ctx)
        backup = database.get_memories(f"{scope_type}_backup", scope_id)
        if not backup:
            await ctx.send("No backup snapshot available — one is saved automatically before each consolidation.")
            return
        current = len(database.get_memories(scope_type, scope_id))
        database.save_memories(scope_type, scope_id, backup)
        await ctx.send(f"Restored {len(backup)} memories from the pre-consolidation backup "
                       f"(replaced {current}).")

    @commands.guild_only()
    @commands.command(name="barkeep")
    async def barkeep(self, ctx: commands.Context, mode: str = None) -> None:
        """Admin: `!barkeep off` stops Toaster reading this channel; `!barkeep on` resumes."""
        from cogs.admin import _is_admin
        if not _is_admin(ctx):
            await ctx.send("Admin only.")
            return
        if mode is None or mode.lower() not in ("on", "off"):
            state = "OFF" if ctx.channel.id in self._barkeep_optout else "ON"
            await ctx.send(f"Barkeep listening in this channel is **{state}**. "
                           f"Use `!barkeep on` / `!barkeep off` to change it.")
            return
        if mode.lower() == "off":
            self._barkeep_optout.add(ctx.channel.id)
            database.add_barkeep_optout(ctx.channel.id)
            await ctx.send("Toaster is no longer reading this channel. "
                           "(@mentions still work; `!barkeep on` to resume.)")
        else:
            self._barkeep_optout.discard(ctx.channel.id)
            database.remove_barkeep_optout(ctx.channel.id)
            await ctx.send("Toaster is reading this channel again.")

    @commands.command(name="reset")
    async def reset_dm(self, ctx: commands.Context, scope: str = None) -> None:
        """Clear chat history. In DMs: your history + memories. In a server
        (admin): this channel's !chat history; `!reset all` also wipes the
        server-wide memory pool."""
        if ctx.guild is not None:
            from cogs.admin import _is_admin
            if not _is_admin(ctx):
                await ctx.send("Only admins can reset a channel's chat history.")
                return
            _ch_sessions.pop(ctx.channel.id, None)
            database.delete_channel_chat_history(ctx.channel.id)
            if scope and scope.lower() == "all":
                database.delete_memories("guild", config.GUILD_ID)
                database.delete_memories("guild_backup", config.GUILD_ID)
                await ctx.send("Channel chat history AND all server memories cleared — full fresh start!")
            else:
                await ctx.send("Channel chat history cleared. (Server memories kept — "
                               "`!reset all` wipes those too.)")
        else:
            _dm_sessions.pop(ctx.author.id, None)
            database.delete_dm_history(ctx.author.id)
            database.delete_memories("dm", ctx.author.id)
            database.delete_memories("dm_backup", ctx.author.id)
            await ctx.send("History and memories cleared — fresh start!")

    @when.error
    async def when_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Could not find that member. Try mentioning them with @.")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"This command is on cooldown. Try again in {error.retry_after:.0f}s.")

    @recap.error
    async def recap_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Usage: `!recap [session_id]`")

    @transcript.error
    async def transcript_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("Usage: `!transcript [session_id]`")


    @commands.guild_only()
    @commands.command(name="memorybuild", aliases=["rebuildmemory"])
    async def memorybuild(self, ctx: commands.Context, mode: str = None) -> None:
        """Admin: backfill memories from transcripts that haven't been processed.

        `!memorybuild full` wipes the guild memory pool and re-extracts from
        EVERY stored transcript — a true rebuild (a backup of the old pool is
        kept; `!memoryrestore` undoes it).
        """
        from cogs.admin import _is_admin
        if not _is_admin(ctx):
            await ctx.send("Admin only.")
            return

        if mode and mode.lower() == "full":
            old = database.get_memories("guild", config.GUILD_ID)
            if old:
                database.save_memories("guild_backup", config.GUILD_ID, old)
            database.save_memories("guild", config.GUILD_ID, [])
            reset = database.reset_memory_extraction()
            msg = await ctx.send(
                f"Full rebuild: cleared {len(old)} memories (backup kept — `!memoryrestore` undoes), "
                f"re-extracting from {reset} transcript(s)…"
            )
        else:
            msg = await ctx.send("Building memories from unprocessed data… this may take a while. "
                                 "(`!memorybuild full` re-extracts everything from scratch.)")

        try:
            pending = database.get_transcripts_pending_memory()
        except Exception as e:
            await msg.edit(content=f"DB error — try `docker compose up -d --build` to apply the latest migration. (`{e}`)")
            return

        await msg.edit(content=f"Processing {len(pending)} transcript(s) + game stats…")

        failed = 0
        for row in pending:
            try:
                segments = database.get_transcript_segments(row["id"])
                await _extract_transcript_memories(row["id"], segments)
            except Exception:
                log.exception("memorybuild: failed on session %d", row["id"])
                failed += 1

        try:
            await _refresh_gaming_stats_memories()
        except Exception:
            log.exception("memorybuild: game stats refresh failed")
            failed += 1

        memories = database.get_memories("guild", config.GUILD_ID)
        note = f" ({failed} error(s) — check logs)" if failed else ""
        await msg.edit(content=f"Done — {len(memories)} total memories in the guild pool.{note}")


def setup(bot: commands.Bot) -> None:
    bot.add_cog(LLM(bot))
