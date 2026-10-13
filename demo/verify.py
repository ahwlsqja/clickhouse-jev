"""Acceptance contract for the disposable, real-server demo.

Create only documented synthetic fixtures in a dedicated demo database.
Check filtering, classification, scoring, and normalized scoring through the
server's executable UDF. Print the returned results and exit nonzero on setup,
query, or result mismatch. No external model or credential is required.
"""
