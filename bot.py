import os
import re
import copy
import json
import time
import asyncio
import logging
import html
from typing import Dict, List, Tuple, Any

from telegram import Update, ChatPermissions
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ====================================================
# 0. LOGGING
# ====================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ====================================================
# 1. CONFIGURATION
# ====================================================
DB_FILE = "database.json"

MAX_TRACKED_MESSAGES_PER_USER = 100

FLOOD_TIMEFRAME = 5.0
FLOOD_MSG_LIMIT = 5
FLOOD_COOLDOWN_SEC = 10.0

MIN_DELETE_TIME = 5
MAX_DELETE_TIME = 86400

DEFAULT_GROUP_SETTINGS: Dict[str, Any] = {
    "welcome_text": "Welcome {mention} to {chat_title}!",
    "exit_text": "Goodbye {name}!",
    "remove_text": "{name} was removed/banned from {chat_title}.",
    "rules": "No rules have been set for this group yet.",

    "auto_delete": True,
    "delete_time": 300,
    "auto_reply_delete": True,
    "reply_delete_time": 120,

    "anti_badword": True,
    "badwords": [
        "bc", "mc", "bhosdike", "bhenchod", "madarchod",
        "gandu", "chutiya", "gaand", "saala", "harami",
        "fuck", "bitch", "bastard", "asshole",
    ],

    "warn_limit": 3,
    "user_warnings": {},

    "report_system": True,
    "report_limit": 5,
    "reports": {},
}

DB_CONFIGS: Dict[int, Dict[str, Any]] = {}
DB_LOCK = asyncio.Lock()

USER_MESSAGE_TRACKER: Dict[Tuple[int, int], List[int]] = {}
USER_MESSAGE_TRACKER_LOCK = asyncio.Lock()

FLOOD_TRACKER: Dict[Tuple[int, int], List[float]] = {}
FLOOD_COOLDOWN_TRACKER: Dict[Tuple[int, int], float] = {}
FLOOD_LOCK = asyncio.Lock()

WARN_LOCKS: Dict[Tuple[int, int], asyncio.Lock] = {}
REPORT_LOCKS: Dict[Tuple[int, int], asyncio.Lock] = {}
LOCKS_GUARD = asyncio.Lock()

ADMIN_CACHE: Dict[Tuple[int, int], Tuple[bool, float]] = {}
ADMIN_CACHE_TTL = 30.0
ADMIN_CACHE_LOCK = asyncio.Lock()

# Only actual URLs / invite links are matched.
RESTRICTED_LINKS_REGEX = re.compile(
    r"(?:"
    r"https?://(?:www\.)?"
    r"(?:instagram\.com|t\.me|telegram\.me|whatsapp\.com|"
    r"chat\.whatsapp\.com|wa\.me|facebook\.com|fb\.me)"
    r"(?:/[^\s]*)?"
    r"|"
    r"(?:www\.)?"
    r"(?:instagram\.com|t\.me|telegram\.me|whatsapp\.com|"
    r"chat\.whatsapp\.com|wa\.me|facebook\.com|fb\.me)"
    r"/[^\s]+"
    r")",
    re.IGNORECASE,
)

# ====================================================
# 2. DATABASE
# ====================================================
def load_db_from_file() -> None:
    global DB_CONFIGS

    if not os.path.exists(DB_FILE):
        DB_CONFIGS = {}
        return

    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("database.json must contain an object")

        DB_CONFIGS = {int(k): v for k, v in data.items()}
        logger.info("database.json loaded successfully.")
    except Exception as e:
        logger.error("Failed to load database.json: %s", e)
        DB_CONFIGS = {}


def _write_db_to_disk() -> None:
    tmp_file = DB_FILE + ".tmp"

    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(
                {str(k): v for k, v in DB_CONFIGS.items()},
                f,
                indent=4,
                ensure_ascii=False,
            )

        os.replace(tmp_file, DB_FILE)
    except Exception as e:
        logger.error("Failed writing database.json: %s", e)
        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
        except OSError:
            pass


load_db_from_file()


async def get_group_config(chat_id: int) -> Dict[str, Any]:
    async with DB_LOCK:
        if chat_id not in DB_CONFIGS:
            DB_CONFIGS[chat_id] = copy.deepcopy(DEFAULT_GROUP_SETTINGS)
            _write_db_to_disk()

        config = DB_CONFIGS[chat_id]
        changed = False

        for key, default_value in DEFAULT_GROUP_SETTINGS.items():
            if key not in config:
                config[key] = copy.deepcopy(default_value)
                changed = True

        if not isinstance(config.get("badwords"), list):
            config["badwords"] = copy.deepcopy(
                DEFAULT_GROUP_SETTINGS["badwords"]
            )
            changed = True

        if not isinstance(config.get("user_warnings"), dict):
            config["user_warnings"] = {}
            changed = True

        if not isinstance(config.get("reports"), dict):
            config["reports"] = {}
            changed = True

        if changed:
            DB_CONFIGS[chat_id] = config
            _write_db_to_disk()

        return copy.deepcopy(config)


