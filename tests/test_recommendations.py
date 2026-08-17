from nostalgiabox.recommendations import suggest_similar


def test_exact_match_returns_curated_suggestions():
    suggestions = suggest_similar("Bluey")
    assert suggestions  # non-empty
    assert "Bluey" not in suggestions  # never suggests the same show back


def test_match_is_case_insensitive():
    assert suggest_similar("bluey") == suggest_similar("BLUEY") == suggest_similar("Bluey")


def test_substring_match_handles_folder_style_names():
    # A real channel name might carry extra words a curated table entry
    # wouldn't have verbatim - substring matching in either direction should
    # still find it.
    assert suggest_similar("Bluey Season 3") == suggest_similar("Bluey")


def test_unknown_show_returns_no_suggestions():
    assert suggest_similar("Some Obscure Home Video Channel") == []


def test_empty_name_returns_no_suggestions():
    assert suggest_similar("") == []
    assert suggest_similar(None) == []


def test_limit_caps_the_number_of_suggestions():
    suggestions = suggest_similar("Bluey", limit=1)
    assert len(suggestions) == 1
