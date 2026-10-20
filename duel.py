"""Monsterdle duels: /duel challenges someone, the bot posts a short clip of a
monster's sound, and whoever types the monster's name first wins. A "Hear more"
button swaps in a longer clip, the same steps the Monsterdle website uses.

The monster list and audio come straight from the public Monsterdle repo, so
new monsters added to the game show up here automatically."""

import asyncio
import io
import json
import random
import re
import time

import aiohttp
import discord
from discord import app_commands

MONSTERDLE_FILES = "https://raw.githubusercontent.com/ebwoo/monsterdle/main/"

CHALLENGE_TIMEOUT = 60       # seconds the opponent has to accept
DUEL_TIMEOUT = 180           # seconds before an unsolved duel ends

# --- Reading monsters.json (same rules as the website's lib/config.js) ---

DEFAULT_MODES = {
    "easy": {"label": "Easy Mode", "start": "beginning", "steps": [0.5, 1, 2, 3, 5, 8]},
    "hard": {"label": "Hard Mode", "start": "random", "steps": [1, 2, 4, 7, 11, 16]},
}


def name_from_file(path: str) -> str:
    """'audio/Pale_Crawler.mp3' -> 'Pale Crawler'"""
    stem = path.split("/")[-1].rsplit(".", 1)[0]
    return re.sub(r"\s+", " ", stem.replace("_", " ")).strip()


def normalize_config(raw: dict) -> dict:
    monsters = []
    for m in raw.get("monsters", []):
        entry = {"audio": m} if isinstance(m, str) else dict(m)
        entry["audio"] = entry.get("audio") or entry.get("spawn") or entry.get("theme")
        if not entry["audio"]:
            continue
        entry["name"] = entry.get("name") or name_from_file(entry["audio"])
        entry["aliases"] = entry.get("aliases") or []
        monsters.append(entry)
    modes = {k: m for k, m in (raw.get("modes") or {}).items()
             if isinstance(m, dict) and m.get("steps")}
    return {"monsters": monsters, "modes": modes or DEFAULT_MODES}


def start_style(config: dict, mode: str) -> str:
    start = config["modes"][mode].get("start")
    if start in ("beginning", "random"):
        return start
    return "beginning" if next(iter(config["modes"])) == mode else "random"


# --- Matching typed guesses to monsters (forgiving, like the website) ---

