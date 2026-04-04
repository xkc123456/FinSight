from typing import List, Dict, Any, Tuple
import asyncio
import os
import re
import subprocess
import numpy as np
import docx2pdf
from src.agents.base_agent import BaseAgent
from src.agents.search_agent.search_agent import DeepSearchAgent
from src.tools.web.web_crawler import ClickResult
from src.tools import ToolResult, get_tool_categories, get_tool_by_name
from src.agents.report_generator.report_class import Report, Section
from src.utils.helper import extract_markdown, get_md_img
from src.utils.index_builder import IndexBuilder
from src.utils.figure_helper import draw_kline_chart


class ReportGenerator(BaseAgent):
    AGENT_NAME = 'report_generator'
    AGENT_DESCRIPTION = 'a agent that can generate report from the data'
    NECESSARY_KEYS = ['task']
    def __init__(
        self,
        config,
        tools = None,
        use_llm_name: str = "deepseek-chat",
        use_embedding_name: str = "qwen3-embedding",
        enable_code = True,
        memory = None,
        agent_id: str = None,
        task_context = None,
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
        # Load prompts based on language and target type settings
        from src.utils.prompt_loader import get_prompt_loader
        
        self.target_language_name = self._get_language_display_name()
        target_type = self.config.config.get('target_type', 'general')
        
        
        # Load prompts using the new YAML-based loader
        self.prompt_loader = get_prompt_loader('report_generator', report_type=target_type)
        
        # Store prompts as instance attributes for easy access
        self.SECTION_WRITING_PROMPT = self.prompt_loader.get_prompt('section_writing')
        self.SECTION_WRITING_WO_CHART_PROMPT = self.prompt_loader.get_prompt('section_writing_wo_chart')
        self.FINAL_POLISH_PROMPT = self.prompt_loader.get_prompt('final_polish')
        
        # For general reports, use outline_draft; for financial, use outline_draft as well
        # (both YAML files have 'outline_draft' key)
        self.DRAFT_GENERATOR_PROMPT = self.prompt_loader.get_prompt('outline_draft')
        
        self.CRITIQUE_PROMPT = self.prompt_loader.get_prompt('outline_critique')
        self.REFINEMENT_PROMPT = self.prompt_loader.get_prompt('outline_refinement')
        
        # used for adding abstract and title
        self.TITLE_PROMPT = self.prompt_loader.get_prompt('title_generation')
        self.ABSTRACT_PROMPT = self.prompt_loader.get_prompt('abstract')

        # used for cover page
        self.TABLE_BEAUTIFY_PROMPT = self.prompt_loader.get_prompt('table_beautify')
        
        self.use_embedding_name = use_embedding_name
        

    def _set_default_tools(self):
        """
        Attach the default tools/agents required by the report generator.
        """
        tool_list = []
        tool_list.append(DeepSearchAgent(
            config=self.config, use_llm_name=self.use_llm_name,
            task_context=self.task_context,
        ))
        self.tools = tool_list

    def _get_collect_data(self, exclude_type=None):
        """Get collected data from task_context."""
        return self.task_context.get("collected_data")

    def _get_analysis_results(self):
        """Get analysis results from task_context."""
        return self.task_context.get("analysis_results")
    
    async def _prepare_executor(self):
        """
        Prepare the code executor with data access functions for section writing.
        """
        current_task_data = self.current_task_data
        tool_list = self.tools
        collect_data_list = self._get_collect_data(exclude_type=['search', 'click'])
        analysis_result_list = self._get_analysis_results()
        
        def _get_data(data_id: int):
            """Get dataset by index"""
            if 0 <= data_id < len(collect_data_list):
                return collect_data_list[data_id].data
            else:
                raise ValueError(f"Invalid data_id: {data_id}. Available range: 0-{len(collect_data_list)-1}")
        
        def _get_analysis_result(data_id: int):
            """Get analysis results matching the query"""
            # Use LLM-based selection to find relevant analysis results
            if 0 <= data_id < len(analysis_result_list):
                return str(analysis_result_list[data_id])[:3000]
            else:
                raise ValueError(f"Invalid data_id: {data_id}. Available range: 0-{len(analysis_result_list)-1}")
        
        def _get_deepsearch_result(query: str):
            """Call deep search agent.

            Uses the async bridge to avoid the asyncio.run() deadlock when
            called from inside exec()'d code within a running event loop.
            """
            from src.utils.async_bridge import get_async_bridge
            bridge = get_async_bridge()
            ds_agent = tool_list[0]
            output = bridge.run_async(ds_agent.async_run(input_data={
                'task': current_task_data.get('task', ''),
                'query': query
            }))
            return output['final_result']
        
        self.code_executor.set_variable("get_data", _get_data)
        self.code_executor.set_variable("get_analysis_result", _get_analysis_result)
        self.code_executor.set_variable("get_data_from_deep_search", _get_deepsearch_result)
        
    
    async def _prepare_init_prompt(self, input_data: dict) -> list[dict]:
        task = input_data.get('task')
        section_outline = input_data.get('section_outline')
        max_iterations = input_data.get('max_iterations', 10)
        if not task:
            raise ValueError("Input data must contain a 'task' key.")
        
        # Get data API description from prompts
        data_api_description = self.prompt_loader.get_prompt('data_api')
        
        # Prepare data information for the agent
        collect_data_list = self._get_collect_data(exclude_type=['search', 'click'])
        analysis_result_list = self._get_analysis_results()
        data_info = "\n\n## Available Datas\n\n"
        for idx, item in enumerate(collect_data_list):
            data_info += f"**Data ID {idx}:**\n{item.brief_str()}\n\n"
        data_info += "\nYou can access these datasets using `get_data(data_id)` in your code.\n"
        data_info += "\n\n## Available Analysis Reports\n\n"
        for idx, item in enumerate(analysis_result_list):
            data_info += f"**Analysis Report ID {idx}:**\n{item.brief_str()}\n\n"
        data_info += "\nYou can access these analysis reports using `get_analysis_result(analysis_result_id)` in your code.\n"
        
        if self.enable_chart:
            return [{
                "role": "user",
                "content": self.SECTION_WRITING_PROMPT.format(
                    task=task,
                    report_theme=input_data.get('task'),
                    section_description=section_outline,
                    data_api=data_api_description,
                    data_info=data_info,
                    max_iterations=max_iterations,
                    target_language=self.target_language_name
                )
            }]
        else:
            return [{
                "role": "user",
                "content": self.SECTION_WRITING_WO_CHART_PROMPT.format(
                    task=task,
                    report_theme=input_data.get('task'),
                    section_description=section_outline,
                    data_api=data_api_description,
                    data_info=data_info,
                    max_iterations=max_iterations,
                    target_language=self.target_language_name
                )
            }]

    async def _handle_search_action(self, action_content: str):
        search_result = await self.tools[0].async_run(input_data={'query': action_content})
        return {
            'action': 'search',
            'action_content': action_content,
            'result': search_result['final_result'],
            'continue': True,
        }
    
    async def _handle_report_action(self, action_content: str):
        """Handle a 'final/report' action."""
        return {
            "action": "report",
            "action_content": action_content,
            "result": action_content,
            "continue": False,
        }
    async def _handle_outline_action(self, action_content: str):
        """Handle a 'outline' action."""
        return {
            "action": "outline",
            "action_content": action_content,
            "result": action_content,
            "continue": False,
        }
    
    async def _handle_draft_action(self, action_content: str):
        """Handle a 'outline' action."""
        return {
            "action": "draft",
            "action_content": action_content,
            "result": action_content,
            "continue": False,
        }
    
    async def _final_polish(self, section_input_data, draft_section: str):
        all_analysis_result = self._get_analysis_results()
        all_image_list = []
        for analysis_result in all_analysis_result:
            all_image_list.extend(analysis_result.get_all_img())
        reference_image = '\n'.join(all_image_list)
        
        final_prompt = self.FINAL_POLISH_PROMPT.format(
            draft_report = draft_section,
            reference_image = reference_image,
            target_language = self.target_language_name
        )
        
        final_message = [{"role": "user", "content": final_prompt}]
        output = await self.llm.generate(messages = final_message)
        final_section = extract_markdown(output)
        return final_section
    
    async def _replace_image_path(self, report):
        """
        Replace placeholder image references in the report with actual local paths.
        """
        # If charts are disabled, simply remove @import placeholders
        if not self.enable_chart:
            for section in report.sections:
                section_new_content = []
                for p_paragraph in section._content:
                    # Replace @import.* with empty string
                    p_paragraph = re.sub(r'@import.*', '', p_paragraph, flags=re.DOTALL)
                    section_new_content.append(p_paragraph)
                section._content = section_new_content
            return report
        
        def remove_suffix(name: str):
            return name.replace(".png", "").replace(".jpg", "").replace(".jpeg", "").replace(".md", "")
        def is_image_file(name: str):
            return name.endswith(".png") or name.endswith(".jpg") or name.endswith(".jpeg") or name.endswith(".md")
        all_analysis_result = self._get_analysis_results()
        img_captions = []
        img_paths = []
        for analysis_result in all_analysis_result:
            short2long = {}
            img_dicts = {} # caption: abs_path 
            chart_name_mapping = analysis_result.chart_name_mapping
            for long_name, short_name in chart_name_mapping.items():
                short2long[remove_suffix(short_name)] = remove_suffix(long_name)
            image_save_dir = analysis_result.image_save_dir
            for image_name in os.listdir(image_save_dir):
                if is_image_file(image_name):
                    img_path = os.path.join(image_save_dir, image_name)
                    img_name = remove_suffix(image_name)
                    long_image_name = short2long.get(img_name, "")
                    if long_image_name != "":
                        img_dicts[long_image_name] = img_path
            img_captions.extend(list(img_dicts.keys()))
            img_paths.extend(list(img_dicts.values()))
        if len(img_captions) == 0:
            self.logger.warning("No image captions found, replacing @import placeholders with fallback text")
            for section in report.sections:
                section_new_content = []
                for p_paragraph in section._content:
                    p_paragraph = re.sub(
                        r'@import.*',
                        '*[Chart not available — no chart images were generated during analysis]*',
                        p_paragraph,
                        flags=re.DOTALL,
                    )
                    section_new_content.append(p_paragraph)
                section._content = section_new_content
            return report
        self.logger.info(f"Building index for {len(img_captions)} images")
        index = IndexBuilder(config=self.config, embedding_model=self.use_embedding_name, working_dir=self.working_dir)
        await index._build_index(img_captions)

        used_img_list = []
        figure_idx = 1
        for section in report.sections:
            section_new_content = []
            for p_paragraph in section._content:
                match = re.findall(r'@import.*', p_paragraph,flags=re.DOTALL)
                try:
                    self.logger.debug(f"Section image placeholders: {len(match)}")
                except Exception:
                    pass
                if match and len(match) > 0:
                    for img_name in match:
                        # img_name is the short placeholder string
                        most_similar_idx = (await index.search(img_name))[0]['id']
                        detect_img_name = img_captions[most_similar_idx]
                        detect_img_path = img_paths[most_similar_idx]
                        
                        if len(img_captions) == 1:
                            # No images left to map
                            self.logger.warning("Available images are exhausted; stop replacing images.")
                            # directly delete the image placeholder
                            p_paragraph = p_paragraph.replace(img_name, "")
                            continue
                        del img_captions[most_similar_idx]
                        del img_paths[most_similar_idx]
                        # Rebuild the index after consuming this caption
                        await index._build_index(img_captions)

                        new_string = get_md_img(detect_img_path, remove_suffix(os.path.basename(detect_img_path)), figure_idx)
                        figure_idx += 1
                        used_img_list.append(detect_img_name)
                        p_paragraph = p_paragraph.replace(img_name, new_string)

                section_new_content.append(p_paragraph)
            section._content = section_new_content
        return report

    
    async def _add_abstract(self, input_data, report):
        """
        Add an abstract and update the title.
        """
        abstract_prompt = self.ABSTRACT_PROMPT
        title_prompt = self.TITLE_PROMPT


        response_content = await self.llm.generate(
            messages = [
            {
                'role': 'user',
                'content': abstract_prompt.format(target_language=self.target_language_name, report_content=report.content)
            }
        ])
        response_content = extract_markdown(response_content)
        report.abstract = response_content
        
        new_title = await self.llm.generate(
            messages = [
            {
                'role': 'user',
                'content': title_prompt.format(target_language=self.target_language_name, report_content=report.content)
            }
        ])
        new_title = new_title.replace("#","").strip()
        report._content = f"# {new_title}\n\n"

        return report

    async def _add_cover_page(self, input_data, report):
        if not self.add_cover_page:
            return report
        stock_code = self.task_context.stock_code if self.task_context else input_data.get('stock_code', '')
        if not stock_code:
            return report

        output_str = "\n\n## Company Fundamentals\n\n"
        # Three statements + shareholder profile
        collect_data_list = self._get_collect_data()
        table_configs = [
            ("Income statement", "Income Statement"),
            ("Balance sheet", "Balance Sheet"),
            ("Cash-flow statement", "Cash-Flow Statement"),
            ("Shareholding structure", "Shareholder Structure"),
        ]
        for keyword, display_name in table_configs:
            target_item_list = [item for item in collect_data_list if keyword in item.name and stock_code in item.name]
            if len(target_item_list) == 0:
                print(f"No {display_name} data found")
                continue
            else:
                table_data = target_item_list[0].data
                if table_data is None:
                    print(f"{display_name} data is empty, skip formatting")
                    continue
                    
                if keyword in ["Income statement", "Balance sheet", "Cash-flow statement"]:
                    if 'Category' in table_data.columns:
                        table_data.rename(columns={'Category': 'Line item (RMB mn)'}, inplace=True)
                prompt = self.TABLE_BEAUTIFY_PROMPT.format(table_name=display_name, table_data=table_data.to_markdown(index=False))
                response = await self.llm.generate(
                    messages = [
                        {"role": "user", "content": prompt}
                    ]
                )
                table_string = "\n".join([line for line in response.split("\n") if line.strip() != ""])

                output_str += f'\n\n### {display_name}\n\n'
                output_str += table_string
                
                output_str += '\n\n'
        
        # Render stock-price chart
        try:
            self.logger.info("Rendering stock-price chart for cover page")
            target_item_list = [item for item in collect_data_list if 'candlestick' in item.name.lower() and stock_code in item.name]
            if len(target_item_list) != 0:
                kline_data = target_item_list[0].data
                if kline_data is None:
                    self.logger.warning("Candlestick data is empty; skip price visualization")
                else:
                    if isinstance(kline_data, list) and len(kline_data) == 1:
                        kline_data = kline_data[0]
                    if 'date' not in kline_data.columns:
                        if '\u65e5\u671f' in kline_data.columns:
                            kline_data.rename(columns={'\u65e5\u671f': 'date'}, inplace=True)
                        if '\u6536\u76d8' in kline_data.columns:
                            kline_data.rename(columns={'\u6536\u76d8': 'close'}, inplace=True)
                    fig_path = draw_kline_chart(kline_data, self.working_dir)
                    output_str += f'\n\n### Share Price Trend\n\n'
                    output_str += f'![Trailing price performance]({fig_path})\n\n'
        except Exception as e:
            self.logger.error(f"Failed to draw price trend: {e}", exc_info=True)
            pass

        first_section = Section('Company Fundamentals', output_str)
        first_section.set_content(output_str)
        report.sections = [first_section] + report.sections

        return report
    

    async def _add_reference(self, report):
        """
        Append the reference-data section and replace placeholder citations.
        """
        collect_data_list = self._get_collect_data()  # only use data, without analysis result
        all_data = []
        for item in collect_data_list:
            name = item.name + '\n' + item.description
            content = item.source
            if isinstance(item, ClickResult):
                url = item.link
                title = item.name
                content = f"{title}\n{url}"

            # content = item.name + '\n' + item.link  # used for display citation
            if content not in [ii['content'] for ii in all_data]:
                all_data.append({
                    'name': name,
                    'content': content 
                })
        self.logger.info(f"Total data for reference: {len(all_data)}")
        
        total_corpus = [item['name'] for item in all_data]
        index = IndexBuilder(config=self.config, embedding_model=self.use_embedding_name, working_dir=self.working_dir)
        await index._build_index(total_corpus)

        total_cited_dict = {}
        for section in report.sections:
            # Optional: log section length
            try:
                self.logger.debug(f"Processing section, content length={len(section.content)}")
            except Exception:
                pass
            section_new_content = []
            for p_paragraph in section._content:
                content = p_paragraph
                # Locate citation placeholders
                match_list = re.findall(r'\[[Ss]ource[：:]\s*(.*?)\]',content)
                self.logger.debug(f"Match list: {match_list}")
                for match_item in match_list:
                    # Use BM25/embedding search
                    search_result = await index.search(match_item, top_k=5)
                    score_list = [item['score'] for item in search_result]
                    id_list = [item['id'] for item in search_result]  # Get actual data indices
                    self.logger.debug(f"Score list: {score_list}")
                    self.logger.debug(f"ID list: {id_list}")
                    # Sort by score (descending) and get corresponding indices
                    sorted_idx = np.argsort(score_list)[::-1]
                    score_list = np.array(score_list)
                    score_list = np.exp(score_list) / np.sum(np.exp(score_list))

                    cite_list = []
                    for pos in sorted_idx:
                        pos = int(pos)
                        actual_idx = id_list[pos]  # Get the actual data index
                        if score_list[pos] > 0.2 and len(cite_list) < 5:
                            cite_list.append(actual_idx)
                    if len(cite_list) == 0:
                        # If no item meets threshold, use the top result
                        cite_list.append(id_list[sorted_idx[0]])
                    new_cite_list = []
                    for idx in cite_list:
                        if idx not in total_cited_dict:
                            total_cited_dict[idx] = len(total_cited_dict) + 1
                    new_cite_list = [total_cited_dict[idx] for idx in cite_list]
                    # Build the regex for replacement
                    pattern_to_replace = r'\[[Ss]ource[：:]\s*' + re.escape(match_item) + r'\]'
                    content = re.sub(pattern_to_replace, f'[{",".join([str(item) for item in new_cite_list])}]', content)

                section_new_content.append(content)
            section._content = section_new_content


        reference_str = "## Reference Data Sources\n\n"
        for old_index, new_index in total_cited_dict.items():
            content = all_data[old_index]['content']
            content = content.replace("\n", " ").replace("[PDF]", "")
            reference_str += f"{new_index}. {content}\n"
        new_section = Section('Reference Data Sources', reference_str)
        new_section.set_content(reference_str)
        report.sections.append(new_section)
        return report

    def _get_persist_extra_state(self) -> Dict[str, Any]:
        return {}

    def _load_persist_extra_state(self, state: Dict[str, Any]):
        enable_chart = state.get('enable_chart')
        if enable_chart is not None:
            try:
                self.enable_chart = bool(enable_chart)
            except Exception:
                pass
        else:
            self.enable_chart = True
    
    async def _prepare_outline_prompt(self, input_data):
        max_iterations = input_data.get('max_iterations', 10)
        outline_template_path = self.config.config.get('outline_template_path', None)
        
        if outline_template_path is None or not os.path.exists(outline_template_path):
            outline_template = ""
        else:
            with open(outline_template_path, 'r', encoding='utf-8') as f:
                outline_template = f.read()
        # Prepare data API description and available analysis info
        data_api_description = self.prompt_loader.get_prompt('data_api_outline')
        analysis_result_list = self._get_analysis_results()
        
        data_info = "You have access to the following analysis results:\n\n"
        for idx, result in enumerate(analysis_result_list):
            data_info += f"**Analysis Report ID {idx}:**\n{result.brief_str()}\n\n"
        data_info += "\nYou can retrieve detailed content using `get_analysis_result(analysis_id)` in your code.\n"
        
        initial_prompt = self.DRAFT_GENERATOR_PROMPT.format(
            task=input_data['task'],
            report_requirements=outline_template,
            data_api=data_api_description,
            data_info=data_info,
            max_iterations=max_iterations,
            target_language=self.target_language_name
        )
        return [{"role": "system", "content": initial_prompt}]

    async def generate_outline(
        self, 
        input_data, 
        max_iterations: int = 10,
        stop_words: list[str] = [],
        echo=False,
        resume: bool = True,
        checkpoint_name: str = 'outline_latest.pkl'
    ):
        """
        Generate the report outline via agentic workflow.

        Args:
            input_data: Dict containing task metadata.
            max_iterations: Maximum number of interaction rounds.

        Returns:
            Report object populated with outline sections.
        """
       
        # Prepare executor for outline generation
        await self._prepare_executor()

        self.logger.info(f"[Outline] Starting agentic outline generation (max {max_iterations} rounds)")
        
        # Create input data for outline generation
        outline_input_data = {
            'task': input_data['task'],
            'max_iterations': max_iterations
        }
        self.current_task_data = outline_input_data

        outline_result = await super().async_run(
            input_data=outline_input_data,
            max_iterations=max_iterations,
            stop_words=stop_words,
            echo=echo,
            resume=resume,
            checkpoint_name=checkpoint_name,
            prompt_function=self._prepare_outline_prompt,
        )
    
        outline_content = extract_markdown(outline_result['final_result'])
        
        return Report(outline_content) if outline_content else Report("# Error: Could not generate outline")



    async def async_run(
        self, 
        input_data: dict, 
        max_iterations: int = 10,
        stop_words: list[str] = [],
        echo=False,
        resume: bool = True,
        checkpoint_name: str = 'report_latest.pkl',
        enable_chart = True,
        add_introduction: bool = None,
        add_cover_page: bool = False,
        add_reference_section: bool = True
    ) -> dict:
        """
        Phase-based execution flow using _run_phases:
          1. outline: generate report outline
          2. sections: per-section drafting
          3. replace_images: replace image placeholders
          4. abstract_title: add abstract and title
          5. cover_page: add cover/basic data page
          6. references: add reference section
          7. render: render to docx/pdf
        """
        self.enable_chart = enable_chart
        input_data['max_iterations'] = max_iterations
        
        # Configure post-processing options from plugin flags (passed via run_kwargs)
        self.add_introduction = add_introduction if add_introduction is not None else True
        self.add_cover_page = add_cover_page
        self.add_reference_section = add_reference_section
        
        # Shared mutable state across phases
        self._rg_state: Dict[str, Any] = {
            'report': None,
            'section_index_done': 0,
        }

        # Determine resume phase
        start_from = None
        if resume:
            state = await self.load(checkpoint_name=checkpoint_name)
            if state is not None:
                self._load_persist_extra_state(state)
                if state.get('finished'):
                    restored_report = state.get('report_obj')
                    if restored_report:
                        self.logger.info("Report already completed, restoring from checkpoint")
                        return restored_report
                self._rg_state['report'] = state.get('report_obj')
                self._rg_state['section_index_done'] = state.get('section_index', 0)
                start_from = state.get('resume_phase')

        async def _phase_outline():
            self.logger.info("[Phase] Generating Report Outline")
            report = await self.generate_outline(
                input_data,
                max_iterations=max_iterations,
                stop_words=stop_words,
                echo=echo,
                resume=resume,
                checkpoint_name='outline_latest.pkl',
            )
            self._rg_state['report'] = report
            await self.save(
                state={
                    'resume_phase': 'sections',
                    'report_obj': report,
                    'input_data': input_data,
                    'enable_chart': self.enable_chart,
                },
                checkpoint_name=checkpoint_name,
            )
            self.logger.info(f"[Phase] Outline completed: sections={len(report.sections)}")

        async def _phase_sections():
            report = self._rg_state['report']
            start_index = self._rg_state.get('section_index_done', 0)
            self.logger.info(f"[Phase] Begin generating sections (start={start_index})")
            for idx, section in enumerate(report.sections):
                if idx < start_index:
                    continue
                section_input_data = input_data.copy()
                section_input_data['section_outline'] = section.outline
                self.logger.info(f"[Phase] Section {idx+1}/{len(report.sections)} start")
                await self._prepare_executor()
                section_result = await super(ReportGenerator, self).async_run(
                    input_data=section_input_data,
                    max_iterations=max_iterations,
                    stop_words=stop_words,
                    echo=echo,
                    resume=resume and idx == start_index,
                    checkpoint_name=f'section_{idx}.pkl',
                )
                draft_section = section_result['final_result']
                final_section = await self._final_polish(section_input_data, draft_section)
                section.set_content(final_section)
                self._rg_state['section_index_done'] = idx + 1
                await self.save(
                    state={
                        'resume_phase': 'sections',
                        'section_index': idx + 1,
                        'report_obj': report,
                        'input_data': input_data,
                    },
                    checkpoint_name=checkpoint_name,
                )
                self.logger.info(f"[Phase] Section {idx+1} done")
            await self.save(
                state={
                    'resume_phase': 'replace_images',
                    'section_index': len(report.sections),
                    'report_obj': report,
                    'input_data': input_data,
                },
                checkpoint_name=checkpoint_name,
            )
            self.logger.info("[Phase] All sections generated")

        async def _phase_replace_images():
            report = self._rg_state['report']
            self.logger.info("[Phase] Replace image paths")
            self._rg_state['report'] = await self._replace_image_path(report)
            await self.save(
                state={'resume_phase': 'abstract_title', 'report_obj': self._rg_state['report']},
                checkpoint_name=checkpoint_name,
            )

        async def _phase_abstract_title():
            report = self._rg_state['report']
            if self.add_introduction:
                self.logger.info("[Phase] Add abstract and title")
                report = await self._add_abstract(input_data, report)
            else:
                self.logger.info("[Phase] Generate title only (no introduction)")
                new_title = await self.llm.generate(
                    messages=[{
                        'role': 'user',
                        'content': self.TITLE_PROMPT.format(
                            target_language=self.target_language_name,
                            report_content=report.content,
                        )
                    }]
                )
                new_title = new_title.replace("#", "").strip()
                report._content = f"# {new_title}\n\n"
            self._rg_state['report'] = report
            await self.save(
                state={'resume_phase': 'cover_page', 'report_obj': report},
                checkpoint_name=checkpoint_name,
            )

        async def _phase_cover_page():
            report = self._rg_state['report']
            self.logger.info("[Phase] Add cover page")
            self._rg_state['report'] = await self._add_cover_page(input_data, report)
            await self.save(
                state={'resume_phase': 'references', 'report_obj': self._rg_state['report']},
                checkpoint_name=checkpoint_name,
            )

        async def _phase_references():
            report = self._rg_state['report']
            if self.add_reference_section:
                self.logger.info("[Phase] Add references")
                report = await self._add_reference(report)
            else:
                self.logger.info("[Phase] Skipping reference section")
            self._rg_state['report'] = report
            await self.save(
                state={'resume_phase': 'render', 'report_obj': report},
                checkpoint_name=checkpoint_name,
            )

        async def _phase_render():
            report = self._rg_state['report']
            self.logger.info("[Phase] Render to docx")
            working_dir = self.config.config['working_dir']
            md_path = os.path.join(working_dir, f'{report.title}.md')
            docx_path = os.path.join(working_dir, f'{report.title}.docx')
            content = report.content.replace("```markdown", "").replace("```", "")
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(content)
            media_dir = os.path.join(working_dir, "media")
            reference_doc = self.config.config['reference_doc_path']
            pandoc_cmd = [
                "pandoc", md_path, "-o", docx_path,
                "--standalone", "--toc", "--toc-depth=3",
                f"--resource-path={working_dir}",
                f"--reference-doc={reference_doc}",
            ]
            if os.path.exists(media_dir):
                pandoc_cmd.append(f"--extract-media={media_dir}")
            print(" ".join(pandoc_cmd))
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            subprocess.run(pandoc_cmd, check=True, capture_output=True, text=True, encoding='utf-8', env=env)
            if not os.path.exists(md_path) or os.path.getsize(md_path) == 0:
                self.logger.error(
                    f"Report output is empty: {md_path}. "
                    "This usually means sections produced no content."
                )
            pdf_path = docx_path.replace(".docx", ".pdf")
            try:
                docx2pdf.convert(docx_path, pdf_path)
            except Exception as e:
                self.logger.error(f"Failed to convert docx to pdf: {e}", exc_info=True)
            await self.save(
                state={'finished': True, 'report_obj': report, 'rendered_md': md_path, 'rendered_docx': docx_path},
                checkpoint_name=checkpoint_name,
            )
            self.logger.info(f"[Phase] Render done: md={md_path}, docx={docx_path}")

        await self._run_phases(
            phases=[
                ('outline', _phase_outline),
                ('sections', _phase_sections),
                ('replace_images', _phase_replace_images),
                ('abstract_title', _phase_abstract_title),
                ('cover_page', _phase_cover_page),
                ('references', _phase_references),
                ('render', _phase_render),
            ],
            start_from=start_from,
        )

        return self._rg_state['report']
    
