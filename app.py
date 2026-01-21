# --- DÒNG 1 VÀ 2 BẮT BUỘC PHẢI Ở TRÊN CÙNG ---
import eventlet
eventlet.monkey_patch() 

import os
import hashlib
import time
from datetime import datetime, timedelta
# Các import khác xuống dưới này
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import func
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_key_parking_demo')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=5)

# CẤU HÌNH DATABASE (Lấy từ biến môi trường hoặc fallback về sqlite để test local)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///parking.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# CẤU HÌNH SOCKETIO
# cors_allowed_origins="*" để chấp nhận kết nối từ mọi tên miền (Render, localhost...)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet') 

# --- ĐỊNH NGHĨA BẢNG (MODEL) ---
class User(db.Model):
    __tablename__ = 'users'
    cccd = db.Column(db.String(20), primary_key=True)
    pin_hash = db.Column(db.String(100), nullable=False)
    full_name = db.Column(db.String(100))
    address = db.Column(db.String(200))
    license_plate = db.Column(db.String(20))
    vehicle_type = db.Column(db.String(50))
    status = db.Column(db.Integer, default=0) # 0: Ngoài, 1: Trong

class ParkingLog(db.Model):
    __tablename__ = 'parking_logs'
    id = db.Column(db.Integer, primary_key=True)
    cccd = db.Column(db.String(20))
    action = db.Column(db.String(10))
    timestamp = db.Column(db.DateTime(timezone=True), server_default=func.now())

# Hàm hash pin
def hash_pin(pin):
    return hashlib.sha256(pin.encode()).hexdigest()

# Hàm tạo DB (Chạy 1 lần đầu)
with app.app_context():
    db.create_all()

# --- MIDDLEWARE CHECK SESSION ---
@app.before_request
def check_session_timeout():
    if request.endpoint in ['login', 'register', 'static', 'process_qr', 'index', 'host']:
        return
    
    if 'cccd' in session:
        now = int(time.time())
        expiry = session.get('expire_at', 0)
        if now > expiry:
            session.clear()
            return redirect(url_for('login', timeout=1))

# --- EVENT SOCKET ---
@socketio.on('join_room')
def handle_join(data):
    """
    Khi Client vào màn hình QR, họ sẽ join vào 1 "phòng" riêng
    Tên phòng chính là số CCCD của họ.
    """
    room = data.get('cccd')
    join_room(room)
    print(f"Client {room} đã tham gia room nhận thông báo.")

# --- ROUTES ---

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        cccd = request.form['cccd']
        pin = request.form['pin']
        
        # Check tồn tại
        existing_user = User.query.get(cccd)
        if existing_user:
            return "Số CCCD đã tồn tại!", 400

        new_user = User(
            cccd=cccd,
            pin_hash=hash_pin(pin),
            full_name=request.form['full_name'],
            address=request.form['address'],
            license_plate=request.form['license_plate'],
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
        msg = "Phiên đăng nhập hết hạn. Vui lòng nhập lại PIN."

    if request.method == 'POST':
        cccd = request.form['cccd']
        pin = request.form['pin']
        
        user = User.query.get(cccd)
        if user and user.pin_hash == hash_pin(pin):
            session['cccd'] = cccd
            session['expire_at'] = int(time.time()) + 300
            return redirect(url_for('dashboard'))
        else:
            msg = "Sai CCCD hoặc PIN!"

    return render_template('login.html', msg=msg)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if 'cccd' not in session: return redirect(url_for('login'))
    
    user = User.query.get(session['cccd'])
    if not user: return redirect(url_for('logout'))

    remaining = session.get('expire_at', 0) - int(time.time())
    if remaining < 0: remaining = 0
    
    return render_template('dashboard.html', user=user, remaining=remaining)

@app.route('/host')
def host():
    return render_template('host.html')

# --- API ---

@app.route('/api/generate_qr', methods=['POST'])
def generate_qr():
    if 'cccd' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Gia hạn session
    session['expire_at'] = int(time.time()) + 300
    session.modified = True
    
    user = User.query.get(session['cccd'])
    action = "IN" if user.status == 0 else "OUT"
    expire_qr = int(time.time()) + 300
    
    # QR Format: CCCD|Action|Expire
    qr_data = f"{user.cccd}|{action}|{expire_qr}"
    
    return jsonify({
        'qr_data': qr_data,
        'action_name': "GỬI XE" if action == "IN" else "LẤY XE",
        'new_timeout': 300
    })

@app.route('/api/process_qr', methods=['POST'])
def process_qr():
    data = request.json
    try:
        parts = data.get('qr_string').split('|')
        cccd_qr, action_qr, expire_time = parts[0], parts[1], int(parts[2])
    except:
        return jsonify({'error': 'QR không hợp lệ'}), 400
        
    if int(time.time()) > expire_time:
        return jsonify({'error': 'QR đã hết hạn'}), 400
        
    user = User.query.get(cccd_qr)
    if not user:
        return jsonify({'error': 'User không tồn tại'}), 404
        
    # Logic check status
    if action_qr == "IN" and user.status == 1:
        return jsonify({'error': 'Xe đang ở trong bãi!'}), 409
    if action_qr == "OUT" and user.status == 0:
        return jsonify({'error': 'Xe đang ở ngoài!'}), 409
        
    # ĐOẠN NÀY QUAN TRỌNG: Khi Host bấm Confirm
    if data.get('confirm') == True:
        # 1. Update DB
        user.status = 1 if action_qr == "IN" else 0
        new_log = ParkingLog(cccd=cccd_qr, action=action_qr)
        db.session.add(new_log)
        db.session.commit()
        
        # 2. BẮN TÍN HIỆU WEBSOCKET VỀ CLIENT
        # Gửi sự kiện 'confirmation_success' vào phòng của user đó
        socketio.emit('confirmation_success', {'status': 'ok'}, to=cccd_qr)
        
        return jsonify({'success': True})
        
    return jsonify({
        'cccd': user.cccd,
        'full_name': user.full_name,
        'license_plate': user.license_plate,
        'vehicle_type': user.vehicle_type,
        'req_action': "GỬI XE" if action_qr == "IN" else "LẤY XE",
        'valid': True
    })

if __name__ == '__main__':
    # Thay app.run() bằng socketio.run()
    socketio.run(app, debug=True, port=5000)