from typing import List, Dict, Any, Tuple
import asyncio
from src.agents.base_agent import BaseAgent
from src.agents import DeepSearchAgent
from src.tools import ToolResult, get_tool_categories, get_tool_by_name


class DataCollector(BaseAgent):
    AGENT_NAME = 'data_collector'
    AGENT_DESCRIPTION = 'a agent that can collect data from the internet and variable apis'
    NECESSARY_KEYS = ['task']
    def __init__(
        self,
        config,
        tools = [],
        use_llm_name: str = "deepseek-chat",
        enable_code = True,
        memory = None,
        agent_id: str = None,
        task_context = None,
        tool_categories: list[str] | None = None,
    ):
        super().__init__(
            config=config,
            tools=tools,
            use_llm_name=use_llm_name,
            enable_code=enable_code,
            memory=memory,
            agent_id=agent_id,
            task_context=task_context,
        )
        # Load prompts using the YAML-based loader with correct report type
        from src.utils.prompt_loader import get_prompt_loader
        target_type = self.config.config.get('target_type', 'general')
        self.prompt_loader = get_prompt_loader('data_collector', report_type=target_type)
        self.DATA_COLLECT_PROMPT = self.prompt_loader.get_prompt('data_collect')
        
        self.collected_data_list: List[ToolResult] = []
        self._tool_categories = tool_categories
        if self.tools == []:
            self._set_default_tools()
        

    def _set_default_tools(self):
        """
        Attach default tools (search agent + API wrappers).
        Uses ``self._tool_categories`` to filter which API categories to load.
        If not set, loads all categories except 'web'.
        """
        tool_list = []
        tool_list.append(DeepSearchAgent(
            config=self.config, use_llm_name=self.use_llm_name,
            memory=self.memory, task_context=self.task_context,
        ))
        # Determine which API categories to load
        categories = self._tool_categories
        if categories is None:
            categories = [k for k in get_tool_categories() if k != 'web']
        for tool_type, tool_name_list in get_tool_categories().items():
            if tool_type not in categories:
                continue
            for tool_name in tool_name_list:
                tool_instance = get_tool_by_name(tool_name)()
                tool_list.append(tool_instance)
        if self.memory is not None:
            for tool in tool_list:
                self.memory.add_dependency(tool.id, self.id)
        self.tools = tool_list
        try:
            self.logger.info(f"Initialized default tools: total {len(tool_list)} items")
        except Exception:
            pass
        

    async def _prepare_executor(self):
        # Expose helper functions to the code executor for LLM-generated code
        self.code_executor.set_variable("call_tool", self._agent_tool_function)
        self.code_executor.set_variable("save_result", self._save_result)

    def _save_result(self, var: Any, result_name: str, result_description: str, data_source: str):
        """Persist execution results into self.collected_data_list."""
        item = ToolResult(
            name=result_name,
            description=result_description,
            data=var,
            source=data_source,
        )
        self.collected_data_list.append(item)
        if self.task_context is not None:
            self.task_context.put("collected_data", item)
        if self.memory is not None:
            self.memory.add_data(item)
        try:
            self.logger.info(f"Saved collect result: {result_name} (source={data_source})")
        except Exception:
            pass
    
    async def _prepare_init_prompt(self, input_data: dict) -> list[dict]:
        task = input_data.get('task')
        if not task:
            raise ValueError("Input data must contain a 'task' key.")
        
        target_language_name = self._get_language_display_name()
        
        # Extract research target from task
        target_name = self.config.config.get('target_name', '')
        stock_code = self.config.config.get('stock_code', '')
        research_target = f"{target_name} (ticker: {stock_code})" if stock_code else target_name
            
        return [{
            "role": "user",
            "content": self.DATA_COLLECT_PROMPT.format(
                api_descriptions=self._get_api_descriptions(),
                current_time=self.current_time,
                task=task,
                target_language=target_language_name,
                research_target=research_target
            )
        }]
    

    async def async_run(
        self, 
        input_data: dict, 
        max_iterations: int = 10,
        stop_words: list[str] = [],
        echo=False,
        resume: bool = True,
        checkpoint_name: str = 'latest.pkl',
    ) -> dict:
        # Reset collected-data cache for each run
        self.collected_data_list = []
        self.logger.info(f"DataCollector started: task={input_data.get('task','')} resume={resume}")
        await self._prepare_executor()
        run_result = await super().async_run(
            input_data=input_data,
            max_iterations=max_iterations,
            stop_words=stop_words,
            echo=echo,
            resume=resume,
            checkpoint_name=checkpoint_name,
        )
        run_result['collected_data_list'] = self.collected_data_list
        # Note: data is already persisted in _save_result() during execution.
        self.logger.info(f"DataCollector finished: collected={len(self.collected_data_list)} items")
        if self.memory is not None:
            self.memory.add_log(
                id=self.id,
                type=self.type,
                input_data=input_data,
                output_data=self.collected_data_list,
                error=False,
                note=f"DataCollector finished: collected={len(self.collected_data_list)} items"
            )
            self.memory.save()
        return run_result

    def _get_persist_extra_state(self) -> Dict[str, Any]:
        return {
            'collected_data_list': self.collected_data_list,
        }

    def _load_persist_extra_state(self, state: Dict[str, Any]):
        self.collected_data_list = state.get('collected_data_list', [])
