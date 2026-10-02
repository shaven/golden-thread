# DNS Zones

The internal zone is `lark.internal`, served by two resolvers. Public records live in the registrar;
TTL is 300 seconds for anything that may fail over. A record change needs a ticket, and the zone
file is linted before reload so a missing trailing dot cannot take a name down.
