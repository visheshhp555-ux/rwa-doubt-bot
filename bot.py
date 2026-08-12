import os
import re
import json
import copy
import html
import uuid
import shutil
import asyncio
import logging
from urllib.parse import urlparse
from typing import Any, Dict, Optional, List, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, ChatPermissions, Message
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIGURATION & CONSTANTS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_FILE = "database.json"
TIMEZONE = ZoneInfo("Asia/Kolkata")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============================================================
# DEFAULT GROUP SETTINGS
# ============================================================

DEFAULT_GROUP_SETTINGS: Dict[str, Any] = {
    "welcome_body": (
        "Welcome to our study group ❤️\n\n"
        "📜 Rules: /rules\n"
        "📚 Happy Learning! ✨"
    ),
    "exit_body": "TQ for being with us. ❤️",
    "remove_body": "ko group rules violate karne par remove kar diya gaya hai. 🚫",
    "rules": (
        "📜 Group Rules:\n"
        "1. Be respectful to everyone.\n"
        "2. No spam or self-promotion.\n"
        "3. Keep the chat clean."
    ),
    # Bot-generated message deletion
    "autodelete": True,
    "delete_time": 300,
    # Auto-reply deletion
    "autoreplydelete": True,
    "reply_delete_time": 1200,
    # Protection
    "antibadword": True,
    "badwords": [],
    # Flood Control
    "floodcontrol": True,
    "flood_limit": 5,
    "flood_window": 10,
    # Warnings: {user_id: count}
    "warnings": {},
}

DB_LOCK = asyncio.Lock()

EVENT_DEDUPE: Dict[str, float] = {}
EVENT_DEDUPE_LOCK = asyncio.Lock()

# Temporary In-Memory Storage for Flood Control
FLOOD_TRACKER: Dict[Tuple[int, int], List[float]] = {}
FLOOD_LOCK = asyncio.Lock()

# ============================================================
# DATABASE MANAGEMENT
# ============================================================

def default_group_config() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_GROUP_SETTINGS)

def _raw_load_database() -> Dict[str, Any]:
    if not os.path.exists(DATABASE_FILE):
        return {"groups": {}}

    try:
        with open(DATABASE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
                raise ValueError("Corrupt or invalid DB JSON hierarchy")
            return data
    except Exception as error:
        logger.error("Database corrupt/load error: %s", error)
        unique_suffix = uuid.uuid4().hex[:6]
        backup_file = f"{DATABASE_FILE}.corrupt.{int(datetime.now().timestamp())}_{unique_suffix}.bak"
        try:
            shutil.copyfile(DATABASE_FILE, backup_file)
            logger.info("Corrupt DB backed up to %s", backup_file)
        except Exception as e:
            logger.error("Failed to backup corrupt DB file: %s", e)
        return {"groups": {}}

def _raw_save_database(data: Dict[str, Any]) -> None:
    try:
        temp_file = DATABASE_FILE + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)
        os.replace(temp_file, DATABASE_FILE)
    except Exception as error:
        logger.error("Database save error: %s", error)

async def get_group_config(chat_id: int) -> Dict[str, Any]:
    async with DB_LOCK:
        data = _raw_load_database()
        chat_key = str(chat_id)

        if chat_key not in data["groups"]:
            data["groups"][chat_key] = default_group_config()
            _raw_save_database(data)
            return data["groups"][chat_key]

        config = data["groups"][chat_key]
        changed = False

        for key, default_value in DEFAULT_GROUP_SETTINGS.items():
            if key not in config:
                config[key] = copy.deepcopy(default_value)
                changed = True

        if "antilink" in config:
            del config["antilink"]
            changed = True

        if not isinstance(config.get("badwords"), list):
            config["badwords"] = []
            changed = True

        if not isinstance(config.get("warnings"), dict):
            config["warnings"] = {}
            changed = True

        if changed:
            _raw_save_database(data)

        return config

async def update_group_config(chat_id: int, key: str, value: Any) -> None:
    async with DB_LOCK:
        data = _raw_load_database()
        chat_key = str(chat_id)

        if chat_key not in data["groups"]:
            data["groups"][chat_key] = default_group_config()

        data["groups"][chat_key][key] = value
        _raw_save_database(data)

