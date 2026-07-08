#!/usr/bin/env python3
"""Alert notifier: alerts the owner when the bot hits trouble.

Channels, in order:
  1. macOS notification (always, best effort)
  2. iMessage to ALERT_IMESSAGE (primary — instant on iPhone)
  3. Mail.app via AppleScript (fallback; requires Mail signed in)
  4. Gmail SMTP fallback (only if ALERT_GMAIL_USER/APP_PASSWORD set in .env)

Recipient contact details come from the environment (.env), not source.

Usage: notify.py "subject" [< body_on_stdin]
"""

import os
import smtplib
import ssl
import subprocess
import sys
from email.message import EmailMessage

TO = os.environ.get("ALERT_EMAIL", "")          # email channels
IMESSAGE_TO = os.environ.get("ALERT_IMESSAGE", "")  # iMessage channel (owner's phone)


def macos_notification(subject):
    try:
        safe = subject.replace('"', "'")
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe}" with title "Trading Bot"'],
            timeout=10, capture_output=True,
        )
    except Exception:
        pass


def send_via_imessage(subject, body):
    text = f"🤖 {subject}\n{body}".strip()[:1800]
    script = f'''
    tell application "Messages"
        set ok to false
        repeat with acc in (every account whose enabled is true)
            try
                set tgt to participant "{IMESSAGE_TO}" of acc
                send {applescript_str(text)} to tgt
                set ok to true
                exit repeat
            end try
        end repeat
        if not ok then error "no enabled account could reach {IMESSAGE_TO}"
    end tell
    '''
    r = subprocess.run(["osascript", "-e", script], capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")


def send_via_mail_app(subject, body):
    script = f'''
    tell application "Mail"
        set newMessage to make new outgoing message with properties {{subject:{applescript_str(subject)}, content:{applescript_str(body)}, visible:false}}
        tell newMessage
            make new to recipient at end of to recipients with properties {{address:"{TO}"}}
        end tell
        send newMessage
    end tell
    '''
    r = subprocess.run(["osascript", "-e", script], capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")


def applescript_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def send_via_smtp(subject, body):
    user = os.environ.get("ALERT_GMAIL_USER", "").strip()
    pw = os.environ.get("ALERT_GMAIL_APP_PASSWORD", "").strip()
    if not (user and pw):
        raise RuntimeError("SMTP creds not configured")
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = TO
    msg["Subject"] = subject
    msg.set_content(body or subject)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)


def main():
    subject = sys.argv[1] if len(sys.argv) > 1 else "Trading bot alert"
    body = "" if sys.stdin.isatty() else sys.stdin.read()
    full_subject = f"[trading-bot] {subject}"

    macos_notification(subject)

    try:
        send_via_imessage(full_subject, body or subject)
        print(f"notify: iMessaged {IMESSAGE_TO}")
        return 0
    except Exception as e:
        print(f"notify: iMessage failed ({e}); trying Mail.app")

    try:
        send_via_mail_app(full_subject, body or subject)
        print(f"notify: emailed {TO} via Mail.app")
        return 0
    except Exception as e:
        print(f"notify: Mail.app failed ({e}); trying SMTP")

    try:
        send_via_smtp(full_subject, body or subject)
        print(f"notify: emailed {TO} via SMTP")
    except Exception as e:
        print(f"notify: all email channels failed ({e}); notification only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
