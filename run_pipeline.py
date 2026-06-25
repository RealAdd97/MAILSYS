import imaplib
import email
from email.header import decode_header
import pyodbc
import random
import json
import ollama
import os
import time
import yaml
import logging
from concurrent.futures import ThreadPoolExecutor
import datetime
import smtplib
import re

today = datetime.date.today().strftime("%d-%b-%Y") # Format: 18-Jun-2026
# Load configuration
def load_config():
    config_path = "config.yaml"
    default_config = {
        "imap": {
            "server": "imap.gmail.com",
            "email": "aadil.016557@gmail.com",
            "app_password": "pcix tlij abir azph"
        },
        "smtp": {
        "server": "smtp.gmail.com",
        "port": 587,
        "email": "aadil.016557@gmail.com",
        "app_password": "pcix tlij abir azph",
        "notification_email": "aadil.016557@gmail.com" # update if sending elsewhere
    },
        "database": {
            "conn_str": "Driver={ODBC Driver 17 for SQL Server};Server=localhost\\SQLEXPRESS;Database=SupportAutomation;Trusted_Connection=yes;"
        },
        "ollama": {
            "model": "llama3"
        },
        "processing": {
            "thread_pool_size": 4,
            "knowledge_base_folder": "knowledge_base",
            "idle_timeout": 30,
            "fallback_polling_interval": 10
        },
        "logging": {
            "level": "INFO",
            "file": ""
        }
    }
    try:
        with open(config_path, 'r') as f:
            user_config = yaml.safe_load(f)
        # Merge user_config with default_config (shallow merge for now)
        # For simplicity, we'll assume the user provides the full structure or we use defaults for missing keys
        # We'll do a simple update for each top-level key
        for key in default_config:
            if key in user_config:
                default_config[key].update(user_config[key])
            else:
                user_config[key] = default_config[key]
        return user_config
    except FileNotFoundError:
        logging.warning("Config file not found, using default configuration")
        return default_config
    except yaml.YAMLError as e:
        logging.error(f"Error parsing config file: {e}, using default configuration")
        return default_config

# Load configuration
config = load_config()

# Setup logging
log_level = getattr(logging, config['logging']['level'].upper(), logging.INFO)
log_handlers = [logging.StreamHandler()]
if config['logging']['file']:
    log_handlers.append(logging.FileHandler(config['logging']['file']))
logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=log_handlers
)
logger = logging.getLogger(__name__)

def extract_topic_semantic(ticket_content):
    """
    Phase A: Use local Llama 3 via Ollama to extract the core topic/subject
    from the ticket. Returns a lower-case topic string, or None on failure.
    """
    try:
        logger.info(" -> [Step 6B Semantics] Querying Ollama for topic extraction...")
        prompt = (
            "You are a support ticket topic extractor. "
            "Given the support email below, output ONLY a single lowercase word or short phrase "
            "(max 3 words) that best captures the core topic. "
            "No explanation, no punctuation, just the topic.\n\n"
            f"Email:\n{ticket_content}\n\nTopic:"
        )
        response = ollama.chat(
            model=config['ollama']['model'],
            messages=[{'role': 'user', 'content': prompt}],
            options={'temperature': 0.0}
        )
        topic = response['message']['content'].strip().lower()
        # Remove any accidental quotes or trailing punctuation
        topic = topic.strip('"\'').rstrip('.')
        if topic:
            logger.debug(f"    [Semantic Topic] {topic}")
            return topic
    except Exception as e:
        logger.debug(f"    [Semantic Fallback] Ollama topic extraction failed: {e}")
    return None


def keyword_search_policies(kb_folder, ticket_content):
    """
    Phase B: Traditional keyword-matching fallback.
    Scans the knowledge_base folder for policy files that share keywords with the ticket.
    """
    context_payload = ""
    keywords = ticket_content.lower().split()
    for filename in os.listdir(kb_folder):
        if filename.endswith(".txt"):
            file_path = os.path.join(kb_folder, filename)
            with open(file_path, "r", encoding="utf-8") as f:
                policy_text = f.read()
                if any(word in policy_text.lower() for word in keywords if len(word) > 4):
                    logger.debug(f"    [*] Found relevant policy match: {filename}")
                    context_payload += f"\n--- Context Document: {filename} ---\n{policy_text}\n"
    return context_payload