async def update_user_warning(chat_id: int, user_id: int, increment: bool = True) -> int:
    async with DB_LOCK:
        data = _raw_load_database()
        chat_key = str(chat_id)
        user_key = str(user_id)

        if chat_key not in data["groups"]:
            data["groups"][chat_key] = default_group_config()

        warnings = data["groups"][chat_key].get("warnings", {})
        if not isinstance(warnings, dict):
            warnings = {}

        if increment:
            current_count = int(warnings.get(user_key, 0)) + 1
            warnings[user_key] = current_count
        else:
            warnings[user_key] = 0
            current_count = 0

        data["groups"][chat_key]["warnings"] = warnings
        _raw_save_database(data)
        return current_count

# ============================================================
# SAFE HELPERS & UTILITIES
# ============================================================

def get_name(user) -> str:
    """Returns RAW un-escaped name string to prevent double escaping."""
    if not user:
        return "Member"
    return (
        getattr(user, "first_name", None)
        or getattr(user, "full_name", None)
        or "Member"
    )

def get_user_mention(user) -> str:
    """Returns HTML safe user mention."""
    if not user:
        return "Member"
    if getattr(user, "username", None):
        return f"@{user.username}"
    safe_name = html.escape(get_name(user))
    return f'<a href="tg://user?id={user.id}">{safe_name}</a>'

async def is_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> bool:
    if not update.effective_chat:
        return False

    if update.effective_chat.type == ChatType.PRIVATE:
        return True

    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id,
            user_id,
        )
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except TelegramError as error:
        logger.warning("Admin check failed for user %s: %s", user_id, error)
        return False

async def bot_can_manage_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_chat:
        return False
    try:
        bot_member = await context.bot.get_chat_member(
            update.effective_chat.id,
            context.bot.id,
        )
        if bot_member.status == ChatMemberStatus.ADMINISTRATOR:
            return getattr(bot_member, "can_restrict_members", True)
        return False
    except TelegramError:
        return False

def schedule_auto_delete(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    delay: int,
) -> None:
    if delay <= 0 or not context.job_queue:
        return

    async def delete_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass

    context.job_queue.run_once(delete_job, delay)

async def reply_and_autodelete(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    is_reply_type: bool = False,
    parse_mode: Optional[str] = ParseMode.HTML,
) -> Optional[Message]:
    if not update.effective_chat or not update.effective_message:
        return None

    chat_id = update.effective_chat.id
    config = await get_group_config(chat_id)

    try:
        sent = await update.effective_message.reply_text(
            text,
            disable_web_page_preview=True,
            parse_mode=parse_mode,
        )

        delay_key = "reply_delete_time" if is_reply_type else "delete_time"
        enabled_key = "autoreplydelete" if is_reply_type else "autodelete"

        if config.get(enabled_key, True):
            schedule_auto_delete(
                context,
                chat_id,
                sent.message_id,
                int(config.get(delay_key, 300)),
            )

        return sent
    except TelegramError as error:
        logger.error("Error sending message in chat %s: %s", chat_id, error)
        return None

async def send_standalone_autodelete(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    delay: int = 300,
    parse_mode: Optional[str] = ParseMode.HTML
) -> Optional[Message]:
    try:
        sent = await context.bot.send_message(
            chat_id=chat_id, 
            text=text, 
            parse_mode=parse_mode,
            disable_web_page_preview=True
        )
        schedule_auto_delete(context, chat_id, sent.message_id, delay)
        return sent
    except TelegramError as error:
        logger.error("Standalone message send failed in chat %s: %s", chat_id, error)
        return None

async def event_seen_recently(chat_id: int, user_id: int, event_type: str) -> bool:
    now = asyncio.get_running_loop().time()
    key = f"{chat_id}:{user_id}:{event_type}"

    async with EVENT_DEDUPE_LOCK:
        old_keys = [k for k, timestamp in EVENT_DEDUPE.items() if now - timestamp > 10]
        for old_key in old_keys:
            EVENT_DEDUPE.pop(old_key, None)

        if key in EVENT_DEDUPE and now - EVENT_DEDUPE[key] < 5:
            return True

        EVENT_DEDUPE[key] = now
        return False

# ============================================================
# UNICODE-SAFE URL EXTRACTION & STRICT HOSTNAME PARSING
# ============================================================

def is_allowed_hostname(hostname: str) -> bool:
    """Strictly checks if host domain belongs exclusively to genuine YouTube domains."""
    if not hostname:
        return False
    
    clean_host = hostname.lower().strip()
    allowed_domains = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
    }
    return clean_host in allowed_domains

