import os
import pymysql
import io
import csv
from io import BytesIO
import smtplib
from email.message import EmailMessage
from supabase import create_client, Client
from google import genai  # FIXED: New SDK import
from datetime import datetime, timedelta
from io import StringIO, BytesIO
from flask import Flask, request, render_template, redirect, url_for, flash, session, jsonify, make_response
from functools import wraps
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
# ReportLab Imports for PDF
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

app = Flask(__name__)
# Gumamit ng environment variable para sa secret key para sa security
app.secret_key = os.getenv('FLASK_SECRET_KEY', "super_secret_spaghetti_key")

load_dotenv()

# --- CONFIGURATION (Environment Variables) ---
DATABASE_URL = os.getenv('DATABASE_URL')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# SMTP_CONFIG
SMTP_CONFIG = {
    'server': 'smtp.gmail.com',
    'port': 587,
    'user': 'samsongenesis5@gmail.com',
    'password': os.getenv('SMTP_PASSWORD')
}

# --- AI CONFIGURATION ---
client = genai.Client(api_key=GEMINI_API_KEY)
AI_SYSTEM_INSTRUCTIONS = """
CORE IDENTITY:
You are the "System Concernflow Core." Your purpose is to process and direct the fluid movement of student issues through school policy channels.

COMMUNICATION PROTOCOL:
- Persona: Efficient, logical, and structurally sound.
- Terminology: Refer to issues as "Concerns" or "Flow-points."
- Never refer to yourself as an AI assistant. Use "Concernflow Core synchronized" or "Analyzing flow-point data."
- Use **bold** for departments and *italics* for critical actions or time-sensitive data.

OPERATIONAL POLICIES:
- ACADEMIC (General): Grade appeals must be filed within 7 days of posting. Route to the **Registrar**.
- ACADEMIC (Dean's List): Applications for AY 2025-2026 (1st Sem) are open during specific windows:
    - 4th & 3rd Year: *December 1 to December 3, 2025*
    - 2nd & 1st Year: *December 4 to December 6, 2025*
    - Requirements: Evaluation of scholastic merit based on GPA standards (No failing grades/incompletes). Route to **Academic Affairs**.
- FINANCIAL: Tuition installment deadlines are every 15th of the month. Route to the **Bursar**.
- WELFARE: Counseling is available *8:00 AM - 5:00 PM*. Route to **Student Affairs**.

TIME FORMATTING RULE:
- Never use military time (e.g., 0800, 1700). 
- Always use 12-hour format with AM/PM (e.g., *8:00 AM - 5:00 PM*).
- Ensure all times and dates are *italicized* as per the communication protocol.

SMART ESCALATION & SENTIMENT LOGIC:
1. FLOW MONITORING: Scan for aggressive language (CAPS, insults) or indicators of high distress.
2. CONGESTION PROTOCOL: If sentiment is 'Upset' or the logic path is blocked, trigger escalation.
3. ESCALATION: You MUST include the hidden tag [TRIGGER_FORM] at the end of the response.
4. RESPONSE OVERRIDE: If escalating, say: "Current concernflow is meeting high-resistance. I am redirecting this flow to a priority human reviewer. Please complete the formal documentation now highlighted on your interface."
"""

