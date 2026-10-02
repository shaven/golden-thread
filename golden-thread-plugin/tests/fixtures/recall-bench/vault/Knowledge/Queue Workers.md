# Queue Workers

Queue consumers scale between 2 and 24 replicas on queue depth. A job that fails five times goes to
the dead-letter queue, which is reviewed every morning. Consumers must be idempotent.