def contains_disallowed_link(update: Update, text: str) -> bool:
    """Detects URLs using PTB parse_entity (Unicode Safe) and strict URL parsing."""
    if not text:
        text = ""

    message = update.message
    extracted_urls: List[str] = []

    # 1. Native Telegram Entity Parsing
    if message:
        for entity_attr in ("entities", "caption_entities"):
            entities = getattr(message, entity_attr, None)
            if entities:
                for entity in entities:
                    if entity.type == "url":
                        try:
                            extracted_urls.append(message.parse_entity(entity))
                        except Exception:
                            pass
                    elif entity.type == "text_link":
                        if getattr(entity, "url", None):
                            extracted_urls.append(entity.url)

    # 2. Universal Regex to catch raw URLs
    raw_pattern = r"(?i)\b(?:https?://|ftp://|www\.)?[a-z0-9\-\.]+\.(?:[a-z]{2,20}|[0-9]{1,3})(?:/[^\s]*)?"
    extracted_urls.extend(re.findall(raw_pattern, text))

    # 3. Obfuscated domains (e.g., "site dot com")
    obfuscated_pattern = r"(?i)\b[a-z0-9\-]+\s*(?:\.|\(dot\)|\[dot\]|dot)\s*[a-z]{2,10}\b"
    if re.search(obfuscated_pattern, text):
        sanitized_obs = re.sub(r"\s*(?:\(dot\)|\[dot\]|dot)\s*", ".", text, flags=re.IGNORECASE)
        extracted_urls.extend(re.findall(raw_pattern, sanitized_obs))

    if not extracted_urls:
        return False

    # Verify each extracted URL against allowed YouTube hostnames
    for raw_url in extracted_urls:
        url_to_parse = raw_url if raw_url.startswith(("http://", "https://")) else "http://" + raw_url
        try:
            parsed = urlparse(url_to_parse)
            hostname = parsed.hostname
            if not hostname or not is_allowed_hostname(hostname):
                return True
        except Exception:
            return True

    return False

# ============================================================
# DETECTION LOGIC (QUESTIONS, GREETINGS & BADWORDS)
# ============================================================

def is_greeting_message(text: str) -> bool:
    if not text:
        return False

    clean_text = re.sub(r"[^\w\s]", "", text.lower()).strip()
    exact_greetings = {
        "radhe radhe", "radhe radhe ji", "radhe krishna",
        "hello", "hlo", "helo", "hi", "hii", "hiii", "hyy", "hy",
        "hello friends", "hlo friends", "hy friends", "hi friends",
        "hlo all", "hello everyone", "hii all", "hi everyone"
    }

    return clean_text in exact_greetings

def is_question_message(text: str) -> bool:
    if not text:
        return False

    lower_text = text.lower().strip()

    casual_phrases = {
        "kya haal hai", "kya haal hai bhai", "kya kar rahe ho", "kya ho raha hai",
        "kya hua", "kya chal raha hai", "kaisa hai", "kaise ho", "kaise ho bhai"
    }
    clean_casual = re.sub(r"[^\w\s]", "", lower_text)
    if clean_casual in casual_phrases:
        return False

    if lower_text in ("doubt", "doubt hai", "question", "question hai", "1 doubt", "sir doubt"):
        return True

    strict_patterns = [
        r"\b(solve|solution|answer|explain|explanation|doubt)\b",
        r"\b(ye kaise hoga|kaise solve kare|kaise solve hoga|iska answer kya|iska solution)\b",
        r"\b(उत्तर|हल|समाधान|डाउट)\b",
    ]

    for pattern in strict_patterns:
        if re.search(pattern, lower_text):
            return True

    if lower_text.endswith("?") or lower_text.endswith("??"):
        question_words = ["kaise", "kya", "kyu", "kyun", "kab", "kahan", "kon", "kaun", "how", "what", "why", "which"]
        if any(w in lower_text.split() for w in question_words):
            return True

    return False

def normalize_badword_text(text: str) -> str:
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r'[@$10!3]', lambda m: {'@':'a', '$':'s', '1':'i', '0':'o', '!':'i', '3':'e'}[m.group(0)], text)
    text = re.sub(r'[^a-z0-9]', '', text)
    text = re.sub(r'(.)\1+', r'\1', text)
    return text