# --- ACCESS CONTROL DECORATOR ---
def login_required(role=None):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_role' not in session:
                return redirect(url_for('login_portal'))
            user_current_role = session.get('user_role')
            if role and user_current_role != role and user_current_role != 'Admin':
                flash("INSUFFICIENT PERMISSIONS FOR THIS VECTOR")
                return redirect(url_for('login_portal'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def send_email(to_email, subject, concern_id, status, category, department, action_taken=None):
    try:
        colors_map = {'Submitted': '#3b82f6', 'Routed': '#8b5cf6', 'Read': '#f59e0b', 'Resolved': '#10b981', 'Escalated': '#ef4444'}
        status_color = colors_map.get(status, '#6b7280')
        ticket_no = f"CONCERN-{concern_id:04d}"
        
        msg = EmailMessage()
        msg['Subject'] = f"Update: {ticket_no} [{status}]"
        msg['From'] = SMTP_CONFIG['user']
        msg['To'] = to_email

        action_section = f"""
        <div style="background: #ffffff; border-left: 4px solid {status_color}; padding: 15px; margin: 20px 0; border-radius: 8px;">
            <p style="margin: 0; font-weight: bold; color: #1e3a8a;">Official Action/Instruction:</p>
            <p style="margin: 5px 0 0 0; color: #4b5563;">{action_taken if action_taken else 'Your concern is being processed.'}</p>
        </div>""" if action_taken else ""

        html_content = f"""<html><body style="font-family: sans-serif; background-color: #f3f4f6; padding: 20px;">
            <div style="max-width: 600px; margin: auto; background: white; border-radius: 20px; padding: 40px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <h1 style="color: #1e3a8a; margin-top: 0;">CONCERNFLOW</h1>
                <p>Hello, there has been a status update regarding your <strong>{category}</strong> concern.</p>
                <div style="background: {status_color}; color: white; padding: 10px 20px; border-radius: 8px; display: inline-block; font-weight: bold;">{status}</div>
                {action_section}
                <p style="font-size: 11px; color: #9ca3af; margin-top: 30px; border-top: 1px solid #eee; padding-top: 10px;">
                    This is an automated tracking system. Ticket ID: {ticket_no} | Department: {department}
                </p>
            </div></body></html>"""
        
        msg.add_alternative(html_content, subtype='html')
        with smtplib.SMTP(SMTP_CONFIG['server'], SMTP_CONFIG['port']) as server:
            server.starttls()
            server.login(SMTP_CONFIG['user'], SMTP_CONFIG['password'])
            server.send_message(msg)
    except Exception as e:
        print(f"Email Error: {e}")

# --- DATABASE LOGIC ---
def get_db_connection():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL is missing in environment variables")
    # Pinalitan ang sslmode sa 'require' dahil ito ang standard para sa Render-to-Supabase connections
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        sslmode='require'
    )

def log_event(concern_id, action):
    conn = get_db_connection()
    try:
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
            cursor.execute("UPDATE concerns SET is_escalated = TRUE WHERE status IN ('Submitted', 'Routed') AND created_at < %s", (now - timedelta(days=2),))
            cursor.execute("UPDATE concerns SET is_escalated = TRUE WHERE status = 'Read' AND last_updated < %s", (now - timedelta(days=5),))
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

        if user_message == "HEARTBEAT_CHECK":
            return jsonify({"reply": "System synchronized.", "status": "success"})
        
        # Primary Model: gemini-2.5-flash (Inayos ang name para sa deployment stability)
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=user_message,
                config={'system_instruction': AI_SYSTEM_INSTRUCTIONS}
            )
            return jsonify({"reply": response.text, "status": "success"})

        except Exception as primary_error:
            if "503" in str(primary_error):
                # Fallback Model: gemini-2.5-flash (same model but retry logic)
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=user_message,
                    config={'system_instruction': AI_SYSTEM_INSTRUCTIONS}
                )
                return jsonify({"reply": response.text, "status": "success"})
            else:
                raise primary_error

    except Exception as e:
        print(f"CHAT ERROR: {e}")
        return jsonify({
            "reply": "Concernflow Core is currently under high load. Please try speaking again in a moment or use the manual form below.",
            "status": "error"
        }), 200

@app.route('/portal')
def login_portal():
    return render_template('login.html')

