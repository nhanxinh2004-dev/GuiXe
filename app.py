import eventlet
eventlet.monkey_patch()

import os
import hashlib
import time
import secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import func
from flask_socketio import SocketIO, emit, join_room
from flask_caching import Cache # [NEW]
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_key_parking_demo')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)

# --- CACHING CONFIG [NEW] ---
# Sử dụng SimpleCache cho môi trường dev/nhỏ. Với Prod, đổi sang 'RedisCache'
app.config['CACHE_TYPE'] = 'SimpleCache' 
app.config['CACHE_DEFAULT_TIMEOUT'] = 300
cache = Cache(app)

# --- DATABASE ---
db_url = os.environ.get('DATABASE_URL', 'sqlite:///parking.db')
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- MODELS ---
class User(db.Model):
    __tablename__ = 'users'
    cccd = db.Column(db.String(20), primary_key=True)
    pin_hash = db.Column(db.String(100), nullable=False)
    full_name = db.Column(db.String(100))
    address = db.Column(db.String(200))
    license_plate = db.Column(db.String(20))
    vehicle_type = db.Column(db.String(50))
    status = db.Column(db.Integer, default=0) # 0: Ngoài, 1: Trong
    current_nonce = db.Column(db.String(50), nullable=True)

    # [NEW] Indexing: Giúp query user theo CCCD cực nhanh khi dữ liệu lớn
    __table_args__ = (db.Index('idx_user_cccd', 'cccd'),)

class ParkingLog(db.Model):
    __tablename__ = 'parking_logs'
    id = db.Column(db.Integer, primary_key=True)
    cccd = db.Column(db.String(20))
    action = db.Column(db.String(10)) # IN / OUT
    timestamp = db.Column(db.DateTime(timezone=True), server_default=func.now())

    # [NEW] Indexing: Query lịch sử nhanh hơn
    __table_args__ = (db.Index('idx_log_cccd', 'cccd'),)

# --- HELPER ---
def hash_pin(pin):
    return hashlib.sha256(pin.encode()).hexdigest()

def format_license_plate(plate):
    if not plate: return {"top": "--", "bot": "--"}
    clean = ''.join(c for c in plate if c.isalnum()).upper()
    split_idx = len(clean) - 5 if len(clean) >= 9 else len(clean) - 4
    if split_idx < 0: split_idx = 0
    
    raw_top = clean[:split_idx]
    raw_bot = clean[split_idx:]
    
    top_fmt = raw_top
    if len(raw_top) >= 4 and raw_top[2].isalpha():
        top_fmt = raw_top[:2] + '-' + raw_top[2:]
        
    bot_fmt = raw_bot
    if len(raw_bot) == 5:
        bot_fmt = raw_bot[:3] + '.' + raw_bot[3:]
        
    return {"top": top_fmt, "bot": bot_fmt}

with app.app_context():
    db.create_all()

