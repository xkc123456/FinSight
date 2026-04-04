import json
import os
import re
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from src.utils import AsyncLLM


# ------------------------------------------------------------------
# Pydantic schema — validates the merged config dict
# ------------------------------------------------------------------
class LLMGenerationParams(BaseModel):
    temperature: float = Field(0.5, ge=0.0, le=2.0)
    max_tokens: int = Field(1000, ge=1)
    top_p: float = Field(1.0, ge=0.0, le=1.0)

    model_config = {"extra": "allow"}


class LLMConfigItem(BaseModel):
    model_name: str
    api_key: str
    base_url: str
    generation_params: LLMGenerationParams = Field(default_factory=LLMGenerationParams)

    model_config = {"extra": "allow"}


class RateLimits(BaseModel):
    search_engines: float = 1.0
    financial_apis: float = 0.5
    fred_api: float = 0.5
    yfinance: float = 0.2

    model_config = {"extra": "allow"}


_VALID_TARGET_TYPES = {
    "financial_company", "financial_industry", "financial_macro",
    "general", "governance",
    # legacy aliases kept for backward-compat
    "company", "macro", "industry",
}

# Maps legacy short names to canonical plugin names.
_TARGET_TYPE_ALIASES: dict[str, str] = {
    "company": "financial_company",
    "macro": "financial_macro",
    "industry": "financial_industry",
}

_VALID_LANGUAGES = {"zh", "en"}


class ConfigSchema(BaseModel):
    """Validates the merged user + default configuration."""

    target_name: str = Field(min_length=1)
    stock_code: str = ""
    target_type: str
    market: str = "A"
    output_dir: str = "./outputs"
    language: str = "zh"
    save_note: Optional[str] = None
    reference_doc_path: Optional[str] = None
    outline_template_path: Optional[str] = None

    custom_collect_tasks: list[str] = Field(default_factory=list)
    custom_analysis_tasks: list[str] = Field(default_factory=list)

    use_collect_data_cache: bool = True
    use_analysis_cache: bool = True
    use_report_outline_cache: bool = True
    use_full_report_cache: bool = True
    use_post_process_cache: bool = True

    rate_limits: RateLimits = Field(default_factory=RateLimits)
    llm_config_list: list[LLMConfigItem] = Field(default_factory=list)

    model_config = {"extra": "allow"}

    @field_validator("target_type")
    @classmethod
    def _check_target_type(cls, v: str) -> str:
        if v not in _VALID_TARGET_TYPES:
            raise ValueError(
                f"target_type must be one of {sorted(_VALID_TARGET_TYPES)}, got {v!r}"
            )
        # Normalise legacy aliases to canonical plugin names.
        return _TARGET_TYPE_ALIASES.get(v, v)

    @field_validator("language")
    @classmethod
    def _check_language(cls, v: str) -> str:
        if v not in _VALID_LANGUAGES:
            raise ValueError(f"language must be one of {sorted(_VALID_LANGUAGES)}, got {v!r}")
        return v


