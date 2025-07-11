import re
import json
import os
import hashlib
import logging
from abc import ABC, abstractmethod
from typing import Iterator, AsyncIterator, Literal, overload

from promptbuilder.llm_client.types import Response, Content, Part, Tool, ToolConfig, FunctionCall, FunctionCallingConfig, Json, ThinkingConfig, ApiKey, PydanticStructure, ResultType, FinishReason
import promptbuilder.llm_client.utils as utils
import promptbuilder.llm_client.logfire_decorators as logfire_decorators
from promptbuilder.llm_client.config import GLOBAL_CONFIG


logger = logging.getLogger(__name__)

class BaseLLMClient(ABC, utils.InheritDecoratorsMixin):
    provider: str
    
    def __init__(
        self,
        provider: str,
        model: str,
        decorator_configs: utils.DecoratorConfigs | None = None,
        default_thinking_config: ThinkingConfig | None = None,
        default_max_tokens: int | None = None,
        **kwargs,
    ):
        self.provider = provider
        self.model = model
        
        if decorator_configs is None:
            if self.full_model_name in GLOBAL_CONFIG.default_decorator_configs:
                decorator_configs = GLOBAL_CONFIG.default_decorator_configs[self.full_model_name]
            else:
                decorator_configs = utils.DecoratorConfigs()
        self._decorator_configs = decorator_configs
        
        if default_thinking_config is None:
            if self.full_model_name in GLOBAL_CONFIG.default_thinking_configs:
                default_thinking_config = GLOBAL_CONFIG.default_thinking_configs[self.full_model_name]
        self.default_thinking_config = default_thinking_config
        
        if default_max_tokens is None:
            if self.full_model_name in GLOBAL_CONFIG.default_max_tokens:
                default_max_tokens = GLOBAL_CONFIG.default_max_tokens[self.full_model_name]
        self.default_max_tokens = default_max_tokens
    
    @property
    @abstractmethod
    def api_key(self) -> ApiKey:
        pass
    
    @property
    def full_model_name(self) -> str:
        """Return the model identifier"""
        return self.provider + ":" + self.model
    
    @staticmethod
    def as_json(text: str) -> Json:
        # Remove markdown code block formatting if present
        text = text.strip()
                
        code_block_pattern = r"```(?:json\s)?(.*)```"
        match = re.search(code_block_pattern, text, re.DOTALL)
        
        if match:
            # Use the content inside code blocks
            text = match.group(1).strip()

        try:
            return json.loads(text, strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse LLM response as JSON:\n{text}")

    @logfire_decorators.create
    @utils.retry_cls
    @utils.rpm_limit_cls
    @abstractmethod
    def create(
        self,
        messages: list[Content],
        result_type: ResultType = None,
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: list[Tool] | None = None,
        tool_config: ToolConfig = ToolConfig(),
    ) -> Response:
        pass
    
    @overload
    def create_value(
        self,
        messages: list[Content],
        result_type: None = None,
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: None = None,
        tool_choice_mode: Literal["NONE"] = "NONE",
    ) -> str: ...
    @overload
    def create_value(
        self,
        messages: list[Content],
        result_type: Literal["json"],
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: None = None,
        tool_choice_mode: Literal["NONE"] = "NONE",
    ) -> Json: ...
    @overload
    def create_value(
        self,
        messages: list[Content],
        result_type: type[PydanticStructure],
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: None = None,
        tool_choice_mode: Literal["NONE"] = "NONE",
    ) -> PydanticStructure: ...
    @overload
    def create_value(
        self,
        messages: list[Content],
        result_type: Literal["tools"],
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: list[Tool],
        tool_choice_mode: Literal["ANY"],
    ) -> list[FunctionCall]: ...

    def create_value(
        self,
        messages: list[Content],
        result_type: ResultType | Literal["tools"] = None,
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: list[Tool] | None = None,
        tool_choice_mode: Literal["ANY", "NONE"] = "NONE",
        autocomplete: bool = False,
    ):
        if result_type == "tools":
            response = self.create(
                messages=messages,
                result_type=None,
                thinking_config=thinking_config,
                system_message=system_message,
                max_tokens=max_tokens,
                tools=tools,
                tool_config=ToolConfig(function_calling_config=FunctionCallingConfig(mode=tool_choice_mode)),
            )
            functions: list[FunctionCall] = []
            for candidate in response.candidates:
                for part in candidate.content.parts:
                    if part.function_call is not None:
                        functions.append(part.function_call)
            return functions

        response = self.create(
            messages=messages,
            result_type=result_type,
            thinking_config=thinking_config,
            system_message=system_message,
            max_tokens=max_tokens,
            tools=tools,
            tool_config=ToolConfig(function_calling_config=FunctionCallingConfig(mode=tool_choice_mode)),
        )

        while autocomplete and response.candidates and response.candidates[0].finish_reason not in [FinishReason.STOP, FinishReason.MAX_TOKENS]:
            BaseLLMClient._append_generated_part(messages, response)

            response = self.create(
                messages=messages,
                result_type=result_type,
                thinking_config=thinking_config,
                system_message=system_message,
                max_tokens=max_tokens,
                tools=tools,
                tool_config=ToolConfig(function_calling_config=FunctionCallingConfig(mode=tool_choice_mode)),
            )

        if result_type is None:
            return response.text
        else:
            if result_type == "json":
                response.parsed = BaseLLMClient.as_json(response.text)
            return response.parsed
    

    @staticmethod
    def _append_generated_part(messages: list[Content], response: Response):
        assert(response.candidates and response.candidates[0].content), "Response must contain at least one candidate with content."

        text_parts = [
            part for part in response.candidates[0].content.parts if part.text is not None and not part.thought
        ] if response.candidates[0].content and response.candidates[0].content.parts else None
        if text_parts is not None and len(text_parts) > 0:
            response_text = "".join(part.text for part in text_parts)
            is_thought = False
        else:
            thought_parts = [
                part for part in response.candidates[0].content.parts if part.text and part.thought
            ] if response.candidates[0].content and response.candidates[0].content.parts else None
            if thought_parts is not None and len(thought_parts) > 0:
                response_text = "".join(part.text for part in thought_parts)
                is_thought = True
            else:
                raise ValueError("No text or thought found in the response parts.")
        
        if len(messages) > 0 and messages[-1].role == "model":
            message_to_append = messages[-1]
            if message_to_append.parts and message_to_append.parts[-1].text is not None and message_to_append.parts[-1].thought == is_thought:
                message_to_append.parts[-1].text += response_text
            else:
                if not message_to_append.parts:
                    message_to_append.parts = []
                message_to_append.parts.append(Part(text=response_text, thought=is_thought))
        else:
            messages.append(Content(parts=[Part(text=response_text, thought=is_thought)], role="model"))

    @logfire_decorators.create_stream
    @utils.retry_cls
    @utils.rpm_limit_cls
    def create_stream(
        self,
        messages: list[Content],
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[Response]:
        raise NotImplementedError
    
    @overload
    def from_text(
        self,
        prompt: str,
        result_type: None = None,
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: None = None,
        tool_choice_mode: Literal["NONE"] = "NONE",
    ) -> str: ...
    @overload
    def from_text(
        self,
        prompt: str,
        result_type: Literal["json"],
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: None = None,
        tool_choice_mode: Literal["NONE"] = "NONE",
    ) -> Json: ...
    @overload
    def from_text(
        self,
        prompt: str,
        result_type: type[PydanticStructure],
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: None = None,
        tool_choice_mode: Literal["NONE"] = "NONE",
    ) -> PydanticStructure: ...
    @overload
    def from_text(
        self,
        prompt: str,
        result_type: Literal["tools"],
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: list[Tool],
        tool_choice_mode: Literal["ANY"],
    ) -> list[FunctionCall]: ...
    
    def from_text(
        self,
        prompt: str,
        result_type: ResultType | Literal["tools"] = None,
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: list[Tool] | None = None,
        tool_choice_mode: Literal["ANY", "NONE"] = "NONE",
    ):
        return self.create_value(
            messages=[Content(parts=[Part(text=prompt)], role="user")],
            result_type=result_type,
            thinking_config=thinking_config,
            system_message=system_message,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice_mode=tool_choice_mode,
        )


class BaseLLMClientAsync(ABC, utils.InheritDecoratorsMixin):
    provider: str
    
    def __init__(
        self,
        provider: str,
        model: str,
        decorator_configs: utils.DecoratorConfigs | None = None,
        default_thinking_config: ThinkingConfig | None = None,
        default_max_tokens: int | None = None,
        **kwargs,
    ):
        self.provider = provider
        self.model = model
        
        if decorator_configs is None:
            if self.full_model_name in GLOBAL_CONFIG.default_decorator_configs:
                decorator_configs = GLOBAL_CONFIG.default_decorator_configs[self.full_model_name]
            else:
                decorator_configs = utils.DecoratorConfigs()
        self._decorator_configs = decorator_configs
        
        if default_thinking_config is None:
            if self.full_model_name in GLOBAL_CONFIG.default_thinking_configs:
                default_thinking_config = GLOBAL_CONFIG.default_thinking_configs[self.full_model_name]
        self.default_thinking_config = default_thinking_config
        
        if default_max_tokens is None:
            if self.full_model_name in GLOBAL_CONFIG.default_max_tokens:
                default_max_tokens = GLOBAL_CONFIG.default_max_tokens[self.full_model_name]
        self.default_max_tokens = default_max_tokens
    
    @property
    @abstractmethod
    def api_key(self) -> ApiKey:
        pass
    
    @property
    def full_model_name(self) -> str:
        """Return the model identifier"""
        return self.provider + ":" + self.model
    
    @logfire_decorators.create_async
    @utils.retry_cls_async
    @utils.rpm_limit_cls_async
    @abstractmethod
    async def create(
        self,
        messages: list[Content],
        result_type: ResultType = None,
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: list[Tool] | None = None,
        tool_config: ToolConfig = ToolConfig(),
    ) -> Response:
        pass
    
    @overload
    async def create_value(
        self,
        messages: list[Content],
        result_type: None = None,
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: None = None,
        tool_choice_mode: Literal["NONE"] = "NONE",
    ) -> str: ...
    @overload
    async def create_value(
        self,
        messages: list[Content],
        result_type: Literal["json"],
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: None = None,
        tool_choice_mode: Literal["NONE"] = "NONE",
    ) -> Json: ...
    @overload
    async def create_value(
        self,
        messages: list[Content],
        result_type: type[PydanticStructure],
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: None = None,
        tool_choice_mode: Literal["NONE"] = "NONE",
    ) -> PydanticStructure: ...
    @overload
    async def create_value(
        self,
        messages: list[Content],
        result_type: Literal["tools"],
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: list[Tool],
        tool_choice_mode: Literal["ANY"],
    ) -> list[FunctionCall]: ...

    async def create_value(
        self,
        messages: list[Content],
        result_type: ResultType | Literal["tools"] = None,
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: list[Tool] | None = None,
        tool_choice_mode: Literal["ANY", "NONE"] = "NONE",
        autocomplete: bool = False,
    ):
        if result_type == "tools":
            response = await self.create(
                messages=messages,
                result_type=None,
                thinking_config=thinking_config,
                system_message=system_message,
                max_tokens=max_tokens,
                tools=tools,
                tool_config=ToolConfig(function_calling_config=FunctionCallingConfig(mode=tool_choice_mode)),
            )
            functions: list[FunctionCall] = []
            for candidate in response.candidates:
                for part in candidate.content.parts:
                    if part.function_call is not None:
                        functions.append(part.function_call)
            return functions

        response = await self.create(
            messages=messages,
            result_type=result_type,
            thinking_config=thinking_config,
            system_message=system_message,
            max_tokens=max_tokens,
            tools=tools,
            tool_config=ToolConfig(function_calling_config=FunctionCallingConfig(mode=tool_choice_mode)),
        )

        if max_tokens is None:
            max_tokens = self.default_max_tokens

        while autocomplete and response.candidates and response.candidates[0].finish_reason not in [FinishReason.STOP, FinishReason.MAX_TOKENS]:
            BaseLLMClient._append_generated_part(messages, response)

            response = await self.create(
                messages=messages,
                result_type=result_type,
                thinking_config=thinking_config,
                system_message=system_message,
                max_tokens=max_tokens,
                tools=tools,
                tool_config=ToolConfig(function_calling_config=FunctionCallingConfig(mode=tool_choice_mode)),
            )

        if result_type is None:
            return response.text
        else:
            if result_type == "json":
                response.parsed = BaseLLMClient.as_json(response.text)
            return response.parsed
    
    @logfire_decorators.create_stream_async
    @utils.retry_cls_async
    @utils.rpm_limit_cls_async
    async def create_stream(
        self,
        messages: list[Content],
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[Response]:
        raise NotImplementedError
    
    @overload
    async def from_text(
        self,
        prompt: str,
        result_type: None = None,
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: None = None,
        tool_choice_mode: Literal["NONE"] = "NONE",
    ) -> str: ...
    @overload
    async def from_text(
        self,
        prompt: str,
        result_type: Literal["json"],
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: None = None,
        tool_choice_mode: Literal["NONE"] = "NONE",
    ) -> Json: ...
    @overload
    async def from_text(
        self,
        prompt: str,
        result_type: type[PydanticStructure],
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: None = None,
        tool_choice_mode: Literal["NONE"] = "NONE",
    ) -> PydanticStructure: ...
    @overload
    async def from_text(
        self,
        prompt: str,
        result_type: Literal["tools"],
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: list[Tool],
        tool_choice_mode: Literal["ANY"],
    ) -> list[FunctionCall]: ...
    
    async def from_text(
        self,
        prompt: str,
        result_type: ResultType | Literal["tools"] = None,
        *,
        thinking_config: ThinkingConfig | None = None,
        system_message: str | None = None,
        max_tokens: int | None = None,
        tools: list[Tool] | None = None,
        tool_choice_mode: Literal["ANY", "NONE"] = "NONE",
    ):
        return await self.create_value(
            messages=[Content(parts=[Part(text=prompt)], role="user")],
            result_type=result_type,
            thinking_config=thinking_config,
            system_message=system_message,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice_mode=tool_choice_mode,
        )


class CachedLLMClient(BaseLLMClient):
    @property
    def api_key(self) -> ApiKey:
        return self.llm_client.api_key

    def __init__(self, llm_client: BaseLLMClient, cache_dir: str = "data/llm_cache"):
        super().__init__(
            provider=llm_client.provider,
            model=llm_client.model,
            decorator_configs=llm_client._decorator_configs,
            default_thinking_config=llm_client.default_thinking_config,
            default_max_tokens=llm_client.default_max_tokens,
        )
        self.provider = llm_client.provider
        self.llm_client = llm_client
        self.cache_dir = cache_dir
    
    def create(self, messages: list[Content], **kwargs) -> Response:
        response, messages_dump, cache_path = CachedLLMClient.create_cached(self.llm_client, self.cache_dir, messages, **kwargs)
        if response is not None:
            return response
        response = self.llm_client.create(messages, **kwargs)
        CachedLLMClient.save_cache(cache_path, self.llm_client.full_model_name, messages_dump, response)
        return response

    @staticmethod
    def create_cached(llm_client: BaseLLMClient | BaseLLMClientAsync, cache_dir: str, messages: list[Content], **kwargs) -> tuple[Response | None, list[dict], str]:
        messages_dump = [message.model_dump() for message in messages]
        key = hashlib.sha256(
            json.dumps((llm_client.full_model_name, messages_dump)).encode()
        ).hexdigest()
        cache_path = os.path.join(cache_dir, f"{key}.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "rt") as f:
                    cache_data = json.load(f)
                    if cache_data["full_model_name"] == llm_client.full_model_name and json.dumps(cache_data["request"]) == json.dumps(messages_dump):
                        return Response(**cache_data["response"]), messages_dump, cache_path
                    else:
                        logger.debug(f"Cache mismatch for {key}")
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Invalid cache file {cache_path}: {str(e)}")
                # Continue to make API call if cache is invalid
        return None, messages_dump, cache_path
    
    @staticmethod
    def save_cache(cache_path: str, full_model_name: str, messages_dump: list[dict], response: Response):
        with open(cache_path, 'wt') as f:
            json.dump({"full_model_name": full_model_name, "request": messages_dump, "response": response.model_dump()}, f, indent=4)


class CachedLLMClientAsync(BaseLLMClientAsync):
    @property
    def api_key(self) -> ApiKey:
        return self.llm_client.api_key

    def __init__(self, llm_client: BaseLLMClientAsync, cache_dir: str = "data/llm_cache"):
        super().__init__(provider=llm_client.provider, model=llm_client.model, decorator_configs=llm_client._decorator_configs, default_max_tokens=llm_client.default_max_tokens)
        self.provider = llm_client.provider
        self.llm_client = llm_client
        self.cache_dir = cache_dir
    
    async def create(self, messages: list[Content], **kwargs) -> Response:
        response, messages_dump, cache_path = CachedLLMClient.create_cached(self.llm_client, self.cache_dir, messages, **kwargs)
        if response is not None:
            return response        
        response = await self.llm_client.create(messages, **kwargs)
        CachedLLMClient.save_cache(cache_path, self.llm_client.full_model_name, messages_dump, response)
        return response

