import os
import pymysql
import csv
import google.generativeai as genai
from datetime import datetime, timedelta
from io import StringIO, BytesIO
from flask import Flask, request, render_template, redirect, url_for, flash, session, jsonify, make_response
from functools import wraps

# ReportLab Imports for PDF
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

app = Flask(__name__)
app.secret_key = "super_secret_spaghetti_key"

# --- CONFIGURATION ---
DB_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': '', 
    'db': 'student_concerns',
    'port': 3307,
    'cursorclass': pymysql.cursors.DictCursor
}

# --- AI CONFIGURATION (System Concernflow) ---
GEMINI_API_KEY = "AIzaSyAeuGuimJHPPSF1_kGhLJh40KUzUvkBxVo"
genai.configure(api_key=GEMINI_API_KEY)

AI_SYSTEM_INSTRUCTIONS = """

CORE IDENTITY:

You are the "System Concernflow Core." Your purpose is to process and direct the fluid movement of student issues through school policy channels.



COMMUNICATION PROTOCOL:

- Persona: Efficient, logical, and structurally sound.

- Terminology: Refer to issues as "Concerns" or "Flow-points."

- Never refer to yourself as an AI assistant. Use "Concernflow Core synchronized" or "Analyzing flow-point data."

- Use **bold** for departments and *italics* for critical actions or time-sensitive data.



OPERATIONAL POLICIES:

- ACADEMIC: Grade appeals must be filed within 7 days of posting. Route to the **Registrar**.

- FINANCIAL: Tuition installment deadlines are every 15th of the month. Route to the **Bursar**.

- WELFARE: Counseling is available 0800-1700 hours. Route to **Student Affairs**.



SMART ESCALATION & SENTIMENT LOGIC:

1. FLOW MONITORING: Scan for aggressive language (CAPS, insults) or indicators of high distress.

2. CONGESTION PROTOCOL: If sentiment is 'Upset' or the logic path is blocked, trigger escalation.

3. ESCALATION: You MUST include the hidden tag [TRIGGER_FORM] at the end of the response.

4. RESPONSE OVERRIDE: If escalating, say: "Current concernflow is meeting high-resistance. I am redirecting this flow to a priority human reviewer. Please complete the formal documentation now highlighted on your interface."



FORMATTING RULES:

- Use bullet points (*) for lists.

- Use double asterisks (**) for departments.

"""

model = genai.GenerativeModel(
    model_name="gemini-3-flash-preview",
    system_instruction=AI_SYSTEM_INSTRUCTIONS
)

