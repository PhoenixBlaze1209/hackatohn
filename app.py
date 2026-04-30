import os
import pymysql
import io
import csv
from io import BytesIO
import smtplib
from email.message import EmailMessage
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
app.secret_key = "super_secret_spaghetti_key"

load_dotenv()

# --- CONFIGURATION (Environment Variables) ---
DATABASE_URL = os.getenv('DATABASE_URL')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# Eto yung required mong SMTP_CONFIG
SMTP_CONFIG = {
    'server': 'smtp.gmail.com',
    'port': 587,
    'user': 'samsongenesis5@gmail.com',
    'password': os.getenv('SMTP_PASSWORD') # Kukunin nito yung 'jzdy...' sa Render
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
        colors = {'Submitted': '#3b82f6', 'Routed': '#8b5cf6', 'Read': '#f59e0b', 'Resolved': '#10b981', 'Escalated': '#ef4444'}
        status_color = colors.get(status, '#6b7280')
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
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        sslmode='require'  # 🔥 REQUIRED FOR RENDER + SUPABASE
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

        # --- 1. HEARTBEAT BYPASS ---
        # This keeps your UI 'Green' without calling the AI API
        if user_message == "HEARTBEAT_CHECK":
            return jsonify({"reply": "System synchronized.", "status": "success"})
        
        # --- 2. PRIMARY ATTEMPT (Gemini 2.5 Flash) ---
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=user_message,
                config={'system_instruction': AI_SYSTEM_INSTRUCTIONS}
            )
            return jsonify({"reply": response.text, "status": "success"})

        except Exception as primary_error:
            # Check if the error is a 503 (Service Unavailable/High Demand)
            if "503" in str(primary_error):
                print("Primary Model Busy. Switching to Fallback...")
                
                # --- 3. FALLBACK ATTEMPT (Gemini 2.0 Flash) ---
                # 2.0 is often more stable during 2.5/3.0 demand spikes
                response = client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=user_message,
                    config={'system_instruction': AI_SYSTEM_INSTRUCTIONS}
                )
                return jsonify({"reply": response.text, "status": "success"})
            else:
                # Re-raise if it's not a demand issue (e.g., auth error)
                raise primary_error

    except Exception as e:
        print(f"CHAT ERROR: {e}")
        # Return a 200 so the frontend can display the error gracefully in the chat bubble
        return jsonify({
            "reply": "Concernflow Core is currently under high load. Please try speaking again in a moment or use the manual form below.",
            "status": "error"
        }), 200

@app.route('/portal')
def login_portal():
    return render_template('login.html') # This is your page with the 4 buttons

