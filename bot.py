from dotenv import load_dotenv
import os

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEEPL_KEY = os.getenv("DEEPL_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# The ID from the sheet's URL: docs.google.com/spreadsheets/d/THIS_PART/edit
# Using the ID (not the title) avoids ambiguity if two sheets ever share a
# name, and avoids needing Drive API scope just to look up a title.
SHEET_ID = os.getenv("SHEET_ID")

import re
import discord
import json
import gspread
import gspread.utils
from google.oauth2.service_account import Credentials
from discord import app_commands
from discord.ext import commands
from langdetect import detect_langs, LangDetectException
from deep_translator import MyMemoryTranslator
import deepl


SHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
google_creds = Credentials.from_service_account_info(
    json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SHEET_SCOPES
)
gc = gspread.authorize(google_creds)

# Worksheet tabs to skip when scanning for players (not player data)
SKIP_WORKSHEETS = {"Templates", "Stats"}

# Cache of player name -> (worksheet_title, column_index). Built at startup
# and rebuildable on demand via /refreshplayers, so we don't hit the Sheets
# API on every keystroke of autocomplete.
PLAYER_INDEX = {}


def build_player_index():
    """Scan every non-excluded worksheet, find each player's column block by
    locating 'Round'/'Monster'/'Date' subheaders, and return a dict mapping
    player name -> (worksheet_title, column_index). Player names live on
    row 2, with the Round/Monster/Date subheaders on row 4 beneath them."""
    index = {}
    spreadsheet = gc.open_by_key(SHEET_ID)
    for ws in spreadsheet.worksheets():
        if ws.title in SKIP_WORKSHEETS:
            continue
        values = ws.get_all_values()
        if len(values) < 4:
            continue
        header_row, subheader_row = values[1], values[3]
        for col_idx, cell in enumerate(header_row):
            name = cell.strip()
            if not name:
                continue
            sub = [s.strip() for s in subheader_row[col_idx:col_idx + 3]]
            if sub == ["Round", "Monster", "Date"]:
                index[name] = (ws.title, col_idx)
    return index


# Fill this in with every monster name as you want it to appear on the
# sheet (proper capitalization/spacing). Whatever the user types gets
# normalized and matched against this list, so they don't need to type
# it exactly.
MONSTER_DATABASE = [
    "diddywallfle17",
    "Baneful Rift",
    "Baneful Glitch",
    "Baneful Hunter",
    "Baneful Devourer",
    "Iron Flesh",
    "The Wasp",
    "Baneful Screamer",
    "Baneful Silence",
    "The Seeker",
    "The Singularity",
    "The Trickster",
    "Desolator",
    "Baneful Dread",
    "The Corruption",
    "The Infection",
    "The Director",
    "The Craftsman",
    "The Howler",
    "Baneful Dream Maker",
    "The Lich",
    "The Ashen Maw",
    "Sycophant",
    "The Periscope",
    "The Watcher",
    "The Harvester",
    "Baneful Gatekeeper",
    "The Psycho",
    "The Sentinel",
    "The Cultists",
    "The Nightstalker",
    "Maestro",
    "The Mathematician",
    "Camo",
    "Woody",
    "Vinemaster",
    "Big Hands Man",
    "The Gyro",
    "The Tumor",
    "The Feral",
    "Waltz",
    "The Arachnid",
    "Tenebris",
    "Wooden Nightmare",
    "Laughing Clown Race",
    "Wicked Dreamer",
    "The Carrion God",
    "Flesh Duo",
    "The Tempest",
    "Malicious Madman",
    "The Veil",
    "The Forecast",
    "God of Greed",
    "Baneful Amalgam",
    "Opened Prism",
    "Baneful Sage",
    "Baneful Construct",
    "Unknown Monster 1",
    "Unknown Monster 2",
    "Unknown Monster 3",
    "Unknown Monster 4",
    "Unknown Monster 5",
    "Unknown Monster 6",
    "Gate",
    "The Executioner",
    "The Hominid",
    "The Follower",
    "Bear Trap",
    "Baneful Rot",
    "Reprieve",
    "The True Nightmare",
]

# Shorthand/abbreviations people might type -> the canonical name from
# MONSTER_DATABASE it should resolve to. Keys are matched the same way as
# the main database (case/whitespace ignored), so "BC", "bc", "b c" all work.
MONSTER_ALIASES = {
    "bc": "Baneful Construct",
    "exec": "The Executioner",
    "executioner": "The Executioner",
    "lcr": "Laughing Clown Race",
    "bhm": "Big Hands Man",
    "gyro": "The Gyro",
    "bg": "Baneful Gatekeeper",
    "gatekeeper": "Baneful Gatekeeper",
    "bdm": "Baneful Dream Maker",
    "dreammaker": "Baneful Dream Maker",
    "vine": "Vinemaster",
    "rift": "Baneful Rift",
    "screamer": "Baneful Screamer",
    "rot": "Baneful Rot",
    "construct": "Baneful Construct",
    "silence": "Baneful Silence",
    "hunter": "Baneful Hunter",
    "tene": "Tenebris",
    "sage": "Baneful Sage",
    "dream maker": "Baneful Dream Maker",
    "dread": "Baneful Dread",
    "tnm": "The True Nightmare",
    "larry": "The Singularity",
    "carrion god": "The Carrion God",
}


def normalize_monster(name: str) -> str:
    """Lowercase and strip all whitespace, so 'baneful  Construct',
    'BANEFUL CONSTRUCT', and 'baneful construct' all match the same entry."""
    return re.sub(r"\s+", "", name).lower()


MONSTER_LOOKUP = {}

# For each monster, register its full normalized name, AND (if it starts
# with "the") a second key with "the" stripped off — so "The Hominid" can
# be found by typing either "The Hominid" or just "Hominid". setdefault
# means a full-name match always wins if some other monster's stripped
# form happens to collide with it.
for m in MONSTER_DATABASE:
    full_key = normalize_monster(m)
    MONSTER_LOOKUP[full_key] = m
    if full_key.startswith("the"):
        MONSTER_LOOKUP.setdefault(full_key[3:], m)

# Merge in aliases. Each alias's target must be a real, exact entry in
# MONSTER_DATABASE — this check catches typos in MONSTER_ALIASES itself
# (e.g. pointing to a name that doesn't exist) at startup instead of
# silently failing later.
for alias, canonical in MONSTER_ALIASES.items():
    if canonical not in MONSTER_DATABASE:
        print(f"WARNING: alias '{alias}' points to '{canonical}', which is "
              "not in MONSTER_DATABASE — check spelling.", flush=True)
        continue
    MONSTER_LOOKUP[normalize_monster(alias)] = canonical


def normalize_for_stats(name: str) -> str:
    """Like normalize_monster, but also strips a leading 'the' — the Stats
    sheet sometimes omits it (e.g. 'Periscope' instead of 'The Periscope'),
    so both sides need to be reduced to the same form to match reliably."""
    n = normalize_monster(name)
    if n.startswith("the"):
        n = n[3:]
    return n


def reorder_stats_sheet():
    """Read the 'Stats' worksheet's Monster/Kills rows, sort by Kills
    descending, and rewrite ONLY the Monster column if the order needs to
    change. The Kills column is deliberately never written to — it's driven
    by a formula (auto-counting from the player sheets), and each row's
    formula recalculates on its own once the correct monster name is
    sitting in that row. Writing to Kills directly would overwrite the
    formula with a static number and break the auto-updating."""
    ws = gc.open_by_key(SHEET_ID).worksheet("Stats")
    values = ws.get_all_values()

    header_row_idx = monster_col = kills_col = None
    for i, row in enumerate(values):
        if "Monster" in row and "Kills" in row:
            header_row_idx = i
            monster_col = row.index("Monster")
            kills_col = row.index("Kills")
            break

    if header_row_idx is None:
        print("WARNING: couldn't find 'Monster'/'Kills' headers on the "
              "Stats sheet — order not checked.", flush=True)
        return

    data_rows = []
    for row in values[header_row_idx + 1:]:
        if len(row) > monster_col and row[monster_col].strip():
            name = row[monster_col]
            kills_str = row[kills_col].strip() if len(row) > kills_col else ""
            try:
                kills = int(kills_str)
            except ValueError:
                kills = 0
            data_rows.append((name, kills))

    sorted_rows = sorted(data_rows, key=lambda r: -r[1])

    if [name for name, _ in sorted_rows] == [name for name, _ in data_rows]:
        return  # already in the right order, nothing to rewrite

    start_row = header_row_idx + 2  # first data row, 1-indexed
    end_row = start_row + len(sorted_rows) - 1

    monster_range = (
        f"{gspread.utils.rowcol_to_a1(start_row, monster_col + 1)}:"
        f"{gspread.utils.rowcol_to_a1(end_row, monster_col + 1)}"
    )
    ws.update(monster_range, [[name] for name, _ in sorted_rows])


intents = discord.Intents.default()
intents.message_content = True  # required to read message text

bot = commands.Bot(command_prefix="!", intents=intents)


# --Run history stuff--

async def player_autocomplete(interaction: discord.Interaction, current: str):
    current_lower = current.lower()
    matches = sorted(name for name in PLAYER_INDEX if current_lower in name.lower())
    return [app_commands.Choice(name=name, value=name) for name in matches[:25]]


@bot.tree.command(name="logrun", description="Log a nightmare run to the spreadsheet")
@app_commands.describe(
    player="Your name as it appears on the sheet",
    roundnumber="Round reached",
    monster="Monster name",
    date="Date (e.g. 9/8/26) — leave blank for N/A"
)
@app_commands.autocomplete(player=player_autocomplete)
async def logrun(
    interaction: discord.Interaction,
    player: str,
    roundnumber: int,
    monster: str,
    date: str = "N/A"
):
    await interaction.response.defer()

    if player not in PLAYER_INDEX:
        await interaction.followup.send(
            f"Couldn't find '{player}' on the sheet. If they were just added, try /refreshplayers first."
        )
        return

    if roundnumber > 50:
        await interaction.followup.send(
            f"No"
        )
        return

    canonical_monster = MONSTER_LOOKUP.get(normalize_monster(monster))
    if canonical_monster is None:
        await interaction.followup.send(
            f"Couldn't match '{monster}' to a known monster. Check the spelling, "
            "or ask for it to be added to the database."
        )
        return
    monster = canonical_monster

    worksheet_title, col_index = PLAYER_INDEX[player]
    ws = gc.open_by_key(SHEET_ID).worksheet(worksheet_title)

    all_values = ws.get_all_values()

    # Pull existing entries for this player only (data starts row 6:
    # row 2 = name, row 4 = Round/Monster/Date subheaders, row 6 = first entry)
    existing = []
    for row in all_values[5:]:
        r, m, d = row[col_index], row[col_index + 1], row[col_index + 2]
        if r.strip():
            existing.append((r, m, d))

    # Add the new entry and sort by round ascending
    existing.append((str(roundnumber), monster, date))
    existing.sort(key=lambda entry: int(entry[0]))

    # Write the sorted block back, only in this player's 3 columns
    start_cell = gspread.utils.rowcol_to_a1(6, col_index + 1)
    end_cell = gspread.utils.rowcol_to_a1(5 + len(existing), col_index + 3)
    ws.update(f"{start_cell}:{end_cell}", existing)

    # Match the sheet's existing text styling — otherwise new/rewritten
    # rows fall back to Google Sheets' plain default formatting.
    ws.format(f"{start_cell}:{end_cell}", {
        "textFormat": {
            "fontFamily": "Nunito",
            "fontSize": 10,
            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
        },
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
    })

    # Since kill counts already auto-update elsewhere, just check whether
    # this changes where the monster should rank and reorder if so.
    try:
        reorder_stats_sheet()
    except Exception as e:
        print(f"Failed to reorder Stats sheet: {e}", flush=True)

    await interaction.followup.send(
        f"Logged for **{player}**: Round {roundnumber} — {monster} ({date})"
    )


@bot.tree.command(name="refreshplayers", description="Rebuild the player list from the spreadsheet")
async def refreshplayers(interaction: discord.Interaction):
    await interaction.response.defer()
    global PLAYER_INDEX
    PLAYER_INDEX = build_player_index()
    await interaction.followup.send(f"Refreshed — found {len(PLAYER_INDEX)} players across the sheet.")


# --Translation Stuff--

# Config: which language(s) trigger translation, and where translations go
WATCHED_LANGUAGES = {"fr"}   # ISO 639-1 codes
TARGET_CHANNEL_ID = 1545938608215556167   # channel where translations get posted
SOURCE_CHANNEL_IDS = {1518211425116491797, 1518454285023838338, 1518212264501710968, 1518308060261650644, 1545989798093660280, 1518211425116491798}  # channels to watch (optional filter)

BLACKLISTED_PEOPLE = {606741399370727446}

# Minimum confidence required before acting on a detected language (0.0 - 1.0)
CONFIDENCE_THRESHOLD = 0.85

FRENCH_STOPWORDS = {
    # Pronouns
    "je", "tu", "il", "elle", "nous", "vous", "ils", "elles",
    "te", "se", "le", "la", "les", "lui",
    "leur", "leurs", "eux", "moi", "toi", "soi", "en",
    "t'as", "kiffer", "avoue",

    # Articles / determiners
    "un", "une", "des", "du", "de", "le", "la", "les",
    "au", "aux", "ce", "cet", "cette", "ces", "mon", "ma", "mes",
    "ta", "tes", "sa", "ses", "notre", "nos",
    "votre", "vos", "leur", "leurs", "quel", "quelle", "quels",
    "quelles", "quelque", "quelques", "chaque", "tout", "toute",
    "tous", "toutes", "aucun", "aucune", "grosse",

    # Common verbs
    "être", "est", "es", "suis", "sommes", "êtes", "sont",
    "avoir", "avons", "avez", "ont",
    "faire", "fais", "fait", "faisons", "faites", "font",
    "aller", "vais", "vas", "va", "allons", "allez", "vont",
    "venir", "viens", "vient", "venons", "venez", "viennent",
    "voir", "vois", "voit", "voyons", "voyez", "voient",
    "dire", "dis", "dit", "disons", "dites", "disent",
    "pouvoir", "peux", "peut", "pouvons", "pouvez", "peuvent",
    "vouloir", "veux", "veut", "voulons", "voulez", "veulent",
    "devoir", "dois", "doit", "devons", "devez", "doivent",
    "savoir", "sais", "sait", "savons", "savez", "savent",
    "prendre", "prends", "prend", "prenons", "prenez", "prennent",
    "donner", "donne", "donnes", "donnons", "donnez", "donnent",
    "mettre", "mets", "mettons", "mettez", "mettent",

    # Negation
    "ne", "pas", "plus", "jamais", "rien", "personne",
    "aucun", "aucune", "sans",

    # Conjunctions
    "et", "ou", "mais", "donc", "car", "ni", "que", "qu",
    "si", "comme", "lorsque", "lorsqu", "puisque", "puisqu",
    "parce", "pourtant", "cependant", "ainsi", "alors",

    # Prepositions
    "à", "de", "en", "dans", "sur", "sous", "avec",
    "sans", "pour", "par", "chez", "entre", "vers", "contre",
    "avant", "après", "depuis", "pendant", "durant", "selon",
    "devant", "derrière", "près", "loin", "parmi", "autour",
    "jusqu", "jusque",

    # Question words
    "qui", "que", "quoi", "où", "quand", "comment", "pourquoi",
    "quel", "quelle", "quels", "quelles", "combien",

    # Adverbs / common modifiers
    "très", "trop", "bien", "mal", "moins", "beaucoup",
    "peu", "assez", "aussi", "encore", "déjà", "toujours",
    "souvent", "parfois", "jamais", "maintenant", "ici", "là",
    "alors", "ainsi", "vraiment", "presque", "seulement",
    "même", "surtout", "peut-être", "ensemble",
    "vite", "tôt", "tard",

    # Common conversational words
    "oui", "non", "voici", "voilà", "merci", "bonjour", "salut",
    "bon", "bonne", "bien", "d'accord", "désolé", "désolée",
    "pardon", "pomme",

    # Demonstrative / existential
    "ceci", "cela", "ça", "ce", "cet", "cette", "ces",

    # Time / quantity words
    "fois", "jour", "jours", "ans", "année", "années",
    "heure", "heures", "temps",

}

# Words that overlap with common standalone English words — on their own,
# a single match isn't strong evidence of French (e.g. "a", "on" both occur
# constantly in ordinary English sentences).
AMBIGUOUS_WORDS = {"y", "en"}

STRONG_FRENCH_WORDS = FRENCH_STOPWORDS - AMBIGUOUS_WORDS

ACCENTED_CHARS = set("àâäéèêëîïôöùûüçœæ")

URL_PATTERN = re.compile(r'https?://[^\s]+')

def looks_like_french(text: str) -> bool:
    """Sanity check independent of langdetect: an accented character, OR at
    least one unambiguous French word, OR at least two *distinct* ambiguous
    words together. Counting distinct words (not raw occurrences) stops a
    single common English word like "on" repeated twice from counting as
    two pieces of evidence."""
    lowered = text.lower()

    if any(char in ACCENTED_CHARS for char in lowered):
        return True

    words = set(re.findall(r"[a-zà-ÿ']+", lowered))
    strong_matches = words & STRONG_FRENCH_WORDS
    ambiguous_matches = words & AMBIGUOUS_WORDS

    print(f"Words detected: {strong_matches} and {ambiguous_matches}")

    return len(strong_matches) >= 1 or len(ambiguous_matches) >= 2

# Set up DeepL translator client once, reused for every message
deepl_translator = deepl.Translator(DEEPL_KEY)

# Maps ISO codes (what langdetect returns) to MyMemory's locale-style codes
MYMEMORY_LANG_MAP = {"fr": "fr-FR", "es": "es-ES", "de": "de-DE"}


def translate_text(text, source_lang):
    """Try DeepL first; fall back to MyMemory if DeepL fails for any reason."""
    try:
        result = deepl_translator.translate_text(
            text, source_lang=source_lang.upper(), target_lang="EN-US"
        )
        return result.text
    except Exception as e:
        print(f"DeepL failed, falling back to MyMemory: {e}")
        mm_source = MYMEMORY_LANG_MAP.get(source_lang, source_lang)
        return MyMemoryTranslator(source=mm_source, target="en-GB").translate(text)


@bot.event
async def on_ready():
    global PLAYER_INDEX
    print(f"Logged in as {bot.user}")
    PLAYER_INDEX = build_player_index()
    print(f"Loaded {len(PLAYER_INDEX)} players from sheet", flush=True)
    await bot.tree.sync()
    print("Slash commands synced", flush=True)


@bot.event
async def on_message(message: discord.Message):
    # Ignore the bot's own messages
    if message.author.bot:
        return

    if URL_PATTERN.search(message.content):
        return

    # Optional: only watch specific channels
    if SOURCE_CHANNEL_IDS and message.channel.id not in SOURCE_CHANNEL_IDS:
        await bot.process_commands(message)
        return

    text = message.content.strip()
    text = re.sub(r"<[@#][!&]?\d+>", "", text).strip()  # remove user/role/channel mentions

    if not text:
        await bot.process_commands(message)
        return

    if len(text) < 5:
        await bot.process_commands(message)
        return

    try:
        results = detect_langs(text)
        top = results[0]          # highest-probability guess
        lang = top.lang
        confidence = top.prob
        print(f"DEBUG: '{text}' -> lang={lang}, confidence={confidence:.2f}, "
              f"looks_like_french={looks_like_french(text)}")
    except LangDetectException:
        await bot.process_commands(message)
        return

    if looks_like_french(text):
        lang = "fr"  # heuristic-confirmed; langdetect's label is unreliable on short text
        try:
            translated = translate_text(text, lang)
        except Exception as e:
            print(f"Translation failed entirely: {e}")
            await bot.process_commands(message)
            return

        target_channel = bot.get_channel(TARGET_CHANNEL_ID)
        if target_channel:
            embed = discord.Embed(
                title=f"Translated from {lang.upper()}",
                color=discord.Color.blurple()
            )
            embed.set_author(
                name=f"{message.author.display_name} (from #{message.channel.name})",
                icon_url=message.author.display_avatar.url
            )
            embed.add_field(name="Original", value=text[:1000], inline=False)
            embed.add_field(name="Translation", value=translated[:1000], inline=False)
            if message.author.id in BLACKLISTED_PEOPLE:
                embed.remove_field(1)
                await target_channel.send(content="# New bobey message", embed=embed)
                return
            await target_channel.send(content="# New malicious message", embed=embed)

    await bot.process_commands(message)


bot.run(TOKEN)