# --- ROUTES ---
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        raw_cccd = request.form['cccd']
        cccd = ''.join(filter(str.isdigit, raw_cccd))
        
        if len(cccd) != 12:
            return "Lỗi: CCCD phải đủ 12 số!", 400

        pin = request.form['pin']
        if len(pin) != 6 or not pin.isdigit():
            return "Lỗi: PIN phải là 6 chữ số!", 400

        if db.session.get(User, cccd):
            return "CCCD đã tồn tại", 400

        raw_plate = request.form['license_plate']
        clean_plate = ''.join(c for c in raw_plate if c.isalnum()).upper()

        new_user = User(
            cccd=cccd,
            pin_hash=hash_pin(pin),
            full_name=request.form['full_name'],
            address=request.form['address'],
            license_plate=clean_plate,
            vehicle_type=request.form['vehicle_type'],
            status=int(request.form['status'])
        )
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    msg = ""
    if request.args.get('timeout'):
        msg = "Hết phiên đăng nhập. Vui lòng nhập lại PIN."
        
    if request.method == 'POST':
        cccd = request.form['cccd']
        pin = request.form['pin']
        remember = request.form.get('remember') # [NEW] Checkbox Remember Me
        
        user = db.session.get(User, cccd)
        if user and user.pin_hash == hash_pin(pin):
            session['cccd'] = cccd
            session.permanent = True 
            # Nếu không tick remember, chỉnh lại lifetime ngắn hơn (VD: session tắt khi đóng browser)
            # Tuy nhiên Flask session mặc định là cookie signed, ta giữ logic permanent=True
            return redirect(url_for('dashboard'))
        else:
            msg = "Sai thông tin hoặc mã PIN!"
            
    return render_template('login.html', msg=msg)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    # Không cache trang này vì chứa User State và Nonce động
    if 'cccd' not in session: return redirect(url_for('login'))
    user = db.session.get(User, session['cccd'])
    if not user: return redirect(url_for('logout'))
    
    last_log = ParkingLog.query.filter_by(cccd=user.cccd).order_by(ParkingLog.timestamp.desc()).first()
    last_activity = "Chưa có lịch sử"
    if last_log:
        vn_time = last_log.timestamp + timedelta(hours=7)
        last_activity = vn_time.strftime("%H:%M %d/%m/%Y") + (" (Gửi)" if last_log.action=="IN" else " (Lấy)")
    
    return render_template('dashboard.html', 
                           user=user, 
                           last_activity=last_activity,
                           plate_display=format_license_plate(user.license_plate))

@app.route('/host')
def host():
    # Có thể cache template host vì nó tĩnh, data load qua API
    # @cache.cached(timeout=600) 
    return render_template('host.html')

# --- API ---
@app.route('/api/generate_qr', methods=['POST'])
def generate_qr():
    if 'cccd' not in session: return jsonify({'error': 'Auth error'}), 401
    
    user = db.session.get(User, session['cccd'])
    if not user: return jsonify({'error': 'User invalid'}), 401
    
    action = "IN" if user.status == 0 else "OUT"
    nonce = secrets.token_hex(4)
    
    user.current_nonce = nonce
    db.session.commit()
    
    expire_timestamp = int(time.time()) + 300 
    qr_data = f"{user.cccd}|{action}|{expire_timestamp}|{nonce}"
    
    return jsonify({
        'qr_data': qr_data,
        'action_name': "GỬI XE" if action == "IN" else "LẤY XE",
        'timeout': 300
    })

@app.route('/api/process_qr', methods=['POST'])
def process_qr():
    data = request.json
    try:
        parts = data.get('qr_string').split('|')
        if len(parts) != 4: return jsonify({'error': 'QR sai định dạng (cũ)'}), 400
        
        cccd, action, expire, nonce = parts[0], parts[1], int(parts[2]), parts[3]
    except:
        return jsonify({'error': 'QR lỗi dữ liệu'}), 400
        
    if int(time.time()) > expire: 
        return jsonify({'error': 'QR ĐÃ HẾT HẠN'}), 400
    
    user = db.session.get(User, cccd)
    if not user: return jsonify({'error': 'User không tồn tại'}), 404
    
    if user.current_nonce != nonce:
        return jsonify({'error': 'QR ĐÃ CŨ HOẶC ĐÃ DÙNG'}), 409
    
    if action == "IN" and user.status == 1: 
        return jsonify({'error': 'Xe đang TRONG bãi, không thể gửi lại!'}), 409
    if action == "OUT" and user.status == 0: 
        return jsonify({'error': 'Xe đang NGOÀI bãi, không thể lấy!'}), 409
    
    if data.get('confirm') == True:
        user.status = 1 if action == "IN" else 0
        user.current_nonce = None 
        
        db.session.add(ParkingLog(cccd=cccd, action=action))
        db.session.commit()
        
        socketio.emit('confirmation_success', {'status':'ok'}, to=cccd)
        return jsonify({'success': True})
    
    return jsonify({
        'cccd': user.cccd,
        'full_name': user.full_name,
        'license_plate': user.license_plate,
        'plate_display': format_license_plate(user.license_plate),
        'vehicle_type': user.vehicle_type,
        'req_action': "GỬI XE" if action == "IN" else "LẤY XE",
        'valid': True
    })

@socketio.on('join_room')
def on_join(data):
    join_room(data.get('cccd'))

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5000)