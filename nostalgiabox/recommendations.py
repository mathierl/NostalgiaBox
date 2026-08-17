"""Text-only "you might also like" suggestions for the admin Insights view
(UKE-29).

NostalgiaBox has no download or content-catalog system at all - channels
are just folders of files a grown-up puts there by hand (see README). So
"recommendations" here can't mean anything gets fetched or added
automatically; it's a small curated lookup table of well-known kids' shows,
matched against whatever channel the kid actually watches most, purely as a
shopping/research pointer for the grown-up reading the Insights screen. If
a favorite channel's name doesn't match anything in the table, there's
simply nothing to suggest - no guessing from an unfamiliar folder name.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# Keyed by a lowercased show name (or a distinctive substring of one).
# Deliberately small and curated rather than exhaustive - only well-known,
# easily-recognized titles, since a wrong or obscure guess is worse than no
# suggestion at all. Entries are hand-picked "similar vibe" shows, not a
# genre taxonomy.
_SIMILAR_SHOWS: Dict[str, Tuple[str, ...]] = {
    "bluey": ("Peppa Pig", "Hey Duggee", "Daniel Tiger's Neighborhood"),
    "peppa pig": ("Bluey", "Ben and Holly's Little Kingdom", "Daniel Tiger's Neighborhood"),
    "dragon tales": ("Dora the Explorer", "Ni Hao, Kai-lan", "Sesame Street"),
    "arthur": ("The Berenstain Bears", "Franklin and Friends", "Clifford the Big Red Dog"),
    "rugrats": ("Hey Arnold!", "The Wild Thornberrys", "Doug"),
    "paw patrol": ("Blaze and the Monster Machines", "Rusty Rivets", "Bubble Guppies"),
    "dora the explorer": ("Dragon Tales", "Ni Hao, Kai-lan", "Go, Diego, Go!"),
    "sesame street": ("Mister Rogers' Neighborhood", "Daniel Tiger's Neighborhood", "Between the Lions"),
    "mister rogers": ("Daniel Tiger's Neighborhood", "Sesame Street", "Reading Rainbow"),
    "daniel tiger": ("Mister Rogers' Neighborhood", "Bluey", "Sesame Street"),
    "winnie the pooh": ("Bear in the Big Blue House", "Franklin and Friends", "The Berenstain Bears"),
    "bear in the big blue house": ("Winnie the Pooh", "Sesame Street", "Blue's Clues"),
    "blues clues": ("Bear in the Big Blue House", "Dora the Explorer", "Sesame Street"),
    "spongebob": ("The Fairly OddParents", "Rocko's Modern Life", "CatDog"),
    "avatar the last airbender": ("The Legend of Korra", "Kipo and the Age of Wonderbeasts", "She-Ra"),
    "gravity falls": ("Amphibia", "The Owl House", "Adventure Time"),
    "adventure time": ("Regular Show", "Gravity Falls", "Steven Universe"),
    "steven universe": ("Adventure Time", "Gravity Falls", "Craig of the Creek"),
    "phineas and ferb": ("Milo Murphy's Law", "Gravity Falls", "The Fairly OddParents"),
}


def suggest_similar(channel_name: str, *, limit: int = 3) -> List[str]:
    """A short list of similar, well-known show titles for ``channel_name``,
    or an empty list if it isn't recognized. Tries an exact (lowercased)
    match first, then falls back to substring matching in either direction
    (so "Bluey Season 3" or a folder just named "bluey" both still hit).
    """
    if not channel_name:
        return []
    key = channel_name.strip().lower()
    if key in _SIMILAR_SHOWS:
        return list(_SIMILAR_SHOWS[key][:limit])
    for table_key, suggestions in _SIMILAR_SHOWS.items():
        if table_key in key or key in table_key:
            return list(suggestions[:limit])
    return []


__all__ = ["suggest_similar"]
