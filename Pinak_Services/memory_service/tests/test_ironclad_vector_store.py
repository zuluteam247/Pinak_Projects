import os
import pytest
import numpy as np
from unittest.mock import patch, MagicMock
from app.services.vector_store import VectorStore

def test_vector_store_initialization_fail_recovery(tmp_path):
    index_file = tmp_path / "corrupt.index"
    index_file.write_text("not a faiss index")
    
    # Should log error and create new index instead of crashing
    with patch("app.services.vector_store.logger") as mock_logger:
        vs = VectorStore(str(index_file), dimension=128)
        assert vs.index is not None
        mock_logger.error.assert_called()
        assert "Failed to load index" in mock_logger.error.call_args[0][0]

def test_vector_store_add_vectors_validation(tmp_path):
    vs = VectorStore(str(tmp_path / "test.index"), dimension=128)
    vectors = np.random.random((5, 128))
    ids = [1, 2, 3] # Mismatch length
    
    with pytest.raises(ValueError) as exc:
        vs.add_vectors(vectors, ids)
    assert "Number of vectors and IDs must match" in str(exc.value)

def test_vector_store_reconstruct_failure(tmp_path):
    vs = VectorStore(str(tmp_path / "test.index"), dimension=128)
    # Add nothing, try reconstruct
    vec = vs.reconstruct(999)
    assert vec is None

def test_vector_store_search_empty(tmp_path):
    vs = VectorStore(str(tmp_path / "test.index"), dimension=128)
    dist, ids = vs.search(np.random.random(128))
    assert dist == []
    assert ids == []

def test_vector_store_batch_add(tmp_path):
    index_path = str(tmp_path / "batch.index")
    vs = VectorStore(index_path, dimension=128)
    vs._save_interval = 60 # Long interval
    
    vectors = np.random.random((2, 128))
    ids = [10, 20]
    
    with vs.batch_add():
        vs.add_vectors(vectors, ids)
        # Should not save yet automatically due to Timer (wait threading)
        # but batch_add forces save at end
    
    assert os.path.exists(index_path)