@app.route('/submit', methods=['POST'])
def submit_concern():
    # 1. Capture Form Data
    email = request.form.get('email')
    subject = request.form.get('subject')
    description = request.form.get('description')
    anonymous = 1 if request.form.get('anonymous') else 0
    
    # 2. AI Prompt
    routing_prompt = f"""
    SYSTEM ROLE: University Routing & Resolution Engine.
    TASK: Analyze the concern. Route it to the correct department and provide an immediate response/instruction.

    ### KNOWLEDGE BASE: DEAN'S LIST AY 2025-2026
    - Window (4th & 3rd Year): December 1-3, 2025.
    - Window (2nd & 1st Year): December 4-6, 2025.
    - Requirements: No F, no INC, meet GPA standards.
    - Procedure: Apply via Academic Affairs.

    ### MAPPING PROTOCOL:
    1. CATEGORY: Academic | DEPT: Academic Affairs
       - KEYWORDS: Instruction, Pedagogy, Syllabus, Grades, GPA, Enrollment, Registrar, Transcript, Accreditation, Probation.
       - DEFAULT INSTRUCTION: Please coordinate with Academic Affairs for evaluation and processing.
    
    2. CATEGORY: Financial | DEPT: Finance Office
       - KEYWORDS: Revenue, Tuition, Fees, Payroll, Billing, Ledger, Scholarship, Grant, Bursary, Refund.
       - DEFAULT INSTRUCTION: Please visit the Finance Office for billing inquiries or payment settlement.
    
    3. CATEGORY: Welfare | DEPT: Guidance Office
       - KEYWORDS: Counseling, Wellbeing, Mental Health, Bullying, Safety, Stress, Harassment, Clinic, Inclusion.
       - DEFAULT INSTRUCTION: Please proceed to the Guidance Office for support and professional assistance.

    ### OUTPUT REQUIREMENT:
    Format: Category | Department | Score | Answer
    - Score: 1-5 (Urgency).
    - Answer: 
      A. If concern is about DEAN'S LIST: Provide dates, requirements, and procedure.
      B. If concern matches any other CATEGORY: Provide the specific DEFAULT INSTRUCTION for that department.
    
    Respond ONLY with the four-part string separated by '|'. No conversation.
    """

    try:
        response = client.models.generate_content(
            model="gemini-1.5-flash", # Use 1.5-flash for stability
            contents=routing_prompt
        )
        
        raw_output = response.text.strip().replace("`", "").replace("*", "")
        parts = [p.strip() for p in raw_output.split("|") if p.strip()]
        
        category = parts[0] if len(parts) > 0 else "Academic"
        dept = parts[1] if len(parts) > 1 else "Academic Affairs"
        score_val = parts[2] if len(parts) > 2 else "1"
        sentiment_score = int(score_val) if score_val.isdigit() else 1
        ai_answer = parts[3] if len(parts) > 3 else "NONE"

        # Determine Flow State
        # Kung may AI Answer (Instructions), mark as Resolved. 
        # Kung fail or plain routing, keep as Routed.
        if ai_answer != "NONE" and ai_answer != "":
            status = 'Resolved'
            action_taken = ai_answer
        else:
            status = 'Routed'
            action_taken = f"Ticket successfully routed to {dept} for manual review."

    except Exception as e:
        print(f"ROUTING ERROR: {str(e)}")
        category, dept, status, sentiment_score, is_escalated, action_taken = "Academic", "Academic Affairs", "Routed", 1, 0, "Processing..."

    # --- DATABASE INSERTION ---
    # FIX: Use RETURNING id instead of cursor.lastrowid (PostgreSQL syntax)
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO concerns 
                (student_email, category, department, subject, description, is_anonymous, status, sentiment_score, action_taken) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (email, category, dept, subject, description, anonymous, status, sentiment_score, action_taken))
            new_id = cursor.fetchone()['id']
        conn.commit()
    finally:
        conn.close()

    # --- EMAIL TRIGGER ---
    try:
        send_email(
            to_email=email,
            subject=subject,
            concern_id=new_id,
            status=status,
            category=category,
            department=dept,
            action_taken=action_taken # Ito na yung instructions na galing sa AI
        )
        flash_msg = "Concern synchronized! Instructions have been sent to your Gmail."
    except Exception as e:
        flash_msg = "Concern submitted, but email failed."

    log_event(new_id, f"AI {status} to {dept}")
    flash(flash_msg)
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
            
            # FIX: Separated queries to avoid broken conditional string concatenation
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

# --- DASHBOARDS ---
@app.route('/admin')
@login_required('Admin')
def admin_dashboard():
    concerns, total, esc_rate, logs = fetch_dashboard_data()
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # FIX: Use PostgreSQL syntax instead of TIMESTAMPDIFF (MySQL only)
            cursor.execute("""
            SELECT AVG(EXTRACT(EPOCH FROM (COALESCE(last_updated, NOW()) - created_at)) / 60) AS average_time
            FROM concerns WHERE status = 'Resolved'
            """)
            result = cursor.fetchone()
            avg_time = result['average_time'] if result else None
    finally:
        conn.close()

    # Siguraduhin na hindi None ang ipapasa sa round()
    final_avg = round(float(avg_time), 1) if avg_time is not None else 0
        
    return render_template('admin.html', 
                           concerns=concerns, 
                           total=total, 
                           esc_rate=esc_rate, 
                           avg_response_time=final_avg,
                           logs=logs)

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

