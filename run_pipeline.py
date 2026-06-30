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
import math

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
            "model": "llama3",
            "embedding_model": "nomic-embed-text"
        },
        "processing": {
            "thread_pool_size": 4,
            "knowledge_base_folder": "knowledge_base",
            "idle_timeout": 30,
            "fallback_polling_interval": 10,
            "semantic_top_k": 2,
            "semantic_min_similarity": 0.55
        },
        "logging": {
            "level": "INFO",
            "file": ""
        },
        "team_leads": [
            {"id": "TL001", "name": "Team Lead 1", "email": "tl001@company.com",
             "categories": ["Payroll / Salary"]},
            {"id": "TL002", "name": "Team Lead 2", "email": "tl002@company.com",
             "categories": ["Finance / Invoice"]},
            {"id": "TL003", "name": "Team Lead 3", "email": "tl003@company.com",
             "categories": ["Technology / VPN"]},
            {"id": "TL004", "name": "Team Lead 4", "email": "tl004@company.com",
             "categories": ["HR / Leave"]}
        ]
    }
    try:
        with open(config_path, 'r') as f:
            user_config = yaml.safe_load(f) or {}
        for key in default_config:
            if key not in user_config:
                user_config[key] = default_config[key]
            elif isinstance(default_config[key], dict) and isinstance(user_config[key], dict):
                merged = dict(default_config[key])
                merged.update(user_config[key])
                user_config[key] = merged
        return user_config
    except FileNotFoundError:
        logging.warning("Config file not found, using default configuration")
        return default_config
    except yaml.YAMLError as e:
        logging.error(f"Error parsing config file: {e}, using default configuration")
        return default_config


config = load_config()

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


def get_team_lead_for_category(category):
    for tl in config.get('team_leads', []):
        if category in tl.get('categories', []):
            return tl
    logger.warning(f"No team lead configured for category '{category}'")
    return None


def send_plain_email(to_address, subject, body):
    try:
        smtp_config = config['smtp']
        msg = f"Subject: {subject}\n\n{body}\n"
        server = smtplib.SMTP(smtp_config['server'], smtp_config['port'])
        server.starttls()
        server.login(smtp_config['email'], smtp_config['app_password'])
        server.sendmail(smtp_config['email'], to_address, msg.encode('utf-8'))
        server.quit()
        logger.info(f"Sent email '{subject}' to {to_address}")
    except Exception as e:
        logger.error(f"Failed to send email '{subject}' to {to_address}: {e}")
        raise


def extract_sender_name(sender):
    name_part = re.split(r'[<@]', sender)[0].strip().strip('"')
    return name_part if name_part else sender


def extract_sender_email(sender):
    match = re.search(r'[\w\.\-+]+@[\w\.\-]+', sender)
    return match.group(0) if match else sender


def get_embedding(text):
    """Get a vector embedding for text using Ollama's embeddings endpoint."""
    try:
        response = ollama.embeddings(
            model=config['ollama'].get('embedding_model', 'nomic-embed-text'),
            prompt=text
        )
        return response.get('embedding')
    except Exception as e:
        logger.debug(f"    [Embedding Error] Failed to get embedding: {e}")
        return None


def cosine_similarity(vec_a, vec_b):
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# In-memory cache of KB document embeddings, keyed by filename.
# Each entry is (mtime, embedding, policy_text) so edited files are
# automatically re-embedded on the next search.
_kb_embedding_cache = {}


def _load_kb_embeddings(kb_folder):
    embeddings = {}
    for filename in os.listdir(kb_folder):
        if not filename.endswith(".txt"):
            continue
        file_path = os.path.join(kb_folder, filename)
        mtime = os.path.getmtime(file_path)
        cached = _kb_embedding_cache.get(filename)
        if cached and cached[0] == mtime:
            embeddings[filename] = (cached[1], cached[2])
            continue
        with open(file_path, "r", encoding="utf-8") as f:
            policy_text = f.read()
        embedding = get_embedding(policy_text)
        if embedding is None:
            continue
        _kb_embedding_cache[filename] = (mtime, embedding, policy_text)
        embeddings[filename] = (embedding, policy_text)
    return embeddings