def extract_solution_details(sop_text):
    """
    Extract solution details from SOP text.
    Looks for '- Description:' and '- Handling Instructions:' lines (case-insensitive)
    and returns them as bullet points.
    """
    import re
    details = []
    # Description
    desc_match = re.search(r'^\s*-\s*Description:\s*(.*)$', sop_text, re.IGNORECASE | re.MULTILINE)
    if desc_match:
        details.append(f"- Description: {desc_match.group(1).strip()}")
    # Handling Instructions
    handling_match = re.search(r'^\s*-\s*Handling Instructions:\s*(.*)$', sop_text, re.IGNORECASE | re.MULTILINE)
    if handling_match:
        details.append(f"- Handling Instructions: {handling_match.group(1).strip()}")
    # If neither found, return empty string
    return "\n".join(details)


# ==========================================
# 6B: KNOWLEDGE SEARCH ENGINE
# ==========================================
def search_local_knowledge_base(ticket_content):
    """
    Hybrid Knowledge Search Engine (Step 6B).
    1. Attempts semantic topic extraction via Ollama.
    2. Falls back to keyword matching if Ollama is unavailable.
    3. Merges results from both phases into a single context payload.
    """
    kb_folder = config['processing']['knowledge_base_folder']
    context_payload = ""

    # Ensure the folder exists
    if not os.path.exists(kb_folder):
        logger.info(" -> [Step 6B Notice] Knowledge base folder not found. Skipping context.")
        return "No specific corporate SOP found."

    logger.info(f" -> [Step 6B] Scanning local policy files for relevant context...")

    # --- Phase A: Semantic Topic Extraction ---
    semantic_topic = extract_topic_semantic(ticket_content)
    if semantic_topic:
        # Use the extracted topic as an additional keyword to find policies
        for filename in os.listdir(kb_folder):
            if filename.endswith(".txt"):
                file_path = os.path.join(kb_folder, filename)
                with open(file_path, "r", encoding="utf-8") as f:
                    policy_text = f.read()
                    if semantic_topic in policy_text.lower():
                        logger.debug(f"    [*] Semantic policy match: {filename}")
                        context_payload += f"\n--- Context Document: {filename} ---\n{policy_text}\n"

    # --- Phase B: Keyword Fallback ---
    keyword_payload = keyword_search_policies(kb_folder, ticket_content)
    if keyword_payload:
        # Avoid duplicating documents already found via semantic search
        context_payload = context_payload + keyword_payload if keyword_payload not in context_payload else context_payload

    return context_payload if context_payload else "No specific matching corporate SOP found."

# ==========================================
# 1. CENTRAL CONFIGURATIONS
# ==========================================
# Configuration values are loaded from config.yaml and accessed via the config dict
# IMAP settings: config['imap']
# Database connection string: config['database']['conn_str']
# Ollama model: config['ollama']['model']

def clean_text(text):
    if isinstance(text, bytes):
        return text.decode('utf-8', errors='ignore')
    return text