def get_average_response_time():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # FIX: Use PostgreSQL syntax instead of TIMESTAMPDIFF (MySQL only)
            query = """
                SELECT department, AVG(EXTRACT(EPOCH FROM (last_updated - created_at)) / 60) as avg_time 
                FROM concerns 
                WHERE status = 'Resolved' 
                GROUP BY department
            """
            cursor.execute(query)
            data = cursor.fetchall()
            return data # Ibabalik nito ay listahan ng depts at kanilang average time
    finally:
        conn.close()

# --- AUTH & ACTIONS ---
@app.route('/login_as/<role>')
def login_as(role):
    # Normalize the role to match your dictionary keys
    role_key = role.capitalize() 
    
    dashboards = {
        'Admin': 'admin_dashboard', 
        'Academic': 'academic_dashboard', 
        'Financial': 'financial_dashboard', 
        'Welfare': 'welfare_dashboard'
    }
    
    target_view = dashboards.get(role_key)

    if target_view:
        session['user_role'] = role_key
        # Log the entry for system audit
        log_event(None, f"User session initiated as {role_key}")
        return redirect(url_for(target_view))
    
    # If the role is invalid, wipe session and go home
    session.clear()
    flash("INVALID ACCESS VECTOR DETECTED")
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/update_status', methods=['POST'])
def update_status():
    cid = request.form.get('id')
    status = request.form.get('status')
    action = request.form.get('action_taken')
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # 1. Fetch student info first to populate the email
            cursor.execute("SELECT student_email, subject, category FROM concerns WHERE id = %s", (cid,))
            student = cursor.fetchone()
            
            if student:
                student_email = student['student_email']
                category = student['category']
                subject = student['subject']

                # 2. Update the record
                cursor.execute("""
                    UPDATE concerns 
                    SET status = %s, action_taken = %s, last_updated = NOW() 
                    WHERE id = %s
                """, (status, action, cid))
                
                if status == 'Resolved':
                    cursor.execute("UPDATE concerns SET is_escalated = FALSE WHERE id = %s", (cid,))
                
                conn.commit()

                # 3. Trigger the Email Dispatch
                # We use the category as the department for now
                send_email(
                    to_email=student_email,
                    subject=subject,
                    concern_id=int(cid),
                    status=status,
                    category=category,
                    department=category, 
                    action_taken=action
                )
    finally:
        conn.close()

    log_event(cid, f"Flow Status set to {status}")
    return redirect(request.referrer)

# --- CSV EXPORT ---
@app.route('/export')
@login_required()
def export_data():
    # 1. Identify context and fetch data
    role = session.get('user_role', 'Unknown')
    conn = get_db_connection()
    
    try:
        with conn.cursor() as cursor:
            # Filter logic: Admin sees all, Departments see their specific vector
            if role == 'Admin':
                query = "SELECT * FROM concerns ORDER BY created_at DESC"
                params = ()
            else:
                query = "SELECT * FROM concerns WHERE category = %s ORDER BY created_at DESC"
                params = (role,)
            
            cursor.execute(query, params)
            rows = cursor.fetchall()

        # 2. Generate CSV in memory
        si = io.StringIO()
        cw = csv.writer(si)
        
        # Write Header Row
        cw.writerow(['REF_ID', 'SUBJECT', 'CATEGORY', 'STATUS', 'DATE', 'RESOLUTION_NOTES']) 
        
        for r in rows:
            # SHORT DATE FIX: Use a shorter format and add a space 
            # This prevents Excel from displaying hashtags (#######)
            short_date = r['created_at'].strftime('%m/%d/%y %H:%M') + " "
            
            cw.writerow([
                f"FLW-{r['id']:04d}", 
                r['subject'], 
                r['category'], 
                r['status'], 
                short_date, 
                r['action_taken'] if r['action_taken'] else "NO_NOTES_INPUTTED"
            ])

        # 3. Prepare the Response
        output = make_response(si.getvalue())
        
        # Create a dynamic filename (e.g., Concernflow_Academic_20260317.csv)
        file_stamp = datetime.now().strftime('%Y%m%d')
        filename = f"Concernflow_{role}_{file_stamp}.csv"
        
        output.headers["Content-Disposition"] = f"attachment; filename={filename}"
        output.headers["Content-type"] = "text/csv"
        
        log_event(None, f"CSV Export Triggered: {role} scope")
        return output

    except Exception as e:
        print(f"CRITICAL_EXPORT_ERROR: {e}")
        return "System error during data extraction", 500
    finally:
        conn.close()