def semantic_search_policies(kb_folder, ticket_content, top_k=None, min_similarity=None):
    """Rank knowledge-base documents by cosine similarity between their embedding
    and the ticket's embedding, instead of relying on literal word overlap."""
    top_k = top_k if top_k is not None else config['processing'].get('semantic_top_k', 2)
    min_similarity = (
        min_similarity if min_similarity is not None
        else config['processing'].get('semantic_min_similarity', 0.55)
    )

    query_embedding = get_embedding(ticket_content)
    if query_embedding is None:
        logger.debug("    [Semantic Search] No query embedding available; skipping.")
        return ""

    kb_embeddings = _load_kb_embeddings(kb_folder)
    if not kb_embeddings:
        return ""

    scored = []
    for filename, (embedding, policy_text) in kb_embeddings.items():
        score = cosine_similarity(query_embedding, embedding)
        scored.append((score, filename, policy_text))
    scored.sort(key=lambda x: x[0], reverse=True)

    context_payload = ""
    for score, filename, policy_text in scored[:top_k]:
        if score < min_similarity:
            continue
        logger.debug(f"    [*] Semantic match: {filename} (similarity={score:.3f})")
        context_payload += f"\n--- Context Document: {filename} (similarity={score:.2f}) ---\n{policy_text}\n"

    return context_payload


def keyword_search_policies(kb_folder, ticket_content):
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


def search_local_knowledge_base(ticket_content):
    kb_folder = config['processing']['knowledge_base_folder']
    context_payload = ""

    if not os.path.exists(kb_folder):
        logger.info(" -> [Step 6B Notice] Knowledge base folder not found. Skipping context.")
        return "No specific corporate SOP found."

    logger.info(f" -> [Step 6B] Scanning local policy files for relevant context...")

    context_payload = semantic_search_policies(kb_folder, ticket_content)

    if not context_payload:
        logger.debug("    [Semantic Search] No semantic matches above threshold; falling back to keyword search.")
        context_payload = keyword_search_policies(kb_folder, ticket_content)

    return context_payload if context_payload else "No specific matching corporate SOP found."


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


def infer_category_from_ticket(ticket_text, min_score: int = 2, dominance_ratio: float = 1.5):
    ticket_lower = ticket_text.lower()

    raw_scores = {cat: 0 for cat in CATEGORY_KEYWORDS}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in ticket_lower:
                raw_scores[cat] += 1

    normalised_scores = {
        cat: raw_scores[cat] / len(CATEGORY_KEYWORDS[cat])
        for cat in raw_scores
    }

    logger.debug(f"    [Category Raw Scores]        {raw_scores}")
    logger.debug(f"    [Category Normalised Scores] {normalised_scores}")

    sorted_scores = sorted(normalised_scores.items(), key=lambda x: x[1], reverse=True)
    best_cat, best_norm_score = sorted_scores[0]
    second_norm_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0

    if raw_scores[best_cat] < min_score:
        logger.debug(
            f"    [Category Guard] Raw score {raw_scores[best_cat]} < min_score {min_score}. "
            "Returning None (trust the AI)."
        )
        return None

    if second_norm_score > 0 and (best_norm_score / second_norm_score) < dominance_ratio:
        logger.debug(
            f"    [Category Guard] Dominance ratio "
            f"{best_norm_score / second_norm_score:.2f} < {dominance_ratio}. "
            "Returning None (trust the AI)."
        )
        return None

    return best_cat


