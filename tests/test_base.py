"""
tests/test_base.py
==================
Basic tests — verify that modules load correctly
and that the file renaming logic works.
"""

from shared.file_manager import rename_file


def test_rename_prisma():
    result = rename_file(
        "prisma",
        "E10484MX20260320_020132.ZIP",
        "{merchant}_{date}.{ext}"
    )
    assert result == "10484_20-03-2026.zip"


def test_rename_fallback():
    """If the filename does not match, returns the original."""
    result = rename_file("prisma", "weird_file.zip", "{merchant}_{date}.{ext}")
    assert result == "weird_file.zip"


def test_rename_original_pattern():
    result = rename_file("cabal", "LIQ_CABAL.zip", "{original}")
    assert result == "LIQ_CABAL.zip"


def test_imap_reader_import():
    from shared.imap_reader import ImapReader
    reader = ImapReader("imap.gmail.com", "test@gmail.com", "password")
    assert reader.host == "imap.gmail.com"


def test_session_store_import():
    from shared.session_store import SessionStore
    store = SessionStore("prisma")
    assert store.provider == "prisma"


def test_agents_import():
    """Verifies that all agents can be imported."""
    from agents.prisma      import PrismaAgent
    from agents.cabal       import CabalAgent
    from agents.naranjax    import NaranjaXAgent
    from agents.fiserv      import FiservAgent
    from agents.amex        import AmexAgent
    from agents.getnet      import GetnetAgent

    assert PrismaAgent.name   == "prisma"
    assert CabalAgent.name    == "cabal"
    assert NaranjaXAgent.name == "naranjax"
    assert FiservAgent.name   == "fiserv"
    assert AmexAgent.name     == "amex"
    assert GetnetAgent.name   == "getnet"
