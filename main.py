import os
import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiohttp
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load bot token from environment
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set")

# CoinGecko API endpoint
COINGECKO_API = "https://api.coingecko.com/api/v3/simple/price"

# Initialize bot and dispatcher
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# User alerts storage (in production, use a database)
user_alerts = {}

# Price history for trend analysis
price_history = {'BTC': [], 'ETH': [], 'TON': []}

# Supported cryptocurrencies with details
CRYPTO_INFO = {
    'bitcoin': {'symbol': 'BTC', 'emoji': '🟠', 'name': 'Bitcoin'},
    'ethereum': {'symbol': 'ETH', 'emoji': '🔵', 'name': 'Ethereum'},
    'the-open-network': {'symbol': 'TON', 'emoji': '💎', 'name': 'TON'},
    'tether': {'symbol': 'USDT', 'emoji': '💵', 'name': 'Tether'},
    'binancecoin': {'symbol': 'BNB', 'emoji': '🟡', 'name': 'BNB'},
    'solana': {'symbol': 'SOL', 'emoji': '🟣', 'name': 'Solana'},
    'ripple': {'symbol': 'XRP', 'emoji': '⚪', 'name': 'Ripple'},
}

# States for alert setup
class AlertStates(StatesGroup):
    waiting_for_crypto = State()
    waiting_for_price = State()


# Cache for prices to reduce API calls
price_cache = {'data': None, 'timestamp': 0}
CACHE_DURATION = 60  # Cache for 60 seconds

