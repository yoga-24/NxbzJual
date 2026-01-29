import os
import io
import uuid
import time
import requests
import qrcode
import asyncio
from datetime import datetime
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from pymongo import MongoClient

# ================= KONFIGURASI =================
# Variable ini DIAMBIL dari Setting Vercel (Environment Variables)
TOKEN = os.environ.get("BOT_TOKEN") 
MONGO_URI = os.environ.get("MONGO_URI") 
ADMIN_ID = os.environ.get("ADMIN_ID") # ID Telegram Admin (Angka)

# Config Manual (Bisa diedit disini)
SAWERIA_USERNAME = 'naila9991' 
VIP_CHANNEL_LINK = 'https://t.me/+Ny2sNFWsyUM5Mjc1'
BANNER_IMAGE = 'https://i.ibb.co.com/mC2ZhzND/1769357453324.jpg'

app = Flask(__name__)

# --- DATABASE CONNECTION (Cached) ---
client = None
db = None

def get_db():
    global client, db
    if not client:
        # Koneksi ke MongoDB Atlas
        client = MongoClient(MONGO_URI)
        db = client['vip_bot_db']
    return db

# --- HELPER SAWERIA & UTIL ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/110.0.0.0",
    "Origin": "https://saweria.co",
    "Referer": f"https://saweria.co/{SAWERIA_USERNAME}"
}

def get_saweria_user_id():
    try:
        url = f"https://saweria.co/{SAWERIA_USERNAME}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        import re
        match = re.search(r'"id":"([0-9a-fA-F-]{36})"', resp.text)
        return match.group(1) if match else None
    except: return None

def create_saweria_qris(amount):
    user_id = get_saweria_user_id()
    if not user_id: return None, None
    
    url = f"https://backend.saweria.co/donations/{user_id}"
    payload = {
        "agree": True, "amount": amount, "currency": "IDR", "message": "VIP Access", 
        "payment_type": "qris", "vote": "",
        "customer_info": {"first_name": "User", "email": "user@vip.com", "phone": "08123456789"}
    }
    try:
        resp = requests.post(url, json=payload, headers=HEADERS, timeout=10)
        data = resp.json().get('data', {})
        return data.get('qr_string'), data.get('id')
    except: return None, None

def check_receipt_status(saweria_id):
    if not saweria_id: return False
    url = f"https://saweria.co/receipt/snap/{saweria_id}?t={int(time.time())}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        # Cek tanda sukses dari response HTML Saweria
        success_keys = ['"verified":true', '"status":"finished"', '"transaction_status":"settlement"']
        if any(k in resp.text for k in success_keys):
            return True
    except: pass
    return False