# --- PDF EXPORT ---
@app.route('/export_pdf')
@login_required()
def export_pdf():
    # 1. Identify context
    role = session.get('user_role', 'Guest')
    
    # 2. Fetch Filtered Data
    # Logic: If Admin, fetch all. If Dept, fetch only that category.
    filter_role = role if role != 'Admin' else None
    concerns, _, _, _ = fetch_dashboard_data(filter_role)
    
    # 3. Initialize PDF Buffer
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), 
                            rightMargin=30, leftMargin=30, 
                            topMargin=30, bottomMargin=30)
    elements = []
    styles = getSampleStyleSheet()
    
    # 4. Header Section
    title_text = f"CONCERNFLOW SYSTEM REPORT: {role.upper()}"
    elements.append(Paragraph(title_text, styles['Title']))
    elements.append(Paragraph(f"Generated on: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}", styles['Normal']))
    elements.append(Spacer(1, 24))
    
    # 5. Table Data Construction
    # Headers
    data = [['Ref ID', 'Date', 'Time', 'Identity', 'Category', 'Status', 'Esc.', 'Notes']]
    
    if not concerns:
        data.append(["-", "-", "-", "No records found", "-", "-", "-", "-"])
    else:
        for c in concerns:
            # Identity masking
            identity = "ENCRYPTED" if c.get('is_anonymous') else (str(c.get('student_email'))[:20])
            
            # Format rows
            data.append([
                f"FLW-{c['id']}",
                c['created_at'].strftime('%Y-%m-%d'),
                c['created_at'].strftime('%I:%M %p'),
                identity,
                c.get('category', 'N/A'),
                c.get('status', 'Submitted'),
                "YES" if c.get('is_escalated') else "NO",
                (c.get('action_taken', '')[:30] + '..') if c.get('action_taken') else "PENDING"
            ])
    
    # 6. Table Styling
    # FIX: Replaced invalid ROWBACKGROUNDS with alternating BACKGROUND commands per row
    table_style_commands = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e3a8a')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]

    # Add zebra striping manually for each odd row
    for i in range(1, len(data)):
        if i % 2 == 0:
            table_style_commands.append(('BACKGROUND', (0, i), (-1, i), colors.whitesmoke))

    table = Table(data, colWidths=[60, 70, 60, 130, 80, 70, 40, 180])
    table.setStyle(TableStyle(table_style_commands))
    
    elements.append(table)
    
    # 7. Build and Response
    doc.build(elements)
    
    log_event(None, f"PDF Report generated for {role} vector")
    
    pdf_value = buffer.getvalue()
    buffer.close()
    
    response = make_response(pdf_value)
    # Dynamic filename including the role
    filename = f"{role}_Report_{datetime.now().strftime('%Y%m%d')}.pdf"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    response.headers["Content-type"] = "application/pdf"
    
    return response

if __name__ == '__main__':
    app.run(debug=True, port=5000)