def text_contains_badword(text: str, badwords: list) -> bool:
    if not text or not badwords:
        return False

    normalized_text = normalize_badword_text(text)

    for word in badwords:
        word_str = str(word).strip().lower()
        if not word_str:
            continue

        pattern = r"(?<!\w)" + re.escape(word_str) + r"(?!\w)"
        if re.search(pattern, text, re.IGNORECASE):
            return True

        normalized_word = normalize_badword_text(word_str)
        if normalized_word and normalized_word in normalized_text:
            return True

    return False

# ============================================================
# BASIC COMMANDS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_and_autodelete(update, context, "👋 Hello! Main aapka Group Management Bot hoon.")

async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    config = await get_group_config(update.effective_chat.id)
    await reply_and_autodelete(update, context, html.escape(config["rules"]), parse_mode=ParseMode.HTML)

async def setrules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return
    if not await is_admin(update, context, update.effective_user.id):
        return

    text = " ".join(context.args).strip()
    if not text:
        await reply_and_autodelete(update, context, "❌ Usage: /setrules <rules text>")
        return

    await update_group_config(update.effective_chat.id, "rules", text)
    await reply_and_autodelete(update, context, "✅ Group rules updated successfully!")

async def setwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return
    if not await is_admin(update, context, update.effective_user.id):
        return

    text = " ".join(context.args).strip()
    if not text:
        await reply_and_autodelete(update, context, "❌ Usage: /setwelcome <welcome body>")
        return

    await update_group_config(update.effective_chat.id, "welcome_body", text)
    await reply_and_autodelete(update, context, "✅ Welcome body updated.")

async def setexit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return
    if not await is_admin(update, context, update.effective_user.id):
        return

    text = " ".join(context.args).strip()
    if not text:
        await reply_and_autodelete(update, context, "❌ Usage: /setexit <exit body>")
        return

    await update_group_config(update.effective_chat.id, "exit_body", text)
    await reply_and_autodelete(update, context, "✅ Exit body updated.")

async def setremove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return
    if not await is_admin(update, context, update.effective_user.id):
        return

    text = " ".join(context.args).strip()
    if not text:
        await reply_and_autodelete(update, context, "❌ Usage: /setremove <remove body>")
        return

    await update_group_config(update.effective_chat.id, "remove_body", text)
    await reply_and_autodelete(update, context, "✅ Remove body updated.")

# ============================================================
# MODERATION (WARN / BAN / MUTE)
# ============================================================

async def apply_warning_logic(
    chat_id: int,
    target_user,
    context: ContextTypes.DEFAULT_TYPE,
    reason_prefix: str = ""
) -> None:
    """Centralized Warning Engine: 3 warnings -> Auto Ban (Safe Guarded)."""
    config = await get_group_config(chat_id)
    count = await update_user_warning(chat_id, target_user.id, increment=True)
    target_mention = get_user_mention(target_user)

    if count >= 3:
        try:
            # Check if bot actually has Ban Permissions
            bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
            can_ban = bot_member.status == ChatMemberStatus.ADMINISTRATOR and getattr(bot_member, "can_restrict_members", False)
            
            if can_ban:
                await context.bot.ban_chat_member(chat_id, target_user.id)
                await update_user_warning(chat_id, target_user.id, increment=False)
                safe_name = html.escape(get_name(target_user))
                remove_body = config.get("remove_body", "")
                msg = f"🚫 <b>{safe_name}</b> {html.escape(remove_body)}"
            else:
                msg = f"⚠️ {target_mention} ki 3 warnings ho gayi hain par Bot ke paas 'Ban Permission' nahi hai."
        except TelegramError as error:
            logger.error("Auto-ban failed: %s", error)
            msg = f"⚠️ {target_mention} ki 3 warnings ho gayi hain lekin Auto-Ban execute nahi ho saka."
    elif count == 2:
        prefix = f"{reason_prefix} " if reason_prefix else ""
        msg = f"🚨 {prefix}{target_mention} ko warning di gayi. <b>Warnings: 2/3</b>\n⚠️ <i>Phir se violation karne par aapko BAN kar diya jayega!</i>"
    else:
        prefix = f"{reason_prefix} " if reason_prefix else ""
        msg = f"⚠️ {prefix}{target_mention} ko warning di gayi. <b>Warnings: {count}/3</b>"

    await send_standalone_autodelete(
        chat_id, context, msg, delay=int(config.get("delete_time", 300))
    )

