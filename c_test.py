from src.pdf_text_extractor import extract_text_from_pdf
from src.hebrew_text_normalizer import normalize_hebrew_text

raw = extract_text_from_pdf(r"receipts\primary\2026\06_June\08_06_26__קבלה_תרומה_80553.pdf")
print(normalize_hebrew_text(raw))
