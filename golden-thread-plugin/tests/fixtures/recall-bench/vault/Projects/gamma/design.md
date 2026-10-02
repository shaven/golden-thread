# gamma design

The phone keeps a local journal and syncs deltas every 15 minutes or on reconnect. Conflicts are
resolved last-writer-wins per field, except for the notes field, which is merged line by line.