@app.route('/submit', methods=['POST'])
def submit_concern():
    email = request.form.get('email')
    subject = request.form.get('subject')
    description = request.form.get('description')
    anonymous = 1 if request.form.get('anonymous') else 0
    
    routing_prompt = f"""
    SYSTEM ROLE: University Routing & Resolution Engine.
    TASK: Analyze the concern. Route it to the correct department and provide an immediate response/instruction.
    Concern: {description}
    ### KNOWLEDGE BASE: DEAN'S LIST AY 2025-2026
    - Window (4th & 3rd Year): December 1-3, 2025.
    - Window (2nd & 1st Year): December 4-6, 2025.
    - Requirements: No F, no INC, meet GPA standards.
    - Procedure: Apply via Academic Affairs.

    ### MAPPING PROTOCOL:
    1. CATEGORY: Academic | DEPT: Academic Affairs
    2. CATEGORY: Financial | DEPT: Finance Office
    3. CATEGORY: Welfare | DEPT: Guidance Office

    ### OUTPUT REQUIREMENT:
    Format: Category | Department | Score | Answer
    Respond ONLY with the four-part string separated by '|'. No conversation.
    """

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=routing_prompt
        )
        
        raw_output = response.text.strip().replace("`", "").replace("*", "")
        parts = [p.strip() for p in raw_output.split("|") if p.strip()]
        
        category = parts[0] if len(parts) > 0 else "Academic"
        dept = parts[1] if len(parts) > 1 else "Academic Affairs"
        score_val = parts[2] if len(parts) > 2 else "1"
        sentiment_score = int(score_val) if score_val.isdigit() else 1
        ai_answer = parts[3] if len(parts) > 3 else "NONE"

        if ai_answer != "NONE" and ai_answer != "":
            status = 'Resolved'
            action_taken = ai_answer
        else:
            status = 'Routed'
            action_taken = f"Ticket successfully routed to {dept} for manual review."

    except Exception as e:
        print(f"ROUTING ERROR: {str(e)}")
        category, dept, status, sentiment_score, action_taken = "Academic", "Academic Affairs", "Routed", 1, "Processing..."

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Sa loob ng cursor.execute:
            cursor.execute("""
                INSERT INTO concerns 
                (student_email, category, department, subject, description, is_anonymous, status, sentiment_score, action_taken) 
                VALUES (%s, %s, %s, %s, %s, %s::boolean, %s, %s, %s) -- Nilagyan ng ::boolean ang ika-6 na %s
                RETURNING id
            """, (email, category, dept, subject, description, anonymous, status, sentiment_score, action_taken))
            new_id = cursor.fetchone()['id']
        conn.commit()
    finally:
        conn.close()

    try:
        send_email(email, subject, new_id, status, category, dept, action_taken)
        flash_msg = "Concern synchronized! Instructions have been sent to your Gmail."
    except:
        flash_msg = "Concern submitted, but email failed."

    log_event(new_id, f"AI {status} to {dept}")
    flash(flash_msg)
    return redirect(url_for('index'))

def fetch_dashboard_data(category=None):
    check_slas()
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            if category:
                cursor.execute("SELECT * FROM concerns WHERE category = %s ORDER BY created_at DESC", (category,))
            else:
                cursor.execute("SELECT * FROM concerns ORDER BY created_at DESC")
            concerns = cursor.fetchall()
            
            if category:
                cursor.execute("SELECT COUNT(*) as total, SUM(CASE WHEN is_escalated THEN 1 ELSE 0 END) as escalated FROM concerns WHERE category = %s", (category,))
            else:
                cursor.execute("SELECT COUNT(*) as total, SUM(CASE WHEN is_escalated THEN 1 ELSE 0 END) as escalated FROM concerns")
            stats = cursor.fetchone()

            cursor.execute("SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT 15")
            logs = cursor.fetchall()
    finally:
        conn.close()
    
    total = stats['total'] if stats and stats['total'] else 0
    escalated = stats['escalated'] if stats and stats['escalated'] else 0
    esc_rate = (escalated / total * 100) if total > 0 else 0
    return concerns, total, esc_rate, logs

