# --- BẮT BUỘC: DÒNG NÀY PHẢI Ở TRÊN CÙNG ---
import eventlet
eventlet.monkey_patch()

import os
import hashlib
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import func
from sqlalchemy import desc
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_key_parking_demo')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=5)

# Cấu hình DB: Ưu tiên lấy từ biến môi trường (Render), nếu không có thì dùng file nội bộ
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///parking.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Cấu hình SocketIO với async_mode là eventlet
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

class ParkingLog(db.Model):
    __tablename__ = 'parking_logs'
    id = db.Column(db.Integer, primary_key=True)
    cccd = db.Column(db.String(20))
    action = db.Column(db.String(10))
    timestamp = db.Column(db.DateTime(timezone=True), server_default=func.now())

def hash_pin(pin):
    return hashlib.sha256(pin.encode()).hexdigest()

def format_license_plate(plate):
    """
    Chuyển biển số "29A112345" thành 2 dòng:
    Dòng 1: 29-A1
    Dòng 2: 123.45
    """
    if not plate: return {"top": "--", "bot": "--"}
    
    # Xóa ký tự đặc biệt, giữ lại chữ và số
    clean_plate = ''.join(c for c in plate if c.isalnum()).upper()
    
    # Logic cắt chuỗi cơ bản (dựa trên độ dài biển VN thường gặp)
    # Ví dụ: 29A112345 (9 ký tự) -> 29A1 | 12345
    # Ví dụ: 29A12345 (8 ký tự) -> 29A1 | 2345
    
    if len(clean_plate) >= 8:
        # Cắt 4 hoặc 5 số cuối làm dòng dưới
        split_index = len(clean_plate) - 5 if len(clean_plate) == 9 else len(clean_plate) - 4
        
        top = clean_plate[:split_index] # VD: 29A1
        bot = clean_plate[split_index:] # VD: 12345
        
        # Thêm dấu gạch ngang cho dòng trên: 29A1 -> 29-A1
        if len(top) >= 4 and top[2].isalpha(): 
             top = top[:2] + '-' + top[2:]
             
        # Thêm dấu chấm cho dòng dưới: 12345 -> 123.45
        if len(bot) == 5:
            bot = bot[:3] + '.' + bot[3:]
        elif len(bot) == 4:
            bot = bot[:1] + '.' + bot[1:] # Hoặc tùy format 4 số
            
        return {"top": top, "bot": bot}
    
    return {"top": clean_plate, "bot": ""}

with app.app_context():
    db.create_all()

# --- MIDDLEWARE ---
@app.before_request
def check_session_timeout():
    # Bỏ qua các route không cần check session
    if request.endpoint in ['login', 'register', 'static', 'index', 'host']:
        return
    
    if 'cccd' in session:
        now = int(time.time())
        expiry = session.get('expire_at', 0)
        if now > expiry:
            session.clear()
            return redirect(url_for('login', timeout=1))

# --- SOCKET EVENTS ---
@socketio.on('join_room')
def handle_join(data):
    room = data.get('cccd')
    if room:
        join_room(room)
        print(f"DEBUG: Client {room} đã vào phòng.")

# --- ROUTES ---
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        cccd = request.form['cccd']
        pin = request.form['pin']
        
        # SỬA LỖI LOGIC: Dùng db.session.get thay vì User.query.get
        existing_user = db.session.get(User, cccd)
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
        msg = "Phiên làm việc hết hạn. Vui lòng đăng nhập lại."

    if request.method == 'POST':
        cccd = request.form['cccd']
        pin = request.form['pin']
        
        user = db.session.get(User, cccd)
        if user and user.pin_hash == hash_pin(pin):
            session['cccd'] = cccd
            session['expire_at'] = int(time.time()) + 300 # 5 phút
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
    
    user = db.session.get(User, session['cccd'])
    if not user: return redirect(url_for('logout'))

    # 2. Tính thời gian session còn lại
    remaining = session.get('expire_at', 0) - int(time.time())
    if remaining < 0: remaining = 0
    
    # 3. Lấy Lịch sử Gửi/Lấy gần nhất
    last_log = ParkingLog.query.filter_by(cccd=user.cccd)\
                .order_by(ParkingLog.timestamp.desc())\
                .first()
    
    last_activity = "Chưa có lịch sử"
    if last_log:
        # Chuyển đổi giờ UTC sang giờ Việt Nam (+7) nếu cần thiết
        # Ở đây giả sử DB lưu UTC, ta cộng thêm 7h để hiển thị
        vn_time = last_log.timestamp + timedelta(hours=7)
        last_activity = vn_time.strftime("%H:%M %d/%m/%Y")
        if last_log.action == "IN":
            last_activity += " (Gửi xe)"
        else:
            last_activity += " (Lấy xe)"

    # 4. Format biển số để hiển thị đẹp
    plate_display = format_license_plate(user.license_plate)

    return render_template('dashboard.html', 
                           user=user, 
                           remaining=remaining, 
                           last_activity=last_activity,
                           plate_display=plate_display)

@app.route('/host')
def host():
    return render_template('host.html')

# --- API ---
@app.route('/api/generate_qr', methods=['POST'])
def generate_qr():
    if 'cccd' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Gia hạn session thêm 5 phút
    session['expire_at'] = int(time.time()) + 300
    session.modified = True
    
    user = db.session.get(User, session['cccd'])
    action = "IN" if user.status == 0 else "OUT"
    expire_qr = int(time.time()) + 300
    
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
        
    user = db.session.get(User, cccd_qr)
    if not user:
        return jsonify({'error': 'User không tồn tại'}), 404
        
    # Logic check trạng thái
    if action_qr == "IN" and user.status == 1:
        return jsonify({'error': 'Xe đang ở TRONG bãi!'}), 409
    if action_qr == "OUT" and user.status == 0:
        return jsonify({'error': 'Xe đang ở NGOÀI bãi!'}), 409
        
    # NẾU HOST BẤM XÁC NHẬN
    if data.get('confirm') == True:
        user.status = 1 if action_qr == "IN" else 0
        new_log = ParkingLog(cccd=cccd_qr, action=action_qr)
        db.session.add(new_log)
        db.session.commit()
        
        # Bắn Socket về Dashboard của User
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
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)