import os
import re
import json
import copy
import uuid
import shutil
import asyncio
import logging
from typing import Any, Dict, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, ChatPermissions, Message
from telegram.constants import ChatMemberStatus, ChatType
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

# Safe Telegram API supported slowmode delays
SAFE_SLOWMODE_DELAYS = (0, 10, 30, 60, 300, 900, 3600, 21600)
DEFAULT_SLOWMODE_DELAY = 300  # Fallback to 5 minutes

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
    "antilink": False,
    "antibadword": False,
    "badwords": [],
    # Slow-mode settings
    "slowmode": True,
    "slowmode_delay": DEFAULT_SLOWMODE_DELAY,
    # Auto slow schedule (IST)
    "autoslow": True,
    "autoslow_off_time": "20:00",
    "autoslow_on_time": "22:00",
    # Warnings: {user_id: count}
    "warnings": {},
}

DB_LOCK = asyncio.Lock()

EVENT_DEDUPE: Dict[str, float] = {}
EVENT_DEDUPE_LOCK = asyncio.Lock()

# ============================================================
# DATABASE (ATOMIC & CORRUPTION BACKUP SAFE)
# ============================================================

def default_group_config() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_GROUP_SETTINGS)

def sanitize_slowmode_delay(seconds: int) -> int:
    """Strictly returns a supported slowmode value or the default fallback."""
    if seconds in SAFE_SLOWMODE_DELAYS:
        return seconds
    return DEFAULT_SLOWMODE_DELAY

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

        if not isinstance(config.get("badwords"), list):
            config["badwords"] = []
            changed = True

        if not isinstance(config.get("warnings"), dict):
            config["warnings"] = {}
            changed = True

        current_delay = config.get("slowmode_delay", DEFAULT_SLOWMODE_DELAY)
        sanitized_delay = sanitize_slowmode_delay(int(current_delay) if str(current_delay).isdigit() else DEFAULT_SLOWMODE_DELAY)
        if current_delay != sanitized_delay:
            config["slowmode_delay"] = sanitized_delay
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
# HELPERS & PERMISSION CHECKS
# ============================================================

def get_name(user) -> str:
    if not user:
        return "Member"
    return (
        getattr(user, "first_name", None)
        or getattr(user, "full_name", None)
        or "Member"
    )

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
) -> Optional[Message]:
    if not update.effective_chat or not update.effective_message:
        return None

    chat_id = update.effective_chat.id
    config = await get_group_config(chat_id)

    try:
        sent = await update.effective_message.reply_text(
            text,
            disable_web_page_preview=True,
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
    delay: int = 300
) -> Optional[Message]:
    try:
        sent = await context.bot.send_message(chat_id=chat_id, text=text)
        schedule_auto_delete(context, chat_id, sent.message_id, delay)
        return sent
    except TelegramError as error:
        logger.error("Standalone message send failed in chat %s: %s", chat_id, error)
        return None

def valid_hhmm(value: str) -> bool:
    if not re.fullmatch(r"\d{2}:\d{2}", value):
        return False
    try:
        hour, minute = map(int, value.split(":"))
        return 0 <= hour <= 23 and 0 <= minute <= 59
    except ValueError:
        return False

def is_slowmode_off_window(now_str: str, off_time: str, on_time: str) -> bool:
    if off_time <= on_time:
        return off_time <= now_str < on_time
    return now_str >= off_time or now_str < on_time

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
# SLOW MODE SETTER & SCHEDULER ENGINE
# ============================================================

