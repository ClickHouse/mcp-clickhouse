import concurrent.futures


class _Executors:
    """Worker pools owned by one server assembly."""

    def __init__(self, max_workers: int):
        self.max_workers = max_workers
        self.query = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        metadata_max_workers = max(1, min(4, max_workers))
        self.metadata = concurrent.futures.ThreadPoolExecutor(max_workers=metadata_max_workers)
        self.cancellation = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.health = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def shutdown(self):
        """Drain query, metadata, cancellation, and health workers."""
        self.query.shutdown(wait=True)
        self.metadata.shutdown(wait=True)
        self.cancellation.shutdown(wait=True)
        self.health.shutdown(wait=True)