async def update_group_config(
    chat_id: int,
    key: str,
    value: Any,
) -> None:
    async with DB_LOCK:
        if chat_id not in DB_CONFIGS:
            DB_CONFIGS[chat_id] = copy.deepcopy(
                DEFAULT_GROUP_SETTINGS
            )

        config = DB_CONFIGS[chat_id]
        config[key] = copy.deepcopy(value)

        for k, default_value in DEFAULT_GROUP_SETTINGS.items():
            if k not in config:
                config[k] = copy.deepcopy(default_value)

        DB_CONFIGS[chat_id] = config
        _write_db_to_disk()


# ====================================================
# 3. LOCK HELPERS
# ====================================================
async def get_user_lock(
    lock_store: Dict[Tuple[int, int], asyncio.Lock],
    key: Tuple[int, int],
) -> asyncio.Lock:
    async with LOCKS_GUARD:
        lock = lock_store.get(key)

        if lock is None:
            lock = asyncio.Lock()
            lock_store[key] = lock

        return lock


# ====================================================
# 4. UTILITIES
# ====================================================
def get_name(user) -> str:
    if not user:
        return "User"

    return user.first_name or user.username or "User"


def safe_title(chat) -> str:
    return html.escape(chat.title or "Group")


def render_template(template: str, user=None, chat=None) -> str:
    name = html.escape(get_name(user))
    title = safe_title(chat) if chat else "Group"

    mention = (
        f'<a href="tg://user?id={user.id}">{name}</a>'
        if user
        else "User"
    )

    return (
        template
        .replace("{mention}", mention)
        .replace("{name}", name)
        .replace("{chat_title}", title)
    )


async def is_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> bool:
    chat = update.effective_chat

    if not chat:
        return False

    key = (chat.id, user_id)
    now = time.monotonic()

    async with ADMIN_CACHE_LOCK:
        cached = ADMIN_CACHE.get(key)

        if cached and now - cached[1] < ADMIN_CACHE_TTL:
            return cached[0]

    try:
        member = await context.bot.get_chat_member(
            chat.id,
            user_id,
        )

        result = member.status in (
            "administrator",
            "creator",
        )
    except TelegramError:
        result = False

    async with ADMIN_CACHE_LOCK:
        ADMIN_CACHE[key] = (result, now)

        if len(ADMIN_CACHE) > 5000:
            oldest = sorted(
                ADMIN_CACHE.items(),
                key=lambda item: item[1][1],
            )[:1000]

            for old_key, _ in oldest:
                ADMIN_CACHE.pop(old_key, None)

    return result


async def invalidate_admin_cache(
    chat_id: int,
    user_id: int,
) -> None:
    async with ADMIN_CACHE_LOCK:
        ADMIN_CACHE.pop((chat_id, user_id), None)


