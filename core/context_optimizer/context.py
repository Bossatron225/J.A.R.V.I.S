from typing import Dict, Any, List, Optional
import time
import logging

logger = logging.getLogger(__name__)

class ContextManager:
    """Manages context processing, token limits, and memory optimization for the engine."""

    def __init__(self, max_context_window: int = 4096, retention_threshold: float = 0.75) -> None:
        """
        Initializes the ContextManager with specific capacity limits and thresholds.

        Args:
            max_context_window: Maximum allowable token count or size indicator.
            retention_threshold: Percentage of capacity before initiating pruning.
        """
        self.max_context_window: int = max_context_window
        self.retention_threshold: float = retention_threshold
        self._memory_store: Dict[str, Any] = {}
        self._access_timestamps: Dict[str, float] = {}

    def store(self, key: str, value: Any) -> None:
        """
        Stores a key-value pair in the context memory with a timestamp.

        Args:
            key: Unique identifier for the context item.
            value: Data payload to store.
        """
        try:
            self._memory_store[key] = value
            self._access_timestamps[key] = time.time()
            self._enforce_capacity()
        except Exception as e:
            logger.error(f"Failed to store context key '{key}': {e}")
            raise

    def retrieve(self, key: str) -> Optional[Any]:
        """
        Retrieves a context item by key, updating its access timestamp.

        Args:
            key: Unique identifier for the context item.

        Returns:
            The stored data payload if present, else None.
        """
        try:
            if key in self._memory_store:
                self._access_timestamps[key] = time.time()
                return self._memory_store[key]
            return None
        except Exception as e:
            logger.error(f"Failed to retrieve context key '{key}': {e}")
            return None

    def clear(self) -> None:
        """Clears all stored context and timestamps."""
        self._memory_store.clear()
        self._access_timestamps.clear()

    def _enforce_capacity(self) -> None:
        """Prunes oldest context items if the memory store exceeds the retention threshold."""
        try:
            if len(self._memory_store) > int(self.max_context_window * self.retention_threshold):
                # Sort keys by access timestamp ascending (oldest first)
                sorted_keys: List[str] = sorted(
                    self._access_timestamps.keys(),
                    key=lambda k: self._access_timestamps[k]
                )
                # Remove the oldest 20% of items
                items_to_remove: int = max(1, int(len(sorted_keys) * 0.2))
                for key in sorted_keys[:items_to_remove]:
                    self._memory_store.pop(key, None)
                    self._access_timestamps.pop(key, None)
                logger.info(f"Pruned {items_to_remove} stale items from context memory.")
        except Exception as e:
            logger.error(f"Error during context capacity enforcement: {e}")

    def get_summary(self) -> Dict[str, Any]:
        """
        Provides a diagnostic summary of the current context state.

        Returns:
            Dictionary containing item count, capacity, and active keys.
        """
        return {
            "total_items": len(self._memory_store),
            "max_capacity": self.max_context_window,
            "keys": list(self._memory_store.keys())
        }