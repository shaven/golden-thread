# Backup Rotation

Larkspur keeps **seven daily** snapshots, **four weekly** and **twelve monthly**. The pruner runs at
02:40 and never deletes the newest weekly. Restores are tested on the first Sunday of each month by
restoring the orders database into a scratch host and comparing row counts.