# ====================================================
# 5. AUTO DELETE
# ====================================================
async def auto_delete_task(
    chat_id: int,
    message_id: int,
    delay: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    try:
        await asyncio.sleep(delay)

        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
    except (TelegramError, asyncio.CancelledError):
        pass


async def reply_with_autodelete(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    is_reply: bool = False,
    is_auto_reply: bool = False,
) -> None:
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    config = await get_group_config(chat_id)

    try:
        if is_reply and update.message.reply_to_message:
            sent = await update.message.reply_to_message.reply_text(
                text,
                parse_mode="HTML",
            )
        else:
            sent = await update.message.reply_text(
                text,
                parse_mode="HTML",
            )
    except TelegramError as e:
        logger.error("Failed to send response: %s", e)
        return

    enabled_key = (
        "auto_reply_delete"
        if is_auto_reply
        else "auto_delete"
    )

    delay_key = (
        "reply_delete_time"
        if is_auto_reply
        else "delete_time"
    )

    if config.get(enabled_key, True):
        try:
            delay = int(config.get(delay_key, 120))
        except (TypeError, ValueError):
            delay = 120

        delay = max(
            MIN_DELETE_TIME,
            min(delay, MAX_DELETE_TIME),
        )

        asyncio.create_task(
            auto_delete_task(
                chat_id,
                sent.message_id,
                delay,
                context,
            )
        )


async def send_standalone_autodelete(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    try:
        config = await get_group_config(chat_id)

        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
        )

        if config.get("auto_delete", True):
            try:
                delay = int(
                    config.get("delete_time", 300)
                )
            except (TypeError, ValueError):
                delay = 300

            delay = max(
                MIN_DELETE_TIME,
                min(delay, MAX_DELETE_TIME),
            )

            asyncio.create_task(
                auto_delete_task(
                    chat_id,
                    sent.message_id,
                    delay,
                    context,
                )
            )

    except TelegramError as e:
        logger.error(
            "Failed sending standalone message: %s",
            e,
        )


# ====================================================
# 6. MESSAGE TRACKER
# ====================================================
async def track_user_message(
    chat_id: int,
    user_id: int,
    message_id: int,
) -> None:
    key = (chat_id, user_id)

    async with USER_MESSAGE_TRACKER_LOCK:
        messages = USER_MESSAGE_TRACKER.get(key, [])

        messages.append(message_id)

        if len(messages) > MAX_TRACKED_MESSAGES_PER_USER:
            messages = messages[
                -MAX_TRACKED_MESSAGES_PER_USER:
            ]

        USER_MESSAGE_TRACKER[key] = messages

        if len(USER_MESSAGE_TRACKER) > 1000:
            old_keys = list(
                USER_MESSAGE_TRACKER.keys()
            )[:-500]

            for old_key in old_keys:
                USER_MESSAGE_TRACKER.pop(
                    old_key,
                    None,
                )


async def delete_tracked_user_messages(
    chat_id: int,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    key = (chat_id, user_id)

    async with USER_MESSAGE_TRACKER_LOCK:
        message_ids = list(
            USER_MESSAGE_TRACKER.get(key, [])
        )

        USER_MESSAGE_TRACKER.pop(key, None)

    deleted = 0

    for message_id in message_ids:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )
            deleted += 1
        except TelegramError:
            pass

    return deleted


# ====================================================
# 7. WELCOME / EXIT / REMOVE
# ====================================================
async def chat_member_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    config = await get_group_config(chat_id)

    if update.message.new_chat_members:
        for member in update.message.new_chat_members:
            if member.is_bot:
                continue

            template = config.get(
                "welcome_text",
                DEFAULT_GROUP_SETTINGS["welcome_text"],
            )

            text = render_template(
                template,
                member,
                update.effective_chat,
            )

            await reply_with_autodelete(
                update,
                context,
                text,
            )

    if update.message.left_chat_member:
        member = update.message.left_chat_member

        if member.is_bot:
            return

        template = config.get(
            "exit_text",
            DEFAULT_GROUP_SETTINGS["exit_text"],
        )

        try:
            member_state = await context.bot.get_chat_member(
                chat_id,
                member.id,
            )

            if member_state.status == "kicked":
                template = config.get(
                    "remove_text",
                    DEFAULT_GROUP_SETTINGS["remove_text"],
                )

        except TelegramError:
            pass

        text = render_template(
            template,
            member,
            update.effective_chat,
        )

        await reply_with_autodelete(
            update,
            context,
            text,
        )


async def setwelcome_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    text = update.message.text.partition(" ")[2].strip()

    if not text:
        await reply_with_autodelete(
            update,
            context,
            "❌ Usage: <code>/setwelcome "
            "Welcome {mention} to {chat_title}!</code>",
        )
        return

    await update_group_config(
        update.effective_chat.id,
        "welcome_text",
        text,
    )

    await reply_with_autodelete(
        update,
        context,
        "✅ Welcome message updated.",
    )


async def setexit_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    text = update.message.text.partition(" ")[2].strip()

    if not text:
        await reply_with_autodelete(
            update,
            context,
            "❌ Usage: <code>/setexit Goodbye {name}!</code>",
        )
        return

    await update_group_config(
        update.effective_chat.id,
        "exit_text",
        text,
    )

    await reply_with_autodelete(
        update,
        context,
        "✅ Exit message updated.",
    )


async def setremove_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    text = update.message.text.partition(" ")[2].strip()

    if not text:
        await reply_with_autodelete(
            update,
            context,
            "❌ Usage: <code>/setremove "
            "{name} was removed from {chat_title}.</code>",
        )
        return

    await update_group_config(
        update.effective_chat.id,
        "remove_text",
        text,
    )

    await reply_with_autodelete(
        update,
        context,
        "✅ Removal message updated.",
    )


# ====================================================
# 8. RULES
# ====================================================
async def rules_command(update, context):
    config = await get_group_config(
        update.effective_chat.id
    )

    rules = config.get(
        "rules",
        "No rules set yet.",
    )

    await reply_with_autodelete(
        update,
        context,
        f"📋 <b>Group Rules</b>\n\n"
        f"{html.escape(rules)}",
    )


async def setrules_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    text = update.message.text.partition(" ")[2].strip()

    if not text:
        await reply_with_autodelete(
            update,
            context,
            "❌ Usage: <code>/setrules "
            "&lt;rules text&gt;</code>",
        )
        return

    await update_group_config(
        update.effective_chat.id,
        "rules",
        text,
    )

    await reply_with_autodelete(
        update,
        context,
        "✅ Group rules updated.",
    )


# ====================================================
# 9. WARNING SYSTEM
# ====================================================
async def warn_user_internal(
    chat_id: int,
    target_user,
    context: ContextTypes.DEFAULT_TYPE,
    reason: str = "",
) -> None:
    lock = await get_user_lock(
        WARN_LOCKS,
        (chat_id, target_user.id),
    )

    should_mute = False
    current = 0
    limit = 3
    key = str(target_user.id)

    async with lock:
        config = await get_group_config(chat_id)
        warnings = config.get(
            "user_warnings",
            {},
        )

        try:
            current = int(
                warnings.get(key, 0)
            ) + 1
        except (TypeError, ValueError):
            current = 1

        warnings[key] = current

        try:
            limit = int(
                config.get("warn_limit", 3)
            )
        except (TypeError, ValueError):
            limit = 3

        limit = max(1, min(limit, 20))

        if current >= limit:
            should_mute = True

        await update_group_config(
            chat_id,
            "user_warnings",
            warnings,
        )

    name = html.escape(
        get_name(target_user)
    )

    # Warning limit reached -> mute, NOT ban.
    if should_mute:
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=target_user.id,
                permissions=ChatPermissions(
                    can_send_messages=False
                ),
            )

            async with lock:
                config = await get_group_config(chat_id)
                warnings = config.get(
                    "user_warnings",
                    {},
                )
                warnings[key] = 0

                await update_group_config(
                    chat_id,
                    "user_warnings",
                    warnings,
                )

            await send_standalone_autodelete(
                chat_id,
                context,
                f"🔇 <b>{name}</b> has been muted!\n"
                f"Warning limit reached "
                f"({limit}/{limit}).",
            )

        except TelegramError as e:
            logger.error(
                "Failed to mute warned user: %s",
                e,
            )

            await send_standalone_autodelete(
                chat_id,
                context,
                f"⚠️ <b>{name}</b> reached warning limit "
                f"({limit}/{limit}), but the bot could not mute "
                f"the user. Check admin permissions.",
            )

        return

    reason_text = (
        f"\nReason: <i>{html.escape(reason)}</i>"
        if reason
        else ""
    )

    await send_standalone_autodelete(
        chat_id,
        context,
        f"⚠️ <b>{name}</b> warned! "
        f"({current}/{limit}).{reason_text}",
    )


