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
TARGET_CHANNEL_ID = 920520291577393155   # channel where translations get posted
SOURCE_CHANNEL_IDS = {1131000939923386478}  # channels to watch (optional filter)

# Minimum confidence required before acting on a detected language (0.0 - 1.0)
CONFIDENCE_THRESHOLD = 0.85

# Common, short French words that are strong signals of real French text.
# Requiring at least one of these (or an accented character) filters out
# short English/slang messages that langdetect confidently misreads as French.
FRENCH_STOPWORDS = {
    "je", "tu", "il", "elle", "nous", "vous", "ils", "elles", "le", "la", "les",
    "un", "une", "des", "et", "est", "es", "suis", "sont", "pas", "que", "qui",
    "de", "du", "au", "aux", "ce", "cette", "avec", "pour", "dans", "sur",
    "mais", "donc", "alors", "très", "trop", "bien", "bon", "oui", "non",
    "moi", "toi", "on", "se", "ne", "plus", "comme", "faire", "fait",
}

ACCENTED_CHARS = set("àâäéèêëîïôöùûüçœæ")


def looks_like_french(text: str) -> bool:
    """Sanity check independent of langdetect: require either an accented
    character, or at least two common French words, to appear in the text.
    Requiring two matches (rather than one) avoids false positives from
    short words that are also valid English (e.g. 'a', 'on', 'y')."""
    lowered = text.lower()

    if any(char in ACCENTED_CHARS for char in lowered):
        return True

    words = re.findall(r"[a-zà-ÿ']+", lowered)
    matches = sum(1 for word in words if word in FRENCH_STOPWORDS)
    return matches >= 2

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

    if len(text) < 10:
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

    if (
        looks_like_french(text)
        or (lang in WATCHED_LANGUAGES and confidence > CONFIDENCE_THRESHOLD)
    ):
        lang = "fr"  # force French: either the heuristic caught it, or langdetect did
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
            await target_channel.send(content="**New translation:**", embed=embed)

    await bot.process_commands(message)


bot.run(TOKEN)