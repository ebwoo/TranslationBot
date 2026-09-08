from dotenv import load_dotenv
import os

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEEPL_KEY = os.getenv("DEEPL_API_KEY")

import re
import discord
from discord.ext import commands
from langdetect import detect_langs, LangDetectException
from deep_translator import MyMemoryTranslator
import deepl

intents = discord.Intents.default()
intents.message_content = True  # required to read message text

bot = commands.Bot(command_prefix="!", intents=intents)

# Config: which language(s) trigger translation, and where translations go
WATCHED_LANGUAGES = {"fr"}   # ISO 639-1 codes
TARGET_CHANNEL_ID = 1545938608215556167   # channel where translations get posted
SOURCE_CHANNEL_IDS = {1518211425116491797, 1518454285023838338, 1518212264501710968, 1518308060261650644, 1545989798093660280, 1518211425116491798}  # channels to watch (optional filter)

# Minimum confidence required before acting on a detected language (0.0 - 1.0)
CONFIDENCE_THRESHOLD = 0.85

FRENCH_STOPWORDS = {
    # Pronouns
    "je", "tu", "il", "elle", "nous", "vous", "ils", "elles",
    "te", "se", "le", "la", "les", "lui",
    "leur", "leurs", "eux", "moi", "toi", "soi","en",
    "t'as", "kiffer", "avoue",

    # Articles / determiners
    "un", "une", "des", "du", "de", "le", "la", "les",
    "au", "aux", "ce", "cet", "cette", "ces", "mon", "ma", "mes",
    "ta", "tes", "sa", "ses", "notre", "nos",
    "votre", "vos", "leur", "leurs", "quel", "quelle", "quels",
    "quelles", "quelque", "quelques", "chaque", "tout", "toute",
    "tous", "toutes", "aucun", "aucune",

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
    "mettre", "mets", "met", "mettons", "mettez", "mettent",

    # Negation
    "ne", "pas", "plus", "jamais", "rien", "personne",
    "aucun", "aucune", "ni", "sans",

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
    print(f"Logged in as {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    # Ignore the bot's own messages
    if message.author.bot:
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
            await target_channel.send(content="# New malicious message", embed=embed)

    await bot.process_commands(message)


bot.run(TOKEN)