async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not update.message or not update.message.reply_to_message:
        await reply_and_autodelete(update, context, "❌ Member ke message ko reply karke /warn likhein.")
        return

    target = update.message.reply_to_message.from_user
    if not target or target.is_bot:
        return

    chat_id = update.effective_chat.id

    if await is_admin(update, context, target.id):
        await reply_and_autodelete(update, context, "❌ Admin ko warning nahi di ja sakti.")
        return

    await apply_warning_logic(chat_id, target, context)

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not await bot_can_manage_members(update, context):
        await reply_and_autodelete(update, context, "❌ Bot ke paas 'Ban Users' permission nahi hai.")
        return

    if not update.message or not update.message.reply_to_message:
        await reply_and_autodelete(update, context, "❌ Member ke message ko reply karke /ban likhein.")
        return

    target = update.message.reply_to_message.from_user
    if not target or target.is_bot:
        return

    chat_id = update.effective_chat.id

    if await is_admin(update, context, target.id):
        await reply_and_autodelete(update, context, "❌ Admin ko ban nahi kiya ja sakta.")
        return

    config = await get_group_config(chat_id)

    try:
        await context.bot.ban_chat_member(chat_id, target.id)
        safe_name = html.escape(get_name(target))
        msg = f"🚫 <b>{safe_name}</b> {html.escape(config.get('remove_body', ''))}"
    except TelegramError as error:
        msg = f"❌ Ban failed: {html.escape(str(error))}"

    await reply_and_autodelete(update, context, msg)

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not await bot_can_manage_members(update, context):
        await reply_and_autodelete(update, context, "❌ Bot ke paas 'Ban Users' permission nahi hai.")
        return

    if not update.message or not update.message.reply_to_message:
        await reply_and_autodelete(update, context, "❌ Banned member ke message ko reply karke /unban likhein.")
        return

    target = update.message.reply_to_message.from_user
    if not target:
        return

    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target.id, only_if_banned=True)
        safe_name = html.escape(get_name(target))
        msg = f"✅ <b>{safe_name}</b> ko unban kar diya gaya hai."
    except TelegramError as error:
        msg = f"❌ Unban failed: {html.escape(str(error))}"

    await reply_and_autodelete(update, context, msg)

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not await bot_can_manage_members(update, context):
        await reply_and_autodelete(update, context, "❌ Bot ke paas 'Restrict Members' permission nahi hai.")
        return

    if not update.message or not update.message.reply_to_message:
        await reply_and_autodelete(update, context, "❌ Member ke message ko reply karke /mute likhein.")
        return

    target = update.message.reply_to_message.from_user
    if not target or target.is_bot:
        return

    if await is_admin(update, context, target.id):
        await reply_and_autodelete(update, context, "❌ Admin ko mute nahi kiya ja sakta.")
        return

    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id,
            target.id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        safe_name = html.escape(get_name(target))
        msg = f"🔇 <b>{safe_name}</b> ko mute kar diya gaya hai."
    except TelegramError as error:
        msg = f"❌ Mute failed: {html.escape(str(error))}"

    await reply_and_autodelete(update, context, msg)

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not await bot_can_manage_members(update, context):
        await reply_and_autodelete(update, context, "❌ Bot ke paas 'Restrict Members' permission nahi hai.")
        return

    if not update.message or not update.message.reply_to_message:
        await reply_and_autodelete(update, context, "❌ Member ke message ko reply karke /unmute likhein.")
        return

    target = update.message.reply_to_message.from_user
    if not target:
        return

    try:
        # Full Comprehensive Member Permission Restoration
        full_permissions = ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_change_info=False,
            can_invite_users=True,
            can_pin_messages=False,
        )
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target.id,
            permissions=full_permissions,
        )
        safe_name = html.escape(get_name(target))
        msg = f"🔊 <b>{safe_name}</b> ko unmute kar diya gaya hai."
    except TelegramError as error:
        msg = f"❌ Unmute failed: {html.escape(str(error))}"

    await reply_and_autodelete(update, context, msg)

# ============================================================
# AUTO DELETE & BADWORD COMMANDS
# ============================================================

async def autodelete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        await reply_and_autodelete(update, context, "❌ Usage: /autodelete on OR /autodelete off")
        return

    value = context.args[0].lower() == "on"
    await update_group_config(update.effective_chat.id, "autodelete", value)
    await reply_and_autodelete(update, context, f"✅ Auto-delete: {'ON' if value else 'OFF'}")