async def warn_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    if not update.message.reply_to_message:
        await reply_with_autodelete(
            update,
            context,
            "❌ Reply to a member's message "
            "to warn them.",
        )
        return

    target = (
        update.message.reply_to_message.from_user
    )

    if not target or target.is_bot:
        await reply_with_autodelete(
            update,
            context,
            "❌ Cannot warn bot accounts.",
        )
        return

    if await is_admin(
        update,
        context,
        target.id,
    ):
        await reply_with_autodelete(
            update,
            context,
            "❌ Admins cannot be warned.",
        )
        return

    reason = (
        update.message.text.partition(" ")[2].strip()
        or "Admin Warning"
    )

    await warn_user_internal(
        update.effective_chat.id,
        target,
        context,
        reason,
    )


async def resetwarning_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    if not update.message.reply_to_message:
        await reply_with_autodelete(
            update,
            context,
            "❌ Reply to a user's message.",
        )
        return

    target = (
        update.message.reply_to_message.from_user
    )

    if not target:
        return

    chat_id = update.effective_chat.id
    config = await get_group_config(chat_id)

    warnings = config.get(
        "user_warnings",
        {},
    )

    warnings.pop(str(target.id), None)

    await update_group_config(
        chat_id,
        "user_warnings",
        warnings,
    )

    await reply_with_autodelete(
        update,
        context,
        f"✅ Warnings reset for "
        f"<b>{html.escape(get_name(target))}</b>.",
    )


# ====================================================
# 10. BAN / UNBAN / MUTE / UNMUTE
# ====================================================
async def ban_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    if not update.message.reply_to_message:
        await reply_with_autodelete(
            update,
            context,
            "❌ Reply to a member's message to ban.",
        )
        return

    target = (
        update.message.reply_to_message.from_user
    )

    if not target or target.is_bot:
        await reply_with_autodelete(
            update,
            context,
            "❌ Invalid user target.",
        )
        return

    if await is_admin(
        update,
        context,
        target.id,
    ):
        await reply_with_autodelete(
            update,
            context,
            "❌ Admins cannot be banned.",
        )
        return

    chat_id = update.effective_chat.id

    try:
        await context.bot.ban_chat_member(
            chat_id=chat_id,
            user_id=target.id,
        )

        await invalidate_admin_cache(
            chat_id,
            target.id,
        )

        config = await get_group_config(chat_id)

        template = config.get(
            "remove_text",
            DEFAULT_GROUP_SETTINGS["remove_text"],
        )

        msg = render_template(
            template,
            target,
            update.effective_chat,
        )

        await reply_with_autodelete(
            update,
            context,
            f"🚫 {msg}",
        )

    except TelegramError as e:
        await reply_with_autodelete(
            update,
            context,
            f"❌ Failed to ban user: "
            f"{html.escape(str(e))}",
        )


async def unban_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    if not update.message.reply_to_message:
        await reply_with_autodelete(
            update,
            context,
            "❌ Reply to a banned user's message "
            "to unban.",
        )
        return

    target = (
        update.message.reply_to_message.from_user
    )

    if not target:
        return

    try:
        await context.bot.unban_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target.id,
            only_if_banned=True,
        )

        await invalidate_admin_cache(
            update.effective_chat.id,
            target.id,
        )

        await reply_with_autodelete(
            update,
            context,
            f"✅ Unbanned "
            f"<b>{html.escape(get_name(target))}</b>.",
        )

    except TelegramError as e:
        await reply_with_autodelete(
            update,
            context,
            f"❌ Failed to unban: "
            f"{html.escape(str(e))}",
        )


