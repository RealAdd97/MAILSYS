# AI Email Ticketing Service

A Python automation pipeline that ingests incoming support emails from a Gmail inbox via IMAP, persists them to a SQL Server database, enriches each ticket with AI categorization and summarization using a local Llama 3 model through Ollama, and routes the ticket to the appropriate team lead.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Database Setup](#database-setup)
- [Knowledge Base](#knowledge-base)
- [Running the Pipeline](#running-the-pipeline)
- [Testing the Pipeline](#testing-the-pipeline)
- [Confidence-Based Routing](#confidence-based-routing)
- [Project Structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Security Notes](#security-notes)

---

## How It Works

The pipeline runs continuously and processes each new email in two phases:

**Phase 1 — Email Ingestion (main thread)**
1. Connects to Gmail via IMAP and fetches unread messages.
2. Generates a ticket ID in the format `INC-2026-XXXX`.
3. Inserts the ticket into the `Tickets` table on SQL Server.
4. Marks the email as `Seen` so it is not reprocessed.
5. Sends an acknowledgement email to the sender.

**Phase 2 — Asynchronous AI Processing (thread pool)**
1. Scans the `knowledge_base/` folder for policy documents relevant to the ticket (semantic search via embeddings, with keyword search as a fallback).
2. Sends the email body plus matched policy context to a local `llama3` model through Ollama, requesting strict JSON output with `category`, `subcategory`, `summary`, and `confidence`.
3. Validates the AI's category against a keyword scan of the email. If they match, confidence is lifted to 0.90. If they disagree, confidence is capped at 0.85.
4. Looks up the team lead for the resolved category and updates the ticket to `Status = 'Processed'`.
5. Routes the ticket based on confidence (see [Confidence-Based Routing](#confidence-based-routing)).

AI processing runs in a thread pool (default 4 workers), so multiple tickets can be processed concurrently. Each AI task uses its own database connection for thread safety.

---

## Prerequisites

Before installing, make sure you have all of the following available on the machine that will run the pipeline.

### 1. Python 3.9 or newer

```bash
python --version
```

If Python is not installed, download it from [python.org](https://www.python.org/downloads/) and make sure to check **"Add Python to PATH"** during installation.

### 2. SQL Server (local instance)

The default connection string targets `localhost\SQLEXPRESS`. If you do not have SQL Server installed:

- **Windows**: Install [SQL Server Express](https://www.microsoft.com/en-us/sql-server/sql-server-downloads) (the free edition is fine).
- **ODBC Driver**: Install [Microsoft ODBC Driver 17 for SQL Server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server). The pipeline uses `pyodbc`, which requires this driver.

Verify the driver is registered by opening the ODBC Data Source Administrator (`odbcad32.exe`) and checking the **Drivers** tab. You should see `ODBC Driver 17 for SQL Server`.

### 3. Ollama (local LLM runtime)

The pipeline calls a local `llama3` model through the Ollama Python client. Install Ollama from [ollama.com](https://ollama.com/download), then pull the required models:

```bash
ollama pull llama3
ollama pull nomic-embed-text
```

`nomic-embed-text` is the embedding model used for semantic search across the knowledge base. If you change either model, update `config.yaml` to match.

Confirm Ollama is running:

```bash
ollama list
```

You should see both `llama3` and `nomic-embed-text` in the output.

### 4. A Gmail account with 2-Step Verification enabled

You will need an **app password**, not your regular account password. To create one:

1. Enable 2-Step Verification on your Google account.
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Generate an app password for "Mail" / "Other device".
4. Copy the 16-character password — you will paste it into `config.yaml`.

> The app password will look something like `abcd efgh ijkl mnop`.

---

## Installation

### 1. Clone or download the project

```bash
cd "C:\path\to\projects"
git clone <repository-url> "Email Ticketing Service"
cd "Email Ticketing Service"
```

If you downloaded a ZIP, extract it and `cd` into the extracted folder.

### 2. Create a virtual environment (recommended)

```bash
python -m venv .venv
```

Activate it:

- **Windows (PowerShell)**:
  ```powershell
  .venv\Scripts\Activate.ps1
  ```
- **Windows (Command Prompt)**:
  ```cmd
  .venv\Scripts\activate.bat
  ```
- **macOS / Linux**:
  ```bash
  source .venv/bin/activate
  ```

### 3. Install dependencies

```bash
pip install pyodbc ollama pyyaml
```

These are the only third-party packages required. The standard library modules (`imaplib`, `email`, `json`, `os`, `random`, `smtplib`, `re`, `math`, `concurrent.futures`, `logging`, `datetime`) are bundled with Python.

---

## Configuration

All runtime configuration is read from `config.yaml` at startup. Open the file and replace the placeholder values with your own.

### IMAP section

```yaml
imap:
  server: "imap.gmail.com"
  email: "your.address@gmail.com"
  app_password: "your 16-char app password"
```

The pipeline logs into this mailbox and watches the **Inbox** for unread messages.

### Database section

```yaml
database:
  conn_str: "Driver={ODBC Driver 17 for SQL Server};Server=localhost\\SQLEXPRESS;Database=SupportAutomation;Trusted_Connection=yes;"
```

If your SQL Server instance has a different name, replace `localhost\SQLEXPRESS`. If you use SQL authentication instead of Windows authentication, change the connection string to include `UID=...;PWD=...;`.

### Ollama section

```yaml
ollama:
  model: "llama3"
  embedding_model: "nomic-embed-text"
```

`model` is the chat model used for categorization and summarization. `embedding_model` is used for semantic search of the knowledge base.

### SMTP section

```yaml
smtp:
  server: "smtp.gmail.com"
  port: 587
  email: "your.address@gmail.com"
  app_password: "your 16-char app password"
  notification_email: "your.address@gmail.com"
```

The pipeline sends three kinds of email:

| When                              | Sent to                         |
| --------------------------------- | ------------------------------- |
| A new ticket is created           | The original sender (ack)       |
| AI categorizes a ticket           | The team lead for the category  |
| AI confidence is ≥ 0.9            | `notification_email`            |
| AI confidence is 0.7 – 0.89       | The team lead (for approval)    |
| AI confidence is < 0.7            | The team lead (escalation)      |

`notification_email` is where you receive a heads-up for high-confidence tickets. It can be the same as the sender or a different address.

### Processing section

```yaml
processing:
  thread_pool_size: 4
  knowledge_base_folder: "knowledge_base"
  idle_timeout: 30
  fallback_polling_interval: 10
```

- `thread_pool_size` — number of AI tasks that can run at the same time. Increase this if you expect heavy email volume.
- `knowledge_base_folder` — relative path to the folder containing `.txt` policy files.
- `idle_timeout` and `fallback_polling_interval` — used by the IMAP polling loop (in seconds).

### Logging section

```yaml
logging:
  level: "INFO"
  file: ""
```

Set `level` to `DEBUG` to see the keyword scores, semantic similarity scores, and raw AI outputs. Set `file` to a path (for example `pipeline.log`) if you also want logs written to disk.

### Team leads section

```yaml
team_leads:
  - id: TL001
    name: Priya Sharma
    email: priya@yourcompany.com
    categories:
      - "Payroll / Salary"
  - id: TL002
    name: Arjun Mehta
    email: arjun@yourcompany.com
    categories:
      - "Finance / Invoice"
  - id: TL003
    name: Sara Lee
    email: sara@yourcompany.com
    categories:
      - "Technology / VPN"
  - id: TL004
    name: Omar Khalid
    email: omar@yourcompany.com
    categories:
      - "HR / Leave"
```

Each ticket is routed to the team lead whose `categories` list includes the AI's resolved category. The category names must match exactly: `Payroll / Salary`, `Finance / Invoice`, `Technology / VPN`, or `HR / Leave`.

---

## Database Setup

The pipeline expects a SQL Server database called `SupportAutomation` with a `Tickets` table. Run the following script in SQL Server Management Studio (or any SQL client connected to your instance):

```sql
CREATE DATABASE SupportAutomation;
GO

USE SupportAutomation;
GO

CREATE TABLE Tickets (
    TicketID           VARCHAR(20)   PRIMARY KEY,
    Subject            NVARCHAR(MAX) NULL,
    Body               NVARCHAR(MAX) NULL,
    Sender             NVARCHAR(255) NULL,
    Status             VARCHAR(20)   NOT NULL DEFAULT 'New',
    Category           VARCHAR(100)  NULL,
    Subcategory        VARCHAR(100)  NULL,
    Summary            NVARCHAR(MAX) NULL,
    AssignedTeamLead   VARCHAR(50)   NULL,
    Notified           BIT           NOT NULL DEFAULT 0
);
```

The `Notified` column is added automatically by the pipeline on first run if it does not exist, so it is safe to omit it from the initial schema. The `TicketNotes` table is also created automatically on first run.

---

## Knowledge Base

The pipeline reads plain-text policy files from the `knowledge_base/` folder. The default set covers the four supported categories:

- `hr_leave_policy.txt`
- `payroll_policy.txt`
- `vpn_policy.txt`
- Plus additional policy files (e.g. `finance_invoice_policy.txt`, `hr_remote_work_policy.txt`, `technology_software_install.txt`)

Each file is embedded once with `nomic-embed-text` and cached in memory. When a new ticket arrives, the pipeline embeds the ticket text, ranks all knowledge-base files by cosine similarity, and injects the top matching files into the LLM prompt as context.

**To add a new policy:** drop a `.txt` file into `knowledge_base/`. The pipeline picks it up on the next ticket (and the file is re-embedded automatically when its modification time changes).

---

## Running the Pipeline

Start the pipeline with:

```bash
python run_pipeline.py
```

The script will:

1. Load `config.yaml`.
2. Begin a polling loop, checking Gmail for unread messages every `fallback_polling_interval` seconds.
3. Insert each new email as a ticket in the database.
4. Submit the ticket to the AI thread pool for categorization and routing.

Leave the terminal open — the pipeline runs continuously until you stop it with **Ctrl+C**. On shutdown it waits for in-flight AI tasks to finish.

### Viewing logs

By default, logs go to the terminal. To also write them to a file, add a path under `logging.file` in `config.yaml`. Increase verbosity by setting `logging.level: "DEBUG"` — this exposes the keyword scores, semantic similarity scores, raw AI outputs, and routing decisions.

---

## Testing the Pipeline

The `test_emails/` folder contains ready-made email bodies that map cleanly to the knowledge base. To test the full flow:

1. Pick a test email file from `test_emails/` (for example `test_email_hr_leave.txt`).
2. Log into the Gmail account configured in `config.yaml`.
3. Compose a new email, paste in the subject and body, and send it to **your own address** (the pipeline monitors that inbox).
4. Run `python run_pipeline.py`.
5. Watch the logs for `AI Confidence Score: 0.XX`.
6. If the score is ≥ 0.9, check the inbox (and spam folder) for the notification email.

You can also use `send_test_emails.py` to send a batch of test emails automatically — see the comments at the top of that script for its config.

To verify SMTP credentials in isolation, run:

```bash
python test_smtp.py
```

---

## Confidence-Based Routing

After the AI returns a confidence score, the pipeline runs a `Category Validation` step that compares the AI's category to a keyword-based inference from the email text. The final confidence determines the routing path:

| Final confidence | Action                                                                                       |
| ---------------- | -------------------------------------------------------------------------------------------- |
| `≥ 0.90`         | Auto-resolve. Send a user-facing solution email based on the matched SOP, mark `Resolved`.  |
| `0.70 – 0.89`    | Send the team lead a draft solution for approval; status becomes `PendingApproval`.          |
| `< 0.70`         | Escalate to the team lead with the full ticket; status becomes `Escalated`.                  |

**Why the `Category Validation` step matters:** it protects against cases where the LLM hallucinates a category that does not actually match the email. If the keyword scan and the LLM agree, the pipeline trusts the AI's reasoning and lifts the score to 0.90. If they disagree, the AI's category is overridden and its score is capped at 0.85 — so the ticket routes to a human for review rather than auto-resolving in the wrong place.

If the keyword scan cannot infer a category (the email avoids category-specific vocabulary), the AI's score passes through unchanged. This is how ambiguous or out-of-scope emails tend to land in the middle of the 0.7 – 0.9 band.

---

## Project Structure

```
Email Ticketing Service/
├── run_pipeline.py                 # Main entry point — all pipeline logic
├── config.yaml                     # Runtime configuration
├── knowledge_base/                 # .txt policy files for context
├── test_emails/                    # Sample emails for testing
├── .venv/                          # (created during installation)
├── FEATURES_AND_ARCHITECTURE.md    # Deeper architectural notes
├── SYSTEM_EXPLANATION.md           # End-to-end walkthrough
├── TEST_EMAIL_INSTRUCTIONS.md      # How to use the test emails
└── README.md                       # This file
```

---

## Troubleshooting

**"Login failed" on Gmail**
- Confirm 2-Step Verification is enabled on the Google account.
- Regenerate an app password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) and paste it into `config.yaml` (no spaces around the 16 characters).
- Make sure the account has IMAP enabled (Gmail → Settings → Forwarding and POP/IMAP → Enable IMAP).

**"Data source name not found" or `[Microsoft][ODBC Driver Manager]`**
- Install [ODBC Driver 17 for SQL Server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server) and verify it appears in the Windows ODBC Data Source Administrator.

**"Cannot connect to SQL Server"**
- Confirm the SQL Server service is running (`services.msc` → SQL Server (SQLEXPRESS) → Start).
- Check the instance name — the default is `localhost\SQLEXPRESS`, but yours may differ. Update the connection string accordingly.
- If using SQL authentication, add `UID=...;PWD=...;` to the connection string and remove `Trusted_Connection=yes;`.

**"Ollama call failed" / "Connection refused"**
- Confirm the Ollama app is running. On Windows it runs as a background service; on macOS/Linux, run `ollama serve` in a terminal.
- Run `ollama list` to confirm `llama3` and `nomic-embed-text` are pulled.

**No notification email at confidence ≥ 0.9**
- Check spam/junk folders.
- Run `python test_smtp.py` to confirm the SMTP credentials work.
- Look at the logs for `Sent confidence email notification for ticket ...` or an SMTP error.

**Confidence is consistently lower than expected**
- Add more specific terms to the email that match the policy wording (for example, mention the exact policy number from the relevant `knowledge_base/` file).
- Set `logging.level: "DEBUG"` to see the keyword scores, semantic similarity scores, and raw AI output. Use them to identify which policy was matched and how strongly.

**Pipeline is slow on a busy inbox**
- Increase `processing.thread_pool_size` in `config.yaml` (each worker needs its own DB connection and Ollama call).
- Reduce `processing.fallback_polling_interval` to check for new mail more often.

---

## Security Notes

- **Never commit `config.yaml` with real credentials.** The `app_password` fields grant access to your Gmail account. Add `config.yaml` to `.gitignore` and consider using a separate config file (for example `config.local.yaml`) for development.
- Treat any `app_password` you generate as a secret. If it leaks, revoke it from your Google account and generate a new one.
- The connection string may contain authentication details. If you switch to SQL authentication, treat the password the same way.
- Logs at `DEBUG` level can include the body of incoming emails. Be careful when sharing log files, especially if they may contain personal or sensitive information from senders.