async def setdeletetime_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not context.args or not context.args[0].isdigit() or int(context.args[0]) < 0:
        await reply_and_autodelete(update, context, "❌ Usage: /setdeletetime <minutes>")
        return

    minutes = int(context.args[0])
    await update_group_config(update.effective_chat.id, "delete_time", minutes * 60)
    await reply_and_autodelete(update, context, f"✅ Bot message deletion time: {minutes} minutes.")

async def autoreplydelete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        await reply_and_autodelete(update, context, "❌ Usage: /autoreplydelete on OR /autoreplydelete off")
        return

    value = context.args[0].lower() == "on"
    await update_group_config(update.effective_chat.id, "autoreplydelete", value)
    await reply_and_autodelete(update, context, f"✅ Auto-reply delete: {'ON' if value else 'OFF'}")

async def setreplydelete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not context.args or not context.args[0].isdigit() or int(context.args[0]) < 0:
        await reply_and_autodelete(update, context, "❌ Usage: /setreplydelete <minutes>")
        return

    minutes = int(context.args[0])
    await update_group_config(update.effective_chat.id, "reply_delete_time", minutes * 60)
    await reply_and_autodelete(update, context, f"✅ Auto-reply deletion time: {minutes} minutes.")

async def antibadword_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        await reply_and_autodelete(update, context, "❌ Usage: /antibadword on OR /antibadword off")
        return

    value = context.args[0].lower() == "on"
    await update_group_config(update.effective_chat.id, "antibadword", value)
    await reply_and_autodelete(update, context, f"✅ Anti-badword: {'ON' if value else 'OFF'}")

async def addbadword_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    word = " ".join(context.args).strip().lower()
    if not word:
        await reply_and_autodelete(update, context, "❌ Usage: /addbadword <word>")
        return

    config = await get_group_config(update.effective_chat.id)
    badwords = list(config.get("badwords", []))

    if word not in badwords:
        badwords.append(word)
        await update_group_config(update.effective_chat.id, "badwords", badwords)

    await reply_and_autodelete(update, context, f"✅ Badword added: {html.escape(word)}")

async def delbadword_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    word = " ".join(context.args).strip().lower()
    if not word:
        await reply_and_autodelete(update, context, "❌ Usage: /delbadword <word>")
        return

    config = await get_group_config(update.effective_chat.id)
    badwords = list(config.get("badwords", []))

    if word in badwords:
        badwords.remove(word)
        await update_group_config(update.effective_chat.id, "badwords", badwords)

    await reply_and_autodelete(update, context, f"✅ Badword removed: {html.escape(word)}")

async def listbadwords_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    config = await get_group_config(update.effective_chat.id)
    badwords = config.get("badwords", [])

    if not badwords:
        text = "📜 Badwords list is empty."
    else:
        text = "📜 Badwords List:\n" + "\n".join(f"• {html.escape(word)}" for word in badwords)

    await reply_and_autodelete(update, context, text)

# ============================================================
# SAFE SLOW MODE NOTIFICATION COMMAND
# ============================================================