async def mute_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    if not update.message.reply_to_message:
        await reply_with_autodelete(
            update,
            context,
            "❌ Reply to a member's message to mute.",
        )
        return

    target = (
        update.message.reply_to_message.from_user
    )

    if not target or target.is_bot:
        await reply_with_autodelete(
            update,
            context,
            "❌ Invalid user target.",
        )
        return

    if await is_admin(
        update,
        context,
        target.id,
    ):
        await reply_with_autodelete(
            update,
            context,
            "❌ Admins cannot be muted.",
        )
        return

    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target.id,
            permissions=ChatPermissions(
                can_send_messages=False
            ),
        )

        await reply_with_autodelete(
            update,
            context,
            f"🔇 <b>{html.escape(get_name(target))}</b> "
            f"has been muted.",
        )

    except TelegramError as e:
        await reply_with_autodelete(
            update,
            context,
            f"❌ Mute failed: "
            f"{html.escape(str(e))}",
        )


async def unmute_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    if not update.message.reply_to_message:
        await reply_with_autodelete(
            update,
            context,
            "❌ Reply to a member's message to unmute.",
        )
        return

    target = (
        update.message.reply_to_message.from_user
    )

    if not target:
        return

    chat_id = update.effective_chat.id

    try:
        permissions = ChatPermissions(
            can_send_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        )

        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target.id,
            permissions=permissions,
        )

        await reply_with_autodelete(
            update,
            context,
            f"🔊 <b>{html.escape(get_name(target))}</b> "
            f"has been unmuted.",
        )

    except TelegramError as e:
        await reply_with_autodelete(
            update,
            context,
            f"❌ Unmute failed: "
            f"{html.escape(str(e))}",
        )


# ====================================================
# 11. ANTI-BADWORD
# ====================================================
def build_badword_pattern(badwords: List[str]):
    words = [
        str(word).strip().lower()
        for word in badwords
        if str(word).strip()
    ]

    if not words:
        return None

    return re.compile(
        r"\b(?:"
        + "|".join(re.escape(w) for w in words)
        + r")\b",
        re.IGNORECASE,
    )