# --- BOT LOGIC ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    database = get_db()
    
    # Simpan User ke DB (Upsert) untuk keperluan Broadcast
    try:
        database.users.update_one(
            {'user_id': user.id}, 
            {'$set': {'full_name': user.full_name, 'username': user.username, 'last_active': datetime.now()}}, 
            upsert=True
        )
    except: pass

    caption = (
        f"Halo, <b>{user.first_name}</b> 👋\n\n"
        "Selamat datang di <b>Layanan VIP Otomatis</b>.\n"
        "Data & Transaksi aman tersimpan di Database Cloud.\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 BELI VIP", callback_data='list_vip')],
        [InlineKeyboardButton("❓ Bantuan", callback_data='help')]
    ])
    
    try: await update.message.reply_photo(BANNER_IMAGE, caption=caption, parse_mode='HTML', reply_markup=kb)
    except: await update.message.reply_text(caption, parse_mode='HTML', reply_markup=kb)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Security: Hanya Admin yang bisa broadcast
    sender_id = str(update.effective_user.id)
    if sender_id != str(ADMIN_ID): 
        return await update.message.reply_text("⛔ Kamu bukan Admin.")

    pesan = ' '.join(context.args)
    if not pesan: return await update.message.reply_text("⚠️ Format: /bc <pesan>")

    database = get_db()
    users = list(database.users.find({}, {'user_id': 1})) # Ambil ID saja biar ringan
    
    await update.message.reply_text(f"🚀 Mengirim pesan ke {len(users)} user...")
    
    sukses = 0
    for u in users:
        try:
            await context.bot.send_message(u['user_id'], pesan, parse_mode='HTML')
            sukses += 1
            await asyncio.sleep(0.05) # Delay anti-spam
        except: pass # User ngeblok bot / akun mati
        
    await update.message.reply_text(f"✅ Broadcast Selesai. Terkirim ke {sukses} user.")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    database = get_db()

    if query.data == 'list_vip':
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 VIP PREMIUM (Rp 30k)", callback_data='buy_30000')],
            [InlineKeyboardButton("🧪 UJI COBA (Rp 1k)", callback_data='buy_1000')]
        ])
        await query.edit_message_caption("📦 <b>PILIH PAKET:</b>", parse_mode='HTML', reply_markup=kb)

    elif query.data.startswith('buy_'):
        amount = int(query.data.split('_')[1])
        qr_raw, s_id = create_saweria_qris(amount)
        
        if not qr_raw: return await query.edit_message_caption("⚠️ Gagal membuat QRIS. Coba lagi.")

        # Simpan Transaksi PENDING ke MongoDB
        order_uuid = str(uuid.uuid4())[:8]
        database.orders.insert_one({
            'order_id': order_uuid, 'user_id': query.from_user.id,
            'amount': amount, 'saweria_id': s_id, 'status': 'PENDING',
            'created_at': datetime.now()
        })

        img = qrcode.make(qr_raw)
        bio = io.BytesIO(); img.save(bio, 'PNG'); bio.seek(0)
        
        caption = f"🧾 <b>TAGIHAN #{order_uuid}</b>\nNominal: Rp {amount:,}\n\n1. Scan QRIS.\n2. Klik Cek Status."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 CEK STATUS", callback_data=f"check_{s_id}")]])
        
        await query.message.delete()
        await context.bot.send_photo(query.message.chat_id, bio, caption=caption, parse_mode='HTML', reply_markup=kb)

    elif query.data.startswith('check_'):
        s_id = query.data.split('_')[1]
        order = database.orders.find_one({'saweria_id': s_id})

        if order and order.get('status') == 'PAID':
            return await query.edit_message_caption("✅ <b>SUDAH LUNAS!</b>", parse_mode='HTML')

        # Cek Real-time ke Saweria
        if check_receipt_status(s_id):
            database.orders.update_one({'saweria_id': s_id}, {'$set': {'status': 'PAID'}})
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔓 BUKA CHANNEL VIP", url=VIP_CHANNEL_LINK)]])
            await query.edit_message_caption("✅ <b>PEMBAYARAN SUKSES!</b>\nSilakan masuk:", parse_mode='HTML', reply_markup=kb)
        else:
            await context.bot.answer_callback_query(query.id, "❌ Belum masuk. Coba refresh.", show_alert=True)

# --- SERVERLESS ENTRY POINT ---
ptb_app = Application.builder().token(TOKEN).build()
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("bc", broadcast))
ptb_app.add_handler(CallbackQueryHandler(callback_handler))

@app.route('/api/index', methods=['POST', 'GET'])
async def main():
    # A. HANDLER CRON JOB (Jalan Tiap 10 Menit)
    if request.args.get('mode') == 'cron_check':
        try:
            db = get_db()
            pending_orders = list(db.orders.find({'status': 'PENDING'}))
            bot = Bot(token=TOKEN)
            verified = 0
            
            for order in pending_orders:
                if check_receipt_status(order['saweria_id']):
                    db.orders.update_one({'_id': order['_id']}, {'$set': {'status': 'PAID'}})
                    # Kirim notif ke user kalau lunas
                    try:
                        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔓 AKSES VIP", url=VIP_CHANNEL_LINK)]])
                        await bot.send_message(order['user_id'], "✅ <b>PEMBAYARAN DITERIMA (AUTO)</b>", parse_mode='HTML', reply_markup=kb)
                        verified += 1
                    except: pass
            return jsonify({"status": "cron_ok", "verified": verified})
        except Exception as e: return jsonify({"error": str(e)})

    # B. HANDLER WEBHOOK TELEGRAM
    if request.method == 'POST':
        try:
            update = Update.de_json(request.get_json(force=True), ptb_app.bot)
            await ptb_app.initialize()
            await ptb_app.process_update(update)
            return "OK"
        except: return "Error", 500
        
    return "Bot is Running with MongoDB!"
