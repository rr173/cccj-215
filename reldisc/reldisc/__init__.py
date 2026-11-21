"""reldisc -- offline cross-table inclusion (reference candidate) discovery.

Discovers whether the values of a set of columns of a source table are covered
by a set of columns of a *different* target table.  Results are candidates for
warehouse validation; they are NOT declared permanent foreign keys, the target
columns are not required to be unique, and no intra-table unique-key or
functional-dependency discovery is performed.
"""

__version__ = "0.1.0"
