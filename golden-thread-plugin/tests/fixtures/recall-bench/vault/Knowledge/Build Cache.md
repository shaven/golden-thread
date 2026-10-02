# Build Cache

Compiled artifacts are cached by content hash in an object store bucket named `lark-build-cache`.
A poisoned entry is cleared with `cachectl evict <hash>`; clearing everything takes about forty
minutes of cold builds. Cache keys include the compiler version, so a toolchain bump is a miss.
