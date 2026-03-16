"""
Mini Agent: 使用 google/gemini-2.5-flash 的对话代理。
每个 MiniAgent 实例通过模型名称进行实例化，并独立维护上下文。
API 从 .env 读取（OPENROUTER_API_KEY / OPENROUTER_BASE_URL）。
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from openai import OpenAI
from rich.console import Console
from rich.markdown import Markdown

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.getenv(
    "OPENROUTER_BASE_URL",
    "https://openrouter.ai/api/v1",
).strip()
DEFAULT_MODEL_ID = "google/gemini-2.5-flash"

TOKEN_COST_PATH = os.path.join(
    os.path.dirname(__file__),
    "token_cost.json",
)


def _create_client() -> OpenAI:
    return OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
    )


class MiniAgent:

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.client = _create_client()
        self._context: List[Dict[str, str]] = []
        # 实例维度累计的输入和输出 token 数量
        self.total_input: int = 0
        self.total_output: int = 0

    def get_context(self) -> List[Dict[str, str]]:
        return list(self._context)

    def clear_context(self) -> None:
        self._context.clear()

    def set_system_prompt(self, prompt: Optional[str]) -> None:
        """
        仅影响之后的新对话，当前历史中的 system 不会被自动替换。
        """
        self._context = [
            m for m in self._context if m.get("role") != "system"
        ]
        if prompt is not None and prompt.strip():
            self._context.insert(
                0,
                {"role": "system", "content": prompt.strip()},
            )

    # --- token 统计与成本估算 ---
    def _extract_usage_from_response(self, resp: Any) -> Tuple[int, int]:
        usage = getattr(resp, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)

        return int(prompt_tokens), int(completion_tokens)

    @classmethod
    def get_cost(
        cls,
        model_name: str,
        total_input: int,
        total_output: int,
    ) -> float:
        with open(TOKEN_COST_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp)

        models = data.get("models", [])
        record: Optional[Dict[str, Any]] = next(
            (m for m in models if m.get("model_name") == model_name),
            None,
        )
        if record is None:
            raise ValueError(
                f"Cost configuration for model '{model_name}' "
                f"not found in token_cost.json."
            )

        input_price = float(record.get("input_price", 0.0))
        output_price = float(record.get("output_price", 0.0))

        cost = (
            total_input * input_price
            + total_output * output_price
        ) / 1_000_000.0
        return cost

    def chat(
        self,
        user_message: str,
        *,
        system_prompt: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> str:
        if system_prompt is not None:
            self.set_system_prompt(system_prompt)

        self._context.append(
            {"role": "user", "content": user_message.strip()}
        )

        resp = self.client.chat.completions.create(
            model=self.model_id,
            messages=self._context,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        # 累计 token 使用量
        input_tokens, output_tokens = self._extract_usage_from_response(resp)
        self.total_input += input_tokens
        self.total_output += output_tokens

        choice = resp.choices[0]
        content = choice.message.content.strip()
        self._context.append({"role": "assistant", "content": content})
        return content

    def run_continuously(self, system_prompt: Optional[str] = None) -> None:
        if system_prompt is not None:
            self.set_system_prompt(system_prompt)

        console = Console()
        console.print(
            f"[bold]Mini Agent ({self.model_id})[/bold]，输入 [bold]quit/exit[/bold] 退出。"
        )
        while True:
            line = input("You: ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                console.print("[bold]Bye.[/bold]")
                break
            reply = self.chat(line, system_prompt=system_prompt)
            console.print(Markdown(reply))

        # 交互结束后输出统计信息与估算花费
        try:
            total_cost = self.get_cost(
                self.model_id,
                self.total_input,
                self.total_output,
            )
            console.print(
                f"[bold]Total input tokens:[/bold] {self.total_input}\n"
                f"[bold]Total output tokens:[/bold] {self.total_output}\n"
                f"[bold]Cost (USD):[/bold] {total_cost:.6f}"
            )
        except Exception as exc:
            console.print(
                "[bold red]Failed to compute cost:[/bold red] "
                f"{exc}"
            )


def create_default_agent() -> MiniAgent:
    return MiniAgent(DEFAULT_MODEL_ID)


if __name__ == "__main__":
    agent = create_default_agent()
    agent.run_continuously()
