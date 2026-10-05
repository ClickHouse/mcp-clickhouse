import concurrent.futures

_ENRICHMENT_MAX_WORKERS = 2


class _Executors:
    """Worker pools owned by one server assembly."""

    def __init__(self, max_workers: int):
        self.max_workers = max_workers
        self.query = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        metadata_max_workers = max(1, min(4, max_workers))
        self.metadata = concurrent.futures.ThreadPoolExecutor(max_workers=metadata_max_workers)
        self.enrichment = concurrent.futures.ThreadPoolExecutor(max_workers=_ENRICHMENT_MAX_WORKERS)
        self.cancellation = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.health = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def shutdown(self):
        """Drain query, metadata, enrichment, cancellation, and health workers."""
        self.query.shutdown(wait=True)
        self.metadata.shutdown(wait=True)
        self.enrichment.shutdown(wait=True)
        self.cancellation.shutdown(wait=True)
        self.health.shutdown(wait=True)
