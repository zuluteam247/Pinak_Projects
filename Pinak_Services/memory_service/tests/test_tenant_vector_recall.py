"""A tenant's vector hits must not be displaced by other tenants."""
import numpy as np
from app.services.memory_service import MemoryService
from app.services.vector_store import VectorStore


class FixedEncoder:
    embedding_dimension = 2

    def encode(self, texts):
        return np.array([[0.0, 0.0] if text == 'query' else [1.0, 1.0]
                         for text in texts], dtype=np.float32)


def test_vector_store_filters_before_top_k(tmp_path):
    store = VectorStore(str(tmp_path / 'vectors'), 2)
    store.add_vectors(np.array([[0, 0], [1, 1], [0, 0]], dtype=np.float32), [1, 2, 3])
    distances, ids = store.search(np.array([0, 0], dtype=np.float32), k=1, allowed_ids={2})
    assert ids == [2]
    assert distances == [2.0]
    assert store.search(np.array([0, 0], dtype=np.float32), k=1, allowed_ids=set()) == ([], [])


def test_hybrid_recall_not_crowded_by_other_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv('PINAK_EMBEDDING_BACKEND', 'dummy')
    monkeypatch.setenv('PINAK_CONFIG_PATH', str(tmp_path / 'missing.json'))
    monkeypatch.chdir(tmp_path)
    service = MemoryService(model=FixedEncoder())
    own = service.db.add_semantic('our distinctive item', [], 'ours', 'project', 300)
    service.vector_store.add_vectors(np.array([[1, 1]], dtype=np.float32), [300])
    for i in range(20):
        service.db.add_semantic(f'foreign item {i}', [], 'theirs', 'project', i + 1)
        service.vector_store.add_vectors(np.array([[0, 0]], dtype=np.float32), [i + 1])
    hits = service.search_hybrid('query', 'ours', 'project', limit=1, semantic_weight=1.0)
    assert [hit['id'] for hit in hits] == [own['id']]
    assert service.search_hybrid('query', 'nobody', 'project', limit=1) == []
