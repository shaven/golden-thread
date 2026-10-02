# alpha decisions

ADR-1: use a single shard until the index passes 40 GB. Rejected: sharding by tenant, because most
tenants are tiny. ADR-2: rebuild nightly rather than incrementally; incremental updates drifted.
