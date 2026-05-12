# Roadmap — Phase 2: Local LLM Integration

Technical plan for adding Ollama + Whisper to the POPG bot. No cloud services — everything runs on the same machine as the bot.

---

## Stack

| Component | Tool | Notes |
|---|---|---|
| LLM runtime | [Ollama](https://ollama.com/) | Local HTTP API on `localhost:11434` |
| Recommended model | Llama 3.1 8B or Mistral 7B | Good balance of quality and speed on consumer hardware |
| Speech-to-text | [openai-whisper](https://github.com/openai/whisper) | Runs fully locally, no API key needed |
| Voice capture | discord.py `WaveSink` | Built-in voice receive API |

---

## New Dependencies (Phase 2)

```
# requirements.txt additions
httpx>=0.27.0          # async HTTP client for Ollama API
openai-whisper>=20240930
PyNaCl>=1.5.0          # required for discord.py voice receive
```

---

## New Files

```
cogs/llm.py              !ask command, narrative summaries, game recommendations
cogs/voice_listener.py   Voice capture → Whisper → DB storage + !recap command
```

---

## Feature 1 — Chat / Q&A (`!ask`)

**Trigger:** `!ask <question>`  
**File:** `cogs/llm.py`

Flow:
1. Pull server context from `database.py`:
   - Top 5 members by each category (`get_leaderboard()`)
   - Active sessions (`get_active_sessions()`)
2. Serialize into a system prompt describing the POPG community state
3. POST to `http://localhost:11434/api/generate` with the user's question
4. Stream the response and send as a Discord message (edit-in-place as tokens arrive)

Prompt template (system section):
```
You are the POPG bot assistant for the "Past our Prime Gamers" Discord server.
Current server snapshot (UTC {timestamp}):
- Online members: {list}
- Members currently gaming: {list with game names}
- Members in voice: {list}
- This week's top gamers: {leaderboard}
Answer questions about the community concisely and with personality.
```

---

## Feature 2 — Narrative Player Summaries

**Trigger:** Appended to `!profile` output when Ollama is available  
**File:** `cogs/llm.py` → called from `cogs/profile.py`

Flow:
1. `get_user_stats(user_id)` → dict of all stats
2. Build a short data prompt: name, top games, hours breakdown
3. Ask Ollama for a 2-3 sentence "gamer personality" blurb
4. Append to the profile embed as a new field "AI Summary"

Gracefully degrade: if Ollama is unreachable, skip the field silently.

---

## Feature 3 — Game Recommendations

**Trigger:** `!recommend [@member]`  
**File:** `cogs/llm.py`

Flow:
1. Pull the requesting member's `game_stats` (their play history)
2. Pull top 3 games for all other POPG members
3. Ask Ollama: "Based on {member}'s history ({games}), which of these community favourites ({other_games}) might they enjoy and why?"
4. Return as an embed

---

## Feature 4 — Voice Transcription + Recap

**Trigger:** Automatic while bot is in a voice channel; `!recap` to view  
**File:** `cogs/voice_listener.py`

Flow:
1. Bot joins voice channel via `!join` (admin only initially)
2. `discord.WaveSink` captures per-member audio streams
3. On silence detection (or `!recap` command), flush audio buffers to WAV files
4. Run Whisper on each file: `whisper.transcribe(audio_path, model="base")`
5. Store transcriptions in a new `voice_transcripts` table:
   ```sql
   voice_transcripts (id, user_id, voice_channel_id, spoken_at, transcript TEXT)
   ```
6. `!recap [#channel]` → Ollama summarises the last N transcripts from that channel

**New DB table needed:**
```sql
CREATE TABLE IF NOT EXISTS voice_transcripts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(user_id),
    voice_channel_id INTEGER NOT NULL,
    spoken_at        TEXT    NOT NULL,
    transcript       TEXT    NOT NULL
);
```

---

## Ollama Helper Module

Create `llm_client.py` (not a cog, just a helper):

```python
# llm_client.py
import httpx

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.1:8b"

async def generate(prompt: str, system: str = "") -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(OLLAMA_URL, json={
            "model": MODEL,
            "prompt": prompt,
            "system": system,
            "stream": False,
        })
        resp.raise_for_status()
        return resp.json()["response"]

async def is_available() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            await client.get("http://localhost:11434")
        return True
    except Exception:
        return False
```

---

## Phase 2 Implementation Order

1. `llm_client.py` + `!ask` command (lowest risk, self-contained)
2. Narrative summaries on `!profile` (adds value to existing command)
3. `!recommend` game recommendations
4. Voice capture + Whisper transcription (most complex, requires bot to join VC)
5. `!recap` summarisation

---

## Ollama Setup (for the human running the bot)

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull a model
ollama pull llama3.1:8b

# Ollama runs as a background service automatically
# Verify it's up:
curl http://localhost:11434
```