async def antibadword_toggle(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    if (
        not context.args
        or context.args[0].lower() not in ("on", "off")
    ):
        await reply_with_autodelete(
            update,
            context,
            "❌ Usage: "
            "<code>/antibadword on|off</code>",
        )
        return

    state = context.args[0].lower() == "on"

    await update_group_config(
        update.effective_chat.id,
        "anti_badword",
        state,
    )

    await reply_with_autodelete(
        update,
        context,
        "✅ Anti-Badword filter is now "
        f"<b>{'ON' if state else 'OFF'}</b>.",
    )


async def addbadword_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    word = (
        update.message.text.partition(" ")[2]
        .strip()
        .lower()
    )

    if not word or len(word) > 100:
        await reply_with_autodelete(
            update,
            context,
            "❌ Usage: "
            "<code>/addbadword &lt;word&gt;</code>",
        )
        return

    chat_id = update.effective_chat.id
    config = await get_group_config(chat_id)
    words = config.get("badwords", [])

    if word not in words:
        words.append(word)

        await update_group_config(
            chat_id,
            "badwords",
            words,
        )

    await reply_with_autodelete(
        update,
        context,
        f"✅ Added "
        f"<b>{html.escape(word)}</b> to badwords.",
    )


async def delbadword_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    word = (
        update.message.text.partition(" ")[2]
        .strip()
        .lower()
    )

    chat_id = update.effective_chat.id
    config = await get_group_config(chat_id)
    words = config.get("badwords", [])

    if word in words:
        words.remove(word)

        await update_group_config(
            chat_id,
            "badwords",
            words,
        )

        await reply_with_autodelete(
            update,
            context,
            f"✅ Removed "
            f"<b>{html.escape(word)}</b> from badwords.",
        )

    else:
        await reply_with_autodelete(
            update,
            context,
            "❌ Word not found in badwords list.",
        )


async def listbadwords_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    config = await get_group_config(
        update.effective_chat.id
    )

    words = config.get("badwords", [])
    words_text = ", ".join(words) if words else "None"

    await reply_with_autodelete(
        update,
        context,
        "🤬 <b>Configured Badwords:</b>\n"
        f"<code>{html.escape(words_text)}</code>",
    )


# ====================================================
# 12. AUTO DELETE / SLOW MODE
# ====================================================
def valid_delay(value: str):
    try:
        sec = int(value)
    except (TypeError, ValueError):
        return None

    if not MIN_DELETE_TIME <= sec <= MAX_DELETE_TIME:
        return None

    return sec


async def autodelete_toggle(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    if (
        not context.args
        or context.args[0].lower() not in ("on", "off")
    ):
        await reply_with_autodelete(
            update,
            context,
            "❌ Usage: <code>/autodelete on|off</code>",
        )
        return

    state = context.args[0].lower() == "on"

    await update_group_config(
        update.effective_chat.id,
        "auto_delete",
        state,
    )

    await reply_with_autodelete(
        update,
        context,
        "✅ Global Auto-Delete is now "
        f"<b>{'ON' if state else 'OFF'}</b>.",
    )


async def setdeletetime_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    if not context.args:
        await reply_with_autodelete(
            update,
            context,
            f"❌ Usage: "
            f"<code>/setdeletetime seconds</code>\n"
            f"Allowed: {MIN_DELETE_TIME}-"
            f"{MAX_DELETE_TIME} seconds.",
        )
        return

    sec = valid_delay(context.args[0])

    if sec is None:
        await reply_with_autodelete(
            update,
            context,
            f"❌ Time must be between "
            f"{MIN_DELETE_TIME} and "
            f"{MAX_DELETE_TIME} seconds.",
        )
        return

    await update_group_config(
        update.effective_chat.id,
        "delete_time",
        sec,
    )

    await reply_with_autodelete(
        update,
        context,
        f"✅ Auto-delete duration set to "
        f"<b>{sec}</b> seconds.",
    )


async def autoreplydelete_toggle(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    if (
        not context.args
        or context.args[0].lower() not in ("on", "off")
    ):
        await reply_with_autodelete(
            update,
            context,
            "❌ Usage: "
            "<code>/autoreplydelete on|off</code>",
        )
        return

    state = context.args[0].lower() == "on"

    await update_group_config(
        update.effective_chat.id,
        "auto_reply_delete",
        state,
    )

    await reply_with_autodelete(
        update,
        context,
        "✅ Auto-Reply Delete is now "
        f"<b>{'ON' if state else 'OFF'}</b>.",
    )


async def setreplydelete_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    if not context.args:
        await reply_with_autodelete(
            update,
            context,
            f"❌ Usage: "
            f"<code>/setreplydelete seconds</code>\n"
            f"Allowed: {MIN_DELETE_TIME}-"
            f"{MAX_DELETE_TIME} seconds.",
        )
        return

    sec = valid_delay(context.args[0])

    if sec is None:
        await reply_with_autodelete(
            update,
            context,
            f"❌ Time must be between "
            f"{MIN_DELETE_TIME} and "
            f"{MAX_DELETE_TIME} seconds.",
        )
        return

    await update_group_config(
        update.effective_chat.id,
        "reply_delete_time",
        sec,
    )

    await reply_with_autodelete(
        update,
        context,
        f"✅ Auto-reply delete duration set to "
        f"<b>{sec}</b> seconds.",
    )


async def slowmode_command(update, context):
    try:
        chat = await context.bot.get_chat(
            update.effective_chat.id
        )

        delay = getattr(
            chat,
            "slow_mode_delay",
            None,
        )

        if delay:
            text = (
                f"⏳ <b>Slow Mode Active</b>\n"
                f"Members must wait <b>{delay}</b> "
                f"seconds between messages."
            )
        else:
            text = (
                "ℹ️ Slow Mode is currently disabled."
            )

        await reply_with_autodelete(
            update,
            context,
            text,
        )

    except TelegramError:
        await reply_with_autodelete(
            update,
            context,
            "❌ Unable to fetch slow mode configuration.",
        )


# ====================================================
# 13. REPORT SYSTEM
# ====================================================
async def report_command(update, context):
    if (
        not update.effective_user
        or not update.effective_chat
    ):
        return

    if (
        not update.message
        or not update.message.reply_to_message
    ):
        await reply_with_autodelete(
            update,
            context,
            "❌ Reply to a member's message "
            "with /report.",
        )
        return

    reporter = update.effective_user
    target = (
        update.message.reply_to_message.from_user
    )
    chat_id = update.effective_chat.id

    if not target or target.is_bot:
        await reply_with_autodelete(
            update,
            context,
            "❌ Cannot report bot accounts.",
        )
        return

    if reporter.id == target.id:
        await reply_with_autodelete(
            update,
            context,
            "❌ You cannot report yourself.",
        )
        return

    if await is_admin(
        update,
        context,
        target.id,
    ):
        await reply_with_autodelete(
            update,
            context,
            "❌ Admins cannot be reported.",
        )
        return

    lock = await get_user_lock(
        REPORT_LOCKS,
        (chat_id, target.id),
    )

    async with lock:
        config = await get_group_config(chat_id)

        if not config.get(
            "report_system",
            True,
        ):
            await reply_with_autodelete(
                update,
                context,
                "ℹ️ Report system is currently disabled.",
            )
            return

        reports = config.get("reports", {})
        target_key = str(target.id)

        reporter_ids = reports.get(
            target_key,
            [],
        )

        if reporter.id in reporter_ids:
            await reply_with_autodelete(
                update,
                context,
                "⚠️ You have already reported "
                "this member.",
            )
            return

        reporter_ids.append(reporter.id)
        reports[target_key] = reporter_ids

        try:
            report_limit = int(
                config.get("report_limit", 5)
            )
        except (TypeError, ValueError):
            report_limit = 5

        report_limit = max(
            1,
            min(report_limit, 50),
        )

        report_count = len(reporter_ids)

        await update_group_config(
            chat_id,
            "reports",
            reports,
        )

        if report_count < report_limit:
            progress_text = (
                f"🚨 <b>Report Received!</b>\n"
                f"👤 Target: "
                f"<b>{html.escape(get_name(target))}</b>\n"
                f"📊 Progress: "
                f"<b>{report_count}/{report_limit}</b>"
            )
        else:
            progress_text = None

    if progress_text:
        await reply_with_autodelete(
            update,
            context,
            progress_text,
            is_reply=True,
        )
        return

    try:
        await update.message.delete()
    except TelegramError:
        pass

    deleted_count = await delete_tracked_user_messages(
        chat_id,
        target.id,
        context,
    )

    restricted = False

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target.id,
            permissions=ChatPermissions(
                can_send_messages=False
            ),
        )

        restricted = True

    except TelegramError as e:
        logger.error(
            "Report restriction failed: %s",
            e,
        )

    lock = await get_user_lock(
        REPORT_LOCKS,
        (chat_id, target.id),
    )

    async with lock:
        config = await get_group_config(chat_id)
        reports = config.get("reports", {})

        reports.pop(
            str(target.id),
            None,
        )

        await update_group_config(
            chat_id,
            "reports",
            reports,
        )

    safe_name = html.escape(
        get_name(target)
    )

    if restricted:
        message = (
            f"🚨 <b>REPORT LIMIT REACHED</b> 🚨\n\n"
            f"👤 Target: <b>{safe_name}</b>\n"
            f"📊 Unique Reports: "
            f"<b>{report_count}/{report_limit}</b>\n"
            f"🗑️ Recent tracked messages deleted: "
            f"<b>{deleted_count}</b>\n\n"
            f"🔇 Target has been restricted/muted.\n"
            f"⚠️ <b>NO AUTOMATIC BAN</b> was executed. "
            f"Admins should review manually."
        )
    else:
        message = (
            f"⚠️ <b>REPORT LIMIT REACHED</b>\n\n"
            f"👤 Target: <b>{safe_name}</b>\n"
            f"📊 Reports: "
            f"<b>{report_count}/{report_limit}</b>\n"
            f"🗑️ Recent tracked messages deleted: "
            f"<b>{deleted_count}</b>\n\n"
            f"❌ Bot could not restrict the user. "
            f"Please check admin permissions."
        )

    await send_standalone_autodelete(
        chat_id,
        context,
        message,
    )


async def resetreports_command(update, context):
    if not await is_admin(
        update,
        context,
        update.effective_user.id,
    ):
        return

    if not update.message.reply_to_message:
        await reply_with_autodelete(
            update,
            context,
            "❌ Reply to a member's message "
            "to reset reports.",
        )
        return

    target = (
        update.message.reply_to_message.from_user
    )

    if not target:
        return

    chat_id = update.effective_chat.id

    lock = await get_user_lock(
        REPORT_LOCKS,
        (chat_id, target.id),
    )

    async with lock:
        config = await get_group_config(chat_id)
        reports = config.get("reports", {})

        reports.pop(
            str(target.id),
            None,
        )

        await update_group_config(
            chat_id,
            "reports",
            reports,
        )

    await reply_with_autodelete(
        update,
        context,
        f"✅ Reports reset for "
        f"<b>{html.escape(get_name(target))}</b>.",
    )


# ====================================================
# 14. MODERATION PIPELINE
# ====================================================
async def process_messages(update, context):
    if (
        not update.effective_chat
        or not update.message
        or not update.effective_user
    ):
        return

    user = update.effective_user

    if user.is_bot:
        return

    chat_id = update.effective_chat.id
    message_id = update.message.message_id

    text = (
        update.message.text
        or update.message.caption
        or ""
    )

    admin = await is_admin(
        update,
        context,
        user.id,
    )

    if not admin:
        await track_user_message(
            chat_id,
            user.id,
            message_id,
        )

    config = await get_group_config(chat_id)

    # ------------------------------------------------
    # 14.1 BADWORD
    # Gali -> DELETE + DIRECT BAN
    # ------------------------------------------------
    if (
        not admin
        and config.get("anti_badword", True)
    ):
        pattern = build_badword_pattern(
            config.get("badwords", [])
        )

        if pattern and pattern.search(text):
            try:
                await update.message.delete()
            except TelegramError:
                pass

            try:
                await context.bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=user.id,
                )

                await invalidate_admin_cache(
                    chat_id,
                    user.id,
                )

                template = config.get(
                    "remove_text",
                    DEFAULT_GROUP_SETTINGS["remove_text"],
                )

                ban_message = render_template(
                    template,
                    user,
                    update.effective_chat,
                )

                await send_standalone_autodelete(
                    chat_id,
                    context,
                    "🚫 <b>Badword Violation — "
                    "User Banned</b>\n"
                    f"{ban_message}",
                )

            except TelegramError as e:
                logger.error(
                    "Failed direct badword ban: %s",
                    e,
                )

                await send_standalone_autodelete(
                    chat_id,
                    context,
                    f"⚠️ Badword detected from "
                    f"<b>{html.escape(get_name(user))}</b>, "
                    f"but the bot could not ban the user. "
                    f"Check admin permissions.",
                )

            return

    # ------------------------------------------------
    # 14.2 FLOOD / SPAM
    # Spam -> DELETE + WARNING, NO BAN
    # ------------------------------------------------
    if not admin:
        now = time.monotonic()
        key = (chat_id, user.id)

        trigger_warning = False
        flood_triggered = False

        async with FLOOD_LOCK:
            timestamps = FLOOD_TRACKER.get(key, [])

            timestamps = [
                timestamp
                for timestamp in timestamps
                if now - timestamp < FLOOD_TIMEFRAME
            ]

            timestamps.append(now)

            if len(timestamps) >= FLOOD_MSG_LIMIT:
                flood_triggered = True

                last_warning = (
                    FLOOD_COOLDOWN_TRACKER.get(
                        key,
                        0.0,
                    )
                )

                if (
                    now - last_warning
                    >= FLOOD_COOLDOWN_SEC
                ):
                    FLOOD_COOLDOWN_TRACKER[key] = now
                    trigger_warning = True

                # Reset after detecting one spam burst.
                FLOOD_TRACKER[key] = []
            else:
                FLOOD_TRACKER[key] = timestamps

        if flood_triggered:
            try:
                await update.message.delete()
            except TelegramError:
                pass

            if trigger_warning:
                await warn_user_internal(
                    chat_id,
                    user,
                    context,
                    "Flood / Spam",
                )

            return

    # ------------------------------------------------
    # 14.3 RESTRICTED LINKS
    # Link -> DELETE + WARNING, NO BAN
    # ------------------------------------------------
    if (
        not admin
        and RESTRICTED_LINKS_REGEX.search(text)
    ):
        try:
            await update.message.delete()
        except TelegramError:
            pass

        await warn_user_internal(
            chat_id,
            user,
            context,
            "Posting Restricted Links",
        )

        return

    # ------------------------------------------------
    # 14.4 AUTO REPLIES
    # ------------------------------------------------
    lowered = text.lower().strip()

    if any(
        greeting in lowered
        for greeting in (
            "radhe radhe",
            "radhey radhey",
            "jai shree radhe",
        )
    ):
        await reply_with_autodelete(
            update,
            context,
            "🙏 <b>Radhe Radhe!</b> "
            "May your day be productive and blessed.",
            is_auto_reply=True,
        )
        return

    is_photo = bool(update.message.photo)

    is_question = (
        "?" in text
        or any(
            word in lowered
            for word in (
                "doubt",
                "help",
                "kaise",
                "solution",
                "kya",
            )
        )
    )

    if (
        (is_photo and is_question)
        or (is_question and len(text) > 10)
    ):
        await reply_with_autodelete(
            update,
            context,
            "❓ <b>Question Received!</b>\n"
            "An Admin or Mentor will respond shortly. "
            "Please wait patiently.",
            is_reply=True,
            is_auto_reply=True,
        )


# ====================================================
# 15. ERROR HANDLER
# ====================================================
async def error_handler(update, context):
    logger.error(
        "Unhandled exception while processing update: %s",
        context.error,
    )


# ====================================================
# 16. MAIN
# ====================================================
def main() -> None:
    bot_token = os.getenv(
        "BOT_TOKEN",
        "",
    ).strip()

    if not bot_token:
        logger.error(
            "BOT_TOKEN environment variable is missing."
        )
        return

    application = (
        Application.builder()
        .token(bot_token)
        .build()
    )

    # Welcome / exit / removal
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS
            | filters.StatusUpdate.LEFT_CHAT_MEMBER,
            chat_member_handler,
        )
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "setwelcome",
            setwelcome_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "setexit",
            setexit_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "setremove",
            setremove_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "rules",
            rules_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "setrules",
            setrules_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "warn",
            warn_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "resetwarning",
            resetwarning_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "ban",
            ban_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "unban",
            unban_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "mute",
            mute_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "unmute",
            unmute_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "antibadword",
            antibadword_toggle,
        )
    )
    application.add_handler(
        CommandHandler(
            "addbadword",
            addbadword_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "delbadword",
            delbadword_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "listbadwords",
            listbadwords_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "autodelete",
            autodelete_toggle,
        )
    )
    application.add_handler(
        CommandHandler(
            "setdeletetime",
            setdeletetime_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "autoreplydelete",
            autoreplydelete_toggle,
        )
    )
    application.add_handler(
        CommandHandler(
            "setreplydelete",
            setreplydelete_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "slowmode",
            slowmode_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "report",
            report_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "resetreports",
            resetreports_command,
        )
    )

    # Normal messages (Text & Media)
    application.add_handler(
        MessageHandler(
            (
                filters.TEXT
                | filters.PHOTO
                | filters.VIDEO
                | filters.Document.ALL                   | filters.AUDIO
                | filters.VOICE
            )
            & ~filters.COMMAND,
            process_messages,
        ),
        group=1,
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Bot started successfully."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
