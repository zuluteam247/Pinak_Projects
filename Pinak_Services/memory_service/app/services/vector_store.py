import os
import numpy as np
import threading
import logging
import time
import json
import tempfile
from typing import List, Tuple, Optional, Dict, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)

class VectorStore:
    """
    Thread-safe Vector Store using Numpy for similarity search.
    Provides identical API to the previous FAISS implementation but without segfaults.
    """
    def __init__(self, index_path: str, dimension: int):
        self.index_path = index_path
        self.dimension = dimension
        self.lock = threading.RLock()
        
        # In-memory storage
        self.vectors = np.empty((0, dimension), dtype=np.float32)
        self.ids = np.array([], dtype=np.int64)
        self.norms = np.array([], dtype=np.float32)
        self._id_to_row: Dict[int, int] = {}
        self._id_to_rows: Dict[int, List[int]] = {}

        self._load_index()

        self._save_timer = None
        self._save_interval = 5.0  # seconds
        self.needs_save = False

    @property
    def index(self):
        return self

    @property
    def ntotal(self):
        return len(self.ids)

    def _reindex_ids(self):
        """Refresh ID lookups while holding the store lock."""
        self._id_to_row = {}
        self._id_to_rows = {}
        for row, value in enumerate(self.ids):
            key = int(value)
            self._id_to_row.setdefault(key, row)
            self._id_to_rows.setdefault(key, []).append(row)

    def _load_index(self):
        """Loads vectors and IDs from a numpy file."""
        with self.lock:
            load_path = None
            if os.path.exists(self.index_path):
                load_path = self.index_path
            elif os.path.exists(f"{self.index_path}.npy"):
                load_path = f"{self.index_path}.npy"
            if load_path:
                try:
                    data = np.load(load_path, allow_pickle=True).item()
                    vectors = np.asarray(data['vectors'], dtype=np.float32)
                    ids = np.asarray(data['ids'], dtype=np.int64)
                    if vectors.ndim != 2 or vectors.shape[1] != self.dimension or len(vectors) != len(ids):
                        raise ValueError("Vector index shape or dimension mismatch")
                    self.vectors = vectors
                    self.ids = ids
                    self._reindex_ids()
                    self.norms = np.sum(np.square(self.vectors), axis=1)
                    logger.info(f"Loaded Vector Store from {load_path}. Size: {len(self.ids)}")
                except Exception as e:
                    logger.error(f"Failed to load index: {e}. Creating new one.")
                    self.vectors = np.empty((0, self.dimension), dtype=np.float32)
                    self.ids = np.array([], dtype=np.int64)
                    self._id_to_row = {}
                    self._id_to_rows = {}
                    self.norms = np.array([], dtype=np.float32)

    def _schedule_save(self):
        """Schedule a debounced save."""
        if self._save_timer is not None:
            self._save_timer.cancel()

        self._save_timer = threading.Timer(self._save_interval, self.save)
        self._save_timer.daemon = True
        self._save_timer.start()

    def save(self):
        """Synchronously save to disk."""
        with self.lock:
            if self.needs_save:
                dirpath = os.path.dirname(self.index_path)
                if dirpath:
                    os.makedirs(dirpath, exist_ok=True)
                # Write a complete snapshot, fsync it, then atomically swap it
                # into place. A crash cannot truncate the last good index.
                fd, temporary = tempfile.mkstemp(prefix=".vectors-", dir=dirpath or ".")
                try:
                    with os.fdopen(fd, "wb") as handle:
                        np.save(handle, {'vectors': self.vectors, 'ids': self.ids})
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, self.index_path)
                    if hasattr(os, "O_DIRECTORY"):
                        directory_fd = os.open(dirpath or ".", os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                self.needs_save = False
                logger.info(f"Saved Vector Store to {self.index_path}. Size: {len(self.ids)}")

    def add_vectors(self, vectors: np.ndarray, ids: List[int]):
        """Add vectors with specific IDs."""
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        if vectors.shape[0] != len(ids):
            raise ValueError("Number of vectors and IDs must match")
        if vectors.shape[1] != self.dimension:
            raise ValueError(f"Vector dimension {vectors.shape[1]} does not match index dimension {self.dimension}")

        vectors = vectors.astype(np.float32)
        id_array = np.array(ids, dtype=np.int64)
        new_norms = np.sum(np.square(vectors), axis=1)

        with self.lock:
            start = len(self.ids)
            self.vectors = np.vstack([self.vectors, vectors])
            self.ids = np.concatenate([self.ids, id_array])
            # Legacy snapshots may hold duplicate IDs pending startup repair.
            for offset, value in enumerate(ids):
                key = int(value)
                row = start + offset
                self._id_to_row.setdefault(key, row)
                self._id_to_rows.setdefault(key, []).append(row)
            self.norms = np.concatenate([self.norms, new_norms])
            self.needs_save = True

        self._schedule_save()

    def search(self, query_vector: np.ndarray, k: int = 10, allowed_ids: Optional[set[int]] = None) -> Tuple[List[float], List[int]]:
        """Find top K nearest neighbors using L2 distance."""
        with self.lock:
            if k < 1 or len(self.ids) == 0:
                return [], []

            # Ensure vectors and query are float32 for consistency
            query_vector = query_vector.astype(np.float32)
            if query_vector.ndim == 1:
                query_vector = query_vector.reshape(1, -1)
            if query_vector.shape[1] != self.dimension:
                return [], []

            # Resolve only allowed rows before the expensive distance calculation.
            # Filtering after top-k silently loses tenant hits when other
            # tenants dominate the global index.
            if allowed_ids is None:
                candidates = np.arange(len(self.ids))
            else:
                # The pre-startup repair path may temporarily hold repeated
                # legacy IDs; include every row until repair deduplicates them.
                # Normal query cost is proportional to this tenant's IDs.
                candidates = np.fromiter(
                    (row for value in allowed_ids for row in self._id_to_rows.get(value, ())),
                    dtype=np.int64,
                )
            actual_k = min(k, len(candidates))
            if not actual_k:
                return [], []
            selected = self.vectors if allowed_ids is None else self.vectors[candidates]
            dot_product = np.dot(selected, query_vector.T).flatten()
            query_norm_sq = float(np.sum(np.square(query_vector)))
            sq_dists = np.maximum(self.norms[candidates] + query_norm_sq - (2.0 * dot_product), 0.0)
            if actual_k < len(candidates):
                partition = np.argpartition(sq_dists, actual_k - 1)[:actual_k]
                ranked = partition[np.argsort(sq_dists[partition])]
            else:
                ranked = np.argsort(sq_dists)
            top_k_idx = candidates[ranked]

            return (
                [float(d) for d in sq_dists[ranked].tolist()],
                [int(i) for i in self.ids[top_k_idx].tolist()],
            )

    def remove_ids(self, ids: List[int]):
        """Remove specific vectors by ID."""
        with self.lock:
            mask = ~np.isin(self.ids, ids)
            self.vectors = self.vectors[mask]
            self.ids = self.ids[mask]
            self._reindex_ids()
            self.norms = self.norms[mask]
            self.needs_save = True
        self._schedule_save()

    @property
    def total(self):
        return len(self.ids)

    def reset(self):
        with self.lock:
            self.vectors = np.empty((0, self.dimension), dtype=np.float32)
            self.ids = np.array([], dtype=np.int64)
            self._id_to_row = {}
            self._id_to_rows = {}
            self.norms = np.array([], dtype=np.float32)
            self.needs_save = True

    def reconstruct(self, vector_id: int) -> Optional[np.ndarray]:
        with self.lock:
            row = self._id_to_row.get(vector_id)
            return self.vectors[row].copy() if row is not None else None

    @contextmanager
    def batch_add(self):
        yield
        self.save()
