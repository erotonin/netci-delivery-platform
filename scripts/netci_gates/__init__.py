"""Shared building blocks for the executable acceptance gates.

Every gate runner records what it actually ran -- command line, timestamps, exit
code, output tail -- and asserts on the result. A gate that cannot produce that
record is not allowed to report success.
"""