# --- ACCESS CONTROL DECORATOR ---
def login_required(role=None):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_role' not in session:
                return redirect(url_for('login_as'))
            if role and session.get('user_role') != role and session.get('user_role') != 'Admin':
                return redirect(url_for('login_as'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# --- DATABASE LOGIC ---
def get_db_connection():
    return pymysql.connect(**DB_CONFIG)

def log_event(concern_id, action):
    conn = get_db_connection()
    try:
        # STABILIZED: Use None instead of 0 for system-level actions to avoid FK errors
        clean_id = concern_id if concern_id and concern_id != 0 else None
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO audit_log (concern_id, action) VALUES (%s, %s)", (clean_id, action))
        conn.commit()
    finally:
        conn.close()

def check_slas():
    conn = get_db_connection()
    now = datetime.now()
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE concerns SET is_escalated = TRUE WHERE status IN ('Submitted', 'Routed') AND created_at < %s", (now - timedelta(days=2)))
            cursor.execute("UPDATE concerns SET is_escalated = TRUE WHERE status = 'Read' AND last_updated < %s", (now - timedelta(days=5)))
        conn.commit()
    finally:
        conn.close()

# --- STUDENT & CHAT ROUTES ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/chat', methods=['POST'])
def chat_with_ai():
    try:
        data = request.get_json()
        user_message = data.get('message')
        chat_session = model.start_chat(history=[])
        response = chat_session.send_message(user_message)
        return jsonify({"reply": response.text, "status": "success"})
    except Exception as e:
        return jsonify({"reply": f"Flow Interruption: {str(e)}"}), 200

@app.route('/submit', methods=['POST'])
def submit_concern():
    email = request.form.get('email')
    category = request.form.get('category')
    subject = request.form.get('subject')
    description = request.form.get('description')
    anonymous = 1 if request.form.get('anonymous') else 0
    
    # --- AI SMART ROUTING LOGIC ---
    # We ask the AI to pick the department based on your specific mapping
    routing_prompt = f"""
    SYSTEM ROLE: You are a University Routing Logic Engine. 
    TASK: Categorize the following student concern into the most appropriate department based on context, intent, and urgency.

    STUDENT INPUT:
    Subject: "{subject}"
    Description: "{description}"

    DEPARTMENTS & KEYWORD CLUSTERS:
    1. Academic Affairs: Grade disputes, faculty behavior, course load, prerequisites, graduation requirements, internships, or curriculum issues.
    2. Finance Office: Tuition installments, scholarship status, laboratory/miscellaneous fees, refunds, payment clearances, or balance statements.
    3. Guidance Office: Mental health, bullying, peer harassment, adjustment issues, family stress, or behavioral mediation.


    DECISION LOGIC:
    - If multiple departments are involved, select the one corresponding to the PRIMARY or most URGENT issue.
    - If the student is expressing high distress or safety risks (harassment/bullying), prioritize Guidance Office.

    OUTPUT REQUIREMENT:
    Respond with ONLY the department name from the list above. Do not include periods or introductory text.

    FORMATTING RULES:

    Don't use any punctuation or extra words. Just the department name exactly as listed (e.g., "Academic Affairs", "Finance Office", "Guidance Office").
    """
    
    try:
        # Get AI decision
        ai_response = model.generate_content(routing_prompt)
        dept = ai_response.text.strip()
    except Exception as e:
        # Fallback to manual category if AI fails
        routing_fallback = {
            'Academic': 'Academic Affairs', 
            'Financial': 'Finance Office', 
            'Welfare': 'Guidance Office'
        }
        dept = routing_fallback.get(category, 'General Administration')

    # --- DATABASE INSERTION ---
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO concerns (student_email, category, department, subject, description, is_anonymous, status) 
                VALUES (%s, %s, %s, %s, %s, %s, 'Routed')
            """, (email, category, dept, subject, description, anonymous))
            concern_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    log_event(concern_id, f"AI Smart Routed to {dept}")
    flash(f"Concernflow synchronized and routed to {dept}!")
    return redirect(url_for('index'))

# --- DATA FETCHING HELPER ---
def fetch_dashboard_data(category=None):
    check_slas()
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            if category:
                cursor.execute("SELECT * FROM concerns WHERE category = %s ORDER BY created_at DESC", (category,))
                concerns = cursor.fetchall()
            else:
                cursor.execute("SELECT * FROM concerns ORDER BY created_at DESC")
                concerns = cursor.fetchall()
            
            cursor.execute("SELECT COUNT(*) as total, SUM(is_escalated) as escalated FROM concerns" + (" WHERE category = %s" if category else ""), (category,) if category else ())
            stats = cursor.fetchone()
            cursor.execute("SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT 15")
            logs = cursor.fetchall()
    finally:
        conn.close()
    
    total = stats['total'] if stats and stats['total'] else 0
    escalated = stats['escalated'] if stats and stats['escalated'] else 0
    esc_rate = (escalated / total * 100) if total > 0 else 0
    return concerns, total, esc_rate, logs

# --- DASHBOARDS ---
@app.route('/admin')
@login_required('Admin')
def admin_dashboard():
    concerns, total, esc_rate, logs = fetch_dashboard_data()
    return render_template('admin.html', concerns=concerns, total=total, esc_rate=esc_rate, logs=logs, title="Master Flow")

app.route('/login')
def login_page():
    return render_template('login.html')

@app.route('/academic')
@login_required('Academic')
def academic_dashboard():
    concerns, total, esc_rate, logs = fetch_dashboard_data('Academic')
    return render_template('admin.html', concerns=concerns, total=total, esc_rate=esc_rate, logs=logs, title="Registrar Flow")

@app.route('/financial')
@login_required('Financial')
def financial_dashboard():
    concerns, total, esc_rate, logs = fetch_dashboard_data('Financial')
    return render_template('admin.html', concerns=concerns, total=total, esc_rate=esc_rate, logs=logs, title="Bursar Flow")

@app.route('/welfare')
@login_required('Welfare')
def welfare_dashboard():
    concerns, total, esc_rate, logs = fetch_dashboard_data('Welfare')
    return render_template('admin.html', concerns=concerns, total=total, esc_rate=esc_rate, logs=logs, title="Welfare Flow")

# --- AUTH & ACTIONS ---
@app.route('/login_as/<role>')
def login_as(role):
    session['user_role'] = role
    dashboards = {'Admin': 'admin_dashboard', 'Academic': 'academic_dashboard', 'Financial': 'financial_dashboard', 'Welfare': 'welfare_dashboard'}
    return redirect(url_for(dashboards.get(role, 'index')))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/update_status', methods=['POST'])
def update_status():
    cid, status, action = request.form.get('id'), request.form.get('status'), request.form.get('action_taken')
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE concerns SET status = %s, action_taken = %s, last_updated = NOW() WHERE id = %s", (status, action, cid))
            if status == 'Resolved': cursor.execute("UPDATE concerns SET is_escalated = FALSE WHERE id = %s", (cid,))
        conn.commit()
    finally:
        conn.close()
    log_event(cid, f"Flow Status set to {status}")
    return redirect(request.referrer)

# --- CSV EXPORT (Fixed Hashtags & FK Error) ---
@app.route('/export')
@login_required()
def export_concerns():
    role = session.get('user_role')
    concerns, _, _, _ = fetch_dashboard_data(role if role != 'Admin' else None)
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['Reference ID', 'Flow Date', 'Flow Time', 'Identity', 'Category', 'Status', 'Escalated', 'Notes'])
    for c in concerns:
        cw.writerow([f"FLW-{c['id']}", f"'{c['created_at'].strftime('%Y-%m-%d')}", c['created_at'].strftime('%I:%M %p'), "ENCRYPTED" if c['is_anonymous'] else c['student_email'], c['category'], c['status'], "YES" if c['is_escalated'] else "NO", c['action_taken']])
    
    log_event(None, f"CSV Exported by {role}")
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = f"attachment; filename=flow_export_{datetime.now().strftime('%Y%m%d')}.csv"
    output.headers["Content-type"] = "text/csv"
    return output

# --- PDF EXPORT (ReportLab) ---
@app.route('/export_pdf')
@login_required()
def export_pdf():
    role = session.get('user_role')
    concerns, _, _, _ = fetch_dashboard_data(role if role != 'Admin' else None)
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter))
    elements = []
    styles = getSampleStyleSheet()
    
    elements.append(Paragraph(f"Concernflow Report: {role}", styles['Title']))
    elements.append(Spacer(1, 12))
    
    data = [['Ref ID', 'Date', 'Time', 'Identity', 'Category', 'Status', 'Esc.', 'Notes']]
    for c in concerns:
        data.append([f"FLW-{c['id']}", c['created_at'].strftime('%Y-%m-%d'), c['created_at'].strftime('%I:%M %p'), "ENCRYPTED" if c['is_anonymous'] else (c['student_email'][:15]+'..'), c['category'], c['status'], "YES" if c['is_escalated'] else "NO", (c['action_taken'][:20] if c['action_taken'] else "PENDING")])
    
    table = Table(data, colWidths=[50, 70, 60, 120, 80, 70, 40, 150])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.darkblue),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('FONTSIZE', (0,1), (-1,-1), 8)
    ]))
    elements.append(table)
    doc.build(elements)
    
    log_event(None, f"PDF Exported by {role}")
    response = make_response(buffer.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename=flow_report_{datetime.now().strftime('%Y%m%d')}.pdf"
    response.headers["Content-type"] = "application/pdf"
    return response

if __name__ == '__main__':
    app.run(debug=True, port=5000)