def clean_text(text):
    if isinstance(text, bytes):
        return text.decode('utf-8', errors='ignore')
    return text


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

        logger.info(f"Found {len(email_ids)} unread email(s). Processing oldest-first...")

        new_ticket_id = None

        for current_email_id in email_ids:
            status, msg_data = mail.fetch(current_email_id, '(RFC822)')

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

                    approval_ticket_id = extract_ticket_id_from_approval_subject(subject)
                    if approval_ticket_id:
                        handle_team_lead_approval_reply(approval_ticket_id, body, sender)
                        logger.info(f"Processed team lead approval reply for ticket {approval_ticket_id} from email {current_email_id.decode()}")
                        mail.store(current_email_id, '+FLAGS', '\\Seen')
                        continue

                    escalation_ticket_id = extract_ticket_id_from_escalation_subject(subject)
                    if escalation_ticket_id:
                        handle_team_lead_escalation_reply(escalation_ticket_id, body, sender)
                        logger.info(f"Processed team lead escalation response for ticket {escalation_ticket_id} from email {current_email_id.decode()}")
                        mail.store(current_email_id, '+FLAGS', '\\Seen')
                        continue

                    reply_ticket_id = extract_ticket_id_from_subject(subject)
                    if reply_ticket_id:
                        add_ticket_note(reply_ticket_id, 'UserReply', body, sender)
                        logger.info(f"Added UserReply note to ticket {reply_ticket_id} from email {current_email_id.decode()}")
                        mail.store(current_email_id, '+FLAGS', '\\Seen')
                        continue

                    ticket_id = f"INC-2026-{random.randint(1000, 9999)}"

                    conn = pyodbc.connect(config['database']['conn_str'])
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO Tickets (TicketID, Subject, Body, Sender, Status) VALUES (?, ?, ?, ?, 'New')",
                        ticket_id, subject, body.strip(), sender
                    )
                    conn.commit()
                    logger.info(f"-> [Step 3] Logged {ticket_id} to Database successfully.")

                    mail.store(current_email_id, '+FLAGS', '\\Seen')
                    cursor.close()
                    conn.close()

                    try:
                        sender_email_addr = extract_sender_email(sender)
                        send_plain_email(
                            to_address=sender_email_addr,
                            subject=f"Ticket created [{ticket_id}]",
                            body=(
                                f"Thank you for contacting support.\n\n"
                                f"Your ticket has been created [{ticket_id}].\n"
                                f"We will get back to you as soon as possible."
                            )
                        )
                    except Exception as e:
                        logger.error(f"Failed to send acknowledgement email for {ticket_id}: {e}")

                    # Only the first newly-created ticket in this batch is handed
                    # back for immediate AI processing; the rest will be picked
                    # up the same way the next time a new ticket is created.
                    if new_ticket_id is None:
                        new_ticket_id = ticket_id

        mail.logout()
        return new_ticket_id

    except Exception as e:
        logger.error(f"Ingestion Error: {e}")
        return None
