from __future__ import annotations

import json
import re
from typing import Any

from openai import AsyncOpenAI

from .config import env


class LLMError(RuntimeError):
    pass


class LLMClient:
    """对 OpenAI 协议的统一封装。

    DeepSeek / Claude(OpenAI 兼容端点) / OpenAI 都走这一套，
    换供应商只需改 .env 里的 base_url 和 model。
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        llm = cfg.get("llm", {})
        api_key = env("LLM_API_KEY")
        if not api_key:
            raise LLMError("缺少 LLM_API_KEY，请在 .env 中配置")

        self.provider = llm.get("provider", "deepseek")
        self.base_url = env("LLM_BASE_URL", llm.get("base_url", "https://api.deepseek.com"))
        self.model = env("LLM_MODEL", llm.get("model", "deepseek-chat"))
        self.reasoning_model = env("LLM_REASONING_MODEL", llm.get("reasoning_model")) or self.model
        self.temperature = float(llm.get("temperature", 0.3))
        self.max_tokens = int(llm.get("max_tokens", 4000))
        self.price_in = float(llm.get("price_input_per_m", 2.0))
        self.price_out = float(llm.get("price_output_per_m", 3.0))

        self.client = AsyncOpenAI(api_key=api_key, base_url=self.base_url)
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    # ------------------------------------------------------------ 底层调用

    async def _chat(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        json_mode: bool = True,
    ) -> tuple[str, str | None]:
        """返回 (正文, finish_reason)。

        finish_reason == "length" 表示输出被 max_tokens 截断，
        此时 JSON 一定是残缺的，需要加大预算重试，而不是原样重试。
        """
        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = await self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            # 部分兼容端点不支持 response_format，降级重试一次
            if json_mode:
                kwargs.pop("response_format", None)
                resp = await self.client.chat.completions.create(**kwargs)
            else:
                raise LLMError(f"LLM 调用失败: {exc}") from exc

        self.usage["calls"] += 1
        if getattr(resp, "usage", None):
            self.usage["prompt_tokens"] += resp.usage.prompt_tokens or 0
            self.usage["completion_tokens"] += resp.usage.completion_tokens or 0

        choice = resp.choices[0]
        return choice.message.content or "", getattr(choice, "finish_reason", None)

    # ------------------------------------------------------------ JSON 解析

    @staticmethod
    def _salvage(text: str) -> str | None:
        """修复被截断的 JSON。

        两种情况都要处理：
        1) 停在字符串中间 -> 补引号再闭合括号
        2) 停在元素中间 -> 回退到最后一个完整元素再闭合
        """
        stack: list[str] = []
        in_str = False
        esc = False
        safe: list[tuple[int, list[str]]] = []

        for i, ch in enumerate(text):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]":
                if stack:
                    stack.pop()
                safe.append((i, list(stack)))
            elif ch == "," and stack:
                safe.append((i - 1, list(stack)))

        # 方案 A：补全结尾未闭合的字符串，再闭合所有括号
        candidate = text + ('"' if in_str else "") + "".join(reversed(stack))
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

        # 方案 B：回退到最后一个完整元素
        for idx, close in reversed(safe[-30:]):
            candidate = text[: idx + 1] + "".join(reversed(close))
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _parse_json(text: str) -> dict:
        text = text.strip()
        # 去掉 ```json ``` 包裹
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if fence:
            text = fence.group(1).strip()

        start, end = text.find("{"), text.rfind("}")
        candidates = [text]
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])

        for candidate in candidates:
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue

        # 最后尝试修复截断的 JSON（例如 max_tokens 耗尽）
        for candidate in candidates:
            repaired = LLMClient._salvage(candidate)
            if repaired:
                print("    [llm] 输出被截断，已修复为合法 JSON")
                return json.loads(repaired)

        raise LLMError(f"无法解析 JSON: {text[:200]}")

    async def json_call(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
    ) -> dict:
        budget = max_tokens or self.max_tokens
        last_exc: Exception | None = None

        for attempt in range(retries + 1):
            try:
                text, finish = await self._chat(system, user, model=model, max_tokens=budget)
                if finish == "length":
                    # 被截断：不是模型的问题，加大预算重试
                    raise LLMError(f"输出被 max_tokens={budget} 截断")
                return self._parse_json(text)
            except LLMError as exc:
                last_exc = exc
                print(f"    [llm] 第 {attempt + 1} 次失败: {exc}")
                budget = min(int(budget * 1.8) + 200, 8000)
            except Exception as exc:
                last_exc = exc
                print(f"    [llm] 第 {attempt + 1} 次失败: {exc}")

        raise LLMError(f"LLM 多次调用失败: {last_exc}")

    # ------------------------------------------------------------ 成本

    def cost_cny(self) -> float:
        return (
            self.usage["prompt_tokens"] / 1_000_000 * self.price_in
            + self.usage["completion_tokens"] / 1_000_000 * self.price_out
        )

    def usage_summary(self) -> str:
        u = self.usage
        return (
            f"LLM 调用 {u['calls']} 次 ｜ "
            f"输入 {u['prompt_tokens']:,} tok ｜ "
            f"输出 {u['completion_tokens']:,} tok ｜ "
            f"约 ¥{self.cost_cny():.4f}"
        )