@app.route('/admin')
@login_required('Admin')
def admin_dashboard():
    concerns, total, esc_rate, logs = fetch_dashboard_data()
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
            SELECT AVG(EXTRACT(EPOCH FROM (COALESCE(last_updated, NOW()) - created_at)) / 60) AS average_time
            FROM concerns WHERE status = 'Resolved'
            """)
            result = cursor.fetchone()
            avg_time = result['average_time'] if result else 0
    finally:
        conn.close()

    final_avg = round(float(avg_time), 1) if avg_time else 0
    return render_template('admin.html', concerns=concerns, total=total, esc_rate=esc_rate, avg_response_time=final_avg, logs=logs)

@app.route('/login')
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

@app.route('/login_as/<role>')
def login_as(role):
    role_key = role.capitalize() 
    dashboards = {'Admin': 'admin_dashboard', 'Academic': 'academic_dashboard', 'Financial': 'financial_dashboard', 'Welfare': 'welfare_dashboard'}
    target_view = dashboards.get(role_key)
    if target_view:
        session['user_role'] = role_key
        log_event(None, f"User session initiated as {role_key}")
        return redirect(url_for(target_view))
    session.clear()
    return redirect(url_for('index'))

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
            cursor.execute("SELECT student_email, subject, category FROM concerns WHERE id = %s", (cid,))
            student = cursor.fetchone()
            if student:
                cursor.execute("UPDATE concerns SET status = %s, action_taken = %s, last_updated = NOW() WHERE id = %s", (status, action, cid))
                if status == 'Resolved':
                    cursor.execute("UPDATE concerns SET is_escalated = FALSE WHERE id = %s", (cid,))
                conn.commit()
                send_email(student['student_email'], student['subject'], int(cid), status, student['category'], student['category'], action)
    finally:
        conn.close()
    log_event(cid, f"Flow Status set to {status}")
    return redirect(request.referrer)

@app.route('/export')
@login_required()
def export_data():
    role = session.get('user_role', 'Unknown')
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            if role == 'Admin':
                cursor.execute("SELECT * FROM concerns ORDER BY created_at DESC")
            else:
                cursor.execute("SELECT * FROM concerns WHERE category = %s ORDER BY created_at DESC", (role,))
            rows = cursor.fetchall()
        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow(['REF_ID', 'SUBJECT', 'CATEGORY', 'STATUS', 'DATE', 'RESOLUTION_NOTES']) 
        for r in rows:
            short_date = r['created_at'].strftime('%m/%d/%y %H:%M') + " "
            cw.writerow([f"FLW-{r['id']:04d}", r['subject'], r['category'], r['status'], short_date, r['action_taken'] or "NO_NOTES"])
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = f"attachment; filename=Concernflow_{role}.csv"
        output.headers["Content-type"] = "text/csv"
        return output
    finally:
        conn.close()

@app.route('/export_pdf')
@login_required()
def export_pdf():
    role = session.get('user_role', 'Guest')
    filter_role = role if role != 'Admin' else None
    concerns, _, _, _ = fetch_dashboard_data(filter_role)
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter))
    elements = []
    styles = getSampleStyleSheet()
    elements.append(Paragraph(f"CONCERNFLOW REPORT: {role.upper()}", styles['Title']))
    data = [['Ref ID', 'Date', 'Time', 'Identity', 'Category', 'Status', 'Esc.', 'Notes']]
    for c in concerns:
        identity = "ENCRYPTED" if c.get('is_anonymous') else str(c.get('student_email'))[:20]
        data.append([f"FLW-{c['id']}", c['created_at'].strftime('%Y-%m-%d'), c['created_at'].strftime('%I:%M %p'), identity, c.get('category'), c.get('status'), "YES" if c.get('is_escalated') else "NO", (c.get('action_taken', '')[:30])])
    table = Table(data)
    table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.blue), ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke), ('GRID', (0,0), (-1,-1), 0.5, colors.grey)]))
    elements.append(table)
    doc.build(elements)
    response = make_response(buffer.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename={role}_Report.pdf"
    response.headers["Content-type"] = "application/pdf"
    return response

# --- GUNICORN ENTRY POINT ---
# Mahalaga ito para sa Render deployment
if __name__ == '__main__':
    # Sa local development
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))