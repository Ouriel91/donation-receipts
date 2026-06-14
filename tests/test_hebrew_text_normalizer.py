from src.hebrew_text_normalizer import normalize_hebrew_text


def test_single_reversed_words():
    assert normalize_hebrew_text("תתומע") == "עמותת"
    assert normalize_hebrew_text("ךיראת") == "תאריך"
    assert normalize_hebrew_text("םוכס") == "סכום"


def test_multi_line_block_preserves_lines():
    source = "תתומע\nךיראת\nםוכס"
    expected = "עמותת\nתאריך\nסכום"

    result = normalize_hebrew_text(source)

    assert result == expected
    assert result.count("\n") == source.count("\n")


def test_numbers_and_dates_untouched():
    source = "1,250\n24/05/2026"

    assert normalize_hebrew_text(source) == source


def test_mixed_hebrew_and_number_line():
    # "סכום" reversed in the source; the number must keep value, order, position.
    result = normalize_hebrew_text("םוכס 1,250 ₪")

    assert result == "סכום 1,250 ₪"


def test_non_hebrew_line_unchanged():
    source = "Donation Receipt 2026"

    assert normalize_hebrew_text(source) == source


def test_empty_and_whitespace_unchanged():
    assert normalize_hebrew_text("") == ""
    assert normalize_hebrew_text("   ") == "   "
    assert normalize_hebrew_text("\n\n") == "\n\n"