def generate_user_facing_solution(ticket_summary, category, subcategory, sop_text,sender=None):
    sender_name = None
    if sender:
        name_part = re.split(r'[<@]', sender)[0].strip().strip('"')
        if name_part:
            sender_name = name_part.split()[0].capitalize()

    greeting = f"Hi {sender_name}," if sender_name else "Hi there,"
    no_sop_messages = [
        "No specific matching corporate SOP found.",
        "No specific corporate SOP found."
    ]
    sop_available = sop_text and not any(msg in sop_text for msg in no_sop_messages)

    if sop_available:
        prompt = (
            f"- Start with exactly this greeting on its own line, then a blank line: '{greeting}'\n"
"- End with exactly: 'Best regards,\\nCustomer Support Team'\n"
            "You are a friendly customer support agent writing a solution email directly to an employee.\n"
            "Your job is to rewrite the internal SOP guidance below into a clear, warm, step-by-step reply "
            "that the employee can follow themselves to resolve their issue.\n\n"
            "Rules:\n"
            "- Use Dates and Times mentioned in the original email to formulate a timeline of events in your response.\n"
            "- Dont NOT use Dear or Hi — just start with a friendly sentence.\n"
            "- Write directly to the employee using 'you' (e.g. 'Please try the following steps').\n"
            "- Do NOT mention internal terms like 'SOP', 'Team Lead', 'routing', 'policy document', or 'handling instructions'.\n"
            "- Do NOT include any internal staff notes or escalation procedures.\n"
            "- Use simple, plain English. No jargon.\n"
            "- Format as a short intro sentence, then a numbered step-by-step list (max 6 steps).\n"
            "- End with one reassuring sentence telling them to reply if the steps don't resolve the issue.\n"
            "- Keep the total response under 200 words.\n\n"
            "- Dont NOT use best regards or email closings at all"
            f"Issue type: {category} — {subcategory}\n"
            f"Issue summary: {ticket_summary}\n\n"
            f"Internal SOP guidance:\n{sop_text}\n\n"
            "User-facing solution:"
        )
    else:
        prompt = (
            f"- Start with exactly this greeting on its own line, then a blank line: '{greeting}'\n"
"- End with exactly: 'Best regards,\\nCustomer Support Team'\n"
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
            "User-facing solution: "
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
    try:
        smtp_config = config['smtp']

        user_solution = generate_user_facing_solution(
            ticket_summary=summary,
            category=category,
            subcategory=subcategory,
            sop_text=matched_corporate_sop,
            sender=sender
        )

        msg = f"""Subject: Re: Your Support Request Ticket {ticket_id}



{user_solution}

---
Your ticket reference: {ticket_id}
If you need further help, simply reply to this email and our team will pick it up.

— AI Ticketing Service
"""
        logger.debug(f"Attempting to send solution email for ticket {ticket_id} to {sender}")
        server = smtplib.SMTP(smtp_config['server'], smtp_config['port'])
        server.starttls()
        server.login(smtp_config['email'], smtp_config['app_password'])
        server.sendmail(smtp_config['email'], sender, msg.encode('utf-8'))
        server.quit()
        logger.info(f"Sent solution email for ticket {ticket_id} (confidence: {confidence:.2f})")
        return user_solution
    except Exception as e:
        logger.error(f"Failed to send solution email for ticket {ticket_id}: {e}")
        raise


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

        matched_corporate_sop = search_local_knowledge_base(combined_ticket_text)

        MAX_SOP_CHARS = 4000
        if len(matched_corporate_sop) > MAX_SOP_CHARS:
            logger.warning(
                "Matched SOP context is %d chars, truncating to %d to avoid crowding out the "
                "model's context window.",
                len(matched_corporate_sop), MAX_SOP_CHARS
            )
            matched_corporate_sop = matched_corporate_sop[:MAX_SOP_CHARS] + "\n...[truncated]"

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

        VALID_CATEGORIES = {'Payroll / Salary', 'Finance / Invoice', 'Technology / VPN', 'HR / Leave'}
        MAX_AI_ATTEMPTS = 2
        ai_data = None
        ai_output = ""

        for attempt in range(1, MAX_AI_ATTEMPTS + 1):
            logger.info(
                "Querying local Llama 3 model with injected policy context... (attempt %d/%d)",
                attempt, MAX_AI_ATTEMPTS
            )
            response = ollama.chat(
                model=config['ollama']['model'],
                messages=[
                    {'role': 'system', 'content': system_instruction},
                    {'role': 'user', 'content': user_payload}
                ],
                format='json',
                # num_ctx is bumped from Ollama's 2048-token default so the injected
                # SOP context + email body can't silently push the instructions/schema
                # out of the model's effective context window.
                options={'temperature': 0.0, 'num_ctx': 8192}
            )

            ai_output = response['message']['content']
            print(f"Raw AI Output Received (attempt {attempt}): {ai_output}")

            clean_json_str = ai_output.strip()
            if "```json" in clean_json_str:
                clean_json_str = clean_json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in clean_json_str:
                clean_json_str = clean_json_str.split("```")[1].split("```")[0].strip()
            else:
                start_idx = clean_json_str.find("{")
                end_idx = clean_json_str.rfind("}") + 1
                if start_idx != -1 and end_idx != 0 and end_idx > start_idx:
                    clean_json_str = clean_json_str[start_idx:end_idx]

            try:
                parsed = json.loads(clean_json_str)
                if isinstance(parsed, dict) and parsed.get('category') in VALID_CATEGORIES:
                    ai_data = parsed
                    logger.info("AI returned usable JSON on attempt %d.", attempt)
                    break
                else:
                    logger.warning(
                        "Attempt %d: AI returned syntactically valid JSON but missing/invalid 'category': %s",
                        attempt, clean_json_str
                    )
            except json.JSONDecodeError as e:
                logger.warning("Attempt %d: AI output was not valid JSON: %s", attempt, e)

            if attempt < MAX_AI_ATTEMPTS:
                logger.info("Retrying Ollama call (attempt %d/%d)...", attempt + 1, MAX_AI_ATTEMPTS)

        if ai_data is None:
            clean_json_str = ai_output.strip()
            start_idx = clean_json_str.find("{")
            end_idx = clean_json_str.rfind("}") + 1
            if start_idx != -1 and end_idx != 0 and end_idx > start_idx:
                clean_json_str = clean_json_str[start_idx:end_idx]
            else:
                logger.warning(
                    "No JSON found in AI output after %d attempt(s), attempting fallback extraction",
                    MAX_AI_ATTEMPTS
                )
                logger.info("Fallback extraction started for ticket %s", ticket_id)

                category = None
                subcategory = "General Inquiry"
                summary = "Support ticket processed"
                confidence = 0.5
                routing_found = False
                summary_found = False

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

                summary_match = re.search(r'\*\*Summary\*\*:\s*([^\n]+)', ai_output, re.IGNORECASE)
                if not summary_match:
                    summary_match = re.search(r'Summary\s*[:]\s*([^\n]+)', ai_output, re.IGNORECASE)
                if summary_match:
                    extracted_summary = summary_match.group(1).strip()
                    logger.info("Extracted summary: %s", extracted_summary)
                    summary = extracted_summary
                    summary_found = True

                if routing_found and summary_found:
                    confidence = 0.9
                    logger.info("Both routing group and summary found; confidence set to 0.9")
                elif routing_found or summary_found:
                    confidence = 0.7
                    logger.info("Only one of routing/summary found; confidence set to 0.7")

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

                ticket_inferred_category = infer_category_from_ticket(combined_ticket_text)
                logger.info("Category inferred from raw ticket content: %s", ticket_inferred_category)

                if ticket_inferred_category and category != ticket_inferred_category:
                    logger.warning(
                        "Category mismatch: AI fallback said '%s' but ticket content indicates '%s'. "
                        "Overriding with ticket-content inference.",
                        category, ticket_inferred_category
                    )
                    category = ticket_inferred_category
                    confidence = min(confidence, 0.85)

                if not category:
                    logger.warning("No category signal found anywhere; defaulting to HR / Leave")
                    category = "HR / Leave"
                    confidence = 0.5

                NARRATION_PREFIXES = (
                    "i will", "i'll", "let me", "based on", "here is", "here's",
                    "to summarize", "in order to", "as an ai", "sure,", "okay,",
                    "i am going to", "i'm going to", "first,", "analyzing"
                )
                if not summary_found:
                    lines = ai_output.strip().split('\n')
                    for line in lines:
                        line = line.strip()
                        if not line or line.startswith('*') or line.startswith('#') or len(line) <= 10:
                            continue
                        cleaned_line = line.replace('**', '').strip()
                        if cleaned_line.lower().startswith(NARRATION_PREFIXES):
                            logger.debug("Skipping narration line for summary: %s", cleaned_line)
                            continue
                        summary = cleaned_line if len(cleaned_line) <= 100 else cleaned_line[:100] + "..."
                        logger.info("Summary from first readable line: %s", summary)
                        break
                    else:
                        summary = "Support ticket processed; AI did not return a usable summary."
                        logger.warning("No non-narration line found for fallback summary; using generic placeholder.")

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

                ai_data = {
                    "category": category,
                    "subcategory": subcategory,
                    "summary": summary,
                    "confidence": confidence
                }

            if ai_data is None:
                try:
                    parsed = json.loads(clean_json_str)
                    if isinstance(parsed, dict) and parsed.get('category') in VALID_CATEGORIES:
                        ai_data = parsed
                except json.JSONDecodeError:
                    pass

        if ai_data is None:
            logger.error(f"Failed to parse usable AI output after fallback extraction.")
            logger.error(f"AI output was: {ai_output[:200]}...")
            inferred = infer_category_from_ticket(combined_ticket_text) or "HR / Leave"
            ai_data = {
                "category": inferred,
                "subcategory": "General Inquiry",
                "summary": "Ticket processed via fallback",
                "confidence": 0.5
            }

        category = ai_data.get('category')
        subcategory = ai_data.get('subcategory') or "General Inquiry"
        summary = ai_data.get('summary') or "Support ticket processed; AI did not return a usable summary."
        confidence = ai_data.get('confidence')
        try:
            confidence = float(confidence) if confidence is not None else 0.0
        except (ValueError, TypeError):
            confidence = 0.0

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

        team_lead = get_team_lead_for_category(category)
        assigned_tl = team_lead['id'] if team_lead else "TL_GENERAL"
        tl_email = team_lead['email'] if team_lead else None

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

        sender_name = extract_sender_name(sender)
        sender_email_addr = extract_sender_email(sender)

        if tl_email:
            try:
                send_plain_email(
                    to_address=tl_email,
                    subject=f"New Ticket Created [{ticket_id}]",
                    body=(
                        f"Employee {sender_name} with mail ID {sender_email_addr} "
                        f"has created [{ticket_id}].\n\n"
                        f"Category: {category} ({subcategory})\n"
                        f"Summary: {summary}\n"
                        f"AI Confidence: {confidence:.2f}\n\n"
                        f"---------- Forwarded message ----------\n"
                        f"From: {sender}\n"
                        f"Subject: {subject}\n\n"
                        f"{email_body.strip()}"
                    )
                )
            except Exception as e:
                logger.error(f"Failed to send ticket-creation notice to team lead for {ticket_id}: {e}")
        else:
            logger.warning(f"No team lead email found for category '{category}'; skipping TL creation notice.")

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
                cursor.execute("UPDATE Tickets SET Notified = 1 WHERE TicketID = ?", ticket_id)
                conn.commit()
                send_confidence_email(ticket_id, category, subcategory, summary, confidence)
                user_ai_solution = send_ai_response_email(ticket_id, category, subcategory, summary, confidence, assigned_tl, sender, matched_corporate_sop)
                cursor.execute("UPDATE Tickets SET Status = 'Resolved' WHERE TicketID = ?", ticket_id)
                conn.commit()
                add_ticket_note(ticket_id, 'SystemNote', 'Solution email sent via knowledge base.', None)

                if tl_email:
                    try:
                        send_plain_email(
                            to_address=tl_email,
                            subject=f"{ticket_id}'s solution produced and sent to {sender_email_addr}",
                            body=(
                                f"{ticket_id}'s solution produced and sent to {sender_email_addr}.\n\n"
                                f"{user_ai_solution}\n\n"
                                f"Category: {category} ({subcategory})\n"
                                f"Summary: {summary}\n"
                                f"AI Confidence: {confidence:.2f}"
                            )
                        )
                    except Exception as e:
                        logger.error(f"Failed to send auto-resolution notice to team lead for {ticket_id}: {e}")

            elif 0.7 <= confidence < 0.9:
                user_solution = generate_user_facing_solution(
                    ticket_summary=summary,
                    category=category,
                    subcategory=subcategory,
                    sop_text=matched_corporate_sop,
                    sender=sender
                )
                add_ticket_note(ticket_id, 'PendingApproval', user_solution, None)
                cursor.execute("UPDATE Tickets SET Notified = 1, Status = 'PendingApproval' WHERE TicketID = ?", ticket_id)
                conn.commit()

                if tl_email:
                    try:
                        send_plain_email(
                            to_address=tl_email,
                            subject=f"Approval Needed: Solution for {ticket_id}",
                            body=(
                                f"{ticket_id} from {sender_email_addr} solution produced:\n\n"
                                f"{user_solution}\n\n"
                                f"waiting for approval. Reply Approved to approve, Reply Reject to Reject."
                            )
                        )
                    
                    except Exception as e:
                        logger.error(f"Failed to send approval-request email to team lead for {ticket_id}: {e}")
                else:
                    logger.warning(f"No team lead email found for category '{category}'; cannot request approval for {ticket_id}.")

            else:
                cursor.execute("UPDATE Tickets SET Notified = 1, Status = 'Escalated' WHERE TicketID = ?", ticket_id)
                conn.commit()
                add_ticket_note(ticket_id, 'SystemNote', 'Escalated to team lead — low AI confidence.', None)

                if tl_email:
                    try:
                        send_plain_email(
                            to_address=tl_email,
                            subject=f"Escalated Ticket: {ticket_id}",
                            body=(
                                f"{ticket_id} from {sender_email_addr} has been escalated to you "
                                f"due to low AI confidence ({confidence:.2f}).\n\n"
                                f"Category: {category} ({subcategory})\n"
                                f"Summary: {summary}\n\n"
                                f"Please review and respond to the employee directly."
                            )
                        )
                    except Exception as e:
                        logger.error(f"Failed to send escalation email to team lead for {ticket_id}: {e}")

        cursor.close()
        conn.close()

    except Exception as e:
        logger.error(f"AI Processing Error: {e}")




def add_ticket_note(ticket_id, note_type, note_body, created_by=None):
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
    match = re.search(r'Re:\s*Solution\s+for\s+Ticket\s+(INC-\d{4}-\d{4})', subject, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def extract_ticket_id_from_approval_subject(subject):
    match = re.search(r'Approval\s+Needed:\s*Solution\s+for\s+(INC-\d{4}-\d{4})', subject, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def extract_ticket_id_from_escalation_subject(subject):
    match = re.search(r'Escalated\s+Ticket\s*:\s*(INC-\d{4}-\d{4})', subject, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def extract_lead_message(body):
    """
    Pull the team lead's own written reply out of an email body, stripping
    quoted thread history (e.g. 'On ... wrote:' / '----- Original Message -----')
    and any standalone 'Approved'/'Reject' decision line, leaving just the
    free-text response (if any) they wrote to be forwarded to the employee.
    """
    quote_split = re.split(
        r'\r?\nOn .{0,80} wrote:|\r?\n-{2,}\s*Original Message\s*-{2,}|\r?\n_{5,}',
        body,
        maxsplit=1,
        flags=re.IGNORECASE
    )
    main_text = quote_split[0]

    cleaned_lines = []
    for line in main_text.splitlines():
        stripped_line = re.sub(r'^>+\s*', '', line).strip()
        if not stripped_line:
            continue
        if re.fullmatch(r'(approved|reject(ed)?)[.!]?', stripped_line, re.IGNORECASE):
            continue
        cleaned_lines.append(stripped_line)

    return '\n'.join(cleaned_lines).strip()


def handle_team_lead_approval_reply(ticket_id, body, sender):
    # Only look at the team lead's own typed reply, not the quoted original
    # message (which always contains the literal words "Approved"/"Reject"
    # in the "Reply Approved to approve, Reply Reject to Reject" instructions).
    lead_only_text = extract_lead_message(body)
    decision_source = lead_only_text if lead_only_text else body
    # Remove the boilerplate "Reply Approved to approve, Reply Reject to
    # Reject." instruction line (often retained in quoted/forwarded text)
    # so it can't be mistaken for the lead's own decision.
    decision_source = re.sub(
        r'reply\s+approved\s+to\s+approve,?\s+reply\s+reject\s+to\s+reject\.?',
        '', decision_source, flags=re.IGNORECASE
    )
    body_lower = decision_source.lower()
    decision = None
    if re.search(r'\bapproved\b', body_lower):
        decision = 'Approved'
    elif re.search(r'\breject(ed)?\b', body_lower):
        decision = 'Rejected'

    if decision is None:
        logger.warning(f"Team lead reply for {ticket_id} did not contain 'Approved' or 'Reject'; ignoring.")
        return

    try:
        conn = pyodbc.connect(config['database']['conn_str'])
        cursor = conn.cursor()
        cursor.execute(
            "SELECT Category, Subcategory, Summary, AssignedTeamLead, Sender FROM Tickets WHERE TicketID = ?",
            ticket_id
        )
        row = cursor.fetchone()
        if not row:
            logger.warning(f"Approval reply received for unknown ticket {ticket_id}")
            cursor.close()
            conn.close()
            return
        category, subcategory, summary, assigned_tl, employee_sender = row

        cursor.execute(
            "SELECT NoteBody FROM TicketNotes WHERE TicketID = ? AND NoteType = 'PendingApproval' ORDER BY NoteID DESC",
            ticket_id
        )
        note_row = cursor.fetchone()
        user_solution = note_row[0] if note_row else None

        if decision == 'Approved':
            if user_solution:
                employee_email_addr = extract_sender_email(employee_sender)
                send_plain_email(
                    to_address=employee_email_addr,
                    subject=f"Re: Solution for Ticket {ticket_id}",
                    body=(
                        f"{user_solution}\n\n"
                        f"---\nYour ticket reference: {ticket_id}"
                    )
                )
                cursor.execute("UPDATE Tickets SET Status = 'Resolved' WHERE TicketID = ?", ticket_id)
                conn.commit()
                add_ticket_note(ticket_id, 'SystemNote', 'Team lead approved solution; sent to employee.', sender)
                logger.info(f"Team lead approved {ticket_id}; solution sent to {employee_email_addr}.")
            else:
                logger.error(f"No stored solution found for approved ticket {ticket_id}; cannot send.")
        else:
            lead_message = extract_lead_message(body)
            employee_email_addr = extract_sender_email(employee_sender)

            if lead_message:
                send_plain_email(
                    to_address=employee_email_addr,
                    subject=f"Re: Solution for Ticket {ticket_id}",
                    body=(
                        f"{lead_message}\n\n"
                        f"---\nYour ticket reference: {ticket_id}"
                    )
                )
                cursor.execute("UPDATE Tickets SET Status = 'Resolved' WHERE TicketID = ?", ticket_id)
                conn.commit()
                add_ticket_note(
                    ticket_id, 'SystemNote',
                    'Team lead rejected suggested solution and sent a custom response to employee.', sender
                )
                logger.info(
                    f"Team lead rejected suggested solution for {ticket_id}; "
                    f"custom response forwarded to {employee_email_addr}."
                )
            else:
                cursor.execute("UPDATE Tickets SET Status = 'Rejected' WHERE TicketID = ?", ticket_id)
                conn.commit()
                add_ticket_note(ticket_id, 'SystemNote', 'Team lead rejected suggested solution; not sent to employee.', sender)
                logger.info(f"Team lead rejected {ticket_id}; no email sent to employee.")

        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to process team lead approval reply for {ticket_id}: {e}")


def handle_team_lead_escalation_reply(ticket_id, body, sender):
    lead_message = extract_lead_message(body)

    if not lead_message:
        logger.warning(
            f"Team lead escalation reply for {ticket_id} contained no usable response text; ignoring."
        )
        return

    try:
        conn = pyodbc.connect(config['database']['conn_str'])
        cursor = conn.cursor()
        cursor.execute(
            "SELECT Category, Subcategory, Summary, Sender FROM Tickets WHERE TicketID = ?",
            ticket_id
        )
        row = cursor.fetchone()
        if not row:
            logger.warning(f"Escalation reply received for unknown ticket {ticket_id}")
            cursor.close()
            conn.close()
            return
        category, subcategory, summary, employee_sender = row
        employee_email_addr = extract_sender_email(employee_sender)

        send_plain_email(
            to_address=employee_email_addr,
            subject=f"Re: Solution for Ticket {ticket_id}",
            body=(
                f"{lead_message}\n\n"
                f"---\nYour ticket reference: {ticket_id}"
            )
        )
        cursor.execute("UPDATE Tickets SET Status = 'Resolved' WHERE TicketID = ?", ticket_id)
        conn.commit()
        add_ticket_note(
            ticket_id, 'SystemNote',
            'Team lead responded to escalation; response forwarded to employee.', sender
        )
        logger.info(
            f"Team lead's escalation response for {ticket_id} forwarded to {employee_email_addr}."
        )

        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to process team lead escalation reply for {ticket_id}: {e}")


def send_confidence_email(ticket_id, category, subcategory, summary, confidence):
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
