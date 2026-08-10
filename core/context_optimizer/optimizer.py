from typing import Dict, Any, List, Callable, Optional
import time
import logging
from core.context_optimizer.context import ContextManager

logger = logging.getLogger(__name__)

class ToolExecutionOptimizer:
    """Optimizes tool execution flow and streamlines execution logic based on recent analytics."""

    def __init__(self, context_manager: ContextManager) -> None:
        """
        Initializes the ToolExecutionOptimizer with a ContextManager instance.

        Args:
            context_manager: An instance of ContextManager for handling execution state and memory.
        """
        self.context_manager: ContextManager = context_manager
        self._execution_metrics: Dict[str, List[float]] = {}

    def optimize_flow(self, tool_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Streamlines the tool execution payload and retrieves cached results if available.

        Args:
            tool_name: The name of the tool to execute.
            payload: Input dictionary payload for the tool.

        Returns:
            Optimized payload or cached execution result.
        """
        try:
            cache_key = f"tool_cache:{tool_name}:{hash(frozenset(payload.items()))}"
            cached_result = self.context_manager.retrieve(cache_key)
            
            if cached_result is not None:
                logger.info(f"Cache hit for tool '{tool_name}'. Streamlining execution flow.")
                return {"status": "cached", "result": cached_result}

            logger.info(f"No cache found for tool '{tool_name}'. Processing payload optimization.")
            optimized_payload = {k: v for k, v in payload.items() if v is not None}
            
            self.context_manager.store(f"last_payload:{tool_name}", optimized_payload)
            return {"status": "optimized", "payload": optimized_payload}
        except Exception as e:
            logger.error(f"Error during flow optimization for tool '{tool_name}': {e}")
            raise

    def execute_streamlined(self, tool_name: str, tool_func: Callable[..., Any], payload: Dict[str, Any]) -> Any:
        """
        Executes a given tool function with performance tracking and context optimization.

        Args:
            tool_name: Name of the tool.
            tool_func: Callable tool function.
            payload: Input parameters for the tool function.

        Returns:
            The output of the tool execution.
        """
        start_time = time.time()
        try:
            optimization_result = self.optimize_flow(tool_name, payload)
            
            if optimization_result["status"] == "cached":
                return optimization_result["result"]

            active_payload = optimization_result["payload"]
            result = tool_func(**active_payload)

            cache_key = f"tool_cache:{tool_name}:{hash(frozenset(payload.items()))}"
            self.context_manager.store(cache_key, result)

            duration = time.time() - start_time
            if tool_name not in self._execution_metrics:
                self._execution_metrics[tool_name] = []
            self._execution_metrics[tool_name].append(duration)

            logger.info(f"Tool '{tool_name}' executed and optimized successfully in {duration:.4f} seconds.")
            return result
        except Exception as e:
            logger.error(f"Execution failed for tool '{tool_name}': {e}")
            raise