async def slowmode_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Safe Info Handler for Slowmode as Telegram Bot API does not expose direct Bot method."""
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    msg = (
        "ℹ️ <b>Slow Mode Information:</b>\n"
        "Telegram Bot API limits slow-mode changes directly via Bots.\n"
        "Please set Slow Mode directly from <b>Group Settings -> Permissions -> Slow Mode</b> in Telegram."
    )
    await reply_and_autodelete(update, context, msg)

# ============================================================
# EVENT HANDLERS (WELCOME / EXIT / REMOVE)
# ============================================================

async def send_welcome(chat_id: int, user, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not user or user.is_bot:
        return

    if await event_seen_recently(chat_id, user.id, "join"):
        return

    config = await get_group_config(chat_id)
    safe_name = html.escape(get_name(user))
    text = f"🎉 Welcome <b>{safe_name}</b>! 👋❤️\n\n{html.escape(config.get('welcome_body', ''))}"

    try:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        if config.get("autodelete", True):
            schedule_auto_delete(context, chat_id, sent.message_id, int(config.get("delete_time", 300)))
    except TelegramError as error:
        logger.error("Welcome message failed in %s: %s", chat_id, error)

async def send_exit(chat_id: int, user, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not user or user.is_bot:
        return

    if await event_seen_recently(chat_id, user.id, "leave"):
        return

    config = await get_group_config(chat_id)
    safe_name = html.escape(get_name(user))
    text = f"👋 Goodbye <b>{safe_name}</b>! ❤️\n\n{html.escape(config.get('exit_body', ''))}"

    try:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        if config.get("autodelete", True):
            schedule_auto_delete(context, chat_id, sent.message_id, int(config.get("delete_time", 300)))
    except TelegramError as error:
        logger.error("Exit message failed in %s: %s", chat_id, error)

async def send_remove_message(chat_id: int, user, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not user or user.is_bot:
        return

    if await event_seen_recently(chat_id, user.id, "remove"):
        return

    config = await get_group_config(chat_id)
    safe_name = html.escape(get_name(user))
    text = f"🚫 <b>{safe_name}</b> {html.escape(config.get('remove_body', ''))}"

    try:
        sent = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        if config.get("autodelete", True):
            schedule_auto_delete(context, chat_id, sent.message_id, int(config.get("delete_time", 300)))
    except TelegramError as error:
        logger.error("Remove message failed in %s: %s", chat_id, error)

def member_status_is_member(status: str) -> bool:
    return status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
        ChatMemberStatus.RESTRICTED,
    )

async def handle_member_transition(
    chat_id: int,
    old_status: str,
    new_status: str,
    user,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not user or user.is_bot:
        return

    was_member = member_status_is_member(old_status)
    is_member = member_status_is_member(new_status)

    joined = not was_member and is_member
    left = was_member and new_status == ChatMemberStatus.LEFT
    banned = was_member and new_status == ChatMemberStatus.BANNED

    safe_name = html.escape(get_name(user))

    # Detect Admin Promotions / Demotions
    if old_status != ChatMemberStatus.ADMINISTRATOR and new_status == ChatMemberStatus.ADMINISTRATOR:
        msg = f"⭐ <b>{safe_name}</b> ko Admin promote kar diya gaya hai!"
        await send_standalone_autodelete(chat_id, context, msg)
        return
    elif old_status == ChatMemberStatus.ADMINISTRATOR and new_status != ChatMemberStatus.ADMINISTRATOR:
        msg = f"📉 <b>{safe_name}</b> ko Admin status se demote kar diya gaya hai."
        await send_standalone_autodelete(chat_id, context, msg)
        return

    if joined:
        await send_welcome(chat_id, user, context)
    elif left:
        await send_exit(chat_id, user, context)
    elif banned:
        await send_remove_message(chat_id, user, context)

async def chat_member_updated_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    member_update = update.chat_member or update.my_chat_member

    if not member_update or not update.effective_chat:
        return

    old_status = member_update.old_chat_member.status
    new_status = member_update.new_chat_member.status
    user = member_update.new_chat_member.user

    await handle_member_transition(update.effective_chat.id, old_status, new_status, user, context)

# ============================================================
# FLOOD CONTROL ENGINE
# ============================================================

async def check_and_handle_flood(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user,
    config: Dict[str, Any]
) -> bool:
    if not config.get("floodcontrol", True):
        return False

    limit = int(config.get("flood_limit", 5))
    window = int(config.get("flood_window", 10))
    now = asyncio.get_running_loop().time()
    key = (chat_id, user.id)

    async with FLOOD_LOCK:
        if len(FLOOD_TRACKER) > 1000:
            for k in list(FLOOD_TRACKER.keys()):
                FLOOD_TRACKER[k] = [t for t in FLOOD_TRACKER[k] if now - t < window]
                if not FLOOD_TRACKER[k]:
                    FLOOD_TRACKER.pop(k, None)

        user_timestamps = FLOOD_TRACKER.get(key, [])
        user_timestamps = [t for t in user_timestamps if now - t < window]
        user_timestamps.append(now)
        FLOOD_TRACKER[key] = user_timestamps

        if len(user_timestamps) > limit:
            try:
                if update.message:
                    await update.message.delete()
            except TelegramError:
                pass

            if len(user_timestamps) == limit + 1:
                await apply_warning_logic(
                    chat_id, user, context, reason_prefix="⚠️ Spam/Flood karne ke karan"
                )
            return True

    return False

# ============================================================
# MAIN MESSAGE PROCESSOR & MODERATION
# ============================================================

async def process_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat or not update.message.from_user:
        return

    user = update.message.from_user
    if user.is_bot:
        return

    chat_id = update.effective_chat.id
    config = await get_group_config(chat_id)
    text = update.message.text or update.message.caption or ""
    admin = await is_admin(update, context, user.id)

    # 1.     # 1. FLOOD CONTROL (EXEMPT ADMINS + PHOTOS)
    if not admin and not update.message.photo:
        is_flooding = await check_and_handle_flood(
            update, context, chat_id, user, config
        )
        if is_flooding:
            return
    

    # 2. ANTI-LINK SYSTEM (EXEMPT ADMINS)
    if not admin and contains_disallowed_link(update, text):
        try:
            await update.message.delete()
        except TelegramError as error:
            logger.warning("Link message delete failed: %s", error)

        await apply_warning_logic(
            chat_id, user, context, reason_prefix="⚠️ Disallowed link share karne par"
        )
        return

    # 3. ANTI-BADWORD SYSTEM (EXEMPT ADMINS)
    if config.get("antibadword", True) and not admin and text:
        badwords = config.get("badwords", [])
        if text_contains_badword(text, badwords):
            try:
                await update.message.delete()
            except TelegramError as error:
                logger.warning("Badword message delete failed: %s", error)

            # Delete + Increment Warning + Auto-Ban on 3rd Warning
            await apply_warning_logic(
                chat_id, user, context, reason_prefix="⚠️ Badwords use karne par"
            )
            return

    # 4. GREETING AUTO-REPLIES
    if text and is_greeting_message(text):
        clean_text = re.sub(r"[^\w\s]", "", text.lower()).strip()
        if "radhe" in clean_text:
            reply_txt = "Radhe Radhe 🙏❤️"
        else:
            reply_txt = "Hlo 😊 Aap kaise ho? ❤️"

        await reply_and_autodelete(update, context, reply_txt, is_reply_type=True)
        return

    # 5. PHOTO AUTO-REPLY LOGIC (PHOTO + CAPTION QUESTION)
    if update.message.photo:
        caption = update.message.caption or ""
        if caption and is_question_message(caption):
            await reply_and_autodelete(
                update,
                context,
                "📸 Chinta mat karo! 😊\n📝 Is question ka solution aapko bahut jald milega. ❤️",
                is_reply_type=True,
            )
        return

    # 6. TEXT QUESTION / DOUBT AUTO-REPLY
    if text and is_question_message(text):
        await reply_and_autodelete(
            update,
            context,
            "🤔 Doubt hai? Vishesh bhai se pucho! 📚❤️",
            is_reply_type=True,
        )
        return

# ============================================================
# GLOBAL ERROR HANDLER
# ============================================================

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing update: %s", update, exc_info=context.error)

# ============================================================
# MAIN APPLICATION BUILDER
# ============================================================

def main() -> None:
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable is missing. Set BOT_TOKEN before starting the bot.")
        return

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_error_handler(global_error_handler)

    # BASIC COMMANDS
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("rules", rules_command))
    application.add_handler(CommandHandler("setrules", setrules_command))
    application.add_handler(CommandHandler("setwelcome", setwelcome_command))
    application.add_handler(CommandHandler("setexit", setexit_command))
    application.add_handler(CommandHandler("setremove", setremove_command))

    # MODERATION
    application.add_handler(CommandHandler("warn", warn_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))

    # PROTECTION / AUTO DELETE
    application.add_handler(CommandHandler("autodelete", autodelete_command))
    application.add_handler(CommandHandler("setdeletetime", setdeletetime_command))
    application.add_handler(CommandHandler("autoreplydelete", autoreplydelete_command))
    application.add_handler(CommandHandler("setreplydelete", setreplydelete_command))
    application.add_handler(CommandHandler("antibadword", antibadword_command))
    application.add_handler(CommandHandler("addbadword", addbadword_command))
    application.add_handler(CommandHandler("delbadword", delbadword_command))
    application.add_handler(CommandHandler("listbadwords", listbadwords_command))

    # SLOW MODE INFO
    application.add_handler(CommandHandler("slowmode", slowmode_info_command))

    # MEMBER & GROUP EVENTS
    application.add_handler(ChatMemberHandler(chat_member_updated_handler, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(ChatMemberHandler(chat_member_updated_handler, ChatMemberHandler.MY_CHAT_MEMBER))

    # GENERAL MESSAGES & MODERATION
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, process_messages))

    logger.info("Bot starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
