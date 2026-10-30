# Modified for FinSight Pro on 2026-10-30; distributed under GNU GPL v3.
from typing import List, Dict, Any, Tuple
import asyncio
import os
import re
import copy
import subprocess
import numpy as np
import docx2pdf
from src.agents.base_agent import BaseAgent
from src.agents import DeepSearchAgent
from src.tools.web.web_crawler import ClickResult
from src.tools import ToolResult, get_tool_categories, get_tool_by_name
from src.agents.report_generator.report_class import Report, Section
from src.utils.helper import extract_markdown, get_md_img
from src.utils.index_builder import IndexBuilder
from src.utils.figure_helper import draw_kline_chart
from src.utils.pro_quality import audit_markdown, write_audit
class ReportGenerator(BaseAgent):
    AGENT_NAME = 'report_generator'
    AGENT_DESCRIPTION = 'a agent that can generate report from the data'
    NECESSARY_KEYS = ['task']
    def __init__(self, config, tools = [], use_llm_name: str = "deepseek-chat", use_embedding_name: str = "qwen3-embedding", enable_code = True, memory = None, agent_id: str = None):
        super().__init__(config=config, tools=tools, use_llm_name=use_llm_name, enable_code=enable_code, memory=memory, agent_id=agent_id)
        from src.utils.prompt_loader import get_prompt_loader
        target_language = self.config.config.get('language', 'zh')
        language_mapping = {'zh': 'Chinese (中文)', 'en': 'English'}
        self.target_language_name = language_mapping.get(target_language, target_language)
        target_type = self.config.config.get('target_type', 'general')
        self.prompt_loader = get_prompt_loader('report_generator', report_type=target_type)
        self.SECTION_WRITING_PROMPT = self.prompt_loader.get_prompt('section_writing')
        self.SECTION_WRITING_WO_CHART_PROMPT = self.prompt_loader.get_prompt('section_writing_wo_chart')
        self.FINAL_POLISH_PROMPT = self.prompt_loader.get_prompt('final_polish')
        self.DRAFT_GENERATOR_PROMPT = self.prompt_loader.get_prompt('outline_draft')
        self.CRITIQUE_PROMPT = self.prompt_loader.get_prompt('outline_critique')
        self.REFINEMENT_PROMPT = self.prompt_loader.get_prompt('outline_refinement')
        self.TITLE_PROMPT = self.prompt_loader.get_prompt('title_generation')
        self.ABSTRACT_PROMPT = self.prompt_loader.get_prompt('abstract')
        self.TABLE_BEAUTIFY_PROMPT = self.prompt_loader.get_prompt('table_beautify')
        self.use_embedding_name = use_embedding_name
        self._phase: str = 'outline'
        self._section_index_done: int = 0
        self._post_stage: int = 0

    def _set_default_tools(self):
        tool_list = [DeepSearchAgent(config=self.config, use_llm_name=self.use_llm_name, memory=self.memory)]
        for tool in tool_list:
            self.memory.add_dependency(tool.id, self.id)
        self.tools = tool_list

    async def _prepare_executor(self):
        current_task_data = self.current_task_data
        tool_list = self.tools
        collect_data_list = self.memory.get_collect_data(exclude_type=['search', 'click'])
        analysis_result_list = self.memory.get_analysis_result()
        def _get_data(data_id: int):
            if 0 <= data_id < len(collect_data_list):
                return collect_data_list[data_id].data
            raise ValueError(f"Invalid data_id: {data_id}. Available range: 0-{len(collect_data_list)-1}")
        def _get_analysis_result(data_id: int):
            if 0 <= data_id < len(analysis_result_list):
                return str(analysis_result_list[data_id])[:3000]
            raise ValueError(f"Invalid data_id: {data_id}. Available range: 0-{len(analysis_result_list)-1}")
        def _get_deepsearch_result(query: str):
            from src.utils.async_bridge import get_async_bridge
            bridge = get_async_bridge()
            output = bridge.run_async(tool_list[0].async_run(input_data={'task': current_task_data.get('task', ''), 'query': query}))
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
        data_api_description = self.prompt_loader.get_prompt('data_api')
        collect_data_list = self.memory.get_collect_data(exclude_type=['search', 'click'])
        analysis_result_list = self.memory.get_analysis_result()
        data_info = "\n\n## Available Datas\n\n"
        for idx, item in enumerate(collect_data_list):
            data_info += f"**Data ID {idx}:**\n{item.brief_str()}\n\n"
        data_info += "\nYou can access these datasets using `get_data(data_id)` in your code.\n"
        data_info += "\n\n## Available Analysis Reports\n\n"
        for idx, item in enumerate(analysis_result_list):
            data_info += f"**Analysis Report ID {idx}:**\n{item.brief_str()}\n\n"
        data_info += "\nYou can access these analysis reports using `get_analysis_result(analysis_result_id)` in your code.\n"
        prompt = self.SECTION_WRITING_PROMPT if self.enable_chart else self.SECTION_WRITING_WO_CHART_PROMPT
        return [{"role": "user", "content": prompt.format(task=task, report_theme=input_data.get('task'), section_description=section_outline, data_api=data_api_description, data_info=data_info, max_iterations=max_iterations, target_language=self.target_language_name)}]

    async def _handle_search_action(self, action_content: str):
        search_result = await self.tools[0].async_run(input_data={'query': action_content})
        return {'action': 'search', 'action_content': action_content, 'result': search_result['final_result'], 'continue': True}

    async def _handle_report_action(self, action_content: str):
        return {"action": "report", "action_content": action_content, "result": action_content, "continue": False}

    async def _handle_outline_action(self, action_content: str):
        return {"action": "outline", "action_content": action_content, "result": action_content, "continue": False}

    async def _handle_draft_action(self, action_content: str):
        return {"action": "draft", "action_content": action_content, "result": action_content, "continue": False}

    async def _final_polish(self, section_input_data, draft_section: str):
        all_analysis_result = self.memory.get_analysis_result()
        all_image_list = []
        for analysis_result in all_analysis_result:
            all_image_list.extend(analysis_result.get_all_img())
        final_prompt = self.FINAL_POLISH_PROMPT.format(draft_report=draft_section, reference_image='\n'.join(all_image_list), target_language=self.target_language_name)
        output = await self.llm.generate(messages=[{"role": "user", "content": final_prompt}])
        return extract_markdown(output)

    async def _pro_global_edit(self, report):
        """One whole-document pass before layout; section writers cannot do this."""
        prompt = f"""你是金融机构的终审编辑，不是扩写器。请对以下完整公司研报做一次全局统稿。
任务：
1. 统一投资评级、基准日、股价、股本、预测年份、目标价与风险结论；若证据冲突，删除无依据结论，不得编造新事实。
2. 区分已披露历史数据、数据库数据和分析师预测。预测必须写出收入、归母净利润及关键假设；不得把合并净利润当归母净利润。
3. 检查所有估值等式与单位。若报告给评级，必须有可复算的目标价逻辑和正常化年份交叉验证；基本面好不等于估值便宜。
4. 删除占位符、空图叙述、重复段落和写作过程痕迹。不得声称不存在的图展示了结论。
5. 保留原有 Markdown 标题、有效 [Source:...] 引文和 @import 图片占位，不新增来源，不修改有依据的数字。
只返回统稿后的完整 Markdown 正文，不要解释，不要代码围栏。

完整研报：
{report.content}
"""
        revised = extract_markdown(await self.llm.generate(messages=[{'role':'user','content':prompt}]))
        if revised and len(revised.strip()) >= len(report.content.strip()) * 0.45:
            return Report(revised.strip())
        self.logger.warning("Pro global edit returned an unexpectedly short report; keeping original")
        return report

    async def _replace_image_path(self, report):
        if not self.enable_chart:
            for section in report.sections:
                section._content = [re.sub(r'@import.*', '', p, flags=re.DOTALL) for p in section._content]
            return report
        def remove_suffix(name: str):
            return name.replace(".png", "").replace(".jpg", "").replace(".jpeg", "").replace(".md", "")
        def is_image_file(name: str):
            return name.endswith(".png") or name.endswith(".jpg") or name.endswith(".jpeg") or name.endswith(".md")
        all_analysis_result = self.memory.get_analysis_result()
        img_captions, img_paths = [], []
        for analysis_result in all_analysis_result:
            short2long, img_dicts = {}, {}
            for long_name, short_name in analysis_result.chart_name_mapping.items():
                short2long[remove_suffix(short_name)] = remove_suffix(long_name)
            for image_name in os.listdir(analysis_result.image_save_dir):
                if is_image_file(image_name):
                    img_path = os.path.join(analysis_result.image_save_dir, image_name)
                    long_image_name = short2long.get(remove_suffix(image_name), "")
                    if long_image_name != "":
                        img_dicts[long_image_name] = img_path
            img_captions.extend(list(img_dicts.keys()))
            img_paths.extend(list(img_dicts.values()))
        if len(img_captions) == 0:
            for section in report.sections:
                section._content = [re.sub(r'@import.*', '*[Chart not available — no chart images were generated during analysis]*', p, flags=re.DOTALL) for p in section._content]
            return report
        index = IndexBuilder(config=self.config, embedding_model=self.use_embedding_name, working_dir=self.working_dir)
        await index._build_index(img_captions)
        figure_idx = 1
        for section in report.sections:
            section_new_content = []
            for p_paragraph in section._content:
                for img_name in re.findall(r'@import.*', p_paragraph, flags=re.DOTALL):
                    most_similar_idx = (await index.search(img_name))[0]['id']
                    detect_img_name, detect_img_path = img_captions[most_similar_idx], img_paths[most_similar_idx]
                    if len(img_captions) == 1:
                        p_paragraph = p_paragraph.replace(img_name, "")
                        continue
                    del img_captions[most_similar_idx]
                    del img_paths[most_similar_idx]
                    await index._build_index(img_captions)
                    p_paragraph = p_paragraph.replace(img_name, get_md_img(detect_img_path, remove_suffix(os.path.basename(detect_img_path)), figure_idx))
                    figure_idx += 1
                section_new_content.append(p_paragraph)
            section._content = section_new_content
        return report

    async def _add_abstract(self, input_data, report):
        response_content = await self.llm.generate(messages=[{'role':'user','content':self.ABSTRACT_PROMPT.format(target_language=self.target_language_name, report_content=report.content)}])
        report.abstract = extract_markdown(response_content)
        new_title = await self.llm.generate(messages=[{'role':'user','content':self.TITLE_PROMPT.format(target_language=self.target_language_name, report_content=report.content)}])
        report._content = f"# {new_title.replace('#','').strip()}\n\n"
        return report

    async def _add_cover_page(self, input_data, report):
        if input_data.get('target_type', 'company') != 'company' or input_data.get('stock_code', '') == "":
            return report
        stock_code = input_data.get('stock_code', '')
        output_str = "\n\n## Company Fundamentals\n\n"
        collect_data_list = self.memory.get_collect_data()
        table_configs = [("Income statement", "Income Statement"), ("Balance sheet", "Balance Sheet"), ("Cash-flow statement", "Cash-Flow Statement"), ("Shareholding structure", "Shareholder Structure")]
        for keyword, display_name in table_configs:
            target_item_list = [item for item in collect_data_list if keyword in item.name and stock_code in item.name]
            if not target_item_list or target_item_list[0].data is None:
                continue
            table_data = target_item_list[0].data
            if keyword in ["Income statement", "Balance sheet", "Cash-flow statement"] and 'Category' in table_data.columns:
                table_data.rename(columns={'Category': 'Line item (RMB mn)'}, inplace=True)
            response = await self.llm.generate(messages=[{"role":"user","content":self.TABLE_BEAUTIFY_PROMPT.format(table_name=display_name, table_data=table_data.to_markdown(index=False))}])
            output_str += f'\n\n### {display_name}\n\n' + "\n".join([line for line in response.split("\n") if line.strip() != ""]) + '\n\n'
        try:
            target_item_list = [item for item in collect_data_list if 'candlestick' in item.name.lower() and stock_code in item.name]
            if target_item_list and target_item_list[0].data is not None:
                kline_data = target_item_list[0].data
                if isinstance(kline_data, list) and len(kline_data) == 1:
                    kline_data = kline_data[0]
                if 'date' not in kline_data.columns:
                    if '日期' in kline_data.columns:
                        kline_data.rename(columns={'日期':'date'}, inplace=True)
                    if '收盘' in kline_data.columns:
                        kline_data.rename(columns={'收盘':'close'}, inplace=True)
                fig_path = draw_kline_chart(kline_data, self.working_dir)
                output_str += f'\n\n### Share Price Trend\n\n![Trailing price performance]({fig_path})\n\n'
        except Exception as e:
            self.logger.error(f"Failed to draw price trend: {e}", exc_info=True)
        first_section = Section('Company Fundamentals', output_str)
        first_section.set_content(output_str)
        report.sections = [first_section] + report.sections
        return report

    async def _add_reference(self, report):
        collect_data_list = self.memory.get_collect_data()
        all_data = []
        for item in collect_data_list:
            name, content = item.name + '\n' + item.description, item.source
            if isinstance(item, ClickResult):
                title = self.memory.get_url_title(item.link) or item.name
                content = f"{title}\n{item.link}"
            if content not in [ii['content'] for ii in all_data]:
                all_data.append({'name':name, 'content':content})
        index = IndexBuilder(config=self.config, embedding_model=self.use_embedding_name, working_dir=self.working_dir)
        await index._build_index([item['name'] for item in all_data])
        total_cited_dict = {}
        for section in report.sections:
            section_new_content = []
            for p_paragraph in section._content:
                content = p_paragraph
                for match_item in re.findall(r'\[[Ss]ource[：:]\s*(.*?)\]', content):
                    search_result = await index.search(match_item, top_k=5)
                    score_list = np.array([item['score'] for item in search_result])
                    id_list = [item['id'] for item in search_result]
                    sorted_idx = np.argsort(score_list)[::-1]
                    score_list = np.exp(score_list) / np.sum(np.exp(score_list))
                    cite_list = []
                    for pos in sorted_idx:
                        pos = int(pos)
                        if score_list[pos] > 0.2 and len(cite_list) < 5:
                            cite_list.append(id_list[pos])
                    if not cite_list:
                        cite_list.append(id_list[sorted_idx[0]])
                    for idx in cite_list:
                        if idx not in total_cited_dict:
                            total_cited_dict[idx] = len(total_cited_dict) + 1
                    new_cites = [total_cited_dict[idx] for idx in cite_list]
                    content = re.sub(r'\[[Ss]ource[：:]\s*' + re.escape(match_item) + r'\]', f'[{",".join(str(i) for i in new_cites)}]', content)
                section_new_content.append(content)
            section._content = section_new_content
        reference_str = "## Reference Data Sources\n\n"
        for old_index, new_index in total_cited_dict.items():
            content = all_data[old_index]['content'].replace("\n", " ").replace("[PDF]", "")
            reference_str += f"{new_index}. {content}\n"
        new_section = Section('Reference Data Sources', reference_str)
        new_section.set_content(reference_str)
        report.sections.append(new_section)
        return report

    async def post_process_report(self, input_data, report):
        current_state = {'phase':'post_process', 'post_stage':self._post_stage, 'report_obj':report}
        if self._post_stage <= 0:
            if self.config.config.get('pro_global_edit', True) and input_data.get('task_type') == 'company':
                report = await self._pro_global_edit(report)
                current_state['report_obj'] = report
            report = await self._replace_image_path(report)
            self._post_stage = 1
            current_state.update(report_obj_stage1=copy.deepcopy(report), report_obj=report, post_stage=1)
            await self.save(state=current_state, checkpoint_name='report_latest.pkl')
        if self._post_stage <= 1:
            if getattr(self, 'add_introduction', True):
                report = await self._add_abstract(input_data, report)
            else:
                new_title = await self.llm.generate(messages=[{'role':'user','content':self.TITLE_PROMPT.format(target_language=self.target_language_name, report_content=report.content)}])
                report._content = f"# {new_title.replace('#','').strip()}\n\n"
            self._post_stage = 2
            current_state.update(report_obj_stage2=copy.deepcopy(report), report_obj=report, post_stage=2)
            await self.save(state=current_state, checkpoint_name='report_latest.pkl')
        if self._post_stage <= 2:
            report = await self._add_cover_page(input_data, report)
            self._post_stage = 3
            current_state.update(report_obj_stage3=copy.deepcopy(report), report_obj=report, post_stage=3)
            await self.save(state=current_state, checkpoint_name='report_latest.pkl')
        if self._post_stage <= 3:
            if getattr(self, 'add_reference_section', True):
                report = await self._add_reference(report)
            self._post_stage = 4
            current_state.update(report_obj_stage4=copy.deepcopy(report), report_obj=report, post_stage=4)
            await self.save(state=current_state, checkpoint_name='report_latest.pkl')
        if self._post_stage <= 4:
            working_dir = self.config.config['working_dir']
            md_path = os.path.join(working_dir, f'{report.title}.md')
            docx_path = os.path.join(working_dir, f'{report.title}.docx')
            content = report.content.replace("```markdown", "").replace("```", "")
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(content)
            if self.config.config.get('pro_quality_gate', True):
                issues = audit_markdown(content, base_dir=working_dir, require_investment_case=input_data.get('task_type') == 'company')
                audit_path = os.path.join(working_dir, 'pro_quality_audit.json')
                audit = write_audit(issues, audit_path, md_path)
                self.logger.info(f"[Pro] quality gate: {audit['errors']} errors, {audit['warnings']} warnings; {audit_path}")
                if not audit['passed']:
                    details = '\n'.join(f"- {i.code}: {i.message}" for i in issues if i.severity == 'error')
                    raise RuntimeError(f"FinSight Pro blocked export. Fix the report, then rerun from the checkpoint.\n{details}")
            media_dir = os.path.join(working_dir, "media")
            pandoc_cmd = ["pandoc", md_path, "-o", docx_path, "--standalone", "--toc", "--toc-depth=3", f"--resource-path={working_dir}", f"--reference-doc={self.config.config['reference_doc_path']}"]
            if os.path.exists(media_dir):
                pandoc_cmd.append(f"--extract-media={media_dir}")
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            subprocess.run(pandoc_cmd, check=True, capture_output=True, text=True, encoding='utf-8', env=env)
            if not os.path.exists(md_path) or os.path.getsize(md_path) == 0:
                self.logger.error(f"Report output is empty: {md_path}.")
            pdf_path = docx_path.replace(".docx", ".pdf")
            try:
                docx2pdf.convert(docx_path, pdf_path)
            except Exception as e:
                self.logger.error(f"Failed to convert docx to pdf: {e}", exc_info=True)
            self._post_stage = 5
            current_state.update(rendered_md=md_path, rendered_docx=docx_path, finished=True)
            await self.save(state=current_state, checkpoint_name='report_latest.pkl')
        return report

    def _get_persist_extra_state(self) -> Dict[str, Any]:
        return {'phase':getattr(self,'_phase','outline'), 'section_index':getattr(self,'_section_index_done',0), 'post_stage':getattr(self,'_post_stage',0)}

    def _load_persist_extra_state(self, state: Dict[str, Any]):
        extra = state.get('extra', {})
        phase = extra.get('phase') or state.get('phase')
        if isinstance(phase, str):
            self._phase = phase
        section_index = extra.get('section_index') or state.get('section_index')
        if section_index is not None:
            self._section_index_done = int(section_index)
        post_stage = extra.get('post_stage') or state.get('post_stage')
        if post_stage is not None:
            self._post_stage = int(post_stage)
        enable_chart = extra.get('enable_chart') or state.get('enable_chart')
        self.enable_chart = bool(enable_chart) if enable_chart is not None else True

    async def _prepare_outline_prompt(self, input_data):
        max_iterations = input_data.get('max_iterations', 10)
        outline_template_path = self.config.config.get('outline_template_path', None)
        outline_template = ""
        if outline_template_path and os.path.exists(outline_template_path):
            with open(outline_template_path, 'r', encoding='utf-8') as f:
                outline_template = f.read()
        data_api_description = self.prompt_loader.get_prompt('data_api_outline')
        analysis_result_list = self.memory.get_analysis_result()
        data_info = "You have access to the following analysis results:\n\n"
        for idx, result in enumerate(analysis_result_list):
            data_info += f"**Analysis Report ID {idx}:**\n{result.brief_str()}\n\n"
        data_info += "\nYou can retrieve detailed content using `get_analysis_result(analysis_id)` in your code.\n"
        initial_prompt = self.DRAFT_GENERATOR_PROMPT.format(task=input_data['task'], report_requirements=outline_template, data_api=data_api_description, data_info=data_info, max_iterations=max_iterations, target_language=self.target_language_name)
        return [{"role":"system","content":initial_prompt}]

    async def generate_outline(self, input_data, max_iterations: int = 10, stop_words: list[str] = [], echo=False, resume: bool = True, checkpoint_name: str = 'outline_latest.pkl'):
        await self._prepare_executor()
        outline_input_data = {'task':input_data['task'], 'max_iterations':max_iterations}
        self.current_task_data = outline_input_data
        outline_result = await super().async_run(input_data=outline_input_data, max_iterations=max_iterations, stop_words=stop_words, echo=echo, resume=resume, checkpoint_name=checkpoint_name, prompt_function=self._prepare_outline_prompt)
        outline_content = extract_markdown(outline_result['final_result'])
        return Report(outline_content) if outline_content else Report("# Error: Could not generate outline")

    async def async_run(self, input_data: dict, max_iterations: int = 10, stop_words: list[str] = [], echo=False, resume: bool = True, checkpoint_name: str = 'report_latest.pkl', enable_chart = True, add_introduction: bool = None, add_reference_section: bool = True) -> dict:
        report, start_index = None, 0
        self.enable_chart = enable_chart
        input_data['max_iterations'] = max_iterations
        target_type = self.config.config.get('target_type', 'general')
        self.add_introduction = target_type not in ['general'] if add_introduction is None else add_introduction
        self.add_reference_section = add_reference_section
        if resume:
            state = await self.load(checkpoint_name=checkpoint_name)
            if state is not None:
                self._load_persist_extra_state(state)
                if state.get('finished') and state.get('report_obj'):
                    return state.get('report_obj')
                if state.get('report_obj') is not None:
                    report = state.get('report_obj')
                    start_index = self._section_index_done
        if self._phase == 'outline' or report is None:
            report = await self.generate_outline(input_data, max_iterations=max_iterations, stop_words=stop_words, echo=echo, resume=resume, checkpoint_name='outline_latest.pkl')
            self._phase = 'sections'
            await self.save(state={'phase':self._phase,'report_obj':report,'input_data':input_data,'enable_chart':self.enable_chart}, checkpoint_name=checkpoint_name)
            self.memory.save()
        if self._phase == 'sections':
            for idx, section in enumerate(report.sections):
                if idx < start_index:
                    continue
                section_input_data = input_data.copy()
                section_input_data['section_outline'] = section.outline
                await self._prepare_executor()
                section_result = await super().async_run(input_data=section_input_data, max_iterations=max_iterations, stop_words=stop_words, echo=echo, resume=resume and idx == start_index, checkpoint_name=f'section_{idx}.pkl')
                final_section = await self._final_polish(section_input_data, section_result['final_result'])
                self.memory.add_log(id=self.id, type=self.type, input_data=section_input_data, output_data=section_result, error=False, note="Report generator executed successfully")
                section.set_content(final_section)
                await self.save(state={'phase':'sections','section_index':idx+1,'report_obj':report,'input_data':input_data}, checkpoint_name=checkpoint_name)
                self.memory.save()
                self._section_index_done = idx + 1
            self._phase = 'post_process'
            await self.save(state={'phase':self._phase,'section_index':self._section_index_done,'post_stage':self._post_stage,'report_obj':report,'input_data':input_data}, checkpoint_name=checkpoint_name)
            self.memory.save()
        if self._phase == 'post_process':
            report = await self.post_process_report(input_data, report)
            self.memory.save()
        return report