# ==========================================
# PHASE 1: EMAIL INGESTION (Steps 2 & 3)
# ==========================================
def fetch_latest_unread_email():
    try:
        logger.info("\n--- [Step 2] Checking Mailbox for New Tickets ---")
        mail = imaplib.IMAP4_SSL(config['imap']['server'])
        mail.login(config['imap']['email'], config['imap']['app_password'])
        mail.select("inbox")
        
        # Search for all unseen emails since today
        status, messages = mail.search(None, f'(UNSEEN SINCE "{today}")')
        all_email_ids = messages[0].split() if messages[0] else []

        # Search for notification emails specifically to log how many we're skipping
        status, notify_messages = mail.search(None, f'(UNSEEN SINCE "{today}" SUBJECT "High Confidence Ticket Notification")')
        notification_email_ids = notify_messages[0].split() if notify_messages[0] else []

        # Our target emails are unseen non-notification emails
        email_ids = [eid for eid in all_email_ids if eid not in notification_email_ids]

        if notification_email_ids:
            logger.info(f"Skipping {len(notification_email_ids)} notification email(s)")

        if not email_ids:
            logger.info("No new unread support emails found. System idling...")
            mail.logout()
            return None

        logger.info(f"Found {len(email_ids)} unread email(s). Fetching the latest...")
        latest_email_id = email_ids[-1]
        status, msg_data = mail.fetch(latest_email_id, '(RFC822)')
        
        ticket_id = None
        
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])
                
                subject = clean_text(decode_header(msg["Subject"])[0][0])
                sender = clean_text(decode_header(msg["From"])[0][0])
                
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                            body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                            break
                else:
                    body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')

                # Check if this email is a reply to a previously sent solution email
                reply_ticket_id = extract_ticket_id_from_subject(subject)
                if reply_ticket_id:
                    # Add a UserReply note
                    add_ticket_note(reply_ticket_id, 'UserReply', body, sender)
                    logger.info(f"Added UserReply note to ticket {reply_ticket_id} from email {latest_email_id.decode()}")
                    # Mark the email as seen
                    mail.store(latest_email_id, '+FLAGS', '\\Seen')
                    mail.logout()
                    return None

                # Generate Ticket ID
                ticket_id = f"INC-2026-{random.randint(1000, 9999)}"
                
                # Write to Database
                conn = pyodbc.connect(config['database']['conn_str'])
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO Tickets (TicketID, Subject, Body, Sender, Status) VALUES (?, ?, ?, ?, 'New')",
                    ticket_id, subject, body.strip(), sender
                )
                conn.commit()
                logger.info(f"-> [Step 3] Logged {ticket_id} to Database successfully.")
                
                # Mark as read
                mail.store(latest_email_id, '+FLAGS', '\\Seen')
                cursor.close()
                conn.close()
                
        mail.logout()
        return ticket_id

    except Exception as e:
        logger.error(f"Ingestion Error: {e}")
        return None