async def fetch_crypto_prices(crypto_ids=None, use_cache=True):
    """Fetch cryptocurrency prices from CoinGecko API"""
    # Check cache first
    if use_cache and price_cache['data'] is not None:
        if datetime.now().timestamp() - price_cache['timestamp'] < CACHE_DURATION:
            logger.info("Using cached price data")
            return price_cache['data']
    
    if crypto_ids is None:
        crypto_ids = ','.join(CRYPTO_INFO.keys())
    
    params = {
        'ids': crypto_ids,
        'vs_currencies': 'usd',
        'include_24hr_change': 'true',
        'include_market_cap': 'true'
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(COINGECKO_API, params=params, timeout=15) as response:
                if response.status == 200:
                    data = await response.json()
                    result = {}
                    for coin_id, info in CRYPTO_INFO.items():
                        if coin_id in data:
                            result[info['symbol']] = {
                                'price': data[coin_id].get('usd'),
                                'change_24h': data[coin_id].get('usd_24h_change'),
                                'market_cap': data[coin_id].get('usd_market_cap')
                            }
                    # Update cache
                    price_cache['data'] = result
                    price_cache['timestamp'] = datetime.now().timestamp()
                    return result
                elif response.status == 429:
                    logger.error("API rate limit exceeded - Too many requests")
                    # Return cached data if available
                    if price_cache['data'] is not None:
                        logger.info("Returning cached data due to rate limit")
                        return price_cache['data']
                    return None
                else:
                    logger.error(f"API request failed with status {response.status}")
                    return None
    except asyncio.TimeoutError:
        logger.error("API request timed out")
        return None
    except aiohttp.ClientError as e:
        logger.error(f"Network error fetching prices: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching prices: {e}")
        return None


def get_trend_emoji(change):
    """Get trend emoji based on price change"""
    if change is None:
        return "➖"
    elif change > 5:
        return "🚀"
    elif change > 0:
        return "📈"
    elif change < -5:
        return "📉"
    elif change < 0:
        return "🔻"
    else:
        return "➖"


def format_price_message(prices):
    """Format prices into a nice message"""
    if not prices:
        return "❌ Narxlarni olishda xatolik yuz berdi."
    
    message = "💰 <b>Kriptovalyuta Narxlari (USD)</b>\n\n"
    
    for symbol, data in prices.items():
        if data['price'] is not None:
            emoji = CRYPTO_INFO.get([k for k, v in CRYPTO_INFO.items() if v['symbol'] == symbol][0], {}).get('emoji', '•')
            trend = get_trend_emoji(data.get('change_24h'))
            change = data.get('change_24h', 0)
            change_text = f"{change:+.2f}%" if change is not None else "N/A"
            
            message += f"{emoji} <b>{symbol}</b>: ${data['price']:,.2f}\n"
            message += f"   {trend} 24h: {change_text}\n\n"
    
    message += f"🕐 Yangilangan: {datetime.now().strftime('%H:%M:%S')}"
    return message


def get_main_keyboard():
    """Create main inline keyboard"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 Barcha narxlar", callback_data="prices_all"),
            InlineKeyboardButton(text="📊 TOP 3", callback_data="prices_top3")
        ],
        [
            InlineKeyboardButton(text="🔔 Alert qo'yish", callback_data="set_alert"),
            InlineKeyboardButton(text="📋 Mening alertlarim", callback_data="my_alerts")
        ],
        [
            InlineKeyboardButton(text="📈 Statistika", callback_data="statistics"),
            InlineKeyboardButton(text="ℹ️ Yordam", callback_data="help")
        ]
    ])
    return keyboard


async def check_price_alerts():
    """Check if any price alerts should be triggered"""
    prices = await fetch_crypto_prices(use_cache=True)  # Use cache to avoid rate limits
    if not prices:
        return
    
    triggered_alerts = []
    
    for user_id, alerts in list(user_alerts.items()):
        for alert in alerts[:]:
            crypto = alert['crypto']
            target_price = alert['target_price']
            alert_type = alert['type']
            
            current_price = prices.get(crypto, {}).get('price')
            if current_price is None:
                continue
            
            if (alert_type == 'above' and current_price >= target_price) or \
               (alert_type == 'below' and current_price <= target_price):
                triggered_alerts.append({
                    'user_id': user_id,
                    'crypto': crypto,
                    'target_price': target_price,
                    'current_price': current_price,
                    'type': alert_type
                })
                alerts.remove(alert)
    
    # Send alert notifications
    for alert in triggered_alerts:
        try:
            message = (
                f"🔔 <b>ALERT!</b>\n\n"
                f"{alert['crypto']} narxi ${alert['current_price']:,.2f} ga yetdi!\n"
                f"Sizning belgilangan narxingiz: ${alert['target_price']:,.2f}"
            )
            await bot.send_message(alert['user_id'], message, parse_mode='HTML')
            logger.info(f"Alert triggered for user {alert['user_id']}: {alert['crypto']}")
        except Exception as e:
            logger.error(f"Failed to send alert to user {alert['user_id']}: {e}")


async def background_price_checker():
    """Background task that checks prices periodically"""
    await asyncio.sleep(5)  # Wait for bot to start
    
    while True:
        try:
            logger.info("Running background price check...")
            prices = await fetch_crypto_prices('bitcoin,ethereum,the-open-network', use_cache=False)
            
            if prices:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.info(f"[{timestamp}] Price Update:")
                
                # Store price history
                for symbol in ['BTC', 'ETH', 'TON']:
                    if symbol in prices and prices[symbol]['price']:
                        price_history[symbol].append({
                            'time': timestamp,
                            'price': prices[symbol]['price']
                        })
                        # Keep only last 100 entries
                        if len(price_history[symbol]) > 100:
                            price_history[symbol].pop(0)
                        
                        logger.info(f"  {symbol}: ${prices[symbol]['price']:,.2f}")
            
            # Check price alerts
            await check_price_alerts()
                
        except Exception as e:
            logger.error(f"Error in background task: {e}")
        
        await asyncio.sleep(120)  # Increased to 2 minutes to avoid rate limits


@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Handle /start command"""
    welcome_text = (
        "👋 <b>Crypto Price Bot'ga xush kelibsiz!</b>\n\n"
        "Men sizga kriptovalyuta narxlarini kuzatishda yordam beraman.\n\n"
        "🚀 <b>Imkoniyatlar:</b>\n"
        "• Real-vaqt narxlari\n"
        "• 24 soatlik o'zgarishlar\n"
        "• Narx alertlari\n"
        "• Trend tahlili\n\n"
        "Quyidagi tugmalardan birini tanlang:"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard(), parse_mode='HTML')


@dp.message(Command("price"))
async def cmd_price(message: Message):
    """Handle /price command"""
    await message.answer("🔍 Narxlarni yangilayapman...", parse_mode='HTML')
    
    prices = await fetch_crypto_prices('bitcoin,ethereum,the-open-network')
    response = format_price_message(prices)
    
    await message.answer(response, parse_mode='HTML')


@dp.message(Command("alert"))
async def cmd_alert(message: Message, state: FSMContext):
    """Handle /alert command"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟠 BTC", callback_data="alert_BTC")],
        [InlineKeyboardButton(text="🔵 ETH", callback_data="alert_ETH")],
        [InlineKeyboardButton(text="💎 TON", callback_data="alert_TON")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")]
    ])
    
    await message.answer(
        "🔔 <b>Alert o'rnatish</b>\n\nQaysi kriptovalyuta uchun alert qo'ymoqchisiz?",
        reply_markup=keyboard,
        parse_mode='HTML'
    )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Show statistics"""
    stats_text = "📊 <b>Statistika</b>\n\n"
    
    for symbol in ['BTC', 'ETH', 'TON']:
        if price_history[symbol]:
            recent_prices = [p['price'] for p in price_history[symbol][-10:]]
            avg_price = sum(recent_prices) / len(recent_prices)
            min_price = min(recent_prices)
            max_price = max(recent_prices)
            
            stats_text += f"<b>{symbol}</b>:\n"
            stats_text += f"  📍 O'rtacha (10 daqiqa): ${avg_price:,.2f}\n"
            stats_text += f"  📉 Min: ${min_price:,.2f}\n"
            stats_text += f"  📈 Max: ${max_price:,.2f}\n\n"
    
    await message.answer(stats_text, parse_mode='HTML')


@dp.callback_query(F.data == "prices_all")
async def show_all_prices(callback: CallbackQuery):
    """Show all cryptocurrency prices"""
    await callback.message.edit_text("🔍 Barcha narxlarni yangilayapman...")
    
    prices = await fetch_crypto_prices()
    response = format_price_message(prices)
    
    await callback.message.edit_text(response, parse_mode='HTML', reply_markup=get_main_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "prices_top3")
async def show_top3_prices(callback: CallbackQuery):
    """Show top 3 cryptocurrency prices"""
    await callback.message.edit_text("🔍 TOP 3 narxlarni yangilayapman...")
    
    prices = await fetch_crypto_prices('bitcoin,ethereum,the-open-network')
    response = format_price_message(prices)
    
    await callback.message.edit_text(response, parse_mode='HTML', reply_markup=get_main_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "set_alert")
async def start_alert_setup(callback: CallbackQuery, state: FSMContext):
    """Start alert setup process"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟠 BTC", callback_data="alert_BTC")],
        [InlineKeyboardButton(text="🔵 ETH", callback_data="alert_ETH")],
        [InlineKeyboardButton(text="💎 TON", callback_data="alert_TON")],
        [InlineKeyboardButton(text="🔙 Ortga", callback_data="back_main")]
    ])
    
    await callback.message.edit_text(
        "🔔 <b>Alert o'rnatish</b>\n\nQaysi kriptovalyuta uchun alert qo'ymoqchisiz?",
        reply_markup=keyboard,
        parse_mode='HTML'
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("alert_"))
async def choose_crypto_for_alert(callback: CallbackQuery, state: FSMContext):
    """Handle crypto selection for alert"""
    crypto = callback.data.split("_")[1]
    await state.update_data(crypto=crypto)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Narx oshganda", callback_data="alert_type_above")],
        [InlineKeyboardButton(text="📉 Narx tushganda", callback_data="alert_type_below")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="back_main")]
    ])
    
    await callback.message.edit_text(
        f"🔔 <b>{crypto} uchun alert</b>\n\nQachon xabar yuborishimni xohlaysiz?",
        reply_markup=keyboard,
        parse_mode='HTML'
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("alert_type_"))
async def set_alert_type(callback: CallbackQuery, state: FSMContext):
    """Set alert type and ask for target price"""
    alert_type = callback.data.split("_")[-1]
    await state.update_data(alert_type=alert_type)
    
    data = await state.get_data()
    crypto = data.get('crypto')
    
    # Get current price
    prices = await fetch_crypto_prices()
    current_price = prices.get(crypto, {}).get('price', 0)
    
    type_text = "oshganda" if alert_type == "above" else "tushganda"
    
    await callback.message.edit_text(
        f"💰 <b>{crypto} uchun narx kiriting</b>\n\n"
        f"Hozirgi narx: ${current_price:,.2f}\n\n"
        f"Narx {type_text} xabar olasiz.\n"
        f"Masalan: 50000 yoki 3500.50",
        parse_mode='HTML'
    )
    
    await state.set_state(AlertStates.waiting_for_price)
    await callback.answer()


@dp.message(AlertStates.waiting_for_price)
async def process_alert_price(message: Message, state: FSMContext):
    """Process the target price for alert"""
    try:
        target_price = float(message.text.replace(',', ''))
        data = await state.get_data()
        
        user_id = message.from_user.id
        if user_id not in user_alerts:
            user_alerts[user_id] = []
        
        user_alerts[user_id].append({
            'crypto': data['crypto'],
            'target_price': target_price,
            'type': data['alert_type']
        })
        
        type_text = "oshganda" if data['alert_type'] == "above" else "tushganda"
        
        await message.answer(
            f"✅ <b>Alert muvaffaqiyatli o'rnatildi!</b>\n\n"
            f"💎 Kriptovalyuta: {data['crypto']}\n"
            f"💰 Narx: ${target_price:,.2f} {type_text}\n\n"
            f"Narx belgilangan darajaga yetganda xabar olasiz!",
            reply_markup=get_main_keyboard(),
            parse_mode='HTML'
        )
        
        await state.clear()
        
    except ValueError:
        await message.answer(
            "❌ Noto'g'ri format! Iltimos, raqam kiriting.\n"
            "Masalan: 50000 yoki 3500.50"
        )


@dp.callback_query(F.data == "my_alerts")
async def show_my_alerts(callback: CallbackQuery):
    """Show user's active alerts"""
    user_id = callback.from_user.id
    alerts = user_alerts.get(user_id, [])
    
    if not alerts:
        await callback.message.edit_text(
            "📋 <b>Sizda hozircha alertlar yo'q</b>\n\n"
            "Alert qo'yish uchun 'Alert qo'yish' tugmasini bosing.",
            reply_markup=get_main_keyboard(),
            parse_mode='HTML'
        )
    else:
        text = "📋 <b>Sizning alertlaringiz:</b>\n\n"
        for i, alert in enumerate(alerts, 1):
            type_text = "oshganda" if alert['type'] == "above" else "tushganda"
            text += f"{i}. {alert['crypto']}: ${alert['target_price']:,.2f} {type_text}\n"
        
        await callback.message.edit_text(
            text,
            reply_markup=get_main_keyboard(),
            parse_mode='HTML'
        )
    
    await callback.answer()


@dp.callback_query(F.data == "statistics")
async def show_statistics(callback: CallbackQuery):
    """Show price statistics"""
    stats_text = "📊 <b>Narx Statistikasi</b>\n\n"
    
    for symbol in ['BTC', 'ETH', 'TON']:
        if price_history[symbol]:
            recent_prices = [p['price'] for p in price_history[symbol][-10:]]
            if recent_prices:
                avg_price = sum(recent_prices) / len(recent_prices)
                min_price = min(recent_prices)
                max_price = max(recent_prices)
                
                stats_text += f"<b>{symbol}</b> (so'nggi 10 daqiqa):\n"
                stats_text += f"  📍 O'rtacha: ${avg_price:,.2f}\n"
                stats_text += f"  📉 Minimal: ${min_price:,.2f}\n"
                stats_text += f"  📈 Maksimal: ${max_price:,.2f}\n\n"
    
    if len(stats_text) < 50:
        stats_text += "Ma'lumot to'planmoqda, biroz kuting..."
    
    await callback.message.edit_text(
        stats_text,
        reply_markup=get_main_keyboard(),
        parse_mode='HTML'
    )
    await callback.answer()


@dp.callback_query(F.data == "help")
async def show_help(callback: CallbackQuery):
    """Show help message"""
    help_text = (
        "ℹ️ <b>Yordam</b>\n\n"
        "<b>Komandalar:</b>\n"
        "/start - Botni ishga tushirish\n"
        "/price - Narxlarni ko'rish\n"
        "/alert - Alert o'rnatish\n"
        "/stats - Statistikani ko'rish\n\n"
        "<b>Xususiyatlar:</b>\n"
        "• Real-vaqt narxlari (har daqiqa yangilanadi)\n"
        "• 24 soatlik o'zgarishlar\n"
        "• Narx alertlari\n"
        "• Trend ko'rsatkichlari\n"
        "• Narx statistikasi\n\n"
        "Savollar bo'lsa, botni qayta ishga tushiring: /start"
    )
    
    await callback.message.edit_text(
        help_text,
        reply_markup=get_main_keyboard(),
        parse_mode='HTML'
    )
    await callback.answer()


@dp.callback_query(F.data == "back_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    """Go back to main menu"""
    await state.clear()
    
    welcome_text = (
        "👋 <b>Bosh menyu</b>\n\n"
        "Quyidagi tugmalardan birini tanlang:"
    )
    
    await callback.message.edit_text(
        welcome_text,
        reply_markup=get_main_keyboard(),
        parse_mode='HTML'
    )
    await callback.answer()


async def main():
    """Main function to run the bot"""
    logger.info("🚀 Crypto Price Bot ishga tushmoqda...")
    
    # Start background task
    asyncio.create_task(background_price_checker())
    
    # Start polling
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())