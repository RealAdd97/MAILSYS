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

today = datetime.date.today().strftime("%d-%b-%Y")  # Format: 18-Jun-2026

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
            "notification_email": "aadil.016557@gmail.com"
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
    Looks for '- Description:' and '- Handling Instructions:' sections (case-insensitive)
    and returns them as formatted text, capturing FULL multi-line content.
    """
    details = []

    desc_match = re.search(
        r'^\s*-\s*Description:\s*(.*?)(?=^\s*-\s*(?:Handling|Resolution|Steps|Notes)|\Z)',
        sop_text,
        re.IGNORECASE | re.MULTILINE | re.DOTALL
    )
    if desc_match:
        desc_content = desc_match.group(1).strip()
        desc_content = re.sub(r'\n{3,}', '\n\n', desc_content)
        desc_content = re.sub(r'\n\s*-\s*', '\n• ', desc_content)
        details.append(f"📋 Description:\n{desc_content}")

    handling_match = re.search(
        r'^\s*-\s*Handling Instructions:\s*(.*?)(?=^\s*-\s*(?:Description|Resolution|Notes)|\Z)',
        sop_text,
        re.IGNORECASE | re.MULTILINE | re.DOTALL
    )
    if handling_match:
        handling_content = handling_match.group(1).strip()
        handling_content = re.sub(r'\n{3,}', '\n\n', handling_content)
        handling_content = re.sub(r'^(\d+)[.)]\s*', r'\n\1. ', handling_content, flags=re.MULTILINE)
        handling_content = re.sub(r'\n\s*-\s*', '\n• ', handling_content)
        details.append(f"🔧 Handling Instructions:\n{handling_content}")

    if not details:
        cleaned = sop_text.strip()
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned

    return "\n\n".join(details)


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

    if not os.path.exists(kb_folder):
        logger.info(" -> [Step 6B Notice] Knowledge base folder not found. Skipping context.")
        return "No specific corporate SOP found."

    logger.info(f" -> [Step 6B] Scanning local policy files for relevant context...")

    # --- Phase A: Semantic Topic Extraction ---
    semantic_topic = extract_topic_semantic(ticket_content)
    if semantic_topic:
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
        context_payload = context_payload + keyword_payload if keyword_payload not in context_payload else context_payload

    return context_payload if context_payload else "No specific matching corporate SOP found."


# ==========================================
# CATEGORY INFERENCE FROM TICKET CONTENT
# ==========================================

# Keyword lists for each category — checked against the raw ticket text,
# not the AI output, so they are immune to model hallucinations.
CATEGORY_KEYWORDS = {
    "Technology / VPN": [
        "vpn", "virtual private network", "remote access", "secure connection",
        "encryption", "tunnel", "network", "firewall", "proxy", "ssl", "tls",
        "authentication failure", "connection failure", "unable to connect",
        "cisco anyconnect", "openvpn", "wireguard", "remote desktop", "rdp",
        "sop-tech", "it support", "patch update", "intranet", "file share"
    ],
    "Finance / Invoice": [
        "invoice", "billing", "payment", "purchase order", "po number",
        "vendor", "supplier", "accounts payable", "accounts receivable",
        "overdue", "receipt", "refund", "credit note", "debit note",
        "finance team", "sop-fin", "disputed invoice", "early payment"
    ],
    "Payroll / Salary": [
        "salary", "payroll", "payslip", "pay slip", "wage", "wages",
        "bonus", "deduction", "tax deduction", "underpayment", "overpayment",
        "net pay", "gross pay", "compensation", "reimbursement",
        "sop-pay", "delayed salary", "incorrect payment"
    ],
    "HR / Leave": [
        "leave", "annual leave", "sick leave", "maternity", "paternity",
        "parental leave", "vacation", "holiday request", "time off",
        "absence", "hr team", "human resources", "adoption leave",
        "sop-hr", "resignation", "onboarding", "offboarding"
    ]
}


# FIX 2 & 3: Require a minimum score AND a clear margin of victory before
# overriding the AI result. Scores are normalised by keyword-list size so
# the larger Technology/VPN list can't out-vote the others on volume alone.
def infer_category_from_ticket(ticket_text, min_score: int = 2, dominance_ratio: float = 1.5):
    """
    Infer the most likely category by scoring the raw ticket text against
    keyword lists for each category.

    Two guards prevent false overrides:
      - min_score: the winning category must have at least this many raw hits.
        A single incidental keyword match (e.g. 'payment' in a payroll email)
        is not enough to override the AI's judgement.
      - dominance_ratio: the winner's normalised score must be at least
        dominance_ratio × the runner-up's score. If the ticket is genuinely
        ambiguous the AI result is preserved.

    Scores are normalised by keyword-list length so a category with more
    keywords (Technology/VPN has 24 vs ~17 for the others) does not get an
    unfair advantage from pure list size.

    Returns the winning category string, or None when evidence is too weak
    or too ambiguous to justify overriding the model.
    """
    ticket_lower = ticket_text.lower()

    raw_scores = {cat: 0 for cat in CATEGORY_KEYWORDS}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in ticket_lower:
                raw_scores[cat] += 1

    # Normalise by list size so larger lists don't have an unfair advantage
    normalised_scores = {
        cat: raw_scores[cat] / len(CATEGORY_KEYWORDS[cat])
        for cat in raw_scores
    }

    logger.debug(f"    [Category Raw Scores]        {raw_scores}")
    logger.debug(f"    [Category Normalised Scores] {normalised_scores}")

    sorted_scores = sorted(normalised_scores.items(), key=lambda x: x[1], reverse=True)
    best_cat, best_norm_score = sorted_scores[0]
    second_norm_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0

    # Guard 1: not enough raw keyword hits — don't override
    if raw_scores[best_cat] < min_score:
        logger.debug(
            f"    [Category Guard] Raw score {raw_scores[best_cat]} < min_score {min_score}. "
            "Returning None (trust the AI)."
        )
        return None

    # Guard 2: win margin too small — ticket is ambiguous, don't override
    if second_norm_score > 0 and (best_norm_score / second_norm_score) < dominance_ratio:
        logger.debug(
            f"    [Category Guard] Dominance ratio "
            f"{best_norm_score / second_norm_score:.2f} < {dominance_ratio}. "
            "Returning None (trust the AI)."
        )
        return None

    return best_cat


# ==========================================
# 1. CENTRAL CONFIGURATIONS
# ==========================================

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

        status, messages = mail.search(None, f'(UNSEEN SINCE "{today}")')
        all_email_ids = messages[0].split() if messages[0] else []

        status, notify_messages = mail.search(None, f'(UNSEEN SINCE "{today}" SUBJECT "High Confidence Ticket Notification")')
        notification_email_ids = notify_messages[0].split() if notify_messages[0] else []

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

                reply_ticket_id = extract_ticket_id_from_subject(subject)
                if reply_ticket_id:
                    add_ticket_note(reply_ticket_id, 'UserReply', body, sender)
                    logger.info(f"Added UserReply note to ticket {reply_ticket_id} from email {latest_email_id.decode()}")
                    mail.store(latest_email_id, '+FLAGS', '\\Seen')
                    mail.logout()
                    return None

                ticket_id = f"INC-2026-{random.randint(1000, 9999)}"

                conn = pyodbc.connect(config['database']['conn_str'])
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO Tickets (TicketID, Subject, Body, Sender, Status) VALUES (?, ?, ?, ?, 'New')",
                    ticket_id, subject, body.strip(), sender
                )
                conn.commit()
                logger.info(f"-> [Step 3] Logged {ticket_id} to Database successfully.")

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

        conn = pyodbc.connect(config['database']['conn_str'])
        cursor = conn.cursor()

        cursor.execute("SELECT Subject, Body, Sender FROM Tickets WHERE TicketID = ?", ticket_id)
        row = cursor.fetchone()
        if not row:
            logger.warning("Ticket data could not be pulled from DB.")
            return
        subject, email_body, sender = row[0], row[1], row[2]

        combined_ticket_text = f"{subject} {email_body}"

        # Run Step 6B: search corporate knowledge base
        matched_corporate_sop = search_local_knowledge_base(combined_ticket_text)

        # FIX 1: One concrete example per category so the model has an equal
        # anchor for each one and does not bias high confidence toward HR/Leave.
        system_instruction = (
            "You are an AI Support Ticket Processor. Analyze the support email using the provided Corporate Policy Context.\n"
            "You MUST output ONLY a valid JSON object and nothing else. Do not include any explanations, analysis, or additional text.\n"
            "The JSON object must have exactly these four keys: 'category', 'subcategory', 'summary', and 'confidence'.\n"
            "Choose the 'category' from one of these exact values: ['Payroll / Salary', 'Finance / Invoice', 'Technology / VPN', 'HR / Leave'].\n"
            "The 'subcategory' should be a specific issue type related to the category.\n"
            "Provide a short 1-sentence summary for 'summary'.\n"
            "Provide a confidence score between 0 and 1 reflecting how certain you are based on the ticket content and context.\n\n"
            "Output format examples (one per category — use these as a guide for confidence calibration):\n"
            "{\"category\": \"HR / Leave\", \"subcategory\": \"Maternity Leave Request\", \"summary\": \"Employee requesting maternity leave per policy.\", \"confidence\": 0.95}\n"
            "{\"category\": \"Finance / Invoice\", \"subcategory\": \"Disputed Invoice\", \"summary\": \"Vendor disputing an invoice payment amount.\", \"confidence\": 0.93}\n"
            "{\"category\": \"Technology / VPN\", \"subcategory\": \"VPN Connection Failure\", \"summary\": \"User unable to connect to corporate VPN remotely.\", \"confidence\": 0.94}\n"
            "{\"category\": \"Payroll / Salary\", \"subcategory\": \"Incorrect Salary Payment\", \"summary\": \"Employee reporting underpayment in last payroll run.\", \"confidence\": 0.92}"
        )

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

        clean_json_str = ai_output.strip()

        # --- JSON extraction from AI output ---
        if "```json" in clean_json_str:
            clean_json_str = clean_json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in clean_json_str:
            clean_json_str = clean_json_str.split("```")[1].split("```")[0].strip()
        else:
            start_idx = clean_json_str.find("{")
            end_idx = clean_json_str.rfind("}") + 1
            if start_idx != -1 and end_idx != 0 and end_idx > start_idx:
                clean_json_str = clean_json_str[start_idx:end_idx]
            else:
                # -------------------------------------------------------
                # FALLBACK: AI did not return JSON — extract fields from
                # the free-text response, then validate the category against
                # the raw ticket content so we never default to a wrong team.
                # -------------------------------------------------------
                logger.warning("No JSON found in AI output, attempting fallback extraction")
                logger.info("Fallback extraction started for ticket %s", ticket_id)

                # Start with NO category so we are forced to derive it properly
                category = None
                subcategory = "General Inquiry"
                summary = "Support ticket processed"
                confidence = 0.5
                routing_found = False
                summary_found = False

                # --- Try to extract routing group from AI narrative ---
                routing_match = re.search(r'Routing\s*Group\s*[:]\s*([^\n]+)', ai_output, re.IGNORECASE)
                if routing_match:
                    routing_group = routing_match.group(1).strip()
                    logger.info("Found routing group in AI output: %s", routing_group)
                    if "Payroll" in routing_group or "Salary" in routing_group:
                        category = "Payroll / Salary"
                    elif "Finance" in routing_group or "Invoice" in routing_group:
                        category = "Finance / Invoice"
                    elif "Technology" in routing_group or "VPN" in routing_group:
                        category = "Technology / VPN"
                    elif "HR" in routing_group or "Leave" in routing_group:
                        category = "HR / Leave"
                    if category:
                        routing_found = True
                        logger.info("Category from routing group: %s", category)

                # --- Try to extract summary from AI narrative ---
                summary_match = re.search(r'\*\*Summary\*\*:\s*([^\n]+)', ai_output, re.IGNORECASE)
                if not summary_match:
                    summary_match = re.search(r'Summary\s*[:]\s*([^\n]+)', ai_output, re.IGNORECASE)
                if summary_match:
                    extracted_summary = summary_match.group(1).strip()
                    logger.info("Extracted summary: %s", extracted_summary)
                    summary = extracted_summary
                    summary_found = True

                # --- Set confidence only when BOTH routing and summary are found ---
                # This prevents a good summary from masking a bad/missing category.
                if routing_found and summary_found:
                    confidence = 0.9
                    logger.info("Both routing group and summary found; confidence set to 0.9")
                elif routing_found or summary_found:
                    confidence = 0.7
                    logger.info("Only one of routing/summary found; confidence set to 0.7")

                # --- Keyword scan of the AI output as a secondary signal ---
                if not category:
                    logger.info("No routing group found; trying keyword scan of AI output")
                    if "Payroll" in ai_output or "Salary" in ai_output:
                        category = "Payroll / Salary"
                    elif "Finance" in ai_output or "Invoice" in ai_output:
                        category = "Finance / Invoice"
                    elif "Technology" in ai_output or "VPN" in ai_output:
                        category = "Technology / VPN"
                    elif "HR" in ai_output or "Leave" in ai_output:
                        category = "HR / Leave"
                    if category:
                        logger.info("Category from AI output keyword scan: %s", category)

                # --- PRIMARY SAFETY NET: score the raw ticket content ---
                # FIX 2 & 3 apply here too: infer_category_from_ticket now
                # requires min_score=2 hits and a 1.5× dominance ratio before
                # it will return a category, preventing single-keyword false
                # overrides and unfair scoring from the larger VPN keyword list.
                ticket_inferred_category = infer_category_from_ticket(combined_ticket_text)
                logger.info("Category inferred from raw ticket content: %s", ticket_inferred_category)

                if ticket_inferred_category and category != ticket_inferred_category:
                    logger.warning(
                        "Category mismatch: AI fallback said '%s' but ticket content indicates '%s'. "
                        "Overriding with ticket-content inference.",
                        category, ticket_inferred_category
                    )
                    category = ticket_inferred_category
                    # Reduce confidence slightly to flag that inference was used
                    confidence = min(confidence, 0.85)

                # --- Absolute last resort: no signal at all ---
                if not category:
                    logger.warning("No category signal found anywhere; defaulting to HR / Leave")
                    category = "HR / Leave"
                    confidence = 0.5

                # --- Derive a better summary from first readable line if still default ---
                if not summary_found:
                    lines = ai_output.strip().split('\n')
                    for line in lines:
                        line = line.strip()
                        if line and not line.startswith('*') and not line.startswith('#') and len(line) > 10:
                            cleaned_line = line.replace('**', '').strip()
                            summary = cleaned_line if len(cleaned_line) <= 100 else cleaned_line[:100] + "..."
                            logger.info("Summary from first readable line: %s", summary)
                            break

                # --- Subcategory derivation ---
                subcategory = "General Inquiry"
                if category == "HR / Leave":
                    if "maternity" in ai_output.lower():
                        subcategory = "Maternity Leave Request"
                    elif "parental" in ai_output.lower():
                        subcategory = "Parental Leave Request"
                    elif "sick" in ai_output.lower() or "medical" in ai_output.lower():
                        subcategory = "Sick Leave"
                    elif "vacation" in ai_output.lower() or "holiday" in ai_output.lower():
                        subcategory = "Vacation Request"
                    elif "adoption" in ai_output.lower():
                        subcategory = "Adoption Leave"
                elif category == "Payroll / Salary":
                    if "incorrect" in ai_output.lower() or "underpayment" in ai_output.lower() or "overpayment" in ai_output.lower():
                        subcategory = "Incorrect Salary Payment"
                    elif "delayed" in ai_output.lower() or "late" in ai_output.lower():
                        subcategory = "Delayed Salary"
                    elif "tax" in ai_output.lower() or "deduction" in ai_output.lower():
                        subcategory = "Tax Deduction Error"
                    elif "benefit" in ai_output.lower():
                        subcategory = "Benefit Related"
                elif category == "Finance / Invoice":
                    if "disputed" in ai_output.lower() or "dispute" in ai_output.lower():
                        subcategory = "Disputed Invoice Resolution"
                    elif "early payment" in ai_output.lower() or "discount" in ai_output.lower():
                        subcategory = "Early Payment Discount"
                    elif "late" in ai_output.lower() or "overdue" in ai_output.lower():
                        subcategory = "Late Invoice"
                elif category == "Technology / VPN":
                    if "connection" in ai_output.lower() or "connect" in ai_output.lower():
                        subcategory = "VPN Connection Failure"
                    elif "authentication" in ai_output.lower() or "login" in ai_output.lower() or "password" in ai_output.lower():
                        subcategory = "VPN Authentication Failure"
                    elif "configuration" in ai_output.lower() or "setup" in ai_output.lower():
                        subcategory = "VPN Configuration Issue"

                # Also check ticket content for subcategory when AI output had no signal
                if subcategory == "General Inquiry":
                    ticket_lower = combined_ticket_text.lower()
                    if category == "Technology / VPN":
                        if "connection" in ticket_lower or "unable to connect" in ticket_lower:
                            subcategory = "VPN Connection Failure"
                        elif "authentication" in ticket_lower or "login" in ticket_lower:
                            subcategory = "VPN Authentication Failure"
                        elif "configuration" in ticket_lower or "setup" in ticket_lower:
                            subcategory = "VPN Configuration Issue"

                clean_json_str = json.dumps({
                    "category": category,
                    "subcategory": subcategory,
                    "summary": summary,
                    "confidence": confidence
                })
                logger.info("Fallback JSON constructed: %s", clean_json_str)

        # --- Parse the final JSON ---
        try:
            ai_data = json.loads(clean_json_str)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI output as JSON: {e}")
            logger.error(f"AI output was: {ai_output[:200]}...")
            # Last resort — still use ticket-content inference for category
            inferred = infer_category_from_ticket(combined_ticket_text) or "HR / Leave"
            ai_data = {
                "category": inferred,
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

        # --- Final safety check: validate AI-returned category against ticket content ---
        # FIX 2 & 3: infer_category_from_ticket requires min_score=2 and a 1.5×
        # dominance ratio before returning a category, so it only fires on clear,
        # unambiguous evidence.
        #
        # FIX 4: when the keyword inference AGREES with the AI, treat it as
        # corroborating evidence and apply a confidence floor of 0.90. This is what
        # was causing an obvious VPN ticket to stay at 0.7 — the agreement path
        # previously did nothing, leaving the AI's low score untouched.
        ticket_inferred_category = infer_category_from_ticket(combined_ticket_text)
        if ticket_inferred_category and category == ticket_inferred_category:
            if confidence < 0.90:
                logger.info(
                    "[Category Validation] Ticket content confirms AI category '%s'. "
                    "Lifting confidence from %.2f → 0.90.",
                    category, confidence
                )
                confidence = 0.90
        elif ticket_inferred_category and category != ticket_inferred_category:
            logger.warning(
                "[Category Validation] AI returned category '%s' but ticket content "
                "strongly indicates '%s'. Overriding.",
                category, ticket_inferred_category
            )
            category = ticket_inferred_category
            confidence = min(confidence, 0.85)
        # If infer_category_from_ticket returned None (ambiguous / low signal),
        # neither branch fires and the AI's confidence is left as-is.

        # Step 6A: Dynamic Team Lead Mapping Look-up
        cursor.execute("SELECT TeamLead FROM TeamLeadMapping WHERE Category = ?", category)
        mapping_row = cursor.fetchone()
        assigned_tl = mapping_row[0] if mapping_row else "TL_GENERAL"

        cursor.execute(
            """
            UPDATE Tickets 
            SET Category = ?, Subcategory = ?, Summary = ?, AssignedTeamLead = ?, Status = 'Processed'
            WHERE TicketID = ?
            """,
            category, subcategory, summary, assigned_tl, ticket_id
        )
        conn.commit()
        add_ticket_note(ticket_id, 'SystemNote', 'Ticket processed and resolved.', None)

        logger.info(f"\n--- [Pipeline Complete] Summary for {ticket_id} ---")
        logger.info(f"Categorized As: {category} ({subcategory})")
        logger.info(f"Routed Directly To: {assigned_tl}")
        logger.info(f"Brief Summary: {summary}")
        logger.info(f"AI Confidence Score: {confidence:.2f}")

        try:
            cursor.execute("SELECT Notified FROM Tickets WHERE TicketID = ?", ticket_id)
            row = cursor.fetchone()
            notified = row[0] if row else 0
        except pyodbc.ProgrammingError:
            cursor.execute("ALTER TABLE Tickets ADD Notified BIT DEFAULT 0")
            conn.commit()
            notified = 0

        if notified == 1:
            logger.info("Notification already sent for ticket %s, skipping", ticket_id)
        else:
            if confidence >= 0.9:
                send_confidence_email(ticket_id, category, subcategory, summary, confidence)
                cursor.execute("UPDATE Tickets SET Notified = 1 WHERE TicketID = ?", ticket_id)
                conn.commit()
                send_ai_response_email(ticket_id, category, subcategory, summary, confidence, assigned_tl, sender, matched_corporate_sop)
                add_ticket_note(ticket_id, 'SystemNote', 'Solution email sent via knowledge base.', None)

        cursor.close()
        conn.close()

    except Exception as e:
        logger.error(f"AI Processing Error: {e}")


# ==========================================
# HELPER FUNCTIONS
# ==========================================

def generate_user_facing_solution(ticket_summary, category, subcategory, sop_text):
    """
    Use Ollama to rewrite internal SOP content into a friendly, actionable
    self-service guide written directly for the end user.

    The raw SOP text is written for internal staff / team leads. This function
    transforms it into plain-English steps the user can follow themselves,
    with a warm tone and no internal jargon.

    Falls back to a generic holding message if Ollama is unavailable.
    """
    no_sop_messages = [
        "No specific matching corporate SOP found.",
        "No specific corporate SOP found."
    ]
    sop_available = sop_text and not any(msg in sop_text for msg in no_sop_messages)

    if sop_available:
        prompt = (
            "You are a friendly customer support agent writing a solution email directly to an employee.\n"
            "Your job is to rewrite the internal SOP guidance below into a clear, warm, step-by-step reply "
            "that the employee can follow themselves to resolve their issue.\n\n"
            "Rules:\n"
            "- Write directly to the employee using 'you' (e.g. 'Please try the following steps').\n"
            "- Do NOT mention internal terms like 'SOP', 'Team Lead', 'routing', 'policy document', or 'handling instructions'.\n"
            "- Do NOT include any internal staff notes or escalation procedures.\n"
            "- Use simple, plain English. No jargon.\n"
            "- Format as a short intro sentence, then a numbered step-by-step list (max 6 steps).\n"
            "- End with one reassuring sentence telling them to reply if the steps don't resolve the issue.\n"
            "- Keep the total response under 200 words.\n\n"
            f"Issue type: {category} — {subcategory}\n"
            f"Issue summary: {ticket_summary}\n\n"
            f"Internal SOP guidance:\n{sop_text}\n\n"
            "User-facing solution:"
        )
    else:
        # No SOP matched — ask the model to give a best-effort answer
        # from its general knowledge for this category and issue type.
        prompt = (
            "You are a friendly customer support agent writing a solution email directly to an employee.\n"
            "No specific policy document was found, so use your general knowledge to give helpful first steps.\n\n"
            "Rules:\n"
            "- Write directly to the employee using 'you'.\n"
            "- Provide 3-5 practical self-service steps they can try right now.\n"
            "- Use simple, plain English. No jargon.\n"
            "- Format as a short intro sentence, then a numbered list.\n"
            "- End by letting them know a support agent will follow up if the steps don't help.\n"
            "- Keep the total response under 180 words.\n\n"
            f"Issue type: {category} — {subcategory}\n"
            f"Issue summary: {ticket_summary}\n\n"
            "User-facing solution:"
        )

    try:
        logger.info(f" -> [Email] Generating user-facing solution via Ollama (sop_available={sop_available})...")
        response = ollama.chat(
            model=config['ollama']['model'],
            messages=[{'role': 'user', 'content': prompt}],
            options={'temperature': 0.3}
        )
        solution = response['message']['content'].strip()
        logger.debug(f"    [Email] User-facing solution generated ({len(solution)} chars)")
        return solution
    except Exception as e:
        logger.warning(f"    [Email] Ollama unavailable for solution rewrite: {e}. Using fallback.")
        return (
            "Please try the following steps to resolve your issue:\n\n"
            "1. Check that your details and credentials are correct.\n"
            "2. Restart any relevant applications or systems.\n"
            "3. If the issue persists, reply to this email with any error messages or screenshots.\n\n"
            "A support agent will follow up with you shortly."
        )


def send_ai_response_email(ticket_id, category, subcategory, summary, confidence, assigned_tl, sender, matched_corporate_sop):
    """
    Send a solution email to the original ticket sender.
    The solution is rewritten by Ollama into a user-friendly, actionable guide
    rather than a dump of internal SOP text.
    """
    try:
        smtp_config = config['smtp']

        # Rewrite SOP content (or generate best-effort steps) as a
        # friendly user-facing reply rather than internal documentation.
        user_solution = generate_user_facing_solution(
            ticket_summary=summary,
            category=category,
            subcategory=subcategory,
            sop_text=matched_corporate_sop
        )

        msg = f"""Subject: Re: Your Support Request — Ticket {ticket_id}

Hi,

Thanks for reaching out to support. We've looked into your request and have put together some steps to help you resolve this.

{user_solution}

---
Your ticket reference: {ticket_id}
If you need further help, simply reply to this email and our team will pick it up.

— Support Team
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
        raise


def add_ticket_note(ticket_id, note_type, note_body, created_by=None):
    """
    Insert a note into the TicketNotes table.
    """
    try:
        conn = pyodbc.connect(config['database']['conn_str'])
        cursor = conn.cursor()
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
        raise


# ==========================================
# EXECUTION CONTROLLER
# ==========================================

if __name__ == "__main__":
    executor = ThreadPoolExecutor(max_workers=config['processing']['thread_pool_size'])
    try:
        while True:
            new_ticket = fetch_latest_unread_email()
            if new_ticket is None:
                time.sleep(config['processing']['fallback_polling_interval'])
                continue

            logger.info(f"\n--- [Main] Submitted ticket {new_ticket} for asynchronous AI processing ---")
            executor.submit(process_ticket_with_ai, new_ticket)
    except KeyboardInterrupt:
        logger.info("\n--- [Main] Shutting down gracefully... ---")
    finally:
        executor.shutdown(wait=True)
        logger.info("--- [Main] Shutdown complete. ---")
