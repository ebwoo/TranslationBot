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
intents.message_content = True  

bot = commands.Bot(command_prefix="!", intents=intents)

WATCHED_LANGUAGES = {"fr"}   # ISO 639-1 codes
TARGET_CHANNEL_ID = 1545938608215556167   # channel where translations get posted
SOURCE_CHANNEL_IDS = {1518211425116491797, 1518454285023838338, 1518212264501710968, 1518308060261650644, 1545989798093660280, 1518211425116491798}  # channels to watch (optional filter)

CONFIDENCE_THRESHOLD = 0.95

FRENCH_STOPWORDS = {
    # Pronouns
    "je", "tu", "il", "elle", "on", "nous", "vous", "ils", "elles",
    "me", "m", "te", "t", "se", "s", "le", "la", "les", "lui",
    "leur", "leurs", "eux", "moi", "toi", "soi", "y", "en",

    # Articles / determiners
    "un", "une", "des", "du", "de", "d", "le", "la", "les", "l",
    "au", "aux", "ce", "cet", "cette", "ces", "mon", "ma", "mes",
    "ton", "ta", "tes", "son", "sa", "ses", "notre", "nos",
    "votre", "vos", "leur", "leurs", "quel", "quelle", "quels",
    "quelles", "quelque", "quelques", "chaque", "tout", "toute",
    "tous", "toutes", "aucun", "aucune",

    # Common verbs
    "être", "est", "es", "suis", "sommes", "êtes", "sont",
    "avoir", "ai", "as", "a", "avons", "avez", "ont",
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
    "venir", "viens", "vient", "venons", "venez", "viennent",

    # Negation
    "ne", "n", "pas", "plus", "jamais", "rien", "personne",
    "aucun", "aucune", "ni", "sans",

    # Conjunctions
    "et", "ou", "mais", "donc", "or", "car", "ni", "que", "qu",
    "si", "comme", "lorsque", "lorsqu", "puisque", "puisqu",
    "parce", "pourtant", "cependant", "ainsi", "alors",

    # Prepositions
    "à", "a", "de", "d", "en", "dans", "sur", "sous", "avec",
    "sans", "pour", "par", "chez", "entre", "vers", "contre",
    "avant", "après", "depuis", "pendant", "durant", "selon",
    "devant", "derrière", "près", "loin", "parmi", "autour",
    "jusqu", "jusque",

    # Question words
    "qui", "que", "quoi", "où", "quand", "comment", "pourquoi",
    "quel", "quelle", "quels", "quelles", "combien",

    # Adverbs / common modifiers
    "très", "trop", "bien", "mal", "plus", "moins", "beaucoup",
    "peu", "assez", "aussi", "encore", "déjà", "toujours",
    "souvent", "parfois", "jamais", "maintenant", "ici", "là",
    "alors", "ainsi", "vraiment", "presque", "seulement",
    "même", "surtout", "peut-être", "ensemble", "ainsi",
    "vite", "tôt", "tard",

    # Common conversational words
    "oui", "non", "voici", "voilà", "merci", "bonjour", "salut",
    "bon", "bonne", "bien", "d'accord", "désolé", "désolée",
    "pardon", "pomme",

    # Demonstrative / existential
    "ceci", "cela", "ça", "ce", "cet", "cette", "ces",
    "voici", "voilà", "il", "y", "a",

    # Time / quantity words
    "fois", "jour", "jours", "an", "ans", "année", "années",
    "heure", "heures", "moment", "temps", "fois",
    
    # Common miscellaneous words
    "chose", "choses", "façon", "manière", "fois", "part",
    "cas", "place", "monde", "gens", "personne", "quelque",
    "quelques", "tout", "tous", "toute", "toutes",
}

ACCENTED_CHARS = set("àâäéèêëîïôöùûüçœæ")


def looks_like_french(text: str) -> bool:
    """Extra sanity check on top of langdetect's guess: require either an
    accented character or a common French word to actually appear in the
    text, so short slang/English messages don't slip through on a lucky
    statistical fluke."""
    lowered = text.lower()

    if any(char in ACCENTED_CHARS for char in lowered):
        return True

    words = re.findall(r"[a-zà-ÿ']+", lowered)
    return any(word in FRENCH_STOPWORDS for word in words)

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
    except LangDetectException:
        await bot.process_commands(message)
        return

    if (
        lang in WATCHED_LANGUAGES
        and confidence > CONFIDENCE_THRESHOLD
        and looks_like_french(text)
    ):
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