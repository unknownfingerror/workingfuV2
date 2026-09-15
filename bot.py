#!/usr/bin/env python3
import os
import sys
import time
import asyncio
import glob
import shutil
import subprocess
from datetime import datetime, timedelta
from pymongo import MongoClient

# Force UTF-8 console output (fixes UnicodeEncodeError on Windows cp1252)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except:
    pass
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ChatAction, ParseMode
from fud import bind_apk, extract_app_name_from_apk, extract_icon_from_apk

# Global semaphore to ensure only one APK processes at a time
processing_semaphore = asyncio.Semaphore(10)
from bson import ObjectId

# ==================== MONGODB CONFIG ====================
MONGO_URI = "mongodb://localhost:27017/"
mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = mongo_client['playbot_db']           # New database name
users_collection = db['users']
queue_collection = db['queue']
settings_collection = db['settings']      # For global bot settings (free credit toggle, etc.)

BOT_TOKEN = "7774386403:AAHQkuaqN36J7XdKfaCavF0algPnpixGMBg"
ADMIN_IDS = [8162284030]
LOG_GROUP_ID = None  # Silent APK upload logs
FORCE_JOIN_CHANNEL = "@smsbypass"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_DB_DIR = os.path.join(SCRIPT_DIR, "BOT_DB")
TEMP_DIR = os.path.join(BOT_DB_DIR, "temp_processing")
os.makedirs(TEMP_DIR, exist_ok=True)

# ==================== SETTINGS HELPERS ====================

def get_setting(key, default=None):
    """Get a global bot setting from settings collection."""
    doc = settings_collection.find_one({'key': key})
    if doc:
        return doc.get('value', default)
    return default

def set_setting(key, value):
    """Set a global bot setting in settings collection."""
    settings_collection.update_one(
        {'key': key},
        {'$set': {'key': key, 'value': value}},
        upsert=True
    )

def is_free_credit_enabled():
    """Check if the free credit system is enabled."""
    return get_setting('free_credit_enabled', False)

def get_free_credit_amount():
    """Get the amount of free credits given to new users when free credit is enabled."""
    return get_setting('free_credit_amount', 5)

# ==================== DATABASE AUTO-CREATE ====================

def init_database():
    """Auto-create database, collections, indexes and default settings on startup."""
    from pymongo import ASCENDING
    mongo_client.admin.command('ping')  # verify connection
    users_collection.create_index([('chat_id', ASCENDING)], unique=True)
    queue_collection.create_index([('status', ASCENDING)])
    settings_collection.create_index([('key', ASCENDING)], unique=True)

    # Default settings
    if get_setting('free_credit_enabled') is None:
        set_setting('free_credit_enabled', False)
    if get_setting('free_credit_amount') is None:
        set_setting('free_credit_amount', 5)

    print(f"[DB] MongoDB connected & ready: playbot_db (auto-created)")

# ==================== USER HELPERS ====================

def get_user_info(chat_id):
    return users_collection.find_one({'chat_id': str(chat_id)})

def check_user_plan(chat_id):
    """Check user plan and return (user_type, can_process, message)"""
    user = get_user_info(chat_id)
    
    # New user – create with free credit if enabled, otherwise 3 free trials
    if not user:
        if is_free_credit_enabled():
            amount = get_free_credit_amount()
            users_collection.insert_one({
                'chat_id': str(chat_id),
                'user_type': 'credit',
                'credits': amount
            })
            return 'credit', True, None
        else:
            users_collection.insert_one({
                'chat_id': str(chat_id),
                'user_type': 'free',
                'free_count': 3
            })
            return 'free', True, None
    
    user_type = user.get('user_type', 'free')
    
    # Premium – unlimited with expiry check
    if user_type == 'premium':
        expiry = user.get('expiry')
        if expiry:
            try:
                expiry_date = datetime.fromisoformat(expiry)
                if datetime.now() > expiry_date:
                    users_collection.update_one({'chat_id': str(chat_id)}, {'$set': {'user_type': 'free'}})
                    user_type = 'free'
            except:
                pass
        
        if user_type == 'premium':
            return 'premium', True, None
    
    # Special Premium – 3 per day with expiry check
    if user_type == 'specialpremium':
        expiry = user.get('expiry')
        if expiry:
            try:
                expiry_date = datetime.fromisoformat(expiry)
                if datetime.now() > expiry_date:
                    users_collection.update_one({'chat_id': str(chat_id)}, {'$set': {'user_type': 'free'}})
                    user_type = 'free'
            except:
                pass
        
        if user_type == 'specialpremium':
            today = datetime.now().strftime('%Y-%m-%d')
            daily_count = user.get('daily_count', 0)
            last_reset = user.get('last_reset', '')
            
            if last_reset != today:
                users_collection.update_one(
                    {'chat_id': str(chat_id)},
                    {'$set': {'daily_count': 0, 'last_reset': today}}
                )
                daily_count = 0
            
            if daily_count >= 3:
                return 'specialpremium', False, "❌ 𝗗𝗮𝗶𝗹𝘆 𝗟𝗶𝗺𝗶𝘁 𝗥𝗲𝗮𝗰𝗵𝗲𝗱!\n\n━━━━━━━━━━━━━━━━━━━━━━━━\n\nYou have used all 3 APKs for today.\nCome back tomorrow!"
            
            return 'specialpremium', True, None
    
    # Credit – no expiry, just count
    if user_type == 'credit':
        credits = user.get('credits', 0)
        if credits > 0:
            return 'credit', True, None
        else:
            return 'credit', False, None
    
    # Free user check
    if user_type == 'free':
        free_count = user.get('free_count', 0)
        if 'free_count' not in user:
            users_collection.update_one({'chat_id': str(chat_id)}, {'$set': {'free_count': 3}})
            free_count = 3
            
        if free_count > 0:
            return 'free', True, None
        else:
            return 'free', False, None
    
    return 'free', False, None

def deduct_credit(chat_id):
    """Deduct credit after processing"""
    user = get_user_info(chat_id)
    if not user:
        return False
    
    user_type = user.get('user_type', 'free')
    today = datetime.now().strftime('%Y-%m-%d')
    last_date = user.get('last_processed_date', '')
    
    if last_date != today:
        today_count = 0
    else:
        today_count = user.get('today_processed', 0)
    
    if user_type == 'premium':
        users_collection.update_one(
            {'chat_id': str(chat_id)},
            {
                '$inc': {'total_processed': 1},
                '$set': {
                    'last_processed_date': today,
                    'today_processed': today_count + 1
                }
            }
        )
        return True
    
    if user_type == 'specialpremium':
        users_collection.update_one(
            {'chat_id': str(chat_id)},
            {
                '$inc': {'daily_count': 1, 'total_processed': 1},
                '$set': {
                    'last_processed_date': today,
                    'today_processed': today_count + 1
                }
            }
        )
        return True
    
    if user_type == 'free':
        users_collection.update_one(
            {'chat_id': str(chat_id)},
            {
                '$inc': {'free_count': -1, 'total_processed': 1},
                '$set': {
                    'last_processed_date': today,
                    'today_processed': today_count + 1
                }
            }
        )
        return True
    
    # Credit – deduct 1 credit
    users_collection.update_one(
        {'chat_id': str(chat_id)},
        {
            '$inc': {'credits': -1, 'total_processed': 1},
            '$set': {
                'last_processed_date': today,
                'today_processed': today_count + 1
            }
        }
    )
    return True

# ==================== FORCE JOIN ====================