async def try_set_slowmode(
    chat_id: int,
    seconds: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    sanitized_seconds = sanitize_slowmode_delay(seconds) if seconds > 0 else 0
    try:
        await context.bot.set_chat_slow_mode_delay(
            chat_id=chat_id,
            slow_mode_delay=sanitized_seconds,
        )
        return True
    except TelegramError as error:
        logger.warning("Slow-mode API call failed for chat %s with %ss: %s", chat_id, sanitized_seconds, error)
        return False

async def apply_auto_slow_state_for_chat(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = await get_group_config(chat_id)
    if not config.get("autoslow", False):
        return

    now_str = datetime.now(TIMEZONE).strftime("%H:%M")
    off_time = config.get("autoslow_off_time", "20:00")
    on_time = config.get("autoslow_on_time", "22:00")

    should_be_off = is_slowmode_off_window(now_str, off_time, on_time)
    target_slowmode = not should_be_off

    raw_delay = int(config.get("slowmode_delay", DEFAULT_SLOWMODE_DELAY))
    delay = sanitize_slowmode_delay(raw_delay) if target_slowmode else 0

    success = await try_set_slowmode(chat_id, delay, context)
    if success:
        await update_group_config(chat_id, "slowmode", target_slowmode)

# ============================================================
# STARTUP INITIALIZATION
# ============================================================

async def on_startup(application: Application) -> None:
    async with DB_LOCK:
        data = _raw_load_database()

    groups = data.get("groups", {})
    now_str = datetime.now(TIMEZONE).strftime("%H:%M")

    class SimpleContext:
        def __init__(self, bot):
            self.bot = bot

    dummy_context = SimpleContext(application.bot)

    for chat_key, config in groups.items():
        try:
            chat_id = int(chat_key)
            autoslow_enabled = config.get("autoslow", False)
            slowmode_enabled = config.get("slowmode", False)

            if autoslow_enabled:
                off_time = config.get("autoslow_off_time", "20:00")
                on_time = config.get("autoslow_on_time", "22:00")
                should_be_off = is_slowmode_off_window(now_str, off_time, on_time)
                target_slowmode = not should_be_off
                raw_delay = int(config.get("slowmode_delay", DEFAULT_SLOWMODE_DELAY))
                delay = sanitize_slowmode_delay(raw_delay) if target_slowmode else 0

                await update_group_config(chat_id, "slowmode", target_slowmode)
                await try_set_slowmode(chat_id, delay, dummy_context)
            elif slowmode_enabled:
                raw_delay = int(config.get("slowmode_delay", DEFAULT_SLOWMODE_DELAY))
                seconds = sanitize_slowmode_delay(raw_delay)
                await try_set_slowmode(chat_id, seconds, dummy_context)
        except Exception as error:
            logger.error("Startup slowmode setup error for %s: %s", chat_key, error)

# ============================================================
# BASIC COMMANDS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_and_autodelete(update, context, "👋 Hello! Main aapka Group Management Bot hoon.")

async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    config = await get_group_config(update.effective_chat.id)
    await reply_and_autodelete(update, context, config["rules"])

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
# MODERATION
# ============================================================

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

    config = await get_group_config(chat_id)
    count = await update_user_warning(chat_id, target.id, increment=True)

    if count >= 3:
        if not await bot_can_manage_members(update, context):
            await reply_and_autodelete(
                update,
                context,
                f"⚠️ {get_name(target)} ki 3 warnings ho gayi hain, "
                "lekin Bot ke paas 'Ban Users' permission missing hai. "
                "Permission dene ke baad dobara /warn karein."
            )
            return

        try:
            await context.bot.ban_chat_member(chat_id, target.id)
            await update_user_warning(chat_id, target.id, increment=False)
            name = get_name(target)
            remove_body = config.get("remove_body", "")
            msg = f"🚫 {name} {remove_body}"
        except TelegramError as error:
            msg = f"❌ Auto-ban failed: {getattr(error, 'message', str(error))}"
    else:
        msg = f"⚠️ {get_name(target)} ko warning di gayi. Warnings: {count}/3"

    await reply_and_autodelete(update, context, msg)

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
        msg = f"🚫 {get_name(target)} {config.get('remove_body', '')}"
    except TelegramError as error:
        msg = f"❌ Ban failed: {getattr(error, 'message', str(error))}"

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
        msg = f"✅ {get_name(target)} ko unban kar diya gaya hai."
    except TelegramError as error:
        msg = f"❌ Unban failed: {getattr(error, 'message', str(error))}"

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
        msg = f"🔇 {get_name(target)} ko mute kar diya gaya hai."
    except TelegramError as error:
        msg = f"❌ Mute failed: {getattr(error, 'message', str(error))}"

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
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target.id,
            permissions=ChatPermissions.all_permissions(),
        )
        msg = f"🔊 {get_name(target)} ko unmute kar diya gaya hai."
    except TelegramError as error:
        msg = f"❌ Unmute failed: {getattr(error, 'message', str(error))}"

    await reply_and_autodelete(update, context, msg)

# ============================================================
# AUTO DELETE / PROTECTION
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

async def antilink_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        await reply_and_autodelete(update, context, "❌ Usage: /antilink on OR /antilink off")
        return

    value = context.args[0].lower() == "on"
    await update_group_config(update.effective_chat.id, "antilink", value)
    await reply_and_autodelete(update, context, f"✅ Anti-link: {'ON' if value else 'OFF'}")

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

    await reply_and_autodelete(update, context, f"✅ Badword added: {word}")

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

    await reply_and_autodelete(update, context, f"✅ Badword removed: {word}")

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
        text = "📜 Badwords List:\n" + "\n".join(f"• {word}" for word in badwords)

    await reply_and_autodelete(update, context, text)

# ============================================================
# SLOW MODE COMMANDS
# ============================================================

async def slowmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        await reply_and_autodelete(update, context, "❌ Usage: /slowmode on OR /slowmode off")
        return

    enabled = context.args[0].lower() == "on"
    chat_id = update.effective_chat.id
    config = await get_group_config(chat_id)

    raw_seconds = int(config.get("slowmode_delay", DEFAULT_SLOWMODE_DELAY))
    seconds = sanitize_slowmode_delay(raw_seconds)
    target_delay = seconds if enabled else 0

    try:
        await context.bot.set_chat_slow_mode_delay(chat_id=chat_id, slow_mode_delay=target_delay)
        await update_group_config(chat_id, "slowmode", enabled)
        status = f"ON ({seconds} seconds)" if enabled else "OFF"
        msg = f"✅ Slow mode set to {status}."
    except TelegramError as error:
        logger.warning("Slowmode toggle error for %s: %s", chat_id, error)
        msg = f"❌ Telegram API error: {getattr(error, 'message', str(error))}"

    await reply_and_autodelete(update, context, msg)

async def setslowtime_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not context.args or not context.args[0].isdigit():
        await reply_and_autodelete(
            update,
            context,
            "❌ Usage: /setslowtime <seconds>\nAllowed values: 0, 10, 30, 60, 300, 900, 3600, 21600",
        )
        return

    seconds = int(context.args[0])

    if seconds not in SAFE_SLOWMODE_DELAYS:
        allowed = ", ".join(map(str, SAFE_SLOWMODE_DELAYS))
        await reply_and_autodelete(
            update,
            context,
            f"❌ Invalid delay! Allowed values (seconds):\n{allowed}",
        )
        return

    chat_id = update.effective_chat.id

    try:
        await context.bot.set_chat_slow_mode_delay(chat_id=chat_id, slow_mode_delay=seconds)
        await update_group_config(chat_id, "slowmode_delay", seconds)
        await update_group_config(chat_id, "slowmode", seconds > 0)

        status_msg = f"set to {seconds} seconds." if seconds > 0 else "disabled."
        await reply_and_autodelete(update, context, f"✅ Slow mode delay {status_msg}")
    except TelegramError as error:
        logger.warning("Slowmode update failed in chat %s: %s", chat_id, error)
        err_msg = getattr(error, "message", str(error))
        await reply_and_autodelete(
            update,
            context,
            f"❌ Telegram ne is slowmode delay value ko reject kar diya: {err_msg}"
        )

async def autoslow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        await reply_and_autodelete(update, context, "❌ Usage: /autoslow on OR /autoslow off")
        return

    enabled = context.args[0].lower() == "on"
    chat_id = update.effective_chat.id

    await update_group_config(chat_id, "autoslow", enabled)
    if enabled:
        await apply_auto_slow_state_for_chat(chat_id, context)

    await reply_and_autodelete(update, context, f"✅ Auto Slow schedule: {'ON' if enabled else 'OFF'}")

async def setslowschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if len(context.args) < 2:
        await reply_and_autodelete(update, context, "❌ Usage: /setslowschedule HH:MM HH:MM")
        return

    off_time = context.args[0]
    on_time = context.args[1]

    if not valid_hhmm(off_time) or not valid_hhmm(on_time):
        await reply_and_autodelete(update, context, "❌ Time format invalid. Example: /setslowschedule 20:00 22:00")
        return

    chat_id = update.effective_chat.id
    await update_group_config(chat_id, "autoslow_off_time", off_time)
    await update_group_config(chat_id, "autoslow_on_time", on_time)
    await update_group_config(chat_id, "autoslow", True)

    await apply_auto_slow_state_for_chat(chat_id, context)

    await reply_and_autodelete(
        update,
        context,
        f"✅ Auto Slow schedule updated & applied immediately!\nOFF: {off_time} IST\nON: {on_time} IST",
    )

async def check_auto_slow_schedule(context: ContextTypes.DEFAULT_TYPE) -> None:
    now_str = datetime.now(TIMEZONE).strftime("%H:%M")

    async with DB_LOCK:
        data = _raw_load_database()

    for chat_key, config in data.get("groups", {}).items():
        if not config.get("autoslow", False):
            continue

        try:
            chat_id = int(chat_key)
            off_time = config.get("autoslow_off_time", "20:00")
            on_time = config.get("autoslow_on_time", "22:00")

            should_be_off = is_slowmode_off_window(now_str, off_time, on_time)
            target_slowmode = not should_be_off

            current_slowmode = config.get("slowmode", False)

            if target_slowmode != current_slowmode:
                raw_delay = int(config.get("slowmode_delay", DEFAULT_SLOWMODE_DELAY))
                delay = sanitize_slowmode_delay(raw_delay) if target_slowmode else 0

                success = await try_set_slowmode(chat_id, delay, context)
                if success:
                    await update_group_config(chat_id, "slowmode", target_slowmode)
        except Exception as error:
            logger.error("Auto slow schedule failed for %s: %s", chat_key, error)

# ============================================================
# WELCOME / EXIT / REMOVE
# ============================================================

async def send_welcome(chat_id: int, user, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not user or user.is_bot:
        return

    if await event_seen_recently(chat_id, user.id, "join"):
        return

    config = await get_group_config(chat_id)
    name = get_name(user)
    text = f"🎉 Welcome {name}! 👋❤️\n\n{config.get('welcome_body', '')}"

    try:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
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
    name = get_name(user)
    text = f"👋 Goodbye {name}! ❤️\n\n{config.get('exit_body', '')}"

    try:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
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
    name = get_name(user)
    text = f"🚫 {name} {config.get('remove_body', '')}"

    try:
        sent = await context.bot.send_message(chat_id=chat_id, text=text)
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

    if joined:
        await send_welcome(chat_id, user, context)
    elif left:
        await send_exit(chat_id, user, context)
    elif banned:
        await send_remove_message(chat_id, user, context)

async def chat_member_updated_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    member_update = update.chat_member

    if not member_update or not update.effective_chat:
        return

    old_status = member_update.old_chat_member.status
    new_status = member_update.new_chat_member.status
    user = member_update.new_chat_member.user

    await handle_member_transition(update.effective_chat.id, old_status, new_status, user, context)

# ============================================================
# MESSAGE FILTERS / AUTO REPLIES
# ============================================================

def text_contains_badword(text: str, badwords: list) -> bool:
    if not text or not badwords:
        return False

    for word in badwords:
        word = str(word).strip()
        if not word:
            continue

        pattern = r"(?<!\w)" + re.escape(word) + r"(?!\w)"
        if re.search(pattern, text, re.IGNORECASE):
            return True

    return False

def message_contains_link(update: Update, text: str) -> bool:
    link_regex = (
        r"(https?://|http://|www\.|"
        r"t\.me/|telegram\.me/|"
        r"\b[a-zA-Z0-9-]+\.(?:com|net|org|in|co|io|me|xyz|info|biz)\b)"
    )

    if text and re.search(link_regex, text, re.IGNORECASE):
        return True

    message = update.message

    for entity_list_name in ("entities", "caption_entities"):
        entities = getattr(message, entity_list_name, None)
        if entities:
            for entity in entities:
                if entity.type in ("url", "text_link"):
                    return True

    return False

async def process_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat or not update.message.from_user:
        return

    user = update.message.from_user
    if user.is_bot:
        return

    chat_id = update.effective_chat.id
    config = await get_group_config(chat_id)
    first_name = get_name(user)
    text = update.message.text or update.message.caption or ""
    admin = await is_admin(update, context, user.id)

    # ANTI BADWORD
    if config.get("antibadword", False) and not admin and text:
        badwords = config.get("badwords", [])
        if text_contains_badword(text, badwords):
            await send_standalone_autodelete(
                chat_id,
                context,
                f"⚠️ {first_name}, badwords allow nahi hain!",
                int(config.get("delete_time", 300)),
            )
            try:
                await update.message.delete()
            except TelegramError as error:
                logger.warning("Badword message delete failed: %s", error)
            return

    # ANTI LINK
    if config.get("antilink", False) and not admin and message_contains_link(update, text):
        await send_standalone_autodelete(
            chat_id,
            context,
            f"⚠️ {first_name}, links allow nahi hain!",
            int(config.get("delete_time", 300)),
        )
        try:
            await update.message.delete()
        except TelegramError as error:
            logger.warning("Link message delete failed: %s", error)
        return

    # PHOTO AUTO REPLY
    if update.message.photo:
        await reply_and_autodelete(
            update,
            context,
            "📸 Chinta mat karo! 😊\n📝 Is question ka solution aapko bahut jald milega. ❤️",
            is_reply_type=True,
        )
        return

    # TEXT AUTO REPLIES
    lower = text.lower().strip()

    if lower in ("doubt", "doubt hai", "question", "question hai"):
        await reply_and_autodelete(update, context, "🤔 Doubt hai? Vishesh bhai se pucho! 📚❤️", is_reply_type=True)
        return

    if lower in ("hi", "hii", "hiii"):
        await reply_and_autodelete(update, context, f"👋 Hii {first_name}! ❤️", is_reply_type=True)
        return

    if lower in ("hello", "helo"):
        await reply_and_autodelete(update, context, f"😊 Hello {first_name}! ❤️", is_reply_type=True)
        return

    if lower in ("good morning", "gm"):
        await reply_and_autodelete(update, context, "🌅 Good Morning! ☀️❤️ Have a great day! 📚✨", is_reply_type=True)
        return

    if lower in ("good night", "gn"):
        await reply_and_autodelete(update, context, "🌙 Good Night! 😴❤️ Sweet Dreams! ✨", is_reply_type=True)
        return

# ============================================================
# GLOBAL ERROR HANDLER
# ============================================================

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing update: %s", update, exc_info=context.error)

# ============================================================
# MAIN
# ============================================================

def main() -> None:
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable is missing. Set BOT_TOKEN before starting the bot.")
        return

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
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
    application.add_handler(CommandHandler("antilink", antilink_command))
    application.add_handler(CommandHandler("antibadword", antibadword_command))
    application.add_handler(CommandHandler("addbadword", addbadword_command))
    application.add_handler(CommandHandler("delbadword", delbadword_command))
    application.add_handler(CommandHandler("listbadwords", listbadwords_command))

    # SLOW MODE
    application.add_handler(CommandHandler("slowmode", slowmode_command))
    application.add_handler(CommandHandler("setslowtime", setslowtime_command))
    application.add_handler(CommandHandler("autoslow", autoslow_command))
    application.add_handler(CommandHandler("setslowschedule", setslowschedule_command))

    # MEMBER EVENTS
    application.add_handler(ChatMemberHandler(chat_member_updated_handler, ChatMemberHandler.CHAT_MEMBER))

    # GENERAL MESSAGES
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, process_messages))

    # AUTO-SLOW JOB
    if application.job_queue:
        application.job_queue.run_repeating(check_auto_slow_schedule, interval=60, first=10)
    else:
        logger.warning("JobQueue unavailable. Install APScheduler support for automatic slow schedule.")

    logger.info("Bot starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
