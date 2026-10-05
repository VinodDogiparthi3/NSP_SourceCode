import time

import chromadb
from fastembed import TextEmbedding

from app.config import Settings
from app.models import SourceRecord


COLLECTIONS = {"nsp": "nsp_records", "sopc": "sopc_results"}


class LocalEmbeddingModel:
    def __init__(self, settings: Settings):
        cache_dir = settings.data_path / "embedding_models"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.model = TextEmbedding(
            model_name=settings.embedding_model,
            cache_dir=str(cache_dir),
            lazy_load=True,
        )

    def embed(self, texts: list[str], parallel: int | None = None) -> list[list[float]]:
        return [
            vector.tolist()
            for vector in self.model.embed(texts, batch_size=128, parallel=parallel)
        ]

    def embed_query(self, text: str) -> list[float]:
        return next(self.model.query_embed(text)).tolist()


class LocalVectorStore:
    def __init__(self, settings: Settings, load_embedding_model: bool = True):
        self.settings = settings
        settings.chroma_path.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(settings.chroma_path))
        self.embedder = LocalEmbeddingModel(settings) if load_embedding_model else None

    def collection(self, dataset: str):
        collection = self.client.get_or_create_collection(
            name=COLLECTIONS[dataset],
            configuration={
                "hnsw": {
                    "space": "cosine",
                    "batch_size": 1000,
                    "sync_threshold": 10000,
                }
            },
            metadata={"dataset": dataset.upper()},
        )
        collection.modify(
            configuration={"hnsw": {"batch_size": 1000, "sync_threshold": 10000}}
        )
        return collection

    def reset_collection(self, dataset: str):
        name = COLLECTIONS[dataset]
        try:
            self.client.delete_collection(name)
        except Exception:
            pass
        return self.collection(dataset)

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        existing = {item.name for item in self.client.list_collections()}
        for dataset, name in COLLECTIONS.items():
            result[dataset] = self.client.get_collection(name).count() if name in existing else 0
        return result

    def upsert(
        self,
        dataset: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, str]],
        parallel: int | None = None,
    ) -> None:
        if self.embedder is None:
            raise RuntimeError("The embedding model was not loaded")
        embeddings = self.embedder.embed(documents, parallel=parallel)
        self.collection(dataset).upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )

    def retrieve(self, question: str, dataset: str, top_k: int) -> tuple[list[SourceRecord], int]:
        if self.embedder is None:
            raise RuntimeError("The embedding model was not loaded")
        started = time.perf_counter()
        datasets = [dataset] if dataset in COLLECTIONS else ["nsp", "sopc"]
        query_vector = self.embedder.embed_query(question)
        records: list[SourceRecord] = []
        for current in datasets:
            collection = self.collection(current)
            if collection.count() == 0:
                continue
            result = collection.query(
                query_embeddings=[query_vector],
                n_results=min(top_k, collection.count()),
                include=["documents", "metadatas", "distances"],
            )
            for record_id, document, metadata, distance in zip(
                result["ids"][0],
                result["documents"][0],
                result["metadatas"][0],
                result["distances"][0],
            ):
                source = "NSP" if current == "nsp" else "SOPC"
                records.append(
                    SourceRecord(
                        record_id=record_id,
                        source=source,
                        title=metadata.get("title", f"{source} record"),
                        document_text=document,
                        metadata={key: value for key, value in metadata.items() if key != "title"},
                        distance=float(distance),
                    )
                )
        records.sort(key=lambda item: item.distance)
        latency_ms = round((time.perf_counter() - started) * 1000)
        return records[:top_k], latency_ms