def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _edit_distance(a: str, b: str) -> int:
    """Typos between two strings: insert, delete, change, or swap two neighbours."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def _typo_allowance(length: int) -> int:
    return 2 if length >= 9 else 1 if length >= 5 else 0


def find_monster(config: dict, text: str):
    """The monster a message names, or None if it isn't a monster name at all.
    Ignores capitals, spaces and punctuation, makes a leading 'The' optional,
    accepts aliases, and forgives small typos."""
    typed = _normalize(text)
    if not typed or len(typed) > 40:
        return None
    best, best_dist = None, None
    for m in config["monsters"]:
        targets = {_normalize(m["name"])} | {_normalize(a) for a in m["aliases"]}
        targets |= {t[3:] for t in list(targets) if t.startswith("the") and len(t) > 3}
        for target in targets:
            dist = _edit_distance(typed, target)
            if dist <= _typo_allowance(len(target)) and (best_dist is None or dist < best_dist):
                best, best_dist = m, dist
    return best


# --- Cutting MP3 clips without re-encoding ---
# MP3 audio is a chain of small frames (about 1/40th of a second each), so a
# clip is just the frames between two times. No ffmpeg needed on the host.

_BITRATES = {
    1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],    # MPEG-1 layer III
    2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],         # MPEG-2/2.5 layer III
}
_SAMPLE_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def mp3_frames(data: bytes):
    """List of (start_byte, end_byte, seconds) for each audio frame in an MP3."""
    pos = 0
    if data[:3] == b"ID3" and len(data) >= 10:   # skip the tag block (title, artist, etc.)
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        pos = 10 + size + (10 if data[5] & 0x10 else 0)
    end = len(data) - (128 if data[-128:-125] == b"TAG" else 0)
    frames = []
    while pos + 4 <= end:
        b1, b2 = data[pos + 1], data[pos + 2]
        version, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
        br_index, sr_index = b2 >> 4, (b2 >> 2) & 3
        if (data[pos] != 0xFF or (b1 & 0xE0) != 0xE0 or version == 1 or layer != 1
                or br_index in (0, 15) or sr_index == 3):
            pos += 1   # not a frame start: look for the next one
            continue
        rate = _SAMPLE_RATES[version][sr_index]
        bitrate = _BITRATES[1 if version == 3 else 2][br_index] * 1000
        samples = 1152 if version == 3 else 576
        length = samples // 8 * bitrate // rate + ((b2 >> 1) & 1)
        if length < 4 or pos + length > end:
            break
        frame = data[pos:pos + length]
        # The first frame is often an info block (b"Xing"/b"Info") rather than sound.
        if frames or (b"Xing" not in frame[:64] and b"Info" not in frame[:64]):
            frames.append((pos, pos + length, samples / rate))
        pos += length
    return frames


def cut_clip(data: bytes, frames, start: float, length: float) -> bytes:
    """The audio from `start` to `start + length` seconds, as a playable MP3."""
    out, t = [], 0.0
    for i, (a, b, secs) in enumerate(frames):
        # One frame of lead-in before the start lets players decode it cleanly.
        lead_in = frames[i + 1][2] if i + 1 < len(frames) else secs
        if t + secs > start - lead_in and t < start + length:
            out.append(data[a:b])
        t += secs
        if t >= start + length:
            break
    return b"".join(out)


def mp3_duration(frames) -> float:
    return sum(secs for _, _, secs in frames)


# --- Downloading from the Monsterdle repo (cached) ---

_config_cache = {"at": 0.0, "config": None}
_audio_cache = {}          # path -> (bytes, frames); a few recent files only
_AUDIO_CACHE_SIZE = 12


async def load_config(session: aiohttp.ClientSession) -> dict:
    if _config_cache["config"] and time.monotonic() - _config_cache["at"] < 600:
        return _config_cache["config"]
    async with session.get(MONSTERDLE_FILES + "monsters.json") as res:
        res.raise_for_status()
        config = normalize_config(json.loads(await res.text()))
    _config_cache.update(at=time.monotonic(), config=config)
    return config


async def load_audio(session: aiohttp.ClientSession, path: str):
    if path in _audio_cache:
        return _audio_cache[path]
    url = MONSTERDLE_FILES + "/".join(p.replace(" ", "%20") for p in path.split("/"))
    async with session.get(url) as res:
        res.raise_for_status()
        data = await res.read()
    frames = mp3_frames(data)
    if not frames:
        raise ValueError(f"{path} isn't a usable MP3")
    if len(_audio_cache) >= _AUDIO_CACHE_SIZE:
        _audio_cache.pop(next(iter(_audio_cache)))
    _audio_cache[path] = (data, frames)
    return data, frames


# --- The duel itself ---

class Duel:
    def __init__(self, channel_id, challenger, opponent, mode, mode_label, steps, monster, data, frames):
        self.channel_id = channel_id
        self.players = (challenger, opponent)
        self.mode_label = mode_label
        self.steps = steps
        self.monster = monster
        self.data = data
        self.frames = frames
        self.offset = 0.0
        self.step = 0
        self.message = None
        self.view = None
        self.over = False
        self.started = time.monotonic()

    def is_player(self, user) -> bool:
        return any(p.id == user.id for p in self.players)

    def clip_file(self) -> discord.File:
        clip = cut_clip(self.data, self.frames, self.offset, self.steps[self.step])
        # A neutral file name, so the attachment can't give the answer away.
        return discord.File(io.BytesIO(clip), filename=f"clip-{self.step + 1}.mp3")

    def status(self) -> str:
        a, b = self.players
        length = self.steps[self.step]
        lines = [
            f"## ⚔️ Monsterdle duel: {a.mention} vs {b.mention}",
            f"**{self.mode_label}** · full {length:g}s clip · duel over" if self.over else
            f"**{self.mode_label}** · clip length **{length:g}s** · first to type the monster's name wins!",
        ]
        lines.append(f"-# Unlimited guesses · ends in {DUEL_TIMEOUT // 60} minutes if nobody gets it")
        return "\n".join(lines)


DUELS = {}   # channel id -> Duel


class ChallengeView(discord.ui.View):
    def __init__(self, challenger, opponent, mode):
        super().__init__(timeout=CHALLENGE_TIMEOUT)
        self.challenger, self.opponent, self.mode = challenger, opponent, mode
        self.message = None
        self.answered = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("This challenge isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="⚔️")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.answered = True
        self.stop()
        await start_duel(interaction, self.challenger, self.opponent, self.mode)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.answered = True
        self.stop()
        await interaction.response.edit_message(
            content=f"{self.opponent.mention} declined the duel with {self.challenger.mention}.", view=None)

    async def on_timeout(self):
        if not self.answered and self.message:
            await self.message.edit(
                content=f"{self.opponent.mention} didn't respond to {self.challenger.mention}'s duel in time.",
                view=None)


class ClipView(discord.ui.View):
    def __init__(self, duel: Duel):
        super().__init__(timeout=DUEL_TIMEOUT + 30)
        self.duel = duel
        self.refresh_button()

    def refresh_button(self):
        duel = self.duel
        if duel.over or duel.step >= len(duel.steps) - 1:
            self.more.disabled = True
            self.more.label = "Full clip"
        else:
            extra = duel.steps[duel.step + 1] - duel.steps[duel.step]
            self.more.label = f"Hear more (+{extra:g}s)"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.duel.is_player(interaction.user):
            await interaction.response.send_message("Only the two duelists can use this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Hear more", style=discord.ButtonStyle.primary, emoji="🔊")
    async def more(self, interaction: discord.Interaction, button: discord.ui.Button):
        duel = self.duel
        if duel.over or duel.step >= len(duel.steps) - 1:
            await interaction.response.defer()
            return
        duel.step += 1
        self.refresh_button()
        await interaction.response.edit_message(
            content=duel.status(), attachments=[duel.clip_file()], view=self)


async def start_duel(interaction: discord.Interaction, challenger, opponent, mode):
    channel_id = interaction.channel_id
    await interaction.response.edit_message(
        content=f"{opponent.mention} accepted! Getting a monster ready…", view=None)
    if channel_id in DUELS:
        await interaction.followup.send("There's already a duel going on in this channel.", ephemeral=True)
        return
    try:
        async with aiohttp.ClientSession() as session:
            config = await load_config(session)
            if mode not in config["modes"]:
                raise ValueError(f"Monsterdle has no '{mode}' mode")
            monster = random.choice(config["monsters"])
            data, frames = await load_audio(session, monster["audio"])
    except Exception as e:
        print(f"Duel setup failed: {e}", flush=True)
        await interaction.followup.send("Couldn't load a monster from Monsterdle. Try again in a moment.")
        return

    mode_cfg = config["modes"][mode]
    steps = [float(s) for s in mode_cfg["steps"]]
    duel = Duel(channel_id, challenger, opponent, mode, mode_cfg.get("label", mode), steps, monster, data, frames)
    if start_style(config, mode) == "random":
        duel.offset = random.uniform(0, max(0.0, mp3_duration(frames) - steps[-1]))
    DUELS[channel_id] = duel
    duel.view = ClipView(duel)
    duel.message = await interaction.followup.send(
        content=duel.status(), file=duel.clip_file(), view=duel.view, wait=True)
    asyncio.create_task(_end_after_timeout(duel))


async def finish(duel: Duel, text: str):
    """Ends a duel: reveals the answer and swaps in the full clip."""
    if duel.over:
        return
    duel.over = True
    DUELS.pop(duel.channel_id, None)
    duel.step = len(duel.steps) - 1
    duel.view.refresh_button()
    duel.view.stop()
    try:
        await duel.message.edit(
            content=f"{duel.status()}\n\n{text}\nThe monster was **{duel.monster['name']}**.",
            attachments=[duel.clip_file()], view=duel.view)
    except discord.HTTPException as e:
        print(f"Couldn't update the duel message: {e}", flush=True)
        await duel.message.channel.send(f"{text}\nThe monster was **{duel.monster['name']}**.")


async def _end_after_timeout(duel: Duel):
    await asyncio.sleep(DUEL_TIMEOUT)
    if not duel.over:
        await finish(duel, "⏰ Time's up! Nobody got it.")


async def handle_message(message: discord.Message) -> bool:
    """Checks a chat message for a duel guess. Returns True if it was one."""
    duel = DUELS.get(message.channel.id)
    if not duel or duel.over or not duel.is_player(message.author):
        return False
    guess = find_monster(_config_cache["config"], message.content)
    if guess is None:
        return False   # just chatting, not a monster name
    if guess["name"] == duel.monster["name"]:
        await message.add_reaction("✅")
        await finish(duel, f"🏆 **{message.author.mention} wins!** Guessed it with a {duel.steps[duel.step]:g}s clip.")
        return True

    await message.add_reaction("❌")
    return True


def setup(bot):
    """Adds the /duel command to the bot."""

    @bot.tree.command(name="duel", description="Challenge someone to a Monsterdle duel")
    @app_commands.describe(opponent="Who to challenge", mode="Easy plays from the start of the sound; Hard from anywhere")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Easy", value="easy"),
        app_commands.Choice(name="Hard", value="hard"),
    ])
    async def duel(interaction: discord.Interaction, opponent: discord.Member, mode: app_commands.Choice[str]):
        if opponent.id == interaction.user.id:
            await interaction.response.send_message("You can't duel yourself.", ephemeral=True)
            return
        if opponent.bot:
            await interaction.response.send_message("Bots can't duel.", ephemeral=True)
            return
        if interaction.channel_id in DUELS:
            await interaction.response.send_message(
                "There's already a duel going on in this channel. Wait for it to finish.", ephemeral=True)
            return
        view = ChallengeView(interaction.user, opponent, mode.value)
        await interaction.response.send_message(
            f"⚔️ {opponent.mention}, {interaction.user.mention} challenges you to a **Monsterdle duel** "
            f"(**{mode.name}** mode)! You have {CHALLENGE_TIMEOUT} seconds to accept.",
            view=view,
            allowed_mentions=discord.AllowedMentions(users=[opponent]),
        )
        view.message = await interaction.original_response()
