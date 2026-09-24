"""Storage layer: SQLite database, repositories and guarded ingestion.

Submodules are imported by their full paths (``data.db.connection`` etc.);
this package deliberately has no eager re-exports so importing the storage
package never drags the agents/evaluation layers into import cycles
(latent cycle found and removed in Phase 8-C1).
"""
