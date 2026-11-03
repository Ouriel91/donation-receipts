# Skill: Receipt Detection

## Purpose

Define how the project should identify donation-related emails and receipt attachments.

This skill should guide detection logic, tests, and harness samples.

---

## Detection Goal

Detect emails that are likely related to donation receipts.

The detector should be conservative enough to avoid many false positives, but flexible enough to catch Hebrew and English donation emails.

---

## Positive Signals

Donation-related emails may include words or phrases such as:

### Hebrew

- תרומה
- קבלה
- קבלה על תרומה
- אישור תרומה
- תרומתך
- תודה על תרומתך
- סעיף 46
- עמותה
- מלכ"ר
- מוסד ציבורי
- זיכוי מס
- החזר מס

### English

- donation
- receipt
- donation receipt
- tax receipt
- tax deductible
- thank you for your donation
- nonprofit
- charity

---

## Attachment Signals

Receipt attachments are usually:

- PDF files
- Image files
- Files with names that include receipt/donation related words
- Attachments from donation-related emails

Allowed attachment extensions may include:

- .pdf
- .jpg
- .jpeg
- .png

---

## Negative Signals

The following keywords indicate commercial or invoice context and will downgrade confidence to `low` unless strong donation signals are also present:

### Hebrew

- חשבונית
- חשבונית מס

### English

- invoice
- tax invoice
- order
- purchase
- shipping
- delivery
- payment receipt

**Override rule:** If a strong donation keyword (e.g. "תרומה", "donation") is present alongside a negative keyword, the strong signal wins and confidence is not downgraded.

---

## Detection Output

Detection should return more than a raw boolean when possible.

Preferred output:

- is_donation: true/false
- confidence: low/medium/high
- reasons: list of matched signals

---

## Important Rules

- Do not rely on one keyword only if it is too generic
- Prefer subject + body + attachment metadata together
- Keep logic simple and testable
- Avoid machine learning for now
- Avoid external services
- Add tests for positive and negative examples
- Add harness samples for common cases