# ==========================================
# PHASE 2: AI BRAIN & ROUTING (Steps 5 & 6A)
# ==========================================
def process_ticket_with_ai(ticket_id):
    if not ticket_id:
        return
        
    try:
        logger.info(f"\n--- [Step 5] Triggering Local AI Processor for {ticket_id} ---")
        
        # Open connection and cursor first!
        conn = pyodbc.connect(config['database']['conn_str'])
        cursor = conn.cursor()
        
        # 1. Fetch Subject, Body, and Sender from your SSMS database
        cursor.execute("SELECT Subject, Body, Sender FROM Tickets WHERE TicketID = ?", ticket_id)
        row = cursor.fetchone()
        if not row:
            logger.warning("Ticket data could not be pulled from DB.")
            return
        subject, email_body, sender = row[0], row[1], row[2]
        
        # 2. RUN STEP 6B: Search for corporate manuals matching the ticket data
        combined_ticket_text = f"{subject} {email_body}"
        matched_corporate_sop = search_local_knowledge_base(combined_ticket_text)
        
        # 3. Enhance the instruction telling the AI to use the attached policy guidelines!
        system_instruction = (
            "You are an AI Support Ticket Processor. Analyze the support email using the provided Corporate Policy Context.\n"
            "You MUST output ONLY a valid JSON object and nothing else. Do not include any explanations, analysis, or additional text.\n"
            "The JSON object must have exactly these four keys: 'category', 'subcategory', 'summary', and 'confidence'.\n"
            "Choose the 'category' from one of these exact values: ['Payroll / Salary', 'Finance / Invoice', 'Technology / VPN', 'HR / Leave'].\n"
            "The 'subcategory' should be a specific issue type related to the category.\n"
            "Provide a short 1-sentence summary for 'summary'.\n"
            "Provide a confidence score between 0 and 1 for 'confidence' as a number (not string).\n"
            "Output format example: {\"category\": \"HR / Leave\", \"subcategory\": \"Maternity Leave Request\", \"summary\": \"Employee requesting maternity leave per policy\", \"confidence\": 0.95}"
        )
        
        # Combine the custom SOP rules with the email text inside the prompt payload
        user_payload = (
            f"Corporate Policy Context:\n{matched_corporate_sop}\n\n"
            f"Customer Email:\n{email_body}"
        )
        
        logger.info("Querying local Llama 3 model with injected policy context...")
        response = ollama.chat(
            model=config['ollama']['model'],
            messages=[
                {'role': 'system', 'content': system_instruction},
                {'role': 'user', 'content': user_payload}
            ],
            options={'temperature': 0.0}
        )
        
        ai_output = response['message']['content']
        print(f"Raw AI Output Received: {ai_output}")
        
        # 4. Strip out conversational filler and markdown backticks if they exist
        clean_json_str = ai_output.strip()

        # Try to extract JSON from various formats
        if "```json" in clean_json_str:
            # Extract JSON from markdown code blocks
            clean_json_str = clean_json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in clean_json_str:
            # Extract JSON from generic markdown code blocks
            clean_json_str = clean_json_str.split("```")[1].split("```")[0].strip()
        else:
            # Look for JSON-like content (curly braces)
            start_idx = clean_json_str.find("{")
            end_idx = clean_json_str.rfind("}") + 1
            if start_idx != -1 and end_idx != 0 and end_idx > start_idx:
                clean_json_str = clean_json_str[start_idx:end_idx]
            else:
                # If no JSON found, try to construct JSON from the analysis
                logger.warning("No JSON found in AI output, attempting to extract information from analysis")
                logger.info("Fallback extraction started for ticket %s", ticket_id)

                # Try to extract category from known values or structured response
                category = "HR / Leave"  # default
                subcategory = "General Inquiry"
                summary = "Support ticket processed"
                confidence = 0.5

                # First, try to extract from structured response like "Routing Group: HR / Leave"
                import re
                routing_match = re.search(r'Routing\s*Group\s*[:]\s*([^\n]+)', ai_output, re.IGNORECASE)
                if routing_match:
                    routing_group = routing_match.group(1).strip()
                    logger.info("Found routing group: %s", routing_group)
                    # Check if it contains any of our known categories
                    if "Payroll" in routing_group or "Salary" in routing_group:
                        category = "Payroll / Salary"
                        logger.info("Matched category Payroll/Salary from routing group")
                    elif "Finance" in routing_group or "Invoice" in routing_group:
                        category = "Finance / Invoice"
                        logger.info("Matched category Finance/Invoice from routing group")
                    elif "Technology" in routing_group or "VPN" in routing_group:
                        category = "Technology / VPN"
                        logger.info("Matched category Technology/VPN from routing group")
                    elif "HR" in routing_group or "Leave" in routing_group:
                        category = "HR / Leave"
                        logger.info("Matched category HR/Leave from routing group")
                    confidence = 0.9  # we are confident in category because we got it from routing group
                    logger.info("Set confidence to 0.9 based on routing group extraction")
                else:
                    logger.info("No routing group found in AI output")

                # Try to extract summary
                summary_match = re.search(r'\*\*Summary\*\*:\s*([^\n]+)', ai_output, re.IGNORECASE)
                if not summary_match:
                    summary_match = re.search(r'Summary\s*[:]\s*([^\n]+)', ai_output, re.IGNORECASE)
                if summary_match:
                    extracted_summary = summary_match.group(1).strip()
                    logger.info("Extracted summary from summary line: %s", extracted_summary)
                    summary = extracted_summary
                    confidence = 0.9  # we are confident in summary because we got it from summary line
                    logger.info("Set confidence to 0.9 based on summary extraction")
                    # If we already set confidence to 0.9 from routing, we leave it at 0.9
                else:
                    logger.info("No summary line found in AI output")

                # If we didn't get confidence from routing or summary, try keyword matching for category
                if confidence == 0.5:
                    logger.info("Confidence still 0.5, attempting keyword matching for category")
                    if "Payroll" in ai_output or "Salary" in ai_output:
                        category = "Payroll / Salary"
                        confidence = 0.7
                        logger.info("Matched category Payroll/Salary via keyword, confidence 0.7")
                    elif "Finance" in ai_output or "Invoice" in ai_output:
                        category = "Finance / Invoice"
                        confidence = 0.7
                        logger.info("Matched category Finance/Invoice via keyword, confidence 0.7")
                    elif "Technology" in ai_output or "VPN" in ai_output:
                        category = "Technology / VPN"
                        confidence = 0.7
                        logger.info("Matched category Technology/VPN via keyword, confidence 0.7")
                    elif "HR" in ai_output or "Leave" in ai_output:
                        category = "HR / Leave"
                        confidence = 0.7
                        logger.info("Matched category HR/Leave via keyword, confidence 0.7")
                    else:
                        logger.info("No keyword matches found for category, keeping default")

                # If we still have low confidence, try to get a better summary from the first line
                if confidence < 0.9 and summary == "Support ticket processed":
                    logger.info("Confidence < 0.9 and summary is default, attempting to extract summary from first line")
                    lines = ai_output.strip().split('\n')
                    for line in lines:
                        line = line.strip()
                        if line and not line.startswith('*') and not line.startswith('#') and len(line) > 10:
                            # Clean up markdown and take first reasonable sentence
                            cleaned_line = line.replace('**', '').strip()
                            logger.info("Using line as summary: %s", cleaned_line)
                            summary = cleaned_line
                            if len(summary) > 100:
                                summary = summary[:100] + "..."
                            break
                    else:
                        logger.info("No suitable line found for summary extraction")

                # Try to extract subcategory from known patterns or use category-appropriate default
                subcategory = "General Inquiry"
                logger.info("Determining subcategory for category: %s", category)
                if category == "HR / Leave":
                    if "maternity" in ai_output.lower():
                        subcategory = "Maternity Leave Request"
                        logger.info("Detected maternity keyword -> Maternity Leave Request")
                    elif "parental" in ai_output.lower():
                        subcategory = "Parental Leave Request"
                        logger.info("Detected parental keyword -> Parental Leave Request")
                    elif "sick" in ai_output.lower() or "medical" in ai_output.lower():
                        subcategory = "Sick Leave"
                        logger.info("Detected sick/medical keyword -> Sick Leave")
                    elif "vacation" in ai_output.lower() or "holiday" in ai_output.lower():
                        subcategory = "Vacation Request"
                        logger.info("Detected vacation/holiday keyword -> Vacation Request")
                    elif "adoption" in ai_output.lower():
                        subcategory = "Adoption Leave"
                        logger.info("Detected adoption keyword -> Adoption Leave")
                elif category == "Payroll / Salary":
                    if "incorrect" in ai_output.lower() or "underpayment" in ai_output.lower() or "overpayment" in ai_output.lower():
                        subcategory = "Incorrect Salary Payment"
                        logger.info("Detected incorrect/underpayment/overpayment keyword -> Incorrect Salary Payment")
                    elif "delayed" in ai_output.lower() or "late" in ai_output.lower():
                        subcategory = "Delayed Salary"
                        logger.info("Detected delayed/late keyword -> Delayed Salary")
                    elif "tax" in ai_output.lower() or "deduction" in ai_output.lower():
                        subcategory = "Tax Deduction Error"
                        logger.info("Detected tax/deduction keyword -> Tax Deduction Error")
                    elif "benefit" in ai_output.lower():
                        subcategory = "Benefit Related"
                        logger.info("Detected benefit keyword -> Benefit Related")
                elif category == "Finance / Invoice":
                    if "disputed" in ai_output.lower() or "dispute" in ai_output.lower():
                        subcategory = "Disputed Invoice Resolution"
                        logger.info("Detected disputed/dispute keyword -> Disputed Invoice Resolution")
                    elif "early payment" in ai_output.lower() or "discount" in ai_output.lower():
                        subcategory = "Early Payment Discount"
                        logger.info("Detected early payment/discount keyword -> Early Payment Discount")
                    elif "late" in ai_output.lower() or "overdue" in ai_output.lower():
                        subcategory = "Late Invoice"
                        logger.info("Detected late/overdue keyword -> Late Invoice")
                elif category == "Technology / VPN":
                    if "connection" in ai_output.lower() or "connect" in ai_output.lower():
                        subcategory = "VPN Connection Failure"
                        logger.info("Detected connection/connect keyword -> VPN Connection Failure")
                    elif "authentication" in ai_output.lower() or "login" in ai_output.lower() or "password" in ai_output.lower():
                        subcategory = "VPN Authentication Failure"
                        logger.info("Detected authentication/login/password keyword -> VPN Authentication Failure")
                    elif "configuration" in ai_output.lower() or "setup" in ai_output.lower():
                        subcategory = "VPN Configuration Issue"
                        logger.info("Detected configuration/setup keyword -> VPN Configuration Issue")

                # Create fallback JSON with confidence based on what we found
                clean_json_str = f'{{"category": "{category}", "subcategory": "{subcategory}", "summary": "{summary}", "confidence": {confidence}}}'
                logger.info("Fallback JSON constructed: %s", clean_json_str)

        # Now pass the sanitized string to the JSON parser safely
        try:
            ai_data = json.loads(clean_json_str)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI output as JSON: {e}")
            logger.error(f"AI output was: {ai_output[:200]}...")
            # Last resort fallback
            ai_data = {
                "category": "HR / Leave",
                "subcategory": "General Inquiry",
                "summary": "Ticket processed via fallback",
                "confidence": 0.5
            }
        category = ai_data.get('category')
        subcategory = ai_data.get('subcategory')
        summary = ai_data.get('summary')
        confidence = ai_data.get('confidence')
        try:
            confidence = float(confidence) if confidence is not None else 0.0
        except (ValueError, TypeError):
            confidence = 0.0
        
        # Step 6A: Dynamic Team Lead Mapping Look-up
        cursor.execute("SELECT TeamLead FROM TeamLeadMapping WHERE Category = ?", category)
        mapping_row = cursor.fetchone()
        assigned_tl = mapping_row[0] if mapping_row else "TL_GENERAL"
        
        # Save enriched analytical data back to SQL Server
        cursor.execute(
            """
            UPDATE Tickets 
            SET Category = ?, Subcategory = ?, Summary = ?, AssignedTeamLead = ?, Status = 'Processed'
            WHERE TicketID = ?
            """,
            category, subcategory, summary, assigned_tl, ticket_id
        )
        conn.commit()
        # Add system note: ticket processed and resolved
        add_ticket_note(ticket_id, 'SystemNote', 'Ticket processed and resolved.', None)

        logger.info(f"\n--- [Pipeline Complete] Summary for {ticket_id} ---")
        logger.info(f"Categorized As: {category} ({subcategory})")
        logger.info(f"Routed Directly To: {assigned_tl}")
        logger.info(f"Brief Summary: {summary}")
        logger.info(f"AI Confidence Score: {confidence:.2f}")
        # Ensure Notified column exists and check if notification already sent
        try:
            cursor.execute("SELECT Notified FROM Tickets WHERE TicketID = ?", ticket_id)
            row = cursor.fetchone()
            notified = row[0] if row else 0
        except pyodbc.ProgrammingError:
            # Notified column doesn't exist, add it
            cursor.execute("ALTER TABLE Tickets ADD Notified BIT DEFAULT 0")
            conn.commit()
            notified = 0

        if notified == 1:
            logger.info("Notification already sent for ticket %s, skipping", ticket_id)
        else:
            # Send email notification if confidence is high
            if confidence >= 0.9:
                send_confidence_email(ticket_id, category, subcategory, summary, confidence)
                # Mark notification as sent
                cursor.execute("UPDATE Tickets SET Notified = 1 WHERE TicketID = ?", ticket_id)
                conn.commit()
                # Send AI response email to original sender
                send_ai_response_email(ticket_id, category, subcategory, summary, confidence, assigned_tl, sender, matched_corporate_sop)
                # Add system note: solution email sent via knowledge base
                add_ticket_note(ticket_id, 'SystemNote', 'Solution email sent via knowledge base.', None)

        cursor.close()
        conn.close()

    except Exception as e:
        logger.error(f"AI Processing Error: {e}")

