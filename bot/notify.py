#!/usr/bin/env python3
"""Alert notifier: alerts the owner when the bot hits trouble.

`ALERT_CHANNEL` (.env) picks the delivery order:
  email     — Gmail SMTP → Mail.app  (no iMessage; the whole message goes through
              intact, so a long desk report isn't truncated to 1800 chars)
  imessage  — iMessage only
  auto      — iMessage → Mail.app → SMTP (the legacy fallback chain; default)

A macOS notification is always attempted, best-effort.

Recipient contact details come from the environment (.env), not source:
  ALERT_GMAIL_USER = the sending Gmail account (+ ALERT_GMAIL_APP_PASSWORD)
  ALERT_EMAIL      = where email is delivered
  ALERT_IMESSAGE   = the phone for iMessage

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
CHANNEL = os.environ.get("ALERT_CHANNEL", "auto").strip().lower()


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
    # Google shows App Passwords as "abcd efgh ijkl mnop" — strip the spaces so a
    # copy-paste straight from the console works.
    pw = os.environ.get("ALERT_GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not (user and pw):
        raise RuntimeError("SMTP creds not configured (ALERT_GMAIL_USER / ALERT_GMAIL_APP_PASSWORD)")
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = TO
    msg["Subject"] = subject
    msg.set_content(body or subject)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)


def channel_order():
    """Delivery attempts, in order, for the configured ALERT_CHANNEL."""
    imsg = ("iMessage", send_via_imessage, lambda: IMESSAGE_TO)
    smtp = ("Gmail SMTP", send_via_smtp, lambda: TO)
    mail = ("Mail.app", send_via_mail_app, lambda: TO)
    if CHANNEL == "email":
        # Gmail ONLY — no iMessage (truncates long reports) and no Mail.app
        # fallback (it would send from whatever account Mail happens to be signed
        # into, not ALERT_GMAIL_USER). If the App Password is missing we want a
        # loud failure, not a message from the wrong sender.
        return [smtp]
    if CHANNEL == "imessage":
        return [imsg]
    return [imsg, mail, smtp]         # "auto" — the legacy fallback chain


def main():
    subject = sys.argv[1] if len(sys.argv) > 1 else "Trading bot alert"
    body = "" if sys.stdin.isatty() else sys.stdin.read()
    full_subject = f"[trading-bot] {subject}"

    macos_notification(subject)

    errors = []
    for name, send, target in channel_order():
        try:
            send(full_subject, body or subject)
            print(f"notify: sent to {target()} via {name}")
            return 0
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"notify: {name} failed ({e})")
    print(f"notify: ALL channels failed ({'; '.join(errors)}); notification only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