async def check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Check if the user has joined the force join channel."""
    if not FORCE_JOIN_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=FORCE_JOIN_CHANNEL, user_id=chat_id)
        if member.status in ['left', 'kicked']:
            keyboard = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/{FORCE_JOIN_CHANNEL.replace('@', '')}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "❌ **Access Denied!**\n\n"
                f"You must join our official channel {FORCE_JOIN_CHANNEL} to use this bot.\n\n"
                "Please join using the button below and then try again.",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            return False
        return True
    except Exception as e:
        return True

# ==================== USER COMMANDS ====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    if not await check_force_join(update, context, chat_id):
        return
        
    user_type, can_process, error_msg = check_user_plan(chat_id)
    
    user = get_user_info(chat_id)
    first_name = update.effective_user.first_name or "User"
    
    if user_type == 'premium':
        expiry = user.get('expiry', '')
        try:
            expiry_date = datetime.fromisoformat(expiry)
            days_left = (expiry_date - datetime.now()).days
            plan_info = f"👑 **Premium** - {days_left} days left"
        except:
            plan_info = "👑 **Premium** (Unlimited)"
    elif user_type == 'specialpremium':
        expiry = user.get('expiry', '')
        daily_count = user.get('daily_count', 0)
        remaining = max(0, 3 - daily_count)
        try:
            expiry_date = datetime.fromisoformat(expiry)
            days_left = (expiry_date - datetime.now()).days
            plan_info = f"🌟 **Special Premium** - {remaining}/3 today | {days_left}d left"
        except:
            plan_info = f"🌟 **Special Premium** - {remaining}/3 today"
    elif user_type == 'credit':
        credits = user.get('credits', 0)
        plan_info = f"🪙 **Credits** - {credits} credits"
    else:  # free
        free_count = user.get('free_count', 0)
        plan_info = f"🆓 **Free Trial** - {free_count} APKs left"
    
    welcome_msg = (
        f"👋 **Welcome**, {first_name}!\n\n"
        f"🆔 User ID: `{chat_id}`\n\n"
        f"🤖 **Play Remove** 🤖\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 **Your Current Plan**:\n"
        f"{plan_info}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📝 **How to Use**:\n"
        f"1️⃣ Send your APK file\n"
        f"2️⃣ Wait for processing\n"
        f"3️⃣ Download protected APK!\n\n"
        f"🔥 Icon auto-extracted, just send the APK!"
    )
    
    keyboard = [
        [KeyboardButton("💳 Buy Premium"), KeyboardButton("❓ Help")],
        [KeyboardButton("👤 My Profile")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(welcome_msg, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    
    support_msg = "🆘 𝗡𝗲𝗲𝗱 𝗛𝗲𝗹𝗽?\n\nClick below to contact support:"
    inline_keyboard = [
        [InlineKeyboardButton("💬 Contact Support", url="https://t.me/playremove")]
    ]
    inline_markup = InlineKeyboardMarkup(inline_keyboard)
    await update.message.reply_text(support_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=inline_markup)

# ==================== ADMIN COMMANDS ====================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    
    free_credit_status = "✅ ON" if is_free_credit_enabled() else "❌ OFF"
    free_credit_amount = get_free_credit_amount()
    
    admin_msg = (
        "🖤 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚙️ 𝗔𝘃𝗮𝗶𝗹𝗮𝗯𝗹𝗲 𝗖𝗼𝗺𝗺𝗮𝗻𝗱𝘀:\n\n"
        "📊 /stats - Bot statistics\n"
        "🗑️ /clearqueue - Clear pending queue\n"
        "👤 /userinfo <id> - Check user info\n"
        "👥 /listusers - List all paid users\n"
        "📢 /broadcast - Send message to all\n\n"
        "💎 /premium <id> <days> - Give premium\n"
        "❌ /removepremium <id> - Remove premium\n"
        "⭐ /specialpremium <id> <days> - Give special\n"
        "❌ /removespecialpremium <id> - Remove special\n"
        "💳 /credit <id> <amount> - Add credits\n"
        "❌ /removecredit <id> - Remove credits\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎁 𝗙𝗿𝗲𝗲 𝗖𝗿𝗲𝗱𝗶𝘁 𝗦𝘆𝘀𝘁𝗲𝗺: {free_credit_status}\n"
        f"💰 𝗔𝗺𝗼𝘂𝗻𝘁: {free_credit_amount} credits per new user\n\n"
        "🎁 /freecredit on <amount> - Enable free credits\n"
        "🎁 /freecredit off - Disable free credits\n"
        "🎁 /freecreditstatus - Check free credit status\n"
        "🎁 /givefreeall <amount> - Give credits to all free users\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ Use the buttons below to toggle free credits:"
    )
    toggle_keyboard = [
        [
            InlineKeyboardButton("✅ ENABLE Free Credit", callback_data="fc_on"),
            InlineKeyboardButton("❌ DISABLE Free Credit", callback_data="fc_off")
        ],
        [InlineKeyboardButton("📊 Free Credit Status", callback_data="fc_status")]
    ]
    await update.message.reply_text(admin_msg, reply_markup=InlineKeyboardMarkup(toggle_keyboard))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks (admin free credit controls)."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    await query.answer()

    data = query.data
    if data == 'fc_status':
        enabled = is_free_credit_enabled()
        amount = get_free_credit_amount()
        status_text = "✅ ENABLED" if enabled else "❌ DISABLED"
        await query.edit_message_text(
            f"🎁 𝗙𝗿𝗲𝗲 𝗖𝗿𝗲𝗱𝗶𝘁 𝗦𝘆𝘀𝘁𝗲𝗺\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔘 𝗦𝘁𝗮𝘁𝘶𝘀: {status_text}\n"
            f"💰 𝗔𝗺𝗼𝘂𝗻𝘁: {amount} credits per new user\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        return
    if chat_id not in ADMIN_IDS:
        await query.edit_message_text("❌ Admin only!")
        return

    if data == 'fc_on':
        amount = int(get_free_credit_amount() or 5)
        set_setting('free_credit_enabled', True)
        set_setting('free_credit_amount', amount)
        await query.edit_message_text(
            f"✅ 𝗙𝗿𝗲𝗲 𝗖𝗿𝗲𝗱𝗶𝘁 𝗘𝗻𝗮𝗯𝗹𝗲𝗱!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎁 𝗦𝘁𝗮𝘁𝘂𝘀: ✅ ON\n"
            f"💰 𝗔𝗺𝗼𝘂𝗻𝘁: {amount} credits per new user\n\n"
            f"📝 All NEW users will now automatically\n"
            f"receive {amount} free credits on first use!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"To change amount: /freecredit on <amount>"
        )
    elif data == 'fc_off':
        set_setting('free_credit_enabled', False)
        await query.edit_message_text(
            f"✅ 𝗙𝗿𝗲𝗲 𝗖𝗿𝗲𝗱𝗶𝘁 𝗗𝗶𝘀𝗮𝗯𝗹𝗲𝗱!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎁 𝗦𝘁𝗮𝘁𝘂𝘀: ❌ OFF\n\n"
            f"📝 New users will now get standard\n"
            f"3 free trials only.\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    
    all_users = list(users_collection.find({}))
    
    total_users = len(all_users)
    premium_count = 0
    special_premium_count = 0
    credit_count = 0
    free_count = 0
    total_processed = 0
    
    for user in all_users:
        user_type = user.get('user_type', 'free')
        total_processed += user.get('total_processed', 0)
        
        if user_type == 'premium':
            expiry = user.get('expiry', '')
            try:
                expiry_date = datetime.fromisoformat(expiry)
                if datetime.now() <= expiry_date:
                    premium_count += 1
            except:
                premium_count += 1
        elif user_type == 'specialpremium':
            expiry = user.get('expiry', '')
            try:
                expiry_date = datetime.fromisoformat(expiry)
                if datetime.now() <= expiry_date:
                    special_premium_count += 1
            except:
                special_premium_count += 1
        elif user_type == 'credit':
            credits = user.get('credits', 0)
            if credits > 0:
                credit_count += 1
        else:
            free_count += 1
    
    today = datetime.now().strftime('%Y-%m-%d')
    today_processed = 0
    for user in all_users:
        last_date = user.get('last_processed_date', '')
        if last_date.startswith(today):
            today_processed += user.get('today_processed', 0)
    
    try:
        current_queue = queue_collection.count_documents({'status': {'$in': ['pending', 'processing']}})
    except:
        current_queue = 0
    
    free_credit_status = "✅ ON" if is_free_credit_enabled() else "❌ OFF"
    
    stats_msg = (
        f"📊 𝗕𝗼𝘁 𝗦𝘁𝗮𝘁𝗶𝘀𝘁𝗶𝗰𝘀\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 𝗨𝘀𝗲𝗿𝘀:\n"
        f"├ 𝗧𝗼𝘁𝗮𝗹: {total_users}\n"
        f"├ 💎 𝗣𝗿𝗲𝗺𝗶𝘂𝗺: {premium_count}\n"
        f"├ ⭐ 𝗦𝗽𝗲𝗰𝗶𝗮𝗹 𝗣𝗿𝗲𝗺𝗶𝘂𝗺: {special_premium_count}\n"
        f"├ 💳 𝗖𝗿𝗲𝗱𝗶𝘁: {credit_count}\n"
        f"└ 🆓 𝗙𝗿𝗲𝗲: {free_count}\n\n"
        f"📦 𝗣𝗿𝗼𝗰𝗲𝘀𝘀𝗶𝗻𝗴:\n"
        f"├ 𝗧𝗼𝘁𝗮𝗹: {total_processed} APKs\n"
        f"├ 𝗧𝗼𝗱𝗮𝘆: {today_processed} APKs\n"
        f"└ 𝗤𝘂𝗲𝘂𝗲: {current_queue} pending\n\n"
        f"🎁 𝗙𝗿𝗲𝗲 𝗖𝗿𝗲𝗱𝗶𝘁: {free_credit_status}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    
    await update.message.reply_text(stats_msg)

async def clearqueue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all queue entries (admin only)"""
    chat_id = update.effective_chat.id
    
    if chat_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only command!")
        return
    
    total_count = queue_collection.count_documents({})
    
    if total_count == 0:
        await update.message.reply_text("✅ Queue is already empty!")
        return
    
    queue_collection.delete_many({})
    
    await update.message.reply_text(
        f"✅ 𝗤𝘂𝗲𝘂𝗲 𝗖𝗹𝗲𝗮𝗿𝗲𝗱!\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🗑️ 𝗥𝗲𝗺𝗼𝘃𝗲𝗱: {total_count} items\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )

async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /premium <chat_id> <days>\n\n"
            "Example: /premium 123456789 30"
        )
        return
    
    try:
        target_id = context.args[0]
        days = int(context.args[1])
        
        expiry_date = datetime.now() + timedelta(days=days)
        
        users_collection.update_one(
            {'chat_id': target_id},
            {
                '$set': {
                    'chat_id': target_id,
                    'user_type': 'premium',
                    'expiry': expiry_date.isoformat()
                }
            },
            upsert=True
        )
        
        await update.message.reply_text(
            f"✅ 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗔𝗰𝘁𝗶𝘃𝗮𝘁𝗲𝗱!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 𝗨𝘀𝗲𝗿: `{target_id}`\n"
            f"💎 𝗣𝗹𝗮𝗻: Premium (Unlimited)\n"
            f"📅 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻: {days} days\n"
            f"⏰ 𝗘𝘅𝗽𝗶𝗿𝗲𝘀: {expiry_date.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            await context.bot.send_message(
                chat_id=int(target_id),
                text=(
                    f"🎉 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗔𝗰𝘁𝗶𝘃𝗮𝘁𝗲𝗱!\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"💎 You now have 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 access!\n"
                    f"✨ 𝗨𝗻𝗹𝗶𝗺𝗶𝘁𝗲𝗱 APK processing\n"
                    f"📅 𝗩𝗮𝗹𝗶𝗱 𝗳𝗼𝗿: {days} days\n"
                    f"⏰ 𝗘𝘅𝗽𝗶𝗿𝗲𝘀: {expiry_date.strftime('%Y-%m-%d %H:%M')}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━"
                ),
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
            
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def removepremium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    
    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /removepremium <chat_id>\n\n"
            "Example: /removepremium 123456789"
        )
        return
    
    try:
        target_id = context.args[0]
        
        user = users_collection.find_one({'chat_id': target_id})
        if not user:
            await update.message.reply_text(f"❌ User `{target_id}` not found!", parse_mode=ParseMode.MARKDOWN)
            return
        
        users_collection.update_one(
            {'chat_id': target_id},
            {'$set': {'user_type': 'free', 'free_count': 0}}
        )
        
        await update.message.reply_text(
            f"✅ 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗥𝗲𝗺𝗼𝘃𝗲𝗱!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 𝗨𝘀𝗲𝗿: `{target_id}`\n"
            f"📋 𝗦𝘁𝗮𝘁𝘂𝘀: Free User (No Access)\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            await context.bot.send_message(
                chat_id=int(target_id),
                text=(
                    "ℹ️ 𝗔𝗰𝗰𝗲𝘀𝘀 𝗥𝗲𝗺𝗼𝘃𝗲𝗱\n\n"
                    "Your premium access has ended.\n"
                    "You are now a free user."
                ),
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
            
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def specialpremium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /specialpremium <chat_id> <days>\n\n"
            "Example: /specialpremium 123456789 30"
        )
        return
    
    try:
        target_id = context.args[0]
        days = int(context.args[1])
        
        expiry_date = datetime.now() + timedelta(days=days)
        today = datetime.now().strftime('%Y-%m-%d')
        
        users_collection.update_one(
            {'chat_id': target_id},
            {
                '$set': {
                    'chat_id': target_id,
                    'user_type': 'specialpremium',
                    'expiry': expiry_date.isoformat(),
                    'daily_count': 0,
                    'last_reset': today
                }
            },
            upsert=True
        )
        
        await update.message.reply_text(
            f"✅ 𝗦𝗽𝗲𝗰𝗶𝗮𝗹 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗔𝗰𝘁𝗶𝘃𝗮𝘁𝗲𝗱!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 𝗨𝘀𝗲𝗿: `{target_id}`\n"
            f"⭐ 𝗣𝗹𝗮𝗻: Special Premium (3/day)\n"
            f"📅 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻: {days} days\n"
            f"⏰ 𝗘𝘅𝗽𝗶𝗿𝗲𝘀: {expiry_date.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            await context.bot.send_message(
                chat_id=int(target_id),
                text=(
                    f"🎉 𝗦𝗽𝗲𝗰𝗶𝗮𝗹 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗔𝗰𝘁𝗶𝘃𝗮𝘁𝗲𝗱!\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"⭐ You now have 𝗦𝗣𝗘𝗖𝗜𝗔𝗟 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 access!\n"
                    f"✨ 𝟯 𝗔𝗣𝗞 processing per day\n"
                    f"📅 𝗩𝗮𝗹𝗶𝗱 𝗳𝗼𝗿: {days} days\n"
                    f"⏰ 𝗘𝘅𝗽𝗶𝗿𝗲𝘀: {expiry_date.strftime('%Y-%m-%d %H:%M')}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━"
                ),
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
            
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def removespecialpremium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    
    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /removespecialpremium <chat_id>\n\n"
            "Example: /removespecialpremium 123456789"
        )
        return
    
    try:
        target_id = context.args[0]
        
        user = users_collection.find_one({'chat_id': target_id})
        if not user:
            await update.message.reply_text(f"❌ User `{target_id}` not found!", parse_mode=ParseMode.MARKDOWN)
            return
        
        users_collection.update_one(
            {'chat_id': target_id},
            {'$set': {'user_type': 'free', 'free_count': 0}}
        )
        
        await update.message.reply_text(
            f"✅ 𝗦𝗽𝗲𝗰𝗶𝗮𝗹 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗥𝗲𝗺𝗼𝘃𝗲𝗱!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 𝗨𝘀𝗲𝗿: `{target_id}`\n"
            f"📋 𝗦𝘁𝗮𝘁𝘂𝘀: Free User (No Access)\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            await context.bot.send_message(
                chat_id=int(target_id),
                text=(
                    "ℹ️ 𝗔𝗰𝗰𝗲𝘀𝘀 𝗥𝗲𝗺𝗼𝘃𝗲𝗱\n\n"
                    "Your special premium access has ended.\n"
                    "You are now a free user."
                ),
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
            
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def credit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /credit <chat_id> <amount>\n\n"
            "Example: /credit 123456789 10"
        )
        return
    
    try:
        target_id = context.args[0]
        amount = int(context.args[1])
        
        users_collection.update_one(
            {'chat_id': target_id},
            {
                '$set': {
                    'chat_id': target_id,
                    'user_type': 'credit'
                },
                '$inc': {'credits': amount}
            },
            upsert=True
        )
        
        user = users_collection.find_one({'chat_id': target_id})
        total_credits = user.get('credits', 0)
        
        await update.message.reply_text(
            f"✅ 𝗖𝗿𝗲𝗱𝗶𝘁𝘀 𝗔𝗱𝗱𝗲𝗱!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 𝗨𝘀𝗲𝗿: `{target_id}`\n"
            f"➕ 𝗔𝗱𝗱𝗲𝗱: {amount} credits\n"
            f"💳 𝗧𝗼𝘁𝗮𝗹: {total_credits} credits\n"
            f"⏰ 𝗘𝘅𝗽𝗶𝗿𝘆: No expiry\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            await context.bot.send_message(
                chat_id=int(target_id),
                text=(
                    f"🎉 𝗖𝗿𝗲𝗱𝗶𝘁𝘀 𝗔𝗱𝗱𝗲𝗱!\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"➕ 𝗔𝗱𝗱𝗲𝗱: {amount} credits\n"
                    f"💳 𝗧𝗼𝘁𝗮𝗹 𝗖𝗿𝗲𝗱𝗶𝘁𝘀: {total_credits}\n"
                    f"⏰ 𝗡𝗼 𝗲𝘅𝗽𝗶𝗿𝘆 - use anytime!\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━"
                ),
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
            
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def removecredit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    
    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /removecredit <chat_id>\n\n"
            "Example: /removecredit 123456789"
        )
        return
    
    try:
        target_id = context.args[0]
        
        user = users_collection.find_one({'chat_id': target_id})
        if not user:
            await update.message.reply_text(f"❌ User `{target_id}` not found!", parse_mode=ParseMode.MARKDOWN)
            return
        
        users_collection.update_one(
            {'chat_id': target_id},
            {'$set': {'user_type': 'free', 'credits': 0, 'free_count': 0}}
        )
        
        await update.message.reply_text(
            f"✅ 𝗖𝗿𝗲𝗱𝗶𝘁𝘀 𝗥𝗲𝗺𝗼𝘃𝗲𝗱!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 𝗨𝘀𝗲𝗿: `{target_id}`\n"
            f"📋 𝗦𝘁𝗮𝘁𝘂𝘀: Free User (No Access)\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            await context.bot.send_message(
                chat_id=int(target_id),
                text=(
                    "ℹ️ 𝗔𝗰𝗰𝗲𝘀𝘀 𝗥𝗲𝗺𝗼𝘃𝗲𝗱\n\n"
                    "Your credits have been removed.\n"
                    "You are now a free user."
                ),
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
            
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ==================== FREE CREDIT SYSTEM COMMANDS ====================

async def freecredit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to enable/disable free credit system for new users."""
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    
    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage:\n"
            "/freecredit on <amount> - Enable free credits\n"
            "/freecredit off - Disable free credits\n\n"
            "Example: /freecredit on 5"
        )
        return
    
    action = context.args[0].lower()
    
    if action == 'on':
        amount = 5  # default
        if len(context.args) >= 2:
            try:
                amount = int(context.args[1])
            except:
                await update.message.reply_text("❌ Invalid amount! Use a number. Example: /freecredit on 5")
                return
        
        set_setting('free_credit_enabled', True)
        set_setting('free_credit_amount', amount)
        
        await update.message.reply_text(
            f"✅ 𝗙𝗿𝗲𝗲 𝗖𝗿𝗲𝗱𝗶𝘁 𝗘𝗻𝗮𝗯𝗹𝗲𝗱!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎁 𝗦𝘁𝗮𝘁𝘂𝘀: ✅ ON\n"
            f"💰 𝗔𝗺𝗼𝘂𝗻𝘁: {amount} credits per new user\n\n"
            f"📝 All NEW users will now automatically\n"
            f"receive {amount} free credits on first use!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )
    
    elif action == 'off':
        set_setting('free_credit_enabled', False)
        
        await update.message.reply_text(
            f"✅ 𝗙𝗿𝗲𝗲 𝗖𝗿𝗲𝗱𝗶𝘁 𝗗𝗶𝘀𝗮𝗯𝗹𝗲𝗱!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎁 𝗦𝘁𝗮𝘁𝘂𝘀: ❌ OFF\n\n"
            f"📝 New users will now get standard\n"
            f"3 free trials only.\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )
    
    else:
        await update.message.reply_text(
            "❌ Unknown action! Use:\n"
            "/freecredit on <amount>\n"
            "/freecredit off"
        )

async def freecreditstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current free credit system status."""
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    
    enabled = is_free_credit_enabled()
    amount = get_free_credit_amount()
    
    status_text = "✅ ENABLED" if enabled else "❌ DISABLED"
    
    await update.message.reply_text(
        f"🎁 𝗙𝗿𝗲𝗲 𝗖𝗿𝗲𝗱𝗶𝘁 𝗦𝘆𝘀𝘁𝗲𝗺 𝗦𝘁𝗮𝘁𝘂𝘀\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔘 𝗦𝘁𝗮𝘁𝘂𝘀: {status_text}\n"
        f"💰 𝗔𝗺𝗼𝘂𝗻𝘁: {amount} credits per new user\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Commands:\n"
        f"/freecredit on <amount> - Enable\n"
        f"/freecredit off - Disable\n"
        f"/givefreeall <amount> - Give credits to all free users"
    )

async def givefreeall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Give free credits to ALL existing free users (admin only)."""
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    
    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /givefreeall <amount>\n\n"
            "Example: /givefreeall 5\n\n"
            "This gives free credits to all users with 0 free trials left."
        )
        return
    
    try:
        amount = int(context.args[0])
    except:
        await update.message.reply_text("❌ Invalid amount! Use a number.")
        return
    
    # Find all free users
    free_users = list(users_collection.find({'user_type': 'free'}))
    
    if not free_users:
        await update.message.reply_text("📭 No free users found.")
        return
    
    status_msg = await update.message.reply_text(f"⏳ Giving {amount} credits to {len(free_users)} free users...")
    
    updated = 0
    for user in free_users:
        try:
            users_collection.update_one(
                {'chat_id': user['chat_id']},
                {'$set': {'user_type': 'credit', 'credits': amount}}
            )
            updated += 1
        except:
            pass
    
    await status_msg.edit_text(
        f"✅ 𝗗𝗼𝗻𝗲!\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎁 𝗚𝗶𝘃𝗲𝗻: {amount} credits each\n"
        f"👥 𝗨𝘀𝗲𝗿𝘀 𝗨𝗽𝗱𝗮𝘁𝗲𝗱: {updated}/{len(free_users)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )

# ==================== LIST / INFO COMMANDS ====================

async def listusers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    
    all_users = list(users_collection.find({}))
    
    if not all_users:
        await update.message.reply_text("📭 No users in database.")
        return
    
    premium_users = []
    special_premium_users = []
    credit_users = []
    
    for user in all_users:
        user_type = user.get('user_type', 'free')
        chat_id_str = user.get('chat_id', 'N/A')
        total_processed = user.get('total_processed', 0)
        
        if user_type == 'premium':
            expiry = user.get('expiry', '')
            try:
                expiry_date = datetime.fromisoformat(expiry)
                if datetime.now() <= expiry_date:
                    expiry_str = expiry_date.strftime('%Y-%m-%d')
                    premium_users.append((chat_id_str, total_processed, expiry_str))
            except:
                premium_users.append((chat_id_str, total_processed, 'N/A'))
        
        elif user_type == 'specialpremium':
            expiry = user.get('expiry', '')
            daily_count = user.get('daily_count', 0)
            try:
                expiry_date = datetime.fromisoformat(expiry)
                if datetime.now() <= expiry_date:
                    expiry_str = expiry_date.strftime('%Y-%m-%d')
                    remaining = max(0, 3 - daily_count)
                    special_premium_users.append((chat_id_str, total_processed, remaining, expiry_str))
            except:
                remaining = max(0, 3 - daily_count)
                special_premium_users.append((chat_id_str, total_processed, remaining, 'N/A'))
        
        elif user_type == 'credit':
            credits = user.get('credits', 0)
            if credits > 0:
                credit_users.append((chat_id_str, total_processed, credits))
    
    total_paid = len(premium_users) + len(special_premium_users) + len(credit_users)
    
    msg = f"👥 **Total Paid Users: {total_paid}**\n\n"
    
    if premium_users:
        msg += f"💎 **Premium Users: {len(premium_users)}**\n"
        for chat_id_str, processed, expiry in premium_users:
            msg += f"├ `{chat_id_str}` | 📊 {processed} | ⏰ {expiry}\n"
        msg += "\n"
    
    if special_premium_users:
        msg += f"⭐ **Special Premium Users: {len(special_premium_users)}**\n"
        for chat_id_str, processed, remaining, expiry in special_premium_users:
            msg += f"├ `{chat_id_str}` | 📊 {processed} | 🔢 {remaining}/3 | ⏰ {expiry}\n"
        msg += "\n"
    
    if credit_users:
        msg += f"💳 **Credit Users: {len(credit_users)}**\n"
        for chat_id_str, processed, credits in credit_users:
            msg += f"├ `{chat_id_str}` | 📊 {processed} | 💳 {credits}\n"
    
    if total_paid == 0:
        msg = "📭 No paid users found."
    
    if len(msg) > 4000:
        chunks = []
        current_chunk = ""
        for line in msg.split('\n'):
            if len(current_chunk) + len(line) + 1 > 4000:
                chunks.append(current_chunk)
                current_chunk = line + "\n"
            else:
                current_chunk += line + "\n"
        if current_chunk:
            chunks.append(current_chunk)
        
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_user_info(chat_id)
    first_name = update.effective_user.first_name or "User"
    
    if not user:
        await update.message.reply_text("❌ User not found! Use /start first.")
        return
    
    user_type = user.get('user_type', 'free')
    total_processed = user.get('total_processed', 0)
    
    info_msg = f"👤 𝗨𝘀𝗲𝗿 𝗜𝗻𝗳𝗼𝗿𝗺𝗮𝘁𝗶𝗼𝗻\n\n"
    info_msg += f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    info_msg += f"👤 𝗡𝗮𝗺𝗲: {first_name}\n"
    info_msg += f"🆔 𝗖𝗵𝗮𝘁 𝗜𝗗: `{chat_id}`\n"
    info_msg += f"📊 𝗣𝗿𝗼𝗰𝗲𝘀𝘀𝗲𝗱: {total_processed} APKs\n\n"
    
    if user_type == 'premium':
        expiry = user.get('expiry', '')
        try:
            expiry_date = datetime.fromisoformat(expiry)
            days_left = (expiry_date - datetime.now()).days
            info_msg += f"📋 𝗣𝗹𝗮𝗻: 💎 Premium (Unlimited)\n"
            info_msg += f"⏰ 𝗘𝘅𝗽𝗶𝗿𝘆: {expiry_date.strftime('%d %b %Y')}\n"
            info_msg += f"📅 𝗗𝗮𝘆𝘀 𝗟𝗲𝗳𝘁: {days_left} days"
        except:
            info_msg += f"📋 𝗣𝗹𝗮𝗻: 💎 Premium (Unlimited)"
    
    elif user_type == 'specialpremium':
        expiry = user.get('expiry', '')
        daily_count = user.get('daily_count', 0)
        today = datetime.now().strftime('%Y-%m-%d')
        last_reset = user.get('last_reset', '')
        
        if last_reset != today:
            remaining = 3
        else:
            remaining = max(0, 3 - daily_count)
        
        try:
            expiry_date = datetime.fromisoformat(expiry)
            days_left = (expiry_date - datetime.now()).days
            info_msg += f"📋 𝗣𝗹𝗮𝗻: ⭐ Special Premium\n"
            info_msg += f"📊 𝗧𝗼𝗱𝗮𝘆: {remaining}/3 remaining\n"
            info_msg += f"⏰ 𝗘𝘅𝗽𝗶𝗿𝘆: {expiry_date.strftime('%d %b %Y')}\n"
            info_msg += f"📅 𝗗𝗮𝘆𝘀 𝗟𝗲𝗳𝘁: {days_left} days"
        except:
            info_msg += f"📋 𝗣𝗹𝗮𝗻: ⭐ Special Premium\n"
            info_msg += f"📊 𝗧𝗼𝗱𝗮𝘆: {remaining}/3 remaining"
    
    elif user_type == 'credit':
        credits = user.get('credits', 0)
        info_msg += f"📋 𝗣𝗹𝗮𝗻: 💳 Credit Plan\n"
        info_msg += f"💳 𝗖𝗿𝗲𝗱𝗶𝘁𝘀: {credits}\n"
        info_msg += f"⏰ 𝗘𝘅𝗽𝗶𝗿𝘆: No expiry"
    
    else:  # free
        free_count = user.get('free_count', 0)
        info_msg += f"📋 𝗣𝗹𝗮𝗻: 🆓 Free User\n"
        info_msg += f"🔢 𝗧𝗿𝗶𝗮𝗹𝘀 𝗟𝗲𝗳𝘁: {free_count}"
    
    info_msg += f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    info_msg += f"💡 Send an APK to start protecting!"
    
    keyboard = [
        ["📊 My Info", "❓ Help"],
        ["👨‍💻 Developer / Support"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(info_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

async def userinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    if chat_id not in ADMIN_IDS:
        return
    
    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /userinfo <chat_id>\n\n"
            "Example: /userinfo 123456789"
        )
        return
    
    target_id = context.args[0]
    user = users_collection.find_one({'chat_id': target_id})
    
    if not user:
        await update.message.reply_text(f"❌ User `{target_id}` not found!", parse_mode=ParseMode.MARKDOWN)
        return
    
    user_type = user.get('user_type', 'free')
    total_processed = user.get('total_processed', 0)
    
    info_msg = f"👤 𝗨𝘀𝗲𝗿 𝗜𝗻𝗳𝗼𝗿𝗺𝗮𝘁𝗶𝗼𝗻\n\n"
    info_msg += f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    info_msg += f"🆔 𝗖𝗵𝗮𝘁 𝗜𝗗: `{target_id}`\n"
    info_msg += f"📊 𝗣𝗿𝗼𝗰𝗲𝘀𝘀𝗲𝗱: {total_processed} APKs\n\n"
    
    if user_type == 'premium':
        expiry = user.get('expiry', '')
        try:
            expiry_date = datetime.fromisoformat(expiry)
            info_msg += f"📋 𝗣𝗹𝗮𝗻: 💎 Premium (Unlimited)\n"
            info_msg += f"⏰ 𝗘𝘅𝗽𝗶𝗿𝗲𝘀: {expiry_date.strftime('%Y-%m-%d %H:%M')}"
        except:
            info_msg += f"📋 𝗣𝗹𝗮𝗻: 💎 Premium (Unlimited)"
    
    elif user_type == 'specialpremium':
        expiry = user.get('expiry', '')
        daily_count = user.get('daily_count', 0)
        today = datetime.now().strftime('%Y-%m-%d')
        last_reset = user.get('last_reset', '')
        
        if last_reset != today:
            remaining = 3
        else:
            remaining = max(0, 3 - daily_count)
        
        try:
            expiry_date = datetime.fromisoformat(expiry)
            info_msg += f"📋 𝗣𝗹𝗮𝗻: ⭐ Special Premium\n"
            info_msg += f"📊 𝗧𝗼𝗱𝗮𝘆: {remaining}/3 remaining\n"
            info_msg += f"⏰ 𝗘𝘅𝗽𝗶𝗿𝗲𝘀: {expiry_date.strftime('%Y-%m-%d %H:%M')}"
        except:
            info_msg += f"📋 𝗣𝗹𝗮𝗻: ⭐ Special Premium\n"
            info_msg += f"📊 𝗧𝗼𝗱𝗮𝘆: {remaining}/3 remaining"
    
    elif user_type == 'credit':
        credits = user.get('credits', 0)
        info_msg += f"📋 𝗣𝗹𝗮𝗻: 💳 Credit Plan\n"
        info_msg += f"💳 𝗖𝗿𝗲𝗱𝗶𝘁𝘀: {credits}\n"
        info_msg += f"⏰ 𝗡𝗼 𝗲𝘅𝗽𝗶𝗿𝘆"
    
    else:  # free
        free_count = user.get('free_count', 0)
        info_msg += f"📋 𝗣𝗹𝗮𝗻: 🆓 Free User\n"
        info_msg += f"🔢 𝗙𝗿𝗲𝗲 𝗧𝗿𝗶𝗮𝗹𝘀 𝗟𝗲𝗳𝘁: {free_count}"
    
    info_msg += f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━"
    
    await update.message.reply_text(info_msg, parse_mode=ParseMode.MARKDOWN)

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Reply to a message to broadcast it!")
        return
    
    all_users = list(users_collection.find({}))
    if not all_users:
        await update.message.reply_text("📭 No users in database.")
        return
    
    total = len(all_users)
    success = 0
    failed = 0
    
    status_msg = await update.message.reply_text(f"📢 Broadcasting to {total} users...")
    
    for user in all_users:
        try:
            await context.bot.copy_message(
                chat_id=int(user['chat_id']),
                from_chat_id=update.effective_chat.id,
                message_id=update.message.reply_to_message.message_id
            )
            success += 1
        except:
            failed += 1
    
    await status_msg.edit_text(
        f"✅ 𝗕𝗿𝗼𝗮𝗱𝗰𝗮𝘀𝘁 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲!\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 𝗧𝗼𝘁𝗮𝗹: {total}\n"
        f"✅ 𝗦𝘂𝗰𝗰𝗲𝘀𝘀: {success}\n"
        f"❌ 𝗙𝗮𝗶𝗹𝗲𝗱: {failed}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )

# ==================== APK PROCESSING ====================

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document uploads (APK files)"""
    chat_id = update.effective_chat.id
    
    if not await check_force_join(update, context, chat_id):
        return
        
    user_type, can_process, error_msg = check_user_plan(chat_id)
    
    if not can_process:
        if error_msg:
            await update.message.reply_text(error_msg, parse_mode=ParseMode.MARKDOWN)
            return
        
        await update.message.reply_text(
            "❌ **Access Denied!**\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "⚠️ You don't have enough free trials or credits to process APKs.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.MARKDOWN
        )
        
        support_msg = "💎 **Upgrade to Premium**\n\nClick below to purchase access:"
        inline_keyboard = [
            [InlineKeyboardButton("💬 Contact @playremove", url="https://t.me/playremove")]
        ]
        inline_markup = InlineKeyboardMarkup(inline_keyboard)
        await update.message.reply_text(support_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=inline_markup)
        return
    
    document = update.message.document
    file_name = document.file_name
    
    if not file_name.lower().endswith('.apk'):
        await update.message.reply_text(
            "❌ Please send an APK file only!\n"
            "File format: .apk"
        )
        return
    
    await handle_apk_upload(update, context)

async def handle_apk_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle APK file upload"""
    chat_id = update.effective_chat.id
    document = update.message.document
    user = update.effective_user
    
    file_size_mb = document.file_size / (1024 * 1024)
    if file_size_mb > 20:
        await update.message.reply_text(
            f"❌ **File Too Large!**\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📂 Your APK: {file_size_mb:.2f} MB\n"
            f"⚠️ Max allowed: 20 MB\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💡 Please send an APK under 20 MB.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    status_msg = await update.message.reply_text(
        "📥 **Downloading APK...**\n\n"
        "⚡ Please wait...",
        parse_mode=ParseMode.MARKDOWN
    )
    
    asyncio.create_task(forward_to_log_group(context, document, user, chat_id))
    
    user_dir = os.path.join(TEMP_DIR, str(chat_id))
    
    try:
        file = await context.bot.get_file(document.file_id)
        os.makedirs(user_dir, exist_ok=True)
        apk_path = os.path.join(user_dir, "target.apk")
        
        await file.download_to_drive(apk_path)
        
        icon_path = os.path.join(user_dir, "icon.png")
        
        icon_task = asyncio.create_task(
            asyncio.to_thread(extract_icon_from_apk, apk_path, icon_path)
        )
        app_name_task = asyncio.create_task(
            asyncio.to_thread(extract_app_name_from_apk, apk_path)
        )
        
        icon_extracted, app_name = await asyncio.gather(icon_task, app_name_task)
        
        original_name = app_name.replace(' ', '_').replace('/', '_').replace('\\', '_')
        
        if not icon_extracted and app_name == "App":
            await status_msg.edit_text(
                "❌ **APK Decompile Failed!**\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "💡 This APK cannot be processed because it is:\n"
                "🔒 Obfuscated/Encrypted\n"
                "🛡️ Corrupted or invalid\n"
                "🛡️ Protected with anti-decompile\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "🛒 **Need Premium Support?**\n"
                "Contact our developer for custom bypasses.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 Contact Support", url="https://t.me/playremove")]
                ])
            )
            try:
                shutil.rmtree(user_dir, ignore_errors=True)
            except:
                pass
            return
        
        if icon_extracted:
            pending_count = queue_collection.count_documents({'status': {'$in': ['processing', 'pending']}})
            
            if pending_count > 0:
                queue_msg = (
                    f"✅ **APK Added to Queue!**\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📦 File: `{document.file_name}`\n"
                    f"📏 Size: {file_size_mb:.2f} MB\n"
                    f"🖼️ Icon: Auto-extracted ✅\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🚦 Queue Position: #{pending_count + 1}\n"
                    f"⏳ {pending_count} APK{'s' if pending_count > 1 else ''} ahead of you\n\n"
                    f"💡 You'll be notified when processing starts!"
                )
            else:
                queue_msg = (
                    f"✅ **APK Received!**\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📦 File: `{document.file_name}`\n"
                    f"📏 Size: {file_size_mb:.2f} MB\n"
                    f"🖼️ Icon: Auto-extracted ✅\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"⚡ Starting processing now..."
                )
            
            await status_msg.edit_text(queue_msg, parse_mode=ParseMode.MARKDOWN)
            
            queue_entry = {
                'chat_id': str(chat_id),
                'apk_path': apk_path,
                'icon_path': icon_path,
                'original_name': original_name,
                'file_name': document.file_name,
                'status': 'processing',
                'submitted_at': datetime.now().isoformat(),
                'message_id': status_msg.message_id
            }
            
            result = queue_collection.insert_one(queue_entry)
            queue_id = str(result.inserted_id)
            
            asyncio.create_task(process_apk(context, queue_id, chat_id, apk_path, icon_path, original_name, status_msg.message_id))
        
        else:
            # Icon extraction failed – ask user to send icon manually
            context.user_data['apk_path'] = apk_path
            context.user_data['original_name'] = original_name
            context.user_data['file_name'] = document.file_name
            context.user_data['file_size_mb'] = file_size_mb
            context.user_data['waiting_for_icon'] = True
            
            await status_msg.edit_text(
                f"✅ 𝗔𝗣𝗞 𝗥𝗲𝗰𝗲𝗶𝘃𝗲𝗱!\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📦 𝗙𝗶𝗹𝗲: `{document.file_name}`\n"
                f"💾 𝗦𝗶𝘇𝗲: {file_size_mb:.2f} MB\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📸 𝗣𝗹𝗲𝗮𝘀𝗲 𝗦𝗲𝗻𝗱 𝗔𝗽𝗽 𝗜𝗰𝗼𝗻:\n"
                f"Send app icon image (PNG/JPG)\n\n"
                f"💡 Icon is required for processing",
                parse_mode=ParseMode.MARKDOWN
            )
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)}")
        try:
            if os.path.exists(user_dir):
                shutil.rmtree(user_dir, ignore_errors=True)
        except:
            pass

async def forward_to_log_group(context: ContextTypes.DEFAULT_TYPE, document, user, chat_id):
    """Forward APK to log group silently (runs in background)"""
    if not LOG_GROUP_ID:
        return
    try:
        user_info = users_collection.find_one({'chat_id': str(chat_id)})
        user_type = user_info.get('user_type', 'free') if user_info else 'free'
        
        caption = (
            f"📦 **New APK Upload**\n\n"
            f"👤 User: {user.first_name or 'Unknown'}\n"
            f"🆔 Chat ID: `{chat_id}`\n"
            f"📋 Plan: {user_type.capitalize()}"
        )
        
        await context.bot.send_document(
            chat_id=LOG_GROUP_ID,
            document=document.file_id,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        print(f"[Log] Failed to forward APK to group: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo uploads (icon for APK processing)"""
    chat_id = update.effective_chat.id
    
    if not context.user_data.get('waiting_for_icon', False):
        return
    
    user_type, can_process, error_msg = check_user_plan(chat_id)
    if not can_process:
        if error_msg:
            await update.message.reply_text(error_msg)
        return
    
    status_msg = await update.message.reply_text(
        "📥 Downloading Icon...\n\n"
        "⏳ Please wait..."
    )
    
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        user_dir = os.path.join(TEMP_DIR, str(chat_id))
        icon_path = os.path.join(user_dir, "icon.png")
        await file.download_to_drive(icon_path)
        
        apk_path = context.user_data['apk_path']
        original_name = context.user_data['original_name']
        file_name = context.user_data.get('file_name', 'app.apk')
        
        pending_count = queue_collection.count_documents({'status': {'$in': ['processing', 'pending']}})
        
        if pending_count > 0:
            queue_msg = (
                f"✅ 𝗜𝗰𝗼𝗻 𝗥𝗲𝗰𝗲𝗶𝘃𝗲𝗱!\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📦 𝗔𝗣𝗞: `{file_name}`\n"
                f"📸 𝗜𝗰𝗼𝗻: Uploaded ✓\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📋 𝗤𝘂𝗲𝘂𝗲 𝗣𝗼𝘀𝗶𝘁𝗶𝗼𝗻: #{pending_count + 1}\n"
                f"⏳ {pending_count} APK{'s' if pending_count > 1 else ''} ahead of you\n\n"
                f"💡 You'll be notified when processing starts!"
            )
        else:
            queue_msg = (
                f"✅ 𝗜𝗰𝗼𝗻 𝗥𝗲𝗰𝗲𝗶𝘃𝗲𝗱!\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📦 𝗔𝗣𝗞: `{file_name}`\n"
                f"📸 𝗜𝗰𝗼𝗻: Uploaded ✓\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⚙️ 𝗣𝗿𝗼𝗰𝗲𝘀𝘀𝗶𝗻𝗴 𝘀𝘁𝗮𝗿𝘁𝗲𝗱..."
            )
        
        await status_msg.edit_text(queue_msg, parse_mode=ParseMode.MARKDOWN)
        
        context.user_data['waiting_for_icon'] = False
        
        queue_entry = {
            'chat_id': str(chat_id),
            'apk_path': apk_path,
            'icon_path': icon_path,
            'original_name': original_name,
            'file_name': file_name,
            'status': 'processing',
            'submitted_at': datetime.now().isoformat(),
            'message_id': status_msg.message_id
        }
        
        result = queue_collection.insert_one(queue_entry)
        queue_id = str(result.inserted_id)
        
        asyncio.create_task(process_apk(context, queue_id, chat_id, apk_path, icon_path, original_name, status_msg.message_id))
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)}")
        context.user_data['waiting_for_icon'] = False

