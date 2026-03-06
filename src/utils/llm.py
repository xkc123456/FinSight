import asyncio
import logging
import random
from openai import OpenAI, AsyncOpenAI
from typing import List, Dict, Optional, Union, Any

logger = logging.getLogger(__name__)

_MAX_CONTEXT_TRIM_ATTEMPTS = 3


class LLM:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_name: str,
        generation_params: dict = None
    ):
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key
        )
        self.model_name = model_name
        self.generation_params = generation_params or {}
    
    def generate_embeddings(
        self, input_texts: List[str],
    ):
        response = self.client.embeddings.create(
            model=self.model_name,
            input=input_texts
        )
        return [embedding_data.embedding for embedding_data in response.data]


    def generate(
        self, 
        messages: List[Dict[str, str]], 
        **params
    ) -> Union[str, Any]:
        """Generate completion from messages."""
        if not (self.client and hasattr(self.client, 'chat') and hasattr(self.client.chat, 'completions')):
            raise NotImplementedError("Invalid sync client provided.")

        messages = list(messages)

        for trim_attempt in range(_MAX_CONTEXT_TRIM_ATTEMPTS + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                    **{**self.generation_params, **params}
                )
                if hasattr(response, 'choices'):
                    return response.choices[0].message.content
                return response

            except Exception as e:
                if "Error code: 400" in str(e) and trim_attempt < _MAX_CONTEXT_TRIM_ATTEMPTS:
                    logger.warning(
                        "Context window exceeded with %d messages (trim attempt %d/%d). "
                        "Removing the earliest non-system message.",
                        len(messages), trim_attempt + 1, _MAX_CONTEXT_TRIM_ATTEMPTS,
                    )
                    removed = False
                    for i, message in enumerate(messages):
                        if message["role"] in ("assistant", "user") and i > 0:
                            messages.pop(i)
                            removed = True
                            break
                    if not removed:
                        raise Exception(f"API call failed: {e}") from e
                    continue
                raise Exception(f"API call failed: {e}") from e


class AsyncLLM:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_name: Union[str, List[str]],
        generation_params: dict = None
    ):
        self.client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key
        )
        self.generation_params = generation_params or {}
        self.model_name = model_name
    
    async def generate_embeddings(
        self, input_texts: List[str],
    ):
        response = await self.client.embeddings.create(
            model=self.model_name,
            input=input_texts
        )
        return [embedding_data.embedding for embedding_data in response.data]

    async def generate(
        self, 
        messages: List[Dict[str, str]],
        max_retries_per_model: int = 5,
        include_stop_string=True,
        **params
    ) -> Union[str, Any]:
        if not (self.client and hasattr(self.client, 'chat') and hasattr(self.client.chat, 'completions')):
            raise NotImplementedError("Invalid async client provided.")

        last_exception = None
        messages = list(messages)
        context_trims = 0

        for attempt in range(max_retries_per_model):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                    **{**self.generation_params, **params}
                )
                if hasattr(response, 'choices') and response.choices:
                    output = response.choices[0].message.content
                else:
                    output = response
                try:
                    stop_reason = response.choices[0].provider_specific_fields['stop_reason']
                except Exception:
                    stop_reason = None
                if include_stop_string and stop_reason is not None:
                    output += stop_reason

                return output
            
            except Exception as e:
                last_exception = e
                error_str = str(e)
                logger.warning("Error in AsyncLLM.generate: %s", e)
                
                if "Error code: 400" in error_str:
                    if 'Invalid max_tokens value' in error_str:
                        self.generation_params['max_tokens'] = 8192
                        break

                    if context_trims >= _MAX_CONTEXT_TRIM_ATTEMPTS:
                        logger.warning("Max context trim attempts (%d) reached; giving up.", _MAX_CONTEXT_TRIM_ATTEMPTS)
                        break

                    logger.warning(
                        "Context length exceeded (%d messages). Removing earliest non-system message.",
                        len(messages),
                    )
                    removed = False
                    for i, message in enumerate(messages):
                        if i == 0:
                            continue
                        if message["role"] in ("user", "assistant"):
                            messages.pop(i)
                            removed = True
                            context_trims += 1
                            break
                    if not removed:
                        logger.warning("No removable message found; skipping further retries.")
                        break
                    continue
                
                base_delay = min(2 ** attempt, 32)
                jitter = random.uniform(0, base_delay * 0.5)
                delay = base_delay + jitter
                logger.info("Retrying in %.1fs (attempt %d/%d)", delay, attempt + 1, max_retries_per_model)
                await asyncio.sleep(delay)

        raise Exception(f"All model attempts failed after retries. Last error: {last_exception}")
