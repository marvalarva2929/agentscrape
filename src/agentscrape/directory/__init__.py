"""Looking people up in an institution's people/student directory.

After a crawl, the directory can supply what the roster pages left blank: an
address, a PGY or class year, a title. `learn` works out, once per school, how
to search the directory; `lookup` searches it for one person and reads back
exactly one unambiguous match.
"""