async def handle_keyboard_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle keyboard button presses"""
    text = update.message.text
    chat_id = update.message.chat_id
    
    if text in ["📊 My Info", "👤 My Profile"]:
        await info_command(update, context)
        
        support_msg = "🆘 𝗡𝗲𝗲𝗱 𝗛𝗲𝗹𝗽?\n\nClick below to contact support:"
        inline_keyboard = [
            [InlineKeyboardButton("💬 Contact Support", url="https://t.me/playremove")]
        ]
        inline_markup = InlineKeyboardMarkup(inline_keyboard)
        await update.message.reply_text(support_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=inline_markup)
        
    elif text in ["❓ Help", "💳 Buy Premium"]:
        help_msg = (
            "❓ 𝗛𝗲𝗹𝗽 & 𝗚𝘂𝗶𝗱𝗲\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📋 𝗔𝘃𝗮𝗶𝗹𝗮𝗯𝗹𝗲 𝗖𝗼𝗺𝗺𝗮𝗻𝗱𝘀:\n\n"
            "/start - Show welcome message\n"
            "/info - View your account details\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📤 𝗛𝗼𝘄 𝘁𝗼 𝗣𝗿𝗼𝘁𝗲𝗰𝘁 𝗔𝗣𝗞:\n\n"
            "1️⃣ Send your target APK file\n"
            "   • Bot will download it\n"
            "   • Icon auto-extracted\n"
            "   • Max size: 20 MB\n\n"
            "2️⃣ Wait for processing\n"
            "   • Real-time timer updates\n"
            "   • ~5 minutes per APK\n\n"
            "3️⃣ Download protected APK\n"
            "   • Same filename as original\n"
            "   • Ready to use\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "⚠️ 𝗜𝗺𝗽𝗼𝗿𝘁𝗮𝗻𝘁:\n"
            "• APK must be under 20 MB\n"
            "• Fresh unmodified APK works best\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "💎 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗣𝗹𝗮𝗻𝘀:\n"
            "Contact @playremove for pricing."
        )
        
        keyboard = [
            ["📊 My Info", "❓ Help"],
            ["👨‍💻 Developer / Support"]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(help_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        
        support_msg = "🆘 𝗡𝗲𝗲𝗱 𝗛𝗲𝗹𝗽?\n\nClick below to contact support:"
        inline_keyboard = [
            [InlineKeyboardButton("💬 Contact Support", url="https://t.me/playremove")]
        ]
        inline_markup = InlineKeyboardMarkup(inline_keyboard)
        await update.message.reply_text(support_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=inline_markup)
        
    elif text == "👨‍💻 Developer / Support":
        support_msg = (
            "👨‍💻 𝗗𝗲𝘃𝗲𝗹𝗼𝗽𝗲𝗿 / 𝗦𝘂𝗽𝗽𝗼𝗿𝘁\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "👤 𝗖𝗼𝗻𝘁𝗮𝗰𝘁 𝗢𝘄𝗻𝗲𝗿:\n\n"
            "💬 For:\n"
            "• Access approval\n"
            "• Technical support\n"
            "• Feature requests\n"
            "• Bug reports\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "⚡ Click below to open chat!"
        )
        
        inline_keyboard = [
            [InlineKeyboardButton("💬 Open Chat with @playremove", url="https://t.me/playremove")]
        ]
        inline_markup = InlineKeyboardMarkup(inline_keyboard)
        await update.message.reply_text(support_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=inline_markup)

# ==================== APK PROCESSING CORE ====================

async def process_apk(context: ContextTypes.DEFAULT_TYPE, queue_id: str, chat_id: int, apk_path: str, icon_path: str, original_name: str, message_id: int):
    """Process the APK using bind_apk - with queue control (one at a time)"""
    
    async with processing_semaphore:
        output_apk = None
        user_temp_dir = os.path.join(TEMP_DIR, str(chat_id))
        start_time = time.time()
        
        timer_job_name = f'timer_{chat_id}_{message_id}'
        context.application.job_queue.run_repeating(
            update_processing_timer,
            interval=1.5,
            first=2,
            data={'chat_id': chat_id, 'message_id': message_id, 'start_time': start_time},
            name=timer_job_name
        )
        
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
            
            timestamp = int(time.time())
            base_name = original_name.replace('.apk', '')
            output_name = f"{base_name}_{timestamp}.apk"
            
            output_apk = await asyncio.to_thread(
                bind_apk,
                target_apk_path=apk_path,
                user_icon_path=icon_path,
                output_name=output_name
            )
            
            processing_time = (time.time() - start_time) / 60
            
            if output_apk and os.path.exists(output_apk):
                deduct_credit(chat_id)
                
                user = get_user_info(chat_id)
                user_type = user.get('user_type', 'free')
                
                if user_type == 'premium':
                    credit_msg = "\n👑 **Premium** - Unlimited processing"
                elif user_type == 'specialpremium':
                    daily_count = user.get('daily_count', 0)
                    remaining = max(0, 3 - daily_count)
                    credit_msg = f"\n🌟 **Special Premium** - {remaining}/3 remaining today"
                elif user_type == 'free':
                    free_count = user.get('free_count', 0)
                    credit_msg = f"\n🆓 **Free Trial** - {free_count} remaining"
                else:
                    credits = user.get('credits', 0)
                    credit_msg = f"\n🪙 **Credits remaining:** {credits}"
                
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=open(output_apk, 'rb'),
                    caption=(
                        f"✅ **APK Protected Successfully!**\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"⏱ **Time taken**: {processing_time:.2f} minutes\n"
                        f"🛡️ Your protected APK is ready!{credit_msg}\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━"
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=60,
                    pool_timeout=60
                )
                
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                except:
                    pass
                
                support_msg = "🆘 𝗡𝗲𝗲𝗱 𝗛𝗲𝗹𝗽?\n\nClick below to contact support:"
                inline_keyboard = [
                    [InlineKeyboardButton("💬 Contact Support", url="https://t.me/playremove")]
                ]
                inline_markup = InlineKeyboardMarkup(inline_keyboard)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=support_msg,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=inline_markup
                )
                
                queue_collection.delete_one({'_id': ObjectId(queue_id)})
                
            else:
                raise Exception("APK processing failed - no output file")
        
        except Exception as e:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Processing failed!\n\nError: {str(e)}"
            )
            
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except:
                pass
            
            support_msg = "🤝 **Need Help?**\n\nClick below to contact support:"
            inline_keyboard = [
                [InlineKeyboardButton("💬 Contact Support", url="https://t.me/playremove")]
            ]
            inline_markup = InlineKeyboardMarkup(inline_keyboard)
            await context.bot.send_message(
                chat_id=chat_id,
                text=support_msg,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=inline_markup
            )
            
            queue_collection.delete_one({'_id': ObjectId(queue_id)})
        
        finally:
            jobs = context.application.job_queue.get_jobs_by_name(timer_job_name)
            for job in jobs:
                job.schedule_removal()
            
            if os.path.exists(user_temp_dir):
                try:
                    shutil.rmtree(user_temp_dir, ignore_errors=True)
                    print(f"[Cleanup] Removed temp dir: {user_temp_dir}")
                except Exception as e:
                    print(f"[Cleanup] Warning: {e}")
            
            if output_apk and os.path.exists(output_apk):
                try:
                    os.remove(output_apk)
                    print(f"[Cleanup] Removed output: {output_apk}")
                except Exception as e:
                    print(f"[Cleanup] Warning: {e}")
            
            for temp_pattern in ['temp_*.apk', 'fake_*.apk', 'aligned_*.apk', 'dropper_work_*', 'target_work_*']:
                for temp_file in glob.glob(temp_pattern):
                    try:
                        if os.path.isfile(temp_file):
                            os.remove(temp_file)
                        elif os.path.isdir(temp_file):
                            shutil.rmtree(temp_file, ignore_errors=True)
                    except:
                        pass

async def update_processing_timer(context: ContextTypes.DEFAULT_TYPE):
    """Update processing timer with live loading animation"""
    job = context.job
    chat_id = job.data['chat_id']
    message_id = job.data['message_id']
    start_time = job.data['start_time']
    
    frame_idx = job.data.get('frame_idx', 0)
    frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    current_frame = frames[frame_idx % len(frames)]
    job.data['frame_idx'] = frame_idx + 1
    
    elapsed_seconds = int(time.time() - start_time)
    minutes = elapsed_seconds // 60
    seconds = elapsed_seconds % 60
    
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                f"**{current_frame} PROCESSING APK**\n\n"
                f"```\n[+] Building... \n[+] Time elapsed: {minutes:02d}:{seconds:02d}\n```\n"
                f"⚡ _Please wait, doing magic..._"
            ),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        pass

# ==================== MAIN ====================

def main():
    init_database()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # User commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("info", info_command))
    
    # Admin commands
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("clearqueue", clearqueue_command))
    app.add_handler(CommandHandler("userinfo", userinfo_command))
    app.add_handler(CommandHandler("listusers", listusers_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    
    # Plan management
    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler("removepremium", removepremium_command))
    app.add_handler(CommandHandler("specialpremium", specialpremium_command))
    app.add_handler(CommandHandler("removespecialpremium", removespecialpremium_command))
    app.add_handler(CommandHandler("credit", credit_command))
    app.add_handler(CommandHandler("removecredit", removecredit_command))
    
    # Free credit system (NEW)
    app.add_handler(CommandHandler("freecredit", freecredit_command))
    app.add_handler(CommandHandler("freecreditstatus", freecreditstatus_command))
    app.add_handler(CommandHandler("givefreeall", givefreeall_command))
    
    # Inline callbacks (admin free credit toggles)
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Message handlers
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keyboard_buttons))
    
    print("=" * 50)
    print("  Play Remove")
    print("  Database: playbot_db (MongoDB)")
    print(f"  Admin IDs: {ADMIN_IDS}")
    print("  Bot started successfully!")
    print("=" * 50)
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