# ==========================================
# EXECUTION CONTROLLER
# ==========================================

def send_ai_response_email(ticket_id, category, subcategory, summary, confidence, assigned_tl, sender, matched_corporate_sop):
    """
    Send a solution email to the original ticket sender using knowledge base information.
    """
    try:
        smtp_config = config['smtp']
        # Extract solution details from the matched SOP
        solution_details = extract_solution_details(matched_corporate_sop)
        # If not found, fall back to the AI summary
        if not solution_details:
            solution_details = summary
        msg = f"""Subject: Solution for Ticket {ticket_id}

Based on our knowledge base, here is the solution to your issue:

{solution_details}

This is an automated response from the AI Email Ticketing Service.
"""
        logger.debug(f"Attempting to send solution email for ticket {ticket_id} to {sender}")
        server = smtplib.SMTP(smtp_config['server'], smtp_config['port'])
        server.starttls()
        server.login(smtp_config['email'], smtp_config['app_password'])
        server.sendmail(smtp_config['email'], sender, msg.encode('utf-8'))
        server.quit()
        logger.info(f"Sent solution email for ticket {ticket_id} (confidence: {confidence:.2f})")
    except Exception as e:
        logger.error(f"Failed to send solution email for ticket {ticket_id}: {e}")
        # Re-raise to make sure the error doesn't go unnoticed in background threads
        raise

