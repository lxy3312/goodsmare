from .base import Item, NoResults, Query, Source
from .mandarake import Mandarake
from .mercari import Mercari
from .rakuma import Rakuma
from .surugaya import Surugaya
from .yahoo_auction import YahooAuction
from .yahoo_flea import YahooFlea

SOURCES: dict[str, Source] = {s.key: s for s in (
    Mercari(), YahooAuction(), YahooFlea(), Rakuma(), Surugaya(), Mandarake(),
)}

__all__ = ["Item", "NoResults", "Query", "Source", "SOURCES"]
