# RFC: shared read-only model assets (proposal, not implemented)

Status: **proposed**. Nothing here is implemented or enabled. Today each engine
exclusively owns one writable store, its database and managed model directory
(see [deployment: graceful shutdown](../deployment.md#graceful-shutdown--drain)).
Engines that share a writable model directory refuse to start.

## Problem

Several engines on one host may want the same large GGUF files without keeping
one copy per engine. Sharing the writable model directory is unsafe: one
engine's download, rename, delete or cleanup would change files that another
engine owns or has loaded.

## Proposed shape

- **Two roots per engine.** A shared **immutable asset root**, mounted
  read-only, plus each engine's own private writable store and database, which
  keep the existing exclusive ownership.
- **Explicit read-only access.** The engine opens assets only for reading, and
  the deployment mounts the root read-only. A read-only mount alone is not the
  design: it does not define identity, integrity or update semantics.
- **Stable identity and integrity.** Each asset is identified by content (for
  example its SHA-256) plus a version. Registering an asset verifies that hash
  before the asset is used, and loading re-checks cheap invariants (size, GGUF
  header).
- **Metadata registration.** An engine registers a shared asset in its own
  database as a new source kind that is distinct from `import`, with its
  identity, version and path.
- **Load and unload.** Loading reads from the asset root. Unloading and deleting
  remove only the engine's own registration, never the file.
- **No mutation, ever.** Downloads, renames, replacements, deletions and garbage
  collection never write inside the asset root.
- **Updates.** A new version is published as a new immutable directory by a
  separately authorized process, never by an engine. Engines switch versions
  through explicit registration.
- **Paths.** Resolved asset paths must stay inside the asset root: symlink and
  `..` escapes are rejected. There is no write path back into the root.

## Tests it would need

- Concurrent readers across processes, and loads while another engine unloads.
- Refused writes of every kind: download, delete, rename, cleanup.
- Symlink and path-escape attempts, hash mismatches and truncated files.
- Version switches, with image-level runs on read-only volumes.

## Explicit non-goals of the current code

- There is no switch that disables managed-store locking.
- Sharing is never permitted based on the current file permissions.
- The existing `import` of a file outside the store does not provide this
  guarantee: an imported file is still treated as the importing engine's model.