def add_ticket_note(ticket_id, note_type, note_body, created_by=None):
    """
    Insert a note into the TicketNotes table.
    """
    try:
        conn = pyodbc.connect(config['database']['conn_str'])
        cursor = conn.cursor()
        # Ensure table exists (create if not exists)
        cursor.execute("""
            IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'TicketNotes')
            BEGIN
                CREATE TABLE TicketNotes (
                    NoteID INT IDENTITY(1,1) PRIMARY KEY,
                    TicketID VARCHAR(20) NOT NULL,
                    NoteType VARCHAR(20) NOT NULL,
                    NoteBody NVARCHAR(MAX) NOT NULL,
                    CreatedAt DATETIME DEFAULT GETDATE(),
                    CreatedBy VARCHAR(100) NULL
                )
            END
        """)
        conn.commit()
        cursor.execute(
            "INSERT INTO TicketNotes (TicketID, NoteType, NoteBody, CreatedBy) VALUES (?, ?, ?, ?)",
            ticket_id, note_type, note_body, created_by
        )
        conn.commit()
        cursor.close()
        conn.close()
        logger.debug(f"Added {note_type} note to ticket {ticket_id}")
    except Exception as e:
        logger.error(f"Failed to add note to ticket {ticket_id}: {e}")

def extract_ticket_id_from_subject(subject):
    """
    Extract ticket ID from a subject like 'Re: Solution for Ticket INC-2026-1234'.
    Returns ticket ID if found, else None.
    """
    match = re.search(r'Re:\s*Solution\s+for\s+Ticket\s+(INC-\d{4}-\d{4})', subject, re.IGNORECASE)
    if match:
        return match.group(1)
    return None

