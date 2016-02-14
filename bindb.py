"""
    Reynir: Natural language processing for Icelandic

    BIN database access module

    Copyright (c) 2016 Vilhjalmur Thorsteinsson
    All rights reserved
    See the accompanying README.md file for further licensing and copyright information.

    This module encapsulates access to the BIN (Beygingarlýsing íslensks nútímamáls)
    database of word forms, including lookup of abbreviations and basic strategies
    for handling missing words.

    The database is assumed to be stored in PostgreSQL. It is accessed via
    the Psycopg2 connector.

"""

from functools import lru_cache
from collections import namedtuple
import threading

# Import the Psycopg2 connector for PostgreSQL
try:
    # For CPython
    import psycopg2.extensions as psycopg2ext
    import psycopg2
except ImportError:
    # For PyPy
    import psycopg2cffi.extensions as psycopg2ext
    import psycopg2cffi as psycopg2

# Make Psycopg2 and PostgreSQL happy with UTF-8
psycopg2ext.register_type(psycopg2ext.UNICODE)
psycopg2ext.register_type(psycopg2ext.UNICODEARRAY)

from settings import Settings, Abbreviations, AdjectiveTemplate, Meanings
from dawgdictionary import Wordbase

# Size of LRU cache for word lookups
CACHE_SIZE = 512

# Adjective endings
ADJECTIVE_TEST = "leg" # Check for adjective if word contains 'leg'

# Named tuple for word meanings fetched from the BÍN database (lexicon)
BIN_Meaning = namedtuple('BIN_Meaning', ['stofn', 'utg', 'ordfl', 'fl', 'ordmynd', 'beyging'])


class BIN_Db:

    """ Encapsulates the BÍN database of word forms """

    # Thread local storage - used for database connections
    tls = threading.local()

    @classmethod
    def get_db(cls):
        """ Obtain a database connection instance """
        # We have one DB connection and cursor per thread.
        db = None
        if hasattr(cls.tls, "bin_db"):
            # Connection already established in this thread: re-use it
            db = cls.tls.bin_db

        if db is None:
            # New connection in this thread
            db = cls.tls.bin_db = cls().open(Settings.DB_HOSTNAME)

        if db is None:
            raise Exception("Could not open BIN database on host {0}".format(Settings.DB_HOSTNAME))

        return db

    def __init__(self):
        """ Initialize DB connection instance """
        self._conn = None # Connection
        self._c = None # Cursor

    def open(self, host):
        """ Open and initialize a database connection """
        self._conn = psycopg2.connect(dbname="bin",
            user="reynir", password="reynir",
            host=host, client_encoding="utf8")

        if not self._conn:
            print("Unable to open connection to database")
            return None

        # Ask for automatic commit after all operations
        # We're doing only reads, so this is fine and makes things less complicated
        self._conn.autocommit = True
        self._c = self._conn.cursor()
        return None if self._c is None else self

    def close(self):
        """ Close the DB connection and the associated cursor """
        self._c.close()
        self._conn.close()
        self._c = self._conn = None
        if BIN_Db.tls.bin_db is self:
            BIN_Db.tls.bin_db = None

    @lru_cache(maxsize = CACHE_SIZE)
    def meanings(self, w):
        """ Return a list of all possible grammatical meanings of the given word """
        assert self._c is not None
        m = None
        try:
            self._c.execute("select stofn, utg, ordfl, fl, ordmynd, beyging " +
                "from ord where ordmynd=(%s);", [ w ])
            # Map the returned data from fetchall() to a list of instances
            # of the BIN_Meaning namedtuple
            g = self._c.fetchall()
            if g is not None:
                m = list(map(BIN_Meaning._make, g))
                if w in Meanings.DICT:
                    # There are additional word meanings in the Meanings dictionary,
                    # coming from the settings file: append them
                    for add_m in Meanings.DICT[w]:
                        m.append(BIN_Meaning._make(add_m))
        except (psycopg2.DataError, psycopg2.ProgrammingError) as e:
            print("Word {0} causing DB exception {1}".format(w, e))
            m = None
        return m

    def lookup_word(self, w, at_sentence_start):
        """ Lookup a simple or compound word in the database and return its meaning(s) """

        def lookup_abbreviation(w):
            """ Lookup abbreviation from abbreviation list """
            # Remove brackets, if any, before lookup
            clean_w = w[1:-1] if w[0] == '[' else w
            # Return a single-entity list with one meaning
            m = Abbreviations.DICT.get(clean_w, None)
            return None if m is None else [ BIN_Meaning._make(m) ]

        # Start with a simple lookup
        m = self.meanings(w)

        if at_sentence_start or not m:
            # No meanings found in database, or at sentence start
            # Try a lowercase version of the word, if different
            lower_w = w.lower()
            if lower_w != w:
                # Do another lookup, this time for lowercase only
                if not m:
                    m = self.meanings(lower_w)
                else:
                    m.extend(self.meanings(lower_w))

            if not m and (lower_w != w or w[0] == '['):
                # Still nothing: check abbreviations
                m = lookup_abbreviation(w)
                if not m and w[0] == '[':
                    # Could be an abbreviation with periods at the start of a sentence:
                    # Lookup a lowercase version
                    m = lookup_abbreviation(lower_w)
                if m and w[0] == '[':
                    # Remove brackets from known abbreviations
                    w = w[1:-1]

            if not m and ADJECTIVE_TEST in lower_w:
                # Not found: Check whether this might be an adjective
                # ending in 'legur'/'leg'/'legt'/'legir'/'legar' etc.
                for aend, beyging in AdjectiveTemplate.ENDINGS:
                    if lower_w.endswith(aend) and len(lower_w) > len(aend):
                        prefix = lower_w[0 : len(lower_w) - len(aend)]
                        # Construct an adjective descriptor
                        if m is None:
                            m = []
                        m.append(BIN_Meaning(prefix + "legur", 0, "lo", "alm", lower_w, beyging))

            if not m:
                # Still nothing: check compound words
                cw = Wordbase.dawg().slice_compound_word(lower_w)
                if cw:
                    # This looks like a compound word:
                    # use the meaning of its last part
                    prefix = "-".join(cw[0:-1])
                    m = self.meanings(cw[-1])
                    m = [ BIN_Meaning(prefix + "-" + r.stofn, r.utg, r.ordfl, r.fl,
                            prefix + "-" + r.ordmynd, r.beyging)
                            for r in m]

            if not m and lower_w.startswith('ó'):
                # Check whether an adjective without the 'ó' prefix is found in BÍN
                # (i.e. create 'óhefðbundinn' from 'hefðbundinn')
                suffix = lower_w[1:]
                if suffix:
                    om = self.meanings(suffix)
                    if om:
                        m = [ BIN_Meaning("ó" + r.stofn, r.utg, r.ordfl, r.fl,
                                "ó" + r.ordmynd, r.beyging)
                                for r in om if r.ordfl == "lo" ]

        return (w, m)

