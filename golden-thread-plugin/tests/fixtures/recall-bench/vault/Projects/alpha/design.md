# alpha design

Queries go through a gateway that applies the tenant filter before the index sees them. Results are
re-ranked by recency for support tickets only. The index rebuild writes to a new alias and swaps.