def send_confidence_email(ticket_id, category, subcategory, summary, confidence):
    """
    Send an email notification when AI confidence is high.
    """
    try:
        smtp_config = config['smtp']
        msg = f"""Subject: High Confidence Ticket Notification: {ticket_id}

Ticket ID: {ticket_id}
Category: {category}
Subcategory: {subcategory}
Summary: {summary}
Confidence: {confidence:.2f}

This is an automated notification from the AI Email Ticketing Service.
"""
        logger.debug(f"Attempting to send confidence email for ticket {ticket_id} to {smtp_config['notification_email']}")
        server = smtplib.SMTP(smtp_config['server'], smtp_config['port'])
        server.starttls()
        server.login(smtp_config['email'], smtp_config['app_password'])
        server.sendmail(smtp_config['email'], smtp_config['notification_email'], msg.encode('utf-8'))
        server.quit()
        logger.info(f"Sent confidence email notification for ticket {ticket_id} (confidence: {confidence:.2f})")
    except Exception as e:
        logger.error(f"Failed to send confidence email for ticket {ticket_id}: {e}")
        # Re-raise to make sure the error doesn't go unnoticed in background threads
        raise


if __name__ == "__main__":
    # Create a thread pool for AI processing
    executor = ThreadPoolExecutor(max_workers=config['processing']['thread_pool_size'])
    try:
        while True:
            # Fetch and process one unread email at a time
            new_ticket = fetch_latest_unread_email()
            if new_ticket is None:
                # No unread emails, wait before checking again
                time.sleep(config['processing']['fallback_polling_interval'])
                continue

            # Submit the ticket for AI processing in the background
            logger.info(f"\n--- [Main] Submitted ticket {new_ticket} for asynchronous AI processing ---")
            executor.submit(process_ticket_with_ai, new_ticket)
    except KeyboardInterrupt:
        logger.info("\n--- [Main] Shutting down gracefully... ---")
    finally:
        # Wait for all submitted tasks to complete
        executor.shutdown(wait=True)
        logger.info("--- [Main] Shutdown complete. ---")