class Config:
    def __init__(self, config_file_path=None, config_dict={}):
        # load default config
        current_path = os.path.dirname(os.path.realpath(__file__))
        default_file_path = os.path.join(current_path, "default_config.yaml")
        self.config = self._load_config(default_file_path)

        # load from file
        self.config_file_path = config_file_path
        if config_file_path is not None:
            file_config = self._load_config(config_file_path)
            self.config.update(file_config)
        
        # load from dict
        self.config.update(config_dict)

        # Validate merged config
        self._validated = ConfigSchema.model_validate(self.config)
        
        self._set_dirs()
        self._set_llms()
        self._set_rate_limiter()

    
    def _load_config(self, config_file_path):
        def build_yaml_loader():
            loader = yaml.FullLoader
            loader.add_implicit_resolver(
                "tag:yaml.org,2002:float",
                re.compile(
                    """^(?:
                [-+]?(?:[0-9][0-9_]*)\\.[0-9_]*(?:[eE][-+]?[0-9]+)?
                |[-+]?(?:[0-9][0-9_]*)(?:[eE][-+]?[0-9]+)
                |\\.[0-9_]+(?:[eE][-+][0-9]+)?
                |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\\.[0-9_]*
                |[-+]?\\.(?:inf|Inf|INF)
                |\\.(?:nan|NaN|NAN))$""",
                    re.X,
                ),
                list("-+0123456789."),
            )
            return loader
    
        def replace_env_vars(obj):
            """Recursively replace ${VAR_NAME} with environment variables"""
            if isinstance(obj, dict):
                return {key: replace_env_vars(value) for key, value in obj.items()}
            elif isinstance(obj, list):
                return [replace_env_vars(item) for item in obj]
            elif isinstance(obj, str):
                # Match ${VAR_NAME} pattern
                pattern = r'\$\{([^}]+)\}'
                matches = re.findall(pattern, obj)
                if matches:
                    result = obj
                    for var_name in matches:
                        env_value = os.getenv(var_name)
                        if env_value is None:
                            raise ValueError(f"Environment variable '{var_name}' is not set")
                        result = result.replace(f"${{{var_name}}}", env_value)
                    return result
                return obj
            else:
                return obj
    
        yaml_loader = build_yaml_loader()
        file_config = dict()
        if os.path.exists(config_file_path):
            if config_file_path.endswith('.yaml'):
                with open(config_file_path, "r", encoding="utf-8") as f:
                    file_config.update(yaml.load(f.read(), Loader=yaml_loader))
            elif config_file_path.endswith('.json'):
                with open(config_file_path, 'r') as f:
                    file_config.update(json.load(f))
            else:
                raise ValueError(f"Unsupported file type: {config_file_path}")
        else:
            raise ValueError(f"Config file not found: {config_file_path}")
        
        # Replace environment variables in the loaded config
        file_config = replace_env_vars(file_config)
        return file_config
    
    
    
    def _set_dirs(self):
        # convert output dir to absolute path
        output_dir = self.config.get('output_dir', './outputs')
        self.config['output_dir'] = output_dir
        target = self.config.get('target_name', 'unknown')
        save_note = self.config.get('save_note', None)
        target = target[:50]
        if save_note:
            target = str(save_note) + '_' + target
        self.working_dir = os.path.join(output_dir, target)
        self.config['working_dir'] = self.working_dir
        os.makedirs(self.working_dir, exist_ok=True)
        with open(os.path.join(self.working_dir, 'config.json'), 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=4, ensure_ascii=False)
        
    
    def _set_llms(self):
        llm_config_list = self.config.get('llm_config_list', [])
        llm_dict = {}
        for llm_config in llm_config_list:
            model_name = llm_config['model_name']
            llm = AsyncLLM(
                base_url=llm_config['base_url'],
                api_key=llm_config['api_key'],
                model_name=model_name,
                generation_params=llm_config.get('generation_params', {})
            )
            llm_dict[model_name] = llm
        self.llm_dict = llm_dict
            
    def _set_rate_limiter(self):
        """Initialize the global rate limiter from config."""
        from src.utils.rate_limiter import RateLimiter
        rate_limits = self.config.get('rate_limits', {})
        self.rate_limiter = RateLimiter(rate_limits)

    # ------------------------------------------------------------------
    # Default model-name helpers (centralises os.getenv fallback)
    # ------------------------------------------------------------------
    @property
    def default_llm_name(self) -> str:
        return self.config.get("default_llm_name", "") or os.getenv("DS_MODEL_NAME", "deepseek-chat")

    @property
    def default_vlm_name(self) -> str:
        return self.config.get("default_vlm_name", "") or os.getenv("VLM_MODEL_NAME", "qwen/qwen3-vl-235b-a22b-instruct")

    @property
    def default_embedding_name(self) -> str:
        return self.config.get("default_embedding_name", "") or os.getenv("EMBEDDING_MODEL_NAME", "qwen/qwen3-embedding-0.6b")

    def __str__(self):
        return str(self.config)
