import eventlet
eventlet.monkey_patch() # BẮT BUỘC: Phải ở dòng đầu tiên

import os
import hashlib
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import func
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_key_parking_demo')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=5)

# --- CẤU HÌNH DATABASE ---
# Tự động xử lý lỗi 'postgres://' của Render thành 'postgresql://'
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
    license_plate = db.Column(db.String(20)) # Lưu dạng thô: 29A112345
    vehicle_type = db.Column(db.String(50))
    status = db.Column(db.Integer, default=0) # 0: Ngoài, 1: Trong

class ParkingLog(db.Model):
    __tablename__ = 'parking_logs'
    id = db.Column(db.Integer, primary_key=True)
    cccd = db.Column(db.String(20))
    action = db.Column(db.String(10))
    timestamp = db.Column(db.DateTime(timezone=True), server_default=func.now())

# --- HELPER FUNCTIONS ---
def hash_pin(pin):
    return hashlib.sha256(pin.encode()).hexdigest()

def format_license_plate(plate):
    """
    Format biển số để hiển thị đẹp.
    Input: "29A112345"
    Output: {"top": "29-A1", "bot": "123.45"}
    """
    if not plate: return {"top": "--", "bot": "--"}
    
    # Đảm bảo chỉ xử lý chuỗi sạch
    clean = ''.join(c for c in plate if c.isalnum()).upper()
    
    # Logic cắt chuỗi:
    # 5 số: 29A1-12345 (9 ký tự) -> Top: 29A1, Bot: 12345
    # 4 số: 29A1-1234 (8 ký tự)  -> Top: 29A1, Bot: 1234
    
    split_idx = len(clean) - 5 if len(clean) >= 9 else len(clean) - 4
    if split_idx < 0: split_idx = 0 # Fallback cho biển lạ
    
    raw_top = clean[:split_idx]
    raw_bot = clean[split_idx:]
    
    # Format Top: Thêm gạch ngang (29A1 -> 29-A1)
    top_fmt = raw_top
    if len(raw_top) >= 4 and raw_top[2].isalpha():
        top_fmt = raw_top[:2] + '-' + raw_top[2:]
        
    # Format Bot: Thêm dấu chấm nếu là 5 số (12345 -> 123.45)
    # Nếu là 4 số thì giữ nguyên (1234) theo yêu cầu
    bot_fmt = raw_bot
    if len(raw_bot) == 5:
        bot_fmt = raw_bot[:3] + '.' + raw_bot[3:]
        
    return {"top": top_fmt, "bot": bot_fmt}

with app.app_context():
    db.create_all()

# --- MIDDLEWARE ---
@app.before_request
def check_session_timeout():
    if request.endpoint in ['login', 'register', 'static', 'index', 'host']:
        return
    if 'cccd' in session:
        now = int(time.time())
        expiry = session.get('expire_at', 0)
        if now > expiry:
            session.clear()
            return redirect(url_for('login', timeout=1))

# --- ROUTES ---
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        # Sanitize input: Chỉ lấy số và chữ, bỏ hết dấu
        raw_cccd = request.form['cccd']
        cccd = ''.join(filter(str.isdigit, raw_cccd))
        
        raw_plate = request.form['license_plate']
        clean_plate = ''.join(c for c in raw_plate if c.isalnum()).upper()
        
        pin = request.form['pin']

        if db.session.get(User, cccd):
            return "CCCD đã tồn tại", 400

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
        msg = "Hết phiên đăng nhập."
    if request.method == 'POST':
        cccd = request.form['cccd']
        pin = request.form['pin']
        user = db.session.get(User, cccd)
        if user and user.pin_hash == hash_pin(pin):
            session['cccd'] = cccd
            session['expire_at'] = int(time.time()) + 300
            return redirect(url_for('dashboard'))
        else:
            msg = "Sai thông tin!"
    return render_template('login.html', msg=msg)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if 'cccd' not in session: return redirect(url_for('login'))
    user = db.session.get(User, session['cccd'])
    if not user: return redirect(url_for('logout'))
    
    remaining = session.get('expire_at', 0) - int(time.time())
    if remaining < 0: remaining = 0
    
    # Lấy lịch sử gần nhất
    last_log = ParkingLog.query.filter_by(cccd=user.cccd).order_by(ParkingLog.timestamp.desc()).first()
    last_activity = "Chưa có lịch sử"
    if last_log:
        vn_time = last_log.timestamp + timedelta(hours=7)
        last_activity = vn_time.strftime("%H:%M %d/%m/%Y") + (" (Gửi)" if last_log.action=="IN" else " (Lấy)")
    
    return render_template('dashboard.html', 
                           user=user, 
                           remaining=remaining, 
                           last_activity=last_activity,
                           plate_display=format_license_plate(user.license_plate))

@app.route('/host')
def host():
    return render_template('host.html')

# --- API ---
@app.route('/api/generate_qr', methods=['POST'])
def generate_qr():
    if 'cccd' not in session: return jsonify({'error': 'Auth error'}), 401
    
    session['expire_at'] = int(time.time()) + 300
    session.modified = True
    
    user = db.session.get(User, session['cccd'])
    action = "IN" if user.status == 0 else "OUT"
    expire = int(time.time()) + 300
    
    return jsonify({
        'qr_data': f"{user.cccd}|{action}|{expire}",
        'action_name': "GỬI XE" if action == "IN" else "LẤY XE",
        'new_timeout': 300
    })

@app.route('/api/process_qr', methods=['POST'])
def process_qr():
    data = request.json
    try:
        parts = data.get('qr_string').split('|')
        cccd, action, expire = parts[0], parts[1], int(parts[2])
    except:
        return jsonify({'error': 'QR lỗi'}), 400
        
    if int(time.time()) > expire: return jsonify({'error': 'QR hết hạn'}), 400
    
    user = db.session.get(User, cccd)
    if not user: return jsonify({'error': 'User không tồn tại'}), 404
    
    # Check trạng thái logic
    if action == "IN" and user.status == 1: return jsonify({'error': 'Xe đang TRONG bãi'}), 409
    if action == "OUT" and user.status == 0: return jsonify({'error': 'Xe đang NGOÀI bãi'}), 409
    
    if data.get('confirm') == True:
        user.status = 1 if action == "IN" else 0
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

# --- SOCKET ---
@socketio.on('join_room')
def on_join(data):
    join_room(data.get('cccd'))

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5000)
